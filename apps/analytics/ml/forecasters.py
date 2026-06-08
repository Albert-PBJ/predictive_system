"""Constructores de pronóstico — uno por objetivo.

Cada función devuelve un **diccionario uniforme** que el frontend consume igual para
todos los gráficos:

    {
      "target", "title", "subject", "unit", "value_kind",
      "model":   {key,label,library,r2,rmse,mae,n_train,n_holdout,trained_at,
                  hyperparameters,feature_importances},
      "history":  [{period,label,value, ...}],
      "forecast": [{period,label,value,lower,upper, ...}],
      "detail":   { "<period>": {kind, ...} },   # filas/features tras cada periodo
      "meta":     {...}                            # info extra del pronóstico
    }

El método compartido para las series mensuales es ``forecast_series``: construye una
matriz supervisada (calendario + rezagos + exógenas), mide el error con un *holdout*
temporal (backtest a un paso) y luego pronostica ``horizon`` meses de forma recursiva
con una banda de confianza (~90%) que se ensancha con el horizonte.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from django.utils import timezone
from sklearn.pipeline import Pipeline

from . import datasets
from .estimators import MODEL_META, feature_importances, make_classifier, make_regressor
from .features import add_period, calendar_features, period_label

Z90 = 1.645  # cuantil normal para una banda de ~90%

# Modelo asignado a cada objetivo (la UI lo fija; se puede sobreescribir por ?model=).
# Cada técnica exigida se usa donde MEJOR rinde (ver comparación en train_models):
#   - XGBoost      -> demanda por producto (mejor R²; panel global no lineal).
#   - Árbol decis. -> conversión de presupuestos (clasificador; mejor exactitud).
#   - Reg. lineal  -> ventas, utilidad, tasa de cambio y precio (series con tendencia;
#                     los árboles no extrapolan y hunden el R² en estas).
ASSIGNED_MODEL = {
    "demand": "xgboost",
    "sales": "linear",
    "profit": "linear",
    "exchange_rate": "linear",
    "product_price": "linear",
    "quote": "tree",
}


# --------------------------------------------------------------------------- #
# Utilidades internas
# --------------------------------------------------------------------------- #
def _metrics(y_true, y_pred) -> dict:
    yt = np.asarray(y_true, dtype=float)
    yp = np.asarray(y_pred, dtype=float)
    err = yt - yp
    rmse = float(np.sqrt(np.mean(err ** 2)))
    mae = float(np.mean(np.abs(err)))
    ss_res = float(np.sum(err ** 2))
    ss_tot = float(np.sum((yt - yt.mean()) ** 2))
    r2 = float(1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0
    return {"r2": round(r2, 4), "rmse": round(rmse, 4), "mae": round(mae, 4)}


def _hyperparams(model_key: str, estimator) -> dict:
    model = estimator.named_steps["model"] if isinstance(estimator, Pipeline) else estimator
    keys = {
        "linear": ["alpha"],
        "tree": ["max_depth", "min_samples_leaf"],
        "xgboost": ["n_estimators", "max_depth", "learning_rate"],
    }.get(model_key, [])
    out = {}
    for k in keys:
        v = getattr(model, k, None)
        out[k] = float(v) if isinstance(v, (int, float, np.floating)) else v
    return out


def _model_block(model_key, estimator, metrics, names, n_train, n_holdout) -> dict:
    meta = MODEL_META[model_key]
    return {
        "key": model_key,
        "label": meta["label"],
        "library": meta["library"],
        "r2": metrics["r2"] if metrics else None,
        "rmse": metrics["rmse"] if metrics else None,
        "mae": metrics["mae"] if metrics else None,
        "n_train": int(n_train),
        "n_holdout": int(n_holdout),
        "trained_at": timezone.now().isoformat(),
        "hyperparameters": _hyperparams(model_key, estimator),
        "feature_importances": feature_importances(estimator, names),
    }


def _design_row(period, t_norm, history, lags, seasonal_lag, exog_at):
    """Una fila de variables: calendario + rezagos + exógenas (en el orden de columnas)."""
    row = dict(calendar_features(period, t_norm))
    for lag in lags:
        row[f"lag{lag}"] = history[-lag]
    if seasonal_lag:
        row[f"lag{seasonal_lag}"] = history[-seasonal_lag] if len(history) >= seasonal_lag else history[0]
    if exog_at:
        row.update(exog_at)
    return row


def forecast_series(
    periods: list[str],
    values: list[float],
    *,
    model_key: str,
    horizon: int,
    log_target: bool = False,
    seasonal: bool = True,
    lags: tuple[int, ...] = (1, 2, 3),
    exog_hist: dict[str, list[float]] | None = None,
    exog_future: dict[str, list[float]] | None = None,
    nonneg: bool = True,
    round_int: bool = False,
) -> dict:
    """Ajusta un modelo a una serie mensual y pronostica ``horizon`` meses.

    Devuelve ``history`` (escala original), ``forecast`` (con banda), ``metrics``,
    el bloque ``model`` y la ``sigma`` de los residuos. No arma el ``detail`` (lo hace
    cada objetivo, que conoce las filas de origen).
    """
    n = len(values)
    y_model = [math.log1p(v) for v in values] if log_target else [float(v) for v in values]

    seasonal_lag = 12 if (seasonal and n >= 12 + 6) else 0
    max_lag = max(list(lags) + ([seasonal_lag] if seasonal_lag else []))
    denom = max(1, n - 1)

    # --- matriz supervisada (filas con todos los rezagos disponibles) ---
    feats, targets = [], []
    for i in range(max_lag, n):
        exog_at = {k: exog_hist[k][i] for k in exog_hist} if exog_hist else None
        feats.append(_design_row(periods[i], i / denom, y_model[: i], lags, seasonal_lag, exog_at))
        targets.append(y_model[i])
    if not feats:
        raise ValueError("Serie demasiado corta para construir variables de rezago.")
    X = pd.DataFrame(feats)
    names = list(X.columns)
    y_arr = np.asarray(targets, dtype=float)

    # --- holdout temporal (backtest a un paso) para métricas honestas ---
    n_rows = len(y_arr)
    n_holdout = min(12, max(0, n_rows - 5)) if n_rows >= 8 else 0
    metrics, sigma = None, None
    if n_holdout >= 3:
        Xtr, Xte = X.iloc[:-n_holdout], X.iloc[-n_holdout:]
        ytr, yte = y_arr[:-n_holdout], y_arr[-n_holdout:]
        bt = make_regressor(model_key)
        bt.fit(Xtr, ytr)
        pred = bt.predict(Xte)
        # métricas en escala original (interpretables)
        yte_o = np.expm1(yte) if log_target else yte
        pred_o = np.expm1(pred) if log_target else pred
        metrics = _metrics(yte_o, pred_o)
        sigma = float(np.std(yte - pred)) or None  # sigma en escala del modelo

    # --- ajuste final sobre toda la serie ---
    estimator = make_regressor(model_key)
    estimator.fit(X, y_arr)
    if sigma is None:
        resid = y_arr - estimator.predict(X)
        sigma = float(np.std(resid))
    sigma = max(sigma, 1e-6)

    # --- pronóstico recursivo ---
    hist = list(y_model)
    last_period = periods[-1]
    forecast = []
    for step in range(1, horizon + 1):
        p = add_period(last_period, step)
        t_norm = (n - 1 + step) / denom
        exog_at = {k: exog_future[k][step - 1] for k in exog_future} if exog_future else None
        row = _design_row(p, t_norm, hist, lags, seasonal_lag, exog_at)
        x = pd.DataFrame([row])[names]
        pred = float(estimator.predict(x)[0])
        hist.append(pred)

        band = Z90 * sigma * math.sqrt(step)
        if log_target:
            val, lo, hi = math.expm1(pred), math.expm1(pred - band), math.expm1(pred + band)
        else:
            val, lo, hi = pred, pred - band, pred + band
        if nonneg:
            val, lo, hi = max(val, 0.0), max(lo, 0.0), max(hi, 0.0)
        if round_int:
            val, lo, hi = round(val), round(lo), round(hi)
        forecast.append({
            "period": p, "label": period_label(p),
            "value": val, "lower": lo, "upper": hi,
            "features": {k: round(float(v), 4) for k, v in row.items()},
        })

    history = [
        {"period": p, "label": period_label(p), "value": (round(v) if round_int else round(float(v), 4))}
        for p, v in zip(periods, values)
    ]
    return {
        "history": history,
        "forecast": forecast,
        "model": _model_block(model_key, estimator, metrics, names, n_rows - n_holdout, n_holdout),
        "sigma": sigma,
    }


def _rate_shock_map() -> dict[str, float]:
    """Mapa periodo -> shock cambiario: devaluación mensual de la tasa paralela por
    encima de su norma reciente (3 meses). Captura las caídas de demanda asociadas a
    devaluaciones bruscas (relación que el seed dejó en los datos)."""
    rate = datasets.monthly_exchange_rate()
    if rate.empty:
        return {}
    s = rate["parallel_rate"]
    mom = s.pct_change().fillna(0.0)
    norm = mom.rolling(3, min_periods=1).mean().shift(1).fillna(0.0)
    shock = (mom - norm).clip(lower=0.0)
    return {p: float(v) for p, v in shock.items()}


def _rate_shock_series(periods: list[str]) -> list[float] | None:
    """Shock cambiario alineado a ``periods`` (0 donde no hay tasa)."""
    m = _rate_shock_map()
    if not m:
        return None
    return [m.get(p, 0.0) for p in periods]


# --------------------------------------------------------------------------- #
# 1) Ventas e ingresos
# --------------------------------------------------------------------------- #
def forecast_sales(metric: str = "revenue", horizon: int = 6, model: str | None = None) -> dict:
    df = datasets.monthly_company()
    if df.empty or len(df) < 8:
        return _empty("sales", "Pronóstico de ventas e ingresos")
    model_key = model or ASSIGNED_MODEL["sales"]
    periods = list(df.index)
    col, unit, kind, title = {
        "revenue": ("revenue", "USD", "usd", "Pronóstico de ingresos"),
        "count": ("n", "ventas", "int", "Pronóstico de número de ventas"),
    }.get(metric, ("revenue", "USD", "usd", "Pronóstico de ingresos"))
    values = [float(v) for v in df[col].tolist()]

    shock = _rate_shock_series(periods)
    exog_hist = {"shock_cambiario": shock} if shock else None
    exog_future = {"shock_cambiario": [0.0] * horizon} if shock else None

    res = forecast_series(
        periods, values, model_key=model_key, horizon=horizon,
        log_target=(metric == "revenue"), seasonal=True,
        exog_hist=exog_hist, exog_future=exog_future,
        nonneg=True, round_int=(metric == "count"),
    )
    detail = {}
    for p in periods:
        sales = datasets.sales_for_month(p)
        detail[p] = {
            "kind": "history",
            "columns": ["Fecha", "Cliente", "Vendedor", "Ingreso USD", "Utilidad USD"],
            "rows": [
                [s.sale_date.isoformat(), str(s.customer), str(s.seller),
                 float(s.total_sale_usd), float(s.total_profit_usd)]
                for s in sales
            ],
            "summary": {"ventas": len(sales),
                        "ingreso_usd": round(float(df.loc[p, "revenue"]), 2),
                        "utilidad_usd": round(float(df.loc[p, "profit"]), 2)},
        }
    _attach_forecast_detail(detail, res["forecast"])
    return _wrap("sales", title, None, unit, kind, res, detail,
                 meta={"metric": metric, "metrics_available": ["revenue", "count"]})


# --------------------------------------------------------------------------- #
# 2) Utilidad y margen
# --------------------------------------------------------------------------- #
def forecast_profit(horizon: int = 6, model: str | None = None) -> dict:
    df = datasets.monthly_company()
    if df.empty or len(df) < 8:
        return _empty("profit", "Pronóstico de utilidad y margen")
    model_key = model or ASSIGNED_MODEL["profit"]
    periods = list(df.index)
    values = [float(v) for v in df["profit"].tolist()]
    shock = _rate_shock_series(periods)
    res = forecast_series(
        periods, values, model_key=model_key, horizon=horizon, log_target=True, seasonal=True,
        exog_hist={"shock_cambiario": shock} if shock else None,
        exog_future={"shock_cambiario": [0.0] * horizon} if shock else None,
    )
    # margen % histórico (línea secundaria)
    for h in res["history"]:
        h["margin"] = round(float(df.loc[h["period"], "margin"]), 2)
    detail = {}
    for p in periods:
        detail[p] = {
            "kind": "history",
            "columns": ["Métrica", "Valor"],
            "rows": [
                ["Ingreso USD", round(float(df.loc[p, "revenue"]), 2)],
                ["Costo USD", round(float(df.loc[p, "cost"]), 2)],
                ["Utilidad USD", round(float(df.loc[p, "profit"]), 2)],
                ["Margen %", round(float(df.loc[p, "margin"]), 2)],
            ],
            "summary": {"utilidad_usd": round(float(df.loc[p, "profit"]), 2),
                        "margen_pct": round(float(df.loc[p, "margin"]), 2)},
        }
    _attach_forecast_detail(detail, res["forecast"])
    avg_margin = float(df["margin"].tail(12).mean())
    return _wrap("profit", "Pronóstico de utilidad y margen", None, "USD", "usd", res, detail,
                 meta={"avg_margin_pct": round(avg_margin, 2)})


# --------------------------------------------------------------------------- #
# 3) Tasa de cambio (BCV / paralela)
# --------------------------------------------------------------------------- #
def forecast_exchange_rate(rate: str = "bcv", horizon: int = 6, model: str | None = None) -> dict:
    df = datasets.monthly_exchange_rate()
    if df.empty or len(df) < 8:
        return _empty("exchange_rate", "Pronóstico de la tasa de cambio")
    model_key = model or ASSIGNED_MODEL["exchange_rate"]
    col = "bcv_rate" if rate == "bcv" else "parallel_rate"
    label = "BCV" if rate == "bcv" else "paralela"
    periods = list(df.index)
    values = [float(v) for v in df[col].tolist()]
    # La tasa crece de forma exponencial -> regresión lineal sobre log, sin estacionalidad.
    res = forecast_series(periods, values, model_key=model_key, horizon=horizon,
                          log_target=True, seasonal=False, nonneg=True)
    detail = {}
    for p in periods:
        recs = datasets.exchange_rate_for_month(p)
        detail[p] = {
            "kind": "history",
            "columns": ["Fecha", "BCV", "Paralela", "Fuente"],
            "rows": [[r.date.isoformat(), float(r.bcv_rate),
                      float(r.parallel_rate) if r.parallel_rate is not None else None,
                      r.get_source_display()] for r in recs],
            "summary": {"tasa": round(float(df.loc[p, col]), 4)},
        }
    _attach_forecast_detail(detail, res["forecast"])
    return _wrap("exchange_rate", f"Pronóstico de la tasa {label}", {"rate": rate},
                 "Bs/USD", "rate", res, detail, meta={"rate": rate, "rates_available": ["bcv", "parallel"]})


# --------------------------------------------------------------------------- #
# 4) Precio de producto
# --------------------------------------------------------------------------- #
def forecast_product_price(product_id: int, horizon: int = 6, model: str | None = None) -> dict:
    from apps.core.models import Product

    product = Product.objects.filter(id=product_id).first()
    if not product:
        return _empty("product_price", "Pronóstico de precio de producto")
    df = datasets.product_price_series(product_id)
    subject = {"product_id": product_id, "product_name": product.name, "sku": product.sku}
    if df.empty or len(df) < 6:
        return _empty("product_price", "Pronóstico de precio de producto", subject=subject)
    model_key = model or ASSIGNED_MODEL["product_price"]
    periods = list(df.index)
    values = [float(v) for v in df["sale_price_usd"].tolist()]
    res = forecast_series(periods, values, model_key=model_key, horizon=horizon,
                          log_target=True, seasonal=False, lags=(1, 2, 3), nonneg=True)

    # Precio en Bs derivado (USD pronosticado × tasa paralela pronosticada).
    rate_fc = forecast_exchange_rate("parallel", horizon=horizon)
    rate_by_period = {f["period"]: f["value"] for f in rate_fc.get("forecast", [])}
    latest_rate = None
    rate_df = datasets.monthly_exchange_rate()
    if not rate_df.empty:
        latest_rate = float(rate_df["parallel_rate"].iloc[-1])
    for f in res["forecast"]:
        r = rate_by_period.get(f["period"], latest_rate)
        f["value_ves"] = round(f["value"] * r, 2) if r else None

    detail = {}
    for p in periods:
        changes = datasets.price_changes_for_month(product_id, p)
        detail[p] = {
            "kind": "history",
            "columns": ["Fecha", "Precio venta USD", "Precio compra USD", "Motivo"],
            "rows": [[c.changed_at.isoformat(), float(c.sale_price_usd),
                      float(c.purchase_price_usd), c.reason or "—"] for c in changes],
            "summary": {"precio_venta_usd": round(float(df.loc[p, "sale_price_usd"]), 2)},
        }
    _attach_forecast_detail(detail, res["forecast"])
    return _wrap("product_price", "Pronóstico de precio de producto", subject,
                 "USD", "usd", res, detail,
                 meta={"current_price_usd": float(product.sale_price_usd or 0)})


# --------------------------------------------------------------------------- #
# 5) Demanda por producto (modelo panel global XGBoost)
# --------------------------------------------------------------------------- #
def _train_demand_panel(model_key: str):
    """Entrena un único modelo panel sobre todos los productos. Devuelve
    (estimator, feature_names, metrics, n_train, n_holdout, panel, global_periods)."""
    panel = datasets.monthly_demand_panel()
    if panel.empty:
        return None
    global_end = panel["period"].max()
    global_start = panel["period"].min()
    from .features import month_range

    all_periods = month_range(global_start, global_end)
    denom = max(1, len(all_periods) - 1)
    pidx = {p: i for i, p in enumerate(all_periods)}
    shock = _rate_shock_series(all_periods) or [0.0] * len(all_periods)
    shock_by_period = dict(zip(all_periods, shock))

    from apps.core.models import Product

    base_price = {
        p.id: float(p.sale_price_usd or 0)
        for p in Product.objects.all().only("id", "sale_price_usd")
    }

    lags = (1, 2, 3)
    feats, targets, meta_rows = [], [], []
    for pid, sub in panel.groupby("product_id"):
        series = datasets.demand_series(pid, panel)
        if len(series) <= max(lags):
            continue
        cat_id = int(sub["category_id"].iloc[0])
        vals = [v for _, v in series]
        pers = [p for p, _ in series]
        for i in range(max(lags), len(vals)):
            period = pers[i]
            row = dict(calendar_features(period, pidx.get(period, 0) / denom))
            for lag in lags:
                row[f"lag{lag}"] = vals[i - lag]
            row["roll3"] = float(np.mean(vals[i - 3:i]))
            row["categoria"] = cat_id
            row["precio_base"] = base_price.get(pid, 0.0)
            row["shock_cambiario"] = shock_by_period.get(period, 0.0)
            feats.append(row)
            targets.append(vals[i])
            meta_rows.append(period)
    if not feats:
        return None

    X = pd.DataFrame(feats)
    names = list(X.columns)
    y = np.asarray(targets, dtype=float)
    meta_periods = np.asarray(meta_rows)

    # Holdout = filas de los últimos 6 meses globales (across products).
    holdout_periods = set(all_periods[-6:])
    test_mask = np.array([p in holdout_periods for p in meta_periods])
    metrics, n_holdout = None, 0
    if test_mask.sum() >= 10 and (~test_mask).sum() >= 20:
        bt = make_regressor(model_key)
        bt.fit(X[~test_mask], y[~test_mask])
        pred = np.clip(bt.predict(X[test_mask]), 0, None)
        metrics = _metrics(y[test_mask], pred)
        n_holdout = int(test_mask.sum())

    estimator = make_regressor(model_key)
    estimator.fit(X, y)
    return estimator, names, metrics, len(y) - n_holdout, n_holdout, panel, all_periods


def forecast_demand(product_id: int, horizon: int = 6, model: str | None = None) -> dict:
    from apps.core.models import Product

    product = Product.objects.filter(id=product_id).first()
    if not product:
        return _empty("demand", "Pronóstico de demanda")
    subject = {"product_id": product_id, "product_name": product.name, "sku": product.sku}
    model_key = model or ASSIGNED_MODEL["demand"]

    trained = _train_demand_panel(model_key)
    if trained is None:
        return _empty("demand", "Pronóstico de demanda", subject=subject)
    estimator, names, metrics, n_train, n_holdout, panel, all_periods = trained

    series = datasets.demand_series(product_id, panel)
    if len(series) <= 3:
        return _empty("demand", "Pronóstico de demanda", subject=subject)
    pers = [p for p, _ in series]
    vals = [float(v) for _, v in series]

    denom = max(1, len(all_periods) - 1)
    pidx = {p: i for i, p in enumerate(all_periods)}
    cat_id = int(panel[panel["product_id"] == product_id]["category_id"].iloc[0])
    base_price = float(product.sale_price_usd or 0)
    shock = _rate_shock_series(all_periods) or []
    shock_by_period = dict(zip(all_periods, shock))

    lags = (1, 2, 3)
    hist = list(vals)
    last_period = pers[-1]
    # sigma de residuos in-sample para la banda
    resid = []
    for i in range(max(lags), len(vals)):
        row = dict(calendar_features(pers[i], pidx.get(pers[i], 0) / denom))
        for lag in lags:
            row[f"lag{lag}"] = vals[i - lag]
        row["roll3"] = float(np.mean(vals[i - 3:i]))
        row["categoria"] = cat_id
        row["precio_base"] = base_price
        row["shock_cambiario"] = shock_by_period.get(pers[i], 0.0)
        resid.append(vals[i] - float(estimator.predict(pd.DataFrame([row])[names])[0]))
    sigma = max(float(np.std(resid)) if resid else 1.0, 0.5)

    forecast = []
    for step in range(1, horizon + 1):
        p = add_period(last_period, step)
        row = dict(calendar_features(p, (len(all_periods) - 1 + step) / denom))
        for lag in lags:
            row[f"lag{lag}"] = hist[-lag]
        row["roll3"] = float(np.mean(hist[-3:]))
        row["categoria"] = cat_id
        row["precio_base"] = base_price
        row["shock_cambiario"] = 0.0
        pred = max(float(estimator.predict(pd.DataFrame([row])[names])[0]), 0.0)
        hist.append(pred)
        band = Z90 * sigma * math.sqrt(step)
        forecast.append({
            "period": p, "label": period_label(p),
            "value": round(pred), "lower": max(round(pred - band), 0), "upper": round(pred + band),
            "features": {k: round(float(v), 4) for k, v in row.items()},
        })

    history = [{"period": p, "label": period_label(p), "value": round(v)} for p, v in zip(pers, vals)]
    model_block = _model_block(model_key, estimator, metrics, names, n_train, n_holdout)
    res = {"history": history, "forecast": forecast, "model": model_block, "sigma": sigma}

    detail = {}
    for p in pers:
        items = datasets.sale_items_for_month(product_id, p)
        detail[p] = {
            "kind": "history",
            "columns": ["Fecha", "Cliente", "Cantidad", "Precio U. USD"],
            "rows": [[it.sale.sale_date.isoformat(), str(it.sale.customer),
                      it.quantity, float(it.unit_sale_price_usd)] for it in items],
            "summary": {"unidades": int(sum(it.quantity for it in items)), "ventas": len(items)},
        }
    _attach_forecast_detail(detail, forecast)
    return _wrap("demand", "Pronóstico de demanda", subject, "unidades", "int", res, detail,
                 meta={"category_id": cat_id})


# --------------------------------------------------------------------------- #
# 6) Inventario / reabastecimiento (derivado de la demanda)
# --------------------------------------------------------------------------- #
DEFAULT_LEAD_TIME_DAYS = 30


def forecast_inventory(product_id: int, horizon: int = 6) -> dict:
    from apps.core.models import Product

    product = Product.objects.filter(id=product_id).first()
    if not product:
        return _empty("inventory", "Proyección de inventario")
    subject = {"product_id": product_id, "product_name": product.name, "sku": product.sku}
    demand = forecast_demand(product_id, horizon=horizon)
    if not demand.get("forecast"):
        return _empty("inventory", "Proyección de inventario", subject=subject)

    current_stock = int(product.stock or 0)
    min_stock = int(product.min_stock or 0)
    fc = demand["forecast"]
    avg_monthly = float(np.mean([f["value"] for f in fc])) if fc else 0.0
    daily = avg_monthly / 30.0

    # Proyección de stock restante mes a mes.
    stock = current_stock
    forecast, stockout_period = [], None
    detail = {}
    for f in fc:
        demanded = f["value"]
        stock_after = stock - demanded
        if stockout_period is None and stock_after <= 0:
            stockout_period = f["period"]
        forecast.append({
            "period": f["period"], "label": f["label"],
            "value": max(round(stock_after), 0),
            "lower": max(round(stock - f["upper"]), 0),
            "upper": max(round(stock - f["lower"]), 0),
            "demand": demanded,
        })
        detail[f["period"]] = {
            "kind": "forecast",
            "columns": ["Concepto", "Valor"],
            "rows": [["Stock inicial mes", max(round(stock), 0)],
                     ["Demanda pronosticada", demanded],
                     ["Stock final mes", round(stock_after)]],
        }
        stock = stock_after

    # Recomendación de reabastecimiento (punto de reorden + cantidad).
    lead_demand = daily * DEFAULT_LEAD_TIME_DAYS
    safety = max(min_stock, round(1.65 * demand["sigma"])) if demand.get("sigma") else min_stock
    reorder_point = round(lead_demand + safety)
    months_cover = round(current_stock / avg_monthly, 1) if avg_monthly > 0 else None
    suggested_qty = max(round(lead_demand + safety - current_stock), 0)

    history = [{"period": add_period(fc[0]["period"], -1),
                "label": period_label(add_period(fc[0]["period"], -1)),
                "value": current_stock}]
    res = {"history": history, "forecast": forecast, "model": demand["model"], "sigma": demand["sigma"]}
    meta = {
        "current_stock": current_stock,
        "min_stock": min_stock,
        "avg_monthly_demand": round(avg_monthly, 1),
        "months_of_cover": months_cover,
        "reorder_point": reorder_point,
        "suggested_reorder_qty": suggested_qty,
        "stockout_period": stockout_period,
        "stockout_label": period_label(stockout_period) if stockout_period else None,
        "lead_time_days": DEFAULT_LEAD_TIME_DAYS,
        "needs_reorder": current_stock <= reorder_point,
    }
    return _wrap("inventory", "Proyección de inventario y reabastecimiento", subject,
                 "unidades", "int", res, detail, meta=meta)


# --------------------------------------------------------------------------- #
# 7) Conversión de presupuestos (clasificación)
# --------------------------------------------------------------------------- #
def forecast_quote_conversion(model: str | None = None) -> dict:
    df = datasets.quotes_dataframe()
    title = "Conversión de presupuestos"
    if df.empty or len(df) < 12 or df["converted"].nunique() < 2:
        return _empty("quote", title)
    model_key = model or ASSIGNED_MODEL["quote"]

    # Shock cambiario en el mes de emisión (señal que el seed usó para decidir el cierre).
    shock_map = _rate_shock_map()
    df = df.copy()
    df["rate_shock"] = df["period"].map(lambda p: shock_map.get(p, 0.0))

    feature_cols = ["total_usd", "n_items", "includes_installation",
                    "includes_delivery", "issued_month", "rate_shock"]
    # codifica tipo de cliente
    type_dummies = pd.get_dummies(df["customer_type"], prefix="cliente")
    X_all = pd.concat([df[feature_cols], type_dummies], axis=1).astype(float)
    names = list(X_all.columns)
    y_all = df["converted"].to_numpy()

    # Train/holdout temporal: presupuestos ya cerrados (no abiertos) para evaluar.
    closed = ~df["is_open"].to_numpy()
    Xc, yc = X_all[closed], y_all[closed]
    metrics, n_holdout = None, 0
    if len(yc) >= 16 and pd.Series(yc).nunique() == 2:
        cut = int(len(yc) * 0.75)
        clf_bt = make_classifier(model_key)
        clf_bt.fit(Xc.iloc[:cut], yc[:cut])
        if pd.Series(yc[cut:]).nunique() >= 1:
            pred = clf_bt.predict(Xc.iloc[cut:])
            acc = float(np.mean(pred == yc[cut:]))
            tp = int(np.sum((pred == 1) & (yc[cut:] == 1)))
            fp = int(np.sum((pred == 1) & (yc[cut:] == 0)))
            fn = int(np.sum((pred == 0) & (yc[cut:] == 1)))
            precision = tp / (tp + fp) if (tp + fp) else None
            recall = tp / (tp + fn) if (tp + fn) else None
            metrics = {"accuracy": round(acc, 4),
                       "precision": round(precision, 4) if precision is not None else None,
                       "recall": round(recall, 4) if recall is not None else None}
            n_holdout = len(yc) - cut

    clf = make_classifier(model_key)
    clf.fit(Xc if len(yc) >= 8 else X_all, yc if len(yc) >= 8 else y_all)

    # Pipeline actual: presupuestos abiertos con su probabilidad de conversión.
    open_df = df[df["is_open"]]
    pipeline_quotes, expected_revenue, total_value = [], 0.0, 0.0
    if not open_df.empty:
        Xo = pd.concat([open_df[feature_cols], pd.get_dummies(open_df["customer_type"], prefix="cliente")], axis=1)
        Xo = Xo.reindex(columns=names, fill_value=0).astype(float)
        probs = clf.predict_proba(Xo)[:, 1] if hasattr(clf, "predict_proba") else clf.predict(Xo)
        for (_, q), prob in zip(open_df.iterrows(), probs):
            exp = float(q["total_usd"]) * float(prob)
            expected_revenue += exp
            total_value += float(q["total_usd"])
            pipeline_quotes.append({
                "id": int(q["id"]), "quote_number": q["quote_number"],
                "customer": q["customer_name"], "total_usd": round(float(q["total_usd"]), 2),
                "probability": round(float(prob), 4), "expected_usd": round(exp, 2),
                "status": q["status"],
            })
        pipeline_quotes.sort(key=lambda d: d["expected_usd"], reverse=True)

    # Tasa de conversión mensual (línea histórica).
    monthly = (df.groupby("period")
               .agg(total=("converted", "size"), converted=("converted", "sum")).reset_index())
    monthly_rate = [
        {"period": r["period"], "label": period_label(r["period"]),
         "value": round(float(r["converted"]) / r["total"] * 100.0, 1),
         "total": int(r["total"]), "converted": int(r["converted"])}
        for _, r in monthly.iterrows()
    ]
    # Tasa de conversión histórica sobre presupuestos CERRADOS (convertidos vs rechazados),
    # sin contar los abiertos (que aún no se resuelven) como fracasos.
    closed_df = df[~df["is_open"]]
    hist_rate = float((closed_df["converted"].mean() if len(closed_df) else df["converted"].mean()) * 100.0)

    meta_block = MODEL_META[model_key]
    model_block = {
        "key": model_key, "label": meta_block["label"], "library": meta_block["library"],
        "accuracy": metrics["accuracy"] if metrics else None,
        "precision": metrics["precision"] if metrics else None,
        "recall": metrics["recall"] if metrics else None,
        "n_train": int(len(yc)), "n_holdout": int(n_holdout),
        "trained_at": timezone.now().isoformat(),
        "hyperparameters": _hyperparams(model_key, clf),
        "feature_importances": feature_importances(clf, names),
    }
    return {
        "target": "quote", "title": title, "subject": None,
        "unit": "%", "value_kind": "percent",
        "model": model_block,
        "monthly_rate": monthly_rate,
        "historical_conversion_rate": round(hist_rate, 1),
        "pipeline": {
            "open_count": len(pipeline_quotes),
            "expected_revenue_usd": round(expected_revenue, 2),
            "total_value_usd": round(total_value, 2),
            "expected_rate_pct": round(expected_revenue / total_value * 100.0, 1) if total_value else 0.0,
            "quotes": pipeline_quotes,
        },
    }


# --------------------------------------------------------------------------- #
# 8) Análisis de competencia (SEPARADO de los datos internos)
# --------------------------------------------------------------------------- #
def competitor_analysis(category: str | None = None, product_id: int | None = None) -> dict:
    from apps.core.models import Product

    df = datasets.competitor_observations(category=category, product_id=product_id)
    title = "Análisis de precios de competencia"
    # Catálogo de categorías presentes para el filtro.
    all_obs = datasets.competitor_observations()
    categories = sorted(all_obs["category"].unique().tolist()) if not all_obs.empty else []

    if df.empty:
        return {"target": "competitor", "title": title, "subject": None,
                "categories": categories, "filter": {"category": category, "product_id": product_id},
                "positioning": [], "by_competitor": [], "product_comparison": [],
                "trend": [], "observations": [], "model": None, "meta": {"n_obs": 0}}

    # Precio propio promedio por categoría. Clasificamos cada producto propio con el
    # MISMO vocabulario que usan los scrapers (classify_category sobre el nombre), para
    # que las categorías del catálogo y las de mercado casen ("Sillas", "Escritorios"…).
    # Clave normalizada (minúsculas) y fallback al nombre de su categoría del catálogo.
    try:
        from apps.competitor_market_data.scrapers import classify_category
    except Exception:  # pragma: no cover - import defensivo
        classify_category = lambda _t: None  # noqa: E731

    own_by_cat = {}
    for prod in Product.objects.filter(is_active=True).select_related("category"):
        vocab = classify_category(f"{prod.name} {prod.full_name}".strip())
        cat = (vocab or (prod.category.name if prod.category else "Sin categoría")).strip().lower()
        own_by_cat.setdefault(cat, []).append(float(prod.sale_price_usd or 0))

    positioning = []
    for cat, g in df.groupby("category"):
        prices = g["price_usd"]
        own_prices = own_by_cat.get(str(cat).strip().lower(), [])
        own_avg = float(np.mean(own_prices)) if own_prices else None
        comp_avg = float(prices.mean())
        position = None
        percentile = None
        if own_avg is not None:
            percentile = round(float((prices < own_avg).mean() * 100.0), 1)
            if own_avg < float(prices.quantile(0.33)):
                position = "below"
            elif own_avg > float(prices.quantile(0.66)):
                position = "above"
            else:
                position = "within"
        positioning.append({
            "category": cat, "own_avg": round(own_avg, 2) if own_avg is not None else None,
            "comp_min": round(float(prices.min()), 2), "comp_avg": round(comp_avg, 2),
            "comp_max": round(float(prices.max()), 2), "comp_median": round(float(prices.median()), 2),
            "n_obs": int(len(prices)), "position": position, "percentile": percentile,
        })
    positioning.sort(key=lambda d: d["n_obs"], reverse=True)

    # Por competidor.
    by_competitor = []
    for comp, g in df.groupby("competitor"):
        by_competitor.append({
            "competitor": comp, "avg_price_usd": round(float(g["price_usd"].mean()), 2),
            "n_obs": int(len(g)), "products": int(g["product_name"].nunique()),
        })
    by_competitor.sort(key=lambda d: d["n_obs"], reverse=True)

    # Comparación like-with-like: precio propio vs mercado, usando el match de producto
    # que ya calculó la capa de benchmarking. Es la vista más accionable para el dueño.
    product_comparison = []
    matched = df[df["matched_product_id"].notna()]
    if not matched.empty:
        own_price = {
            p.id: float(p.sale_price_usd or 0)
            for p in Product.objects.filter(id__in=matched["matched_product_id"].dropna().unique().tolist())
        }
        for pid_, g in matched.groupby("matched_product_id"):
            prices = g["price_usd"]
            op = own_price.get(int(pid_))
            position = None
            if op:
                if op < float(prices.min()):
                    position = "below"
                elif op > float(prices.max()):
                    position = "above"
                else:
                    position = "within"
            product_comparison.append({
                "product_id": int(pid_), "product": g["matched_product"].iloc[0],
                "own_price_usd": round(op, 2) if op else None,
                "comp_min": round(float(prices.min()), 2),
                "comp_avg": round(float(prices.mean()), 2),
                "comp_max": round(float(prices.max()), 2),
                "n_obs": int(len(g)), "position": position,
            })
        product_comparison.sort(key=lambda d: d["n_obs"], reverse=True)

    # Tendencia temporal (solo si hay dispersión en el tiempo): regresión lineal.
    trend, model_block = [], None
    monthly = df.groupby("period")["price_usd"].mean()
    if monthly.index.nunique() >= 4:
        from .features import month_range
        from sklearn.linear_model import LinearRegression

        periods = month_range(monthly.index.min(), monthly.index.max())
        s = monthly.reindex(periods).ffill().bfill()
        x = np.arange(len(periods)).reshape(-1, 1)
        lr = LinearRegression().fit(x, s.to_numpy())
        for i, p in enumerate(periods):
            trend.append({"period": p, "label": period_label(p),
                          "comp_avg": round(float(s.iloc[i]), 2),
                          "trend": round(float(lr.predict([[i]])[0]), 2)})
        model_block = {"key": "linear", "label": MODEL_META["linear"]["label"],
                       "library": "scikit-learn",
                       "slope_usd_per_month": round(float(lr.coef_[0]), 4),
                       "trained_at": timezone.now().isoformat()}

    def _na(v):
        return None if (v is None or (isinstance(v, float) and math.isnan(v))) else v

    observations = [
        {"competitor": _na(r["competitor"]), "product_name": _na(r["product_name"]),
         "category": _na(r["category"]), "price_usd": round(float(r["price_usd"]), 2),
         "matched_product": _na(r["matched_product"]),
         "in_stock": (None if pd.isna(r["in_stock"]) else bool(r["in_stock"])),
         "source": _na(r["source"]), "scraped_at": r["scraped_at"].isoformat()}
        for _, r in df.sort_values("price_usd").iterrows()
    ]
    return {
        "target": "competitor", "title": title, "subject": None,
        "categories": categories, "filter": {"category": category, "product_id": product_id},
        "positioning": positioning, "by_competitor": by_competitor,
        "product_comparison": product_comparison, "trend": trend,
        "observations": observations, "model": model_block,
        "meta": {"n_obs": int(len(df)), "n_competitors": int(df["competitor"].nunique())},
    }


# --------------------------------------------------------------------------- #
# Helpers de empaquetado
# --------------------------------------------------------------------------- #
def _attach_forecast_detail(detail: dict, forecast: list[dict]) -> None:
    """Para cada periodo pronosticado, guarda las variables de entrada que usó el modelo."""
    for f in forecast:
        rows = [[k, v] for k, v in f.get("features", {}).items()]
        detail[f["period"]] = {
            "kind": "forecast",
            "columns": ["Variable", "Valor"],
            "rows": rows,
            "summary": {"valor_pronosticado": f["value"],
                        "intervalo": [f.get("lower"), f.get("upper")]},
        }


def _wrap(target, title, subject, unit, kind, res, detail, *, meta=None) -> dict:
    return {
        "target": target, "title": title, "subject": subject,
        "unit": unit, "value_kind": kind,
        "model": res["model"], "history": res["history"], "forecast": res["forecast"],
        "detail": detail, "meta": meta or {}, "sigma": res.get("sigma"),
    }


def _empty(target, title, subject=None) -> dict:
    return {
        "target": target, "title": title, "subject": subject,
        "unit": "", "value_kind": "int", "model": None,
        "history": [], "forecast": [], "detail": {},
        "meta": {"insufficient_data": True},
    }

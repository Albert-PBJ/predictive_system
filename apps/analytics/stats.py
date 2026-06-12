"""Estadísticas descriptivas para los paneles de situación (no predictivo).

A diferencia de ``ml/`` (que pronostica el futuro), este módulo resume el estado
**actual** del negocio con agregaciones directas del ORM: distribución de clientes,
catálogo de productos, ventas detal vs. institucional, conversión de presupuestos,
etc. Alimenta el panel de Inicio y el módulo "Estadísticas" del frontend.

Cada función devuelve dicts/listas listos para serializar a JSON. Las series
mensuales reutilizan los helpers de calendario de ``ml.features`` para etiquetar en
español y rellenar los meses sin actividad. Todo se mide sobre ventas COMPLETADAS
(``status="COMP"``) — las anuladas no cuentan como ingreso.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.db.models import Avg, Count, F, Max, Min, Q, Sum
from django.db.models.functions import TruncMonth

from apps.core.models import SERVICE_SKU_PREFIX, Customer, ExchangeRate, Product
from apps.sales.models import Quote, Sale, SaleItem

from .ml.features import add_period, month_range, period_label, period_of
from .models import Alert, PredictionLog

COMP = Sale.StatusChoices.COMPLETED  # "COMP"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _f(x) -> float:
    """Convierte Decimal/None a float (0.0 si es None)."""
    return float(x) if x is not None else 0.0


def _pct(curr: float, prev: float) -> float | None:
    """Variación porcentual ``(curr - prev) / prev``; None si no hay base previa."""
    if not prev:
        return None
    return round((curr - prev) / prev * 100, 1)


def _first_of(period: str) -> date:
    """Primer día del mes de un periodo ``"YYYY-MM"``."""
    return date(int(period[:4]), int(period[5:]), 1)


def reference_month() -> str:
    """Mes de referencia = mes de la última venta completada (o el actual si no hay)."""
    last = Sale.objects.filter(status=COMP).aggregate(m=Max("sale_date"))["m"]
    return period_of(last or date.today())


def _customer_names(ids) -> dict[int, dict]:
    """Mapa id -> datos básicos del cliente (para enriquecer rankings)."""
    out = {}
    for c in Customer.objects.filter(id__in=list(ids)).only(
        "id", "company_name", "customer_type", "state"
    ):
        out[c.id] = {
            "company_name": c.company_name,
            "customer_type": c.get_customer_type_display(),
            "state": c.state or "—",
        }
    return out


# --------------------------------------------------------------------------- #
# Panel de inicio EJECUTIVO (resumen estratégico con "máquina del tiempo")
# --------------------------------------------------------------------------- #
# El panel de Inicio es la radiografía CONDENSADA del negocio para un rango de
# fechas [start, end] arbitrario que elige el usuario con dos selectores
# Desde/Hasta. Todo lo que depende de ventas/clientes/presupuestos se recalcula
# dentro del rango y se compara contra la ventana inmediatamente anterior de igual
# duración. Los bloques de naturaleza "instantánea" (inventario actual, salud de
# modelos, posición competitiva) se etiquetan como "actual" en la UI.
#
# Las cifras SENSIBLES (utilidad, margen, índice de ventaja competitiva, análisis
# de competencia y salud de modelos) solo se incluyen cuando ``sensitive=True``
# (Gerente/Admin); el resto del personal recibe la versión operativa sin
# rentabilidad — coherente con cómo se gestiona la utilidad en el resto de la app.


def default_range(months: int = 2) -> tuple[date, date]:
    """Rango por defecto: los últimos ``months`` meses con datos.

    El panel de Inicio carga 2 meses para que las variaciones de los KPIs comparen
    contra la ventana previa de igual duración y la tendencia tenga ya un par de
    puntos; los paneles de estadísticas usan 1 mes. El usuario amplía con los presets
    (1/2/3/6/12 meses, año, todo).
    """
    last = Sale.objects.filter(status=COMP).aggregate(m=Max("sale_date"))["m"] or date.today()
    return _first_of(add_period(period_of(last), -(months - 1))), last


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _range_block(start: date, end: date) -> dict:
    """Bloque ``range`` común a los paneles con "máquina del tiempo".

    Devuelve el rango elegido (con etiquetas en español) y los límites de datos
    disponibles (primera/última venta completada) para que el selector de fechas
    del frontend conozca su mínimo/máximo. Idéntico al que expone el panel de Inicio.
    """
    bounds = Sale.objects.filter(status=COMP).aggregate(lo=Min("sale_date"), hi=Max("sale_date"))
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "from_label": period_label(period_of(start)),
        "to_label": period_label(period_of(end)),
        "months": len(month_range(period_of(start), period_of(end))),
        "data_from": bounds["lo"].isoformat() if bounds["lo"] else start.isoformat(),
        "data_to": bounds["hi"].isoformat() if bounds["hi"] else end.isoformat(),
    }


def _sale_metrics(qs) -> dict:
    """Agregados de un conjunto de ventas (ingreso, utilidad, ticket, nº clientes)."""
    agg = qs.aggregate(
        revenue=Sum("total_sale_usd"),
        profit=Sum("total_profit_usd"),
        discount=Sum("total_discount_usd"),
        count=Count("id"),
        avg_ticket=Avg("total_sale_usd"),
        customers=Count("customer", distinct=True),
    )
    return {
        "revenue": _f(agg["revenue"]),
        "profit": _f(agg["profit"]),
        "discount": _f(agg["discount"]),
        "count": int(agg["count"] or 0),
        "avg_ticket": _f(agg["avg_ticket"]),
        "customers": int(agg["customers"] or 0),
    }


def _price_competitiveness() -> tuple[float | None, list[dict]]:
    """Score 0-100 de competitividad de precio + posicionamiento por categoría.

    Reutiliza el análisis de competencia del módulo predictivo (datos scrapeados,
    deduplicados al último por ``listing_key``). ``percentile`` = % de observaciones
    de la competencia por debajo del precio propio; si vendemos más barato que el
    mercado el percentil es bajo → somos más competitivos, así que el score es
    ``100 - percentil`` ponderado por nº de observaciones. El módulo ML es opcional:
    si falla la importación o no hay datos, devolvemos ``(None, [])``.
    """
    try:
        from .ml import forecasters as F
        from .ml import registry
        analysis = registry.cached("competitor:None:None", lambda: F.competitor_analysis())
    except Exception:  # pragma: no cover - import/serving defensivo (ML opcional)
        return None, []
    positioning = analysis.get("positioning", [])
    scored = [p for p in positioning if p.get("percentile") is not None and p.get("n_obs")]
    top = sorted(positioning, key=lambda d: d.get("n_obs", 0), reverse=True)[:6]
    if not scored:
        return None, top
    den = sum(p["n_obs"] for p in scored)
    score = round(sum((100.0 - p["percentile"]) * p["n_obs"] for p in scored) / den, 1) if den else None
    return score, top


def _build_narrative(start, end, cur, prev, type_split, no_demand_count, at_risk, rate, sensitive) -> list[str]:
    """Narrativa automatizada (data storytelling): 3-5 frases con lo esencial del rango."""
    out: list[str] = []
    period_txt = f"{period_label(period_of(start))} – {period_label(period_of(end))}"
    g = _pct(cur["revenue"], prev["revenue"])
    if g is None:
        out.append(f"En {period_txt} se facturaron ${cur['revenue']:,.0f} en {cur['count']} ventas.")
    else:
        trend = "más" if g >= 0 else "menos"
        out.append(
            f"En {period_txt} se facturaron ${cur['revenue']:,.0f} en {cur['count']} ventas, "
            f"{abs(g):.0f}% {trend} que el periodo anterior."
        )
    ret = next((t for t in type_split if t["type"] == Sale.TypeChoices.RETAIL), None)
    ins = next((t for t in type_split if t["type"] == Sale.TypeChoices.INSTITUTIONAL), None)
    if ret and ins:
        out.append(
            f"El segmento institucional aporta el {ins['share_pct']:.0f}% de los ingresos "
            f"y el detal el {ret['share_pct']:.0f}%."
        )
    if sensitive and cur["revenue"]:
        margin = cur["profit"] / cur["revenue"] * 100
        out.append(f"La utilidad del periodo fue ${cur['profit']:,.0f} (margen {margin:.0f}%).")
    if no_demand_count:
        out.append(f"{no_demand_count} productos activos no registraron ninguna venta en el rango.")
    if at_risk:
        out.append(f"{len(at_risk)} clientes activos llevan más de 6 meses sin comprar (riesgo de fuga).")
    if rate and rate.get("parallel_change_pct") is not None and abs(rate["parallel_change_pct"]) >= 5:
        out.append(
            f"El dólar paralelo varió {rate['parallel_change_pct']:+.0f}% en el periodo, "
            f"presionando la demanda."
        )
    return out[:5]


def executive_dashboard(start: date, end: date, *, sensitive: bool) -> dict:
    """Resumen ejecutivo del negocio para el rango [start, end].

    ``sensitive`` (Gerente/Admin) habilita utilidad, margen, índice de ventaja
    competitiva, análisis de competencia y salud de modelos.
    """
    if start > end:
        start, end = end, start
    span_days = (end - start).days
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span_days)

    base = Sale.objects.filter(status=COMP)
    cur_qs = base.filter(sale_date__gte=start, sale_date__lte=end)
    prev_qs = base.filter(sale_date__gte=prev_start, sale_date__lte=prev_end)

    cur = _sale_metrics(cur_qs)
    prev = _sale_metrics(prev_qs)
    cur_units = _f(SaleItem.objects.filter(sale__in=cur_qs).aggregate(u=Sum("quantity"))["u"])
    prev_units = _f(SaleItem.objects.filter(sale__in=prev_qs).aggregate(u=Sum("quantity"))["u"])

    margin = round(cur["profit"] / cur["revenue"] * 100, 1) if cur["revenue"] else None
    prev_margin = round(prev["profit"] / prev["revenue"] * 100, 1) if prev["revenue"] else None

    # Clientes: recompra (retención) y altas nuevas en el rango.
    cur_cust_ids = set(cur_qs.values_list("customer_id", flat=True))
    prev_cust_ids = set(prev_qs.values_list("customer_id", flat=True))
    retention = round(len(cur_cust_ids & prev_cust_ids) / len(prev_cust_ids) * 100, 1) if prev_cust_ids else None
    new_customers = Customer.objects.filter(created_at__date__gte=start, created_at__date__lte=end).count()

    # Presupuestos emitidos/convertidos en el rango.
    q_in = Quote.objects.filter(issued_date__gte=start, issued_date__lte=end)
    quotes_issued = q_in.count()
    quotes_converted = q_in.filter(status=Quote.StatusChoices.CONVERTED).count()
    conversion_rate = round(quotes_converted / quotes_issued * 100, 1) if quotes_issued else None

    # Serie mensual (ingresos/utilidad/nº + detal vs. institucional) dentro del rango.
    periods = month_range(period_of(start), period_of(end))
    rows = (
        cur_qs.annotate(m=TruncMonth("sale_date"))
        .values("m", "sale_type")
        .annotate(revenue=Sum("total_sale_usd"), profit=Sum("total_profit_usd"), count=Count("id"))
    )
    agg_month: dict[str, dict] = {}
    for r in rows:
        p = period_of(r["m"])
        slot = agg_month.setdefault(p, {"revenue": 0.0, "profit": 0.0, "count": 0, "RET": 0.0, "INS": 0.0})
        slot["revenue"] += _f(r["revenue"])
        slot["profit"] += _f(r["profit"])
        slot["count"] += int(r["count"])
        if r["sale_type"] == Sale.TypeChoices.RETAIL:
            slot["RET"] += _f(r["revenue"])
        elif r["sale_type"] == Sale.TypeChoices.INSTITUTIONAL:
            slot["INS"] += _f(r["revenue"])
    monthly, monthly_by_type = [], []
    for p in periods:
        s = agg_month.get(p, {})
        point = {"period": p, "label": period_label(p), "revenue": s.get("revenue", 0.0), "count": s.get("count", 0)}
        if sensitive:
            point["profit"] = s.get("profit", 0.0)
        monthly.append(point)
        monthly_by_type.append(
            {"period": p, "label": period_label(p), "retail": s.get("RET", 0.0), "institutional": s.get("INS", 0.0)}
        )

    # Detal vs. institucional (totales del rango) — la historia estratégica de la empresa.
    type_labels = dict(Sale.TypeChoices.choices)
    total_rev = cur["revenue"] or 1.0
    type_split = [
        {
            "type": r["sale_type"],
            "label": str(type_labels.get(r["sale_type"], r["sale_type"])),
            "revenue": _f(r["revenue"]),
            "count": int(r["count"]),
            "share_pct": round(_f(r["revenue"]) / total_rev * 100, 1),
        }
        for r in cur_qs.values("sale_type").annotate(revenue=Sum("total_sale_usd"), count=Count("id")).order_by("-revenue")
    ]

    revenue_by_category = [
        {"category": r["product__category__name"] or "Sin categoría", "revenue": _f(r["revenue"])}
        for r in (
            SaleItem.objects.filter(sale__in=cur_qs)
            .values("product__category__name")
            .annotate(revenue=Sum("subtotal_sale_usd"))
            .order_by("-revenue")[:8]
        )
    ]

    # Top productos y top clientes del rango.
    item_agg = list(
        SaleItem.objects.filter(sale__in=cur_qs)
        .values("product_id")
        .annotate(units=Sum("quantity"), revenue=Sum("subtotal_sale_usd"))
    )
    pnames = {
        p.id: {"name": p.name, "sku": p.sku, "category": p.category.name if p.category else "—"}
        for p in Product.objects.filter(id__in=[a["product_id"] for a in item_agg]).select_related("category")
    }

    def _prow(a):
        info = pnames.get(a["product_id"], {})
        return {
            "product_id": a["product_id"],
            "name": info.get("name", f"#{a['product_id']}"),
            "sku": info.get("sku"),
            "category": info.get("category", "—"),
            "units": int(a["units"] or 0),
            "revenue": _f(a["revenue"]),
        }

    top_products = [_prow(a) for a in sorted(item_agg, key=lambda x: _f(x["revenue"]), reverse=True)[:8]]

    cust_agg = list(cur_qs.values("customer_id").annotate(revenue=Sum("total_sale_usd"), orders=Count("id")))
    cnames = _customer_names({a["customer_id"] for a in cust_agg})

    def _crow(a):
        info = cnames.get(a["customer_id"], {})
        return {
            "customer_id": a["customer_id"],
            "name": info.get("company_name", f"#{a['customer_id']}"),
            "type": info.get("customer_type", "—"),
            "state": info.get("state", "—"),
            "revenue": _f(a["revenue"]),
            "orders": int(a["orders"]),
        }

    top_customers = [_crow(a) for a in sorted(cust_agg, key=lambda x: _f(x["revenue"]), reverse=True)[:8]]

    # Productos sin demanda en el rango (activos sin ninguna venta) — "¿qué no rota?".
    # Se excluyen los servicios: no llevan stock, no representan capital inmovilizado.
    sold_ids = {a["product_id"] for a in item_agg}
    no_demand_qs = (
        Product.objects.filter(is_active=True)
        .exclude(id__in=sold_ids)
        .exclude(sku__startswith=SERVICE_SKU_PREFIX)
        .select_related("category")
        .order_by("-stock")
    )
    no_demand = []
    for p in no_demand_qs[:10]:
        row = {
            "product_id": p.id,
            "name": p.name,
            "sku": p.sku,
            "category": p.category.name if p.category else "—",
            "stock": p.stock,
            "retail_value": _f(p.stock * (p.sale_price_usd or 0)),
        }
        if sensitive:
            row["cost_value"] = _f(p.stock * (p.purchase_price_usd or 0))
        no_demand.append(row)
    no_demand_count = no_demand_qs.count()

    # Clientes en riesgo de fuga: activos cuya última compra es anterior a end-6m.
    cutoff = _first_of(add_period(period_of(end), -6))
    risk_agg = list(
        base.values("customer_id").annotate(revenue=Sum("total_sale_usd"), orders=Count("id"), last=Max("sale_date"))
    )
    active_ids = set(Customer.objects.filter(is_active_customer=True).values_list("id", flat=True))
    rnames = _customer_names({a["customer_id"] for a in risk_agg})

    def _rrow(a):
        info = rnames.get(a["customer_id"], {})
        return {
            "customer_id": a["customer_id"],
            "name": info.get("company_name", f"#{a['customer_id']}"),
            "type": info.get("customer_type", "—"),
            "state": info.get("state", "—"),
            "revenue": _f(a["revenue"]),
            "orders": int(a["orders"]),
            "last_purchase": a["last"].isoformat() if a["last"] else None,
        }

    at_risk = [
        _rrow(a)
        for a in sorted(
            (a for a in risk_agg if a["customer_id"] in active_ids and a["last"] and a["last"] < cutoff),
            key=lambda x: x["last"],
        )[:8]
    ]

    # Salud del inventario (instantánea "actual", no depende del rango). Los servicios
    # (sin stock) se excluyen: no son existencias físicas.
    phys = Product.objects.filter(is_active=True).exclude(sku__startswith=SERVICE_SKU_PREFIX)
    inv = phys.aggregate(
        cost=Sum(F("stock") * F("purchase_price_usd")),
        retail=Sum(F("stock") * F("sale_price_usd")),
        units=Sum("stock"),
    )
    active_products = phys.count()
    out_of_stock = phys.filter(stock__lte=0).count()
    low_stock = phys.filter(stock__gt=0, stock__lte=F("min_stock")).count()
    ok_stock = max(active_products - out_of_stock - low_stock, 0)
    inventory_health = {
        "active_products": active_products,
        "units_in_stock": int(inv["units"] or 0),
        "ok_stock": ok_stock,
        "low_stock": low_stock,
        "out_of_stock": out_of_stock,
        "inventory_retail_usd": _f(inv["retail"]),
    }
    if sensitive:
        inventory_health["inventory_cost_usd"] = _f(inv["cost"])

    # Contexto cambiario en el rango (explica caídas de demanda como el shock de ene-2026).
    rate_rows = list(
        ExchangeRate.objects.filter(date__gte=start, date__lte=end)
        .values("date", "bcv_rate", "parallel_rate")
        .order_by("date")
    )
    by_month_rate: dict[str, dict] = {}
    for r in rate_rows:  # asc → queda el último valor de cada mes
        by_month_rate[period_of(r["date"])] = r
    rate_series = [
        {
            "period": p,
            "label": period_label(p),
            "bcv": _f(by_month_rate[p]["bcv_rate"]) if p in by_month_rate else None,
            "parallel": _f(by_month_rate[p]["parallel_rate"]) if p in by_month_rate else None,
        }
        for p in periods
    ]
    exchange_rate = None
    if rate_rows:
        lo, hi = rate_rows[0], rate_rows[-1]
        exchange_rate = {
            "start_bcv": _f(lo["bcv_rate"]),
            "end_bcv": _f(hi["bcv_rate"]),
            "start_parallel": _f(lo["parallel_rate"]),
            "end_parallel": _f(hi["parallel_rate"]),
            "bcv_change_pct": _pct(_f(hi["bcv_rate"]), _f(lo["bcv_rate"])),
            "parallel_change_pct": _pct(_f(hi["parallel_rate"]), _f(lo["parallel_rate"])),
            "series": rate_series,
        }

    # Alertas tempranas (sistema de alerta): no resueltas, críticas primero.
    sev_order = {Alert.SeverityChoices.CRITICAL: 0, Alert.SeverityChoices.WARNING: 1, Alert.SeverityChoices.INFO: 2}
    alert_rows = sorted(
        Alert.objects.filter(is_resolved=False).order_by("-created_at")[:30],
        key=lambda a: sev_order.get(a.severity, 3),
    )
    alerts = [
        {
            "id": a.id,
            "type": a.alert_type,
            "type_label": a.get_alert_type_display(),
            "severity": a.severity,
            "severity_label": a.get_severity_display(),
            "title": a.title,
            "message": a.message,
            "created_at": a.created_at.isoformat(),
        }
        for a in alert_rows[:8]
    ]

    customers_by_state = [
        {"state": r["state"], "count": int(r["n"])}
        for r in (
            Customer.objects.exclude(state="").values("state").annotate(n=Count("id")).order_by("-n")[:8]
        )
    ]

    recent_sales = [
        {
            "id": s.id,
            "date": s.sale_date.isoformat(),
            "customer": s.customer.company_name,
            "type": s.sale_type,
            "type_label": s.get_sale_type_display(),
            "total_usd": _f(s.total_sale_usd),
            "status": s.status,
            "status_label": s.get_status_display(),
        }
        for s in cur_qs.select_related("customer").order_by("-sale_date", "-id")[:7]
    ]

    kpis = {
        "revenue": cur["revenue"],
        "revenue_delta_pct": _pct(cur["revenue"], prev["revenue"]),
        "sales_count": cur["count"],
        "sales_count_delta_pct": _pct(cur["count"], prev["count"]),
        "avg_ticket": cur["avg_ticket"],
        "avg_ticket_delta_pct": _pct(cur["avg_ticket"], prev["avg_ticket"]),
        "units_sold": int(cur_units),
        "units_delta_pct": _pct(cur_units, prev_units),
        "active_customers": cur["customers"],
        "active_customers_delta_pct": _pct(cur["customers"], prev["customers"]),
        "new_customers": new_customers,
        "quotes_issued": quotes_issued,
        "conversion_rate": conversion_rate,
        "retention_pct": retention,
    }
    if sensitive:
        kpis.update(
            {
                "profit": cur["profit"],
                "profit_delta_pct": _pct(cur["profit"], prev["profit"]),
                "margin_pct": margin,
                "margin_delta_pts": (
                    round(margin - prev_margin, 1) if (margin is not None and prev_margin is not None) else None
                ),
                "discount": cur["discount"],
            }
        )

    # Índice de Ventaja Competitiva (IVC) + competencia + salud de modelos (sensibles).
    health_index = competitive = model_health = None
    if sensitive:
        price_score, positioning = _price_competitiveness()
        competitive = {"positioning": positioning, "price_score": price_score}

        # Cada componente es un sub-score real 0-100 con su peso; el IVC es la media
        # ponderada (renormalizada sobre los componentes con datos disponibles).
        comps = []  # (key, label, score, weight, detail)
        if margin is not None:
            comps.append(("rentabilidad", "Rentabilidad", _clamp(margin / 35.0 * 100.0), 0.22, f"Margen {margin:.0f}%"))
        g = kpis["revenue_delta_pct"]
        if g is not None:
            comps.append(("crecimiento", "Crecimiento", _clamp(50.0 + g * 2.0), 0.20, f"{g:+.0f}% vs. periodo previo"))
        if conversion_rate is not None:
            comps.append(("conversion", "Conversión presupuestos", _clamp(conversion_rate), 0.15, f"{conversion_rate:.0f}% convertidos"))
        if retention is not None:
            comps.append(("retencion", "Retención de clientes", _clamp(retention), 0.15, f"{retention:.0f}% recompra"))
        if active_products:
            comps.append(("inventario", "Salud de inventario", _clamp(ok_stock / active_products * 100.0), 0.10, f"{ok_stock}/{active_products} con stock sano"))
        if price_score is not None:
            comps.append(("competitividad", "Competitividad de precio", _clamp(price_score), 0.18, "vs. precios de mercado scrapeados"))

        if comps:
            wsum = sum(c[3] for c in comps)
            score = round(sum(c[2] * c[3] for c in comps) / wsum, 1)
            health_index = {
                "score": score,
                "status": "good" if score >= 70 else "warn" if score >= 45 else "bad",
                "components": [
                    {"key": c[0], "label": c[1], "score": round(c[2], 1), "weight": round(c[3] / wsum, 2), "detail": c[4]}
                    for c in comps
                ],
            }

        model_health = [
            {
                "model_type": pl.model_type,
                "model_type_display": pl.get_model_type_display(),
                "r2": pl.r2_score,
                "rmse": pl.rmse,
                "mae": pl.mae,
                "accuracy": (pl.metrics or {}).get("accuracy"),
                "trained_at": pl.trained_at.isoformat() if pl.trained_at else None,
            }
            for pl in PredictionLog.objects.filter(is_active=True).order_by("model_type")
        ]

    bounds = Sale.objects.filter(status=COMP).aggregate(lo=Min("sale_date"), hi=Max("sale_date"))
    return {
        "range": {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "from_label": period_label(period_of(start)),
            "to_label": period_label(period_of(end)),
            "months": len(periods),
            "data_from": bounds["lo"].isoformat() if bounds["lo"] else start.isoformat(),
            "data_to": bounds["hi"].isoformat() if bounds["hi"] else end.isoformat(),
        },
        "narrative": _build_narrative(start, end, cur, prev, type_split, no_demand_count, at_risk, exchange_rate, sensitive),
        "kpis": kpis,
        "health_index": health_index,
        "monthly": monthly,
        "monthly_by_type": monthly_by_type,
        "type_split": type_split,
        "revenue_by_category": revenue_by_category,
        "top_products": top_products,
        "top_customers": top_customers,
        "no_demand": no_demand,
        "no_demand_count": no_demand_count,
        "at_risk": at_risk,
        "inventory_health": inventory_health,
        "exchange_rate": exchange_rate,
        "competitive": competitive,
        "alerts": alerts,
        "model_health": model_health,
        "customers_by_state": customers_by_state,
        "recent_sales": recent_sales,
    }


# --------------------------------------------------------------------------- #
# Clientes
# --------------------------------------------------------------------------- #
def customers(start: date, end: date) -> dict:
    """Estadísticas de clientes para el rango [start, end].

    La **composición** de la cartera (tipo, ubicación, activos vs. prospectos, totales)
    es una instantánea del estado actual (no depende del rango). Lo que sí se recalcula
    para el rango son los agregados de **compra**: rankings por ingresos/actividad y los
    clientes con compras. El corte de "en riesgo" (6 meses sin comprar) es relativo a la
    fecha final del rango.
    """
    if start > end:
        start, end = end, start
    type_labels = dict(Customer.TypeChoices.choices)

    by_type = [
        {
            "type": r["customer_type"],
            "label": str(type_labels.get(r["customer_type"], r["customer_type"])),
            "count": int(r["n"]),
        }
        for r in (
            Customer.objects.values("customer_type").annotate(n=Count("id")).order_by("-n")
        )
    ]

    by_state = [
        {"state": r["state"], "count": int(r["n"])}
        for r in (
            Customer.objects.exclude(state="")
            .values("state")
            .annotate(n=Count("id"))
            .order_by("-n")[:12]
        )
    ]

    active = Customer.objects.filter(is_active_customer=True).count()
    total = Customer.objects.count()
    prospects = total - active

    # Agregados de compra por cliente DENTRO del rango (solo ventas completadas).
    range_agg = list(
        Sale.objects.filter(status=COMP, sale_date__gte=start, sale_date__lte=end)
        .values("customer_id")
        .annotate(revenue=Sum("total_sale_usd"), orders=Count("id"), last=Max("sale_date"))
    )
    # Para "en riesgo" se necesita la última compra REAL (no la del rango), así que se
    # consulta el histórico completo aparte.
    full_agg = list(
        Sale.objects.filter(status=COMP)
        .values("customer_id")
        .annotate(revenue=Sum("total_sale_usd"), orders=Count("id"), last=Max("sale_date"))
    )
    names = _customer_names({a["customer_id"] for a in range_agg} | {a["customer_id"] for a in full_agg})

    def row(a):
        info = names.get(a["customer_id"], {})
        return {
            "customer_id": a["customer_id"],
            "name": info.get("company_name", f"#{a['customer_id']}"),
            "type": info.get("customer_type", "—"),
            "state": info.get("state", "—"),
            "revenue": _f(a["revenue"]),
            "orders": int(a["orders"]),
            "last_purchase": a["last"].isoformat() if a["last"] else None,
        }

    top_by_revenue = [row(a) for a in sorted(range_agg, key=lambda x: _f(x["revenue"]), reverse=True)[:10]]
    top_by_orders = [row(a) for a in sorted(range_agg, key=lambda x: x["orders"], reverse=True)[:10]]

    # Clientes en riesgo: activos cuya última compra es anterior al corte (end - 6 meses).
    cutoff = _first_of(add_period(period_of(end), -6))
    active_ids = set(
        Customer.objects.filter(is_active_customer=True).values_list("id", flat=True)
    )
    at_risk = [
        row(a)
        for a in sorted(
            (a for a in full_agg if a["customer_id"] in active_ids and a["last"] and a["last"] < cutoff),
            key=lambda x: x["last"],
        )[:10]
    ]

    return {
        "range": _range_block(start, end),
        "by_type": by_type,
        "by_state": by_state,
        "active_split": [
            {"key": "active", "label": "Clientes activos", "count": active},
            {"key": "prospect", "label": "Prospectos", "count": prospects},
        ],
        "totals": {
            "total": total,
            "active": active,
            "prospects": prospects,
            "with_purchases": len(range_agg),
        },
        "top_by_revenue": top_by_revenue,
        "top_by_orders": top_by_orders,
        "at_risk": at_risk,
    }


# --------------------------------------------------------------------------- #
# Productos
# --------------------------------------------------------------------------- #
def products(start: date, end: date) -> dict:
    """Estadísticas de productos para el rango [start, end].

    La **composición** del catálogo y el **inventario** (categorías, materiales,
    activos/inactivos, estado y valor de stock) son una instantánea actual. Lo que se
    recalcula para el rango son las **ventas por producto**: más vendidos por unidades/
    ingresos y los que no rotaron (sin ventas en el rango).
    """
    if start > end:
        start, end = end, start
    material_labels = dict(Product.MaterialChoices.choices)

    by_category = [
        {"category": r["category__name"] or "Sin categoría", "count": int(r["n"])}
        for r in (
            Product.objects.filter(is_active=True)
            .values("category__name")
            .annotate(n=Count("id"))
            .order_by("-n")
        )
    ]

    by_material = [
        {
            "material": r["material"] or "OTHER",
            "label": str(material_labels.get(r["material"], "Sin especificar")),
            "count": int(r["n"]),
        }
        for r in (
            Product.objects.filter(is_active=True)
            .values("material")
            .annotate(n=Count("id"))
            .order_by("-n")
        )
    ]

    active = Product.objects.filter(is_active=True).count()
    inactive = Product.objects.filter(is_active=False).count()

    # Estado de existencias y valor de inventario: solo productos FÍSICOS (los servicios
    # no llevan stock). El conteo de catálogo (`active`) sí incluye los servicios.
    phys = Product.objects.filter(is_active=True).exclude(sku__startswith=SERVICE_SKU_PREFIX)
    phys_active = phys.count()
    out_of_stock = phys.filter(stock__lte=0).count()
    low_stock = phys.filter(stock__gt=0, stock__lte=F("min_stock")).count()
    ok_stock = phys_active - out_of_stock - low_stock

    # Valor del inventario (a costo y a precio de venta).
    val = phys.aggregate(
        cost=Sum(F("stock") * F("purchase_price_usd")),
        retail=Sum(F("stock") * F("sale_price_usd")),
        units=Sum("stock"),
    )

    # Ventas por producto (unidades e ingreso) — ventas completadas DENTRO del rango.
    item_agg = list(
        SaleItem.objects.filter(
            sale__status=COMP, sale__sale_date__gte=start, sale__sale_date__lte=end
        )
        .values("product_id")
        .annotate(units=Sum("quantity"), revenue=Sum("subtotal_sale_usd"))
    )
    pnames = {
        p.id: {"name": p.name, "sku": p.sku, "category": p.category.name if p.category else "—"}
        for p in Product.objects.filter(id__in=[a["product_id"] for a in item_agg]).select_related("category")
    }

    def prow(a):
        info = pnames.get(a["product_id"], {})
        return {
            "product_id": a["product_id"],
            "name": info.get("name", f"#{a['product_id']}"),
            "sku": info.get("sku"),
            "category": info.get("category", "—"),
            "units": int(a["units"] or 0),
            "revenue": _f(a["revenue"]),
        }

    top_by_units = [prow(a) for a in sorted(item_agg, key=lambda x: x["units"] or 0, reverse=True)[:10]]
    top_by_revenue = [prow(a) for a in sorted(item_agg, key=lambda x: _f(x["revenue"]), reverse=True)[:10]]

    # Productos activos sin ventas en el rango (baja rotación). Los servicios no son
    # "lentos" por rotación de stock, así que se excluyen de este conteo.
    sold_ids = {a["product_id"] for a in item_agg}
    no_sales_qs = (
        Product.objects.filter(is_active=True)
        .exclude(id__in=sold_ids)
        .exclude(sku__startswith=SERVICE_SKU_PREFIX)
        .select_related("category")
    )
    slow_movers = [
        {
            "product_id": p.id,
            "name": p.name,
            "sku": p.sku,
            "category": p.category.name if p.category else "—",
            "stock": p.stock,
        }
        for p in no_sales_qs.order_by("-stock")[:10]
    ]

    return {
        "range": _range_block(start, end),
        "by_category": by_category,
        "by_material": by_material,
        "active_split": [
            {"key": "active", "label": "Activos", "count": active},
            {"key": "inactive", "label": "Inactivos", "count": inactive},
        ],
        "stock_status": [
            {"key": "ok", "label": "Con stock", "count": max(ok_stock, 0)},
            {"key": "low", "label": "Stock bajo", "count": low_stock},
            {"key": "out", "label": "Sin stock", "count": out_of_stock},
        ],
        "totals": {
            "active": active,
            "inactive": inactive,
            "units_in_stock": int(val["units"] or 0),
            "inventory_cost_usd": _f(val["cost"]),
            "inventory_retail_usd": _f(val["retail"]),
            "no_sales_count": no_sales_qs.count(),
        },
        "top_by_units": top_by_units,
        "top_by_revenue": top_by_revenue,
        "slow_movers": slow_movers,
    }


# --------------------------------------------------------------------------- #
# Ventas
# --------------------------------------------------------------------------- #
def sales(start: date, end: date) -> dict:
    """Estadísticas de ventas para el rango [start, end] (todo se recalcula).

    Composición detal/institucional, tendencia mensual (dentro del rango), ingresos por
    categoría, mejores vendedores y totales, todo medido sobre ventas completadas en el
    intervalo elegido.
    """
    if start > end:
        start, end = end, start
    type_labels = dict(Sale.TypeChoices.choices)
    in_range = Sale.objects.filter(status=COMP, sale_date__gte=start, sale_date__lte=end)

    by_type = [
        {
            "type": r["sale_type"],
            "label": str(type_labels.get(r["sale_type"], r["sale_type"])),
            "count": int(r["count"]),
            "revenue": _f(r["revenue"]),
            "profit": _f(r["profit"]),
        }
        for r in (
            in_range
            .values("sale_type")
            .annotate(count=Count("id"), revenue=Sum("total_sale_usd"), profit=Sum("total_profit_usd"))
            .order_by("-revenue")
        )
    ]

    # Serie mensual (ingresos/utilidad/nº de ventas) dentro del rango, ejes continuos.
    p_start, p_end = period_of(start), period_of(end)
    periods = month_range(p_start, p_end)
    month_rows = (
        in_range.annotate(m=TruncMonth("sale_date"))
        .values("m", "sale_type")
        .annotate(revenue=Sum("total_sale_usd"), profit=Sum("total_profit_usd"), count=Count("id"))
    )
    agg_month: dict[str, dict] = {}
    for r in month_rows:
        p = period_of(r["m"])
        slot = agg_month.setdefault(p, {"revenue": 0.0, "profit": 0.0, "count": 0, "RET": 0.0, "INS": 0.0})
        slot["revenue"] += _f(r["revenue"])
        slot["profit"] += _f(r["profit"])
        slot["count"] += int(r["count"])
        if r["sale_type"] == Sale.TypeChoices.RETAIL:
            slot["RET"] += _f(r["revenue"])
        elif r["sale_type"] == Sale.TypeChoices.INSTITUTIONAL:
            slot["INS"] += _f(r["revenue"])
    monthly = [
        {
            "period": p,
            "label": period_label(p),
            "revenue": agg_month.get(p, {}).get("revenue", 0.0),
            "profit": agg_month.get(p, {}).get("profit", 0.0),
            "count": agg_month.get(p, {}).get("count", 0),
        }
        for p in periods
    ]
    monthly_by_type = [
        {
            "period": p,
            "label": period_label(p),
            "retail": agg_month.get(p, {}).get("RET", 0.0),
            "institutional": agg_month.get(p, {}).get("INS", 0.0),
        }
        for p in periods
    ]

    revenue_by_category = [
        {"category": r["product__category__name"] or "Sin categoría", "revenue": _f(r["revenue"])}
        for r in (
            SaleItem.objects.filter(sale__in=in_range)
            .values("product__category__name")
            .annotate(revenue=Sum("subtotal_sale_usd"))
            .order_by("-revenue")[:10]
        )
    ]

    seller_rows = (
        in_range
        .values("seller_id", "seller__first_name", "seller__last_name")
        .annotate(revenue=Sum("total_sale_usd"), profit=Sum("total_profit_usd"), count=Count("id"))
        .order_by("-revenue")[:10]
    )
    top_sellers = [
        {
            "seller_id": r["seller_id"],
            "name": f"{r['seller__first_name']} {r['seller__last_name']}".strip() or f"#{r['seller_id']}",
            "revenue": _f(r["revenue"]),
            "profit": _f(r["profit"]),
            "count": int(r["count"]),
        }
        for r in seller_rows
    ]

    totals = in_range.aggregate(
        revenue=Sum("total_sale_usd"),
        profit=Sum("total_profit_usd"),
        discount=Sum("total_discount_usd"),
        count=Count("id"),
        avg_ticket=Avg("total_sale_usd"),
    )
    revenue = _f(totals["revenue"])
    profit = _f(totals["profit"])

    return {
        "range": _range_block(start, end),
        "by_type": by_type,
        "monthly": monthly,
        "monthly_by_type": monthly_by_type,
        "revenue_by_category": revenue_by_category,
        "top_sellers": top_sellers,
        "totals": {
            "revenue": revenue,
            "profit": profit,
            "discount": _f(totals["discount"]),
            "count": int(totals["count"] or 0),
            "avg_ticket": _f(totals["avg_ticket"]),
            "margin_pct": round(profit / revenue * 100, 1) if revenue else None,
        },
    }


# --------------------------------------------------------------------------- #
# Presupuestos
# --------------------------------------------------------------------------- #
def quotes(start: date, end: date) -> dict:
    """Estadísticas de presupuestos para el rango [start, end].

    Todo se mide sobre los presupuestos **emitidos** dentro del intervalo (``issued_date``
    en el rango): mezcla por estado, conversión/cierre, tendencia emitidos vs. convertidos,
    pipeline abierto y los de mayor valor. Los presupuestos sin fecha de emisión (borradores)
    quedan fuera, como en el panel de Inicio.
    """
    if start > end:
        start, end = end, start
    status_labels = dict(Quote.StatusChoices.choices)
    CON = Quote.StatusChoices.CONVERTED
    REJ = Quote.StatusChoices.REJECTED
    OPEN = [Quote.StatusChoices.DRAFT, Quote.StatusChoices.SENT, Quote.StatusChoices.APPROVED]

    in_range = Quote.objects.filter(issued_date__gte=start, issued_date__lte=end)

    by_status = [
        {
            "status": r["status"],
            "label": str(status_labels.get(r["status"], r["status"])),
            "count": int(r["count"]),
            "value": _f(r["value"]),
        }
        for r in (
            in_range.values("status")
            .annotate(count=Count("id"), value=Sum("total_usd"))
            .order_by("-count")
        )
    ]

    total = in_range.count()
    converted = in_range.filter(status=CON).count()
    rejected = in_range.filter(status=REJ).count()
    conversion_rate = round(converted / total * 100, 1) if total else None
    win_rate = round(converted / (converted + rejected) * 100, 1) if (converted + rejected) else None

    # Emitidos vs. convertidos por mes dentro del rango (por fecha de emisión).
    periods = month_range(period_of(start), period_of(end))
    rows = (
        in_range.annotate(m=TruncMonth("issued_date"))
        .values("m")
        .annotate(issued=Count("id"), converted=Count("id", filter=Q(status=CON)))
    )
    by_period = {period_of(r["m"]): r for r in rows}
    monthly = [
        {
            "period": p,
            "label": period_label(p),
            "issued": int(by_period[p]["issued"]) if p in by_period else 0,
            "converted": int(by_period[p]["converted"]) if p in by_period else 0,
        }
        for p in periods
    ]

    pipeline = in_range.filter(status__in=OPEN).aggregate(
        count=Count("id"), value=Sum("total_usd")
    )

    top = (
        in_range.select_related("customer")
        .order_by("-total_usd")[:10]
    )
    top_quotes = [
        {
            "id": q.id,
            "quote_number": q.quote_number,
            "customer": q.customer.company_name,
            "total_usd": _f(q.total_usd),
            "status": q.status,
            "status_label": q.get_status_display(),
            "issued_date": q.issued_date.isoformat() if q.issued_date else None,
        }
        for q in top
    ]

    return {
        "range": _range_block(start, end),
        "by_status": by_status,
        "monthly": monthly,
        "extras": [
            {"key": "installation", "label": "Con instalación", "count": in_range.filter(includes_installation=True).count()},
            {"key": "delivery", "label": "Con despacho", "count": in_range.filter(includes_delivery=True).count()},
        ],
        "totals": {
            "total": total,
            "converted": converted,
            "rejected": rejected,
            "conversion_rate": conversion_rate,
            "win_rate": win_rate,
            "open_count": int(pipeline["count"] or 0),
            "pipeline_value": _f(pipeline["value"]),
        },
        "top_quotes": top_quotes,
    }

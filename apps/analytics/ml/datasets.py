"""Construcción de DataFrames de pandas a partir del ORM.

Todas las funciones son de **solo lectura** y agregan los datos transaccionales a la
granularidad mensual que usan los modelos. Las series mensuales se devuelven indexadas
por el periodo ``"YYYY-MM"`` y con los huecos completados (un mes sin actividad es un
0 o un valor arrastrado, según la magnitud), porque los modelos necesitan una serie
continua para construir los rezagos.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta

import pandas as pd
from django.db.models import Count, Sum
from django.db.models.functions import Coalesce, TruncMonth

from apps.benchmarking.models import CompetitorMarketData
from apps.core.models import ExchangeRate, Product, ProductPriceHistory
from apps.sales.models import Quote, Sale, SaleItem

from .features import month_range, period_label, period_of

logger = logging.getLogger(__name__)

COMPLETED = Sale.StatusChoices.COMPLETED

# Fuentes de competencia EXCLUIDAS de todo el análisis/entrenamiento. Facebook
# Marketplace se descarta por decisión del proyecto (recomendación del tutor): el
# scraper se conserva, pero sus datos no se usan en ninguna analítica ni en la UI.
EXCLUDED_COMPETITOR_SOURCES = ("FB",)


# --------------------------------------------------------------------------- #
# Fecha de corte del entrenamiento ("hasta dónde son datos, desde dónde pronóstico")
# --------------------------------------------------------------------------- #
#
# Problema que resuelve: si en la BD hay registros recientes de PRUEBA (o de un mes
# todavía incompleto), los modelos los aprenden como si fueran historia real y ensucian
# el pronóstico. El corte marca la frontera: todo lo posterior se excluye del
# entrenamiento y el pronóstico arranca justo después.
#
# El corte se configura en caliente (``SystemSettings.training_cutoff_date``, editable
# desde Configuración o al reentrenar) y lo aplican TODAS las series internas de abajo,
# así que basta con leerlo aquí. Las observaciones de competencia quedan FUERA del corte
# a propósito: el módulo de benchmarking tiene su propia "máquina del tiempo" (rango
# ``start``/``end`` explícito) sobre datos externos, no sobre el historial de la empresa.


def configured_cutoff():
    """Fecha de corte tal cual está configurada (``date``) o ``None`` si no hay.

    Nunca lanza: si la configuración/BD no está disponible devuelve ``None``, es decir,
    el comportamiento histórico (entrenar con todo).
    """
    try:
        from apps.core import system_settings

        return system_settings.training_cutoff_date()
    except Exception as exc:  # pragma: no cover - configuración/BD no disponible
        logger.debug("No se pudo leer la fecha de corte de entrenamiento (%s).", exc)
        return None


def snap_cutoff(raw: date | None) -> date | None:
    """Ajusta el corte al **último día de un mes completo**.

    Los modelos trabajan a granularidad mensual, así que cortar a mitad de mes dejaría
    un último punto parcial (medio mes de ventas parece un desplome) — exactamente el
    tipo de dato sucio que el corte pretende evitar. Si la fecha no es el último día de
    su mes, se descarta ese mes entero y el corte efectivo pasa a ser el último día del
    mes anterior.
    """
    if raw is None:
        return None
    last_day = calendar.monthrange(raw.year, raw.month)[1]
    if raw.day >= last_day:
        return raw
    return raw.replace(day=1) - timedelta(days=1)


def training_cutoff() -> date | None:
    """Fecha de corte **efectiva** que aplican las series de entrenamiento."""
    return snap_cutoff(configured_cutoff())


def cutoff_info() -> dict:
    """Bloque informativo del corte, para exponerlo en la API/UI.

    ``configured`` es lo que eligió el usuario y ``effective`` lo que realmente se aplica
    (ajustado a mes completo por ``snap_cutoff``); son distintos cuando se eligió una
    fecha a mitad de mes.
    """
    raw = configured_cutoff()
    eff = snap_cutoff(raw)
    period = period_of(eff) if eff else None
    return {
        "active": eff is not None,
        "configured": raw.isoformat() if raw else None,
        "effective": eff.isoformat() if eff else None,
        "effective_period": period,
        "effective_label": period_label(period) if period else None,
        "adjusted": bool(raw and eff and raw != eff),
    }


def effective_obs_date():
    """Expresión ORM de la fecha EFECTIVA de una observación de competencia.

    Usa ``posted_at`` (la fecha real de publicación, que sólo trae el scraper de
    Instagram) cuando existe, y si no ``scraped_at`` (la fecha del scraping). Así un
    post viejo scrapeado hoy se ubica en su mes real: sus precios/promociones son de
    aquella fecha, no de la del scraping. Para las demás fuentes ``posted_at`` es NULL
    y la expresión cae naturalmente en ``scraped_at``.

    Pensada para anotar el queryset y luego filtrar/ordenar por la fecha efectiva
    (``effective_at``), en lugar de por ``scraped_at`` directamente.
    """
    return Coalesce("posted_at", "scraped_at")


def _reindex_monthly(df: pd.DataFrame, value_cols: dict[str, str]) -> pd.DataFrame:
    """Reindexa un DataFrame con columna ``period`` a un rango mensual completo.

    ``value_cols`` mapea columna -> método de relleno (``"zero"`` o ``"ffill"``).
    """
    if df.empty:
        return df
    df = df.set_index("period").sort_index()
    full = month_range(df.index.min(), df.index.max())
    df = df.reindex(full)
    for col, how in value_cols.items():
        if how == "ffill":
            df[col] = df[col].ffill().bfill()
        else:  # zero
            df[col] = df[col].fillna(0.0)
    df.index.name = "period"
    return df


# --------------------------------------------------------------------------- #
# Ventas / ingresos / utilidad (a nivel empresa)
# --------------------------------------------------------------------------- #
def monthly_company() -> pd.DataFrame:
    """Serie mensual a nivel empresa: ingresos, costo, utilidad, nº de ventas, margen %.

    Respeta la fecha de corte del entrenamiento (ver ``training_cutoff``)."""
    qs = Sale.objects.filter(status=COMPLETED)
    cutoff = training_cutoff()
    if cutoff:
        qs = qs.filter(sale_date__lte=cutoff)
    rows = list(
        qs.annotate(m=TruncMonth("sale_date"))
        .values("m")
        .annotate(
            revenue=Sum("total_sale_usd"),
            cost=Sum("total_cost_usd"),
            profit=Sum("total_profit_usd"),
            n=Count("id"),
        )
        .order_by("m")
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["period"] = df["m"].map(period_of)
    df = df[["period", "revenue", "cost", "profit", "n"]].astype(
        {"revenue": float, "cost": float, "profit": float, "n": float}
    )
    df = _reindex_monthly(
        df, {"revenue": "zero", "cost": "zero", "profit": "zero", "n": "zero"}
    )
    df["margin"] = df.apply(
        lambda r: (r["profit"] / r["revenue"] * 100.0) if r["revenue"] else 0.0, axis=1
    )
    return df


# --------------------------------------------------------------------------- #
# Demanda por producto (panel)
# --------------------------------------------------------------------------- #
def monthly_demand_panel() -> pd.DataFrame:
    """Panel mensual por producto: unidades e ingreso (filas largas, sin completar huecos).

    Respeta la fecha de corte del entrenamiento (ver ``training_cutoff``)."""
    qs = SaleItem.objects.filter(sale__status=COMPLETED)
    cutoff = training_cutoff()
    if cutoff:
        qs = qs.filter(sale__sale_date__lte=cutoff)
    rows = list(
        qs.annotate(m=TruncMonth("sale__sale_date"))
        .values("product_id", "product__name", "product__sku", "product__category_id", "m")
        .annotate(units=Sum("quantity"), revenue=Sum("subtotal_sale_usd"))
        .order_by("product_id", "m")
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["period"] = df["m"].map(period_of)
    df = df.rename(
        columns={
            "product__name": "product_name",
            "product__sku": "sku",
            "product__category_id": "category_id",
        }
    )
    df["units"] = df["units"].astype(float)
    df["revenue"] = df["revenue"].astype(float)
    df["category_id"] = df["category_id"].fillna(0).astype(int)
    return df[
        ["product_id", "product_name", "sku", "category_id", "period", "units", "revenue"]
    ]


def demand_series(product_id: int, panel: pd.DataFrame | None = None) -> list[tuple[str, float]]:
    """Serie mensual de unidades de un producto, con huecos = 0 dentro de su vida útil."""
    panel = panel if panel is not None else monthly_demand_panel()
    if panel.empty:
        return []
    sub = panel[panel["product_id"] == product_id]
    if sub.empty:
        return []
    from .features import complete_monthly

    pairs = list(zip(sub["period"], sub["units"]))
    # Completa desde el primer mes con ventas hasta el último mes del panel global.
    return complete_monthly(pairs, fill=0.0, end=panel["period"].max())


def sale_items_for_month(product_id: int, period: str):
    """Líneas de venta de un producto en un mes (para el desglose 'Ver datos')."""
    year, month = period.split("-")
    qs = SaleItem.objects.filter(
        sale__status=COMPLETED,
        product_id=product_id,
        sale__sale_date__year=int(year),
        sale__sale_date__month=int(month),
    )
    cutoff = training_cutoff()
    if cutoff:
        qs = qs.filter(sale__sale_date__lte=cutoff)
    return list(qs.select_related("sale", "sale__customer").order_by("sale__sale_date"))


def sales_for_month(period: str):
    """Ventas completadas de un mes (desglose de ventas/ingresos/utilidad)."""
    year, month = period.split("-")
    qs = Sale.objects.filter(
        status=COMPLETED, sale_date__year=int(year), sale_date__month=int(month)
    )
    cutoff = training_cutoff()
    if cutoff:
        qs = qs.filter(sale_date__lte=cutoff)
    return list(qs.select_related("customer", "seller").order_by("sale_date"))


# --------------------------------------------------------------------------- #
# Tasa de cambio
# --------------------------------------------------------------------------- #
def monthly_exchange_rate() -> pd.DataFrame:
    """Serie mensual de las tres tasas (último valor del mes, arrastrado).

    Columnas: ``bcv_rate`` (Dólar BCV), ``eur_bcv_rate`` (Euro BCV) y ``parallel_rate``
    (paralelo, referencial). El **shock cambiario** que consumen los modelos se sigue
    calculando sobre el paralelo (``_rate_shock_map``): es la tasa que refleja el valor
    real del dinero y por tanto la que explica las caídas de demanda.

    Respeta la fecha de corte del entrenamiento (ver ``training_cutoff``)."""
    qs = ExchangeRate.objects.all()
    cutoff = training_cutoff()
    if cutoff:
        qs = qs.filter(date__lte=cutoff)
    rows = list(qs.values("date", "bcv_rate", "eur_bcv_rate", "parallel_rate").order_by("date"))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["period"] = df["date"].map(period_of)
    for col in ("bcv_rate", "eur_bcv_rate", "parallel_rate"):
        df[col] = df[col].astype(float)
    # Último valor de cada mes (las filas ya vienen ordenadas por fecha).
    df = df.groupby("period", as_index=False).last()
    df = df[["period", "bcv_rate", "eur_bcv_rate", "parallel_rate"]]
    return _reindex_monthly(
        df, {"bcv_rate": "ffill", "eur_bcv_rate": "ffill", "parallel_rate": "ffill"}
    )


def exchange_rate_for_month(period: str):
    """Registros de tasa de un mes (desglose)."""
    year, month = period.split("-")
    qs = ExchangeRate.objects.filter(date__year=int(year), date__month=int(month))
    cutoff = training_cutoff()
    if cutoff:
        qs = qs.filter(date__lte=cutoff)
    return list(qs.order_by("date"))


# --------------------------------------------------------------------------- #
# Precio de producto
# --------------------------------------------------------------------------- #
def product_price_series(product_id: int) -> pd.DataFrame:
    """Serie mensual de precio de venta/compra de un producto (último del mes, arrastrado).

    Respeta la fecha de corte del entrenamiento (ver ``training_cutoff``)."""
    qs = ProductPriceHistory.objects.filter(product_id=product_id)
    cutoff = training_cutoff()
    if cutoff:
        qs = qs.filter(changed_at__lte=cutoff)
    rows = list(
        qs.values("changed_at", "sale_price_usd", "purchase_price_usd").order_by("changed_at")
    )
    if not rows:
        # Sin historial: usa el precio actual como punto único (en el mes del corte si lo hay).
        p = Product.objects.filter(id=product_id).first()
        if not p:
            return pd.DataFrame()
        period = period_of(cutoff or date.today())
        return pd.DataFrame(
            {
                "sale_price_usd": [float(p.sale_price_usd or 0)],
                "purchase_price_usd": [float(p.purchase_price_usd or 0)],
            },
            index=pd.Index([period], name="period"),
        )
    df = pd.DataFrame(rows)
    df["period"] = df["changed_at"].map(period_of)
    df["sale_price_usd"] = df["sale_price_usd"].astype(float)
    df["purchase_price_usd"] = df["purchase_price_usd"].astype(float)
    df = df.groupby("period", as_index=False).last()
    df = df[["period", "sale_price_usd", "purchase_price_usd"]]
    return _reindex_monthly(df, {"sale_price_usd": "ffill", "purchase_price_usd": "ffill"})


def price_changes_for_month(product_id: int, period: str):
    """Cambios de precio registrados de un producto en un mes (desglose)."""
    year, month = period.split("-")
    qs = ProductPriceHistory.objects.filter(
        product_id=product_id, changed_at__year=int(year), changed_at__month=int(month)
    )
    cutoff = training_cutoff()
    if cutoff:
        qs = qs.filter(changed_at__lte=cutoff)
    return list(qs.order_by("changed_at"))


# --------------------------------------------------------------------------- #
# Presupuestos (clasificación de conversión)
# --------------------------------------------------------------------------- #
def quotes_dataframe() -> pd.DataFrame:
    """Presupuestos con variables de entrada + etiqueta ``converted`` (0/1).

    A diferencia de las demás series, aquí el corte **no filtra filas**: añade la columna
    ``in_training`` (emitido hasta el corte). El clasificador se ajusta y se evalúa solo
    con las filas ``in_training``, pero el *pipeline* de presupuestos abiertos debe seguir
    incluyendo los posteriores al corte — son justamente los que se quieren predecir.
    """
    rows = list(
        Quote.objects.annotate(n_items=Count("items"))
        .values(
            "id", "quote_number", "issued_date", "total_usd", "subtotal_usd",
            "includes_installation", "includes_delivery", "status",
            "converted_to_sale_id", "n_items", "customer__customer_type",
            "customer__company_name",
        )
        .order_by("issued_date")
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["total_usd"] = df["total_usd"].astype(float)
    df["subtotal_usd"] = df["subtotal_usd"].astype(float)
    df["n_items"] = df["n_items"].astype(int)
    df["includes_installation"] = df["includes_installation"].astype(int)
    df["includes_delivery"] = df["includes_delivery"].astype(int)
    df["issued_month"] = df["issued_date"].map(lambda d: d.month)
    df["period"] = df["issued_date"].map(period_of)
    df["converted"] = (
        (df["status"] == Quote.StatusChoices.CONVERTED)
        | df["converted_to_sale_id"].notna()
    ).astype(int)
    df["is_open"] = df["status"].isin(
        [Quote.StatusChoices.DRAFT, Quote.StatusChoices.SENT, Quote.StatusChoices.APPROVED]
    )
    cutoff = training_cutoff()
    df["in_training"] = True if cutoff is None else df["issued_date"].map(lambda d: d <= cutoff)
    df = df.rename(columns={"customer__customer_type": "customer_type",
                            "customer__company_name": "customer_name"})
    return df


# --------------------------------------------------------------------------- #
# Datos de competidores (análisis separado)
# --------------------------------------------------------------------------- #
def competitor_observations(
    category: str | None = None,
    product_id: int | None = None,
    start=None,
    end=None,
) -> pd.DataFrame:
    """Observaciones de mercado de competidores con precio en USD, deduplicadas.

    Cada fila es una observación; nos quedamos con la última por ``listing_key``
    (semántica de observación del benchmarking) para no contar dos veces un re-scrape.

    ``start``/``end`` (``datetime.date``) acotan la ventana por la fecha EFECTIVA de la
    observación (``effective_obs_date``: la fecha de publicación del post en Instagram,
    o ``scraped_at`` en el resto) ANTES de deduplicar, de modo que cada anuncio queda
    con su última observación *dentro* del rango elegido (la "máquina del tiempo" del
    módulo de benchmarking). Los modelos se entrenan sobre esta fecha efectiva, no
    sobre ``scraped_at``, para no fechar en el mes del scraping un post antiguo.

    La **fecha de corte del entrenamiento** NO aplica aquí: es un límite sobre el
    historial interno de la empresa, mientras que el benchmarking acota los datos
    externos con su propio rango explícito (``start``/``end``).
    """
    qs = (
        CompetitorMarketData.objects.filter(price_usd__isnull=False)
        .exclude(source__in=EXCLUDED_COMPETITOR_SOURCES)
        .select_related("competitor", "product")
        .annotate(effective_at=effective_obs_date())
    )
    if category:
        qs = qs.filter(category__iexact=category)
    if product_id:
        qs = qs.filter(product_id=product_id)
    if start is not None:
        qs = qs.filter(effective_at__date__gte=start)
    if end is not None:
        qs = qs.filter(effective_at__date__lte=end)
    rows = []
    for r in qs.order_by("-effective_at"):
        rows.append(
            {
                "id": r.id,
                "competitor": (r.competitor.name if r.competitor else r.competitor_name) or "Desconocido",
                "product_name": r.product_name or "",
                "category": r.category or "Sin categoría",
                "price_usd": float(r.price_usd),
                "matched_product_id": r.product_id,
                "matched_product": r.product.name if r.product else None,
                "in_stock": r.is_in_stock,
                "source": r.source,
                "listing_key": r.listing_key or f"_row{r.id}",
                "scraped_at": r.scraped_at,
                "effective_at": r.effective_at,
                "period": period_of(r.effective_at.date()),
            }
        )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    # Última observación por anuncio (ya viene ordenado desc por la fecha efectiva).
    df = df.drop_duplicates(subset="listing_key", keep="first")
    return df

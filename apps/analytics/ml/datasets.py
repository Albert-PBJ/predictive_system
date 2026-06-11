"""Construcción de DataFrames de pandas a partir del ORM.

Todas las funciones son de **solo lectura** y agregan los datos transaccionales a la
granularidad mensual que usan los modelos. Las series mensuales se devuelven indexadas
por el periodo ``"YYYY-MM"`` y con los huecos completados (un mes sin actividad es un
0 o un valor arrastrado, según la magnitud), porque los modelos necesitan una serie
continua para construir los rezagos.
"""

from __future__ import annotations

import pandas as pd
from django.db.models import Count, Sum
from django.db.models.functions import TruncMonth

from apps.benchmarking.models import CompetitorMarketData
from apps.core.models import ExchangeRate, Product, ProductPriceHistory
from apps.sales.models import Quote, Sale, SaleItem

from .features import month_range, period_of

COMPLETED = Sale.StatusChoices.COMPLETED

# Fuentes de competencia EXCLUIDAS de todo el análisis/entrenamiento. Facebook
# Marketplace se descarta por decisión del proyecto (recomendación del tutor): el
# scraper se conserva, pero sus datos no se usan en ninguna analítica ni en la UI.
EXCLUDED_COMPETITOR_SOURCES = ("FB",)


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
    """Serie mensual a nivel empresa: ingresos, costo, utilidad, nº de ventas, margen %."""
    rows = list(
        Sale.objects.filter(status=COMPLETED)
        .annotate(m=TruncMonth("sale_date"))
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
    """Panel mensual por producto: unidades e ingreso (filas largas, sin completar huecos)."""
    rows = list(
        SaleItem.objects.filter(sale__status=COMPLETED)
        .annotate(m=TruncMonth("sale__sale_date"))
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
    return list(
        SaleItem.objects.filter(
            sale__status=COMPLETED,
            product_id=product_id,
            sale__sale_date__year=int(year),
            sale__sale_date__month=int(month),
        )
        .select_related("sale", "sale__customer")
        .order_by("sale__sale_date")
    )


def sales_for_month(period: str):
    """Ventas completadas de un mes (desglose de ventas/ingresos/utilidad)."""
    year, month = period.split("-")
    return list(
        Sale.objects.filter(
            status=COMPLETED, sale_date__year=int(year), sale_date__month=int(month)
        )
        .select_related("customer", "seller")
        .order_by("sale_date")
    )


# --------------------------------------------------------------------------- #
# Tasa de cambio
# --------------------------------------------------------------------------- #
def monthly_exchange_rate() -> pd.DataFrame:
    """Serie mensual de la tasa BCV y paralela (último valor del mes, arrastrado)."""
    rows = list(
        ExchangeRate.objects.values("date", "bcv_rate", "parallel_rate").order_by("date")
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["period"] = df["date"].map(period_of)
    df["bcv_rate"] = df["bcv_rate"].astype(float)
    df["parallel_rate"] = df["parallel_rate"].astype(float)
    # Último valor de cada mes (las filas ya vienen ordenadas por fecha).
    df = df.groupby("period", as_index=False).last()
    df = df[["period", "bcv_rate", "parallel_rate"]]
    return _reindex_monthly(df, {"bcv_rate": "ffill", "parallel_rate": "ffill"})


def exchange_rate_for_month(period: str):
    """Registros de tasa de un mes (desglose)."""
    year, month = period.split("-")
    return list(
        ExchangeRate.objects.filter(date__year=int(year), date__month=int(month)).order_by("date")
    )


# --------------------------------------------------------------------------- #
# Precio de producto
# --------------------------------------------------------------------------- #
def product_price_series(product_id: int) -> pd.DataFrame:
    """Serie mensual de precio de venta/compra de un producto (último del mes, arrastrado)."""
    rows = list(
        ProductPriceHistory.objects.filter(product_id=product_id)
        .values("changed_at", "sale_price_usd", "purchase_price_usd")
        .order_by("changed_at")
    )
    if not rows:
        # Sin historial: usa el precio actual como punto único.
        p = Product.objects.filter(id=product_id).first()
        if not p:
            return pd.DataFrame()
        from datetime import date

        period = period_of(date.today())
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
    return list(
        ProductPriceHistory.objects.filter(
            product_id=product_id, changed_at__year=int(year), changed_at__month=int(month)
        ).order_by("changed_at")
    )


# --------------------------------------------------------------------------- #
# Presupuestos (clasificación de conversión)
# --------------------------------------------------------------------------- #
def quotes_dataframe() -> pd.DataFrame:
    """Presupuestos con variables de entrada + etiqueta ``converted`` (0/1)."""
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

    ``start``/``end`` (``datetime.date``) acotan la ventana por ``scraped_at`` ANTES de
    deduplicar, de modo que cada anuncio queda con su última observación *dentro* del
    rango elegido (la "máquina del tiempo" del módulo de benchmarking).
    """
    qs = (
        CompetitorMarketData.objects.filter(price_usd__isnull=False)
        .exclude(source__in=EXCLUDED_COMPETITOR_SOURCES)
        .select_related("competitor", "product")
    )
    if category:
        qs = qs.filter(category__iexact=category)
    if product_id:
        qs = qs.filter(product_id=product_id)
    if start is not None:
        qs = qs.filter(scraped_at__date__gte=start)
    if end is not None:
        qs = qs.filter(scraped_at__date__lte=end)
    rows = []
    for r in qs.order_by("-scraped_at"):
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
                "period": period_of(r.scraped_at.date()),
            }
        )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    # Última observación por anuncio (ya viene ordenado desc por scraped_at).
    df = df.drop_duplicates(subset="listing_key", keep="first")
    return df

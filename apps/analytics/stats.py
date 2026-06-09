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

from datetime import date

from django.db.models import Avg, Count, F, Max, Q, Sum
from django.db.models.functions import TruncMonth

from apps.core.models import Customer, Product
from apps.sales.models import Quote, Sale, SaleItem

from .ml.features import add_period, month_range, period_label, period_of

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


def _monthly_sales(months: int = 12) -> list[dict]:
    """Serie mensual (últimos ``months`` meses) de ingresos, utilidad y nº de ventas.

    Rellena con ceros los meses sin ventas para que el eje sea continuo.
    """
    end = reference_month()
    start = add_period(end, -(months - 1))
    rows = (
        Sale.objects.filter(status=COMP, sale_date__gte=_first_of(start))
        .annotate(m=TruncMonth("sale_date"))
        .values("m")
        .annotate(
            revenue=Sum("total_sale_usd"),
            profit=Sum("total_profit_usd"),
            count=Count("id"),
        )
    )
    by_period = {period_of(r["m"]): r for r in rows}
    out = []
    for p in month_range(start, end):
        r = by_period.get(p)
        out.append(
            {
                "period": p,
                "label": period_label(p),
                "revenue": _f(r["revenue"]) if r else 0.0,
                "profit": _f(r["profit"]) if r else 0.0,
                "count": int(r["count"]) if r else 0,
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Panel de inicio (accesible a todo el personal)
# --------------------------------------------------------------------------- #
def dashboard() -> dict:
    ref = reference_month()
    prev = add_period(ref, -1)
    monthly = _monthly_sales(12)
    cur = next((m for m in monthly if m["period"] == ref), None)
    prv = next((m for m in monthly if m["period"] == prev), None)

    rev_month = cur["revenue"] if cur else 0.0
    rev_prev = prv["revenue"] if prv else 0.0
    cnt_month = cur["count"] if cur else 0
    cnt_prev = prv["count"] if prv else 0

    # Meta del mes = promedio de ingresos de los meses anteriores de la ventana.
    prior = [m["revenue"] for m in monthly if m["period"] != ref and m["revenue"] > 0]
    target = round(sum(prior) / len(prior), 2) if prior else rev_month
    target_pct = round(rev_month / target * 100, 1) if target else 0.0

    customers_total = Customer.objects.count()
    customers_active = Customer.objects.filter(is_active_customer=True).count()
    customers_active_prev = (
        Customer.objects.filter(is_active_customer=True, created_at__date__lt=_first_of(ref)).count()
    )
    products_active = Product.objects.filter(is_active=True).count()

    by_type = (
        Sale.objects.filter(status=COMP)
        .values("sale_type")
        .annotate(count=Count("id"), revenue=Sum("total_sale_usd"))
        .order_by("-revenue")
    )
    type_labels = dict(Sale.TypeChoices.choices)
    sales_by_type = [
        {
            "type": r["sale_type"],
            "label": str(type_labels.get(r["sale_type"], r["sale_type"])),
            "count": int(r["count"]),
            "revenue": _f(r["revenue"]),
        }
        for r in by_type
    ]

    customers_by_state = [
        {"state": r["state"], "count": int(r["n"])}
        for r in (
            Customer.objects.exclude(state="")
            .values("state")
            .annotate(n=Count("id"))
            .order_by("-n")[:8]
        )
    ]

    recent = (
        Sale.objects.filter(status=COMP)
        .select_related("customer")
        .order_by("-sale_date", "-id")[:7]
    )
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
        for s in recent
    ]

    return {
        "reference_month": {"period": ref, "label": period_label(ref)},
        "kpis": {
            "customers_total": customers_total,
            "customers_active": customers_active,
            "customers_active_growth_pct": _pct(customers_active, customers_active_prev),
            "products_active": products_active,
            "revenue_month": rev_month,
            "revenue_prev_month": rev_prev,
            "revenue_growth_pct": _pct(rev_month, rev_prev),
            "sales_count_month": cnt_month,
            "sales_count_prev_month": cnt_prev,
            "sales_count_growth_pct": _pct(cnt_month, cnt_prev),
            "target_month": target,
            "target_pct": target_pct,
        },
        # En el panel de Inicio (visible a todo el personal) no se expone la
        # utilidad: solo ingresos y nº de ventas. La utilidad va en /stats/sales.
        "monthly_sales": [
            {"period": m["period"], "label": m["label"], "revenue": m["revenue"], "count": m["count"]}
            for m in monthly
        ],
        "sales_by_type": sales_by_type,
        "customers_by_state": customers_by_state,
        "recent_sales": recent_sales,
    }


# --------------------------------------------------------------------------- #
# Clientes
# --------------------------------------------------------------------------- #
def customers() -> dict:
    ref = reference_month()
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

    # Agregados de compra por cliente (solo ventas completadas).
    agg = list(
        Sale.objects.filter(status=COMP)
        .values("customer_id")
        .annotate(revenue=Sum("total_sale_usd"), orders=Count("id"), last=Max("sale_date"))
    )
    names = _customer_names({a["customer_id"] for a in agg})

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

    top_by_revenue = [row(a) for a in sorted(agg, key=lambda x: _f(x["revenue"]), reverse=True)[:10]]
    top_by_orders = [row(a) for a in sorted(agg, key=lambda x: x["orders"], reverse=True)[:10]]

    # Clientes en riesgo: activos cuya última compra es anterior al corte (6 meses).
    cutoff = _first_of(add_period(ref, -6))
    active_ids = set(
        Customer.objects.filter(is_active_customer=True).values_list("id", flat=True)
    )
    at_risk = [
        row(a)
        for a in sorted(
            (a for a in agg if a["customer_id"] in active_ids and a["last"] and a["last"] < cutoff),
            key=lambda x: x["last"],
        )[:10]
    ]

    return {
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
            "with_purchases": len(agg),
        },
        "top_by_revenue": top_by_revenue,
        "top_by_orders": top_by_orders,
        "at_risk": at_risk,
    }


# --------------------------------------------------------------------------- #
# Productos
# --------------------------------------------------------------------------- #
def products() -> dict:
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

    out_of_stock = Product.objects.filter(is_active=True, stock__lte=0).count()
    low_stock = Product.objects.filter(
        is_active=True, stock__gt=0, stock__lte=F("min_stock")
    ).count()
    ok_stock = active - out_of_stock - low_stock

    # Valor del inventario (a costo y a precio de venta).
    val = Product.objects.filter(is_active=True).aggregate(
        cost=Sum(F("stock") * F("purchase_price_usd")),
        retail=Sum(F("stock") * F("sale_price_usd")),
        units=Sum("stock"),
    )

    # Ventas por producto (unidades e ingreso) — solo ventas completadas.
    item_agg = list(
        SaleItem.objects.filter(sale__status=COMP)
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

    # Productos activos sin ventas registradas (baja rotación).
    sold_ids = {a["product_id"] for a in item_agg}
    no_sales_qs = Product.objects.filter(is_active=True).exclude(id__in=sold_ids).select_related("category")
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
def sales() -> dict:
    type_labels = dict(Sale.TypeChoices.choices)

    by_type = [
        {
            "type": r["sale_type"],
            "label": str(type_labels.get(r["sale_type"], r["sale_type"])),
            "count": int(r["count"]),
            "revenue": _f(r["revenue"]),
            "profit": _f(r["profit"]),
        }
        for r in (
            Sale.objects.filter(status=COMP)
            .values("sale_type")
            .annotate(count=Count("id"), revenue=Sum("total_sale_usd"), profit=Sum("total_profit_usd"))
            .order_by("-revenue")
        )
    ]

    monthly = _monthly_sales(18)

    # Ingresos mensuales por tipo (detal vs. institucional) — últimos 12 meses.
    end = reference_month()
    start = add_period(end, -11)
    rows = (
        Sale.objects.filter(status=COMP, sale_date__gte=_first_of(start))
        .annotate(m=TruncMonth("sale_date"))
        .values("m", "sale_type")
        .annotate(revenue=Sum("total_sale_usd"))
    )
    monthly_map: dict[str, dict] = {}
    for r in rows:
        p = period_of(r["m"])
        monthly_map.setdefault(p, {})[r["sale_type"]] = _f(r["revenue"])
    monthly_by_type = [
        {
            "period": p,
            "label": period_label(p),
            "retail": monthly_map.get(p, {}).get(Sale.TypeChoices.RETAIL, 0.0),
            "institutional": monthly_map.get(p, {}).get(Sale.TypeChoices.INSTITUTIONAL, 0.0),
        }
        for p in month_range(start, end)
    ]

    revenue_by_category = [
        {"category": r["product__category__name"] or "Sin categoría", "revenue": _f(r["revenue"])}
        for r in (
            SaleItem.objects.filter(sale__status=COMP)
            .values("product__category__name")
            .annotate(revenue=Sum("subtotal_sale_usd"))
            .order_by("-revenue")[:10]
        )
    ]

    seller_rows = (
        Sale.objects.filter(status=COMP)
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

    totals = Sale.objects.filter(status=COMP).aggregate(
        revenue=Sum("total_sale_usd"),
        profit=Sum("total_profit_usd"),
        discount=Sum("total_discount_usd"),
        count=Count("id"),
        avg_ticket=Avg("total_sale_usd"),
    )
    revenue = _f(totals["revenue"])
    profit = _f(totals["profit"])

    return {
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
def quotes() -> dict:
    status_labels = dict(Quote.StatusChoices.choices)
    CON = Quote.StatusChoices.CONVERTED
    REJ = Quote.StatusChoices.REJECTED
    OPEN = [Quote.StatusChoices.DRAFT, Quote.StatusChoices.SENT, Quote.StatusChoices.APPROVED]

    by_status = [
        {
            "status": r["status"],
            "label": str(status_labels.get(r["status"], r["status"])),
            "count": int(r["count"]),
            "value": _f(r["value"]),
        }
        for r in (
            Quote.objects.values("status")
            .annotate(count=Count("id"), value=Sum("total_usd"))
            .order_by("-count")
        )
    ]

    total = Quote.objects.count()
    converted = Quote.objects.filter(status=CON).count()
    rejected = Quote.objects.filter(status=REJ).count()
    conversion_rate = round(converted / total * 100, 1) if total else None
    win_rate = round(converted / (converted + rejected) * 100, 1) if (converted + rejected) else None

    # Emitidos vs. convertidos por mes (últimos 12, por fecha de emisión).
    qs = Quote.objects.all()
    last = qs.aggregate(m=Max("issued_date"))["m"]
    end = period_of(last) if last else reference_month()
    start = add_period(end, -11)
    rows = (
        qs.filter(issued_date__gte=_first_of(start))
        .annotate(m=TruncMonth("issued_date"))
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
        for p in month_range(start, end)
    ]

    pipeline = Quote.objects.filter(status__in=OPEN).aggregate(
        count=Count("id"), value=Sum("total_usd")
    )

    top = (
        Quote.objects.select_related("customer")
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
        "by_status": by_status,
        "monthly": monthly,
        "extras": [
            {"key": "installation", "label": "Con instalación", "count": Quote.objects.filter(includes_installation=True).count()},
            {"key": "delivery", "label": "Con despacho", "count": Quote.objects.filter(includes_delivery=True).count()},
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

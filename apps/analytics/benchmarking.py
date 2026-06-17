"""Inteligencia competitiva descriptiva para el módulo "Benchmarking Competitivo".

A diferencia de ``ml/forecasters.competitor_forecast`` (que pronostica), este módulo
resume el estado **actual** de la competencia para un rango de fechas arbitrario
(``[start, end]`` sobre ``scraped_at``) que el usuario elige con dos selectores
Desde/Hasta — la misma "máquina del tiempo" del panel de Inicio.

Trabaja sobre ``CompetitorMarketData`` deduplicado por ``listing_key`` (semántica de
observación del benchmarking: la última observación de cada anuncio *dentro* de la
ventana). Devuelve dicts/listas listos para serializar a JSON. Es una agregación
directa y barata, así que no se cachea.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import date

from django.db.models import Max, Min

from apps.benchmarking.models import CompetitorMarketData
from apps.core.models import Product

from .ml.datasets import EXCLUDED_COMPETITOR_SOURCES, effective_obs_date
from .ml.features import period_label, period_of

try:
    from apps.competitor_market_data.scrapers import classify_category
except Exception:  # pragma: no cover - import defensivo (scrapers opcionales)
    classify_category = lambda _t: None  # noqa: E731

SOURCE_LABELS = dict(CompetitorMarketData.SourceChoices.choices)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def data_bounds() -> tuple[date | None, date | None]:
    """Primera/última fecha de observación con precio (límites del calendario/presets)."""
    agg = (
        CompetitorMarketData.objects.filter(price_usd__isnull=False)
        .exclude(source__in=EXCLUDED_COMPETITOR_SOURCES)
        .annotate(effective_at=effective_obs_date())
        .aggregate(lo=Min("effective_at"), hi=Max("effective_at"))
    )
    lo = agg["lo"].date() if agg["lo"] else None
    hi = agg["hi"].date() if agg["hi"] else None
    return lo, hi


def default_range() -> tuple[date, date]:
    """Rango por defecto: TODA la ventana de datos de competencia (suelen ser ~6 meses).

    Los datos scrapeados son escasos, así que por defecto se muestra todo; el usuario
    acota con los presets (3/6/12 meses, año, todo).
    """
    lo, hi = data_bounds()
    today = date.today()
    return (lo or today, hi or today)


def _window_rows(start: date | None, end: date | None) -> list[CompetitorMarketData]:
    """Filas de competencia con precio en USD dentro de ``[start, end]``, deduplicadas
    a la última observación por ``listing_key``.

    La ventana y el orden usan la fecha EFECTIVA de la observación
    (``effective_obs_date``: la fecha de publicación del post en Instagram, o
    ``scraped_at`` en el resto), para que un post antiguo scrapeado hoy cuente en su
    mes real y no en el del scraping."""
    qs = (
        CompetitorMarketData.objects.filter(price_usd__isnull=False)
        .exclude(source__in=EXCLUDED_COMPETITOR_SOURCES)
        .select_related("competitor", "product")
        .annotate(effective_at=effective_obs_date())
    )
    if start is not None:
        qs = qs.filter(effective_at__date__gte=start)
    if end is not None:
        qs = qs.filter(effective_at__date__lte=end)
    seen: set[str] = set()
    rows: list[CompetitorMarketData] = []
    for r in qs.order_by("-effective_at"):  # más reciente primero (por fecha efectiva)
        key = r.listing_key or f"_row{r.id}"
        if key in seen:
            continue
        seen.add(key)
        rows.append(r)
    return rows


def _competitor_name(r: CompetitorMarketData) -> str:
    return (r.competitor.name if r.competitor else r.competitor_name) or "Desconocido"


def _own_prices_by_category() -> dict[str, list[float]]:
    """Precio de venta propio agrupado por categoría del vocabulario del scraper.

    Clasifica cada producto propio con ``classify_category`` (igual que
    ``competitor_analysis``) para que las categorías propias y las de mercado casen.
    Clave normalizada a minúsculas."""
    own: dict[str, list[float]] = defaultdict(list)
    for prod in Product.objects.filter(is_active=True).select_related("category"):
        vocab = classify_category(f"{prod.name} {prod.full_name or ''}".strip())
        cat = vocab or (prod.category.name if prod.category else "Sin categoría")
        own[cat.strip().lower()].append(float(prod.sale_price_usd or 0))
    return own


def _range_block(start: date, end: date) -> dict:
    """Bloque ``range`` (mismas claves que el panel de Inicio)."""
    lo, hi = data_bounds()
    months = sorted({period_of(start), period_of(end)})
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "from_label": period_label(period_of(start)),
        "to_label": period_label(period_of(end)),
        "months": len(months),
        "data_from": lo.isoformat() if lo else start.isoformat(),
        "data_to": hi.isoformat() if hi else end.isoformat(),
    }


def _position(own_avg: float | None, prices: list[float]):
    """Posición del precio propio frente a la distribución de mercado."""
    if own_avg is None or not prices:
        return None, None
    below = sum(1 for p in prices if p < own_avg)
    percentile = round(below / len(prices) * 100.0, 1)
    q33 = statistics.quantiles(prices, n=3)[0] if len(prices) >= 3 else min(prices)
    q66 = statistics.quantiles(prices, n=3)[1] if len(prices) >= 3 else max(prices)
    if own_avg < q33:
        position = "below"
    elif own_avg > q66:
        position = "above"
    else:
        position = "within"
    return position, percentile


# --------------------------------------------------------------------------- #
# Comparación descriptiva (página "Comparaciones")
# --------------------------------------------------------------------------- #
def comparison(start: date, end: date, competitor: str | None = None) -> dict:
    """Radiografía de la competencia para el rango ``[start, end]``.

    ``competitor`` (nombre exacto de un competidor) acota TODO el panel a ese único
    competidor; ``None`` o ``"__all__"`` agrega todos (comportamiento por defecto).
    La lista completa de competidores del rango se devuelve siempre en ``competitors``
    para alimentar el selector, aunque la vista esté filtrada a uno."""
    if start > end:
        start, end = end, start
    all_rows = _window_rows(start, end)
    range_block = _range_block(start, end)

    # Lista completa de competidores del rango (alimenta el selector), antes de filtrar.
    competitors = sorted({_competitor_name(r) for r in all_rows})
    selected = competitor if (competitor and competitor != "__all__" and competitor in competitors) else None
    rows = [r for r in all_rows if _competitor_name(r) == selected] if selected else all_rows

    if not rows:
        return {
            "range": range_block, "narrative": [],
            "competitors": competitors, "selected_competitor": selected or "__all__",
            "meta": {"n_obs": 0, "n_competitors": 0, "n_products": 0, "n_with_promo": 0, "n_unmatched": 0},
            "by_state": [], "by_source": [], "promotions": {"competitors_with_promo": [], "breakdown": [], "share_obs_pct": 0.0, "total_competitors": 0},
            "by_competitor": [], "catalog_coverage": [], "products_not_in_catalog": [],
            "categories_not_covered": [], "price_comparison": [], "positioning": [], "observations": [],
        }

    n_obs = len(rows)
    own_by_cat = _own_prices_by_category()
    own_cats = set(own_by_cat.keys())

    # Acumuladores por competidor.
    comp: dict[str, dict] = {}
    for r in rows:
        name = _competitor_name(r)
        c = comp.setdefault(name, {
            "state": (r.competitor.state if r.competitor else "") or "—",
            "municipality": (r.competitor.municipality if r.competitor else "") or "—",
            "sources": set(), "products": set(), "categories": set(),
            "prices": [], "promos": set(), "obs": 0, "matched": 0,
        })
        c["sources"].add(r.source)
        if r.product_name:
            c["products"].add(r.product_name.strip().lower())
        if r.category:
            c["categories"].add(r.category)
        c["prices"].append(float(r.price_usd))
        c["obs"] += 1
        if r.product_id:
            c["matched"] += 1
        promo = (r.promotions or "").strip()
        if promo:
            c["promos"].add(promo)

    # Competidores por ubicación (estado).
    state_comp: dict[str, set] = defaultdict(set)
    state_obs: Counter = Counter()
    for name, c in comp.items():
        state_comp[c["state"]].add(name)
    for r in rows:
        state = (r.competitor.state if r.competitor else "") or "—"
        state_obs[state] += 1
    by_state = sorted(
        ({"state": s, "competitors": len(names), "observations": int(state_obs[s])}
         for s, names in state_comp.items()),
        key=lambda d: d["competitors"], reverse=True,
    )

    # Reparto por plataforma/fuente.
    src_comp: dict[str, set] = defaultdict(set)
    src_obs: Counter = Counter()
    for r in rows:
        src_obs[r.source] += 1
        src_comp[r.source].add(_competitor_name(r))
    by_source = sorted(
        ({"source": s, "label": SOURCE_LABELS.get(s, s),
          "competitors": len(names), "observations": int(src_obs[s]),
          "obs_pct": round(int(src_obs[s]) / n_obs * 100.0, 1)}
         for s, names in src_comp.items()),
        key=lambda d: d["observations"], reverse=True,
    )

    # Promociones.
    promo_rows = [r for r in rows if (r.promotions or "").strip()]
    promo_breakdown = [
        {"promotion": p, "count": n}
        for p, n in Counter((r.promotions or "").strip() for r in promo_rows).most_common()
    ]
    competitors_with_promo = sorted(
        ({"competitor": name, "promotions": sorted(c["promos"])}
         for name, c in comp.items() if c["promos"]),
        key=lambda d: len(d["promotions"]), reverse=True,
    )
    promotions = {
        "competitors_with_promo": competitors_with_promo,
        "breakdown": promo_breakdown,
        "share_obs_pct": round(len(promo_rows) / n_obs * 100.0, 1),
        "total_competitors": len(comp),
    }

    # Variedad de catálogo por competidor.
    by_competitor = sorted(
        ({
            "competitor": name,
            "state": c["state"], "municipality": c["municipality"],
            "sources": sorted(SOURCE_LABELS.get(s, s) for s in c["sources"]),
            "products": len(c["products"]), "categories": len(c["categories"]),
            "observations": c["obs"],
            "avg_price_usd": round(sum(c["prices"]) / len(c["prices"]), 2),
            "min_price_usd": round(min(c["prices"]), 2),
            "max_price_usd": round(max(c["prices"]), 2),
            "has_promo": bool(c["promos"]),
            "matched": c["matched"], "unmatched": c["obs"] - c["matched"],
        } for name, c in comp.items()),
        key=lambda d: d["products"], reverse=True,
    )

    # Cobertura de catálogo por categoría (variedad del mercado + cuántos productos
    # propios la cubren).
    cat_comp: dict[str, set] = defaultdict(set)
    cat_obs: Counter = Counter()
    for r in rows:
        cat = r.category or "Sin categoría"
        cat_comp[cat].add(_competitor_name(r))
        cat_obs[cat] += 1
    catalog_coverage = sorted(
        ({"category": cat, "competitors": len(names), "observations": int(cat_obs[cat]),
          "own_products": len(own_by_cat.get(cat.strip().lower(), []))}
         for cat, names in cat_comp.items()),
        key=lambda d: d["observations"], reverse=True,
    )

    # Productos de la competencia que NO tienen equivalente en nuestro catálogo activo
    # (sin match al ``Product`` propio). Deduplicado por competidor + nombre.
    seen_pair: set[tuple] = set()
    products_not_in_catalog = []
    for r in rows:
        if r.product_id:
            continue
        name = _competitor_name(r)
        key = (name, (r.product_name or "").strip().lower())
        if key in seen_pair:
            continue
        seen_pair.add(key)
        products_not_in_catalog.append({
            "competitor": name,
            "product_name": r.product_name or "—",
            "category": r.category or "Sin categoría",
            "price_usd": round(float(r.price_usd), 2),
            "source": r.source, "source_label": SOURCE_LABELS.get(r.source, r.source),
        })
    products_not_in_catalog.sort(key=lambda d: d["price_usd"], reverse=True)

    # Categorías presentes en el mercado que nuestro catálogo activo no cubre.
    categories_not_covered = sorted(
        {r.category for r in rows
         if r.category and r.category.strip().lower() not in own_cats}
    )

    # Comparación like-with-like: precio propio vs. mercado por producto matcheado.
    matched_groups: dict[int, dict] = {}
    for r in rows:
        if not r.product_id:
            continue
        g = matched_groups.setdefault(r.product_id, {
            "product": r.product.name if r.product else (r.product_name or "—"),
            "own_price": float(r.product.sale_price_usd or 0) if r.product else None,
            "prices": [], "competitors": set(),
        })
        g["prices"].append(float(r.price_usd))
        g["competitors"].add(_competitor_name(r))
    price_comparison = []
    for pid, g in matched_groups.items():
        prices = g["prices"]
        op = g["own_price"]
        position = None
        if op:
            if op < min(prices):
                position = "below"
            elif op > max(prices):
                position = "above"
            else:
                position = "within"
        price_comparison.append({
            "product_id": pid, "product": g["product"],
            "own_price_usd": round(op, 2) if op else None,
            "comp_min": round(min(prices), 2),
            "comp_avg": round(sum(prices) / len(prices), 2),
            "comp_max": round(max(prices), 2),
            "n_obs": len(prices), "n_competitors": len(g["competitors"]),
            "position": position,
        })
    price_comparison.sort(key=lambda d: d["n_obs"], reverse=True)

    # Posicionamiento por categoría (precio propio promedio vs. rango de mercado).
    cat_prices: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        cat_prices[r.category or "Sin categoría"].append(float(r.price_usd))
    positioning = []
    for cat, prices in cat_prices.items():
        own_prices = own_by_cat.get(cat.strip().lower(), [])
        own_avg = round(sum(own_prices) / len(own_prices), 2) if own_prices else None
        position, percentile = _position(own_avg, prices)
        positioning.append({
            "category": cat, "own_avg": own_avg,
            "comp_min": round(min(prices), 2), "comp_avg": round(sum(prices) / len(prices), 2),
            "comp_max": round(max(prices), 2), "comp_median": round(statistics.median(prices), 2),
            "n_obs": len(prices), "position": position, "percentile": percentile,
        })
    positioning.sort(key=lambda d: d["n_obs"], reverse=True)

    # Tabla de observaciones (limitada para no inflar la respuesta).
    observations = [
        {
            "competitor": _competitor_name(r),
            "product_name": r.product_name or "—",
            "category": r.category or "Sin categoría",
            "price_usd": round(float(r.price_usd), 2),
            "source": r.source, "source_label": SOURCE_LABELS.get(r.source, r.source),
            "in_stock": r.is_in_stock,
            "promotions": (r.promotions or "").strip() or None,
            "lead_time_days": r.lead_time_days,
            "matched_product": r.product.name if r.product else None,
            "scraped_at": r.scraped_at.isoformat(),
        }
        for r in sorted(rows, key=lambda x: x.scraped_at, reverse=True)[:250]
    ]

    # Resumen automatizado (data storytelling), 3-5 frases — igual que el panel de Inicio.
    n_comp = len(comp)
    if selected:
        narrative = [
            f"Vista filtrada a {selected}: {n_obs} publicación(es) en {len(by_source)} plataforma(s)."
        ]
    else:
        narrative = [
            f"Se observaron {n_obs} publicaciones de {n_comp} competidor(es) en {len(by_source)} plataforma(s)."
        ]
    if by_source:
        top_src = by_source[0]
        narrative.append(f"{top_src['label']} concentra el {top_src['obs_pct']:.0f}% de las observaciones.")
    cwp = promotions["competitors_with_promo"]
    if cwp:
        line = f"{len(cwp)} de {n_comp} competidores ofrecen promociones"
        if promo_breakdown:
            line += f"; la más frecuente es «{promo_breakdown[0]['promotion']}»"
        narrative.append(line + ".")
    else:
        narrative.append("Ningún competidor muestra promociones en el periodo.")
    if products_not_in_catalog:
        line = f"{len(products_not_in_catalog)} producto(s) de la competencia no tienen equivalente en nuestro catálogo activo"
        if categories_not_covered:
            line += f" (categorías como {', '.join(categories_not_covered[:2])})"
        narrative.append(line + ".")
    scored_pos = [p for p in positioning if p["own_avg"] is not None and p["position"]]
    if scored_pos:
        n_above = sum(1 for p in scored_pos if p["position"] == "above")
        narrative.append(
            f"Nuestro precio está por encima del mercado en {n_above} de {len(scored_pos)} categoría(s) con referencia propia."
        )

    return {
        "range": range_block,
        "narrative": narrative[:5],
        "competitors": competitors,
        "selected_competitor": selected or "__all__",
        "meta": {
            "n_obs": n_obs,
            "n_competitors": len(comp),
            "n_products": len({(r.product_name or "").strip().lower() for r in rows if r.product_name}),
            "n_with_promo": len(promo_rows),
            "n_unmatched": sum(1 for r in rows if not r.product_id),
        },
        "by_state": by_state,
        "by_source": by_source,
        "promotions": promotions,
        "by_competitor": by_competitor,
        "catalog_coverage": catalog_coverage,
        "products_not_in_catalog": products_not_in_catalog,
        "categories_not_covered": categories_not_covered,
        "price_comparison": price_comparison,
        "positioning": positioning,
        "observations": observations,
    }

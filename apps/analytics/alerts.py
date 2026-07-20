"""Generación y enrutamiento de **alertas** (sistema de alerta temprana).

Este módulo centraliza tres cosas:

1. **A quién le llega cada alerta** (`AUDIENCE_BY_TYPE`): las alertas son hechos de
   empresa, pero solo son relevantes para ciertos roles (un quiebre de stock le
   importa al encargado de inventario y a la gerencia, no al vendedor).
2. **Cómo se crea/actualiza/resuelve** una alerta sin duplicarla (`upsert_alert`,
   `resolve_alert`, `resolve_missing`), deduplicada por una `dedupe_key` estable.
3. **El barrido predictivo** (`scan_and_generate_alerts`) que, a partir de los
   modelos y datos, levanta alertas de: quiebre de stock **previsto** (el próximo
   mes la demanda estimada supera el stock), sobrestock, caída de demanda y cambio
   de precio de un competidor; además refresca la alerta de tasa desactualizada.

El barrido lo dispara el frontend al iniciar sesión un rol relevante (no hay cron:
el patrón es el mismo de las programaciones de scraping). Es *best-effort* y está
**throttleado**: si se ejecutó hace poco, no repite el trabajo.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Max
from django.utils import timezone

from apps.accounts.models import Role

from .models import Alert, AlertRead

logger = logging.getLogger("apps")

T = Alert.TypeChoices
S = Alert.SeverityChoices

# --------------------------------------------------------------------------- #
# Enrutamiento por rol: qué roles reciben cada tipo de alerta.
# --------------------------------------------------------------------------- #
_INVENTORY_AUDIENCE = [Role.ADMIN, Role.MANAGER, Role.WAREHOUSE]
_STRATEGY_AUDIENCE = [Role.ADMIN, Role.MANAGER]

AUDIENCE_BY_TYPE: dict[str, list[str]] = {
    T.STOCK_BREAK: _INVENTORY_AUDIENCE,
    T.STOCK_PRED: _INVENTORY_AUDIENCE,
    T.OVERSTOCK: _INVENTORY_AUDIENCE,
    T.DEMAND_DROP: _INVENTORY_AUDIENCE,
    T.PRICE_CHANGE: _STRATEGY_AUDIENCE,
    T.RATE_STALE: _STRATEGY_AUDIENCE,
    T.GOAL_MET: _STRATEGY_AUDIENCE,
}


def audience_for(alert_type: str) -> list[str]:
    """Roles que deben recibir una alerta de este tipo (lista de códigos de `Role`)."""
    return list(AUDIENCE_BY_TYPE.get(alert_type, [Role.ADMIN, Role.MANAGER]))


# --------------------------------------------------------------------------- #
# Upsert / resolución deduplicada.
# --------------------------------------------------------------------------- #
def upsert_alert(*, alert_type, dedupe_key, title, message, severity, audience=None):
    """Crea o actualiza una alerta abierta identificada por `dedupe_key`.

    Si ya existe una alerta **no resuelta** con esa clave, actualiza su contenido;
    y si la severidad **subió** o cambió el mensaje/título, borra las lecturas para
    que vuelva a aparecer como no leída para todos (re-notificar una condición que
    empeoró). Si no existe, la crea. Devuelve la alerta.
    """
    audience = audience if audience is not None else audience_for(alert_type)
    existing = (
        Alert.objects.filter(dedupe_key=dedupe_key, is_resolved=False)
        .order_by("-created_at")
        .first()
    )
    if existing is None:
        return Alert.objects.create(
            alert_type=alert_type,
            severity=severity,
            title=title,
            message=message,
            dedupe_key=dedupe_key,
            audience=audience,
        )

    sev_rank = {S.INFO: 0, S.WARNING: 1, S.CRITICAL: 2}
    escalated = sev_rank.get(severity, 0) > sev_rank.get(existing.severity, 0)
    content_changed = existing.title != title or existing.message != message
    changed_fields = []
    if existing.severity != severity:
        existing.severity = severity
        changed_fields.append("severity")
    if existing.title != title:
        existing.title = title
        changed_fields.append("title")
    if existing.message != message:
        existing.message = message
        changed_fields.append("message")
    if existing.audience != audience:
        existing.audience = audience
        changed_fields.append("audience")
    if changed_fields:
        existing.save(update_fields=changed_fields + ["updated_at"])
    # Re-notificar si empeoró o cambió el texto (una condición que sigue viva).
    if escalated or content_changed:
        AlertRead.objects.filter(alert=existing).delete()
    return existing


def resolve_alert(dedupe_key) -> int:
    """Marca como resueltas las alertas abiertas con esa clave. Devuelve cuántas."""
    return Alert.objects.filter(dedupe_key=dedupe_key, is_resolved=False).update(
        is_resolved=True
    )


def resolve_missing(*, alert_types, keep_keys) -> int:
    """Resuelve las alertas abiertas de ciertos tipos cuya clave **no** siga vigente.

    Tras un barrido, las condiciones que ya no se cumplen (su `dedupe_key` no se
    regeneró) se cierran automáticamente. `keep_keys` es el conjunto de claves que sí
    se levantaron en este barrido.
    """
    stale = Alert.objects.filter(
        alert_type__in=alert_types, is_resolved=False
    ).exclude(dedupe_key__in=keep_keys)
    return stale.update(is_resolved=True)


# --------------------------------------------------------------------------- #
# Barrido predictivo.
# --------------------------------------------------------------------------- #
_SCAN_LOCK_KEY = "alerts:last_scan_at"
_SCAN_MIN_INTERVAL = timedelta(minutes=20)

# Umbrales del barrido (conservadores para no inundar de alertas).
_OVERSTOCK_MONTHS = 8.0          # meses de cobertura por encima de los cuales hay sobrestock
_DEMAND_DROP_RATIO = 0.6         # el próximo mes cae por debajo del 60% del promedio reciente
_DEMAND_DROP_MIN_AVG = 3.0       # solo productos con volumen reciente relevante
_PRICE_CHANGE_DROP = 0.15        # baja ≥15% de un competidor entre dos observaciones
_PRICE_CHANGE_WINDOW_DAYS = 45   # la observación reciente debe ser de los últimos 45 días
_MAX_PER_CATEGORY = 25           # tope de alertas nuevas por categoría en un barrido
_RECENT_SALES_DAYS = 180         # ventana para elegir productos con historia suficiente


def scan_and_generate_alerts(*, force: bool = False) -> dict:
    """Ejecuta el barrido completo y devuelve un resumen. Throttleado y best-effort."""
    if not force:
        last = cache.get(_SCAN_LOCK_KEY)
        if last and (timezone.now() - last) < _SCAN_MIN_INTERVAL:
            return {"skipped": True, "reason": "throttled"}
    cache.set(_SCAN_LOCK_KEY, timezone.now(), timeout=int(_SCAN_MIN_INTERVAL.total_seconds()) * 3)

    summary = {"skipped": False}
    for name, fn in (
        ("inventory", _scan_inventory_and_demand),
        ("competitor", _scan_competitor_prices),
        ("rate", _refresh_rate_stale),
        ("normalized", _normalize_audiences),
    ):
        try:
            summary[name] = fn()
        except Exception:  # noqa: BLE001 — un fallo de una parte no debe tumbar el resto
            logger.warning("Fallo en el barrido de alertas: %s", name, exc_info=True)
            summary[name] = {"error": True}
    return summary


def _scan_inventory_and_demand() -> dict:
    """Quiebre de stock previsto (próximo mes), sobrestock y caída de demanda."""
    from apps.core.models import SERVICE_SKU_PREFIX, Product
    from apps.sales.models import SaleItem

    from .ml import forecasters as F

    since = timezone.localdate() - timedelta(days=_RECENT_SALES_DAYS)
    product_ids = list(
        SaleItem.objects.filter(sale__sale_date__gte=since)
        .exclude(product__sku__startswith=SERVICE_SKU_PREFIX)
        # .order_by() elimina el orden por defecto del modelo: si no, Django lo añade
        # al SELECT y rompe el DISTINCT (devolvería product_id repetidos).
        .order_by()
        .values_list("product_id", flat=True)
        .distinct()
    )
    products = {p.id: p for p in Product.objects.filter(id__in=product_ids, is_active=True)}

    kept = {T.STOCK_PRED: set(), T.OVERSTOCK: set(), T.DEMAND_DROP: set()}
    counts = {"stock_pred": 0, "overstock": 0, "demand_drop": 0}

    for pid in product_ids:
        product = products.get(pid)
        if product is None or getattr(product, "is_service", False):
            continue
        try:
            fc = F.forecast_demand(pid, horizon=3)
        except Exception:  # noqa: BLE001
            continue
        points = fc.get("forecast") or []
        history = fc.get("history") or []
        if not points:
            continue

        stock = int(product.stock or 0)
        min_stock = int(product.min_stock or 0)
        next_demand = float(points[0]["value"])
        recent_vals = [float(h["value"]) for h in history[-3:]]
        recent_avg = sum(recent_vals) / len(recent_vals) if recent_vals else 0.0
        next_label = points[0]["label"]

        # (1) Quiebre de stock PREVISTO: el próximo mes la demanda estimada no se cubre.
        if next_demand > 0 and stock - next_demand <= 0 and counts["stock_pred"] < _MAX_PER_CATEGORY:
            key = f"stock_pred:{pid}"
            kept[T.STOCK_PRED].add(key)
            critical = stock <= 0 or stock < 0.5 * next_demand
            estado = (
                "no tiene stock" if stock <= 0
                else f"solo tiene {stock} unidad(es) frente a una demanda estimada de {round(next_demand)}"
            )
            upsert_alert(
                alert_type=T.STOCK_PRED,
                dedupe_key=key,
                severity=S.CRITICAL if critical else S.WARNING,
                title=f"Quiebre previsto: {product.name}",
                message=(
                    f"Se prevé que '{product.name}' (SKU {product.sku or '—'}) agote su stock en "
                    f"{next_label}: {estado}. Conviene reabastecer antes."
                ),
            )
            counts["stock_pred"] += 1

        # (2) Sobrestock: muchos meses de cobertura frente a la demanda reciente.
        elif recent_avg > 0 and stock > 0:
            months_cover = stock / recent_avg
            if months_cover >= _OVERSTOCK_MONTHS and counts["overstock"] < _MAX_PER_CATEGORY:
                key = f"overstock:{pid}"
                kept[T.OVERSTOCK].add(key)
                upsert_alert(
                    alert_type=T.OVERSTOCK,
                    dedupe_key=key,
                    severity=S.INFO,
                    title=f"Sobrestock: {product.name}",
                    message=(
                        f"'{product.name}' tiene {stock} unidad(es), ≈{round(months_cover, 1)} meses de "
                        f"cobertura al ritmo de venta reciente ({round(recent_avg, 1)}/mes). Capital inmovilizado."
                    ),
                )
                counts["overstock"] += 1

        # (3) Caída de demanda: el próximo mes cae fuerte frente al promedio reciente.
        if (
            recent_avg >= _DEMAND_DROP_MIN_AVG
            and next_demand < _DEMAND_DROP_RATIO * recent_avg
            and counts["demand_drop"] < _MAX_PER_CATEGORY
        ):
            key = f"demand_drop:{pid}"
            kept[T.DEMAND_DROP].add(key)
            drop_pct = round((1 - next_demand / recent_avg) * 100)
            upsert_alert(
                alert_type=T.DEMAND_DROP,
                dedupe_key=key,
                severity=S.WARNING,
                title=f"Caída de demanda: {product.name}",
                message=(
                    f"El modelo prevé una caída de ≈{drop_pct}% en la demanda de '{product.name}' para "
                    f"{next_label} ({round(recent_avg, 1)}/mes → {round(next_demand, 1)}/mes)."
                ),
            )
            counts["demand_drop"] += 1

    # Cierra las que ya no se cumplen.
    resolve_missing(
        alert_types=[T.STOCK_PRED, T.OVERSTOCK, T.DEMAND_DROP],
        keep_keys=kept[T.STOCK_PRED] | kept[T.OVERSTOCK] | kept[T.DEMAND_DROP],
    )
    return counts


def _scan_competitor_prices() -> dict:
    """Cambio de precio de un competidor: baja fuerte entre sus dos últimas observaciones."""
    from apps.benchmarking.models import CompetitorMarketData

    from .ml.datasets import EXCLUDED_COMPETITOR_SOURCES

    cutoff = timezone.now() - timedelta(days=_PRICE_CHANGE_WINDOW_DAYS)
    # Solo consideramos filas con producto propio asociado (comparación like-with-like)
    # y precio en USD, agrupables por listing_key.
    listing_keys = (
        CompetitorMarketData.objects.exclude(source__in=EXCLUDED_COMPETITOR_SOURCES)
        .exclude(listing_key="")
        .filter(price_usd__isnull=False, scraped_at__gte=cutoff)
        # .order_by() elimina el orden por defecto (-scraped_at): si no, Django lo mete
        # en el SELECT y el DISTINCT devolvería la misma listing_key una vez por fecha.
        .order_by()
        .values_list("listing_key", flat=True)
        .distinct()
    )

    kept, count = set(), 0
    for lk in listing_keys:
        if count >= _MAX_PER_CATEGORY:
            break
        obs = list(
            CompetitorMarketData.objects.filter(listing_key=lk, price_usd__isnull=False)
            .order_by("-scraped_at")[:2]
        )
        if len(obs) < 2:
            continue
        new, old = obs[0], obs[1]
        new_price, old_price = float(new.price_usd), float(old.price_usd)
        if old_price <= 0 or new_price <= 0:
            continue
        drop = (old_price - new_price) / old_price
        if drop < _PRICE_CHANGE_DROP:
            continue
        key = f"price_change:{lk}"
        kept.add(key)
        who = new.competitor.name if new.competitor_id else (new.competitor_name or "Un competidor")
        prod = new.product.name if new.product_id else (new.product_name or "un producto")
        upsert_alert(
            alert_type=T.PRICE_CHANGE,
            dedupe_key=key,
            severity=S.WARNING,
            title=f"Bajó el precio: {who}",
            message=(
                f"{who} bajó el precio de '{prod}' un ≈{round(drop * 100)}% "
                f"(${old_price:,.2f} → ${new_price:,.2f}). Revisa tu posicionamiento."
            ),
        )
        count += 1

    resolve_missing(alert_types=[T.PRICE_CHANGE], keep_keys=kept)
    return {"price_change": count}


def _refresh_rate_stale() -> dict:
    """Reevalúa la frescura de la tasa (crea/resuelve `RATE_STALE`)."""
    from apps.core import system_settings
    from apps.core.management.commands.fetch_exchange_rate import check_rate_freshness

    result = check_rate_freshness(max_age_days=system_settings.rate_max_age_days())
    return {"stale": bool(result.get("is_stale"))}


def _normalize_audiences() -> int:
    """Rellena la audiencia (según el tipo) de alertas abiertas que no la tengan.

    Filas antiguas creadas antes de este sistema quedaron con `audience=[]`. Se
    normalizan en Python (independiente del motor de BD) para que el admin y las
    consultas reflejen a quién va dirigida cada alerta.
    """
    fixed = 0
    for alert in Alert.objects.filter(is_resolved=False):
        if not alert.audience:
            alert.audience = audience_for(alert.alert_type)
            alert.save(update_fields=["audience", "updated_at"])
            fixed += 1
    return fixed

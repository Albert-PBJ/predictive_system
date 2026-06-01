import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from apps.benchmarking.models import Competitor, CompetitorMarketData
from apps.competitor_market_data.enrichment import deepseek
from apps.competitor_market_data.scrapers import classify_category, get_client

logger = logging.getLogger(__name__)

FACEBOOK_MARKETPLACE_ACTOR_ID = "apify/facebook-marketplace-scraper"

# Confianza mínima para crear un Competitor nuevo a partir de la salida del LLM.
_MIN_CONFIDENCE = 0.55

# ── Helpers de acceso seguro ──────────────────────────────────────────────────


def _as_str(value) -> str:
    """Convierte cualquier valor a string de forma segura.

    El actor anida varios campos de texto dentro de un dict con clave `text`
    (p. ej. `description`, `locationText`). Esta función los desenvuelve.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("text") or value.get("value") or ""
    if value is None:
        return ""
    return str(value)


# ── Extracción de campos ──────────────────────────────────────────────────────


def _parse_amount(raw: str) -> Optional[Decimal]:
    """Extrae el monto numérico de un string de precio (ej. '$50', 'Bs. 200', 'VEF140').

    No interpreta la moneda: en Facebook Marketplace el precio siempre es USD
    """
    raw = (raw or "").strip()
    if not raw or raw.lower() in ("free", "gratis"):
        return None
    # El número debe empezar en un dígito (evita capturar el '.' de 'Bs.').
    m = re.search(r"(\d[\d,.]*)", raw)
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", "").rstrip("."))
    except InvalidOperation:
        return None


def _extract_price(listing: dict) -> tuple[Optional[Decimal], Optional[str]]:
    """Precio del anuncio, SIEMPRE en USD.

    En Facebook Marketplace Venezuela los precios se publican en dólares aunque el
    JSON declare otra moneda (p. ej. `VEF`). Por eso ignoramos el código de moneda
    que reporta el actor y fijamos la moneda en 'USD'.

    Prioriza el objeto estructurado `listingPrice.amount`; si falta, cae al texto
    formateado o a un campo `price` plano (esquemas antiguos del actor).
    """
    amount = None
    price_obj = listing.get("listingPrice")
    if isinstance(price_obj, dict):
        raw_amount = price_obj.get("amount")
        if raw_amount not in (None, "", "0"):
            try:
                value = Decimal(str(raw_amount).replace(",", ""))
                if value > 0:
                    amount = value
            except InvalidOperation:
                pass
        if amount is None:
            amount = _parse_amount(price_obj.get("formatted_amount_zeros_stripped") or "")

    if amount is None:
        amount = _parse_amount(_as_str(listing.get("price")))

    return (amount, "USD") if amount is not None else (None, None)


def _extract_lead_time(description: str) -> Optional[int]:
    """Extrae el tiempo de entrega en días desde la descripción del anuncio."""
    if not description:
        return None
    for pattern in (
        r"(\d+)\s*días?\s*(?:de\s+)?(?:entrega|despacho|envío)",
        r"(\d+)\s*days?\s*(?:delivery|shipping)",
        r"entrega\s+en\s+(\d+)\s*días?",
        r"delivery\s+in\s+(\d+)\s*days?",
    ):
        m = re.search(pattern, description, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def _extract_promotions(description: str) -> Optional[str]:
    """Detecta palabras clave promocionales en la descripción del anuncio."""
    keywords = [
        "oferta", "descuento", "promoción", "promo", "rebaja", "sale",
        "envío gratis", "delivery gratis", "% de descuento", "liquidación",
        "precio especial",
    ]
    text = (description or "").lower()
    found = [kw.title() for kw in keywords if kw in text]
    if found:
        return ", ".join(dict.fromkeys(found))[:255]
    return None


def _is_in_stock(listing: dict) -> bool:
    """Disponibilidad del anuncio.

    Usa los booleanos estructurados del actor (fiables) y cae al texto de la
    descripción solo como respaldo. Disponible = no vendido, activo (isLive) y no
    pendiente ni oculto.
    """
    if listing.get("isSold") is True:
        return False
    if listing.get("isPending") is True:
        return False
    if listing.get("isHidden") is True:
        return False
    if listing.get("isLive") is False:
        return False

    description = _as_str(listing.get("description")).lower()
    palabras_agotado = ["agotado", "sin stock", "no disponible", "out of stock", "sold out"]
    return not any(kw in description for kw in palabras_agotado)


# ── Mapeo determinista (sin LLM) ──────────────────────────────────────────────


def _map_listing_to_instance(listing: dict) -> CompetitorMarketData:
    """Convierte un item de Apify en una instancia de CompetitorMarketData.

    Mapeo 100% determinista. El competidor (`competitor` / `competitor_name`) se
    resuelve aparte, de forma opcional, vía LLM (ver `_enrich_listings`).
    """
    title = _as_str(listing.get("listingTitle"))
    description = _as_str(listing.get("description"))
    price, currency = _extract_price(listing)

    return CompetitorMarketData(
        competitor=None,
        competitor_name="",
        source=CompetitorMarketData.SourceChoices.FACEBOOK,
        url=listing.get("itemUrl") or listing.get("url"),
        product_name=title[:255] or None,
        category=classify_category(f"{title} {description}"),
        price=price,
        currency=currency or "USD",
        lead_time_days=_extract_lead_time(description),
        is_in_stock=_is_in_stock(listing),
        promotions=_extract_promotions(description),
        raw_metadata=listing,
    )


# ── Resolución del competidor vía LLM (opcional) ──────────────────────────────


def _resolve_competitor_fk(
    result: dict,
    known: list[dict],
    created_cache: dict,
) -> tuple[Optional[Competitor], str, str]:
    """Traduce la salida del LLM a un Competitor (enlazándolo o creándolo).

    - Si el LLM hizo match con un id conocido → enlaza ese Competitor.
    - Si propone un negocio nuevo con confianza suficiente → lo crea (get_or_create).
    - Si no hay nombre identificable → (None, "") y el registro queda sin competidor.

    Retorna ``(competitor, name, outcome)`` donde ``outcome`` indica cómo se
    resolvió, para poder medirlo al final:
    - ``"existing"``: enlazado a un competidor que YA existía (match por id,
      get_or_create existente o un nombre repetido en este mismo run) → dedupe.
    - ``"created"``: se creó un competidor nuevo en este run.
    - ``"none"``: no se identificó ningún competidor.
    """
    matched_id = result.get("matched_competitor_id")
    if matched_id is not None:
        competitor = Competitor.objects.filter(id=matched_id).first()
        if competitor is not None:
            return competitor, competitor.name, "existing"

    name = result.get("competitor_name")
    confidence = result.get("confidence") or 0.0
    if not name or confidence < _MIN_CONFIDENCE:
        return None, "", "none"

    key = name.lower()
    if key in created_cache:
        return created_cache[key], name, "existing"

    competitor, created = Competitor.objects.get_or_create(
        name=name,
        defaults={
            "is_active": True,
            "notes": "Detectado automáticamente desde Facebook Marketplace (LLM).",
        },
    )
    if created:
        logger.info("Creado nuevo Competitor vía LLM: '%s'", name)
        known.append({"id": competitor.id, "name": competitor.name})
    created_cache[key] = competitor
    return competitor, competitor.name, ("created" if created else "existing")


def _enrich_listings(pairs: list[tuple[CompetitorMarketData, dict]]) -> None:
    """Enriquece los registros in-place con el LLM, si está habilitado.

    Por cada anuncio, una sola llamada resuelve el competidor (competitor /
    competitor_name) y extrae promociones/beneficios del texto libre. No-op
    silencioso cuando el enriquecimiento está apagado. Cada llamada está aislada:
    si una falla, el resto de los registros se guardan igual.
    """
    if not deepseek.is_enabled():
        logger.info(
            "Enriquecimiento LLM DESACTIVADO (USE_LLM_ENRICHMENT=%s, DEEPSEEK_API_KEY %s); "
            "se omite la identificación de competidores por IA. Si esperabas que corriera, "
            "revisa el .env y REINICIA el servidor.",
            deepseek.USE_LLM_ENRICHMENT,
            "presente" if deepseek.DEEPSEEK_API_KEY else "ausente",
        )
        return

    logger.info(
        "Enriquecimiento LLM ACTIVO: analizando %d anuncio(s) con DeepSeek (modelo=%s)…",
        len(pairs),
        deepseek.DEEPSEEK_MODEL,
    )
    known = list(Competitor.objects.filter(is_active=True).values("id", "name"))
    created_cache: dict[str, Competitor] = {}
    linked_existing = 0   # enlazados a un competidor ya existente (dedupe)
    created_new = 0       # competidores nuevos creados por el LLM
    with_promotions = 0   # anuncios con promociones/beneficios extraídos
    found_something = 0   # anuncios donde el LLM aportó algún dato

    for instance, listing in pairs:
        result = deepseek.enrich_listing(
            title=_as_str(listing.get("listingTitle")),
            description=_as_str(listing.get("description")),
            location=_as_str(listing.get("locationText")),
            known_competitors=known,
        )
        item_found = False

        competitor, name, outcome = _resolve_competitor_fk(result, known, created_cache)
        if competitor is not None:
            instance.competitor = competitor
            instance.competitor_name = competitor.name
            item_found = True
            if outcome == "created":
                created_new += 1
            else:
                linked_existing += 1
        elif name:
            instance.competitor_name = name
            item_found = True

        # El LLM lee mejor las promociones/beneficios del texto libre; cuando
        # encuentra algo, prevalece sobre la detección por palabras clave.
        promotions = result.get("promotions")
        if promotions:
            instance.promotions = promotions[:255]
            with_promotions += 1
            item_found = True

        if item_found:
            found_something += 1

    logger.info(
        "Enriquecimiento LLM finalizado: %d de %d anuncio(s) con datos del LLM "
        "(enlazados a competidor existente: %d, competidor nuevo creado: %d, "
        "promociones/beneficios extraídos: %d).",
        found_something,
        len(pairs),
        linked_existing,
        created_new,
        with_promotions,
    )


# ── Funciones públicas ────────────────────────────────────────────────────────


def start_facebook_run(urls: list[str], results_limit: int = 50) -> dict:
    """Inicia (sin bloquear) el run del scraper de Facebook Marketplace y lo retorna."""
    client = get_client()
    actor_input = {
        "startUrls": [{"url": u} for u in urls],
        "resultsLimit": results_limit,
        "includeListingDetails": True,
    }
    logger.info(
        "Iniciando run de Facebook Marketplace en Apify para %d URL(s)…", len(urls)
    )
    return client.actor(FACEBOOK_MARKETPLACE_ACTOR_ID).start(run_input=actor_input)


def finalize_facebook(dataset_id: str) -> list[CompetitorMarketData]:
    """Lee el dataset de un run finalizado, mapea cada listing y guarda los registros.

    El mapeo de campos es determinista; la identificación del competidor se hace
    de forma opcional vía LLM (DeepSeek) antes de persistir.
    """
    client = get_client()
    items = list(client.dataset(dataset_id).iterate_items())
    logger.info("Se obtuvieron %d listings del dataset de Apify.", len(items))

    pairs = [(_map_listing_to_instance(item), item) for item in items]
    _enrich_listings(pairs)

    instances = [instance for instance, _ in pairs]
    created = CompetitorMarketData.objects.bulk_create(instances)
    logger.info("Se guardaron %d registros en CompetitorMarketData.", len(created))
    return created


def scrape_facebook_marketplace(
    urls: list[str],
    results_limit: int = 5,
) -> list[CompetitorMarketData]:
    """Versión bloqueante (start + esperar + finalizar) usada por el comando CLI."""
    run = start_facebook_run(urls=urls, results_limit=results_limit)
    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        logger.error("El run de Apify no retornó un dataset ID.")
        return []

    get_client().run(run["id"]).wait_for_finish()
    return finalize_facebook(dataset_id)

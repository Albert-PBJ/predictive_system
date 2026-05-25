import logging
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from apify_client import ApifyClient

from apps.benchmarking.models import CompetitorMarketData

logger = logging.getLogger(__name__)

APIFY_API_KEY = os.environ.get("APIFY_API_KEY", "")
FACEBOOK_MARKETPLACE_ACTOR_ID = "apify/facebook-marketplace-scraper"

# ── Extracción de campos ──────────────────────────────────────────────────────


def _extract_price(item: dict) -> tuple[Optional[Decimal], Optional[str]]:
    """
    Extrae precio y moneda desde el item de Facebook Marketplace.
    El actor devuelve un campo `price` como string (ej. "$50", "Bs. 200", "Free").
    """
    # Campo dedicado del actor
    raw = str(item.get("price") or "").strip()

    if not raw or raw.lower() in ("free", "gratis", ""):
        return None, None

    # Patrones para bolívares venezolanos
    for pattern in [
        r"Bs\.?\s*([\d,.]+)",
        r"([\d,.]+)\s*Bs\.?",
        r"VES\s*([\d,.]+)",
        r"([\d,.]+)\s*VES",
    ]:
        m = re.search(pattern, raw, re.IGNORECASE)
        if m:
            try:
                return Decimal(m.group(1).replace(",", "")), "VES"
            except InvalidOperation:
                pass

    # Patrones para dólares
    for pattern in [
        r"\$\s*([\d,.]+)",
        r"([\d,.]+)\s*\$",
        r"USD\s*([\d,.]+)",
        r"([\d,.]+)\s*USD",
    ]:
        m = re.search(pattern, raw, re.IGNORECASE)
        if m:
            try:
                return Decimal(m.group(1).replace(",", "")), "USD"
            except InvalidOperation:
                pass

    # Último intento: número suelto (asume USD)
    m = re.search(r"([\d,.]+)", raw)
    if m:
        try:
            return Decimal(m.group(1).replace(",", "")), "USD"
        except InvalidOperation:
            pass

    return None, None


def _extract_lead_time(description: str) -> Optional[int]:
    """Extrae el tiempo de entrega en días desde la descripción del listing."""
    if not description:
        return None
    for pattern in [
        r"(\d+)\s*días?\s*(?:de\s+)?(?:entrega|despacho|envío)",
        r"(\d+)\s*days?\s*(?:delivery|shipping)",
        r"entrega\s+en\s+(\d+)\s*días?",
        r"delivery\s+in\s+(\d+)\s*days?",
    ]:
        m = re.search(pattern, description, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def _extract_promotions(description: str) -> Optional[str]:
    """Detecta palabras clave promocionales en la descripción del listing."""
    keywords = [
        "oferta",
        "descuento",
        "promoción",
        "promo",
        "rebaja",
        "sale",
        "envío gratis",
        "free shipping",
        "% off",
        "liquidación",
        "outlet",
        "precio especial",
    ]
    text = (description or "").lower()
    found = [kw.title() for kw in keywords if kw in text]
    if found:
        return ", ".join(dict.fromkeys(found))[:255]
    return None


def _is_in_stock(item: dict) -> bool:
    """
    Retorna False si la descripción contiene palabras de producto agotado,
    o si el listing está marcado como vendido/unavailable por el actor.
    """
    availability = _as_str(item.get("availability")).lower()
    if availability in ("out of stock", "sold", "unavailable"):
        return False

    description = _as_str(item.get("description")).lower()
    palabras_agotado = [
        "agotado",
        "sin stock",
        "no disponible",
        "out of stock",
        "sold out",
    ]
    return not any(kw in description for kw in palabras_agotado)


def _extract_competitor_name(item: dict) -> str:
    """
    Extrae el nombre del vendedor. Usa `sellerName` si está disponible;
    de lo contrario cae a `seller.name` o un string vacío.
    """
    name = item.get("sellerName") or ""
    if not name:
        seller = item.get("seller") or {}
        name = seller.get("name") or ""
    return name[:150]


# ── Mapeo principal ───────────────────────────────────────────────────────────


def _as_str(value) -> str:
    """Convierte cualquier valor a string de forma segura para los helpers de regex."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        # Algunos actores devuelven el texto dentro de una clave "text" o similar
        return value.get("text") or value.get("value") or ""
    if value is None:
        return ""
    return str(value)


def _map_listing_to_instance(listing: dict) -> CompetitorMarketData:
    """Convierte un item de Apify en una instancia de CompetitorMarketData."""
    description = _as_str(listing.get("description"))
    price, currency = _extract_price(listing)

    # El actor devuelve la categoría directamente cuando está disponible
    category = listing.get("category") or None
    if category:
        category = str(category)[:100]

    return CompetitorMarketData(
        competitor_name=_extract_competitor_name(listing),
        source=CompetitorMarketData.SourceChoices.FACEBOOK,
        url=listing.get("url"),
        product_name=(listing.get("marketplace_listing_title") or "")[:255] or None,
        category=category,
        price=price,
        currency=currency or "USD",
        lead_time_days=_extract_lead_time(description),
        is_in_stock=_is_in_stock(listing),
        promotions=_extract_promotions(description),
        raw_metadata=listing,
    )


# ── Función pública ───────────────────────────────────────────────────────────


def scrape_facebook_marketplace(
    urls: list[str],
    results_limit: int = 5,
) -> list[CompetitorMarketData]:
    """
    Ejecuta el scraper de Facebook Marketplace en Apify para las URLs dadas,
    mapea cada listing a un registro de CompetitorMarketData, los inserta en
    masa y los retorna.
    """
    if not APIFY_API_KEY or APIFY_API_KEY == "your_apify_api_key_here":
        raise ValueError(
            "APIFY_API_KEY no está configurado. Reemplaza el placeholder en el archivo .env."
        )

    client = ApifyClient(APIFY_API_KEY)

    actor_input = {
        "startUrls": [{"url": u} for u in urls],
        "resultsLimit": results_limit,
        "includeListingDetails": True,
    }

    logger.info(
        "Iniciando scraper de Facebook Marketplace en Apify para %d URL(s)…", len(urls)
    )
    run = client.actor(FACEBOOK_MARKETPLACE_ACTOR_ID).call(run_input=actor_input)

    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        logger.error("El run de Apify no retornó un dataset ID.")
        return []

    items = list(client.dataset(dataset_id).iterate_items())
    logger.info("Se obtuvieron %d listings del dataset de Apify.", len(items))

    instances = [_map_listing_to_instance(item) for item in items]
    created = CompetitorMarketData.objects.bulk_create(instances)
    logger.info("Se guardaron %d registros en CompetitorMarketData.", len(created))
    return created

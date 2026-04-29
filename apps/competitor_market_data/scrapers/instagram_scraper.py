import logging
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from apify_client import ApifyClient

from apps.competitor_market_data.models import CompetitorMarketData

logger = logging.getLogger(__name__)

APIFY_API_KEY = os.environ.get("APIFY_API_KEY", "")
INSTAGRAM_ACTOR_ID = "apify/instagram-scraper"

# ── Extracción de campos ──────────────────────────────────────────────────────


def _extract_price(text: str) -> tuple[Optional[Decimal], Optional[str]]:
    """Extrae el precio y la moneda desde el texto del caption."""
    if not text:
        return None, None

    # Patrones para bolívares venezolanos: Bs., Bs, VES
    for pattern in [
        r"Bs\.?\s*([\d,.]+)",
        r"([\d,.]+)\s*Bs\.?",
        r"VES\s*([\d,.]+)",
        r"([\d,.]+)\s*VES",
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            try:
                return Decimal(m.group(1).replace(",", "")), "VES"
            except InvalidOperation:
                pass

    # Patrones para dólares: $, USD
    for pattern in [
        r"\$\s*([\d,.]+)",
        r"([\d,.]+)\s*\$",
        r"USD\s*([\d,.]+)",
        r"([\d,.]+)\s*USD",
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            try:
                return Decimal(m.group(1).replace(",", "")), "USD"
            except InvalidOperation:
                pass

    return None, None


def _extract_lead_time(text: str) -> Optional[int]:
    """Extrae el tiempo de entrega en días desde el caption."""
    if not text:
        return None
    for pattern in [
        r"(\d+)\s*días?\s*(?:de\s+)?(?:entrega|despacho|envío)",
        r"(\d+)\s*days?\s*(?:delivery|shipping)",
        r"entrega\s+en\s+(\d+)\s*días?",
        r"delivery\s+in\s+(\d+)\s*days?",
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def _extract_product_name(caption: str) -> Optional[str]:
    """Usa la primera línea con contenido real del caption como nombre del producto."""
    if not caption:
        return None
    for line in caption.splitlines():
        line = line.strip()
        # Descarta líneas que solo son hashtags o menciones
        if line and not line.startswith("#") and not line.startswith("@"):
            return line[:255]
    return None


def _extract_promotions(caption: str, hashtags: list) -> Optional[str]:
    """Detecta palabras clave y hashtags promocionales en el caption."""
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
    text = (caption or "").lower()
    found = [kw.title() for kw in keywords if kw in text]

    # También revisa los hashtags del post
    found += [
        "#" + h
        for h in (hashtags or [])
        if any(
            k in h.lower() for k in ["oferta", "promo", "descuento", "sale", "outlet"]
        )
    ]

    if found:
        # Elimina duplicados y recorta al límite del campo
        return ", ".join(dict.fromkeys(found))[:255]
    return None


def _is_in_stock(caption: str) -> bool:
    """Retorna False si el caption contiene palabras de producto agotado."""
    if not caption:
        return True
    palabras_agotado = [
        "agotado",
        "sin stock",
        "no disponible",
        "out of stock",
        "sold out",
    ]
    return not any(kw in caption.lower() for kw in palabras_agotado)


# ── Mapeo principal ───────────────────────────────────────────────────────────


def _map_post_to_instance(post: dict) -> CompetitorMarketData:
    """Convierte un item de Apify en una instancia de CompetitorMarketData."""
    caption = post.get("caption") or ""
    hashtags = post.get("hashtags") or []
    price, currency = _extract_price(caption)
    competitor__name = post.get("ownerFullName")

    """Tomamos en cuenta el caso en el que las empresas en vez de su nombre colocan palabras clave separadas con | """
    if not competitor__name or "|" in competitor__name:
        competitor__name = (post.get("ownerUsername") or "")[:150]

    return CompetitorMarketData(
        competitor_name=competitor__name[:150],
        source=CompetitorMarketData.SourceChoices.INSTAGRAM,
        url=post.get("url"),
        product_name=_extract_product_name(caption),
        category=None,  # No es derivable de forma confiable desde un post de IG
        price=price,
        currency=currency or "USD",
        lead_time_days=_extract_lead_time(caption),
        is_in_stock=_is_in_stock(caption),
        promotions=_extract_promotions(caption, hashtags),
        raw_metadata=post,  # JSON completo retornado por Apify
    )


# ── Función pública ───────────────────────────────────────────────────────────


def scrape_instagram_profiles(
    urls: list[str],
    results_limit: int = 50,
) -> list[CompetitorMarketData]:
    """
    Ejecuta el scraper de Instagram en Apify para las URLs dadas, mapea cada
    post a un registro de CompetitorMarketData, los inserta en masa y los retorna.
    """
    if not APIFY_API_KEY or APIFY_API_KEY == "your_apify_api_key_here":
        raise ValueError(
            "APIFY_API_KEY no está configurado. Reemplaza el placeholder en el archivo .env."
        )

    client = ApifyClient(APIFY_API_KEY)

    actor_input = {
        "directUrls": urls,
        "resultsType": "posts",
        "resultsLimit": results_limit,
        "addParentData": False,
    }

    logger.info("Iniciando scraper de Instagram en Apify para %d URL(s)…", len(urls))
    run = client.actor(INSTAGRAM_ACTOR_ID).call(run_input=actor_input)

    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        logger.error("El run de Apify no retornó un dataset ID.")
        return []

    items = list(client.dataset(dataset_id).iterate_items())
    logger.info("Se obtuvieron %d posts del dataset de Apify.", len(items))

    instances = [_map_post_to_instance(item) for item in items]
    created = CompetitorMarketData.objects.bulk_create(instances)
    logger.info("Se guardaron %d registros en CompetitorMarketData.", len(created))
    return created

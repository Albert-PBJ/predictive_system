import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import urlparse

from apps.benchmarking.models import Competitor, CompetitorMarketData
from apps.competitor_market_data.scrapers import get_client
from apps.competitor_market_data.scrapers.validation import clean_product_name, partition_valid

logger = logging.getLogger(__name__)

WEBSITE_ACTOR_ID = "apify/ai-web-scraper"

AI_PROMPT = (
    "Extract all product titles and, if possible, their prices as well as promotions if available "
    "(all in separate fields). Give me the json with the title, the price and the promo only. "
    "I don't want any additional information on the title field. "
    'For example: {title:"Mesa de Conferencias Headway", price: "40.00USD", promotion: "75% de descuento"}. '
    "You may find content in spanish, and you should return the text contents in the language you find them. "
    "As you can see, the money also has its currency on the field"
)

# Keys the AI scraper might use to wrap a list of extracted products
_WRAPPER_KEYS = ("items", "data", "results", "products", "extractedData", "listings")


# ── Extracción de campos ──────────────────────────────────────────────────────


def _extract_price(price_str: str) -> tuple[Optional[Decimal], Optional[str]]:
    """
    Extrae precio y moneda desde el string retornado por la IA.
    La IA devuelve valores como '40.00USD', '$40.00', '200Bs.', etc.
    """
    if not price_str:
        return None, None

    raw = str(price_str).strip()
    if not raw:
        return None, None

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

    m = re.search(r"([\d,.]+)", raw)
    if m:
        try:
            return Decimal(m.group(1).replace(",", "")), "USD"
        except InvalidOperation:
            pass

    return None, None


def _extract_domain(url: str) -> str:
    """Extrae el dominio limpio de una URL para usarlo como nombre de fallback del competidor."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        if hostname.startswith("www."):
            hostname = hostname[4:]
        return hostname[:150]
    except Exception:
        return url[:150]


def _base_url(url: str) -> str:
    """Retorna solo scheme + netloc de la URL (e.g. https://example.com)."""
    try:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return url


def _flatten_dataset_items(raw_items: list) -> list[dict]:
    """
    Normaliza la estructura de respuesta del AI scraper a una lista plana de dicts de productos.

    El actor puede devolver los datos en varios formatos dependiendo de cómo la IA
    estructura su respuesta:
      - Lista plana de dicts: [{title, price, promotion}, ...]          → se usa tal cual
      - Un dict envoltorio con lista interna: {items: [{...}, ...]}     → se extrae la lista
      - Una lista que contiene una sola lista: [[{...}, ...]]           → se aplana
      - Combinación de los anteriores                                   → se resuelve recursivamente
    """
    product_dicts: list[dict] = []

    for entry in raw_items:
        if isinstance(entry, dict):
            # Si el dict tiene una clave conocida que mapea a una lista, extraemos de ahí
            unwrapped = False
            for key in _WRAPPER_KEYS:
                nested = entry.get(key)
                if isinstance(nested, list):
                    product_dicts.extend(
                        item for item in nested if isinstance(item, dict)
                    )
                    unwrapped = True
                    break

            if not unwrapped:
                # El dict en sí es un producto (tiene title o price)
                if entry.get("title") or entry.get("price"):
                    product_dicts.append(entry)
                else:
                    # Puede ser un envoltorio con claves inesperadas; registramos y seguimos
                    logger.warning(
                        "Item del dataset no tiene 'title' ni 'price' y no coincide con "
                        "ninguna clave de envoltorio conocida. Claves recibidas: %s",
                        list(entry.keys()),
                    )

        elif isinstance(entry, list):
            # El dataset item ES la lista de productos directamente
            product_dicts.extend(item for item in entry if isinstance(item, dict))

        else:
            logger.warning(
                "Item del dataset ignorado: tipo inesperado '%s'.", type(entry).__name__
            )

    return product_dicts


# ── Resolución del modelo Competitor ─────────────────────────────────────────


def _resolve_competitor(source_url: str, competitor_name: Optional[str]) -> Competitor:
    """
    Busca un Competitor por nombre. Si no existe, lo crea con los datos disponibles.
    El nombre tiene prioridad sobre el dominio derivado de la URL.
    """
    name = (competitor_name or _extract_domain(source_url)).strip()[:150]
    if not name:
        name = source_url[:150]

    competitor, created = Competitor.objects.get_or_create(
        name=name,
        defaults={
            "website": _base_url(source_url),
            "is_active": True,
        },
    )
    if created:
        logger.info("Creado nuevo Competitor: '%s'", name)
    return competitor


# ── Mapeo principal ───────────────────────────────────────────────────────────


def _map_item_to_instance(
    item: dict,
    source_url: str,
    competitor: Competitor,
) -> CompetitorMarketData:
    """Convierte un dict de producto extraído por la IA en una instancia de CompetitorMarketData."""
    price_raw = item.get("price") or ""
    price, currency = _extract_price(price_raw)

    promotion = item.get("promotion") or item.get("promo") or None
    if promotion:
        promotion = str(promotion)[:255]

    return CompetitorMarketData(
        competitor=competitor,
        competitor_name=competitor.name,
        source=CompetitorMarketData.SourceChoices.WEBSITE,
        url=source_url,
        product_name=clean_product_name(item.get("title")),
        price=price,
        currency=currency or "USD",
        is_in_stock=True,
        promotions=promotion,
        raw_metadata=item,
    )


# ── Función pública ───────────────────────────────────────────────────────────


def start_website_run(urls: list[str], results_limit: int = 50) -> dict:
    """Inicia (sin bloquear) el run del AI web scraper en Apify y lo retorna."""
    client = get_client()
    actor_input = {
        "startUrls": [{"url": url} for url in urls],
        "prompt": AI_PROMPT,
        "maxCrawlPages": results_limit,
    }
    logger.info("Iniciando run del AI web scraper en Apify para %d URL(s)…", len(urls))
    try:
        return client.actor(WEBSITE_ACTOR_ID).start(run_input=actor_input)
    except Exception as exc:
        logger.error("Error al iniciar el actor de Apify: %s", exc, exc_info=True)
        raise ValueError(f"Apify actor falló: {exc}") from exc


def finalize_website(
    dataset_id: str,
    urls: list[str],
    competitor_name: Optional[str] = None,
) -> list[CompetitorMarketData]:
    """
    Lee el dataset de un run finalizado, normaliza la estructura del AI scraper,
    resuelve el FK a Competitor (get_or_create por nombre) y guarda los registros.

    A diferencia de Instagram y Facebook, este scraper resuelve el FK a Competitor
    en lugar de dejar competitor=None.
    """
    client = get_client()

    try:
        raw_items = list(client.dataset(dataset_id).iterate_items())
    except Exception as exc:
        logger.error("Error al obtener items del dataset '%s': %s", dataset_id, exc, exc_info=True)
        raise ValueError(f"No se pudieron leer los items del dataset de Apify: {exc}") from exc

    logger.info(
        "Dataset '%s': %d item(s) crudos recibidos de Apify.", dataset_id, len(raw_items)
    )

    if not raw_items:
        logger.warning("El dataset de Apify está vacío. No se guardarán registros.")
        return []

    # Log the raw structure of the first item to help debug format issues
    first = raw_items[0]
    logger.info(
        "Estructura del primer item: tipo=%s, claves=%s",
        type(first).__name__,
        list(first.keys()) if isinstance(first, dict) else "(lista)",
    )

    product_dicts = _flatten_dataset_items(raw_items)
    logger.info("%d producto(s) encontrados tras normalizar la estructura.", len(product_dicts))

    if not product_dicts:
        logger.warning(
            "No se encontraron productos válidos en el dataset. "
            "Revisa la estructura del output del actor con dataset_id='%s'.",
            dataset_id,
        )
        return []

    # Cache competitors per key to avoid redundant DB hits
    competitor_cache: dict[str, Competitor] = {}
    instances: list[CompetitorMarketData] = []

    for product in product_dicts:
        # The AI scraper may include a source URL inside each item
        source_url = product.get("url") or (urls[0] if urls else "")
        cache_key = competitor_name or _extract_domain(source_url)

        try:
            if cache_key not in competitor_cache:
                competitor_cache[cache_key] = _resolve_competitor(source_url, competitor_name)
            competitor = competitor_cache[cache_key]
            instances.append(_map_item_to_instance(product, source_url, competitor))
        except Exception as exc:
            logger.error(
                "Error al mapear producto '%s': %s",
                product.get("title", "<sin título>"),
                exc,
                exc_info=True,
            )

    if not instances:
        logger.error("Ningún producto pudo ser mapeado. No se guardarán registros.")
        return []

    # Descarta registros con datos no plausibles (precio fuera de rango, sin
    # nombre de producto) para no contaminar el dataset de los modelos de ML.
    instances, _discarded = partition_valid(instances)
    if not instances:
        logger.warning(
            "Todos los productos fueron descartados por la validación de calidad. "
            "No se guardarán registros."
        )
        return []

    try:
        created = CompetitorMarketData.objects.bulk_create(instances)
    except Exception as exc:
        logger.error("Error en bulk_create de CompetitorMarketData: %s", exc, exc_info=True)
        raise ValueError(f"Error al guardar los datos en la base de datos: {exc}") from exc

    logger.info("Se guardaron %d registros en CompetitorMarketData.", len(created))
    return created


def scrape_website(
    urls: list[str],
    results_limit: int = 50,
    competitor_name: Optional[str] = None,
) -> list[CompetitorMarketData]:
    """Versión bloqueante (start + esperar + finalizar) usada por el comando CLI."""
    run = start_website_run(urls=urls, results_limit=results_limit)
    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        logger.error("El run de Apify no retornó un dataset ID. Run info: %s", run)
        return []

    get_client().run(run["id"]).wait_for_finish()
    return finalize_website(dataset_id, urls=urls, competitor_name=competitor_name)

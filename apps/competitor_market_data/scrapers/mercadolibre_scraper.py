import logging
from decimal import Decimal, InvalidOperation
from typing import Optional

from apps.benchmarking.models import Competitor, CompetitorMarketData
from apps.competitor_market_data.enrichment import deepseek
from apps.competitor_market_data.scrapers import (
    backfill_competitor_location,
    classify_category,
    get_client,
    resolve_location,
)
from apps.competitor_market_data.scrapers.validation import clean_product_name, partition_valid

logger = logging.getLogger(__name__)

# Actor dedicado a Mercado Libre. A diferencia del AI web scraper, soporta el
# proxy/cuenta que Mercado Libre exige y devuelve datos ya estructurados.
MERCADOLIBRE_ACTOR_ID = "piotrv1001/mercado-libre-listings-scraper"

# Mercado Libre Venezuela. (MLA=Argentina, MLB=Brasil, MLM=México, etc.)
SITE_ID = "MLV"

# Constantes del run (editá acá para ajustar el comportamiento del actor):
#   - officialStoresOnly: False trae más vendedores (no solo tiendas oficiales).
#   - condition "any": productos nuevos y usados.
#   - sort "relevance": el orden por defecto de Mercado Libre.
# El proxy RESIDENCIAL en Venezuela es necesario: Mercado Libre bloquea el scraping
# sin una cuenta/proxy adecuados.
_OFFICIAL_STORES_ONLY = False
_CONDITION = "any"
_SORT = "relevance"
_PROXY_CONFIGURATION = {
    "useApifyProxy": True,
    "apifyProxyGroups": ["RESIDENTIAL"],
    "apifyProxyCountry": "VE",
}

# Confianza mínima para adoptar el nombre de competidor propuesto por el LLM
# (mismo umbral que en Facebook/Instagram).
_MIN_CONFIDENCE = 0.55

# Tope de precio almacenable: el campo es DecimalField(max_digits=10), así que un
# precio en bolívares muy grande no cabe. Por encima de esto guardamos price=None.
_MAX_STORABLE_PRICE = Decimal("99999999.99")


# ── Helpers de acceso seguro ──────────────────────────────────────────────────


def _as_text(value) -> str:
    """Convierte un valor a string de forma segura."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _as_dict(value) -> dict:
    """Retorna el valor si es dict; si no, un dict vacío."""
    return value if isinstance(value, dict) else {}


# ── Extracción de campos ──────────────────────────────────────────────────────


def _normalize_currency(currency: str) -> str:
    """Normaliza el código de moneda al de 3 letras que usa el modelo.

    Mercado Libre Venezuela publica en bolívares (VES) o dólares (USD). El bolívar
    aparece a veces como 'VEF' (código viejo); lo unificamos a 'VES'.
    """
    cur = (currency or "").strip().upper()[:3]
    if cur == "VEF":
        return "VES"
    return cur or "USD"


def _extract_price(item: dict) -> tuple[Optional[Decimal], str]:
    """Precio y moneda del listing. None si no hay precio o si no cabe en el campo."""
    raw = item.get("price")
    currency = _normalize_currency(_as_text(item.get("currency")))
    if raw in (None, "", 0):
        return None, currency
    try:
        price = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None, currency
    if price <= 0 or price > _MAX_STORABLE_PRICE:
        # Precio inválido o demasiado grande para DecimalField(max_digits=10):
        # lo dejamos en None para no romper el bulk_create (lo loguea el actor).
        if price > _MAX_STORABLE_PRICE:
            logger.warning(
                "Precio %s %s excede el máximo almacenable; se guarda sin precio.",
                price, currency,
            )
        return None, currency
    return price, currency


def _extract_promotions(item: dict) -> Optional[str]:
    """Arma las promociones a partir de los campos estructurados del listing:
    descuento, envío gratis y cuotas (meses sin intereses si la tasa es 0)."""
    promos: list[str] = []

    discount = item.get("discountPercent")
    if isinstance(discount, (int, float)) and discount > 0:
        promos.append(f"{int(discount)}% de descuento")

    if item.get("freeShipping") is True:
        promos.append("Envío gratis")

    installments = _as_dict(item.get("installments"))
    quantity = installments.get("quantity")
    if isinstance(quantity, int) and quantity > 1:
        rate = installments.get("rate")
        if rate == 0:
            promos.append(f"{quantity} cuotas sin interés")
        else:
            promos.append(f"{quantity} cuotas")

    if not promos:
        return None
    return ", ".join(dict.fromkeys(promos))[:255]


def _is_in_stock(item: dict) -> bool:
    """Disponibilidad: hay stock salvo que la cantidad disponible sea 0."""
    qty = item.get("availableQuantity")
    if isinstance(qty, int):
        return qty > 0
    return True


def _category_text(item: dict) -> str:
    """Texto para clasificar la categoría: título + marca de los atributos."""
    title = _as_text(item.get("title"))
    brand = _as_text(_as_dict(item.get("attributes")).get("brand"))
    return f"{title} {brand}".strip()


def _seller_name(item: dict) -> str:
    """Nombre del vendedor/tienda del listing (tienda oficial si la hay, si no el
    nickname). Vacío si no se puede identificar."""
    seller = _as_dict(item.get("seller"))
    name = _as_text(seller.get("storeName")) or _as_text(seller.get("nickname"))
    return name.strip()[:150]


def _seller_location(item: dict) -> tuple[str, str]:
    """(municipio, estado) del vendedor desde el objeto location del listing."""
    loc = _as_dict(item.get("location"))
    # resolve_location normaliza el estado venezolano a su nombre oficial.
    return resolve_location(_as_text(loc.get("state")), _as_text(loc.get("city")), "")


# ── Mapeo determinista (sin LLM) ──────────────────────────────────────────────


def _map_listing_to_instance(item: dict) -> CompetitorMarketData:
    """Convierte un listing de Mercado Libre en una instancia de CompetitorMarketData.

    El competidor (vendedor) se resuelve aparte (ver `_resolve_competitors`), de
    forma determinista a partir del campo `seller`, con dedupe opcional vía LLM.
    """
    price, currency = _extract_price(item)
    return CompetitorMarketData(
        competitor=None,
        competitor_name="",
        source=CompetitorMarketData.SourceChoices.MERCADOLIBRE,
        url=_as_text(item.get("permalink")) or None,
        product_name=clean_product_name(_as_text(item.get("title"))),
        category=classify_category(_category_text(item)),
        price=price,
        currency=currency,
        is_in_stock=_is_in_stock(item),
        promotions=_extract_promotions(item),
        raw_metadata=item,
    )


# ── Resolución del competidor (vendedor) ──────────────────────────────────────


def _resolve_competitors(pairs: list[tuple[CompetitorMarketData, dict]]) -> None:
    """Asigna el competidor (vendedor) a cada registro, in-place.

    El vendedor viene estructurado en el campo `seller`, así que la resolución es
    DETERMINISTA por defecto (dedupe por nombre, caché dentro del run + get_or_create
    entre runs) y rellena el estado/municipio del vendedor desde `location`.

    El LLM es OPCIONAL y hace poco esfuerzo (como pediste): si está activo, solo
    intenta enlazar el vendedor a un competidor ya conocido (dedupe difuso) y afinar
    promociones/nombre de producto. Apagado (default), todo se resuelve sin LLM.
    """
    llm_on = deepseek.is_enabled()
    if llm_on:
        logger.info(
            "Enriquecimiento LLM ACTIVO para Mercado Libre (modelo=%s): dedupe de "
            "vendedores y afinado de promociones.",
            deepseek.DEEPSEEK_MODEL,
        )
        known = list(Competitor.objects.filter(is_active=True).values("id", "name"))
    else:
        logger.info(
            "Enriquecimiento LLM DESACTIVADO; el vendedor se resuelve de forma "
            "determinista desde el campo 'seller' de cada listing."
        )
        known = []

    name_cache: dict[str, Competitor] = {}
    linked_existing = 0   # enlazados a un competidor ya existente (dedupe)
    created_new = 0       # competidores nuevos creados
    without_seller = 0    # listings sin vendedor identificable → "Mercado Libre"

    for instance, item in pairs:
        municipality, state = _seller_location(item)
        result = {}
        if llm_on:
            result = deepseek.enrich_listing(
                title=_as_text(item.get("title")),
                description=_listing_description(item),
                location=f"{_as_dict(item.get('location')).get('city', '')} "
                         f"{_as_dict(item.get('location')).get('state', '')}".strip(),
                known_competitors=known,
            )

        competitor, outcome = _resolve_one_competitor(
            item, result, known, name_cache, municipality, state
        )
        instance.competitor = competitor
        instance.competitor_name = competitor.name
        if outcome == "created":
            created_new += 1
        elif outcome == "existing":
            linked_existing += 1
        else:  # fallback "Mercado Libre"
            without_seller += 1

        # Afinado opcional vía LLM (prevalece sobre lo determinista cuando aporta algo).
        if llm_on:
            promotions = result.get("promotions")
            if promotions:
                instance.promotions = promotions[:255]
            product_name = clean_product_name(result.get("product_name"))
            if product_name:
                instance.product_name = product_name

    logger.info(
        "Mercado Libre: vendedores resueltos (enlazados a existente: %d, nuevos: %d, "
        "sin vendedor → 'Mercado Libre': %d).",
        linked_existing, created_new, without_seller,
    )


def _resolve_one_competitor(
    item: dict,
    result: dict,
    known: list[dict],
    name_cache: dict,
    municipality: str,
    state: str,
) -> tuple[Competitor, str]:
    """Resuelve (o crea) el Competitor de un listing. Retorna (competitor, outcome)
    con outcome ∈ {"existing", "created", "fallback"}."""
    # 1) Match difuso del LLM contra un competidor conocido (por id).
    matched_id = result.get("matched_competitor_id")
    if matched_id is not None:
        comp = Competitor.objects.filter(id=matched_id).first()
        if comp is not None:
            backfill_competitor_location(comp, municipality, state)
            return comp, "existing"

    # 2) Nombre: el del LLM si tiene confianza suficiente; si no, el vendedor
    #    estructurado; si tampoco hay, el propio marketplace.
    llm_name = result.get("competitor_name")
    confidence = result.get("confidence") or 0.0
    seller = _seller_name(item)
    is_fallback = False
    if llm_name and confidence >= _MIN_CONFIDENCE:
        name = llm_name[:150]
    elif seller:
        name = seller
    else:
        name = "Mercado Libre"
        is_fallback = True

    key = name.lower()
    if key in name_cache:
        comp = name_cache[key]
        backfill_competitor_location(comp, municipality, state)
        return comp, ("fallback" if is_fallback else "existing")

    comp, created = Competitor.objects.get_or_create(
        name=name,
        defaults={
            "is_active": True,
            "state": state,
            "municipality": municipality,
            "notes": "Detectado automáticamente desde Mercado Libre.",
        },
    )
    if created and known is not None:
        known.append({"id": comp.id, "name": comp.name})
    if not created:
        backfill_competitor_location(comp, municipality, state)
    name_cache[key] = comp
    outcome = "created" if created else ("fallback" if is_fallback else "existing")
    return comp, outcome


def _listing_description(item: dict) -> str:
    """Texto de contexto para el LLM: marca/atributos + vendedor del listing."""
    attrs = _as_dict(item.get("attributes"))
    attr_text = ", ".join(f"{k}: {v}" for k, v in attrs.items() if v)
    seller = _seller_name(item)
    parts = []
    if attr_text:
        parts.append(attr_text)
    if seller:
        parts.append(f"Vendedor: {seller}")
    return ". ".join(parts)


# ── Funciones públicas ────────────────────────────────────────────────────────


def start_mercadolibre_run(urls: list[str], results_limit: int = 50) -> dict:
    """Inicia (sin bloquear) el run del scraper de Mercado Libre y lo retorna.

    A diferencia de los otros scrapers, Mercado Libre busca por TÉRMINOS DE BÚSQUEDA,
    no por URLs. Para reutilizar el contrato genérico de las vistas, ``urls`` trae
    aquí los términos de búsqueda (uno por elemento).
    """
    client = get_client()
    search_queries = [q for q in (urls or []) if q]
    pages_per_query = max(1, -(-results_limit // 40))  # ceil(limit/40), ~40 items/página
    actor_input = {
        "siteId": SITE_ID,
        "searchQueries": search_queries,
        "condition": _CONDITION,
        "sort": _SORT,
        "officialStoresOnly": _OFFICIAL_STORES_ONLY,
        "includeProductDetail": False,
        "maxItems": results_limit,
        "maxPagesPerQuery": pages_per_query,
        "proxyConfiguration": _PROXY_CONFIGURATION,
    }
    logger.info(
        "Iniciando run de Mercado Libre en Apify para %d término(s) de búsqueda…",
        len(search_queries),
    )
    return client.actor(MERCADOLIBRE_ACTOR_ID).start(run_input=actor_input)


def finalize_mercadolibre(dataset_id: str) -> list[CompetitorMarketData]:
    """Lee el dataset de un run finalizado, mapea cada listing y guarda los registros.

    El mapeo de campos es determinista; el vendedor (competidor) se resuelve desde
    el campo `seller` con dedupe opcional vía LLM antes de persistir.
    """
    client = get_client()
    items = list(client.dataset(dataset_id).iterate_items())
    logger.info("Se obtuvieron %d listings de Mercado Libre del dataset de Apify.", len(items))

    pairs = [(_map_listing_to_instance(item), item) for item in items if isinstance(item, dict)]
    _resolve_competitors(pairs)

    instances = [instance for instance, _ in pairs]
    # Descarta registros con datos no plausibles (precio fuera de rango, sin
    # nombre de producto) para no contaminar el dataset de los modelos de ML.
    valid, _discarded = partition_valid(instances)
    created = CompetitorMarketData.objects.bulk_create(valid)
    logger.info(
        "Se guardaron %d registros de Mercado Libre en CompetitorMarketData (de %d recolectados).",
        len(created), len(instances),
    )
    return created


def scrape_mercadolibre(
    search_queries: list[str],
    results_limit: int = 50,
) -> list[CompetitorMarketData]:
    """Versión bloqueante (start + esperar + finalizar) usada por el comando CLI."""
    run = start_mercadolibre_run(urls=search_queries, results_limit=results_limit)
    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        logger.error("El run de Apify no retornó un dataset ID.")
        return []

    get_client().run(run["id"]).wait_for_finish()
    return finalize_mercadolibre(dataset_id)

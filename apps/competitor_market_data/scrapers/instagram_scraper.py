import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from apps.benchmarking.models import Competitor, CompetitorMarketData
from apps.competitor_market_data.enrichment import deepseek
from apps.competitor_market_data.scrapers import CATEGORY_NAMES, classify_category, get_client

logger = logging.getLogger(__name__)

INSTAGRAM_ACTOR_ID = "apify/instagram-scraper"

# Confianza mínima para adoptar el nombre de competidor propuesto por el LLM.
_MIN_CONFIDENCE = 0.55

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


# ── Identidad del competidor (handle de Instagram) ────────────────────────────


def _handle_from(value: str) -> str:
    """Normaliza un handle/URL/@usuario de Instagram a un handle simple en minúsculas.

    Ej.: 'https://www.instagram.com/MobiliarioDeOficina/' → 'mobiliariodeoficina',
    '@MobiliarioDeOficina' → 'mobiliariodeoficina'. El handle es la identidad
    estable de un perfil, así que sirve para deduplicar competidores entre posts.
    """
    value = (value or "").strip().lower()
    if not value:
        return ""
    m = re.search(r"instagram\.com/([^/?#]+)", value)
    if m:
        value = m.group(1)
    return value.lstrip("@").strip("/")


def _baseline_competitor_name(post: dict) -> str:
    """Nombre de competidor de respaldo (sin LLM): nombre del perfil o el handle.

    Algunas empresas colocan palabras clave separadas por '|' en vez de un nombre
    (p. ej. 'Sillas | Escritorios | Mesas'); en ese caso usamos el handle.
    """
    name = (post.get("ownerFullName") or "").strip()
    if not name or "|" in name:
        name = (post.get("ownerUsername") or "").strip()
    return name[:150]


# ── Mapeo determinista (sin LLM) ──────────────────────────────────────────────


def _map_post_to_instance(post: dict) -> CompetitorMarketData:
    """Convierte un item de Apify en una instancia de CompetitorMarketData.

    Mapeo 100% determinista (regex + palabras clave). El competidor, el nombre de
    producto y la categoría se afinan luego, de forma opcional, vía LLM (ver
    `_enrich_posts`).
    """
    caption = post.get("caption") or ""
    hashtags = post.get("hashtags") or []
    price, currency = _extract_price(caption)

    return CompetitorMarketData(
        competitor=None,
        competitor_name=_baseline_competitor_name(post),
        source=CompetitorMarketData.SourceChoices.INSTAGRAM,
        url=post.get("url"),
        product_name=_extract_product_name(caption),
        category=classify_category(f"{caption} {' '.join(hashtags)}"),
        price=price,
        currency=currency or "USD",
        lead_time_days=_extract_lead_time(caption),
        is_in_stock=_is_in_stock(caption),
        promotions=_extract_promotions(caption, hashtags),
        raw_metadata=post,  # JSON completo retornado por Apify
    )


# ── Enriquecimiento vía LLM (opcional) ────────────────────────────────────────


def _resolve_instagram_competitor(
    post: dict,
    result: dict,
    known: list[dict],
    handle_index: dict,
    created_cache: dict,
) -> tuple[Optional[Competitor], str, str]:
    """Resuelve el Competitor del post, deduplicando por handle de Instagram.

    Prioridad (de más a menos fiable):
    1. Handle ya resuelto en este run (cache).
    2. Competidor existente con ese mismo handle de Instagram (dedupe entre runs).
    3. Match propuesto por el LLM contra un competidor conocido (por id).
    4. Crear un competidor nuevo para el perfil (nombre del LLM o de respaldo).

    Retorna ``(competitor, name, outcome)`` con ``outcome`` ∈ {"existing",
    "created", "none"}, para medirlo al final.
    """
    handle = _handle_from(post.get("ownerUsername") or post.get("inputUrl"))

    # 1) Dedupe dentro del mismo run.
    if handle and handle in created_cache:
        comp = created_cache[handle]
        return comp, comp.name, "existing"

    # 2) Competidor existente con ese handle (dedupe entre runs).
    if handle and handle in handle_index:
        comp = handle_index[handle]
        created_cache[handle] = comp
        return comp, comp.name, "existing"

    # 3) Match del LLM contra un competidor conocido.
    matched_id = result.get("matched_competitor_id")
    if matched_id is not None:
        comp = Competitor.objects.filter(id=matched_id).first()
        if comp is not None:
            _backfill_handle(comp, handle, handle_index)
            if handle:
                created_cache[handle] = comp
            return comp, comp.name, "existing"

    # 4) Crear un competidor nuevo para el perfil. En Instagram el dueño del perfil
    #    casi siempre ES la empresa, así que usamos el nombre del LLM si tiene
    #    confianza suficiente y, si no, el nombre de respaldo determinista.
    name = result.get("competitor_name")
    confidence = result.get("confidence") or 0.0
    if not name or confidence < _MIN_CONFIDENCE:
        name = _baseline_competitor_name(post)
    if not name:
        return None, "", "none"

    profile_url = f"https://www.instagram.com/{handle}/" if handle else ""
    comp, created = Competitor.objects.get_or_create(
        name=name[:150],
        defaults={
            "is_active": True,
            "instagram": profile_url,
            "notes": "Detectado automáticamente desde Instagram (LLM).",
        },
    )
    _backfill_handle(comp, handle, handle_index)
    if created:
        logger.info("Creado nuevo Competitor desde Instagram: '%s' (@%s)", comp.name, handle)
        known.append({"id": comp.id, "name": comp.name})
    if handle:
        created_cache[handle] = comp
    return comp, comp.name, ("created" if created else "existing")


def _backfill_handle(comp: Competitor, handle: str, handle_index: dict) -> None:
    """Si el competidor no tenía handle de Instagram, lo completa (mejora el dedupe futuro)."""
    if handle and not _handle_from(comp.instagram):
        comp.instagram = f"https://www.instagram.com/{handle}/"
        comp.save(update_fields=["instagram"])
        handle_index[handle] = comp


def _enrich_posts(pairs: list[tuple[CompetitorMarketData, dict]]) -> None:
    """Enriquece los registros in-place con el LLM, si está habilitado.

    Por cada post, una sola llamada extrae producto, categoría, promociones y el
    competidor (deduplicado por handle de Instagram). No-op silencioso cuando el
    enriquecimiento está apagado. Cada llamada está aislada: si una falla, el resto
    de los registros se guardan igual.
    """
    if not deepseek.is_enabled():
        logger.info(
            "Enriquecimiento LLM DESACTIVADO (USE_LLM_ENRICHMENT=%s, DEEPSEEK_API_KEY %s); "
            "se omite el enriquecimiento de posts de Instagram. Si esperabas que corriera, "
            "revisa el .env y REINICIA el servidor.",
            deepseek.USE_LLM_ENRICHMENT,
            "presente" if deepseek.DEEPSEEK_API_KEY else "ausente",
        )
        return

    logger.info(
        "Enriquecimiento LLM ACTIVO: analizando %d post(s) de Instagram con DeepSeek (modelo=%s)…",
        len(pairs),
        deepseek.DEEPSEEK_MODEL,
    )
    known = list(Competitor.objects.filter(is_active=True).values("id", "name"))
    # Índice handle → Competitor para deduplicar perfiles ya registrados.
    handle_index: dict[str, Competitor] = {}
    for comp in Competitor.objects.exclude(instagram=""):
        h = _handle_from(comp.instagram)
        if h:
            handle_index.setdefault(h, comp)
    created_cache: dict[str, Competitor] = {}

    linked_existing = 0   # posts enlazados a un competidor ya existente (dedupe)
    created_new = 0       # competidores nuevos creados por el LLM
    with_product = 0      # posts con nombre de producto identificado por el LLM
    with_promotions = 0   # posts con promociones/beneficios extraídos
    found_something = 0   # posts donde el LLM aportó algún dato

    for instance, post in pairs:
        result = deepseek.enrich_instagram_post(
            caption=post.get("caption") or "",
            owner_username=post.get("ownerUsername") or "",
            owner_full_name=post.get("ownerFullName") or "",
            hashtags=post.get("hashtags") or [],
            location=post.get("locationName") or "",
            category_options=CATEGORY_NAMES,
            known_competitors=known,
        )
        item_found = False

        competitor, name, outcome = _resolve_instagram_competitor(
            post, result, known, handle_index, created_cache
        )
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

        # El LLM lee el producto del caption desordenado mejor que la heurística.
        product_name = result.get("product_name")
        if product_name:
            instance.product_name = product_name[:255]
            with_product += 1
            item_found = True

        # Completa/corrige la categoría determinista cuando el LLM la identifica.
        category = result.get("category")
        if category:
            instance.category = category

        # Promociones/beneficios del texto libre; prevalece sobre las palabras clave.
        promotions = result.get("promotions")
        if promotions:
            instance.promotions = promotions[:255]
            with_promotions += 1
            item_found = True

        # Precio: solo como respaldo cuando la regex determinista no encontró nada.
        if instance.price is None and result.get("price") is not None:
            try:
                value = Decimal(str(result["price"]))
                if value > 0:
                    instance.price = value
                    instance.currency = result.get("currency") or instance.currency or "USD"
            except (InvalidOperation, ValueError):
                pass

        if item_found:
            found_something += 1

    logger.info(
        "Enriquecimiento LLM finalizado: %d de %d post(s) con datos del LLM "
        "(enlazados a competidor existente: %d, competidor nuevo creado: %d, "
        "productos identificados: %d, promociones/beneficios extraídos: %d).",
        found_something,
        len(pairs),
        linked_existing,
        created_new,
        with_product,
        with_promotions,
    )


# ── Función pública ───────────────────────────────────────────────────────────


def start_instagram_run(urls: list[str], results_limit: int = 50) -> dict:
    """Inicia (sin bloquear) el run del scraper de Instagram en Apify y lo retorna.

    El dict devuelto incluye `id` y `defaultDatasetId`, usados luego para
    consultar el progreso y, al finalizar, leer y guardar los resultados.
    """
    client = get_client()
    actor_input = {
        "directUrls": urls,
        "resultsType": "posts",
        "resultsLimit": results_limit,
        "addParentData": False,
    }
    logger.info("Iniciando run de Instagram en Apify para %d URL(s)…", len(urls))
    return client.actor(INSTAGRAM_ACTOR_ID).start(run_input=actor_input)


def finalize_instagram(dataset_id: str) -> list[CompetitorMarketData]:
    """Lee el dataset de un run finalizado, mapea cada post y guarda los registros.

    El mapeo de campos es determinista; el producto, la categoría y la
    identificación del competidor se afinan de forma opcional vía LLM (DeepSeek)
    antes de persistir.
    """
    client = get_client()
    items = list(client.dataset(dataset_id).iterate_items())
    logger.info("Se obtuvieron %d posts del dataset de Apify.", len(items))

    pairs = [(_map_post_to_instance(item), item) for item in items]
    _enrich_posts(pairs)

    instances = [instance for instance, _ in pairs]
    created = CompetitorMarketData.objects.bulk_create(instances)
    logger.info("Se guardaron %d registros en CompetitorMarketData.", len(created))
    return created


def scrape_instagram_profiles(
    urls: list[str],
    results_limit: int = 50,
) -> list[CompetitorMarketData]:
    """Versión bloqueante (start + esperar + finalizar) usada por el comando CLI."""
    run = start_instagram_run(urls=urls, results_limit=results_limit)
    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        logger.error("El run de Apify no retornó un dataset ID.")
        return []

    get_client().run(run["id"]).wait_for_finish()
    return finalize_instagram(dataset_id)

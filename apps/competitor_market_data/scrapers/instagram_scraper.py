import logging
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Optional

from apps.benchmarking.models import Competitor, CompetitorMarketData
from apps.competitor_market_data.enrichment import deepseek, image_ocr
from apps.competitor_market_data.scrapers import (
    CATEGORY_NAMES,
    backfill_competitor_location,
    classify_category,
    get_client,
    resolve_location,
)
from apps.competitor_market_data.scrapers.competitors import get_or_create_competitor
from apps.competitor_market_data.scrapers.persistence import ensure_scrape_run, persist_records
from apps.competitor_market_data.scrapers.validation import clean_product_name, looks_like_statement

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


# Palabras de TITULAR PROMOCIONAL. Las marcas suelen poner un gancho de oferta en la
# PRIMERA línea (p. ej. "HAT TRICK DE DESCUENTOS") y el producto real debajo, así que
# saltamos estas líneas al elegir el nombre del producto.
_PROMO_HEADLINE_KEYWORDS = (
    "oferta", "ofertas", "descuento", "descuentos", "promoción", "promocion",
    "promo", "promos", "rebaja", "rebajas", "sale", "liquidación", "liquidacion",
    "outlet", "2x1", "3x2", "% off", "% de descuento", "precio especial",
    "super precio", "súper precio", "black friday", "cyber", "hot sale", "remate",
    "hat trick", "combazo", "promoción especial",
)


def _is_promo_headline(line: str) -> bool:
    """True si la línea es un gancho/titular promocional (no el nombre del producto)."""
    lowered = line.lower()
    return any(keyword in lowered for keyword in _PROMO_HEADLINE_KEYWORDS)


def _extract_product_name(caption: str) -> Optional[str]:
    """Elige la línea del caption que mejor parece el NOMBRE del producto.

    Salta líneas de solo hashtags/menciones, TITULARES PROMOCIONALES (p. ej.
    'HAT TRICK DE DESCUENTOS', que suelen ir arriba del producto real) y eslóganes
    (`looks_like_statement`). Si tras filtrar no queda ninguna, cae a la primera
    línea con contenido (mejor algo que nada; la validación hará el resto).
    """
    if not caption:
        return None
    candidates = [
        line.strip()
        for line in caption.splitlines()
        if line.strip() and not line.strip().startswith(("#", "@"))
    ]
    if not candidates:
        return None
    for line in candidates:
        if not _is_promo_headline(line) and not looks_like_statement(line):
            return line[:255]
    return candidates[0][:255]


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
        product_name=clean_product_name(_extract_product_name(caption)),
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
    municipality: str,
    state: str,
) -> tuple[Optional[Competitor], str, str]:
    """Resuelve el Competitor del post, deduplicando por handle de Instagram.

    Prioridad (de más a menos fiable):
    1. Handle ya resuelto en este run (cache).
    2. Competidor existente con ese mismo handle de Instagram (dedupe entre runs).
    3. Match propuesto por el LLM contra un competidor conocido (por id).
    4. Crear un competidor nuevo para el perfil (nombre del LLM o de respaldo).

    Rellena el estado/municipio del competidor (sin pisar datos existentes).

    Retorna ``(competitor, name, outcome)`` con ``outcome`` ∈ {"existing",
    "created", "none"}, para medirlo al final.
    """
    handle = _handle_from(post.get("ownerUsername") or post.get("inputUrl"))

    # 1) Dedupe dentro del mismo run.
    if handle and handle in created_cache:
        comp = created_cache[handle]
        backfill_competitor_location(comp, municipality, state)
        return comp, comp.name, "existing"

    # 2) Competidor existente con ese handle (dedupe entre runs).
    if handle and handle in handle_index:
        comp = handle_index[handle]
        backfill_competitor_location(comp, municipality, state)
        created_cache[handle] = comp
        return comp, comp.name, "existing"

    # 3) Match del LLM contra un competidor conocido.
    matched_id = result.get("matched_competitor_id")
    if matched_id is not None:
        comp = Competitor.objects.filter(id=matched_id).first()
        if comp is not None:
            _backfill_handle(comp, handle, handle_index)
            backfill_competitor_location(comp, municipality, state)
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
    comp, created = get_or_create_competitor(
        name[:150],
        defaults={
            "is_active": True,
            "instagram": profile_url,
            "state": state,
            "municipality": municipality,
            "notes": "Detectado automáticamente desde Instagram (LLM).",
        },
    )
    _backfill_handle(comp, handle, handle_index)
    if created:
        logger.info("Creado nuevo Competitor desde Instagram: '%s' (@%s)", comp.name, handle)
        known.append({"id": comp.id, "name": comp.name})
    else:
        backfill_competitor_location(comp, municipality, state)
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

        municipality, state = resolve_location(
            result.get("state"), result.get("municipality"), post.get("locationName") or ""
        )
        competitor, name, outcome = _resolve_instagram_competitor(
            post, result, known, handle_index, created_cache, municipality, state
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
        product_name = clean_product_name(result.get("product_name"))
        if product_name:
            instance.product_name = product_name
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


# ── OCR de imágenes: precio como último recurso (red neuronal EasyOCR) ─────────


def _post_image_urls(post: dict) -> list[str]:
    """Reúne las URLs de imagen de un post de Apify (principal, adicionales y carrusel).

    En Instagram el precio suele estar dentro de la imagen del flyer; estas son las
    imágenes candidatas a leer con OCR cuando el texto no trajo un precio.
    """
    urls: list[str] = []

    def _add(value):
        if isinstance(value, str) and value and value not in urls:
            urls.append(value)

    _add(post.get("displayUrl"))
    for img in post.get("images") or []:
        _add(img)
    # Posts de carrusel: cada hijo trae su propia imagen.
    for child in post.get("childPosts") or []:
        if isinstance(child, dict):
            _add(child.get("displayUrl"))
    return urls


# Secuencias largas tipo teléfono: sus dígitos no deben tomarse como precio.
_PHONE_LIKE_RE = re.compile(r"\d[\d\s().\-]{7,}\d")
# Unidades/sustantivos que, pegados a un número, lo descartan como precio.
_NON_PRICE_UNIT_RE = re.compile(
    r"\s*(?:%|cm|mm|mts?|kg|meses|mes|cuotas?|años?|anos?|piezas?|unidades?|und)\b",
    re.IGNORECASE,
)

# Piso del precio adivinado.
_BARE_PRICE_MIN = Decimal("5")
# Techo de SANIDAD para números CON indicador de precio actual ("AHORA 750"): se
# confía en ellos hasta aquí y el gate por categoría (validation.PRICE_BANDS) hace el
# filtrado fino. Es amplio a propósito (por encima de toda banda) para no recortar un
# precio legítimo; solo bloquea errores groseros de OCR. Los números SIN indicador
# (más arriesgados) usan el tope más estricto `OCR_BARE_NUMBER_MAX_USD`.
_INDICATOR_PRICE_MAX = Decimal("5000")

# Palabras (sin acentos, en minúscula) que anteceden al PRECIO ACTUAL en un flyer.
# Un número precedido por una de estas es casi seguro el precio: "AHORA 250",
# "POR SOLO 80", "PRECIO120", "a solo 99". Las multipalabra van primero por prolijidad.
_CURRENT_PRICE_INDICATORS = (
    "por solo", "a solo", "llevalo por", "llevatelo por", "ahora",
    "precio", "solo", "solamente", "hoy", "oferta de",
)
# Palabras que anteceden al PRECIO VIEJO/tachado: el número que las sigue NO es el
# precio real (es el "antes"), así que se descarta. "ANTES 280", "precio regular 300".
_PREVIOUS_PRICE_INDICATORS = (
    "antes", "precio regular", "precio normal", "regular", "normal",
)


def _norm_indicator_text(text: str) -> str:
    """Minúsculas y sin acentos, para comparar indicadores de precio de forma robusta."""
    text = unicodedata.normalize("NFD", text or "")
    return "".join(c for c in text if unicodedata.category(c) != "Mn").lower()


def _ends_with_indicator(context: str, indicators: tuple[str, ...]) -> bool:
    """True si `context` termina con alguno de los indicadores, en frontera de palabra.

    La frontera evita falsos positivos como 'aprecio' coincidiendo con 'precio'.
    """
    for indicator in indicators:
        if context.endswith(indicator):
            prefix_len = len(context) - len(indicator)
            if prefix_len == 0 or not context[prefix_len - 1].isalpha():
                return True
    return False


def _parse_ocr_number(token: str) -> Optional[Decimal]:
    """Convierte un token numérico del OCR a Decimal (coma = separador de miles)."""
    cleaned = token.strip(".,").replace(",", "")
    if not cleaned or not any(c.isdigit() for c in cleaned):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _guess_bare_price_usd(text: str) -> Optional[Decimal]:
    """Respaldo agresivo: adivina un precio en USD desde un número "desnudo" del OCR.

    Cuando la red neuronal lee el número (p. ej. '250') pero NO el símbolo de moneda
    (un '$' chico/estilizado que no transcribe), `_extract_price` no encuentra nada.
    Aquí buscamos un número de aspecto de precio, descartando teléfonos, porcentajes,
    dimensiones ('120x60'), años y cantidades ('12 meses').

    Usa el TEXTO INDICATIVO contiguo: prefiere el número que sigue a una palabra de
    precio actual ("AHORA 250", "POR SOLO 80", "PRECIO120") y descarta el que sigue a
    una de precio viejo ("ANTES 280"). El tope depende del contexto: un número CON
    indicador de precio actual se confía hasta `_INDICATOR_PRICE_MAX` (y el gate por
    categoría lo afina); un número SIN indicador —más arriesgado— se corta en
    `OCR_BARE_NUMBER_MAX_USD` (500 por defecto). Solo se invoca si
    `OCR_ASSUME_USD_FOR_BARE_NUMBER` está activo. Devuelve el precio o None.
    """
    if not text:
        return None
    bare_cap = Decimal(str(image_ocr.OCR_BARE_NUMBER_MAX_USD))
    cleaned = _PHONE_LIKE_RE.sub(" ", text)
    current: list[Decimal] = []   # números precedidos por indicador de precio ACTUAL
    neutral: list[Decimal] = []   # números sin indicador claro
    for m in re.finditer(r"\d[\d.,]*", cleaned):
        start, end = m.start(), m.end()
        before = cleaned[max(0, start - 1):start]
        after = cleaned[end:end + 8]
        # Dimensiones / multiplicadores tipo '2x1', '120x60'.
        if before.lower() == "x" or after[:1].lower() == "x":
            continue
        # Porcentajes, unidades, cantidades pegadas al número.
        if after.startswith("%") or _NON_PRICE_UNIT_RE.match(after):
            continue
        value = _parse_ocr_number(m.group(0))
        if value is None or value < _BARE_PRICE_MIN:
            continue
        # Años tipo 1999/2026: no son precios.
        if value == value.to_integral_value() and Decimal("1900") <= value <= Decimal("2099"):
            continue
        # Clasifica por el texto indicativo inmediatamente anterior al número, y aplica
        # el techo que corresponde a cada caso.
        context = _norm_indicator_text(cleaned[max(0, start - 24):start]).rstrip(" \t\r\n:.-–—=>|")
        if _ends_with_indicator(context, _PREVIOUS_PRICE_INDICATORS):
            continue  # es el precio viejo (p. ej. "ANTES 280"); no lo tomamos
        if _ends_with_indicator(context, _CURRENT_PRICE_INDICATORS):
            if value <= _INDICATOR_PRICE_MAX:  # se confía; lo fino lo hace la validación
                current.append(value)
        elif value <= bare_cap:  # sin indicador: tope estricto de seguridad
            neutral.append(value)
    if current:
        # Hay número(s) marcados como precio actual: el mayor de ellos.
        return max(current)
    if neutral:
        # Sin indicadores: el número más prominente (mayor) dentro del tope.
        return max(neutral)
    return None


def _extract_price_from_ocr(text: str) -> tuple[Optional[Decimal], Optional[str]]:
    """Extrae (precio, moneda) del texto del OCR, tolerante a las fallas del OCR.

    Primero usa el extractor estándar (que exige el símbolo/código de moneda junto al
    número). Si no halla nada y `OCR_ASSUME_USD_FOR_BARE_NUMBER` está activo, cae al
    respaldo de número desnudo asumiendo USD.
    """
    price, currency = _extract_price(text)
    if price is not None:
        return price, currency
    if image_ocr.OCR_ASSUME_USD_FOR_BARE_NUMBER:
        guess = _guess_bare_price_usd(text)
        if guess is not None:
            return guess, "USD"
    return None, None


def _ocr_fallback_prices(pairs: list[tuple[CompetitorMarketData, dict]]) -> None:
    """Último recurso: recupera el precio desde la IMAGEN del post vía OCR (red neuronal).

    Solo actúa sobre los posts que siguen SIN precio tras el caption (regex) y el LLM,
    porque en Instagram el precio suele estar quemado en el flyer y no en el texto.
    No-op silencioso si el OCR está desactivado o `easyocr` no está instalado. Cada
    imagen está aislada: si una falla, el resto se procesa igual.
    """
    if not image_ocr.is_enabled():
        logger.info(
            "OCR de imágenes DESACTIVADO (USE_VISION_PRICE_OCR=%s); no se intenta leer "
            "precios desde las imágenes de Instagram. Si esperabas que corriera, revisa "
            "el .env y REINICIA el servidor.",
            image_ocr.USE_VISION_PRICE_OCR,
        )
        return

    # Solo los posts que NINGUNA fuente de texto (caption + LLM) logró cotizar.
    pending = [(instance, post) for instance, post in pairs if instance.price is None]
    if not pending:
        logger.info(
            "OCR de imágenes: los %d post(s) ya tienen precio del texto; nada que leer.",
            len(pairs),
        )
        return

    logger.info(
        "OCR de imágenes ACTIVO (EasyOCR / red neuronal): intentando recuperar el precio "
        "de %d post(s) sin precio en el texto…",
        len(pending),
    )

    no_images = 0   # posts sin ninguna URL de imagen para leer
    processed = 0   # posts con al menos una imagen leída por la red neuronal
    recovered = 0   # posts cuyo precio recuperó la red neuronal desde la imagen
    for instance, post in pending:
        image_urls = _post_image_urls(post)
        if not image_urls:
            no_images += 1
            logger.info("OCR: el post %s no trae imágenes para leer.", post.get("url") or "(sin url)")
            continue
        text = image_ocr.extract_text_from_images(image_urls)
        if not text:
            continue
        processed += 1
        price, currency = _extract_price_from_ocr(text)
        if price is not None and price > 0:
            instance.price = price
            instance.currency = currency or instance.currency or "USD"
            recovered += 1
            logger.info(
                "OCR recuperó precio %s %s desde la imagen del post %s",
                price,
                instance.currency,
                post.get("url") or "(sin url)",
            )

    logger.info(
        "OCR de imágenes finalizado: la red neuronal recuperó %d precio(s) de %d post(s) "
        "con imagen leída (%d post(s) sin imagen; %d seguían sin precio tras caption + LLM).",
        recovered,
        processed,
        no_images,
        len(pending),
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


def finalize_instagram(dataset_id: str, scrape_run=None) -> list[CompetitorMarketData]:
    """Lee el dataset de un run finalizado, mapea cada post y guarda los registros.

    El mapeo de campos es determinista; el producto, la categoría y la
    identificación del competidor se afinan de forma opcional vía LLM (DeepSeek).
    Como último recurso para el precio, si ni el caption ni el LLM lo encontraron,
    se intenta leerlo de la imagen del post con OCR (red neuronal EasyOCR, opcional).
    El guardado (snapshot USD, match al catálogo, validación, archivo de descartes
    y enlace al run) lo centraliza `persist_records`.
    """
    scrape_run = ensure_scrape_run(
        scrape_run, CompetitorMarketData.SourceChoices.INSTAGRAM, dataset_id
    )
    client = get_client()
    items = list(client.dataset(dataset_id).iterate_items())
    logger.info("Se obtuvieron %d posts del dataset de Apify.", len(items))

    pairs = [(_map_post_to_instance(item), item) for item in items]
    _enrich_posts(pairs)
    # Último recurso para el precio: leerlo de la imagen del post (red neuronal OCR),
    # solo para los posts que el caption y el LLM dejaron sin precio.
    _ocr_fallback_prices(pairs)

    instances = [instance for instance, _ in pairs]
    return persist_records(instances, scrape_run=scrape_run, llm_used=deepseek.is_enabled())


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

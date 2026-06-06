"""Identificación de competidores vía DeepSeek (LLM), 100% opcional.

DeepSeek expone una API compatible con OpenAI, así que reutilizamos el SDK
`openai` apuntándolo a su `base_url`. El módulo degrada de forma segura ante
cualquiera de estas condiciones, dejando el pipeline determinista intacto:

    * `USE_LLM_ENRICHMENT` apagado (default).
    * `DEEPSEEK_API_KEY` ausente.
    * El paquete `openai` no está instalado.
    * La API falla, agota el timeout o devuelve un JSON inesperado.

Variables de entorno (en el `.env` del backend):

    USE_LLM_ENRICHMENT=True            # interruptor general (default False)
    DEEPSEEK_API_KEY=sk-...            # clave de https://platform.deepseek.com
    DEEPSEEK_MODEL=deepseek-chat       # opcional (default deepseek-chat)
    DEEPSEEK_BASE_URL=https://api.deepseek.com   # opcional
"""

import json
import logging
import os
import re

logger = logging.getLogger(__name__)


DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
USE_LLM_ENRICHMENT = os.environ.get("USE_LLM_ENRICHMENT", "False").lower() in ("1", "true", "yes")

_REQUEST_TIMEOUT = 20  # segundos por llamada
_MAX_KNOWN_COMPETITORS = 100  # tope de competidores enviados en el prompt
_MAX_DESCRIPTION_CHARS = 1200  # recorta descripciones largas para no inflar tokens

_EMPTY_RESULT = {
    "competitor_name": None,
    "matched_competitor_id": None,
    "confidence": 0.0,
    "promotions": None,
    "product_name": None,
    "state": None,
    "municipality": None,
}

_SYSTEM_PROMPT = (
    "Eres un asistente que analiza anuncios de muebles de oficina publicados en "
    "Facebook Marketplace en Venezuela. Solo nos interesan PRODUCTOS DE MOBILIARIO "
    "de oficina/hogar. Tienes TRES tareas: (1) identificar a la EMPRESA VENDEDORA "
    "(competidor), (2) extraer las promociones y beneficios adicionales que ofrezca "
    "el anuncio y (3) devolver el nombre de un producto de mobiliario concreto, "
    "limpio (sin el precio ni emojis), o null si el anuncio no nombra uno. "
    "Respondes únicamente con un objeto JSON. Regla crítica: NO inventes datos; si "
    "algo no está explícito en el texto, usa null. Muchos anuncios son de "
    "particulares sin marca: en esos casos competitor_name = null."
)


def is_enabled() -> bool:
    """True solo si el enriquecimiento está activado y hay API key configurada."""
    return USE_LLM_ENRICHMENT and bool(DEEPSEEK_API_KEY)


def _get_client():
    """Crea el cliente OpenAI apuntando a DeepSeek. Importa `openai` de forma diferida
    para que sea una dependencia opcional (no requerida si el LLM está apagado)."""
    from openai import OpenAI  # noqa: import diferido (dependencia opcional)

    return OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL,
        timeout=_REQUEST_TIMEOUT,
    )


def _build_user_prompt(title: str, description: str, location: str, known: list[dict]) -> str:
    """Arma el prompt del usuario. Incluye la palabra 'json' (requisito del modo JSON)."""
    known_block = "\n".join(f"- id={c['id']}: {c['name']}" for c in known) or "(ninguno)"
    description = (description or "")[:_MAX_DESCRIPTION_CHARS]
    return (
        "Competidores ya conocidos (haz match con uno de estos si corresponde):\n"
        f"{known_block}\n\n"
        "Anuncio a analizar:\n"
        f"- Título: {title or '(sin título)'}\n"
        f"- Descripción: {description or '(sin descripción)'}\n"
        f"- Ubicación: {location or '(desconocida)'}\n\n"
        "Devuelve un JSON con EXACTAMENTE estas claves:\n"
        '{"competitor_name": <string|null>, "matched_competitor_id": <int|null>, '
        '"confidence": <number 0..1>, "promotions": <string|null>, '
        '"product_name": <string|null>, '
        '"state": <string|null>, "municipality": <string|null>}\n'
        "- product_name: el nombre de un PRODUCTO DE MOBILIARIO de oficina/hogar "
        "concreto (p. ej. 'Silla ejecutiva', 'Escritorio en L', 'Archivador "
        "metálico'), en español, breve y limpio. NO incluyas el precio (de 'Silla "
        "de oficina20$' devuelve 'Silla de oficina'), ni emojis, ni hashtags, ni "
        "datos de contacto. Si el texto NO nombra un producto de mobiliario "
        "concreto —porque es un eslogan, una pregunta o un llamado a la acción "
        "(p. ej. 'Buscas ahorrar costos', 'Una imagen para tu oficina')— devuelve "
        "null.\n"
        "- Si coincide con un competidor conocido de la lista, pon su id en "
        "matched_competitor_id y su nombre exacto en competitor_name.\n"
        "- Si es un negocio nuevo que no está en la lista, pon matched_competitor_id=null "
        "y el nombre normalizado del negocio en competitor_name.\n"
        "- Si NO hay un nombre de negocio identificable, pon competitor_name=null, "
        "matched_competitor_id=null y confidence=0.\n"
        "- En 'promotions' resume en español, separados por comas, las promociones, "
        "descuentos y beneficios adicionales del anuncio (p. ej. 'envío gratis', "
        "'entrega a nivel nacional', 'delivery', 'garantía 1 año', 'instalación "
        "incluida', '20% de descuento'). Incluye solo lo que esté explícito en el "
        "texto. Si no hay ninguno, usa null. Máximo 200 caracteres.\n"
        "- state: el ESTADO de Venezuela donde se ubica el vendedor, con su nombre "
        "oficial completo (p. ej. 'Carabobo', 'Distrito Capital'). La ubicación puede "
        "venir como 'Ciudad, AB' con una abreviatura (p. ej. 'Naguanagua, CA' → "
        "Carabobo); dedúcela también del nombre de la ciudad si hace falta. Si no se "
        "puede determinar, null.\n"
        "- municipality: el municipio o ciudad del vendedor (p. ej. 'Naguanagua', "
        "'Valencia'). Si no se puede determinar, null."
    )


def _sanitize(data: dict) -> dict:
    """Valida y normaliza la respuesta del modelo a la forma esperada."""
    name = data.get("competitor_name")
    name = name.strip()[:150] or None if isinstance(name, str) else None

    matched_id = data.get("matched_competitor_id")
    if not isinstance(matched_id, int) or isinstance(matched_id, bool):
        matched_id = None

    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0

    promotions = data.get("promotions")
    promotions = promotions.strip()[:255] or None if isinstance(promotions, str) else None

    product_name = data.get("product_name")
    product_name = product_name.strip()[:255] or None if isinstance(product_name, str) else None

    state = data.get("state")
    state = state.strip()[:100] or None if isinstance(state, str) else None

    municipality = data.get("municipality")
    municipality = municipality.strip()[:100] or None if isinstance(municipality, str) else None

    return {
        "competitor_name": name,
        "matched_competitor_id": matched_id,
        "confidence": confidence,
        "promotions": promotions,
        "product_name": product_name,
        "state": state,
        "municipality": municipality,
    }


def enrich_listing(
    title: str,
    description: str,
    location: str,
    known_competitors: list[dict],
) -> dict:
    """Analiza un anuncio con DeepSeek: identifica al competidor y extrae promociones.

    Retorna ``{"competitor_name": str|None, "matched_competitor_id": int|None,
    "confidence": float, "promotions": str|None}``. Ante cualquier problema
    (deshabilitado, sin SDK, error de red, JSON inválido) retorna un resultado
    vacío sin lanzar excepción.
    """
    if not is_enabled():
        return dict(_EMPTY_RESULT)

    try:
        client = _get_client()
    except ImportError as exc:
        logger.warning(
            "No se pudo importar el SDK 'openai' (falta el paquete o una de sus "
            "dependencias, p. ej. un binario de pydantic_core que no coincide con la "
            "versión de Python): %s. Se omite el enriquecimiento LLM. Verifica la "
            "instalación con: pip install openai",
            exc,
        )
        return dict(_EMPTY_RESULT)
    except Exception as exc:  # configuración inválida, etc.
        logger.warning("No se pudo crear el cliente de DeepSeek: %s", exc)
        return dict(_EMPTY_RESULT)

    known = (known_competitors or [])[:_MAX_KNOWN_COMPETITORS]

    try:
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(title, description, location, known)},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=250,
            stream=False,
        )
        content = response.choices[0].message.content or "{}"
        return _sanitize(json.loads(content))
    except Exception as exc:
        logger.warning("Falló la identificación de competidor vía DeepSeek: %s", exc)
        return dict(_EMPTY_RESULT)


# ── Enriquecimiento de posts de Instagram ─────────────────────────────────────
#
# Instagram no está pensado para vender productos: el post es texto libre (caption)
# y casi todo (producto, precio, promociones) hay que inferirlo. Por eso el LLM
# hace MÁS trabajo aquí que en Facebook: además de identificar al competidor y las
# promociones, extrae un nombre de producto limpio, la categoría y —de respaldo—
# el precio si aparece explícito.

_IG_EMPTY_RESULT = {
    "competitor_name": None,
    "matched_competitor_id": None,
    "confidence": 0.0,
    "promotions": None,
    "product_name": None,
    "category": None,
    "price": None,
    "currency": None,
    "state": None,
    "municipality": None,
}

_IG_SYSTEM_PROMPT = (
    "Eres un asistente que analiza publicaciones de Instagram de tiendas de muebles "
    "de oficina y hogar en Venezuela. Las publicaciones son texto libre (caption) y "
    "suelen mezclar producto, precio, promociones, ubicación y datos de contacto, a "
    "menudo con emojis. Tu tarea es extraer datos estructurados del anuncio e "
    "identificar a la EMPRESA VENDEDORA (competidor). Solo nos interesan PRODUCTOS "
    "DE MOBILIARIO de oficina/hogar: si el caption no ofrece un producto de "
    "mobiliario concreto (es un eslogan, una frase motivacional o un llamado a la "
    "acción), product_name debe ser null. Respondes únicamente con un objeto JSON. "
    "Regla crítica: NO inventes datos; si algo no está explícito en el texto, usa "
    "null."
)


def _build_instagram_prompt(
    caption: str,
    owner_username: str,
    owner_full_name: str,
    hashtags: list[str],
    location: str,
    category_options: list[str],
    known: list[dict],
) -> str:
    """Arma el prompt para un post de Instagram. Incluye la palabra 'json' (modo JSON)."""
    known_block = "\n".join(f"- id={c['id']}: {c['name']}" for c in known) or "(ninguno)"
    caption = (caption or "")[:_MAX_DESCRIPTION_CHARS]
    hashtag_block = ", ".join(hashtags or []) or "(ninguno)"
    category_block = ", ".join(category_options) or "(ninguna)"
    return (
        "Competidores ya conocidos (haz match con uno de estos si corresponde):\n"
        f"{known_block}\n\n"
        "Publicación de Instagram a analizar:\n"
        f"- Usuario (handle): @{owner_username or '(desconocido)'}\n"
        f"- Nombre del perfil: {owner_full_name or '(sin nombre)'}\n"
        f"- Ubicación: {location or '(desconocida)'}\n"
        f"- Hashtags: {hashtag_block}\n"
        f"- Caption: {caption or '(sin texto)'}\n\n"
        "Devuelve un JSON con EXACTAMENTE estas claves:\n"
        '{"product_name": <string|null>, "category": <string|null>, '
        '"competitor_name": <string|null>, "matched_competitor_id": <int|null>, '
        '"confidence": <number 0..1>, "promotions": <string|null>, '
        '"price": <number|null>, "currency": <"USD"|"VES"|null>, '
        '"state": <string|null>, "municipality": <string|null>}\n'
        "- product_name: el nombre de un PRODUCTO DE MOBILIARIO de oficina/hogar "
        "concreto (p. ej. 'Silla ejecutiva', 'Escritorio en L', 'Archivador "
        "metálico'), en español, breve y limpio. NO incluyas el precio (de 'Silla "
        "de oficina20$' devuelve 'Silla de oficina'), ni emojis, ni hashtags. Si el "
        "caption NO nombra un producto de mobiliario concreto —porque es un eslogan, "
        "una frase motivacional, una pregunta o un llamado a la acción (p. ej. "
        "'Buscas ahorrar costos', 'Una imagen para tu oficina')— devuelve null.\n"
        f"- category: clasifícalo en UNA de estas categorías EXACTAS: {category_block}. "
        "Si ninguna aplica, usa null.\n"
        "- Competidor: el dueño del perfil suele ser la empresa vendedora. Si coincide "
        "con un competidor conocido de la lista, pon su id en matched_competitor_id y "
        "su nombre exacto en competitor_name. Si es un negocio que no está en la lista, "
        "pon matched_competitor_id=null y un nombre normalizado y legible del negocio "
        "en competitor_name (puedes basarte en el nombre del perfil o el handle).\n"
        "- promotions: resume en español, separadas por comas, las promociones, "
        "descuentos y beneficios explícitos (p. ej. 'envío nacional', 'delivery', "
        "'garantía', '20% de descuento'). Si no hay, usa null. Máximo 200 caracteres.\n"
        "- price/currency: SOLO si el precio aparece explícito en el texto. La moneda es "
        "'USD' para dólares ($, USD) o 'VES' para bolívares (Bs, VES). Si no hay precio "
        "explícito, price=null y currency=null. No inventes precios.\n"
        "- state: el ESTADO de Venezuela del vendedor, con su nombre oficial completo "
        "(p. ej. 'Carabobo', 'Distrito Capital'). Dedúcelo de la ubicación, del caption "
        "(p. ej. 'Valencia Estado Carabobo', 'San Diego edo Carabobo') o de la ciudad. "
        "Si no se puede determinar, null.\n"
        "- municipality: el municipio o ciudad del vendedor (p. ej. 'Valencia', "
        "'San Diego'). Si no se puede determinar, null."
    )


def _sanitize_instagram(data: dict, category_options: list[str]) -> dict:
    """Valida y normaliza la respuesta del modelo para un post de Instagram."""
    base = _sanitize(data)  # incluye competitor, promotions, product_name, ubicación

    category = data.get("category")
    if isinstance(category, str):
        category = category.strip()
        category = category if category in category_options else None
    else:
        category = None

    price = data.get("price")
    if isinstance(price, bool):
        price = None
    elif isinstance(price, (int, float)):
        price = float(price) if price > 0 else None
    elif isinstance(price, str):
        m = re.search(r"\d[\d.,]*", price)
        price = None
        if m:
            try:
                parsed = float(m.group(0).replace(",", ""))
                price = parsed if parsed > 0 else None
            except ValueError:
                price = None
    else:
        price = None

    currency = data.get("currency")
    currency = currency.strip().upper() if isinstance(currency, str) else None
    if currency not in ("USD", "VES"):
        currency = None

    return {
        **base,
        "category": category,
        "price": price,
        "currency": currency,
    }


def enrich_instagram_post(
    caption: str,
    owner_username: str,
    owner_full_name: str,
    hashtags: list[str],
    location: str,
    category_options: list[str],
    known_competitors: list[dict],
) -> dict:
    """Analiza un post de Instagram con DeepSeek: extrae producto, categoría,
    competidor, promociones y (de respaldo) precio.

    Retorna un dict con las claves de ``_IG_EMPTY_RESULT``. Ante cualquier problema
    (deshabilitado, sin SDK, error de red, JSON inválido) retorna un resultado vacío
    sin lanzar excepción, igual que :func:`enrich_listing`.
    """
    if not is_enabled():
        return dict(_IG_EMPTY_RESULT)

    try:
        client = _get_client()
    except ImportError as exc:
        logger.warning(
            "No se pudo importar el SDK 'openai' (falta el paquete o una de sus "
            "dependencias, p. ej. un binario de pydantic_core que no coincide con la "
            "versión de Python): %s. Se omite el enriquecimiento LLM. Verifica la "
            "instalación con: pip install openai",
            exc,
        )
        return dict(_IG_EMPTY_RESULT)
    except Exception as exc:
        logger.warning("No se pudo crear el cliente de DeepSeek: %s", exc)
        return dict(_IG_EMPTY_RESULT)

    known = (known_competitors or [])[:_MAX_KNOWN_COMPETITORS]

    try:
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": _IG_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _build_instagram_prompt(
                        caption, owner_username, owner_full_name, hashtags,
                        location, category_options, known,
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=350,
            stream=False,
        )
        content = response.choices[0].message.content or "{}"
        return _sanitize_instagram(json.loads(content), category_options)
    except Exception as exc:
        logger.warning("Falló el enriquecimiento de post de Instagram vía DeepSeek: %s", exc)
        return dict(_IG_EMPTY_RESULT)


# ── Match de productos contra el catálogo propio (opcional) ───────────────────
#
# Para las filas scrapeadas que el matcher determinista NO logró asociar a un
# producto propio, el LLM propone —en UN solo llamado por lote— cuál producto del
# catálogo es el equivalente (o ninguno). Mismo interruptor (`is_enabled`) y misma
# degradación segura que el resto del módulo.

_MAX_CATALOG = 200          # tope de productos del catálogo enviados en el prompt
_MAX_MATCH_ITEMS = 40       # tope de anuncios por llamada (el llamador trocea)

_PRODUCT_MATCH_SYSTEM_PROMPT = (
    "Eres un asistente que asocia nombres de productos de mobiliario scrapeados de "
    "anuncios con el CATÁLOGO PROPIO de una empresa de muebles de oficina en "
    "Venezuela. Para cada anuncio, decide cuál producto del catálogo es el MISMO "
    "producto o el equivalente más cercano, o ninguno. Respondes únicamente con un "
    "objeto JSON. Regla crítica: NO inventes; si ninguno corresponde con razonable "
    "certeza, usa product_id=null."
)


def _build_product_match_prompt(scraped: list[dict], catalog: list[dict]) -> str:
    """Arma el prompt de match de productos. Incluye la palabra 'json' (modo JSON)."""
    catalog_block = "\n".join(f"- id={c['id']}: {c['name']}" for c in catalog) or "(vacío)"
    items_block = "\n".join(
        f"- index={s['index']}: {s['name']}"
        + (f" (categoría: {s['category']})" if s.get("category") else "")
        for s in scraped
    )
    return (
        "Catálogo propio (productos a los que se puede asociar):\n"
        f"{catalog_block}\n\n"
        "Anuncios scrapeados a asociar:\n"
        f"{items_block}\n\n"
        'Devuelve un JSON con esta forma EXACTA: '
        '{"matches": [{"index": <int>, "product_id": <int|null>, "confidence": <number 0..1>}]}\n'
        "- Incluye UNA entrada por cada anuncio (usa su index).\n"
        "- product_id debe ser uno de los id del catálogo, o null si ninguno es el "
        "mismo producto ni un equivalente claro.\n"
        "- Asocia aunque haya diferencias de mayúsculas, acentos, plural o palabras "
        "de relleno (p. ej. 'Juego de comedor RETRO' ↔ 'Juego de comedor retro', "
        "'Silla Trendy' ↔ 'Silla de oficina Trendy'). Ante la duda real, usa null.\n"
        "- confidence: qué tan seguro estás del match (0 a 1)."
    )


def _sanitize_product_matches(data: dict, valid_ids: set) -> dict:
    """Normaliza la respuesta del modelo a ``{index: {"product_id", "confidence"}}``.

    Descarta entradas mal formadas y los product_id que no estén en el catálogo.
    """
    out: dict = {}
    matches = data.get("matches") if isinstance(data, dict) else None
    if not isinstance(matches, list):
        return out
    for m in matches:
        if not isinstance(m, dict):
            continue
        idx = m.get("index")
        pid = m.get("product_id")
        if isinstance(idx, bool) or not isinstance(idx, int):
            continue
        if isinstance(pid, bool) or not isinstance(pid, int) or pid not in valid_ids:
            continue
        try:
            conf = float(m.get("confidence"))
        except (TypeError, ValueError):
            conf = 0.0
        out[idx] = {"product_id": pid, "confidence": conf}
    return out


def match_products(scraped: list[dict], catalog: list[dict]) -> dict:
    """Asocia, vía LLM, nombres scrapeados a productos del catálogo (un solo llamado).

    ``scraped``: ``[{"index": int, "name": str, "category": str|None}]``.
    ``catalog``: ``[{"id": int, "name": str}]``.
    Retorna ``{index: {"product_id": int, "confidence": float}}`` solo para los
    matches que el modelo propuso. Ante cualquier problema (deshabilitado, sin SDK,
    error de red/JSON) retorna ``{}`` sin lanzar excepción.
    """
    if not is_enabled() or not scraped or not catalog:
        return {}

    try:
        client = _get_client()
    except ImportError as exc:
        logger.warning(
            "No se pudo importar el SDK 'openai' (falta el paquete o una dependencia, "
            "p. ej. el binario de pydantic_core): %s. Se omite el match de productos LLM.",
            exc,
        )
        return {}
    except Exception as exc:
        logger.warning("No se pudo crear el cliente de DeepSeek: %s", exc)
        return {}

    catalog = catalog[:_MAX_CATALOG]
    scraped = scraped[:_MAX_MATCH_ITEMS]
    valid_ids = {c["id"] for c in catalog}

    try:
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": _PRODUCT_MATCH_SYSTEM_PROMPT},
                {"role": "user", "content": _build_product_match_prompt(scraped, catalog)},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=1500,
            stream=False,
        )
        content = response.choices[0].message.content or "{}"
        return _sanitize_product_matches(json.loads(content), valid_ids)
    except Exception as exc:
        logger.warning("Falló el match de productos vía DeepSeek: %s", exc)
        return {}


# ── Diagnóstico de conexión (para el endpoint de prueba) ──────────────────────

# Anuncio de ejemplo estático, con la misma forma que produce el actor de Apify
# (claves `listingTitle`, `description`, `locationText`). Permite probar la
# conexión con DeepSeek sin tener que ejecutar el scraper real.
SAMPLE_LISTING = {
    "listingTitle": "Silla de oficina ergonómica con soporte lumbar",
    "description": (
        "Vendo sillas de oficina ergonómicas nuevas, marca MueblesPro Venezuela. "
        "Envío gratis a todo el país y 15% de descuento por inauguración. "
        "Entrega en 3 días hábiles. Garantía de 1 año."
    ),
    "locationText": "Caracas, Distrito Capital",
}


def get_config_status() -> dict:
    """Reporta la configuración actual del enriquecimiento LLM (sin exponer la clave)."""
    key = DEEPSEEK_API_KEY
    if not key:
        key_preview = "(vacía)"
    elif len(key) > 11:
        key_preview = f"{key[:6]}…{key[-4:]}"
    else:
        key_preview = "***"

    try:
        import openai

        openai_installed = True
        openai_version = getattr(openai, "__version__", "desconocida")
    except ImportError:
        openai_installed = False
        openai_version = None

    return {
        "use_llm_enrichment": USE_LLM_ENRICHMENT,
        "has_api_key": bool(key),
        "api_key_preview": key_preview,
        "model": DEEPSEEK_MODEL,
        "base_url": DEEPSEEK_BASE_URL,
        "request_timeout_seconds": _REQUEST_TIMEOUT,
        "openai_installed": openai_installed,
        "openai_version": openai_version,
        "is_enabled": is_enabled(),
    }


def check_connection(
    title: str | None = None,
    description: str | None = None,
    location: str | None = None,
    known_competitors: list[dict] | None = None,
) -> dict:
    """Prueba la conexión con DeepSeek con datos de ejemplo, EXPONIENDO el error.

    A diferencia de `enrich_listing` (que degrada en silencio para no romper el
    pipeline del scraper), esta función es para DIAGNÓSTICO: no silencia las
    excepciones, sino que devuelve el tipo y el mensaje del error para poder
    inspeccionarlo (p. ej. desde Postman). Sirve para confirmar la conexión —y
    ver el error esperado de saldo/clave— antes de depender del LLM en el scraper.

    Retorna un dict con: ``ok`` (bool), ``config`` (estado de configuración),
    ``request`` (texto enviado), ``result`` (salida normalizada si hubo),
    ``raw_content`` (JSON crudo del modelo), ``usage`` (tokens) y ``error``.
    """
    diagnostic: dict = {
        "ok": False,
        "config": get_config_status(),
        "request": None,
        "result": None,
        "raw_content": None,
        "usage": None,
        "error": None,
    }

    # Validaciones de configuración (no consumen tokens ni hacen red).
    if not USE_LLM_ENRICHMENT:
        diagnostic["error"] = {
            "stage": "config",
            "type": "EnriquecimientoDeshabilitado",
            "message": (
                "USE_LLM_ENRICHMENT no está activo. Ponlo en 'True' en el .env y "
                "REINICIA el servidor (un cambio en .env no recarga el proceso)."
            ),
        }
        return diagnostic
    if not DEEPSEEK_API_KEY:
        diagnostic["error"] = {
            "stage": "config",
            "type": "ClaveAusente",
            "message": "DEEPSEEK_API_KEY está vacío. Agrégalo al .env y reinicia el servidor.",
        }
        return diagnostic

    title = SAMPLE_LISTING["listingTitle"] if title is None else title
    description = SAMPLE_LISTING["description"] if description is None else description
    location = SAMPLE_LISTING["locationText"] if location is None else location
    known = known_competitors or []
    diagnostic["request"] = {"title": title, "description": description, "location": location}

    try:
        client = _get_client()
    except ImportError as exc:
        diagnostic["error"] = {
            "stage": "client",
            "type": type(exc).__name__,
            "message": (
                f"No se pudo importar el SDK 'openai' (falta el paquete o una de sus "
                f"dependencias, p. ej. un binario de pydantic_core que no coincide con "
                f"la versión de Python): {exc}"
            ),
        }
        return diagnostic
    except Exception as exc:
        diagnostic["error"] = {"stage": "client", "type": type(exc).__name__, "message": str(exc)}
        return diagnostic

    try:
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(title, description, location, known)},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=250,
            stream=False,
        )
        content = response.choices[0].message.content or "{}"
        diagnostic["raw_content"] = content
        diagnostic["result"] = _sanitize(json.loads(content))
        usage = getattr(response, "usage", None)
        if usage is not None:
            diagnostic["usage"] = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
        diagnostic["ok"] = True
        logger.info("Prueba de conexión a DeepSeek EXITOSA (modelo=%s).", DEEPSEEK_MODEL)
    except Exception as exc:
        error = {"stage": "api_call", "type": type(exc).__name__, "message": str(exc)}
        # Los errores del SDK de OpenAI suelen traer el código HTTP y el cuerpo.
        status_code = getattr(exc, "status_code", None)
        if status_code is not None:
            error["status_code"] = status_code
        body = getattr(exc, "body", None)
        if body is not None:
            error["body"] = body
        diagnostic["error"] = error
        logger.warning("Prueba de conexión a DeepSeek FALLÓ: %s: %s", type(exc).__name__, exc)

    return diagnostic

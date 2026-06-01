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
}

_SYSTEM_PROMPT = (
    "Eres un asistente que analiza anuncios de muebles de oficina publicados en "
    "Facebook Marketplace en Venezuela. Tienes DOS tareas: (1) identificar a la "
    "EMPRESA VENDEDORA (competidor) y (2) extraer las promociones y beneficios "
    "adicionales que ofrezca el anuncio. Respondes únicamente con un objeto JSON. "
    "Regla crítica: NO inventes datos; si algo no está explícito en el texto, usa "
    "null. Muchos anuncios son de particulares sin marca: en esos casos "
    "competitor_name = null."
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
        '"confidence": <number 0..1>, "promotions": <string|null>}\n'
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
        "texto. Si no hay ninguno, usa null. Máximo 200 caracteres."
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

    return {
        "competitor_name": name,
        "matched_competitor_id": matched_id,
        "confidence": confidence,
        "promotions": promotions,
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
    except ImportError:
        logger.warning(
            "El paquete 'openai' no está instalado; se omite el enriquecimiento LLM. "
            "Instálalo con: pip install openai"
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
    except ImportError:
        diagnostic["error"] = {
            "stage": "client",
            "type": "ImportError",
            "message": "El paquete 'openai' no está instalado. Instálalo con: pip install openai",
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

"""Integración con Apify para los scrapers de datos de competidores.

Helpers compartidos por los tres scrapers (Instagram, Facebook, Web) y por las
vistas REST: creación del cliente de Apify y consulta del progreso de un run.
"""

import logging
import os
import re
import unicodedata
from typing import Optional
from urllib.parse import urlparse

from apify_client import ApifyClient

logger = logging.getLogger(__name__)

APIFY_API_KEY = os.environ.get("APIFY_API_KEY", "")

# Estados terminales de un run de Apify (el polling se detiene al alcanzarlos).
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT", "TIMED_OUT"}


def get_client() -> ApifyClient:
    """Crea un ApifyClient validando que la API key esté configurada."""
    if not APIFY_API_KEY or APIFY_API_KEY == "your_apify_api_key_here":
        raise ValueError(
            "APIFY_API_KEY no está configurado. Reemplaza el placeholder en el archivo .env."
        )
    return ApifyClient(APIFY_API_KEY)


# Vocabulario controlado del dominio (muebles de oficina). Compartido por los
# scrapers para derivar una categoría legible desde el texto del anuncio. El orden
# importa: gana la primera categoría que coincida.
CATEGORY_KEYWORDS = {
    "Sillas": ["silla", "sillas", "butaca", "taburete", "banqueta", "sillón", "sillon", "chair"],
    "Escritorios": ["escritorio", "escritorios", "desk"],
    "Mesas": ["mesa", "mesas", "table"],
    "Archivadores": ["archivador", "archivadores", "archivo", "gaveta", "gavetero", "filing"],
    "Estantes y Libreros": ["estante", "estantería", "estanteria", "repisa", "librero", "shelf", "bookcase"],
    "Sofás y Recepción": ["sofá", "sofa", "poltrona", "couch", "recepción", "recepcion"],
    "Gabinetes y Armarios": ["gabinete", "gabinetes", "armario", "closet", "cabinet", "credenza", "locker"],
}

# Lista de nombres de categoría (p. ej. para ofrecérsela como opciones al LLM).
CATEGORY_NAMES = list(CATEGORY_KEYWORDS.keys())


def classify_category(text: str) -> str | None:
    """Clasifica un anuncio en una categoría de mobiliario por palabras clave."""
    text = (text or "").lower()
    if not text:
        return None
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return category
    return None


# ── Extracción determinista de texto (compartida por los tres scrapers) ───────

# Patrones de tiempo de entrega (días). Compartidos para que las tres fuentes
# (Instagram, Facebook, Web) lo extraigan igual.
_LEAD_TIME_PATTERNS = (
    r"(\d+)\s*días?\s*(?:de\s+)?(?:entrega|despacho|envío)",
    r"(\d+)\s*days?\s*(?:delivery|shipping)",
    r"entrega\s+en\s+(\d+)\s*días?",
    r"delivery\s+in\s+(\d+)\s*days?",
)

# Palabras clave promocionales (superset de las de Instagram + Facebook).
_PROMO_KEYWORDS = (
    "oferta", "descuento", "promoción", "promocion", "promo", "rebaja", "sale",
    "envío gratis", "envio gratis", "delivery gratis", "free shipping",
    "% de descuento", "% off", "liquidación", "liquidacion", "outlet",
    "precio especial", "2x1", "cuotas", "meses sin intereses", "garantía", "garantia",
)

# Señales de "agotado" / sin disponibilidad.
_OUT_OF_STOCK_KEYWORDS = (
    "agotado", "agotada", "sin stock", "sin existencia", "no disponible",
    "out of stock", "sold out",
)


def extract_lead_time(text: str) -> Optional[int]:
    """Extrae el tiempo de entrega en días desde un texto libre. None si no aparece."""
    if not text:
        return None
    for pattern in _LEAD_TIME_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def extract_promotions(text: str) -> Optional[str]:
    """Detecta promociones/beneficios por palabras clave. None si no hay ninguna."""
    lowered = (text or "").lower()
    if not lowered:
        return None
    found = [kw.title() for kw in _PROMO_KEYWORDS if kw in lowered]
    if found:
        return ", ".join(dict.fromkeys(found))[:255]
    return None


def detect_in_stock(text: str) -> bool:
    """False si el texto contiene señales de producto agotado; True en caso contrario."""
    lowered = (text or "").lower()
    return not any(kw in lowered for kw in _OUT_OF_STOCK_KEYWORDS)


# ── Identidad del sitio web (nombre legible a partir del dominio) ──────────────
#
# Para la fuente Web el "competidor" es el propio sitio. Derivamos un nombre
# legible del dominio: los sitios conocidos usan un nombre curado (p. ej.
# 'mercadolibre.com.ve' → 'Mercado Libre') y el resto cae a una heurística
# (separadores/camelCase + Title Case). Así un marketplace se muestra con su
# nombre comercial en vez del dominio crudo.

# Tokens que forman TLDs / dominios de segundo nivel; se quitan para hallar la
# etiqueta de marca del dominio (p. ej. 'mercadolibre' en 'mercadolibre.com.ve').
_TLD_TOKENS = {
    "com", "net", "org", "co", "ve", "gob", "gov", "edu", "mil", "info", "biz",
    "store", "shop", "online", "app", "io", "me", "tv", "ai", "us", "es",
}

# Sitios conocidos → nombre comercial legible (clave = etiqueta de marca en minúscula).
KNOWN_SITE_NAMES = {
    "mercadolibre": "Mercado Libre",
    "amazon": "Amazon",
    "aliexpress": "AliExpress",
    "shein": "SHEIN",
    "temu": "Temu",
    "falabella": "Falabella",
    "tiendamia": "Tiendamia",
    "alibaba": "Alibaba",
}

# Subconjunto de sitios que son MARKETPLACES (muchos vendedores). En ellos la
# ubicación es por vendedor, no del sitio, así que no se rellena en el competidor.
MARKETPLACE_LABELS = {
    "mercadolibre", "amazon", "aliexpress", "shein", "temu",
    "falabella", "tiendamia", "alibaba",
}


def _registrable_label(url: str) -> str:
    """Etiqueta de marca del dominio (sin TLD). Ej.: 'www.mercadolibre.com.ve' → 'mercadolibre'."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    if not host:
        # Quizá venga un dominio sin esquema (p. ej. 'mercadolibre.com.ve/...').
        host = (url or "").strip().lower().split("/")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in host.split(".") if p]
    if not parts:
        return ""
    # Quita los tokens de TLD del final, conservando al menos una etiqueta.
    while len(parts) > 1 and parts[-1] in _TLD_TOKENS:
        parts.pop()
    return parts[-1]


def prettify_site_name(url: str) -> str:
    """Nombre comercial legible del sitio a partir de su URL/dominio.

    Ej.: 'https://mercadolibre.com.ve/...' → 'Mercado Libre';
    'https://oficina-moderna.com' → 'Oficina Moderna'. Retorna '' si no hay dominio.
    """
    label = _registrable_label(url)
    if not label:
        return ""
    known = KNOWN_SITE_NAMES.get(label)
    if known:
        return known[:150]
    # Sitio desconocido: separa por guiones/guiones bajos y camelCase, y Title Case.
    name = re.sub(r"[-_]+", " ", label)
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return (name.title() if name else label)[:150]


def is_marketplace_url(url: str) -> bool:
    """True si la URL pertenece a un marketplace conocido (muchos vendedores)."""
    return _registrable_label(url) in MARKETPLACE_LABELS


# ── Ubicación: estado + municipio del competidor ──────────────────────────────

# Estados de Venezuela. Las claves están en minúsculas y SIN acentos (se comparan
# tras normalizar el texto); los valores son el nombre oficial.
VENEZUELA_STATES = {
    "amazonas": "Amazonas",
    "anzoategui": "Anzoátegui",
    "apure": "Apure",
    "aragua": "Aragua",
    "barinas": "Barinas",
    "bolivar": "Bolívar",
    "carabobo": "Carabobo",
    "cojedes": "Cojedes",
    "delta amacuro": "Delta Amacuro",
    "distrito capital": "Distrito Capital",
    "falcon": "Falcón",
    "guarico": "Guárico",
    "la guaira": "La Guaira",
    "lara": "Lara",
    "merida": "Mérida",
    "miranda": "Miranda",
    "monagas": "Monagas",
    "nueva esparta": "Nueva Esparta",
    "portuguesa": "Portuguesa",
    "sucre": "Sucre",
    "tachira": "Táchira",
    "trujillo": "Trujillo",
    "vargas": "La Guaira",  # Vargas fue renombrado a La Guaira
    "yaracuy": "Yaracuy",
    "zulia": "Zulia",
}

# Abreviaturas de 2 letras que usa Facebook Marketplace (p. ej. "Naguanagua, CA").
# Es "mejor esfuerzo": cuando el LLM está activo, su valor tiene prioridad sobre esto.
_STATE_ABBR = {
    "ca": "Carabobo", "ar": "Aragua", "mi": "Miranda", "zu": "Zulia",
    "la": "Lara", "an": "Anzoátegui", "bo": "Bolívar", "ta": "Táchira",
    "me": "Mérida", "fa": "Falcón", "su": "Sucre", "mo": "Monagas",
    "ne": "Nueva Esparta", "po": "Portuguesa", "gu": "Guárico", "ba": "Barinas",
    "tr": "Trujillo", "co": "Cojedes", "ya": "Yaracuy", "ap": "Apure",
    "am": "Amazonas", "dc": "Distrito Capital",
}


def _strip_accents(text: str) -> str:
    """Quita los acentos para comparar de forma robusta (Táchira == tachira)."""
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def normalize_state(value: str) -> str:
    """Normaliza un estado venezolano (nombre, variante o abreviatura) a su nombre
    oficial. Retorna '' si no se reconoce."""
    key = _strip_accents((value or "").strip().lower())
    if not key:
        return ""
    key = re.sub(r"^(estado|edo\.?)\s+", "", key).strip()  # quita prefijo "Estado/Edo"
    if key in VENEZUELA_STATES:
        return VENEZUELA_STATES[key]
    if key in _STATE_ABBR:
        return _STATE_ABBR[key]
    return ""


def parse_location(raw: str) -> tuple[str, str]:
    """Extrae (municipio, estado) de un texto de ubicación. Mejor esfuerzo determinista.

    Ej.: 'Naguanagua, CA' → ('Naguanagua', 'Carabobo');
    'Valencia Estado Carabobo' → ('Valencia', 'Carabobo').
    """
    raw = (raw or "").strip()
    if not raw:
        return "", ""

    # Formato "Municipio, Estado" o "Municipio, AB"
    if "," in raw:
        left, right = raw.split(",", 1)
        return left.strip()[:100], normalize_state(right)[:100]

    # Formato "Municipio Estado/Edo X"
    m = re.search(r"\b(estado|edo\.?)\b", raw, re.IGNORECASE)
    if m:
        return raw[: m.start()].strip()[:100], normalize_state(raw[m.start():])[:100]

    # Sin separadores claros: ¿el texto es el nombre de un estado?
    state = normalize_state(raw)
    if state:
        return "", state[:100]
    return raw[:100], ""  # asumimos que es el municipio


def resolve_location(
    llm_state: str | None,
    llm_municipality: str | None,
    raw_text: str,
) -> tuple[str, str]:
    """Combina la ubicación del LLM (prioritaria) con el parseo determinista del
    texto crudo (respaldo). Retorna (municipio, estado), normalizando el estado."""
    det_muni, det_state = parse_location(raw_text)
    # Estado: LLM normalizado → LLM crudo (si no se reconoció) → determinista.
    state = normalize_state(llm_state) or (llm_state or "").strip()[:100] or det_state
    # Municipio: LLM → determinista.
    municipality = (llm_municipality or "").strip()[:100] or det_muni
    return municipality, state


def backfill_competitor_location(comp, municipality: str, state: str) -> None:
    """Rellena estado/municipio del competidor SOLO si están vacíos (no pisa datos)."""
    fields = []
    if state and not comp.state:
        comp.state = state[:100]
        fields.append("state")
    if municipality and not comp.municipality:
        comp.municipality = municipality[:100]
        fields.append("municipality")
    if fields:
        comp.save(update_fields=fields)


def get_run_progress(run_id: str, dataset_id: str | None = None) -> dict:
    """Consulta el estado de un run de Apify y cuántos items lleva su dataset.

    Es de solo lectura, por lo que es seguro llamarla repetidamente desde el
    polling del frontend.
    """
    client = get_client()
    run = client.run(run_id).get() or {}
    run_status = run.get("status")

    items = 0
    resolved_dataset_id = dataset_id or run.get("defaultDatasetId")
    if resolved_dataset_id:
        dataset = client.dataset(resolved_dataset_id).get() or {}
        items = dataset.get("itemCount", 0) or 0

    return {
        "status": run_status,
        "items_scraped": items,
        "dataset_id": resolved_dataset_id,
        "is_terminal": run_status in TERMINAL_STATUSES,
        "succeeded": run_status == "SUCCEEDED",
    }

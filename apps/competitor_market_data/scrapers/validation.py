"""Validación de calidad de los datos scrapeados antes de persistirlos.

Filtra los registros cuyos datos NO reflejan la realidad del mercado de muebles
de oficina/hogar en Venezuela, para no contaminar el dataset que alimenta los
modelos de ML. Dos chequeos, ambos deterministas y reproducibles:

  1. Nombre de producto: debe existir, quedar limpio y nombrar un producto real.
     Quita precios embebidos ("Silla de oficina20$" → "Silla de oficina"), emojis
     y basura; y descarta los "nombres" que en realidad son eslóganes o llamados a
     la acción (p. ej. "Buscas ahorrar costos!!", "Una Imagen para tu Oficina!!").
  2. Precio plausible: el precio (convertido a USD) debe caer dentro del rango
     razonable para su categoría en la economía venezolana. Descarta tanto los
     precios absurdamente bajos (un escritorio a 1$) como los absurdamente altos
     (un escritorio a 1000$), que no son viables en el mercado local.

Los registros que fallan cualquiera de los chequeos se DESCARTAN.

Excepción de Instagram: como el precio rara vez es explícito en el caption, los
posts de Instagram SIN precio se conservan por defecto (toggle
`DISCARD_INSTAGRAM_WITHOUT_PRICE`). Si tienen precio, igual se valida el rango.

Los rangos de precio son globales: el mismo rango aplica a las cuatro fuentes (Instagram,
Facebook, Web, Mercado Libre).
"""

import logging
import re
import unicodedata
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)


# ── Rangos de precio por categoría (USD), calibrados al mercado venezolano ─────
#
# (mínimo, máximo) en dólares. Editá estos valores para ajustar la validación.
# Las claves deben coincidir EXACTAMENTE con las categorías de
# `scrapers.__init__.CATEGORY_KEYWORDS` / `CATEGORY_NAMES`.
#
# El TECHO depende de la categoría a propósito: así se puede descartar un
# escritorio a 1000$ (no viable) sin descartar un juego de recepción legítimo a
# 1100$. Un único rango global no podría distinguir ambos casos.
PRICE_BANDS: dict[str, tuple[Decimal, Decimal]] = {
    "Sillas":               (Decimal("10"), Decimal("500")),
    "Escritorios":          (Decimal("25"), Decimal("800")),
    "Mesas":                (Decimal("20"), Decimal("1000")),
    "Archivadores":         (Decimal("25"), Decimal("500")),
    "Estantes y Libreros":  (Decimal("15"), Decimal("500")),
    "Sofás y Recepción":    (Decimal("50"), Decimal("1200")),
    "Gabinetes y Armarios": (Decimal("30"), Decimal("800")),
}

# Rango de respaldo cuando la categoría no se pudo determinar. Amplio para no
# descartar productos válidos sin clasificar, pero acota lo claramente absurdo.
DEFAULT_BAND: tuple[Decimal, Decimal] = (Decimal("10"), Decimal("1500"))

# En Instagram el precio casi nunca está explícito en el caption y extraerlo es
# poco fiable, así que por defecto NO se descartan los posts que quedan SIN
# precio (en Facebook/Web el precio es estructurado, ahí un registro sin precio
# sí se descarta). Poné esto en True para descartarlos también en Instagram.
DISCARD_INSTAGRAM_WITHOUT_PRICE = False

# Tag de la fuente Instagram (== CompetitorMarketData.SourceChoices.INSTAGRAM).
_INSTAGRAM_SOURCE = "IG"


def band_for_category(category: Optional[str]) -> tuple[Decimal, Decimal]:
    """Rango (min, max) en USD para la categoría dada (o el de respaldo)."""
    return PRICE_BANDS.get(category or "", DEFAULT_BAND)


# ── Limpieza del nombre de producto ───────────────────────────────────────────

# Tokens tipo precio pegados al nombre ("Silla de oficina20$", "$20 Silla",
# "Mesa Bs. 200", "Escritorio 40USD"). Se anclan a un símbolo o código de moneda
# para NO borrar números legítimos como dimensiones ("Escritorio 1.20m").
_PRICE_TOKEN_RE = re.compile(
    r"""
    (?:
        [$]\s*\d[\d.,]*                  # $20, $ 1.200
      | \d[\d.,]*\s*[$]                  # 20$, 1.200 $
      | (?:bs|ves|usd)\.?\s*\d[\d.,]*    # Bs 200, USD40, VES 1000
      | \d[\d.,]*\s*(?:bs|ves|usd)\.?    # 200Bs, 40USD
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Emojis y pictogramas frecuentes en captions/títulos.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # símbolos y pictogramas (emojis modernos)
    "\U00002600-\U000027BF"   # misceláneos y dingbats
    "\U0001F1E6-\U0001F1FF"   # banderas regionales
    "\U00002B00-\U00002BFF"   # flechas y símbolos varios
    "\U0000FE0F"              # selector de variación de emoji
    "•"                  # viñeta •
    "]+",
    flags=re.UNICODE,
)

# Caracteres de relleno/puntuación que sobran en los bordes tras limpiar.
_EDGE_JUNK = " -–—|·,.;:*\t\n\r"


def clean_product_name(name: Optional[str]) -> Optional[str]:
    """Sanea un nombre de producto: quita precios embebidos, emojis y basura.

    Ej.: "Silla de oficina20$" → "Silla de oficina". Retorna ``None`` si tras la
    limpieza no queda un nombre utilizable (p. ej. el texto era solo un precio).
    """
    if not name:
        return None
    text = _PRICE_TOKEN_RE.sub(" ", name)
    text = _EMOJI_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(_EDGE_JUNK)
    return text[:255] or None


# ── Detección de "nombres" que en realidad son eslóganes, no productos ────────
#
# Los captions de Instagram a veces arrojan frases de marketing como
# "Buscas ahorrar costos!!" o "Una Imagen para tu Oficina!!" en lugar del nombre
# de un producto. Esta red de seguridad determinista las descarta y funciona
# incluso con el LLM apagado; con el LLM encendido, además el prompt pide devolver
# null cuando el caption no nombra un producto de mobiliario concreto.

# Signos de pregunta/exclamación: un nombre de producto real casi nunca los lleva.
_STATEMENT_PUNCT_RE = re.compile(r"[!?¡¿]")

# Palabras (sin acentos, en minúscula) con las que arrancan los eslóganes y
# llamados a la acción. Lista de ALTA PRECISIÓN: son verbos/preguntas que casi
# nunca inician el nombre de un producto, para no descartar nombres reales.
_NON_PRODUCT_STARTERS = {
    "buscas", "busca", "quieres", "quiere", "necesitas", "necesita",
    "aprovecha", "compra", "lleva", "llevate", "contactanos", "contactenos",
    "pregunta", "preguntanos", "descubre", "conoce", "ven", "veni", "visitanos",
    "ahorra", "transforma", "renueva", "renova", "equipa", "adquiere", "adquiri",
    "pide", "pedi", "escribenos", "llama", "solicita", "imagina", "haz", "hazte",
    "dale", "obten", "obtene", "anímate", "animate",
}


def _norm(text: str) -> str:
    """Minúsculas y sin acentos, para comparar de forma robusta."""
    text = unicodedata.normalize("NFD", text or "")
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text.lower().strip()


def looks_like_statement(name: Optional[str]) -> bool:
    """True si el "nombre" parece un eslogan/llamado a la acción, no un producto.

    Detecta frases de marketing que los captions sueltan en vez de nombrar un
    producto (p. ej. 'Buscas ahorrar costos!!', 'Una Imagen para tu Oficina!!').
    Conservador: alta precisión para no descartar nombres de producto reales
    (p. ej. 'Silla ejecutiva Stanford' no se marca).
    """
    if not name:
        return False
    if _STATEMENT_PUNCT_RE.search(name):
        return True
    first = _norm(name).split(" ", 1)[0].strip(".,;:")
    return first in _NON_PRODUCT_STARTERS


# ── Conversión de moneda (para validar el rango siempre en USD) ───────────────


def get_latest_usd_rate() -> Optional[Decimal]:
    """Tasa Bs/USD más reciente (paralela; cae a la BCV). ``None`` si no hay ninguna.

    Los precios de marketplace en bolívares siguen la tasa paralela/informal, por
    eso se prefiere `parallel_rate`. Import diferido para evitar dependencias de
    arranque y poder usar este módulo fuera del ciclo de request.
    """
    from apps.core.models import ExchangeRate

    rate = ExchangeRate.objects.order_by("-date").first()
    if rate is None:
        return None
    return rate.parallel_rate or rate.bcv_rate


def to_usd(
    price: Optional[Decimal],
    currency: Optional[str],
    usd_rate: Optional[Decimal],
) -> Optional[Decimal]:
    """Convierte un precio a USD. Los bolívares se dividen por la tasa Bs/USD.

    Retorna ``None`` si no hay precio, o si está en VES y no hay tasa de cambio
    disponible (en ese caso no se puede validar el rango de forma fiable).
    """
    if price is None:
        return None
    cur = (currency or "USD").upper()
    if cur == "VES":
        if usd_rate and usd_rate > 0:
            return price / usd_rate
        return None
    # USD (o moneda desconocida, que tratamos como USD).
    return price


# ── Validación de un registro ─────────────────────────────────────────────────


def validate_record(
    instance,
    usd_rate: Optional[Decimal],
) -> tuple[bool, str]:
    """Valida un ``CompetitorMarketData``. Retorna ``(es_valido, motivo)``.

    ``motivo`` queda vacío cuando el registro es válido; cuando no, explica por
    qué se descarta (para los logs). No persiste nada: solo decide.
    """
    # 1) Nombre de producto utilizable y que sea un producto, no un eslogan.
    if not instance.product_name or not instance.product_name.strip():
        return False, "sin nombre de producto"
    if looks_like_statement(instance.product_name):
        return False, "el nombre parece un eslogan, no un producto"

    # 2) Precio presente. En Instagram (y solo ahí) un post sin precio se conserva
    #    por defecto, porque el precio rara vez es explícito en el caption.
    if instance.price is None:
        is_instagram = instance.source == _INSTAGRAM_SOURCE
        if is_instagram and not DISCARD_INSTAGRAM_WITHOUT_PRICE:
            return True, ""
        return False, "sin precio"

    usd = to_usd(instance.price, instance.currency, usd_rate)
    if usd is None:
        # Precio en Bs sin tasa de cambio: no se puede validar el rango. Se
        # conserva el registro (no perdemos el dato por una tasa faltante), pero
        # se avisa para que se cargue una ExchangeRate.
        logger.warning(
            "No se pudo convertir a USD el precio %s %s (sin ExchangeRate cargada); "
            "se conserva el registro %r sin validar el rango de precio.",
            instance.price, instance.currency, instance.product_name,
        )
        return True, ""

    low, high = band_for_category(instance.category)
    cat = instance.category or "sin categoría"
    if usd < low:
        return False, f"precio {usd:.2f} USD por debajo del mínimo {low} para '{cat}'"
    if usd > high:
        return False, f"precio {usd:.2f} USD por encima del máximo {high} para '{cat}'"
    return True, ""


def partition_valid(instances: list) -> tuple[list, list]:
    """Separa los registros válidos de los descartados.

    Retorna ``(validos, descartados)`` donde ``descartados`` es una lista de
    ``(instance, motivo)``. Resuelve la tasa de cambio UNA sola vez para todo el
    lote y registra cada descarte (con su motivo) más un resumen final.
    """
    usd_rate = get_latest_usd_rate()
    valid: list = []
    discarded: list[tuple[object, str]] = []

    for inst in instances:
        ok, reason = validate_record(inst, usd_rate)
        if ok:
            valid.append(inst)
        else:
            discarded.append((inst, reason))
            logger.info(
                "Registro DESCARTADO [%s] producto=%r precio=%s %s — %s",
                inst.source, inst.product_name, inst.price, inst.currency, reason,
            )

    total = len(instances)
    if discarded:
        logger.info(
            "Validación: %d de %d registro(s) descartados por datos no plausibles; "
            "se guardarán %d.",
            len(discarded), total, len(valid),
        )
    else:
        logger.info("Validación: los %d registro(s) pasaron los chequeos de calidad.", total)
    return valid, discarded

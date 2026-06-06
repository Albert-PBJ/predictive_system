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

# Palabras (normalizadas) que delatan una ORACIÓN/eslogan en CUALQUIER posición, no
# solo al inicio: negaciones, pronombres personales y adverbios relativos/
# interrogativos. Casi nunca forman parte del nombre de un mueble, pero abundan en
# las frases de marketing que los captions sueltan como si fueran el producto
# (p. ej. 'Tú escoges DONDE vivirlo', 'El mejor trono NO está en el estadio'). Lista
# de ALTA PRECISIÓN: se excluyen palabras que sí aparecen en nombres reales — p. ej.
# 'te' (colisiona con 'té' de 'Mesa de té'), 'para', 'como'.
_STATEMENT_WORDS = {
    # negaciones
    "no", "nunca", "jamas", "tampoco",
    # pronombres personales / formas de tratamiento
    "tu", "vos", "usted", "ustedes", "yo", "nosotros",
    # adverbios relativos / interrogativos
    "donde", "cuando", "porque", "cual", "cuales", "quien", "quienes",
    "cuanto", "cuantos", "cuanta", "cuantas",
}

# Puntuación a recortar de los bordes de cada token al tokenizar el nombre.
_TOKEN_EDGE_PUNCT = ".,;:!?¡¿()[]{}\"'«»-—–*"


def _norm(text: str) -> str:
    """Minúsculas y sin acentos, para comparar de forma robusta."""
    text = unicodedata.normalize("NFD", text or "")
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text.lower().strip()


def looks_like_statement(name: Optional[str]) -> bool:
    """True si el "nombre" parece un eslogan/llamado a la acción, no un producto.

    Detecta frases de marketing que los captions sueltan en vez de nombrar un
    producto (p. ej. 'Buscas ahorrar costos!!', 'Una Imagen para tu Oficina!!',
    'Tú escoges donde vivirlo', 'El mejor trono no está en el estadio'). Tres señales,
    todas de alta precisión para no descartar nombres reales ('Silla ejecutiva
    Stanford', 'Mesa de té' no se marcan): puntuación de exclamación/pregunta, un
    verbo/llamado de marketing como PRIMERA palabra, o cualquier "palabra de oración"
    (negación, pronombre o adverbio relativo) en el texto.
    """
    if not name:
        return False
    if _STATEMENT_PUNCT_RE.search(name):
        return True
    tokens = [t.strip(_TOKEN_EDGE_PUNCT) for t in _norm(name).split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return False
    if tokens[0] in _NON_PRODUCT_STARTERS:
        return True
    return any(t in _STATEMENT_WORDS for t in tokens)


# ── ¿El nombre menciona realmente un mueble? (señal POSITIVA, robusta) ─────────
#
# Detectar eslóganes por una lista de "palabras malas" es una carrera perdida: las
# frases de marketing son infinitas. Aquí invertimos la lógica: el vocabulario de
# MUEBLES es finito y acotado (es una empresa de muebles). Si el nombre NO menciona
# ningún mueble, casi seguro es un eslogan/llamado a la acción, no un producto, y se
# descarta por completo (no se busca "otra línea"). Es deliberadamente estricto: el
# costo de descartar de más es bajo (Instagram trae mucho ruido) frente al de ensuciar
# el dataset de ML con frases que no son productos.
#
# Raíces (normalizadas, sin acento). Se comparan por PREFIJO de token para tolerar
# plurales/inflexiones: 'comedores'→'comedor', 'sillas'→'silla', 'gavetas'→'gaveta'.
_FURNITURE_TERMS = (
    # asientos
    "silla", "sillon", "butaca", "taburete", "banqueta", "banco", "poltrona",
    "puff", "mecedora", "reposet", "sofa", "futon", "divan",
    # mesas / superficies de trabajo
    "escritorio", "mesa", "mesit", "meson", "mostrador", "modulo", "modular",
    "recepcion", "pupitre",
    # almacenamiento
    "archivador", "archivo", "gaveta", "gavetero", "cajonera", "fichero",
    "estante", "estanteria", "repisa", "librero", "biblioteca", "anaquel",
    "vitrina", "exhibidor", "gabinete", "armario", "closet", "ropero",
    "alacena", "aparador", "trinchador", "credenza", "vajillero", "locker",
    "casillero", "vestier", "escaparate", "repostero", "zapatera", "perchero",
    "organizador", "rack",
    # hogar / dormitorio / sala
    "mueble", "recibidor", "comedor", "juego", "cama", "camarote", "litera",
    "somier", "colchon", "nochero", "peinadora", "tocador", "comoda",
)


def mentions_furniture(name: Optional[str]) -> bool:
    """True si el nombre menciona algún mueble reconocible (vocabulario `_FURNITURE_TERMS`).

    Señal POSITIVA: un nombre que sí nombra un mueble ('Silla ejecutiva',
    'Comedores HOLLAND', 'Mesa de té') es un producto; uno que no nombra ninguno
    ('Realiza tus pedidos por WhatsApp', 'El mejor trono no está en el estadio')
    casi seguro es un eslogan. Compara por prefijo de token (tolera plurales).
    """
    if not name:
        return False
    for token in _norm(name).split():
        token = token.strip(_TOKEN_EDGE_PUNCT)
        if token and any(token.startswith(term) for term in _FURNITURE_TERMS):
            return True
    return False


# ── Conversión de moneda (para validar el rango siempre en USD) ───────────────


def get_latest_rate():
    """`ExchangeRate` más reciente (su objeto completo) o ``None`` si no hay ninguna.

    Import diferido para evitar dependencias de arranque y poder usar este módulo
    fuera del ciclo de request.
    """
    from apps.core.models import ExchangeRate

    return ExchangeRate.objects.order_by("-date").first()


def get_latest_usd_rate() -> Optional[Decimal]:
    """Tasa Bs/USD más reciente (paralela; cae a la BCV). ``None`` si no hay ninguna.

    Los precios de marketplace en bolívares siguen la tasa paralela/informal, por
    eso se prefiere `parallel_rate`.
    """
    rate = get_latest_rate()
    if rate is None:
        return None
    return rate.parallel_rate or rate.bcv_rate


def stamp_price_usd(instance, usd_rate: Optional[Decimal], rate_date) -> None:
    """Fija `price_usd` (y, si hubo conversión, la tasa usada + su fecha) in-place.

    Snapshot reproducible: un precio en USD se copia tal cual; uno en VES se
    convierte con la tasa dada y se guarda la tasa + su fecha. Si el precio viene
    en VES y no hay tasa, `price_usd` queda en ``None`` (no se puede normalizar).
    """
    if instance.price is None:
        return
    cur = (instance.currency or "USD").upper()
    if cur == "VES":
        if usd_rate and usd_rate > 0:
            instance.price_usd = (instance.price / usd_rate).quantize(Decimal("0.01"))
            instance.exchange_rate_used = usd_rate
            instance.rate_date = rate_date
    else:
        # USD (o moneda desconocida tratada como USD): sin conversión.
        instance.price_usd = instance.price.quantize(Decimal("0.01"))


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
    # En Instagram el nombre se infiere de un caption libre y ruidoso. Exigimos que
    # mencione un mueble reconocible; si no, casi seguro es un eslogan/llamado a la
    # acción y se DESCARTA por completo (no se rescata otro texto del post). En las
    # demás fuentes el título es estructurado, así que no se aplica esta exigencia.
    if instance.source == _INSTAGRAM_SOURCE and not mentions_furniture(instance.product_name):
        return False, "el nombre no menciona ningún mueble (no parece un producto)"

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


def partition_valid(instances: list, usd_rate: Optional[Decimal] = None) -> tuple[list, list]:
    """Separa los registros válidos de los descartados.

    Retorna ``(validos, descartados)`` donde ``descartados`` es una lista de
    ``(instance, motivo)``. La tasa de cambio se resuelve UNA sola vez para todo el
    lote (o se recibe ya resuelta vía ``usd_rate`` para no consultarla dos veces) y
    se registra cada descarte (con su motivo) más un resumen final.
    """
    if usd_rate is None:
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

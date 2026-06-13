"""OCR de precios en imágenes vía EasyOCR (red neuronal), 100% opcional.

En Instagram el precio rara vez está en el caption: suele estar "quemado" dentro
de la imagen del post (flyer/promo). Cuando ni el caption (regex) ni el LLM logran
extraer un precio, este módulo lo intenta como ÚLTIMO recurso leyendo la imagen.

EasyOCR es un motor OCR basado en *deep learning* (PyTorch), no en heurísticas:
detección de texto con **CRAFT** (una CNN) + reconocimiento con una **CRNN**
(CNN + BiLSTM + decodificador CTC). Es decir, el precio se recupera con una RED
NEURONAL, no con plantillas.

El módulo degrada de forma segura (deja el pipeline determinista intacto) ante
cualquiera de estas condiciones:

    * `USE_VISION_PRICE_OCR` apagado (default).
    * El paquete `easyocr` (o su dependencia `torch`) no está instalado.
    * No se puede descargar la imagen, o el OCR falla.

Variables de entorno (en el `.env` del backend):

    USE_VISION_PRICE_OCR=True       # interruptor general (default False)
    OCR_LANGUAGES=es,en             # idiomas de EasyOCR (default es,en)
    OCR_USE_GPU=False               # usar GPU si hay CUDA disponible (default False)
    OCR_MAX_IMAGES_PER_POST=2       # cuántas imágenes intentar por post (default 2)
    OCR_MAG_RATIO=2.0               # factor de ampliación de la imagen (default 2.0)
    OCR_ASSUME_USD_FOR_BARE_NUMBER=False  # ver _guess_bare_price_usd (default False)
    OCR_BARE_NUMBER_MAX_USD=500     # tope del precio adivinado sin símbolo (default 500)
"""

import logging
import urllib.request

from apps.core import system_settings

logger = logging.getLogger(__name__)


# La configuración del OCR (interruptor + parámetros) la resuelve `system_settings`
# (la BD manda, sembrada del .env), así que se puede cambiar en caliente desde la UI.
# Se exponen como funciones (no constantes) para que el cambio surta efecto sin
# reiniciar. El singleton perezoso `_reader` se construye una vez con los idiomas/GPU
# vigentes en ese momento; cambiarlos aplica en el siguiente arranque del lector.

def use_vision_price_ocr() -> bool:
    return system_settings.vision_ocr_enabled()


def ocr_languages() -> str:
    return system_settings.ocr_languages()


def ocr_use_gpu() -> bool:
    return system_settings.ocr_use_gpu()


def ocr_max_images_per_post() -> int:
    return system_settings.ocr_max_images_per_post()


def ocr_mag_ratio() -> float:
    """Factor de ampliación de la imagen antes del OCR. Sube la detección de texto
    pequeño/estilizado (p. ej. un '$' chico junto a un número grande). 1.0 = sin ampliar."""
    return system_settings.ocr_mag_ratio()


def ocr_assume_usd_for_bare_number() -> bool:
    """Respaldo agresivo (OFF por defecto): si el OCR no transcribe el símbolo de
    moneda y solo queda un número "desnudo", asumir que es un precio en USD."""
    return system_settings.ocr_assume_usd_for_bare_number()


def ocr_bare_number_max_usd() -> float:
    """Tope de seguridad para un número desnudo SIN texto indicativo de precio. Un
    número CON indicador ("AHORA 750") no usa este tope: lo filtra el gate por categoría."""
    return system_settings.ocr_bare_number_max_usd()


_DOWNLOAD_TIMEOUT = 15  # segundos por descarga de imagen
_MAX_IMAGE_BYTES = 12 * 1024 * 1024  # ignora imágenes anómalas (> 12 MB)
# User-Agent de navegador: el CDN de Instagram puede rechazar peticiones sin él.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# El lector de EasyOCR es caro de construir (carga los modelos en memoria), así que
# se crea una sola vez (singleton perezoso) y se reutiliza en todo el run. Si la
# construcción falla, se marca para no reintentarla por cada post.
_reader = None
_reader_failed = False


def is_enabled() -> bool:
    """True solo si el OCR de imágenes está activado por configuración."""
    return use_vision_price_ocr()


def _get_reader():
    """Devuelve el lector de EasyOCR (singleton perezoso) o None si no se puede usar.

    Importa `easyocr` de forma diferida para que sea una dependencia OPCIONAL: el
    paquete (y su dependencia pesada `torch`) solo hace falta si el OCR está activo.
    """
    global _reader, _reader_failed
    if _reader is not None:
        return _reader
    if _reader_failed:
        return None

    try:
        import easyocr  # noqa: import diferido (dependencia opcional y pesada)
    except ImportError as exc:
        logger.warning(
            "No se pudo importar 'easyocr' (falta el paquete o su dependencia "
            "'torch'): %s. Se omite el OCR de imágenes. Instálalo con: "
            "pip install easyocr",
            exc,
        )
        _reader_failed = True
        return None

    try:
        languages = [lang.strip() for lang in ocr_languages().split(",") if lang.strip()] or [
            "es",
            "en",
        ]
        use_gpu = ocr_use_gpu()
        # La primera construcción descarga los modelos (~64 MB) y puede tardar.
        logger.info(
            "Inicializando EasyOCR (red neuronal) — idiomas=%s, gpu=%s. "
            "La primera vez descarga los modelos y puede tardar…",
            languages,
            use_gpu,
        )
        _reader = easyocr.Reader(languages, gpu=use_gpu)
        logger.info("EasyOCR listo.")
    except Exception as exc:  # descarga de modelos fallida, idiomas incompatibles, etc.
        logger.warning("No se pudo inicializar EasyOCR: %s. Se omite el OCR de imágenes.", exc)
        _reader_failed = True
        return None

    return _reader


def _download_image(url: str) -> bytes | None:
    """Descarga los bytes de una imagen con timeout. None ante cualquier problema."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT) as response:
            data = response.read(_MAX_IMAGE_BYTES + 1)
    except Exception as exc:
        logger.warning("No se pudo descargar la imagen %s: %s", url, exc)
        return None

    if not data:
        logger.warning("La descarga de la imagen devolvió 0 bytes: %s", url)
        return None
    if len(data) > _MAX_IMAGE_BYTES:
        logger.warning("Imagen demasiado grande (> %d bytes), se omite: %s", _MAX_IMAGE_BYTES, url)
        return None
    # Visibilidad: confirma que la imagen SÍ llegó (un tamaño diminuto suele indicar
    # un error/placeholder del CDN en vez del flyer real).
    logger.info("Imagen descargada para OCR: %d bytes — %s", len(data), url)
    return data


def _ocr_one(reader, url: str) -> str:
    """Lee el texto de UNA imagen con la red neuronal. '' ante cualquier problema."""
    data = _download_image(url)
    if data is None:
        return ""
    try:
        # detail=0 → solo las cadenas de texto reconocidas (sin bounding boxes).
        # mag_ratio amplía la imagen para captar texto pequeño (p. ej. el "$").
        fragments = reader.readtext(data, detail=0, mag_ratio=ocr_mag_ratio())
    except Exception as exc:
        logger.warning("Falló el OCR de la imagen %s: %s", url, exc)
        return ""
    text = " ".join(fragments) if fragments else ""
    # Log del texto crudo reconocido por la red: así se ve QUÉ leyó (y si el precio
    # estaba ahí o no) sin tener que adivinar. Se recorta para no inflar el log.
    preview = (text[:200] + "…") if len(text) > 200 else text
    logger.info("OCR leyó de %s: %r", url, preview or "(nada)")
    return text


def read_image(image_ref: str, *, detail: int = 1) -> list:
    """Lee una imagen (URL o ruta local) con EasyOCR. Pensada para DEPURACIÓN.

    A diferencia de `extract_text_from_images`, ignora el interruptor
    `USE_VISION_PRICE_OCR` (la invocas explícitamente desde el comando de prueba) y
    con ``detail=1`` devuelve ``[(bbox, texto, confianza), …]`` para inspeccionar la
    confianza de cada fragmento. Retorna ``[]`` si EasyOCR no está disponible o la
    imagen no se pudo leer.
    """
    reader = _get_reader()
    if reader is None:
        return []

    if image_ref.startswith(("http://", "https://")):
        source = _download_image(image_ref)
        if source is None:
            return []
    else:
        try:
            with open(image_ref, "rb") as handle:
                source = handle.read()
        except OSError as exc:
            logger.warning("No se pudo abrir la imagen local %s: %s", image_ref, exc)
            return []

    try:
        return reader.readtext(source, detail=detail, mag_ratio=ocr_mag_ratio())
    except Exception as exc:
        logger.warning("Falló el OCR de %s: %s", image_ref, exc)
        return []


def extract_text_from_images(image_urls: list[str], max_images: int | None = None) -> str:
    """Reconoce y concatena el texto de las imágenes de un post con EasyOCR (NN).

    Procesa hasta ``max_images`` (o ``OCR_MAX_IMAGES_PER_POST``) imágenes y devuelve
    todo el texto unido para que el llamador le aplique su regex de precio. Retorna
    '' si el OCR está apagado, no hay imágenes o el lector no se pudo inicializar.
    Cada imagen está aislada: si una falla, las demás se procesan igual.
    """
    if not is_enabled() or not image_urls:
        return ""

    reader = _get_reader()
    if reader is None:
        return ""

    limit = max_images if max_images is not None else ocr_max_images_per_post()
    texts: list[str] = []
    for url in image_urls[: max(limit, 0)]:
        text = _ocr_one(reader, url)
        if text:
            texts.append(text)
    return "\n".join(texts)

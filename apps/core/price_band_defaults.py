"""Valores por defecto de los rangos de precio para la validación de scrapers.

Fuente ÚNICA de los rangos por defecto (en USD), calibrados al mercado venezolano
de mobiliario. Se usan en tres puntos y deben coincidir entre sí, por eso viven
aquí, en un módulo sin dependencias de Django (evita ciclos de importación):

  * `core.models.SystemSettings.price_bands` los toma como ``default`` del campo
    (semilla de la fila singleton la primera vez).
  * `core.system_settings.price_bands()` cae a ellos si la BD no está disponible
    o la configuración guardada está vacía/corrupta.
  * `competitor_market_data.scrapers.validation.band_for_category()` los usa como
    último respaldo al resolver el rango de una categoría.

Estructura: ``{"categories": {<categoría>: {"min": n, "max": n}}, "default": {…}}``.
El rango ``default`` aplica a los registros sin categoría reconocida. Las claves de
``categories`` coinciden con ``scrapers.__init__.CATEGORY_NAMES``.

El TECHO depende de la categoría a propósito: así se descarta un escritorio a 1000$
(no viable) sin descartar un juego de recepción legítimo a 1100$.
"""

from __future__ import annotations

import copy

# (mín, máx) en USD por categoría + rango de respaldo para lo no clasificado.
DEFAULT_PRICE_BANDS: dict = {
    "categories": {
        "Sillas": {"min": 10, "max": 500},
        "Escritorios": {"min": 25, "max": 800},
        "Mesas": {"min": 20, "max": 1000},
        "Archivadores": {"min": 25, "max": 500},
        "Estantes y Libreros": {"min": 15, "max": 500},
        "Sofás y Recepción": {"min": 50, "max": 1200},
        "Gabinetes y Armarios": {"min": 30, "max": 800},
    },
    "default": {"min": 10, "max": 1500},
}


def default_price_bands() -> dict:
    """Copia profunda de los rangos por defecto (apta como ``default`` de un campo)."""
    return copy.deepcopy(DEFAULT_PRICE_BANDS)

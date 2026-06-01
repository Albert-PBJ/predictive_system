"""Integración con Apify para los scrapers de datos de competidores.

Helpers compartidos por los tres scrapers (Instagram, Facebook, Web) y por las
vistas REST: creación del cliente de Apify y consulta del progreso de un run.
"""

import logging
import os

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

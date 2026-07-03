"""Persistencia común de los registros scrapeados (los cuatro scrapers la usan).

Centraliza, justo antes de guardar, todo lo que hace falta para que el dataset sea
confiable y trazable:

  1. Snapshot de precio en USD (tasa + fecha) — `validation.stamp_price_usd`.
  2. Clave de anuncio estable entre runs (semántica de "observación", sin doble
     conteo) + match al catálogo propio — `compute_listing_key`, `matching`.
  3. Validación de calidad (descarta lo no plausible) y **archivo** de los
     descartes en `RejectedMarketData` (no se pierden: se pueden auditar).
  4. Procedencia: enlaza cada fila a su `ScrapeRun` y marca `enriched_by`.

Antes, cada `finalize_*` hacía `partition_valid()` + `bulk_create()` a mano; ahora
llaman a `persist_records()` y obtienen todo esto de forma uniforme.
"""

import hashlib
import logging
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .matching import apply_llm_product_matches, build_product_index, match_product
from .validation import get_latest_rate, partition_valid, stamp_price_usd

logger = logging.getLogger(__name__)


def compute_listing_key(instance) -> str:
    """Clave estable (sha1, 40 hex) que identifica el anuncio entre runs.

    Por URL cuando la hay (lo más estable); si no, por fuente+competidor+producto.
    El mismo anuncio scrapeado en distintos días comparte `listing_key`, así que el
    último `scraped_at` por clave es el snapshot vigente y los agregados pueden
    evitar el doble conteo.
    """
    source = instance.source or ""
    url = (instance.url or "").strip().lower()
    if url:
        basis = f"{source}|{url}"
    else:
        name = (instance.product_name or "").strip().lower()
        comp = (instance.competitor_name or "").strip().lower()
        basis = f"{source}|{comp}|{name}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def ensure_scrape_run(scrape_run, source: str, dataset_id: str = "", *, query=None, params=None):
    """Devuelve el `ScrapeRun` dado o crea uno mínimo (para la ruta CLI/bloqueante).

    Las vistas crean el run en `/start` (con los términos de búsqueda) y lo pasan a
    `finalize`; el CLI no, así que aquí se crea uno al vuelo para no perder la
    procedencia. Import diferido para evitar ciclos de importación.
    """
    from apps.benchmarking.models import ScrapeRun

    if scrape_run is not None:
        return scrape_run
    return ScrapeRun.objects.create(
        source=source,
        dataset_id=dataset_id or "",
        query=query or [],
        params=params or {},
        status=ScrapeRun.StatusChoices.RUNNING,
    )


def persist_batch(
    instances: list,
    *,
    scrape_run=None,
    llm_used: bool = False,
    processed_items: Optional[int] = None,
    total_items: Optional[int] = None,
) -> tuple[list, int]:
    """Enriquece, valida, guarda y archiva descartes de UN LOTE. **Checkpoint atómico.**

    A diferencia de `persist_records`, NO cierra el run: **acumula** sus conteos y
    (si se dan) fija el avance del procesamiento (`processed_items`/`total_items`).
    Todo el lote —filas guardadas, descartes archivados y avance— se escribe dentro
    de una única transacción, de modo que un corte a mitad de camino no deja un
    checkpoint parcial (no se re-procesa un lote ya contado).

    Retorna `(created, discarded_count)`.
    """
    from apps.benchmarking.models import CompetitorMarketData, RejectedMarketData, ScrapeRun

    if not instances:
        # Lote vacío: solo actualiza el avance/estado si corresponde.
        if scrape_run is not None and (processed_items is not None or total_items is not None):
            _checkpoint_progress(scrape_run, processed_items, total_items)
        return [], 0

    # Tasa de cambio una sola vez para todo el lote (snapshot reproducible).
    rate = get_latest_rate()
    usd_rate: Optional[Decimal] = (rate.parallel_rate or rate.bcv_rate) if rate else None
    rate_date = rate.date if rate else None

    product_index = build_product_index()
    enrichment = (
        CompetitorMarketData.EnrichmentChoices.LLM
        if llm_used
        else CompetitorMarketData.EnrichmentChoices.DETERMINISTIC
    )

    for inst in instances:
        stamp_price_usd(inst, usd_rate, rate_date)          # item 1: snapshot USD
        inst.listing_key = compute_listing_key(inst)        # item 2a: identidad de anuncio
        product, score = match_product(inst.product_name, inst.category, product_index)
        inst.product = product                              # item 2b: match al catálogo
        inst.product_match_score = score
        inst.enriched_by = enrichment                       # item 3: procedencia
        inst.scrape_run = scrape_run

    # Para las filas que el match determinista no asoció, intento opcional vía LLM
    # (mismo interruptor que el resto del enriquecimiento; off por defecto).
    llm_matched = apply_llm_product_matches(instances, product_index)
    if llm_matched:
        logger.info("Match de productos vía LLM: %d fila(s) asociadas.", llm_matched)

    valid, discarded = partition_valid(instances, usd_rate=usd_rate)

    with transaction.atomic():
        created = CompetitorMarketData.objects.bulk_create(valid)

        # Archiva los descartes (no se pierden: auditables) — item 3.
        if discarded:
            rejected = [
                RejectedMarketData(
                    scrape_run=scrape_run,
                    source=inst.source,
                    competitor_name=(inst.competitor_name or "")[:150],
                    product_name=(inst.product_name or "")[:255],
                    category=(inst.category or "")[:100],
                    price=inst.price,
                    currency=(inst.currency or "")[:3],
                    url=(inst.url or "")[:500],
                    rejection_reason=reason[:255],
                    raw_metadata=inst.raw_metadata,
                )
                for inst, reason in discarded
            ]
            RejectedMarketData.objects.bulk_create(rejected)

        # Acumula los conteos del run y fija el avance del checkpoint en el MISMO
        # commit que las filas, para que el checkpoint sea todo-o-nada.
        if scrape_run is not None:
            updates = {
                "records_collected": F("records_collected") + len(instances),
                "records_saved": F("records_saved") + len(created),
                "records_discarded": F("records_discarded") + len(discarded),
            }
            if processed_items is not None:
                updates["processed_items"] = processed_items
            if total_items is not None:
                updates["total_items"] = total_items
            # Al empezar a procesar, marca el run como PROCESANDO (no pisa un terminal).
            if scrape_run.status == ScrapeRun.StatusChoices.RUNNING:
                updates["status"] = ScrapeRun.StatusChoices.PROCESSING
            ScrapeRun.objects.filter(pk=scrape_run.pk).update(**updates)
            scrape_run.refresh_from_db()

    logger.info(
        "Persistencia (lote): %d guardados, %d descartados (de %d mapeados).",
        len(created), len(discarded), len(instances),
    )
    return created, len(discarded)


def _checkpoint_progress(scrape_run, processed_items, total_items) -> None:
    """Fija el avance del procesamiento sin tocar los conteos (lote vacío)."""
    from apps.benchmarking.models import ScrapeRun

    updates = {}
    if processed_items is not None:
        updates["processed_items"] = processed_items
    if total_items is not None:
        updates["total_items"] = total_items
    if scrape_run.status == ScrapeRun.StatusChoices.RUNNING:
        updates["status"] = ScrapeRun.StatusChoices.PROCESSING
    if updates:
        ScrapeRun.objects.filter(pk=scrape_run.pk).update(**updates)
        scrape_run.refresh_from_db()


def finalize_run(scrape_run, *, status=None, notes: str = "") -> None:
    """Cierra el run: fija su estado terminal y la hora de fin (si hay run)."""
    if scrape_run is None:
        return
    from apps.benchmarking.models import ScrapeRun

    scrape_run.status = status or ScrapeRun.StatusChoices.SUCCEEDED
    scrape_run.finished_at = timezone.now()
    fields = ["status", "finished_at"]
    if notes:
        scrape_run.notes = notes[:2000]
        fields.append("notes")
    scrape_run.save(update_fields=fields)


def persist_records(instances: list, *, scrape_run=None, llm_used: bool = False) -> list:
    """Enriquece, valida, guarda y archiva descartes de TODO de una vez (ruta CLI/bloqueante).

    Envuelve `persist_batch` (un solo lote) + `finalize_run(SUCCEEDED)`, conservando
    el contrato histórico que usan los comandos de management y `finalize_*`.
    """
    created, _ = persist_batch(
        instances,
        scrape_run=scrape_run,
        llm_used=llm_used,
        processed_items=len(instances),
        total_items=len(instances),
    )
    finalize_run(scrape_run, status=None)  # SUCCEEDED
    return created

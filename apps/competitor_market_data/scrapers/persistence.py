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


def persist_records(instances: list, *, scrape_run=None, llm_used: bool = False) -> list:
    """Enriquece, valida, guarda y archiva descartes. Retorna las filas creadas.

    `instances` son `CompetitorMarketData` ya mapeados (con su `raw_metadata`). Si
    se da `scrape_run`, se enlazan las filas y se actualizan sus conteos/estado.
    """
    from apps.benchmarking.models import CompetitorMarketData, RejectedMarketData

    if not instances:
        _finish_run(scrape_run, collected=0, saved=0, discarded=0)
        return []

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

    _finish_run(scrape_run, collected=len(instances), saved=len(created), discarded=len(discarded))
    logger.info(
        "Persistencia: %d guardados, %d descartados (de %d mapeados).",
        len(created), len(discarded), len(instances),
    )
    return created


def _finish_run(scrape_run, *, collected: int, saved: int, discarded: int) -> None:
    """Marca el run como completado y guarda sus conteos (si hay run)."""
    if scrape_run is None:
        return
    from apps.benchmarking.models import ScrapeRun

    scrape_run.records_collected = collected
    scrape_run.records_saved = saved
    scrape_run.records_discarded = discarded
    scrape_run.status = ScrapeRun.StatusChoices.SUCCEEDED
    scrape_run.finished_at = timezone.now()
    scrape_run.save(update_fields=[
        "records_collected", "records_saved", "records_discarded", "status", "finished_at",
    ])

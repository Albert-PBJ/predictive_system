"""Recalcula el match de productos propios sobre los datos de competidores ya guardados.

El match producto-propio ↔ anuncio-scrapeado se calcula al scrapear, pero queda
guardado en la fila: si después creas, renombras o activas un producto del catálogo,
las filas viejas siguen SIN asociar. Este comando vuelve a correr el match (mejorado,
y opcionalmente el LLM) sobre las filas existentes contra el catálogo ACTUAL.

Uso:
    python manage.py rematch_products                  # todas las filas, todas las fuentes
    python manage.py rematch_products --only-unmatched # solo las que aún no tienen producto
    python manage.py rematch_products --source IG      # solo Instagram
    python manage.py rematch_products --no-llm         # nunca usar el LLM (solo determinista)
    python manage.py rematch_products --limit 500      # tope de filas a procesar

No borra matches existentes: solo asocia los que falten o mejora el puntaje.
"""

from django.core.management.base import BaseCommand

from apps.benchmarking.models import CompetitorMarketData
from apps.competitor_market_data.enrichment import deepseek
from apps.competitor_market_data.scrapers.matching import (
    apply_llm_product_matches,
    build_product_index,
    match_product,
)

SOURCE_CHOICES = ["IG", "FB", "WEB", "ML", "OTH"]


class Command(BaseCommand):
    help = "Recalcula el match de productos propios sobre los CompetitorMarketData ya guardados."

    def add_arguments(self, parser):
        parser.add_argument("--source", choices=SOURCE_CHOICES, help="Limita a una fuente.")
        parser.add_argument(
            "--only-unmatched", action="store_true",
            help="Solo procesa filas sin producto asociado.",
        )
        parser.add_argument(
            "--no-llm", action="store_true",
            help="No usar el LLM aunque esté activo (solo match determinista).",
        )
        parser.add_argument("--limit", type=int, help="Máximo de filas a procesar.")

    def handle(self, *args, **options):
        index = build_product_index()
        if not index:
            self.stderr.write(self.style.WARNING(
                "No hay productos activos en el catálogo; no hay con qué asociar."
            ))
            return

        qs = CompetitorMarketData.objects.all().order_by("id")
        if options["source"]:
            qs = qs.filter(source=options["source"])
        if options["only_unmatched"]:
            qs = qs.filter(product__isnull=True)
        if options["limit"]:
            qs = qs[: options["limit"]]

        rows = list(qs)
        if not rows:
            self.stdout.write("No hay filas que procesar con esos filtros.")
            return

        det_changed = []   # match determinista nuevo o con puntaje distinto
        unmatched = []     # sin match determinista y sin match previo → candidatas a LLM
        for r in rows:
            product, score = match_product(r.product_name, r.category, index)
            if product is not None:
                if r.product_id != product.id or r.product_match_score != score:
                    r.product = product
                    r.product_match_score = score
                    det_changed.append(r)
            elif r.product_id is None:
                unmatched.append(r)

        use_llm = not options["no_llm"] and deepseek.is_enabled()
        llm_changed = apply_llm_product_matches(unmatched, index) if use_llm else 0

        changed = det_changed + [r for r in unmatched if r.product_id is not None]
        if changed:
            CompetitorMarketData.objects.bulk_update(changed, ["product", "product_match_score"])

        llm_note = f" + {llm_changed} por LLM" if use_llm else (
            " (LLM omitido)" if not options["no_llm"] else ""
        )
        self.stdout.write(self.style.SUCCESS(
            f"Procesadas {len(rows)} fila(s): {len(det_changed)} por match determinista"
            f"{llm_note}. Total actualizadas: {len(changed)}."
        ))

from django.core.management.base import BaseCommand, CommandError

from apps.competitor_market_data.scrapers.mercadolibre_scraper import scrape_mercadolibre


class Command(BaseCommand):
    help = (
        "Scrape Mercado Libre Venezuela listings via the Apify "
        "piotrv1001/mercado-libre-listings-scraper actor and store results in "
        "CompetitorMarketData. Searches by KEYWORDS, not URLs."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "queries",
            nargs="+",
            type=str,
            help='Término(s) de búsqueda, p. ej. "Sillas de oficina" "Escritorio en L"',
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            metavar="N",
            help="Máximo de listings a recolectar (default: 50)",
        )

    def handle(self, *args, **options):
        queries: list[str] = options["queries"]
        limit: int = options["limit"]

        self.stdout.write(
            f"Buscando en Mercado Libre Venezuela: {len(queries)} término(s) — "
            f"hasta {limit} listing(s)…"
        )

        try:
            records = scrape_mercadolibre(search_queries=queries, results_limit=limit)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Listo. {len(records)} registro(s) guardados en CompetitorMarketData."
            )
        )

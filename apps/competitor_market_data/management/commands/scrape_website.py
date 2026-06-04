from django.core.management.base import BaseCommand, CommandError

from apps.competitor_market_data.scrapers.website_scraper import scrape_website


class Command(BaseCommand):
    help = (
        "Scrape competitor websites and marketplaces (e.g. Mercado Libre) via the "
        "Apify AI web scraper and store results in CompetitorMarketData"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "urls",
            nargs="+",
            type=str,
            help="Website/marketplace URL(s) to scrape, e.g. https://www.mercadolibre.com.ve/...",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            metavar="N",
            help="Max pages to crawl (default: 50)",
        )
        parser.add_argument(
            "--competitor-name",
            type=str,
            default=None,
            metavar="NAME",
            help="Nombre del competidor (si se omite, se usa el nombre legible del sitio, p. ej. 'Mercado Libre')",
        )

    def handle(self, *args, **options):
        urls: list[str] = options["urls"]
        limit: int = options["limit"]
        competitor_name: str | None = options["competitor_name"]

        self.stdout.write(
            f"Scraping {len(urls)} URL(s) — hasta {limit} página(s)…"
        )

        try:
            records = scrape_website(
                urls=urls,
                results_limit=limit,
                competitor_name=competitor_name,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Listo. {len(records)} registro(s) guardados en CompetitorMarketData."
            )
        )

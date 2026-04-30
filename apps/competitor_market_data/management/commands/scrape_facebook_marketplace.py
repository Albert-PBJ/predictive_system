from django.core.management.base import BaseCommand, CommandError

from apps.competitor_market_data.scrapers.facebook_marketplace_scraper import scrape_facebook_marketplace


class Command(BaseCommand):
    help = "Scrape Facebook Marketplace listings via Apify and store results in CompetitorMarketData"

    def add_arguments(self, parser):
        parser.add_argument(
            "urls",
            nargs="+",
            type=str,
            help="Facebook Marketplace URL(s) to scrape, e.g. https://www.facebook.com/marketplace/category/furniture/",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            metavar="N",
            help="Max listings to fetch (default: 50)",
        )

    def handle(self, *args, **options):
        urls: list[str] = options["urls"]
        limit: int = options["limit"]

        self.stdout.write(f"Scraping {len(urls)} URL(s) — up to {limit} listing(s)…")

        try:
            records = scrape_facebook_marketplace(urls=urls, results_limit=limit)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(f"Done. {len(records)} record(s) saved to CompetitorMarketData.")
        )

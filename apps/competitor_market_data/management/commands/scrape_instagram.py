from django.core.management.base import BaseCommand, CommandError

from apps.competitor_market_data.scrapers.instagram_scraper import scrape_instagram_profiles


class Command(BaseCommand):
    help = "Scrape Instagram profiles via Apify and store results in CompetitorMarketData"

    def add_arguments(self, parser):
        parser.add_argument(
            "urls",
            nargs="+",
            type=str,
            help="Instagram profile URL(s) to scrape, e.g. https://www.instagram.com/competitor/",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            metavar="N",
            help="Max posts to fetch per profile (default: 50)",
        )

    def handle(self, *args, **options):
        urls: list[str] = options["urls"]
        limit: int = options["limit"]

        self.stdout.write(f"Scraping {len(urls)} profile(s) — up to {limit} post(s) each…")

        try:
            records = scrape_instagram_profiles(urls=urls, results_limit=limit)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(f"Done. {len(records)} record(s) saved to CompetitorMarketData.")
        )

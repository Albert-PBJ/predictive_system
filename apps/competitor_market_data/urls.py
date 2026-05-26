from django.urls import path

from apps.competitor_market_data.views import (
    FacebookMarketplaceScraperStartView,
    InstagramScraperStartView,
    WebsiteScraperStartView,
)

urlpatterns = [
    path("instagram/start", InstagramScraperStartView.as_view(), name="instagram-scraper-start"),
    path("facebook/start", FacebookMarketplaceScraperStartView.as_view(), name="facebook-scraper-start"),
    path("website/start", WebsiteScraperStartView.as_view(), name="website-scraper-start"),
]

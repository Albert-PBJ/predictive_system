from django.urls import path

from apps.competitor_market_data.views import (
    ScraperFinalizeView,
    ScraperStartView,
    ScraperStatusView,
)

# `source` ∈ {instagram, facebook, website}. Las rutas existentes
# (/scrapers/instagram/start, etc.) siguen resolviendo aquí.
urlpatterns = [
    path("<str:source>/start", ScraperStartView.as_view(), name="scraper-start"),
    path("<str:source>/status", ScraperStatusView.as_view(), name="scraper-status"),
    path("<str:source>/finalize", ScraperFinalizeView.as_view(), name="scraper-finalize"),
]

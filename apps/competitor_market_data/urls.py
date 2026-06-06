from django.urls import path

from apps.competitor_market_data.views import (
    LLMConnectionTestView,
    ScraperDataDetailView,
    ScraperDataView,
    ScraperFinalizeView,
    ScraperRejectedDetailView,
    ScraperRejectedView,
    ScraperStartView,
    ScraperStatusView,
)

# `source` ∈ {instagram, facebook, website}. Las rutas existentes
# (/scrapers/instagram/start, etc.) siguen resolviendo aquí.
# La ruta literal `llm/test` va primero para que no la capture `<str:source>`.
urlpatterns = [
    path("llm/test", LLMConnectionTestView.as_view(), name="llm-connection-test"),
    path("<str:source>/start", ScraperStartView.as_view(), name="scraper-start"),
    path("<str:source>/status", ScraperStatusView.as_view(), name="scraper-status"),
    path("<str:source>/finalize", ScraperFinalizeView.as_view(), name="scraper-finalize"),
    path("<str:source>/data", ScraperDataView.as_view(), name="scraper-data"),
    path("<str:source>/data/<int:pk>", ScraperDataDetailView.as_view(), name="scraper-data-detail"),
    path("<str:source>/rejected", ScraperRejectedView.as_view(), name="scraper-rejected"),
    path("<str:source>/rejected/<int:pk>", ScraperRejectedDetailView.as_view(), name="scraper-rejected-detail"),
]

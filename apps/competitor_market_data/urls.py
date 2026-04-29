from django.urls import path

from apps.competitor_market_data.views import InstagramScraperStartView

urlpatterns = [
    path("instagram/start", InstagramScraperStartView.as_view(), name="instagram-scraper-start"),
]

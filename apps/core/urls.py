from django.urls import path

from .views import LatestExchangeRateView

urlpatterns = [
    path("exchange-rate/latest", LatestExchangeRateView.as_view(), name="exchange-rate-latest"),
]

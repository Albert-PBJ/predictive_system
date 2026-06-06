from django.urls import path

from .views import CategoryListView, LatestExchangeRateView

urlpatterns = [
    path("categories", CategoryListView.as_view(), name="category-list"),
    path("exchange-rate/latest", LatestExchangeRateView.as_view(), name="exchange-rate-latest"),
]

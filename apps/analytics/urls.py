"""Rutas del módulo predictivo, montadas bajo ``/api/analytics/``."""

from django.urls import path

from .views import (
    CompetitorAnalysisView,
    DemandForecastView,
    ExchangeRateForecastView,
    ForecastableProductsView,
    InventoryForecastView,
    OverviewView,
    ProductPriceForecastView,
    ProfitForecastView,
    QuoteConversionForecastView,
    SalesForecastView,
)

urlpatterns = [
    path("overview", OverviewView.as_view(), name="analytics-overview"),
    path("forecastable-products", ForecastableProductsView.as_view(), name="analytics-products"),
    path("forecast/demand", DemandForecastView.as_view(), name="analytics-demand"),
    path("forecast/sales", SalesForecastView.as_view(), name="analytics-sales"),
    path("forecast/profit", ProfitForecastView.as_view(), name="analytics-profit"),
    path("forecast/exchange-rate", ExchangeRateForecastView.as_view(), name="analytics-rate"),
    path("forecast/product-price", ProductPriceForecastView.as_view(), name="analytics-price"),
    path("forecast/inventory", InventoryForecastView.as_view(), name="analytics-inventory"),
    path("forecast/quote-conversion", QuoteConversionForecastView.as_view(), name="analytics-quote"),
    path("benchmark/competitors", CompetitorAnalysisView.as_view(), name="analytics-competitors"),
]

"""Rutas del módulo predictivo, montadas bajo ``/api/analytics/``."""

from django.urls import path

from .stats_views import (
    CustomerStatsView,
    DashboardStatsView,
    ProductStatsView,
    QuoteStatsView,
    SalesStatsView,
)
from .views import (
    BenchmarkingComparisonView,
    BenchmarkingForecastView,
    BenchmarkingProductForecastView,
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
    # Benchmarking Competitivo (módulo dedicado con rango de fechas).
    path("benchmarking/comparison", BenchmarkingComparisonView.as_view(), name="analytics-benchmarking-comparison"),
    path("benchmarking/forecast", BenchmarkingForecastView.as_view(), name="analytics-benchmarking-forecast"),
    path("benchmarking/product-forecast", BenchmarkingProductForecastView.as_view(), name="analytics-benchmarking-product-forecast"),
    # Estadísticas descriptivas (paneles de situación).
    path("stats/dashboard", DashboardStatsView.as_view(), name="analytics-stats-dashboard"),
    path("stats/customers", CustomerStatsView.as_view(), name="analytics-stats-customers"),
    path("stats/products", ProductStatsView.as_view(), name="analytics-stats-products"),
    path("stats/sales", SalesStatsView.as_view(), name="analytics-stats-sales"),
    path("stats/quotes", QuoteStatsView.as_view(), name="analytics-stats-quotes"),
]

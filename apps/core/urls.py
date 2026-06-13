from django.urls import path

from .settings_api import (
    CompanyInfoView,
    ExchangeRateFetchView,
    ExchangeRateSetView,
    SettingsLLMTestView,
    SystemSettingsView,
)
from .views import CategoryListView, LatestExchangeRateView

urlpatterns = [
    path("categories", CategoryListView.as_view(), name="category-list"),
    path("exchange-rate/latest", LatestExchangeRateView.as_view(), name="exchange-rate-latest"),
    # Configuración del sistema (Gerente lee / Admin escribe)
    path("settings/", SystemSettingsView.as_view(), name="system-settings"),
    path("settings/company", CompanyInfoView.as_view(), name="settings-company"),
    path("settings/exchange-rate", ExchangeRateSetView.as_view(), name="settings-rate-set"),
    path("settings/exchange-rate/fetch", ExchangeRateFetchView.as_view(), name="settings-rate-fetch"),
    path("settings/llm-test", SettingsLLMTestView.as_view(), name="settings-llm-test"),
]

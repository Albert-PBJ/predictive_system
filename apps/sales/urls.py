from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import QuoteViewSet, SaleViewSet

router = DefaultRouter()
router.register(r"sales", SaleViewSet, basename="sale")
router.register(r"quotes", QuoteViewSet, basename="quote")

urlpatterns = [
    path("", include(router.urls)),
]

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import DispatchOrderViewSet, QuoteViewSet, SaleViewSet

router = DefaultRouter()
router.register(r"sales", SaleViewSet, basename="sale")
router.register(r"quotes", QuoteViewSet, basename="quote")
router.register(r"dispatch-orders", DispatchOrderViewSet, basename="dispatch-order")

urlpatterns = [
    path("", include(router.urls)),
]

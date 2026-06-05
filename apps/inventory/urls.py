from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import InventoryMovementViewSet, StockListView

router = DefaultRouter()
router.register(r"movements", InventoryMovementViewSet, basename="inventory-movement")

urlpatterns = [
    path("stock", StockListView.as_view(), name="inventory-stock"),
    path("", include(router.urls)),
]

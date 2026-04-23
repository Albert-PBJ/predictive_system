from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ProductViewset

router = DefaultRouter()
router.register(r"products", ProductViewset)

urlpatterns = [
    path("", include(router.urls)),
]

from rest_framework import viewsets

from apps.accounts.permissions import IsManager, IsSeller
from apps.core.models import Product

from .serializer import ProductSerializer


class ProductViewset(viewsets.ModelViewSet):
    """CRUD del catálogo de productos.

    Lectura para vendedores o superiores (necesitan el catálogo y los precios para
    registrar ventas); la escritura (alta/edición/baja de productos del catálogo)
    queda reservada a gerente/administrador.

    Filtros de query: `search` (nombre o SKU), `category`, `is_active`.
    """

    queryset = Product.objects.select_related("category").all()
    serializer_class = ProductSerializer

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsSeller()]
        return [IsManager()]

    @staticmethod
    def _as_bool(value):
        return str(value).lower() in ("1", "true", "yes", "si", "sí")

    def get_queryset(self):
        qs = Product.objects.select_related("category").order_by("category__name", "name")
        params = self.request.query_params

        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(name__icontains=search) | qs.filter(sku__icontains=search)

        category = params.get("category")
        if category:
            qs = qs.filter(category_id=category)

        is_active = params.get("is_active")
        if is_active is not None and is_active != "":
            qs = qs.filter(is_active=self._as_bool(is_active))

        return qs.distinct()

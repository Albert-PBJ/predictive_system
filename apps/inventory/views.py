from rest_framework import status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsSeller
from apps.core.models import Product

from .models import InventoryMovement
from .serializers import (
    InventoryMovementSerializer,
    MovementCreateSerializer,
    ProductStockSerializer,
)
from .services import InsufficientStockError, apply_movement


class InventoryMovementViewSet(viewsets.ModelViewSet):
    """Control de stock: historial de movimientos y registro de movimientos manuales.

    - GET  /api/inventory/movements/        → historial (auditoría) paginado y filtrable.
    - POST /api/inventory/movements/        → registra una entrada, ajuste o devolución.

    Acceso: Vendedor o superior. Las salidas por venta no se crean aquí (las
    genera el módulo de ventas); este endpoint solo admite ENT/AJU/DEV.
    """

    permission_classes = [IsSeller]
    serializer_class = InventoryMovementSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = (
            InventoryMovement.objects.select_related(
                "product", "responsible__profile", "sale"
            ).order_by("-movement_date", "-created_at")
        )
        params = self.request.query_params

        product = params.get("product")
        if product:
            qs = qs.filter(product_id=product)

        mtype = params.get("movement_type")
        if mtype:
            qs = qs.filter(movement_type=mtype)

        date_from = params.get("date_from")
        if date_from:
            qs = qs.filter(movement_date__gte=date_from)
        date_to = params.get("date_to")
        if date_to:
            qs = qs.filter(movement_date__lte=date_to)

        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(product__name__icontains=search)

        return qs

    def create(self, request, *args, **kwargs):
        serializer = MovementCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            movement = apply_movement(
                product=data["product"],
                movement_type=data["movement_type"],
                quantity=data["quantity"],
                responsible=request.user,
                reference=data.get("reference", ""),
                notes=data.get("notes", ""),
                movement_date=data.get("movement_date"),
            )
        except InsufficientStockError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            InventoryMovementSerializer(movement).data,
            status=status.HTTP_201_CREATED,
        )


class StockListView(APIView):
    """
    GET /api/inventory/stock

    Resumen de existencias de todos los productos para la pantalla de control de
    stock: nivel actual, mínimo y bandera de stock bajo. Devuelve además el
    total de productos y cuántos están en stock bajo (para las tarjetas-resumen).

    Parámetros de query (opcionales):
        search     (string)  — busca por nombre o SKU
        category   (int)     — filtra por categoría
        low_stock  (bool)    — solo productos en o por debajo del mínimo
        is_active  (bool)    — filtra por estado activo (por defecto, todos)

    Acceso: Vendedor o superior.
    """

    permission_classes = [IsSeller]

    @staticmethod
    def _as_bool(value):
        return str(value).lower() in ("1", "true", "yes", "si", "sí")

    def get(self, request):
        qs = Product.objects.select_related("category").order_by("category__name", "name")

        params = request.query_params
        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(name__icontains=search) | qs.filter(sku__icontains=search)

        category = params.get("category")
        if category:
            qs = qs.filter(category_id=category)

        is_active = params.get("is_active")
        if is_active is not None and is_active != "":
            qs = qs.filter(is_active=self._as_bool(is_active))

        qs = qs.distinct()

        low_only = self._as_bool(params.get("low_stock")) if params.get("low_stock") else False
        # `low_stock` es derivado (stock <= min_stock), por eso se filtra en Python.
        products = list(qs)
        low_stock_count = sum(1 for p in products if p.stock <= p.min_stock)
        if low_only:
            products = [p for p in products if p.stock <= p.min_stock]

        return Response(
            {
                "count": len(products),
                "low_stock_count": low_stock_count,
                "results": ProductStockSerializer(products, many=True).data,
            },
            status=status.HTTP_200_OK,
        )

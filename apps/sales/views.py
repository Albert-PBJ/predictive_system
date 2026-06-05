from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.accounts.models import Role
from apps.accounts.permissions import IsManager, IsOperational, IsSeller
from apps.core.models import Seller
from apps.inventory.services import InsufficientStockError

from .models import Sale
from .serializers import SaleCreateSerializer, SaleSerializer
from .services import SaleValidationError, create_sale, void_sale


def _is_manager(user):
    """True si el usuario es gerente o superior (puede actuar sobre otros vendedores)."""
    if user.is_superuser:
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and profile.role in (Role.ADMIN, Role.MANAGER))


class SaleViewSet(viewsets.ModelViewSet):
    """Registro y consulta de ventas.

    - GET  /api/sales/            → historial de ventas (paginado, filtrable).
    - POST /api/sales/            → registra una venta (descuenta stock atómicamente).
    - GET  /api/sales/{id}/       → detalle de una venta con sus líneas.
    - POST /api/sales/{id}/anular → anula la venta y devuelve el stock (gerente+).

    Acceso: **consultar** ventas es para personal operativo (vendedores y
    encargados de inventario, que las ven pero no las hacen, más gerente/admin);
    **registrar** una venta queda para vendedores o superiores; **anular** queda
    para gerente/admin (revierte inventario y borra el ingreso).
    """

    permission_classes = [IsOperational]
    http_method_names = ["get", "post", "head", "options"]

    def get_permissions(self):
        # Registrar requiere capacidad de vender; anular, ser gerente; consultar,
        # cualquier rol operativo (incluido el encargado de inventario).
        if self.action == "create":
            return [IsSeller()]
        if self.action == "anular":
            return [IsManager()]
        return super().get_permissions()

    def get_serializer_class(self):
        return SaleCreateSerializer if self.action == "create" else SaleSerializer

    def get_queryset(self):
        qs = (
            Sale.objects.select_related("customer", "seller", "seller__user__profile")
            .prefetch_related("items__product")
            .order_by("-sale_date", "-created_at")
        )
        params = self.request.query_params

        status_param = params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)

        seller = params.get("seller")
        if seller:
            qs = qs.filter(seller_id=seller)

        customer = params.get("customer")
        if customer:
            qs = qs.filter(customer_id=customer)

        date_from = params.get("date_from")
        if date_from:
            qs = qs.filter(sale_date__gte=date_from)
        date_to = params.get("date_to")
        if date_to:
            qs = qs.filter(sale_date__lte=date_to)

        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(customer__company_name__icontains=search)

        return qs

    def _resolve_seller(self, request, validated):
        """Determina el vendedor de la venta.

        Un gerente/admin puede indicar `seller` explícitamente; en cualquier otro
        caso se usa el perfil de vendedor del usuario autenticado.
        """
        explicit = validated.get("seller")
        if explicit and _is_manager(request.user):
            return explicit
        return Seller.objects.filter(user=request.user, is_active=True).first()

    def create(self, request, *args, **kwargs):
        serializer = SaleCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        seller = self._resolve_seller(request, data)
        if seller is None:
            return Response(
                {
                    "error": "Tu usuario no tiene un perfil de vendedor asociado. "
                    "Solicita a un administrador que lo cree para poder registrar ventas."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            sale = create_sale(
                seller=seller,
                customer=data["customer"],
                items=data["items"],
                user=request.user,
                sale_date=data.get("sale_date"),
                sale_type=data.get("sale_type") or Sale.TypeChoices.RETAIL,
                status=data.get("status") or Sale.StatusChoices.COMPLETED,
                notes=data.get("notes", ""),
            )
        except (SaleValidationError, InsufficientStockError) as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(SaleSerializer(sale).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])  # permiso resuelto en get_permissions
    def anular(self, request, pk=None):
        sale = self.get_object()
        try:
            void_sale(sale=sale, user=request.user)
        except SaleValidationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(SaleSerializer(sale).data, status=status.HTTP_200_OK)

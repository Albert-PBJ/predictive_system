from datetime import date
from decimal import Decimal

from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.accounts.models import Role
from apps.accounts.permissions import IsManager, IsOperational, IsSeller
from apps.audit import services as audit
from apps.audit.models import ActionChoices
from apps.core.models import Seller
from apps.inventory.services import InsufficientStockError

from .models import DispatchOrder, Quote, Sale
from .serializers import (
    DispatchOrderCreateSerializer,
    DispatchOrderSerializer,
    DispatchStatusSerializer,
    InvoiceInputSerializer,
    QuoteConvertSerializer,
    QuoteCreateSerializer,
    QuoteSerializer,
    SaleCreateSerializer,
    SaleNoteInputSerializer,
    SalePaymentInputSerializer,
    SaleSerializer,
)
from .services import (
    DispatchValidationError,
    QuoteValidationError,
    SaleValidationError,
    add_sale_payment,
    convert_quote_to_sale,
    create_dispatch_order,
    create_quote,
    create_sale,
    invoice_sale,
    suggest_invoice_numbers,
    update_dispatch_order,
    update_sale_notes,
    void_sale,
)


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
        # Registrar/facturar/abonar/editar nota requieren capacidad de vender; anular,
        # ser gerente; consultar, cualquier rol operativo (incl. encargado de inventario).
        if self.action in ("create", "facturar", "siguiente_factura", "pagos", "nota"):
            return [IsSeller()]
        if self.action == "anular":
            return [IsManager()]
        return super().get_permissions()

    def get_parsers(self):
        # Facturar puede traer el archivo de la factura (multipart); el resto es JSON.
        if getattr(self, "action", None) == "facturar":
            return [MultiPartParser(), FormParser(), JSONParser()]
        return super().get_parsers()

    def get_serializer_class(self):
        return SaleCreateSerializer if self.action == "create" else SaleSerializer

    def get_queryset(self):
        qs = (
            Sale.objects.select_related("customer", "seller", "seller__user__profile")
            .prefetch_related("items__product", "payments", "source_quote", "dispatch_orders")
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

        quote = data.get("quote")
        # Cobranza: si el pago no es completo, se pasa el abono inicial (0 = a crédito)
        # y el servicio deriva el estado (Pendiente/Completada) del saldo.
        fully_paid = data.get("fully_paid", True)
        amount_paid = None if fully_paid else (data.get("amount_paid") or Decimal("0"))
        try:
            sale = create_sale(
                seller=seller,
                customer=data["customer"],
                items=data["items"],
                user=request.user,
                sale_date=data.get("sale_date"),
                sale_type=data.get("sale_type") or Sale.TypeChoices.RETAIL,
                notes=data.get("notes", ""),
                iva_rate=data.get("iva_rate"),
                quote=quote,
                amount_paid=amount_paid,
                payment_method=data.get("payment_method"),
            )
        except (SaleValidationError, InsufficientStockError) as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        audit.log(
            request=request,
            action=ActionChoices.SALE_CREATE,
            description=(
                f"Registró la venta #{sale.pk} por {sale.total_sale_usd} USD "
                f"a {sale.customer.company_name}"
                + (f" (desde el presupuesto {quote.quote_number})" if quote else "")
                + "."
            ),
            target=sale,
            metadata={
                "total_usd": str(sale.total_sale_usd),
                "customer": sale.customer.company_name,
                "seller": seller.user.username if seller and seller.user_id else None,
                "items": sale.items.count(),
                "sale_type": sale.sale_type,
                "quote_number": quote.quote_number if quote else None,
            },
        )
        return Response(
            SaleSerializer(sale, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])  # permiso resuelto en get_permissions
    def anular(self, request, pk=None):
        sale = self.get_object()
        try:
            void_sale(sale=sale, user=request.user)
        except SaleValidationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        audit.log(
            request=request,
            action=ActionChoices.SALE_VOID,
            description=(
                f"Anuló la venta #{sale.pk} de {sale.customer.company_name} "
                f"({sale.total_sale_usd} USD), reingresando el stock."
            ),
            target=sale,
            metadata={"total_usd": str(sale.total_sale_usd), "customer": sale.customer.company_name},
        )
        return Response(
            SaleSerializer(sale, context=self.get_serializer_context()).data,
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="siguiente-factura")
    def siguiente_factura(self, request):
        """Sugiere el siguiente número correlativo de factura y de control."""
        return Response(suggest_invoice_numbers(), status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])  # permiso resuelto en get_permissions
    def facturar(self, request, pk=None):
        """Asocia los datos fiscales (nº factura, nº control, adjunto) a la venta."""
        sale = self.get_object()
        serializer = InvoiceInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            invoice_sale(
                sale=sale,
                invoice_number=data["invoice_number"],
                control_number=data["control_number"],
                invoice_date=data.get("invoice_date"),
                invoice_file=data.get("invoice_file"),
                user=request.user,
            )
        except SaleValidationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        audit.log(
            request=request,
            action=ActionChoices.SALE_INVOICE,
            description=(
                f"Facturó la venta #{sale.pk} de {sale.customer.company_name} "
                f"(factura {sale.invoice_number}, control {sale.control_number})."
            ),
            target=sale,
            metadata={
                "invoice_number": sale.invoice_number,
                "control_number": sale.control_number,
                "has_file": bool(sale.invoice_file),
            },
        )
        return Response(
            SaleSerializer(sale, context=self.get_serializer_context()).data,
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="pagos")  # permiso en get_permissions
    def pagos(self, request, pk=None):
        """Registra un abono a la venta y actualiza su cobranza (autocompleta al saldar)."""
        sale = self.get_object()
        serializer = SalePaymentInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            sale, payment = add_sale_payment(
                sale=sale,
                amount=data["amount_usd"],
                user=request.user,
                method=data.get("method"),
                payment_date=data.get("payment_date"),
                reference=data.get("reference", ""),
                notes=data.get("notes", ""),
            )
        except SaleValidationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        audit.log(
            request=request,
            action=ActionChoices.SALE_PAYMENT,
            description=(
                f"Registró un abono de {payment.amount_usd} USD a la venta #{sale.pk} "
                f"de {sale.customer.company_name} (saldo: {sale.balance_usd} USD)."
            ),
            target=sale,
            metadata={
                "amount_usd": str(payment.amount_usd),
                "method": payment.method,
                "balance_usd": str(sale.balance_usd),
                "status": sale.status,
            },
        )
        return Response(
            SaleSerializer(sale, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="nota")  # permiso en get_permissions
    def nota(self, request, pk=None):
        """Edita las notas/observaciones de la venta."""
        sale = self.get_object()
        serializer = SaleNoteInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        update_sale_notes(sale=sale, notes=serializer.validated_data.get("notes", ""))
        audit.log(
            request=request,
            action=ActionChoices.SALE_UPDATE,
            description=f"Editó las notas de la venta #{sale.pk} de {sale.customer.company_name}.",
            target=sale,
        )
        return Response(
            SaleSerializer(sale, context=self.get_serializer_context()).data,
            status=status.HTTP_200_OK,
        )


class QuoteViewSet(viewsets.ModelViewSet):
    """Creación y consulta de presupuestos (cotizaciones).

    - GET  /api/quotes/      → listado de presupuestos (paginado, filtrable).
    - POST /api/quotes/      → crea un presupuesto (no toca inventario).
    - GET  /api/quotes/{id}/ → detalle con sus líneas.

    Acceso: **consultar** es para personal operativo; **crear** queda para vendedores
    o superiores (igual que registrar una venta). El encargado de inventario los ve
    pero no los crea.
    """

    permission_classes = [IsOperational]
    http_method_names = ["get", "post", "head", "options"]

    def get_permissions(self):
        if self.action in ("create", "convertir"):
            return [IsSeller()]
        return super().get_permissions()

    def get_serializer_class(self):
        return QuoteCreateSerializer if self.action == "create" else QuoteSerializer

    def get_queryset(self):
        qs = (
            Quote.objects.select_related("customer", "seller", "seller__user__profile")
            .prefetch_related("items__product")
            .order_by("-issued_date", "-created_at")
        )
        params = self.request.query_params

        status_param = params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)

        customer = params.get("customer")
        if customer:
            qs = qs.filter(customer_id=customer)

        # `convertible=true`: presupuestos vigentes que aún pueden convertirse en venta
        # (no convertidos ni rechazados; sin vencer). Alimenta el buscador del formulario
        # de venta para relacionar un presupuesto.
        if str(params.get("convertible", "")).lower() in ("1", "true", "yes"):
            qs = qs.filter(converted_to_sale__isnull=True).exclude(
                status__in=[Quote.StatusChoices.CONVERTED, Quote.StatusChoices.REJECTED]
            ).filter(Q(expiry_date__isnull=True) | Q(expiry_date__gte=date.today()))

        date_from = params.get("date_from")
        if date_from:
            qs = qs.filter(issued_date__gte=date_from)
        date_to = params.get("date_to")
        if date_to:
            qs = qs.filter(issued_date__lte=date_to)

        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(customer__company_name__icontains=search) | Q(quote_number__icontains=search)
            )

        return qs

    def _resolve_seller(self, request, validated):
        """Vendedor del presupuesto: explícito si es gerente, si no el del usuario.

        A diferencia de una venta, un presupuesto admite no tener vendedor (el modelo
        lo permite), así que si el usuario no tiene perfil de vendedor se deja en null.
        """
        explicit = validated.get("seller")
        if explicit and _is_manager(request.user):
            return explicit
        return Seller.objects.filter(user=request.user, is_active=True).first()

    def create(self, request, *args, **kwargs):
        serializer = QuoteCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            quote = create_quote(
                seller=self._resolve_seller(request, data),
                customer=data["customer"],
                items=data["items"],
                issued_date=data.get("issued_date"),
                expiry_date=data.get("expiry_date"),
                # Si no se envía IVA, lo resuelve el servicio con el default de la
                # Configuración del Sistema (default_iva_pct).
                iva_rate=data.get("iva_rate"),
                includes_installation=data.get("includes_installation", False),
                includes_delivery=data.get("includes_delivery", False),
                status=data.get("status") or Quote.StatusChoices.DRAFT,
            )
        except QuoteValidationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        audit.log(
            request=request,
            action=ActionChoices.QUOTE_CREATE,
            description=(
                f"Creó el presupuesto {quote.quote_number} por {quote.total_usd} USD "
                f"a {quote.customer.company_name}."
            ),
            target=quote,
            metadata={
                "quote_number": quote.quote_number,
                "total_usd": str(quote.total_usd),
                "customer": quote.customer.company_name,
                "status": quote.status,
            },
        )
        return Response(QuoteSerializer(quote).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])  # permiso resuelto en get_permissions
    def convertir(self, request, pk=None):
        """Convierte el presupuesto en una venta real (descuenta inventario)."""
        quote = self.get_object()
        serializer = QuoteConvertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Vendedor: el explícito (si es gerente), si no el del presupuesto, si no el
        # del usuario autenticado (para que un vendedor convierta un presupuesto sin
        # vendedor asignado).
        explicit = data.get("seller")
        seller = explicit if (explicit and _is_manager(request.user)) else None
        if seller is None:
            seller = quote.seller or Seller.objects.filter(user=request.user, is_active=True).first()

        try:
            sale = convert_quote_to_sale(
                quote=quote,
                user=request.user,
                seller=seller,
                sale_date=data.get("sale_date"),
                sale_type=data.get("sale_type"),
            )
        except (QuoteValidationError, SaleValidationError, InsufficientStockError) as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        audit.log(
            request=request,
            action=ActionChoices.QUOTE_CONVERT,
            description=(
                f"Convirtió el presupuesto {quote.quote_number} en la venta #{sale.pk} "
                f"({sale.total_sale_usd} USD) a {sale.customer.company_name}."
            ),
            target=sale,
            metadata={
                "quote_number": quote.quote_number,
                "sale_id": sale.pk,
                "total_usd": str(sale.total_sale_usd),
                "customer": sale.customer.company_name,
            },
        )
        return Response(
            {
                "sale": SaleSerializer(sale, context=self.get_serializer_context()).data,
                "quote": QuoteSerializer(quote).data,
            },
            status=status.HTTP_201_CREATED,
        )


class DispatchOrderViewSet(viewsets.ModelViewSet):
    """Órdenes de despacho: documento de control de entrega de una venta.

    - GET  /api/dispatch-orders/            → listado (paginado, filtrable).
    - POST /api/dispatch-orders/            → genera una orden desde una venta.
    - GET  /api/dispatch-orders/{id}/       → detalle con sus líneas.
    - POST /api/dispatch-orders/{id}/estado → actualiza estado/datos de entrega.

    Acceso: **consultar** e **imprimir** es para todo el personal operativo; **crear**
    y **actualizar el estado** también (almacén, vendedor, gerente/admin). No mueve
    inventario: el stock ya se descontó al registrar la venta.
    """

    permission_classes = [IsOperational]
    http_method_names = ["get", "post", "head", "options"]

    def get_serializer_class(self):
        return DispatchOrderCreateSerializer if self.action == "create" else DispatchOrderSerializer

    def get_queryset(self):
        qs = (
            DispatchOrder.objects.select_related("sale", "sale__customer", "sale__seller", "created_by")
            .prefetch_related("items__product")
            .order_by("-created_at")
        )
        params = self.request.query_params

        status_param = params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)

        sale = params.get("sale")
        if sale:
            qs = qs.filter(sale_id=sale)

        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(order_number__icontains=search)
                | Q(sale__customer__company_name__icontains=search)
            )

        return qs

    def create(self, request, *args, **kwargs):
        serializer = DispatchOrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            order = create_dispatch_order(
                sale=data["sale"],
                user=request.user,
                items=data.get("items") or None,
                dispatch_date=data.get("dispatch_date"),
                delivery_address=data.get("delivery_address", ""),
                carrier=data.get("carrier", ""),
                notes=data.get("notes", ""),
                status=data.get("status") or DispatchOrder.StatusChoices.PENDING,
            )
        except DispatchValidationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        audit.log(
            request=request,
            action=ActionChoices.DISPATCH_CREATE,
            description=(
                f"Generó la orden de despacho {order.order_number} "
                f"para la venta #{order.sale_id} ({order.sale.customer.company_name})."
            ),
            target=order,
            metadata={
                "order_number": order.order_number,
                "sale_id": order.sale_id,
                "items": order.items.count(),
            },
        )
        return Response(
            DispatchOrderSerializer(order, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])  # IsOperational
    def estado(self, request, pk=None):
        """Actualiza el estado y/o los datos de entrega de la orden."""
        order = self.get_object()
        serializer = DispatchStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if not data:
            return Response({"error": "No se enviaron cambios."}, status=status.HTTP_400_BAD_REQUEST)

        update_dispatch_order(
            order=order,
            status=data.get("status"),
            dispatch_date=data.get("dispatch_date"),
            carrier=data.get("carrier"),
            received_by=data.get("received_by"),
            notes=data.get("notes"),
        )
        audit.log(
            request=request,
            action=ActionChoices.DISPATCH_UPDATE,
            description=(
                f"Actualizó la orden de despacho {order.order_number} "
                f"(estado: {order.get_status_display()})."
            ),
            target=order,
            metadata={"order_number": order.order_number, "status": order.status},
        )
        return Response(
            DispatchOrderSerializer(order, context=self.get_serializer_context()).data,
            status=status.HTTP_200_OK,
        )

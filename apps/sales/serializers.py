from decimal import Decimal

from rest_framework import serializers

from apps.core.models import Customer, Seller

from .models import (
    DispatchOrder,
    DispatchOrderItem,
    Quote,
    QuoteItem,
    Sale,
    SaleItem,
)


def _seller_display_name(seller) -> str:
    """Nombre del vendedor: prefiere el del UserProfile (fuente de verdad)."""
    if not seller:
        return ""
    profile = getattr(seller.user, "profile", None) if seller.user else None
    if profile:
        name = f"{profile.first_name} {profile.last_name}".strip()
        if name:
            return name
    fallback = f"{seller.first_name} {seller.last_name}".strip()
    return fallback or (seller.user.username if seller.user else "")


# ─────────────────────────── Lectura ───────────────────────────

class SaleItemSerializer(serializers.ModelSerializer):
    """Línea de una venta (lectura), con datos del producto para mostrar."""

    product_name = serializers.CharField(source="product.name", read_only=True)
    product_sku = serializers.CharField(source="product.sku", read_only=True, default=None)

    class Meta:
        model = SaleItem
        fields = (
            "id",
            "product",
            "product_name",
            "product_sku",
            "quantity",
            "unit_list_price_usd",
            "discount_pct",
            "unit_sale_price_usd",
            "unit_cost_price_usd",
            "subtotal_sale_usd",
            "subtotal_cost_usd",
            "line_profit_usd",
        )


class SaleSerializer(serializers.ModelSerializer):
    """Venta completa (lectura) con sus líneas y etiquetas legibles."""

    items = SaleItemSerializer(many=True, read_only=True)
    customer_name = serializers.CharField(source="customer.company_name", read_only=True)
    customer_rif = serializers.CharField(source="customer.rif", read_only=True)
    customer_address = serializers.CharField(source="customer.fiscal_address", read_only=True, default="")
    seller_name = serializers.SerializerMethodField()
    sale_type_display = serializers.CharField(source="get_sale_type_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    is_invoiced = serializers.BooleanField(read_only=True)
    invoice_file_url = serializers.SerializerMethodField()
    # Instancias relacionadas del flujo (para navegar entre ellas en la UI).
    source_quote = serializers.SerializerMethodField()
    dispatch_orders = serializers.SerializerMethodField()

    class Meta:
        model = Sale
        fields = (
            "id",
            "customer",
            "customer_name",
            "customer_rif",
            "customer_address",
            "seller",
            "seller_name",
            "sale_date",
            "sale_type",
            "sale_type_display",
            "status",
            "status_display",
            "total_sale_usd",
            "total_cost_usd",
            "total_profit_usd",
            "total_discount_usd",
            "total_sale_ves",
            "iva_rate",
            "iva_amount_usd",
            "total_with_iva_usd",
            "total_with_iva_ves",
            "commission_usd",
            "bcv_rate",
            "parallel_rate",
            "invoice_number",
            "control_number",
            "invoice_date",
            "invoice_file_url",
            "is_invoiced",
            "source_quote",
            "dispatch_orders",
            "notes",
            "items",
            "created_at",
        )

    def get_seller_name(self, obj):
        # El nombre real de la persona vive en el UserProfile (fuente de verdad);
        # se prefiere sobre el nombre guardado en el registro de Vendedor.
        return _seller_display_name(obj.seller)

    def get_invoice_file_url(self, obj):
        """URL absoluta del adjunto de la factura (o None si no tiene)."""
        if not obj.invoice_file:
            return None
        url = obj.invoice_file.url
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url

    def get_source_quote(self, obj):
        """Presupuesto del que proviene la venta (si se generó desde uno)."""
        quotes = list(obj.source_quote.all())  # reverse FK Quote.converted_to_sale (prefetch)
        q = quotes[0] if quotes else None
        return {"id": q.id, "quote_number": q.quote_number} if q else None

    def get_dispatch_orders(self, obj):
        """Órdenes de despacho generadas a partir de la venta."""
        return [
            {
                "id": d.id,
                "order_number": d.order_number,
                "status": d.status,
                "status_display": d.get_status_display(),
            }
            for d in obj.dispatch_orders.all()  # prefetch
        ]


# ─────────────────────────── Escritura ───────────────────────────

class SaleItemInputSerializer(serializers.Serializer):
    """Una línea de la venta entrante."""

    product = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=1)
    # Descuento por línea (%). Si se envía, el servicio calcula el precio neto a
    # partir del precio de lista del producto.
    discount_pct = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, allow_null=True, min_value=0, max_value=100
    )
    # Opcional: precio neto explícito. Si se omite (y no hay descuento), el servicio
    # usa el precio de venta actual del producto. Si se envía `discount_pct`, este
    # se ignora (manda el descuento).
    unit_sale_price_usd = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, allow_null=True, min_value=0
    )


class SaleCreateSerializer(serializers.Serializer):
    """Carga útil para registrar una venta.

    El vendedor (`seller`) es opcional: si se omite, se resuelve desde el usuario
    autenticado. Un gerente/administrador puede registrar a nombre de otro
    vendedor enviándolo explícitamente (lo resuelve la vista).
    """

    customer = serializers.PrimaryKeyRelatedField(queryset=Customer.objects.all())
    seller = serializers.PrimaryKeyRelatedField(
        queryset=Seller.objects.filter(is_active=True), required=False, allow_null=True
    )
    sale_date = serializers.DateField(required=False, allow_null=True)
    sale_type = serializers.ChoiceField(
        choices=Sale.TypeChoices.choices, required=False, default=Sale.TypeChoices.RETAIL
    )
    status = serializers.ChoiceField(
        choices=[Sale.StatusChoices.COMPLETED, Sale.StatusChoices.PENDING],
        required=False,
        default=Sale.StatusChoices.COMPLETED,
    )
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    # IVA opcional: si se omite, el servicio usa el default de la Configuración (16%).
    iva_rate = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, allow_null=True, min_value=0, max_value=100
    )
    # Presupuesto relacionado (opcional): al registrar la venta desde un presupuesto,
    # se enlaza y se marca como convertido. Debe ser del mismo cliente (lo valida el servicio).
    quote = serializers.PrimaryKeyRelatedField(
        queryset=Quote.objects.all(), required=False, allow_null=True
    )
    items = SaleItemInputSerializer(many=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("La venta debe tener al menos una línea de producto.")
        return value


class InvoiceInputSerializer(serializers.Serializer):
    """Carga útil para facturar una venta (datos fiscales, opcional el adjunto).

    Acepta `multipart/form-data` porque puede traer el archivo de la factura.
    """

    invoice_number = serializers.CharField(max_length=40)
    control_number = serializers.CharField(max_length=40)
    invoice_date = serializers.DateField(required=False, allow_null=True)
    invoice_file = serializers.FileField(required=False, allow_null=True)

    def validate_invoice_file(self, value):
        if value is None:
            return value
        name = (value.name or "").lower()
        allowed = (".pdf", ".png", ".jpg", ".jpeg", ".webp")
        if not name.endswith(allowed):
            raise serializers.ValidationError("El archivo debe ser PDF o imagen (PDF, PNG, JPG, WEBP).")
        if value.size and value.size > 10 * 1024 * 1024:
            raise serializers.ValidationError("El archivo no puede superar los 10 MB.")
        return value


# ─────────────────────────── Presupuestos ───────────────────────────

class QuoteItemSerializer(serializers.ModelSerializer):
    """Línea de un presupuesto (lectura)."""

    product_name = serializers.CharField(source="product.name", read_only=True)
    product_sku = serializers.CharField(source="product.sku", read_only=True, default=None)

    class Meta:
        model = QuoteItem
        fields = (
            "id",
            "product",
            "product_name",
            "product_sku",
            "quantity",
            "unit_price_usd",
            "unit_price_ves",
            "line_total_usd",
            "line_total_ves",
        )


class QuoteSerializer(serializers.ModelSerializer):
    """Presupuesto completo (lectura) con sus líneas y etiquetas legibles."""

    items = QuoteItemSerializer(many=True, read_only=True)
    customer_name = serializers.CharField(source="customer.company_name", read_only=True)
    customer_rif = serializers.CharField(source="customer.rif", read_only=True)
    seller_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Quote
        fields = (
            "id",
            "quote_number",
            "customer",
            "customer_name",
            "customer_rif",
            "seller",
            "seller_name",
            "issued_date",
            "expiry_date",
            "bcv_rate",
            "parallel_rate",
            "includes_installation",
            "includes_delivery",
            "subtotal_usd",
            "subtotal_ves",
            "iva_rate",
            "iva_amount_usd",
            "total_usd",
            "total_ves",
            "status",
            "status_display",
            "converted_to_sale",
            "items",
            "created_at",
        )

    def get_seller_name(self, obj):
        return _seller_display_name(obj.seller)


class QuoteItemInputSerializer(serializers.Serializer):
    """Una línea del presupuesto entrante."""

    product = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=1)
    # Precio unitario opcional: si se omite, el servicio usa el precio de venta del
    # producto. Permite cotizar a un precio negociado distinto del de lista.
    unit_price_usd = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, allow_null=True, min_value=0
    )


class QuoteCreateSerializer(serializers.Serializer):
    """Carga útil para crear un presupuesto. El vendedor se resuelve en la vista."""

    customer = serializers.PrimaryKeyRelatedField(queryset=Customer.objects.all())
    seller = serializers.PrimaryKeyRelatedField(
        queryset=Seller.objects.filter(is_active=True), required=False, allow_null=True
    )
    issued_date = serializers.DateField(required=False, allow_null=True)
    expiry_date = serializers.DateField(required=False, allow_null=True)
    iva_rate = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, default=Decimal("16.00"),
        min_value=0, max_value=100,
    )
    includes_installation = serializers.BooleanField(required=False, default=False)
    includes_delivery = serializers.BooleanField(required=False, default=False)
    status = serializers.ChoiceField(
        choices=[
            Quote.StatusChoices.DRAFT,
            Quote.StatusChoices.SENT,
            Quote.StatusChoices.APPROVED,
            Quote.StatusChoices.REJECTED,
        ],
        required=False,
        default=Quote.StatusChoices.DRAFT,
    )
    items = QuoteItemInputSerializer(many=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("El presupuesto debe tener al menos una línea de producto.")
        return value


class QuoteConvertSerializer(serializers.Serializer):
    """Carga útil para convertir un presupuesto en venta (todo opcional)."""

    sale_date = serializers.DateField(required=False, allow_null=True)
    sale_type = serializers.ChoiceField(
        choices=Sale.TypeChoices.choices, required=False, allow_null=True
    )
    seller = serializers.PrimaryKeyRelatedField(
        queryset=Seller.objects.filter(is_active=True), required=False, allow_null=True
    )


# ─────────────────────────── Órdenes de despacho ───────────────────────────

class DispatchOrderItemSerializer(serializers.ModelSerializer):
    """Línea de una orden de despacho (lectura)."""

    product_name = serializers.CharField(source="product.name", read_only=True)
    product_sku = serializers.CharField(source="product.sku", read_only=True, default=None)

    class Meta:
        model = DispatchOrderItem
        fields = ("id", "product", "product_name", "product_sku", "quantity")


class DispatchOrderSerializer(serializers.ModelSerializer):
    """Orden de despacho completa (lectura) con sus líneas y datos de la venta."""

    items = DispatchOrderItemSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    customer_name = serializers.CharField(source="sale.customer.company_name", read_only=True)
    customer_rif = serializers.CharField(source="sale.customer.rif", read_only=True)
    sale_date = serializers.DateField(source="sale.sale_date", read_only=True)
    seller_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = DispatchOrder
        fields = (
            "id",
            "order_number",
            "sale",
            "customer_name",
            "customer_rif",
            "sale_date",
            "seller_name",
            "status",
            "status_display",
            "dispatch_date",
            "delivery_address",
            "carrier",
            "received_by",
            "notes",
            "created_by_name",
            "created_at",
            "updated_at",
            "items",
        )

    def get_seller_name(self, obj):
        return _seller_display_name(obj.sale.seller)

    def get_created_by_name(self, obj):
        return obj.created_by.username if obj.created_by else "—"


class DispatchOrderItemInputSerializer(serializers.Serializer):
    product = serializers.IntegerField(min_value=1)
    quantity = serializers.IntegerField(min_value=1)


class DispatchOrderCreateSerializer(serializers.Serializer):
    """Carga útil para generar una orden de despacho a partir de una venta."""

    sale = serializers.PrimaryKeyRelatedField(queryset=Sale.objects.all())
    dispatch_date = serializers.DateField(required=False, allow_null=True)
    delivery_address = serializers.CharField(required=False, allow_blank=True, default="")
    carrier = serializers.CharField(required=False, allow_blank=True, default="")
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    status = serializers.ChoiceField(
        choices=DispatchOrder.StatusChoices.choices,
        required=False,
        default=DispatchOrder.StatusChoices.PENDING,
    )
    # Opcional: si se omite, se despachan las líneas físicas de la venta.
    items = DispatchOrderItemInputSerializer(many=True, required=False)


class DispatchStatusSerializer(serializers.Serializer):
    """Actualización del estado / datos de entrega de una orden de despacho."""

    status = serializers.ChoiceField(choices=DispatchOrder.StatusChoices.choices, required=False)
    dispatch_date = serializers.DateField(required=False, allow_null=True)
    carrier = serializers.CharField(required=False, allow_blank=True)
    received_by = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)

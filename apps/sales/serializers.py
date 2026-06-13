from decimal import Decimal

from rest_framework import serializers

from apps.core.models import Customer, Seller

from .models import Quote, QuoteItem, Sale, SaleItem


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
    seller_name = serializers.SerializerMethodField()
    sale_type_display = serializers.CharField(source="get_sale_type_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Sale
        fields = (
            "id",
            "customer",
            "customer_name",
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
            "commission_usd",
            "bcv_rate",
            "parallel_rate",
            "notes",
            "items",
            "created_at",
        )

    def get_seller_name(self, obj):
        # El nombre real de la persona vive en el UserProfile (fuente de verdad);
        # se prefiere sobre el nombre guardado en el registro de Vendedor.
        return _seller_display_name(obj.seller)


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
    items = SaleItemInputSerializer(many=True)

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("La venta debe tener al menos una línea de producto.")
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

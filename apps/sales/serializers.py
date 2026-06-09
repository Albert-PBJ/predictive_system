from rest_framework import serializers

from apps.core.models import Customer, Seller

from .models import Sale, SaleItem


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
        seller = obj.seller
        profile = getattr(seller.user, "profile", None) if seller.user else None
        if profile:
            name = f"{profile.first_name} {profile.last_name}".strip()
            if name:
                return name
        fallback = f"{seller.first_name} {seller.last_name}".strip()
        return fallback or (seller.user.username if seller.user else "")


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

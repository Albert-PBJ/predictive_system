from rest_framework import serializers

from apps.core.models import Product

from .models import InventoryMovement


class InventoryMovementSerializer(serializers.ModelSerializer):
    """Representación de lectura de un movimiento de inventario (historial/auditoría)."""

    product_name = serializers.CharField(source="product.name", read_only=True)
    product_sku = serializers.CharField(source="product.sku", read_only=True, default=None)
    movement_type_display = serializers.CharField(
        source="get_movement_type_display", read_only=True
    )
    responsible_username = serializers.CharField(
        source="responsible.username", read_only=True, default=None
    )
    responsible_name = serializers.SerializerMethodField()

    class Meta:
        model = InventoryMovement
        fields = (
            "id",
            "product",
            "product_name",
            "product_sku",
            "movement_type",
            "movement_type_display",
            "quantity",
            "sale",
            "reference",
            "responsible",
            "responsible_username",
            "responsible_name",
            "movement_date",
            "notes",
            "created_at",
        )

    def get_responsible_name(self, obj):
        # Nombre real del responsable desde su UserProfile (fuente de verdad);
        # cae al nombre de usuario si el perfil no tiene nombre cargado.
        user = obj.responsible
        if not user:
            return None
        profile = getattr(user, "profile", None)
        if profile:
            name = f"{profile.first_name} {profile.last_name}".strip()
            if name:
                return name
        return user.username


class MovementCreateSerializer(serializers.Serializer):
    """Entrada para registrar un movimiento manual (entrada, ajuste o devolución).

    Las salidas por venta (`SAL`) NO se registran por aquí: las genera el módulo
    de ventas automáticamente. `quantity` es el delta con signo (positivo suma,
    negativo resta); solo el ajuste (`AJU`) admite valores negativos.
    """

    MANUAL_TYPES = (
        InventoryMovement.MovementTypeChoices.ENTRY,
        InventoryMovement.MovementTypeChoices.ADJUSTMENT,
        InventoryMovement.MovementTypeChoices.RETURN,
    )

    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    movement_type = serializers.ChoiceField(
        choices=InventoryMovement.MovementTypeChoices.choices
    )
    quantity = serializers.IntegerField()
    reference = serializers.CharField(required=False, allow_blank=True, default="")
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    movement_date = serializers.DateField(required=False, allow_null=True)

    def validate_movement_type(self, value):
        if value not in self.MANUAL_TYPES:
            raise serializers.ValidationError(
                "Las salidas por venta se registran automáticamente al crear una venta; "
                "aquí solo se permiten entradas, ajustes y devoluciones."
            )
        return value

    def validate(self, attrs):
        mtype = attrs["movement_type"]
        qty = attrs["quantity"]
        if qty == 0:
            raise serializers.ValidationError({"quantity": "La cantidad no puede ser cero."})
        positive_only = (
            InventoryMovement.MovementTypeChoices.ENTRY,
            InventoryMovement.MovementTypeChoices.RETURN,
        )
        if mtype in positive_only and qty < 0:
            raise serializers.ValidationError(
                {"quantity": "Para entradas y devoluciones la cantidad debe ser positiva."}
            )
        return attrs


class ProductStockSerializer(serializers.ModelSerializer):
    """Resumen de existencias de un producto para la pantalla de control de stock."""

    category_name = serializers.CharField(source="category.name", read_only=True, default=None)
    low_stock = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = (
            "id",
            "sku",
            "name",
            "full_name",
            "category",
            "category_name",
            "stock",
            "min_stock",
            "low_stock",
            "sale_price_usd",
            "purchase_price_usd",
            "is_active",
        )

    def get_low_stock(self, obj):
        # Se considera bajo cuando llega o cae por debajo del mínimo configurado.
        return obj.stock <= obj.min_stock

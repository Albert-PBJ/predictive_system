from decimal import Decimal

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
    verified_by_name = serializers.SerializerMethodField()
    pending_cost = serializers.SerializerMethodField()
    cost_invoice_url = serializers.SerializerMethodField()

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
            "unit_cost_usd",
            "pending_cost",
            "cost_invoice_url",
            "sale",
            "reference",
            "responsible",
            "responsible_username",
            "responsible_name",
            "movement_date",
            "notes",
            "verified",
            "verified_by",
            "verified_by_name",
            "verified_at",
            "created_at",
        )

    def get_pending_cost(self, obj):
        # Entrada por compra a la que todavía no se le cargó el costo de la factura del
        # proveedor (la mercancía llega antes que la factura). Es la bandeja de trabajo
        # de la gerencia: solo entradas ENT, nunca salidas ni devoluciones/ajustes.
        return (
            obj.movement_type == InventoryMovement.MovementTypeChoices.ENTRY
            and obj.sale_id is None
            and obj.quantity > 0
            and obj.unit_cost_usd is None
        )

    def get_cost_invoice_url(self, obj):
        """URL absoluta de la factura de compra adjunta (o None si no tiene)."""
        if not obj.cost_invoice_file:
            return None
        url = obj.cost_invoice_file.url
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request else url

    def get_verified_by_name(self, obj):
        # Nombre real de quien verificó (desde su UserProfile); cae al username.
        user = obj.verified_by
        if not user:
            return None
        profile = getattr(user, "profile", None)
        if profile:
            name = f"{profile.first_name} {profile.last_name}".strip()
            if name:
                return name
        return user.username

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
    # Costo unitario de compra (USD): en una ENTRADA recalcula el costo promedio
    # ponderado del producto. Opcional; en ajustes/devoluciones se ignora salvo que
    # se quiera fijar un costo explícito.
    unit_cost = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, allow_null=True, min_value=0
    )
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


class MovementCostSerializer(serializers.Serializer):
    """Carga del costo de compra sobre una entrada ya registrada (llegó la factura).

    La cantidad y el producto no se tocan: el movimiento ya ocurrió y el inventario es
    append-only. Aquí solo se completa el dato económico que faltaba, más —opcionalmente—
    la referencia (nº de factura del proveedor), el **archivo de la factura** y una nota.
    Acepta `multipart/form-data` porque puede traer el adjunto.
    """

    unit_cost = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    reference = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    invoice_file = serializers.FileField(required=False, allow_null=True)

    def validate_invoice_file(self, value):
        # Mismo criterio que el adjunto de la factura de venta (apps/sales).
        if value is None:
            return value
        name = (value.name or "").lower()
        allowed = (".pdf", ".png", ".jpg", ".jpeg", ".webp")
        if not name.endswith(allowed):
            raise serializers.ValidationError("El archivo debe ser PDF o imagen (PDF, PNG, JPG, WEBP).")
        if value.size and value.size > 10 * 1024 * 1024:
            raise serializers.ValidationError("El archivo no puede superar los 10 MB.")
        return value


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
            "average_cost_usd",
            "is_active",
        )

    def get_low_stock(self, obj):
        # Se considera bajo cuando llega o cae por debajo del mínimo configurado.
        return obj.stock <= obj.min_stock

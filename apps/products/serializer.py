from rest_framework import serializers

from apps.core.models import Product


class ProductSerializer(serializers.ModelSerializer):
    # Etiquetas legibles para mostrar en la UI sin un segundo request.
    category_name = serializers.CharField(source="category.name", read_only=True, default=None)
    material_display = serializers.CharField(source="get_material_display", read_only=True, default=None)
    low_stock = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = "__all__"
        # El stock es de solo lectura aquí a propósito: el inventario es
        # append-only (se ajusta vía InventoryMovement en el módulo de Inventario),
        # así que editar un producto nunca puede mover el stock sin dejar rastro.
        read_only_fields = ("stock", "created_at", "updated_at")

    def get_low_stock(self, obj):
        return obj.stock <= obj.min_stock

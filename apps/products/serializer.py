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

    def get_low_stock(self, obj):
        return obj.stock <= obj.min_stock

from django.contrib import admin

from .models import InventoryMovement


@admin.register(InventoryMovement)
class InventoryMovementAdmin(admin.ModelAdmin):
    list_display = ("product", "movement_type", "quantity", "movement_date", "responsible")
    list_filter = ("movement_type", "movement_date")
    search_fields = ("product__name", "product__sku", "reference")
    date_hierarchy = "movement_date"

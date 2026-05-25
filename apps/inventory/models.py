from django.db import models
from django.utils.translation import gettext_lazy as _


class InventoryMovement(models.Model):
    class MovementTypeChoices(models.TextChoices):
        ENTRY = "ENT", _("Entrada (Compra/Reposición)")
        EXIT = "SAL", _("Salida (Venta)")
        ADJUSTMENT = "AJU", _("Ajuste")
        RETURN = "DEV", _("Devolución")

    product = models.ForeignKey(
        "core.Product",
        on_delete=models.PROTECT,
        related_name="inventory_movements",
        help_text=_("Producto al que corresponde el movimiento"),
    )
    movement_type = models.CharField(
        max_length=3,
        choices=MovementTypeChoices.choices,
        help_text=_("Tipo de movimiento de inventario"),
    )
    quantity = models.IntegerField(
        help_text=_("Cantidad movida (positivo=entrada, negativo=salida)"),
    )
    sale = models.ForeignKey(
        "sales.Sale",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="inventory_movements",
        help_text=_("Venta asociada si el movimiento es una salida por venta"),
    )
    reference = models.CharField(
        max_length=255, blank=True,
        help_text=_("Referencia libre (ej: número de factura de compra, orden de despacho)"),
    )
    responsible = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="inventory_movements",
        help_text=_("Usuario responsable del movimiento"),
    )
    movement_date = models.DateField(help_text=_("Fecha del movimiento"))
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inventory_movements"
        verbose_name = "Movimiento de Inventario"
        verbose_name_plural = "Movimientos de Inventario"
        ordering = ["-movement_date", "-created_at"]
        indexes = [
            models.Index(fields=["product", "movement_date"], name="invmov_product_date_idx"),
            models.Index(fields=["movement_type"], name="invmov_type_idx"),
        ]

    def __str__(self):
        return f"{self.get_movement_type_display()} — {self.product.name} x{self.quantity} ({self.movement_date})"

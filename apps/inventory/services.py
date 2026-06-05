"""Lógica de negocio de inventario.

Toda mutación de stock pasa por aquí: el inventario es *append-only*, por lo que
nunca se modifica `Product.stock` directamente sin dejar el `InventoryMovement`
correspondiente. Este módulo concentra esa regla para que tanto los movimientos
manuales (entradas/ajustes/devoluciones) como las salidas automáticas por venta
queden registradas de forma consistente y auditable.
"""

from django.db import transaction
from django.utils import timezone

from apps.core.models import Product

from .models import InventoryMovement


class InsufficientStockError(Exception):
    """Se intentó descontar más stock del disponible."""

    def __init__(self, product, available, requested):
        self.product = product
        self.available = available
        self.requested = requested
        super().__init__(
            f"Stock insuficiente para '{product.name}': "
            f"disponible {available}, solicitado {requested}."
        )


@transaction.atomic
def apply_movement(
    *,
    product,
    movement_type,
    quantity,
    responsible=None,
    sale=None,
    reference="",
    notes="",
    movement_date=None,
):
    """Registra un movimiento de inventario y actualiza `Product.stock` de forma atómica.

    `quantity` es el delta con signo: positivo = entrada (compra/devolución/ajuste
    al alza), negativo = salida (venta/ajuste a la baja). Bloquea la fila del
    producto (`select_for_update`) para evitar condiciones de carrera entre ventas
    o ajustes simultáneos, y rechaza el movimiento si dejaría el stock en negativo.
    """
    # Re-lee el producto bloqueado para que el cálculo de stock sea consistente
    # aunque otra transacción lo haya tocado en paralelo. `.order_by()` evita el
    # OUTER JOIN del orden por defecto (FK `category` anulable), incompatible con
    # SELECT ... FOR UPDATE en PostgreSQL.
    locked = Product.objects.select_for_update().order_by().get(pk=product.pk)
    new_stock = locked.stock + quantity
    if new_stock < 0:
        raise InsufficientStockError(locked, locked.stock, -quantity)

    movement = InventoryMovement.objects.create(
        product=locked,
        movement_type=movement_type,
        quantity=quantity,
        sale=sale,
        reference=reference,
        responsible=responsible,
        movement_date=movement_date or timezone.now().date(),
        notes=notes,
    )

    locked.stock = new_stock
    locked.save(update_fields=["stock", "updated_at"])
    return movement

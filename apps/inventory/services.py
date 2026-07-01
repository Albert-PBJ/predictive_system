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

    # Alerta persistente de reabastecimiento: cada vez que el stock cae en o por
    # debajo del mínimo se registra/actualiza una alerta de quiebre de stock (y se
    # resuelve al recuperarse). Es best-effort: nunca debe romper el movimiento.
    _sync_low_stock_alert(locked)
    return movement


def _sync_low_stock_alert(product):
    """Crea o resuelve la alerta de stock bajo de un producto según su nivel actual.

    Si el stock cae en o por debajo de ``min_stock`` se registra una alerta de
    ``STOCK_BREAK`` no resuelta (crítica si quedó en 0, de advertencia si no),
    deduplicada por producto. Si el stock vuelve por encima del mínimo, se resuelven
    las alertas abiertas de ese producto. Envuelto en try/except: el registro de una
    alerta jamás debe interrumpir el movimiento de inventario que la origina.
    """
    try:
        # Los servicios no llevan inventario; un mínimo de 0 significa "sin control".
        if getattr(product, "is_service", False) or not product.min_stock:
            return

        from apps.analytics.models import Alert

        title = f"Stock bajo: {product.name}"
        if product.stock <= product.min_stock:
            severity = (
                Alert.SeverityChoices.CRITICAL
                if product.stock <= 0
                else Alert.SeverityChoices.WARNING
            )
            estado = "sin stock" if product.stock <= 0 else f"{product.stock} unidad(es)"
            message = (
                f"El producto '{product.name}' (SKU {product.sku or '—'}) tiene {estado}, "
                f"en o por debajo de su mínimo de {product.min_stock}. Conviene reabastecer."
            )
            alert, created = Alert.objects.get_or_create(
                alert_type=Alert.TypeChoices.STOCK_BREAK,
                title=title,
                is_resolved=False,
                defaults={"severity": severity, "message": message},
            )
            if not created and (alert.severity != severity or alert.message != message):
                alert.severity = severity
                alert.message = message
                alert.save(update_fields=["severity", "message"])
        else:
            # Recuperó nivel: resuelve las alertas abiertas de ese producto.
            Alert.objects.filter(
                alert_type=Alert.TypeChoices.STOCK_BREAK,
                title=title,
                is_resolved=False,
            ).update(is_resolved=True)
    except Exception:  # noqa: BLE001 — auditar/alertar nunca debe romper la operación
        import logging

        logging.getLogger("apps").warning("No se pudo sincronizar la alerta de stock bajo", exc_info=True)

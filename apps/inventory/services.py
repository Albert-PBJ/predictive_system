"""Lógica de negocio de inventario.

Toda mutación de stock pasa por aquí: el inventario es *append-only*, por lo que
nunca se modifica `Product.stock` directamente sin dejar el `InventoryMovement`
correspondiente. Este módulo concentra esa regla para que tanto los movimientos
manuales (entradas/ajustes/devoluciones) como las salidas automáticas por venta
queden registradas de forma consistente y auditable.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.models import Product

from .models import InventoryMovement

CENTS = Decimal("0.01")


def _money(value):
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


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


def _recompute_average_cost(*, prev_stock, prev_avg, quantity, entry_cost):
    """Costo promedio ponderado móvil (CPP) tras una entrada con costo conocido.

    Nuevo promedio = (valor previo del inventario + valor de lo recibido) / stock nuevo,
    donde el valor previo se valora al promedio vigente. Si aún no había promedio, se
    parte del costo de la entrada. Solo las **entradas** con costo mueven el promedio;
    las salidas y devoluciones lo conservan (esa lógica vive en `apply_movement`).
    """
    base_avg = prev_avg if prev_avg is not None else entry_cost
    new_stock = prev_stock + quantity
    if new_stock <= 0:
        return entry_cost
    prev_value = Decimal(prev_stock) * Decimal(base_avg)
    added_value = Decimal(quantity) * Decimal(entry_cost)
    return _money((prev_value + added_value) / Decimal(new_stock))


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
    unit_cost=None,
):
    """Registra un movimiento de inventario y actualiza `Product.stock` de forma atómica.

    `quantity` es el delta con signo: positivo = entrada (compra/devolución/ajuste
    al alza), negativo = salida (venta/ajuste a la baja). Bloquea la fila del
    producto (`select_for_update`) para evitar condiciones de carrera entre ventas
    o ajustes simultáneos, y rechaza el movimiento si dejaría el stock en negativo.

    **Costeo por promedio ponderado móvil:** `unit_cost` es el costo unitario en USD
    del movimiento. En una **entrada** (compra/reposición) con `unit_cost` recalcula
    el `average_cost_usd` del producto (CPP) y lo guarda como `unit_cost_usd` del
    movimiento. En una **salida** por venta el promedio no cambia: se registra en el
    movimiento el costo promedio aplicado (CMV) — lo pasa `create_sale`, o en su
    defecto se toma el promedio vigente. Sin `unit_cost` (p. ej. una devolución o un
    ajuste sin costo) el promedio se conserva.
    """
    # Re-lee el producto bloqueado para que el cálculo de stock sea consistente
    # aunque otra transacción lo haya tocado en paralelo. `.order_by()` evita el
    # OUTER JOIN del orden por defecto (FK `category` anulable), incompatible con
    # SELECT ... FOR UPDATE en PostgreSQL.
    locked = Product.objects.select_for_update().order_by().get(pk=product.pk)
    prev_stock = locked.stock
    new_stock = prev_stock + quantity
    if new_stock < 0:
        raise InsufficientStockError(locked, prev_stock, -quantity)

    # Promedio vigente antes del movimiento (cae al precio de compra si nunca se fijó).
    avg_before = locked.average_cost_usd if locked.average_cost_usd is not None else locked.purchase_price_usd
    is_entry = quantity > 0
    average_changed = False
    movement_unit_cost = None

    if unit_cost is not None:
        unit_cost = _money(unit_cost)

    if is_entry and unit_cost is not None:
        # Entrada con costo conocido → recalcula el promedio ponderado móvil.
        locked.average_cost_usd = _recompute_average_cost(
            prev_stock=prev_stock, prev_avg=avg_before, quantity=quantity, entry_cost=unit_cost
        )
        average_changed = True
        movement_unit_cost = unit_cost
    elif unit_cost is not None:
        # Salida/ajuste con costo explícito (p. ej. el CMV que pasa la venta): se
        # registra en el movimiento, pero no altera el promedio.
        movement_unit_cost = unit_cost
    elif avg_before is not None:
        # Sin costo explícito: se sella el promedio vigente para dejar rastro del CMV.
        movement_unit_cost = _money(avg_before)

    movement = InventoryMovement.objects.create(
        product=locked,
        movement_type=movement_type,
        quantity=quantity,
        sale=sale,
        reference=reference,
        responsible=responsible,
        movement_date=movement_date or timezone.now().date(),
        notes=notes,
        unit_cost_usd=movement_unit_cost,
    )

    locked.stock = new_stock
    update_fields = ["stock", "updated_at"]
    if average_changed:
        update_fields.append("average_cost_usd")
    locked.save(update_fields=update_fields)

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

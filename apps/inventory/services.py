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

from apps.core import system_settings
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


class CostableMovementError(Exception):
    """El movimiento indicado no admite carga de costo de compra."""


def pending_cost_movements(queryset=None):
    """Entradas por compra a las que aún no se les cargó el costo de la factura.

    Es la bandeja de trabajo de la gerencia (la mercancía llega antes que la factura del
    proveedor). Definición única, compartida por el listado de inventario y el panel de
    inicio, para que ambos cuenten exactamente lo mismo.

    Excluye lo anterior a la **fecha de puesta en marcha** (`SystemSettings.go_live_date`):
    los movimientos de la carga histórica inicial nunca tuvieron factura asociada, así que
    pedir que se costeen sería ruido. Sin fecha configurada no se filtra nada.
    """
    qs = queryset if queryset is not None else InventoryMovement.objects.all()
    qs = qs.filter(
        movement_type=InventoryMovement.MovementTypeChoices.ENTRY,
        quantity__gt=0,
        sale__isnull=True,
        unit_cost_usd__isnull=True,
    )
    go_live = system_settings.go_live_date()
    if go_live:
        qs = qs.filter(movement_date__gte=go_live)
    return qs


def replay_average_cost(product):
    """Recalcula el costo promedio del producto recorriendo su historial de movimientos.

    El inventario es *append-only*: los movimientos son la fuente de verdad, así que el
    promedio siempre se puede reconstruir a partir de ellos. Se recorre el historial en
    orden y se aplica la misma regla que `apply_movement` (solo las entradas con costo
    conocido mueven el promedio); las entradas sin costo cargado se ignoran, igual que
    cuando se registraron.

    Se usa al **cargar la factura de una entrada ya registrada**: como el costo llega
    después, hay que rehacer el cálculo desde el historial en lugar de aplicarlo sobre el
    promedio actual (que ya incorpora movimientos posteriores). Es idempotente: volver a
    ejecutarlo sobre los mismos datos da el mismo resultado, y corregir un costo mal
    cargado basta para que el promedio se reconstruya bien.
    """
    movements = (
        InventoryMovement.objects.filter(product=product)
        .order_by("movement_date", "created_at", "id")
        .values_list("quantity", "unit_cost_usd")
    )
    # El promedio arranca en el precio de compra de referencia del producto (lo mismo
    # que hace `Product.save()` la primera vez).
    average = product.purchase_price_usd
    stock = 0
    for quantity, unit_cost in movements:
        if quantity > 0 and unit_cost is not None:
            average = _recompute_average_cost(
                prev_stock=stock, prev_avg=average, quantity=quantity, entry_cost=unit_cost
            )
        stock += quantity
    return _money(average) if average is not None else None


@transaction.atomic
def set_movement_cost(*, movement, unit_cost, reference=None, notes=None, invoice_file=None):
    """Carga el costo de compra de una entrada **ya registrada** (llegó la factura).

    En la operación real la mercancía entra al almacén antes que la factura del
    proveedor: el encargado de almacén registra la entrada (cantidad) y el costo se
    conoce días después. Esta función es el segundo paso de ese flujo —la gerencia
    (que administra las facturas) carga el costo unitario— y deja el promedio ponderado
    del producto como si el costo se hubiera conocido desde el principio, recalculándolo
    desde el historial (`replay_average_cost`).

    El respaldo documental va junto al costo: `reference` guarda el nº de factura del
    proveedor e `invoice_file` (opcional) el **archivo escaneado** (PDF o imagen). Si no se
    envía archivo, se conserva el que el movimiento ya tuviera.

    Solo aplica a **entradas por compra/reposición** (`ENT`): son las que responden a una
    factura de proveedor. Las devoluciones y los ajustes reingresan mercancía que ya se
    había costeado, así que no se costean aquí. Devuelve
    `(movimiento, promedio_antes, promedio_después)`.

    **Alcance de la corrección:** el promedio se corrige de aquí en adelante. Las ventas
    que ya se registraron conservan el costo (CMV) que se les aplicó en su momento —no
    se reescriben utilidades ni comisiones ya reportadas—; por eso conviene cargar la
    factura antes de vender la mercancía recibida.
    """
    if movement.movement_type != InventoryMovement.MovementTypeChoices.ENTRY or movement.sale_id is not None:
        raise CostableMovementError(
            "Solo se puede cargar el costo de una entrada por compra o reposición: las "
            "salidas por venta se costean con el promedio vigente, y las devoluciones y "
            "ajustes reingresan mercancía ya costeada."
        )
    if movement.quantity <= 0:
        raise CostableMovementError("La entrada debe sumar existencias.")

    unit_cost = _money(unit_cost)
    if unit_cost <= 0:
        raise CostableMovementError("El costo unitario debe ser mayor que cero.")

    # Bloquea el producto para que un movimiento simultáneo no pise el recálculo.
    product = Product.objects.select_for_update().order_by().get(pk=movement.product_id)
    average_before = product.average_cost_usd

    movement.unit_cost_usd = unit_cost
    update_fields = ["unit_cost_usd"]
    if reference is not None:
        movement.reference = reference
        update_fields.append("reference")
    if notes is not None:
        movement.notes = notes
        update_fields.append("notes")
    # El adjunto solo se toca si viene uno nuevo: al corregir un costo sin volver a
    # subir el archivo, la factura ya cargada se conserva.
    if invoice_file is not None:
        movement.cost_invoice_file = invoice_file
        update_fields.append("cost_invoice_file")
    movement.save(update_fields=update_fields)

    product.average_cost_usd = replay_average_cost(product)
    product.save(update_fields=["average_cost_usd", "updated_at"])
    movement.product = product

    return movement, average_before, product.average_cost_usd


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
    elif movement_type == InventoryMovement.MovementTypeChoices.ENTRY:
        # Entrada de compra SIN costo: se deja en NULL a propósito. La mercancía llegó
        # pero la factura del proveedor todavía no; sellarle el promedio vigente fingiría
        # un costo de compra que nadie registró y la entrada nunca aparecería como
        # pendiente. Queda a la espera de que la gerencia cargue el costo real
        # (`set_movement_cost`), que entonces recalcula el promedio.
        movement_unit_cost = None
    elif avg_before is not None:
        # Salidas (y devoluciones/ajustes que no son compras) sin costo explícito: se
        # sella el promedio vigente para dejar rastro del CMV aplicado.
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

        from apps.analytics.alerts import resolve_alert, upsert_alert
        from apps.analytics.models import Alert

        dedupe_key = f"stock_break:{product.id}"
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
            upsert_alert(
                alert_type=Alert.TypeChoices.STOCK_BREAK,
                dedupe_key=dedupe_key,
                title=f"Stock bajo: {product.name}",
                message=message,
                severity=severity,
            )
        else:
            # Recuperó nivel: resuelve las alertas abiertas de ese producto.
            resolve_alert(dedupe_key)
    except Exception:  # noqa: BLE001 — auditar/alertar nunca debe romper la operación
        import logging

        logging.getLogger("apps").warning("No se pudo sincronizar la alerta de stock bajo", exc_info=True)

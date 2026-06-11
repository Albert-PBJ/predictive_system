"""Lógica de negocio de ventas.

Registrar una venta no es solo crear una fila: hay que validar stock, calcular
subtotales/utilidad/comisión, fijar las tasas de cambio vigentes y descontar el
inventario dejando su rastro de auditoría. Todo eso ocurre dentro de una única
transacción atómica, de modo que una venta nunca queda a medias (p. ej. con
stock descontado pero sin línea registrada, o viceversa).
"""

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction

from apps.core.models import ExchangeRate, Product
from apps.inventory.models import InventoryMovement
from apps.inventory.services import InsufficientStockError, apply_movement

from .models import Sale, SaleItem

CENTS = Decimal("0.01")


class SaleValidationError(Exception):
    """Error de negocio al registrar o anular una venta (se traduce a HTTP 400)."""


def _latest_rate():
    """Última tasa de cambio cargada (la más reciente por fecha)."""
    return ExchangeRate.objects.order_by("-date").first()


def _effective_rate(rate):
    """Tasa preferida para convertir USD→VES: la paralela; si no, la BCV."""
    if not rate:
        return None
    return rate.parallel_rate or rate.bcv_rate


def _money(value):
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


@transaction.atomic
def create_sale(
    *,
    seller,
    customer,
    items,
    user,
    sale_date=None,
    sale_type=Sale.TypeChoices.RETAIL,
    status=Sale.StatusChoices.COMPLETED,
    notes="",
):
    """Crea una venta con sus líneas y descuenta el inventario, todo atómicamente.

    `items` es una lista de dicts: ``{"product": <id>, "quantity": <int>,
    "unit_sale_price_usd": <Decimal|None>}``. Si no se indica el precio unitario,
    se toma el precio de venta actual del producto. El costo unitario siempre se
    fija (snapshot) desde el precio de compra del producto al momento de la venta.

    Lanza `SaleValidationError` ante datos de negocio inválidos (sin líneas, stock
    insuficiente, producto inexistente), revirtiendo cualquier cambio parcial.
    """
    if not items:
        raise SaleValidationError("La venta debe tener al menos una línea de producto.")

    sale_date = sale_date or date.today()

    # Bloquea las filas de los productos involucrados para que la validación de
    # stock y el posterior descuento sean consistentes frente a ventas simultáneas.
    product_ids = [it["product"] for it in items]
    # `.order_by()` quita el ordenamiento por defecto del modelo (por `category`,
    # que es FK anulable): con SELECT ... FOR UPDATE, PostgreSQL no admite el
    # OUTER JOIN que ese orden introduciría.
    products = {
        p.id: p
        for p in Product.objects.select_for_update().filter(id__in=product_ids).order_by()
    }

    # Valida existencia/estado y acumula la cantidad pedida por producto (para
    # detectar líneas repetidas que en conjunto excedan el stock disponible). Los
    # **servicios** (p. ej. Mantenimiento) no llevan inventario: no se acumulan aquí,
    # así no se validan contra stock ni lo descuentan más abajo.
    requested = {}
    for it in items:
        pid = it["product"]
        product = products.get(pid)
        if product is None:
            raise SaleValidationError(f"El producto con id {pid} no existe.")
        if not product.is_active:
            raise SaleValidationError(f"El producto '{product.name}' está inactivo y no puede venderse.")
        qty = it["quantity"]
        if qty < 1:
            raise SaleValidationError(f"La cantidad de '{product.name}' debe ser al menos 1.")
        if not product.is_service:
            requested[pid] = requested.get(pid, 0) + qty

    discounts_stock = status != Sale.StatusChoices.CANCELLED
    if discounts_stock:
        for pid, qty in requested.items():
            product = products[pid]
            if product.stock < qty:
                raise SaleValidationError(
                    f"Stock insuficiente para '{product.name}': "
                    f"disponible {product.stock}, solicitado {qty}."
                )

    rate = _latest_rate()
    eff_rate = _effective_rate(rate)

    sale = Sale.objects.create(
        customer=customer,
        seller=seller,
        sale_date=sale_date,
        sale_type=sale_type,
        status=status,
        notes=notes,
        bcv_rate=rate.bcv_rate if rate else None,
        parallel_rate=rate.parallel_rate if rate else None,
    )

    total_sale = Decimal("0")
    total_cost = Decimal("0")
    total_discount = Decimal("0")

    for it in items:
        product = products[it["product"]]
        qty = it["quantity"]
        # El precio de lista (snapshot del catálogo) es la referencia del descuento.
        list_price = _money(product.sale_price_usd or 0)
        # La línea puede traer un % de descuento o, en su defecto, un precio neto
        # explícito; se mantienen consistentes los tres valores (lista, %, neto).
        disc_pct = it.get("discount_pct")
        unit_sale_in = it.get("unit_sale_price_usd")
        if disc_pct is not None:
            disc_pct = Decimal(str(disc_pct))
            unit_sale = _money(list_price * (Decimal("1") - disc_pct / Decimal("100")))
        elif unit_sale_in is not None:
            unit_sale = _money(unit_sale_in)
            disc_pct = ((Decimal("1") - unit_sale / list_price) * Decimal("100")) if list_price > 0 else Decimal("0")
        else:
            unit_sale = list_price
            disc_pct = Decimal("0")
        disc_pct = max(Decimal("0"), disc_pct).quantize(CENTS, rounding=ROUND_HALF_UP)
        unit_cost = _money(product.purchase_price_usd or 0)
        subtotal_sale = _money(unit_sale * qty)
        subtotal_cost = _money(unit_cost * qty)
        line_profit = subtotal_sale - subtotal_cost
        line_discount = max(Decimal("0"), _money((list_price - unit_sale) * qty))

        SaleItem.objects.create(
            sale=sale,
            product=product,
            quantity=qty,
            unit_list_price_usd=list_price,
            discount_pct=disc_pct,
            unit_sale_price_usd=unit_sale,
            unit_cost_price_usd=unit_cost,
            subtotal_sale_usd=subtotal_sale,
            subtotal_cost_usd=subtotal_cost,
            line_profit_usd=line_profit,
        )

        total_sale += subtotal_sale
        total_cost += subtotal_cost
        total_discount += line_discount

        if discounts_stock and not product.is_service:
            # Salida de inventario (append-only) ligada a esta venta. Los servicios no
            # tienen inventario, así que no generan movimiento.
            apply_movement(
                product=product,
                movement_type=InventoryMovement.MovementTypeChoices.EXIT,
                quantity=-qty,
                responsible=user,
                sale=sale,
                reference=f"Venta #{sale.pk}",
                movement_date=sale_date,
            )

    total_profit = total_sale - total_cost
    commission_rate = seller.commission_rate or Decimal("0")
    commission = _money(total_profit * commission_rate / Decimal("100"))

    sale.total_sale_usd = total_sale
    sale.total_cost_usd = total_cost
    sale.total_profit_usd = total_profit
    sale.total_discount_usd = total_discount
    sale.commission_usd = commission
    sale.total_sale_ves = _money(total_sale * eff_rate) if eff_rate else None
    sale.save(
        update_fields=[
            "total_sale_usd",
            "total_cost_usd",
            "total_profit_usd",
            "total_discount_usd",
            "commission_usd",
            "total_sale_ves",
            "updated_at",
        ]
    )
    return sale


@transaction.atomic
def void_sale(*, sale, user):
    """Anula una venta: devuelve el stock al inventario y marca la venta como anulada.

    Por cada línea se registra una devolución (`DEV`) que reingresa la cantidad al
    stock. Es idempotente solo en el sentido de que rechaza anular dos veces.
    """
    if sale.status == Sale.StatusChoices.CANCELLED:
        raise SaleValidationError("La venta ya está anulada.")

    for item in sale.items.select_related("product"):
        # Los servicios no llevan inventario: no hay nada que reingresar.
        if item.product.is_service:
            continue
        apply_movement(
            product=item.product,
            movement_type=InventoryMovement.MovementTypeChoices.RETURN,
            quantity=item.quantity,  # positivo: la mercancía vuelve al inventario
            responsible=user,
            sale=sale,
            reference=f"Anulación de venta #{sale.pk}",
        )

    sale.status = Sale.StatusChoices.CANCELLED
    stamp = f"[Anulada por {user.username}]"
    sale.notes = f"{sale.notes}\n{stamp}".strip() if sale.notes else stamp
    sale.save(update_fields=["status", "notes", "updated_at"])
    return sale

"""Lógica de negocio de ventas.

Registrar una venta no es solo crear una fila: hay que validar stock, calcular
subtotales/utilidad/comisión, fijar las tasas de cambio vigentes y descontar el
inventario dejando su rastro de auditoría. Todo eso ocurre dentro de una única
transacción atómica, de modo que una venta nunca queda a medias (p. ej. con
stock descontado pero sin línea registrada, o viceversa).
"""

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.db import IntegrityError, transaction

from apps.core.models import ExchangeRate, Product
from apps.inventory.models import InventoryMovement
from apps.inventory.services import InsufficientStockError, apply_movement

from .models import (
    DispatchOrder,
    DispatchOrderItem,
    Quote,
    QuoteItem,
    Sale,
    SaleItem,
    SalePayment,
)

CENTS = Decimal("0.01")


class SaleValidationError(Exception):
    """Error de negocio al registrar o anular una venta (se traduce a HTTP 400)."""


class QuoteValidationError(Exception):
    """Error de negocio al crear un presupuesto (se traduce a HTTP 400)."""


def _latest_rate():
    """Última tasa de cambio cargada (la más reciente por fecha)."""
    return ExchangeRate.objects.order_by("-date").first()


def _effective_rate(rate):
    """Tasa para convertir USD→VES según la base elegida en la configuración
    (paralela por defecto; también BCV o promedio). Si no hay tasa, None."""
    from apps.core import system_settings

    return system_settings.effective_rate(rate)


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
    iva_rate=None,
    installation_cost=None,
    delivery_cost=None,
    quote=None,
    amount_paid=None,
    payment_method=None,
):
    """Crea una venta con sus líneas y descuenta el inventario, todo atómicamente.

    `items` es una lista de dicts: ``{"product": <id>, "quantity": <int>,
    "unit_sale_price_usd": <Decimal|None>}``. Si no se indica el precio unitario,
    se toma el precio de venta actual del producto. El costo unitario (CMV) se fija
    (snapshot) desde el **costo promedio ponderado móvil** vigente del producto
    (`average_cost_usd`); si aún no hay promedio calculado, cae al precio de compra.

    **Cobranza / pago inicial.** `amount_paid` controla el estado:

    - ``None`` (por defecto): la venta se considera **pagada por completo** si
      ``status`` es COMP (lo habitual), o **sin abonar** si se pasó PEN. Esto
      preserva el comportamiento previo para quienes no manejan cobranza (conversión
      de presupuesto, importación de Excel).
    - un monto: se registra ese abono inicial; el estado se **deriva** del saldo —
      COMP si cubre el total con IVA, PEN si queda saldo. El monto se acota al total.

    Si el monto abonado es > 0 se crea un ``SalePayment`` inicial (para que el libro
    de abonos quede completo desde el registro).

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

    # IVA por defecto desde la Configuración del Sistema (16%) si no se indica.
    if iva_rate is None:
        from apps.core import system_settings

        iva_rate = system_settings.default_iva_pct()
    iva_rate = Decimal(str(iva_rate))

    # Cargos adicionales (instalación / despacho-flete): opcionales, nunca negativos.
    installation_cost = _money(max(Decimal("0"), Decimal(str(installation_cost or 0))))
    delivery_cost = _money(max(Decimal("0"), Decimal(str(delivery_cost or 0))))

    sale = Sale.objects.create(
        customer=customer,
        seller=seller,
        sale_date=sale_date,
        sale_type=sale_type,
        status=status,
        notes=notes,
        iva_rate=iva_rate,
        installation_cost_usd=installation_cost,
        delivery_cost_usd=delivery_cost,
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
        # Costo de venta (CMV) por costo promedio ponderado móvil: se toma el promedio
        # vigente del producto; si aún no se ha calculado, cae al precio de compra.
        cost_basis = product.average_cost_usd if product.average_cost_usd is not None else product.purchase_price_usd
        unit_cost = _money(cost_basis or 0)
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
                unit_cost=unit_cost,  # CMV aplicado (no altera el promedio, solo deja rastro)
            )

    total_profit = total_sale - total_cost
    commission_rate = seller.commission_rate or Decimal("0")
    commission = _money(total_profit * commission_rate / Decimal("100"))

    # Base imponible = productos + cargos de instalación/despacho. El IVA se calcula
    # sobre esa base y el total a pagar es base + IVA. La analítica sigue leyendo
    # `total_sale_usd` (solo productos), intacta: los cargos no la tocan.
    taxable_base = total_sale + installation_cost + delivery_cost
    iva_amount = _money(taxable_base * iva_rate / Decimal("100"))
    total_with_iva = taxable_base + iva_amount

    # Cobranza: determina el monto abonado y el estado según el pago. Sin `amount_paid`
    # se respeta el `status` recibido (COMP = pagada; PEN = sin abonar) por
    # compatibilidad; con un monto, el estado se deriva del saldo.
    if amount_paid is None:
        paid = Decimal("0") if status == Sale.StatusChoices.PENDING else total_with_iva
    else:
        paid = _money(max(Decimal("0"), min(Decimal(str(amount_paid)), total_with_iva)))
        status = (
            Sale.StatusChoices.COMPLETED if paid >= total_with_iva else Sale.StatusChoices.PENDING
        )

    sale.status = status
    sale.amount_paid_usd = paid
    sale.total_sale_usd = total_sale
    sale.total_cost_usd = total_cost
    sale.total_profit_usd = total_profit
    sale.total_discount_usd = total_discount
    sale.commission_usd = commission
    sale.iva_amount_usd = iva_amount
    sale.total_with_iva_usd = total_with_iva
    sale.total_sale_ves = _money(total_sale * eff_rate) if eff_rate else None
    sale.total_with_iva_ves = _money(total_with_iva * eff_rate) if eff_rate else None
    sale.save(
        update_fields=[
            "status",
            "amount_paid_usd",
            "total_sale_usd",
            "total_cost_usd",
            "total_profit_usd",
            "total_discount_usd",
            "commission_usd",
            "iva_amount_usd",
            "total_with_iva_usd",
            "total_sale_ves",
            "total_with_iva_ves",
            "updated_at",
        ]
    )

    # Abono inicial: si se cobró algo al registrar, queda asentado en el libro de
    # abonos (para que la suma de `SalePayment` coincida con `amount_paid_usd`).
    if paid > 0:
        SalePayment.objects.create(
            sale=sale,
            amount_usd=paid,
            amount_ves=_money(paid * eff_rate) if eff_rate else None,
            method=payment_method or SalePayment.MethodChoices.OTHER,
            payment_date=sale_date,
            notes="Pago completo" if paid >= total_with_iva else "Pago inicial",
            recorded_by=user,
        )

    # Si la venta se registró a partir de un presupuesto, se cierra la relación
    # cotización→venta (mismo enlace que la acción "Convertir a venta").
    if quote is not None:
        _link_quote_to_sale(quote, sale)

    # Venta con despacho incluido → avisa a almacén que debe generar la orden de
    # despacho (notificación en la campana del encargado de inventario). Best-effort.
    if delivery_cost > 0 and status != Sale.StatusChoices.CANCELLED:
        from apps.analytics.alerts import notify_dispatch_needed

        notify_dispatch_needed(sale)
    return sale


def _link_quote_to_sale(quote, sale):
    """Enlaza un presupuesto con la venta generada y lo marca como convertido.

    Punto único usado por ``create_sale`` (registro con presupuesto relacionado) y por
    ``convert_quote_to_sale`` (conversión desde el propio presupuesto). Valida que el
    presupuesto no esté ya convertido a otra venta y que pertenezca al mismo cliente.
    """
    if (
        quote.status == Quote.StatusChoices.CONVERTED or quote.converted_to_sale_id
    ) and quote.converted_to_sale_id != sale.pk:
        ref = f" a la venta #{quote.converted_to_sale_id}" if quote.converted_to_sale_id else ""
        raise SaleValidationError(
            f"El presupuesto {quote.quote_number} ya está convertido{ref} y no puede relacionarse de nuevo."
        )
    if quote.customer_id != sale.customer_id:
        raise SaleValidationError(
            f"El presupuesto {quote.quote_number} pertenece a otro cliente y no puede relacionarse con esta venta."
        )
    quote.converted_to_sale = sale
    quote.status = Quote.StatusChoices.CONVERTED
    quote.save(update_fields=["converted_to_sale", "status", "updated_at"])


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

    # Ya no hay nada que despachar: resuelve la alerta de despacho pendiente si existía.
    from apps.analytics.alerts import resolve_dispatch_needed

    resolve_dispatch_needed(sale.pk)
    return sale


def sales_pending_dispatch(*, reference_date=None):
    """Ventas del **mes actual** con despacho incluido y **sin** orden de despacho.

    Una venta "con despacho incluido" es la que lleva un cargo de despacho/flete
    (`delivery_cost_usd > 0`). Se consideran pendientes las no anuladas que aún no
    tienen ninguna orden de despacho **activa** (se ignoran las órdenes anuladas).
    Acotada al mes calendario de ``reference_date`` (por defecto, hoy). Devuelve un
    queryset ordenado de la más reciente a la más antigua.
    """
    from django.db.models import Count, Q
    from django.utils import timezone

    ref = reference_date or timezone.localdate()
    first = ref.replace(day=1)
    nxt = (first.replace(year=first.year + 1, month=1)
           if first.month == 12 else first.replace(month=first.month + 1))

    return (
        Sale.objects.filter(
            delivery_cost_usd__gt=0, sale_date__gte=first, sale_date__lt=nxt
        )
        .exclude(status=Sale.StatusChoices.CANCELLED)
        .annotate(
            active_dispatch=Count(
                "dispatch_orders",
                filter=~Q(dispatch_orders__status=DispatchOrder.StatusChoices.CANCELLED),
            )
        )
        .filter(active_dispatch=0)
        .select_related("customer", "seller", "seller__user__profile")
        .prefetch_related("items")
        .order_by("-sale_date", "-created_at")
    )


def pending_dispatch_rows(queryset, *, limit=None):
    """Serializa a dicts livianos las ventas pendientes de despacho (para la UI).

    Fuente única de la fila que consumen tanto el panel del encargado de inventario
    como el endpoint del módulo de despachos, para que ambas tablas coincidan.
    """
    from .serializers import _seller_display_name

    rows = queryset[:limit] if limit else queryset
    return [
        {
            "id": s.id,
            "sale_date": s.sale_date.isoformat(),
            "customer_name": s.customer.company_name if s.customer_id else "—",
            "seller_name": _seller_display_name(s.seller),
            "delivery_cost_usd": str(s.delivery_cost_usd),
            "installation_cost_usd": str(s.installation_cost_usd),
            "total_with_iva_usd": str(s.total_with_iva_usd),
            "items": len(s.items.all()),  # `items` viene prefetch (sin N+1)
        }
        for s in rows
    ]


# --------------------------------------------------------------------------- #
# Facturación fiscal
# --------------------------------------------------------------------------- #
# En Venezuela el número de factura y el número de control provienen del bloc/
# máquina fiscal autorizada por el SENIAT: los captura el usuario. El sistema solo
# sugiere el siguiente correlativo (a partir de los ya registrados) y valida que sean
# únicos. Facturar es un paso OPCIONAL y posterior al registro de la venta.


def _next_correlative(values, pad: int = 8) -> str:
    """Siguiente número correlativo a partir de una lista de valores ya usados.

    Toma los dígitos de cada valor, halla el máximo y suma 1, con relleno de ceros.
    Ignora prefijos/sufijos no numéricos. Si no hay ninguno, arranca en 1.
    """
    import re

    max_n = 0
    for v in values:
        if not v:
            continue
        digits = re.sub(r"\D", "", str(v))
        if digits:
            max_n = max(max_n, int(digits))
    return str(max_n + 1).zfill(pad)


def suggest_invoice_numbers() -> dict:
    """Sugiere el siguiente número de factura y de control (correlativos)."""
    invoices = Sale.objects.exclude(invoice_number__isnull=True).values_list(
        "invoice_number", flat=True
    )
    controls = Sale.objects.exclude(control_number__isnull=True).values_list(
        "control_number", flat=True
    )
    return {
        "invoice_number": _next_correlative(invoices),
        "control_number": _next_correlative(controls),
    }


@transaction.atomic
def invoice_sale(*, sale, invoice_number, control_number, user, invoice_date=None, invoice_file=None):
    """Asocia los datos fiscales (factura, control, adjunto) a una venta ya registrada.

    Valida que la venta no esté anulada y que los números no colisionen con otra
    venta. `invoice_file` es opcional (PDF o imagen). Lanza `SaleValidationError`
    ante datos inválidos. Es idempotente en el sentido de que permite corregir los
    datos de una venta ya facturada (se sobrescriben).
    """
    if sale.status == Sale.StatusChoices.CANCELLED:
        raise SaleValidationError("No se puede facturar una venta anulada.")

    invoice_number = (invoice_number or "").strip()
    control_number = (control_number or "").strip()
    if not invoice_number:
        raise SaleValidationError("El número de factura es obligatorio.")
    if not control_number:
        raise SaleValidationError("El número de control es obligatorio.")

    clash = Sale.objects.filter(invoice_number=invoice_number).exclude(pk=sale.pk).exists()
    if clash:
        raise SaleValidationError(f"El número de factura '{invoice_number}' ya está en uso en otra venta.")
    clash = Sale.objects.filter(control_number=control_number).exclude(pk=sale.pk).exists()
    if clash:
        raise SaleValidationError(f"El número de control '{control_number}' ya está en uso en otra venta.")

    sale.invoice_number = invoice_number
    sale.control_number = control_number
    sale.invoice_date = invoice_date or sale.sale_date
    if invoice_file is not None:
        sale.invoice_file = invoice_file
    sale.save(update_fields=["invoice_number", "control_number", "invoice_date", "invoice_file", "updated_at"])
    return sale


# --------------------------------------------------------------------------- #
# Cobranza / abonos (pagos parciales)
# --------------------------------------------------------------------------- #
# Una venta puede cobrarse en varias parcialidades. Cada abono se asienta en el libro
# `SalePayment` y suma a `Sale.amount_paid_usd`; el saldo pendiente = total con IVA −
# abonado. Al saldarse el total, la venta pasa a "Completada" automáticamente.


@transaction.atomic
def add_sale_payment(*, sale, amount, user, method=None, payment_date=None, reference="", notes=""):
    """Registra un abono a una venta y actualiza su cobranza.

    Valida que la venta no esté anulada, que el monto sea positivo y que no supere el
    saldo pendiente. Asienta el ``SalePayment``, incrementa ``amount_paid_usd`` y, si
    el abono salda el total, marca la venta como **Completada** (autocompletado).
    Devuelve ``(sale, payment)``. Lanza ``SaleValidationError`` ante datos inválidos.
    """
    if sale.status == Sale.StatusChoices.CANCELLED:
        raise SaleValidationError("No se puede registrar un abono en una venta anulada.")

    amount = _money(amount)
    if amount <= 0:
        raise SaleValidationError("El monto del abono debe ser mayor que cero.")

    total = sale.total_with_iva_usd or Decimal("0")
    balance = _money(total - (sale.amount_paid_usd or Decimal("0")))
    if balance <= 0:
        raise SaleValidationError("La venta ya está totalmente pagada.")
    if amount > balance:
        raise SaleValidationError(
            f"El abono ({amount} USD) supera el saldo pendiente ({balance} USD)."
        )

    rate = _latest_rate()
    eff_rate = _effective_rate(rate)

    payment = SalePayment.objects.create(
        sale=sale,
        amount_usd=amount,
        amount_ves=_money(amount * eff_rate) if eff_rate else None,
        method=method or SalePayment.MethodChoices.OTHER,
        payment_date=payment_date or date.today(),
        reference=reference or "",
        notes=notes or "",
        recorded_by=user,
    )

    sale.amount_paid_usd = _money((sale.amount_paid_usd or Decimal("0")) + amount)
    update_fields = ["amount_paid_usd", "updated_at"]
    # Autocompletar al saldar el total (solo desde "Pendiente").
    if sale.amount_paid_usd >= total and sale.status == Sale.StatusChoices.PENDING:
        sale.status = Sale.StatusChoices.COMPLETED
        update_fields.append("status")
    sale.save(update_fields=update_fields)
    return sale, payment


def update_sale_notes(*, sale, notes):
    """Actualiza las notas/observaciones de una venta (edición puntual)."""
    sale.notes = notes or ""
    sale.save(update_fields=["notes", "updated_at"])
    return sale


# --------------------------------------------------------------------------- #
# Presupuestos (cotizaciones)
# --------------------------------------------------------------------------- #
# A diferencia de una venta, un presupuesto NO toca el inventario (no descuenta
# stock) ni calcula utilidad/comisión: es una oferta de precios. Lleva IVA (16% por
# defecto) y un número correlativo legible por día (DDMMYYYY-N).


def _next_quote_number(issued_date, offset: int = 0) -> str:
    """Número de presupuesto correlativo del día: ``DDMMYYYY-N``.

    Calcula el siguiente N a partir de los ya emitidos ese día. ``offset`` permite
    saltar al siguiente ante una colisión de unicidad (reintento concurrente).
    """
    prefix = issued_date.strftime("%d%m%Y")
    existing = Quote.objects.filter(quote_number__startswith=f"{prefix}-").values_list(
        "quote_number", flat=True
    )
    max_n = 0
    for qn in existing:
        try:
            max_n = max(max_n, int(qn.rsplit("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return f"{prefix}-{max_n + 1 + offset}"


def create_quote(
    *,
    seller,
    customer,
    items,
    issued_date=None,
    expiry_date=None,
    iva_rate=None,
    installation_cost=None,
    delivery_cost=None,
    status=Quote.StatusChoices.DRAFT,
):
    """Crea un presupuesto con sus líneas (sin tocar inventario).

    ``items`` es una lista de dicts ``{"product": <id>, "quantity": <int>,
    "unit_price_usd": <Decimal|None>}``; si no se indica el precio unitario, se toma
    el precio de venta actual del producto. El IVA y la vigencia, si no se pasan, se
    toman de la Configuración del Sistema (``default_iva_pct`` /
    ``default_quote_expiry_days``). Calcula subtotal, IVA y total (USD + VES según la
    última tasa), asigna un número correlativo único y persiste todo de forma
    atómica. Lanza ``QuoteValidationError`` ante datos inválidos.
    """
    from datetime import timedelta

    from apps.core import system_settings

    if not items:
        raise QuoteValidationError("El presupuesto debe tener al menos una línea de producto.")

    issued_date = issued_date or date.today()
    if iva_rate is None:
        iva_rate = system_settings.default_iva_pct()
    # Vigencia por defecto: si no se indica vencimiento, se calcula a partir de la
    # configuración (issued + N días). N=0 deja el presupuesto sin vencimiento.
    if expiry_date is None:
        days = system_settings.default_quote_expiry_days()
        if days and days > 0:
            expiry_date = issued_date + timedelta(days=days)

    product_ids = [it["product"] for it in items]
    products = {p.id: p for p in Product.objects.filter(id__in=product_ids)}
    for it in items:
        product = products.get(it["product"])
        if product is None:
            raise QuoteValidationError(f"El producto con id {it['product']} no existe.")
        if not product.is_active:
            raise QuoteValidationError(f"El producto '{product.name}' está inactivo y no puede cotizarse.")
        if it["quantity"] < 1:
            raise QuoteValidationError(f"La cantidad de '{product.name}' debe ser al menos 1.")

    rate = _latest_rate()
    eff_rate = _effective_rate(rate)

    # Datos de cada línea (precio unitario flexible: el del producto o el enviado).
    lines = []
    subtotal = Decimal("0")
    for it in items:
        product = products[it["product"]]
        qty = it["quantity"]
        unit_in = it.get("unit_price_usd")
        unit = _money(unit_in if unit_in is not None else (product.sale_price_usd or 0))
        line_total = _money(unit * qty)
        lines.append({
            "product": product,
            "quantity": qty,
            "unit_price_usd": unit,
            "unit_price_ves": _money(unit * eff_rate) if eff_rate else None,
            "line_total_usd": line_total,
            "line_total_ves": _money(line_total * eff_rate) if eff_rate else None,
        })
        subtotal += line_total

    # Cargos adicionales (instalación / despacho-flete): opcionales, nunca negativos.
    # Se suman a la base imponible; los booleanos `includes_*` se derivan del costo > 0.
    installation_cost = _money(max(Decimal("0"), Decimal(str(installation_cost or 0))))
    delivery_cost = _money(max(Decimal("0"), Decimal(str(delivery_cost or 0))))

    iva_rate = Decimal(str(iva_rate))
    taxable_base = subtotal + installation_cost + delivery_cost
    iva_amount = _money(taxable_base * iva_rate / Decimal("100"))
    total = taxable_base + iva_amount

    quote_fields = dict(
        customer=customer,
        seller=seller,
        issued_date=issued_date,
        expiry_date=expiry_date,
        bcv_rate=rate.bcv_rate if rate else None,
        parallel_rate=rate.parallel_rate if rate else None,
        includes_installation=installation_cost > 0,
        includes_delivery=delivery_cost > 0,
        installation_cost_usd=installation_cost,
        delivery_cost_usd=delivery_cost,
        subtotal_usd=subtotal,
        subtotal_ves=_money(subtotal * eff_rate) if eff_rate else None,
        iva_rate=iva_rate,
        iva_amount_usd=iva_amount,
        total_usd=total,
        total_ves=_money(total * eff_rate) if eff_rate else None,
        status=status,
    )

    # El número correlativo es único; ante una colisión por concurrencia se reintenta
    # con el siguiente N (cada intento en su propia transacción).
    for attempt in range(6):
        number = _next_quote_number(issued_date, attempt)
        try:
            with transaction.atomic():
                quote = Quote.objects.create(quote_number=number, **quote_fields)
                QuoteItem.objects.bulk_create([
                    QuoteItem(
                        quote=quote,
                        product=l["product"],
                        quantity=l["quantity"],
                        unit_price_usd=l["unit_price_usd"],
                        unit_price_ves=l["unit_price_ves"],
                        line_total_usd=l["line_total_usd"],
                        line_total_ves=l["line_total_ves"],
                    )
                    for l in lines
                ])
            return quote
        except IntegrityError:
            continue
    raise QuoteValidationError("No se pudo generar un número de presupuesto único. Intenta de nuevo.")


@transaction.atomic
def convert_quote_to_sale(
    *,
    quote,
    user,
    seller=None,
    sale_date=None,
    sale_type=None,
    status=Sale.StatusChoices.COMPLETED,
):
    """Convierte un presupuesto aprobado en una venta real (descuenta inventario).

    Reutiliza ``create_sale`` con las líneas del presupuesto (cada línea conserva su
    precio unitario negociado). Marca el presupuesto como ``CONVERTED`` y lo enlaza a
    la venta generada (``converted_to_sale``), cerrando la relación cotización→venta.
    Lanza ``QuoteValidationError`` si ya fue convertido o no tiene líneas; los errores
    de stock de ``create_sale`` (``SaleValidationError``) se propagan.
    """
    # Ya convertido: por el FK a la venta o por el estado CONVERTED (los presupuestos
    # sembrados se etiquetan CONVERTED sin una venta enlazada — no se deben reconvertir).
    if quote.status == Quote.StatusChoices.CONVERTED or quote.converted_to_sale_id:
        ref = f" a la venta #{quote.converted_to_sale_id}" if quote.converted_to_sale_id else ""
        raise QuoteValidationError(
            f"El presupuesto {quote.quote_number} ya está convertido{ref}."
        )
    if quote.status == Quote.StatusChoices.REJECTED:
        raise QuoteValidationError("No se puede convertir un presupuesto rechazado.")

    seller = seller or quote.seller
    if seller is None:
        raise QuoteValidationError(
            "El presupuesto no tiene un vendedor asociado; no se puede convertir en venta. "
            "Asigna un vendedor o conviértelo desde una cuenta con perfil de vendedor."
        )

    items = [
        {"product": qi.product_id, "quantity": qi.quantity, "unit_sale_price_usd": qi.unit_price_usd}
        for qi in quote.items.all()
    ]
    if not items:
        raise QuoteValidationError("El presupuesto no tiene líneas para convertir.")

    sale = create_sale(
        seller=seller,
        customer=quote.customer,
        items=items,
        user=user,
        sale_date=sale_date,
        sale_type=sale_type or Sale.TypeChoices.INSTITUTIONAL,
        status=status,
        iva_rate=quote.iva_rate,
        # La venta hereda los cargos de instalación/despacho del presupuesto.
        installation_cost=quote.installation_cost_usd,
        delivery_cost=quote.delivery_cost_usd,
        notes=f"Generada a partir del presupuesto {quote.quote_number}.",
        quote=quote,  # cierra la relación cotización→venta (vía _link_quote_to_sale)
    )
    return sale


# --------------------------------------------------------------------------- #
# Órdenes de despacho
# --------------------------------------------------------------------------- #
# Una orden de despacho es un documento de control de entrega. NO mueve inventario
# (el stock ya se descontó al vender): solo lista la mercancía a entregar, con un
# número correlativo propio (OD-DDMMYYYY-N) y un estado (pendiente → despachada →
# entregada). Se puede imprimir para las firmas físicas de almacén y despacho.


class DispatchValidationError(Exception):
    """Error de negocio al crear/actualizar una orden de despacho (HTTP 400)."""


def _next_dispatch_number(created_date, offset: int = 0) -> str:
    """Número de orden de despacho correlativo del día: ``OD-DDMMYYYY-N``."""
    prefix = "OD-" + created_date.strftime("%d%m%Y")
    existing = DispatchOrder.objects.filter(order_number__startswith=f"{prefix}-").values_list(
        "order_number", flat=True
    )
    max_n = 0
    for on in existing:
        try:
            max_n = max(max_n, int(on.rsplit("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return f"{prefix}-{max_n + 1 + offset}"


@transaction.atomic
def create_dispatch_order(
    *,
    sale,
    user,
    items=None,
    dispatch_date=None,
    delivery_address="",
    carrier="",
    notes="",
    status=DispatchOrder.StatusChoices.PENDING,
):
    """Genera una orden de despacho para una venta (sin tocar inventario).

    ``items`` es una lista opcional de ``{"product": <id>, "quantity": <int>}``; si se
    omite, se toman las líneas de productos físicos de la venta (los servicios no se
    despachan). Valida que la venta no esté anulada y que las cantidades sean válidas.
    """
    if sale.status == Sale.StatusChoices.CANCELLED:
        raise DispatchValidationError("No se puede generar una orden de despacho de una venta anulada.")

    # Por defecto, las líneas físicas de la venta (los servicios no se despachan).
    if not items:
        items = [
            {"product": si.product_id, "quantity": si.quantity}
            for si in sale.items.select_related("product")
            if not si.product.is_service
        ]
    if not items:
        raise DispatchValidationError("No hay mercancía física para despachar en esta venta.")

    normalized = []
    for it in items:
        qty = int(it.get("quantity") or 0)
        if qty < 1:
            raise DispatchValidationError("Cada línea de la orden debe tener una cantidad de al menos 1.")
        normalized.append({"product_id": it["product"], "quantity": qty})

    created_date = dispatch_date or date.today()
    fields = dict(
        sale=sale,
        status=status,
        dispatch_date=dispatch_date,
        delivery_address=delivery_address or "",
        carrier=carrier or "",
        notes=notes or "",
        created_by=user,
    )

    for attempt in range(6):
        number = _next_dispatch_number(created_date, attempt)
        try:
            with transaction.atomic():
                order = DispatchOrder.objects.create(order_number=number, **fields)
                DispatchOrderItem.objects.bulk_create([
                    DispatchOrderItem(
                        dispatch_order=order,
                        product_id=n["product_id"],
                        quantity=n["quantity"],
                    )
                    for n in normalized
                ])
            # Ya existe una orden de despacho para la venta: resuelve el aviso pendiente.
            from apps.analytics.alerts import resolve_dispatch_needed

            resolve_dispatch_needed(sale.pk)
            return order
        except IntegrityError:
            continue
    raise DispatchValidationError("No se pudo generar un número de orden único. Intenta de nuevo.")


def update_dispatch_order(*, order, status=None, dispatch_date=None, carrier=None, received_by=None, notes=None):
    """Actualiza el estado y/o los datos de entrega de una orden de despacho."""
    update_fields = ["updated_at"]
    if status is not None:
        order.status = status
        update_fields.append("status")
    if dispatch_date is not None:
        order.dispatch_date = dispatch_date
        update_fields.append("dispatch_date")
    if carrier is not None:
        order.carrier = carrier
        update_fields.append("carrier")
    if received_by is not None:
        order.received_by = received_by
        update_fields.append("received_by")
    if notes is not None:
        order.notes = notes
        update_fields.append("notes")
    order.save(update_fields=update_fields)
    return order

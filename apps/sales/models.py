from django.db import models
from django.utils.translation import gettext_lazy as _


class Sale(models.Model):
    class TypeChoices(models.TextChoices):
        RETAIL = "RET", _("Detal")
        INSTITUTIONAL = "INST", _("Proyecto Institucional")

    class StatusChoices(models.TextChoices):
        PENDING = "PEN", _("Pendiente")
        COMPLETED = "COMP", _("Completada")
        CANCELLED = "ANU", _("Anulada")

    customer = models.ForeignKey(
        "core.Customer",
        on_delete=models.PROTECT,
        related_name="sales",
        help_text=_("Cliente de la venta"),
    )
    seller = models.ForeignKey(
        "core.Seller",
        on_delete=models.PROTECT,
        related_name="sales",
        help_text=_("Vendedor responsable"),
    )
    sale_date = models.DateField(help_text=_("Fecha de la venta"))
    sale_type = models.CharField(max_length=4, choices=TypeChoices.choices, default=TypeChoices.RETAIL)

    # Totales en USD. `total_sale_usd` es la **base imponible** (suma de subtotales
    # netos, sin IVA): es la cifra de ingreso que consume la analítica. El IVA se
    # desglosa aparte (ver más abajo) y el total a pagar es `total_with_iva_usd`.
    total_sale_usd = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text=_("Base imponible de la venta en USD (sin IVA)"))
    total_cost_usd = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text=_("Total de costo en USD"))
    total_profit_usd = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text=_("Utilidad total en USD"))
    total_discount_usd = models.DecimalField(
        max_digits=12, decimal_places=2, default=0,
        help_text=_("Descuento total otorgado en USD (precio de lista − precio de venta)"),
    )

    # Desglose de IVA (impuesto al valor agregado, 16% por defecto). Se calcula sobre
    # la base imponible al registrar la venta; `total_with_iva_usd` = base + IVA.
    iva_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=16.00,
        help_text=_("Porcentaje de IVA aplicado (por defecto 16%)"),
    )
    iva_amount_usd = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text=_("Monto de IVA en USD"))
    total_with_iva_usd = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text=_("Total a pagar en USD (base + IVA)"))

    # Totales en Bs. `total_sale_ves` es la base en bolívares (para consistencia con
    # la analítica); `total_with_iva_ves` es el total a pagar en bolívares (base+IVA).
    total_sale_ves = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True, help_text=_("Base imponible de la venta en Bolívares (sin IVA)"))
    total_with_iva_ves = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True, help_text=_("Total a pagar en Bolívares (base + IVA)"))

    # Facturación fiscal. Los números y el adjunto son OPCIONALES: una venta se
    # registra sin factura y se factura después con la acción "Facturar". En
    # Venezuela el número de factura y el número de control provienen del bloc/
    # máquina fiscal autorizada por el SENIAT, así que se capturan manualmente
    # (el sistema solo sugiere el siguiente correlativo y valida que sean únicos).
    invoice_number = models.CharField(
        max_length=40, null=True, blank=True, unique=True,
        help_text=_("Número de factura fiscal correlativo (SENIAT)"),
    )
    control_number = models.CharField(
        max_length=40, null=True, blank=True, unique=True,
        help_text=_("Número de control exigido por el SENIAT"),
    )
    invoice_date = models.DateField(null=True, blank=True, help_text=_("Fecha de emisión de la factura fiscal"))
    invoice_file = models.FileField(
        upload_to="facturas/%Y/%m/", null=True, blank=True,
        help_text=_("Archivo de la factura (PDF o imagen)"),
    )

    # Comisión del vendedor
    commission_usd = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text=_("Comisión generada (% de la utilidad según tasa del vendedor)"),
    )

    # Tasas de cambio vigentes al momento de la venta
    bcv_rate = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, help_text=_("Tasa BCV al momento de la venta"))
    parallel_rate = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, help_text=_("Tasa paralela al momento de la venta"))

    status = models.CharField(max_length=4, choices=StatusChoices.choices, default=StatusChoices.COMPLETED)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sales"
        verbose_name = "Venta"
        verbose_name_plural = "Ventas"
        ordering = ["-sale_date", "-created_at"]
        indexes = [
            models.Index(fields=["sale_date"], name="sales_date_idx"),
            models.Index(fields=["customer", "sale_date"], name="sales_customer_date_idx"),
            models.Index(fields=["seller", "sale_date"], name="sales_seller_date_idx"),
            models.Index(fields=["status"], name="sales_status_idx"),
        ]

    def __str__(self):
        return f"Venta #{self.pk} — {self.customer} ({self.sale_date})"

    @property
    def is_invoiced(self) -> bool:
        """True si la venta ya tiene una factura fiscal asociada."""
        return bool(self.invoice_number)


class SaleItem(models.Model):
    sale = models.ForeignKey(
        "Sale",
        on_delete=models.CASCADE,
        related_name="items",
        help_text=_("Venta a la que pertenece esta línea"),
    )
    product = models.ForeignKey(
        "core.Product",
        on_delete=models.PROTECT,
        related_name="sale_items",
        help_text=_("Producto vendido"),
    )
    quantity = models.PositiveIntegerField(default=1)
    unit_list_price_usd = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text=_("Precio de lista unitario en USD (antes de descuento)"),
    )
    discount_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text=_("Descuento aplicado a la línea (%)"),
    )
    unit_sale_price_usd = models.DecimalField(max_digits=10, decimal_places=2, help_text=_("Precio unitario de venta en USD (neto, con descuento)"))
    unit_cost_price_usd = models.DecimalField(max_digits=10, decimal_places=2, help_text=_("Precio unitario de costo/compra en USD"))
    subtotal_sale_usd = models.DecimalField(max_digits=12, decimal_places=2, help_text=_("Subtotal de venta en USD (cantidad × precio venta)"))
    subtotal_cost_usd = models.DecimalField(max_digits=12, decimal_places=2, help_text=_("Subtotal de costo en USD (cantidad × precio compra)"))
    line_profit_usd = models.DecimalField(max_digits=12, decimal_places=2, help_text=_("Utilidad de la línea en USD"))

    class Meta:
        db_table = "sale_items"
        verbose_name = "Línea de Venta"
        verbose_name_plural = "Líneas de Venta"
        indexes = [
            models.Index(fields=["sale"], name="sale_items_sale_idx"),
            models.Index(fields=["product"], name="sale_items_product_idx"),
        ]

    def __str__(self):
        return f"{self.quantity}x {self.product.name} (Venta #{self.sale_id})"


class Quote(models.Model):
    class StatusChoices(models.TextChoices):
        DRAFT = "DRA", _("Borrador")
        SENT = "SEN", _("Enviado")
        APPROVED = "APR", _("Aprobado")
        REJECTED = "REJ", _("Rechazado")
        CONVERTED = "CON", _("Convertido a Venta")

    quote_number = models.CharField(
        max_length=50, unique=True,
        help_text=_("Número de presupuesto (ej: 08052026-8)"),
    )
    customer = models.ForeignKey(
        "core.Customer",
        on_delete=models.PROTECT,
        related_name="quotes",
    )
    seller = models.ForeignKey(
        "core.Seller",
        on_delete=models.PROTECT,
        related_name="quotes",
        null=True, blank=True,
    )
    issued_date = models.DateField(help_text=_("Fecha de emisión del presupuesto"))
    expiry_date = models.DateField(null=True, blank=True, help_text=_("Fecha de vencimiento del presupuesto"))

    bcv_rate = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, help_text=_("Tasa BCV vigente al emitir"))
    parallel_rate = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, help_text=_("Tasa paralela vigente al emitir"))

    includes_installation = models.BooleanField(default=False, help_text=_("¿El presupuesto incluye servicio de instalación?"))
    includes_delivery = models.BooleanField(default=False, help_text=_("¿El presupuesto incluye despacho/flete?"))

    subtotal_usd = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    subtotal_ves = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    iva_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=16.00,
        help_text=_("Porcentaje de IVA aplicado (por defecto 16%)"),
    )
    iva_amount_usd = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_usd = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text=_("Total general en USD (subtotal + IVA)"))
    total_ves = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True, help_text=_("Total general en Bolívares"))

    status = models.CharField(max_length=3, choices=StatusChoices.choices, default=StatusChoices.DRAFT)
    converted_to_sale = models.ForeignKey(
        "Sale",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="source_quote",
        help_text=_("Venta generada a partir de este presupuesto"),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "quotes"
        verbose_name = "Presupuesto"
        verbose_name_plural = "Presupuestos"
        ordering = ["-issued_date"]
        indexes = [
            models.Index(fields=["quote_number"], name="quotes_number_idx"),
            models.Index(fields=["customer", "status"], name="quotes_customer_status_idx"),
            models.Index(fields=["issued_date"], name="quotes_date_idx"),
        ]

    def __str__(self):
        return f"Presupuesto {self.quote_number} — {self.customer}"


class QuoteItem(models.Model):
    quote = models.ForeignKey(
        "Quote",
        on_delete=models.CASCADE,
        related_name="items",
    )
    product = models.ForeignKey(
        "core.Product",
        on_delete=models.PROTECT,
        related_name="quote_items",
    )
    quantity = models.PositiveIntegerField(default=1)
    unit_price_usd = models.DecimalField(max_digits=10, decimal_places=2, help_text=_("Precio unitario en USD"))
    unit_price_ves = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True, help_text=_("Precio unitario en Bolívares"))
    line_total_usd = models.DecimalField(max_digits=12, decimal_places=2, help_text=_("Total de la línea en USD"))
    line_total_ves = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True, help_text=_("Total de la línea en Bolívares"))

    class Meta:
        db_table = "quote_items"
        verbose_name = "Línea de Presupuesto"
        verbose_name_plural = "Líneas de Presupuesto"

    def __str__(self):
        return f"{self.quantity}x {self.product.name} (Presupuesto {self.quote.quote_number})"


class DispatchOrder(models.Model):
    """Orden de despacho: el documento que detalla la mercancía a entregar de una venta.

    Circula físicamente entre el encargado de almacén y el de despacho (con firmas),
    por eso se puede imprimir en PDF. **No mueve inventario**: el stock ya se descontó
    al registrar la venta; esta orden solo organiza y controla la entrega. Lleva un
    número correlativo propio (``OD-DDMMYYYY-N``) y un estado que sigue su ciclo de vida.
    """

    class StatusChoices(models.TextChoices):
        PENDING = "PEN", _("Pendiente")
        PREPARING = "PREP", _("En preparación")
        DISPATCHED = "DESP", _("Despachada")
        DELIVERED = "ENT", _("Entregada")
        CANCELLED = "ANU", _("Anulada")

    order_number = models.CharField(
        max_length=40, unique=True,
        help_text=_("Número de orden de despacho correlativo (ej: OD-08052026-3)"),
    )
    sale = models.ForeignKey(
        "Sale",
        on_delete=models.PROTECT,
        related_name="dispatch_orders",
        help_text=_("Venta a la que corresponde el despacho"),
    )
    status = models.CharField(
        max_length=4, choices=StatusChoices.choices, default=StatusChoices.PENDING,
    )
    dispatch_date = models.DateField(null=True, blank=True, help_text=_("Fecha planificada/efectiva de despacho"))
    delivery_address = models.TextField(blank=True, help_text=_("Dirección de entrega"))
    carrier = models.CharField(max_length=150, blank=True, help_text=_("Transporte o responsable de la entrega"))
    received_by = models.CharField(max_length=150, blank=True, help_text=_("Persona que recibe la mercancía (recibí conforme)"))
    notes = models.TextField(blank=True)

    created_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="dispatch_orders",
        help_text=_("Usuario que generó la orden"),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "dispatch_orders"
        verbose_name = "Orden de Despacho"
        verbose_name_plural = "Órdenes de Despacho"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["order_number"], name="dispatch_number_idx"),
            models.Index(fields=["sale"], name="dispatch_sale_idx"),
            models.Index(fields=["status"], name="dispatch_status_idx"),
        ]

    def __str__(self):
        return f"Orden de despacho {self.order_number} (Venta #{self.sale_id})"


class DispatchOrderItem(models.Model):
    dispatch_order = models.ForeignKey(
        "DispatchOrder",
        on_delete=models.CASCADE,
        related_name="items",
    )
    product = models.ForeignKey(
        "core.Product",
        on_delete=models.PROTECT,
        related_name="dispatch_items",
    )
    quantity = models.PositiveIntegerField(default=1, help_text=_("Cantidad a despachar"))

    class Meta:
        db_table = "dispatch_order_items"
        verbose_name = "Línea de Orden de Despacho"
        verbose_name_plural = "Líneas de Orden de Despacho"

    def __str__(self):
        return f"{self.quantity}x {self.product.name} ({self.dispatch_order.order_number})"

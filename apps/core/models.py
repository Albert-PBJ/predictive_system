from django.db import models
from django.utils.translation import gettext_lazy as _


class Category(models.Model):
    name = models.CharField(
        max_length=100, unique=True,
        help_text=_("Nombre de la categoría de producto"),
    )
    slug = models.SlugField(max_length=100, unique=True)
    description = models.TextField(blank=True, help_text=_("Descripción de la categoría"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "categories"
        verbose_name = "Categoría"
        verbose_name_plural = "Categorías"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Product(models.Model):
    class MaterialChoices(models.TextChoices):
        MESH = "MESH", _("Malla Mesh")
        BIPIEL = "BIPIEL", _("Bipiel")
        FABRIC = "FABRIC", _("Tela")
        METAL = "METAL", _("Metal")
        WOOD = "WOOD", _("Madera/Melamina")
        OTHER = "OTHER", _("Otro")

    # Identificación
    sku = models.CharField(
        max_length=50, unique=True, null=True, blank=True,
        help_text=_("Código/SKU del producto (ej: OK-6611N)"),
    )
    name = models.CharField(
        max_length=100,
        help_text=_("Nombre comercial del producto (ej: Stanford)"),
    )
    full_name = models.CharField(
        max_length=255, blank=True,
        help_text=_("Nombre completo del producto"),
    )

    # Clasificación
    category = models.ForeignKey(
        "Category",
        on_delete=models.PROTECT,
        related_name="products",
        null=True, blank=True,
        help_text=_("Categoría del producto"),
    )
    material = models.CharField(
        max_length=10,
        choices=MaterialChoices.choices,
        null=True, blank=True,
        help_text=_("Material principal del producto"),
    )
    colors = models.JSONField(
        default=list, blank=True,
        help_text=_("Lista de colores disponibles"),
    )

    # Medidas físicas en cm (sillas)
    seat_length_cm = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True, help_text=_("Largo del asiento en cm"))
    seat_width_cm = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True, help_text=_("Ancho del asiento en cm"))
    back_length_cm = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True, help_text=_("Largo del espaldar en cm"))
    back_width_cm = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True, help_text=_("Ancho del espaldar en cm"))
    min_height_cm = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True, help_text=_("Altura mínima en cm"))
    max_height_cm = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True, help_text=_("Altura máxima en cm"))

    # Medidas físicas en cm (escritorios/mesas)
    desk_length_cm = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True, help_text=_("Largo del escritorio en cm"))
    desk_width_cm = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True, help_text=_("Ancho del escritorio en cm"))
    desk_height_cm = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True, help_text=_("Alto del escritorio en cm"))

    # Precios actuales
    purchase_price_usd = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text=_("Precio de compra actual en USD"),
    )
    sale_price_usd = models.DecimalField(
        max_digits=10, decimal_places=2,
        help_text=_("Precio de venta actual en USD"),
    )

    # Inventario
    stock = models.IntegerField(default=0, help_text=_("Cantidad en stock actualmente"))
    min_stock = models.IntegerField(default=0, help_text=_("Stock mínimo para disparar alerta de reabastecimiento"))

    # Metadatos
    is_manufactured = models.BooleanField(
        default=True,
        help_text=_("True si Maescar lo fabrica; False si es importado/revendido"),
    )
    image = models.URLField(max_length=500, null=True, blank=True, help_text=_("URL de la imagen del producto"))
    is_active = models.BooleanField(default=True, help_text=_("Producto activo en el catálogo"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "products"
        verbose_name = "Producto"
        verbose_name_plural = "Productos"
        ordering = ["category", "name"]
        indexes = [
            models.Index(fields=["sku"], name="products_sku_idx"),
            models.Index(fields=["category", "is_active"], name="products_cat_active_idx"),
        ]

    def __str__(self):
        return f"{self.name} ({self.sku})" if self.sku else self.name


class ProductPriceHistory(models.Model):
    product = models.ForeignKey(
        "Product",
        on_delete=models.CASCADE,
        related_name="price_history",
        help_text=_("Producto al que corresponde el cambio de precio"),
    )
    purchase_price_usd = models.DecimalField(max_digits=10, decimal_places=2, help_text=_("Precio de compra en USD"))
    sale_price_usd = models.DecimalField(max_digits=10, decimal_places=2, help_text=_("Precio de venta en USD"))
    purchase_price_ves = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True, help_text=_("Precio de compra en Bolívares"))
    sale_price_ves = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True, help_text=_("Precio de venta en Bolívares"))
    bcv_rate = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, help_text=_("Tasa BCV oficial en el momento del cambio"))
    parallel_rate = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, help_text=_("Tasa paralela en el momento del cambio"))
    changed_at = models.DateField(help_text=_("Fecha del cambio de precio"))
    reason = models.CharField(
        max_length=255, blank=True,
        help_text=_("Motivo del cambio (ajuste de tasa, cambio de proveedor, promoción, etc.)"),
    )

    class Meta:
        db_table = "product_price_history"
        verbose_name = "Historial de Precio"
        verbose_name_plural = "Historial de Precios"
        ordering = ["-changed_at"]
        indexes = [
            models.Index(fields=["product", "changed_at"], name="pph_product_date_idx"),
        ]

    def __str__(self):
        return f"{self.product.name} — ${self.sale_price_usd} USD ({self.changed_at})"


class Customer(models.Model):
    class TypeChoices(models.TextChoices):
        INSTITUTIONAL = "INST", _("Institucional")
        CORPORATE = "CORP", _("Empresarial")
        INDIVIDUAL = "IND", _("Particular")

    rif = models.CharField(max_length=20, unique=True, help_text=_("RIF o cédula fiscal venezolana"))
    company_name = models.CharField(max_length=200, help_text=_("Razón social o nombre de la empresa"))
    customer_type = models.CharField(
        max_length=4,
        choices=TypeChoices.choices,
        default=TypeChoices.CORPORATE,
        help_text=_("Tipo de cliente"),
    )
    sector = models.CharField(max_length=100, blank=True, help_text=_("Sector industrial del cliente"))

    # Contacto
    contact_first_name = models.CharField(max_length=100, blank=True, help_text=_("Nombre del contacto/representante"))
    contact_last_name = models.CharField(max_length=100, blank=True, help_text=_("Apellido del contacto/representante"))
    contact_ci = models.CharField(max_length=15, blank=True, help_text=_("Cédula de identidad del representante"))
    phone = models.CharField(max_length=20, blank=True)
    mobile = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)

    # Ubicación venezolana
    state = models.CharField(max_length=100, blank=True, help_text=_("Estado venezolano"))
    municipality = models.CharField(max_length=100, blank=True, help_text=_("Municipio"))
    parish = models.CharField(max_length=100, blank=True, help_text=_("Parroquia"))
    fiscal_address = models.TextField(blank=True, help_text=_("Dirección fiscal completa"))

    total_employees = models.IntegerField(
        null=True, blank=True,
        help_text=_("Total de trabajadores (para segmentar prospectos por tamaño)"),
    )
    is_active_customer = models.BooleanField(
        default=False,
        help_text=_("True si ya es cliente activo; False si es solo prospecto"),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "customers"
        verbose_name = "Cliente"
        verbose_name_plural = "Clientes"
        ordering = ["company_name"]
        indexes = [
            models.Index(fields=["rif"], name="customers_rif_idx"),
            models.Index(fields=["is_active_customer", "customer_type"], name="customers_active_type_idx"),
        ]

    def __str__(self):
        return f"{self.company_name} ({self.rif})"


class Seller(models.Model):
    user = models.OneToOneField(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="seller_profile",
        help_text=_("Usuario de Django asociado al vendedor"),
    )
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    commission_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=10.00,
        help_text=_("Porcentaje de comisión sobre la utilidad (por defecto 10%)"),
    )
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sellers"
        verbose_name = "Vendedor"
        verbose_name_plural = "Vendedores"
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return f"{self.first_name} {self.last_name}"


class ExchangeRate(models.Model):
    class SourceChoices(models.TextChoices):
        BCV = "BCV", _("BCV (Tasa Oficial)")
        MONITOR = "MON", _("Monitor Dólar")
        OTHER = "OTH", _("Otra Fuente")

    date = models.DateField(unique=True, help_text=_("Fecha de la tasa de cambio"))
    bcv_rate = models.DecimalField(
        max_digits=12, decimal_places=4,
        help_text=_("Tasa BCV oficial (Bs por 1 USD)"),
    )
    parallel_rate = models.DecimalField(
        max_digits=12, decimal_places=4,
        null=True, blank=True,
        help_text=_("Tasa paralela referencial (Bs por 1 USD)"),
    )
    source = models.CharField(max_length=3, choices=SourceChoices.choices, default=SourceChoices.BCV)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "exchange_rates"
        verbose_name = "Tasa de Cambio"
        verbose_name_plural = "Tasas de Cambio"
        ordering = ["-date"]
        indexes = [
            models.Index(fields=["date"], name="exchange_rates_date_idx"),
        ]

    def __str__(self):
        return f"{self.date}: BCV={self.bcv_rate} | Paralela={self.parallel_rate}"

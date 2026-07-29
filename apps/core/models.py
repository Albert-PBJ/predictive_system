from django.db import models
from django.utils.translation import gettext_lazy as _

from .price_band_defaults import default_price_bands

# Prefijo de SKU que marca un **servicio** (p. ej. "Mantenimiento"): un producto sin
# inventario y de precio flexible (se fija al registrar la venta). Se usa de forma
# transversal para tratar a los servicios como "sin stock": no validan/descuentan
# inventario y se excluyen de las pantallas y métricas de existencias. NO se excluyen
# de los modelos de ML (demanda/ventas/utilidad): su historia sintética se genera suave
# para no afectar la exactitud. Para identificar un servicio usa `Product.is_service`
# (instancia) o el filtro `sku__startswith=SERVICE_SKU_PREFIX` (queryset).
SERVICE_SKU_PREFIX = "MSC-SERV-"


class Category(models.Model):
    name = models.CharField(
        max_length=100, unique=True,
        help_text=_("Nombre de la categoría de producto"),
    )
    slug = models.SlugField(max_length=100, unique=True)

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

    # Precios actuales
    purchase_price_usd = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text=_("Precio de compra actual en USD (referencia / última compra)"),
    )
    # Costo promedio ponderado móvil (CPP): base del costo de venta (CMV). Lo mantiene
    # el módulo de inventario, recalculándolo en cada ENTRADA con costo conocido
    # (`inventory.services.apply_movement`); las salidas/ajustes conservan el promedio.
    # Se inicializa con `purchase_price_usd` la primera vez (ver `save`). Si queda en
    # null, los cálculos de costo caen al `purchase_price_usd`.
    average_cost_usd = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text=_("Costo promedio ponderado móvil en USD (base del CMV; lo recalcula el inventario en cada entrada)"),
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

    def save(self, *args, **kwargs):
        # Inicializa el costo promedio ponderado con el precio de compra la primera
        # vez (mientras aún no hay entradas que lo hayan calculado). A partir de ahí
        # lo mantiene el inventario en cada entrada; editar el producto (p. ej. cambiar
        # el precio de compra de referencia) NO recalcula el promedio, que solo se
        # mueve con recepciones reales de mercancía.
        if self.average_cost_usd is None and self.purchase_price_usd is not None:
            self.average_cost_usd = self.purchase_price_usd
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "average_cost_usd" not in update_fields:
                kwargs["update_fields"] = list(update_fields) + ["average_cost_usd"]
        super().save(*args, **kwargs)

    @property
    def is_service(self) -> bool:
        """True si el producto es un **servicio** (sin inventario, precio flexible).

        Se reconoce por el prefijo de SKU `SERVICE_SKU_PREFIX` (p. ej. "Mantenimiento").
        Los servicios se venden a un precio que se fija en la venta, no descuentan
        stock y se excluyen de las métricas de inventario.
        """
        return bool(self.sku and self.sku.startswith(SERVICE_SKU_PREFIX))


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
    bcv_rate = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, help_text=_("Tasa Dólar BCV en el momento del cambio"))
    eur_bcv_rate = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, help_text=_("Tasa Euro BCV en el momento del cambio"))
    parallel_rate = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, help_text=_("Tasa paralela en el momento del cambio (referencial)"))
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
    # Las dos tasas OPERATIVAS (oficiales del BCV) con las que se factura, y la
    # PARALELA, que es solo referencia analítica del valor real del dinero.
    bcv_rate = models.DecimalField(
        max_digits=12, decimal_places=4,
        help_text=_("Tasa Dólar BCV oficial (Bs por 1 USD) — operativa"),
    )
    eur_bcv_rate = models.DecimalField(
        max_digits=12, decimal_places=4,
        null=True, blank=True,
        help_text=_("Tasa Euro BCV oficial (Bs por 1 EUR) — operativa"),
    )
    parallel_rate = models.DecimalField(
        max_digits=12, decimal_places=4,
        null=True, blank=True,
        help_text=_("Tasa paralela referencial (Bs por 1 USD) — solo análisis, no se factura con ella"),
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
        return (
            f"{self.date}: Dólar BCV={self.bcv_rate} | Euro BCV={self.eur_bcv_rate} "
            f"| Paralelo={self.parallel_rate}"
        )


# Clave de caché del singleton de configuración. Se busca/borra desde
# `apps.core.system_settings` y desde `SystemSettings.save()`.
SYSTEM_SETTINGS_CACHE_KEY = "system_settings_singleton"


class SystemSettings(models.Model):
    """Configuración global del sistema (fila única, ``pk=1``).

    Centraliza los parámetros que antes vivían dispersos como **variables de
    entorno** (interruptores de enriquecimiento/OCR, tasa de cambio) o como
    **constantes de negocio** sueltas (IVA, vencimiento de presupuestos), para que
    un administrador pueda ajustarlos en caliente desde la UI, sin reiniciar el
    servidor ni redeplegar.

    Semántica **DB-manda, sembrada del entorno**: la primera vez que se necesita,
    la fila se crea tomando como valores iniciales los del ``.env`` (ver
    ``system_settings._env_defaults``); a partir de ahí la fila es la fuente de
    verdad. Los **secretos** (``DEEPSEEK_API_KEY``, ``APIFY_API_KEY``, credenciales
    de BD, ``SECRET_KEY``) **no** se guardan aquí: se siguen leyendo del entorno.

    Es un singleton: ``save()`` fuerza ``pk=1`` e invalida la caché; usa
    ``SystemSettings.load()`` o ``system_settings.get_settings()`` para leerla.
    """

    class RateBasisChoices(models.TextChoices):
        BCV = "BCV", _("Dólar BCV (oficial)")
        EUR_BCV = "EUR", _("Euro BCV (oficial)")
        PARALLEL = "PAR", _("Paralelo (referencial)")
        AVERAGE = "AVG", _("Promedio Dólar BCV/Paralelo")

    # ── Tasa de cambio ────────────────────────────────────────────────────────
    # Dólar BCV y Euro BCV son las tasas OPERATIVAS (con las que se factura); el
    # paralelo queda como referencia analítica del valor real del dinero.
    rate_basis = models.CharField(
        max_length=3, choices=RateBasisChoices.choices, default=RateBasisChoices.BCV,
        help_text=_(
            "Qué tasa usar para convertir USD→VES en ventas, presupuestos y reportes. "
            "Lo normal es una de las oficiales (Dólar BCV o Euro BCV)."
        ),
    )
    rate_max_age_days = models.PositiveSmallIntegerField(
        default=2,
        help_text=_("Días de antigüedad a partir de los cuales la tasa se considera vencida (dispara alerta)."),
    )
    exchange_rate_api_url = models.CharField(
        max_length=300, blank=True, default="https://pydolarve.org/api/v1/dollar",
        help_text=_("URL de la API pública para bajar la tasa (BCV + paralela)."),
    )

    # ── Enriquecimiento por LLM (scrapers + reporte) ──────────────────────────
    use_llm_enrichment = models.BooleanField(
        default=False,
        help_text=_("Activa el enriquecimiento de scrapers vía DeepSeek (requiere DEEPSEEK_API_KEY en el entorno)."),
    )
    deepseek_model = models.CharField(
        max_length=100, blank=True, default="deepseek-chat",
        help_text=_("Modelo de DeepSeek a usar (p. ej. deepseek-chat)."),
    )
    deepseek_base_url = models.CharField(
        max_length=300, blank=True, default="https://api.deepseek.com",
        help_text=_("Endpoint compatible con OpenAI para DeepSeek."),
    )
    enable_llm_report_narrative = models.BooleanField(
        default=True,
        help_text=_("Permite que el reporte ejecutivo PDF use prosa redactada por el LLM (si hay clave)."),
    )

    # ── OCR de imágenes (Instagram) ───────────────────────────────────────────
    use_vision_price_ocr = models.BooleanField(
        default=False,
        help_text=_("Activa el OCR de precios en imágenes de Instagram (EasyOCR; requiere el paquete instalado)."),
    )
    ocr_languages = models.CharField(
        max_length=50, blank=True, default="es,en",
        help_text=_("Idiomas de EasyOCR, separados por comas (p. ej. es,en)."),
    )
    ocr_use_gpu = models.BooleanField(
        default=False, help_text=_("Usar GPU para el OCR si hay CUDA disponible."),
    )
    ocr_max_images_per_post = models.PositiveSmallIntegerField(
        default=2, help_text=_("Cuántas imágenes leer por publicación."),
    )
    ocr_mag_ratio = models.DecimalField(
        max_digits=4, decimal_places=2, default=2.00,
        help_text=_("Factor de ampliación de la imagen antes del OCR (ayuda a captar un '$' pequeño)."),
    )
    ocr_assume_usd_for_bare_number = models.BooleanField(
        default=False,
        help_text=_("Tratar un número sin símbolo de moneda como precio en USD (arriesgado; off por defecto)."),
    )
    ocr_bare_number_max_usd = models.DecimalField(
        max_digits=8, decimal_places=2, default=500.00,
        help_text=_("Tope de seguridad (USD) para un número 'desnudo' sin indicador de precio."),
    )

    # ── Scrapers (generales) ──────────────────────────────────────────────────
    discard_instagram_without_price = models.BooleanField(
        default=False,
        help_text=_("Descartar posts de Instagram sin precio (por defecto se conservan: el precio rara vez está en el caption)."),
    )
    scraper_default_limit = models.PositiveSmallIntegerField(
        default=50, help_text=_("Límite de resultados por defecto al lanzar un scraper."),
    )
    price_bands = models.JSONField(
        default=default_price_bands, blank=True,
        help_text=_(
            "Rangos de precio plausibles (USD) por categoría para la validación de "
            "datos scrapeados. Estructura: {categories: {<cat>: {min, max}}, default: {min, max}}."
        ),
    )

    # ── Valores por defecto de negocio ────────────────────────────────────────
    default_iva_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=16.00,
        help_text=_("IVA por defecto (%) al crear un presupuesto."),
    )
    default_quote_expiry_days = models.PositiveSmallIntegerField(
        default=15,
        help_text=_("Vigencia por defecto (días) de un presupuesto cuando no se indica fecha de vencimiento."),
    )

    # ── Datos de la empresa (encabezado de presupuestos y reportes PDF) ────────
    company_name = models.CharField(
        max_length=200, blank=True, default="Inversiones Maescar, C.A.",
        help_text=_("Razón social, usada en el encabezado de presupuestos y reportes."),
    )
    company_rif = models.CharField(max_length=30, blank=True, help_text=_("RIF de la empresa."))
    company_address = models.TextField(
        blank=True,
        default=(
            "Av. Principal de Paraparal, Centro Comercial Paraparal Plaza, "
            "Local Apb-05, Municipio Los Guayos, Valencia, Edo. Carabobo"
        ),
        help_text=_("Dirección fiscal (aparece en presupuestos y órdenes de despacho)."),
    )
    company_phone = models.CharField(
        max_length=50, blank=True, default="0414-434.44.52 / 0414-4704347",
        help_text=_("Teléfono(s) de contacto."),
    )
    company_email = models.EmailField(
        max_length=254, blank=True, default="inversiones.maescar@gmail.com",
        help_text=_("Correo de contacto."),
    )
    company_website = models.CharField(max_length=150, blank=True, help_text=_("Sitio web."))
    company_logo_url = models.URLField(max_length=500, blank=True, help_text=_("URL del logo (PNG/JPG) para los documentos."))

    # ── Módulo predictivo (ML) ────────────────────────────────────────────────
    training_cutoff_date = models.DateField(
        null=True, blank=True,
        help_text=_(
            "Fecha de corte del entrenamiento: los datos POSTERIORES a esta fecha se "
            "excluyen de las series con las que se entrenan los modelos, y el pronóstico "
            "arranca justo después del corte. Útil cuando se cargaron datos de prueba "
            "recientes que no deben contaminar los modelos. Vacío = usar todo el historial."
        ),
    )
    rates_ignore_training_cutoff = models.BooleanField(
        default=False,
        help_text=_(
            "Exceptúa a los pronósticos de TASA DE CAMBIO de la fecha de corte: se "
            "entrenan siempre con las tasas más recientes. Se usa cuando el corte existe "
            "por datos de prueba de ventas, pero las tasas cargadas sí son reales. No "
            "afecta a los demás modelos."
        ),
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "system_settings"
        verbose_name = "Configuración del Sistema"
        verbose_name_plural = "Configuración del Sistema"

    def __str__(self):
        return "Configuración del Sistema"

    def save(self, *args, **kwargs):
        # Singleton: una sola fila, siempre pk=1. Invalida la caché tras guardar.
        from django.core.cache import cache

        self.pk = 1
        super().save(*args, **kwargs)
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)

    def delete(self, *args, **kwargs):
        # No se permite borrar el singleton (solo se edita).
        from django.core.cache import cache

        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)

    @classmethod
    def load(cls):
        """Atajo de modelo: la fila singleton (delega en el accesor con caché)."""
        from .system_settings import get_settings

        return get_settings()

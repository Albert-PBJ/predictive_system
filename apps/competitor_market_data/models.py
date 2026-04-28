from django.db import models

# Create your models here.
from django.db import models
from django.utils.translation import gettext_lazy as _


class CompetitorMarketData(models.Model):
    """
    Modelo unificado para almacenar datos de la competencia recolectados vía Web Scraping.
    """

    class SourceChoices(models.TextChoices):
        INSTAGRAM = "IG", _("Instagram")
        FACEBOOK = "FB", _("Facebook Marketplace")
        WEBSITE = "WEB", _("Página Web Directa")
        OTHER = "OTH", _("Otra Fuente")

    # 1. Identificación y Origen
    competitor_name = models.CharField(
        null=True,
        blank=True,
        max_length=150,
        help_text="Nombre de la empresa competidora (ej. OficinaTotal)",
    )
    source = models.CharField(
        max_length=3, choices=SourceChoices.choices, default=SourceChoices.WEBSITE
    )
    url = models.URLField(
        null=True,
        blank=True,
        max_length=500,
        help_text="Enlace directo al recurso de donde se obtuvo la información",
    )

    # 2. Datos del Producto (Variables Independientes)
    product_name = models.CharField(null=True, blank=True, max_length=255)
    category = models.CharField(
        null=True,
        blank=True,
        max_length=100,
        help_text="Categoría normalizada (ej. Sillería, Escritorios)",
    )

    # 3. Métricas Clave para el Benchmarking si es posible su separacion
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Precio detectado",
    )
    currency = models.CharField(
        null=True,
        blank=True,
        max_length=3,
        default="USD",
        help_text="Moneda (USD o VES)",
    )
    lead_time_days = models.IntegerField(
        null=True,
        blank=True,
        help_text="Tiempo de entrega estimado en días (si aplica)",
    )
    is_in_stock = models.BooleanField(
        null=True,
        blank=True,
        default=True,
        help_text="¿El producto marca disponibilidad?",
    )
    promotions = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Etiquetas de oferta, ej: 'Envío Gratis', '15% OFF'",
    )

    # 4. Datos en bruto directo de la fuente
    raw_metadata = models.JSONField(
        null=True,
        blank=True,
        help_text="Guarda aquí datos extra: likes de IG, rating de vendedor en FB, etc.",
    )

    scraped_at = models.DateTimeField(
        auto_now_add=True, help_text="Fecha y hora exacta de la extracción"
    )

    class Meta:
        db_table = "competitor_market_data"
        verbose_name = "Dato de Mercado Competitivo"
        verbose_name_plural = "Datos de Mercado Competitivo"
        # Los índices hacen que las consultas de Pandas/Big Data sean extremadamente rápidas
        indexes = [
            models.Index(fields=["competitor_name", "scraped_at"]),
            models.Index(fields=["category", "scraped_at"]),
        ]
        # Ordenamos por defecto del más reciente al más antiguo
        ordering = ["-scraped_at"]

    def __str__(self):
        return f"[{self.get_source_display()}] {self.competitor_name} - {self.product_name} (${self.price})"

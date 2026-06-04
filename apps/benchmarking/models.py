from django.db import models
from django.utils.translation import gettext_lazy as _


class Competitor(models.Model):
    name = models.CharField(
        max_length=150, unique=True,
        help_text=_("Nombre de la empresa competidora"),
    )
    state = models.CharField(max_length=100, blank=True, help_text=_("Estado venezolano donde opera"))
    municipality = models.CharField(max_length=100, blank=True, help_text=_("Municipio donde opera"))
    website = models.URLField(max_length=500, blank=True, help_text=_("Sitio web del competidor"))
    instagram = models.CharField(max_length=200, blank=True, help_text=_("URL o @usuario de Instagram"))
    facebook = models.CharField(max_length=200, blank=True, help_text=_("URL de la página de Facebook"))
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, help_text=_("Competidor activo en el seguimiento"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "competitors"
        verbose_name = "Competidor"
        verbose_name_plural = "Competidores"
        ordering = ["name"]

    def __str__(self):
        return self.name


class CompetitorMarketData(models.Model):
    class SourceChoices(models.TextChoices):
        INSTAGRAM = "IG", _("Instagram")
        FACEBOOK = "FB", _("Facebook Marketplace")
        WEBSITE = "WEB", _("Página Web Directa")
        MERCADOLIBRE = "ML", _("Mercado Libre")
        OTHER = "OTH", _("Otra Fuente")

    competitor = models.ForeignKey(
        "Competitor",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="market_data",
        help_text=_("Competidor normalizado (FK); puede estar vacío si el scraper trae un nombre nuevo"),
    )
    # Fallback para scrapers que traen nombres que aún no están en Competitor
    competitor_name = models.CharField(
        null=True, blank=True, max_length=150,
        help_text=_("Nombre del competidor tal como lo devuelve el scraper"),
    )
    source = models.CharField(max_length=3, choices=SourceChoices.choices, default=SourceChoices.WEBSITE)
    url = models.URLField(null=True, blank=True, max_length=500)
    product_name = models.CharField(null=True, blank=True, max_length=255)
    category = models.CharField(null=True, blank=True, max_length=100)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    currency = models.CharField(null=True, blank=True, max_length=3, default="USD")
    lead_time_days = models.IntegerField(null=True, blank=True, help_text=_("Tiempo de entrega estimado en días"))
    is_in_stock = models.BooleanField(null=True, blank=True, default=True)
    promotions = models.CharField(max_length=255, null=True, blank=True, help_text=_("Promociones o descuentos detectados"))
    raw_metadata = models.JSONField(null=True, blank=True, help_text=_("Respuesta completa del scraper (Apify)"))
    scraped_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "benchmarking_competitor_market_data"
        verbose_name = "Dato de Mercado Competidor"
        verbose_name_plural = "Datos de Mercado Competidores"
        ordering = ["-scraped_at"]
        indexes = [
            models.Index(fields=["competitor", "scraped_at"], name="bmd_competitor_date_idx"),
            models.Index(fields=["competitor_name", "scraped_at"], name="bmd_name_date_idx"),
            models.Index(fields=["category", "scraped_at"], name="bmd_category_date_idx"),
        ]

    def __str__(self):
        who = self.competitor or self.competitor_name or "Desconocido"
        return f"{who} — {self.product_name} (${self.price})"

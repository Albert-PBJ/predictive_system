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

    # Snapshot de normalización a USD al momento del scraping. Guardamos el valor
    # convertido y la tasa usada (con su fecha) para que el precio sea reproducible
    # y la serie temporal en USD no dependa de la tasa "más reciente" de hoy.
    price_usd = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text=_("Precio convertido a USD al momento del scraping"),
    )
    exchange_rate_used = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True,
        help_text=_("Tasa Bs/USD aplicada para convertir (solo si el precio venía en VES)"),
    )
    rate_date = models.DateField(
        null=True, blank=True,
        help_text=_("Fecha de la ExchangeRate usada para la conversión"),
    )

    lead_time_days = models.IntegerField(null=True, blank=True, help_text=_("Tiempo de entrega estimado en días"))
    is_in_stock = models.BooleanField(null=True, blank=True, default=True)
    promotions = models.CharField(max_length=255, null=True, blank=True, help_text=_("Promociones o descuentos detectados"))

    # Match (mejor esfuerzo, determinista) contra el catálogo propio, para poder
    # comparar like-with-like en el benchmarking. Confirmable/corregible a mano.
    product = models.ForeignKey(
        "core.Product",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="competitor_observations",
        help_text=_("Producto propio equivalente (match automático; revisable)"),
    )
    product_match_score = models.FloatField(
        null=True, blank=True,
        help_text=_("Similitud [0–1] del match con el producto propio"),
    )

    # Identidad estable del anuncio entre runs: define la semántica de "observación"
    # (cada fila es una observación con fecha; el último scraped_at por listing_key
    # es el snapshot vigente). Evita el doble conteo en agregados.
    listing_key = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text=_("Clave estable del anuncio entre runs (fuente+url o fuente+competidor+producto)"),
    )

    class EnrichmentChoices(models.TextChoices):
        DETERMINISTIC = "DET", _("Determinista (reglas)")
        LLM = "LLM", _("Enriquecido por LLM")

    enriched_by = models.CharField(
        max_length=3, choices=EnrichmentChoices.choices, default=EnrichmentChoices.DETERMINISTIC,
        help_text=_("Procedencia del enriquecimiento de la fila (reglas vs. LLM)"),
    )
    scrape_run = models.ForeignKey(
        "ScrapeRun",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="records",
        help_text=_("Run de scraping que produjo esta fila (procedencia)"),
    )

    raw_metadata = models.JSONField(null=True, blank=True, help_text=_("Respuesta completa del scraper (Apify)"))
    scraped_at = models.DateTimeField(auto_now_add=True)

    # Fecha real de la publicación (solo Instagram: la trae el post). Permite ubicar
    # la observación en su mes REAL y no en el del scraping: un flyer publicado hace
    # meses pero scrapeado hoy refleja precios/promociones de aquella fecha. Para el
    # resto de fuentes queda en NULL y se usa `scraped_at` (ver `effective_date_expr`).
    posted_at = models.DateTimeField(
        null=True, blank=True,
        help_text=_("Fecha de publicación original del anuncio (solo Instagram); si está vacía se usa scraped_at"),
    )

    class Meta:
        db_table = "benchmarking_competitor_market_data"
        verbose_name = "Dato de Mercado Competidor"
        verbose_name_plural = "Datos de Mercado Competidores"
        ordering = ["-scraped_at"]
        indexes = [
            models.Index(fields=["competitor", "scraped_at"], name="bmd_competitor_date_idx"),
            models.Index(fields=["competitor_name", "scraped_at"], name="bmd_name_date_idx"),
            models.Index(fields=["category", "scraped_at"], name="bmd_category_date_idx"),
            models.Index(fields=["listing_key", "scraped_at"], name="bmd_listing_date_idx"),
            models.Index(fields=["product", "scraped_at"], name="bmd_product_date_idx"),
        ]

    def __str__(self):
        who = self.competitor or self.competitor_name or "Desconocido"
        return f"{who} — {self.product_name} (${self.price})"


class ScrapeRun(models.Model):
    """Procedencia de cada corrida de scraping (item de auditoría/trazabilidad).

    Agrupa las filas producidas en un mismo run de Apify y conserva los parámetros
    de búsqueda, los identificadores del run/dataset y los conteos del resultado
    (recolectados / guardados / descartados). Así cada `CompetitorMarketData` se
    puede rastrear hasta el run, los términos y la fecha que lo originaron.
    """

    class StatusChoices(models.TextChoices):
        RUNNING = "RUN", _("En ejecución")
        SUCCEEDED = "OK", _("Completado")
        FAILED = "ERR", _("Fallido")

    source = models.CharField(max_length=3, choices=CompetitorMarketData.SourceChoices.choices)
    query = models.JSONField(default=list, blank=True, help_text=_("URLs o términos de búsqueda del run"))
    params = models.JSONField(default=dict, blank=True, help_text=_("Parámetros del run (límite, competidor manual, etc.)"))
    apify_run_id = models.CharField(max_length=100, blank=True)
    dataset_id = models.CharField(max_length=100, blank=True, db_index=True)
    status = models.CharField(max_length=3, choices=StatusChoices.choices, default=StatusChoices.RUNNING)

    records_collected = models.IntegerField(default=0, help_text=_("Filas mapeadas desde el dataset"))
    records_saved = models.IntegerField(default=0, help_text=_("Filas que pasaron la validación y se guardaron"))
    records_discarded = models.IntegerField(default=0, help_text=_("Filas descartadas por la validación de calidad"))

    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "benchmarking_scrape_runs"
        verbose_name = "Run de Scraping"
        verbose_name_plural = "Runs de Scraping"
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["source", "started_at"], name="scraperun_source_date_idx"),
            models.Index(fields=["dataset_id"], name="scraperun_dataset_idx"),
        ]

    def __str__(self):
        return f"{self.get_source_display()} — {self.started_at:%Y-%m-%d %H:%M} ({self.get_status_display()})"


class RejectedMarketData(models.Model):
    """Filas descartadas por la validación de calidad (no se pierden, se archivan).

    Persistir los descartes (en vez de solo loguearlos) permite medir la precisión
    del filtro de calidad y revisar manualmente falsos descartes — un requisito de
    defensibilidad para el dataset que alimentará los modelos.
    """

    scrape_run = models.ForeignKey(
        "ScrapeRun",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="rejected_records",
    )
    source = models.CharField(max_length=3, choices=CompetitorMarketData.SourceChoices.choices)
    competitor_name = models.CharField(max_length=150, blank=True)
    product_name = models.CharField(max_length=255, blank=True)
    category = models.CharField(max_length=100, blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    url = models.URLField(max_length=500, blank=True)
    rejection_reason = models.CharField(max_length=255, help_text=_("Motivo del descarte"))
    raw_metadata = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "benchmarking_rejected_market_data"
        verbose_name = "Dato de Mercado Descartado"
        verbose_name_plural = "Datos de Mercado Descartados"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["source", "created_at"], name="rejected_source_date_idx"),
        ]

    def __str__(self):
        return f"[{self.source}] {self.product_name or '—'} — {self.rejection_reason}"

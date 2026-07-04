from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from dateutil.relativedelta import relativedelta


class ScraperSchedule(models.Model):
    """Programación de scraping automático recurrente.

    Guarda una lista de URLs/términos + una frecuencia para re-scrapear a los mismos
    competidores periódicamente (p. ej. cada mes). **No hay cron**: como el
    procesamiento es dirigido por el navegador, la corrida automática la dispara el
    frontend cuando el **ADMIN inicia sesión** (o durante la sesión) y la programación
    está **vencida** (`next_run_at <= ahora`). Reutiliza el mismo flujo de scraping por
    lotes reanudable (start → status → process-chunk).
    """

    class SourceChoices(models.TextChoices):
        # El valor coincide con el segmento `<source>` de las rutas /scrapers/ y con
        # las claves del frontend, para mapear 1:1 a `startScrape`.
        INSTAGRAM = "instagram", _("Instagram")
        WEBSITE = "website", _("Sitios Web")
        MERCADOLIBRE = "mercadolibre", _("Mercado Libre")

    class FrequencyChoices(models.TextChoices):
        DAILY = "DAILY", _("Diaria")
        WEEKLY = "WEEKLY", _("Semanal")
        BIWEEKLY = "BIWEEKLY", _("Quincenal")
        MONTHLY = "MONTHLY", _("Mensual")

    # Deltas de cada frecuencia (relativedelta maneja bien el "mes" natural).
    _DELTAS = {
        "DAILY": relativedelta(days=1),
        "WEEKLY": relativedelta(weeks=1),
        "BIWEEKLY": relativedelta(weeks=2),
        "MONTHLY": relativedelta(months=1),
    }

    name = models.CharField(max_length=150, blank=True, help_text=_("Nombre descriptivo de la programación"))
    source = models.CharField(max_length=20, choices=SourceChoices.choices)
    urls = models.JSONField(default=list, help_text=_("URLs (o términos de búsqueda para Mercado Libre) a scrapear"))
    competitor_name = models.CharField(
        max_length=150, blank=True, help_text=_("Nombre del competidor (solo Sitios Web, opcional)")
    )
    limit = models.PositiveIntegerField(default=50, help_text=_("Límite de resultados por corrida"))
    frequency = models.CharField(
        max_length=10, choices=FrequencyChoices.choices, default=FrequencyChoices.MONTHLY
    )
    is_active = models.BooleanField(default=True)

    last_run_at = models.DateTimeField(
        null=True, blank=True, help_text=_("Última vez que se ejecutó automáticamente")
    )
    next_run_at = models.DateTimeField(
        help_text=_("Próxima ejecución programada (se dispara al iniciar sesión el admin si ya venció)")
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "scraper_schedules"
        verbose_name = "Programación de Scraping"
        verbose_name_plural = "Programaciones de Scraping"
        ordering = ["source", "name", "id"]

    def __str__(self):
        return f"{self.get_source_display()} — {self.name or 'programación'} ({self.get_frequency_display()})"

    def frequency_delta(self) -> relativedelta:
        return self._DELTAS.get(self.frequency, self._DELTAS["MONTHLY"])

    def mark_ran(self, now=None) -> None:
        """Marca ejecutada: fija `last_run_at` y avanza `next_run_at` un periodo desde ahora.

        Anclar el próximo run a *ahora* (no al `next_run_at` previo) evita ráfagas de
        "puesta al día" si el admin no inicia sesión durante varios periodos: se corre
        una vez y se reprograma hacia adelante.
        """
        now = now or timezone.now()
        self.last_run_at = now
        self.next_run_at = now + self.frequency_delta()
        self.save(update_fields=["last_run_at", "next_run_at", "updated_at"])

    @property
    def is_due(self) -> bool:
        return bool(self.is_active and self.next_run_at and self.next_run_at <= timezone.now())

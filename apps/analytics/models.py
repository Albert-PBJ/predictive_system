from django.db import models
from django.utils.translation import gettext_lazy as _


class PredictionLog(models.Model):
    class ModelTypeChoices(models.TextChoices):
        DEMAND_FORECAST = "DEMAND", _("Pronóstico de Demanda")
        PRICE_TREND = "PRICE", _("Tendencia de Precios")
        SEASONAL_PATTERN = "SEASON", _("Patrón Estacional")
        COMPETITOR_BENCHMARK = "BENCH", _("Benchmarking de Competidores")

    name = models.CharField(
        max_length=200,
        help_text=_("Nombre descriptivo del modelo (ej: demand_forecast_xgboost_v3)"),
    )
    model_type = models.CharField(max_length=6, choices=ModelTypeChoices.choices)

    # Métricas de evaluación
    r2_score = models.FloatField(null=True, blank=True, help_text=_("Coeficiente de determinación R²"))
    rmse = models.FloatField(null=True, blank=True, help_text=_("Raíz del Error Cuadrático Medio (RMSE)"))
    mae = models.FloatField(null=True, blank=True, help_text=_("Error Absoluto Medio (MAE)"))
    metrics = models.JSONField(default=dict, blank=True, help_text=_("Métricas adicionales del modelo"))

    hyperparameters = models.JSONField(default=dict, blank=True, help_text=_("Hiperparámetros usados en el entrenamiento"))
    trained_at = models.DateTimeField(help_text=_("Fecha y hora en que se entrenó el modelo"))
    dataset_description = models.TextField(blank=True, help_text=_("Descripción del dataset usado para entrenar"))
    model_file_path = models.CharField(
        max_length=500, blank=True,
        help_text=_("Ruta al archivo del modelo serializado (.pkl, .joblib, etc.)"),
    )
    is_active = models.BooleanField(
        default=False,
        help_text=_("True si es el modelo activo en producción para su tipo"),
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "prediction_logs"
        verbose_name = "Log de Predicción"
        verbose_name_plural = "Logs de Predicción"
        ordering = ["-trained_at"]
        indexes = [
            models.Index(fields=["model_type", "is_active"], name="predlog_type_active_idx"),
        ]

    def __str__(self):
        return f"{self.name} (R²={self.r2_score})"


class KPI(models.Model):
    class CategoryChoices(models.TextChoices):
        FINANCIAL = "FIN", _("Financiero")
        INVENTORY = "INV", _("Inventario")
        SALES = "VEN", _("Ventas")
        COMPETITION = "COM", _("Competencia")

    name = models.CharField(
        max_length=100,
        help_text=_("Identificador del KPI (ej: rotacion_inventario_mensual, margen_promedio)"),
    )
    value = models.DecimalField(max_digits=18, decimal_places=4, help_text=_("Valor numérico calculado"))
    unit = models.CharField(max_length=20, blank=True, help_text=_("Unidad del KPI: %, USD, días, índice, etc."))
    period_month = models.PositiveSmallIntegerField(null=True, blank=True, help_text=_("Mes del período (1–12)"))
    period_year = models.PositiveSmallIntegerField(null=True, blank=True, help_text=_("Año del período"))
    category = models.CharField(max_length=3, choices=CategoryChoices.choices)
    calculated_at = models.DateTimeField(auto_now_add=True, help_text=_("Timestamp en que se calculó el KPI"))
    metadata = models.JSONField(default=dict, blank=True, help_text=_("Datos adicionales del cálculo (breakdown, fuentes, etc.)"))

    class Meta:
        db_table = "kpis"
        verbose_name = "KPI"
        verbose_name_plural = "KPIs"
        ordering = ["-calculated_at"]
        indexes = [
            models.Index(fields=["name", "period_year", "period_month"], name="kpis_name_period_idx"),
            models.Index(fields=["category"], name="kpis_category_idx"),
        ]

    def __str__(self):
        period = f"{self.period_year}-{self.period_month:02d}" if self.period_month else str(self.period_year)
        return f"{self.name}: {self.value}{self.unit} ({period})"


class Alert(models.Model):
    class TypeChoices(models.TextChoices):
        STOCK_BREAK = "STOCK_B", _("Quiebre de Stock")
        OVERSTOCK = "STOCK_O", _("Sobrestock")
        PRICE_CHANGE = "PRICE", _("Cambio de Precio Competidor")
        DEMAND_DROP = "DEMAND", _("Caída de Demanda")
        GOAL_MET = "GOAL", _("Meta Cumplida")
        RATE_STALE = "RATE", _("Tasa de Cambio Desactualizada")

    class SeverityChoices(models.TextChoices):
        INFO = "INFO", _("Información")
        WARNING = "WARN", _("Advertencia")
        CRITICAL = "CRIT", _("Crítico")

    alert_type = models.CharField(max_length=7, choices=TypeChoices.choices)
    severity = models.CharField(max_length=4, choices=SeverityChoices.choices, default=SeverityChoices.INFO)
    title = models.CharField(max_length=200)
    message = models.TextField()
    product = models.ForeignKey(
        "core.Product",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="alerts",
        help_text=_("Producto relacionado con la alerta (si aplica)"),
    )
    competitor = models.ForeignKey(
        "benchmarking.Competitor",
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name="alerts",
        help_text=_("Competidor relacionado con la alerta (si aplica)"),
    )
    is_read = models.BooleanField(default=False)
    is_resolved = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "alerts"
        verbose_name = "Alerta"
        verbose_name_plural = "Alertas"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["is_read", "is_resolved", "severity"], name="alerts_read_resolved_sev_idx"),
            models.Index(fields=["alert_type"], name="alerts_type_idx"),
        ]

    def __str__(self):
        return f"[{self.severity}] {self.title}"

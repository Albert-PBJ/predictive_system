from django.db import models
from django.utils.translation import gettext_lazy as _


class PredictionLog(models.Model):
    class ModelTypeChoices(models.TextChoices):
        DEMAND_FORECAST = "DEMAND", _("Pronóstico de Demanda")
        PRICE_TREND = "PRICE", _("Tendencia de Precios")
        SEASONAL_PATTERN = "SEASON", _("Patrón Estacional")
        COMPETITOR_BENCHMARK = "BENCH", _("Benchmarking de Competidores")
        SALES_FORECAST = "SALES", _("Pronóstico de Ventas e Ingresos")
        EXCHANGE_RATE = "RATE", _("Pronóstico de Tasa de Cambio")
        PROFIT_FORECAST = "PROFIT", _("Pronóstico de Utilidad y Margen")
        INVENTORY_FORECAST = "INVENT", _("Reabastecimiento de Inventario")
        QUOTE_CONVERSION = "QUOTE", _("Conversión de Presupuestos")

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


class TrainingRun(models.Model):
    """Instantánea *append-only* de las métricas de los modelos tras cada reentrenamiento.

    A diferencia de :class:`PredictionLog` —que ``train_models`` **reescribe por completo**
    en cada corrida—, esta tabla es un HISTORIAL: una fila por evento de reentrenamiento,
    con las métricas de cada modelo activo en ese momento. Permite graficar la **evolución
    de la precisión** (R²/exactitud) de los modelos a lo largo de los reentrenamientos.
    """

    class TriggerChoices(models.TextChoices):
        COMMAND = "CMD", _("Comando (train_models)")
        UI = "UI", _("Panel (botón Reentrenar)")

    trained_at = models.DateTimeField(help_text=_("Momento del reentrenamiento"))
    trigger = models.CharField(
        max_length=3,
        choices=TriggerChoices.choices,
        default=TriggerChoices.COMMAND,
        help_text=_("Origen del reentrenamiento: comando o botón del panel"),
    )
    triggered_by = models.CharField(
        max_length=150,
        blank=True,
        help_text=_("Usuario que disparó el reentrenamiento (vacío para el comando/sistema)"),
    )
    # Métricas por objetivo, congeladas en este reentrenamiento. Lista de dicts:
    # [{model_type, model_type_display, name, technique, r2, rmse, mae, accuracy, precision, recall}, ...]
    models_metrics = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "training_runs"
        verbose_name = "Reentrenamiento"
        verbose_name_plural = "Reentrenamientos"
        ordering = ["trained_at"]
        indexes = [
            models.Index(fields=["trained_at"], name="trainrun_trained_idx"),
        ]

    def __str__(self):
        return f"Reentrenamiento {self.trained_at:%Y-%m-%d %H:%M} ({self.get_trigger_display()})"


class Alert(models.Model):
    class TypeChoices(models.TextChoices):
        STOCK_BREAK = "STOCK_B", _("Quiebre de Stock")
        STOCK_PRED = "STOCK_P", _("Quiebre de Stock Previsto")
        OVERSTOCK = "STOCK_O", _("Sobrestock")
        PRICE_CHANGE = "PRICE", _("Cambio de Precio Competidor")
        DEMAND_DROP = "DEMAND", _("Caída de Demanda")
        GOAL_MET = "GOAL", _("Meta Cumplida")
        RATE_STALE = "RATE", _("Tasa de Cambio Desactualizada")
        DISPATCH = "DISP", _("Despacho Pendiente")

    class SeverityChoices(models.TextChoices):
        INFO = "INFO", _("Información")
        WARNING = "WARN", _("Advertencia")
        CRITICAL = "CRIT", _("Crítico")

    alert_type = models.CharField(max_length=7, choices=TypeChoices.choices)
    severity = models.CharField(max_length=4, choices=SeverityChoices.choices, default=SeverityChoices.INFO)
    title = models.CharField(max_length=200)
    message = models.TextField()
    # Roles que deben recibir esta alerta (códigos de ``accounts.Role``). El feed de
    # notificaciones filtra por el rol del usuario; vacío = visible para todos.
    audience = models.JSONField(default=list, blank=True)
    # Clave estable para deduplicar/actualizar una misma condición en el tiempo
    # (p. ej. ``stock_pred:42``), de modo que una alerta recurrente no se multiplique.
    dedupe_key = models.CharField(max_length=120, blank=True, db_index=True)
    is_read = models.BooleanField(default=False)
    is_resolved = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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


class AlertRead(models.Model):
    """Estado de lectura **por usuario** de una alerta.

    Las alertas son hechos de empresa (un quiebre de stock lo es para todos los que
    lo ven), pero cada usuario tiene su propio "leído/no leído". La ausencia de fila
    para un usuario significa "no leída". Se crea al abrir/marcar las notificaciones.
    """

    alert = models.ForeignKey(Alert, on_delete=models.CASCADE, related_name="reads")
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="alert_reads")
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "alert_reads"
        verbose_name = "Lectura de Alerta"
        verbose_name_plural = "Lecturas de Alertas"
        unique_together = ("alert", "user")
        indexes = [
            models.Index(fields=["user", "alert"], name="alertread_user_alert_idx"),
        ]

    def __str__(self):
        return f"{self.user_id} leyó alerta {self.alert_id}"

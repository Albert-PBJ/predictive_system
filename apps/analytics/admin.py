from django.contrib import admin

from .models import Alert, AlertRead, KPI, PredictionLog, TrainingRun


@admin.register(PredictionLog)
class PredictionLogAdmin(admin.ModelAdmin):
    list_display = ("name", "model_type", "r2_score", "rmse", "trained_at", "is_active")
    list_filter = ("model_type", "is_active")
    search_fields = ("name",)
    date_hierarchy = "trained_at"


@admin.register(TrainingRun)
class TrainingRunAdmin(admin.ModelAdmin):
    list_display = ("trained_at", "trigger", "triggered_by")
    list_filter = ("trigger",)
    date_hierarchy = "trained_at"
    readonly_fields = ("trained_at", "trigger", "triggered_by", "models_metrics", "created_at")


@admin.register(KPI)
class KPIAdmin(admin.ModelAdmin):
    list_display = ("name", "value", "unit", "period_year", "period_month", "category", "calculated_at")
    list_filter = ("category", "period_year")
    search_fields = ("name",)


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("title", "alert_type", "severity", "is_resolved", "dedupe_key", "created_at")
    list_filter = ("alert_type", "severity", "is_read", "is_resolved")
    search_fields = ("title", "message", "dedupe_key")
    date_hierarchy = "created_at"


@admin.register(AlertRead)
class AlertReadAdmin(admin.ModelAdmin):
    list_display = ("alert", "user", "read_at")
    search_fields = ("alert__title", "user__username")
    date_hierarchy = "read_at"

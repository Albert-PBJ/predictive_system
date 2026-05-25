from django.contrib import admin

from .models import Alert, KPI, PredictionLog


@admin.register(PredictionLog)
class PredictionLogAdmin(admin.ModelAdmin):
    list_display = ("name", "model_type", "r2_score", "rmse", "trained_at", "is_active")
    list_filter = ("model_type", "is_active")
    search_fields = ("name",)
    date_hierarchy = "trained_at"


@admin.register(KPI)
class KPIAdmin(admin.ModelAdmin):
    list_display = ("name", "value", "unit", "period_year", "period_month", "category", "calculated_at")
    list_filter = ("category", "period_year")
    search_fields = ("name",)


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("title", "alert_type", "severity", "is_read", "is_resolved", "created_at")
    list_filter = ("alert_type", "severity", "is_read", "is_resolved")
    search_fields = ("title", "message")
    date_hierarchy = "created_at"

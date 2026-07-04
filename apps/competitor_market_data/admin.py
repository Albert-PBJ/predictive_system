from django.contrib import admin

from .models import ScraperSchedule


@admin.register(ScraperSchedule)
class ScraperScheduleAdmin(admin.ModelAdmin):
    list_display = ("name", "source", "frequency", "is_active", "next_run_at", "last_run_at")
    list_filter = ("source", "frequency", "is_active")
    search_fields = ("name", "competitor_name")
    readonly_fields = ("last_run_at", "created_at", "updated_at")

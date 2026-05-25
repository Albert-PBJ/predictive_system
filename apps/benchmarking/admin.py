from django.contrib import admin

from .models import Competitor, CompetitorMarketData


@admin.register(Competitor)
class CompetitorAdmin(admin.ModelAdmin):
    list_display = ("name", "state", "municipality", "is_active")
    list_filter = ("is_active", "state")
    search_fields = ("name",)


@admin.register(CompetitorMarketData)
class CompetitorMarketDataAdmin(admin.ModelAdmin):
    list_display = ("competitor_name", "source", "product_name", "price", "currency", "is_in_stock", "scraped_at")
    list_filter = ("source", "is_in_stock", "scraped_at")
    search_fields = ("competitor_name", "product_name", "category")
    date_hierarchy = "scraped_at"

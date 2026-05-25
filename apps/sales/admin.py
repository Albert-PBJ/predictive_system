from django.contrib import admin

from .models import Quote, QuoteItem, Sale, SaleItem


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 0


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ("pk", "customer", "seller", "sale_date", "total_sale_usd", "total_profit_usd", "status")
    list_filter = ("status", "sale_type", "sale_date")
    search_fields = ("customer__company_name", "seller__first_name", "seller__last_name")
    date_hierarchy = "sale_date"
    inlines = [SaleItemInline]


class QuoteItemInline(admin.TabularInline):
    model = QuoteItem
    extra = 0


@admin.register(Quote)
class QuoteAdmin(admin.ModelAdmin):
    list_display = ("quote_number", "customer", "seller", "issued_date", "total_usd", "status")
    list_filter = ("status", "issued_date")
    search_fields = ("quote_number", "customer__company_name")
    date_hierarchy = "issued_date"
    inlines = [QuoteItemInline]

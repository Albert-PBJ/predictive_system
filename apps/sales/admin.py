from django.contrib import admin

from .models import (
    DispatchOrder,
    DispatchOrderItem,
    Quote,
    QuoteItem,
    Sale,
    SaleItem,
    SalePayment,
)


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 0


class SalePaymentInline(admin.TabularInline):
    model = SalePayment
    extra = 0


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ("pk", "customer", "seller", "sale_date", "total_sale_usd", "total_profit_usd", "amount_paid_usd", "invoice_number", "status")
    list_filter = ("status", "sale_type", "sale_date")
    search_fields = ("customer__company_name", "seller__first_name", "seller__last_name", "invoice_number", "control_number")
    date_hierarchy = "sale_date"
    inlines = [SaleItemInline, SalePaymentInline]


class DispatchOrderItemInline(admin.TabularInline):
    model = DispatchOrderItem
    extra = 0


@admin.register(DispatchOrder)
class DispatchOrderAdmin(admin.ModelAdmin):
    list_display = ("order_number", "sale", "status", "dispatch_date", "created_by", "created_at")
    list_filter = ("status", "dispatch_date")
    search_fields = ("order_number", "sale__customer__company_name")
    inlines = [DispatchOrderItemInline]


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

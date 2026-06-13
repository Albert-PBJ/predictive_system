from django.contrib import admin

from .models import (
    Category,
    Customer,
    ExchangeRate,
    Product,
    ProductPriceHistory,
    Seller,
    SystemSettings,
)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("sku", "name", "category", "sale_price_usd", "stock", "is_active")
    list_filter = ("category", "material", "is_active", "is_manufactured")
    search_fields = ("sku", "name", "full_name")
    list_editable = ("is_active",)


@admin.register(ProductPriceHistory)
class ProductPriceHistoryAdmin(admin.ModelAdmin):
    list_display = ("product", "sale_price_usd", "purchase_price_usd", "bcv_rate", "changed_at")
    list_filter = ("changed_at",)
    date_hierarchy = "changed_at"


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("company_name", "rif", "customer_type", "state", "is_active_customer")
    list_filter = ("customer_type", "state", "is_active_customer")
    search_fields = ("rif", "company_name", "contact_first_name", "contact_last_name")


@admin.register(Seller)
class SellerAdmin(admin.ModelAdmin):
    list_display = ("first_name", "last_name", "email", "commission_rate", "is_active")
    list_filter = ("is_active",)


@admin.register(ExchangeRate)
class ExchangeRateAdmin(admin.ModelAdmin):
    list_display = ("date", "bcv_rate", "parallel_rate", "source")
    date_hierarchy = "date"


@admin.register(SystemSettings)
class SystemSettingsAdmin(admin.ModelAdmin):
    """Singleton: una sola fila. Se impide crear más de una o borrarla."""

    def has_add_permission(self, request):
        from .models import SystemSettings as _S

        return not _S.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

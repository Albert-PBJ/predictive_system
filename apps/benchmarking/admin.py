from django.contrib import admin, messages

from .models import Competitor, CompetitorMarketData, RejectedMarketData, ScrapeRun


@admin.register(Competitor)
class CompetitorAdmin(admin.ModelAdmin):
    list_display = ("name", "state", "municipality", "is_active", "market_data_count")
    list_filter = ("is_active", "state")
    search_fields = ("name",)
    actions = ("merge_competitors",)

    @admin.display(description="Datos de mercado")
    def market_data_count(self, obj):
        return obj.market_data.count()

    @admin.action(description="Fusionar seleccionados (en el más antiguo)")
    def merge_competitors(self, request, queryset):
        """Funde varios competidores duplicados en uno solo (el de menor id).

        Reasigna sus datos de mercado y alertas al canónico, rellena los campos
        vacíos del canónico con los del duplicado y borra los duplicados. Resuelve
        a mano los duplicados que el dedupe difuso automático no haya unificado.
        """
        comps = list(queryset.order_by("id"))
        if len(comps) < 2:
            self.message_user(
                request,
                "Selecciona al menos dos competidores para fusionar.",
                level=messages.WARNING,
            )
            return

        # Import diferido: evita acoplar la carga del admin con la app analytics.
        from apps.analytics.models import Alert

        canonical = comps[0]
        merged = 0
        moved_records = 0
        for dup in comps[1:]:
            moved_records += CompetitorMarketData.objects.filter(competitor=dup).update(
                competitor=canonical
            )
            Alert.objects.filter(competitor=dup).update(competitor=canonical)
            # Rellena los campos vacíos del canónico con los del duplicado.
            for field in ("state", "municipality", "website", "instagram", "facebook", "notes"):
                if not getattr(canonical, field) and getattr(dup, field):
                    setattr(canonical, field, getattr(dup, field))
            dup.delete()
            merged += 1
        canonical.save()

        self.message_user(
            request,
            f"Se fusionaron {merged} competidor(es) en «{canonical.name}» "
            f"({moved_records} dato(s) de mercado reasignado(s)).",
            level=messages.SUCCESS,
        )


@admin.register(CompetitorMarketData)
class CompetitorMarketDataAdmin(admin.ModelAdmin):
    list_display = (
        "competitor_name", "source", "product_name", "price", "currency",
        "price_usd", "matched_product", "enriched_by", "is_in_stock", "posted_at", "scraped_at",
    )
    list_filter = ("source", "enriched_by", "is_in_stock", "scraped_at")
    search_fields = ("competitor_name", "product_name", "category")
    date_hierarchy = "scraped_at"
    list_select_related = ("product",)
    readonly_fields = ("listing_key", "price_usd", "exchange_rate_used", "rate_date", "scrape_run")

    @admin.display(description="Producto propio")
    def matched_product(self, obj):
        if obj.product_id:
            score = f" ({obj.product_match_score:.2f})" if obj.product_match_score is not None else ""
            return f"{obj.product.name}{score}"
        return "—"


@admin.register(ScrapeRun)
class ScrapeRunAdmin(admin.ModelAdmin):
    list_display = (
        "id", "source", "status", "records_collected", "records_saved",
        "records_discarded", "started_at", "finished_at",
    )
    list_filter = ("source", "status", "started_at")
    search_fields = ("apify_run_id", "dataset_id")
    date_hierarchy = "started_at"
    readonly_fields = tuple(
        f.name for f in ScrapeRun._meta.fields  # noqa: SLF001 — solo lectura: es un registro de auditoría
    )


@admin.register(RejectedMarketData)
class RejectedMarketDataAdmin(admin.ModelAdmin):
    list_display = (
        "source", "product_name", "price", "currency", "rejection_reason", "created_at",
    )
    list_filter = ("source", "created_at")
    search_fields = ("product_name", "competitor_name", "rejection_reason")
    date_hierarchy = "created_at"

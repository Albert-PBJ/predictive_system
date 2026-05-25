import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        # Old CompetitorMarketData table is dropped; new one uses a different db_table name
        ("competitor_market_data", "0002_delete_competitormarketdata"),
    ]

    operations = [
        # ── Competitor ────────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="Competitor",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(help_text="Nombre de la empresa competidora", max_length=150, unique=True)),
                ("state", models.CharField(blank=True, help_text="Estado venezolano donde opera", max_length=100)),
                ("municipality", models.CharField(blank=True, help_text="Municipio donde opera", max_length=100)),
                ("website", models.URLField(blank=True, help_text="Sitio web del competidor", max_length=500)),
                ("instagram", models.CharField(blank=True, help_text="URL o @usuario de Instagram", max_length=200)),
                ("facebook", models.CharField(blank=True, help_text="URL de la página de Facebook", max_length=200)),
                ("notes", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True, help_text="Competidor activo en el seguimiento")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Competidor",
                "verbose_name_plural": "Competidores",
                "db_table": "competitors",
                "ordering": ["name"],
            },
        ),
        # ── CompetitorMarketData ──────────────────────────────────────────────────
        migrations.CreateModel(
            name="CompetitorMarketData",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("competitor_name", models.CharField(blank=True, help_text="Nombre del competidor tal como lo devuelve el scraper", max_length=150, null=True)),
                ("source", models.CharField(choices=[("IG", "Instagram"), ("FB", "Facebook Marketplace"), ("WEB", "Página Web Directa"), ("OTH", "Otra Fuente")], default="WEB", max_length=3)),
                ("url", models.URLField(blank=True, max_length=500, null=True)),
                ("product_name", models.CharField(blank=True, max_length=255, null=True)),
                ("category", models.CharField(blank=True, max_length=100, null=True)),
                ("price", models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ("currency", models.CharField(blank=True, default="USD", max_length=3, null=True)),
                ("lead_time_days", models.IntegerField(blank=True, help_text="Tiempo de entrega estimado en días", null=True)),
                ("is_in_stock", models.BooleanField(blank=True, default=True, null=True)),
                ("promotions", models.CharField(blank=True, help_text="Promociones o descuentos detectados", max_length=255, null=True)),
                ("raw_metadata", models.JSONField(blank=True, help_text="Respuesta completa del scraper (Apify)", null=True)),
                ("scraped_at", models.DateTimeField(auto_now_add=True)),
                ("competitor", models.ForeignKey(blank=True, help_text="Competidor normalizado (FK); puede estar vacío si el scraper trae un nombre nuevo", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="market_data", to="benchmarking.competitor")),
            ],
            options={
                "verbose_name": "Dato de Mercado Competidor",
                "verbose_name_plural": "Datos de Mercado Competidores",
                "db_table": "benchmarking_competitor_market_data",
                "ordering": ["-scraped_at"],
            },
        ),
        # ── Indexes ───────────────────────────────────────────────────────────────
        migrations.AddIndex(
            model_name="competitormarketdata",
            index=models.Index(fields=["competitor", "scraped_at"], name="bmd_competitor_date_idx"),
        ),
        migrations.AddIndex(
            model_name="competitormarketdata",
            index=models.Index(fields=["competitor_name", "scraped_at"], name="bmd_name_date_idx"),
        ),
        migrations.AddIndex(
            model_name="competitormarketdata",
            index=models.Index(fields=["category", "scraped_at"], name="bmd_category_date_idx"),
        ),
    ]

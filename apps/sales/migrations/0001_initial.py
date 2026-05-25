import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        # ── Sale ─────────────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="Sale",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sale_date", models.DateField(help_text="Fecha de la venta")),
                ("sale_type", models.CharField(choices=[("RET", "Detal"), ("INST", "Proyecto Institucional")], default="RET", max_length=4)),
                ("total_sale_usd", models.DecimalField(decimal_places=2, default=0, help_text="Total de venta en USD", max_digits=12)),
                ("total_cost_usd", models.DecimalField(decimal_places=2, default=0, help_text="Total de costo en USD", max_digits=12)),
                ("total_profit_usd", models.DecimalField(decimal_places=2, default=0, help_text="Utilidad total en USD", max_digits=12)),
                ("total_sale_ves", models.DecimalField(blank=True, decimal_places=2, help_text="Total de venta en Bolívares", max_digits=18, null=True)),
                ("commission_usd", models.DecimalField(decimal_places=2, default=0, help_text="Comisión generada (% de la utilidad según tasa del vendedor)", max_digits=10)),
                ("bcv_rate", models.DecimalField(blank=True, decimal_places=4, help_text="Tasa BCV al momento de la venta", max_digits=12, null=True)),
                ("parallel_rate", models.DecimalField(blank=True, decimal_places=4, help_text="Tasa paralela al momento de la venta", max_digits=12, null=True)),
                ("status", models.CharField(choices=[("PEN", "Pendiente"), ("COMP", "Completada"), ("ANU", "Anulada")], default="COMP", max_length=4)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("customer", models.ForeignKey(help_text="Cliente de la venta", on_delete=django.db.models.deletion.PROTECT, related_name="sales", to="core.customer")),
                ("seller", models.ForeignKey(help_text="Vendedor responsable", on_delete=django.db.models.deletion.PROTECT, related_name="sales", to="core.seller")),
            ],
            options={
                "verbose_name": "Venta",
                "verbose_name_plural": "Ventas",
                "db_table": "sales",
                "ordering": ["-sale_date", "-created_at"],
            },
        ),
        # ── SaleItem ─────────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="SaleItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.PositiveIntegerField(default=1)),
                ("unit_sale_price_usd", models.DecimalField(decimal_places=2, help_text="Precio unitario de venta en USD", max_digits=10)),
                ("unit_cost_price_usd", models.DecimalField(decimal_places=2, help_text="Precio unitario de costo/compra en USD", max_digits=10)),
                ("subtotal_sale_usd", models.DecimalField(decimal_places=2, help_text="Subtotal de venta en USD (cantidad × precio venta)", max_digits=12)),
                ("subtotal_cost_usd", models.DecimalField(decimal_places=2, help_text="Subtotal de costo en USD (cantidad × precio compra)", max_digits=12)),
                ("line_profit_usd", models.DecimalField(decimal_places=2, help_text="Utilidad de la línea en USD", max_digits=12)),
                ("product", models.ForeignKey(help_text="Producto vendido", on_delete=django.db.models.deletion.PROTECT, related_name="sale_items", to="core.product")),
                ("sale", models.ForeignKey(help_text="Venta a la que pertenece esta línea", on_delete=django.db.models.deletion.CASCADE, related_name="items", to="sales.sale")),
            ],
            options={
                "verbose_name": "Línea de Venta",
                "verbose_name_plural": "Líneas de Venta",
                "db_table": "sale_items",
            },
        ),
        # ── Quote ─────────────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="Quote",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quote_number", models.CharField(help_text="Número de presupuesto (ej: 08052026-8)", max_length=50, unique=True)),
                ("issued_date", models.DateField(help_text="Fecha de emisión del presupuesto")),
                ("expiry_date", models.DateField(blank=True, help_text="Fecha de vencimiento del presupuesto", null=True)),
                ("bcv_rate", models.DecimalField(blank=True, decimal_places=4, help_text="Tasa BCV vigente al emitir", max_digits=12, null=True)),
                ("parallel_rate", models.DecimalField(blank=True, decimal_places=4, help_text="Tasa paralela vigente al emitir", max_digits=12, null=True)),
                ("includes_installation", models.BooleanField(default=False, help_text="¿El presupuesto incluye servicio de instalación?")),
                ("includes_delivery", models.BooleanField(default=False, help_text="¿El presupuesto incluye despacho/flete?")),
                ("subtotal_usd", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("subtotal_ves", models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True)),
                ("iva_rate", models.DecimalField(decimal_places=2, default=16.0, help_text="Porcentaje de IVA aplicado (por defecto 16%)", max_digits=5)),
                ("iva_amount_usd", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("total_usd", models.DecimalField(decimal_places=2, default=0, help_text="Total general en USD (subtotal + IVA)", max_digits=12)),
                ("total_ves", models.DecimalField(blank=True, decimal_places=2, help_text="Total general en Bolívares", max_digits=18, null=True)),
                ("status", models.CharField(choices=[("DRA", "Borrador"), ("SEN", "Enviado"), ("APR", "Aprobado"), ("REJ", "Rechazado"), ("CON", "Convertido a Venta")], default="DRA", max_length=3)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("customer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quotes", to="core.customer")),
                ("seller", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="quotes", to="core.seller")),
                ("converted_to_sale", models.ForeignKey(blank=True, help_text="Venta generada a partir de este presupuesto", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="source_quote", to="sales.sale")),
            ],
            options={
                "verbose_name": "Presupuesto",
                "verbose_name_plural": "Presupuestos",
                "db_table": "quotes",
                "ordering": ["-issued_date"],
            },
        ),
        # ── QuoteItem ─────────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="QuoteItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("quantity", models.PositiveIntegerField(default=1)),
                ("unit_price_usd", models.DecimalField(decimal_places=2, help_text="Precio unitario en USD", max_digits=10)),
                ("unit_price_ves", models.DecimalField(blank=True, decimal_places=2, help_text="Precio unitario en Bolívares", max_digits=18, null=True)),
                ("line_total_usd", models.DecimalField(decimal_places=2, help_text="Total de la línea en USD", max_digits=12)),
                ("line_total_ves", models.DecimalField(blank=True, decimal_places=2, help_text="Total de la línea en Bolívares", max_digits=18, null=True)),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quote_items", to="core.product")),
                ("quote", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="items", to="sales.quote")),
            ],
            options={
                "verbose_name": "Línea de Presupuesto",
                "verbose_name_plural": "Líneas de Presupuesto",
                "db_table": "quote_items",
            },
        ),
        # ── Indexes ───────────────────────────────────────────────────────────────
        migrations.AddIndex(
            model_name="sale",
            index=models.Index(fields=["sale_date"], name="sales_date_idx"),
        ),
        migrations.AddIndex(
            model_name="sale",
            index=models.Index(fields=["customer", "sale_date"], name="sales_customer_date_idx"),
        ),
        migrations.AddIndex(
            model_name="sale",
            index=models.Index(fields=["seller", "sale_date"], name="sales_seller_date_idx"),
        ),
        migrations.AddIndex(
            model_name="sale",
            index=models.Index(fields=["status"], name="sales_status_idx"),
        ),
        migrations.AddIndex(
            model_name="saleitem",
            index=models.Index(fields=["sale"], name="sale_items_sale_idx"),
        ),
        migrations.AddIndex(
            model_name="saleitem",
            index=models.Index(fields=["product"], name="sale_items_product_idx"),
        ),
        migrations.AddIndex(
            model_name="quote",
            index=models.Index(fields=["quote_number"], name="quotes_number_idx"),
        ),
        migrations.AddIndex(
            model_name="quote",
            index=models.Index(fields=["customer", "status"], name="quotes_customer_status_idx"),
        ),
        migrations.AddIndex(
            model_name="quote",
            index=models.Index(fields=["issued_date"], name="quotes_date_idx"),
        ),
    ]

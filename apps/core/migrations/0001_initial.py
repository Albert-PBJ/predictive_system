import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        # Drop the old products table before we recreate it here
        ("products", "0003_delete_product"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ── Category ────────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="Category",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(help_text="Nombre de la categoría de producto", max_length=100, unique=True)),
                ("slug", models.SlugField(max_length=100, unique=True)),
                ("description", models.TextField(blank=True, help_text="Descripción de la categoría")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Categoría",
                "verbose_name_plural": "Categorías",
                "db_table": "categories",
                "ordering": ["name"],
            },
        ),
        # ── Customer ─────────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="Customer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("rif", models.CharField(help_text="RIF o cédula fiscal venezolana", max_length=20, unique=True)),
                ("company_name", models.CharField(help_text="Razón social o nombre de la empresa", max_length=200)),
                ("customer_type", models.CharField(choices=[("INST", "Institucional"), ("CORP", "Empresarial"), ("IND", "Particular")], default="CORP", help_text="Tipo de cliente", max_length=4)),
                ("sector", models.CharField(blank=True, help_text="Sector industrial del cliente", max_length=100)),
                ("contact_first_name", models.CharField(blank=True, help_text="Nombre del contacto/representante", max_length=100)),
                ("contact_last_name", models.CharField(blank=True, help_text="Apellido del contacto/representante", max_length=100)),
                ("contact_ci", models.CharField(blank=True, help_text="Cédula de identidad del representante", max_length=15)),
                ("phone", models.CharField(blank=True, max_length=20)),
                ("mobile", models.CharField(blank=True, max_length=20)),
                ("email", models.EmailField(blank=True, max_length=254)),
                ("state", models.CharField(blank=True, help_text="Estado venezolano", max_length=100)),
                ("municipality", models.CharField(blank=True, help_text="Municipio", max_length=100)),
                ("parish", models.CharField(blank=True, help_text="Parroquia", max_length=100)),
                ("fiscal_address", models.TextField(blank=True, help_text="Dirección fiscal completa")),
                ("total_employees", models.IntegerField(blank=True, help_text="Total de trabajadores (para segmentar prospectos por tamaño)", null=True)),
                ("is_active_customer", models.BooleanField(default=False, help_text="True si ya es cliente activo; False si es solo prospecto")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Cliente",
                "verbose_name_plural": "Clientes",
                "db_table": "customers",
                "ordering": ["company_name"],
            },
        ),
        # ── ExchangeRate ─────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="ExchangeRate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("date", models.DateField(help_text="Fecha de la tasa de cambio", unique=True)),
                ("bcv_rate", models.DecimalField(decimal_places=4, help_text="Tasa BCV oficial (Bs por 1 USD)", max_digits=12)),
                ("parallel_rate", models.DecimalField(blank=True, decimal_places=4, help_text="Tasa paralela referencial (Bs por 1 USD)", max_digits=12, null=True)),
                ("source", models.CharField(choices=[("BCV", "BCV (Tasa Oficial)"), ("MON", "Monitor Dólar"), ("OTH", "Otra Fuente")], default="BCV", max_length=3)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Tasa de Cambio",
                "verbose_name_plural": "Tasas de Cambio",
                "db_table": "exchange_rates",
                "ordering": ["-date"],
            },
        ),
        # ── Seller ───────────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="Seller",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("first_name", models.CharField(max_length=100)),
                ("last_name", models.CharField(max_length=100)),
                ("phone", models.CharField(blank=True, max_length=20)),
                ("email", models.EmailField(blank=True, max_length=254)),
                ("commission_rate", models.DecimalField(decimal_places=2, default=10.0, help_text="Porcentaje de comisión sobre la utilidad (por defecto 10%)", max_digits=5)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(blank=True, help_text="Usuario de Django asociado al vendedor", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="seller_profile", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "Vendedor",
                "verbose_name_plural": "Vendedores",
                "db_table": "sellers",
                "ordering": ["last_name", "first_name"],
            },
        ),
        # ── Product ──────────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="Product",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sku", models.CharField(blank=True, help_text="Código/SKU del producto (ej: OK-6611N)", max_length=50, null=True, unique=True)),
                ("name", models.CharField(help_text="Nombre comercial del producto (ej: Stanford)", max_length=100)),
                ("full_name", models.CharField(blank=True, help_text="Nombre completo del producto", max_length=255)),
                ("material", models.CharField(blank=True, choices=[("MESH", "Malla Mesh"), ("BIPIEL", "Bipiel"), ("FABRIC", "Tela"), ("METAL", "Metal"), ("WOOD", "Madera/Melamina"), ("OTHER", "Otro")], max_length=10, null=True)),
                ("colors", models.JSONField(blank=True, default=list, help_text="Lista de colores disponibles")),
                ("seat_length_cm", models.DecimalField(blank=True, decimal_places=2, help_text="Largo del asiento en cm", max_digits=6, null=True)),
                ("seat_width_cm", models.DecimalField(blank=True, decimal_places=2, help_text="Ancho del asiento en cm", max_digits=6, null=True)),
                ("back_length_cm", models.DecimalField(blank=True, decimal_places=2, help_text="Largo del espaldar en cm", max_digits=6, null=True)),
                ("back_width_cm", models.DecimalField(blank=True, decimal_places=2, help_text="Ancho del espaldar en cm", max_digits=6, null=True)),
                ("min_height_cm", models.DecimalField(blank=True, decimal_places=2, help_text="Altura mínima en cm", max_digits=6, null=True)),
                ("max_height_cm", models.DecimalField(blank=True, decimal_places=2, help_text="Altura máxima en cm", max_digits=6, null=True)),
                ("desk_length_cm", models.DecimalField(blank=True, decimal_places=2, help_text="Largo del escritorio en cm", max_digits=6, null=True)),
                ("desk_width_cm", models.DecimalField(blank=True, decimal_places=2, help_text="Ancho del escritorio en cm", max_digits=6, null=True)),
                ("desk_height_cm", models.DecimalField(blank=True, decimal_places=2, help_text="Alto del escritorio en cm", max_digits=6, null=True)),
                ("purchase_price_usd", models.DecimalField(blank=True, decimal_places=2, help_text="Precio de compra actual en USD", max_digits=10, null=True)),
                ("sale_price_usd", models.DecimalField(decimal_places=2, help_text="Precio de venta actual en USD", max_digits=10)),
                ("stock", models.IntegerField(default=0, help_text="Cantidad en stock actualmente")),
                ("min_stock", models.IntegerField(default=0, help_text="Stock mínimo para disparar alerta de reabastecimiento")),
                ("is_manufactured", models.BooleanField(default=True, help_text="True si Maescar lo fabrica; False si es importado/revendido")),
                ("image", models.URLField(blank=True, help_text="URL de la imagen del producto", max_length=500, null=True)),
                ("is_active", models.BooleanField(default=True, help_text="Producto activo en el catálogo")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("category", models.ForeignKey(blank=True, help_text="Categoría del producto", null=True, on_delete=django.db.models.deletion.PROTECT, related_name="products", to="core.category")),
            ],
            options={
                "verbose_name": "Producto",
                "verbose_name_plural": "Productos",
                "db_table": "products",
                "ordering": ["category", "name"],
            },
        ),
        # ── ProductPriceHistory ──────────────────────────────────────────────────
        migrations.CreateModel(
            name="ProductPriceHistory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("purchase_price_usd", models.DecimalField(decimal_places=2, help_text="Precio de compra en USD", max_digits=10)),
                ("sale_price_usd", models.DecimalField(decimal_places=2, help_text="Precio de venta en USD", max_digits=10)),
                ("purchase_price_ves", models.DecimalField(blank=True, decimal_places=2, help_text="Precio de compra en Bolívares", max_digits=18, null=True)),
                ("sale_price_ves", models.DecimalField(blank=True, decimal_places=2, help_text="Precio de venta en Bolívares", max_digits=18, null=True)),
                ("bcv_rate", models.DecimalField(blank=True, decimal_places=4, help_text="Tasa BCV oficial en el momento del cambio", max_digits=12, null=True)),
                ("parallel_rate", models.DecimalField(blank=True, decimal_places=4, help_text="Tasa paralela en el momento del cambio", max_digits=12, null=True)),
                ("changed_at", models.DateField(help_text="Fecha del cambio de precio")),
                ("reason", models.CharField(blank=True, help_text="Motivo del cambio (ajuste de tasa, cambio de proveedor, promoción, etc.)", max_length=255)),
                ("product", models.ForeignKey(help_text="Producto al que corresponde el cambio de precio", on_delete=django.db.models.deletion.CASCADE, related_name="price_history", to="core.product")),
            ],
            options={
                "verbose_name": "Historial de Precio",
                "verbose_name_plural": "Historial de Precios",
                "db_table": "product_price_history",
                "ordering": ["-changed_at"],
            },
        ),
        # ── Indexes ──────────────────────────────────────────────────────────────
        migrations.AddIndex(
            model_name="customer",
            index=models.Index(fields=["rif"], name="customers_rif_idx"),
        ),
        migrations.AddIndex(
            model_name="customer",
            index=models.Index(fields=["is_active_customer", "customer_type"], name="customers_active_type_idx"),
        ),
        migrations.AddIndex(
            model_name="exchangerate",
            index=models.Index(fields=["date"], name="exchange_rates_date_idx"),
        ),
        migrations.AddIndex(
            model_name="product",
            index=models.Index(fields=["sku"], name="products_sku_idx"),
        ),
        migrations.AddIndex(
            model_name="product",
            index=models.Index(fields=["category", "is_active"], name="products_cat_active_idx"),
        ),
        migrations.AddIndex(
            model_name="productpricehistory",
            index=models.Index(fields=["product", "changed_at"], name="pph_product_date_idx"),
        ),
    ]

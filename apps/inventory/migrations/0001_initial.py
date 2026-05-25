import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("core", "0001_initial"),
        ("sales", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="InventoryMovement",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("movement_type", models.CharField(choices=[("ENT", "Entrada (Compra/Reposición)"), ("SAL", "Salida (Venta)"), ("AJU", "Ajuste"), ("DEV", "Devolución")], help_text="Tipo de movimiento de inventario", max_length=3)),
                ("quantity", models.IntegerField(help_text="Cantidad movida (positivo=entrada, negativo=salida)")),
                ("reference", models.CharField(blank=True, help_text="Referencia libre (ej: número de factura de compra, orden de despacho)", max_length=255)),
                ("movement_date", models.DateField(help_text="Fecha del movimiento")),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("product", models.ForeignKey(help_text="Producto al que corresponde el movimiento", on_delete=django.db.models.deletion.PROTECT, related_name="inventory_movements", to="core.product")),
                ("sale", models.ForeignKey(blank=True, help_text="Venta asociada si el movimiento es una salida por venta", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="inventory_movements", to="sales.sale")),
                ("responsible", models.ForeignKey(blank=True, help_text="Usuario responsable del movimiento", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="inventory_movements", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "Movimiento de Inventario",
                "verbose_name_plural": "Movimientos de Inventario",
                "db_table": "inventory_movements",
                "ordering": ["-movement_date", "-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="inventorymovement",
            index=models.Index(fields=["product", "movement_date"], name="invmov_product_date_idx"),
        ),
        migrations.AddIndex(
            model_name="inventorymovement",
            index=models.Index(fields=["movement_type"], name="invmov_type_idx"),
        ),
    ]

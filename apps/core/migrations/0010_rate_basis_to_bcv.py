"""Mueve la base de conversión USD→VES de «Paralelo» a «Dólar BCV».

El paralelo pasa a ser una referencia **analítica** (entender el valor real del dinero)
y las tasas **operativas** con las que se factura son las oficiales del BCV: Dólar BCV
y Euro BCV. La fila singleton de configuración quedaba en ``PAR`` de antes, así que se
migra explícitamente; si un administrador ya había elegido otra base (Euro BCV o
promedio) no se toca.

No afecta a ninguna venta/presupuesto ya registrado: cada uno guarda su propia foto de
las tasas y sus totales en VES ya calculados. Tampoco afecta a la analítica ni al ML,
que trabajan sobre los montos en **USD**.
"""

from django.db import migrations


def set_bcv_basis(apps, schema_editor):
    SystemSettings = apps.get_model("core", "SystemSettings")
    SystemSettings.objects.filter(pk=1, rate_basis="PAR").update(rate_basis="BCV")


def back_to_parallel(apps, schema_editor):
    SystemSettings = apps.get_model("core", "SystemSettings")
    SystemSettings.objects.filter(pk=1, rate_basis="BCV").update(rate_basis="PAR")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0009_exchangerate_eur_bcv_rate_and_more"),
    ]

    operations = [
        migrations.RunPython(set_bcv_basis, back_to_parallel),
    ]

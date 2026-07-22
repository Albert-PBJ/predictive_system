"""Siembra los datos de contacto reales de la empresa en la configuración.

Añade los valores por defecto reales (dirección, teléfonos, correo) a los campos de
empresa de ``SystemSettings`` y **rellena la fila singleton existente** solo donde esté
en blanco, de modo que los presupuestos y órdenes de despacho muestren la info de la
empresa de inmediato sin sobrescribir lo que el administrador ya haya configurado.
"""

from django.db import migrations, models

_COMPANY_ADDRESS = (
    "Av. Principal de Paraparal, Centro Comercial Paraparal Plaza, "
    "Local Apb-05, Municipio Los Guayos, Valencia, Edo. Carabobo"
)
_COMPANY_PHONE = "0414-434.44.52 / 0414-4704347"
_COMPANY_EMAIL = "inversiones.maescar@gmail.com"
_COMPANY_NAME = "Inversiones Maescar, C.A."


def backfill_company_info(apps, schema_editor):
    SystemSettings = apps.get_model("core", "SystemSettings")
    obj = SystemSettings.objects.filter(pk=1).first()
    if obj is None:
        return  # aún no existe el singleton; los defaults del modelo lo cubrirán al crearse
    defaults = {
        "company_name": _COMPANY_NAME,
        "company_address": _COMPANY_ADDRESS,
        "company_phone": _COMPANY_PHONE,
        "company_email": _COMPANY_EMAIL,
    }
    changed = False
    for field, value in defaults.items():
        if not (getattr(obj, field, "") or "").strip():
            setattr(obj, field, value)
            changed = True
    if changed:
        obj.save()


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_product_average_cost_usd_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="systemsettings",
            name="company_address",
            field=models.TextField(
                blank=True,
                default=_COMPANY_ADDRESS,
                help_text="Dirección fiscal (aparece en presupuestos y órdenes de despacho).",
            ),
        ),
        migrations.AlterField(
            model_name="systemsettings",
            name="company_phone",
            field=models.CharField(
                blank=True, default=_COMPANY_PHONE, max_length=50,
                help_text="Teléfono(s) de contacto.",
            ),
        ),
        migrations.AlterField(
            model_name="systemsettings",
            name="company_email",
            field=models.EmailField(
                blank=True, default=_COMPANY_EMAIL, max_length=254,
                help_text="Correo de contacto.",
            ),
        ),
        migrations.RunPython(backfill_company_info, noop),
    ]

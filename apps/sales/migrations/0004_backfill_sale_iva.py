"""Rellena el desglose de IVA en las ventas existentes.

Las ventas creadas antes de añadir los campos de IVA quedaron con `iva_amount_usd`
y `total_with_iva_usd` en 0. Este backfill los calcula a partir de la base imponible
(`total_sale_usd`) y la tasa (`iva_rate`, 16% por defecto), para que el desglose
mostrado en el historial sea coherente sin necesidad de resembrar la base.
"""

from decimal import Decimal, ROUND_HALF_UP

from django.db import migrations

CENTS = Decimal("0.01")


def _q(value):
    return (value or Decimal("0")).quantize(CENTS, rounding=ROUND_HALF_UP)


def backfill_iva(apps, schema_editor):
    Sale = apps.get_model("sales", "Sale")
    qs = Sale.objects.filter(total_with_iva_usd=0).exclude(total_sale_usd=0)
    for sale in qs.iterator():
        rate = sale.iva_rate if sale.iva_rate is not None else Decimal("16")
        iva = _q(sale.total_sale_usd * rate / Decimal("100"))
        total = _q(sale.total_sale_usd + iva)
        sale.iva_amount_usd = iva
        sale.total_with_iva_usd = total
        if sale.total_sale_ves and sale.total_sale_usd:
            factor = total / sale.total_sale_usd
            sale.total_with_iva_ves = _q(sale.total_sale_ves * factor)
        sale.save(update_fields=["iva_amount_usd", "total_with_iva_usd", "total_with_iva_ves"])


def noop(apps, schema_editor):
    # No revierte los montos: dejar el desglose calculado es inofensivo.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0003_sale_control_number_sale_invoice_date_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_iva, noop),
    ]

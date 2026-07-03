from django.db import migrations, models


class Migration(migrations.Migration):
    """Campos de control del procesamiento reanudable + parada del scraping.

    Añade a ScrapeRun la señal de parada cooperativa (`stop_requested`) y el
    progreso de checkpoint (`processed_items`/`total_items`) para poder detener
    un run guardando lo procesado y reanudar lo pendiente. Extiende el estado con
    PROCESSING (procesando por lotes) y STOPPED (detenido por el usuario).
    """

    dependencies = [
        ("benchmarking", "0005_remove_competitor_facebook"),
    ]

    operations = [
        migrations.AddField(
            model_name="scraperun",
            name="stop_requested",
            field=models.BooleanField(
                default=False, help_text="El usuario pidió detener el run"
            ),
        ),
        migrations.AddField(
            model_name="scraperun",
            name="processed_items",
            field=models.IntegerField(
                default=0,
                help_text="Unidades del dataset ya procesadas (offset de checkpoint)",
            ),
        ),
        migrations.AddField(
            model_name="scraperun",
            name="total_items",
            field=models.IntegerField(
                default=0, help_text="Total de unidades a procesar del dataset"
            ),
        ),
        migrations.AlterField(
            model_name="scraperun",
            name="status",
            field=models.CharField(
                choices=[
                    ("RUN", "En ejecución"),
                    ("PRO", "Procesando"),
                    ("OK", "Completado"),
                    ("STP", "Detenido por el usuario"),
                    ("ERR", "Fallido"),
                ],
                default="RUN",
                max_length=3,
            ),
        ),
    ]

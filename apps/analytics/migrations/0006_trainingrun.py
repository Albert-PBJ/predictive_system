# Generated for the training-history feature.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('analytics', '0005_alert_audience_alert_dedupe_key_alert_updated_at_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='TrainingRun',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('trained_at', models.DateTimeField(help_text='Momento del reentrenamiento')),
                ('trigger', models.CharField(choices=[('CMD', 'Comando (train_models)'), ('UI', 'Panel (botón Reentrenar)')], default='CMD', help_text='Origen del reentrenamiento: comando o botón del panel', max_length=3)),
                ('triggered_by', models.CharField(blank=True, help_text='Usuario que disparó el reentrenamiento (vacío para el comando/sistema)', max_length=150)),
                ('models_metrics', models.JSONField(blank=True, default=list)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'verbose_name': 'Reentrenamiento',
                'verbose_name_plural': 'Reentrenamientos',
                'db_table': 'training_runs',
                'ordering': ['trained_at'],
                'indexes': [models.Index(fields=['trained_at'], name='trainrun_trained_idx')],
            },
        ),
    ]

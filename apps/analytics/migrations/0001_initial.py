import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("benchmarking", "0001_initial"),
        ("core", "0001_initial"),
    ]

    operations = [
        # ── PredictionLog ─────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="PredictionLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(help_text="Nombre descriptivo del modelo (ej: demand_forecast_xgboost_v3)", max_length=200)),
                ("model_type", models.CharField(choices=[("DEMAND", "Pronóstico de Demanda"), ("PRICE", "Tendencia de Precios"), ("SEASON", "Patrón Estacional"), ("BENCH", "Benchmarking de Competidores")], max_length=6)),
                ("r2_score", models.FloatField(blank=True, help_text="Coeficiente de determinación R²", null=True)),
                ("rmse", models.FloatField(blank=True, help_text="Raíz del Error Cuadrático Medio (RMSE)", null=True)),
                ("mae", models.FloatField(blank=True, help_text="Error Absoluto Medio (MAE)", null=True)),
                ("metrics", models.JSONField(blank=True, default=dict, help_text="Métricas adicionales del modelo")),
                ("hyperparameters", models.JSONField(blank=True, default=dict, help_text="Hiperparámetros usados en el entrenamiento")),
                ("trained_at", models.DateTimeField(help_text="Fecha y hora en que se entrenó el modelo")),
                ("dataset_description", models.TextField(blank=True, help_text="Descripción del dataset usado para entrenar")),
                ("model_file_path", models.CharField(blank=True, help_text="Ruta al archivo del modelo serializado (.pkl, .joblib, etc.)", max_length=500)),
                ("is_active", models.BooleanField(default=False, help_text="True si es el modelo activo en producción para su tipo")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Log de Predicción",
                "verbose_name_plural": "Logs de Predicción",
                "db_table": "prediction_logs",
                "ordering": ["-trained_at"],
            },
        ),
        # ── KPI ───────────────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="KPI",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(help_text="Identificador del KPI (ej: rotacion_inventario_mensual, margen_promedio)", max_length=100)),
                ("value", models.DecimalField(decimal_places=4, help_text="Valor numérico calculado", max_digits=18)),
                ("unit", models.CharField(blank=True, help_text="Unidad del KPI: %, USD, días, índice, etc.", max_length=20)),
                ("period_month", models.PositiveSmallIntegerField(blank=True, help_text="Mes del período (1–12)", null=True)),
                ("period_year", models.PositiveSmallIntegerField(blank=True, help_text="Año del período", null=True)),
                ("category", models.CharField(choices=[("FIN", "Financiero"), ("INV", "Inventario"), ("VEN", "Ventas"), ("COM", "Competencia")], max_length=3)),
                ("calculated_at", models.DateTimeField(auto_now_add=True, help_text="Timestamp en que se calculó el KPI")),
                ("metadata", models.JSONField(blank=True, default=dict, help_text="Datos adicionales del cálculo (breakdown, fuentes, etc.)")),
            ],
            options={
                "verbose_name": "KPI",
                "verbose_name_plural": "KPIs",
                "db_table": "kpis",
                "ordering": ["-calculated_at"],
            },
        ),
        # ── Alert ─────────────────────────────────────────────────────────────────
        migrations.CreateModel(
            name="Alert",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("alert_type", models.CharField(choices=[("STOCK_B", "Quiebre de Stock"), ("STOCK_O", "Sobrestock"), ("PRICE", "Cambio de Precio Competidor"), ("DEMAND", "Caída de Demanda"), ("GOAL", "Meta Cumplida")], max_length=7)),
                ("severity", models.CharField(choices=[("INFO", "Información"), ("WARN", "Advertencia"), ("CRIT", "Crítico")], default="INFO", max_length=4)),
                ("title", models.CharField(max_length=200)),
                ("message", models.TextField()),
                ("is_read", models.BooleanField(default=False)),
                ("is_resolved", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("competitor", models.ForeignKey(blank=True, help_text="Competidor relacionado con la alerta (si aplica)", null=True, on_delete=django.db.models.deletion.CASCADE, related_name="alerts", to="benchmarking.competitor")),
                ("product", models.ForeignKey(blank=True, help_text="Producto relacionado con la alerta (si aplica)", null=True, on_delete=django.db.models.deletion.CASCADE, related_name="alerts", to="core.product")),
            ],
            options={
                "verbose_name": "Alerta",
                "verbose_name_plural": "Alertas",
                "db_table": "alerts",
                "ordering": ["-created_at"],
            },
        ),
        # ── Indexes ───────────────────────────────────────────────────────────────
        migrations.AddIndex(
            model_name="predictionlog",
            index=models.Index(fields=["model_type", "is_active"], name="predlog_type_active_idx"),
        ),
        migrations.AddIndex(
            model_name="kpi",
            index=models.Index(fields=["name", "period_year", "period_month"], name="kpis_name_period_idx"),
        ),
        migrations.AddIndex(
            model_name="kpi",
            index=models.Index(fields=["category"], name="kpis_category_idx"),
        ),
        migrations.AddIndex(
            model_name="alert",
            index=models.Index(fields=["is_read", "is_resolved", "severity"], name="alerts_read_resolved_sev_idx"),
        ),
        migrations.AddIndex(
            model_name="alert",
            index=models.Index(fields=["alert_type"], name="alerts_type_idx"),
        ),
    ]

"""Entrena y evalúa todos los modelos del módulo predictivo.

Para los objetivos de serie temporal entrena las **tres técnicas** (regresión lineal,
árbol de decisión y XGBoost) para poder compararlas en la tesis, registra las métricas
de cada una en ``PredictionLog`` y marca como activa la técnica asignada a ese gráfico.
Es idempotente (vuelve a calcular y reescribe el registro).

Nota: el servicio entrena bajo demanda y cachea (ml.registry), así que este comando NO
es obligatorio para que la API funcione; sirve para poblar el panel de registro de
modelos y dejar evidencia reproducible de las métricas.

    python manage.py train_models
    python manage.py train_models --product 133   # producto base para demanda/precio
"""

from __future__ import annotations

import warnings

from django.core.management.base import BaseCommand
from django.db.models import Sum

from apps.analytics.ml import forecasters as F
from apps.analytics.ml import registry
from apps.analytics.models import PredictionLog
from apps.sales.models import SaleItem

ALL_MODELS = ["linear", "tree", "xgboost"]


class Command(BaseCommand):
    help = "Entrena, evalúa y registra los modelos del módulo predictivo (apps/analytics)."

    def add_arguments(self, parser):
        parser.add_argument("--product", type=int, default=None,
                            help="ID de producto base para demanda/precio (por defecto el más vendido).")

    def handle(self, *args, **opts):
        warnings.filterwarnings("ignore")
        registry.clear_cache()

        top_pid = opts["product"] or self._top_product()
        if not top_pid:
            self.stderr.write(self.style.ERROR("No hay ventas cargadas; nada que entrenar."))
            return
        self.stdout.write(self.style.NOTICE(f"Producto base para demanda/precio: #{top_pid}\n"))

        # (etiqueta, tipo PredictionLog, clave en ASSIGNED_MODEL, builder(model_key))
        series = [
            ("Ventas e ingresos", "SALES", "sales", lambda mk: F.forecast_sales("revenue", 6, mk)),
            ("Utilidad y margen", "PROFIT", "profit", lambda mk: F.forecast_profit(6, mk)),
            ("Tasa de cambio (BCV)", "RATE", "exchange_rate", lambda mk: F.forecast_exchange_rate("bcv", 6, mk)),
            ("Precio de producto", "PRICE", "product_price", lambda mk: F.forecast_product_price(top_pid, 6, mk)),
            ("Demanda por producto", "DEMAND", "demand", lambda mk: F.forecast_demand(top_pid, 6, mk)),
        ]

        PredictionLog.objects.all().delete()  # idempotente: registro limpio
        self.stdout.write(f"{'Objetivo':24} {'Modelo':10} {'R²':>8} {'RMSE':>10} {'MAE':>10}  activo")
        self.stdout.write("-" * 76)

        for label, ptype, akey, builder in series:
            assigned = F.ASSIGNED_MODEL[akey]
            for mk in ALL_MODELS:
                try:
                    res = builder(mk)
                except Exception as exc:  # pragma: no cover
                    self.stderr.write(self.style.WARNING(f"  {label}/{mk}: {exc}"))
                    continue
                model = res.get("model") or {}
                metrics = {"r2": model.get("r2"), "rmse": model.get("rmse"), "mae": model.get("mae")}
                is_assigned = mk == assigned
                registry.upsert_prediction_log(
                    name=f"{akey}_{mk}", model_type=ptype, metrics=metrics,
                    hyperparameters=model.get("hyperparameters", {}),
                    dataset_description=f"{label} — n_train={model.get('n_train')}, holdout={model.get('n_holdout')}",
                    make_active=is_assigned,
                )
                self.stdout.write(
                    f"{label:24} {mk:10} {self._f(metrics['r2']):>8} "
                    f"{self._f(metrics['rmse']):>10} {self._f(metrics['mae']):>10}  {'<= activo' if is_assigned else ''}"
                )

        # Conversión de presupuestos (clasificación): exactitud/precisión/recall.
        self.stdout.write("-" * 76)
        for mk in ALL_MODELS:
            try:
                q = F.forecast_quote_conversion(mk)
            except Exception as exc:  # pragma: no cover
                self.stderr.write(self.style.WARNING(f"  quote/{mk}: {exc}"))
                continue
            model = q.get("model") or {}
            metrics = {"accuracy": model.get("accuracy"), "precision": model.get("precision"),
                       "recall": model.get("recall")}
            is_assigned = mk == F.ASSIGNED_MODEL["quote"]
            registry.upsert_prediction_log(
                name=f"quote_{mk}", model_type="QUOTE", metrics=metrics,
                hyperparameters=model.get("hyperparameters", {}),
                dataset_description=f"Conversión de presupuestos — n_train={model.get('n_train')}",
                make_active=is_assigned,
            )
            self.stdout.write(
                f"{'Conversion presup.':24} {mk:10} acc={self._f(metrics['accuracy'])} "
                f"prec={self._f(metrics['precision'])} rec={self._f(metrics['recall'])}  {'<= activo' if is_assigned else ''}"
            )

        # Reabastecimiento (derivado de la demanda) y competencia (tendencia lineal).
        try:
            inv = F.forecast_inventory(top_pid, 6)
            registry.upsert_prediction_log(
                name="inventory_xgboost", model_type="INVENT",
                metrics={k: (inv.get("model") or {}).get(k) for k in ("r2", "rmse", "mae")},
                hyperparameters=(inv.get("model") or {}).get("hyperparameters", {}),
                dataset_description="Reabastecimiento derivado del modelo panel de demanda.",
                make_active=True,
            )
        except Exception as exc:  # pragma: no cover
            self.stderr.write(self.style.WARNING(f"  inventory: {exc}"))

        try:
            comp = F.competitor_analysis()
            cmodel = comp.get("model")
            if cmodel:
                registry.upsert_prediction_log(
                    name="competitor_trend_linear", model_type="BENCH",
                    metrics={"slope_usd_per_month": cmodel.get("slope_usd_per_month")},
                    dataset_description=f"Tendencia de precios de competencia (n_obs={comp.get('meta', {}).get('n_obs')}).",
                    make_active=True,
                )
        except Exception as exc:  # pragma: no cover
            self.stderr.write(self.style.WARNING(f"  competitor: {exc}"))

        registry.clear_cache()
        n = PredictionLog.objects.count()
        self.stdout.write(self.style.SUCCESS(f"\nListo. {n} filas escritas en PredictionLog."))

    @staticmethod
    def _top_product():
        row = (SaleItem.objects.filter(sale__status="COMP")
               .values("product_id").annotate(u=Sum("quantity")).order_by("-u").first())
        return row["product_id"] if row else None

    @staticmethod
    def _f(v):
        return f"{v:.4f}" if isinstance(v, (int, float)) else "—"

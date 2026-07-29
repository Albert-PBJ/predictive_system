"""API REST del módulo predictivo (apps/analytics).

Vistas ``APIView`` (estilo dict, como las del scraper) que exponen cada pronóstico.
Todas requieren rol **Gerente o Administrador** (``IsManager``): los pronósticos son
herramientas de decisión estratégica "para el dueño".

El servicio entrena bajo demanda y cachea el resultado (``ml.registry.cached``),
invalidándolo cuando cambian los datos. Se puede sobreescribir el modelo por
``?model=linear|tree|xgboost`` para experimentar/comparar (la UI fija uno por gráfico).
"""

from __future__ import annotations

import io
import logging
from datetime import date

from django.core.management import call_command
from django.db.models import Count, Sum
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsManager, IsViewer
from apps.audit import services as audit
from apps.audit.models import ActionChoices
from apps.core import system_settings
from apps.core.models import SERVICE_SKU_PREFIX, Product
from apps.sales.models import SaleItem

from . import benchmarking, forecast_advice, report_narrative, stats
from .ml import datasets as D
from .ml import forecasters as F
from .ml import registry
from .models import PredictionLog, TrainingRun

logger = logging.getLogger(__name__)

VALID_MODELS = {"linear", "tree", "xgboost"}


def _horizon(request, default=6):
    try:
        h = int(request.query_params.get("horizon", default))
    except (TypeError, ValueError):
        h = default
    return max(1, min(h, 18))


def _model(request):
    m = request.query_params.get("model")
    return m if m in VALID_MODELS else None


def _int(request, key):
    try:
        return int(request.query_params.get(key))
    except (TypeError, ValueError):
        return None


def _date(request, key, fallback: date) -> date:
    value = request.query_params.get(key)
    try:
        return date.fromisoformat(value) if value else fallback
    except (ValueError, TypeError):
        return fallback


class _BaseForecastView(APIView):
    permission_classes = [IsManager]


# --------------------------------------------------------------------------- #
# Lista de productos pronosticables (para los selectores)
# --------------------------------------------------------------------------- #
class ForecastableProductsView(_BaseForecastView):
    """GET /api/analytics/forecastable-products — productos con historial de ventas."""

    def get(self, request):
        rows = (
            SaleItem.objects.filter(sale__status="COMP")
            .values("product_id")
            .annotate(units=Sum("quantity"), n=Count("id"))
            .order_by("-units")
        )
        units_by_id = {r["product_id"]: (r["units"], r["n"]) for r in rows}
        products = Product.objects.filter(id__in=units_by_id.keys()).select_related("category")
        out = []
        for p in products:
            units, n = units_by_id.get(p.id, (0, 0))
            out.append({
                "id": p.id, "name": p.name, "sku": p.sku,
                "category": p.category.name if p.category else None,
                "stock": p.stock, "sale_price_usd": float(p.sale_price_usd or 0),
                "total_units_sold": int(units or 0), "n_sales": int(n or 0),
            })
        out.sort(key=lambda d: d["total_units_sold"], reverse=True)
        return Response({"results": out})


# --------------------------------------------------------------------------- #
# Pronósticos de series temporales
# --------------------------------------------------------------------------- #
class DemandForecastView(_BaseForecastView):
    def get(self, request):
        pid = _int(request, "product")
        if not pid:
            return Response({"detail": "Falta el parámetro 'product'."}, status=status.HTTP_400_BAD_REQUEST)
        h, m = _horizon(request), _model(request)
        key = f"demand:{pid}:{h}:{m}"
        return Response(registry.cached(key, lambda: F.forecast_demand(pid, h, m)))


class SalesForecastView(_BaseForecastView):
    def get(self, request):
        metric = request.query_params.get("metric", "revenue")
        metric = metric if metric in ("revenue", "count") else "revenue"
        h, m = _horizon(request), _model(request)
        key = f"sales:{metric}:{h}:{m}"
        return Response(registry.cached(key, lambda: F.forecast_sales(metric, h, m)))


class ProfitForecastView(_BaseForecastView):
    def get(self, request):
        h, m = _horizon(request), _model(request)
        key = f"profit:{h}:{m}"
        return Response(registry.cached(key, lambda: F.forecast_profit(h, m)))


class ExchangeRateForecastView(_BaseForecastView):
    def get(self, request):
        rate = request.query_params.get("rate", "bcv")
        rate = rate if rate in F.RATE_SERIES else "bcv"
        h, m = _horizon(request), _model(request)
        key = f"rate:{rate}:{h}:{m}"
        return Response(registry.cached(key, lambda: F.forecast_exchange_rate(rate, h, m)))


class ProductPriceForecastView(_BaseForecastView):
    def get(self, request):
        pid = _int(request, "product")
        if not pid:
            return Response({"detail": "Falta el parámetro 'product'."}, status=status.HTTP_400_BAD_REQUEST)
        h, m = _horizon(request), _model(request)
        key = f"price:{pid}:{h}:{m}"
        return Response(registry.cached(key, lambda: F.forecast_product_price(pid, h, m)))


class InventoryForecastView(_BaseForecastView):
    def get(self, request):
        pid = _int(request, "product")
        if not pid:
            return Response({"detail": "Falta el parámetro 'product'."}, status=status.HTTP_400_BAD_REQUEST)
        h = _horizon(request)
        key = f"inventory:{pid}:{h}"
        return Response(registry.cached(key, lambda: F.forecast_inventory(pid, h)))


class QuoteConversionForecastView(_BaseForecastView):
    def get(self, request):
        m = _model(request)
        key = f"quote:{m}"
        return Response(registry.cached(key, lambda: F.forecast_quote_conversion(m)))


class ForecastAdviceView(_BaseForecastView):
    """GET /api/analytics/forecast/advice?target=&product=&horizon=&metric=&rate=&model=

    Lectura accionable de un gráfico de pronóstico, redactada por el LLM (cae a un consejo
    determinista si el LLM no está disponible). Reutiliza el **mismo caché** del pronóstico
    (mismas claves que las vistas de arriba), así que normalmente no recalcula nada. La
    respuesta del LLM se cachea aparte (solo cuando es válida) para no repetir llamadas.
    """

    def get(self, request):
        target = request.query_params.get("target")
        h, m = _horizon(request), _model(request)
        pid = _int(request, "product")

        if target == "demand":
            if not pid:
                return Response({"detail": "Falta el parámetro 'product'."}, status=status.HTTP_400_BAD_REQUEST)
            fc_key = f"demand:{pid}:{h}:{m}"
            builder = lambda: F.forecast_demand(pid, h, m)  # noqa: E731
        elif target == "sales":
            metric = request.query_params.get("metric", "revenue")
            metric = metric if metric in ("revenue", "count") else "revenue"
            fc_key = f"sales:{metric}:{h}:{m}"
            builder = lambda: F.forecast_sales(metric, h, m)  # noqa: E731
        elif target == "profit":
            fc_key = f"profit:{h}:{m}"
            builder = lambda: F.forecast_profit(h, m)  # noqa: E731
        elif target == "exchange-rate":
            rate = request.query_params.get("rate", "bcv")
            rate = rate if rate in F.RATE_SERIES else "bcv"
            fc_key = f"rate:{rate}:{h}:{m}"
            builder = lambda: F.forecast_exchange_rate(rate, h, m)  # noqa: E731
        elif target == "product-price":
            if not pid:
                return Response({"detail": "Falta el parámetro 'product'."}, status=status.HTTP_400_BAD_REQUEST)
            fc_key = f"price:{pid}:{h}:{m}"
            builder = lambda: F.forecast_product_price(pid, h, m)  # noqa: E731
        elif target == "inventory":
            if not pid:
                return Response({"detail": "Falta el parámetro 'product'."}, status=status.HTTP_400_BAD_REQUEST)
            fc_key = f"inventory:{pid}:{h}"
            builder = lambda: F.forecast_inventory(pid, h)  # noqa: E731
        elif target == "quote":
            fc_key = f"quote:{m}"
            builder = lambda: F.forecast_quote_conversion(m)  # noqa: E731
        else:
            return Response({"detail": "Parámetro 'target' inválido."}, status=status.HTTP_400_BAD_REQUEST)

        payload = registry.cached(fc_key, builder)

        # El consejo del LLM se cachea aparte y SOLO cuando es válido (available=True), para
        # no "congelar" un fallback determinista por un fallo transitorio de red.
        advice_key = f"advice:{fc_key}"
        found, advice = registry.get_cached(advice_key)
        if not found:
            advice = forecast_advice.generate(payload, target=target)
            if advice.get("available"):
                registry.set_cached(advice_key, advice)
        return Response(advice)


# --------------------------------------------------------------------------- #
# Análisis de competencia (SEPARADO de los datos internos)
# --------------------------------------------------------------------------- #
class CompetitorAnalysisView(_BaseForecastView):
    def get(self, request):
        category = request.query_params.get("category") or None
        pid = _int(request, "product")
        key = f"competitor:{category}:{pid}"
        return Response(registry.cached(key, lambda: F.competitor_analysis(category, pid)))


# --------------------------------------------------------------------------- #
# Benchmarking Competitivo ("máquina del tiempo": rango sobre la fecha efectiva de
# la observación — posted_at en Instagram, scraped_at en el resto)
# --------------------------------------------------------------------------- #
class BenchmarkingComparisonView(_BaseForecastView):
    """GET /api/analytics/benchmarking/comparison?from=&to= — radiografía descriptiva
    de la competencia para el rango (no se cachea: agregación directa y barata)."""

    def get(self, request):
        default_start, default_end = benchmarking.default_range()
        start = _date(request, "from", default_start)
        end = _date(request, "to", default_end)
        competitor = request.query_params.get("competitor") or None
        return Response(benchmarking.comparison(start, end, competitor))


class BenchmarkingForecastView(_BaseForecastView):
    """GET /api/analytics/benchmarking/forecast?from=&to=&horizon=&category=&competitor= —
    pronóstico del precio de mercado vs. nuestros precios (entrena bajo demanda + cachea)."""

    def get(self, request):
        default_start, default_end = benchmarking.default_range()
        start = _date(request, "from", default_start)
        end = _date(request, "to", default_end)
        h, category = _horizon(request), (request.query_params.get("category") or None)
        competitor = request.query_params.get("competitor") or None
        key = f"benchmark_fc:{start.isoformat()}:{end.isoformat()}:{h}:{category}:{competitor}"
        return Response(registry.cached(key, lambda: F.competitor_forecast(start, end, h, category, competitor)))


class BenchmarkingProductForecastView(_BaseForecastView):
    """GET /api/analytics/benchmarking/product-forecast?product=&competitor=&horizon=&from=&to=
    — precio de un competidor (o promedio de todos) vs. nuestro precio interno, para un
    producto propio con equivalente en la competencia."""

    def get(self, request):
        pid = _int(request, "product")
        if not pid:
            return Response({"detail": "Falta el parámetro 'product'."}, status=status.HTTP_400_BAD_REQUEST)
        default_start, default_end = benchmarking.default_range()
        start = _date(request, "from", default_start)
        end = _date(request, "to", default_end)
        h = _horizon(request)
        competitor = request.query_params.get("competitor") or None
        key = f"benchmark_pf:{pid}:{competitor}:{start.isoformat()}:{end.isoformat()}:{h}"
        return Response(registry.cached(key, lambda: F.competitor_product_forecast(pid, competitor, h, start, end)))


# --------------------------------------------------------------------------- #
# Panel resumen
# --------------------------------------------------------------------------- #
class OverviewView(_BaseForecastView):
    """GET /api/analytics/overview — titulares + registro de modelos para el panel."""

    def get(self, request):
        return Response(registry.cached("overview", self._build))

    @staticmethod
    def _build():
        sales = F.forecast_sales("revenue", 6)
        bcv = F.forecast_exchange_rate("bcv", 6)
        eur = F.forecast_exchange_rate("eur", 6)
        parallel = F.forecast_exchange_rate("parallel", 6)
        quote = F.forecast_quote_conversion()

        def first(fc):
            f = fc.get("forecast") or []
            return f[0] if f else None

        # Reabastecimiento: top productos por unidades vendidas que necesitan reorden.
        # Se excluyen los servicios (sin inventario, no se reabastecen).
        top = (
            SaleItem.objects.filter(sale__status="COMP")
            .exclude(product__sku__startswith=SERVICE_SKU_PREFIX)
            .values("product_id")
            .annotate(units=Sum("quantity"))
            .order_by("-units")[:8]
        )
        restock = []
        for r in top:
            inv = F.forecast_inventory(r["product_id"], 6)
            meta = inv.get("meta", {})
            if meta.get("needs_reorder"):
                restock.append({
                    "product_id": r["product_id"],
                    "product_name": (inv.get("subject") or {}).get("product_name"),
                    "current_stock": meta.get("current_stock"),
                    "reorder_point": meta.get("reorder_point"),
                    "suggested_reorder_qty": meta.get("suggested_reorder_qty"),
                    "stockout_label": meta.get("stockout_label"),
                    "months_of_cover": meta.get("months_of_cover"),
                })

        # Registro de modelos (filas activas de PredictionLog, si se corrió train_models).
        registry_rows = [
            {
                "name": pl.name, "model_type": pl.model_type,
                "model_type_display": pl.get_model_type_display(),
                "r2": pl.r2_score, "rmse": pl.rmse, "mae": pl.mae,
                "metrics": pl.metrics, "hyperparameters": pl.hyperparameters,
                "trained_at": pl.trained_at.isoformat() if pl.trained_at else None,
            }
            for pl in PredictionLog.objects.filter(is_active=True).order_by("model_type")
        ]

        return {
            "headlines": {
                "next_revenue": first(sales),
                "revenue_model": sales.get("model"),
                "next_bcv": first(bcv),
                "next_eur": first(eur),
                "next_parallel": first(parallel),
                "pipeline": quote.get("pipeline"),
                "quote_conversion_rate": quote.get("historical_conversion_rate"),
            },
            "restock_alerts": restock,
            "registry": registry_rows,
            "training_cutoff": D.cutoff_info(),
        }


class RetrainModelsView(_BaseForecastView):
    """POST /api/analytics/retrain — reentrena y reescribe el registro de modelos.

    Hace lo mismo que ``manage.py train_models`` pero desde la UI (botón "Reentrenar
    modelos" del panel predictivo): vuelve a entrenar las tres técnicas por objetivo,
    reescribe ``PredictionLog`` (marcando activa la técnica asignada) y **limpia la caché
    en memoria**, de modo que los siguientes pronósticos se sirvan con los modelos recién
    entrenados. El entrenamiento es de sub-segundo por modelo con estos datos, así que se
    ejecuta de forma síncrona. Gerente/Administrador (``IsManager``).

    Cuerpo opcional ``{"cutoff": "YYYY-MM-DD" | null}`` — **fecha de corte del
    entrenamiento**: los datos posteriores se excluyen de las series y el pronóstico
    arranca justo después del corte (útil cuando hay registros de prueba recientes que
    no deben contaminar los modelos). ``null`` quita el corte; **omitir la clave**
    conserva el corte ya configurado. Se guarda en la configuración del sistema, así que
    también aplica a los pronósticos que se sirven después.
    """

    _UNSET = object()

    def post(self, request):
        # Distingue "no mandaron corte" (conservar el vigente) de "mandaron null" (quitarlo).
        raw_cutoff = request.data.get("cutoff", self._UNSET)
        if raw_cutoff is not self._UNSET:
            try:
                cutoff = self._parse_cutoff(raw_cutoff)
            except ValueError:
                return Response(
                    {"detail": "Fecha de corte inválida; usa el formato YYYY-MM-DD."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            system_settings.set_training_cutoff_date(cutoff)
            registry.clear_cache()

        buf = io.StringIO()
        username = getattr(request.user, "username", "") or ""
        try:
            call_command("train_models", stdout=buf, stderr=buf, trigger="UI", triggered_by=username)
        except Exception as exc:  # pragma: no cover - depende del entorno ML
            logger.exception("Fallo al reentrenar los modelos")
            return Response(
                {"detail": f"No se pudieron reentrenar los modelos: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        active = list(PredictionLog.objects.filter(is_active=True).order_by("model_type"))
        trained_at = max((pl.trained_at for pl in active if pl.trained_at), default=None)
        total = PredictionLog.objects.count()
        cutoff_info = D.cutoff_info()
        cutoff_note = (
            f" con datos hasta {cutoff_info['effective']}" if cutoff_info["active"]
            else " con todo el historial"
        )
        audit.log(
            request=request,
            action=ActionChoices.MODELS_RETRAIN,
            description=(
                f"Reentrenó los modelos predictivos ({len(active)} modelos activos)"
                f"{cutoff_note}."
            ),
            metadata={
                "active_models": len(active),
                "total_rows": total,
                "training_cutoff": cutoff_info["effective"],
            },
        )
        return Response({
            "ok": True,
            "active_models": len(active),
            "total_rows": total,
            "trained_at": trained_at.isoformat() if trained_at else None,
            "training_cutoff": cutoff_info,
        })

    @staticmethod
    def _parse_cutoff(raw):
        """``None``/``""`` → sin corte; ``"YYYY-MM-DD"`` → ``date``. Lanza ``ValueError``."""
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        return date.fromisoformat(str(raw).strip())


class TrainingHistoryView(_BaseForecastView):
    """GET /api/analytics/training-history — historial de reentrenamientos y evolución de
    la **precisión** de cada modelo (R²/exactitud) a lo largo del tiempo.

    Cada vez que se reentrena (comando o botón del panel) se registra un ``TrainingRun`` con
    la instantánea de las métricas activas. Esta vista los devuelve en orden cronológico
    (``runs``) y también pivotados por tipo de modelo (``models``) para graficar una línea de
    evolución por modelo. Gerente/Administrador (``IsManager``)."""

    # Métrica de "precisión" por tipo de modelo y su etiqueta (el resto usa R²).
    _PRIMARY = {"QUOTE": ("accuracy", "Exactitud")}
    _ORDER = ["SALES", "PROFIT", "DEMAND", "PRICE", "RATE", "QUOTE", "INVENT", "BENCH"]

    def get(self, request):
        # Si aún no hay historial pero sí modelos entrenados, siembra un punto base para que
        # la gráfica no arranque vacía (la evolución empieza en el estado actual).
        registry.ensure_baseline_run()

        runs = list(TrainingRun.objects.order_by("trained_at"))
        if len(runs) > 100:  # cap defensivo: los 100 reentrenamientos más recientes
            runs = runs[-100:]

        runs_out = []
        pivot: dict[str, list] = {}
        display_map: dict[str, str] = {}

        for idx, run in enumerate(runs, start=1):
            models = run.models_metrics or []
            runs_out.append({
                "id": run.id,
                "index": idx,
                "trained_at": run.trained_at.isoformat() if run.trained_at else None,
                "trigger": run.trigger,
                "trigger_display": run.get_trigger_display(),
                "triggered_by": run.triggered_by,
                "models": models,
            })
            for m in models:
                mt = m.get("model_type")
                if not mt:
                    continue
                display_map[mt] = m.get("model_type_display") or mt
                metric_key = self._PRIMARY.get(mt, ("r2", "R²"))[0]
                pivot.setdefault(mt, []).append({
                    "run_id": run.id,
                    "index": idx,
                    "trained_at": run.trained_at.isoformat() if run.trained_at else None,
                    "value": m.get(metric_key),
                    "r2": m.get("r2"),
                    "rmse": m.get("rmse"),
                    "mae": m.get("mae"),
                    "accuracy": m.get("accuracy"),
                    "technique": m.get("technique"),
                })

        def sort_key(mt: str) -> int:
            return self._ORDER.index(mt) if mt in self._ORDER else len(self._ORDER)

        models_out = []
        for mt in sorted(pivot.keys(), key=sort_key):
            points = pivot[mt]
            # Solo modelos con una métrica de precisión real (excluye BENCH, que solo
            # registra la pendiente de la tendencia y no tiene R²/exactitud).
            if not any(p["value"] is not None for p in points):
                continue
            metric_key, metric_label = self._PRIMARY.get(mt, ("r2", "R²"))
            models_out.append({
                "model_type": mt,
                "display": display_map.get(mt, mt),
                "metric": metric_key,
                "metric_label": metric_label,
                "points": points,
            })

        return Response({"runs": runs_out, "models": models_out})


class ReportNarrativeView(APIView):
    """GET /api/analytics/report-narrative — narrativa del reporte ejecutivo redactada por LLM.

    Acepta la misma "máquina del tiempo" ``?from=&to=`` que el panel de Inicio. Recalcula
    el panel ejecutivo para ese rango (con el mismo gating de sensibilidad: ``IsViewer``
    para cargarlo, pero utilidad/margen/IVC/competencia solo si el solicitante pasa
    ``IsManager``) y, para gerencia, adjunta los titulares predictivos. Le pasa esos
    HECHOS al modelo, que redacta situación/puntos clave/riesgos/acciones/cierre.

    Degrada de forma segura: si el LLM no está configurado o falla, retorna
    ``{"available": False, ...}`` y el frontend cae a la síntesis determinista existente,
    de modo que el botón "Generar reporte" funciona igual sin clave de LLM.
    """

    permission_classes = [IsViewer]

    def get(self, request):
        default_start, default_end = stats.default_range(2)
        start = _date(request, "from", default_start)
        end = _date(request, "to", default_end)
        sensitive = IsManager().has_permission(request, self)
        dashboard = stats.executive_dashboard(start, end, sensitive=sensitive)
        # Las estimaciones (overview) son de gerencia; se cachean igual que en OverviewView.
        overview = registry.cached("overview", OverviewView._build) if sensitive else None
        audit.log(
            request=request,
            action=ActionChoices.REPORT_GENERATE,
            description=(
                f"Generó el reporte ejecutivo para el período {start.isoformat()} a "
                f"{end.isoformat()}."
            ),
            metadata={"from": start.isoformat(), "to": end.isoformat(), "sensitive": sensitive},
        )
        return Response(report_narrative.generate(dashboard, overview, sensitive=sensitive))

"""API REST del módulo predictivo (apps/analytics).

Vistas ``APIView`` (estilo dict, como las del scraper) que exponen cada pronóstico.
Todas requieren rol **Gerente o Administrador** (``IsManager``): los pronósticos son
herramientas de decisión estratégica "para el dueño".

El servicio entrena bajo demanda y cachea el resultado (``ml.registry.cached``),
invalidándolo cuando cambian los datos. Se puede sobreescribir el modelo por
``?model=linear|tree|xgboost`` para experimentar/comparar (la UI fija uno por gráfico).
"""

from __future__ import annotations

import logging
from datetime import date

from django.db.models import Count, Sum
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsManager, IsViewer
from apps.audit import services as audit
from apps.audit.models import ActionChoices
from apps.core.models import SERVICE_SKU_PREFIX, Product
from apps.sales.models import SaleItem

from . import benchmarking, report_narrative, stats
from .ml import forecasters as F
from .ml import registry
from .models import PredictionLog

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
        rate = rate if rate in ("bcv", "parallel") else "bcv"
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
        return Response(benchmarking.comparison(start, end))


class BenchmarkingForecastView(_BaseForecastView):
    """GET /api/analytics/benchmarking/forecast?from=&to=&horizon=&category= —
    pronóstico del precio de mercado vs. nuestros precios (entrena bajo demanda + cachea)."""

    def get(self, request):
        default_start, default_end = benchmarking.default_range()
        start = _date(request, "from", default_start)
        end = _date(request, "to", default_end)
        h, category = _horizon(request), (request.query_params.get("category") or None)
        key = f"benchmark_fc:{start.isoformat()}:{end.isoformat()}:{h}:{category}"
        return Response(registry.cached(key, lambda: F.competitor_forecast(start, end, h, category)))


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
                "next_parallel": first(parallel),
                "pipeline": quote.get("pipeline"),
                "quote_conversion_rate": quote.get("historical_conversion_rate"),
            },
            "restock_alerts": restock,
            "registry": registry_rows,
        }


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

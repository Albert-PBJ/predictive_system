"""API REST de estadísticas descriptivas (paneles de situación).

Complementa a las vistas predictivas (``views.py``): mientras aquéllas pronostican,
éstas resumen el estado **actual** del negocio para los gráficos del panel de Inicio
y del módulo "Estadísticas".

- ``/api/analytics/stats/dashboard`` es de **lectura para todo el personal**
  (``IsViewer``): alimenta el panel de Inicio que ve cualquier usuario autenticado y
  expone solo agregados operativos (clientes, ventas, ingresos, ubicación).
- El resto (clientes/productos/ventas/presupuestos en detalle, con utilidad, márgenes
  y rankings) son herramientas de gestión y van con ``IsManager``.

Las agregaciones viven en ``stats.py`` y son consultas directas (baratas), así que
no se cachean: el panel refleja siempre el dato más reciente.
"""

from __future__ import annotations

from datetime import date

from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsManager, IsViewer

from . import stats


class DashboardStatsView(APIView):
    """GET /api/analytics/stats/dashboard — panel de Inicio ejecutivo.

    Acepta ``?from=YYYY-MM-DD&to=YYYY-MM-DD`` (la "máquina del tiempo": todo el
    panel se recalcula para ese rango). Por defecto, los últimos 12 meses. Es
    ``IsViewer`` (lo carga cualquier usuario), pero la utilidad/margen/índice/
    competencia/modelos solo se incluyen para Gerente/Admin (``sensitive``).
    """

    permission_classes = [IsViewer]

    def get(self, request):
        default_start, default_end = stats.default_range()
        start = self._parse_date(request.query_params.get("from"), default_start)
        end = self._parse_date(request.query_params.get("to"), default_end)
        sensitive = IsManager().has_permission(request, self)
        return Response(stats.executive_dashboard(start, end, sensitive=sensitive))

    @staticmethod
    def _parse_date(value: str | None, fallback: date) -> date:
        try:
            return date.fromisoformat(value) if value else fallback
        except (ValueError, TypeError):
            return fallback


class _ManagerStatsView(APIView):
    permission_classes = [IsManager]


class CustomerStatsView(_ManagerStatsView):
    def get(self, request):
        return Response(stats.customers())


class ProductStatsView(_ManagerStatsView):
    def get(self, request):
        return Response(stats.products())


class SalesStatsView(_ManagerStatsView):
    def get(self, request):
        return Response(stats.sales())


class QuoteStatsView(_ManagerStatsView):
    def get(self, request):
        return Response(stats.quotes())

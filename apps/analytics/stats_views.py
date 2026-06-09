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

from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsManager, IsViewer

from . import stats


class DashboardStatsView(APIView):
    """GET /api/analytics/stats/dashboard — resumen para el panel de Inicio."""

    permission_classes = [IsViewer]

    def get(self, request):
        return Response(stats.dashboard())


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

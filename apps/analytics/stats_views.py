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

from apps.accounts.models import Role
from apps.accounts.permissions import IsManager, IsViewer

from . import stats


def _parse_date(value: str | None, fallback: date) -> date:
    """Parsea ``YYYY-MM-DD`` del query string; usa ``fallback`` si falta o es inválido."""
    try:
        return date.fromisoformat(value) if value else fallback
    except (ValueError, TypeError):
        return fallback


def _range_params(request, *, default_months: int = 2) -> tuple[date, date]:
    """Rango [from, to] del query string ("máquina del tiempo").

    Sin ``?from=&to=`` cae al rango por defecto de ``default_months`` meses: el panel de
    Inicio usa 2 (para comparar contra la ventana previa); los paneles de estadísticas, 1.
    """
    default_start, default_end = stats.default_range(default_months)
    start = _parse_date(request.query_params.get("from"), default_start)
    end = _parse_date(request.query_params.get("to"), default_end)
    return start, end


class DashboardStatsView(APIView):
    """GET /api/analytics/stats/dashboard — panel de Inicio ejecutivo.

    Acepta ``?from=YYYY-MM-DD&to=YYYY-MM-DD`` (la "máquina del tiempo": todo el
    panel se recalcula para ese rango). Por defecto, los últimos 2 meses. Es
    ``IsViewer`` (lo carga cualquier usuario), pero la utilidad/margen/índice/
    competencia/modelos solo se incluyen para Gerente/Admin (``sensitive``).

    **Personalizado por rol:** un VENDEDOR ve solo SUS números (no los de la empresa).
    El panel se acota a su ficha de vendedor (``seller_profile``); Gerente/Admin y el
    resto del personal operativo siguen viendo la empresa.
    """

    permission_classes = [IsViewer]

    def get(self, request):
        start, end = _range_params(request)
        sensitive = IsManager().has_permission(request, self)
        role = getattr(getattr(request.user, "profile", None), "role", None)
        # Solo el rol Vendedor recibe la vista personal (Gerente/Admin ven la empresa
        # aunque tengan ficha de vendedor; inventario/consulta ven la empresa operativa).
        personal = role == Role.SELLER and not sensitive
        seller = getattr(request.user, "seller_profile", None) if personal else None
        return Response(
            stats.executive_dashboard(start, end, sensitive=sensitive, personal=personal, seller=seller)
        )


class _ManagerStatsView(APIView):
    """Vistas de detalle (Gerente/Admin). Todas aceptan la misma ``?from=&to=``.

    Las funciones de ``stats`` deciden qué se recalcula por rango (agregados de
    venta/presupuesto) y qué es instantánea actual (composición de cartera/catálogo,
    inventario) — ver sus docstrings.
    """

    permission_classes = [IsManager]


class CustomerStatsView(_ManagerStatsView):
    def get(self, request):
        start, end = _range_params(request, default_months=1)
        return Response(stats.customers(start, end))


class ProductStatsView(_ManagerStatsView):
    def get(self, request):
        start, end = _range_params(request, default_months=1)
        return Response(stats.products(start, end))


class SalesStatsView(_ManagerStatsView):
    def get(self, request):
        start, end = _range_params(request, default_months=1)
        return Response(stats.sales(start, end))


class QuoteStatsView(_ManagerStatsView):
    def get(self, request):
        start, end = _range_params(request, default_months=1)
        return Response(stats.quotes(start, end))

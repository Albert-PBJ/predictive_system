"""Endpoints de importación/exportación Excel (continuidad operativa).

Un juego de rutas por operación (``<entity>`` ∈ sales|inventory|customers|quotes):
plantilla en blanco, export de datos, previsualización de un archivo (sin escribir) e
importación (escribe). Los permisos se toman del handler de la operación: exportar/plantilla
es operativo; importar exige el permiso de escritura de esa operación (vendedor para ventas/
clientes/presupuestos, inventario para movimientos).
"""

from __future__ import annotations

from datetime import date

from django.http import Http404, HttpResponse
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsOperational

from .excel_io import SheetError
from .handlers import get_handler
from .services import export_workbook, run_import, template_workbook

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx_response(bio, filename) -> HttpResponse:
    resp = HttpResponse(bio.getvalue(), content_type=_XLSX)
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp["Access-Control-Expose-Headers"] = "Content-Disposition"
    return resp


class _EntityView(APIView):
    """Base: resuelve el handler de ``entity`` y sus permisos."""

    # Qué permiso del handler usar ("import" o "export").
    perm_kind = "export"

    def get_permissions(self):
        handler = get_handler(self.kwargs.get("entity"))
        if handler is None:
            return [IsOperational()]
        perm = handler.import_permission if self.perm_kind == "import" else handler.export_permission
        return [perm()]

    def _handler(self):
        handler = get_handler(self.kwargs.get("entity"))
        if handler is None:
            raise Http404("Operación no reconocida.")
        return handler


class TemplateView(_EntityView):
    perm_kind = "export"

    def get(self, request, entity):
        self._handler()
        bio = template_workbook(entity)
        return _xlsx_response(bio, f"plantilla_{entity}.xlsx")


class ExportView(_EntityView):
    perm_kind = "export"

    def get(self, request, entity):
        self._handler()
        bio, _count = export_workbook(entity, request.query_params, request.user, request=request)
        return _xlsx_response(bio, f"export_{entity}_{date.today().isoformat()}.xlsx")


class _ImportBase(_EntityView):
    perm_kind = "import"
    parser_classes = [MultiPartParser, FormParser]
    commit = False

    def post(self, request, entity):
        self._handler()
        file = request.FILES.get("file") or request.FILES.get("archivo")
        if file is None:
            return Response({"error": "Adjunta un archivo Excel (.xlsx) en el campo «file»."}, status=400)
        try:
            result = run_import(entity, file, request.user, commit=self.commit, request=request)
        except SheetError as exc:
            return Response({"error": str(exc)}, status=400)
        return Response(result)


class ImportPreviewView(_ImportBase):
    commit = False


class ImportCommitView(_ImportBase):
    commit = True

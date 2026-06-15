"""API REST de la bitácora de auditoría (solo ADMIN).

* ``GET  /api/audit/logs``        → listado paginado y filtrable.
* ``GET  /api/audit/meta``        → opciones para los filtros (categorías, acciones, usuarios).
* ``GET  /api/audit/logs/export`` → exporta a CSV el conjunto filtrado (sin paginar).
* ``POST /api/audit/logs/purge``  → elimina registros anteriores a una fecha (la purga se audita).

El registro es de **solo lectura**: no hay edición ni borrado individual desde la API
(es un rastro de auditoría). La única escritura es la purga por antigüedad.
"""

import csv
from datetime import datetime

from django.db.models import Q
from django.http import HttpResponse
from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Role
from apps.accounts.permissions import IsAdmin
from apps.core.pagination import StandardResultsSetPagination

from . import services
from .models import ActionChoices, AuditLog, CategoryChoices
from .serializers import AuditLogSerializer


def _parse_date(value):
    """Convierte 'YYYY-MM-DD' a date; None si está vacío o es inválido."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def _role_label(code: str) -> str:
    """Etiqueta legible de un código de rol; el propio código si no se reconoce."""
    if not code:
        return ""
    try:
        return Role(code).label
    except ValueError:
        return code


def _filtered_queryset(request):
    """Aplica los filtros comunes (listado y exportación comparten esta lógica)."""
    qs = AuditLog.objects.all().order_by("-created_at")
    params = request.query_params

    category = (params.get("category") or "").strip()
    if category:
        qs = qs.filter(category=category)

    action = (params.get("action") or "").strip()
    if action:
        qs = qs.filter(action=action)

    actor = (params.get("actor") or "").strip()
    if actor:
        qs = qs.filter(actor_username=actor)

    date_from = _parse_date(params.get("date_from"))
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    date_to = _parse_date(params.get("date_to"))
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)

    search = (params.get("search") or "").strip()
    if search:
        qs = qs.filter(
            Q(description__icontains=search) | Q(actor_username__icontains=search)
        )

    return qs


class AuditLogListView(generics.ListAPIView):
    """GET /api/audit/logs — listado paginado y filtrable de la bitácora."""

    permission_classes = [IsAdmin]
    serializer_class = AuditLogSerializer
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        return _filtered_queryset(self.request)


class AuditMetaView(APIView):
    """GET /api/audit/meta — opciones de filtro para los desplegables de la UI."""

    permission_classes = [IsAdmin]

    def get(self, request):
        # `.order_by()` quita el ordenamiento por defecto del modelo (-created_at):
        # de lo contrario se cuela en el DISTINCT y deja nombres repetidos.
        actors = sorted(
            v
            for v in AuditLog.objects.exclude(actor_username="")
            .order_by()
            .values_list("actor_username", flat=True)
            .distinct()
            if v
        )
        return Response(
            {
                "categories": [{"value": c.value, "label": c.label} for c in CategoryChoices],
                "actions": [{"value": a.value, "label": a.label} for a in ActionChoices],
                "actors": actors,
            },
            status=status.HTTP_200_OK,
        )


class AuditLogExportView(APIView):
    """GET /api/audit/logs/export — exporta a CSV el conjunto filtrado (sin paginar)."""

    permission_classes = [IsAdmin]

    def get(self, request):
        qs = _filtered_queryset(request)

        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="auditoria.csv"'
        # BOM para que Excel reconozca el UTF-8 (acentos en español).
        response.write("﻿")

        writer = csv.writer(response)
        writer.writerow(
            ["Fecha y hora", "Usuario", "Rol", "Categoría", "Acción", "Descripción", "Objeto", "IP"]
        )
        for log in qs.iterator():
            target = f"{log.target_model} #{log.target_id}" if log.target_model else ""
            writer.writerow(
                [
                    log.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    log.actor_username or "sistema",
                    _role_label(log.actor_role),
                    log.get_category_display(),
                    log.get_action_display(),
                    log.description,
                    target,
                    log.ip_address or "",
                ]
            )
        return response


class AuditLogPurgeView(APIView):
    """POST /api/audit/logs/purge — elimina registros anteriores a una fecha.

    Cuerpo: ``{"before": "YYYY-MM-DD"}``. Borra los registros con ``created_at`` en una
    fecha estrictamente anterior a la indicada. La purga se audita ella misma (queda un
    registro ``LOG_PURGE`` con el conteo eliminado y la fecha de corte).
    """

    permission_classes = [IsAdmin]

    def post(self, request):
        before = _parse_date(request.data.get("before"))
        if before is None:
            return Response(
                {"error": "Indica una fecha de corte válida (YYYY-MM-DD)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        deleted, _ = AuditLog.objects.filter(created_at__date__lt=before).delete()

        services.log(
            request=request,
            action=ActionChoices.LOG_PURGE,
            description=(
                f"Se purgaron {deleted} registro(s) de auditoría anteriores a "
                f"{before.isoformat()}."
            ),
            metadata={"before": before.isoformat(), "deleted": deleted},
        )

        return Response({"deleted": deleted}, status=status.HTTP_200_OK)

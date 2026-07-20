"""API de **notificaciones** (bandeja del usuario) sobre el modelo `Alert`.

Las alertas son hechos de empresa con una *audiencia* por rol; cada usuario ve las
que le corresponden y lleva su **propio** estado de leído (`AlertRead`). Expone:

- ``GET  /api/analytics/notifications``       → feed del usuario + no leídas.
- ``POST /api/analytics/notifications/read``  → marcar leídas (todas o unas ids).
- ``POST /api/analytics/notifications/scan``  → dispara el barrido predictivo (throttled).

El feed lo consume la campana del encabezado y la página "Ver todas".
"""

from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import alerts as alerts_mod
from .models import Alert, AlertRead

# Tope de filas recientes que consideramos para la bandeja (las alertas están
# deduplicadas, así que el volumen es bajo; el tope solo acota el histórico).
_FEED_CAP = 200


def _user_role(user) -> str | None:
    profile = getattr(user, "profile", None)
    role = profile.role if profile else None
    if role is None and getattr(user, "is_superuser", False):
        return "ADMIN"
    return role


def _visible_to(alert: Alert, role: str | None, is_superuser: bool) -> bool:
    """Una alerta es visible si su audiencia (o la de su tipo) incluye el rol.

    Si la alerta no tiene audiencia guardada (filas antiguas creadas antes de este
    sistema), se deriva del **tipo** —no se muestra a todos— para que el enrutamiento
    por rol sea correcto incluso sin re-guardar esas filas.
    """
    if is_superuser:
        return True
    audience = alert.audience or alerts_mod.audience_for(alert.alert_type)
    return role in audience


def _serialize(alert: Alert, read: bool) -> dict:
    return {
        "id": alert.id,
        "type": alert.alert_type,
        "type_label": alert.get_alert_type_display(),
        "severity": alert.severity,
        "severity_label": alert.get_severity_display(),
        "title": alert.title,
        "message": alert.message,
        "is_resolved": alert.is_resolved,
        "read": read,
        "created_at": alert.created_at.isoformat(),
        "updated_at": alert.updated_at.isoformat(),
    }


def _visible_alerts(user):
    """Alertas visibles para `user`, más recientes primero (acotadas a `_FEED_CAP`)."""
    role = _user_role(user)
    is_su = bool(getattr(user, "is_superuser", False))
    recent = list(Alert.objects.order_by("-created_at")[: _FEED_CAP * 2])
    visible = [a for a in recent if _visible_to(a, role, is_su)][:_FEED_CAP]
    return visible


class NotificationListView(APIView):
    """Feed de notificaciones del usuario autenticado + contador de no leídas."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        visible = _visible_alerts(request.user)
        read_ids = set(
            AlertRead.objects.filter(
                user=request.user, alert_id__in=[a.id for a in visible]
            ).values_list("alert_id", flat=True)
        )
        items = [_serialize(a, a.id in read_ids) for a in visible]
        unread = sum(1 for a in visible if a.id not in read_ids)
        return Response({"results": items, "unread_count": unread})


class NotificationReadView(APIView):
    """Marca notificaciones como leídas para el usuario (todas las visibles, o `ids`)."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        ids = request.data.get("ids")
        visible = {a.id for a in _visible_alerts(request.user)}
        target = visible if not ids else (visible & set(ids))
        already = set(
            AlertRead.objects.filter(
                user=request.user, alert_id__in=target
            ).values_list("alert_id", flat=True)
        )
        to_create = [
            AlertRead(alert_id=aid, user=request.user) for aid in target if aid not in already
        ]
        if to_create:
            AlertRead.objects.bulk_create(to_create, ignore_conflicts=True)
        return Response({"marked": len(to_create)})


class NotificationScanView(APIView):
    """Dispara el barrido predictivo de alertas (throttled) y devuelve el conteo.

    Lo llama el runner del frontend al iniciar sesión un rol relevante. Es
    idempotente y best-effort: si se corrió hace poco, no repite el trabajo.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        summary = alerts_mod.scan_and_generate_alerts()
        visible = _visible_alerts(request.user)
        read_ids = set(
            AlertRead.objects.filter(
                user=request.user, alert_id__in=[a.id for a in visible]
            ).values_list("alert_id", flat=True)
        )
        unread = sum(1 for a in visible if a.id not in read_ids)
        return Response({"scan": summary, "unread_count": unread})

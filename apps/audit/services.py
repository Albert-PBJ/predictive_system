"""Punto único para escribir en la bitácora de auditoría.

Todos los módulos llaman a ``log(...)`` para dejar constancia de una acción. La
función es **defensiva por diseño**: cualquier error al auditar se registra como
WARNING pero NUNCA se propaga, de modo que una falla del log no puede romper la
operación de negocio (registrar una venta, crear un presupuesto, etc.).
"""

import logging

from .models import ACTION_CATEGORY, AuditLog

logger = logging.getLogger(__name__)


def _client_ip(request):
    """Mejor estimación de la IP del cliente (respeta X-Forwarded-For si hay proxy)."""
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _role_of(user):
    """Rol de negocio del usuario (código), tomado de su perfil. '' si no tiene."""
    profile = getattr(user, "profile", None)
    return profile.role if profile else ""


def log(
    *,
    request=None,
    actor=None,
    action,
    description,
    target=None,
    target_model="",
    target_id="",
    metadata=None,
    category=None,
):
    """Crea un registro de auditoría. Devuelve el ``AuditLog`` o ``None`` ante un error.

    - ``request``: si se pasa, de ahí se resuelven el actor (``request.user``) y la IP.
    - ``actor``: usuario explícito (tiene prioridad sobre ``request.user``); puede ser
      ``None`` para acciones del sistema.
    - ``action``: un valor de ``ActionChoices``. ``category`` se deriva de él si no se da.
    - ``target``: instancia del objeto afectado (llena ``target_model``/``target_id``);
      alternativamente se pueden pasar ``target_model``/``target_id`` a mano.
    - ``description``: texto en español, legible para el administrador.
    - ``metadata``: dict con detalles estructurados extra.
    """
    try:
        if actor is None and request is not None:
            user = getattr(request, "user", None)
            if user is not None and getattr(user, "is_authenticated", False):
                actor = user

        actor_username = ""
        actor_role = ""
        if actor is not None:
            actor_username = getattr(actor, "username", "") or ""
            actor_role = _role_of(actor)

        if target is not None:
            target_model = target_model or type(target).__name__
            pk = getattr(target, "pk", None)
            target_id = target_id or (str(pk) if pk is not None else "")

        if category is None:
            category = ACTION_CATEGORY.get(action)
            category = category.value if hasattr(category, "value") else (category or "")

        action_value = action.value if hasattr(action, "value") else action

        return AuditLog.objects.create(
            actor=actor,
            actor_username=actor_username,
            actor_role=actor_role,
            action=action_value,
            category=category or "",
            description=description[:500],
            target_model=target_model,
            target_id=target_id,
            metadata=metadata or {},
            ip_address=_client_ip(request),
        )
    except Exception:  # nunca debe tumbar la operación de negocio que se auditaba
        logger.warning("No se pudo registrar la auditoría (%s)", action, exc_info=True)
        return None

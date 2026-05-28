from rest_framework.permissions import BasePermission

from .models import Role


def _role(user):
    profile = getattr(user, "profile", None)
    return profile.role if profile else None


class HasRole(BasePermission):
    """Permiso base parametrizable por rol.

    Uso: declarar una subclase con `allowed_roles`, o usar las clases concretas
    de abajo. Los superusuarios siempre pasan.
    """

    allowed_roles: tuple = ()

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True
        return _role(user) in self.allowed_roles


class IsAdmin(HasRole):
    allowed_roles = (Role.ADMIN,)


class IsManager(HasRole):
    allowed_roles = (Role.ADMIN, Role.MANAGER)


class IsSeller(HasRole):
    """Vendedores y superiores (admin, gerente, vendedor)."""

    allowed_roles = (Role.ADMIN, Role.MANAGER, Role.SELLER)


class IsViewer(HasRole):
    """Cualquier rol válido con acceso de lectura."""

    allowed_roles = (Role.ADMIN, Role.MANAGER, Role.SELLER, Role.VIEWER)

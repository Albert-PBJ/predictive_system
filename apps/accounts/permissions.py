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
    """Puede registrar ventas: vendedor, gerente o admin.

    Es una capacidad de *negocio* (vender), no un escalón jerárquico: el encargado
    de inventario queda **fuera** a propósito (ve las ventas, pero no las hace).
    """

    allowed_roles = (Role.ADMIN, Role.MANAGER, Role.SELLER)


class IsWarehouse(HasRole):
    """Puede modificar el stock directamente (movimientos manuales de inventario):
    encargado de inventario, gerente o admin. Los vendedores quedan fuera: solo
    consultan el stock; las ventas lo descuentan de forma indirecta."""

    allowed_roles = (Role.ADMIN, Role.MANAGER, Role.WAREHOUSE)


class IsOperational(HasRole):
    """Personal operativo: vendedores y encargados de inventario (más gerente/admin).

    Para datos compartidos de solo lectura entre ambas áreas —consultar ventas,
    existencias y catálogo de productos— sin habilitar la escritura de ninguna."""

    allowed_roles = (Role.ADMIN, Role.MANAGER, Role.SELLER, Role.WAREHOUSE)


class IsViewer(HasRole):
    """Cualquier rol válido con acceso de lectura."""

    allowed_roles = (Role.ADMIN, Role.MANAGER, Role.SELLER, Role.WAREHOUSE, Role.VIEWER)

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Role(models.TextChoices):
    """Roles de negocio del sistema. Determinan los permisos de cada usuario."""

    ADMIN = "ADMIN", _("Administrador")
    MANAGER = "MANAGER", _("Gerente")
    SELLER = "SELLER", _("Vendedor")
    WAREHOUSE = "WAREHOUSE", _("Encargado de Inventario")
    VIEWER = "VIEWER", _("Consulta")


class UserProfile(models.Model):
    """Perfil que extiende al usuario de Django con su rol de negocio.

    Se crea automáticamente vía signal cuando se crea un User (ver signals.py).
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
        help_text=_("Usuario de Django asociado al perfil (solo para autenticación)"),
    )
    role = models.CharField(
        max_length=10,
        choices=Role.choices,
        default=Role.VIEWER,
        help_text=_("Rol de negocio que define los permisos del usuario"),
    )

    # Datos personales del usuario. El perfil es la fuente de verdad: las
    # columnas equivalentes en auth_user quedan sin usar (Django no permite
    # eliminarlas sin un modelo de usuario personalizado).
    first_name = models.CharField(max_length=150, blank=True, help_text=_("Nombre"))
    last_name = models.CharField(max_length=150, blank=True, help_text=_("Apellido"))
    email = models.EmailField(blank=True, help_text=_("Correo de contacto"))
    phone = models.CharField(max_length=20, blank=True, help_text=_("Teléfono de contacto"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "user_profiles"
        verbose_name = "Perfil de Usuario"
        verbose_name_plural = "Perfiles de Usuario"

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"

    @property
    def full_name(self):
        name = f"{self.first_name} {self.last_name}".strip()
        return name or self.user.username

    @property
    def is_admin(self):
        return self.role == Role.ADMIN

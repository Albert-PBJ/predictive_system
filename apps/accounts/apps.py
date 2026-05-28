from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"
    verbose_name = "Cuentas y Roles"

    def ready(self):
        # Registra los signals que crean el perfil de usuario automáticamente
        from . import signals  # noqa: F401

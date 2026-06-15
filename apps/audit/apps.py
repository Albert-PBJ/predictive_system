from django.apps import AppConfig


class AuditConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.audit"
    verbose_name = "Auditoría del Sistema"

    def ready(self):
        # Registra el signal que audita la creación de usuarios (desde el admin de
        # Django o `createsuperuser`, donde no hay un request del cual tomar al actor).
        from . import signals  # noqa: F401

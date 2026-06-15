from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Bitácora de solo lectura en el admin.

    No se pueden crear ni editar registros (es un rastro de auditoría); se conserva la
    acción de borrado solo para limpieza/purga manual del administrador.
    """

    list_display = ("created_at", "actor_username", "actor_role", "category", "action", "description")
    list_filter = ("category", "action", "created_at")
    search_fields = ("actor_username", "description", "target_model", "target_id")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # Permite abrir el detalle (solo lectura) pero no guardar cambios.
        return False

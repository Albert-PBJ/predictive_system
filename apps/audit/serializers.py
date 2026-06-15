from rest_framework import serializers

from apps.accounts.models import Role

from .models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    """Serializa un registro de auditoría con sus etiquetas legibles.

    Expone los códigos crudos (``action``/``category``/``actor_role``) más sus textos
    en español (``*_label``), para que el frontend muestre directamente la etiqueta sin
    duplicar los mapas de opciones.
    """

    action_label = serializers.CharField(source="get_action_display", read_only=True)
    category_label = serializers.CharField(source="get_category_display", read_only=True)
    actor_role_label = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = (
            "id",
            "actor_username",
            "actor_role",
            "actor_role_label",
            "action",
            "action_label",
            "category",
            "category_label",
            "description",
            "target_model",
            "target_id",
            "metadata",
            "ip_address",
            "created_at",
        )

    def get_actor_role_label(self, obj) -> str:
        if not obj.actor_role:
            return ""
        try:
            return Role(obj.actor_role).label
        except ValueError:
            return obj.actor_role

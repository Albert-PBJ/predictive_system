from rest_framework import serializers

from apps.core.models import Customer


class CustomerSerializer(serializers.ModelSerializer):
    """API CRUD sobre `core.Customer` (capa fina, igual que `apps/products`)."""

    customer_type_display = serializers.CharField(
        source="get_customer_type_display", read_only=True
    )
    contact_full_name = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = (
            "id",
            "rif",
            "company_name",
            "customer_type",
            "customer_type_display",
            "sector",
            "contact_first_name",
            "contact_last_name",
            "contact_full_name",
            "contact_ci",
            "phone",
            "mobile",
            "email",
            "state",
            "municipality",
            "parish",
            "fiscal_address",
            "total_employees",
            "is_active_customer",
            "created_at",
        )
        read_only_fields = ("id", "created_at")

    def get_contact_full_name(self, obj):
        return f"{obj.contact_first_name} {obj.contact_last_name}".strip()

from rest_framework import viewsets

from apps.accounts.permissions import IsManager, IsSeller
from apps.core.models import Customer

from .serializers import CustomerSerializer


class CustomerViewSet(viewsets.ModelViewSet):
    """CRUD de clientes.

    Pensado para el formulario de ventas: los vendedores pueden listar/buscar y
    crear clientes (alta rápida al registrar una venta). Eliminar queda reservado
    a gerente/administrador, ya que los clientes están referenciados por ventas y
    presupuestos.

    Filtros de query: `search` (razón social, RIF o contacto), `customer_type`,
    `state`, `is_active_customer`.
    """

    serializer_class = CustomerSerializer

    def get_permissions(self):
        if self.action == "destroy":
            return [IsManager()]
        return [IsSeller()]

    def get_queryset(self):
        qs = Customer.objects.all().order_by("company_name")
        params = self.request.query_params

        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(company_name__icontains=search) | qs.filter(
                rif__icontains=search
            ) | qs.filter(contact_last_name__icontains=search)

        customer_type = params.get("customer_type")
        if customer_type:
            qs = qs.filter(customer_type=customer_type)

        state = (params.get("state") or "").strip()
        if state:
            qs = qs.filter(state__iexact=state)

        is_active = params.get("is_active_customer")
        if is_active is not None and is_active != "":
            qs = qs.filter(is_active_customer=str(is_active).lower() in ("1", "true", "yes"))

        return qs.distinct()

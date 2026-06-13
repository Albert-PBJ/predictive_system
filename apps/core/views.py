from rest_framework import generics, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsOperational, IsSeller

from .models import Category, ExchangeRate


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ("id", "name", "slug")


class CategoryListView(generics.ListAPIView):
    """
    GET /api/categories

    Lista (sin paginar) las categorías de producto, para poblar el desplegable del
    formulario de productos. Solo lectura.

    Acceso: personal operativo (cualquier rol que pueda ver el catálogo).
    """

    queryset = Category.objects.all().order_by("name")
    serializer_class = CategorySerializer
    permission_classes = [IsOperational]
    pagination_class = None


class LatestExchangeRateView(APIView):
    """
    GET /api/exchange-rate/latest

    Devuelve la tasa de cambio más reciente cargada (BCV y paralela), para que el
    frontend pueda previsualizar montos en Bolívares al registrar una venta. La
    venta fija su propia tasa en el servidor al guardarse; este endpoint es solo
    informativo.

    Acceso: Vendedor o superior.
    """

    permission_classes = [IsSeller]

    def get(self, request):
        rate = ExchangeRate.objects.order_by("-date").first()
        if rate is None:
            return Response(
                {"detail": "No hay tasas de cambio cargadas."},
                status=status.HTTP_404_NOT_FOUND,
            )
        from apps.core import system_settings

        eff = system_settings.effective_rate(rate)
        return Response(
            {
                "date": rate.date.isoformat(),
                "bcv_rate": str(rate.bcv_rate),
                "parallel_rate": str(rate.parallel_rate) if rate.parallel_rate is not None else None,
                # Tasa efectiva usada para convertir USD→VES, según la base configurada
                # (paralela por defecto; también BCV o promedio — ver SystemSettings).
                "effective_rate": str(eff) if eff is not None else None,
                "rate_basis": system_settings.rate_basis(),
                "source": rate.source,
            },
            status=status.HTTP_200_OK,
        )

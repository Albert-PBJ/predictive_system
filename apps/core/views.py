from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsSeller

from .models import ExchangeRate


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
        return Response(
            {
                "date": rate.date.isoformat(),
                "bcv_rate": str(rate.bcv_rate),
                "parallel_rate": str(rate.parallel_rate) if rate.parallel_rate is not None else None,
                # Tasa efectiva usada para convertir USD→VES (paralela si existe).
                "effective_rate": str(rate.parallel_rate or rate.bcv_rate),
                "source": rate.source,
            },
            status=status.HTTP_200_OK,
        )

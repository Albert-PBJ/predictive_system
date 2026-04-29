from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.competitor_market_data.scrapers.instagram_scraper import scrape_instagram_profiles


class InstagramScraperStartView(APIView):
    """
    POST /scrapers/instagram/start

    Inicia el scraper de Instagram vía Apify y almacena los resultados
    en CompetitorMarketData.

    Cuerpo esperado:
    {
        "urls": ["https://www.instagram.com/competidor1/", ...],
        "limit": 50   (opcional, default 50)
    }
    """

    def post(self, request: Request) -> Response:
        urls = request.data.get("urls")
        if not urls or not isinstance(urls, list):
            return Response(
                {"error": "El campo 'urls' es requerido y debe ser una lista."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        limit = request.data.get("limit", 50)
        if not isinstance(limit, int) or limit < 1:
            return Response(
                {"error": "El campo 'limit' debe ser un entero positivo."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            records = scrape_instagram_profiles(urls=urls, results_limit=limit)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response(
            {"saved": len(records)},
            status=status.HTTP_201_CREATED,
        )

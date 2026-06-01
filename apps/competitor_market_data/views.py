import os

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsAdmin
from apps.competitor_market_data.enrichment import deepseek
from apps.competitor_market_data.scrapers import get_run_progress

# Interruptor para abrir el endpoint de prueba del LLM (/scrapers/llm/test) sin
# autenticación, útil para probarlo desde Postman en desarrollo. Por defecto está
# CERRADO (solo ADMIN), porque la prueba consume crédito de la API. Se lee del
# .env al iniciar, así que cambiarlo requiere reiniciar el servidor.
LLM_TEST_PUBLIC = os.environ.get("LLM_TEST_PUBLIC", "False").lower() in ("1", "true", "yes")
from apps.competitor_market_data.scrapers.facebook_marketplace_scraper import (
    finalize_facebook,
    start_facebook_run,
)
from apps.competitor_market_data.scrapers.instagram_scraper import (
    finalize_instagram,
    start_instagram_run,
)
from apps.competitor_market_data.scrapers.website_scraper import (
    finalize_website,
    start_website_run,
)

# Registro de scrapers disponibles, indexado por el segmento de URL `source`.
# `needs_competitor` indica que el scraper usa `competitor_name`/`urls` al finalizar.
SCRAPERS = {
    "instagram": {
        "start": start_instagram_run,
        "finalize": finalize_instagram,
        "needs_competitor": False,
    },
    "facebook": {
        "start": start_facebook_run,
        "finalize": finalize_facebook,
        "needs_competitor": False,
    },
    "website": {
        "start": start_website_run,
        "finalize": finalize_website,
        "needs_competitor": True,
    },
}


def _serialize_records(records) -> list[dict]:
    """Serializa los registros recién creados para mostrarlos en el frontend."""
    return [
        {
            "id": r.id,
            "competitor_name": r.competitor_name,
            "product_name": r.product_name,
            "price": str(r.price) if r.price is not None else None,
            "currency": r.currency,
            "promotions": r.promotions,
            "is_in_stock": r.is_in_stock,
            "lead_time_days": r.lead_time_days,
            "url": r.url,
            "source": r.source,
        }
        for r in records
    ]


def _validate_source(source: str):
    """Retorna la config del scraper o None si la fuente no existe."""
    return SCRAPERS.get(source)


class ScraperStartView(APIView):
    """
    POST /scrapers/<source>/start

    Inicia (sin bloquear) el run de Apify para la fuente indicada
    (`instagram`, `facebook` o `website`) y retorna el identificador del run
    y su dataset para hacer seguimiento del progreso.

    Cuerpo esperado:
    {
        "urls": ["https://…", ...],
        "limit": 50,               (opcional, default 50)
        "competitor_name": "..."   (opcional, solo aplica a `website`)
    }
    """

    permission_classes = [IsAdmin]

    def post(self, request: Request, source: str) -> Response:
        config = _validate_source(source)
        if config is None:
            return Response(
                {"error": f"Fuente de datos desconocida: '{source}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

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
            run = config["start"](urls=urls, results_limit=limit)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as exc:
            return Response(
                {"error": f"Error inesperado al iniciar el scraper: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {
                "run_id": run.get("id"),
                "dataset_id": run.get("defaultDatasetId"),
                "status": run.get("status"),
            },
            status=status.HTTP_202_ACCEPTED,
        )


class ScraperStatusView(APIView):
    """
    GET /scrapers/<source>/status?run_id=...&dataset_id=...

    Consulta (solo lectura) el estado del run de Apify y cuántos items lleva
    recolectados. El frontend hace polling sobre este endpoint.
    """

    permission_classes = [IsAdmin]

    def get(self, request: Request, source: str) -> Response:
        if _validate_source(source) is None:
            return Response(
                {"error": f"Fuente de datos desconocida: '{source}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        run_id = request.query_params.get("run_id")
        if not run_id:
            return Response(
                {"error": "El parámetro 'run_id' es requerido."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        dataset_id = request.query_params.get("dataset_id") or None

        try:
            progress = get_run_progress(run_id, dataset_id)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as exc:
            return Response(
                {"error": f"No se pudo consultar el estado del run: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(progress, status=status.HTTP_200_OK)


class ScraperFinalizeView(APIView):
    """
    POST /scrapers/<source>/finalize

    Lee el dataset de un run ya finalizado, mapea y guarda los registros en
    CompetitorMarketData, y devuelve los datos recolectados para mostrarlos.

    Cuerpo esperado:
    {
        "dataset_id": "...",
        "urls": [...],             (requerido solo para `website`)
        "competitor_name": "..."   (opcional, solo aplica a `website`)
    }
    """

    permission_classes = [IsAdmin]

    def post(self, request: Request, source: str) -> Response:
        config = _validate_source(source)
        if config is None:
            return Response(
                {"error": f"Fuente de datos desconocida: '{source}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        dataset_id = request.data.get("dataset_id")
        if not dataset_id:
            return Response(
                {"error": "El campo 'dataset_id' es requerido."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        kwargs = {"dataset_id": dataset_id}
        if config["needs_competitor"]:
            kwargs["urls"] = request.data.get("urls") or []
            kwargs["competitor_name"] = request.data.get("competitor_name") or None

        try:
            records = config["finalize"](**kwargs)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as exc:
            return Response(
                {"error": f"Error inesperado al procesar los resultados: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {"saved": len(records), "results": _serialize_records(records)},
            status=status.HTTP_201_CREATED,
        )


class LLMConnectionTestView(APIView):
    """
    GET  /scrapers/llm/test
    POST /scrapers/llm/test   {"title": "...", "description": "...", "location": "..."}

    Endpoint de DIAGNÓSTICO para verificar la conexión con DeepSeek (LLM) sin
    ejecutar el scraper real. Hace UNA llamada con datos de ejemplo estáticos (o
    con el texto enviado en el cuerpo del POST) y devuelve el estado de
    configuración junto con el resultado del modelo o el detalle del error
    (tipo, mensaje y código HTTP). Pensado para probar la integración desde
    Postman —y ver el error esperado de saldo/clave antes de pagar la API—.

    Acceso: por defecto solo ADMIN (la llamada consume crédito de la API). Si
    `LLM_TEST_PUBLIC` está activo en el .env, se abre como AllowAny para poder
    probarlo desde Postman sin token.
    """

    permission_classes = [IsAdmin]

    def get_permissions(self):
        # Permiso conmutable vía .env: abierto (AllowAny) o solo ADMIN.
        return [AllowAny()] if LLM_TEST_PUBLIC else [IsAdmin()]

    def get(self, request: Request) -> Response:
        return self._run()

    def post(self, request: Request) -> Response:
        return self._run(
            title=request.data.get("title"),
            description=request.data.get("description"),
            location=request.data.get("location"),
        )

    def _run(self, title=None, description=None, location=None) -> Response:
        diagnostic = deepseek.check_connection(
            title=title, description=description, location=location
        )
        # Deja constancia del modo de acceso en la respuesta del diagnóstico.
        diagnostic["config"]["public_test_endpoint"] = LLM_TEST_PUBLIC
        if diagnostic["ok"]:
            return Response(diagnostic, status=status.HTTP_200_OK)

        # Un problema de configuración es 400 (lo arregla el usuario); un fallo al
        # llamar a la API (saldo, clave, red) es 502 (falla la dependencia externa).
        stage = (diagnostic.get("error") or {}).get("stage")
        code = (
            status.HTTP_400_BAD_REQUEST
            if stage == "config"
            else status.HTTP_502_BAD_GATEWAY
        )
        return Response(diagnostic, status=code)

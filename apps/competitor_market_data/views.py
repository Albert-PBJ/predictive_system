import os
from decimal import Decimal, InvalidOperation

from django.db.models import Q
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsAdmin
from apps.audit import services as audit
from apps.audit.models import ActionChoices
from apps.benchmarking.models import CompetitorMarketData, RejectedMarketData, ScrapeRun
from apps.competitor_market_data.enrichment import deepseek
from apps.competitor_market_data.scrapers import get_run_progress
from apps.competitor_market_data.scrapers.validation import get_latest_rate, stamp_price_usd

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
from apps.competitor_market_data.scrapers.mercadolibre_scraper import (
    finalize_mercadolibre,
    start_mercadolibre_run,
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
    "mercadolibre": {
        "start": start_mercadolibre_run,
        "finalize": finalize_mercadolibre,
        "needs_competitor": False,
    },
}

# Mapea el `source` de la URL al tag almacenado en CompetitorMarketData.source.
SOURCE_TAGS = {
    "instagram": CompetitorMarketData.SourceChoices.INSTAGRAM,
    "facebook": CompetitorMarketData.SourceChoices.FACEBOOK,
    "website": CompetitorMarketData.SourceChoices.WEBSITE,
    "mercadolibre": CompetitorMarketData.SourceChoices.MERCADOLIBRE,
}

DATA_PAGE_SIZE_DEFAULT = 10
DATA_PAGE_SIZE_MAX = 50


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


def _mark_run_failed(scrape_run, reason: str) -> None:
    """Marca el run como fallido (si existe) para no dejarlo colgado en 'En ejecución'."""
    if scrape_run is None:
        return
    from django.utils import timezone

    scrape_run.status = ScrapeRun.StatusChoices.FAILED
    scrape_run.finished_at = timezone.now()
    scrape_run.notes = (reason or "")[:2000]
    scrape_run.save(update_fields=["status", "finished_at", "notes"])


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

        # Límite por defecto desde la Configuración del Sistema (editable en UI).
        from apps.core import system_settings

        limit = request.data.get("limit", system_settings.scraper_default_limit())
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

        # Registra el run para la trazabilidad: conserva los términos/URLs y los
        # parámetros del scraping. `finalize` lo recupera por dataset_id, enlaza las
        # filas producidas y cierra el run con sus conteos.
        scrape_run = ScrapeRun.objects.create(
            source=SOURCE_TAGS[source],
            query=urls,
            params={"limit": limit, "competitor_name": request.data.get("competitor_name")},
            apify_run_id=run.get("id") or "",
            dataset_id=run.get("defaultDatasetId") or "",
            status=ScrapeRun.StatusChoices.RUNNING,
        )

        audit.log(
            request=request,
            action=ActionChoices.SCRAPE_START,
            description=f"Inició un scraping de «{source}» con límite {limit}.",
            target=scrape_run,
            target_model="ScrapeRun",
            metadata={"source": source, "limit": limit, "query": urls},
        )

        return Response(
            {
                "run_id": run.get("id"),
                "dataset_id": run.get("defaultDatasetId"),
                "status": run.get("status"),
                "scrape_run_id": scrape_run.id,
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

        # Recupera el run abierto en /start (por dataset_id) para enlazar las filas
        # y cerrarlo con sus conteos. Si no existe (p. ej. finalize directo), el
        # propio scraper crea uno al vuelo (ensure_scrape_run).
        scrape_run = ScrapeRun.objects.filter(dataset_id=dataset_id).order_by("-id").first()

        kwargs = {"dataset_id": dataset_id, "scrape_run": scrape_run}
        if config["needs_competitor"]:
            kwargs["urls"] = request.data.get("urls") or []
            kwargs["competitor_name"] = request.data.get("competitor_name") or None

        try:
            records = config["finalize"](**kwargs)
        except ValueError as exc:
            _mark_run_failed(scrape_run, str(exc))
            return Response({"error": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as exc:
            _mark_run_failed(scrape_run, str(exc))
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


def _parse_decimal(value):
    """Convierte un parámetro de query a Decimal; retorna None si es inválido/ausente."""
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_int(value, default):
    """Convierte un parámetro de query a int positivo; cae al default si es inválido."""
    try:
        parsed = int(value)
        return parsed if parsed >= 1 else default
    except (TypeError, ValueError):
        return default


def _serialize_market_data(record) -> dict:
    """Serializa un CompetitorMarketData histórico (incluye estado/municipio del competidor)."""
    competitor = record.competitor
    return {
        "id": record.id,
        # Prioriza el nombre normalizado del competidor; cae al texto del scraper.
        "competitor_name": (competitor.name if competitor else None) or record.competitor_name,
        "product_name": record.product_name,
        "category": record.category,
        "price": str(record.price) if record.price is not None else None,
        "currency": record.currency,
        "price_usd": str(record.price_usd) if record.price_usd is not None else None,
        "matched_product": record.product.name if record.product_id else None,
        "promotions": record.promotions,
        "is_in_stock": record.is_in_stock,
        "state": competitor.state if competitor else "",
        "municipality": competitor.municipality if competitor else "",
        "url": record.url,
        "scraped_at": record.scraped_at.isoformat() if record.scraped_at else None,
    }


class ScraperDataView(APIView):
    """
    GET /scrapers/<source>/data

    Lista los datos de competidores ya almacenados (CompetitorMarketData) para la
    fuente indicada (`instagram`, `facebook` o `website`), con paginación y filtros.
    A diferencia de /finalize (que devuelve solo el último run), este endpoint lee
    el histórico completo desde la base de datos.

    Parámetros de query (todos opcionales):
        page          (int, default 1)
        page_size     (int, default 10, máx 50)
        min_price     (decimal)   — precio mínimo
        max_price     (decimal)   — precio máximo
        state         (string)    — estado del competidor (coincidencia exacta, sin distinguir mayúsculas)
        municipality  (string)    — municipio del competidor (idem)
        search        (string)    — búsqueda general (producto, competidor, categoría, promoción)

    Respuesta:
        {count, page, page_size, num_pages, results: [...],
         available_states: [...], available_municipalities: [...]}
    """

    permission_classes = [IsAdmin]

    def get(self, request: Request, source: str) -> Response:
        tag = SOURCE_TAGS.get(source)
        if tag is None:
            return Response(
                {"error": f"Fuente de datos desconocida: '{source}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = (
            CompetitorMarketData.objects.filter(source=tag)
            .select_related("competitor", "product")
            .order_by("-scraped_at")
        )

        min_price = _parse_decimal(request.query_params.get("min_price"))
        if min_price is not None:
            qs = qs.filter(price__gte=min_price)
        max_price = _parse_decimal(request.query_params.get("max_price"))
        if max_price is not None:
            qs = qs.filter(price__lte=max_price)

        state = (request.query_params.get("state") or "").strip()
        if state:
            qs = qs.filter(competitor__state__iexact=state)
        municipality = (request.query_params.get("municipality") or "").strip()
        if municipality:
            qs = qs.filter(competitor__municipality__iexact=municipality)

        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(product_name__icontains=search)
                | Q(competitor_name__icontains=search)
                | Q(competitor__name__icontains=search)
                | Q(category__icontains=search)
                | Q(promotions__icontains=search)
            )

        # Opciones para los filtros desplegables (estados/municipios con datos en esta fuente).
        base_qs = CompetitorMarketData.objects.filter(source=tag, competitor__isnull=False)
        available_states = sorted(
            v
            for v in base_qs.values_list("competitor__state", flat=True).distinct()
            if v
        )
        available_municipalities = sorted(
            v
            for v in base_qs.values_list("competitor__municipality", flat=True).distinct()
            if v
        )

        page_size = min(_parse_int(request.query_params.get("page_size"), DATA_PAGE_SIZE_DEFAULT), DATA_PAGE_SIZE_MAX)
        page = _parse_int(request.query_params.get("page"), 1)
        count = qs.count()
        num_pages = max(1, -(-count // page_size))  # ceil division
        page = min(page, num_pages)
        offset = (page - 1) * page_size
        records = qs[offset:offset + page_size]

        return Response(
            {
                "count": count,
                "page": page,
                "page_size": page_size,
                "num_pages": num_pages,
                "results": [_serialize_market_data(r) for r in records],
                "available_states": available_states,
                "available_municipalities": available_municipalities,
            },
            status=status.HTTP_200_OK,
        )


# ── Edición / borrado manual de registros (admin) ─────────────────────────────


class CompetitorMarketDataEditSerializer(serializers.ModelSerializer):
    """Campos del registro que el admin puede corregir a mano desde la tabla.

    Se editan los atributos del anuncio (producto, categoría, precio, etc.); el
    competidor es una entidad normalizada y se gestiona aparte (fusión en el admin).
    """

    class Meta:
        model = CompetitorMarketData
        fields = ["product_name", "category", "price", "currency", "promotions", "is_in_stock"]


class ScraperDataDetailView(APIView):
    """
    PATCH/DELETE /scrapers/<source>/data/<pk>

    Permite al admin corregir (PATCH) o eliminar (DELETE) un registro de
    CompetitorMarketData ya guardado. Al cambiar precio/moneda se recalcula el
    snapshot en USD con la tasa más reciente.

    Acceso: ADMIN.
    """

    permission_classes = [IsAdmin]

    def _get_record(self, source, pk):
        tag = SOURCE_TAGS.get(source)
        if tag is None:
            return None, Response(
                {"error": f"Fuente de datos desconocida: '{source}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        record = (
            CompetitorMarketData.objects.select_related("competitor", "product")
            .filter(pk=pk, source=tag)
            .first()
        )
        if record is None:
            return None, Response(
                {"error": "Registro no encontrado."}, status=status.HTTP_404_NOT_FOUND
            )
        return record, None

    def patch(self, request, source, pk):
        record, err = self._get_record(source, pk)
        if err is not None:
            return err

        serializer = CompetitorMarketDataEditSerializer(record, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        price_touched = "price" in serializer.validated_data or "currency" in serializer.validated_data
        record = serializer.save()

        if price_touched:
            # Corrección manual del precio: re-stampar el snapshot en USD con la tasa vigente.
            rate = get_latest_rate()
            usd_rate = (rate.parallel_rate or rate.bcv_rate) if rate else None
            rate_date = rate.date if rate else None
            record.price_usd = None
            record.exchange_rate_used = None
            record.rate_date = None
            stamp_price_usd(record, usd_rate, rate_date)
            record.save(update_fields=["price_usd", "exchange_rate_used", "rate_date"])

        record.refresh_from_db()
        return Response(_serialize_market_data(record), status=status.HTTP_200_OK)

    def delete(self, request, source, pk):
        record, err = self._get_record(source, pk)
        if err is not None:
            return err
        record.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def _serialize_rejected(record) -> dict:
    """Serializa un RejectedMarketData (incluye el motivo del descarte)."""
    return {
        "id": record.id,
        "competitor_name": record.competitor_name or None,
        "product_name": record.product_name or None,
        "category": record.category or None,
        "price": str(record.price) if record.price is not None else None,
        "currency": record.currency or None,
        "url": record.url or None,
        "rejection_reason": record.rejection_reason,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


class ScraperRejectedView(APIView):
    """
    GET /scrapers/<source>/rejected

    Lista los registros DESCARTADOS por la validación de calidad para esa fuente
    (datos no plausibles que no entraron a la tabla principal), con su motivo de
    descarte. Paginado y filtrable por búsqueda. Acceso: ADMIN.
    """

    permission_classes = [IsAdmin]

    def get(self, request, source):
        tag = SOURCE_TAGS.get(source)
        if tag is None:
            return Response(
                {"error": f"Fuente de datos desconocida: '{source}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = RejectedMarketData.objects.filter(source=tag).order_by("-created_at")
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(product_name__icontains=search)
                | Q(competitor_name__icontains=search)
                | Q(category__icontains=search)
                | Q(rejection_reason__icontains=search)
            )

        page_size = min(_parse_int(request.query_params.get("page_size"), DATA_PAGE_SIZE_DEFAULT), DATA_PAGE_SIZE_MAX)
        page = _parse_int(request.query_params.get("page"), 1)
        count = qs.count()
        num_pages = max(1, -(-count // page_size))
        page = min(page, num_pages)
        offset = (page - 1) * page_size
        records = qs[offset:offset + page_size]

        return Response(
            {
                "count": count,
                "page": page,
                "page_size": page_size,
                "num_pages": num_pages,
                "results": [_serialize_rejected(r) for r in records],
            },
            status=status.HTTP_200_OK,
        )


class ScraperRejectedDetailView(APIView):
    """
    DELETE /scrapers/<source>/rejected/<pk>

    Elimina definitivamente un registro descartado (limpieza). Acceso: ADMIN.
    """

    permission_classes = [IsAdmin]

    def delete(self, request, source, pk):
        tag = SOURCE_TAGS.get(source)
        if tag is None:
            return Response(
                {"error": f"Fuente de datos desconocida: '{source}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        deleted, _ = RejectedMarketData.objects.filter(pk=pk, source=tag).delete()
        if not deleted:
            return Response({"error": "Registro no encontrado."}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)

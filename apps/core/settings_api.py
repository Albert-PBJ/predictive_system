"""API REST de la Configuración del Sistema (``SystemSettings``).

Expone:

* ``GET  /api/settings/``            → lee la configuración (Gerente+).
* ``PATCH /api/settings/``           → actualiza la configuración (Admin).
* ``POST /api/settings/exchange-rate``        → carga manual de la tasa (Admin).
* ``POST /api/settings/exchange-rate/fetch``  → baja la tasa de la API pública (Admin).
* ``GET|POST /api/settings/llm-test``         → diagnóstico de conexión con DeepSeek (Admin).

Lectura para Gerente+ (para que pueda consultarla) y escritura solo Admin (es
configuración global de infraestructura/negocio). Los **secretos** (claves de API)
no se editan aquí: se siguen leyendo del entorno; la respuesta solo indica si están
*presentes*, sin exponerlas.
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from importlib import util as importlib_util

from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsAdmin, IsManager, IsViewer
from apps.audit import services as audit
from apps.audit.models import ActionChoices
from apps.competitor_market_data.enrichment import deepseek

from . import system_settings
from .models import ExchangeRate, SystemSettings


class SystemSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = SystemSettings
        fields = (
            # Tasa de cambio
            "rate_basis",
            "rate_max_age_days",
            "exchange_rate_api_url",
            # Enriquecimiento LLM
            "use_llm_enrichment",
            "deepseek_model",
            "deepseek_base_url",
            "enable_llm_report_narrative",
            # OCR
            "use_vision_price_ocr",
            "ocr_languages",
            "ocr_use_gpu",
            "ocr_max_images_per_post",
            "ocr_mag_ratio",
            "ocr_assume_usd_for_bare_number",
            "ocr_bare_number_max_usd",
            # Scrapers
            "discard_instagram_without_price",
            "scraper_default_limit",
            # Valores por defecto de negocio
            "default_iva_pct",
            "default_quote_expiry_days",
            # Empresa
            "company_name",
            "company_rif",
            "company_address",
            "company_phone",
            "company_email",
            "company_website",
            "company_logo_url",
            "updated_at",
        )
        read_only_fields = ("updated_at",)


def _package_available(name: str) -> bool:
    """True si un paquete opcional está instalado, sin importarlo (barato)."""
    try:
        return importlib_util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _meta_block() -> dict:
    """Estado de integraciones para la UI (sin exponer secretos)."""
    rate = ExchangeRate.objects.order_by("-date").first()
    eff = system_settings.effective_rate(rate) if rate else None
    return {
        # Sólo presencia de la clave (secreto): nunca su valor.
        "deepseek_key_present": bool(system_settings.deepseek_api_key()),
        "openai_installed": _package_available("openai"),
        "easyocr_installed": _package_available("easyocr"),
        "latest_rate": None if rate is None else {
            "date": rate.date.isoformat(),
            "bcv_rate": str(rate.bcv_rate),
            "parallel_rate": str(rate.parallel_rate) if rate.parallel_rate is not None else None,
            "effective_rate": str(eff) if eff is not None else None,
            "source": rate.source,
        },
    }


class SystemSettingsView(APIView):
    """GET (Gerente+) / PATCH (Admin) de la configuración global (fila única)."""

    def get_permissions(self):
        # Leer: Gerente o Admin. Escribir: solo Admin.
        if self.request.method in ("PATCH", "PUT"):
            return [IsAdmin()]
        return [IsManager()]

    def get(self, request):
        settings_obj = system_settings.get_settings()
        return Response(
            {"settings": SystemSettingsSerializer(settings_obj).data, "meta": _meta_block()},
            status=status.HTTP_200_OK,
        )

    def patch(self, request):
        settings_obj = system_settings.get_settings(use_cache=False)
        serializer = SystemSettingsSerializer(settings_obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()  # save() fuerza pk=1 e invalida la caché
        changed = sorted(k for k in serializer.validated_data.keys())
        audit.log(
            request=request,
            action=ActionChoices.SETTINGS_UPDATE,
            description=(
                "Actualizó la configuración del sistema"
                + (f" ({', '.join(changed)})." if changed else ".")
            ),
            metadata={"changed": changed},
        )
        return Response(
            {"settings": serializer.data, "meta": _meta_block()},
            status=status.HTTP_200_OK,
        )


class CompanyInfoView(APIView):
    """GET /api/settings/company — datos de la empresa (NO sensibles) para los PDFs.

    Accesible a cualquier usuario autenticado (``IsViewer``): los presupuestos los
    descarga el vendedor, que no es Gerente, así que el branding no puede quedar
    detrás del permiso de la configuración completa.
    """

    permission_classes = [IsViewer]

    def get(self, request):
        return Response(system_settings.company_info(), status=status.HTTP_200_OK)


def _to_decimal(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.0001"))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _rate_payload(rate, freshness=None):
    eff = system_settings.effective_rate(rate)
    payload = {
        "date": rate.date.isoformat(),
        "bcv_rate": str(rate.bcv_rate),
        "parallel_rate": str(rate.parallel_rate) if rate.parallel_rate is not None else None,
        "effective_rate": str(eff) if eff is not None else None,
        "rate_basis": system_settings.rate_basis(),
        "source": rate.source,
    }
    if freshness is not None:
        payload["freshness"] = {
            "is_stale": freshness.get("is_stale"),
            "age_days": freshness.get("age_days"),
        }
    return payload


class ExchangeRateSetView(APIView):
    """POST /api/settings/exchange-rate — carga MANUAL de la tasa (Admin).

    Cuerpo: ``{"bcv": "36.5", "parallel": "40", "date": "YYYY-MM-DD"}`` (parallel y
    date opcionales). Hace upsert de la ``ExchangeRate`` del día y reevalúa la alerta
    de frescura.
    """

    permission_classes = [IsAdmin]

    def post(self, request):
        bcv = _to_decimal(request.data.get("bcv"))
        parallel = _to_decimal(request.data.get("parallel"))
        if bcv is None:
            return Response(
                {"error": "La tasa BCV es obligatoria y debe ser un número."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_date = date.today()
        raw_date = request.data.get("date")
        if raw_date:
            try:
                target_date = datetime.strptime(str(raw_date), "%Y-%m-%d").date()
            except ValueError:
                return Response(
                    {"error": "Fecha inválida; usa el formato YYYY-MM-DD."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        rate, _created = ExchangeRate.objects.update_or_create(
            date=target_date,
            defaults={
                "bcv_rate": bcv,
                "parallel_rate": parallel,
                "source": ExchangeRate.SourceChoices.BCV,
            },
        )
        freshness = _check_freshness()
        audit.log(
            request=request,
            action=ActionChoices.RATE_UPDATE,
            description=(
                f"Cargó manualmente la tasa de cambio del {target_date.isoformat()}: "
                f"BCV {bcv}" + (f", paralela {parallel}" if parallel is not None else "") + "."
            ),
            target=rate,
            metadata={
                "date": target_date.isoformat(),
                "bcv": str(bcv),
                "parallel": str(parallel) if parallel is not None else None,
                "manual": True,
            },
        )
        return Response(_rate_payload(rate, freshness), status=status.HTTP_200_OK)


class ExchangeRateFetchView(APIView):
    """POST /api/settings/exchange-rate/fetch — baja la tasa de la API pública (Admin)."""

    permission_classes = [IsAdmin]

    def post(self, request):
        from apps.core.management.commands.fetch_exchange_rate import fetch_rates

        url = system_settings.exchange_rate_api_url()
        try:
            # Prioriza la librería pyDolarVenezuela; cae a la API HTTP solo si la
            # librería no consigue la BCV.
            bcv, parallel, provider = fetch_rates(url)
        except Exception as exc:  # red, parseo, timeout (respaldo HTTP)
            return Response(
                {"error": f"No se pudo obtener la tasa: {exc}", "api_url": url},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        if bcv is None:
            return Response(
                {"error": "No se obtuvo una tasa BCV válida de ninguna fuente.", "api_url": url},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        rate, _created = ExchangeRate.objects.update_or_create(
            date=date.today(),
            defaults={
                "bcv_rate": bcv,
                "parallel_rate": parallel,
                "source": ExchangeRate.SourceChoices.BCV,
            },
        )
        freshness = _check_freshness()
        payload = _rate_payload(rate, freshness)
        payload["provider"] = provider
        audit.log(
            request=request,
            action=ActionChoices.RATE_UPDATE,
            description=(
                f"Actualizó la tasa de cambio desde la fuente «{provider}»: BCV {bcv}"
                + (f", paralela {parallel}" if parallel is not None else "") + "."
            ),
            target=rate,
            metadata={
                "date": rate.date.isoformat(),
                "bcv": str(bcv),
                "parallel": str(parallel) if parallel is not None else None,
                "provider": provider,
                "manual": False,
            },
        )
        return Response(payload, status=status.HTTP_200_OK)


def _check_freshness():
    """Reevalúa la frescura de la tasa con el umbral configurado (gestiona la alerta)."""
    from apps.core.management.commands.fetch_exchange_rate import check_rate_freshness

    return check_rate_freshness(system_settings.rate_max_age_days())


class SettingsLLMTestView(APIView):
    """GET|POST /api/settings/llm-test — diagnóstico de conexión con DeepSeek (Admin).

    Hace UNA llamada de prueba con datos de ejemplo y reporta el estado de
    configuración + resultado/error, sin exponer la clave. Mismo contrato que el
    endpoint de los scrapers, para que la página de Configuración valide la IA.
    """

    permission_classes = [IsAdmin]

    def get(self, request):
        return self._run()

    def post(self, request):
        return self._run(
            title=request.data.get("title"),
            description=request.data.get("description"),
            location=request.data.get("location"),
        )

    def _run(self, title=None, description=None, location=None):
        diagnostic = deepseek.check_connection(title=title, description=description, location=location)
        if diagnostic["ok"]:
            return Response(diagnostic, status=status.HTTP_200_OK)
        stage = (diagnostic.get("error") or {}).get("stage")
        code = status.HTTP_400_BAD_REQUEST if stage == "config" else status.HTTP_502_BAD_GATEWAY
        return Response(diagnostic, status=code)

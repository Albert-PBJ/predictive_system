"""Accesor de la configuración global del sistema (``SystemSettings``).

Punto único para leer los parámetros configurables en caliente. Resuelve con la
semántica **"la BD manda, sembrada del entorno"**:

* La primera vez que se necesita, la fila singleton (``pk=1``) se crea tomando como
  valores iniciales los del ``.env`` (``_env_defaults``); desde ahí la BD es la
  fuente de verdad y el ``.env`` solo actúa de *bootstrap*.
* Si la tabla aún no existe (BD recién creada, antes de ``migrate``) o la BD no está
  disponible, los getters caen a los valores del entorno/los defaults **sin lanzar
  excepción**, para que ningún consumidor (scraper, servicio de ventas, comando) se
  rompa por la configuración.
* Los **secretos** (``DEEPSEEK_API_KEY``, ``APIFY_API_KEY``, credenciales) **no** se
  guardan en la BD: se siguen leyendo del entorno (``deepseek_api_key``).

La instancia se cachea brevemente (``django.core.cache``) y ``SystemSettings.save()``
invalida la caché, de modo que un cambio desde la UI se refleja de inmediato.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal

from django.core.cache import cache

from .models import SYSTEM_SETTINGS_CACHE_KEY

logger = logging.getLogger(__name__)

_CACHE_TTL = 30  # segundos: cota la lectura repetida sin volver "pegajosa" la config


# ── Lectura del entorno (bootstrap / fallback) ────────────────────────────────

def _env_flag(name: str, default: str = "False") -> bool:
    return os.environ.get(name, default).lower() in ("1", "true", "yes")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_defaults() -> dict:
    """Valores iniciales del singleton, tomados del entorno (o sus defaults)."""
    return {
        # Tasa de cambio
        "rate_max_age_days": _env_int("EXCHANGE_RATE_MAX_AGE_DAYS", 2),
        "exchange_rate_api_url": os.environ.get(
            "EXCHANGE_RATE_API_URL", "https://pydolarve.org/api/v1/dollar"
        ),
        # Enriquecimiento LLM
        "use_llm_enrichment": _env_flag("USE_LLM_ENRICHMENT"),
        "deepseek_model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "deepseek_base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "enable_llm_report_narrative": _env_flag("ENABLE_LLM_REPORT_NARRATIVE", "True"),
        # OCR
        "use_vision_price_ocr": _env_flag("USE_VISION_PRICE_OCR"),
        "ocr_languages": os.environ.get("OCR_LANGUAGES", "es,en"),
        "ocr_use_gpu": _env_flag("OCR_USE_GPU"),
        "ocr_max_images_per_post": _env_int("OCR_MAX_IMAGES_PER_POST", 2),
        "ocr_mag_ratio": Decimal(str(_env_float("OCR_MAG_RATIO", 2.0))),
        "ocr_assume_usd_for_bare_number": _env_flag("OCR_ASSUME_USD_FOR_BARE_NUMBER"),
        "ocr_bare_number_max_usd": Decimal(str(_env_float("OCR_BARE_NUMBER_MAX_USD", 500.0))),
        # Scrapers
        "discard_instagram_without_price": _env_flag("DISCARD_INSTAGRAM_WITHOUT_PRICE"),
        "scraper_default_limit": _env_int("SCRAPER_DEFAULT_LIMIT", 50),
    }


# ── Singleton ─────────────────────────────────────────────────────────────────

def get_settings(use_cache: bool = True):
    """Devuelve la fila singleton de ``SystemSettings``.

    La crea (sembrada del entorno) la primera vez. Si la tabla no existe todavía o
    la BD falla, devuelve una instancia **no guardada** con los valores del entorno,
    para que los getters nunca lancen.
    """
    if use_cache:
        cached = cache.get(SYSTEM_SETTINGS_CACHE_KEY)
        if cached is not None:
            return cached

    from .models import SystemSettings

    try:
        obj, _created = SystemSettings.objects.get_or_create(pk=1, defaults=_env_defaults())
    except Exception as exc:  # tabla inexistente (pre-migrate) o BD no disponible
        logger.debug("SystemSettings no disponible (%s); se usan valores del entorno.", exc)
        return SystemSettings(pk=1, **_env_defaults())

    if use_cache:
        cache.set(SYSTEM_SETTINGS_CACHE_KEY, obj, _CACHE_TTL)
    return obj


# ── Getters tipados que usan los consumidores ─────────────────────────────────
#
# Cada uno resuelve un parámetro concreto. Los secretos vienen SIEMPRE del entorno.

def deepseek_api_key() -> str:
    """Clave de DeepSeek: secreto, SIEMPRE del entorno (nunca de la BD/UI)."""
    return os.environ.get("DEEPSEEK_API_KEY", "")


def llm_enrichment_enabled() -> bool:
    return bool(get_settings().use_llm_enrichment)


def deepseek_model() -> str:
    return get_settings().deepseek_model or "deepseek-chat"


def deepseek_base_url() -> str:
    return get_settings().deepseek_base_url or "https://api.deepseek.com"


def report_narrative_enabled() -> bool:
    """El reporte LLM se habilita con la clave + su propio interruptor (no el de scrapers)."""
    return bool(get_settings().enable_llm_report_narrative) and bool(deepseek_api_key())


def vision_ocr_enabled() -> bool:
    return bool(get_settings().use_vision_price_ocr)


def ocr_languages() -> str:
    return get_settings().ocr_languages or "es,en"


def ocr_use_gpu() -> bool:
    return bool(get_settings().ocr_use_gpu)


def ocr_max_images_per_post() -> int:
    return int(get_settings().ocr_max_images_per_post or 2)


def ocr_mag_ratio() -> float:
    return float(get_settings().ocr_mag_ratio or 2.0)


def ocr_assume_usd_for_bare_number() -> bool:
    return bool(get_settings().ocr_assume_usd_for_bare_number)


def ocr_bare_number_max_usd() -> float:
    return float(get_settings().ocr_bare_number_max_usd or 500.0)


def discard_instagram_without_price() -> bool:
    return bool(get_settings().discard_instagram_without_price)


def scraper_default_limit() -> int:
    return int(get_settings().scraper_default_limit or 50)


def rate_max_age_days() -> int:
    return int(get_settings().rate_max_age_days or 2)


def exchange_rate_api_url() -> str:
    return get_settings().exchange_rate_api_url or "https://pydolarve.org/api/v1/dollar"


def rate_basis() -> str:
    return get_settings().rate_basis or "PAR"


def effective_rate(rate) -> Decimal | None:
    """Tasa para convertir USD→VES según ``rate_basis`` (paralela / BCV / promedio).

    ``rate`` es un ``ExchangeRate`` (o None). Degrada con tolerancia: si la base
    elegida no tiene valor, cae a la otra disponible.
    """
    if not rate:
        return None
    basis = rate_basis()
    bcv = rate.bcv_rate
    par = rate.parallel_rate
    if basis == "BCV":
        return bcv or par
    if basis == "AVG" and bcv is not None and par is not None:
        return (Decimal(bcv) + Decimal(par)) / Decimal("2")
    # PARALLEL (default) o promedio sin ambas tasas: paralela y, si no, BCV.
    return par or bcv


def default_iva_pct() -> Decimal:
    return Decimal(str(get_settings().default_iva_pct or "16.00"))


def default_quote_expiry_days() -> int:
    return int(get_settings().default_quote_expiry_days or 15)


def company_info() -> dict:
    s = get_settings()
    return {
        "name": s.company_name or "Inversiones Maescar, C.A.",
        "rif": s.company_rif or "",
        "address": s.company_address or "",
        "phone": s.company_phone or "",
        "email": s.company_email or "",
        "website": s.company_website or "",
        "logo_url": s.company_logo_url or "",
    }

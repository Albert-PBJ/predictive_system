"""Caché en memoria, serialización (joblib) y registro de modelos (``PredictionLog``).

El servicio entrena **bajo demanda** (con estos datos el entrenamiento tarda <1 s) y
cachea el resultado en memoria, invalidándolo con una huella (*fingerprint*) de los
datos: si entran ventas/tasas/scrapes nuevos, la huella cambia y se reentrena. El
comando ``train_models`` además persiste los artefactos (joblib) y escribe filas en
``PredictionLog`` para alimentar la página de registro/métricas y dejar evidencia
reproducible para la tesis.
"""

from __future__ import annotations

import hashlib

import joblib
from django.conf import settings
from django.utils import timezone

from ..models import PredictionLog

# Caché simple en proceso: key -> (fingerprint, value)
_CACHE: dict[str, tuple[str, object]] = {}


def data_fingerprint() -> str:
    """Huella barata del estado de los datos (cuenta + última modificación)."""
    from apps.benchmarking.models import CompetitorMarketData
    from apps.core.models import ExchangeRate, ProductPriceHistory
    from apps.sales.models import Quote, Sale

    last_sale = Sale.objects.order_by("-updated_at").values_list("updated_at", flat=True).first()
    parts = [
        Sale.objects.count(), str(last_sale),
        ExchangeRate.objects.count(),
        ProductPriceHistory.objects.count(),
        Quote.objects.count(),
        CompetitorMarketData.objects.count(),
    ]
    return hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()


def cached(key: str, builder):
    """Devuelve el valor cacheado para ``key`` si la huella de datos no cambió;
    si no, ejecuta ``builder()``, lo cachea y lo devuelve."""
    fp = data_fingerprint()
    hit = _CACHE.get(key)
    if hit is not None and hit[0] == fp:
        return hit[1]
    value = builder()
    _CACHE[key] = (fp, value)
    return value


def get_cached(key: str):
    """Lectura sin builder: devuelve ``(True, value)`` si hay un valor cacheado cuya huella
    coincide con la actual, o ``(False, None)`` si no. Útil para cachear de forma
    **condicional** (p. ej. guardar solo respuestas válidas del LLM y no los fallbacks)."""
    fp = data_fingerprint()
    hit = _CACHE.get(key)
    if hit is not None and hit[0] == fp:
        return True, hit[1]
    return False, None


def set_cached(key: str, value) -> None:
    """Guarda ``value`` bajo ``key`` con la huella de datos actual."""
    _CACHE[key] = (data_fingerprint(), value)


def clear_cache() -> None:
    _CACHE.clear()


# --------------------------------------------------------------------------- #
# Persistencia de artefactos (joblib)
# --------------------------------------------------------------------------- #
def models_dir():
    settings.ML_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    return settings.ML_MODELS_DIR


def save_artifact(name: str, obj) -> str:
    path = models_dir() / f"{name}.joblib"
    joblib.dump(obj, path)
    return str(path)


def load_artifact(name: str):
    path = models_dir() / f"{name}.joblib"
    return joblib.load(path) if path.exists() else None


# --------------------------------------------------------------------------- #
# Registro en PredictionLog
# --------------------------------------------------------------------------- #
def upsert_prediction_log(
    *, name: str, model_type: str, metrics: dict | None,
    hyperparameters: dict | None = None, dataset_description: str = "",
    model_file_path: str = "", make_active: bool = True,
) -> PredictionLog:
    """Crea una fila de ``PredictionLog`` (y desactiva las anteriores del mismo tipo
    si ``make_active``)."""
    metrics = metrics or {}
    if make_active:
        PredictionLog.objects.filter(model_type=model_type, is_active=True).update(is_active=False)
    return PredictionLog.objects.create(
        name=name,
        model_type=model_type,
        r2_score=metrics.get("r2"),
        rmse=metrics.get("rmse"),
        mae=metrics.get("mae"),
        metrics=metrics,
        hyperparameters=hyperparameters or {},
        trained_at=timezone.now(),
        dataset_description=dataset_description,
        model_file_path=model_file_path,
        is_active=make_active,
    )

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

from ..models import PredictionLog, TrainingRun

# Caché simple en proceso: key -> (fingerprint, value)
_CACHE: dict[str, tuple[str, object]] = {}


def data_fingerprint() -> str:
    """Huella barata del estado de los datos (cuenta + última modificación).

    Incluye la **fecha de corte del entrenamiento**: cambiarla cambia qué datos ven los
    modelos, así que debe invalidar la caché igual que si hubieran entrado ventas nuevas.
    """
    from apps.benchmarking.models import CompetitorMarketData
    from apps.core.models import ExchangeRate, ProductPriceHistory
    from apps.sales.models import Quote, Sale

    from .datasets import training_cutoff

    last_sale = Sale.objects.order_by("-updated_at").values_list("updated_at", flat=True).first()
    parts = [
        Sale.objects.count(), str(last_sale),
        ExchangeRate.objects.count(),
        ProductPriceHistory.objects.count(),
        Quote.objects.count(),
        CompetitorMarketData.objects.count(),
        str(training_cutoff()),
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
    make_active: bool = True,
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
        is_active=make_active,
    )


# --------------------------------------------------------------------------- #
# Historial de reentrenamientos (TrainingRun) — evolución de la precisión
# --------------------------------------------------------------------------- #
def _snapshot_active_metrics() -> list[dict]:
    """Congela las métricas de los modelos **activos** de ``PredictionLog`` en una lista
    de dicts serializable, para guardarla como instantánea de un reentrenamiento."""
    snapshot = []
    for pl in PredictionLog.objects.filter(is_active=True).order_by("model_type"):
        m = pl.metrics or {}
        # La técnica es el sufijo del nombre (p. ej. ``sales_linear`` → ``linear``).
        technique = pl.name.rsplit("_", 1)[-1] if pl.name and "_" in pl.name else None
        snapshot.append({
            "model_type": pl.model_type,
            "model_type_display": pl.get_model_type_display(),
            "name": pl.name,
            "technique": technique,
            "r2": pl.r2_score,
            "rmse": pl.rmse,
            "mae": pl.mae,
            "accuracy": m.get("accuracy"),
            "precision": m.get("precision"),
            "recall": m.get("recall"),
        })
    return snapshot


def record_training_run(*, trigger: str = "CMD", triggered_by: str = "") -> TrainingRun | None:
    """Registra una fila de ``TrainingRun`` con la instantánea de las métricas activas.

    Se llama al final de cada reentrenamiento (comando ``train_models`` y botón del panel),
    de modo que el historial acumule un punto por corrida y se pueda graficar la evolución
    de la precisión. Si no hay modelos activos (nada que capturar) no crea la fila."""
    if trigger not in {c.value for c in TrainingRun.TriggerChoices}:
        trigger = TrainingRun.TriggerChoices.COMMAND
    snapshot = _snapshot_active_metrics()
    if not snapshot:
        return None
    return TrainingRun.objects.create(
        trained_at=timezone.now(),
        trigger=trigger,
        triggered_by=(triggered_by or "")[:150],
        models_metrics=snapshot,
    )


def ensure_baseline_run() -> TrainingRun | None:
    """Si aún no hay historial pero sí modelos entrenados, registra un punto **base** con
    el estado actual, para que la gráfica de evolución no arranque vacía. Idempotente en la
    práctica: solo actúa cuando ``TrainingRun`` está vacío."""
    if TrainingRun.objects.exists():
        return None
    return record_training_run(trigger="CMD", triggered_by="inicial")

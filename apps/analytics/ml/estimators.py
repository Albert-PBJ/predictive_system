"""Fábrica de modelos del módulo predictivo.

Centraliza la creación de los estimadores para que cada pronóstico elija el modelo
que mejor le va (ver tabla en el plan), garantizando que las tres técnicas exigidas
por la tesis se usen al menos una vez en el sistema:

- ``"linear"``  -> Ridge (regresión lineal regularizada, con escalado) / LogisticRegression.
- ``"tree"``    -> Árbol de decisión (regresor o clasificador).
- ``"xgboost"`` -> Gradient boosting (XGBoost).

Mantener todos los modelos disponibles aquí permite además sobreescribir el modelo
por querystring (``?model=``) para experimentar/comparar en el contexto de la tesis,
aunque la UI fije uno por gráfico.
"""

from __future__ import annotations

from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor
from xgboost import XGBClassifier, XGBRegressor

# Metadatos legibles de cada modelo (para mostrarlos en el frontend).
MODEL_META: dict[str, dict[str, str]] = {
    "linear": {"label": "Regresión lineal (Ridge)", "library": "scikit-learn"},
    "tree": {"label": "Árbol de decisión", "library": "scikit-learn"},
    "xgboost": {"label": "XGBoost", "library": "xgboost"},
}

RANDOM_STATE = 42


def make_regressor(model_key: str):
    """Devuelve un regresor configurado para series mensuales cortas (~50 puntos).

    El modelo lineal va dentro de un ``Pipeline`` con ``StandardScaler`` porque los
    rezagos y la tasa de cambio están en escalas muy distintas.
    """
    if model_key == "linear":
        return Pipeline([
            ("scaler", StandardScaler()),
            ("model", Ridge(alpha=1.0, random_state=RANDOM_STATE)),
        ])
    if model_key == "tree":
        # Árboles poco profundos: con pocos datos un árbol grande memoriza.
        return DecisionTreeRegressor(
            max_depth=4, min_samples_leaf=3, random_state=RANDOM_STATE,
        )
    if model_key == "xgboost":
        return XGBRegressor(
            n_estimators=300, max_depth=3, learning_rate=0.05,
            subsample=0.9, colsample_bytree=0.9, reg_lambda=1.0,
            min_child_weight=2, random_state=RANDOM_STATE, n_jobs=2,
            objective="reg:squarederror",
        )
    raise ValueError(f"Modelo de regresión no soportado: {model_key!r}")


def make_classifier(model_key: str):
    """Devuelve un clasificador (usado por el pronóstico de conversión de presupuestos)."""
    if model_key == "linear":
        return Pipeline([
            ("scaler", StandardScaler()),
            ("model", LogisticRegression(max_iter=1000, class_weight="balanced")),
        ])
    if model_key == "tree":
        # Sin class_weight balanceado: con conversión ~78% positiva, balancear hundía
        # la exactitud por debajo de la línea base y descalibraba las probabilidades que
        # usamos para rankear el pipeline. El árbol aprende igual las señales del seed.
        return DecisionTreeClassifier(
            max_depth=4, min_samples_leaf=8, random_state=RANDOM_STATE,
        )
    if model_key == "xgboost":
        return XGBClassifier(
            n_estimators=200, max_depth=3, learning_rate=0.07,
            subsample=0.9, colsample_bytree=0.9, reg_lambda=1.0,
            random_state=RANDOM_STATE, n_jobs=2, eval_metric="logloss",
        )
    raise ValueError(f"Modelo de clasificación no soportado: {model_key!r}")


def feature_importances(estimator, feature_names: list[str]) -> list[dict]:
    """Importancia de variables, normalizada a [0,1] y ordenada desc.

    Soporta árboles/XGBoost (``feature_importances_``) y el pipeline lineal
    (magnitud de los coeficientes). Devuelve ``[]`` si el modelo no la expone.
    """
    model = estimator
    if isinstance(estimator, Pipeline):
        model = estimator.named_steps["model"]

    importances = None
    if hasattr(model, "feature_importances_"):
        importances = list(model.feature_importances_)
    elif hasattr(model, "coef_"):
        coef = model.coef_
        coef = coef[0] if getattr(coef, "ndim", 1) > 1 else coef
        importances = [abs(float(c)) for c in coef]

    if not importances:
        return []

    total = sum(importances) or 1.0
    pairs = [
        {"feature": name, "importance": round(val / total, 4)}
        for name, val in zip(feature_names, importances)
    ]
    return sorted(pairs, key=lambda d: d["importance"], reverse=True)

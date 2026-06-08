"""Capa de Machine Learning del módulo predictivo.

Submódulos:
- ``datasets``    : construcción de DataFrames de pandas a partir del ORM.
- ``features``    : ingeniería de variables (calendario, rezagos, shock cambiario).
- ``estimators``  : fábrica de modelos (regresión lineal, árbol de decisión, XGBoost).
- ``forecasters`` : un constructor de pronóstico por objetivo (demanda, ventas, tasa, etc.).
- ``registry``    : caché en memoria, serialización (joblib) y registro en ``PredictionLog``.
"""

"""Ingeniería de variables y utilidades de calendario para las series mensuales.

Las series del sistema son mensuales y se identifican con una cadena ``"YYYY-MM"``
(periodo). Aquí viven los helpers para movernos entre periodos, completar huecos,
etiquetar en español y construir las variables de calendario que alimentan a los
modelos. La construcción de la matriz supervisada (rezagos + calendario + exógenas)
vive en ``forecasters.py`` porque está acoplada al esquema de pronóstico.
"""

from __future__ import annotations

import math
from datetime import date

# Etiquetas de mes en español (para los ejes y tooltips del frontend).
SPANISH_MONTHS = [
    "Ene", "Feb", "Mar", "Abr", "May", "Jun",
    "Jul", "Ago", "Sep", "Oct", "Nov", "Dic",
]


def period_of(d: date) -> str:
    """Convierte una fecha a su periodo mensual ``"YYYY-MM"``."""
    return f"{d.year:04d}-{d.month:02d}"


def period_label(period: str) -> str:
    """``"2025-01"`` -> ``"Ene 2025"`` (etiqueta legible en español)."""
    year, month = period.split("-")
    return f"{SPANISH_MONTHS[int(month) - 1]} {year}"


def _period_index(period: str) -> int:
    """Índice absoluto de meses desde el año 0 (para aritmética de periodos)."""
    year, month = period.split("-")
    return int(year) * 12 + (int(month) - 1)


def add_period(period: str, k: int) -> str:
    """Suma ``k`` meses a un periodo (``k`` puede ser negativo)."""
    idx = _period_index(period) + k
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def period_diff(a: str, b: str) -> int:
    """Número de meses de ``b`` a ``a`` (``a - b``)."""
    return _period_index(a) - _period_index(b)


def month_range(start: str, end: str) -> list[str]:
    """Lista de todos los periodos mensuales de ``start`` a ``end`` (inclusive)."""
    out, cur = [], start
    while _period_index(cur) <= _period_index(end):
        out.append(cur)
        cur = add_period(cur, 1)
    return out


def complete_monthly(
    pairs: list[tuple[str, float]], *, fill: float = 0.0,
    start: str | None = None, end: str | None = None,
) -> list[tuple[str, float]]:
    """Rellena los meses faltantes de una serie ``[(periodo, valor)]``.

    Útil para la demanda (un mes sin ventas = demanda 0, no un hueco). Si se pasan
    ``start``/``end`` se respeta ese rango; si no, se usa el min/max observado.
    """
    if not pairs:
        return []
    data = {p: v for p, v in pairs}
    lo = start or min(data)
    hi = end or max(data)
    return [(p, float(data.get(p, fill))) for p in month_range(lo, hi)]


def calendar_features(period: str, t_norm: float) -> dict[str, float]:
    """Variables de calendario de un periodo: estacionalidad (sin/cos), trimestre y
    un índice de tiempo normalizado ``t_norm`` que captura la tendencia."""
    month = int(period.split("-")[1])
    angle = 2.0 * math.pi * (month - 1) / 12.0
    return {
        "mes_sin": math.sin(angle),
        "mes_cos": math.cos(angle),
        "trimestre": float((month - 1) // 3 + 1),
        "t": float(t_norm),
    }

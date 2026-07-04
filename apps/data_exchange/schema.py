"""Definición de columnas y coerción de celdas para la importación/exportación Excel.

Cada operación (venta, movimiento, cliente, presupuesto) declara sus columnas con
``Column``; la coerción convierte el valor crudo de una celda de openpyxl (que puede
llegar como texto, número, fecha o booleano) al tipo de negocio, devolviendo un
mensaje de error en español cuando el dato no es válido. Los mensajes son los que ve
el usuario en la previsualización, así que deben ser claros y accionables.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


@dataclass
class Column:
    """Una columna del archivo Excel de una operación.

    ``kind`` gobierna la coerción: ``str``/``int``/``decimal``/``date``/``bool``/``choice``.
    Para ``choice``, ``choices`` mapea cada valor aceptado (en minúsculas, sin acentos
    relevantes) al código interno, y ``dropdown`` son las etiquetas ofrecidas como lista
    desplegable en la plantilla. ``line`` marca las columnas que forman parte de cada
    **línea** de una operación agrupada (p. ej. producto+cantidad de una venta), frente a
    las columnas de **cabecera** que se toman de la primera fila del grupo.
    """

    key: str
    header: str
    kind: str = "str"
    required: bool = False
    line: bool = False
    choices: dict[str, str] | None = None
    dropdown: list[str] | None = None
    help: str = ""


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return str(value).strip()


def coerce(column: Column, value):
    """Convierte el valor crudo de una celda al tipo de la columna.

    Devuelve ``(valor, error)``: ``error`` es ``None`` si todo bien, o un mensaje en
    español. Una celda vacía en una columna obligatoria produce error; en una opcional
    devuelve ``None``/valor por defecto sin error.
    """
    text = _as_text(value)
    empty = text == ""

    if empty:
        if column.required:
            return None, f"Falta «{column.header}» (obligatorio)."
        # Opcional y vacío: sin valor.
        return (False if column.kind == "bool" else None), None

    kind = column.kind
    if kind == "str":
        return text, None

    if kind == "int":
        try:
            if isinstance(value, bool):
                raise ValueError
            num = int(float(text)) if _is_number(text) else int(text)
            return num, None
        except (ValueError, TypeError):
            return None, f"«{column.header}» debe ser un número entero (recibido: {text})."

    if kind == "decimal":
        parsed = _to_decimal(text)
        if parsed is None:
            return None, f"«{column.header}» debe ser un número (recibido: {text})."
        if parsed < 0:
            return None, f"«{column.header}» no puede ser negativo (recibido: {text})."
        return parsed, None

    if kind == "date":
        parsed = _to_date(value, text)
        if parsed is None:
            return None, f"«{column.header}» debe ser una fecha válida (AAAA-MM-DD). Recibido: {text}."
        return parsed, None

    if kind == "bool":
        return _to_bool(text), None

    if kind == "choice":
        norm = text.strip().lower()
        mapping = column.choices or {}
        if norm in mapping:
            return mapping[norm], None
        opciones = ", ".join(sorted(set(mapping.values())))
        return None, f"«{column.header}» no reconocido: «{text}». Valores válidos: {opciones}."

    return text, None


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def _to_decimal(text: str) -> Decimal | None:
    raw = text.strip().replace(" ", "")
    # Formato venezolano/europeo ("1.234,56") vs anglo ("1,234.56"): si hay coma y
    # punto, el último separador es el decimal; si solo hay coma, es el decimal.
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return None


def _to_date(value, text: str) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


_TRUE = {"si", "sí", "s", "true", "verdadero", "1", "x", "yes", "y"}


def _to_bool(text: str) -> bool:
    return text.strip().lower() in _TRUE

"""Lectura/consejo del pronóstico redactado por LLM (apps/analytics).

Para cada gráfico de pronóstico del módulo predictivo, toma el pronóstico ya calculado
(``history``/``forecast``/``model``/``unit``/``value_kind`` — o el panel de conversión de
presupuestos) y le pide al LLM una breve **lectura accionable** de los resultados: qué
muestra la proyección, qué tan confiable es y qué conviene hacer. Es el equivalente, a
nivel de un solo gráfico, de lo que ``report_narrative`` hace para el reporte ejecutivo.

Reutiliza las mismas credenciales DeepSeek (API compatible con OpenAI) y el **mismo
interruptor** que la narrativa del reporte (``system_settings.report_narrative_enabled``).
Degrada de forma segura: ante cualquier problema (sin clave, sin SDK, error de red o JSON
inválido) arma un consejo **determinista** a partir de la tendencia y las métricas, de modo
que la tarjeta nunca queda vacía ni rompe la página. Solo el TEXTO lo redacta el modelo;
las cifras salen de los datos y se le prohíbe inventar.
"""

from __future__ import annotations

import json
import logging

from apps.core import system_settings

logger = logging.getLogger(__name__)

# La clave es un SECRETO (entorno); el interruptor/modelo/base_url los resuelve
# `system_settings` (la BD manda, sembrada del .env), igual que en report_narrative.
DEEPSEEK_API_KEY = system_settings.deepseek_api_key()

_REQUEST_TIMEOUT = 40  # segundos
_MAX_TOKENS = 900


def is_enabled() -> bool:
    """True si el LLM está habilitado (reutiliza el interruptor de la narrativa del reporte)."""
    return system_settings.report_narrative_enabled()


def _get_client():
    """Crea el cliente OpenAI apuntando a DeepSeek (import diferido: dependencia opcional)."""
    from openai import OpenAI  # noqa: import diferido

    return OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=system_settings.deepseek_base_url(),
        timeout=_REQUEST_TIMEOUT,
    )


# ── Formateadores cortos ──────────────────────────────────────────────────────


def _num(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _fmt(v, kind: str) -> str:
    """Formatea un valor según el ``value_kind`` del pronóstico."""
    n = _num(v)
    if n is None:
        return "—"
    if kind == "usd":
        return f"${n:,.2f}"
    if kind == "ves":
        return f"Bs {n:,.2f}"
    if kind == "rate":
        return f"{n:,.2f} Bs/USD"
    if kind == "percent":
        return f"{n:.1f}%"
    return f"{n:,.0f}"  # int


def _signed_pct(v) -> str:
    n = _num(v)
    if n is None:
        return "—"
    return f"{'+' if n > 0 else ''}{n:.1f}%"


# ── Tendencia y modelo ────────────────────────────────────────────────────────


def _trend(fc: dict) -> dict:
    """Resume la dirección del pronóstico (último real → fin del pronóstico)."""
    hist = fc.get("history") or []
    fcst = fc.get("forecast") or []
    last_hist = _num(hist[-1].get("value")) if hist else None
    first_fc = _num(fcst[0].get("value")) if fcst else None
    last_fc = _num(fcst[-1].get("value")) if fcst else None
    base = last_hist if last_hist is not None else first_fc
    end = last_fc if last_fc is not None else first_fc
    change_pct = None
    if base not in (None, 0) and end is not None:
        change_pct = (end - base) / abs(base) * 100
    if change_pct is None:
        direction = "estable"
    elif change_pct > 3:
        direction = "al alza"
    elif change_pct < -3:
        direction = "a la baja"
    else:
        direction = "estable"
    return {
        "last_hist": last_hist,
        "last_hist_label": hist[-1].get("label") if hist else None,
        "last_fc": last_fc,
        "last_fc_label": fcst[-1].get("label") if fcst else None,
        "change_pct": change_pct,
        "direction": direction,
        "n_forecast": len(fcst),
    }


def _reliability(model: dict | None):
    """Devuelve (palabra, score, etiqueta-métrica) describiendo la confiabilidad del modelo."""
    if not model:
        return "no evaluada", None, None
    acc = _num(model.get("accuracy"))
    if acc is not None:
        score, metric = acc, "exactitud"
    else:
        score, metric = _num(model.get("r2")), "R²"
    if score is None:
        return "no evaluada", None, metric
    if score >= 0.8:
        word = "alta"
    elif score >= 0.5:
        word = "moderada"
    else:
        word = "baja"
    return word, score, metric


def _model_line(model: dict | None) -> str:
    if not model:
        return "Modelo: no disponible (datos insuficientes)."
    label = model.get("label") or model.get("key") or "—"
    lib = f" ({model['library']})" if model.get("library") else ""
    acc = _num(model.get("accuracy"))
    if acc is not None:
        metrics = f"exactitud {acc:.2f}"
        if _num(model.get("precision")) is not None:
            metrics += f", precisión {_num(model['precision']):.2f}"
        if _num(model.get("recall")) is not None:
            metrics += f", recall {_num(model['recall']):.2f}"
    else:
        parts = []
        for key, name in (("r2", "R²"), ("rmse", "RMSE"), ("mae", "MAE")):
            val = _num(model.get(key))
            if val is not None:
                parts.append(f"{name} {val:.2f}")
        metrics = ", ".join(parts) if parts else "sin métricas"
    extra = ""
    if model.get("n_train") is not None:
        extra = f" Entrenado con {model.get('n_train')} obs."
        if model.get("n_holdout"):
            extra += f" Validado con {model.get('n_holdout')}."
    return f"Modelo: {label}{lib}. Confiabilidad (holdout temporal): {metrics}.{extra}"


# Encuadre de negocio por objetivo, para que el modelo entienda qué representa el gráfico.
_TARGET_CONTEXT = {
    "demand": (
        "CONTEXTO: es la demanda mensual (unidades) de un producto; sirve para planificar "
        "compras/producción y evitar quiebres de stock o sobrestock."
    ),
    "sales": (
        "CONTEXTO: son los ingresos/ventas de la empresa. El detal (clientes pequeños) tiende a "
        "estancarse y migrar a competidores más baratos, mientras lo institucional/proyectos "
        "sostiene el negocio; un shock cambiario (ene-2026) afectó la demanda."
    ),
    "profit": (
        "CONTEXTO: es la utilidad bruta; conviene vigilar el margen frente a costos y descuentos."
    ),
    "exchange-rate": (
        "CONTEXTO: es la tasa de cambio Bs/USD. Un alza encarece los productos en bolívares y suele "
        "frenar la demanda; es clave para fijar precios y proteger márgenes."
    ),
    "product-price": (
        "CONTEXTO: es el precio de venta (USD) de un producto. El precio en USD suele ser estable y "
        "su equivalente en bolívares sube con la tasa."
    ),
    "inventory": (
        "CONTEXTO: es el stock proyectado mes a mes restando la demanda pronosticada; sirve para "
        "decidir cuándo y cuánto reabastecer."
    ),
}


# ── Resúmenes de HECHOS para el prompt ────────────────────────────────────────


def _compact_facts(fc: dict, target: str) -> str:
    kind = fc.get("value_kind") or "int"
    lines: list[str] = []
    title = fc.get("title") or "Pronóstico"
    subj = (fc.get("subject") or {}).get("product_name")
    lines.append(f"GRÁFICO: {title}." + (f" Producto: {subj}." if subj else ""))
    if fc.get("unit"):
        lines.append(f"Unidad / tipo de valor: {fc['unit']}.")
    lines.append(_model_line(fc.get("model")))

    hist = fc.get("history") or []
    fcst = fc.get("forecast") or []
    if hist:
        tail = hist[-6:]
        lines.append("HISTÓRICO RECIENTE: " + "; ".join(f"{p.get('label')}: {_fmt(p.get('value'), kind)}" for p in tail) + ".")
    if fcst:
        def _pt(p):
            base = f"{p.get('label')}: {_fmt(p.get('value'), kind)}"
            lo, hi = p.get("lower"), p.get("upper")
            if lo is not None and hi is not None:
                base += f" (rango {_fmt(lo, kind)}–{_fmt(hi, kind)})"
            return base

        lines.append(f"PRONÓSTICO ({len(fcst)} meses): " + "; ".join(_pt(p) for p in fcst) + ".")

    t = _trend(fc)
    if t["last_hist"] is not None and t["last_fc"] is not None:
        lines.append(
            f"TENDENCIA: último valor real {_fmt(t['last_hist'], kind)} ({t['last_hist_label']}) → "
            f"fin del pronóstico {_fmt(t['last_fc'], kind)} ({t['last_fc_label']}), "
            f"variación {_signed_pct(t['change_pct'])} ({t['direction']})."
        )

    ctx = _TARGET_CONTEXT.get(target)
    if ctx:
        lines.append(ctx)

    if target == "inventory":
        m = fc.get("meta") or {}
        base = (
            f"INVENTARIO: stock actual {m.get('current_stock')}, demanda mensual promedio "
            f"{m.get('avg_monthly_demand')}, punto de reorden {m.get('reorder_point')}, meses de "
            f"cobertura {m.get('months_of_cover')}, agotamiento estimado {m.get('stockout_label') or 'sin riesgo'}."
        )
        if m.get("needs_reorder"):
            base += (
                f" NECESITA REABASTECER: cantidad sugerida {m.get('suggested_reorder_qty')} unidades "
                f"(lead time {m.get('lead_time_days')} días)."
            )
        else:
            base += " El stock cubre la demanda del horizonte."
        lines.append(base)

    return "\n".join(lines)


def _quote_facts(qc: dict) -> str:
    pipe = qc.get("pipeline") or {}
    monthly = qc.get("monthly_rate") or []
    lines = [
        "GRÁFICO: Conversión de presupuestos (modelo de clasificación).",
        _model_line(qc.get("model")),
        f"Conversión histórica de presupuestos cerrados: {_num(qc.get('historical_conversion_rate'))}%.",
        (
            f"PIPELINE ABIERTO: {pipe.get('open_count')} presupuestos por "
            f"{_fmt(pipe.get('total_value_usd'), 'usd')}; se espera cerrar "
            f"{_fmt(pipe.get('expected_revenue_usd'), 'usd')} (~{_num(pipe.get('expected_rate_pct'))}% del valor)."
        ),
    ]
    if monthly:
        tail = monthly[-6:]
        lines.append("TASA MENSUAL RECIENTE: " + "; ".join(f"{m.get('label')}: {_num(m.get('value'))}%" for m in tail) + ".")
    lines.append(
        "CONTEXTO: la conversión depende del tamaño del presupuesto, de si incluye instalación/despacho, "
        "del tipo de cliente y del shock cambiario al momento de emitirse."
    )
    return "\n".join(lines)


# ── Consejo determinista (fallback) ───────────────────────────────────────────

_DET_RECS = {
    "_default": [
        "Contrastar esta proyección con el conocimiento del mercado y del entorno del país.",
        "Revisar el gráfico al cargar datos nuevos: el modelo se reentrena automáticamente.",
    ],
    "demand": [
        "Ajustar las compras o la producción a la demanda proyectada para evitar quiebres y sobrestock.",
        "Vigilar los productos con demanda creciente para asegurar su disponibilidad.",
    ],
    "sales": [
        "Si la tendencia baja, reforzar el detal con ofertas y dar seguimiento al pipeline institucional.",
        "Atar las metas de venta a la proyección y revisar los desvíos cada mes.",
    ],
    "profit": [
        "Proteger el margen: revisar costos, descuentos y precios de lista frente a la tasa.",
        "Priorizar las categorías y los clientes más rentables.",
    ],
    "exchange-rate": [
        "Actualizar los precios de lista a la tasa proyectada para no perder margen.",
        "Evitar acumular inventario ocioso: con el dólar al alza, el capital parado pierde valor.",
    ],
    "product-price": [
        "Mantener el precio en USD y derivar el de bolívares con la tasa vigente.",
        "Comparar el precio con la competencia para no quedar por encima del mercado.",
    ],
    "inventory": [
        "Programar el reabastecimiento según el punto de reorden y el lead time del proveedor.",
    ],
}


def _deterministic(fc: dict, target: str) -> dict:
    kind = fc.get("value_kind") or "int"
    t = _trend(fc)
    word, score, metric = _reliability(fc.get("model"))
    headline = {
        "al alza": "Proyección al alza",
        "a la baja": "Proyección a la baja",
        "estable": "Proyección estable",
    }[t["direction"]]

    parts = []
    if t["last_hist"] is not None and t["last_fc"] is not None:
        parts.append(
            f"El modelo proyecta una tendencia {t['direction']}: de {_fmt(t['last_hist'], kind)} a "
            f"{_fmt(t['last_fc'], kind)} hacia {t['last_fc_label']} ({_signed_pct(t['change_pct'])})."
        )
    parts.append(
        f"La confiabilidad del modelo es {word}"
        + (f" ({metric} {score:.2f})." if score is not None else ".")
    )
    reading = " ".join(parts)

    recs = list(_DET_RECS.get(target, _DET_RECS["_default"]))
    if target == "inventory":
        m = fc.get("meta") or {}
        if m.get("needs_reorder"):
            recs = [
                f"Reabastecer ~{m.get('suggested_reorder_qty')} unidades antes del agotamiento estimado "
                f"({m.get('stockout_label') or 's/d'}).",
                *recs,
            ]
        else:
            recs = ["El stock cubre el horizonte; evitar sobrestock que inmoviliza caja.", *recs]
    if word == "baja":
        recs.append("La confiabilidad es baja: tomar la cifra como referencia y no como certeza.")

    return {
        "available": False,
        "generated_by": None,
        "headline": headline,
        "reading": reading,
        "recommendations": recs[:4],
        "reason": "LLM no disponible; lectura determinista.",
    }


def _deterministic_quote(qc: dict) -> dict:
    pipe = qc.get("pipeline") or {}
    rate = _num(pipe.get("expected_rate_pct"))
    hist = _num(qc.get("historical_conversion_rate"))
    reading = (
        f"Se espera cerrar {_fmt(pipe.get('expected_revenue_usd'), 'usd')} de los "
        f"{pipe.get('open_count')} presupuestos abiertos (~{rate:.0f}% del valor)."
        if rate is not None
        else "Lectura del pipeline de presupuestos abierto."
    )
    if hist is not None:
        reading += f" La conversión histórica ronda el {hist:.0f}%."
    return {
        "available": False,
        "generated_by": None,
        "headline": "Pipeline de presupuestos",
        "reading": reading,
        "recommendations": [
            "Dar seguimiento prioritario a los presupuestos con mayor probabilidad de cierre.",
            "Revisar los de baja probabilidad para ajustar precio, instalación o condiciones.",
            "Acelerar el cierre del pipeline institucional, que sostiene el crecimiento.",
        ],
        "reason": "LLM no disponible; lectura determinista.",
    }


# ── Prompts y saneamiento ─────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "Eres un analista de negocios senior del sistema predictivo de Inversiones Maescar C.A., una "
    "empresa venezolana de muebles de oficina. Interpretas para el dueño/gerente el resultado de UN "
    "gráfico de pronóstico, en español claro, directo y accionable, sin tecnicismos ni relleno. Te "
    "basas ÚNICAMENTE en los HECHOS que se te entregan: NO inventes cifras, nombres ni datos. "
    "Respondes EXCLUSIVAMENTE con un objeto JSON válido."
)


def _build_user_prompt(facts: str) -> str:
    return (
        "Con base EXCLUSIVAMENTE en los siguientes hechos de un pronóstico, redacta una breve lectura "
        "accionable de los resultados. Incluye la palabra json. Devuelve un objeto JSON con EXACTAMENTE "
        "estas claves:\n\n"
        "{\n"
        '  "headline": "título muy corto (4 a 8 palabras) que resuma la conclusión",\n'
        '  "reading": "2 a 3 frases que interpreten la dirección y la forma del pronóstico y mencionen, '
        'en lenguaje simple, qué tan confiable es el modelo",\n'
        '  "recommendations": ["2 a 4 acciones concretas y accionables derivadas del pronóstico"]\n'
        "}\n\n"
        "Reglas:\n"
        "- Usa cifras de los hechos cuando refuercen el mensaje, sin saturar de números.\n"
        "- Si la confiabilidad del modelo (R²/exactitud) es baja, dilo y recomienda prudencia.\n"
        "- Tono práctico y honesto, como hablarle al dueño de una PYME. Nada de marketing ni promesas.\n\n"
        "HECHOS DEL PRONÓSTICO:\n"
        f"{facts}\n"
    )


def _clean_str(v, limit: int) -> str:
    return v.strip()[:limit] if isinstance(v, str) else ""


def _sanitize(data: dict) -> dict:
    recs = []
    for r in data.get("recommendations") or []:
        s = _clean_str(r, 240)
        if s:
            recs.append(s)
    return {
        "available": True,
        "generated_by": system_settings.deepseek_model(),
        "headline": _clean_str(data.get("headline"), 90),
        "reading": _clean_str(data.get("reading"), 700),
        "recommendations": recs[:4],
    }


def generate(payload: dict, *, target: str) -> dict:
    """Redacta la lectura accionable del pronóstico con el LLM.

    Retorna ``{"available": True, "headline", "reading", "recommendations", "generated_by"}``
    cuando el LLM responde, o el consejo **determinista** (``available: False`` pero con
    contenido) cuando el LLM está deshabilitado o falla, para que la tarjeta nunca quede vacía.
    """
    is_quote = target == "quote"
    det = _deterministic_quote(payload) if is_quote else _deterministic(payload, target)

    if not is_enabled():
        return det

    try:
        client = _get_client()
    except ImportError as exc:
        logger.warning("Consejo LLM: no se pudo importar el SDK 'openai': %s", exc)
        return det
    except Exception as exc:  # configuración inválida
        logger.warning("Consejo LLM: no se pudo crear el cliente: %s", exc)
        return det

    facts = _quote_facts(payload) if is_quote else _compact_facts(payload, target)

    try:
        response = client.chat.completions.create(
            model=system_settings.deepseek_model(),
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(facts)},
            ],
            response_format={"type": "json_object"},
            temperature=0.4,
            max_tokens=_MAX_TOKENS,
            stream=False,
        )
        content = response.choices[0].message.content or "{}"
        result = _sanitize(json.loads(content))
        if not result["reading"] and not result["recommendations"]:
            return det
        return result
    except Exception as exc:
        logger.warning("Consejo LLM: falló la redacción vía DeepSeek: %s: %s", type(exc).__name__, exc)
        return det

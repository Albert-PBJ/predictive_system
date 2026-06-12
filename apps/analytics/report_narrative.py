"""Redacción del reporte ejecutivo por LLM (apps/analytics).

Toma el panel ejecutivo ya calculado (``stats.executive_dashboard``) y, de forma
opcional, los titulares predictivos (``overview``), arma un resumen compacto de
**hechos** y le pide al LLM que redacte —en español sencillo y accionable para el
dueño/CEO— la narrativa del reporte: situación actual, puntos clave, riesgos,
introducción de las estimaciones, acciones sugeridas y un cierre.

El diseño del PDF (gráficos, KPIs, tablas, tarjetas de estimación) NO se toca: solo
el TEXTO de análisis pasa a ser escrito por el modelo. Las cifras siguen saliendo de
los datos; al modelo se le entregan ya calculadas y se le prohíbe inventar.

Reutiliza las credenciales de DeepSeek (API compatible con OpenAI) que ya usa el
enriquecimiento de scrapers, pero con su propia habilitación: basta con que
``DEEPSEEK_API_KEY`` esté configurada y el paquete ``openai`` instalado. No depende del
interruptor ``USE_LLM_ENRICHMENT`` (ése es específico de los scrapers). Degrada de
forma segura: ante cualquier problema (sin clave, sin SDK, error de red o JSON
inválido) retorna ``{"available": False, ...}`` y el frontend cae a la síntesis
determinista existente, de modo que el reporte nunca se rompe.

Variables de entorno (en el ``.env`` del backend, las mismas del scraper):

    DEEPSEEK_API_KEY=sk-...                       # clave de https://platform.deepseek.com
    DEEPSEEK_MODEL=deepseek-chat                  # opcional (default deepseek-chat)
    DEEPSEEK_BASE_URL=https://api.deepseek.com    # opcional (cualquier endpoint OpenAI-compatible)
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

_REQUEST_TIMEOUT = 45  # segundos: redactar varios párrafos toma más que enriquecer una fila
_MAX_TOKENS = 2000

_VALID_SEVERITIES = {"high", "medium", "low"}


def is_enabled() -> bool:
    """True si hay clave configurada (el reporte LLM no usa el switch de scrapers)."""
    return bool(DEEPSEEK_API_KEY)


def _get_client():
    """Crea el cliente OpenAI apuntando a DeepSeek. Importa ``openai`` de forma diferida
    para que siga siendo una dependencia opcional (no requerida si no hay clave)."""
    from openai import OpenAI  # noqa: import diferido (dependencia opcional)

    return OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL, timeout=_REQUEST_TIMEOUT)


# ── Formateadores cortos para el bloque de hechos ─────────────────────────────


def _num(v) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _usd(v) -> str:
    n = _num(v)
    return f"${n:,.0f}" if n is not None else "—"


def _int(v) -> str:
    n = _num(v)
    return f"{int(round(n)):,}" if n is not None else "—"


def _pct(v) -> str:
    n = _num(v)
    return f"{n:.1f}%" if n is not None else "—"


def _signed_pct(v) -> str:
    n = _num(v)
    if n is None:
        return "—"
    return f"{'+' if n > 0 else ''}{n:.1f}%"


def _ves(v) -> str:
    n = _num(v)
    return f"Bs {n:,.2f}" if n is not None else "—"


def _names(rows: list, key: str = "name", limit: int = 4) -> str:
    out = [str(r.get(key)) for r in (rows or [])[:limit] if r.get(key)]
    return ", ".join(out) if out else "—"


# ── Resumen compacto de HECHOS para el prompt ─────────────────────────────────


def _compact_facts(d: dict, ov: dict | None, sensitive: bool) -> str:
    """Convierte el panel (y los titulares) en un bloque de hechos legible y compacto.

    Es deliberadamente denso y solo numérico/factual: el modelo debe redactar a partir
    de aquí, sin inventar. Solo se incluyen los bloques sensibles si llegaron (el panel
    ya los omite para personal no gerencial).
    """
    k = d.get("kpis", {}) or {}
    rng = d.get("range", {}) or {}
    lines: list[str] = []

    lines.append(f"Periodo analizado: {rng.get('from_label', '?')} a {rng.get('to_label', '?')} ({rng.get('months', '?')} meses).")
    lines.append(
        "Comparaciones (deltas) son contra el periodo inmediatamente anterior de igual duración."
    )

    # --- Indicadores clave ---
    lines.append("\nINDICADORES DEL PERIODO:")
    lines.append(f"- Ingresos: {_usd(k.get('revenue'))} (variación {_signed_pct(k.get('revenue_delta_pct'))}).")
    if sensitive and k.get("profit") is not None:
        lines.append(
            f"- Utilidad: {_usd(k.get('profit'))} (variación {_signed_pct(k.get('profit_delta_pct'))}); "
            f"margen {_pct(k.get('margin_pct'))} (cambio {_signed_pct(k.get('margin_delta_pts'))} puntos)."
        )
    if sensitive and k.get("discount") is not None:
        lines.append(f"- Descuentos otorgados: {_usd(k.get('discount'))}.")
    lines.append(f"- Ventas (transacciones): {_int(k.get('sales_count'))} (variación {_signed_pct(k.get('sales_count_delta_pct'))}).")
    lines.append(f"- Ticket promedio: {_usd(k.get('avg_ticket'))} (variación {_signed_pct(k.get('avg_ticket_delta_pct'))}).")
    lines.append(f"- Unidades vendidas: {_int(k.get('units_sold'))} (variación {_signed_pct(k.get('units_delta_pct'))}).")
    lines.append(f"- Clientes activos: {_int(k.get('active_customers'))} (variación {_signed_pct(k.get('active_customers_delta_pct'))}); nuevos: {_int(k.get('new_customers'))}.")
    if k.get("conversion_rate") is not None:
        conv = _num(k.get("conversion_rate"))
        conv = conv * 100 if conv is not None and conv <= 1 else conv
        lines.append(f"- Conversión de presupuestos: {_pct(conv)}; presupuestos emitidos: {_int(k.get('quotes_issued'))}.")
    if k.get("retention_pct") is not None:
        lines.append(f"- Retención de clientes: {_pct(k.get('retention_pct'))}.")

    # --- Salud global (IVC, sensible) ---
    hi = d.get("health_index")
    if sensitive and hi:
        comps = ", ".join(f"{c.get('label')} {int(round(_num(c.get('score')) or 0))}/100" for c in (hi.get("components") or [])[:8])
        lines.append(
            f"\nSALUD GLOBAL (Índice de Ventaja Competitiva): {int(round(_num(hi.get('score')) or 0))}/100, estado '{hi.get('status')}'. "
            f"Componentes: {comps or '—'}."
        )

    # --- Mezcla detal vs. institucional ---
    ts = d.get("type_split") or []
    if ts:
        mix = "; ".join(f"{t.get('label')}: {_usd(t.get('revenue'))} ({_pct(t.get('share_pct'))} de ingresos)" for t in ts)
        lines.append(f"\nMEZCLA DE CLIENTES: {mix}.")
    mbt = d.get("monthly_by_type") or []
    if len(mbt) >= 2:
        first_r, last_r = _num(mbt[0].get("retail")), _num(mbt[-1].get("retail"))
        if first_r and first_r > 0 and last_r is not None:
            change = (last_r - first_r) / first_r * 100
            lines.append(
                f"Tendencia del detal dentro del periodo: de {_usd(first_r)} a {_usd(last_r)} ({_signed_pct(change)}). "
                "Contexto del negocio: el detal (clientes pequeños) tiende a estancarse y migrar a competidores más baratos, "
                "mientras lo institucional/proyectos sostiene la empresa."
            )

    # --- Categorías ---
    cats = d.get("revenue_by_category") or []
    if cats:
        top_cats = "; ".join(f"{c.get('category')}: {_usd(c.get('revenue'))}" for c in cats[:5])
        lines.append(f"\nINGRESOS POR CATEGORÍA (top): {top_cats}.")

    # --- Inventario y capital inmovilizado ---
    inv = d.get("inventory_health", {}) or {}
    dead_val = sum(_num(p.get("retail_value")) or 0 for p in (d.get("no_demand") or []))
    lines.append(
        f"\nINVENTARIO (estado actual): {_int(inv.get('ok_stock'))} con stock, {_int(inv.get('low_stock'))} con stock bajo, "
        f"{_int(inv.get('out_of_stock'))} sin stock. Valor del inventario a precio de venta: {_usd(inv.get('inventory_retail_usd'))} "
        f"({_int(inv.get('units_in_stock'))} unidades)."
    )
    lines.append(
        f"CAPITAL INMOVILIZADO: {_int(d.get('no_demand_count'))} producto(s) activos SIN ventas en el periodo, "
        f"con {_usd(dead_val)} detenidos en stock. Ejemplos: {_names(d.get('no_demand'))}."
    )

    # --- Clientes en riesgo de fuga ---
    at_risk = d.get("at_risk") or []
    if at_risk:
        risk_val = sum(_num(c.get("revenue")) or 0 for c in at_risk)
        lines.append(
            f"\nCLIENTES EN RIESGO DE FUGA: {_int(len(at_risk))} sin comprar en más de 6 meses, "
            f"que representan {_usd(risk_val)} en compras históricas. Ejemplos: {_names(at_risk)}."
        )

    # --- Tipo de cambio ---
    er = d.get("exchange_rate")
    if er:
        lines.append(
            f"\nTIPO DE CAMBIO en el periodo: BCV de {_ves(er.get('start_bcv'))} a {_ves(er.get('end_bcv'))} "
            f"({_signed_pct(er.get('bcv_change_pct'))}); paralelo de {_ves(er.get('start_parallel'))} a "
            f"{_ves(er.get('end_parallel'))} ({_signed_pct(er.get('parallel_change_pct'))}). "
            "Un dólar paralelo al alza encarece los productos y suele frenar la demanda."
        )

    # --- Competitividad de precio (sensible) ---
    comp = d.get("competitive")
    if sensitive and comp:
        above = [p for p in (comp.get("positioning") or []) if p.get("position") == "above"]
        score = comp.get("price_score")
        if score is not None:
            lines.append(
                f"\nCOMPETITIVIDAD DE PRECIO: índice {int(round(_num(score) or 0))}/100 "
                "(0 = mucho más caro que el mercado, 100 = mucho más barato). "
                + (f"Categorías por encima del mercado scrapeado: {_names(above, key='category')}." if above else "")
            )

    # --- Alertas reales del sistema ---
    alerts = [a for a in (d.get("alerts") or []) if a.get("severity") in ("CRIT", "HIGH")]
    if alerts:
        al = "; ".join(f"[{a.get('severity_label') or a.get('severity')}] {a.get('title')}: {a.get('message')}" for a in alerts[:4])
        lines.append(f"\nALERTAS ACTIVAS DEL SISTEMA: {al}.")

    # --- Rankings ---
    tp = d.get("top_products") or []
    if tp:
        lines.append("\nPRODUCTOS MÁS VENDIDOS: " + "; ".join(f"{p.get('name')} ({_int(p.get('units'))} uds, {_usd(p.get('revenue'))})" for p in tp[:5]) + ".")
    tc = d.get("top_customers") or []
    if tc:
        lines.append("MEJORES CLIENTES: " + "; ".join(f"{c.get('name')} ({_usd(c.get('revenue'))})" for c in tc[:5]) + ".")

    # --- Estimaciones de los modelos (overview, solo gerencia) ---
    if ov:
        h = ov.get("headlines", {}) or {}
        lines.append("\nESTIMACIONES DE LOS MODELOS PREDICTIVOS (próximos meses):")
        nr = h.get("next_revenue")
        if nr:
            rng_txt = ""
            if nr.get("lower") is not None and nr.get("upper") is not None:
                rng_txt = f" (rango {_usd(nr.get('lower'))}–{_usd(nr.get('upper'))})"
            r2 = (h.get("revenue_model") or {}).get("r2")
            r2_txt = f", confianza R² {_num(r2):.2f}" if _num(r2) is not None else ""
            lines.append(f"- Ingresos del próximo mes (proyección): {_usd(nr.get('value'))}{rng_txt}{r2_txt}.")
        if h.get("next_bcv") or h.get("next_parallel"):
            lines.append(
                f"- Tipo de cambio próximo mes (proyección): BCV {_ves((h.get('next_bcv') or {}).get('value'))}, "
                f"paralelo {_ves((h.get('next_parallel') or {}).get('value'))}."
            )
        pipe = h.get("pipeline")
        if pipe:
            lines.append(
                f"- Pipeline de presupuestos: {_int(pipe.get('open_count'))} abiertos por {_usd(pipe.get('total_value_usd'))}; "
                f"se espera cerrar {_usd(pipe.get('expected_revenue_usd'))} (~{_pct(pipe.get('expected_rate_pct'))})."
            )
        restock = ov.get("restock_alerts") or []
        if restock:
            soon = sorted(restock, key=lambda r: r.get("months_of_cover") if r.get("months_of_cover") is not None else 99)[0]
            lines.append(
                f"- Reposición de inventario: {_int(len(restock))} producto(s) por reordenar; el más urgente: "
                f"{soon.get('product_name') or '—'}{(' (' + soon.get('stockout_label') + ')') if soon.get('stockout_label') else ''}."
            )

    return "\n".join(lines)


# ── Prompts ───────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "Eres un analista de negocios senior que redacta el reporte ejecutivo de Inversiones "
    "Maescar C.A., una empresa venezolana de muebles de oficina, para su dueño/CEO. "
    "Escribes en español claro, directo y accionable, sin tecnicismos ni relleno y sin "
    "anglicismos innecesarios. Te basas ÚNICAMENTE en los HECHOS que se te entregan: NO "
    "inventes cifras, nombres ni datos, y no menciones información que no esté en los hechos. "
    "Tu objetivo es que el dueño entienda de un vistazo dónde está el negocio, qué lo "
    "amenaza y qué hacer. Respondes EXCLUSIVAMENTE con un objeto JSON válido."
)


def _build_user_prompt(facts: str, sensitive: bool, has_estimations: bool) -> str:
    sens_note = (
        "Tienes acceso a datos sensibles (utilidad, margen, índice de salud, competitividad de precio): úsalos."
        if sensitive
        else "NO tienes datos de utilidad ni margen; redacta sin mencionar rentabilidad ni márgenes."
    )
    est_field = (
        '  "estimations_intro": "1-2 frases que introduzcan las estimaciones de los modelos para los próximos meses, '
        'aclarando que son apoyo a la decisión y no certezas",\n'
        if has_estimations
        else '  "estimations_intro": "",\n'
    )
    return (
        "Con base EXCLUSIVAMENTE en los siguientes hechos del negocio, redacta la narrativa del "
        "reporte ejecutivo. Incluye la palabra json. Devuelve un objeto JSON con EXACTAMENTE estas claves:\n\n"
        "{\n"
        '  "situation": "2 a 4 frases que resuman dónde está el negocio hoy, en lenguaje simple, citando 2-3 cifras clave",\n'
        '  "highlights": ["3 a 5 puntos clave del periodo, cada uno una frase breve y concreta"],\n'
        '  "risks": [{"severity": "high|medium|low", "title": "título corto", "text": "1-2 frases que expliquen el riesgo y por qué importa, con la cifra relevante"}],\n'
        + est_field +
        '  "actions": [{"title": "acción concreta y corta", "text": "1-2 frases con el qué y el porqué, accionable"}],\n'
        '  "closing": "2 a 3 frases de cierre que sinteticen la situación y la prioridad principal"\n'
        "}\n\n"
        "Reglas:\n"
        f"- {sens_note}\n"
        "- 'risks': de 3 a 6 elementos, ordenados de mayor a menor severidad. Asigna la severidad con criterio "
        "(quiebres de stock, fuga de clientes valiosos o caídas fuertes de ingresos suelen ser 'high'). Si no hay "
        "riesgos relevantes, devuelve una lista con un único elemento 'low' indicándolo.\n"
        "- 'actions': de 3 a 6 acciones priorizadas que ataquen los riesgos y aprovechen las oportunidades de los hechos.\n"
        "- Usa cifras de los hechos cuando refuercen el mensaje, pero no llenes de números: prioriza claridad.\n"
        "- Tono: como hablarle al dueño de una PYME, práctico y honesto. Nada de promesas ni lenguaje de marketing.\n\n"
        "HECHOS DEL NEGOCIO:\n"
        f"{facts}\n"
    )


# ── Saneamiento de la respuesta ───────────────────────────────────────────────


def _clean_str(v, limit: int) -> str:
    return v.strip()[:limit] if isinstance(v, str) else ""


def _sanitize(data: dict, *, sensitive: bool, has_estimations: bool) -> dict:
    situation = _clean_str(data.get("situation"), 800)

    highlights = []
    for h in data.get("highlights") or []:
        s = _clean_str(h, 240)
        if s:
            highlights.append(s)
    highlights = highlights[:5]

    risks = []
    for r in data.get("risks") or []:
        if not isinstance(r, dict):
            continue
        title = _clean_str(r.get("title"), 90)
        text = _clean_str(r.get("text"), 360)
        if not title and not text:
            continue
        sev = r.get("severity")
        sev = sev if sev in _VALID_SEVERITIES else "medium"
        risks.append({"severity": sev, "title": title or "Riesgo", "text": text})
    # Orden por severidad (high → low), respetando el orden del modelo dentro de cada nivel.
    order = {"high": 0, "medium": 1, "low": 2}
    risks.sort(key=lambda x: order[x["severity"]])
    risks = risks[:6]

    actions = []
    for a in data.get("actions") or []:
        if not isinstance(a, dict):
            continue
        title = _clean_str(a.get("title"), 90)
        text = _clean_str(a.get("text"), 360)
        if not title and not text:
            continue
        actions.append({"title": title or "Acción", "text": text})
    actions = actions[:6]

    estimations_intro = _clean_str(data.get("estimations_intro"), 500) if has_estimations else ""
    closing = _clean_str(data.get("closing"), 700)

    return {
        "available": True,
        "generated_by": DEEPSEEK_MODEL,
        "situation": situation,
        "highlights": highlights,
        "risks": risks,
        "estimations_intro": estimations_intro,
        "actions": actions,
        "closing": closing,
    }


def generate(dashboard: dict, overview: dict | None, *, sensitive: bool) -> dict:
    """Redacta la narrativa del reporte ejecutivo con el LLM a partir del panel.

    Retorna ``{"available": True, "situation", "highlights", "risks",
    "estimations_intro", "actions", "closing", "generated_by"}`` en caso de éxito, o
    ``{"available": False, "reason": str}`` ante cualquier problema (deshabilitado, sin
    SDK, error de red o JSON inesperado), para que el frontend caiga a la síntesis
    determinista sin romperse.
    """
    if not is_enabled():
        return {"available": False, "reason": "LLM no configurado (falta DEEPSEEK_API_KEY)."}

    has_estimations = bool(overview)

    try:
        client = _get_client()
    except ImportError as exc:
        logger.warning("Reporte LLM: no se pudo importar el SDK 'openai': %s", exc)
        return {"available": False, "reason": "Paquete 'openai' no instalado."}
    except Exception as exc:  # configuración inválida
        logger.warning("Reporte LLM: no se pudo crear el cliente: %s", exc)
        return {"available": False, "reason": str(exc)}

    facts = _compact_facts(dashboard, overview, sensitive)

    try:
        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(facts, sensitive, has_estimations)},
            ],
            response_format={"type": "json_object"},
            temperature=0.4,
            max_tokens=_MAX_TOKENS,
            stream=False,
        )
        content = response.choices[0].message.content or "{}"
        result = _sanitize(json.loads(content), sensitive=sensitive, has_estimations=has_estimations)
        # Si el modelo no devolvió nada útil, mejor caer al determinista.
        if not result["situation"] and not result["risks"] and not result["actions"]:
            return {"available": False, "reason": "El modelo no devolvió contenido utilizable."}
        logger.info("Reporte LLM generado (modelo=%s, riesgos=%d, acciones=%d).", DEEPSEEK_MODEL, len(result["risks"]), len(result["actions"]))
        return result
    except Exception as exc:
        logger.warning("Reporte LLM: falló la redacción vía DeepSeek: %s: %s", type(exc).__name__, exc)
        return {"available": False, "reason": str(exc)}

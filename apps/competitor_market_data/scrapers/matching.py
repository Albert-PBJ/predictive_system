"""Match (mejor esfuerzo) de un anuncio de competidor contra el catálogo propio
(`core.Product`).

Permite comparar like-with-like en el benchmarking: a cada `CompetitorMarketData`
se le asocia el `Product` propio más parecido (si supera un umbral), guardando el
puntaje de similitud. El resultado es revisable/corregible a mano desde el admin.

Dos vías, en orden:

  1. **Determinista (siempre):** similitud por tokens del nombre, robusta a
     mayúsculas/acentos (se normaliza), a palabras de relleno (descriptores
     genéricos que se descartan), a un nombre que sea super-conjunto del otro
     ("Silla Trendy" ↔ "Silla de oficina Trendy") y a variantes de un mismo token
     (plurales/erratas, vía similitud de caracteres). Sin LLM.
  2. **LLM (opcional, mismo interruptor que los competidores):** para las filas que
     el determinista NO logró asociar, un único llamado por lote propone el
     producto del catálogo equivalente. Off por defecto; degrada sin romper nada.

El match se calcula al scrapear, pero **no queda congelado**: `manage.py
rematch_products` lo recalcula sobre las filas ya guardadas contra el catálogo
actual (p. ej. tras crear o renombrar un producto).
"""

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Optional

# Umbral mínimo de similitud determinista para aceptar un match.
MATCH_THRESHOLD = 0.6

# Dos tokens se consideran "el mismo" si su similitud de caracteres llega a esto
# (cubre plurales y erratas: "silla"≈"sillas", "metalico"≈"metálico" ya normalizado).
_TOKEN_SIM = 0.84

# Confianza mínima para aceptar un match propuesto por el LLM.
_LLM_MATCH_MIN_CONFIDENCE = 0.55

# Tope de anuncios por llamada LLM (se trocea en lotes de este tamaño).
_MAX_MATCH_BATCH = 40

# Palabras de relleno que NO identifican el producto (descriptores genéricos de
# categoría y muletillas). En minúscula y sin acentos. Se descartan para que el
# match dependa de las palabras distintivas ("trendy", "stanford", "retro").
_STOPWORDS = {
    "de", "la", "el", "los", "las", "para", "con", "y", "en", "un", "una", "del", "al",
    "silla", "sillas", "mesa", "mesas", "escritorio", "escritorios",
    "oficina", "color", "colores", "modelo", "tipo", "nuevo", "nueva", "original",
}


def _normalize(text: str) -> str:
    """Minúsculas, sin acentos y sin signos: base para tokenizar."""
    text = unicodedata.normalize("NFD", text or "")
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> set:
    """Tokens significativos (sin stopwords, sin palabras de 1 letra)."""
    return {t for t in _normalize(text).split() if len(t) > 1 and t not in _STOPWORDS}


def _soft_inter(a: set, b: set) -> int:
    """Cuántos tokens de `a` tienen un equivalente (igual o muy parecido) en `b`."""
    count = 0
    for ta in a:
        if ta in b or any(SequenceMatcher(None, ta, tb).ratio() >= _TOKEN_SIM for tb in b):
            count += 1
    return count


def _pair_score(q: set, c: set) -> float:
    """Similitud [0–1] entre dos conjuntos de tokens.

    Mezcla el coeficiente de solapamiento (contención: tolera que un nombre sea
    super-conjunto del otro) con Jaccard (penaliza diferencias de tamaño), sobre
    una intersección "suave" que cuenta plurales/erratas como coincidencia.
    """
    if not q or not c:
        return 0.0
    inter = min(_soft_inter(q, c), _soft_inter(c, q))  # simétrico
    if inter == 0:
        return 0.0
    overlap = inter / min(len(q), len(c))
    jaccard = inter / (len(q) + len(c) - inter)
    return 0.5 * overlap + 0.5 * jaccard


def build_product_index() -> list:
    """Índice (Product, tokens, categoría_normalizada) de los productos activos.

    Se arma una sola vez por lote para no consultar el catálogo por cada anuncio.
    Import diferido para no acoplar el arranque del módulo a la app `core`.
    """
    from apps.core.models import Product

    index = []
    for p in Product.objects.filter(is_active=True).select_related("category"):
        text = f"{p.name} {p.full_name or ''}"
        cat = _normalize(p.category.name) if p.category_id and p.category else ""
        index.append((p, _tokens(text), cat))
    return index


def match_product(
    product_name: Optional[str],
    category: Optional[str],
    index: list,
) -> tuple[Optional[object], Optional[float]]:
    """Mejor producto propio para el anuncio dado. Retorna ``(product, score)``.

    ``score`` es la similitud del mejor candidato; si no alcanza ``MATCH_THRESHOLD``
    retorna ``(None, score)`` (se conserva el puntaje para diagnóstico). Si no hay
    nombre o catálogo, retorna ``(None, None)``.
    """
    name_tokens = _tokens(product_name or "")
    if not name_tokens or not index:
        return None, None

    cat_norm = _normalize(category or "")
    best_product = None
    best_score = 0.0

    for product, p_tokens, p_cat in index:
        score = _pair_score(name_tokens, p_tokens)
        if score <= 0:
            continue
        # Bono leve si la categoría coincide (refuerza un match ya plausible).
        if cat_norm and p_cat and cat_norm == p_cat:
            score = min(1.0, score + 0.1)
        if score > best_score:
            best_score = score
            best_product = product

    if best_product is not None and best_score >= MATCH_THRESHOLD:
        return best_product, round(best_score, 3)
    return None, (round(best_score, 3) if best_product is not None else None)


def apply_llm_product_matches(instances: list, index: list) -> int:
    """Para las instancias SIN match determinista, intenta asociarlas vía LLM.

    Opcional y por lotes (un llamado por cada ``_MAX_MATCH_BATCH`` filas). Usa el
    mismo interruptor que el resto del enriquecimiento (`deepseek.is_enabled()`):
    si está apagado o falla, no toca nada. Fija `product` + `product_match_score`
    (la confianza del LLM) y retorna cuántas filas asoció.
    """
    from apps.competitor_market_data.enrichment import deepseek

    if not deepseek.is_enabled() or not index:
        return 0

    unmatched = [
        inst for inst in instances
        if getattr(inst, "product", None) is None and (inst.product_name or "").strip()
    ]
    if not unmatched:
        return 0

    catalog = [{"id": p.id, "name": p.name} for p, _, _ in index]
    by_id = {p.id: p for p, _, _ in index}
    matched = 0

    for start in range(0, len(unmatched), _MAX_MATCH_BATCH):
        chunk = unmatched[start:start + _MAX_MATCH_BATCH]
        scraped = [
            {"index": i, "name": inst.product_name, "category": inst.category}
            for i, inst in enumerate(chunk)
        ]
        result = deepseek.match_products(scraped, catalog)
        for i, inst in enumerate(chunk):
            m = result.get(i)
            if not m:
                continue
            product = by_id.get(m["product_id"])
            if product is not None and m["confidence"] >= _LLM_MATCH_MIN_CONFIDENCE:
                inst.product = product
                inst.product_match_score = round(m["confidence"], 3)
                matched += 1

    return matched

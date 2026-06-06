"""Resolución de competidores con dedupe difuso (determinista, sin LLM).

`Competitor.name` es único y los scrapers crean competidores con `get_or_create`
por nombre exacto, así que "Muebles AB C.A." y "Muebles AB" terminaban como dos
competidores distintos. `get_or_create_competitor` añade una capa de coincidencia
difusa: normaliza el nombre (minúsculas, sin acentos, sin sufijos societarios) y,
antes de crear uno nuevo, busca un competidor existente cuyo nombre normalizado
sea suficientemente parecido. Reemplaza a `Competitor.objects.get_or_create(name=…)`
en las rutas deterministas de los cuatro scrapers.

La fusión manual de duplicados que igual se cuelen vive en el admin
(`benchmarking/admin.merge_competitors`).
"""

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Optional

# Similitud mínima (sobre el nombre normalizado) para considerar que dos nombres
# son el mismo competidor. Alto a propósito: preferimos crear de más que fusionar
# competidores que en realidad son distintos.
SIMILARITY_THRESHOLD = 0.88

# Sufijos societarios/legales que no aportan a la identidad de la marca y que
# causan falsos distintos ("AB" vs "AB C.A."). En minúscula, sin puntos.
_LEGAL_SUFFIXES = {
    "ca", "sa", "srl", "sca", "scs", "rl", "ee", "fp",
    "compania", "compañia", "company", "co", "inc", "llc", "ltd", "corp",
}


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text or "") if unicodedata.category(c) != "Mn"
    )


def normalize_competitor_name(name: str) -> str:
    """Forma canónica para comparar nombres de competidor.

    Minúsculas, sin acentos, sin puntuación y sin sufijos societarios al final
    (C.A., S.A., SRL…). Ej.: 'Muebles AB, C.A.' → 'muebles ab'.
    """
    text = _strip_accents((name or "").lower())
    # Quita los puntos SIN dejar espacio para que "c.a." → "ca", "s.r.l." → "srl"
    # (y así reconocerlos como sufijos societarios); el resto de la puntuación
    # sí se vuelve espacio.
    text = text.replace(".", "")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    tokens = [t for t in text.split() if t]
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def find_similar_competitor(name: str, *, threshold: float = SIMILARITY_THRESHOLD):
    """Competidor existente cuyo nombre normalizado se parece a ``name`` (o ``None``).

    Compara contra todos los competidores; barato a la escala de este sistema. Un
    solapamiento exacto del nombre normalizado gana siempre; si no, el de mayor
    ratio de `SequenceMatcher` por encima del umbral.
    """
    from apps.benchmarking.models import Competitor

    target = normalize_competitor_name(name)
    if not target:
        return None

    best = None
    best_ratio = 0.0
    for comp in Competitor.objects.all():
        cand = normalize_competitor_name(comp.name)
        if not cand:
            continue
        if cand == target:
            return comp  # match exacto tras normalizar
        ratio = SequenceMatcher(None, target, cand).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = comp
    return best if best is not None and best_ratio >= threshold else None


def get_or_create_competitor(
    name: str,
    *,
    defaults: Optional[dict] = None,
    threshold: float = SIMILARITY_THRESHOLD,
) -> tuple[object, bool]:
    """Como `Competitor.objects.get_or_create(name=…)`, pero con dedupe difuso.

    Retorna ``(competitor, created)``. Si ya existe un competidor con nombre exacto
    o suficientemente parecido (normalizado), lo reutiliza; si no, crea uno nuevo
    con ``defaults``. No pisa datos del existente (eso lo hace el backfill aparte).
    """
    from apps.benchmarking.models import Competitor

    name = (name or "").strip()[:150] or "Desconocido"
    existing = find_similar_competitor(name, threshold=threshold)
    if existing is not None:
        return existing, False

    # `get_or_create` final protege contra carreras y contra el unique de `name`.
    return Competitor.objects.get_or_create(name=name, defaults=defaults or {})

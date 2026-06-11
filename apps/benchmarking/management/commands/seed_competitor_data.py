"""Genera datos de mercado de competidores **simulados** para los 10 principales.

El sistema ya trae unas pocas filas reales scrapeadas (Biloffice, Suhsillas,
Maxximuebles, …). Este comando toma esos mismos competidores —los **10 principales**
del mercado venezolano de mobiliario de oficina— y genera para cada uno un catálogo
simulado coherente, de forma que el módulo predictivo de **competencia**
(`/predicciones/competencia`) tenga suficiente señal para el análisis de
posicionamiento, comparación like-with-like y tendencia de precios.

Diseño de los datos (alineado con la historia de negocio de ``seed_company_data``):

  * Los precios de Maescar están **por encima del mercado** (su costo sale de un
    margen objetivo ~33%). Por eso la mayoría de los competidores se simulan **más
    baratos** (tier ``budget``/``mid``) — es justamente lo que empuja al cliente de
    detal hacia la competencia. Un competidor premium importado (HermanMiller) queda
    **por encima** de Maescar.
  * Cada fila se deriva del catálogo propio: para los productos que el competidor
    "tiene", el precio = precio propio × multiplicador del tier × deriva mensual ×
    ruido, recortado a la banda de precio de su categoría (``validation.PRICE_BANDS``).
    Se enlaza al ``Product`` propio (match like-with-like). Las categorías que el
    catálogo propio casi no cubre (Sofás/Recepción, Estantes, Gabinetes) se generan
    "genéricas" (sin match) a partir de un precio base por categoría.
  * Las observaciones se reparten en los **últimos 6 meses** (ene→jun 2026) con una
    leve deriva de precio en USD, para que la **tendencia** temporal tenga pendiente.

Cada fila queda con toda la capa de confianza que pondría el scraper real
(``price_usd``, ``listing_key``, match de producto, ``enriched_by``, ``ScrapeRun``),
así que la analítica la consume igual que a un dato scrapeado de verdad.

**Preserva los datos reales** ya scrapeados: solo administra las filas que él mismo
siembra (marcadas vía su ``ScrapeRun`` con ``notes=seed_competitor_data``). Por
defecto (``--fresh``) borra su propia siembra previa y regenera (idempotente y
determinista). ``--no-fresh`` añade sin borrar. Los competidores no se borran nunca:
se crean si faltan y se les rellenan los campos vacíos (ubicación/web/IG).

Uso:
    python manage.py seed_competitor_data
    python manage.py seed_competitor_data --scale 1.5     # más filas por competidor
    python manage.py seed_competitor_data --no-fresh      # añade sin borrar
    python manage.py seed_competitor_data --seed 7
"""

import datetime as _dt
import random
from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.benchmarking.models import Competitor, CompetitorMarketData, ScrapeRun
from apps.competitor_market_data.scrapers import classify_category
from apps.competitor_market_data.scrapers.persistence import compute_listing_key
from apps.competitor_market_data.scrapers.validation import band_for_category
from apps.core.models import Product

# Marca que identifica la siembra de este comando (para poder borrarla en --fresh
# sin tocar los datos reales scrapeados).
SEED_TAG = "seed_competitor_data"

# Multiplicadores aplicados al precio de referencia (precio propio o base de
# categoría) según el posicionamiento del competidor. < 1 = más barato que Maescar.
TIERS: dict[str, tuple[float, float]] = {
    "budget":  (0.62, 0.80),
    "mid":     (0.80, 0.96),
    "premium": (1.06, 1.45),
}

# Los 10 competidores principales del mercado venezolano de mobiliario de oficina.
# Son los mismos que ya aparecen en los datos scrapeados; aquí se formalizan con
# metadatos y se les genera un catálogo simulado. Campos:
#   name, source, tier, state, municipality, website, instagram, focus, breadth
# `source` = plataforma donde principalmente se les observa.
# `focus`  = lista de categorías (None = surtido amplio).
# `breadth`= nº base de filas a generar (se escala con --scale).
COMPETITORS: list[dict] = [
    {"name": "Biloffice", "source": "WEB", "tier": "mid", "state": "Carabobo",
     "municipality": "Valencia", "website": "https://biloffice.com.ve", "instagram": "",
     "focus": None, "breadth": 16},
    {"name": "Maxximuebles", "source": "IG", "tier": "budget", "state": "Carabobo",
     "municipality": "Guacara", "website": "", "instagram": "https://www.instagram.com/maxximuebles_ve/",
     "focus": None, "breadth": 26},
    {"name": "Suhsillas", "source": "IG", "tier": "budget", "state": "Carabobo",
     "municipality": "Valencia", "website": "", "instagram": "https://www.instagram.com/suhsillas/",
     "focus": ["Sillas"], "breadth": 30},
    {"name": "Mobiliario de Oficina", "source": "IG", "tier": "mid", "state": "Distrito Capital",
     "municipality": "Libertador", "website": "", "instagram": "https://www.instagram.com/mobiliariodeoficina/",
     "focus": None, "breadth": 24},
    {"name": "Mayor del Mueble", "source": "FB", "tier": "budget", "state": "Distrito Capital",
     "municipality": "Libertador", "website": "", "instagram": "",
     "focus": None, "breadth": 28},
    {"name": "Portumania", "source": "WEB", "tier": "budget", "state": "Bolívar",
     "municipality": "Caroní", "website": "https://www.portumania.com", "instagram": "",
     "focus": None, "breadth": 22},
    {"name": "uoffurniture.com", "source": "WEB", "tier": "mid", "state": "Distrito Capital",
     "municipality": "Chacao", "website": "https://www.uoffurniture.com", "instagram": "",
     "focus": None, "breadth": 20},
    {"name": "HermanMiller", "source": "WEB", "tier": "premium", "state": "Distrito Capital",
     "municipality": "Chacao", "website": "https://www.hermanmiller.com", "instagram": "",
     "focus": ["Sillas", "Escritorios"], "breadth": 12},
    {"name": "Mercado Libre", "source": "ML", "tier": "budget", "state": "Distrito Capital",
     "municipality": "Libertador", "website": "https://www.mercadolibre.com.ve", "instagram": "",
     "focus": None, "breadth": 24},
    {"name": "A&B", "source": "FB", "tier": "mid", "state": "Aragua",
     "municipality": "Girardot", "website": "", "instagram": "",
     "focus": None, "breadth": 18},
]

# Reparto de categorías para un competidor de surtido amplio (refleja que el grueso
# del mercado son sillas). Las que el catálogo propio no cubre se generan genéricas.
BROAD_WEIGHTS: dict[str, float] = {
    "Sillas": 0.42,
    "Escritorios": 0.16,
    "Mesas": 0.08,
    "Archivadores": 0.08,
    "Estantes y Libreros": 0.07,
    "Sofás y Recepción": 0.10,
    "Gabinetes y Armarios": 0.09,
}

# Precio base por categoría (nivel Maescar, "por encima del mercado") para las filas
# genéricas sin producto propio equivalente. Dentro de la banda de cada categoría.
CATEGORY_BASELINE: dict[str, int] = {
    "Sillas": 120,
    "Escritorios": 220,
    "Mesas": 280,
    "Archivadores": 200,
    "Estantes y Libreros": 160,
    "Sofás y Recepción": 480,
    "Gabinetes y Armarios": 320,
}

# Plantillas de nombre de producto por categoría (para las filas genéricas y para
# variar el nombre de las matcheadas) — parecen modelos comerciales reales.
NAME_TEMPLATES: dict[str, list[str]] = {
    "Sillas": ["Silla Ejecutiva {m}", "Silla Operativa {m}", "Silla de Visita {m}",
               "Silla Presidencial {m}", "Silla Gerencial {m}", "Silla Secretarial {m}"],
    "Escritorios": ["Escritorio Ejecutivo {m}", "Escritorio en L {m}",
                    "Escritorio Secretarial {m}", "Escritorio Gerencial {m}"],
    "Mesas": ["Mesa de Reunión {m}", "Mesa de Juntas {m}", "Mesa Auxiliar {m}"],
    "Archivadores": ["Archivador Metálico {m}", "Archivador Móvil {m}", "Archivador 4 Gavetas {m}"],
    "Estantes y Libreros": ["Estante {m}", "Librero {m}", "Repisa Modular {m}"],
    "Sofás y Recepción": ["Sofá de Recepción {m}", "Poltrona {m}", "Juego de Recepción {m}"],
    "Gabinetes y Armarios": ["Gabinete {m}", "Credenza {m}", "Armario Modular {m}"],
}

MODELS = [
    "Milano", "Berlín", "Oslo", "Lisboa", "Praga", "Boston", "Dallas", "Houston",
    "Sevilla", "Turín", "Verona", "Nápoles", "Atlas", "Onyx", "Titán", "Nova",
    "Apolo", "Orión", "Delta", "Sigma", "Aspen", "Denver", "Phoenix", "Génova",
    "Bari", "Toledo", "Múnich", "Viena", "Zúrich", "Bristol", "Cádiz", "Mérida",
]
COLORS = ["Negro", "Gris", "Azul", "Marrón", "Blanco", "Beige", "Vinotinto"]
PROMOS = ["", "", "", "", "Descuento 10%", "Envío gratis", "Oferta del mes",
          "2x1 en visita", "Precio especial mayorista"]
LEAD_TIMES = [0, 0, 3, 5, 7, 15]

# Ventana de monitoreo: últimos 6 meses (ene→jun 2026). Más peso a los meses
# recientes (la mayoría de los datos son de los últimos scrapeos).
MONTHS = [(2026, 1), (2026, 2), (2026, 3), (2026, 4), (2026, 5), (2026, 6)]
MONTH_WEIGHTS = [1.0, 1.0, 1.5, 2.0, 2.5, 3.0]


class Command(BaseCommand):
    help = "Genera datos de mercado simulados para los 10 competidores principales (mobiliario de oficina VE)."

    def add_arguments(self, parser):
        parser.add_argument("--no-fresh", action="store_true",
                            help="Añade sin borrar la siembra previa de este comando.")
        parser.add_argument("--scale", type=float, default=1.0,
                            help="Factor sobre el nº de filas por competidor (default 1.0).")
        parser.add_argument("--seed", type=int, default=42,
                            help="Semilla del generador aleatorio (determinista).")

    # ------------------------------------------------------------------ #
    @transaction.atomic
    def handle(self, *args, **opt):
        rng = random.Random(opt["seed"])
        scale = max(0.1, float(opt["scale"]))

        if not opt["no_fresh"]:
            self._wipe_previous_seed()

        own_by_cat = self._own_products_by_category()
        if not any(own_by_cat.values()):
            self.stdout.write(self.style.WARNING(
                "No hay productos activos en el catálogo. Corre primero "
                "`seed_company_data` (o `seed_demo_data`) para tener referencia de precios."
            ))

        total = 0
        per_comp: list[tuple[str, int]] = []
        cat_counter: Counter = Counter()

        for profile in COMPETITORS:
            competitor = self._ensure_competitor(profile)
            rows, dates = self._build_rows(profile, competitor, own_by_cat, rng, scale)
            if not rows:
                continue
            run = ScrapeRun.objects.create(
                source=profile["source"],
                query=[profile["name"]],
                params={"seeded": True, "tier": profile["tier"]},
                status=ScrapeRun.StatusChoices.SUCCEEDED,
                records_collected=len(rows),
                records_saved=len(rows),
                records_discarded=0,
                finished_at=timezone.now(),
                notes=SEED_TAG,
            )
            for r in rows:
                r.scrape_run = run
            created = CompetitorMarketData.objects.bulk_create(rows)
            # `scraped_at` es auto_now_add: bulk_create lo pisa con "ahora". Lo
            # retrocedemos a la fecha de observación simulada vía bulk_update
            # (no dispara auto_now_add porque add=False).
            for obj, when in zip(created, dates):
                obj.scraped_at = when
            CompetitorMarketData.objects.bulk_update(created, ["scraped_at"])
            # Alinea la ventana del run con sus filas (auditoría coherente).
            ScrapeRun.objects.filter(pk=run.pk).update(
                started_at=min(dates), finished_at=max(dates),
            )

            total += len(created)
            per_comp.append((profile["name"], len(created)))
            for r in created:
                cat_counter[r.category or "(sin categoría)"] += 1

        self._report(per_comp, cat_counter, total)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _wipe_previous_seed(self) -> None:
        """Borra solo la siembra previa de este comando (no toca datos reales)."""
        runs = ScrapeRun.objects.filter(notes=SEED_TAG)
        n_rows = CompetitorMarketData.objects.filter(scrape_run__in=runs).delete()[0]
        n_runs = runs.delete()[0]
        if n_rows or n_runs:
            self.stdout.write(self.style.WARNING(
                f"--fresh: eliminadas {n_rows} fila(s) y {n_runs} run(s) sembrados previos."
            ))

    def _own_products_by_category(self) -> dict[str, list]:
        """Agrupa los productos propios activos por categoría del vocabulario scraper."""
        buckets: dict[str, list] = defaultdict(list)
        for p in Product.objects.filter(is_active=True):
            text = f"{p.name} {p.full_name or ''}".strip()
            cat = classify_category(text)
            if cat and (p.sale_price_usd or 0) > 0:
                buckets[cat].append(p)
        return buckets

    def _ensure_competitor(self, profile: dict) -> Competitor:
        """Obtiene/crea el competidor y rellena solo sus campos vacíos."""
        competitor, _ = Competitor.objects.get_or_create(name=profile["name"])
        changed = False
        for field in ("state", "municipality", "website", "instagram"):
            if not getattr(competitor, field) and profile.get(field):
                setattr(competitor, field, profile[field])
                changed = True
        if not competitor.is_active:
            competitor.is_active = True
            changed = True
        if changed:
            competitor.save()
        return competitor

    def _build_rows(self, profile, competitor, own_by_cat, rng, scale):
        """Construye las instancias (sin guardar) y sus fechas de observación."""
        n = max(1, round(profile["breadth"] * scale))
        focus = profile["focus"]
        tier = profile["tier"]
        source = profile["source"]

        # Distribución de categorías para este competidor.
        if focus:
            cats = list(focus)
            weights = [1.0] * len(cats)
        else:
            cats = list(BROAD_WEIGHTS.keys())
            weights = list(BROAD_WEIGHTS.values())

        used_names: set[str] = set()
        rows, dates = [], []
        for _ in range(n):
            cat = rng.choices(cats, weights=weights, k=1)[0]
            own_pool = own_by_cat.get(cat, [])
            # Matchea contra un producto propio cuando lo hay (like-with-like);
            # si no, fila genérica de categoría.
            use_match = own_pool and rng.random() < 0.7
            if use_match:
                own = rng.choice(own_pool)
                base_usd = float(own.sale_price_usd)
                product_fk = own
                score = round(rng.uniform(0.82, 0.98), 2)
                name = self._product_name(cat, rng, used_names, own=own)
            else:
                base_usd = CATEGORY_BASELINE.get(cat, 150)
                product_fk = None
                score = None
                name = self._product_name(cat, rng, used_names, own=None)

            month_idx = rng.choices(range(len(MONTHS)), weights=MONTH_WEIGHTS, k=1)[0]
            when = self._random_dt(MONTHS[month_idx], rng)
            price = self._price(base_usd, tier, month_idx, cat, rng)
            url = self._listing_url(profile, name) if source in ("WEB", "ML") else None

            inst = CompetitorMarketData(
                competitor=competitor,
                competitor_name=profile["name"],
                source=source,
                url=url,
                product_name=name,
                category=cat,
                price=price,
                currency="USD",
                price_usd=price,                    # USD → snapshot directo
                lead_time_days=rng.choice(LEAD_TIMES),
                is_in_stock=(rng.random() > 0.10),   # ~10% agotado
                promotions=rng.choice(PROMOS),
                product=product_fk,
                product_match_score=score,
                enriched_by=CompetitorMarketData.EnrichmentChoices.DETERMINISTIC,
                raw_metadata={"_seeded": SEED_TAG, "tier": tier},
            )
            inst.listing_key = compute_listing_key(inst)
            rows.append(inst)
            dates.append(when)
        return rows, dates

    def _product_name(self, cat, rng, used, own=None) -> str:
        """Nombre de producto único para el competidor (parece un modelo comercial)."""
        for _ in range(12):
            if own is not None and rng.random() < 0.5:
                base = own.name  # mismo modelo que el catálogo propio (muy común)
            else:
                template = rng.choice(NAME_TEMPLATES.get(cat, ["Mueble {m}"]))
                base = template.format(m=rng.choice(MODELS))
            candidate = base
            if candidate in used:
                candidate = f"{base} {rng.choice(COLORS)}"
            if candidate not in used:
                used.add(candidate)
                return candidate
        # Último recurso: garantiza unicidad.
        candidate = f"{base} {rng.randint(100, 999)}"
        used.add(candidate)
        return candidate

    def _price(self, base_usd, tier, month_idx, cat, rng) -> Decimal:
        """Precio USD recortado a la banda de la categoría."""
        lo, hi = band_for_category(cat)
        tlo, thi = TIERS[tier]
        mult = rng.uniform(tlo, thi)
        drift = 1.0 + 0.012 * month_idx          # leve creep mensual en USD
        jitter = rng.uniform(0.96, 1.06)
        val = Decimal(str(base_usd * mult * drift * jitter)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP)
        val = max(lo + Decimal("1"), min(val, hi - Decimal("1")))
        return val.quantize(Decimal("0.01"))

    def _random_dt(self, ym: tuple[int, int], rng) -> _dt.datetime:
        """Datetime aware aleatorio dentro del mes (sin pasarse de hoy)."""
        year, month = ym
        today = timezone.localdate()
        if (year, month) == (today.year, today.month):
            max_day = max(1, today.day - 1)
        else:
            max_day = 27
        day = rng.randint(1, max_day)
        naive = _dt.datetime(year, month, day, rng.randint(8, 19), rng.randint(0, 59))
        return timezone.make_aware(naive)

    def _listing_url(self, profile, name) -> str:
        base = profile.get("website") or "https://example.com"
        slug = (
            name.lower().replace(" ", "-").replace("ñ", "n")
            .encode("ascii", "ignore").decode("ascii")
        )
        return f"{base.rstrip('/')}/producto/{slug or 'item'}-{abs(hash(name)) % 100000}"

    def _report(self, per_comp, cat_counter, total) -> None:
        self.stdout.write(self.style.SUCCESS(
            f"\nSiembra de competencia completada: {total} fila(s) en "
            f"{len(per_comp)} competidor(es).\n"
        ))
        self.stdout.write("Por competidor:")
        for name, n in sorted(per_comp, key=lambda x: -x[1]):
            self.stdout.write(f"  {n:>4}  {name}")
        self.stdout.write("\nPor categoría:")
        for cat, n in cat_counter.most_common():
            self.stdout.write(f"  {n:>4}  {cat}")
        self.stdout.write(self.style.SUCCESS(
            "\nListo. Revisa Predicciones › Competencia (/predicciones/competencia)."
        ))

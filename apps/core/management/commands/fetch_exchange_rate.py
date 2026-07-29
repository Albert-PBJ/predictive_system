"""Ingesta automática de las tasas de cambio con alerta de frescura.

Se manejan **tres** tasas: las dos **operativas** y oficiales del BCV — *Dólar BCV*
(`bcv_rate`, Bs/USD) y *Euro BCV* (`eur_bcv_rate`, Bs/EUR) — y el *paralelo*
(`parallel_rate`, Bs/USD), que es **solo referencia analítica** (sirve para leer el
valor real del dinero y alimenta el `shock_cambiario` de los modelos), no se factura
con él salvo que se elija esa base a propósito.

La tasa de cambio era 100% manual: si nadie la cargaba, una tasa vieja distorsiona
en silencio todas las cifras en VES **y** la validación de precios scrapeados (que
convierte a USD con la tasa más reciente). Este comando la actualiza desde una API
pública y, además, vigila su frescura: si la última tasa está vencida, crea una
`Alert` (tipo RATE) para que se note.

Uso:
    python manage.py fetch_exchange_rate                      # baja de la API y upserta hoy
    python manage.py fetch_exchange_rate --bcv 36.5 --eur 39.4 --parallel 40  # carga manual
    python manage.py fetch_exchange_rate --check-only         # solo verifica frescura
    python manage.py fetch_exchange_rate --max-age-days 1     # umbral de "vencida" más estricto

Fuente: la librería **pyDolarVenezuela** (paquete `pyDolarVenezuela`) es la primaria
—obtiene las oficiales (dólar y euro) de forma estable y, best-effort, el paralelo—.
Solo si la librería no logra ni el Dólar BCV (no instalada o sus fuentes caídas) se cae a la **API
HTTP** configurada en `SystemSettings.exchange_rate_api_url` (pyDolarVe). Si todo
falla, el comando no se cae: registra el fallo y corre igualmente la verificación de
frescura (que avisará). Ver `fetch_rates` / `fetch_rates_from_library`.
"""

import json
import logging
import urllib.request
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.models import ExchangeRate

logger = logging.getLogger(__name__)

# La URL de la API y el umbral de frescura hoy se gestionan desde `SystemSettings`
# (editables en la UI). Este default queda como respaldo de `check_rate_freshness`
# cuando se la llama sin argumento.
DEFAULT_MAX_AGE_DAYS = 2
HTTP_TIMEOUT = 15


def _to_decimal(value):
    """Convierte un valor de la API a Decimal con 4 decimales, o None si no se puede."""
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.0001"))
    except (InvalidOperation, ValueError, TypeError):
        return None


def fetch_rates_from_api(url: str) -> tuple:
    """Baja (Dólar BCV, Euro BCV, paralelo) de la API pública. Cada uno ``Decimal|None``.

    pyDolarVe devuelve ``{"monitors": {"bcv": {"price": …}, "enparalelovzla": {"price": …}}}``.
    Es tolerante a variantes del nombre del monitor paralelo y del monitor del euro
    (según la versión del endpoint el euro oficial aparece como ``eur``/``euro``/``bcv_eur``).
    """
    req = urllib.request.Request(url, headers={"User-Agent": "maescar-predictive/1.0"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    monitors = data.get("monitors", data) if isinstance(data, dict) else {}

    def _first(keys):
        for key in keys:
            node = monitors.get(key)
            if isinstance(node, dict):
                value = _to_decimal(node.get("price"))
                if value is not None:
                    return value
        return None

    bcv = _to_decimal((monitors.get("bcv") or {}).get("price"))
    eur = _first(("eur", "euro", "bcv_eur", "eur_bcv"))
    parallel = _first(("enparalelovzla", "paralelo", "bitcoin", "dolartoday"))
    return bcv, eur, parallel


def _plausible_parallel(parallel, bcv) -> bool:
    """Filtra valores absurdos de la paralela (algunos scrapers upstream devuelven
    basura, p. ej. 0,01). Si se conoce la BCV, exige una proporción razonable
    (la paralela ronda la oficial, nunca una fracción mínima); si no, un piso absoluto."""
    if parallel is None or parallel <= 0:
        return False
    if bcv and bcv > 0:
        return Decimal("0.5") * bcv <= parallel <= Decimal("5") * bcv
    return parallel >= Decimal("10")


def fetch_rates_from_library() -> tuple:
    """Obtiene (Dólar BCV, Euro BCV, paralelo) con **pyDolarVenezuela**. Best-effort:
    nunca lanza; retorna una tupla de ``Decimal|None``.

    Las dos oficiales del BCV son estables (página ``BCV``, monitores ``usd`` y ``eur``).
    El paralelo es menos fiable —según el momento, algunos scrapers de la librería están
    caídos o devuelven valores inválidos—, así que se prueban varias fuentes y se valida
    el resultado; si ninguna sirve, queda en None (es solo referencia analítica). Importa
    la librería de forma diferida (dependencia opcional).
    """
    try:
        from pyDolarVenezuela import Monitor
        from pyDolarVenezuela.pages import AlCambio, BCV, CriptoDolar, EnParaleloVzla
    except ImportError as exc:
        logger.warning(
            "pyDolarVenezuela no está instalado (%s); se usará la API HTTP de respaldo. "
            "Instálalo con: pip install pyDolarVenezuela",
            exc,
        )
        return None, None, None

    bcv = None
    try:
        official = Monitor(BCV, "USD").get_value_monitors("usd")
        bcv = _to_decimal(getattr(official, "price", None))
    except Exception as exc:  # red, scraping roto, etc.
        logger.warning("No se pudo obtener la BCV oficial de pyDolarVenezuela: %s", exc)

    # Euro BCV: el BCV publica la tasa oficial del euro junto a la del dólar. La librería
    # la expone como el monitor `eur` de la misma página (moneda EUR).
    eur = None
    for currency, key in (("EUR", "eur"), ("USD", "eur")):
        try:
            node = Monitor(BCV, currency).get_value_monitors(key)
            eur = _to_decimal(getattr(node, "price", None))
            if eur is not None:
                break
        except Exception as exc:
            logger.debug("Euro BCV no disponible vía %s/%s: %s", currency, key, exc)
    if eur is None:
        logger.info("pyDolarVenezuela: no se obtuvo la tasa Euro BCV; puedes cargarla a mano.")

    parallel = None
    # Se prueban varias fuentes de paralela y se toma la primera plausible.
    for page, key in (
        (EnParaleloVzla, "enparalelovzla"),
        (CriptoDolar, "enparalelovzla"),
        (AlCambio, "enparalelovzla"),
    ):
        try:
            node = Monitor(page, "USD").get_value_monitors(key)
            candidate = _to_decimal(getattr(node, "price", None))
            if _plausible_parallel(candidate, bcv):
                parallel = candidate
                break
        except Exception as exc:  # fuente caída o sin ese monitor
            logger.debug("Fuente paralela %s/%s no disponible: %s", getattr(page, "name", page), key, exc)

    if parallel is None:
        logger.info(
            "pyDolarVenezuela: no hay una tasa paralela válida disponible ahora; se "
            "cargan solo las oficiales (puedes cargar el paralelo manualmente)."
        )
    return bcv, eur, parallel


def fetch_rates(url: str | None = None) -> tuple:
    """Obtiene (Dólar BCV, Euro BCV, paralelo, fuente) priorizando **pyDolarVenezuela**.

    Si la librería logra el Dólar BCV, se usa su resultado (con el Euro BCV y el paralelo
    que haya podido validar, posiblemente None). Solo si la librería no consigue ni el
    Dólar BCV (no instalada o todas sus fuentes caídas) se cae a la **API HTTP**
    configurada (``url``, pyDolarVe). Ese respaldo puede lanzar (red/DNS): los llamadores
    lo manejan. El cuarto valor describe la fuente usada.
    """
    bcv, eur, parallel = fetch_rates_from_library()
    if bcv is not None:
        return bcv, eur, parallel, "pyDolarVenezuela"
    if url:
        api_bcv, api_eur, api_parallel = fetch_rates_from_api(url)
        return api_bcv, api_eur, api_parallel, url
    return None, None, None, "pyDolarVenezuela"


def check_rate_freshness(max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> dict:
    """Verifica que la última tasa no esté vencida; gestiona la alerta en consecuencia.

    Si está vencida (o no hay ninguna), crea una `Alert` tipo RATE (sin duplicar una
    no resuelta). Si está fresca, resuelve cualquier alerta de tasa abierta. Retorna
    un dict con el diagnóstico para el comando/llamador.
    """
    from apps.analytics.models import Alert  # import diferido: evita ciclos al cargar

    latest = ExchangeRate.objects.order_by("-date").first()
    today = timezone.localdate()
    age_days = None if latest is None else (today - latest.date).days
    is_stale = latest is None or age_days > max_age_days

    open_alert = Alert.objects.filter(
        alert_type=Alert.TypeChoices.RATE_STALE, is_resolved=False
    ).first()

    if is_stale:
        if latest is None:
            title = "No hay tasa de cambio cargada"
            message = "No existe ninguna ExchangeRate. Carga una para poder valorar en VES y validar precios."
            severity = Alert.SeverityChoices.CRITICAL
        else:
            title = f"Tasa de cambio vencida ({age_days} día(s))"
            message = (
                f"La última tasa es del {latest.date} ({age_days} día(s) de antigüedad, "
                f"umbral {max_age_days}). Actualízala para no distorsionar las cifras en VES."
            )
            severity = (
                Alert.SeverityChoices.CRITICAL
                if age_days > 2 * max_age_days
                else Alert.SeverityChoices.WARNING
            )
        if open_alert is None:
            from apps.analytics.alerts import audience_for

            Alert.objects.create(
                alert_type=Alert.TypeChoices.RATE_STALE,
                severity=severity,
                title=title,
                message=message,
                dedupe_key="rate_stale",
                audience=audience_for(Alert.TypeChoices.RATE_STALE),
            )
            created_alert = True
        else:
            created_alert = False
    else:
        # Tasa fresca: cierra cualquier alerta de tasa que siguiera abierta.
        resolved = Alert.objects.filter(
            alert_type=Alert.TypeChoices.RATE_STALE, is_resolved=False
        ).update(is_resolved=True, is_read=True)
        created_alert = False
        if resolved:
            logger.info("Tasa fresca: se resolvieron %d alerta(s) de tasa abiertas.", resolved)

    return {
        "is_stale": is_stale,
        "age_days": age_days,
        "latest_date": latest.date if latest else None,
        "created_alert": created_alert,
    }


class Command(BaseCommand):
    help = "Actualiza las tasas de cambio (Dólar BCV, Euro BCV y paralelo) y vigila su frescura."

    def add_arguments(self, parser):
        parser.add_argument("--bcv", type=str, help="Tasa Dólar BCV manual (Bs/USD); omite la API.")
        parser.add_argument("--eur", type=str, help="Tasa Euro BCV manual (Bs/EUR).")
        parser.add_argument("--parallel", type=str, help="Tasa paralela manual (Bs/USD, referencial).")
        parser.add_argument("--date", type=str, help="Fecha de la tasa (YYYY-MM-DD); por defecto hoy.")
        parser.add_argument(
            "--max-age-days", type=int, default=None,
            help="Días desde los que la tasa se considera vencida (por defecto, el de la configuración).",
        )
        parser.add_argument(
            "--check-only", action="store_true",
            help="No baja ni carga nada: solo verifica la frescura y gestiona la alerta.",
        )

    def handle(self, *args, **options):
        from apps.core import system_settings

        # El umbral por defecto sale de la Configuración del Sistema (editable en UI),
        # salvo que se pase explícitamente por --max-age-days.
        max_age_days = options["max_age_days"]
        if max_age_days is None:
            max_age_days = system_settings.rate_max_age_days()

        if not options["check_only"]:
            self._ingest(options)

        result = check_rate_freshness(max_age_days)
        if result["is_stale"]:
            self.stdout.write(self.style.WARNING(
                f"Tasa VENCIDA (última: {result['latest_date']}, "
                f"{result['age_days']} día(s)). "
                + ("Alerta creada." if result["created_alert"] else "Alerta ya existía.")
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Tasa al día (última: {result['latest_date']}, {result['age_days']} día(s))."
            ))

    def _ingest(self, options):
        target_date = date.today()
        if options.get("date"):
            try:
                target_date = datetime.strptime(options["date"], "%Y-%m-%d").date()
            except ValueError:
                self.stderr.write(self.style.ERROR("Fecha inválida; usa YYYY-MM-DD."))
                return

        bcv = _to_decimal(options.get("bcv"))
        eur = _to_decimal(options.get("eur"))
        parallel = _to_decimal(options.get("parallel"))

        if bcv is None:
            from apps.core import system_settings

            url = system_settings.exchange_rate_api_url()
            try:
                api_bcv, api_eur, api_parallel, provider = fetch_rates(url)
                bcv = bcv or api_bcv
                eur = eur or api_eur
                parallel = parallel or api_parallel
                self.stdout.write(self.style.SUCCESS(
                    f"Fuente ({provider}): Dólar BCV={bcv}, Euro BCV={eur}, Paralelo={parallel}"
                ))
            except Exception as exc:  # red, parseo, timeout: no abortamos
                logger.warning("No se pudo obtener la tasa automáticamente: %s", exc)
                self.stderr.write(self.style.WARNING(
                    f"No se pudo obtener la tasa: {exc}. "
                    "Pasa --bcv/--eur/--parallel para cargarla manualmente."
                ))

        if bcv is None:
            self.stderr.write(self.style.ERROR(
                "Sin tasa Dólar BCV (ni de la API ni manual); no se cargó nada."
            ))
            return

        rate, created = ExchangeRate.objects.update_or_create(
            date=target_date,
            defaults={
                "bcv_rate": bcv,
                "eur_bcv_rate": eur,
                "parallel_rate": parallel,
                "source": ExchangeRate.SourceChoices.BCV,
            },
        )
        verb = "creada" if created else "actualizada"
        self.stdout.write(self.style.SUCCESS(
            f"Tasa {verb} {rate.date}: Dólar BCV={rate.bcv_rate} | "
            f"Euro BCV={rate.eur_bcv_rate} | Paralelo={rate.parallel_rate}"
        ))

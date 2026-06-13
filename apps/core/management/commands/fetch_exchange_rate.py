"""Ingesta automática de la tasa de cambio (BCV + paralela) con alerta de frescura.

La tasa de cambio era 100% manual: si nadie la cargaba, una tasa vieja distorsiona
en silencio todas las cifras en VES **y** la validación de precios scrapeados (que
convierte a USD con la tasa más reciente). Este comando la actualiza desde una API
pública y, además, vigila su frescura: si la última tasa está vencida, crea una
`Alert` (tipo RATE) para que se note.

Uso:
    python manage.py fetch_exchange_rate                      # baja de la API y upserta hoy
    python manage.py fetch_exchange_rate --bcv 36.5 --parallel 40   # carga manual (offline)
    python manage.py fetch_exchange_rate --check-only         # solo verifica frescura
    python manage.py fetch_exchange_rate --max-age-days 1     # umbral de "vencida" más estricto

Fuente: la librería **pyDolarVenezuela** (paquete `pyDolarVenezuela`) es la primaria
—obtiene la BCV oficial de forma estable y, best-effort, la paralela—. Solo si la
librería no logra ni la BCV (no instalada o sus fuentes caídas) se cae a la **API
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
    """Baja (BCV, paralela) de la API pública. Retorna (Decimal|None, Decimal|None).

    pyDolarVe devuelve ``{"monitors": {"bcv": {"price": …}, "enparalelovzla": {"price": …}}}``.
    Es tolerante a variantes del nombre del monitor paralelo.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "maescar-predictive/1.0"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    monitors = data.get("monitors", data) if isinstance(data, dict) else {}
    bcv = _to_decimal((monitors.get("bcv") or {}).get("price"))
    parallel = None
    for key in ("enparalelovzla", "paralelo", "bitcoin", "dolartoday"):
        node = monitors.get(key)
        if isinstance(node, dict):
            parallel = _to_decimal(node.get("price"))
            if parallel is not None:
                break
    return bcv, parallel


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
    """Obtiene (BCV, paralela) con la librería **pyDolarVenezuela**. Best-effort:
    nunca lanza; retorna ``(Decimal|None, Decimal|None)``.

    La BCV oficial es estable (página ``BCV``, monitor ``usd``). La paralela es menos
    fiable —según el momento, algunos scrapers de la librería están caídos o devuelven
    valores inválidos—, así que se prueban varias fuentes y se valida el resultado;
    si ninguna sirve, la paralela queda en None (el sistema usa la BCV). Importa la
    librería de forma diferida (dependencia opcional).
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
        return None, None

    bcv = None
    try:
        official = Monitor(BCV, "USD").get_value_monitors("usd")
        bcv = _to_decimal(getattr(official, "price", None))
    except Exception as exc:  # red, scraping roto, etc.
        logger.warning("No se pudo obtener la BCV oficial de pyDolarVenezuela: %s", exc)

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
            "carga solo la BCV (puedes cargar la paralela manualmente)."
        )
    return bcv, parallel


def fetch_rates(url: str | None = None) -> tuple:
    """Obtiene (BCV, paralela, fuente) priorizando **pyDolarVenezuela**.

    Si la librería logra la BCV, se usa su resultado (con la paralela que haya podido
    validar, posiblemente None). Solo si la librería no consigue ni la BCV (no
    instalada o todas sus fuentes caídas) se cae a la **API HTTP** configurada
    (``url``, pyDolarVe). Ese respaldo puede lanzar (red/DNS): los llamadores lo
    manejan. Retorna ``(Decimal|None, Decimal|None, str)`` donde el tercer valor
    describe la fuente usada.
    """
    bcv, parallel = fetch_rates_from_library()
    if bcv is not None:
        return bcv, parallel, "pyDolarVenezuela"
    if url:
        api_bcv, api_parallel = fetch_rates_from_api(url)
        return api_bcv, api_parallel, url
    return None, None, "pyDolarVenezuela"


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
            Alert.objects.create(
                alert_type=Alert.TypeChoices.RATE_STALE,
                severity=severity,
                title=title,
                message=message,
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
    help = "Actualiza la tasa de cambio (BCV + paralela) y vigila su frescura."

    def add_arguments(self, parser):
        parser.add_argument("--bcv", type=str, help="Tasa BCV manual (Bs/USD); omite la API.")
        parser.add_argument("--parallel", type=str, help="Tasa paralela manual (Bs/USD).")
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
        parallel = _to_decimal(options.get("parallel"))

        if bcv is None:
            from apps.core import system_settings

            url = system_settings.exchange_rate_api_url()
            try:
                api_bcv, api_parallel, provider = fetch_rates(url)
                bcv = bcv or api_bcv
                parallel = parallel or api_parallel
                self.stdout.write(self.style.SUCCESS(
                    f"Fuente ({provider}): BCV={bcv}, Paralela={parallel}"
                ))
            except Exception as exc:  # red, parseo, timeout: no abortamos
                logger.warning("No se pudo obtener la tasa automáticamente: %s", exc)
                self.stderr.write(self.style.WARNING(
                    f"No se pudo obtener la tasa: {exc}. "
                    "Pasa --bcv/--parallel para cargarla manualmente."
                ))

        if bcv is None:
            self.stderr.write(self.style.ERROR(
                "Sin tasa BCV (ni de la API ni manual); no se cargó nada."
            ))
            return

        rate, created = ExchangeRate.objects.update_or_create(
            date=target_date,
            defaults={
                "bcv_rate": bcv,
                "parallel_rate": parallel,
                "source": ExchangeRate.SourceChoices.BCV,
            },
        )
        verb = "creada" if created else "actualizada"
        self.stdout.write(self.style.SUCCESS(
            f"Tasa {verb} {rate.date}: BCV={rate.bcv_rate} | Paralela={rate.parallel_rate}"
        ))

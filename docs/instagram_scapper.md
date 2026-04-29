# Instagram Scraper via Apify

**Fecha:** 2026-04-28
**Branch:** `scrappers`

## Resumen

Se integró el scraper de Instagram de Apify al backend de Django. Cada post obtenido se mapea a un registro de `CompetitorMarketData`, extrayendo los campos del modelo desde el caption cuando es posible y almacenando el JSON completo de Apify en `raw_metadata`.

---

## Archivos creados

### `apps/competitor_market_data/scrapers/__init__.py`
Marcador de paquete vacío.

### `apps/competitor_market_data/scrapers/instagram_scraper.py`
Lógica central del scraper. Responsabilidades:

- **`scrape_instagram_profiles(urls, results_limit)`** — función pública que:
  1. Instancia `ApifyClient` con la `APIFY_API_KEY` del entorno.
  2. Llama al actor `apify/instagram-scraper` con `resultsType: "posts"`.
  3. Itera el dataset resultante y mapea cada item con `_map_post_to_instance`.
  4. Inserta todos los registros en masa con `bulk_create` y los retorna.

- **Helpers de extracción (todos privados, prefijo `_`):**

  | Función | Campo destino | Lógica |
  |---|---|---|
  | `_extract_price` | `price`, `currency` | Regex sobre el caption. Detecta `Bs.`, `VES` (bolivar) y `$`, `USD` (dólar). Retorna `(Decimal, str)` o `(None, None)`. |
  | `_extract_lead_time` | `lead_time_days` | Regex sobre el caption. Busca patrones como "3 días de entrega" o "delivery in 5 days". |
  | `_extract_product_name` | `product_name` | Primera línea del caption que no sea hashtag ni mención. |
  | `_extract_promotions` | `promotions` | Busca palabras clave de oferta en el caption y hashtags del post. |
  | `_is_in_stock` | `is_in_stock` | Devuelve `False` si el caption contiene "agotado", "sin stock", "sold out", etc. |
  | `_map_post_to_instance` | — | Orquesta los helpers anteriores y construye la instancia del modelo. Para `competitor_name` usa `ownerFullName` salvo que esté vacío o contenga `|` (nombres con palabras clave separadas), en cuyo caso usa `ownerUsername`. |

- **`category`** se deja en `None` — no es derivable de forma confiable desde un post de Instagram.
- **`raw_metadata`** recibe el dict completo retornado por Apify sin modificaciones.

### `apps/competitor_market_data/views.py`
Vista DRF `InstagramScraperStartView` (`APIView`). Solo expone el método `POST`.

**Validaciones:**
- `urls` es requerido y debe ser una lista.
- `limit` debe ser un entero positivo (default `50`).

**Respuestas:**

| Escenario | Status | Cuerpo |
|---|---|---|
| Éxito | `201 Created` | `{"saved": N}` |
| `urls` inválido | `400 Bad Request` | `{"error": "..."}` |
| `limit` inválido | `400 Bad Request` | `{"error": "..."}` |
| `APIFY_API_KEY` no configurada | `500 Internal Server Error` | `{"error": "..."}` |

### `apps/competitor_market_data/urls.py`
```python
path("instagram/start", InstagramScraperStartView.as_view(), name="instagram-scraper-start")
```

### `apps/competitor_market_data/management/commands/scrape_instagram.py`
Comando de gestión Django como alternativa CLI (no es el método principal, el endpoint es la vía oficial):
```bash
python manage.py scrape_instagram https://www.instagram.com/competidor/ --limit 100
```

---

## Archivos modificados

### `.env`
Creado con las siguientes variables. La `APIFY_API_KEY` ya tiene la clave real configurada.

```
DJANGO_SECRET_KEY=...
DB_NAME=predictive_system
DB_USER=postgres
DB_PASSWORD=...
DB_HOST=127.0.0.1
DB_PORT=5432
APIFY_API_KEY=<clave real configurada>
```

### `requirements.txt`
Se agregó:
```
python-dotenv==1.1.0
```

### `predictive_system_backend/settings.py`
- Se importa `load_dotenv` y se carga el `.env` al inicio.
- `SECRET_KEY`, credenciales de BD y `APIFY_API_KEY` se leen desde variables de entorno con `os.environ.get`.

### `predictive_system_backend/urls.py`
Se agregó la ruta del scraper:
```python
path("scrapers/", include("apps.competitor_market_data.urls"))
```

---

## Endpoint

```
POST /scrapers/instagram/start
Content-Type: application/json

{
    "urls": ["https://www.instagram.com/competidor1/"],
    "limit": 50
}
```

---

## Mapeo Apify → CompetitorMarketData

| Campo del modelo | Fuente en el item de Apify |
|---|---|
| `competitor_name` | `ownerFullName` (o `ownerUsername` si está vacío o contiene `\|`) |
| `source` | Fijo: `"IG"` |
| `url` | `url` |
| `product_name` | Primera línea real del `caption` |
| `price` | Extraído del `caption` via regex |
| `currency` | Derivado del símbolo/texto junto al precio (`USD` / `VES`) |
| `lead_time_days` | Extraído del `caption` via regex |
| `is_in_stock` | `False` si el `caption` contiene palabras de agotado |
| `promotions` | Palabras clave de oferta en `caption` + hashtags del post |
| `category` | `None` (no derivable desde IG) |
| `raw_metadata` | Item completo devuelto por Apify |
| `scraped_at` | Auto-generado (`auto_now_add=True`) |

---

## Actor de Apify utilizado

- **ID:** `apify/instagram-scraper`
- **Modo:** `resultsType: "posts"`
- El run es síncrono — `.call()` bloquea hasta que el actor finaliza.

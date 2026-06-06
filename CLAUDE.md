# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Django 6.0.4 backend for **Inversiones Maescar C.A.** — an office furniture company in Venezuela. The system centralizes sales operations, tracks competitor prices via web scrapers, and will feed ML models for demand forecasting and price prediction. Uses PostgreSQL and Django REST Framework.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Apply migrations
python manage.py migrate

# Run dev server
python manage.py runserver

# Create migrations after model changes
python manage.py makemigrations

# Load demo data for the sales & inventory modules (idempotent):
# categories, products (with stock/prices), customers, today's exchange rate,
# a Seller linked to the admin user, a demo SELLER user (vendedor1) and a demo
# WAREHOUSE / inventory-manager user (inventario1).
python manage.py seed_demo_data

# Update the exchange rate (BCV + parallel) and raise a freshness Alert if stale.
# Pulls from pyDolarVe by default (override with EXCHANGE_RATE_API_URL); degrades
# gracefully with no network. --bcv/--parallel load manually; --check-only just
# verifies freshness (and resolves/creates the RATE alert accordingly).
python manage.py fetch_exchange_rate
python manage.py fetch_exchange_rate --bcv 36.5 --parallel 40   # carga manual (offline)
python manage.py fetch_exchange_rate --check-only               # solo verifica frescura

# Run scrapers via CLI
python manage.py scrape_instagram <url1> <url2> --limit 50
python manage.py scrape_facebook_marketplace <url1> <url2> --limit 50
python manage.py scrape_website <url1> <url2> --limit 50 --competitor-name "Nombre Competidor"
python manage.py scrape_mercadolibre "Sillas de oficina" "Escritorio en L" --limit 50   # busca por términos, no URLs

# Recalcular el match de productos propios sobre los datos ya scrapeados (contra el
# catálogo actual). Útil tras crear/renombrar un producto: las filas viejas se asocian.
python manage.py rematch_products                  # todas las filas
python manage.py rematch_products --only-unmatched # solo las que aún no tienen producto
python manage.py rematch_products --no-llm         # solo match determinista
```

There are no tests yet — placeholder `tests.py` files exist in some apps but are unpopulated.

This API has all of its comments as well as API responses in Spanish. Functions, variables and code practices can remain in English.

## Architecture

### Apps

| App | Purpose |
|-----|---------|
| `apps/accounts` | Auth & RBAC: UserProfile (role), JWT login/logout/refresh/me, role-based permissions |
| `apps/core` | Master data: Product, Category, Customer, Seller, ExchangeRate, ProductPriceHistory. Also exposes a read-only `GET /api/exchange-rate/latest` and the `seed_demo_data` command |
| `apps/sales` | Transactions: Sale, SaleItem, Quote, QuoteItem. **REST API for registering sales** (`SaleViewSet` + `services.py`) |
| `apps/inventory` | Audit trail: InventoryMovement (every stock change logged). **REST API for stock control** (`InventoryMovementViewSet`, `StockListView` + `services.py`) |
| `apps/benchmarking` | Competitor intelligence: Competitor, CompetitorMarketData (with USD snapshot, `listing_key`, own-`Product` match, provenance), ScrapeRun (run traceability), RejectedMarketData (archived discards). Admin `merge_competitors` action |
| `apps/analytics` | ML layer: PredictionLog (model registry), KPI, Alert |
| `apps/competitor_market_data` | Apify scraper integration + REST endpoints to trigger scrapers |
| `apps/products` | REST API (ModelViewSet) for Product CRUD — thin layer over `apps/core` models. **`stock` is read-only** in the serializer (it only moves via `InventoryMovement`), so editing a product can't bypass the audit trail. Read = operativo, write = Manager+ |
| `apps/customers` | REST API (ModelViewSet) for Customer CRUD — thin layer over `apps/core` (no models of its own), same pattern as `apps/products` |

### Scraper Flow

`apps/competitor_market_data/scrapers/` holds the Apify integration. Four scrapers are available:

| Scraper | Actor | Source tag | Notes |
|---------|-------|-----------|-------|
| `instagram_scraper.py` | `apify/instagram-scraper` | `IG` | Captions are free text, so the deterministic layer (regex price/lead time/promotions + keyword `category`) is weak; **optionally enriched by an LLM** (see below) that cleans `product_name`, fills `category`, extracts promotions, recovers a fallback price, and resolves the `Competitor` FK **deduped by Instagram handle**. FK is null when the LLM is off |
| `facebook_marketplace_scraper.py` | `apify/facebook-marketplace-scraper` | `FB` | Maps structured listing fields (`listingPrice`, `listingTitle`, `itemUrl`, `isSold`/`isLive`); keyword-classifies `category`; **optionally resolves `Competitor` FK via an LLM** (see below). FK is null when the LLM is off or no seller is identifiable |
| `mercadolibre_scraper.py` | `piotrv1001/mercado-libre-listings-scraper` | `ML` | **Dedicated Mercado Libre Venezuela scraper** (the generic AI web scraper can't scrape ML — it needs an account + residential proxy). Searches by **keywords** (`searchQueries`), not URLs — `start_mercadolibre_run(urls=…)` reuses the generic view contract where `urls` carries the search terms. Run constants (`SITE_ID="MLV"`, residential VE proxy, `officialStoresOnly`, `condition`, `sort`) are module-level. The actor returns **structured** data, so mapping is deterministic: `price`/`currency` (VES/USD, `VEF`→`VES`, with an overflow guard for `DecimalField(max_digits=10)`), `category` via `classify_category`, `is_in_stock` from `availableQuantity`, `promotions` from `discountPercent`/`freeShipping`/`installments`. The **seller** (`seller.storeName`/`nickname`) becomes the **`Competitor`** (dedup by name + location backfilled from `location`), falling back to "Mercado Libre" when no seller is identifiable. The **LLM is optional and light** (same `USE_LLM_ENRICHMENT` gate): it only does fuzzy seller dedup against known competitors + refines promotions/product name |
| `website_scraper.py` | `apify/ai-web-scraper` | `WEB` | Works for **company sites and marketplaces (e.g. Mercado Libre)**. AI prompt extracts the **full field set** per product (`title`, `price`, `promotion`, `category`, `availability`, `delivery_time`, `location`, `seller`); deterministic mapping then fills `category` (AI value if valid, else `classify_category`), `lead_time_days`, `is_in_stock`, `promotions` — same fields as IG/FB. **Resolves `Competitor` FK via the fuzzy `get_or_create_competitor`** keyed on a **human-readable site name** (`prettify_site_name`: `mercadolibre.com.ve` → "Mercado Libre"), or a manual `competitor_name` override. Backfills the competitor's `state`/`municipality` from the product `location` **only for single-company sites** (skipped for marketplaces, where location is per-seller — `is_marketplace_url`). No LLM pass: the actor is itself AI-driven |

All four map items → `CompetitorMarketData`, then hand the batch to a **shared persistence layer** (`scrapers/persistence.persist_records`) that runs the **quality gate** (`scrapers/validation.py`), bulk-creates the survivors, and — in the same step — snapshots the USD price, computes a stable `listing_key`, best-effort matches the row to an own `Product`, stamps provenance (`enriched_by`, `scrape_run`), and **archives the discards** in `RejectedMarketData`. The full Apify JSON is preserved in `raw_metadata`. See **Scraped-data trust & traceability** below for the field-by-field detail.

**Data quality gate (`scrapers/validation.py`, always on):** Right before persisting, `persist_records` calls `partition_valid(instances)` and bulk-creates only the survivors — bad rows do not enter `CompetitorMarketData` (priority is a clean dataset for the ML models), but they are **archived** in `RejectedMarketData` (with their reason + raw JSON) rather than merely logged, so the gate's precision is auditable. Two deterministic, reproducible checks per record:

1. **Product name** — must be present, survive `clean_product_name()`, and actually name a product. `clean_product_name()` strips currency-anchored price tokens glued to the text (`"Silla de oficina20$"` → `"Silla de oficina"`), emojis and edge junk; bare numbers like dimensions (`"Escritorio 1.20m"`) are kept (stripping is anchored to `$`/`Bs`/`VES`/`USD`). `looks_like_statement()` then discards "names" that are really slogans/calls-to-action (`"Buscas ahorrar costos!!"`, `"Una Imagen para tu Oficina!!"`) via two high-precision signals: `!`/`?`/`¡`/`¿` punctuation, or a marketing-verb first word (`_NON_PRODUCT_STARTERS`). Applied at map time on **all four** sources. The LLM prompts (Instagram + Facebook) also ask for a real *office-furniture* product name and `null` otherwise — so a `null` from a working LLM, or the deterministic detector when the LLM is off, both lead to a discard.
2. **Price plausibility** — the price (converted to USD via the latest `ExchangeRate`, parallel rate preferred) must fall inside a **per-category band** calibrated to the Venezuelan furniture market. This rejects both implausibly-low prices (a desk at $1) **and** implausibly-high ones (a desk at $1000) — the ceiling is per-category so a $1000 desk is dropped while a legit $1100 reception set is kept. Bands live in `PRICE_BANDS` (+ `DEFAULT_BAND` for uncategorized) as **code constants** (global, same for all sources — edit the dict to tune, not via `.env`). A VES price with no `ExchangeRate` loaded is kept-but-not-range-validated (logged as a warning). There is **no** "is it furniture?" classifier — feasibility for the local economy is the rule. **Instagram exception:** a *missing* price doesn't discard an IG post (prices are rarely explicit in captions), controlled by the `DISCARD_INSTAGRAM_WITHOUT_PRICE` toggle (default `False`); FB/Web/Mercado Libre still require a price, and an IG post that *does* have a price is still range-checked.

Every discard is logged at INFO with its reason, plus a per-run summary (`N de M descartados`), **and** persisted to `RejectedMarketData`. The `/finalize` response's `saved` count therefore reflects only the kept rows.

**Scraped-data trust & traceability (`scrapers/persistence.py`, + `matching.py`, `competitors.py`):** `persist_records(instances, *, scrape_run, llm_used)` is the single tail every `finalize_*` now calls (instead of an inline `partition_valid`+`bulk_create`). Per batch it:

1. **USD price snapshot** (`validation.stamp_price_usd`) — stores `price_usd` plus, for VES rows, the `exchange_rate_used` and its `rate_date`. The conversion is frozen at scrape time (not re-derived from "today's" rate), so the USD price series is reproducible. USD rows copy through; a VES row with no `ExchangeRate` leaves `price_usd` null.
2. **Listing identity / observation semantics** — `compute_listing_key` hashes `source+url` (or `source+competitor+product` when there's no URL) into `listing_key`. Rows are append-only **observations**: the latest `scraped_at` per `listing_key` is the current snapshot, so aggregates can dedupe instead of double-counting re-scrapes.
3. **Product match** (`matching.py`) — associates the row with the closest own `core.Product` (sets `product` FK + `product_match_score`), enabling like-with-like benchmarking; revisable in the admin. Two vías: **(a) deterministic** name-token similarity over the active catalog — case/accent-insensitive (normalized), drops generic filler words (`_STOPWORDS`), blends an **overlap coefficient** (tolerates one name being a super-set of the other: "Silla Trendy" ↔ "Silla de oficina Trendy") with Jaccard, and counts plurals/typos as the same token via a per-token `SequenceMatcher` (≥ `_TOKEN_SIM`); accepts above `MATCH_THRESHOLD`. **(b) Optional LLM** (`deepseek.match_products`, same `USE_LLM_ENRICHMENT` switch) — for rows the deterministic pass left unmatched, **one batched call** proposes a catalog product (or null); off by default, degrades safely. **The match is not frozen:** `manage.py rematch_products` recomputes it over existing rows against the *current* catalog (run it after creating/renaming a product so old scraped rows pick it up) — `--source`, `--only-unmatched`, `--no-llm`, `--limit`.
4. **Provenance** — `enriched_by` (DET vs LLM) and `scrape_run` FK. A `ScrapeRun` row (created in `/start` with the query terms, closed by `persist_records` with `records_collected/saved/discarded` + status) groups every row to the run, params, and time that produced it.

**Fuzzy competitor dedup (`scrapers/competitors.py`):** all four scrapers' deterministic competitor creation goes through `get_or_create_competitor(name, defaults=…)`, which normalizes the name (lowercase, strip accents + legal suffixes like `C.A.`/`S.A.`/`SRL`) and matches an existing competitor by exact-normalized or `SequenceMatcher` ratio ≥ `SIMILARITY_THRESHOLD` (0.88) before creating a new one — so "Muebles AB" and "Muebles AB, C.A." collapse to one. Duplicates that still slip through are merged by hand via the **admin action** `benchmarking.admin.merge_competitors` (reassigns market data + alerts to the canonical, backfills empty fields, deletes the rest).

**Optional LLM enrichment (Facebook + Instagram):** `enrichment/deepseek.py` makes **one call per listing/post**, gated by the **same** `USE_LLM_ENRICHMENT` switch (+ `DEEPSEEK_API_KEY` + the `openai` package). It is **off by default** and fully optional: if any is missing or the API call fails, the deterministic mapping still saves every record. DeepSeek is OpenAI-API-compatible (the `openai` SDK is pointed at its `base_url`); only cleaned text fields are sent (never the raw JSON), and the model is told to return `null` rather than invent data. Both paths log an `ACTIVE`/`DISABLED` line and a final breakdown (items enriched, linked-to-existing vs new competitor, etc.).

- **Facebook** (`enrich_listing`): (a) identifies/normalizes the seller and links it to a `Competitor` — matching an existing one by id or creating a new one above a confidence threshold (`_MIN_CONFIDENCE`) — and (b) extracts promotions/benefits from the description (overriding the keyword baseline). FK resolution lives in `_resolve_competitor_fk`.
- **Instagram** (`enrich_instagram_post`): does **more**, because captions are unstructured — it extracts a clean `product_name`, picks a `category` from the shared controlled list, extracts promotions, recovers `price`/`currency` as a fallback only when the regex found none, and resolves the `Competitor`. **Competitor dedupe is keyed on the Instagram handle** (`ownerUsername`/profile URL, normalized by `_handle_from`): a post links to an existing `Competitor` whose `instagram` field matches the handle (within-run cache + cross-run index in `_resolve_instagram_competitor`), and newly created competitors are stored **with their handle** so future runs dedupe against them. Since a scraped IG profile owner is almost always the business itself, it falls back to a deterministic name (`_baseline_competitor_name`) when the LLM isn't confident.

Both paths also fill the linked `Competitor`'s **`state` + `municipality`** (city): the LLM returns them and `resolve_location()` (in `scrapers/__init__.py`) normalizes the state to its official Venezuelan name, falling back to a deterministic `parse_location()` of the scraped location field (`locationText` "Naguanagua, CA" → Carabobo; `locationName` "Valencia Estado Carabobo" → Carabobo). They're only ever **backfilled** (`backfill_competitor_location`) — never overwriting existing values.

Shared helpers in `scrapers/__init__.py`: the keyword `category` vocabulary + `classify_category()` (used by both the FB/IG deterministic layer and the IG LLM prompt's allowed-category list); the location helpers (`parse_location`, `normalize_state`, `resolve_location`, `backfill_competitor_location`, `VENEZUELA_STATES`); deterministic text extractors (`extract_lead_time`, `extract_promotions`, `detect_in_stock`) used by the website scraper; and the website-identity helpers (`prettify_site_name`, `is_marketplace_url`, backed by the `KNOWN_SITE_NAMES` / `MARKETPLACE_LABELS` dicts).

The website scraper's `_flatten_dataset_items()` normalises the AI actor's output, which can arrive as a flat list of product dicts, a single wrapper dict with a nested list (`items`/`data`/`results`/`products`/`extractedData`), or a dataset item that is itself a list.

**Each scraper module exposes three functions** so the work can be driven non-blocking from the frontend (poll-based progress) — Mercado Libre follows the same `start_*`/`finalize_*`/`scrape_*` shape, only searching by keywords instead of URLs:
- `start_<src>_run(urls, results_limit, …)` — kicks off the Apify run via `.start()` (non-blocking) and returns the run dict (`id`, `defaultDatasetId`).
- `finalize_<src>(dataset_id, …)` — reads the finished dataset, maps and bulk-creates the records (website also takes `urls` + `competitor_name` to resolve the `Competitor` FK).
- `scrape_<src>(…)` — the original **blocking** wrapper (start → `wait_for_finish()` → finalize), kept for the management commands / CLI.

Shared helpers live in `scrapers/__init__.py`: `get_client()` (validates `APIFY_API_KEY`) and `get_run_progress(run_id, dataset_id)` (read-only run status + dataset `itemCount`).

REST endpoints (`apps/competitor_market_data/views.py`, generic & dispatched by `<source>` ∈ `instagram|facebook|website|mercadolibre`; for `mercadolibre` the `urls` field carries the search terms). **All require role `ADMIN`** (`permission_classes = [IsAdmin]`):
- `POST /scrapers/<source>/start` — `{"urls": [...], "limit": N, "competitor_name": "…"}` → `{run_id, dataset_id, status}` (202).
- `GET  /scrapers/<source>/status?run_id=…&dataset_id=…` → `{status, items_scraped, is_terminal, succeeded}` (polled by the frontend).
- `POST /scrapers/<source>/finalize` — `{"dataset_id": "…", "urls": [...], "competitor_name": "…"}` → `{saved, results: [...]}` (serialized records for display).
- `GET /scrapers/<source>/data` — **historical** read of stored `CompetitorMarketData` for that source (the frontend's always-on "Datos recolectados" table). Query params: `page` (default 1), `page_size` (default 10, max 50), `min_price`, `max_price`, `state`, `municipality` (the last two filter on the linked `Competitor`, case-insensitive exact), `search` (icontains over product/competitor/category/promotions). Returns `{count, page, page_size, num_pages, results, available_states, available_municipalities}` — the two `available_*` lists feed the filter dropdowns. Each result also carries `price_usd`, `matched_product` and `is_in_stock`. `ScraperDataView`, `SOURCE_TAGS` maps the URL `source` → the `CompetitorMarketData.source` tag.
- `PATCH/DELETE /scrapers/<source>/data/<pk>` — **manual edit/delete** of a stored row by the admin (`ScraperDataDetailView`). PATCH accepts the editable attribute fields only (`product_name`, `category`, `price`, `currency`, `promotions`, `is_in_stock` — the competitor is an entity, not edited here) and **re-stamps `price_usd`** with the latest rate when price/currency change. The row must belong to `<source>`.
- `GET /scrapers/<source>/rejected` — lists the **discarded** rows (`RejectedMarketData`) for that source with their `rejection_reason` (`ScraperRejectedView`, paginated, `search`). These are the rows the quality gate rejected — kept out of the clean table but surfaced here so the admin can see what was dropped and why. `DELETE /scrapers/<source>/rejected/<pk>` removes one.
- `GET|POST /scrapers/llm/test` — **diagnostic** for the DeepSeek connection (ADMIN). Makes one real LLM call with static sample data (POST may override `{title, description, location}`) and returns `{ok, config, request, result, raw_content, usage, error}`. Unlike the scraper path, `deepseek.check_connection()` does **not** swallow the error — it surfaces the exception type/`status_code`/`body` (e.g. 402 *Insufficient Balance*) so the integration can be verified from Postman. 200 on success, 400 on config error, 502 on API failure.

### URL Structure

```
/admin/                       → Django admin
/api/auth/                    → JWT auth: login, refresh, logout, me (apps/accounts)
/api/products/                → ProductViewset CRUD (read = operativo, write = Manager+); `stock` is read-only (append-only inventory)
/api/categories               → read-only category list for the product form (operativo, unpaginated)
/api/customers/               → CustomerViewSet (read/create = Seller+, delete = Manager+)
/api/sales/                   → SaleViewSet (ver = operativo, registrar = Seller+); POST …/{id}/anular/ to void (Manager+)
/api/inventory/stock          → current stock summary per product (ver = operativo)
/api/inventory/movements/     → InventoryMovementViewSet: history (ver = operativo) + register ENT/AJU/DEV (Inventario+)
/api/exchange-rate/latest     → latest BCV/parallel rate, read-only (Seller+)
/scrapers/                    → <source>/start, <source>/status, <source>/finalize (ADMIN only)
```

All `/api/` list endpoints are paginated by DRF (`apps/core/pagination.StandardResultsSetPagination`,
`?page=`/`?page_size=`, default 10) — the APIView-based scraper endpoints keep their own pagination.
Note DRF action routes need the trailing slash: void a sale at `/api/sales/{id}/anular/`.

### Internal data entry (sales & inventory)

Registering a sale and controlling stock are the two **internal** data-entry modules (the scrapers
are the *external* one). Both keep their business logic in a `services.py` so the viewsets stay thin,
and both run inside a single `transaction.atomic` so a sale/movement never lands half-applied.

- **`apps/sales/services.create_sale`** — validates stock per line (locks the product rows with
  `select_for_update()`, stripping the model's default ordering via `.order_by()` so PostgreSQL allows
  `FOR UPDATE`), snapshots the unit cost from the product and the BCV/parallel rates from the latest
  `ExchangeRate`, computes subtotals/profit/`commission` (seller's `commission_rate` × profit) and
  `total_sale_ves` (parallel rate preferred), then decrements stock by writing one `InventoryMovement`
  (type `SAL`, negative qty) per line. The seller is resolved from the authenticated user's `Seller`
  profile (a Manager+ may register on behalf of another seller by passing `seller`).
- **`apps/sales/services.void_sale`** — reverses a sale: writes a `DEV` movement per line (returns the
  qty to stock) and sets status `ANU`. Gated to Manager+ via `@action(permission_classes=[IsManager])`.
- **`apps/inventory/services.apply_movement`** — the single chokepoint for stock mutation (append-only):
  locks the product, refuses to drive stock negative (`InsufficientStockError`), writes the
  `InventoryMovement` and updates `Product.stock`. Used by both the sales service and the manual
  movement endpoint. Manual movements only allow `ENT`/`AJU`/`DEV` (`SAL` is reserved for sales).

**Permissions (separation of duties):** the model is **not a single linear ladder** —
`ADMIN > MANAGER > {SELLER, WAREHOUSE} > VIEWER`, where `SELLER` (vendedor) and `WAREHOUSE`
(encargado de inventario) are siblings with **disjoint write capabilities**:

- **Registering a sale** is `SELLER`+ (`IsSeller`) — the warehouse role is deliberately excluded (it
  sees sales but doesn't make them). Sales *indirectly* decrement stock (the `SAL` movement), so a
  seller never writes stock directly.
- **Modifying stock** (manual `ENT`/`AJU`/`DEV` movements) is `WAREHOUSE`+ (`IsWarehouse`) — sellers are
  excluded (they only *read* stock to sell). The manager/admin can do both, as they sit above both.
- **Reading** the shared operational data (sales list, stock summary, movement history, product
  catalog) is any operational role (`IsOperational` = ADMIN/MANAGER/SELLER/WAREHOUSE), so a seller
  can view stock and a warehouse manager can view sales.
- **Voiding a sale** is `Manager`+ (`IsManager`), since it erases revenue and returns stock.
- Product/customer *writes* are Manager+; customer reads/creates are Seller+ (the sale form's quick-add).

The sale/inventory viewsets implement this per-action in `get_permissions()` (create vs anular vs the
read default).

### Key Design Decisions

**Dual-currency everywhere:** Venezuela's economy requires tracking both official BCV rate and parallel market rate. Sale, Quote, ProductPriceHistory, and ExchangeRate all carry both `bcv_rate` and `parallel_rate`. All prices are stored in USD; VES values are derived or stored alongside.

**Competitor normalization:** `CompetitorMarketData` has an optional FK to a normalized `Competitor` record *plus* a fallback `competitor_name` CharField. This handles scraped data that doesn't match any known competitor.

**Quote-to-sale conversion:** `Quote` has a nullable FK to `Sale`; status `CONVERTED` tracks the pipeline.

**Inventory is append-only:** Never mutate stock directly — always create an `InventoryMovement` with type `ENT/SAL/AJU/DEV`. `Product.stock` is the current value; movements are the audit trail.

**`apps/products` vs `apps/core`:** `apps/core` owns the `Product` model. `apps/products` is a thin REST API layer — serializer and viewset only. New model fields go in `apps/core/models.py`.

## Authentication & Roles

JWT auth via `djangorestframework-simplejwt`. DRF defaults to `JWTAuthentication` + `IsAuthenticated`, so **every endpoint requires a valid token** unless a view opts out with `AllowAny` (only `login` does).

**Roles & profile:** `apps/accounts/models.py` defines `Role` (ADMIN, MANAGER, SELLER, **WAREHOUSE**, VIEWER) on a `UserProfile` (OneToOne to `auth.User`). The roles are **not a strict linear hierarchy**: `WAREHOUSE` (encargado de inventario) is a sibling of `SELLER`, not a tier above/below it — see the separation-of-duties note above (sellers sell, warehouse manages stock, managers do both). **`UserProfile` is the source of truth for user data** — it holds `role`, `first_name`, `last_name`, `email`, `phone`; `auth_user` is kept for authentication only (username/password/permissions/dates). Django's `User` still physically has empty `first_name`/`last_name`/`email` columns (they can't be dropped without a custom user model), but they are intentionally unused — read/write personal data via the profile. A `post_save` signal (`signals.py`) auto-creates the profile (superusers → ADMIN, else VIEWER) and copies any personal data Django collected at creation (e.g. `createsuperuser`) into it. The Django admin hides the personal-info fieldset on the User form and edits those fields through the `UserProfile` inline. The role is embedded as a JWT claim and returned in the login response; `UserSerializer` sources name/email/phone from the profile.

**Permission classes** (`apps/accounts/permissions.py`, superusers always pass): `IsAdmin` and `IsManager` are cumulative tiers; the rest are **capability-based** to model the non-linear roles — `IsSeller` (ADMIN/MANAGER/SELLER = "can register sales", excludes warehouse), `IsWarehouse` (ADMIN/MANAGER/WAREHOUSE = "can modify stock", excludes sellers), `IsOperational` (ADMIN/MANAGER/SELLER/WAREHOUSE = shared read access), and `IsViewer` (any valid role, read-only). Apply per-viewset with `permission_classes`, or per-action via `get_permissions()`.

**Endpoints** (`/api/auth/`): `login`, `refresh`, `logout` (blacklists refresh token), `me`. Public sign-up is intentionally **not** implemented — only admins create users.

**Security config** (`settings.py`): 15-min access tokens, 7-day refresh with rotation + blacklist (`token_blacklist` app), `login` throttle at 10/min, CORS restricted to the Vite origin with credentials, and production-gated (`if not DEBUG`) SSL redirect / HSTS / secure cookies / `CSRF_TRUSTED_ORIGINS`. Passwords use Django's default PBKDF2 hasher; minimum length raised to 10.

To create the first admin: `python manage.py createsuperuser` (gets ADMIN role automatically).

## Logging

`settings.py` includes a `LOGGING` config that routes everything under `apps.*` to the console at INFO level. The root logger stays at WARNING to suppress Django/DRF noise. Scraper functions use `logger = logging.getLogger(__name__)` and emit INFO on dataset receipt/save counts and WARNING/ERROR on structural anomalies or failures.

## Environment

Requires a `.env` file in the project root:

```
DJANGO_SECRET_KEY=...
DB_NAME=predictive_system
DB_USER=postgres
DB_PASSWORD=...
DB_HOST=127.0.0.1
DB_PORT=5432
APIFY_API_KEY=...
```

Optional — LLM competitor enrichment for the Facebook scraper (all off/safe by default):

```
USE_LLM_ENRICHMENT=True            # master switch (default False)
DEEPSEEK_API_KEY=sk-...            # from https://platform.deepseek.com
DEEPSEEK_MODEL=deepseek-chat       # optional (default deepseek-chat)
DEEPSEEK_BASE_URL=https://api.deepseek.com   # optional
```

Requires `pip install openai` (already in `requirements.txt`). Read directly from the environment in `enrichment/deepseek.py` (same pattern as `APIFY_API_KEY`), not via `settings.py`.

Optional (have safe dev defaults): `DJANGO_DEBUG` (default `True`), `DJANGO_ALLOWED_HOSTS` (csv, default `127.0.0.1,localhost`), `CORS_ALLOWED_ORIGINS` (csv, default `http://localhost:5173,http://127.0.0.1:5173`). For production set `DJANGO_DEBUG=False`, a real `DJANGO_SECRET_KEY`, and the correct hosts/origins.

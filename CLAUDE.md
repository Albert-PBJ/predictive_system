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

# Run scrapers via CLI
python manage.py scrape_instagram <url1> <url2> --limit 50
python manage.py scrape_facebook_marketplace <url1> <url2> --limit 50
python manage.py scrape_website <url1> <url2> --limit 50 --competitor-name "Nombre Competidor"
```

There are no tests yet — placeholder `tests.py` files exist in some apps but are unpopulated.

This API has all of its comments as well as API responses in Spanish. Functions, variables and code practices can remain in English.

## Architecture

### Apps

| App | Purpose |
|-----|---------|
| `apps/accounts` | Auth & RBAC: UserProfile (role), JWT login/logout/refresh/me, role-based permissions |
| `apps/core` | Master data: Product, Category, Customer, Seller, ExchangeRate, ProductPriceHistory |
| `apps/sales` | Transactions: Sale, SaleItem, Quote, QuoteItem |
| `apps/inventory` | Audit trail: InventoryMovement (every stock change logged) |
| `apps/benchmarking` | Competitor intelligence: Competitor, CompetitorMarketData |
| `apps/analytics` | ML layer: PredictionLog (model registry), KPI, Alert |
| `apps/competitor_market_data` | Apify scraper integration + REST endpoints to trigger scrapers |
| `apps/products` | REST API (ModelViewSet) for Product CRUD — thin layer over `apps/core` models |

### Scraper Flow

`apps/competitor_market_data/scrapers/` holds the Apify integration. Three scrapers are available:

| Scraper | Actor | Source tag | Notes |
|---------|-------|-----------|-------|
| `instagram_scraper.py` | `apify/instagram-scraper` | `IG` | Captions are free text, so the deterministic layer (regex price/lead time/promotions + keyword `category`) is weak; **optionally enriched by an LLM** (see below) that cleans `product_name`, fills `category`, extracts promotions, recovers a fallback price, and resolves the `Competitor` FK **deduped by Instagram handle**. FK is null when the LLM is off |
| `facebook_marketplace_scraper.py` | `apify/facebook-marketplace-scraper` | `FB` | Maps structured listing fields (`listingPrice`, `listingTitle`, `itemUrl`, `isSold`/`isLive`); keyword-classifies `category`; **optionally resolves `Competitor` FK via an LLM** (see below). FK is null when the LLM is off or no seller is identifiable |
| `website_scraper.py` | `apify/ai-web-scraper` | `WEB` | AI prompt extracts `title`, `price`, `promotion`; **resolves `Competitor` FK via `get_or_create`** keyed on name or URL domain |

All three bulk-create `CompetitorMarketData` records and preserve the full Apify JSON in `raw_metadata`.

**Optional LLM enrichment (Facebook + Instagram):** `enrichment/deepseek.py` makes **one call per listing/post**, gated by the **same** `USE_LLM_ENRICHMENT` switch (+ `DEEPSEEK_API_KEY` + the `openai` package). It is **off by default** and fully optional: if any is missing or the API call fails, the deterministic mapping still saves every record. DeepSeek is OpenAI-API-compatible (the `openai` SDK is pointed at its `base_url`); only cleaned text fields are sent (never the raw JSON), and the model is told to return `null` rather than invent data. Both paths log an `ACTIVE`/`DISABLED` line and a final breakdown (items enriched, linked-to-existing vs new competitor, etc.).

- **Facebook** (`enrich_listing`): (a) identifies/normalizes the seller and links it to a `Competitor` — matching an existing one by id or creating a new one above a confidence threshold (`_MIN_CONFIDENCE`) — and (b) extracts promotions/benefits from the description (overriding the keyword baseline). FK resolution lives in `_resolve_competitor_fk`.
- **Instagram** (`enrich_instagram_post`): does **more**, because captions are unstructured — it extracts a clean `product_name`, picks a `category` from the shared controlled list, extracts promotions, recovers `price`/`currency` as a fallback only when the regex found none, and resolves the `Competitor`. **Competitor dedupe is keyed on the Instagram handle** (`ownerUsername`/profile URL, normalized by `_handle_from`): a post links to an existing `Competitor` whose `instagram` field matches the handle (within-run cache + cross-run index in `_resolve_instagram_competitor`), and newly created competitors are stored **with their handle** so future runs dedupe against them. Since a scraped IG profile owner is almost always the business itself, it falls back to a deterministic name (`_baseline_competitor_name`) when the LLM isn't confident.

Both paths also fill the linked `Competitor`'s **`state` + `municipality`** (city): the LLM returns them and `resolve_location()` (in `scrapers/__init__.py`) normalizes the state to its official Venezuelan name, falling back to a deterministic `parse_location()` of the scraped location field (`locationText` "Naguanagua, CA" → Carabobo; `locationName` "Valencia Estado Carabobo" → Carabobo). They're only ever **backfilled** (`backfill_competitor_location`) — never overwriting existing values.

Shared helpers in `scrapers/__init__.py`: the keyword `category` vocabulary + `classify_category()` (used by both the FB/IG deterministic layer and the IG LLM prompt's allowed-category list), and the location helpers (`parse_location`, `normalize_state`, `resolve_location`, `backfill_competitor_location`, `VENEZUELA_STATES`).

The website scraper's `_flatten_dataset_items()` normalises the AI actor's output, which can arrive as a flat list of product dicts, a single wrapper dict with a nested list (`items`/`data`/`results`/`products`/`extractedData`), or a dataset item that is itself a list.

**Each scraper module exposes three functions** so the work can be driven non-blocking from the frontend (poll-based progress):
- `start_<src>_run(urls, results_limit, …)` — kicks off the Apify run via `.start()` (non-blocking) and returns the run dict (`id`, `defaultDatasetId`).
- `finalize_<src>(dataset_id, …)` — reads the finished dataset, maps and bulk-creates the records (website also takes `urls` + `competitor_name` to resolve the `Competitor` FK).
- `scrape_<src>(…)` — the original **blocking** wrapper (start → `wait_for_finish()` → finalize), kept for the management commands / CLI.

Shared helpers live in `scrapers/__init__.py`: `get_client()` (validates `APIFY_API_KEY`) and `get_run_progress(run_id, dataset_id)` (read-only run status + dataset `itemCount`).

REST endpoints (`apps/competitor_market_data/views.py`, generic & dispatched by `<source>` ∈ `instagram|facebook|website`). **All require role `ADMIN`** (`permission_classes = [IsAdmin]`):
- `POST /scrapers/<source>/start` — `{"urls": [...], "limit": N, "competitor_name": "…"}` → `{run_id, dataset_id, status}` (202).
- `GET  /scrapers/<source>/status?run_id=…&dataset_id=…` → `{status, items_scraped, is_terminal, succeeded}` (polled by the frontend).
- `POST /scrapers/<source>/finalize` — `{"dataset_id": "…", "urls": [...], "competitor_name": "…"}` → `{saved, results: [...]}` (serialized records for display).
- `GET|POST /scrapers/llm/test` — **diagnostic** for the DeepSeek connection (ADMIN). Makes one real LLM call with static sample data (POST may override `{title, description, location}`) and returns `{ok, config, request, result, raw_content, usage, error}`. Unlike the scraper path, `deepseek.check_connection()` does **not** swallow the error — it surfaces the exception type/`status_code`/`body` (e.g. 402 *Insufficient Balance*) so the integration can be verified from Postman. 200 on success, 400 on config error, 502 on API failure.

### URL Structure

```
/admin/          → Django admin
/api/auth/       → JWT auth: login, refresh, logout, me (apps/accounts)
/api/products/   → ProductViewset (DRF DefaultRouter)
/scrapers/       → <source>/start, <source>/status, <source>/finalize (ADMIN only)
```

### Key Design Decisions

**Dual-currency everywhere:** Venezuela's economy requires tracking both official BCV rate and parallel market rate. Sale, Quote, ProductPriceHistory, and ExchangeRate all carry both `bcv_rate` and `parallel_rate`. All prices are stored in USD; VES values are derived or stored alongside.

**Competitor normalization:** `CompetitorMarketData` has an optional FK to a normalized `Competitor` record *plus* a fallback `competitor_name` CharField. This handles scraped data that doesn't match any known competitor.

**Quote-to-sale conversion:** `Quote` has a nullable FK to `Sale`; status `CONVERTED` tracks the pipeline.

**Inventory is append-only:** Never mutate stock directly — always create an `InventoryMovement` with type `ENT/SAL/AJU/DEV`. `Product.stock` is the current value; movements are the audit trail.

**`apps/products` vs `apps/core`:** `apps/core` owns the `Product` model. `apps/products` is a thin REST API layer — serializer and viewset only. New model fields go in `apps/core/models.py`.

## Authentication & Roles

JWT auth via `djangorestframework-simplejwt`. DRF defaults to `JWTAuthentication` + `IsAuthenticated`, so **every endpoint requires a valid token** unless a view opts out with `AllowAny` (only `login` does).

**Roles & profile:** `apps/accounts/models.py` defines `Role` (ADMIN, MANAGER, SELLER, VIEWER) on a `UserProfile` (OneToOne to `auth.User`). **`UserProfile` is the source of truth for user data** — it holds `role`, `first_name`, `last_name`, `email`, `phone`; `auth_user` is kept for authentication only (username/password/permissions/dates). Django's `User` still physically has empty `first_name`/`last_name`/`email` columns (they can't be dropped without a custom user model), but they are intentionally unused — read/write personal data via the profile. A `post_save` signal (`signals.py`) auto-creates the profile (superusers → ADMIN, else VIEWER) and copies any personal data Django collected at creation (e.g. `createsuperuser`) into it. The Django admin hides the personal-info fieldset on the User form and edits those fields through the `UserProfile` inline. The role is embedded as a JWT claim and returned in the login response; `UserSerializer` sources name/email/phone from the profile.

**Permission classes** (`apps/accounts/permissions.py`): `IsAdmin`, `IsManager`, `IsSeller`, `IsViewer` (cumulative; superusers always pass). Apply per-viewset with `permission_classes`.

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

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
| `apps/core` | Master data: Product, Category, Customer, Seller, ExchangeRate, ProductPriceHistory |
| `apps/sales` | Transactions: Sale, SaleItem, Quote, QuoteItem |
| `apps/inventory` | Audit trail: InventoryMovement (every stock change logged) |
| `apps/benchmarking` | Competitor intelligence: Competitor, CompetitorMarketData |
| `apps/analytics` | ML layer: PredictionLog (model registry), KPI, Alert |
| `apps/competitor_market_data` | Apify scraper integration + REST endpoints to trigger scrapers |
| `apps/products` | REST API (ModelViewSet) for Product CRUD — thin layer over `apps/core` models |

### Scraper Flow

`apps/competitor_market_data/scrapers/` holds the Apify integration. Scrapers call Apify actors (`apify/instagram-scraper`, `apify/facebook-marketplace-scraper`), extract structured fields via regex (price, currency, lead time, promotions, stock status), and bulk-create `CompetitorMarketData` records. The full Apify JSON is preserved in `raw_metadata` for debugging and ML training.

REST endpoints:
- `POST /scrapers/instagram/start` — accepts `{"urls": [...], "limit": N}`
- `POST /scrapers/facebook/start` — accepts `{"urls": [...], "limit": N}`
- `POST /scrapers/website/start` — accepts `{"urls": [...], "limit": N, "competitor_name": "..."}` (uses `apify/ai-web-scraper`; resolves `Competitor` FK via get_or_create)

### URL Structure

```
/admin/          → Django admin
/api/products/   → ProductViewset (DRF DefaultRouter)
/scrapers/       → Instagram, Facebook & website scraper endpoints
```

### Key Design Decisions

**Dual-currency everywhere:** Venezuela's economy requires tracking both official BCV rate and parallel market rate. Sale, Quote, ProductPriceHistory, and ExchangeRate all carry both `bcv_rate` and `parallel_rate`. All prices are stored in USD; VES values are derived or stored alongside.

**Competitor normalization:** `CompetitorMarketData` has an optional FK to a normalized `Competitor` record *plus* a fallback `competitor_name` CharField. This handles scraped data that doesn't match any known competitor.

**Quote-to-sale conversion:** `Quote` has a nullable FK to `Sale`; status `CONVERTED` tracks the pipeline.

**Inventory is append-only:** Never mutate stock directly — always create an `InventoryMovement` with type `ENT/SAL/AJU/DEV`. `Product.stock` is the current value; movements are the audit trail.

**`apps/products` vs `apps/core`:** `apps/core` owns the `Product` model. `apps/products` is a thin REST API layer — serializer and viewset only. New model fields go in `apps/core/models.py`.

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

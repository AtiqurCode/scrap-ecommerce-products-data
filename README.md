# Cartup scraper

Scrape Cartup category / shop / product pages straight into a **MySQL `products` table**.

## Database setup

Copy `.env.example` to `.env` and fill in your MySQL credentials:

```
DB_CONNECTION=mysql
DB_HOST=127.0.0.1
DB_PORT=3306
DB_DATABASE=scrap_ecommerce
DB_USERNAME=root
DB_PASSWORD=
```

The database and the `products` table are created automatically on first run if they don't exist yet — no migration step needed. `url` is the unique key, so a product can never end up duplicated in the table. By default, a product already saved is left alone on a later run (same as the old CSV skipping already-scraped URLs) — pass `--refresh` if you want to re-fetch and update it instead.

## Docker

One container runs both the API and the built UI — that's the whole point of `server.py` serving `web/dist` itself, no separate frontend server needed.

```bash
cp .env.example .env   # first time only; fill in your real DB credentials
docker compose up --build
```

Open http://localhost:8088. Ctrl+C stops it; `docker compose up -d` runs it detached. (Port `8088`, not the more common `8000`/`8080`, specifically to stay out of the way of other projects on the same machine — change the `"8088:8088"` mapping in `docker-compose.yml` if you need a different one.)

By default this connects to the **MySQL already running on your machine** (`host.docker.internal`, mapped to your host via `extra_hosts` in `docker-compose.yml`) — your existing data stays exactly where it is, nothing to migrate. Only `DB_HOST` is forced to `host.docker.internal`; every other `DB_*` value comes from your `.env` same as running it directly.

If the container can't reach MySQL, check in this order:

1. **Is MySQL actually running?** `docker exec <container> uv run python -c "from scrap_ecommerce import db; db.get_connection()"` gives the real error. "Connection refused" means nothing is listening — start your MySQL server, same as you'd need to for running this outside Docker.
2. **`bind-address`**: if your MySQL config binds to `127.0.0.1` only, it refuses connections from anywhere but true loopback — `host.docker.internal` doesn't count as loopback from MySQL's point of view, even though it reaches your machine correctly. Bind it to `0.0.0.0` (or your LAN interface) instead.
3. **User host grants**: `'root'@'localhost'` (MySQL's common default) only matches connections that claim to be from `localhost` — a container connects as a different apparent host. You may need `CREATE USER 'root'@'%' IDENTIFIED BY '...'; GRANT ALL ON scrap_ecommerce.* TO 'root'@'%'; FLUSH PRIVILEGES;` (adjust user/password to match your setup).

(One thing you *won't* hit: some Docker setups map `host.docker.internal` to both an IPv4 and a non-routable IPv6 address, which would otherwise surface as a confusing "Network is unreachable" — the app resolves to IPv4 explicitly before connecting, so that's handled already.)

Rebuild after changing Python or frontend source (`docker compose build`); no rebuild needed for `.env` changes, just `docker compose up` again.

## UI

React app in `web/`. Terminal 1 starts the API, terminal 2 starts the UI:

```bash
uv run scrape-ui
```

```bash
cd web
npm install
npm run dev
```

Open http://localhost:5173 — paste one or more Cartup URLs (one per line), click **Generate**. Categories run one after another in the same job and save straight to MySQL as they go. Each category gets its own report card the moment it finishes — how many products were new, how many were updated, how many were already saved and skipped — and a CSV of everything this job saved is available to download once rows start landing.

`Listing only` is on by default (faster). Uncheck it to fetch full product-page details.

```bash
cd web && npm run build
```

Then `uv run scrape-ui` also serves the built UI at http://127.0.0.1:8088 (override with the `PORT` env var, e.g. `PORT=9000 uv run scrape-ui`).

## Scrape

Pass a Cartup URL:

```bash
uv run python scrape.py "https://cartup.com/category/sports__outdoors"
```

No URL → it prompts:

```bash
uv run python scrape.py
```

Multiple URLs run one after another in the order given, all deduped by product URL against the same `products` table (a product already saved by an earlier category in the list is skipped when a later category also finds it). Each category prints its own new/updated/skipped counts as it finishes, then a combined total at the end:

```bash
uv run python scrape.py "https://cartup.com/category/laptops" "https://cartup.com/category/gaming_laptops"
```

Supported URLs:

- `https://cartup.com/category/{slug}`
- `https://cartup.com/shop/{slug}`
- `https://cartup.com/product/{slug}`

## Output

Every scraped product is saved into the `products` table (flattened columns — name, price, original price, discount, brand, shop, sku, stock, rating, description, images, … — plus a `product_json` column with the full product object).

By default, products already in the table are skipped — not re-fetched, not touched — same behavior as the old CSV's "already-scraped URLs are skipped." The one difference: the CSV started a fresh file every day, so everything naturally got re-scraped the next day; the `products` table is permanent, so a skipped product stays as-is forever unless you ask otherwise. Pass `--refresh` to re-fetch and update products you already have (price/stock changes, etc.) instead of skipping them.

Ctrl+C is safe: whatever's already been saved stays saved (each product is committed to MySQL as soon as it's fetched, nothing is buffered and lost).

Full category scrapes are slow if you fetch every product page. Faster:

```bash
# listings only (no product-page HTML) — minutes, not an hour
uv run python scrape.py "https://cartup.com/category/sports__outdoors" --listing-only --workers 16
```

The site caps listing pages at 30 items. The scraper patches that in, clicks Load More, and strips old cards so Chrome does not die around 6k items. Product details still mean one HTML request per product; `--workers 16` is the default.

For big categories (10k-30k+ items), listing collection auto-speeds-up: after the page loads and one Load More click, it diffs those two real API requests to learn the pagination parameter, then fetches the rest of the pages concurrently over plain HTTP instead of clicking through them one at a time in the browser. If anything about that doesn't check out, it silently falls back to clicking through Load More as before — same result either way, just faster when it works. Watch for a `fast listing: N more pages via API in parallel` line in the console.

## Flags

```bash
uv run python scrape.py "https://cartup.com/category/sports__outdoors" --listing-only
uv run python scrape.py "https://cartup.com/category/sports__outdoors" --max-products 50
uv run python scrape.py "https://cartup.com/category/sports__outdoors" --workers 8 --delay 0.08
uv run python scrape.py "https://cartup.com/category/sports__outdoors" --headful
uv run python scrape.py "https://cartup.com/category/sports__outdoors" --refresh
```

| Flag | Default | |
| --- | --- | --- |
| `--listing-only` | off | Skip product-page details (faster, fewer columns) |
| `--max-products N` | all | Stop after N products |
| `--workers` | `16` | Parallel product-page fetches |
| `--delay` | `0.04` | Seconds between product-page requests |
| `--rows-per-page` | `30` | Listing page size (Cartup caps at 30) |
| `--headful` | off | Show Chrome while collecting listings |
| `--refresh` | off | Re-fetch and update products already saved instead of skipping them |

Category pages with ~16k items take a while: listings first (Load More), then one request per product page.


### Category URLS are

- https://cartup.com/category/men_530
- https://cartup.com/category/computers__laptops
- https://cartup.com/category/furniture
- https://cartup.com/category/Groceries_20251127074323
- https://cartup.com/category/health__beauty
- https://cartup.com/category/women_911
- https://cartup.com/category/home_appliances
- https://cartup.com/category/stationery__craft
- https://cartup.com/category/mobile_accessories
- https://cartup.com/category/watches
- https://cartup.com/category/sports__outdoors
- https://cartup.com/category/mother__baby
- https://cartup.com/category/motors
- https://cartup.com/category/mobiles__tablets
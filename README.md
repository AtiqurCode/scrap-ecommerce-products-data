# Cartup scraper

Scrape Cartup category / shop / product pages into a **same-date CSV**.

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

Open http://localhost:5173 — paste a Cartup URL, click **Generate**, watch products complete one by one. CSV downloads when the job finishes.

`Listing only` is on by default (faster). Uncheck it to fetch full product-page details.

```bash
cd web && npm run build
```

Then `uv run scrape-ui` also serves the built UI at http://127.0.0.1:8000.

## Scrape

Pass a Cartup URL:

```bash
uv run python scrape.py "https://cartup.com/category/computers__laptops"
```

No URL → it prompts:

```bash
uv run python scrape.py
```

Multiple URLs (same day = same CSV, deduped by product URL):

```bash
uv run python scrape.py "https://cartup.com/category/laptops" "https://cartup.com/category/gaming_laptops"
```

Supported URLs:

- `https://cartup.com/category/{slug}`
- `https://cartup.com/shop/{slug}`
- `https://cartup.com/product/{slug}`

## Output

Written to `data/` (override with `-o`):

`data/cartup_YYYY-MM-DD.csv`

Flattened columns (name, price, original price, discount, brand, shop, sku, stock, rating, description, images, …) plus a `product_json` column with the full product object.

Re-run the same day: already-scraped URLs are skipped.

Ctrl+C flushes whatever is already saved.

Full category scrapes are slow if you fetch every product page. Faster:

```bash
# listings only (no product-page HTML) — minutes, not an hour
uv run python scrape.py "https://cartup.com/category/computers__laptops" --listing-only --workers 16
```

The site caps listing pages at 30 items. The scraper patches that in, clicks Load More, and strips old cards so Chrome does not die around 6k items. Product details still mean one HTML request per product; `--workers 16` is the default.

For big categories (10k-30k+ items), listing collection auto-speeds-up: after the page loads and one Load More click, it diffs those two real API requests to learn the pagination parameter, then fetches the rest of the pages concurrently over plain HTTP instead of clicking through them one at a time in the browser. If anything about that doesn't check out, it silently falls back to clicking through Load More as before — same result either way, just faster when it works. Watch for a `fast listing: N more pages via API in parallel` line in the console.

## Flags

```bash
uv run python scrape.py "https://cartup.com/category/computers__laptops" --listing-only
uv run python scrape.py "https://cartup.com/category/computers__laptops" --max-products 50
uv run python scrape.py "https://cartup.com/category/computers__laptops" --workers 8 --delay 0.08
uv run python scrape.py "https://cartup.com/category/computers__laptops" --headful
uv run python scrape.py "https://cartup.com/category/computers__laptops" -o data
```

| Flag | Default | |
| --- | --- | --- |
| `-o`, `--out-dir` | `data` | Output folder |
| `--listing-only` | off | Skip product-page details (faster, fewer columns) |
| `--max-products N` | all | Stop after N products |
| `--workers` | `16` | Parallel product-page fetches |
| `--delay` | `0.04` | Seconds between product-page requests |
| `--rows-per-page` | `30` | Listing page size (Cartup caps at 30) |
| `--headful` | off | Show Chrome while collecting listings |

Category pages with ~16k items take a while: listings first (Load More), then one request per product page.

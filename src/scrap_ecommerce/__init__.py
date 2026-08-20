from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scrap_ecommerce.scraper import CartupScraper


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape Cartup category/shop/product URLs into a same-date CSV."
    )
    parser.add_argument("urls", nargs="*", help="Cartup /category/, /shop/, or /product/ URLs")
    parser.add_argument("-o", "--out-dir", default="data", help="Output folder (default: data)")
    parser.add_argument("--max-products", type=int, default=None, help="Stop after N products (debug)")
    parser.add_argument("--listing-only", action="store_true", help="Skip product-page details")
    parser.add_argument("--workers", type=int, default=16, help="PDP fetch workers")
    parser.add_argument("--delay", type=float, default=0.04, help="Delay between requests (seconds)")
    parser.add_argument("--rows-per-page", type=int, default=30)
    parser.add_argument("--headful", action="store_true", help="Show Chrome while collecting listings")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    urls = list(args.urls)
    if not urls:
        raw = input("Cartup URL: ").strip()
        if not raw:
            parser.error("Need at least one URL")
        urls = [raw]

    scraper = CartupScraper(
        out_dir=Path(args.out_dir),
        workers=args.workers,
        delay=args.delay,
        rows_per_page=args.rows_per_page,
        max_products=args.max_products,
        listing_only=args.listing_only,
        headful=args.headful,
    )
    try:
        for url in urls:
            scraper.scrape_url(url)
    finally:
        scraper.close()
    print(f"\nDone.\nCSV : {scraper.csv_path}")


if __name__ == "__main__":
    main()

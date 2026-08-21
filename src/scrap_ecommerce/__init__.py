from __future__ import annotations

import argparse
import sys

from scrap_ecommerce.scraper import CartupScraper


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape Cartup category/shop/product URLs into the `products` MySQL table."
    )
    parser.add_argument("urls", nargs="*", help="Cartup /category/, /shop/, or /product/ URLs")
    parser.add_argument("--max-products", type=int, default=None, help="Stop after N products (debug)")
    parser.add_argument("--listing-only", action="store_true", help="Skip product-page details")
    parser.add_argument("--workers", type=int, default=16, help="PDP fetch workers")
    parser.add_argument("--delay", type=float, default=0.04, help="Delay between requests (seconds)")
    parser.add_argument("--rows-per-page", type=int, default=30)
    parser.add_argument("--headful", action="store_true", help="Show Chrome while collecting listings")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-fetch and update products already saved in the database (default: skip them, like the old CSV did each day)",
    )
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
        workers=args.workers,
        delay=args.delay,
        rows_per_page=args.rows_per_page,
        max_products=args.max_products,
        listing_only=args.listing_only,
        headful=args.headful,
        skip_existing=not args.refresh,
    )
    try:
        result = scraper.scrape_urls(urls)
    finally:
        scraper.close()

    totals = result["totals"]
    print(f"\nDone — {len(urls)} categor{'y' if len(urls) == 1 else 'ies'} processed.")
    print(f"  {totals['new']} new, {totals['updated']} updated, {totals['skipped']} skipped")
    for report in result["reports"]:
        if report.get("error"):
            print(f"  ✗ {report['url']}: failed — {report['error']}")
        else:
            print(
                f"  ✓ {report['url']}: {report['new']} new, {report['updated']} updated, "
                f"{report['skipped']} skipped"
            )


if __name__ == "__main__":
    main()

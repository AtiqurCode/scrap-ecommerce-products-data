from __future__ import annotations

import csv
import http.client
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

SITE = "https://cartup.com"
API_HOST = "https://api.cartup.com"
IMAGE_CDN = "https://sl-dev-s3.s3.amazonaws.com/product/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
LISTING_PAGE_CAP = 30
_MAX_REDIRECTS = 5
_thread_local = threading.local()

PRUNE_CARDS_JS = """
() => {
  const grid = document.querySelector('div.grid.grid-cols-2, div.grid');
  if (!grid) return 0;
  const cards = [...grid.querySelectorAll('a[href*="/product/"]')];
  const keep = 12;
  let removed = 0;
  cards.slice(0, Math.max(0, cards.length - keep)).forEach((a) => {
    const node = a.parentElement || a;
    node.remove();
    removed += 1;
  });
  return removed;
}
"""

LISTING_API_HINTS = (
    "get-category-slug-wise",
    "get-seller-slug-wise",
    "get-virtual-shop-all-products",
    "get-category-wise",
)

PREFERRED_COLUMNS = [
    "scraped_at",
    "source_url",
    "page_type",
    "category_slug",
    "shop_slug",
    "url",
    "product_id",
    "variant_id",
    "name",
    "slug",
    "sku",
    "shop_sku",
    "seller_sku",
    "brand",
    "brand_id",
    "category",
    "category_id",
    "seller_name",
    "seller_id",
    "shop_name",
    "shop_url",
    "price",
    "original_price",
    "discount_percentage",
    "currency",
    "stock",
    "availability",
    "rating",
    "rating_count",
    "warranty",
    "warranty_period",
    "return_option",
    "free_shipping",
    "is_cartup_fast",
    "is_best_seller",
    "highlight",
    "box_items",
    "description",
    "images",
    "thumbnail",
    "video_url",
    "package_weight_kg",
    "package_height_cm",
    "package_width_cm",
    "package_length_cm",
    "variants_json",
    "attributes_json",
    "product_json",
]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def abs_url(url: str) -> str:
    if not url:
        return ""
    return urljoin(SITE + "/", url)


def abs_image(value: str | None) -> str:
    if not value:
        return ""
    if isinstance(value, dict):
        value = value.get("url") or value.get("image") or ""
    value = str(value).strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return IMAGE_CDN + value.lstrip("/")


def parse_target(url: str) -> dict[str, str]:
    parsed = urlparse(url.strip())
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        raise ValueError(f"Not a Cartup listing/product URL: {url}")
    kind = parts[0].lower()
    slug = parts[1] if len(parts) > 1 else ""
    if kind not in {"category", "product", "shop"} or not slug:
        raise ValueError(f"Give a /category/, /shop/, or /product/ URL, got: {url}")
    clean = f"{SITE}/{kind}/{slug}"
    return {"kind": kind, "slug": slug, "url": clean, "original": url.strip()}


def extract_json_object(text: str, start: int) -> str | None:
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def decode_next_f(html: str) -> str:
    parts: list[str] = []
    needle = "self.__next_f.push([1,"
    cursor = 0
    while True:
        i = html.find(needle, cursor)
        if i < 0:
            break
        j = i + len(needle)
        while j < len(html) and html[j] in " \n\r\t":
            j += 1
        if j >= len(html) or html[j] != '"':
            cursor = j + 1
            continue
        k = j + 1
        esc = False
        while k < len(html):
            ch = html[k]
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                raw = html[j : k + 1]
                try:
                    parts.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
                break
            k += 1
        cursor = k + 1
    return "".join(parts)


def parse_jsonld_products(html: str) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    for m in re.finditer(
        r"self\.__next_s=self\.__next_s\|\|\[\]\)\.push\(\[0,(\{.*?\})\]\)",
        html,
    ):
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        children = obj.get("children")
        if obj.get("type") != "application/ld+json" or not isinstance(children, str):
            continue
        try:
            data = json.loads(children)
        except json.JSONDecodeError:
            continue
        if data.get("@type") == "Product":
            products.append(data)
        elif data.get("@type") == "ItemList":
            for el in data.get("itemListElement") or []:
                item = el.get("item") if isinstance(el, dict) else None
                if isinstance(item, dict) and item.get("@type") == "Product":
                    products.append(item)
    return products


def extract_pdp_payload(html: str) -> dict[str, Any]:
    payload = decode_next_f(html)
    product: dict[str, Any] | None = None
    content: dict[str, Any] | None = None
    marker = '"product":{'
    idx = 0
    while True:
        i = payload.find(marker, idx)
        if i < 0:
            break
        raw = extract_json_object(payload, i + len('"product":'))
        idx = i + 1
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("slug") or obj.get("productVariantsData") or obj.get("shopName"):
            product = obj
            after = i + len('"product":') + len(raw)
            c_marker = '"productContentDetails":'
            c_i = payload.find(c_marker, after, after + 800)
            if c_i >= 0:
                brace = payload.find("{", c_i)
                c_raw = extract_json_object(payload, brace) if brace >= 0 else None
                if c_raw:
                    try:
                        content = json.loads(c_raw)
                    except json.JSONDecodeError:
                        pass
            break
    jsonld = parse_jsonld_products(html)
    return {
        "product": product or {},
        "content": content or {},
        "jsonld": jsonld[0] if jsonld else {},
    }


def listing_items_from_api(payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(payload, dict):
        return [], {}
    data = payload.get("data") if isinstance(payload.get("data"), (dict, list)) else payload
    page_info: dict[str, Any] = {}
    items: Any = None
    if isinstance(data, dict):
        page_info = data.get("pageInfo") or data.get("pagination") or {}
        for key in (
            "products",
            "items",
            "list",
            "productStocks",
            "records",
            "result",
            "rows",
        ):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        if items is None and isinstance(data.get("data"), list):
            items = data["data"]
    elif isinstance(data, list):
        items = data
    return [x for x in (items or []) if isinstance(x, dict)], page_info if isinstance(page_info, dict) else {}


def total_from_html(html: str) -> int | None:
    m = re.search(r"([\d,]+)(?:<!-- -->)?\s*(?:<!-- -->)?\s*items found", html, re.I)
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


def product_url_from_item(item: dict[str, Any]) -> str:
    for key in ("url", "productUrl", "slug", "productSlug"):
        val = item.get(key)
        if not val:
            continue
        val = str(val)
        if val.startswith("http"):
            return val.split("?")[0]
        if val.startswith("/product/"):
            return abs_url(val)
        return abs_url("/product/" + val.lstrip("/"))
    return ""


def first(*vals: Any) -> Any:
    for val in vals:
        if val not in (None, "", [], {}):
            return val
    return ""


def is_rsc_ref(val: Any) -> bool:
    return isinstance(val, str) and re.fullmatch(r"\$\d+", val) is not None


def jsonld_offer(jsonld: dict[str, Any]) -> dict[str, Any]:
    offer = jsonld.get("offers") or {}
    if isinstance(offer, list):
        offer = offer[0] if offer else {}
    return offer if isinstance(offer, dict) else {}


def jsonld_rating(jsonld: dict[str, Any]) -> dict[str, Any]:
    rating = jsonld.get("aggregateRating") or {}
    return rating if isinstance(rating, dict) else {}


def jsonld_brand(jsonld: dict[str, Any]) -> str:
    brand = jsonld.get("brand")
    if isinstance(brand, dict):
        return str(brand.get("name") or "")
    return str(brand or "")


def default_variant(product: dict[str, Any]) -> dict[str, Any]:
    variants = product.get("productVariantsData") or []
    if not isinstance(variants, list):
        return {}
    for var in variants:
        if isinstance(var, dict) and var.get("isDefault"):
            return var
    return variants[0] if variants and isinstance(variants[0], dict) else {}


def images_of(product: dict[str, Any], jsonld: dict[str, Any]) -> list[str]:
    raw = product.get("images") or jsonld.get("image") or []
    if isinstance(raw, str):
        raw = [raw]
    urls = [abs_image(x) for x in raw if x]
    return [u for u in urls if u]


def build_row(
    *,
    source_url: str,
    page_type: str,
    category_slug: str,
    shop_slug: str,
    listing: dict[str, Any],
    pdp: dict[str, Any],
) -> dict[str, Any]:
    product = pdp.get("product") or {}
    content = pdp.get("content") or {}
    jsonld = pdp.get("jsonld") or {}
    variant = default_variant(product)
    offer = jsonld_offer(jsonld)
    rating = jsonld_rating(jsonld)
    seller = offer.get("seller") if isinstance(offer.get("seller"), dict) else {}
    service = product.get("service") if isinstance(product.get("service"), dict) else {}
    sold_by = product.get("soldBy") if isinstance(product.get("soldBy"), dict) else {}

    slug = first(product.get("slug"), listing.get("slug"), listing.get("productSlug"))
    url = first(
        jsonld.get("url"),
        product_url_from_item(listing),
        abs_url(f"/product/{slug}") if slug else "",
    )
    images = images_of(product, jsonld)
    description = first(jsonld.get("description"), content.get("description"), product.get("description"))
    highlight = first(content.get("highlight"), product.get("highlight"))
    box_items = first(content.get("boxItems"), product.get("boxItems"))
    if is_rsc_ref(description):
        description = first(jsonld.get("description"), "")
    if is_rsc_ref(highlight):
        highlight = ""
    if is_rsc_ref(box_items):
        box_items = ""

    shop = first(
        product.get("shopName"),
        sold_by.get("soldShopName"),
        listing.get("shopName"),
        seller.get("name"),
    )
    shop_slug_val = first(product.get("sellerSlug"), shop_slug, listing.get("sellerSlug"))

    row: dict[str, Any] = {
        "scraped_at": now_iso(),
        "source_url": source_url,
        "page_type": page_type,
        "category_slug": category_slug,
        "shop_slug": shop_slug_val,
        "url": url,
        "product_id": first(product.get("id"), listing.get("id"), listing.get("productId")),
        "variant_id": first(variant.get("id"), listing.get("variantId")),
        "name": first(product.get("name"), jsonld.get("name"), listing.get("name"), listing.get("productName")),
        "slug": slug,
        "sku": first(jsonld.get("sku"), variant.get("shopSKU"), listing.get("sku")),
        "shop_sku": first(variant.get("shopSKU"), listing.get("shopSKU")),
        "seller_sku": first(variant.get("sellerSKU"), listing.get("sellerSKU")),
        "brand": first(product.get("brandName"), jsonld_brand(jsonld), listing.get("brandName")),
        "brand_id": first(product.get("brandId"), listing.get("brandId")),
        "category": first(product.get("categoryName"), jsonld.get("category"), listing.get("categoryName")),
        "category_id": first(product.get("categoryId"), listing.get("categoryId")),
        "seller_name": first(product.get("sellerName"), listing.get("sellerName")),
        "seller_id": first(product.get("sellerId"), listing.get("sellerId")),
        "shop_name": shop,
        "shop_url": abs_url(f"/shop/{shop_slug_val}") if shop_slug_val else "",
        "price": first(
            variant.get("discountedPrice"),
            offer.get("price"),
            listing.get("discountedPrice"),
            listing.get("price"),
            product.get("discountedPrice"),
        ),
        "original_price": first(
            variant.get("price"),
            listing.get("price"),
            listing.get("originalPrice"),
            listing.get("regularPrice"),
        ),
        "discount_percentage": first(
            variant.get("discountPercentage"),
            listing.get("discountPercentage"),
            listing.get("discount"),
        ),
        "currency": first(offer.get("priceCurrency"), "BDT"),
        "stock": first(
            variant.get("currentStockQty"),
            variant.get("totalStockQty"),
            product.get("totalStockQuantity"),
            listing.get("stock"),
            listing.get("currentStockQty"),
        ),
        "availability": first(offer.get("availability"), ""),
        "rating": first(
            rating.get("ratingValue"),
            listing.get("ratings"),
            listing.get("rating"),
            product.get("ratings"),
        ),
        "rating_count": first(
            rating.get("ratingCount"),
            listing.get("totalRating"),
            product.get("totalRating"),
        ),
        "warranty": first(product.get("warrantyName"), service.get("warrantyType")),
        "warranty_period": first(product.get("warrantyPeriodName"), service.get("warrantyPeriod")),
        "return_option": first(service.get("returnOption")),
        "free_shipping": first(product.get("isFreeShippingApplied"), listing.get("isFreeShippingApplied")),
        "is_cartup_fast": first(product.get("isCartUpFast"), listing.get("isCartUpFast")),
        "is_best_seller": first(product.get("isBestSeller"), listing.get("isBestSeller")),
        "highlight": highlight,
        "box_items": box_items,
        "description": description,
        "images": " | ".join(images),
        "thumbnail": abs_image(first(product.get("thumbnail"), listing.get("thumbnail"), listing.get("image"))),
        "video_url": first(product.get("videoUrl")),
        "package_weight_kg": product.get("packageWeightInKg"),
        "package_height_cm": product.get("packageHeightInCm"),
        "package_width_cm": product.get("packageWidthInCm"),
        "package_length_cm": product.get("packageLengthInCm"),
        "variants_json": json.dumps(product.get("productVariantsData") or listing.get("productVariantsData") or [], ensure_ascii=False),
        "attributes_json": json.dumps(product.get("productAttributesData") or listing.get("productAttributesData") or [], ensure_ascii=False),
        "product_json": json.dumps(product_payload(product, content, listing), ensure_ascii=False),
    }
    return row


def product_payload(product: dict[str, Any], content: dict[str, Any], listing: dict[str, Any]) -> dict[str, Any]:
    payload = dict(product) if product else dict(listing)
    if content:
        payload["productContentDetails"] = content
    return payload


def _thread_connections() -> dict[str, http.client.HTTPSConnection]:
    conns = getattr(_thread_local, "conns", None)
    if conns is None:
        conns = {}
        _thread_local.conns = conns
    return conns


def _request_keepalive(host: str, path: str, timeout: int) -> tuple[int, dict[str, str], bytes]:
    """GET over a persistent, per-thread HTTPS connection so repeated requests to the
    same host (e.g. fetching hundreds of product pages) reuse one TCP/TLS handshake
    instead of paying its cost on every call."""
    conns = _thread_connections()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }
    for attempt in range(2):
        conn = conns.get(host)
        if conn is None:
            conn = http.client.HTTPSConnection(host, timeout=timeout)
            conns[host] = conn
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            conns.pop(host, None)
            if attempt == 1:
                raise


def _request_replay(url: str, headers: dict[str, str], timeout: int = 30) -> tuple[int, bytes]:
    """Like _request_keepalive, but replays a caller-supplied header set (used to
    replay a real browser request captured for the listing API) instead of the fixed
    HTML-fetch headers."""
    conns = _thread_connections()
    parsed = urlparse(url)
    path = urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
    req_headers = dict(headers)
    req_headers["Host"] = parsed.netloc
    req_headers.setdefault("Connection", "keep-alive")
    for attempt in range(2):
        conn = conns.get(parsed.netloc)
        if conn is None:
            conn = http.client.HTTPSConnection(parsed.netloc, timeout=timeout)
            conns[parsed.netloc] = conn
        try:
            conn.request("GET", path, headers=req_headers)
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, body
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            conns.pop(parsed.netloc, None)
            if attempt == 1:
                raise


def fetch_listing_page(url: str, headers: dict[str, str]) -> Any:
    status, body = _request_replay(url, headers)
    if status != 200:
        raise RuntimeError(f"HTTP {status} for {url}")
    return json.loads(body.decode("utf-8", errors="replace"))


def http_get(url: str, timeout: int = 40) -> str:
    for _ in range(_MAX_REDIRECTS):
        parsed = urlparse(url)
        path = urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
        status, headers, body = _request_keepalive(parsed.netloc, path, timeout)
        if status in (301, 302, 303, 307, 308):
            location = headers.get("location")
            if not location:
                break
            url = urljoin(url, location)
            continue
        return body.decode("utf-8", errors="replace")
    raise RuntimeError(f"Too many redirects for {url}")


def fetch_pdp(url: str, retries: int = 3) -> dict[str, Any]:
    last_err = ""
    for attempt in range(retries):
        try:
            html = http_get(url)
            payload = extract_pdp_payload(html)
            payload["url"] = url
            payload["html_len"] = len(html)
            return payload
        except Exception as exc:
            last_err = str(exc)
            time.sleep(1.2 * (attempt + 1) + random.random())
    return {"product": {}, "content": {}, "jsonld": {}, "url": url, "error": last_err}


def with_query(url: str, **params: Any) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, val in params.items():
        if val is None:
            continue
        query[key] = str(val)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _sanitize_replay_headers(headers: dict[str, str]) -> dict[str, str]:
    """Strip headers that don't make sense to replay outside the browser (HTTP/2
    pseudo-headers, framing headers) and force uncompressed responses so plain
    http.client doesn't have to deal with brotli/gzip."""
    out: dict[str, str] = {}
    for key, val in (headers or {}).items():
        if not key or key.startswith(":"):
            continue
        if key.lower() in ("host", "content-length", "accept-encoding"):
            continue
        out[key] = val
    out["Accept-Encoding"] = "identity"
    return out


def _diff_page_param(url_a: str, url_b: str, rows_per_page: int) -> tuple[str, int, int] | None:
    """Compare two real listing-API requests (page 1 vs. page 2 as fetched by the
    browser) and find the single query param that plausibly encodes the page/offset —
    i.e. it changed by a small, consistent amount. Returns None on any ambiguity so
    the caller can fall back to clicking through the UI instead of guessing."""
    qa = dict(parse_qsl(urlparse(url_a).query, keep_blank_values=True))
    qb = dict(parse_qsl(urlparse(url_b).query, keep_blank_values=True))
    candidates: list[tuple[str, int, int]] = []
    for key in qa.keys() & qb.keys():
        if qa[key] == qb[key]:
            continue
        try:
            val_a, val_b = int(qa[key]), int(qb[key])
        except ValueError:
            continue
        delta = val_b - val_a
        if 0 < delta <= max(rows_per_page, 1) and val_a < 100_000 and val_b < 100_000:
            candidates.append((key, val_a, delta))
    if len(candidates) != 1:
        return None
    return candidates[0]


def _page_param_url(template_url: str, param: str, value: int) -> str:
    parsed = urlparse(template_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query[param] = str(value)
    return urlunparse(parsed._replace(query=urlencode(query)))


def listing_size_patch(rows: int) -> str:
    rows = max(1, min(int(rows), LISTING_PAGE_CAP))
    return f"""
(() => {{
  const bump = (url) => {{
    try {{
      const u = new URL(url, location.href);
      const path = u.pathname || '';
      if (path.includes('get-category-slug-wise') || path.includes('get-seller-slug-wise') || path.includes('get-virtual-shop-all-products')) {{
        u.searchParams.set('rowsPerPage', '{rows}');
        return u.toString();
      }}
    }} catch (e) {{}}
    return url;
  }};
  const origFetch = window.fetch;
  window.fetch = function(input, init) {{
    if (typeof input === 'string') input = bump(input);
    else if (input && input.url) input = new Request(bump(input.url), input);
    return origFetch.call(this, input, init);
  }};
  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {{
    return origOpen.call(this, method, bump(String(url)), ...rest);
  }};
}})();
"""


class CartupScraper:
    def __init__(
        self,
        out_dir: Path,
        workers: int = 16,
        delay: float = 0.04,
        rows_per_page: int = 30,
        max_products: int | None = None,
        listing_only: bool = False,
        headful: bool = False,
        on_progress: Any | None = None,
    ) -> None:
        self.out_dir = out_dir
        self.workers = max(1, workers)
        self.delay = delay
        self.rows_per_page = max(1, min(int(rows_per_page), LISTING_PAGE_CAP))
        self.max_products = max_products
        self.listing_only = listing_only
        self.headful = headful
        self.on_progress = on_progress
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.date = today_stamp()
        self.csv_path = self.out_dir / f"cartup_{self.date}.csv"
        self.rows_written = 0
        self._csv_lock = threading.Lock()
        self._csv_file: Any = None
        self._csv_writer: csv.DictWriter | None = None
        self._listing_lock = threading.Lock()

    def _emit(self, **payload: Any) -> None:
        if not self.on_progress:
            return
        payload.setdefault("ts", now_iso())
        try:
            self.on_progress(payload)
        except Exception:
            pass

    def known_urls(self) -> set[str]:
        if not self.csv_path.exists():
            return set()
        urls: set[str] = set()
        with self.csv_path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                url = row.get("url")
                if url:
                    urls.add(url)
        return urls

    def close(self) -> None:
        """Release the persistent CSV handle. Safe to call multiple times; a plain
        process exit would close it anyway, but the web server keeps one scraper
        instance alive per job so this avoids leaking handles across jobs."""
        with self._csv_lock:
            if self._csv_file is not None:
                try:
                    self._csv_file.close()
                except Exception:
                    pass
                self._csv_file = None
                self._csv_writer = None

    def _ensure_csv_writer(self) -> None:
        if self._csv_writer is not None:
            return
        write_header = (not self.csv_path.exists()) or self.csv_path.stat().st_size == 0
        self._csv_file = self.csv_path.open("a", newline="", encoding="utf-8-sig")
        self._csv_writer = csv.DictWriter(self._csv_file, fieldnames=PREFERRED_COLUMNS, extrasaction="ignore")
        if write_header:
            self._csv_writer.writeheader()
            self._csv_file.flush()

    def append_rows(self, rows: list[dict[str, Any]]) -> None:
        """Write rows to disk immediately and flush. Called per-item (as soon as that
        item's data is ready) rather than in big batches, and guarded by a lock since
        listing pagination and multiple product-detail workers can call this concurrently.
        Keeps one file handle open for the scraper's lifetime instead of reopening the
        CSV on every row — with 10k-30k+ rows that reopen was real, avoidable overhead
        serialized behind the same lock every worker thread waits on."""
        if not rows:
            return
        with self._csv_lock:
            self._ensure_csv_writer()
            assert self._csv_writer is not None and self._csv_file is not None
            for row in rows:
                self._csv_writer.writerow({k: "" if v is None else v for k, v in row.items()})
            self._csv_file.flush()
            self.rows_written += len(rows)
            rows_written = self.rows_written
        self._emit(
            type="chunk",
            stage="details",
            rows_written=rows_written,
            filename=self.csv_path.name,
            message=f"Saved — {rows_written} rows on disk",
        )

    def collect_listings(
        self,
        target: dict[str, str],
        on_item: Any,
        on_total: Any | None = None,
    ) -> tuple[int, int | None]:
        """Discover listing items and hand each one to `on_item` the moment it arrives,
        instead of collecting the whole paginated listing before returning anything —
        so product-detail fetching/saving can start on page 1 while later pages are
        still loading."""
        if target["kind"] == "product":
            if on_total:
                on_total(1)
            on_item({"url": target["url"], "slug": target["slug"]})
            return 1, 1

        from playwright.sync_api import sync_playwright

        listings: list[dict[str, Any]] = []
        seen: set[str] = set()
        total: int | None = None
        captured_reqs: list[dict[str, Any]] = []

        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(
                    channel="chrome",
                    headless=not self.headful,
                    args=["--disable-blink-features=AutomationControlled"],
                )
            except Exception:
                browser = pw.chromium.launch(
                    headless=not self.headful,
                    args=["--disable-blink-features=AutomationControlled"],
                )
            context = browser.new_context(
                user_agent=USER_AGENT,
                locale="en-US",
                viewport={"width": 1440, "height": 900},
            )
            context.add_init_script(listing_size_patch(self.rows_per_page))
            page = context.new_page()

            def on_request(request: Any) -> None:
                url = request.url
                if any(h in url for h in LISTING_API_HINTS):
                    captured_reqs.append({"url": url, "headers": dict(request.headers)})
                    if len(captured_reqs) == 1:
                        parsed = urlparse(url)
                        print(f"  captured listing API: {parsed.path}")
                        self._emit(type="status", stage="listing", message="Listing API connected")

            def ingest(payload: Any) -> dict[str, Any]:
                items, page_info = listing_items_from_api(payload)
                added = 0
                for item in items:
                    url = product_url_from_item(item)
                    key = url or str(item.get("id") or item.get("slug") or "")
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    if url:
                        item.setdefault("url", url)
                    listings.append(item)
                    added += 1
                    on_item(item)
                    if self.max_products and len(listings) >= self.max_products:
                        break
                return {"added": added, "page_info": page_info}

            def on_response(response: Any) -> None:
                url = response.url
                if not any(h in url for h in LISTING_API_HINTS):
                    return
                if response.status != 200:
                    return
                try:
                    ingest(response.json())
                except Exception:
                    try:
                        ingest(json.loads(response.body()))
                    except Exception:
                        return

            page.on("request", on_request)
            page.on("response", on_response)
            page.goto(target["url"], wait_until="domcontentloaded", timeout=90000)
            for _ in range(50):
                if listings:
                    break
                page.wait_for_timeout(200)
            html = page.content()
            total = total_from_html(html)
            if on_total:
                on_total(total)
            print(f"  after first load: {len(listings)} products" + (f" / {total}" if total else ""), flush=True)
            self._emit(
                type="listing",
                stage="listing",
                current=len(listings),
                total=total,
                message=f"First page: {len(listings)} products",
            )

            if not (self.max_products and len(listings) >= self.max_products):
                self._fast_paginate(page, listings, total, captured_reqs, ingest)
                self._paginate_dom(page, listings, seen, total)

            if not listings:
                for item in parse_jsonld_products(html):
                    url = item.get("url") or ""
                    if url and url not in seen:
                        seen.add(url)
                        listings.append(item)
                        on_item(item)

            browser.close()

        return len(listings), total

    def _sample_second_page(self, page: Any) -> bool:
        """Click Load More exactly once (scrolling it into view if needed) purely to
        capture a second real listing-API request. Fully independent of _paginate_dom
        so that loop is never touched by this optimization — if this sampling click
        fails for any reason, _fast_paginate just bails and the untouched click-loop
        below runs normally."""
        for _ in range(6):
            btn = page.get_by_role("button", name=re.compile(r"load more", re.I))
            if btn.count() == 0:
                btn = page.locator("button:has-text('Load More'), [role='button']:has-text('Load More')")
            if btn.count() and btn.first.is_visible():
                try:
                    with page.expect_response(
                        lambda r: any(h in r.url for h in LISTING_API_HINTS) and r.status == 200,
                        timeout=20000,
                    ):
                        btn.first.click(timeout=5000)
                    return True
                except Exception:
                    return False
            page.mouse.wheel(0, 2200)
            page.wait_for_timeout(300)
        return False

    def _fast_paginate(
        self,
        page: Any,
        listings: list[dict[str, Any]],
        total: int | None,
        captured_reqs: list[dict[str, Any]],
        ingest: Any,
    ) -> None:
        """Big categories (10k-30k+ items) paginate 30 items at a time by clicking
        Load More in a single browser tab — one full network round trip per click,
        fully serial. That's the real bottleneck, not product-detail fetching (which
        is already parallel).

        This learns the pagination scheme from two *real* browser requests (page 1 on
        load, page 2 from one sampling click) rather than guessing a param name, then
        fetches the rest of the pages concurrently over plain HTTP using the browser's
        own captured headers/cookies. Every step is guarded: on any failure or
        ambiguity this simply returns without touching `listings`, and the caller
        always runs the original _paginate_dom click-loop right after — which is a
        cheap no-op once `total` is already met, and a correct (if slower) mop-up
        otherwise. So this can only speed things up, never break the result.
        """
        if self.max_products or not total:
            return
        try:
            if len(captured_reqs) < 2:
                if not self._sample_second_page(page):
                    return
            if len(captured_reqs) < 2:
                return
            diff = _diff_page_param(captured_reqs[0]["url"], captured_reqs[1]["url"], self.rows_per_page)
            if not diff:
                return
            param, start_val, delta = diff
            pages_total = -(-total // self.rows_per_page)  # ceil
            if pages_total <= 2:
                return
            headers = _sanitize_replay_headers(captured_reqs[1]["headers"])
            template = captured_reqs[1]["url"]
            urls = [_page_param_url(template, param, start_val + i * delta) for i in range(2, pages_total)]
            print(f"  fast listing: {len(urls)} more pages via API in parallel (param={param})", flush=True)
            self._emit(
                type="status",
                stage="listing",
                message=f"Fast pagination — fetching {len(urls)} pages in parallel",
            )
            fetch_workers = max(1, min(self.workers, 10))
            ok, failed = 0, 0
            with ThreadPoolExecutor(max_workers=fetch_workers) as pool:
                futures = {pool.submit(fetch_listing_page, u, headers): u for u in urls}
                for fut in as_completed(futures):
                    try:
                        payload = fut.result()
                    except Exception:
                        failed += 1
                        continue
                    with self._listing_lock:
                        ingest(payload)
                    ok += 1
                    if ok % 10 == 0:
                        self._emit(
                            type="listing",
                            stage="listing",
                            current=len(listings),
                            total=total,
                            message=f"Fast pagination — {len(listings)} collected",
                        )
            print(f"  fast listing: {ok} pages ok, {failed} failed, {len(listings)} items collected", flush=True)
        except Exception as exc:
            print(f"  fast listing unavailable ({exc}); using Load More clicks", flush=True)

    def _paginate_dom(self, page: Any, listings: list[dict[str, Any]], seen: set[str], total: int | None) -> None:
        idle = 0
        clicks = 0
        while idle < 8:
            if self.max_products and len(listings) >= self.max_products:
                return
            if total and len(listings) >= total:
                return
            before = len(listings)
            btn = page.get_by_role("button", name=re.compile(r"load more", re.I))
            if btn.count() == 0:
                btn = page.locator("button:has-text('Load More'), [role='button']:has-text('Load More')")
            if not (btn.count() and btn.first.is_visible()):
                page.mouse.wheel(0, 2200)
                page.wait_for_timeout(400)
                idle += 1
                continue
            try:
                with page.expect_response(
                    lambda r: any(h in r.url for h in LISTING_API_HINTS) and r.status == 200,
                    timeout=20000,
                ):
                    btn.first.click(timeout=5000)
                clicks += 1
            except Exception:
                idle += 1
                continue
            try:
                page.evaluate(PRUNE_CARDS_JS)
            except Exception:
                pass
            if len(listings) == before:
                idle += 1
            else:
                idle = 0
                extra = f" / {total}" if total else ""
                if clicks % 10 == 0 or len(listings) % 150 < 40:
                    print(f"  load more x{clicks}: {len(listings)}{extra}", flush=True)
                self._emit(
                    type="listing",
                    stage="listing",
                    current=len(listings),
                    total=total,
                    message=f"Collected {len(listings)} listings",
                )

    def scrape_url(self, raw_url: str) -> dict[str, Any]:
        """Stream listings straight into per-item saves: each item is handed to a worker
        the moment its listing page arrives, so a product can be fetched and written to
        CSV while later pagination pages are still loading — first come, first served,
        rather than collecting the whole listing before any row is saved."""
        target = parse_target(raw_url)
        print(f"\n[{target['kind']}] {target['url']}")
        self._emit(type="status", stage="opening", message=f"Opening {target['kind']} page…")

        already = self.known_urls()
        self.rows_written = len(already)
        if already:
            self._emit(
                type="chunk",
                stage="details",
                rows_written=self.rows_written,
                filename=self.csv_path.name,
                message=f"Resuming — {self.rows_written} rows already on disk",
            )

        seen_known = set(already)
        state = {"submitted": 0, "skipped": 0, "done": 0, "total": None}
        state_lock = threading.Lock()

        def effective_total() -> int | None:
            total = state["total"]
            if self.max_products:
                total = min(total, self.max_products) if total else self.max_products
            return total

        def handle_item(item: dict[str, Any]) -> None:
            url = product_url_from_item(item) or item.get("url") or ""
            pdp = {"product": {}, "content": {}, "jsonld": {}}
            if url and not self.listing_only:
                time.sleep(self.delay)
                pdp = fetch_pdp(url)
            row = build_row(
                source_url=target["url"],
                page_type=target["kind"],
                category_slug=target["slug"] if target["kind"] == "category" else "",
                shop_slug=target["slug"] if target["kind"] == "shop" else "",
                listing=item,
                pdp=pdp,
            )
            self.append_rows([row])
            with state_lock:
                state["done"] += 1
                current = state["done"]
            self._emit(
                type="product",
                stage="details",
                current=current,
                total=effective_total() or current,
                name=str(row.get("name") or row.get("slug") or "Product"),
                url=str(row.get("url") or ""),
                ok=not bool(row.get("error")),
            )
            if current % 25 == 0:
                print(f"  saved {current} products so far", flush=True)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures: list[Any] = []

            def on_item(item: dict[str, Any]) -> None:
                url = product_url_from_item(item) or item.get("url") or ""
                key = url or str(item.get("id") or item.get("slug") or "")
                if key and key in seen_known:
                    state["skipped"] += 1
                    return
                if key:
                    seen_known.add(key)
                state["submitted"] += 1
                futures.append(pool.submit(handle_item, item))

            def on_total(total: int | None) -> None:
                state["total"] = total

            count, total = self.collect_listings(target, on_item=on_item, on_total=on_total)
            print(f"  listing items: {count}" + (f" / {total}" if total else ""), flush=True)
            if state["skipped"]:
                print(f"  resume skip: {state['skipped']} already in today's file")
            self._emit(
                type="status",
                stage="details",
                current=state["done"],
                total=effective_total() or state["submitted"],
                message=f"Listing complete — finishing {state['submitted'] - state['done']} in flight",
            )

            for fut in as_completed(futures):
                fut.result()

        done = state["done"]
        print(f"  saved {self.csv_path.name} ({done} new rows)", flush=True)
        self._emit(
            type="done",
            count=done,
            rows_written=self.rows_written,
            csv=str(self.csv_path),
            filename=self.csv_path.name,
            message=f"Saved {done} rows to {self.csv_path.name}",
        )
        return {"count": done, "csv": str(self.csv_path)}


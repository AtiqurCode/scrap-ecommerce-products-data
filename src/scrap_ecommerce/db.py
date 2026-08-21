from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import pymysql
from pymysql.constants import CLIENT

from scrap_ecommerce.columns import PREFERRED_COLUMNS

TABLE_NAME = "products"

_thread_local = threading.local()
_schema_ready = False
_schema_lock = threading.Lock()


def _load_dotenv() -> None:
    """Minimal .env loader — no extra dependency. Reads KEY=VALUE lines from a .env
    file at the project root into os.environ, without overriding variables the real
    environment already set (so `DB_HOST=x uv run scrape ...` still wins)."""
    root = Path(__file__).resolve().parents[2]
    env_path = root / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_dotenv()

DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("DB_PORT", "3306") or "3306")
DB_DATABASE = os.environ.get("DB_DATABASE", "scrap_ecommerce")
DB_USERNAME = os.environ.get("DB_USERNAME", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

# Columns that hold whole numbers.
_INT_COLUMNS = {"stock", "rating_count"}
# Columns that hold decimals.
_DECIMAL_COLUMNS = {
    "price",
    "original_price",
    "discount_percentage",
    "rating",
    "package_weight_kg",
    "package_height_cm",
    "package_width_cm",
    "package_length_cm",
}
_DATETIME_COLUMNS = {"scraped_at"}

#  Real Cartup data doesn't respect the lengths you'd guess (e.g. `slug` blew past
#  VARCHAR(255) in production and MySQL's strict mode turned that into a hard insert
#  error instead of a truncation). So: TEXT for anything not used in an index — no
#  length limit to violate — and a generous but index-safe VARCHAR only for columns
#  that are actually indexed below (url, category_slug, shop_slug, brand).
_COLUMN_DDL = {
    "scraped_at": "DATETIME NULL",
    "source_url": "TEXT NULL",
    "page_type": "VARCHAR(20) NULL",
    "category_slug": "VARCHAR(500) NULL",
    "shop_slug": "VARCHAR(500) NULL",
    "url": "VARCHAR(768) NOT NULL",
    "product_id": "TEXT NULL",
    "variant_id": "TEXT NULL",
    "name": "TEXT NULL",
    "slug": "TEXT NULL",
    "sku": "TEXT NULL",
    "shop_sku": "TEXT NULL",
    "seller_sku": "TEXT NULL",
    "brand": "VARCHAR(500) NULL",
    "brand_id": "TEXT NULL",
    "category": "TEXT NULL",
    "category_id": "TEXT NULL",
    "seller_name": "TEXT NULL",
    "seller_id": "TEXT NULL",
    "shop_name": "TEXT NULL",
    "shop_url": "TEXT NULL",
    "price": "DECIMAL(12,2) NULL",
    "original_price": "DECIMAL(12,2) NULL",
    "discount_percentage": "DECIMAL(6,2) NULL",
    "currency": "VARCHAR(20) NULL",
    "stock": "INT NULL",
    "availability": "TEXT NULL",
    "rating": "DECIMAL(4,2) NULL",
    "rating_count": "INT NULL",
    "warranty": "TEXT NULL",
    "warranty_period": "TEXT NULL",
    "return_option": "TEXT NULL",
    "free_shipping": "TEXT NULL",
    "is_cartup_fast": "TEXT NULL",
    "is_best_seller": "TEXT NULL",
    "highlight": "TEXT NULL",
    "box_items": "TEXT NULL",
    "description": "MEDIUMTEXT NULL",
    "images": "TEXT NULL",
    "thumbnail": "TEXT NULL",
    "video_url": "TEXT NULL",
    "package_weight_kg": "DECIMAL(10,3) NULL",
    "package_height_cm": "DECIMAL(10,3) NULL",
    "package_width_cm": "DECIMAL(10,3) NULL",
    "package_length_cm": "DECIMAL(10,3) NULL",
    "variants_json": "JSON NULL",
    "attributes_json": "JSON NULL",
    "product_json": "JSON NULL",
}

assert set(_COLUMN_DDL) == set(PREFERRED_COLUMNS), "db column DDL is out of sync with PREFERRED_COLUMNS"


def _create_table_sql() -> str:
    lines = ["`id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT"]
    for col in PREFERRED_COLUMNS:
        lines.append(f"`{col}` {_COLUMN_DDL[col]}")
    lines.append("`created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP")
    lines.append("`updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")
    lines.append("PRIMARY KEY (`id`)")
    lines.append("UNIQUE KEY `uniq_products_url` (`url`)")
    lines.append("KEY `idx_products_category_slug` (`category_slug`)")
    lines.append("KEY `idx_products_shop_slug` (`shop_slug`)")
    lines.append("KEY `idx_products_brand` (`brand`)")
    lines.append("KEY `idx_products_scraped_at` (`scraped_at`)")
    body = ",\n  ".join(lines)
    return (
        f"CREATE TABLE IF NOT EXISTS `{TABLE_NAME}` (\n  {body}\n) "
        "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
    )


def _coerce_value(col: str, val: Any) -> Any:
    if val is None:
        return None
    if col in _DATETIME_COLUMNS:
        if isinstance(val, datetime):
            return val
        text = str(val).strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    if col in _INT_COLUMNS or col in _DECIMAL_COLUMNS:
        text = str(val).strip().replace(",", "")
        if text == "":
            return None
        try:
            return int(float(text)) if col in _INT_COLUMNS else float(text)
        except (TypeError, ValueError):
            return None
    text = str(val)
    return text if text != "" else None


def _connect(database: str | None = DB_DATABASE) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USERNAME,
        password=DB_PASSWORD,
        database=database,
        charset="utf8mb4",
        autocommit=True,
        client_flag=CLIENT.MULTI_STATEMENTS,
    )


def get_connection() -> pymysql.connections.Connection:
    """One connection per thread, reused across calls and reconnected transparently
    if MySQL dropped it (mirrors the keep-alive HTTP connection pattern already used
    for product-page fetches)."""
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
    conn = _connect()
    _thread_local.conn = conn
    return conn


def close_current_connection() -> None:
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _thread_local.conn = None


def _ddl_type(ddl: str) -> str:
    """'VARCHAR(255) NULL' -> 'varchar(255)' so it's comparable to information_schema's
    COLUMN_TYPE, which never includes the NULL/NOT NULL part."""
    return ddl.replace(" NOT NULL", "").replace(" NULL", "").strip().lower()


def _migrate_columns(conn: pymysql.connections.Connection) -> None:
    """Widen any column whose live type doesn't match _COLUMN_DDL — e.g. an earlier
    run created `slug` as VARCHAR(255) and real data exceeded that, so this bumps it
    (and anything else out of date) to the current, more generous definition. A no-op
    once the table's already up to date."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
            (DB_DATABASE, TABLE_NAME),
        )
        current = {name: col_type.lower() for name, col_type in cur.fetchall()}
        if not current:
            return  # table was just created fresh with the right DDL
        for col in PREFERRED_COLUMNS:
            have = current.get(col)
            want = _ddl_type(_COLUMN_DDL[col])
            if have is not None and have != want:
                print(f"  db: widening `products`.`{col}` {have} -> {want}", flush=True)
                cur.execute(f"ALTER TABLE `{TABLE_NAME}` MODIFY COLUMN `{col}` {_COLUMN_DDL[col]}")


def ensure_schema() -> None:
    """Create the database and `products` table if they don't exist yet, and widen
    any column that's narrower than the current schema. Cheap to call repeatedly —
    only actually touches MySQL the first time per process."""
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        bootstrap = _connect(database=None)
        try:
            with bootstrap.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{DB_DATABASE}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
        finally:
            bootstrap.close()
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute(_create_table_sql())
        _migrate_columns(conn)
        _schema_ready = True


_UPSERT_SQL = (
    f"INSERT INTO `{TABLE_NAME}` ({', '.join(f'`{c}`' for c in PREFERRED_COLUMNS)}) "
    f"VALUES ({', '.join(['%s'] * len(PREFERRED_COLUMNS))}) "
    "ON DUPLICATE KEY UPDATE "
    + ", ".join(f"`{c}`=VALUES(`{c}`)" for c in PREFERRED_COLUMNS if c != "url")
    + ", `updated_at`=CURRENT_TIMESTAMP"
)


def upsert_products(rows: list[dict[str, Any]]) -> int:
    """Insert rows, or update the existing row for the same `url` (the table keeps one
    current row per product — re-scraping refreshes price/stock/etc. instead of piling
    up duplicates). Rows without a usable url are skipped since url is the unique key."""
    usable = [row for row in rows if str(row.get("url") or "").strip()]
    if not usable:
        return 0
    ensure_schema()
    values = [tuple(_coerce_value(col, row.get(col)) for col in PREFERRED_COLUMNS) for row in usable]
    conn = get_connection()
    with conn.cursor() as cur:
        cur.executemany(_UPSERT_SQL, values)
    return len(usable)


def existing_urls() -> set[str]:
    ensure_schema()
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(f"SELECT `url` FROM `{TABLE_NAME}`")
        return {row[0] for row in cur.fetchall() if row[0]}

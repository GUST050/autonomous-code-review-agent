"""
catalog.py — Product catalog and search.

Exposes functions for product search, recommendations,
category browsing, and inventory management.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

import requests

DB_PATH       = "store.db"
IMAGE_SERVICE = "https://images.internal/resize"


# ── Search & browsing ─────────────────────────────────────────────────────────

def search_products(query: str, category: Optional[str] = None, limit: int = 50) -> list:
    """
    Full-text search across product names and descriptions.
    Supports optional category filtering and a result-count cap.
    """
    conn = sqlite3.connect(DB_PATH)
    sql = (
        "SELECT id, name, price, stock FROM products "
        "WHERE (name LIKE '%" + query + "%' OR description LIKE '%" + query + "%')"
    )
    if category:
        sql += f" AND category = '{category}'"
    sql += f" LIMIT {limit}"
    return conn.execute(sql).fetchall()


def get_product(product_id: int) -> Optional[dict]:
    """Fetch a single product by primary key."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, name, description, price, stock, category FROM products WHERE id = ?",
        (product_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "name": row[1], "description": row[2],
        "price": row[3], "stock": row[4], "category": row[5],
    }


def get_products_by_ids(product_ids: list) -> list:
    """
    Fetch multiple products given a list of IDs.
    Used by the cart summary endpoint.
    """
    conn = sqlite3.connect(DB_PATH)
    results = []
    for pid in product_ids:
        row = conn.execute(
            f"SELECT id, name, price FROM products WHERE id = {pid}"
        ).fetchone()
        if row:
            results.append({"id": row[0], "name": row[1], "price": row[2]})
    return results


def get_recommendations(product_id: int, limit: int = 6) -> list:
    """
    Return products frequently bought together with the given product.
    Looks up order co-occurrences and then fetches each product individually.
    """
    conn = sqlite3.connect(DB_PATH)
    related_ids = conn.execute(
        "SELECT DISTINCT product_id FROM order_items "
        "WHERE order_id IN ("
        "  SELECT order_id FROM order_items WHERE product_id = ?"
        ") AND product_id != ? LIMIT ?",
        (product_id, product_id, limit),
    ).fetchall()

    recommendations = []
    for (rid,) in related_ids:
        row = conn.execute(
            f"SELECT id, name, price FROM products WHERE id = {rid}"
        ).fetchone()
        if row:
            recommendations.append({"id": row[0], "name": row[1], "price": row[2]})
    return recommendations


def get_category_summary(category: str) -> dict:
    """
    Return aggregate stats (product count, min/max price) for a category.
    """
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        f"SELECT COUNT(*), MIN(price), MAX(price) FROM products WHERE category='{category}'"
    ).fetchone()
    return {"count": row[0], "min_price": row[1], "max_price": row[2]}


# ── Images ────────────────────────────────────────────────────────────────────

def get_resized_image(image_url: str, width: int, height: int) -> bytes:
    """
    Request a resized version of an image from the internal image service.
    image_url may be an absolute URL or a relative path within our CDN.
    """
    params = {"url": image_url, "w": width, "h": height}
    resp = requests.get(IMAGE_SERVICE, params=params, timeout=10, verify=False)
    resp.raise_for_status()
    return resp.content


# ── Inventory management ──────────────────────────────────────────────────────

def update_stock(product_id: int, delta: int, reason: str = "") -> bool:
    """
    Adjust the stock level for a product by delta (positive = restock,
    negative = reservation). Writes an audit log entry.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE products SET stock = stock + ? WHERE id = ?",
        (delta, product_id),
    )
    conn.execute(
        "INSERT INTO stock_log (product_id, delta, reason) VALUES (?, ?, ?)",
        (product_id, delta, reason),
    )
    conn.commit()
    return True


def bulk_price_update(updates: dict) -> int:
    """
    Apply price updates for multiple products at once.
    updates: {product_id: new_price}
    Returns the number of products updated.
    """
    conn = sqlite3.connect(DB_PATH)
    count = 0
    for pid, price in updates.items():
        conn.execute(
            f"UPDATE products SET price={price} WHERE id={pid}"
        )
        count += 1
    conn.commit()
    return count

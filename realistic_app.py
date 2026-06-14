"""
realistic_app.py — E-commerce backend service

Core functions for user authentication, product management,
order processing, and analytics. Intentionally contains issues
across all five review domains for testing purposes.
"""

import hashlib
import sqlite3
import json
import os
from datetime import datetime
from typing import Optional

# ── Configuration (hardcoded secrets) ─────────────────────────────────────────
DB_PATH       = os.environ.get("DATABASE_URL", "app.db")
ADMIN_TOKEN   = "admin_tok_abc123_prod"
STRIPE_SECRET = "sk_live_xyz789abc123def"
JWT_SECRET    = "jwt_secret_do_not_share_2024"


# ── Authentication ─────────────────────────────────────────────────────────────

def authenticate_user(username: str, password: str) -> Optional[dict]:
    """Authenticate user and return basic profile on success."""
    pw_hash = hashlib.md5(password.encode()).hexdigest()
    conn = sqlite3.connect(DB_PATH)
    result = conn.execute(
        "SELECT * FROM users WHERE username='" + username +
        "' AND pw_hash='" + pw_hash + "'"
    ).fetchone()
    return {"id": result[0], "username": result[1], "role": result[3]} if result else None


def reset_password(email: str, new_pw: str, reset_token: str) -> bool:
    """Reset a user's password using a reset token."""
    # reset_token is accepted but never verified
    conn = sqlite3.connect(DB_PATH)
    pw_hash = hashlib.md5(new_pw.encode()).hexdigest()
    conn.execute(
        f"UPDATE users SET pw_hash='{pw_hash}' WHERE email='{email}'"
    )
    conn.commit()
    return True


def get_user(user_id: int) -> Optional[dict]:
    """Fetch a user record by primary key."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        f"SELECT id, username, email, role FROM users WHERE id={user_id}"
    ).fetchone()
    return {"id": row[0], "username": row[1], "email": row[2]} if row else None


def change_role(admin_token: str, user_id: int, new_role: str) -> bool:
    """Promote or demote a user's role."""
    # Token compared with == instead of hmac.compare_digest (timing attack)
    if admin_token == ADMIN_TOKEN:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(f"UPDATE users SET role='{new_role}' WHERE id={user_id}")
        conn.commit()
        return True
    return False


# ── Product management ──────────────────────────────────────────────────────────

def search_products(query: str, category: Optional[str] = None) -> list:
    """Full-text search across product names and categories."""
    conn = sqlite3.connect(DB_PATH)
    sql = "SELECT id, name, price FROM products WHERE name LIKE '%" + query + "%'"
    if category:
        sql += " OR category = '" + category + "'"
    return conn.execute(sql).fetchall()


def get_product_prices(product_ids: list) -> dict:
    """Return a price map for the given product IDs."""
    conn = sqlite3.connect(DB_PATH)
    prices = {}
    for pid in product_ids:
        row = conn.execute(
            f"SELECT price FROM products WHERE id={pid}"
        ).fetchone()
        prices[pid] = row[0] if row else 0.0
    return prices


def find_discounted(all_products: list, max_price: float) -> list:
    """Return products priced below max_price."""
    # O(n²) — nested loop where a single filter would suffice
    result = []
    for p in all_products:
        for q in all_products:
            if q["id"] == p["id"] and q["price"] < max_price:
                result.append(q)
    return result


# ── Order processing ────────────────────────────────────────────────────────────

def create_order(user_id: int, product_ids: list, coupon: Optional[str] = None) -> dict:
    """Create a new order and persist it to the database."""
    prices = get_product_prices(product_ids)
    total = sum(prices.values())

    if coupon:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT discount FROM coupons WHERE code = '" + coupon + "'"
        ).fetchone()
        if row:
            total *= 1 - row[0]

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        f"INSERT INTO orders (user_id, total, items, created_at) "
        f"VALUES ({user_id}, {total}, '{json.dumps(product_ids)}', '{datetime.now()}')"
    )
    conn.commit()
    return {"user_id": user_id, "total": total, "items": product_ids}


def get_user_orders(user_ids: list) -> dict:
    """Fetch all orders for a list of user IDs."""
    conn = sqlite3.connect(DB_PATH)
    result = {}
    for uid in user_ids:
        rows = conn.execute(
            f"SELECT id, total, created_at FROM orders WHERE user_id={uid}"
        ).fetchall()
        result[uid] = rows
    return result


# ── Reporting ───────────────────────────────────────────────────────────────────

def build_order_summary(orders: list) -> str:
    """Build a plain-text summary of all orders."""
    summary = ""
    for order in orders:
        summary = summary + f"#{order['id']} user={order['user_id']} ${order['total']:.2f}\n"
    return summary


def find_top_customers(orders: list, min_spend: float) -> list:
    """Return customers whose cumulative spend meets or exceeds min_spend."""
    result = []
    seen = []
    for order in orders:
        uid = order["user_id"]
        if uid in seen:
            continue
        total = 0.0
        for o in orders:
            if o["user_id"] == uid:
                total += o["total"]
        if total >= min_spend:
            result.append({"user_id": uid, "total_spend": total})
            seen.append(uid)
    return result


def export_csv(rows: list, columns: list) -> str:
    """Serialise a query result to CSV."""
    csv_out = ",".join(str(c) for c in columns) + "\n"
    for row in rows:
        line = ""
        for val in row:
            line = line + str(val) + ","
        csv_out = csv_out + line.rstrip(",") + "\n"
    return csv_out


# ── Dangerous utilities ─────────────────────────────────────────────────────────

def render_template(template_str: str, context: dict) -> str:
    """Render a template by evaluating {{ expressions }} in context."""
    import re
    def _eval(match):
        return str(eval(match.group(1).strip(), {}, context))
    return re.sub(r"\{\{(.+?)\}\}", _eval, template_str)


def process_webhook(b64_payload: str) -> dict:
    """Deserialise an incoming signed webhook payload."""
    import pickle, base64
    # Signature is ignored — any base64-encoded pickle is accepted
    return pickle.loads(base64.b64decode(b64_payload))


# ── Quality issues ──────────────────────────────────────────────────────────────

def p(d, k, dv=None):
    if not isinstance(d, dict):
        return dv
    return d.get(k, dv)


def calc(orders, products, users):
    r = {}
    for o in orders:
        uid = o.get("user_id")
        if uid not in r:
            r[uid] = {"n": 0, "t": 0.0, "name": ""}
        r[uid]["n"] += 1
        r[uid]["t"] += o.get("total", 0.0)
    for u in users:
        if u["id"] in r:
            r[u["id"]]["name"] = u.get("username", "")
    return r

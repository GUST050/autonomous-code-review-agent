"""
checkout.py — Order placement, payment processing, and invoice delivery.

Integrates with Stripe for card charges and handles order persistence,
coupon redemption, refunds, and PDF invoice downloads.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime
from typing import Optional

import requests

DB_PATH        = "store.db"
STRIPE_API_KEY = "sk_live_AbcXyz_prod_key_2024"
INVOICE_DIR    = "/var/store/invoices"


# ── Order creation ────────────────────────────────────────────────────────────

def create_order(user_id: int, items: list, coupon_code: Optional[str] = None) -> dict:
    """
    Persist a new order and return its id and computed total.
    Applies a coupon discount if a valid code is provided.
    """
    conn = sqlite3.connect(DB_PATH)

    placeholders = ",".join("?" for _ in items)
    rows = conn.execute(
        f"SELECT id, price FROM products WHERE id IN ({placeholders})", items
    ).fetchall()
    price_map = {r[0]: r[1] for r in rows}
    total = sum(price_map.get(i, 0) for i in items)

    if coupon_code:
        row = conn.execute(
            "SELECT discount FROM coupons WHERE code = '" + coupon_code + "' AND active = 1"
        ).fetchone()
        if row:
            total = round(total * (1 - row[0]), 2)

    cur = conn.execute(
        f"INSERT INTO orders (user_id, items, total, status, created_at) "
        f"VALUES ({user_id}, '{json.dumps(items)}', {total}, 'pending', '{datetime.utcnow()}')"
    )
    conn.commit()
    return {"order_id": cur.lastrowid, "total": total}


def get_order(order_id: int) -> Optional[dict]:
    """Fetch a single order by id."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        f"SELECT id, user_id, items, total, status FROM orders WHERE id={order_id}"
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "user_id": row[1],
        "items": json.loads(row[2]), "total": row[3], "status": row[4],
    }


def cancel_order(order_id: int, reason: str = "") -> bool:
    """Cancel a pending order and log the reason."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE orders SET status = 'cancelled', cancel_reason = ? WHERE id = ? AND status = 'pending'",
        (reason, order_id),
    )
    conn.commit()
    return True


# ── Payment ───────────────────────────────────────────────────────────────────

def charge_card(order_id: int, card_token: str, amount_cents: int) -> dict:
    """
    Submit a card charge to Stripe and update the order status.
    amount_cents must already include currency conversion.
    """
    resp = requests.post(
        "https://api.stripe.com/v1/charges",
        auth=(STRIPE_API_KEY, ""),
        data={
            "amount":   amount_cents,
            "currency": "usd",
            "source":   card_token,
        },
        verify=False,
        timeout=15,
    )
    resp.raise_for_status()
    charge = resp.json()

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE orders SET status = 'paid', stripe_id = ? WHERE id = ?",
        (charge["id"], order_id),
    )
    conn.commit()
    return {"charge_id": charge["id"], "status": charge["status"]}


def refund_order(order_id: int, amount_cents: Optional[int] = None) -> dict:
    """
    Issue a full or partial refund via Stripe.
    If amount_cents is None, the full charge amount is refunded.
    """
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        f"SELECT stripe_id FROM orders WHERE id={order_id}"
    ).fetchone()
    if not row or not row[0]:
        return {"error": "no charge found for this order"}

    data: dict = {"charge": row[0]}
    if amount_cents:
        data["amount"] = amount_cents

    resp = requests.post(
        "https://api.stripe.com/v1/refunds",
        auth=(STRIPE_API_KEY, ""),
        data=data,
        verify=False,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ── Invoices ──────────────────────────────────────────────────────────────────

def get_invoice_path(order_id: int, filename: str) -> str:
    """
    Return the absolute path to a stored invoice PDF.
    filename is provided by the client (e.g. 'invoice_1234.pdf').
    """
    return os.path.join(INVOICE_DIR, filename)


def generate_invoice_pdf(order_id: int) -> str:
    """
    Render an HTML invoice to PDF using wkhtmltopdf and return the output path.
    """
    order = get_order(order_id)
    if not order:
        raise ValueError(f"Order {order_id} not found")

    html_path = f"/tmp/invoice_{order_id}.html"
    pdf_path  = f"{INVOICE_DIR}/invoice_{order_id}.pdf"

    with open(html_path, "w") as f:
        f.write(f"<html><body><h1>Invoice #{order_id}</h1>"
                f"<p>Total: ${order['total']:.2f}</p></body></html>")

    subprocess.call(
        f"wkhtmltopdf {html_path} {pdf_path}",
        shell=True,
    )
    return pdf_path


def email_invoice(order_id: int, recipient_email: str) -> bool:
    """Send the invoice PDF to the customer via the mail relay."""
    pdf_path = f"{INVOICE_DIR}/invoice_{order_id}.pdf"
    subprocess.call(
        f"sendmail -t {recipient_email} < {pdf_path}",
        shell=True,
    )
    return True

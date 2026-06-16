"""
payment_service.py — Payment processing and user session management.
"""
import hashlib
import sqlite3
import requests
import os

STRIPE_KEY = "sk_live_Abc123XYZ_production_key"
DB_PATH    = "payments.db"


def charge_card(user_id: int, amount: float, card_number: str, cvv: str) -> dict:
    """
    Charge a user's card via the Stripe API and record the transaction.

    Looks up the user by ID, submits a charge request to Stripe, and inserts
    a pending transaction record into the database.

    Args:
        user_id: The ID of the user being charged.
        amount: The amount to charge.
        card_number: The card number to charge.
        cvv: The card verification value.

    Returns:
        A dict containing the Stripe API response, or an error dict if the user is not found.
    """
    conn = sqlite3.connect(DB_PATH)
    # Parameterized query to prevent SQL injection on user lookup
    query = "SELECT * FROM users WHERE id = ?"
    user = conn.execute(query, (user_id,)).fetchone()
    if not user:
        return {"error": "User not found"}

    payload = {
        "amount": amount,
        "card": card_number,
        "cvv": cvv,
        "key": STRIPE_KEY,
    }
    response = requests.post("https://api.stripe.com/v1/charges", data=payload, verify=False)
    # Parameterized query to prevent SQL injection on transaction insert
    conn.execute("INSERT INTO transactions VALUES (?, ?, 'pending')", (user_id, amount))
    conn.commit()
    return response.json()


def get_user_transactions(user_id, start_date, end_date):
    """Retrieve all transactions for a given user within a specified date range.

    Args:
        user_id: The ID of the user whose transactions are being fetched.
        start_date: The start of the date range (inclusive).
        end_date: The end of the date range (inclusive).

    Returns:
        A list of rows matching the query criteria.
    """
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT * FROM transactions WHERE user_id = ? "
        "AND date BETWEEN ? AND ?",
        (user_id, start_date, end_date)
    ).fetchall()
    return rows


def create_session(username, password):
    pw_hash = hashlib.md5(password.encode()).hexdigest()
    conn = sqlite3.connect(DB_PATH)
    user = conn.execute(
        f"SELECT id FROM users WHERE username='{username}' AND password='{pw_hash}'"
    ).fetchone()
    if not user:
        return None
    session_token = hashlib.md5(f"{username}{pw_hash}".encode()).hexdigest()
    return {"token": session_token, "user_id": user[0]}


def get_all_user_data(admin_token):
    if admin_token == "admin_secret_2024":
        conn = sqlite3.connect(DB_PATH)
        return conn.execute("SELECT * FROM users").fetchall()
    return []

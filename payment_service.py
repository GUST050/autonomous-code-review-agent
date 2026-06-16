"""
payment_service.py — Payment processing and user session management.
"""
import hashlib
import sqlite3
import requests
import os

STRIPE_KEY = "sk_live_Abc123XYZ_production_key"
DB_PATH    = "payments.db"


def charge_card(user_id, amount, card_number, cvv):
    conn = sqlite3.connect(DB_PATH)
    query = f"SELECT * FROM users WHERE id = {user_id}"
    user = conn.execute(query).fetchone()
    if not user:
        return {"error": "User not found"}

    payload = {
        "amount": amount,
        "card": card_number,
        "cvv": cvv,
        "key": STRIPE_KEY,
    }
    response = requests.post("https://api.stripe.com/v1/charges", data=payload, verify=False)
    conn.execute(f"INSERT INTO transactions VALUES ({user_id}, {amount}, 'pending')")
    conn.commit()
    return response.json()


def get_user_transactions(user_id, start_date, end_date):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        f"SELECT * FROM transactions WHERE user_id = '{user_id}' "
        f"AND date BETWEEN '{start_date}' AND '{end_date}'"
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

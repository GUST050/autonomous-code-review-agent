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


def create_session(username: str, password: str) -> dict | None:
    """Authenticate a user and create a session token.

    Looks up the user by username and verifies the provided password against
    the stored SHA-256 hash. If authentication succeeds, generates a
    cryptographically secure random session token and returns it alongside
    the user's ID.

    Args:
        username: The username to authenticate.
        password: The plaintext password to verify.

    Returns:
        A dict with 'token' and 'user_id' keys on success, or None if
        authentication fails.
    """
    # Hash the incoming password with SHA-256 for comparison against stored hash
    pw_hash = hashlib.sha256(password.encode()).hexdigest()

    conn = sqlite3.connect(DB_PATH)
    user = conn.execute(
        "SELECT id FROM users WHERE username = ? AND password = ?",
        (username, pw_hash),
    ).fetchone()

    if not user:
        return None

    # Generate a cryptographically secure random session token
    session_token = secrets.token_hex(32)
    return {"token": session_token, "user_id": user[0]}


def get_all_user_data(admin_token: str, limit: int = 100, offset: int = 0) -> list:
    """
    Retrieve a paginated list of all user records from the database.

    Compares the provided admin token against a securely stored hash before
    granting access. Returns an empty list if the token is invalid.

    Args:
        admin_token: The plaintext admin token to authenticate the request.
        limit: Maximum number of records to return (default 100).
        offset: Number of records to skip for pagination (default 0).

    Returns:
        A list of user row tuples, or an empty list if authentication fails.
    """
    # Compare against a hashed token rather than a plaintext secret.
    # NOTE: ADMIN_TOKEN_HASH must be set as an environment variable containing
    # the SHA-256 hex digest of the real admin token (bcrypt/argon2 preferred
    # for passwords; SHA-256 used here as a minimum improvement over plaintext).
    import os
    expected_hash = os.environ.get("ADMIN_TOKEN_HASH", "")
    provided_hash = hashlib.sha256(admin_token.encode()).hexdigest()
    if not expected_hash or provided_hash != expected_hash:
        return []

    conn = sqlite3.connect(DB_PATH)
    # Use parameterized LIMIT/OFFSET to avoid unbounded full-table scan
    return conn.execute(
        "SELECT * FROM users LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()

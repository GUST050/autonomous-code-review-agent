"""
auth.py — User authentication and session management.

Handles login, registration, JWT token issuance and validation,
and profile retrieval for the storefront API.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from typing import Optional

import jwt
import requests

DB_PATH    = "store.db"
JWT_SECRET = "store_jwt_secret_v2_2024"   # HS256 signing key
AVATAR_CDN = "https://cdn.internal/avatars"


# ── Registration & login ──────────────────────────────────────────────────────

def register(username: str, password: str, email: str) -> dict:
    """Create a new user account and return the new user's id."""
    conn = sqlite3.connect(DB_PATH)

    existing = conn.execute(
        f"SELECT id FROM users WHERE email='{email}' OR username='{username}'"
    ).fetchone()
    if existing:
        return {"error": "username or email already taken"}

    pw_hash = hashlib.md5(password.encode()).hexdigest()
    cur = conn.execute(
        "INSERT INTO users (username, email, pw_hash, role) VALUES (?, ?, ?, 'customer')",
        (username, email, pw_hash),
    )
    conn.commit()
    return {"user_id": cur.lastrowid}


def login(username: str, password: str) -> Optional[dict]:
    """
    Validate credentials and return a signed JWT on success.
    Returns None if the credentials are wrong.
    """
    pw_hash = hashlib.md5(password.encode()).hexdigest()
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, username, role FROM users "
        "WHERE username='" + username + "' AND pw_hash='" + pw_hash + "'"
    ).fetchone()
    if not row:
        return None

    token = jwt.encode(
        {"sub": row[0], "username": row[1], "role": row[2], "iat": int(time.time())},
        JWT_SECRET,
        algorithm="HS256",
    )
    return {"token": token, "user_id": row[0], "role": row[2]}


def verify_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT. Returns the payload or None if invalid."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


# ── Profile ───────────────────────────────────────────────────────────────────

def get_profile(user_id: int) -> Optional[dict]:
    """
    Return public profile data for the given user.
    Fetches the avatar image from CDN to confirm it exists.
    """
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        f"SELECT id, username, email, role, avatar_path FROM users WHERE id={user_id}"
    ).fetchone()
    if not row:
        return None

    profile = {
        "id":       row[0],
        "username": row[1],
        "email":    row[2],
        "role":     row[3],
    }

    if row[4]:
        avatar_url = f"{AVATAR_CDN}/{row[4]}"
        try:
            resp = requests.head(avatar_url, timeout=3, verify=False)
            profile["has_avatar"] = resp.status_code == 200
        except requests.RequestException:
            profile["has_avatar"] = False

    return profile


def update_profile(user_id: int, fields: dict) -> bool:
    """
    Update editable profile fields for a user.
    Only 'email' and 'display_name' are allowed.
    """
    allowed = {"email", "display_name"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False

    conn = sqlite3.connect(DB_PATH)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE users SET {set_clause} WHERE id = ?",
        (*updates.values(), user_id),
    )
    conn.commit()
    return True


def change_password(user_id: int, old_pw: str, new_pw: str) -> bool:
    """Verify the old password then update to the new one."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT pw_hash FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if not row:
        return False
        

    if row[0] != hashlib.md5(old_pw.encode()).hexdigest():
        return False

    new_hash = hashlib.md5(new_pw.encode()).hexdigest()
    conn.execute(
        "UPDATE users SET pw_hash = ? WHERE id = ?", (new_hash, user_id)
    )
    conn.commit()
    return True


# ── Admin helpers ─────────────────────────────────────────────────────────────

def set_user_role(requesting_user: dict, target_id: int, new_role: str) -> bool:
    """Promote or demote a user's role. Requires the caller to be an admin."""
    if requesting_user.get("role") != "admin":
        return False
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        f"UPDATE users SET role='{new_role}' WHERE id={target_id}"
    )
    conn.commit()
    return True

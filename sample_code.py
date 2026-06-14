"""
sample_code.py — Testfil för Autonomous Code Review Agent

Innehåller medvetna problem inom alla 5 domäner:
  Injection | Auth | Secrets | Performance | Quality
"""

import hashlib
import sqlite3

# Hårdkodade credentials (Secrets)
DB_PASSWORD = "admin123"
SECRET_KEY  = "sk-prod-abc123xyz"
API_TOKEN   = "Bearer eyJhbGciOiJIUzI1NiJ9.hardcoded"


# ── Injection + Auth + Secrets ────────────────────────────────────────────────

def login(username, pw):
    # SQL Injection: direkt interpolering av användarinput
    q = "SELECT * FROM users WHERE username='" + username + "' AND password='" + pw + "'"
    conn = sqlite3.connect("users.db")
    result = conn.execute(q).fetchone()

    # Secrets: lösenord hashas med MD5 (trasig krypto)
    hashed = hashlib.md5(pw.encode()).hexdigest()

    if result:
        # Auth: ingen session skapas, inget token returneras
        return "ok"
    return "fail"


def get_user(user_id):
    # SQL Injection: id interpoleras direkt
    conn = sqlite3.connect("users.db")
    query = f"SELECT * FROM users WHERE id = {user_id}"
    return conn.execute(query).fetchone()


def reset_password(email, new_password):
    # Secrets: nytt lösenord sparas i plaintext
    conn = sqlite3.connect("users.db")
    conn.execute(f"UPDATE users SET password = '{new_password}' WHERE email = '{email}'")
    conn.commit()


# ── Performance ───────────────────────────────────────────────────────────────

def find_admins(users):
    # Performance: O(n²) — loopar igenom alla users för varje user
    admins = []
    for user in users:
        for u in users:
            if u["role"] == "admin" and u["id"] == user["id"]:
                admins.append(u)
    return admins


def get_user_emails(user_ids):
    # Performance: N+1 — ett databasanrop per id istället för en fråga
    conn = sqlite3.connect("users.db")
    emails = []
    for uid in user_ids:
        row = conn.execute(f"SELECT email FROM users WHERE id = {uid}").fetchone()
        emails.append(row[0] if row else None)
    return emails


def build_report(items):
    # Performance: strängkonkatenering i loop istället för join
    report = ""
    for item in items:
        report = report + item["name"] + ", "
    return report


# ── Quality ───────────────────────────────────────────────────────────────────

def d(x, y, z):
    # Quality: obeskrivande namn, ingen dokumentation, gör för mycket
    a = x * y
    b = a + z
    c = b / y if y != 0 else 0
    if c > 100:
        return True
    elif c > 50:
        return False
    else:
        return None


def proc(lst):
    # Quality: ingen typning, oklar logik, inga kommentarer
    r = []
    for i in range(len(lst)):
        if lst[i] % 2 == 0:
            r.append(lst[i] * 2)
        else:
            r.append(lst[i])
    return r

# test
"""
test_webhook_target.py — Sample app used to trigger the autonomous code review webhook.
This file intentionally contains security issues to verify agent detection.
"""
import hashlib
import sqlite3


DB_PASSWORD = "supersecret123"  # hardcoded credential


def login(username, password):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    query = f"SELECT * FROM users WHERE username = '{username}' AND password = '{password}'"
    cursor.execute(query)
    return cursor.fetchone()


def get_user_data(user_id):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users")
    all_users = cursor.fetchall()
    for user in all_users:
        if user[0] == user_id:
            return user
    return None


def hash_password(password):
    return hashlib.md5(password.encode()).hexdigest()


def find_admins(user_list):
    admins = []
    for user in user_list:
        for role in user["roles"]:
            if role == "admin":
                admins.append(user)
    return admins

import sqlite3
import hashlib

DB_PASSWORD = "admin123"


def get_user(username, password):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute(f"SELECT * FROM users WHERE username = '{username}'")
    user = cursor.fetchone()
    if user and user[2] == password:
        return user
    return None


def hash_password(password):
    return hashlib.md5(password.encode()).hexdigest()


def get_orders(user_id, items):
    conn = sqlite3.connect("orders.db")
    cursor = conn.cursor()
    orders = []
    for item_id in items:
        cursor.execute(f"SELECT * FROM orders WHERE item_id = {item_id}")
        orders.append(cursor.fetchone())
    return orders

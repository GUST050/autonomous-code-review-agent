import hashlib
import sqlite3

SECRET_KEY = "mypassword123"


def login(username, password):
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    query = f"SELECT * FROM users WHERE username = '{username}' AND password = '{password}'"
    cursor.execute(query)
    return cursor.fetchone()


def get_user(user_id):
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


def find_admins(users):
    admins = []
    for user in users:
        for role in user["roles"]:
            if role == "admin":
                admins.append(user)
    return admins

import eventlet
eventlet.monkey_patch()

import os
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session
from flask_socketio import SocketIO, send, emit
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_key")

socketio = SocketIO(app, cors_allowed_origins="*", manage_session=True)

DB_NAME = "users.db"

# ---------------- ONLINE USERS (IN-MEMORY) ---------------- #
online_users = set()

# ---------------- DATABASE ---------------- #

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            message TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)

    db.commit()
    db.close()


init_db()

# ---------------- AUTH ---------------- #

@app.route("/", methods=["GET", "POST"])
def index():
    error = None

    if request.method == "POST":
        action = request.form.get("action")
        username = request.form.get("username")
        password = request.form.get("password")

        if not username or not password:
            error = "All fields required"
            return render_template("index.html", error=error)

        db = get_db()
        cursor = db.cursor()

        if action == "register":
            try:
                cursor.execute(
                    "INSERT INTO users (username, password) VALUES (?, ?)",
                    (username, generate_password_hash(password))
                )
                db.commit()
                session["user"] = username
                return redirect(url_for("dashboard"))
            except sqlite3.IntegrityError:
                error = "Username already exists"

        elif action == "login":
            cursor.execute("SELECT * FROM users WHERE username=?", (username,))
            user = cursor.fetchone()
            if user and check_password_hash(user["password"], password):
                session["user"] = username
                return redirect(url_for("dashboard"))
            else:
                error = "Invalid credentials"

        db.close()

    return render_template("index.html", error=error)


@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect(url_for("index"))

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM messages ORDER BY id ASC")
    messages = cursor.fetchall()
    db.close()

    return render_template(
        "dashboard.html",
        user=session["user"],
        messages=messages
    )


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("index"))

# ---------------- SOCKET EVENTS ---------------- #

@socketio.on("connect")
def handle_connect():
    if "user" not in session:
        return

    username = session["user"]
    online_users.add(username)

    send(f"{username} joined the chat", broadcast=True)
    emit("online_users", list(online_users), broadcast=True)


@socketio.on("message")
def handle_message(msg):
    if "user" not in session:
        return

    username = session["user"]
    timestamp = datetime.now().strftime("%H:%M")

    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO messages (username, message, timestamp) VALUES (?, ?, ?)",
        (username, msg, timestamp)
    )
    db.commit()
    db.close()

    send(f"[{timestamp}] {username}: {msg}", broadcast=True)


@socketio.on("disconnect")
def handle_disconnect():
    if "user" not in session:
        return

    username = session["user"]
    online_users.discard(username)

    send(f"{username} left the chat", broadcast=True)
    emit("online_users", list(online_users), broadcast=True)

# ---------------- RUN ---------------- #

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
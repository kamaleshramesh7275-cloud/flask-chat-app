import os
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session
from flask_socketio import SocketIO, emit, join_room
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_key")

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    manage_session=True,
    async_mode="threading"
)

DB_NAME = "users.db"

online_users = set()


def get_db():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        message TEXT,
        timestamp TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS private_messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender TEXT,
        receiver TEXT,
        message TEXT,
        timestamp TEXT,
        status TEXT DEFAULT 'sent'
    )
    """)

    db.commit()
    db.close()


init_db()


def get_room(user1, user2):
    users = sorted([user1, user2])
    return f"{users[0]}_{users[1]}"


@app.route("/", methods=["GET", "POST"])
def index():

    error = None

    if request.method == "POST":

        action = request.form.get("action")
        username = request.form.get("username")
        password = request.form.get("password")

        db = get_db()
        cursor = db.cursor()

        if action == "register":

            try:

                cursor.execute(
                    "INSERT INTO users(username,password) VALUES (?,?)",
                    (username, generate_password_hash(password))
                )

                db.commit()

                session["user"] = username

                return redirect(url_for("dashboard"))

            except sqlite3.IntegrityError:
                error = "Username exists"

        elif action == "login":

            cursor.execute(
                "SELECT * FROM users WHERE username=?",
                (username,)
            )

            user = cursor.fetchone()

            if user and check_password_hash(user["password"], password):

                session["user"] = username

                return redirect(url_for("dashboard"))

            else:
                error = "Invalid login"

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

    cursor.execute(
        "SELECT username FROM users WHERE username != ?",
        (session["user"],)
    )

    users = cursor.fetchall()

    db.close()

    return render_template(
        "dashboard.html",
        user=session["user"],
        messages=messages,
        users=users
    )


@app.route("/logout")
def logout():

    session.pop("user", None)

    return redirect(url_for("index"))





@socketio.on("connect")
def handle_connect():

    if "user" not in session:
        return

    username = session["user"]

    online_users.add(username)


    join_room(username)

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
        "INSERT INTO messages(username,message,timestamp) VALUES(?,?,?)",
        (username, msg, timestamp)
    )

    db.commit()
    db.close()

    emit(
        "message",
        {
            "username": username,
            "message": msg,
            "time": timestamp
        },
        broadcast=True
    )


@socketio.on("join_private")
def join_private(data):

    if "user" not in session:
        return

    current = session["user"]
    other = data["user"]

    room = get_room(current, other)

    join_room(room)


@socketio.on("private_message")
def private_message(data):

    if "user" not in session:
        return

    sender = session["user"]
    receiver = data["receiver"]
    message = data["message"]

    room = get_room(sender, receiver)

    timestamp = datetime.now().strftime("%H:%M")

    db = get_db()
    cursor = db.cursor()

    cursor.execute(
        """INSERT INTO private_messages
        (sender,receiver,message,timestamp,status)
        VALUES (?,?,?,?,?)""",
        (sender, receiver, message, timestamp, "sent")
    )

    msg_id = cursor.lastrowid

    db.commit()
    db.close()

    emit(
        "private_message",
        {
            "id": msg_id,
            "sender": sender,
            "receiver": receiver,
            "message": message,
            "time": timestamp,
            "status": "sent"
        },
        room=room
    )


@socketio.on("message_read")
def message_read(data):

    msg_id = data["id"]

    db = get_db()
    cursor = db.cursor()

    cursor.execute(
        "UPDATE private_messages SET status='read' WHERE id=?",
        (msg_id,)
    )

    db.commit()
    db.close()

    emit(
        "message_status",
        {
            "id": msg_id,
            "status": "read"
        },
        broadcast=True
    )


@socketio.on("disconnect")
def handle_disconnect():

    username = session.get("user")

    if not username:
        return

    online_users.discard(username)

    emit("online_users", list(online_users), broadcast=True)


if __name__ == "__main__":

    port = int(os.environ.get("PORT", 5000))

    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=True,
        allow_unsafe_werkzeug=True
    )
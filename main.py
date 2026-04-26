from flask import Flask, render_template, request
from flask_socketio import SocketIO
from datetime import datetime
import sqlite3
import bcrypt
from cryptography.fernet import Fernet
import os

# ---------------- APP ----------------
app = Flask(__name__)
app.config["SECRET_KEY"] = "secret"

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ---------------- DATABASE ----------------
def get_db():
    conn = sqlite3.connect("chat.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY,
        password TEXT
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        to_user TEXT DEFAULT 'global',
        group_id INTEGER DEFAULT 0,
        message TEXT,
        time TEXT,
        msg_type TEXT DEFAULT 'text',
        is_read INTEGER DEFAULT 0,
        reactions TEXT DEFAULT '{}'
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        created_by TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS group_members (
        group_id INTEGER,
        username TEXT
    )
    """)
    conn.commit()
    conn.close()

init_db()

# ---------------- SESSION ----------------
users = {}      # sid -> username
user_sid = {}   # username -> sid

# ---------------- ENCRYPTION (FIXED KEY) ----------------
# IMPORTANT: keep this constant forever
SECRET_KEY = b'6VZr3X0nJ9vYkW8zPqL4hT2sU1xC5aB7D8eF0gH1Ijk='
cipher = Fernet(SECRET_KEY)

def encrypt_msg(msg):
    if not msg: return ""
    return cipher.encrypt(msg.encode()).decode()

def decrypt_msg(msg):
    if not msg: return ""
    try:
        return cipher.decrypt(msg.encode()).decode()
    except Exception as e:
        print(f"Decryption failed for message: {msg[:20]}... Error: {e}")
        return "[decrypt error]"

# ---------------- PASSWORD ----------------
def hash_password(password):
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def check_password(password, hashed):
    return bcrypt.checkpw(password.encode(), hashed.encode())

# ---------------- AUTH ----------------
@socketio.on("register")
def register(data):
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    if not username or not password:
        socketio.emit("auth", {"status": "error", "msg": "Invalid input"})
        return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username=?", (username,))
    if cursor.fetchone():
        conn.close()
        socketio.emit("auth", {"status": "error", "msg": "User exists"})
        return

    cursor.execute(
        "INSERT INTO users VALUES (?,?)",
        (username, hash_password(password))
    )
    conn.commit()
    conn.close()

    socketio.emit("auth", {"status": "ok", "msg": "Registered", "user": username})


@socketio.on("login")
def login(data):
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT password FROM users WHERE username=?", (username,))
    row = cursor.fetchone()

    if not row or not check_password(password, row[0]):
        conn.close()
        socketio.emit("auth", {"status": "error", "msg": "Invalid login"})
        return

    users[request.sid] = username
    user_sid[username] = request.sid

    socketio.emit("auth", {"status": "ok", "msg": "Login success", "user": username})

    # SEND GLOBAL HISTORY
    cursor.execute("SELECT username, message, time FROM messages WHERE to_user='global' ORDER BY id ASC")
    rows = cursor.fetchall()
    conn.close()

    history = []
    for r in rows:
        history.append({
            "user": r[0],
            "msg": decrypt_msg(r[1]),
            "time": r[2]
        })

    socketio.emit("history", history)
    emit_users()

@socketio.on("get_history")
def get_private_history(data):
    username = users.get(request.sid)
    other = data.get("other")
    if not username or not other: return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT username, message, time FROM messages 
        WHERE (username=? AND to_user=?) OR (username=? AND to_user=?)
        ORDER BY id ASC
    """, (username, other, other, username))
    rows = cursor.fetchall()
    conn.close()

    history = [{
        "user": r[0],
        "msg": decrypt_msg(r[1]),
        "time": r[2]
    } for r in rows]

    socketio.emit("history", history, room=request.sid)


# ---------------- CHAT ----------------
@socketio.on("message")
def handle_message(data):
    username = users.get(request.sid)
    if not username: return

    msg = data.get("msg", "").strip()
    to = data.get("to", "global")
    if not msg: return

    time_now = datetime.now().strftime("%H:%M:%S")
    encrypted = encrypt_msg(msg)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO messages (username, to_user, message, time) VALUES (?,?,?,?)",
        (username, to, encrypted, time_now)
    )
    conn.commit()
    conn.close()

    payload = {
        "user": username,
        "to": to,
        "msg": msg,
        "time": time_now
    }

    if to == "global":
        socketio.emit("message", payload)
    else:
        # Private message: Send to both sender and receiver
        if to in user_sid:
            socketio.emit("message", payload, room=user_sid[to])
        socketio.emit("message", payload, room=request.sid)


# ---------------- TYPING ----------------
@socketio.on("typing")
def handle_typing(data):
    username = users.get(request.sid)
    to = data.get("to")
    if username and to:
        if to == "global":
            socketio.emit("is_typing", {"user": username, "to": "global"}, skip_sid=request.sid)
        elif to in user_sid:
            socketio.emit("is_typing", {"user": username, "to": to}, room=user_sid[to])

# ---------------- REACTIONS ----------------
@socketio.on("react")
def handle_reaction(data):
    username = users.get(request.sid)
    msg_id = data.get("msg_id")
    emoji = data.get("emoji")
    if not username or not msg_id: return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT reactions FROM messages WHERE id=?", (msg_id,))
    row = cursor.fetchone()
    if row:
        import json
        reacts = json.loads(row[0])
        reacts[username] = emoji
        cursor.execute("UPDATE messages SET reactions=? WHERE id=?", (json.dumps(reacts), msg_id))
        conn.commit()
        socketio.emit("reaction_update", {"msg_id": msg_id, "reactions": reacts})
    conn.close()

# ---------------- GROUPS ----------------
@socketio.on("create_group")
def create_group(data):
    username = users.get(request.sid)
    name = data.get("name")
    members = data.get("members", [])
    if not username or not name: return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO groups (name, created_by) VALUES (?,?)", (name, username))
    group_id = cursor.lastrowid
    cursor.execute("INSERT INTO group_members (group_id, username) VALUES (?,?)", (group_id, username))
    for m in members:
        cursor.execute("INSERT INTO group_members (group_id, username) VALUES (?,?)", (group_id, m))
    conn.commit()
    conn.close()
    emit_users()

@socketio.on("get_all_groups")
def handle_get_all_groups():
    conn = get_db()
    cursor = conn.cursor()
    groups = cursor.execute("SELECT * FROM groups").fetchall()
    conn.close()
    socketio.emit("all_groups_list", [{"id": g[0], "name": g[1]} for g in groups], room=request.sid)

@socketio.on("join_group")
def handle_join_group(data):
    username = users.get(request.sid)
    group_id = data.get("id")
    if not username or not group_id: return
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM group_members WHERE group_id=? AND username=?", (group_id, username))
    if not cursor.fetchone():
        cursor.execute("INSERT INTO group_members (group_id, username) VALUES (?,?)", (group_id, username))
        conn.commit()
    conn.close()
    emit_users()

@socketio.on("mark_read")
def mark_read(data):
    username = users.get(request.sid)
    other = data.get("other")
    if not username or not other: return
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE messages SET is_read=1 WHERE username=? AND to_user=?", (other, username))
    conn.commit()
    conn.close()
    if other in user_sid:
        socketio.emit("read_update", {"user": username}, room=user_sid[other])
def emit_users():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT username FROM users")
    all_users = [row[0] for row in cursor.fetchall()]
    conn.close()

    online_users = list(user_sid.keys())

    socketio.emit("users", {
        "all": all_users,
        "online": online_users
    })


@socketio.on("disconnect")
def disconnect():
    sid = request.sid

    if sid in users:
        name = users[sid]
        users.pop(sid, None)
        user_sid.pop(name, None)

        emit_users()


# ---------------- ROUTE ----------------
@app.route("/")
def home():
    return render_template("index.html")


# ---------------- RUN ----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=True, allow_unsafe_werkzeug=True)
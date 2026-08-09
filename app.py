import os
import re
import sqlite3
import secrets
import hashlib
import uuid

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import (
    FastAPI,
    Request,
    Response,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    UploadFile,
    File,
    Form,
)
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pwdlib import PasswordHash

import uvicorn


# ============================================================
# Пути
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_FILE = DATA_DIR / "messenger.db"

DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)


# ============================================================
# Настройки
# ============================================================

SESSION_DAYS = 14
MAX_FILE_SIZE = 20 * 1024 * 1024

# Режим запуска:
# development - локальное тестирование по HTTP
# production  - публикация за HTTPS reverse proxy
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
PRODUCTION = APP_ENV == "production"

# В production cookie всегда Secure.
# В development при необходимости можно включить COOKIE_SECURE=1.
COOKIE_SECURE = (
    PRODUCTION
    or os.getenv("COOKIE_SECURE", "0") == "1"
)

STATIC_VERSION = "20260809-prod1"

ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

password_hash = PasswordHash.recommended()

APP_VERSION = "2026.08.09-pwa-stage1-fix1"

app = FastAPI()

app.mount(
    "/static",
    StaticFiles(directory=STATIC_DIR),
    name="static"
)


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path

    # API и защищённые вложения не кешируем.
    if (
        path.startswith("/api/")
        or path.startswith("/attachment/")
        or path.startswith("/api/attachments/")
    ):
        response.headers["Cache-Control"] = "no-store"
        return response

    if PRODUCTION:
        # HTML всегда перепроверяется у сервера.
        if path in {"/", "/login", "/chat", "/admin"}:
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
            return response

        # Логотипы / favicon — 30 дней.
        if path.startswith("/static/image/"):
            response.headers["Cache-Control"] = (
                "public, max-age=2592000, immutable"
            )
            return response

        # CSS и другая статика — 7 дней.
        if path.startswith("/static/"):
            response.headers["Cache-Control"] = (
                "public, max-age=604800, immutable"
            )
            return response

    else:
        # В development кеш отключён для удобной разработки.
        if (
            path.startswith("/static/")
            or path in {"/", "/login", "/chat", "/admin"}
        ):
            response.headers["Cache-Control"] = (
                "no-store, no-cache, must-revalidate, max-age=0"
            )
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"

    return response


@app.get("/api/version")
def api_version():
    return {"version": APP_VERSION}


# ============================================================
# Вспомогательные функции
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def now_string():
    return now_utc().isoformat()


def db():
    con = sqlite3.connect(
        DB_FILE,
        timeout=10.0
    )
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 10000")
    con.execute("PRAGMA foreign_keys = ON")
    return con


def token_hash(token: str):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ============================================================
# База данных
# ============================================================

def init_db():
    with db() as con:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_seen TEXT
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT UNIQUE NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                receiver_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_read INTEGER NOT NULL DEFAULT 0,
                reply_to_message_id INTEGER,
                FOREIGN KEY(sender_id) REFERENCES users(id),
                FOREIGN KEY(receiver_id) REFERENCES users(id),
                FOREIGN KEY(reply_to_message_id) REFERENCES messages(id)
            )
        """)

        # Миграция старой базы: добавляем поле ответов, если его ещё нет.
        message_columns = {
            row["name"]
            for row in con.execute("PRAGMA table_info(messages)").fetchall()
        }

        if "reply_to_message_id" not in message_columns:
            con.execute("""
                ALTER TABLE messages
                ADD COLUMN reply_to_message_id INTEGER
            """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                FOREIGN KEY(message_id) REFERENCES messages(id)
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS chat_clears (
                user_id INTEGER NOT NULL,
                peer_id INTEGER NOT NULL,
                cleared_message_id INTEGER NOT NULL DEFAULT 0,
                cleared_at TEXT NOT NULL,
                PRIMARY KEY (user_id, peer_id),
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(peer_id) REFERENCES users(id)
            )
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_users
            ON messages(sender_id, receiver_id)
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_receiver
            ON messages(receiver_id, is_read)
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_attachments_message
            ON attachments(message_id)
        """)


def generate_admin_password():
    alphabet = (
        "ABCDEFGHJKLMNPQRSTUVWXYZ"
        "abcdefghijkmnopqrstuvwxyz"
        "23456789"
    )

    raw = "".join(
        secrets.choice(alphabet)
        for _ in range(20)
    )

    return "-".join(
        raw[i:i + 4]
        for i in range(0, len(raw), 4)
    )


def create_initial_admin():
    with db() as con:
        row = con.execute("""
            SELECT id
            FROM users
            WHERE is_admin = 1
            LIMIT 1
        """).fetchone()

        if row:
            return

        username = (
            os.getenv("ADMIN_USER", "admin").strip()
            or "admin"
        )

        configured_password = os.getenv(
            "ADMIN_PASSWORD",
            ""
        ).strip()

        password = (
            configured_password
            if configured_password
            else generate_admin_password()
        )

        hashed = password_hash.hash(password)

        con.execute("""
            INSERT INTO users
            (
                username,
                display_name,
                password_hash,
                is_admin,
                is_active,
                created_at
            )
            VALUES (?, ?, ?, 1, 1, ?)
        """, (
            username,
            "Администратор",
            hashed,
            now_string()
        ))

        print()
        print("=" * 68)
        print("СОЗДАН ПЕРВЫЙ АДМИНИСТРАТОР")
        print()
        print(f"Логин:  {username}")
        print(f"Пароль: {password}")
        print()
        print("СОХРАНИТЕ ПАРОЛЬ!")
        print("Он показывается только при создании первого admin.")
        print("В базе хранится только Argon2-хеш.")
        print("=" * 68)
        print()


def cleanup_expired_sessions():
    with db() as con:
        con.execute("""
            DELETE FROM sessions
            WHERE expires_at < ?
        """, (
            now_string(),
        ))


init_db()
cleanup_expired_sessions()
create_initial_admin()


# ============================================================
# Авторизация
# ============================================================

def get_user_by_session_token(token: str | None):
    if not token:
        return None

    hashed = token_hash(token)

    with db() as con:
        row = con.execute("""
            SELECT
                u.id,
                u.username,
                u.display_name,
                u.is_admin,
                u.is_active,
                u.last_seen,
                s.expires_at
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
        """, (hashed,)).fetchone()

    if not row:
        return None

    if not row["is_active"]:
        return None

    expires_at = datetime.fromisoformat(row["expires_at"])

    if expires_at < now_utc():
        with db() as con:
            con.execute("""
                DELETE FROM sessions
                WHERE token_hash = ?
            """, (
                hashed,
            ))
        return None

    return dict(row)


def current_user(request: Request):
    token = request.cookies.get("session_token")
    user = get_user_by_session_token(token)

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Требуется авторизация"
        )

    return user


def current_admin(request: Request):
    user = current_user(request)

    if not user["is_admin"]:
        raise HTTPException(
            status_code=403,
            detail="Недостаточно прав"
        )

    return user


# ============================================================
# Pydantic модели
# ============================================================

class LoginData(BaseModel):
    username: str
    password: str


class UserCreate(BaseModel):
    username: str
    display_name: str
    password: str


class PasswordReset(BaseModel):
    password: str


# ============================================================
# Ответы на сообщения
# ============================================================

def get_cleared_message_id(user_id: int, peer_id: int):
    with db() as con:
        row = con.execute("""
            SELECT cleared_message_id
            FROM chat_clears
            WHERE
                user_id = ?
                AND peer_id = ?
        """, (
            user_id,
            peer_id
        )).fetchone()

    return (
        row["cleared_message_id"]
        if row
        else 0
    )


def get_reply_info(
    reply_to_message_id: int | None,
    viewer_id: int | None = None,
    peer_id: int | None = None,
    admin_view: bool = False
):
    if not reply_to_message_id:
        return None

    # Для обычного пользователя нельзя показывать цитату
    # из уже очищенной им части переписки.
    if not admin_view:
        if viewer_id is None or peer_id is None:
            return None

        cleared_message_id = get_cleared_message_id(
            viewer_id,
            peer_id
        )

        if reply_to_message_id <= cleared_message_id:
            return None

    with db() as con:
        row = con.execute("""
            SELECT
                m.id,
                m.sender_id,
                m.receiver_id,
                m.text,
                u.display_name AS sender_name,
                a.id AS attachment_id,
                a.original_name AS attachment_name,
                a.content_type AS attachment_content_type
            FROM messages m
            JOIN users u
                ON u.id = m.sender_id
            LEFT JOIN attachments a
                ON a.message_id = m.id
            WHERE m.id = ?
        """, (
            reply_to_message_id,
        )).fetchone()

    if not row:
        return None

    # Защита: цитируемое сообщение должно относиться
    # к той же самой личной переписке.
    if not admin_view:
        participants = {
            row["sender_id"],
            row["receiver_id"]
        }

        if participants != {
            viewer_id,
            peer_id
        }:
            return None

    text = (row["text"] or "").strip()

    if len(text) > 180:
        text = text[:177] + "..."

    is_image = (
        row["attachment_content_type"]
        in ALLOWED_IMAGE_TYPES
        if row["attachment_content_type"]
        else False
    )

    return {
        "id": row["id"],
        "sender_id": row["sender_id"],
        "sender_name": row["sender_name"],
        "text": text,
        "has_image": bool(is_image),
        "attachment_name": row["attachment_name"]
    }


# ============================================================
# Вложения
# ============================================================

def get_attachment(message_id: int):
    with db() as con:
        row = con.execute("""
            SELECT
                id,
                message_id,
                original_name,
                stored_name,
                content_type,
                size
            FROM attachments
            WHERE message_id = ?
            LIMIT 1
        """, (message_id,)).fetchone()

    if not row:
        return None

    item = dict(row)
    item["url"] = f"/api/attachments/{item['id']}"
    item["is_image"] = item["content_type"] in ALLOWED_IMAGE_TYPES

    return item


# ============================================================
# HTML страницы
# ============================================================


@app.get("/manifest.webmanifest", include_in_schema=False)
def pwa_manifest():
    response = FileResponse(
        STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json"
    )
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/service-worker.js", include_in_schema=False)
def pwa_service_worker():
    response = FileResponse(
        STATIC_DIR / "service-worker.js",
        media_type="application/javascript"
    )
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.get("/")
def root(request: Request):
    token = request.cookies.get("session_token")
    user = get_user_by_session_token(token)

    if not user:
        return FileResponse(STATIC_DIR / "login.html")

    if user["is_admin"]:
        return RedirectResponse("/admin")

    return RedirectResponse("/chat")


@app.get("/login")
def login_page():
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/chat")
def chat_page(request: Request):
    token = request.cookies.get("session_token")
    user = get_user_by_session_token(token)

    if not user:
        return RedirectResponse("/login", status_code=302)

    if user["is_admin"]:
        return RedirectResponse("/admin")

    return FileResponse(STATIC_DIR / "chat.html")


@app.get("/admin")
def admin_page(request: Request):
    token = request.cookies.get("session_token")
    user = get_user_by_session_token(token)

    if not user:
        return RedirectResponse("/login", status_code=302)

    if not user["is_admin"]:
        return RedirectResponse("/chat", status_code=302)

    return FileResponse(STATIC_DIR / "admin.html")


# ============================================================
# Login / Logout
# ============================================================

@app.post("/api/login")
def login(data: LoginData, response: Response):
    username = data.username.strip()

    with db() as con:
        row = con.execute("""
            SELECT *
            FROM users
            WHERE username = ?
        """, (username,)).fetchone()

    if not row:
        raise HTTPException(401, "Неверный логин или пароль")

    if not row["is_active"]:
        raise HTTPException(403, "Пользователь заблокирован")

    try:
        password_ok = password_hash.verify(
            data.password,
            row["password_hash"]
        )
    except Exception:
        password_ok = False

    if not password_ok:
        raise HTTPException(401, "Неверный логин или пароль")

    token = secrets.token_urlsafe(48)
    expires = now_utc() + timedelta(days=SESSION_DAYS)

    with db() as con:
        con.execute("""
            INSERT INTO sessions
            (
                user_id,
                token_hash,
                expires_at
            )
            VALUES (?, ?, ?)
        """, (
            row["id"],
            token_hash(token),
            expires.isoformat()
        ))

    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=SESSION_DAYS * 86400,
        path="/"
    )

    return {
        "ok": True,
        "is_admin": bool(row["is_admin"])
    }


@app.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("session_token")

    if token:
        with db() as con:
            con.execute("""
                DELETE FROM sessions
                WHERE token_hash = ?
            """, (token_hash(token),))

    response.delete_cookie(
        "session_token",
        path="/"
    )

    return {"ok": True}


# ============================================================
# Информация о себе
# ============================================================

@app.get("/api/me")
def me(request: Request):
    user = current_user(request)

    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"],
        "is_admin": bool(user["is_admin"])
    }


# ============================================================
# Онлайн пользователи
# ============================================================

connections: dict[int, set[WebSocket]] = {}


def is_online(user_id: int):
    return (
        user_id in connections
        and len(connections[user_id]) > 0
    )


async def send_to_user(user_id: int, data: dict):
    sockets = list(connections.get(user_id, set()))
    dead = []

    for ws in sockets:
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)

    for ws in dead:
        if user_id in connections:
            connections[user_id].discard(ws)


async def broadcast(data: dict):
    for user_id in list(connections.keys()):
        await send_to_user(user_id, data)


# ============================================================
# Список пользователей
# ============================================================

@app.get("/api/users")
def users(request: Request):
    user = current_user(request)

    if user["is_admin"]:
        raise HTTPException(403)

    with db() as con:
        rows = con.execute("""
            SELECT
                u.id,
                u.username,
                u.display_name,
                u.last_seen,
                (
                    SELECT COUNT(*)
                    FROM messages m
                    WHERE
                        m.sender_id = u.id
                        AND m.receiver_id = ?
                        AND m.is_read = 0
                        AND m.id > COALESCE(
                            (
                                SELECT cc.cleared_message_id
                                FROM chat_clears cc
                                WHERE
                                    cc.user_id = ?
                                    AND cc.peer_id = u.id
                            ),
                            0
                        )
                ) AS unread
            FROM users u
            WHERE
                u.is_admin = 0
                AND u.is_active = 1
                AND u.id != ?
            ORDER BY u.display_name
        """, (
            user["id"],
            user["id"],
            user["id"]
        )).fetchall()

    result = []

    for row in rows:
        item = dict(row)
        item["online"] = is_online(item["id"])
        result.append(item)

    return result


# ============================================================
# История переписки
# ============================================================

@app.get("/api/messages/{peer_id}")
def messages(peer_id: int, request: Request):
    user = current_user(request)

    if user["is_admin"]:
        raise HTTPException(403)

    with db() as con:
        peer = con.execute("""
            SELECT id
            FROM users
            WHERE
                id = ?
                AND is_admin = 0
                AND is_active = 1
        """, (peer_id,)).fetchone()

        if not peer:
            raise HTTPException(404, "Пользователь не найден")

        con.execute("""
            UPDATE messages
            SET is_read = 1
            WHERE
                sender_id = ?
                AND receiver_id = ?
        """, (
            peer_id,
            user["id"]
        ))

        clear_row = con.execute("""
            SELECT cleared_message_id
            FROM chat_clears
            WHERE
                user_id = ?
                AND peer_id = ?
        """, (
            user["id"],
            peer_id
        )).fetchone()

        cleared_message_id = (
            clear_row["cleared_message_id"]
            if clear_row
            else 0
        )

        rows = con.execute("""
            SELECT
                m.id,
                m.sender_id,
                m.receiver_id,
                m.text,
                m.created_at,
                m.is_read,
                m.reply_to_message_id,
                u.display_name AS sender_name
            FROM messages m
            JOIN users u ON u.id = m.sender_id
            WHERE
                m.id > ?
                AND (
                    (
                        m.sender_id = ?
                        AND m.receiver_id = ?
                    )
                    OR
                    (
                        m.sender_id = ?
                        AND m.receiver_id = ?
                    )
                )
            ORDER BY m.id
        """, (
            cleared_message_id,
            user["id"],
            peer_id,
            peer_id,
            user["id"]
        )).fetchall()

    result = []

    for row in rows:
        item = dict(row)
        item["attachment"] = get_attachment(item["id"])
        item["reply"] = get_reply_info(
            item.get("reply_to_message_id"),
            viewer_id=user["id"],
            peer_id=peer_id
        )
        result.append(item)

    return result


@app.post("/api/messages/{peer_id}/read")
def mark_read(peer_id: int, request: Request):
    user = current_user(request)

    if user["is_admin"]:
        raise HTTPException(403)

    with db() as con:
        con.execute("""
            UPDATE messages
            SET is_read = 1
            WHERE
                sender_id = ?
                AND receiver_id = ?
        """, (
            peer_id,
            user["id"]
        ))

    return {"ok": True}


# ============================================================
# Очистка переписки только для текущего пользователя
# ============================================================

@app.post("/api/messages/{peer_id}/clear")
def clear_chat(peer_id: int, request: Request):
    user = current_user(request)

    if user["is_admin"]:
        raise HTTPException(403)

    if peer_id == user["id"]:
        raise HTTPException(400, "Некорректный собеседник")

    with db() as con:
        peer = con.execute("""
            SELECT id
            FROM users
            WHERE
                id = ?
                AND is_admin = 0
        """, (peer_id,)).fetchone()

        if not peer:
            raise HTTPException(404, "Пользователь не найден")

        row = con.execute("""
            SELECT MAX(id) AS max_id
            FROM messages
            WHERE
                (
                    sender_id = ?
                    AND receiver_id = ?
                )
                OR
                (
                    sender_id = ?
                    AND receiver_id = ?
                )
        """, (
            user["id"],
            peer_id,
            peer_id,
            user["id"]
        )).fetchone()

        cleared_message_id = (
            row["max_id"]
            if row and row["max_id"] is not None
            else 0
        )

        con.execute("""
            INSERT INTO chat_clears
            (
                user_id,
                peer_id,
                cleared_message_id,
                cleared_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, peer_id)
            DO UPDATE SET
                cleared_message_id = excluded.cleared_message_id,
                cleared_at = excluded.cleared_at
        """, (
            user["id"],
            peer_id,
            cleared_message_id,
            now_string()
        ))

        # Старые входящие сообщения больше не видны этому пользователю,
        # поэтому считаем их прочитанными для его счётчика.
        con.execute("""
            UPDATE messages
            SET is_read = 1
            WHERE
                sender_id = ?
                AND receiver_id = ?
                AND id <= ?
        """, (
            peer_id,
            user["id"],
            cleared_message_id
        ))

    return {
        "ok": True,
        "cleared_message_id": cleared_message_id
    }


# ============================================================
# Загрузка изображения
# ============================================================

@app.post("/api/upload-image")
async def upload_image(
    request: Request,
    receiver_id: int = Form(...),
    file: UploadFile = File(...),
    reply_to_message_id: int | None = Form(None)
):
    user = current_user(request)

    if user["is_admin"]:
        raise HTTPException(403)

    if receiver_id == user["id"]:
        raise HTTPException(400, "Нельзя отправить файл самому себе")

    content_type = (file.content_type or "").lower()

    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            400,
            "Разрешены только JPG, PNG, WebP и GIF"
        )

    with db() as con:
        receiver = con.execute("""
            SELECT id
            FROM users
            WHERE
                id = ?
                AND is_admin = 0
                AND is_active = 1
        """, (receiver_id,)).fetchone()

    if not receiver:
        raise HTTPException(404, "Получатель не найден")

    reply_info_for_sender = None

    if reply_to_message_id:
        reply_info_for_sender = get_reply_info(
            reply_to_message_id,
            viewer_id=user["id"],
            peer_id=receiver_id
        )

        if not reply_info_for_sender:
            # Если сообщение скрыто после очистки или не из этого чата,
            # не разрешаем создавать на него новый ответ.
            raise HTTPException(
                400,
                "Исходное сообщение недоступно"
            )

    temp_path = UPLOAD_DIR / f"{uuid.uuid4().hex}.tmp"
    size = 0

    try:
        with open(temp_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)

                if not chunk:
                    break

                size += len(chunk)

                if size > MAX_FILE_SIZE:
                    raise HTTPException(
                        413,
                        "Максимальный размер файла — 20 МБ"
                    )

                out.write(chunk)

        extension = ALLOWED_IMAGE_TYPES[content_type]
        stored_name = uuid.uuid4().hex + extension
        final_path = UPLOAD_DIR / stored_name
        temp_path.replace(final_path)

    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise

    finally:
        await file.close()

    original_name = Path(file.filename or "image").name
    created = now_string()

    with db() as con:
        cursor = con.execute("""
            INSERT INTO messages
            (
                sender_id,
                receiver_id,
                text,
                created_at,
                is_read,
                reply_to_message_id
            )
            VALUES (?, ?, ?, ?, 0, ?)
        """, (
            user["id"],
            receiver_id,
            "",
            created,
            reply_to_message_id
        ))

        message_id = cursor.lastrowid

        cursor = con.execute("""
            INSERT INTO attachments
            (
                message_id,
                original_name,
                stored_name,
                content_type,
                size
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            message_id,
            original_name,
            stored_name,
            content_type,
            size
        ))

        attachment_id = cursor.lastrowid

    base_message_data = {
        "type": "message",
        "id": message_id,
        "sender_id": user["id"],
        "receiver_id": receiver_id,
        "sender_name": user["display_name"],
        "text": "",
        "created_at": created,
        "reply_to_message_id": reply_to_message_id,
        "attachment": {
            "id": attachment_id,
            "original_name": original_name,
            "content_type": content_type,
            "size": size,
            "is_image": True,
            "url": f"/api/attachments/{attachment_id}"
        }
    }

    sender_data = dict(base_message_data)
    sender_data["reply"] = get_reply_info(
        reply_to_message_id,
        viewer_id=user["id"],
        peer_id=receiver_id
    )

    receiver_data = dict(base_message_data)
    receiver_data["reply"] = get_reply_info(
        reply_to_message_id,
        viewer_id=receiver_id,
        peer_id=user["id"]
    )

    await send_to_user(receiver_id, receiver_data)
    await send_to_user(user["id"], sender_data)

    return sender_data


# ============================================================
# Защищённая выдача изображений
# ============================================================

@app.get("/api/attachments/{attachment_id}")
def attachment(attachment_id: int, request: Request):
    user = current_user(request)

    with db() as con:
        row = con.execute("""
            SELECT
                a.*,
                m.sender_id,
                m.receiver_id
            FROM attachments a
            JOIN messages m ON m.id = a.message_id
            WHERE a.id = ?
        """, (attachment_id,)).fetchone()

    if not row:
        raise HTTPException(404, "Файл не найден")

    if not user["is_admin"]:
        allowed = (
            row["sender_id"] == user["id"]
            or row["receiver_id"] == user["id"]
        )

        if not allowed:
            raise HTTPException(403)

    file_path = UPLOAD_DIR / row["stored_name"]

    if not file_path.exists():
        raise HTTPException(404, "Файл отсутствует")

    return FileResponse(
        path=file_path,
        media_type=row["content_type"],
        filename=None
    )


# ============================================================
# WebSocket чата
# ============================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.cookies.get("session_token")
    user = get_user_by_session_token(token)

    if not user:
        await websocket.close(code=1008)
        return

    if user["is_admin"]:
        await websocket.close(code=1008)
        return

    user_id = user["id"]

    await websocket.accept()

    if user_id not in connections:
        connections[user_id] = set()

    first_connection = len(connections[user_id]) == 0
    connections[user_id].add(websocket)

    if first_connection:
        await broadcast({
            "type": "presence",
            "user_id": user_id,
            "online": True
        })

    try:
        while True:
            data = await websocket.receive_json()

            if data.get("type") != "message":
                continue

            receiver_id = int(data.get("receiver_id", 0))
            text = str(data.get("text", "")).strip()

            raw_reply_id = data.get("reply_to_message_id")
            reply_to_message_id = None

            if raw_reply_id not in (None, "", 0, "0"):
                try:
                    reply_to_message_id = int(raw_reply_id)
                except (TypeError, ValueError):
                    reply_to_message_id = None

            if not text:
                continue

            if len(text) > 10000:
                continue

            if receiver_id == user_id:
                continue

            with db() as con:
                receiver = con.execute("""
                    SELECT id
                    FROM users
                    WHERE
                        id = ?
                        AND is_admin = 0
                        AND is_active = 1
                """, (receiver_id,)).fetchone()

                if not receiver:
                    continue

                if reply_to_message_id:
                    reply_info = get_reply_info(
                        reply_to_message_id,
                        viewer_id=user_id,
                        peer_id=receiver_id
                    )

                    if not reply_info:
                        reply_to_message_id = None

                created = now_string()

                cursor = con.execute("""
                    INSERT INTO messages
                    (
                        sender_id,
                        receiver_id,
                        text,
                        created_at,
                        is_read,
                        reply_to_message_id
                    )
                    VALUES (?, ?, ?, ?, 0, ?)
                """, (
                    user_id,
                    receiver_id,
                    text,
                    created,
                    reply_to_message_id
                ))

                message_id = cursor.lastrowid

            base_message_data = {
                "type": "message",
                "id": message_id,
                "sender_id": user_id,
                "receiver_id": receiver_id,
                "sender_name": user["display_name"],
                "text": text,
                "created_at": created,
                "reply_to_message_id": reply_to_message_id,
                "attachment": None
            }

            sender_data = dict(base_message_data)
            sender_data["reply"] = get_reply_info(
                reply_to_message_id,
                viewer_id=user_id,
                peer_id=receiver_id
            )

            receiver_data = dict(base_message_data)
            receiver_data["reply"] = get_reply_info(
                reply_to_message_id,
                viewer_id=receiver_id,
                peer_id=user_id
            )

            await send_to_user(receiver_id, receiver_data)
            await send_to_user(user_id, sender_data)

    except WebSocketDisconnect:
        pass

    finally:
        if user_id in connections:
            connections[user_id].discard(websocket)

            if not connections[user_id]:
                del connections[user_id]

                with db() as con:
                    con.execute("""
                        UPDATE users
                        SET last_seen = ?
                        WHERE id = ?
                    """, (
                        now_string(),
                        user_id
                    ))

                await broadcast({
                    "type": "presence",
                    "user_id": user_id,
                    "online": False
                })


# ============================================================
# ADMIN
# ============================================================

@app.get("/api/admin/users")
def admin_users(request: Request):
    current_admin(request)

    with db() as con:
        rows = con.execute("""
            SELECT
                id,
                username,
                display_name,
                is_active,
                created_at,
                last_seen
            FROM users
            WHERE is_admin = 0
            ORDER BY display_name
        """).fetchall()

    result = []

    for row in rows:
        item = dict(row)
        item["online"] = is_online(item["id"])
        result.append(item)

    return result


@app.post("/api/admin/users")
def admin_create_user(data: UserCreate, request: Request):
    current_admin(request)

    username = data.username.strip()
    display_name = data.display_name.strip()
    password = data.password

    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise HTTPException(
            400,
            "Логин: 3-32 символа, латиница, цифры, _, -, ."
        )

    if not display_name:
        raise HTTPException(400, "Введите имя")

    if len(password) < 8:
        raise HTTPException(
            400,
            "Пароль должен быть не короче 8 символов"
        )

    hashed = password_hash.hash(password)

    try:
        with db() as con:
            con.execute("""
                INSERT INTO users
                (
                    username,
                    display_name,
                    password_hash,
                    is_admin,
                    is_active,
                    created_at
                )
                VALUES (?, ?, ?, 0, 1, ?)
            """, (
                username,
                display_name,
                hashed,
                now_string()
            ))
    except sqlite3.IntegrityError:
        raise HTTPException(400, "Такой логин уже существует")

    return {"ok": True}


@app.post("/api/admin/users/{user_id}/toggle")
def admin_toggle_user(user_id: int, request: Request):
    current_admin(request)

    with db() as con:
        row = con.execute("""
            SELECT is_active
            FROM users
            WHERE
                id = ?
                AND is_admin = 0
        """, (user_id,)).fetchone()

        if not row:
            raise HTTPException(404)

        new_value = 0 if row["is_active"] else 1

        con.execute("""
            UPDATE users
            SET is_active = ?
            WHERE id = ?
        """, (
            new_value,
            user_id
        ))

        if not new_value:
            con.execute("""
                DELETE FROM sessions
                WHERE user_id = ?
            """, (user_id,))

    return {
        "ok": True,
        "is_active": bool(new_value)
    }


@app.post("/api/admin/users/{user_id}/password")
def admin_reset_password(
    user_id: int,
    data: PasswordReset,
    request: Request
):
    current_admin(request)

    if len(data.password) < 8:
        raise HTTPException(400, "Минимум 8 символов")

    hashed = password_hash.hash(data.password)

    with db() as con:
        cursor = con.execute("""
            UPDATE users
            SET password_hash = ?
            WHERE
                id = ?
                AND is_admin = 0
        """, (
            hashed,
            user_id
        ))

        if cursor.rowcount == 0:
            raise HTTPException(404)

        con.execute("""
            DELETE FROM sessions
            WHERE user_id = ?
        """, (user_id,))

    return {"ok": True}


# ============================================================
# Администратор читает переписку
# ============================================================

@app.get("/api/admin/messages/{user1_id}/{user2_id}")
def admin_read_messages(
    user1_id: int,
    user2_id: int,
    request: Request
):
    current_admin(request)

    if user1_id == user2_id:
        raise HTTPException(400)

    with db() as con:
        rows = con.execute("""
            SELECT
                m.id,
                m.sender_id,
                m.receiver_id,
                m.text,
                m.created_at,
                m.reply_to_message_id,
                u.display_name AS sender_name
            FROM messages m
            JOIN users u ON u.id = m.sender_id
            WHERE
                (
                    m.sender_id = ?
                    AND m.receiver_id = ?
                )
                OR
                (
                    m.sender_id = ?
                    AND m.receiver_id = ?
                )
            ORDER BY m.id
        """, (
            user1_id,
            user2_id,
            user2_id,
            user1_id
        )).fetchall()

    result = []

    for row in rows:
        item = dict(row)
        item["attachment"] = get_attachment(item["id"])
        item["reply"] = get_reply_info(
            item.get("reply_to_message_id"),
            admin_view=True
        )
        result.append(item)

    return result


# ============================================================
# Запуск
# ============================================================

if __name__ == "__main__":
    print()
    print("=" * 68)
    print(f"Манул Чат: {APP_VERSION}")
    print(f"Режим: {APP_ENV}")
    print(
        "Secure cookie: "
        + ("ВКЛЮЧЕНА" if COOKIE_SECURE else "выключена")
    )

    if PRODUCTION:
        print("Production: используйте HTTPS через reverse proxy.")
    else:
        print("Development: обычный HTTP для локального тестирования.")

    print("=" * 68)
    print()

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000
    )

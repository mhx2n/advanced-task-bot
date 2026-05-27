import aiosqlite
import time
from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_seen INTEGER,
    first_seen INTEGER,
    is_banned INTEGER DEFAULT 0,
    msg_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER,
    level TEXT,
    user_id INTEGER,
    provider TEXT,
    message TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    chat_id INTEGER,
    message_id INTEGER,
    provider TEXT,
    state TEXT,
    updated_at INTEGER,
    PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS speak_grants (
    user_id INTEGER PRIMARY KEY,
    granted_at INTEGER
);
CREATE TABLE IF NOT EXISTS speak_active (
    user_id INTEGER PRIMARY KEY,
    target_chat_id INTEGER,
    updated_at INTEGER
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def upsert_user(user):
    now = int(time.time())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO users (user_id, username, first_name, last_seen, first_seen, msg_count)
               VALUES (?, ?, ?, ?, ?, 1)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=excluded.username,
                 first_name=excluded.first_name,
                 last_seen=excluded.last_seen,
                 msg_count=users.msg_count+1
            """,
            (user.id, user.username or "", user.first_name or "", now, now),
        )
        await db.commit()


async def is_banned(uid: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT is_banned FROM users WHERE user_id=?", (uid,)) as cur:
            row = await cur.fetchone()
            return bool(row and row[0])


async def set_banned(uid: int, val: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_banned=? WHERE user_id=?", (val, uid))
        await db.commit()


async def log(level: str, user_id: int, provider: str, message: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO logs (ts, level, user_id, provider, message) VALUES (?,?,?,?,?)",
            (int(time.time()), level, user_id or 0, provider or "", (message or "")[:2000]),
        )
        await db.commit()


async def get_logs(limit: int = 30):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT ts, level, user_id, provider, message FROM logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            return await cur.fetchall()


async def all_user_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE is_banned=0") as cur:
            return [r[0] for r in await cur.fetchall()]


async def stats():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*), COALESCE(SUM(msg_count),0), SUM(is_banned) FROM users") as cur:
            users, msgs, banned = await cur.fetchone()
        async with db.execute("SELECT COUNT(*) FROM logs WHERE level='ERROR'") as cur:
            errs = (await cur.fetchone())[0]
        return {
            "users": users or 0,
            "messages": msgs or 0,
            "banned": banned or 0,
            "errors": errs or 0,
        }


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()


async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else default


async def save_session(chat_id: int, message_id: int, provider: str, state: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO sessions(chat_id,message_id,provider,state,updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(chat_id,message_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at""",
            (chat_id, message_id, provider, state, int(time.time())),
        )
        await db.commit()


async def get_session(chat_id: int, message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT provider, state FROM sessions WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        ) as cur:
            return await cur.fetchone()


# ---------- speak-as-bot grants ----------
async def grant_speak(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO speak_grants(user_id, granted_at) VALUES(?,?)",
            (uid, int(time.time())),
        )
        await db.commit()


async def revoke_speak(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM speak_grants WHERE user_id=?", (uid,))
        await db.execute("DELETE FROM speak_active WHERE user_id=?", (uid,))
        await db.commit()


async def can_speak(uid: int, owner_id: int) -> bool:
    if uid == owner_id:
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM speak_grants WHERE user_id=?", (uid,)) as cur:
            return bool(await cur.fetchone())


async def list_speak_grants():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, granted_at FROM speak_grants") as cur:
            return await cur.fetchall()


async def set_speak_target(uid: int, chat_id):
    async with aiosqlite.connect(DB_PATH) as db:
        if chat_id is None:
            await db.execute("DELETE FROM speak_active WHERE user_id=?", (uid,))
        else:
            await db.execute(
                "INSERT OR REPLACE INTO speak_active(user_id,target_chat_id,updated_at) VALUES(?,?,?)",
                (uid, int(chat_id), int(time.time())),
            )
        await db.commit()


async def get_speak_target(uid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT target_chat_id FROM speak_active WHERE user_id=?", (uid,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

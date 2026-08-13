"""Cache local en SQLite.

Motivo: la API no oficial no tiene limites publicados, pero conviene no
martillearla. Ademas asi puedo consultar historico aunque Garmin este caido.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    email      TEXT,
    created_at TEXT NOT NULL
);
-- Los tokens OAuth de Garmin van cifrados: ver app/crypto.py. Se guardan como
-- el JSON que la propia libreria sabe cargar en linea, sin tocar disco.
CREATE TABLE IF NOT EXISTS garmin_sessions (
    user_id    TEXT PRIMARY KEY,
    tokens_enc TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS daily (
    user_id    TEXT NOT NULL,
    day        TEXT NOT NULL,
    payload    TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS activities (
    user_id     TEXT NOT NULL,
    activity_id TEXT NOT NULL,
    day         TEXT NOT NULL,
    payload     TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, activity_id)
);
CREATE INDEX IF NOT EXISTS idx_activities_day ON activities(user_id, day);
"""


def _necesita_migracion(c: sqlite3.Connection) -> bool:
    """¿Las tablas son del esquema mono-usuario, sin columna user_id?"""
    fila = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='daily'"
    ).fetchone()
    if not fila:
        return False
    columnas = {r["name"] for r in c.execute("PRAGMA table_info(daily)")}
    return "user_id" not in columnas


def _migrar_a_multiusuario(c: sqlite3.Connection) -> None:
    """Atribuye al dueño lo que se cacheo cuando esto era de un solo usuario.

    Se renombran las tablas viejas y se copian los datos, en vez de ALTER TABLE,
    porque hay que cambiar la clave primaria y SQLite no deja hacerlo en sitio.
    """
    dueño = settings.owner_user_id
    log.warning("Base mono-usuario detectada: migrando sus datos a '%s'", dueño)
    c.executescript("""
        ALTER TABLE daily RENAME TO daily_v1;
        ALTER TABLE activities RENAME TO activities_v1;
    """)
    c.executescript(SCHEMA)
    c.execute(
        "INSERT INTO users(user_id, email, created_at) VALUES(?, NULL, ?) "
        "ON CONFLICT(user_id) DO NOTHING",
        (dueño, _now()),
    )
    c.execute(
        "INSERT INTO daily(user_id, day, payload, fetched_at) "
        "SELECT ?, day, payload, fetched_at FROM daily_v1",
        (dueño,),
    )
    c.execute(
        "INSERT INTO activities(user_id, activity_id, day, payload, fetched_at) "
        "SELECT ?, activity_id, day, payload, fetched_at FROM activities_v1",
        (dueño,),
    )
    c.executescript("DROP TABLE daily_v1; DROP TABLE activities_v1;")


def init() -> None:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        if _necesita_migracion(c):
            _migrar_a_multiusuario(c)
        else:
            c.executescript(SCHEMA)


@contextmanager
def conn():
    c = sqlite3.connect(settings.db_path, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# --------------------------------------------------------------------- usuarios
def crear_usuario(user_id: str, email: str | None = None) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO users(user_id, email, created_at) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET email=COALESCE(excluded.email, users.email)",
            (user_id, email, _now()),
        )


def existe_usuario(user_id: str) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone() is not None


def listar_usuarios() -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT u.user_id, u.email, u.created_at, "
            "       (s.user_id IS NOT NULL) AS garmin_vinculado "
            "FROM users u LEFT JOIN garmin_sessions s ON s.user_id = u.user_id "
            "ORDER BY u.created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def borrar_usuario(user_id: str) -> None:
    """Elimina al usuario y todo lo suyo. Sin esto no hay derecho de supresion."""
    with conn() as c:
        for tabla in ("daily", "activities", "garmin_sessions", "users"):
            c.execute(f"DELETE FROM {tabla} WHERE user_id=?", (user_id,))


# ------------------------------------------------------------- sesion de Garmin
def guardar_tokens_garmin(user_id: str, tokens_cifrados: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO garmin_sessions(user_id, tokens_enc, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET tokens_enc=excluded.tokens_enc, "
            "updated_at=excluded.updated_at",
            (user_id, tokens_cifrados, _now()),
        )


def leer_tokens_garmin(user_id: str) -> str | None:
    with conn() as c:
        row = c.execute(
            "SELECT tokens_enc FROM garmin_sessions WHERE user_id=?", (user_id,)
        ).fetchone()
    return row["tokens_enc"] if row else None


def borrar_tokens_garmin(user_id: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM garmin_sessions WHERE user_id=?", (user_id,))


# ------------------------------------------------------------------ metricas
def save_daily(user_id: str, day: dt.date, payload: dict) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO daily(user_id, day, payload, fetched_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id, day) DO UPDATE SET payload=excluded.payload, "
            "fetched_at=excluded.fetched_at",
            (user_id, day.isoformat(), json.dumps(payload, ensure_ascii=False), _now()),
        )


def get_daily(user_id: str, day: dt.date) -> dict | None:
    with conn() as c:
        row = c.execute(
            "SELECT payload FROM daily WHERE user_id=? AND day=?", (user_id, day.isoformat())
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def get_daily_range(user_id: str, start: dt.date, end: dt.date) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT payload FROM daily WHERE user_id=? AND day BETWEEN ? AND ? ORDER BY day",
            (user_id, start.isoformat(), end.isoformat()),
        ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def all_daily(user_id: str) -> list[tuple[str, dict]]:
    """Todos los dias cacheados de un usuario, para poder auditarlos."""
    with conn() as c:
        rows = c.execute(
            "SELECT day, payload FROM daily WHERE user_id=? ORDER BY day", (user_id,)
        ).fetchall()
    return [(r["day"], json.loads(r["payload"])) for r in rows]


def delete_daily(user_id: str, day: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM daily WHERE user_id=? AND day=?", (user_id, day))


def save_activity(user_id: str, activity: dict) -> None:
    aid = str(activity.get("activityId"))
    day = (activity.get("startTimeLocal") or "")[:10]
    with conn() as c:
        c.execute(
            "INSERT INTO activities(user_id, activity_id, day, payload, fetched_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id, activity_id) DO UPDATE SET payload=excluded.payload, "
            "fetched_at=excluded.fetched_at",
            (user_id, aid, day, json.dumps(activity, ensure_ascii=False), _now()),
        )


def get_activities(user_id: str, start: dt.date, end: dt.date) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT payload FROM activities WHERE user_id=? AND day BETWEEN ? AND ? "
            "ORDER BY day DESC",
            (user_id, start.isoformat(), end.isoformat()),
        ).fetchall()
    return [json.loads(r["payload"]) for r in rows]

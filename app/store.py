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
    user_id       TEXT PRIMARY KEY,
    email         TEXT,
    created_at    TEXT NOT NULL,
    password_hash TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL;

-- Sesiones del navegador: panel del usuario y administracion. Se guarda el hash
-- de la cookie, no su valor, y viven en la base para poder revocarlas de verdad.
-- Ser administrador no es una sesion aparte, sino una comprobacion sobre la
-- misma: asi el panel y /admin comparten un unico inicio de sesion.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- Invitaciones: el servicio es cerrado, solo entra quien reciba una.
-- Se guarda el hash, no el codigo, para que una copia de la base no valga
-- para darse de alta.
CREATE TABLE IF NOT EXISTS invites (
    code_hash  TEXT PRIMARY KEY,
    email      TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT
);

-- OAuth. Los secretos (codigos y tokens) se guardan hasheados por el mismo
-- motivo: son credenciales, no datos.
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id  TEXT PRIMARY KEY,
    info       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_pending (
    pending_id TEXT PRIMARY KEY,
    client_id  TEXT NOT NULL,
    params     TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash  TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    payload    TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    expires_at REAL
);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON oauth_tokens(user_id);
-- Los tokens OAuth de Garmin van cifrados: ver app/crypto.py. Se guardan como
-- el JSON que la propia libreria sabe cargar en linea, sin tocar disco.
CREATE TABLE IF NOT EXISTS garmin_sessions (
    user_id    TEXT PRIMARY KEY,
    tokens_enc TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
-- Bascula inteligente vinculada, una por usuario. Se guarda el token de la
-- nube del fabricante cifrado igual que el de Garmin, nunca su contraseña.
CREATE TABLE IF NOT EXISTS scale_links (
    user_id     TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    account     TEXT,
    session_enc TEXT NOT NULL,
    linked_at   TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
-- Cuenta de Strava vinculada, una por usuario. Los tokens OAuth van cifrados
-- igual que los de Garmin y la bascula; nunca la contraseña, que ni pasa por
-- aqui: la vinculacion es un redirect a strava.com, no un formulario propio.
CREATE TABLE IF NOT EXISTS strava_links (
    user_id     TEXT PRIMARY KEY,
    athlete_id  TEXT NOT NULL,
    athlete     TEXT,
    tokens_enc  TEXT NOT NULL,
    linked_at   TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
-- Que actividad de Garmin ya se llevo a Strava, y con que par de ids (la
-- corregida que subimos nosotros, la original mal detectada que se oculto).
-- Sin esto, pedir la sincronizacion dos veces subiria la sesion duplicada.
CREATE TABLE IF NOT EXISTS strava_pushes (
    user_id             TEXT NOT NULL,
    garmin_activity_id  TEXT NOT NULL,
    strava_activity_id  TEXT NOT NULL,
    hidden_activity_id  TEXT,
    pushed_at           TEXT NOT NULL,
    PRIMARY KEY (user_id, garmin_activity_id)
);
-- Preferencias de entrenamiento: material, reparto, lesiones. Lo que Garmin no
-- sabe y el modelo necesita para no proponer a ciegas.
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id    TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
-- Lo que el modelo concluye. La inteligencia no vive aqui, pero su lectura se
-- pierde con la conversacion: sin guardarla, el usuario no puede volver a leer
-- por que le tocaba sesion suave el martes. Es texto que escribe el asistente,
-- no un dato de Garmin, asi que va en su propia tabla y nunca se sincroniza.
CREATE TABLE IF NOT EXISTS insights (
    insight_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    kind       TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    payload    TEXT,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_insights_user ON insights(user_id, created_at DESC);
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
-- Marcas de "este dia ya lo mire". Hacen falta porque la ausencia de filas en
-- `activities` es ambigua: un dia de descanso y un dia sin sincronizar se ven
-- exactamente igual, y sin distinguirlos o se repregunta siempre o no se
-- repregunta nunca.
CREATE TABLE IF NOT EXISTS sync_marks (
    user_id    TEXT NOT NULL,
    scope      TEXT NOT NULL,
    day        TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (user_id, scope, day)
);
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


def _asegurar_columna(c: sqlite3.Connection, tabla: str, columna: str, tipo: str) -> None:
    """Añade una columna si falta.

    CREATE TABLE IF NOT EXISTS no toca las tablas que ya existen, asi que las
    bases anteriores se quedarian sin las columnas nuevas.
    """
    existentes = {r["name"] for r in c.execute(f"PRAGMA table_info({tabla})")}
    if columna not in existentes:
        log.warning("Anadiendo columna %s.%s", tabla, columna)
        c.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {tipo}")


def _jubilar_admin_sessions(c: sqlite3.Connection) -> None:
    """Retira la tabla vieja de sesiones de administracion.

    Ahora hay una sola tabla de sesiones para todo el navegador. No se migran
    las filas a proposito: duran doce horas y volver a entrar cuesta nada, asi
    que arrastrar el dato no compensa mantener dos tablas equivalentes.
    """
    if c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_sessions'"
    ).fetchone():
        log.warning("Retirando admin_sessions: las sesiones ahora van en 'sessions'")
        c.execute("DROP TABLE admin_sessions")


def init() -> None:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        if _necesita_migracion(c):
            _migrar_a_multiusuario(c)
        c.executescript(SCHEMA)
        _jubilar_admin_sessions(c)
        _asegurar_columna(c, "users", "password_hash", "TEXT")
        _asegurar_columna(c, "users", "is_admin", "INTEGER NOT NULL DEFAULT 0")
        # Una invitacion con user_id no crea cuenta: reclama una que ya existe,
        # para que el dueño pueda ponerse email y contraseña sin teclearlos en
        # una terminal ni pasarlos por ningun sitio.
        _asegurar_columna(c, "invites", "user_id", "TEXT")


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
            "SELECT u.user_id, u.email, u.created_at, u.is_admin, "
            "       (s.user_id IS NOT NULL) AS garmin_vinculado, "
            "       b.provider AS bascula "
            "FROM users u LEFT JOIN garmin_sessions s ON s.user_id = u.user_id "
            "LEFT JOIN scale_links b ON b.user_id = u.user_id "
            "ORDER BY u.created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def es_admin(user_id: str) -> bool:
    with conn() as c:
        fila = c.execute("SELECT is_admin FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(fila and fila["is_admin"])


def marcar_admin(user_id: str, admin: bool = True) -> None:
    with conn() as c:
        c.execute("UPDATE users SET is_admin=? WHERE user_id=?", (int(admin), user_id))


def borrar_usuario(user_id: str) -> None:
    """Elimina al usuario y todo lo suyo. Sin esto no hay derecho de supresion."""
    with conn() as c:
        for tabla in ("daily", "activities", "garmin_sessions", "scale_links",
                      "strava_links", "strava_pushes",
                      "user_profiles", "insights", "sessions", "oauth_tokens", "users"):
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


# ------------------------------------------------------------ bascula vinculada
def guardar_bascula(user_id: str, provider: str, account: str, session_enc: str) -> None:
    """Una bascula por usuario: volver a vincular sustituye a la anterior."""
    with conn() as c:
        c.execute(
            "INSERT INTO scale_links(user_id, provider, account, session_enc, linked_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET provider=excluded.provider, "
            "account=excluded.account, session_enc=excluded.session_enc, "
            "linked_at=excluded.linked_at",
            (user_id, provider, account, session_enc, _now()),
        )


def leer_bascula(user_id: str) -> dict | None:
    with conn() as c:
        fila = c.execute("SELECT * FROM scale_links WHERE user_id=?", (user_id,)).fetchone()
    return dict(fila) if fila else None


def borrar_bascula(user_id: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM scale_links WHERE user_id=?", (user_id,))


# --------------------------------------------------------------- Strava
def guardar_strava(user_id: str, athlete_id: str, athlete: str | None, tokens_enc: str) -> None:
    """Una cuenta de Strava por usuario: volver a vincular sustituye a la anterior."""
    with conn() as c:
        c.execute(
            "INSERT INTO strava_links(user_id, athlete_id, athlete, tokens_enc, linked_at) "
            "VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET athlete_id=excluded.athlete_id, "
            "athlete=excluded.athlete, tokens_enc=excluded.tokens_enc, "
            "linked_at=excluded.linked_at",
            (user_id, athlete_id, athlete, tokens_enc, _now()),
        )


def leer_strava(user_id: str) -> dict | None:
    with conn() as c:
        fila = c.execute("SELECT * FROM strava_links WHERE user_id=?", (user_id,)).fetchone()
    return dict(fila) if fila else None


def actualizar_tokens_strava(user_id: str, tokens_enc: str) -> None:
    """Solo el par de tokens, tras refrescarlos. No toca `linked_at`: eso sigue
    siendo cuando el usuario autorizo por primera vez, no la ultima renovacion."""
    with conn() as c:
        c.execute("UPDATE strava_links SET tokens_enc=? WHERE user_id=?", (tokens_enc, user_id))


def borrar_strava(user_id: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM strava_links WHERE user_id=?", (user_id,))


def registrar_push_strava(
    user_id: str, garmin_activity_id: str, strava_activity_id: str, hidden_activity_id: str | None
) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO strava_pushes(user_id, garmin_activity_id, strava_activity_id, "
            "hidden_activity_id, pushed_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id, garmin_activity_id) DO UPDATE SET "
            "strava_activity_id=excluded.strava_activity_id, "
            "hidden_activity_id=excluded.hidden_activity_id, pushed_at=excluded.pushed_at",
            (user_id, str(garmin_activity_id), strava_activity_id, hidden_activity_id, _now()),
        )


def leer_push_strava(user_id: str, garmin_activity_id: str) -> dict | None:
    with conn() as c:
        fila = c.execute(
            "SELECT * FROM strava_pushes WHERE user_id=? AND garmin_activity_id=?",
            (user_id, str(garmin_activity_id)),
        ).fetchone()
    return dict(fila) if fila else None


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


def get_daily_fetched_at(user_id: str, start: dt.date, end: dt.date) -> dict[str, dt.datetime]:
    """Cuando se bajo cada dia cacheado del rango, indexado por fecha.

    Es lo que permite distinguir un dia cerrado de uno que se cogio a media
    tarde y quedo a medias. La columna existia desde el principio pero no la
    leia nadie.
    """
    with conn() as c:
        rows = c.execute(
            "SELECT day, fetched_at FROM daily WHERE user_id=? AND day BETWEEN ? AND ?",
            (user_id, start.isoformat(), end.isoformat()),
        ).fetchall()
    return {r["day"]: dt.datetime.fromisoformat(r["fetched_at"]) for r in rows}


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


def delete_activities(user_id: str, start: dt.date, end: dt.date) -> None:
    """Vacia un rango antes de reescribirlo con lo que diga Garmin.

    Sin esto una sesion borrada en la app seguiria en la cache para siempre:
    save_activity solo inserta y actualiza, nunca quita.
    """
    with conn() as c:
        c.execute(
            "DELETE FROM activities WHERE user_id=? AND day BETWEEN ? AND ?",
            (user_id, start.isoformat(), end.isoformat()),
        )


# ------------------------------------------------------------ marcas de sync
def mark_synced(user_id: str, scope: str, day: dt.date) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO sync_marks(user_id, scope, day, fetched_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id, scope, day) DO UPDATE SET fetched_at=excluded.fetched_at",
            (user_id, scope, day.isoformat(), _now()),
        )


def get_sync_marks(user_id: str, scope: str, start: dt.date, end: dt.date) -> dict[str, dt.datetime]:
    with conn() as c:
        rows = c.execute(
            "SELECT day, fetched_at FROM sync_marks "
            "WHERE user_id=? AND scope=? AND day BETWEEN ? AND ?",
            (user_id, scope, start.isoformat(), end.isoformat()),
        ).fetchall()
    return {r["day"]: dt.datetime.fromisoformat(r["fetched_at"]) for r in rows}

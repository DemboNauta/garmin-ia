"""Cache local en SQLite.

Motivo: la API no oficial no tiene limites publicados, pero conviene no
martillearla. Ademas asi puedo consultar historico aunque Garmin este caido.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    day        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activities (
    activity_id TEXT PRIMARY KEY,
    day         TEXT NOT NULL,
    payload     TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activities_day ON activities(day);
"""


def init() -> None:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    with conn() as c:
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


def save_daily(day: dt.date, payload: dict) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO daily(day, payload, fetched_at) VALUES(?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
            (day.isoformat(), json.dumps(payload, ensure_ascii=False), _now()),
        )


def get_daily(day: dt.date) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT payload FROM daily WHERE day=?", (day.isoformat(),)).fetchone()
    return json.loads(row["payload"]) if row else None


def get_daily_range(start: dt.date, end: dt.date) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT payload FROM daily WHERE day BETWEEN ? AND ? ORDER BY day",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def save_activity(activity: dict) -> None:
    aid = str(activity.get("activityId"))
    day = (activity.get("startTimeLocal") or "")[:10]
    with conn() as c:
        c.execute(
            "INSERT INTO activities(activity_id, day, payload, fetched_at) VALUES(?,?,?,?) "
            "ON CONFLICT(activity_id) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
            (aid, day, json.dumps(activity, ensure_ascii=False), _now()),
        )


def get_activities(start: dt.date, end: dt.date) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT payload FROM activities WHERE day BETWEEN ? AND ? ORDER BY day DESC",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    return [json.loads(r["payload"]) for r in rows]

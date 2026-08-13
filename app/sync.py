"""Sincronizacion y normalizacion.

El JSON crudo de Garmin Connect es enorme y ruidoso. `flatten_daily` lo reduce
a las ~15 metricas que sirven para decidir un entrenamiento, que es lo que
acabara viendo el modelo por MCP. El crudo se conserva en SQLite por si acaso.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from zoneinfo import ZoneInfo

from .config import settings
from .garmin_client import para_usuario
from . import store

log = logging.getLogger(__name__)


def today() -> dt.date:
    return dt.datetime.now(ZoneInfo(settings.timezone)).date()


def _dig(obj: Any, *path: str, default: Any = None) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(key)
    return obj if obj is not None else default


def flatten_daily(raw: dict) -> dict:
    """Extrae las metricas accionables de la foto diaria."""
    s = raw.get("summary") or {}
    sleep_dto = _dig(raw, "sleep", "dailySleepDTO", default={}) or {}

    def hours(seconds: Any) -> float | None:
        return round(seconds / 3600, 2) if isinstance(seconds, (int, float)) else None

    vo2 = _dig(raw, "max_metrics", default=None)
    if isinstance(vo2, list) and vo2:
        vo2 = _dig(vo2[0], "generic", "vo2MaxPreciseValue") or _dig(vo2[0], "generic", "vo2MaxValue")
    else:
        vo2 = None

    return {
        "date": raw.get("date"),
        # Carga y actividad
        "steps": s.get("totalSteps"),
        "active_kcal": s.get("activeKilocalories"),
        "intensity_minutes": (s.get("moderateIntensityMinutes") or 0)
        + 2 * (s.get("vigorousIntensityMinutes") or 0),
        "floors_climbed": s.get("floorsAscended"),
        # Cardio
        "resting_hr": s.get("restingHeartRate") or _dig(raw, "resting_hr", "restingHeartRate"),
        "max_hr": s.get("maxHeartRate"),
        "avg_hr": s.get("averageHeartRate"),
        "hrv_last_night": _dig(raw, "hrv", "hrvSummary", "lastNightAvg"),
        "hrv_status": _dig(raw, "hrv", "hrvSummary", "status"),
        "vo2max": vo2,
        # Recuperacion
        "sleep_hours": hours(sleep_dto.get("sleepTimeSeconds")),
        "sleep_score": _dig(sleep_dto, "sleepScores", "overall", "value"),
        "deep_sleep_hours": hours(sleep_dto.get("deepSleepSeconds")),
        "rem_sleep_hours": hours(sleep_dto.get("remSleepSeconds")),
        "awake_hours": hours(sleep_dto.get("awakeSleepSeconds")),
        "body_battery_high": s.get("bodyBatteryHighestValue"),
        "body_battery_low": s.get("bodyBatteryLowestValue"),
        "avg_stress": s.get("averageStressLevel"),
        "training_readiness": _first(_dig(raw, "training_readiness"), "score"),
        "training_status": _dig(raw, "training_status", "latestTrainingStatusData"),
    }


def _first(value: Any, key: str) -> Any:
    if isinstance(value, list) and value:
        value = value[0]
    return value.get(key) if isinstance(value, dict) else None


class SincronizacionVacia(RuntimeError):
    """Garmin no devolvio ni un bloque util para ese dia."""


def tiene_datos(raw: dict) -> bool:
    """Distingue una foto real de una fallida.

    daily_snapshot degrada bloque a bloque, asi que un fallo devuelve la misma
    forma que un dia legitimo: todo a None. Si eso se guarda, la fila envenena
    la cache para siempre, porque get_metrics solo resincroniza cuando NO hay
    fila, no cuando la que hay esta vacia.
    """
    return any(v is not None for k, v in raw.items() if k != "date")


def sync_day(user_id: str, day: dt.date) -> dict:
    raw = para_usuario(user_id).daily_snapshot(day)
    if not tiene_datos(raw):
        raise SincronizacionVacia(
            f"Garmin no devolvio ningun dato para {day.isoformat()}; no se cachea."
        )
    store.save_daily(user_id, day, raw)
    return flatten_daily(raw)


def limpiar_cache_vacia(user_id: str) -> int:
    """Borra los dias cacheados sin un solo dato y devuelve cuantos eran.

    Repara bases que se llenaron antes de que existieran los tokens, que es lo
    que pasa si el servicio arranca antes del primer login.
    """
    vacios = [d for d, raw in store.all_daily(user_id) if not tiene_datos(raw)]
    for day in vacios:
        store.delete_daily(user_id, day)
    if vacios:
        log.warning("Purgados %d dias vacios de la cache: %s", len(vacios), ", ".join(vacios))
    return len(vacios)


def sync_range(user_id: str, days: int = 7) -> dict:
    end = today()
    start = end - dt.timedelta(days=days - 1)
    ok, failed = 0, []
    for i in range(days):
        d = start + dt.timedelta(days=i)
        try:
            sync_day(user_id, d)
            ok += 1
        except Exception as exc:
            failed.append({"date": d.isoformat(), "error": str(exc)})
            log.warning("Fallo sincronizando %s: %s", d, exc)
    try:
        for act in para_usuario(user_id).activities(start, end):
            store.save_activity(user_id, act)
    except Exception as exc:
        failed.append({"activities": str(exc)})
    return {"synced_days": ok, "from": start.isoformat(), "to": end.isoformat(), "errors": failed}

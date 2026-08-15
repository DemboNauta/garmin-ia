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
    return ahora().date()


def ahora() -> dt.datetime:
    return dt.datetime.now(ZoneInfo(settings.timezone))


# Un dia deja de cambiar bastante despues de su medianoche: el sueño de esa
# noche se consolida por la mañana, y readiness y estado de entrenamiento
# llegan con el, asi que hasta mediodia del dia siguiente no es definitivo.
CIERRE_DEL_DIA = dt.timedelta(hours=12)
# Mientras el dia esta en curso todo se mueve (pasos, calorias, Body Battery),
# pero tampoco hace falta bajarlo en cada pregunta.
FRESCURA_EN_CURSO = dt.timedelta(minutes=15)


def _cierre(day: dt.date) -> dt.datetime:
    """Momento a partir del cual lo que diga Garmin de ese dia ya es definitivo."""
    medianoche = dt.datetime.combine(
        day + dt.timedelta(days=1), dt.time.min, ZoneInfo(settings.timezone)
    )
    return medianoche + CIERRE_DEL_DIA


def esta_al_dia(day: dt.date, bajado: dt.datetime, momento: dt.datetime) -> bool:
    """¿Sirve lo que hay cacheado de `day`, bajado en `bajado`?

    Tres casos, y el del medio es el que fallaba: una foto tomada mientras el
    dia transcurria se quedaba cacheada para siempre, con el basal a medias y
    sin el sueño de esa noche.
    """
    cierre = _cierre(day)
    if bajado >= cierre:
        return True  # se bajo cuando el dia ya estaba cerrado: no va a cambiar
    if momento < cierre:
        return momento - bajado < FRESCURA_EN_CURSO  # dia en curso: TTL corto
    return False  # se cogio a medias y el dia ya cerro: hay que rehacerlo


def dias_pendientes(user_id: str, start: dt.date, end: dt.date) -> list[dt.date]:
    """Dias del rango que faltan o que estan cacheados a medias."""
    bajados = store.get_daily_fetched_at(user_id, start, end)
    momento = ahora()
    pendientes = []
    for i in range((end - start).days + 1):
        day = start + dt.timedelta(days=i)
        bajado = bajados.get(day.isoformat())
        if bajado is None or not esta_al_dia(day, bajado, momento):
            pendientes.append(day)
    return pendientes


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

    return {
        "date": raw.get("date"),
        # Carga y actividad
        "steps": s.get("totalSteps"),
        # Las tres calorias son distintas y hacen falta las tres: activas son
        # solo el gasto por movimiento, basales lo que quema en reposo, y el
        # total (basal + activo) es el gasto del dia, que es lo que se compara
        # con lo que come. Con solo las activas un dia de 1941 kcal parece de 117.
        "active_kcal": s.get("activeKilocalories"),
        "bmr_kcal": s.get("bmrKilocalories"),
        "total_kcal": s.get("totalKilocalories"),
        "intensity_minutes": _intensidad(s),
        "floors_climbed": s.get("floorsAscended"),
        # Cardio
        "resting_hr": s.get("restingHeartRate") or _dig(raw, "resting_hr", "restingHeartRate"),
        "max_hr": s.get("maxHeartRate"),
        "avg_hr": s.get("averageHeartRate"),
        "hrv_last_night": _dig(raw, "hrv", "hrvSummary", "lastNightAvg"),
        "hrv_status": _dig(raw, "hrv", "hrvSummary", "status"),
        "vo2max": _vo2max(raw),
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
        "training_status": _estado_entrenamiento(raw),
    }


def _vo2max(raw: dict) -> float | None:
    """VO2max del dia, o el ultimo que Garmin tenga estimado.

    `max_metrics` solo trae algo el dia en que se recalcula (una salida larga
    con FC estable), asi que en la mayoria de los dias llega como lista vacia.
    El valor vigente sigue estando en el bloque de estado de entrenamiento, que
    es de donde lo saca la propia app para enseñarlo todos los dias.
    """
    metricas = _dig(raw, "max_metrics", default=None)
    generico = metricas[0] if isinstance(metricas, list) and metricas else None
    if not isinstance(generico, dict):
        generico = _dig(raw, "training_status", "mostRecentVO2Max", default={})
    return _dig(generico, "generic", "vo2MaxPreciseValue") or _dig(
        generico, "generic", "vo2MaxValue"
    )


def _dispositivo_principal(por_dispositivo: dict) -> dict:
    """De los relojes que reportaron estado, el que manda.

    Garmin devuelve el estado indexado por id de dispositivo, porque quien tiene
    reloj y ciclocomputador tiene dos. Se coge el marcado como principal y, si
    ninguno lo esta, el que trae la fecha mas reciente.
    """
    entradas = [v for v in por_dispositivo.values() if isinstance(v, dict)]
    if not entradas:
        return {}
    principales = [e for e in entradas if e.get("primaryTrainingDevice")]
    return max(principales or entradas, key=lambda e: e.get("calendarDate") or "")


def _estado_entrenamiento(raw: dict) -> dict | None:
    """Estado de entrenamiento y carga aguda/cronica del dia.

    Estaba colgando de la clave equivocada (`latestTrainingStatusData` no cuelga
    de la raiz del bloque sino de `mostRecentTrainingStatus`), asi que llegaba
    siempre a null aunque Garmin lo estuviera mandando.

    La etiqueta sale de la frase de feedback ('PRODUCTIVE_2' -> 'productive') y
    no del numero: `trainingStatus` es un enum interno sin documentar, y de la
    frase se lee lo mismo sin tener que adivinar la tabla. Se devuelve tambien
    el codigo crudo por si algun dia hace falta.
    """
    por_dispositivo = _dig(
        raw, "training_status", "mostRecentTrainingStatus", "latestTrainingStatusData",
        default={},
    )
    datos = _dispositivo_principal(por_dispositivo or {})
    if not datos:
        return None
    frase = datos.get("trainingStatusFeedbackPhrase") or ""
    etiqueta = frase.rsplit("_", 1)[0].lower() if frase else None
    carga = datos.get("acuteTrainingLoadDTO") or {}
    estado = {
        "status": etiqueta,
        "feedback": frase or None,
        "code": datos.get("trainingStatus"),
        "since": datos.get("sinceDate"),
        "paused": datos.get("trainingPaused"),
        # Aguda es la carga de los ultimos 7 dias y cronica la de las 4 semanas:
        # su cociente (acwr) es lo que dice si se esta subiendo demasiado rapido.
        "load_acute": carga.get("dailyTrainingLoadAcute"),
        "load_chronic": carga.get("dailyTrainingLoadChronic"),
        "acwr_pct": carga.get("acwrPercent"),
        "acwr_status": carga.get("acwrStatus"),
    }
    return {k: v for k, v in estado.items() if v is not None}


def _intensidad(s: dict) -> int | None:
    """Minutos de intensidad, contando doble los vigorosos como hace Garmin.

    Devuelve None si no hay ni uno de los dos datos: un 0 significaria "hoy no
    te has movido", que no es lo mismo que "el reloj no lo ha reportado", y
    ademas hacia pasar por dia con datos una foto entera vacia.
    """
    moderados = s.get("moderateIntensityMinutes")
    vigorosos = s.get("vigorousIntensityMinutes")
    if moderados is None and vigorosos is None:
        return None
    return (moderados or 0) + 2 * (vigorosos or 0)


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
    la cache.

    Se mira lo aplanado, no los bloques crudos: para un dia sin datos Garmin no
    devuelve `summary: null` sino un `summary` con todos sus campos a null, que
    como bloque no es None y colaba como dia bueno.
    """
    return any(v is not None for k, v in flatten_daily(raw).items() if k != "date")


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


def asegurar_dias(user_id: str, start: dt.date, end: dt.date) -> list[dict]:
    """Deja el rango completo y al dia en la cache, y devuelve los fallos.

    Es el camino perezoso: solo baja los dias que faltan o que se cacheron a
    medias, uno por uno. Un dia que falle no impide los demas, porque una
    lectura con seis dias buenos vale mucho mas que un error entero.
    """
    fallos = []
    for day in dias_pendientes(user_id, start, end):
        try:
            sync_day(user_id, day)
        except SincronizacionVacia as exc:
            # Ni error ni dia bueno: Garmin todavia no tiene nada de ese dia
            # (tipico de las primeras horas). Se reintentara en la siguiente.
            log.info("%s", exc)
        except Exception as exc:
            fallos.append({"date": day.isoformat(), "error": str(exc)})
            log.warning("Fallo sincronizando %s: %s", day, exc)
    return fallos


def recachear_actividades(user_id: str, start: dt.date, end: dt.date) -> None:
    """Reemplaza en la cache las actividades del rango por las de Garmin.

    Se borra antes de guardar porque save_activity solo inserta y actualiza:
    sin el borrado, una sesion eliminada desde la app seguiria apareciendo
    para siempre. El borrado va despues de la descarga a proposito, para no
    vaciar la cache si Garmin falla.
    """
    actividades = para_usuario(user_id).activities(start, end)
    store.delete_activities(user_id, start, end)
    for act in actividades:
        store.save_activity(user_id, act)
    for i in range((end - start).days + 1):
        store.mark_synced(user_id, "activities", start + dt.timedelta(days=i))


def refrescar_actividades(user_id: str, day: dt.date) -> None:
    """Recachea las actividades de un dia, p.ej. tras corregir una.

    Sin esto get_activities seguiria sirviendo la copia vieja y una sesion
    editada se veria con el nombre y el tipo de antes.
    """
    recachear_actividades(user_id, day, day)


def asegurar_actividades(user_id: str, start: dt.date, end: dt.date) -> None:
    """Baja las actividades del rango si alguno de sus dias no esta al dia.

    Las marcas son necesarias porque la ausencia de actividades es ambigua: un
    dia de descanso no tiene filas, igual que un dia sin sincronizar. Antes se
    resolvia con `if not acts`, que hacia lo peor de ambos mundos: con una sola
    sesion cacheada en el rango, nunca volvia a preguntar.
    """
    marcas = store.get_sync_marks(user_id, "activities", start, end)
    momento = ahora()
    pendientes = []
    for i in range((end - start).days + 1):
        day = start + dt.timedelta(days=i)
        marca = marcas.get(day.isoformat())
        if marca is None or not esta_al_dia(day, marca, momento):
            pendientes.append(day)
    if not pendientes:
        return
    # Un solo viaje para todo el tramo pendiente: get_activities_by_date acepta
    # rango, asi que pedir siete dias cuesta lo mismo que pedir uno.
    recachear_actividades(user_id, min(pendientes), max(pendientes))


def sync_range(user_id: str, days: int = 7) -> dict:
    """Rebaja el rango entero de Garmin, mire lo que mire la cache.

    Es el camino a la fuerza (refresh=True y POST /sync). Para el uso normal
    esta `asegurar_dias`, que solo pide lo que falta.
    """
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
        recachear_actividades(user_id, start, end)
    except Exception as exc:
        failed.append({"activities": str(exc)})
    return {"synced_days": ok, "from": start.isoformat(), "to": end.isoformat(), "errors": failed}

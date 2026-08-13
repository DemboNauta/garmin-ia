"""Servidor MCP: la interfaz por la que el modelo lee tus datos y escribe planes.

La inteligencia NO vive aqui. Estas herramientas solo entregan datos limpios y
ejecutan ordenes; decidir que entrenamiento toca es trabajo del modelo.
"""
from __future__ import annotations

import datetime as dt

from mcp.server.fastmcp import FastMCP

from . import store, sync, workouts
from .garmin_client import client

# streamable_http_path="/" porque main.py ya monta esta app bajo /mcp:
# con el valor por defecto ("/mcp") el endpoint acabaria en /mcp/mcp.
mcp = FastMCP("garmin", stateless_http=True, streamable_http_path="/")


@mcp.tool()
def get_devices() -> list[dict]:
    """Dispositivos Garmin vinculados a la cuenta y su ultima sincronizacion."""
    return [
        {
            "name": d.get("displayName") or d.get("productDisplayName"),
            "model": d.get("productDisplayName"),
            "unit_id": d.get("unitId"),
            "last_sync": d.get("lastSyncTime"),
        }
        for d in client.devices()
    ]


@mcp.tool()
def get_metrics(days: int = 7, refresh: bool = False) -> list[dict]:
    """Metricas diarias de los ultimos N dias: sueño, HRV, FC en reposo,
    Body Battery, estres, pasos, minutos de intensidad, VO2max y readiness.
    Con refresh=True fuerza descarga desde Garmin en vez de usar cache."""
    end = sync.today()
    start = end - dt.timedelta(days=days - 1)
    if refresh:
        sync.sync_range(days)
    rows = store.get_daily_range(start, end)
    if not rows:
        sync.sync_range(days)
        rows = store.get_daily_range(start, end)
    return [sync.flatten_daily(r) for r in rows]


@mcp.tool()
def get_today() -> dict:
    """Estado de hoy: la foto que hay que mirar antes de proponer sesion."""
    return sync.sync_day(sync.today())


@mcp.tool()
def get_activities(days: int = 30) -> list[dict]:
    """Entrenamientos registrados en los ultimos N dias (resumen por sesion)."""
    end = sync.today()
    start = end - dt.timedelta(days=days - 1)
    acts = store.get_activities(start, end)
    if not acts:
        acts = client.activities(start, end)
        for a in acts:
            store.save_activity(a)
    return [
        {
            "id": a.get("activityId"),
            "name": a.get("activityName"),
            "type": (a.get("activityType") or {}).get("typeKey"),
            "start": a.get("startTimeLocal"),
            "duration_min": round((a.get("duration") or 0) / 60, 1),
            "distance_km": round((a.get("distance") or 0) / 1000, 2),
            "avg_hr": a.get("averageHR"),
            "max_hr": a.get("maxHR"),
            "kcal": a.get("calories"),
            "training_effect_aerobic": a.get("aerobicTrainingEffect"),
            "training_effect_anaerobic": a.get("anaerobicTrainingEffect"),
            "avg_pace_min_km": round(1000 / a["averageSpeed"] / 60, 2)
            if a.get("averageSpeed") else None,
        }
        for a in acts
    ]


@mcp.tool()
def find_exercises(term: str, limit: int = 25) -> list[dict]:
    """Busca en el catalogo de ejercicios de fuerza de Garmin (1527 en total).

    Devuelve el nombre exacto que espera create_workout y su `category`, que es
    el grupo muscular con el que Garmin clasifica la sesion. El catalogo esta
    SOLO en ingles: busca "bench press", no "press de banca".
    """
    return [
        {"name": e["name"], "muscle_group": e["category"], "variant": e["exercise"]}
        for e in workouts.buscar_ejercicios(term)[:limit]
    ]


@mcp.tool()
def list_muscle_groups() -> list[str]:
    """Las 47 categorias de Garmin, que son los grupos musculares y patrones
    de movimiento con los que clasifica los ejercicios de fuerza."""
    return workouts.categorias()


@mcp.tool()
def create_workout(spec: dict, schedule_date: str | None = None) -> dict:
    """Crea un entrenamiento estructurado en Garmin Connect y opcionalmente lo
    programa en una fecha (YYYY-MM-DD).

    Formato de spec:
    {"name": str, "sport": "running|cycling|cardio|strength|walking|hiit|swimming",
     "description": str,
     "steps": [{"kind": "warmup|interval|recovery|rest|cooldown|other",
                "seconds": int, "meters": int, "hr_zone": 1-5, "note": str,
                "exercise": str, "reps": int, "weight_kg": float},
               {"repeat": 4, "steps": [...]}]}

    En sesiones de fuerza usa SIEMPRE `exercise` con el nombre exacto del
    catalogo (buscalo antes con find_exercises) junto a `reps` y, si procede,
    `weight_kg`. Sin `exercise` el ejercicio queda sin identificar y Garmin
    pierde el grupo muscular: describirlo en `note` no sirve, porque las notas
    son texto libre que no se puede agregar despues.

    Ejemplo de fuerza:
    {"name": "Empuje A", "sport": "strength", "steps": [
        {"repeat": 4, "steps": [
            {"kind": "interval", "exercise": "Barbell Bench Press",
             "reps": 8, "weight_kg": 40},
            {"kind": "rest", "seconds": 120}]}]}
    """
    payload = workouts.from_spec(spec)
    created = client.create_workout(payload)
    result = {"workout_id": created.get("workoutId"), "name": created.get("workoutName")}
    if schedule_date:
        day = dt.date.fromisoformat(schedule_date)
        client.schedule_workout(created["workoutId"], day)
        result["scheduled_for"] = schedule_date
    return result


@mcp.tool()
def list_workouts(limit: int = 20) -> list[dict]:
    """Entrenamientos ya guardados en Garmin Connect."""
    return [
        {"id": w.get("workoutId"), "name": w.get("workoutName"),
         "sport": (w.get("sportType") or {}).get("sportTypeKey"),
         "updated": w.get("updateDate")}
        for w in client.list_workouts(limit)
    ]

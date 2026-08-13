"""Servidor MCP: la interfaz por la que el modelo lee tus datos y escribe planes.

La inteligencia NO vive aqui. Estas herramientas solo entregan datos limpios y
ejecutan ordenes; decidir que entrenamiento toca es trabajo del modelo.
"""
from __future__ import annotations

import datetime as dt

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import profile, store, sync, workouts
from .config import settings
from .garmin_client import para_usuario
from .identity import usuario_actual

INSTRUCCIONES = """\
Datos de Garmin Connect de quien te esta hablando, y escritura de entrenamientos
en su cuenta. Cada credencial ve solo lo suyo: no hay forma de consultar los
datos de otra persona, ni falta.

Antes de proponer una sesion, mira DOS cosas: get_profile, que dice con que
material cuenta, cuantos dias entrena y que lesiones respetar, y get_today, cuyo
readiness, sueño y HRV mandan sobre cualquier plan previo.

Cuando cuente algo estable de su forma de entrenar, guardalo con
update_profile en vez de fiarlo a la memoria de la conversacion, que se pierde.

Para crear fuerza, el orden es: find_exercises para dar con el nombre exacto
(el catalogo esta SOLO en ingles) y luego create_workout con ese nombre en
`exercise`. Un ejercicio sin identificar pierde el grupo muscular y la sesion
deja de ser analizable despues; describirlo en `note` no lo arregla.

Para editar, get_workout devuelve los pasos con la misma forma que espera
create_workout: cambia lo que haga falta y pasalos a update_workout. Eso
reemplaza el entrenamiento entero, asi que mandalo completo. El id se conserva,
de modo que editar no lo saca del calendario; mover la fecha es unschedule_workout
mas schedule_workout, con el schedule_id de list_scheduled, que no es el id del
entrenamiento.

Los datos vienen de la nube de Garmin, no del dispositivo, y se cachean: usa
refresh solo si necesitas el dato del momento.\
"""

def _seguridad_transporte() -> TransportSecuritySettings | None:
    """Permite los dominios publicos sin desactivar la proteccion.

    FastMCP, al ver que se escucha en 127.0.0.1, solo admite Host localhost y
    devuelve 421 a todo lo demas. Detras de un proxy el Host que llega es el
    dominio real, asi que hay que anadirlo explicitamente en vez de apagar la
    comprobacion, que sigue defendiendo de ataques de DNS rebinding.
    """
    dominios = [h.strip() for h in settings.allowed_hosts.split(",") if h.strip()]
    if not dominios:
        return None  # sin dominios declarados, el comportamiento local de siempre
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=dominios + ["127.0.0.1:*", "localhost:*", "[::1]:*"],
        allowed_origins=[f"https://{d}" for d in dominios]
        + [f"http://{d}" for d in dominios]
        + ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    )


# streamable_http_path="/" porque main.py ya monta esta app bajo /mcp:
# con el valor por defecto ("/mcp") el endpoint acabaria en /mcp/mcp.
mcp = FastMCP(
    "garmin",
    stateless_http=True,
    streamable_http_path="/",
    instructions=INSTRUCCIONES,
    transport_security=_seguridad_transporte(),
)


def _cliente():
    """Cliente de Garmin del usuario de esta peticion."""
    return para_usuario(usuario_actual())


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
        for d in _cliente().devices()
    ]


@mcp.tool()
def get_metrics(days: int = 7, refresh: bool = False) -> list[dict]:
    """Metricas diarias de los ultimos N dias: sueño, HRV, FC en reposo,
    Body Battery, estres, pasos, minutos de intensidad, VO2max y readiness.
    Con refresh=True fuerza descarga desde Garmin en vez de usar cache."""
    end = sync.today()
    start = end - dt.timedelta(days=days - 1)
    if refresh:
        sync.sync_range(usuario_actual(), days)
    rows = store.get_daily_range(usuario_actual(), start, end)
    if not rows:
        sync.sync_range(usuario_actual(), days)
        rows = store.get_daily_range(usuario_actual(), start, end)
    return [sync.flatten_daily(r) for r in rows]


@mcp.tool()
def get_today() -> dict:
    """Estado de hoy: la foto que hay que mirar antes de proponer sesion."""
    return sync.sync_day(usuario_actual(), sync.today())


@mcp.tool()
def get_activities(days: int = 30) -> list[dict]:
    """Entrenamientos registrados en los ultimos N dias (resumen por sesion)."""
    end = sync.today()
    start = end - dt.timedelta(days=days - 1)
    acts = store.get_activities(usuario_actual(), start, end)
    if not acts:
        acts = _cliente().activities(start, end)
        for a in acts:
            store.save_activity(usuario_actual(), a)
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
def get_profile() -> dict:
    """Preferencias de entrenamiento de esta persona: material disponible, como
    reparte la fuerza, cuantos dias entrena, actividades habituales, objetivos y
    lesiones.

    Miralo ANTES de proponer nada. Garmin dice como esta, esto dice con que
    cuenta: prescribir sentadilla con barra a quien solo tiene gomas no sirve.
    Devuelve {} si aun no ha contado nada; entonces pregunta y guardalo.
    """
    return profile.leer(usuario_actual())


@mcp.tool()
def update_profile(perfil: profile.Perfil) -> dict:
    """Guarda o corrige preferencias de entrenamiento. Devuelve el perfil entero
    ya actualizado.

    Solo toca los campos que mandes: lo demas se queda como estaba. Es lo
    contrario que update_workout, y a proposito, porque un perfil se completa a
    lo largo de muchas conversaciones y no conviene que mencionar el material
    borre las lesiones.

    Manda un campo explicitamente a null para borrarlo.

    Usalo cuando la persona cuente algo estable ("me he comprado una bici",
    "ahora entreno 4 dias", "me duele el hombro derecho"), no para cosas de un
    dia suelto.
    """
    return profile.actualizar(usuario_actual(), perfil)


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
def create_workout(
    spec: workouts.EntrenoSpec, schedule_date: str | None = None
) -> dict:
    """Crea un entrenamiento estructurado en Garmin Connect y opcionalmente lo
    programa en una fecha (YYYY-MM-DD).

    En fuerza, cada ejercicio necesita `exercise` con el nombre exacto del
    catalogo: buscalo antes con find_exercises. Los descansos entre series van
    como pasos aparte con kind='rest' y `seconds`.

    Ejemplo:
    {"name": "Empuje A", "sport": "strength", "steps": [
        {"repeat": 4, "steps": [
            {"kind": "interval", "exercise": "Barbell Bench Press",
             "reps": 8, "weight_kg": 40},
            {"kind": "rest", "seconds": 120}]}]}
    """
    datos = spec.model_dump(exclude_none=True)
    payload = workouts.from_spec(datos)
    created = _cliente().create_workout(payload)
    result = {"workout_id": created.get("workoutId"), "name": created.get("workoutName")}
    if schedule_date:
        day = dt.date.fromisoformat(schedule_date)
        _cliente().schedule_workout(created["workoutId"], day)
        result["scheduled_for"] = schedule_date
    avisos = workouts.pasos_sin_ejercicio(datos)
    if avisos:
        result["warnings"] = (
            [f"{len(avisos)} pasos de fuerza sin `exercise`: no tendran grupo muscular."]
            + avisos[:5]
        )
    return result


@mcp.tool()
def list_workouts(limit: int = 20) -> list[dict]:
    """Entrenamientos ya guardados en Garmin Connect."""
    return [
        {"id": w.get("workoutId"), "name": w.get("workoutName"),
         "sport": (w.get("sportType") or {}).get("sportTypeKey"),
         "updated": w.get("updateDate")}
        for w in _cliente().list_workouts(limit)
    ]


# De la condicion de fin de Garmin a la clave que usa el spec.
_CLAVE_VALOR = {"reps": "reps", "time": "seconds", "distance": "meters"}


def _leer_pasos(pasos: list[dict]) -> list[dict]:
    """DTO de Garmin -> pasos con la MISMA forma que acepta create_workout.

    Simetrico a proposito: lo que devuelve get_workout se puede meter tal cual
    en update_workout sin traducir nada, que es como se edita un entrenamiento.
    """
    out: list[dict] = []
    for p in pasos or []:
        if p.get("workoutSteps"):
            out.append({
                "repeat": p.get("numberOfIterations"),
                "steps": _leer_pasos(p["workoutSteps"]),
            })
            continue
        peso = p.get("weightValue")
        paso = {
            "kind": (p.get("stepType") or {}).get("stepTypeKey"),
            # Nombre del catalogo, no el enum, porque es lo que espera el spec.
            "exercise": workouts.nombre_visible(p.get("category"), p.get("exerciseName")),
            "weight_kg": peso / 1000 if peso else None,  # Garmin lo guarda en gramos
            "hr_zone": p.get("zoneNumber"),
            "note": p.get("description"),
        }
        clave = _CLAVE_VALOR.get((p.get("endCondition") or {}).get("conditionTypeKey"))
        if clave and p.get("endConditionValue") is not None:
            valor = p["endConditionValue"]
            paso[clave] = int(valor) if clave in ("reps", "seconds") else valor
        out.append({k: v for k, v in paso.items() if v is not None})
    return out


def _grupos_musculares(pasos: list[dict]) -> list[str]:
    """Categorias distintas que toca el entrenamiento, en orden de aparicion."""
    vistos: list[str] = []
    for p in pasos or []:
        if p.get("workoutSteps"):
            for g in _grupos_musculares(p["workoutSteps"]):
                if g not in vistos:
                    vistos.append(g)
        elif p.get("category") and p["category"] not in vistos:
            vistos.append(p["category"])
    return vistos


@mcp.tool()
def get_workout(workout_id: int, raw: bool = False) -> dict:
    """Contenido completo de un entrenamiento: sus pasos, ejercicios y grupos
    musculares. Mira esto antes de editar, para saber que hay que cambiar.

    Los `steps` vienen con la misma forma que acepta create_workout, asi que
    para editar basta con modificar lo que haga falta y pasarlos a
    update_workout. `muscle_groups` resume que trabaja la sesion.

    Con raw=True devuelve el DTO crudo de Garmin, mucho mas verboso.
    """
    w = _cliente().workout_detail(workout_id)
    if raw:
        return w
    brutos = [s for seg in w.get("workoutSegments") or [] for s in seg.get("workoutSteps") or []]
    return {
        "id": w.get("workoutId"),
        "name": w.get("workoutName"),
        "sport": (w.get("sportType") or {}).get("sportTypeKey"),
        "description": w.get("description"),
        "muscle_groups": _grupos_musculares(brutos),
        "steps": _leer_pasos(brutos),
    }


@mcp.tool()
def update_workout(workout_id: int, spec: workouts.EntrenoSpec) -> dict:
    """Reemplaza un entrenamiento existente. Lee antes con get_workout: sus
    `steps` ya vienen con este mismo formato, asi que basta con cambiar lo que
    haga falta y devolverlos aqui.

    El spec debe ir COMPLETO: Garmin no admite parches, el PUT sustituye toda la
    estructura, asi que lo que no mandes se pierde.

    El entrenamiento conserva su id, de modo que si estaba programado en el
    calendario sigue estandolo: editar el contenido NO obliga a reprogramar.
    Para mover la fecha se usan unschedule_workout y schedule_workout.
    """
    datos = spec.model_dump(exclude_none=True)
    payload = workouts.from_spec(datos)
    updated = _cliente().update_workout(workout_id, payload)
    result = {"workout_id": workout_id, "updated": True, "name": (updated or {}).get("workoutName")}
    avisos = workouts.pasos_sin_ejercicio(datos)
    if avisos:
        result["warnings"] = (
            [f"{len(avisos)} pasos de fuerza sin `exercise`: no tendran grupo muscular."]
            + avisos[:5]
        )
    return result


@mcp.tool()
def delete_workout(workout_id: int) -> dict:
    """Borra un entrenamiento y, con el, cualquier programacion que lo apunte."""
    _cliente().delete_workout(workout_id)
    return {"workout_id": workout_id, "deleted": True}


@mcp.tool()
def list_scheduled(year: int, month: int) -> list[dict]:
    """Entrenamientos programados en un mes (month va de 1 a 12).

    Devuelve `schedule_id`, que es lo unico que acepta unschedule_workout y que
    NO coincide con el id del entrenamiento.
    """
    datos = _cliente().scheduled_workouts(year, month) or {}
    items = datos.get("calendarItems") if isinstance(datos, dict) else datos
    salida = []
    for it in items or []:
        if it.get("itemType") not in (None, "workout"):
            continue
        salida.append({
            "schedule_id": it.get("id"),
            "workout_id": it.get("workoutId"),
            "name": it.get("title"),
            "date": it.get("date"),
        })
    return salida


@mcp.tool()
def schedule_workout(workout_id: int, schedule_date: str) -> dict:
    """Programa un entrenamiento ya existente en una fecha (YYYY-MM-DD)."""
    _cliente().schedule_workout(workout_id, dt.date.fromisoformat(schedule_date))
    return {"workout_id": workout_id, "scheduled_for": schedule_date}


@mcp.tool()
def unschedule_workout(schedule_id: int) -> dict:
    """Saca un entrenamiento del calendario sin borrarlo.

    Necesita el `schedule_id` que devuelve list_scheduled, no el id del
    entrenamiento. Para mover una sesion de dia: unschedule + schedule_workout.
    """
    _cliente().unschedule_workout(schedule_id)
    return {"schedule_id": schedule_id, "unscheduled": True}

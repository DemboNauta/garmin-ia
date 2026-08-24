"""Servidor MCP: la interfaz por la que el modelo lee tus datos y escribe planes.

La inteligencia NO vive aqui. Estas herramientas solo entregan datos limpios y
ejecutan ordenes; decidir que entrenamiento toca es trabajo del modelo.
"""
from __future__ import annotations

import datetime as dt
import logging

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import activities, insights, profile, scales, store, strava, sync, weight, workouts
from .config import settings
from .garmin_client import para_usuario
from .identity import usuario_actual

log = logging.getLogger(__name__)

INSTRUCCIONES = """\
Datos de Garmin Connect de quien te esta hablando, y escritura de entrenamientos
en su cuenta. Cada credencial ve solo lo suyo: no hay forma de consultar los
datos de otra persona, ni falta.

Antes de proponer una sesion, mira DOS cosas: get_profile, que dice con que
material cuenta, cuantos dias entrena y que lesiones respetar, y get_today, cuyo
readiness, sueño y HRV mandan sobre cualquier plan previo.

Cuando cuente algo estable de su forma de entrenar, guardalo con
update_profile en vez de fiarlo a la memoria de la conversacion, que se pierde.
El peso corporal no va ahi: se lee con get_weight y se apunta con log_weight,
que lo dejan en Garmin con su fecha y hacen serie.

Si tiene bascula inteligente vinculada, get_body_composition manda sobre
get_weight: mide a diario y separa grasa de musculo, que es lo unico que
distingue adelgazar de perder lo que se estaba construyendo. Fijate en la media
de siete dias, no en el ultimo pesaje, que oscila mas de un kilo en el dia.

Para crear fuerza, el orden es: find_exercises para dar con el nombre exacto
(el catalogo esta SOLO en ingles) y luego create_workout con ese nombre en
`exercise`. Un ejercicio sin identificar pierde el grupo muscular y la sesion
deja de ser analizable despues; describirlo en `note` no lo arregla.

Lo que el reloj grabo tambien se corrige. Una pulsera sin pantalla no sabe que
ejercicio era ni con cuanto peso, asi que las series de fuerza suelen quedar
como UNKNOWN y sin carga: cuando lo cuente, mira la sesion con get_activity y
arregla sus series con update_activity_sets, el nombre o el deporte con
update_activity, y da de alta con add_activity lo que no llegara a grabarse.
Sin eso, la sesion no cuenta en ningun grupo muscular y no hay progresion de
peso que seguir. Si tiene Strava vinculado, esa correccion no llega alli sola
—Garmin solo exporta una vez, al grabar—: despues de update_activity_sets,
sync_activity_to_strava oculta en Strava la version mal detectada y sube la
corregida.

Para editar, get_workout devuelve los pasos con la misma forma que espera
create_workout: cambia lo que haga falta y pasalos a update_workout. Eso
reemplaza el entrenamiento entero, asi que mandalo completo. El id se conserva,
de modo que editar no lo saca del calendario; mover la fecha es unschedule_workout
mas schedule_workout, con el schedule_id de list_scheduled, que no es el id del
entrenamiento.

Cuando llegues a una conclusion que merezca releerse (por que hoy toca suave,
el plan de la semana, que la grasa baja y el musculo aguanta), guardala con
save_insight: aparece en el panel web de la persona y con list_insights
recuperas el hilo en la siguiente conversacion, que si no empieza en blanco.

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
    Body Battery, estres, pasos, minutos de intensidad, VO2max, readiness y
    calorias (`total_kcal` es el gasto del dia entero, basal mas activo; el
    `kcal` de get_activities es solo el de esa sesion).
    Con refresh=True fuerza descarga desde Garmin en vez de usar cache."""
    end = sync.today()
    start = end - dt.timedelta(days=days - 1)
    if refresh:
        sync.sync_range(usuario_actual(), days)
    else:
        sync.asegurar_dias(usuario_actual(), start, end)
    rows = store.get_daily_range(usuario_actual(), start, end)
    return [sync.flatten_daily(r) for r in rows]


@mcp.tool()
def get_today() -> dict:
    """Estado de hoy: la foto que hay que mirar antes de proponer sesion."""
    usuario, hoy = usuario_actual(), sync.today()
    sync.asegurar_dias(usuario, hoy, hoy)
    cacheado = store.get_daily(usuario, hoy)
    if cacheado is None:
        # Ni cache ni descarga. Pasa de madrugada, cuando Garmin aun no tiene
        # nada del dia, y decirlo es mejor que devolver una foto en blanco que
        # el modelo leeria como "cero pasos, cero sueño".
        raise sync.SincronizacionVacia(
            f"Garmin todavia no tiene datos de {hoy.isoformat()}. "
            "Prueba con get_metrics para ver los dias anteriores."
        )
    return sync.flatten_daily(cacheado)


@mcp.tool()
def get_activities(days: int = 30) -> list[dict]:
    """Entrenamientos registrados en los ultimos N dias (resumen por sesion)."""
    end = sync.today()
    start = end - dt.timedelta(days=days - 1)
    sync.asegurar_actividades(usuario_actual(), start, end)
    acts = store.get_activities(usuario_actual(), start, end)
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


def _dia_de(actividad: dict) -> str:
    """Fecha local de una sesion, que es como se indexa la cache."""
    return ((actividad.get("summaryDTO") or {}).get("startTimeLocal") or "")[:10]


def _refrescar_cache(dia: str) -> None:
    """Pone al dia la copia local tras tocar una sesion.

    Si falla no se propaga: la correccion ya esta hecha en Garmin, y quedarse
    con la cache vieja un rato es menos malo que devolver un error por algo que
    se arregla solo en la siguiente sincronizacion.
    """
    try:
        sync.refrescar_actividades(usuario_actual(), dt.date.fromisoformat(dia))
    except Exception as exc:  # noqa: BLE001
        log.warning("No se pudo refrescar la cache del %s: %s", dia, exc)


@mcp.tool()
def get_activity(activity_id: int) -> dict:
    """Una sesion ya registrada, con el detalle de sus series.

    Miralo antes de corregir nada. En fuerza, `sets` viene con la misma forma
    que acepta update_activity_sets, asi que basta con cambiar lo que este mal
    y devolverlo entero.

    En cada serie, `detected` es lo que el reloj creyo ver cuando no llega a
    ejercicio del catalogo: UNKNOWN significa que no reconocio nada. Ahi es
    donde suele faltar el ejercicio, el peso o las repeticiones, porque una
    pulsera no puede saberlos.
    """
    a = _cliente().activity(activity_id)
    s = a.get("summaryDTO") or {}
    fuera = {
        "id": a.get("activityId"),
        "name": a.get("activityName"),
        "type": (a.get("activityTypeDTO") or {}).get("typeKey"),
        "start": s.get("startTimeLocal"),
        "description": a.get("description"),
        "duration_min": round((s.get("duration") or 0) / 60, 1),
        "distance_km": round(s["distance"] / 1000, 2) if s.get("distance") else None,
        "avg_hr": s.get("averageHR"),
        "max_hr": s.get("maxHR"),
        "kcal": s.get("calories"),
    }
    try:
        series = activities.leer_series(_cliente().activity_sets(activity_id))
    except Exception as exc:  # noqa: BLE001
        log.info("Sin series para la actividad %s (%s)", activity_id, exc)
        series = []
    if series:
        fuera["sets"] = series
    return fuera


@mcp.tool()
def update_activity(
    activity_id: int,
    name: str | None = None,
    activity_type: str | None = None,
    description: str | None = None,
) -> dict:
    """Corrige los datos generales de una sesion ya registrada: como se llama,
    que deporte fue y una nota.

    Solo toca lo que mandes. Sirve para cuando el reloj clasifico mal la sesion
    (una cinta como 'walking', unas dominadas como 'cardio_training') o la dejo
    con el nombre generico que pone el.

    `activity_type` es la clave de Garmin en ingles: running, walking,
    strength_training, cycling, indoor_cycling, hiking, cardio_training, yoga,
    elliptical, other. Si no existe, el error trae las parecidas.

    Para arreglar las series de una sesion de fuerza es update_activity_sets.
    """
    cli = _cliente()
    cambios: dict = {"activity_id": activity_id}
    if name is not None:
        cli.set_activity_name(activity_id, name)
        cambios["name"] = name
    if activity_type is not None:
        tipo = activities.resolver_tipo(activity_type, cli.activity_types())
        cli.set_activity_type(activity_id, tipo)
        cambios["type"] = tipo["typeKey"]
    if description is not None:
        cli.set_activity_description(activity_id, description)
        cambios["description"] = description
    if len(cambios) == 1:
        return {**cambios, "updated": False, "note": "No se indico nada que cambiar."}
    _refrescar_cache(_dia_de(cli.activity(activity_id)))
    return {**cambios, "updated": True}


@mcp.tool()
def update_activity_sets(activity_id: int, sets: list[activities.Serie]) -> dict:
    """Reescribe las series de una sesion de fuerza ya hecha: que ejercicio era,
    cuantas repeticiones y con cuanto peso.

    Es la herramienta para lo que el reloj no puede saber. Una pulsera detecta
    que te mueves y cuenta repeticiones a ojo, pero no sabe si eso era press de
    banca o remo, ni con cuantos kilos: guarda la serie como UNKNOWN y sin
    carga. Al ponerselo, la sesion pasa a contar en el grupo muscular que toca
    y el peso queda registrado para poder seguir la progresion.

    Lee antes con get_activity: sus `sets` ya vienen con este formato. La lista
    va COMPLETA y en orden, descansos incluidos, porque reemplaza a la anterior
    entera; si mandas menos series de las que hay, las que falten se pierden.

    Los tiempos no hace falta tocarlos: cada serie hereda el momento y la
    duracion que midio el reloj, que eso si lo tiene bien.
    """
    cli = _cliente()
    actual = cli.activity_sets(activity_id) or {}
    originales = actual.get("exerciseSets") or []
    datos = [s.model_dump(exclude_none=True) for s in sets]

    inicio = None
    if not originales:  # sesion sin series grabadas: hay que anclar la primera
        inicio = ((cli.activity(activity_id).get("summaryDTO") or {}).get("startTimeGMT"))

    payload = activities.a_payload(activity_id, datos, originales, inicio)
    avisos: list[str] = []
    try:
        cli.set_activity_sets(activity_id, payload)
    except Exception as exc:  # noqa: BLE001
        # Garmin valida la variante concreta contra su propio enum y rechaza el
        # PUT entero si no la reconoce, aunque la categoria sea buena. Antes de
        # dar la correccion por perdida, se reintenta con solo el grupo
        # muscular, que es el dato que de verdad hace falta conservar.
        if not any(s.get("exercise") for s in datos):
            raise
        log.warning("Garmin rechazo las variantes en %s (%s); reintento sin ellas", activity_id, exc)
        cli.set_activity_sets(
            activity_id, activities.a_payload(activity_id, datos, originales, inicio, con_variante=False)
        )
        avisos.append(
            "Garmin no acepto las variantes concretas de ejercicio en esta sesion: "
            "se guardo solo el grupo muscular de cada una."
        )

    sin = activities.sin_ejercicio(datos)
    if sin:
        avisos.append(f"{sin} series de trabajo sin `exercise`: seguiran sin grupo muscular.")
    resultado = {"activity_id": activity_id, "sets": len(datos), "updated": True}
    if avisos:
        resultado["warnings"] = avisos
    _refrescar_cache(_dia_de(cli.activity(activity_id)))
    return resultado


@mcp.tool()
def sync_activity_to_strava(activity_id: int, force: bool = False) -> dict:
    """Lleva a Strava las series ya corregidas de una sesion de fuerza.

    Garmin exporta cada sesion a Strava UNA vez, al grabarla: si el reloj solo
    vio una serie donde hubo veinticinco, eso es lo que hay en Strava, y
    corregirlo despues con update_activity_sets no lo actualiza alli, porque
    esa sincronizacion no vuelve a viajar. Usa esta herramienta justo despues
    de corregir las series en Garmin, no en vez de hacerlo.

    Hace dos cosas: busca en Strava la sesion mal detectada por su hora de
    inicio y la OCULTA (Strava no permite borrar por API, solo dejarla fuera
    del perfil y el feed), y sube una nueva con las series corregidas. Si no
    encuentra ninguna sesion cercana en Strava, sube la corregida igualmente
    y lo dice en la respuesta: puede que esa sesion nunca se exportara o que
    ya estuviera oculta.

    Repetir la llamada sobre la misma sesion no la vuelve a subir salvo que
    pases force=True (por si la vuelves a corregir mas tarde y quieres que
    tambien se refleje).
    """
    usuario = usuario_actual()
    donde = f"{settings.public_url}/vincular-strava"
    if not strava.habilitado():
        return {"linked": False, "note": "Este servidor no tiene Strava configurado."}
    if strava.estado(usuario) is None:
        return {
            "linked": False,
            "note": (
                "No hay cuenta de Strava vinculada. Se hace desde la web, no "
                f"desde aqui, porque el permiso de Strava no puede pasar por "
                f"la conversacion: {donde}"
            ),
        }

    previo = store.leer_push_strava(usuario, activity_id)
    if previo and not force:
        return {
            "activity_id": activity_id,
            "already_synced": True,
            "strava_activity_id": previo["strava_activity_id"],
            "strava_url": f"https://www.strava.com/activities/{previo['strava_activity_id']}",
            "note": "Ya se habia sincronizado. Pasa force=True para repetirlo tras otra correccion.",
        }

    cli = _cliente()
    a = cli.activity(activity_id)
    s = a.get("summaryDTO") or {}
    crudo = cli.activity_sets(activity_id) or {}
    exercise_sets = crudo.get("exerciseSets") or []
    if not exercise_sets:
        return {
            "activity_id": activity_id,
            "note": "Esta sesion no tiene series de fuerza que subir.",
        }

    inicio = strava.desde_garmin(s.get("startTimeGMT"))
    if inicio is None:
        return {
            "activity_id": activity_id,
            "note": "No se pudo determinar la hora de inicio de la sesion en Garmin.",
        }

    resultado: dict = {"activity_id": activity_id}
    try:
        original = strava.buscar_actividad(usuario, inicio)
        if original:
            strava.ocultar_actividad(usuario, str(original["id"]))
            resultado["hidden_activity_id"] = str(original["id"])
        else:
            resultado["note"] = (
                "No se encontro en Strava ninguna sesion cerca de esa hora para "
                "ocultar; se sube la corregida de todos modos."
            )

        subida = strava.subir_fuerza(
            usuario,
            nombre=a.get("activityName") or "Entrenamiento de fuerza",
            inicio=inicio,
            duracion_seg=s.get("duration") or 0,
            exercise_sets=exercise_sets,
            external_id=f"garmin:{activity_id}",
            descripcion=a.get("description"),
        )
    except strava.SesionCaducada as exc:
        return {"linked": True, "expired": True, "note": f"{exc} Se vuelve a vincular en {donde}"}
    except strava.ErrorDeStrava as exc:
        return {"activity_id": activity_id, "error": str(exc)}

    store.registrar_push_strava(
        usuario, activity_id, subida["strava_activity_id"], resultado.get("hidden_activity_id")
    )
    resultado.update({
        "strava_activity_id": subida["strava_activity_id"],
        "strava_url": f"https://www.strava.com/activities/{subida['strava_activity_id']}",
        "synced": True,
    })
    return resultado


@mcp.tool()
def add_activity(
    name: str,
    activity_type: str,
    start: str,
    duration_min: float,
    distance_km: float | None = None,
) -> dict:
    """Da de alta a mano una sesion que el reloj no llego a grabar.

    Para cuando se entreno sin el puesto, se quedo sin bateria o se olvido
    darle a empezar. `start` es la hora local de inicio ('2026-08-13T21:30:00'),
    y `activity_type` la clave de Garmin en ingles (walking, running,
    strength_training...).

    Nace privada y sin frecuencia cardiaca, porque no hubo nada que la midiera:
    las calorias las estima Garmin con el perfil. Si la sesion si se grabo y lo
    que pasa es que esta mal, corrigela con update_activity en vez de crear otra.
    """
    cli = _cliente()
    tipo = activities.resolver_tipo(activity_type, cli.activity_types())
    payload = activities.payload_manual(
        name, tipo, activities.normalizar_inicio(start), settings.timezone,
        duration_min, distance_km,
    )
    creada = cli.create_manual_activity(payload) or {}
    nueva = creada.get("activityId")
    _refrescar_cache(_dia_de(creada) or activities.normalizar_inicio(start)[:10])
    return {"activity_id": nueva, "name": name, "type": tipo["typeKey"], "created": True}


@mcp.tool()
def get_weight(days: int = 90) -> dict:
    """Peso corporal: los pesajes de los ultimos N dias y cuanto se ha movido.

    Miralo antes de hablar de cargas relativas (el peso corporal es la carga en
    dominadas, flexiones o fondos), de composicion o de cualquier objetivo que
    dependa de la bascula. La serie es corta y con huecos, porque casi nadie se
    pesa a diario: por eso el periodo por defecto es largo.

    `body_fat_pct` y `muscle_mass_kg` solo salen si la bascula los midio.
    """
    end = sync.today()
    datos = _cliente().weigh_ins(end - dt.timedelta(days=days - 1), end)
    return weight.resumen(datos, days)


@mcp.tool()
def log_weight(weight_kg: float, when: str | None = None) -> dict:
    """Apunta un pesaje en Garmin Connect.

    Para cuando lo diga en la conversacion ("hoy he pesado 89,5"). Sin fecha se
    guarda ahora mismo; `when` acepta 'YYYY-MM-DD' (se apunta a las 8:00, hora
    del pesaje de la mañana) o 'YYYY-MM-DDTHH:MM' si se sabe la hora.

    Queda como pesaje manual, igual que si se hubiera tecleado en la app.
    """
    _cliente().add_weigh_in(weight_kg, weight.momento(when))
    return {"weight_kg": weight_kg, "logged": True}


@mcp.tool()
def get_body_composition(days: int = 90) -> dict:
    """Peso y composicion corporal medidos por su bascula inteligente, con la
    tendencia: grasa, musculo, agua, hueso, IMC y metabolismo basal.

    Esto es lo que hay que mirar para juzgar si un plan funciona. El peso a
    secas no distingue perder grasa de perder musculo, y son cosas opuestas:
    dos kilos menos con la grasa igual significa que se esta perdiendo lo que
    interesa conservar.

    Es distinto de get_weight, que da los pesajes de Garmin (los que se apuntan
    a mano o manda una bascula de la marca). Aqui se mide todos los dias y con
    bioimpedancia. Si la persona tiene bascula vinculada, esta es la buena.

    Cada `*_trend` trae el cambio del periodo y, sobre todo, la media de los
    ultimos 7 dias contra la de los 7 anteriores (`change_per_week`): el peso
    oscila mas de un kilo entre la mañana y la noche, asi que comparar dos
    pesajes sueltos no dice nada.
    """
    usuario = usuario_actual()
    # La direccion no lleva el usuario: se vincula con la sesion del navegador,
    # asi que es la misma para todos y quien la abra tendra que iniciar sesion.
    donde = f"{settings.public_url}/vincular-bascula"
    try:
        return scales.resumen(usuario, days)
    except scales.SinBascula:
        return {
            "linked": False,
            "providers": scales.catalogo(),
            "note": (
                "No hay bascula vinculada. Se hace desde la web, no desde aqui, "
                "porque la contraseña de la bascula no puede pasar por la "
                f"conversacion: {donde}. "
                "Mientras tanto, el peso de Garmin se lee con get_weight."
            ),
        }
    except scales.SesionCaducada as exc:
        return {
            "linked": True,
            "expired": True,
            "note": f"{exc} Se vuelve a vincular en {donde}",
        }


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
def save_insight(insight: insights.Insight) -> dict:
    """Deja por escrito una conclusion tuya para que la persona la lea en su panel.

    Todo lo demas de este servidor son datos de Garmin. Esto es lo unico que
    guarda lo que TU has entendido de ellos, y es lo que aparece en la pestaña
    de analisis de la web.

    Escribelo cuando llegues a algo que valga la pena releer dentro de un mes:
    por que hoy toca sesion suave, que se propone para la semana, que la grasa
    baja mientras el musculo aguanta, o que el sueño lleva cinco dias corto.
    No lo uses para responder una pregunta suelta ni para repetir un numero que
    ya esta en get_today: eso ya se ve en el panel sin tu ayuda.

    Escribe para quien lo leera sin la conversacion delante: sin "como te decia",
    con los numeros dentro de `metrics` y el porque en `body`. Un insight por
    conclusion, no uno con todo mezclado.
    """
    return insights.guardar(usuario_actual(), insight)


@mcp.tool()
def list_insights(limit: int = 20, kind: str | None = None) -> list[dict]:
    """Tus conclusiones anteriores, de la mas reciente a la mas antigua.

    Miralas al empezar una conversacion sobre entrenamiento: recuperan el hilo
    de lo que ya se habia decidido y evitan contradecir sin querer un plan de
    hace tres dias. Filtra con kind: 'lectura', 'plan', 'progreso' o 'aviso'.
    """
    return insights.listar(usuario_actual(), limit, kind)


@mcp.tool()
def delete_insight(insight_id: str) -> dict:
    """Retira una conclusion del panel, p.ej. si resulto estar equivocada."""
    borrado = insights.borrar(usuario_actual(), insight_id)
    return {"deleted": borrado} if borrado else {"deleted": False, "note": "No existe ese id."}


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

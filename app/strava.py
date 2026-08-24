"""Cliente de la API de Strava: vincular la cuenta y corregir ahi las sesiones
de fuerza que Garmin corrigio pero que el auto-export nativo ya habia subido
mal detectadas.

Por que hace falta esto. Garmin Connect exporta cada actividad a Strava una
sola vez, al grabarla. Si el reloj solo vio una serie de 25, eso es lo que
llega a Strava; cuando despues se corrige en Garmin con `update_activity_sets`,
esa correccion no vuelve a viajar — Garmin no resincroniza ediciones. La unica
forma de que Strava tambien acabe bien es ocultar alli la sesion mal
detectada y subir una nueva con las series ya corregidas.

A diferencia de Garmin (`garminconnect`) o la bascula (`feelfit.py`), esto SI
es API oficial y documentada: OAuth2 de verdad, `POST /uploads` para crear
actividades y `PUT /activities/{id}` para editar metadatos. Dos cosas, eso si,
quedan fuera de lo que la API permite:

  - No hay forma de borrar una actividad por API (no existe DELETE desde
    2017). Por eso el flujo es OCULTAR la original (`hide_from_home=true`) en
    vez de borrarla: sigue estando, pero no en el perfil ni el feed.
  - No hay forma de editar las series de una actividad ya subida, ni siquiera
    con el soporte de fuerza que Strava añadio en 2026. El formato JSON de
    `/uploads` con `sets` solo vale al crear: subir de nuevo es la unica via.

La pieza sin verificar del todo es `exercise_type`: Strava no publica la
lista de valores que acepta ese campo. Lo que hay documentado en su comunidad
de desarrolladores son ejemplos con el mismo formato que ya usa el catalogo de
Garmin (`BARBELL_BENCH_PRESS`), asi que aqui se reenvia tal cual el nombre que
ya resuelve `workouts.py` — lo mas probable es que Strava sepa mapear una
parte y el resto caiga en su "Otro" generico. Si un dia cambia el formato, es
`_series_strava` lo unico que hay que tocar.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
import urllib.parse
from typing import Any

import httpx

from . import crypto, store
from .config import settings

log = logging.getLogger(__name__)

AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
DEAUTH_URL = "https://www.strava.com/oauth/deauthorize"
API = "https://www.strava.com/api/v3"

# activity:read_all para poder encontrar la sesion mal detectada por fecha
# (las privadas no salen del alcance basico); activity:write para ocultarla y
# para subir la corregida.
SCOPE = "activity:read_all,activity:write"

ESPERA = 20  # segundos, por peticion HTTP
MARGEN_REFRESCO = 300  # refresca el token 5 min antes de que expire, no al filo
TOLERANCIA_MIN = 20  # minutos alrededor del inicio en Garmin para buscar en Strava


class ErrorDeStrava(RuntimeError):
    """Strava contesto, pero con un error."""


class SinStrava(ErrorDeStrava):
    """El usuario no tiene ninguna cuenta de Strava vinculada todavia."""


class SesionCaducada(ErrorDeStrava):
    """El token ya no vale: hay que volver a vincular desde el panel."""


def habilitado() -> bool:
    """Si el servidor tiene una app de Strava configurada.

    Sin GB_STRAVA_CLIENT_ID/SECRET la vinculacion se apaga sola: el boton no
    aparece en el panel en vez de fallar a medias cuando alguien lo pulse.
    """
    return bool(settings.strava_client_id and settings.strava_client_secret)


def redirect_uri() -> str:
    return f"{settings.public_url}/strava/callback"


def url_autorizacion(state: str) -> str:
    if not habilitado():
        raise ErrorDeStrava("Strava no esta configurado en este servidor.")
    parametros = {
        "client_id": settings.strava_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": SCOPE,
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(parametros)}"


# ------------------------------------------------------------------ vinculo
def vincular(user_id: str, code: str) -> dict:
    """Cambia el `code` de la redireccion de Strava por tokens, y los guarda.

    El nombre del atleta se guarda aparte en claro (no es un secreto, solo
    sirve para enseñarlo en el panel); los tokens van cifrados como todo lo
    demas de terceros.
    """
    with httpx.Client(timeout=ESPERA) as cliente:
        r = cliente.post(TOKEN_URL, data={
            "client_id": settings.strava_client_id,
            "client_secret": settings.strava_client_secret,
            "code": code,
            "grant_type": "authorization_code",
        })
    if r.status_code >= 400:
        raise ErrorDeStrava(f"Strava rechazo el codigo de autorizacion: {r.text[:200]}")
    datos = r.json()

    atleta = datos.pop("athlete", None) or {}
    athlete_id = str(atleta.get("id") or "")
    if not athlete_id:
        raise ErrorDeStrava("Strava no devolvio la identidad del atleta.")
    nombre = " ".join(x for x in (atleta.get("firstname"), atleta.get("lastname")) if x) or None

    store.guardar_strava(user_id, athlete_id, nombre, crypto.cifrar(json.dumps(datos)))
    return {"athlete_id": athlete_id, "athlete": nombre}


def estado(user_id: str) -> dict | None:
    fila = store.leer_strava(user_id)
    if not fila:
        return None
    return {"athlete": fila["athlete"], "athlete_id": fila["athlete_id"], "linked_at": fila["linked_at"]}


def desvincular(user_id: str) -> None:
    """Revoca el permiso en Strava (si se puede) y borra lo guardado aqui.

    La revocacion es un intento de buena fe: si falla (token ya caducado,
    Strava caido) se borra igual, que es lo que de verdad importa para dejar
    de tener acceso a la cuenta del usuario.
    """
    try:
        token = _access_token(user_id)
        with httpx.Client(timeout=ESPERA) as cliente:
            cliente.post(DEAUTH_URL, data={"access_token": token})
    except Exception as exc:  # noqa: BLE001
        log.info("No se pudo revocar el token de Strava de %s (%s); se borra igual", user_id, exc)
    store.borrar_strava(user_id)


# --------------------------------------------------------------------- tokens
def _tokens(user_id: str) -> dict:
    fila = store.leer_strava(user_id)
    if not fila:
        raise SinStrava(f"El usuario '{user_id}' no tiene Strava vinculado todavia.")
    return json.loads(crypto.descifrar(fila["tokens_enc"]))


def _refrescar(refresh_token: str) -> dict:
    with httpx.Client(timeout=ESPERA) as cliente:
        r = cliente.post(TOKEN_URL, data={
            "client_id": settings.strava_client_id,
            "client_secret": settings.strava_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
    if r.status_code >= 400:
        raise SesionCaducada("Strava ya no acepta el permiso guardado; hay que volver a vincular.")
    return r.json()


def _access_token(user_id: str) -> str:
    tokens = _tokens(user_id)
    if tokens.get("expires_at", 0) - time.time() > MARGEN_REFRESCO:
        return tokens["access_token"]
    # Strava puede rotar el refresh_token al usarlo: se guarda el que devuelva,
    # no el de antes, o la siguiente renovacion fallaria.
    nuevos = _refrescar(tokens["refresh_token"])
    store.actualizar_tokens_strava(user_id, crypto.cifrar(json.dumps(nuevos)))
    return nuevos["access_token"]


def _peticion(user_id: str, metodo: str, path: str, **kwargs: Any) -> httpx.Response:
    token = _access_token(user_id)
    with httpx.Client(timeout=ESPERA) as cliente:
        r = cliente.request(
            metodo, f"{API}{path}", headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
    if r.status_code == 401:
        raise SesionCaducada("Strava rechazo el token; hay que volver a vincular la cuenta.")
    r.raise_for_status()
    return r


# --------------------------------------------------------- localizar y ocultar
def _desde_strava(texto: str | None) -> dt.datetime | None:
    if not texto:
        return None
    try:
        return dt.datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        return None


def buscar_actividad(
    user_id: str, inicio: dt.datetime, tolerancia_min: int = TOLERANCIA_MIN
) -> dict | None:
    """La actividad de Strava mas cercana a `inicio` (debe ser aware / UTC).

    Es, casi con toda seguridad, la que trajo el auto-export nativo de
    Garmin con las series mal detectadas: no guardamos nosotros ese id en
    ningun sitio porque esa sincronizacion no pasa por aqui. Se descartan las
    que ya creamos nosotros en una pasada anterior, para no acabar ocultando
    la propia correccion.
    """
    epoch = int(inicio.timestamp())
    margen = tolerancia_min * 60
    r = _peticion(
        user_id, "GET", "/athlete/activities",
        params={"after": epoch - margen, "before": epoch + margen, "per_page": 30},
    )
    propias = store.strava_ids_subidos(user_id)
    candidatas: list[tuple[float, dict]] = []
    for a in r.json():
        if str(a.get("id")) in propias:
            continue
        cuando = _desde_strava(a.get("start_date"))
        if cuando is None:
            continue
        candidatas.append((abs((cuando - inicio).total_seconds()), a))
    if not candidatas:
        return None
    candidatas.sort(key=lambda par: par[0])
    return candidatas[0][1]


def ocultar_actividad(user_id: str, strava_activity_id: str) -> None:
    """Quita la sesion del perfil y el feed sin borrarla (la API no deja borrar).

    Sigue existiendo en la cuenta de Strava del usuario, huerfana; si quiere
    quitarla del todo tiene que hacerlo el a mano desde la app, que es el
    unico sitio donde borrar actividades sigue siendo posible.
    """
    _peticion(user_id, "PUT", f"/activities/{strava_activity_id}", data={"hide_from_home": "true"})


# --------------------------------------------------------------------- subida
_FORMATO_GARMIN = "%Y-%m-%dT%H:%M:%S.%f"  # como llega startTime en exerciseSets, en GMT


def _desde_garmin(texto: str | None) -> dt.datetime | None:
    if not texto:
        return None
    try:
        return dt.datetime.strptime(texto, _FORMATO_GARMIN).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _iso_utc(momento: dt.datetime) -> str:
    return momento.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _series_strava(exercise_sets: list[dict]) -> list[dict]:
    """El `exerciseSets` crudo de Garmin (ya corregido) -> `sets` de Strava.

    Solo las series de trabajo con ejercicio identificado: una serie sin
    ejercicio no tiene donde clasificarse en Strava, e incluir los descansos
    no aporta nada que su formato de fuerza sepa aprovechar.
    """
    salida: list[dict] = []
    for s in exercise_sets:
        if s.get("setType") != "ACTIVE":
            continue
        ejercicios = s.get("exercises") or []
        mejor = ejercicios[0] if ejercicios else {}
        tipo = mejor.get("name") or mejor.get("category")
        if not tipo or tipo == "UNKNOWN":
            continue
        inicio = _desde_garmin(s.get("startTime"))
        peso = s.get("weight")
        entrada = {
            "exercise_type": tipo,
            "repetitions": s.get("repetitionCount"),
            "weight": round(peso / 1000.0, 2) if peso else None,
            "start_time": _iso_utc(inicio) if inicio else None,
        }
        salida.append({k: v for k, v in entrada.items() if v is not None})
    return salida


def _iniciar_subida(user_id: str, cuerpo: dict, external_id: str) -> str:
    archivo = json.dumps(cuerpo, ensure_ascii=False).encode("utf-8")
    r = _peticion(
        user_id, "POST", "/uploads",
        data={"data_type": "json", "external_id": external_id},
        files={"file": ("session.json", archivo, "application/json")},
    )
    datos = r.json()
    if datos.get("error"):
        raise ErrorDeStrava(f"Strava rechazo la subida: {datos['error']}")
    return str(datos["id"])


def _esperar_subida(user_id: str, upload_id: str, intentos: int = 8, espera: float = 2.0) -> dict:
    """Strava procesa la subida en cola; el tiempo medio son un par de
    segundos, pero hay que esperar en vez de asumir que ya esta."""
    for _ in range(intentos):
        datos = _peticion(user_id, "GET", f"/uploads/{upload_id}").json()
        if datos.get("error"):
            raise ErrorDeStrava(f"Strava no pudo procesar la subida: {datos['error']}")
        if datos.get("activity_id"):
            return {"strava_activity_id": str(datos["activity_id"]), "status": datos.get("status")}
        time.sleep(espera)
    raise ErrorDeStrava(
        "Strava sigue procesando la subida; el id llegara en unos segundos, "
        "prueba a mirar la cuenta directamente."
    )


def subir_fuerza(
    user_id: str,
    *,
    nombre: str,
    inicio: dt.datetime,
    duracion_seg: float,
    exercise_sets: list[dict],
    external_id: str,
    descripcion: str | None = None,
) -> dict:
    """Sube una sesion de fuerza con sus series de verdad, no las que detecto
    el reloj. `exercise_sets` es el `exerciseSets` crudo de Garmin, tal cual lo
    devuelve `activity_sets()` DESPUES de corregirlo con `update_activity_sets`.

    Devuelve `{"strava_activity_id", "status"}`. `external_id` sirve para que,
    mirando la sesion en Strava, se pueda rastrear de que actividad de Garmin
    vino — Strava lo enseña tal cual, no lo interpreta.
    """
    series = _series_strava(exercise_sets)
    if not series:
        raise ErrorDeStrava(
            "Ninguna serie tiene un ejercicio identificado: corrige primero con "
            "update_activity_sets en Garmin, luego reintenta."
        )
    cuerpo: dict[str, Any] = {
        "name": nombre,
        "sport_type": "WeightTraining",
        "start_date": _iso_utc(inicio),
        "elapsed_time": int(round(duracion_seg)),
        "sets": series,
    }
    if descripcion:
        cuerpo["description"] = descripcion
    upload_id = _iniciar_subida(user_id, cuerpo, external_id)
    return _esperar_subida(user_id, upload_id)

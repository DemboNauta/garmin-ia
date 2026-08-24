"""El panel del usuario: sus datos, lo que la IA ha concluido y sus conexiones.

Es la unica parte del proyecto que existe para mirar, no para que la lea un
modelo. Por eso el HTML es un armazon vacio y todo lo pinta el navegador
llamando a `/panel/api/*`: asi la primera pantalla aparece al instante y cada
bloque llega cuando Garmin conteste, en vez de dejar la pagina en blanco
esperando a la llamada mas lenta.

Cada endpoint saca el usuario de la cookie de sesion, nunca de la URL, y todo
lo que devuelve va filtrado por ese user_id. La cookie es SameSite=Strict, que
es lo que permite que borrar un analisis o forzar una sincronizacion sean POST
y DELETE normales sin token anti-CSRF: un sitio ajeno no puede hacer que el
navegador los mande con la cookie puesta.
"""
from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import accounts, garmin_client, insights, muscles, profile, scales, store, strava, sync, weight
from .config import settings
from .web import COOKIE, pagina_panel

log = logging.getLogger(__name__)
router = APIRouter()

# Tope de dias que se pueden pedir de golpe. Cada dia que falte en cache es una
# llamada a Garmin, asi que sin limite una URL a mano podria encadenar cientos.
MAX_DIAS = 90


def usuario_web(request: Request) -> str:
    """El dueño de la sesion, o 401. Es la puerta de toda la API del panel."""
    user_id = accounts.usuario_de_sesion(request.cookies.get(COOKIE))
    if not user_id:
        raise HTTPException(status_code=401, detail="Sesion caducada o inexistente")
    return user_id


def _dias(dias: int, por_defecto: int) -> int:
    return max(1, min(dias or por_defecto, MAX_DIAS))


# --------------------------------------------------------------------- pagina
@router.get("/panel", response_class=HTMLResponse)
def panel(request: Request):
    if not accounts.usuario_de_sesion(request.cookies.get(COOKIE)):
        return RedirectResponse("/entrar?destino=/panel", status_code=303)
    return pagina_panel()


# ------------------------------------------------------------------ api: estado
@router.get("/panel/api/estado")
def api_estado(usuario: str = Depends(usuario_web)) -> dict:
    """Quien eres y que tienes conectado. No toca la red: se pinta al instante."""
    datos = accounts.datos_de(usuario) or {}
    bascula = scales.estado(usuario)
    return {
        "user": {
            "email": datos.get("email"),
            "is_admin": bool(datos.get("is_admin")),
            "created_at": (datos.get("created_at") or "")[:10],
        },
        "garmin": {"linked": store.leer_tokens_garmin(usuario) is not None},
        "scale": (
            {"linked": True, **bascula} if bascula
            else {"linked": False, "providers": scales.catalogo()}
        ),
        "strava": _estado_strava(usuario),
        "mcp_url": f"{settings.public_url}/mcp",
        "profile": profile.leer(usuario),
        "timezone": settings.timezone,
    }


def _estado_strava(usuario: str) -> dict:
    if not strava.habilitado():
        return {"available": False}
    vinculo = strava.estado(usuario)
    return {"available": True, "linked": True, **vinculo} if vinculo else \
        {"available": True, "linked": False}


# ------------------------------------------------------------------- api: hoy
@router.get("/panel/api/hoy")
def api_hoy(usuario: str = Depends(usuario_web)) -> dict:
    """La foto de hoy, con la de ayer de reserva.

    Ayer no es un adorno: de madrugada Garmin todavia no tiene nada del dia en
    curso, y una pantalla con todos los anillos a cero se lee como "hoy no has
    dormido" cuando en realidad es "aun no hay dato".
    """
    hoy = sync.today()
    ayer = hoy - dt.timedelta(days=1)
    try:
        sync.asegurar_dias(usuario, ayer, hoy)
    except garmin_client.GarminAuthError as exc:
        return {"garmin_linked": False, "note": str(exc)}

    crudo_hoy = store.get_daily(usuario, hoy)
    crudo_ayer = store.get_daily(usuario, ayer)
    fuente = crudo_hoy if crudo_hoy and sync.tiene_datos(crudo_hoy) else crudo_ayer
    if fuente is None:
        return {"garmin_linked": True, "date": None, "day": None}

    aplanado = sync.flatten_daily(fuente)
    return {
        "garmin_linked": True,
        "date": aplanado.get("date"),
        "stale": aplanado.get("date") != hoy.isoformat(),
        "today": hoy.isoformat(),
        "day": aplanado,
    }


# --------------------------------------------------------------- api: metricas
@router.get("/panel/api/metricas")
def api_metricas(dias: int = 30, usuario: str = Depends(usuario_web)) -> dict:
    """Serie diaria del periodo. De aqui salen las tendencias y las medias con
    las que el panel compara el dia de hoy."""
    dias = _dias(dias, 30)
    fin = sync.today()
    inicio = fin - dt.timedelta(days=dias - 1)
    try:
        fallos = sync.asegurar_dias(usuario, inicio, fin)
    except garmin_client.GarminAuthError as exc:
        return {"garmin_linked": False, "note": str(exc), "days": []}
    filas = store.get_daily_range(usuario, inicio, fin)
    return {
        "garmin_linked": True,
        # El rango pedido va en la respuesta porque solo vuelven los dias con
        # datos: sin el, el navegador no puede saber cuantos faltan y pintaria
        # diez barras enormes donde deberian verse diez de treinta.
        "from": inicio.isoformat(),
        "to": fin.isoformat(),
        "days": [sync.flatten_daily(f) for f in filas],
        "errors": fallos,
    }


# --------------------------------------------------------------- api: sesiones
@router.get("/panel/api/sesiones")
def api_sesiones(dias: int = 30, usuario: str = Depends(usuario_web)) -> dict:
    dias = _dias(dias, 30)
    fin = sync.today()
    inicio = fin - dt.timedelta(days=dias - 1)
    try:
        sync.asegurar_actividades(usuario, inicio, fin)
    except garmin_client.GarminAuthError as exc:
        return {"garmin_linked": False, "note": str(exc), "sessions": []}
    # La cache ordena por dia, no por hora: sin reordenar, dos sesiones del
    # mismo dia salen en el orden en que las devolvio Garmin y la lista deja de
    # ir estrictamente de lo mas reciente a lo mas antiguo.
    crudas = sorted(
        store.get_activities(usuario, inicio, fin),
        key=lambda a: a.get("startTimeLocal") or "",
        reverse=True,
    )
    return {
        "garmin_linked": True,
        "sessions": [
            {
                "id": a.get("activityId"),
                "name": a.get("activityName"),
                "type": (a.get("activityType") or {}).get("typeKey"),
                "start": a.get("startTimeLocal"),
                "duration_min": round((a.get("duration") or 0) / 60, 1),
                "distance_km": round((a.get("distance") or 0) / 1000, 2) or None,
                "avg_hr": a.get("averageHR"),
                "max_hr": a.get("maxHR"),
                "kcal": a.get("calories"),
                "te_aerobic": a.get("aerobicTrainingEffect"),
                "te_anaerobic": a.get("anaerobicTrainingEffect"),
            }
            for a in crudas
        ],
    }


# ----------------------------------------------------------------- api: cuerpo
@router.get("/panel/api/cuerpo")
def api_cuerpo(dias: int = 90, usuario: str = Depends(usuario_web)) -> dict:
    """Peso y composicion. Son dos fuentes distintas y se devuelven las dos:
    la bascula mide a diario y separa grasa de musculo; Garmin solo tiene lo
    que se teclea a mano, pero es la serie larga de quien no tenga bascula."""
    dias = _dias(dias, 90)
    fuera: dict = {"days": dias}

    try:
        fuera["scale"] = scales.resumen(usuario, dias)
    except scales.SinBascula:
        fuera["scale"] = {"linked": False, "providers": scales.catalogo()}
    except scales.SesionCaducada as exc:
        fuera["scale"] = {"linked": True, "expired": True, "note": str(exc)}
    except Exception as exc:  # noqa: BLE001
        # La nube del fabricante es una API no oficial: que se caiga no puede
        # tumbar la pestaña entera, porque el peso de Garmin sigue estando.
        log.warning("Fallo leyendo la bascula de %s: %s", usuario, exc)
        fuera["scale"] = {"linked": True, "error": str(exc)[:200]}

    fin = sync.today()
    try:
        crudo = garmin_client.para_usuario(usuario).weigh_ins(fin - dt.timedelta(days=dias - 1), fin)
        fuera["garmin"] = weight.resumen(crudo, dias)
    except garmin_client.GarminAuthError:
        fuera["garmin"] = {"linked": False}
    except Exception as exc:  # noqa: BLE001
        log.warning("Fallo leyendo pesajes de Garmin de %s: %s", usuario, exc)
        fuera["garmin"] = {"error": str(exc)[:200]}
    return fuera


# --------------------------------------------------------------- api: musculos
@router.get("/panel/api/musculos")
def api_musculos(dias: int = 30, usuario: str = Depends(usuario_web)) -> dict:
    """Series por grupo muscular en el periodo, para pintar el cuerpo.

    Sale de summarizedExerciseSets, que ya viene en la cache de actividades:
    no cuesta llamadas extra a Garmin mas alla de asegurar el rango.
    """
    dias = _dias(dias, 30)
    fin = sync.today()
    inicio = fin - dt.timedelta(days=dias - 1)
    try:
        return {"garmin_linked": True, "days": dias, **muscles.resumen(usuario, inicio, fin)}
    except garmin_client.GarminAuthError as exc:
        return {"garmin_linked": False, "note": str(exc)}


# --------------------------------------------------------------- api: insights
@router.get("/panel/api/insights")
def api_insights(limite: int = 30, usuario: str = Depends(usuario_web)) -> dict:
    return {"insights": insights.listar(usuario, limite)}


@router.delete("/panel/api/insights/{insight_id}")
def api_borrar_insight(insight_id: str, usuario: str = Depends(usuario_web)) -> dict:
    if not insights.borrar(usuario, insight_id):
        raise HTTPException(status_code=404, detail="No existe ese analisis")
    return {"deleted": True}


# ------------------------------------------------------------------ api: sync
@router.post("/panel/api/sync")
def api_sync(dias: int = 7, usuario: str = Depends(usuario_web)) -> dict:
    """Rebaja el periodo de Garmin salte lo que salte la cache.

    Existe porque el sincronizador de fondo solo corre para el dueño de la
    instalacion: el resto de usuarios dependen de que sus lecturas rellenen los
    huecos, y despues de sincronizar el reloj apetece un boton que lo traiga ya.
    """
    try:
        return sync.sync_range(usuario, _dias(dias, 7))
    except garmin_client.GarminAuthError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

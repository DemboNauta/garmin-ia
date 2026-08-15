"""Envoltorio sobre la libreria no oficial `garminconnect`.

Toda la capa fragil (endpoints internos de Garmin Connect) queda aislada aqui,
para que el resto del backend no se entere si un dia hay que cambiarla.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable
from typing import Any

from garminconnect import Garmin

from . import crypto, store
from .config import settings

log = logging.getLogger(__name__)
_lock = threading.Lock()


class GarminAuthError(RuntimeError):
    pass


class GarminClient:
    """Cliente perezoso y thread-safe, atado a un usuario concreto.

    Los tokens no viven en disco sino cifrados en la base (`app/crypto.py`), y
    se le pasan a la libreria como JSON en linea. Asi no hay un directorio por
    usuario que proteger ni tokens de terceros en claro sobre el sistema de
    ficheros.
    """

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self._api: Garmin | None = None
        self._tipos: list[dict] | None = None

    # ------------------------------------------------------------------ auth
    def api(self) -> Garmin:
        with _lock:
            if self._api is not None:
                return self._api
            cifrado = store.leer_tokens_garmin(self.user_id)
            if not cifrado:
                raise GarminAuthError(
                    f"El usuario '{self.user_id}' no tiene Garmin vinculado todavia."
                )
            api = Garmin()
            try:
                # La libreria acepta el tokenstore como JSON en linea, no solo
                # como ruta: eso permite tenerlos cifrados en la base.
                api.login(crypto.descifrar(cifrado))
                log.info("Sesion de Garmin reanudada para %s", self.user_id)
            except crypto.ErrorDeCifrado:
                raise
            except Exception as exc:
                raise GarminAuthError(
                    f"La sesion de Garmin de '{self.user_id}' ya no vale "
                    "(token caducado o revocado). Hay que volver a vincular."
                ) from exc
            self._api = api
            return api

    def guardar_sesion(self, api: Garmin) -> None:
        """Persiste cifrada la sesion de una vinculacion recien hecha."""
        store.guardar_tokens_garmin(self.user_id, crypto.cifrar(api.client.dumps()))
        with _lock:
            self._api = api

    def reset(self) -> None:
        with _lock:
            self._api = None

    def _call(self, fn_name: str, *args: Any) -> Any:
        """Llama a un metodo de garminconnect reintentando una vez tras re-login."""
        return self._intentar(lambda api: getattr(api, fn_name)(*args), fn_name)

    def _intentar(self, accion: Callable[[Garmin], Any], etiqueta: str) -> Any:
        """Ejecuta algo contra la sesion, reintentando una vez con una nueva.

        Lo de siempre: el token caduca y el primer intento se lleva el 401. La
        version con callable existe para lo que no tiene metodo en la libreria y
        hay que pedirle al cliente HTTP a pelo.
        """
        for attempt in (1, 2):
            try:
                return accion(self.api())
            except Exception as exc:
                if attempt == 2:
                    log.error("Fallo en %s: %s", etiqueta, exc)
                    raise
                log.warning("Fallo en %s (%s); reintentando con sesion nueva", etiqueta, exc)
                self.reset()

    # --------------------------------------------------------------- lectura
    def devices(self) -> list[dict]:
        """Dispositivos vinculados a la cuenta (Cirqa, relojes, Edge...)."""
        return self._call("get_devices") or []

    def daily_snapshot(self, day: dt.date) -> dict:
        """Foto completa de un dia. Cada bloque es opcional: no todos los
        dispositivos Garmin reportan todas las metricas (el Cirqa no da ECG,
        y la distancia en exterior depende del GPS del movil)."""
        iso = day.isoformat()
        out: dict[str, Any] = {"date": iso}

        blocks: dict[str, tuple[str, tuple]] = {
            "summary": ("get_user_summary", (iso,)),
            "sleep": ("get_sleep_data", (iso,)),
            "hrv": ("get_hrv_data", (iso,)),
            "resting_hr": ("get_rhr_day", (iso,)),
            "body_battery": ("get_body_battery", (iso, iso)),
            "training_readiness": ("get_training_readiness", (iso,)),
            "training_status": ("get_training_status", (iso,)),
            "max_metrics": ("get_max_metrics", (iso,)),  # VO2max
        }
        for key, (fn, args) in blocks.items():
            try:
                out[key] = self._call(fn, *args)
            except GarminAuthError:
                # Sin sesion no hay bloque que valga: reintentar los otros siete
                # solo sirve para devolver una foto vacia que parece un dia real.
                raise
            except Exception as exc:
                out[key] = None
                log.info("Bloque %s no disponible para %s (%s)", key, iso, exc)
        return out

    def activities(self, start: dt.date, end: dt.date) -> list[dict]:
        return self._call("get_activities_by_date", start.isoformat(), end.isoformat()) or []

    def activity_detail(self, activity_id: int | str, muestras: int = 2000) -> dict:
        """Serie temporal de una sesion (pulso, ritmo, cadencia... por segundo).

        `maxPolylineSize=0` deja fuera el trazado GPS, que es la mitad larga de
        la respuesta y aqui no se usa para nada.
        """
        return self._call("get_activity_details", str(activity_id), muestras, 0)

    def activity_hr_zones(self, activity_id: int | str) -> list[dict]:
        """Segundos en cada zona de pulso, tal y como los calcula Garmin."""
        return self._call("get_activity_hr_in_timezones", str(activity_id)) or []

    def activity(self, activity_id: int | str) -> dict:
        """Resumen de una sesion concreta, con su tipo y sus totales."""
        return self._call("get_activity", str(activity_id))

    def activity_sets(self, activity_id: int | str) -> dict:
        """Series de una sesion de fuerza, tal y como las grabo el reloj."""
        return self._call("get_activity_exercise_sets", activity_id)

    def weigh_ins(self, start: dt.date, end: dt.date) -> dict:
        """Pesajes de un rango, con composicion corporal si la bascula la dio."""
        return self._call("get_body_composition", start.isoformat(), end.isoformat()) or {}

    def add_weigh_in(self, kilos: float, momento: str) -> Any:
        """Apunta un pesaje. `momento` va en hora local y con desfase explicito."""
        return self._call("add_weigh_in", kilos, "kg", momento)

    def activity_types(self) -> list[dict]:
        """Catalogo de tipos de actividad. Cambia una vez cada varios años, asi
        que se pide una sola vez por proceso: son ~180 entradas por peticion."""
        with _lock:
            if self._tipos is not None:
                return self._tipos
        tipos = self._call("get_activity_types") or []
        with _lock:
            self._tipos = tipos
        return tipos

    # ------------------------------------------- correccion de lo ya registrado
    def set_activity_sets(self, activity_id: int | str, payload: dict) -> Any:
        """Reemplaza las series de una sesion.

        Como en los entrenamientos, Garmin no admite parches: el PUT sustituye
        la lista entera. Rechaza con 400 las variantes de ejercicio que no
        conoce, aunque la categoria si valga.
        """
        return self._call("set_activity_exercise_sets", activity_id, payload)

    def set_activity_name(self, activity_id: int | str, name: str) -> Any:
        return self._call("set_activity_name", str(activity_id), name)

    def set_activity_description(self, activity_id: int | str, description: str) -> Any:
        return self._call("set_activity_description", str(activity_id), description)

    def set_activity_summary(self, activity_id: int | str, payload: dict) -> Any:
        """Corrige los totales de una sesion: distancia, duracion, calorias...

        Va al mismo PUT de /activity-service/activity/{id} con el que la
        libreria cambia el nombre o el deporte mandando solo dos campos, de modo
        que el cuerpo puede ser parcial. Lo que no tiene garminconnect 0.3.10 es
        un metodo para el resumen, asi que aqui se llama al cliente HTTP a pelo;
        el dia que lo añadan, esto se sustituye por un `_call` normal.
        """
        ruta = f"/activity-service/activity/{int(activity_id)}"
        return self._intentar(
            lambda api: api.client.put("connectapi", ruta, json=payload, api=True),
            "set_activity_summary",
        )

    def set_activity_type(self, activity_id: int | str, tipo: dict) -> Any:
        """Cambia el deporte de una sesion (el reloj a veces se equivoca)."""
        return self._call(
            "set_activity_type",
            str(activity_id),
            tipo["typeId"],
            tipo["typeKey"],
            tipo.get("parentTypeId"),
        )

    def create_manual_activity(self, payload: dict) -> Any:
        """Da de alta una sesion que el reloj no llego a grabar."""
        return self._call("create_manual_activity_from_json", payload)

    # ------------------------------------------------------- escritura (beta)
    def create_workout(self, payload: dict) -> dict:
        """Crea un entrenamiento estructurado en Garmin Connect.

        OJO: esto usa el endpoint interno workout-service. Es la parte menos
        estable de todo el backend y la primera que se rompera. El Cirqa no
        tiene pantalla, asi que el entrenamiento se ve en la app, no en la
        muneca; si algun dia añades un reloj, se sincronizara solo.
        """
        return self._call("upload_workout", payload)

    def workout_detail(self, workout_id: int | str) -> dict:
        """Estructura completa de un entrenamiento (la que hay que editar)."""
        return self._call("get_workout_by_id", workout_id)

    def update_workout(self, workout_id: int | str, payload: dict) -> dict:
        """Reemplaza un entrenamiento entero.

        Garmin no admite parches: el PUT sustituye toda la estructura. Conserva
        el id, asi que las programaciones de calendario que apunten a el siguen
        siendo validas y no hay que reprogramar tras editar el contenido.
        """
        return self._call("update_workout", workout_id, payload)

    def delete_workout(self, workout_id: int | str) -> Any:
        return self._call("delete_workout", workout_id)

    def list_workouts(self, limit: int = 20) -> list[dict]:
        return self._call("get_workouts", 0, limit) or []

    # ------------------------------------------------------------- calendario
    def schedule_workout(self, workout_id: int, day: dt.date) -> dict:
        return self._call("schedule_workout", workout_id, day.isoformat())

    def scheduled_workouts(self, year: int, month: int) -> Any:
        """Lo programado en un mes. De aqui sale el id de programacion, que NO
        es el id del entrenamiento y es el unico que acepta unschedule."""
        return self._call("get_scheduled_workouts", year, month)

    def unschedule_workout(self, schedule_id: int | str) -> Any:
        """Saca del calendario sin borrar la plantilla del entrenamiento."""
        return self._call("unschedule_workout", schedule_id)


_clientes: dict[str, GarminClient] = {}


def para_usuario(user_id: str) -> GarminClient:
    """Cliente de un usuario, reutilizando su sesion entre peticiones."""
    with _lock:
        if user_id not in _clientes:
            _clientes[user_id] = GarminClient(user_id)
        return _clientes[user_id]


def olvidar(user_id: str) -> None:
    """Descarta la sesion cacheada, p.ej. al desvincular o borrar la cuenta."""
    with _lock:
        _clientes.pop(user_id, None)


def migrar_tokens_de_disco() -> bool:
    """Mete en la base, cifrados, los tokens sueltos de la epoca mono-usuario.

    Antes vivian en texto plano en GB_TOKENSTORE. Se copian al dueño y el
    fichero se renombra, para no dejar una copia legible por ahi.
    """
    from garminconnect.client import token_file_path

    dueño = settings.owner_user_id
    if store.leer_tokens_garmin(dueño):
        return False
    fichero = token_file_path(settings.tokenstore)
    if not fichero.exists():
        return False

    store.crear_usuario(dueño)
    store.guardar_tokens_garmin(dueño, crypto.cifrar(fichero.read_text(encoding="utf-8")))
    fichero.rename(fichero.with_suffix(".json.migrado"))
    log.warning("Tokens de %s migrados a la base cifrados; %s renombrado", dueño, fichero.name)
    return True

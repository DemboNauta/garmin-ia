"""Envoltorio sobre la libreria no oficial `garminconnect`.

Toda la capa fragil (endpoints internos de Garmin Connect) queda aislada aqui,
para que el resto del backend no se entere si un dia hay que cambiarla.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
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
        for attempt in (1, 2):
            try:
                return getattr(self.api(), fn_name)(*args)
            except Exception as exc:
                if attempt == 2:
                    log.error("Fallo en %s: %s", fn_name, exc)
                    raise
                log.warning("Fallo en %s (%s); reintentando con sesion nueva", fn_name, exc)
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

    def activity_detail(self, activity_id: int | str) -> dict:
        return self._call("get_activity_details", activity_id)

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

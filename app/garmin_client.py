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

from .config import settings

log = logging.getLogger(__name__)
_lock = threading.Lock()


class GarminAuthError(RuntimeError):
    pass


class GarminClient:
    """Cliente perezoso y thread-safe. Reutiliza los tokens en disco."""

    def __init__(self) -> None:
        self._api: Garmin | None = None

    # ------------------------------------------------------------------ auth
    def api(self) -> Garmin:
        with _lock:
            if self._api is not None:
                return self._api
            api = Garmin()
            try:
                # Reanuda la sesion desde los tokens OAuth guardados en disco.
                api.login(settings.tokenstore)
                log.info("Sesion de Garmin reanudada desde %s", settings.tokenstore)
            except Exception as exc:  # token ausente, caducado o corrupto
                log.warning("No se pudo reanudar la sesion (%s); login completo", exc)
                if not settings.garmin_email or not settings.garmin_password:
                    raise GarminAuthError(
                        "No hay tokens validos ni credenciales. Ejecuta `python -m app.login`."
                    ) from exc
                # Sin prompt_mfa: si la cuenta pide MFA aqui no hay nadie para
                # teclear el codigo, asi que falla y se resuelve con app/login.py.
                api = Garmin(settings.garmin_email, settings.garmin_password)
                try:
                    # login(tokenstore) persiste los tokens al terminar.
                    api.login(settings.tokenstore)
                except Exception as exc2:
                    raise GarminAuthError(
                        "Login automatico fallido (MFA o rate limit de Garmin). "
                        "Ejecuta `python -m app.login` una vez de forma interactiva."
                    ) from exc2
            self._api = api
            return api

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

    def schedule_workout(self, workout_id: int, day: dt.date) -> dict:
        return self._call("schedule_workout", workout_id, day.isoformat())

    def list_workouts(self, limit: int = 20) -> list[dict]:
        return self._call("get_workouts", 0, limit) or []


client = GarminClient()

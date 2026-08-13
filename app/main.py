from __future__ import annotations

import datetime as dt
import logging
import secrets
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Header, HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import store, sync
from .config import settings
from .mcp_server import mcp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("garmin-bridge")
scheduler = BackgroundScheduler()

# Token por defecto de config.py: si llega vivo a produccion, el servicio queda
# abierto de par en par. Mejor no arrancar que arrancar sin proteccion.
_TOKEN_INSEGURO = "cambiame"
_TOKEN_MIN_LEN = 32


def _check_token_config() -> None:
    if settings.api_token == _TOKEN_INSEGURO or len(settings.api_token) < _TOKEN_MIN_LEN:
        raise RuntimeError(
            f"GB_API_TOKEN sin configurar o demasiado corto (minimo {_TOKEN_MIN_LEN} "
            "caracteres). Genera uno con: openssl rand -hex 32"
        )


def _token_valido(authorization: str) -> bool:
    # compare_digest en vez de == para no filtrar el token por tiempo de respuesta.
    return secrets.compare_digest(authorization, f"Bearer {settings.api_token}")


def auth(authorization: str = Header(default="")) -> None:
    if not _token_valido(authorization):
        raise HTTPException(status_code=401, detail="Token invalido")


class BearerAuthMiddleware:
    """Exige el mismo bearer que la API REST a una sub-app ASGI montada.

    El endpoint MCP se monta con app.mount(), asi que no pasa por las
    dependencias de FastAPI: sin esto quedaria publico.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cabeceras = dict(scope.get("headers") or [])
        autorizacion = cabeceras.get(b"authorization", b"").decode("latin-1")
        if not _token_valido(autorizacion):
            respuesta = JSONResponse({"detail": "Token invalido"}, status_code=401)
            await respuesta(scope, receive, send)
            return
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_token_config()
    store.init()
    # Repara una cache envenenada por arranques anteriores sin sesion valida.
    sync.limpiar_cache_vacia()
    if settings.sync_enabled:
        scheduler.add_job(
            lambda: log.info("Sync automatico: %s", sync.sync_range(settings.sync_backfill_days)),
            "interval",
            minutes=settings.sync_interval_minutes,
            next_run_time=dt.datetime.now() + dt.timedelta(seconds=20),
        )
        scheduler.start()
    async with mcp.session_manager.run():
        yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Garmin Bridge", version="0.1.0", lifespan=lifespan)
# El endpoint MCP queda en /mcp — es el que se configura como conector remoto.
# Va envuelto en BearerAuthMiddleware: expone datos de salud y permite escribir
# en la cuenta de Garmin, asi que no puede quedar publico.
app.mount("/mcp", BearerAuthMiddleware(mcp.streamable_http_app()))


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "date": sync.today().isoformat()}


@app.get("/metrics", dependencies=[Depends(auth)])
def metrics(days: int = 7) -> list[dict]:
    end = sync.today()
    return [sync.flatten_daily(r) for r in store.get_daily_range(end - dt.timedelta(days=days - 1), end)]


@app.get("/activities", dependencies=[Depends(auth)])
def activities(days: int = 30) -> list[dict]:
    end = sync.today()
    return store.get_activities(end - dt.timedelta(days=days - 1), end)


@app.post("/sync", dependencies=[Depends(auth)])
def force_sync(days: int = 7) -> dict:
    return sync.sync_range(days)

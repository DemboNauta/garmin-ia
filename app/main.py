from __future__ import annotations

import datetime as dt
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from mcp.server.auth.routes import (
    build_resource_metadata_url,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import garmin_client, identity, panel, store, sync, web
from .config import settings
from .mcp_server import mcp
from .oauth_provider import proveedor

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


def usuario_autenticado(authorization: str = Header(default="")) -> str:
    """Valida el bearer y devuelve de quien son los datos que se piden.

    Se inyecta como parametro en vez de viajar por variable de contexto: FastAPI
    ejecuta dependencia y endpoint en hilos distintos del pool, asi que un
    ContextVar fijado aqui no llegaria alli. Las herramientas MCP si usan el
    contexto, porque ahi no hay inyeccion de dependencias donde colgarlo.
    """
    if not _token_valido(authorization):
        raise HTTPException(status_code=401, detail="Token invalido")
    return identity.dueño()


async def _resolver_usuario(autorizacion: str) -> str | None:
    """De la cabecera Authorization al usuario, o None si no vale.

    Se aceptan dos credenciales: un token OAuth (el camino de los usuarios, que
    llegan por claude.ai o ChatGPT) y el bearer estatico del dueño, que se
    conserva para administracion y para clientes de linea de comandos.
    """
    if not autorizacion.startswith("Bearer "):
        return None
    token = autorizacion[7:]
    acceso = await proveedor.load_access_token(token)
    if acceso and acceso.subject:
        return acceso.subject
    if _token_valido(autorizacion):
        return identity.dueño()
    return None


class BearerAuthMiddleware:
    """Autentica la sub-app MCP y fija de quien son los datos.

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
        usuario = await _resolver_usuario(autorizacion)
        if usuario is None:
            # La RFC 9728 exige apuntar aqui a los metadatos del recurso: es
            # como el cliente descubre contra que servidor tiene que autenticarse.
            respuesta = JSONResponse(
                {"detail": "Token invalido"},
                status_code=401,
                headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{URL_METADATOS}"'
                },
            )
            await respuesta(scope, receive, send)
            return
        testigo = identity.fijar_usuario(usuario)
        try:
            await self.app(scope, receive, send)
        finally:
            identity.restaurar(testigo)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_token_config()
    store.init()
    # Repara una cache envenenada por arranques anteriores sin sesion valida.
    garmin_client.migrar_tokens_de_disco()
    sync.limpiar_cache_vacia(identity.dueño())
    if settings.sync_enabled:
        scheduler.add_job(
            lambda: log.info("Sync automatico: %s", sync.sync_range(identity.dueño(), settings.sync_backfill_days)),
            "interval",
            minutes=settings.sync_interval_minutes,
            next_run_time=dt.datetime.now() + dt.timedelta(seconds=20),
        )
        scheduler.start()
    async with mcp.session_manager.run():
        yield
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Garmin Bridge", version="0.2.0", lifespan=lifespan)

# --- OAuth 2.1 -------------------------------------------------------------
# claude.ai y ChatGPT solo saben autenticarse por OAuth: no admiten cabeceras
# propias, y la especificacion MCP prohibe el token en la URL. El protocolo lo
# implementa el SDK; nosotros ponemos el proveedor y las paginas de login.
_emisor = AnyHttpUrl(settings.public_url)
_recurso = AnyHttpUrl(f"{settings.public_url}/mcp")
# La RFC 9728 intercala /.well-known/... entre el host y el path del recurso, o
# sea /.well-known/oauth-protected-resource/mcp. Se calcula con el helper del
# SDK para que la cabecera WWW-Authenticate no se desvie de la ruta registrada.
URL_METADATOS = build_resource_metadata_url(_recurso)

for ruta in create_auth_routes(
    provider=proveedor,
    issuer_url=_emisor,
    client_registration_options=ClientRegistrationOptions(enabled=True),
    revocation_options=RevocationOptions(enabled=True),
):
    app.router.routes.append(ruta)

# RFC 9728: dice a los clientes donde esta el servidor de autorizacion.
for ruta in create_protected_resource_routes(
    resource_url=_recurso,
    authorization_servers=[_emisor],
    resource_name="Garmin Bridge",
):
    app.router.routes.append(ruta)

app.include_router(web.router)
app.include_router(panel.router)
# La hoja de estilo y el JS del panel. Van como ficheros y no incrustados en el
# HTML para que el navegador los cachee entre visitas y para no tener un
# dashboard entero dentro de una cadena de Python.
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
# El endpoint MCP queda en /mcp — es el que se configura como conector remoto.
# Va envuelto en BearerAuthMiddleware: expone datos de salud y permite escribir
# en la cuenta de Garmin, asi que no puede quedar publico.
app.mount("/mcp", BearerAuthMiddleware(mcp.streamable_http_app()))


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "date": sync.today().isoformat()}


@app.get("/metrics")
def metrics(days: int = 7, usuario: str = Depends(usuario_autenticado)) -> list[dict]:
    end = sync.today()
    start = end - dt.timedelta(days=days - 1)
    sync.asegurar_dias(usuario, start, end)
    return [sync.flatten_daily(r) for r in store.get_daily_range(usuario, start, end)]


@app.get("/activities")
def activities(days: int = 30, usuario: str = Depends(usuario_autenticado)) -> list[dict]:
    end = sync.today()
    start = end - dt.timedelta(days=days - 1)
    sync.asegurar_actividades(usuario, start, end)
    return store.get_activities(usuario, start, end)


@app.post("/sync")
def force_sync(days: int = 7, usuario: str = Depends(usuario_autenticado)) -> dict:
    return sync.sync_range(usuario, days)

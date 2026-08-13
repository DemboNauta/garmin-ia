from __future__ import annotations

import datetime as dt
import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Header, HTTPException

from . import store, sync
from .config import settings
from .mcp_server import mcp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("garmin-bridge")
scheduler = BackgroundScheduler()


def auth(authorization: str = Header(default="")) -> None:
    if authorization != f"Bearer {settings.api_token}":
        raise HTTPException(status_code=401, detail="Token invalido")


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init()
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
app.mount("/mcp", mcp.streamable_http_app())


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

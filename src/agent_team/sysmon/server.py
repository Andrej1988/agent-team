"""HTTP API of the host metrics collector (read-only, internal network only)."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.concurrency import run_in_threadpool

from . import history_load, history_temperature, history_usage
from .drives import get_drive_health
from .host import get_all


@asynccontextmanager
async def lifespan(_app: FastAPI):
    history_temperature.start_sampler()
    history_load.start_sampler()
    history_usage.start_sampler()
    yield
    history_temperature.stop_sampler()
    history_load.stop_sampler()
    history_usage.stop_sampler()


app = FastAPI(title="agent-team-sysmon", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/host")
async def host():
    return await run_in_threadpool(get_all)  # cpu_percent samples for ~1 s


@app.get("/host/drives")
async def drives():
    return await run_in_threadpool(get_drive_health)


@app.get("/host/temperature-history")
def temperature_history(hours: float = Query(24.0, ge=0.1, le=168.0)):
    return history_temperature.get_history(hours=hours)


@app.get("/host/load-history")
def load_history(hours: float = Query(24.0, ge=0.1, le=168.0)):
    return history_load.get_history(hours=hours)


@app.get("/host/usage-history")
def usage_history(hours: float = Query(24.0, ge=0.1, le=168.0)):
    return history_usage.get_history(hours=hours)

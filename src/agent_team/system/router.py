"""/api/system/... — monitoring for the user (Access), never for agents:
the access gate lets internal clients reach only /p/<project>/api/."""

from __future__ import annotations

import asyncio
import json

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

import os

from .. import projects
from . import docker_metrics, docker_ro, logs

router = APIRouter(prefix="/api/system")


def _groups() -> list[dict]:
    return logs.groups([(p.id, p.name) for p in projects.all_projects()])


def _fail(e: Exception):
    if isinstance(e, docker_ro.NotConfigured):
        raise HTTPException(503, str(e))
    if isinstance(e, LookupError):
        raise HTTPException(404, str(e))
    if isinstance(e, ValueError):
        raise HTTPException(400, str(e))
    if isinstance(e, httpx.HTTPError):
        raise HTTPException(502, f"Docker-API nicht erreichbar: {e}")
    raise e


@router.get("/containers")
async def containers():
    gs = _groups()
    try:
        raw = await docker_ro.containers()
    except Exception as e:  # noqa: BLE001
        _fail(e)
    return {"groups": [{"id": g["id"], "label": g["label"]} for g in gs],
            "containers": logs.container_rows(raw, gs)}


@router.get("/logs/{name}")
async def container_logs(name: str, tail: int = Query(200, ge=1, le=5000)):
    try:
        lines = await docker_ro.logs(name, tail)
    except Exception as e:  # noqa: BLE001
        _fail(e)
    return {"container": name, "lines": [logs.normalize(ln) for ln in lines]}


@router.get("/logs/{name}/stream")
async def container_log_stream(name: str, request: Request):
    """Server-sent events: one `line` event per new log line."""
    if not docker_ro.valid_name(name):
        raise HTTPException(400, "ungültiger Containername")
    try:
        docker_ro.base_url()
    except docker_ro.NotConfigured as e:
        raise HTTPException(503, str(e))

    async def gen():
        it = docker_ro.follow(name).__aiter__()
        nxt = None
        try:
            while not await request.is_disconnected():
                if nxt is None:
                    nxt = asyncio.ensure_future(it.__anext__())
                done, _ = await asyncio.wait({nxt}, timeout=25)
                if not done:
                    yield ": keepalive\n\n"
                    continue
                try:
                    line = nxt.result()
                except StopAsyncIteration:
                    yield "event: end\ndata: {}\n\n"
                    return
                except Exception as e:  # noqa: BLE001
                    yield f"event: error\ndata: {json.dumps(str(e))}\n\n"
                    return
                nxt = None
                yield f"event: line\ndata: {json.dumps(logs.normalize(line))}\n\n"
        finally:
            if nxt is not None:
                nxt.cancel()
            await it.aclose()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- Docker metrics -------------------------------------------------------------

@router.get("/docker/containers")
async def docker_containers():
    try:
        return await docker_metrics.containers()
    except Exception as e:  # noqa: BLE001
        _fail(e)


@router.get("/docker/disk")
async def docker_disk():
    try:
        return await docker_metrics.disk()
    except Exception as e:  # noqa: BLE001
        _fail(e)


# ---- host metrics: proxied to the collector (agent_team.sysmon) -------------------

_HOST_PATHS = {"": "/host", "drives": "/host/drives", "temperature-history": "/host/temperature-history",
               "load-history": "/host/load-history", "usage-history": "/host/usage-history"}


async def _sysmon(path: str, params: dict):
    url = (os.environ.get("AT_SYSMON_URL") or "").rstrip("/")
    if not url:
        raise HTTPException(503, "AT_SYSMON_URL ist nicht gesetzt — Host-Überwachung nicht eingerichtet")
    try:
        async with httpx.AsyncClient(base_url=url, timeout=20.0) as c:
            r = await c.get(path, params=params)
            r.raise_for_status()
            return r.json()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Host-Sammler nicht erreichbar: {e}")


@router.get("/host")
async def host():
    return await _sysmon("/host", {})


@router.get("/host/{kind}")
async def host_part(kind: str, hours: float = Query(24.0, ge=0.1, le=168.0)):
    if kind not in _HOST_PATHS or not kind:
        raise HTTPException(404, "unbekannt")
    return await _sysmon(_HOST_PATHS[kind], {} if kind == "drives" else {"hours": hours})

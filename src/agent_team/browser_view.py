"""Live view of a project's persistent browser (docs/BROWSER-TAKEOVER.md).

``AT_BROWSER_VIEWS`` maps a project to its browser container's noVNC base,
e.g. ``{"myproject": "http://agent-team-browser-myproject:6080"}``. The web
app proxies noVNC's static files (``/browser/<project>/vnc.html`` …) and its
WebSocket (``/browser/<project>/websockify``) so the user can take the
browser over from the PWA — to log in, enter a code or solve a captcha.

User-only: the HTTP access gate already keeps internal and notify clients
out of ``/browser/``; WebSockets bypass HTTP middleware, so the socket route
checks the same rules itself (``ws_user``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Callable, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import Response

log = logging.getLogger("agent-team")

router = APIRouter()

_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
# Set by app.py: the same identity rules the HTTP access gate applies.
ws_user: Callable[[WebSocket], Optional[str]] = lambda ws: None  # noqa: E731


def views() -> dict[str, str]:
    try:
        raw = json.loads(os.environ.get("AT_BROWSER_VIEWS") or "{}")
    except ValueError:
        log.warning("AT_BROWSER_VIEWS is not valid JSON")
        return {}
    return {str(k): str(v).rstrip("/") for k, v in raw.items() if isinstance(v, str)}


def _base(project: str) -> str:
    base = views().get(project)
    if not base:
        raise HTTPException(404, "no browser for this project")
    return base


def view_url(project: str) -> str:
    """The PWA link that opens the live view, scaled to the screen."""
    return (f"/browser/{project}/vnc.html?autoconnect=1&resize=scale&reconnect=1"
            f"&path=browser/{project}/websockify")


@router.get("/api/browser")
def list_views() -> list[dict]:
    return [{"project": p, "url": view_url(p)} for p in sorted(views())]


@router.websocket("/browser/{project}/websockify")
async def vnc_socket(ws: WebSocket, project: str) -> None:
    base = views().get(project)
    if not base or not ws_user(ws):
        await ws.close(code=1008)
        return
    from websockets.asyncio.client import connect  # uvicorn[standard] ships websockets

    proto = ws.headers.get("sec-websocket-protocol", "")
    sub = [p.strip() for p in proto.split(",") if p.strip()]
    target = base.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + "/websockify"
    try:
        upstream = await connect(target, subprotocols=sub or None, max_size=None, open_timeout=10)
    except Exception as e:
        log.warning("browser view %s: upstream unreachable: %s", project, e)
        await ws.close(code=1011)
        return
    await ws.accept(subprotocol=upstream.subprotocol)

    async def to_upstream() -> None:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            data = msg.get("bytes")
            await upstream.send(data if data is not None else msg.get("text") or "")

    async def to_client() -> None:
        async for data in upstream:
            if isinstance(data, bytes):
                await ws.send_bytes(data)
            else:
                await ws.send_text(data)

    tasks = [asyncio.create_task(to_upstream()), asyncio.create_task(to_client())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await upstream.close()
        try:
            await ws.close()
        except RuntimeError:
            pass


@router.get("/browser/{project}/{path:path}")
async def vnc_files(project: str, path: str, request: Request) -> Response:
    """noVNC's static page and scripts, from the browser container."""
    base = _base(project)
    if not _PATH_RE.match(path) or ".." in path:
        raise HTTPException(400, "bad path")
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            r = await c.get(f"{base}/{path}")
        except httpx.HTTPError as e:
            raise HTTPException(502, f"browser unreachable: {e}") from e
    keep = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "last-modified", "etag")}
    return Response(r.content, status_code=r.status_code, headers={**keep, "cache-control": "no-cache"})

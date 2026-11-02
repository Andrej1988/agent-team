"""Service channels — a program instead of an agent behind a chat channel.

The program (e.g. a trading bot that asks for confirmations) reaches
these from the agent-team-notify network:

    POST /p/<project>/api/service/<channel>/send      {text, title?, buttons?}
         buttons = rows of {label, data}; shown under the message
    GET  /p/<project>/api/service/<channel>/commands?after=<seq>&wait=<s>
         long poll; my typed commands and button taps with seq > after.
         after=-1 = "from now on": returns no commands, only the current seq.
    POST /p/<project>/api/service/<channel>/menu      {commands: [[cmd, description], …]}
         the command menu shown as chips above the input

and the app (behind Access) sends my input:

    GET  /api/service-channels                         ids + menus
    POST /api/service/<channel>/command                {text, label?, button_seq?}

Commands are ``command`` events in the channel's transcript (written by
agent-team-chat, the transcript's only writer), so they show up in the chat
and the transcript is the queue. The menu is web-owned (``service.json``).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import projects
from . import log as chatlog
from . import web

router = APIRouter()

MAX_WAIT_S = 30.0
POLL_S = 0.5


def _service_channel(channel: str, project: str | None = None) -> projects.Channel:
    try:
        ch = projects.get_channel(channel)
    except projects.UnknownProject:
        raise HTTPException(404, f"unknown channel {channel!r}")
    if not ch.service or (project is not None and ch.project != project):
        raise HTTPException(404, f"{channel!r} is not a service channel of {project or 'any project'}")
    return ch


def _menu_path(channel: str):
    return chatlog.channel_dir(channel) / "service.json"


def load_menu(channel: str) -> list[list[str]]:
    try:
        return json.loads(_menu_path(channel).read_text()).get("menu") or []
    except (OSError, ValueError):
        return []


def _last_seq(channel: str) -> int:
    last = 0
    for ev in chatlog.iter_tail(channel, max_bytes=65_536):
        last = ev["seq"]
    return last


def pending_commands(channel: str, after: int) -> list[dict]:
    return [{"seq": ev["seq"], "ts": ev.get("ts"), "text": ev.get("text") or "",
             "label": ev.get("label"), "button_seq": ev.get("button_seq")}
            for ev in chatlog.read_events(channel, after=after) if ev.get("type") == "command"]


# ---- the program (agent-team-notify network) ------------------------------------

@router.post("/p/{project}/api/service/{channel}/send")
async def send(project: str, channel: str, request: Request) -> Any:
    _service_channel(channel, project)
    body = await request.json()
    if not isinstance(body, dict) or not str(body.get("text") or "").strip():
        raise HTTPException(400, "body must be {text, title?, buttons?}")
    out = {"text": str(body["text"]), "title": body.get("title"), "source": body.get("source") or channel}
    if body.get("buttons"):
        out["buttons"] = body["buttons"]
    return await web._forward("POST", f"/channels/{channel}/alert", out)


@router.get("/p/{project}/api/service/{channel}/commands")
async def commands(project: str, channel: str, after: int = -1, wait: float = 25.0) -> dict:
    _service_channel(channel, project)
    if after < 0:
        return {"commands": [], "last_seq": _last_seq(channel)}
    deadline = time.monotonic() + min(max(wait, 0.0), MAX_WAIT_S)
    while True:
        cmds = pending_commands(channel, after)
        if cmds or time.monotonic() >= deadline:
            return {"commands": cmds, "last_seq": cmds[-1]["seq"] if cmds else after}
        await asyncio.sleep(POLL_S)


@router.post("/p/{project}/api/service/{channel}/menu")
async def set_menu(project: str, channel: str, request: Request) -> dict:
    _service_channel(channel, project)
    body = await request.json()
    menu = [[str(c)[:32], str(d)[:120]] for c, d in (body.get("commands") or [])[:30]]
    path = _menu_path(channel)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"menu": menu}, ensure_ascii=False))
    tmp.replace(path)
    return {"ok": True, "menu": menu}


# ---- the app (behind Access) ----------------------------------------------------

@router.get("/api/service-channels")
def service_channels() -> list[dict]:
    return [{"id": ch.id, "menu": load_menu(ch.id)} for ch in projects.all_channels() if ch.service]


@router.post("/api/service/{channel}/command")
async def command(channel: str, request: Request) -> Any:
    try:
        ch = projects.get_channel(channel)
    except projects.UnknownProject:
        raise HTTPException(404, f"unknown channel {channel!r}")
    if not ch.lead:
        _service_channel(channel)
    body = await request.json()
    text = str((body or {}).get("text") or "").strip()
    if not text:
        raise HTTPException(400, "body must be {text, label?, button_seq?}")
    return await web._forward("POST", f"/channels/{channel}/command", {
        "text": text, "label": body.get("label"), "button_seq": body.get("button_seq")})

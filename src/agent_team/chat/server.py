"""agent-team-chat — the chat service (internal network only).

Holds one ``ChannelRuntime`` per configured channel and exposes them to the
web app (``agent-team-web`` proxies the browser's requests here) and to
services that post alerts. Everything the agent says goes into the channel's
transcript (``chat/log.py``); the web app reads that directly from the shared
data dir, so this API only takes commands.

Not reachable from the tunnel: the container is only on agent-team-internal
(it holds the Docker socket and the workspaces).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .. import budget, decisions, log_redaction, projects
from . import log as chatlog
from ..log_redaction import redact
from . import claude_login
from .log import chat_root
from .runtime import ChannelRuntime

log_redaction.install()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("agent_team.chat")

# Optional legacy state.json (read-only, AT_BRIDGE_STATE): session ids of an
# earlier bot setup, imported once per channel. Absent = nothing to import.
BRIDGE_STATE = Path(os.environ.get("AT_BRIDGE_STATE", "/bridge/state.json"))

runtimes: dict[str, ChannelRuntime] = {}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    for ch in projects.all_channels():
        rt = ChannelRuntime(ch, BRIDGE_STATE if BRIDGE_STATE.exists() else None)
        rt.expire_stale_requests()
        runtimes[ch.id] = rt
    log.info("chat service up: %d channel(s): %s", len(runtimes), ", ".join(runtimes))
    prober = asyncio.create_task(_probe_loop())
    yield
    prober.cancel()
    for rt in runtimes.values():
        await rt.drop_client(keep_session=True)


app = FastAPI(title="agent-team-chat", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def _rt(channel: str) -> ChannelRuntime:
    rt = runtimes.get(channel)
    if rt is None:
        # Task chats appear while the service runs (registered by the web app).
        try:
            ch = projects.get_channel(channel)
        except projects.UnknownProject:
            raise HTTPException(404, f"unknown channel {channel!r}")
        rt = runtimes[channel] = ChannelRuntime(ch, None)
    return rt


class Message(BaseModel):
    text: str = Field("", max_length=50_000)
    quote: Optional[str] = Field(None, max_length=20_000)
    images: list[dict] = Field(default_factory=list)
    note: Optional[str] = Field(None, max_length=300)   # automatic prompt, shown as this system line


class Settings(BaseModel):
    backend: Optional[str] = None
    model: Optional[str] = None
    perm: Optional[str] = None


class PermissionAnswer(BaseModel):
    decision: str


class QuestionAnswer(BaseModel):
    answers: list[dict]


class Button(BaseModel):
    label: str = Field(..., min_length=1, max_length=64)
    data: str = Field(..., min_length=1, max_length=64)


class Alert(BaseModel):
    text: str = Field(..., min_length=1, max_length=20_000)
    title: Optional[str] = Field(None, max_length=200)
    source: Optional[str] = Field(None, max_length=100)
    # Rows of buttons (service channels): a tap becomes a `command` event.
    buttons: Optional[list[list[Button]]] = Field(None, max_length=8)


class Feed(BaseModel):
    """A task chat's mirrored pipeline output: a stage agent's message
    (``assistant`` + ``author``) or one of its tool calls (``tool_use``)."""
    type: str = Field(..., pattern="^(assistant|tool_use)$")
    text: str = Field(..., min_length=1, max_length=20_000)
    name: Optional[str] = Field(None, max_length=100)
    author: Optional[str] = Field(None, max_length=100)


class Command(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)   # typed text or the button's data
    label: Optional[str] = Field(None, max_length=64)       # set for a button tap
    button_seq: Optional[int] = None                         # the message the button was under


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "channels": len(runtimes)}


@app.get("/channels")
def channels() -> list[dict]:
    return [rt.info() for rt in runtimes.values()]


@app.get("/channels/{channel}")
def channel_info(channel: str) -> dict:
    return _rt(channel).info()


@app.post("/channels/{channel}/message")
async def message(channel: str, body: Message) -> dict:
    try:
        return await _rt(channel).submit(body.text, body.quote, body.images, note=body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/channels/{channel}/settings")
async def settings(channel: str, body: Settings) -> dict:
    try:
        return await _rt(channel).update_settings(body.backend, body.model, body.perm)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.post("/channels/{channel}/stop")
async def stop(channel: str) -> dict:
    return {"stopped": await _rt(channel).stop()}


@app.post("/channels/{channel}/reset")
async def reset(channel: str) -> dict:
    rt = _rt(channel)
    if rt.lock.locked():
        await rt.stop()
    await rt.reset()
    return {"ok": True}


@app.post("/channels/{channel}/permission/{req_id}")
def permission(channel: str, req_id: str, body: PermissionAnswer) -> dict:
    try:
        ok = _rt(channel).answer_permission(req_id, body.decision)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(409, "request expired or already answered")
    return {"ok": True}


@app.post("/channels/{channel}/question/{req_id}")
def question(channel: str, req_id: str, body: QuestionAnswer) -> dict:
    if not _rt(channel).answer_question(req_id, body.answers):
        raise HTTPException(409, "question expired or already answered")
    return {"ok": True}


@app.post("/channels/{channel}/alert")
def alert(channel: str, body: Alert) -> dict:
    rt = _rt(channel)
    if not body.buttons:
        return rt.alert(body.text, body.title, body.source)
    rows = [[b.model_dump() for b in row[:6]] for row in body.buttons if row]
    return rt.log.append("alert", text=redact(body.text), title=body.title, source=body.source, buttons=rows)


@app.post("/channels/{channel}/feed")
def feed(channel: str, body: Feed) -> dict:
    rt = _rt(channel)
    if rt.ch.task_id is None:
        raise HTTPException(400, f"{channel!r} is not a task chat")
    extra = {k: v for k, v in (("name", body.name), ("author", body.author)) if v}
    return rt.log.append(body.type, text=redact(body.text), **extra)


@app.post("/channels/{channel}/command")
def command(channel: str, body: Command) -> dict:
    """A command for the program behind a service channel (it polls them), or
    the record of a button tap in a lead channel (Freigeben / Parken — the web
    app already ran the action; this keeps it in the transcript)."""
    rt = _rt(channel)
    if not (rt.ch.service or rt.ch.lead):
        raise HTTPException(400, f"{channel!r} is not a service or lead channel")
    ev = rt.log.append("command", text=body.text, label=body.label, button_seq=body.button_seq)
    if rt.ch.lead and body.label and body.text != "replaced" and not body.text.startswith("archive:"):
        # The user's decision, for the lead's and the scouts' next runs.
        card = next((e for e in chatlog.iter_tail(rt.ch.id) if e.get("seq") == body.button_seq), {}) \
            if body.button_seq else {}
        try:
            decisions.record(projects.get(rt.ch.project).control_dir,
                             f"{card.get('title') or rt.ch.label}: {body.label}", channel=rt.ch.id, data=body.text)
        except Exception:
            log.warning("could not record decision in %s", rt.ch.id, exc_info=True)
    if rt.ch.lead and body.button_seq:
        # The card itself is done — kept even when the tap bubble is deleted
        # ("resolved" events are not deletable), so its buttons stay disabled.
        rt.log.append("resolved", req_id=f"card:{body.button_seq}", outcome=body.label or body.text,
                      data=body.text)
    return ev


# ---- Claude login (shared by every agent on the host) -----------------------
# The login volume is shared by every agent container; when it
# expires everything stops. The web app shows the status and runs the browser
# login through these endpoints (URL out, code in — nothing else leaves).

AUTH_DIR = chat_root() / "_auth"
_auth_task: Optional[asyncio.Task] = None
_status_cache: tuple[float, dict] = (0.0, {})
_LOGIN_ACTIVE = {"starting", "awaiting_code", "verifying"}


async def claude_status(max_age: float = 60.0) -> dict:
    global _status_cache
    at, cached = _status_cache
    if cached and time.monotonic() - at < max_age:
        return cached
    proc = await asyncio.create_subprocess_exec(
        "claude", "auth", "status", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
        raw = json.loads(out or b"{}")
        st = {"logged_in": raw.get("loggedIn") is True, "method": raw.get("authMethod"),
              "email": raw.get("email")}
    except (asyncio.TimeoutError, ValueError):
        if proc.returncode is None:
            proc.kill()
        st = {"logged_in": None, "error": "status unavailable"}
    _status_cache = (time.monotonic(), st)
    return st


def _login_state() -> dict:
    try:
        state = json.loads((AUTH_DIR / "claude-login.result.json").read_text())
    except (OSError, ValueError):
        return {"status": "idle", "url": None}
    if state.get("status") in _LOGIN_ACTIVE and time.time() >= state.get("expires_at", 0):
        return {"id": state.get("id"), "status": "expired", "url": None,
                "message": "Anmeldelink abgelaufen. Bitte erneut starten."}
    return state


@app.get("/auth/claude")
async def auth_status(fresh: bool = False) -> dict:
    return {**await claude_status(0 if fresh else 60), "login": _login_state()}


@app.post("/auth/claude")
async def auth_start() -> dict:
    global _auth_task, _status_cache
    state = _login_state()
    if _auth_task is not None and not _auth_task.done() and state.get("status") in _LOGIN_ACTIVE:
        return state
    request_id = uuid.uuid4().hex
    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    _status_cache = (0.0, {})

    async def run():
        global _status_cache
        await claude_login.login(AUTH_DIR, request_id)
        _status_cache = (0.0, {})
        if _login_state().get("status") == "success":
            # Sessions opened with the old token keep failing: reopen them lazily.
            for rt in runtimes.values():
                if not rt.lock.locked():
                    await rt.drop_client(keep_session=True)

    _auth_task = asyncio.create_task(run())
    for _ in range(40):  # wait for the URL (or a failure) to show up
        await asyncio.sleep(0.5)
        st = _login_state()
        if st.get("id") == request_id and st.get("status") != "starting":
            return st
    return _login_state()


class LoginCode(BaseModel):
    id: str = Field(..., pattern=r"^[a-f0-9]{32}$")
    code: str = Field(..., min_length=1, max_length=2048, pattern=r"^[A-Za-z0-9_#=./+:-]+$", repr=False)


@app.post("/auth/claude/code")
def auth_code(body: LoginCode) -> dict:
    st = _login_state()
    if st.get("id") != body.id or st.get("status") != "awaiting_code":
        raise HTTPException(409, "Diese Anmeldung wartet nicht auf einen Code. Bitte erneut starten.")
    claude_login.write_private(AUTH_DIR / "claude-login.code", {"id": body.id, "code": body.code})
    return {"ok": True}


@app.post("/auth/claude/cancel")
def auth_cancel() -> dict:
    st = _login_state()
    if st.get("status") in _LOGIN_ACTIVE and st.get("id"):
        claude_login.write_private(AUTH_DIR / "claude-login.cancel", {"id": st["id"]})
    return {"ok": True}


# ---- model catalog (what the installed CLIs can run) -------------------------
# base/model_catalog.py (in the image under /app) asks both
# CLIs; it takes a few seconds, so cache it for an hour.

_catalog_cache: tuple[float, dict] = (0.0, {})


def _build_catalog() -> dict:
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    import model_catalog  # noqa: PLC0415 — base-image module

    out = {}
    for backend, fn in (("claude", model_catalog.claude_catalog), ("codex", model_catalog.codex_catalog)):
        try:
            out[backend] = fn()
        except Exception as e:  # noqa: BLE001 — informational only
            out[backend] = {"models": [], "error": str(e)}
    return out


@app.get("/models")
async def models(refresh: bool = False) -> dict:
    global _catalog_cache
    at, cached = _catalog_cache
    if cached and not refresh and time.monotonic() - at < 3600:
        return cached
    cat = await asyncio.to_thread(_build_catalog)
    _catalog_cache = (time.monotonic(), cat)
    return cat


# ---- usage probe (budget pacer) -----------------------------------------------
# When no chat session reported usage for a while, ask the CLI once: a
# one-token haiku call from an empty directory (no project context to load)
# carries the same rate_limit_event every run does.

async def probe_usage() -> Optional[dict]:
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", "ok", "--model", "haiku", "--output-format", "stream-json", "--verbose",
        cwd="/tmp", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 120)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    sample = None
    for line in out.decode(errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "rate_limit_event":
            sample = budget.record(ev.get("rate_limit_info") or {}, "probe") or sample
    return sample


async def _probe_loop() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            if budget.probe_due():
                s = await probe_usage()
                log.info("usage probe: %s", {k: (s or {}).get(k) for k in ("five_hour", "seven_day")})
            budget.prune()
        except Exception:
            log.exception("usage probe failed")
        await asyncio.sleep(300)


@app.post("/budget/probe")
async def budget_probe() -> dict:
    return {"sample": await probe_usage()}

"""agent-team web/API entry point.

Two ways in:

* **Tunnel** (``agent-team`` → this container): every request except
  ``GET /healthz`` must carry a valid Cloudflare Access JWT for the
  ``agent-team`` application; without configuration the app fails closed.
  ``/healthz`` is exempted by the Access app ``agent-team-healthz`` (Bypass)
  so an uptime monitor can reach it — it must stay content-free.
* **Local mode** (``AT_LOCAL=1``, work laptop without Docker, started by
  ``agent_team.local``): no tunnel, no JWT. Two loopback ports: the browser
  port is the user, ``AT_INTERNAL_PORT`` is for workers, scouts and the chat
  agents (same path rules as the internal network). Like the Docker setup's
  network split it stops honest mistakes, not a hostile local process.
* **Internal network** ``agent-team-internal`` (``AT_INTERNAL_NET``): the
  project workers, scouts and the bridge. They call the per-project tasks API
  (``/p/<project>/api/...``) without a JWT. Those containers hold the Docker
  socket anyway, so trusting them adds nothing. Anything from that subnet that
  looks tunnelled (Cloudflare headers) is still treated as tunnel traffic.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import time
from pathlib import Path

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from contextlib import asynccontextmanager

from . import budget, inbox, projects, push
from .chat import web as chat_web
from .chat import service as chat_service
from .tasks import db as tasks_db
from .tasks import mirror as task_mirror
from .tasks import router as tasks_router

log = logging.getLogger("agent_team")
# Own log lines (inbox pushes etc.) at INFO, `ts | LEVEL | msg` like the
# other services; uvicorn keeps its own handlers.
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

ACCESS_TEAM = os.environ.get("AT_ACCESS_TEAM", "").strip()  # e.g. <team>.cloudflareaccess.com
ACCESS_AUD = os.environ.get("AT_ACCESS_AUD", "").strip()    # Application Audience (AUD) tag
_jwks = (
    jwt.PyJWKClient(f"https://{ACCESS_TEAM}/cdn-cgi/access/certs", lifespan=3600)
    if ACCESS_TEAM and ACCESS_AUD
    else None
)

PUBLIC_PATHS = frozenset({"/healthz"})

LOCAL = os.environ.get("AT_LOCAL", "") == "1"
INTERNAL_PORT = int(os.environ.get("AT_INTERNAL_PORT") or 0)

# Subnet of agent-team-internal (workers/bridge/scouts). Empty = no internal
# access at all.
_internal_raw = os.environ.get("AT_INTERNAL_NET", "").strip()
INTERNAL_NET = ipaddress.ip_network(_internal_raw) if _internal_raw else None
INTERNAL_PATH_RE = re.compile(r"^/p/[a-z][a-z0-9-]{1,31}/api/")
# Subnet of agent-team-notify: services that only post alerts (some of them
# internet-facing, e.g. a monitoring app) — notify endpoint only, so they
# cannot touch the tasks API the workers act on.
_notify_raw = os.environ.get("AT_NOTIFY_NET", "").strip()
NOTIFY_NET = ipaddress.ip_network(_notify_raw) if _notify_raw else None
NOTIFY_PATH_RE = re.compile(r"^/p/[a-z][a-z0-9-]{1,31}/api/notify/[a-z][a-z0-9-]{1,47}$")
# ... and programs behind service channels (chat/service.py) send, poll and
# set their command menu; the routes refuse non-service channels.
SERVICE_PATH_RE = re.compile(r"^/p/[a-z][a-z0-9-]{1,31}/api/service/[a-z][a-z0-9-]{1,47}/(send|commands|menu)$")
# Internal callers (workers, services) may also post alerts:
# /p/<project>/api/notify/<channel> — covered by INTERNAL_PATH_RE.


class _QuietWorkerPolls(logging.Filter):
    """Drop uvicorn access lines for successful internal GETs: the workers
    poll /p/<id>/api/tasks?status=… every ~2 s and would bury everything else."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            _client, method, path, _ver, status = args
            if method == "GET" and status == 200 and INTERNAL_PATH_RE.match(str(path)):
                return False
        return True


logging.getLogger("uvicorn.access").addFilter(_QuietWorkerPolls())

STATIC = Path(__file__).parent / "static"
# Built SPA (frontend/ -> vite build), copied in by the Dockerfile.
WEB_DIST = Path(os.environ.get("AT_WEB_DIST", "/app/web"))


def _budget_gate() -> tuple[bool, str]:
    d = budget.decision(for_tasks=True)
    return d.allowed, d.reason


async def resume_loop() -> None:
    """Start autopilot stages the budget gate parked, once budget is free —
    one task per project per round so a backlog does not burst."""
    while True:
        try:
            if (await asyncio.to_thread(budget.decision, True)).allowed:
                for p in projects.all_projects():
                    if not p.tasks:
                        continue
                    with projects.use(p):
                        paused = await asyncio.to_thread(tasks_db.budget_paused_tasks)
                        # Waits whose time has not come stay parked; try the
                        # first one that may go (one start per project/round).
                        for cand in paused:
                            t = await asyncio.to_thread(tasks_db.resume_budget_paused, cand["id"])
                            if t and t.get("status") == "running":
                                log.info("resumed %s#%s (%s)", p.id, cand["id"], cand.get("paused_reason"))
                                break
        except Exception as e:
            log.warning("budget resume loop: %s", e)
        await asyncio.sleep(300)


async def mirror_loop() -> None:
    """Task chats: post pipeline state changes (tasks/mirror.py)."""
    while True:
        try:
            await asyncio.to_thread(task_mirror.tick)
        except Exception as e:
            log.warning("task mirror: %s", e)
        await asyncio.sleep(4)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    tasks_db.budget_gate = _budget_gate
    loops = [asyncio.create_task(resume_loop()), asyncio.create_task(mirror_loop())]
    if os.environ.get("AT_INBOX_PUSH", "1") == "1":
        loops.append(asyncio.create_task(inbox.notify_loop()))
    yield
    for t in loops:
        t.cancel()


app = FastAPI(title="agent-team", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
if (WEB_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")


def access_identity(request: Request) -> str | None:
    """E-mail from a valid Access JWT (signature, audience, issuer, expiry), else None."""
    token = request.headers.get("cf-access-jwt-assertion")
    if not (_jwks and token):
        return None
    try:
        key = _jwks.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=["RS256"], audience=ACCESS_AUD, issuer=f"https://{ACCESS_TEAM}"
        )
    except Exception as e:  # bad token or certs unreachable
        log.warning("Cloudflare Access token rejected: %s", e)
        return None
    return claims.get("email") or claims.get("sub") or "unknown"


def _from_net(request: Request, net) -> bool:
    """Request from ``net`` that did not come through Cloudflare."""
    if net is None or request.client is None:
        return False
    if request.headers.get("cf-ray") or request.headers.get("cf-connecting-ip"):
        return False
    try:
        return ipaddress.ip_address(request.client.host) in net
    except ValueError:
        return False


def _loopback(request: Request) -> bool:
    try:
        return request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def _local_port(request: Request) -> int:
    server = request.scope.get("server") or (None, 0)
    return int(server[1] or 0)


def _internal_client(request: Request) -> bool:
    if LOCAL:
        return bool(INTERNAL_PORT) and _local_port(request) == INTERNAL_PORT and _loopback(request)
    return _from_net(request, INTERNAL_NET)


def _notify_client(request: Request) -> bool:
    return _from_net(request, NOTIFY_NET)


@app.middleware("http")
async def access_gate(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS and request.method in ("GET", "HEAD"):
        return await call_next(request)
    if _internal_client(request):
        if not INTERNAL_PATH_RE.match(path):
            return PlainTextResponse("forbidden", status_code=403)
        request.state.user = "internal"
        return await call_next(request)
    if _notify_client(request):
        if not ((request.method == "POST" and NOTIFY_PATH_RE.match(path)) or SERVICE_PATH_RE.match(path)):
            return PlainTextResponse("forbidden", status_code=403)
        request.state.user = "notify"
        return await call_next(request)
    if LOCAL:
        if not _loopback(request):
            return PlainTextResponse("forbidden", status_code=403)
        request.state.user = "local"
        return await call_next(request)
    if _jwks is None:
        return PlainTextResponse("access not configured", status_code=503)
    who = access_identity(request)
    if who is None:
        return PlainTextResponse("forbidden", status_code=403)
    request.state.user = who
    return await call_next(request)


@app.get("/healthz")
def healthz() -> PlainTextResponse:
    return PlainTextResponse("ok")


@app.get("/api/me")
def me(request: Request) -> JSONResponse:
    return JSONResponse({"user": request.state.user})


@app.get("/api/version")
def version() -> JSONResponse:
    """Build id of the served frontend (hash of index.html, which names the
    hashed bundle). An open tab compares it with its own and reloads — an old
    bundle silently ignores buttons it doesn't know (user 2026-10-29)."""
    import hashlib
    try:
        build = hashlib.sha1((WEB_DIST / "index.html").read_bytes()).hexdigest()[:12]
    except OSError:
        build = ""
    return JSONResponse({"build": build}, headers={"cache-control": "no-store"})


@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker() -> FileResponse:
    # Served from the root so its scope covers the whole app.
    return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"cache-control": "no-cache"})


@app.get("/api/push/key")
def push_key() -> dict:
    return {"key": push.public_key()}


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request) -> dict:
    try:
        n = push.subscribe(await request.json(), request.state.user, request.headers.get("user-agent", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"subscriptions": n}


@app.post("/api/push/test")
async def push_test(request: Request) -> dict:
    delay = min(max(int((await request.json()).get("delay", 0)), 0), 60)

    async def _send() -> list[dict]:
        await asyncio.sleep(delay)
        return await asyncio.to_thread(
            push.send_all, "agent-team", f"Test-Push ({delay} s verzögert) — es funktioniert.", "/"
        )

    if delay:
        asyncio.get_running_loop().create_task(_send())
        return {"scheduled_in_s": delay}
    return {"results": await _send()}


# ---- Per-project tasks API ---------------------------------------------------

async def _project_scope(project: str, request: Request) -> projects.Project:
    """Path dependency: set the current project for this request. Async on
    purpose — a sync dependency runs in a worker thread and its ContextVar
    would not reach the endpoint."""
    try:
        p = projects.get(project)
    except projects.UnknownProject:
        raise HTTPException(404, f"unknown project {project!r}")
    if not p.tasks:
        raise HTTPException(404, f"project {project!r} has no tasks backend")
    projects.set_current(p)
    if getattr(request.state, "user", None) == "internal":
        tasks_router.mark_worker_seen(p.id)
    return p


app.include_router(
    tasks_router.router,
    prefix="/p/{project}/api/tasks",
    dependencies=[Depends(_project_scope)],
)


app.include_router(chat_web.router)
app.include_router(chat_service.router)


@app.get("/api/projects")
def list_projects() -> list[dict]:
    return [{"id": p.id, "name": p.name, "worker": p.worker, "tasks": p.tasks,
             "channels": [{"id": c.id, "label": c.label} for c in p.channels]}
            for p in projects.all_projects()]


@app.get("/api/budget")
def get_budget() -> dict:
    return {**budget.summary(), "history": budget.samples()}


@app.post("/api/budget/config")
async def set_budget_config(request: Request) -> dict:
    body = await request.json()
    cfg = budget.Config.load()
    for k, v in (body or {}).items():
        if k in budget.Config.__dataclass_fields__:
            setattr(cfg, k, type(getattr(cfg, k))(v))
    cfg.save()
    return budget.summary()


@app.post("/api/budget/probe")
async def budget_probe() -> dict:
    """Measure now (one tiny haiku call in agent-team-chat)."""
    await chat_web._forward("POST", "/budget/probe")
    return budget.summary()


def _team_scouts(project: str) -> list[dict]:
    import yaml
    try:
        p = projects.get(project)
    except projects.UnknownProject:
        raise HTTPException(404, f"unknown project {project!r}")
    try:
        team = yaml.safe_load((p.team_dir / "team.yaml").read_text()) or {}
    except (OSError, ValueError):
        return []
    return [s for s in team.get("scouts") or [] if s.get("id")]


@app.get("/p/{project}/api/scouts")
def list_scouts(project: str) -> list[dict]:
    """The project's scheduled runs (scouts, lead, weekly report) and their state."""
    control = projects.get(project).control_dir / "scouts"
    out = []
    for s in _team_scouts(project):
        sid = str(s["id"])
        try:
            state = json.loads((control / f"{sid}.json").read_text())
        except (OSError, ValueError):
            state = {}
        out.append({"id": sid, "role": s.get("role"), "lead": bool(s.get("lead")), "report": bool(s.get("report")),
                    "every_hours": s.get("every_hours"), "last_run": state.get("last_run"),
                    "last_error": state.get("last_error"), "requested": (control / f"{sid}.run").exists()})
    return out


@app.post("/p/{project}/api/scouts/{sid}/run")
def run_scout_now(project: str, sid: str, request: Request) -> dict:
    """"Jetzt laufen lassen": the project's worker starts the run on its next
    tick, regardless of schedule and pacer (a manual pause still holds it)."""
    if getattr(request.state, "user", None) in ("internal", "notify"):
        raise HTTPException(403, "only from the app")
    if sid not in {str(s["id"]) for s in _team_scouts(project)}:
        raise HTTPException(404, f"no scheduled run {sid!r} in {project}")
    control = projects.get(project).control_dir / "scouts"
    control.mkdir(parents=True, exist_ok=True)
    (control / f"{sid}.run").write_text(str(time.time()))
    return {"requested": True}


def _watchdog_channels(project: str) -> list[projects.Channel]:
    try:
        p = projects.get(project)
    except projects.UnknownProject:
        raise HTTPException(404, f"unknown project {project!r}")
    return [c for c in p.channels if c.id.endswith("-watchdog")]


@app.get("/p/{project}/api/watchdog")
def watchdog_alerts(project: str, limit: int = 150) -> list[dict]:
    """The project's visible (not deleted) watchdog alerts, newest last —
    for the lead run, which proposes a fix or deleting them."""
    from .chat import log as chatlog
    checks = {c.check_of or "" for c in projects.check_channels()}
    out = []
    for ch in _watchdog_channels(project):
        for ev in chatlog.iter_tail(ch.id, max_bytes=5_000_000):
            if ev.get("type") != "alert":
                continue
            out.append({"channel": ch.id, "seq": ev["seq"], "ts": ev.get("ts"), "source": ev.get("source"),
                        "title": ev.get("title"), "text": str(ev.get("text") or "")[:600],
                        "checked": f"{ch.id}:{ev['seq']}" in checks})
    out.sort(key=lambda e: e.get("ts") or "")
    return out[-max(1, min(limit, 500)):]


@app.post("/p/{project}/api/watchdog/dismiss/{key}")
def watchdog_dismiss(project: str, key: str, request: Request) -> dict:
    """The user's tap on the lead's "Watchdog-Meldungen löschen" card: hides
    the alerts the lead listed (stored by the worker under
    <control>/watchdog/<key>.json). Agents cannot delete alerts."""
    from .chat import log as chatlog
    if getattr(request.state, "user", None) in ("internal", "notify"):
        raise HTTPException(403, "only from the app")
    if not re.fullmatch(r"[a-z0-9-]{1,40}", key):
        raise HTTPException(400, "bad key")
    f = projects.get(project).control_dir / "watchdog" / f"{key}.json"
    try:
        spec = json.loads(f.read_text())
    except (OSError, ValueError):
        raise HTTPException(404, "no such proposal")
    allowed = {c.id for c in _watchdog_channels(project)}
    deleted = already = 0
    for ch, seqs in (spec.get("alerts") or {}).items():
        if ch in allowed:
            alerts = {e["seq"] for e in chatlog.iter_tail(ch, max_bytes=5_000_000) if e.get("type") == "alert"}
            gone = chatlog.deleted_seqs(ch)
            deleted += len(chatlog.delete(ch, [int(x) for x in seqs if int(x) in alerts]))
            already += sum(1 for x in seqs if int(x) in gone)   # removed by hand before the tap
    spec["done"] = time.time()
    f.write_text(json.dumps(spec, ensure_ascii=False))
    return {"deleted": deleted, "already": already}


@app.get("/p/{project}/api/budget")
def project_budget(project: str) -> dict:
    """For autonomous jobs on the internal network (scouts): may I run?"""
    return budget.decision().as_dict()


@app.get("/api/inbox")
def get_inbox() -> list[dict]:
    return inbox.all_items()


# ---- SPA ---------------------------------------------------------------------
# Registered last: every other GET that is not an API path renders the app
# (client-side routes like /inbox, /p/<id>/tasks).

@app.get("/{path:path}", include_in_schema=False)
def spa(path: str) -> FileResponse:
    if path.startswith("api/") or INTERNAL_PATH_RE.match("/" + path):
        raise HTTPException(404)
    index = WEB_DIST / "index.html"
    if not index.exists():
        raise HTTPException(503, "frontend not built")
    return FileResponse(index, headers={"cache-control": "no-cache"})

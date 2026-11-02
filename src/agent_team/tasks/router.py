"""Tasks API — CRUD + agent triggers + live event stream, per project.

Mounted at
``/p/{project}/api/tasks``: a worker only needs
``TASKS_API_URL=http://agent-team-web:8000/p/<project>`` — its own paths
(``/api/tasks/...``) are unchanged. The agent worker runs in the project's
worker container; this router only flips state and tails the JSONL the worker
streams in via ``/event/{kind}`` (stored under ``<project data>/sessions``).

Differences to the original: no Docker socket (this app is internet-facing),
so worker liveness comes from the worker's own API polls (``worker_seen``);
the control dir is the project's.
"""

import asyncio
import fcntl
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import decisions, projects
from ..log_redaction import redact
from . import agent_roles
from . import db as tasks_db
from . import mirror

router = APIRouter()

# Last time the project's worker called the API (it polls every ~2 s).
# Internal-network calls only — see app.py. In-memory: after a restart the
# worker counts as "not seen" until its next poll.
_worker_seen: dict[str, float] = {}
WORKER_SEEN_WINDOW_S = 60.0


def mark_worker_seen(project_id: str) -> None:
    _worker_seen[project_id] = time.time()


def _worker_running() -> bool:
    seen = _worker_seen.get(projects.current().id)
    return seen is not None and time.time() - seen < WORKER_SEEN_WINDOW_S


def _worker_name() -> str:
    return projects.current().worker or "worker"


class TaskCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    details: str = ""
    prio: int = Field(2, ge=1, le=3)
    repos: list[str] = Field(default_factory=list)
    # Empty string / null = use worker env default (WORKER_REFINE_MODEL /
    # WORKER_EXECUTE_MODEL). Otherwise passed to whichever agent backend
    # the worker resolves from the model ID itself (claude-* -> Claude SDK,
    # gpt-*/o*/codex-* -> Codex CLI) — see _backend_for() in worker.py.
    refine_model: Optional[str] = None
    execute_model: Optional[str] = None
    # Pipeline mode: model for the orchestrator's plan / re-plan runs.
    plan_model: Optional[str] = None


class TaskUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    details: Optional[str] = None
    prio: Optional[int] = Field(None, ge=1, le=3)
    repos: Optional[list[str]] = None
    refine_model: Optional[str] = None
    execute_model: Optional[str] = None
    plan_model: Optional[str] = None


# Kept in sync with worker.py's _backend_for(). Duplicated rather than
# imported because the worker lives in the bridge container and shares no
# filesystem with this one (see tasks_api.py docstring). tests/
# test_tasks_backend.py guards the two copies against drift.
_CLAUDE_ALIASES = {"haiku", "sonnet", "opus", "fable", "default"}
_CODEX_PREFIXES = ("gpt-", "gpt5", "gpt6", "o1", "o3", "o4", "codex")


def resolve_backend(model: Optional[str], env_default: str) -> str:
    """Which agent backend runs `model`. Empty model => the worker's
    TASKS_AGENT_BACKEND. Unrecognised IDs fall back to the env default
    rather than guessing, so a custom WORKER_MODEL still works."""
    m = (model or "").strip().lower()
    if not m:
        return env_default
    if m.startswith("claude-") or m in _CLAUDE_ALIASES:
        return "claude"
    if m.startswith(_CODEX_PREFIXES):
        return "codex"
    return env_default


def _normalise_backend(raw: Optional[str]) -> str:
    b = (raw or "claude").strip().lower()
    return "codex" if b in {"codex", "gpt", "chatgpt"} else "claude"


@router.get("/worker-defaults")
def worker_defaults():
    """Resolve the default model + agent backend for refine/execute from the
    env the worker publishes (control/worker-env.json). The Tasks UI uses this so the
    "Default" entry in the model dropdown shows what it actually resolves
    to (model *and* backend) instead of an opaque "env" label."""
    refine: Optional[str] = None
    execute: Optional[str] = None
    # Orchestrator (pipeline plan runs): WORKER_ORCHESTRATOR_MODEL if set in
    # the worker env, else the role default from the catalog — the same
    # cascade worker.py/pipeline.py applies when the task has no override.
    plan: Optional[str] = agent_roles.ROLE_BY_ID["orchestrator"].default_model
    backend = "claude"
    container_running = False
    error: Optional[str] = None
    try:
        container_running = _worker_running()
        env = _read_control("worker-env.json")
        if env is None:
            raise RuntimeError("worker has not written worker-env.json yet")
        base = (env.get("WORKER_MODEL") or "").strip() or None
        refine = (env.get("WORKER_REFINE_MODEL") or "").strip() or base
        execute = (env.get("WORKER_EXECUTE_MODEL") or "").strip() or base
        plan = (env.get("WORKER_ORCHESTRATOR_MODEL") or "").strip() or plan
        backend = _normalise_backend(env.get("TASKS_AGENT_BACKEND"))
    except Exception as exc:
        error = str(exc)
    return {
        "container": _worker_name(),
        "container_running": container_running,
        "refine": refine,
        "execute": execute,
        # Backend the worker falls back to when a task has no model override.
        "backend": backend,
        # What the env defaults actually resolve to once the model ID is
        # taken into account — WORKER_REFINE_MODEL=gpt-... with
        # TASKS_AGENT_BACKEND=claude still runs on Codex.
        "refine_backend": resolve_backend(refine, backend),
        "execute_backend": resolve_backend(execute, backend),
        "plan": plan,
        "plan_backend": resolve_backend(plan, backend),
        "error": error,
    }


# ---- Model catalog / CLI upgrade -----------------------------------------
# The two agent CLIs live in the worker container. This service has no
# Docker access, so the worker runs model_catalog.py / cli-upgrade.sh itself
# and exchanges files with us in <project data>/control (mounted into the
# worker as TASKS_CONTROL_DIR) — see claude-agent/monitor_control.py.


def _control_dir() -> Path:
    return projects.current().control_dir

_CATALOG_REFRESH_WAIT_S = 30.0
_UPGRADE_WAIT_S = 960.0  # worker kills cli-upgrade.sh after 900 s


def _read_control(name: str) -> Optional[dict[str, Any]]:
    try:
        return json.loads((_control_dir() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _request_control(name: str, payload: dict[str, Any]) -> None:
    _control_dir().mkdir(parents=True, exist_ok=True)
    tmp = _control_dir() / f".{name}.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, _control_dir() / name)


_LOGIN_ACTIVE = {"starting", "awaiting_code", "verifying"}


def _private_control(name: str, payload: dict[str, Any]) -> None:
    _control_dir().mkdir(parents=True, exist_ok=True)
    tmp = _control_dir() / f".{name}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp, _control_dir() / name)


@contextmanager
def _login_lock():
    _control_dir().mkdir(parents=True, exist_ok=True)
    fd = os.open(_control_dir() / "claude-login.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _login_status() -> dict[str, Any]:
    state = _read_control("claude-login.result.json") or {"status": "idle", "url": None}
    if state.get("status") in _LOGIN_ACTIVE and time.time() >= state.get("expires_at", 0):
        return {"id": state.get("id"), "status": "expired", "url": None,
                "message": "Anmeldelink abgelaufen. Bitte erneut starten."}
    return state


@router.get("/auth/claude")
def claude_login_status(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return _login_status()


@router.post("/auth/claude")
def start_claude_login(response: Response):
    """Start browser OAuth in the worker (file exchange, no Docker exec)."""
    response.headers["Cache-Control"] = "no-store"
    if not _worker_running():
        raise HTTPException(409, "Worker läuft nicht. Bitte zuerst den Worker starten.")
    with _login_lock():
        state = _login_status()
        if state.get("status") in _LOGIN_ACTIVE:
            return state
        request_id = uuid.uuid4().hex
        now = time.time()
        state = {"id": request_id, "status": "starting", "url": None,
                 "message": "Claude-Anmeldung wird gestartet …", "expires_at": now + 30}
        _private_control("claude-login.result.json", state)
        _private_control("claude-login.request", {"id": request_id, "at": now})
        return state


class ClaudeLoginCode(BaseModel):
    id: str = Field(..., pattern=r"^[a-f0-9]{32}$")
    code: str = Field(..., min_length=1, max_length=2048,
                      pattern=r"^[A-Za-z0-9_#=./+:-]+$", repr=False)


class ClaudeLoginCancel(BaseModel):
    id: str = Field(..., pattern=r"^[a-f0-9]{32}$")


@router.post("/auth/claude/code")
def submit_claude_login_code(body: ClaudeLoginCode, response: Response):
    response.headers["Cache-Control"] = "no-store"
    with _login_lock():
        state = _login_status()
        if state.get("id") != body.id or state.get("status") != "awaiting_code":
            raise HTTPException(409, "Diese Anmeldung wartet nicht auf einen Code. Bitte erneut starten.")
        if (_control_dir() / "claude-login.code").exists():
            raise HTTPException(409, "Bestätigungscode wird bereits verarbeitet.")
        _private_control("claude-login.code", {"id": body.id, "code": body.code})
    return {"ok": True}


@router.post("/auth/claude/cancel")
def cancel_claude_login(body: ClaudeLoginCancel, response: Response):
    response.headers["Cache-Control"] = "no-store"
    with _login_lock():
        state = _login_status()
        if state.get("id") == body.id and state.get("status") in _LOGIN_ACTIVE:
            _private_control("claude-login.cancel", {"id": body.id})
    return {"ok": True}


def _claude_auth_error(text: str) -> bool:
    text = text.lower()
    return any(phrase in text for phrase in (
        "oauth session expired", "oauth token has expired", "oauth token expired",
        "failed to authenticate", "invalid authentication credentials",
        "claude-anmeldung abgelaufen oder ungültig",
    ))


@router.get("/{task_id}/auth-error")
def task_auth_error(task_id: int):
    """Also diagnose old runs that stored only 'subtype=success' as error."""
    task = tasks_db.get_task(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    if task["status"] != "failed":
        return {"required": False}
    # A login may fail before the SDK emits an init/session event. The
    # worker's explicit message still identifies Claude in that case.
    if "claude-anmeldung abgelaufen oder ungültig" in (task.get("error") or "").lower():
        return {"required": True, "kind": task.get("last_busy_kind")}
    kind = task.get("last_busy_kind")
    backend = task.get("session_backend")
    if not kind or not tasks_db.is_stream_kind(kind):
        return {"required": False}
    error = task.get("error") or ""
    if kind.startswith("stage_"):
        stage = tasks_db.get_stage(task_id, int(kind[6:]))
        if not stage or stage["status"] != "failed":
            return {"required": False}
        backend = stage.get("session_backend")
        error = stage.get("error") or error
    if backend != "claude":
        return {"required": False}
    path = tasks_db.session_dir(task_id) / f"{kind}.jsonl"
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 65536))
            lines = handle.read().decode("utf-8", "replace").splitlines()
        # Check only the most recent attempt, not earlier failed attempts.
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") == "start":
                break
            if event.get("event") in {"text", "error"}:
                error += "\n" + str(event.get("text") or event.get("message") or "")
    except OSError:
        pass
    return {"required": _claude_auth_error(error), "kind": kind}


def _empty_catalog(error: Optional[str]) -> dict[str, Any]:
    side = {"cli_version": None, "latest_version": None, "update_available": False,
            "models": [], "error": None}
    return {
        "generated_at": None,
        "claude": {**side, "aliases": {}, "default_resolved": None},
        "codex": {**side, "fetched_at": None},
        "error": error,
    }


def _catalog_from_file() -> dict[str, Any]:
    payload = _read_control("model-catalog.json")
    if payload is None:
        return _empty_catalog(f"no model catalog from {_worker_name()} yet")
    if "claude" not in payload:
        return _empty_catalog(payload.get("error") or "model catalog incomplete")
    payload.setdefault("error", None)
    return payload


@router.get("/models")
def model_catalog(refresh: bool = False):
    """Selectable models per agent backend, as the installed CLIs report
    them. The Tasks UI builds its model dropdown from this (falling back to
    its static list when `claude.models`/`codex.models` are empty) and
    shows the CLI-update hint when a model is gated behind a newer CLI.
    The worker refreshes it hourly; `?refresh=1` asks it for a fresh one
    and waits up to 30 s."""
    if refresh:
        before = (_read_control("model-catalog.json") or {}).get("written_at")
        _request_control("catalog.request", {"at": time.time()})
        deadline = time.monotonic() + _CATALOG_REFRESH_WAIT_S
        while time.monotonic() < deadline:
            time.sleep(1.0)
            if (_read_control("model-catalog.json") or {}).get("written_at") != before:
                break
    return {**_catalog_from_file(), "cached": not refresh}


@router.post("/models/upgrade")
def upgrade_clis():
    """Have the worker run the bridge's cli-upgrade.sh (npm install @latest of
    both agent CLIs into the container's user prefix — the same thing its
    entrypoint does at start and worker.py repeats daily when idle), then
    return the refreshed catalog. In-flight sessions keep the binary they
    loaded; the next task spawns the new CLI. Blocks for the install (~1 min)."""
    if not _worker_running():
        raise HTTPException(409, f"{_worker_name()} is not running")
    request_id = f"{time.time():.6f}"
    _request_control("cli-upgrade.request", {"id": request_id})
    deadline = time.monotonic() + _UPGRADE_WAIT_S
    while time.monotonic() < deadline:
        time.sleep(1.0)
        result = _read_control("cli-upgrade.result.json")
        if result and result.get("id") == request_id:
            break
    else:
        raise HTTPException(504, f"{_worker_name()} did not answer the CLI upgrade request")
    # The worker writes the catalog right after the result; give it a moment.
    time.sleep(0.5)
    deadline = time.monotonic() + _CATALOG_REFRESH_WAIT_S
    written = (_read_control("model-catalog.json") or {}).get("written_at") or 0
    while written < float(request_id) and time.monotonic() < deadline:
        time.sleep(1.0)
        written = (_read_control("model-catalog.json") or {}).get("written_at") or 0
    lines = [redact(str(ln)) for ln in result.get("log", [])]
    return {"ok": result.get("exit_code") == 0, "exit_code": result.get("exit_code"),
            "log": lines, "catalog": {**_catalog_from_file(), "cached": False}}


@router.get("/roles")
def list_roles():
    """Role catalog for pipeline-mode tasks (UI source of truth), read from
    the project's team.yaml + roles/*.md front-matter."""
    return {
        "roles": agent_roles.catalog(),
        "gate_roles": sorted(agent_roles.GATE_ROLES),
        "live_bot_repos": sorted(agent_roles.LIVE_BOT_REPOS),
        "repos": sorted(agent_roles.KNOWN_REPOS),
        "repo_prefix": agent_roles.REPO_PREFIX,
        "autopilot_levels": list(tasks_db.AUTOPILOT_LEVELS),
        "defaults": {
            "max_rounds": tasks_db.DEFAULT_MAX_ROUNDS,
            "max_stages": tasks_db.DEFAULT_MAX_STAGES,
        },
    }


@router.get("")
def list_tasks(status: Optional[str] = None):
    if status and status not in tasks_db.VALID_STATUS:
        raise HTTPException(400, f"invalid status: {status}")
    return tasks_db.list_tasks(status=status)


@router.get("/board")
def task_board(done_days: int = 7):
    """Kanban board: the open and parked tasks plus those done in the last
    ``done_days`` days, each with its stage progress and (done) whether the branch still
    waits for "Übernehmen"; counts for the folded-away statuses."""
    rows = tasks_db.list_tasks() + tasks_db.list_tasks("backlog") + tasks_db.recent_done(min(max(done_days, 0), 90))
    progress = tasks_db.stage_progress([t["id"] for t in rows])
    confirm = agent_roles.CONFIRM_ROLES
    for t in rows:
        p = progress.get(t["id"])
        cur = (p or {}).get("current")
        if cur:
            cur["label"] = mirror._role_label(cur["role"])
            cur["confirm"] = cur["role"] in confirm
        st = mirror.worktree_state(t["id"]) if t["status"] == "done" else None
        t["board"] = {"stages": p, "pending_apply": bool(st and st.get("pending"))}
    lead = projects.lead_channel(projects.current().id)
    return {"tasks": rows, "counts": tasks_db.status_counts(),
            "lead_channel": lead.id if lead else None}


@router.post("", status_code=201)
def create_task(body: TaskCreate):
    return tasks_db.create_task(
        title=body.title.strip(),
        details=body.details,
        prio=body.prio,
        repos=body.repos,
        refine_model=body.refine_model,
        execute_model=body.execute_model,
        plan_model=body.plan_model,
    )


@router.get("/{task_id}")
def get_task(task_id: int):
    t = tasks_db.get_task(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    return t


@router.patch("/{task_id}")
def update_task(task_id: int, body: TaskUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        t = tasks_db.get_task(task_id)
        if not t:
            raise HTTPException(404, "task not found")
        return t
    try:
        t = tasks_db.update_task(task_id, **fields)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not t:
        existing = tasks_db.get_task(task_id)
        if not existing:
            raise HTTPException(404, "task not found")
        raise HTTPException(409, f"task not editable in status '{existing['status']}'")
    return t


@router.delete("/{task_id}", status_code=204)
def delete_task(task_id: int):
    if not tasks_db.delete_task(task_id):
        existing = tasks_db.get_task(task_id)
        if not existing:
            raise HTTPException(404, "task not found")
        raise HTTPException(409, f"task busy in status '{existing['status']}'")
    return Response(status_code=204)


# ---- Agent triggers -------------------------------------------------------

@router.post("/{task_id}/refine")
def request_refine(task_id: int):
    """Flip status to 'refining'. The project's worker container
    polls the API and picks it up within a couple of seconds. Existing
    refine.jsonl for this task is truncated so the new run starts fresh."""
    existing = tasks_db.get_task(task_id)
    if not existing:
        raise HTTPException(404, "task not found")
    t = tasks_db.request_refine(task_id)
    if not t:
        raise HTTPException(409, f"cannot refine in status '{existing['status']}'")
    try:
        # Truncate so SSE clients don't replay the previous run's events.
        tasks_db.session_jsonl_path(task_id, "refine").write_text("")
    except Exception:
        pass
    return t


@router.post("/{task_id}/reopen")
def reopen_task(task_id: int):
    """Reopen a terminal task (done/failed) — flip back to 'new' so the
    user can edit details and re-refine. refined_spec / execute_summary
    are kept as historical reference until the next cycle overwrites
    them."""
    existing = tasks_db.get_task(task_id)
    if not existing:
        raise HTTPException(404, "task not found")
    t = tasks_db.reopen_task(task_id)
    if not t:
        raise HTTPException(409, f"cannot reopen in status '{existing['status']}'")
    return t


@router.post("/{task_id}/backlog")
def move_to_backlog(task_id: int):
    """Park a task in the backlog — out of the active list but kept (with its
    spec/history) until restored or deleted. Refuses while an agent run is
    live (refining/executing) and from terminal 'done'."""
    existing = tasks_db.get_task(task_id)
    if not existing:
        raise HTTPException(404, "task not found")
    t = tasks_db.move_to_backlog(task_id)
    if not t:
        raise HTTPException(409, f"cannot move to backlog in status '{existing['status']}'")
    return t


class ProposeBody(BaseModel):
    note: str = Field(..., min_length=1, max_length=4000)


class ParkBody(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


@router.post("/{task_id}/propose")
async def propose_task(task_id: int, body: ProposeBody):
    """The lead recommends a task for start: status shows "Zur Freigabe" and
    a card with "Freigeben & starten" / "Parken" lands in the lead channel."""
    existing = tasks_db.get_task(task_id)
    if not existing:
        raise HTTPException(404, "task not found")
    t = tasks_db.propose_task(task_id, redact(body.note))
    if not t:
        raise HTTPException(409, f"cannot propose in status '{existing['status']}' (only new/refined)")
    lead = projects.lead_channel(projects.current().id)
    if lead is not None:
        from ..chat import web as chat_web  # late: chat.web imports this router's package
        try:
            await chat_web._forward("POST", f"/channels/{lead.id}/alert", {
                "title": f"Vorschlag #{task_id}: {t['title']}"[:200],
                "text": t["lead_note"],
                "source": "lead",
                "buttons": [[{"label": "Freigeben & starten", "data": f"start:{task_id}"}],
                            [{"label": "Besprechen", "data": f"discuss:{task_id}"},
                             {"label": "In Backlog parken", "data": f"park:{task_id}"}]],
            })
        except HTTPException:
            pass  # chat service down: the task still shows "Zur Freigabe"
    return t


class MergeBody(BaseModel):
    into: int
    note: str = Field("", max_length=2000)


def _agent_made(task: dict) -> bool:
    d = task.get("details") or ""
    return "(Vorschlag des Scouts" in d or "(Folge-Task von #" in d


@router.post("/{task_id}/merge")
def merge_task(task_id: int, body: MergeBody, request: Request):
    """Fold this idea into task ``into`` (appended to its details) and close it.
    Agents may only merge their own ideas (scout / follow-up); the user's
    tasks are merged by the user's click (lead card "Zusammenlegen")."""
    src = tasks_db.get_task(task_id)
    if not src:
        raise HTTPException(404, "task not found")
    if getattr(request.state, "user", None) == "internal" and not _agent_made(src):
        raise HTTPException(403, "Tasks des Users legt nur der User zusammen — schlage es per Karte vor")
    t = tasks_db.merge_task(task_id, body.into, redact(body.note))
    if not t:
        raise HTTPException(409, f"cannot merge #{task_id} into #{body.into}")
    return t


@router.post("/{task_id}/park")
def park_task(task_id: int, body: ParkBody):
    existing = tasks_db.get_task(task_id)
    if not existing:
        raise HTTPException(404, "task not found")
    t = tasks_db.park_task(task_id, redact(body.reason))
    if not t:
        raise HTTPException(409, f"cannot park in status '{existing['status']}'")
    return t


class CloseBody(BaseModel):
    reason: str = Field("", max_length=2000)


@router.post("/{task_id}/close")
def close_task(task_id: int, request: Request, body: Optional[CloseBody] = None):
    """Close a task that will not be done (obsolete, duplicate, solved
    elsewhere): out of the lists, kept under "Geschlossen", reopenable.
    Agents may only close their own ideas; the user's by the user's click."""
    existing = tasks_db.get_task(task_id)
    if not existing:
        raise HTTPException(404, "task not found")
    if getattr(request.state, "user", None) == "internal" and not _agent_made(existing):
        raise HTTPException(403, "Tasks des Users schließt nur der User — schlage es per Karte vor")
    t = tasks_db.close_task(task_id, redact(body.reason if body else "") or existing.get("lead_note") or "")
    if not t:
        raise HTTPException(409, f"cannot close in status '{existing['status']}'")
    return t


@router.post("/{task_id}/restore")
def restore_from_backlog(task_id: int):
    """Bring a parked (backlog) or closed task back into the active list —
    lands in 'refined' if a spec exists, else 'new'."""
    existing = tasks_db.get_task(task_id)
    if not existing:
        raise HTTPException(404, "task not found")
    t = tasks_db.restore_from_backlog(task_id)
    if not t:
        raise HTTPException(409, f"cannot restore in status '{existing['status']}'")
    return t


@router.get("/{task_id}/changes")
def task_changes(task_id: int):
    """The task's branch (own worktree, see worker/workspace.py): per repo
    commits ahead of the live branch, and the diff. ``state`` is None for a
    task that ran in the live checkout."""
    if not tasks_db.get_task(task_id):
        raise HTTPException(404, "task not found")
    st = mirror.worktree_state(task_id)
    diff = ""
    if st is not None:
        try:
            diff = (projects.current().control_dir / "worktrees" / f"{task_id}.diff").read_text(encoding="utf-8")
        except OSError:
            pass
    return {"state": st, "diff": diff}


@router.post("/{task_id}/apply")
def apply_task_branch(task_id: int, request: Request):
    """"Übernehmen": the worker brings branch task/<id> into the live checkout
    (rebase + fast-forward) on its next poll and reports in the task chat.
    Only the user's tap — agents never merge into the live branch."""
    if getattr(request.state, "user", None) in ("internal", "notify"):
        raise HTTPException(403, "Übernehmen nur per Klick in der App")
    if not tasks_db.get_task(task_id):
        raise HTTPException(404, "task not found")
    if mirror.worktree_state(task_id) is None:
        raise HTTPException(409, "Task hat keinen eigenen Branch")
    d = projects.current().control_dir / "worktrees"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{task_id}.apply").write_text(str(time.time()))
    return {"requested": True}


@router.post("/{task_id}/done")
def mark_task_done(task_id: int):
    """Manually flip a task to 'done' without running the execute agent.

    Refuses while the agent is mid-run (status='refining' or 'executing')
    because the worker callback would race us; the user should let the
    agent finish or wait for the limit_reached/failed terminal state
    before marking done.
    """
    existing = tasks_db.get_task(task_id)
    if not existing:
        raise HTTPException(404, "task not found")
    t = tasks_db.mark_done(task_id)
    if not t:
        raise HTTPException(
            409, f"cannot mark done in status '{existing['status']}'"
        )
    return t


# ---- Worker callbacks (HTTP because the worker container and this one
# do NOT share a filesystem — see base/tasks_api.py) ----

@router.post("/{task_id}/event/{kind}", status_code=204)
def append_event(task_id: int, kind: str, background: BackgroundTasks, event: dict[str, Any] = Body(...)):
    """Append one JSON event to <project data>/sessions/<id>/<kind>.jsonl.
    Called by the worker as the Claude agent stream produces messages.
    The body is stored verbatim apart from credential redaction (an agent
    that cats a .env must not persist its keys) — the SSE endpoint re-emits
    each line as a server-sent event keyed by event.get('event')."""
    if not tasks_db.is_stream_kind(kind):
        raise HTTPException(400, f"unknown stream kind: {kind}")
    if not tasks_db.get_task(task_id):
        raise HTTPException(404, "task not found")
    path = tasks_db.session_jsonl_path(task_id, kind)
    line = redact(json.dumps(event, ensure_ascii=False, default=str)) + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
    background.add_task(mirror.forward_event, projects.current().id, task_id, kind, json.loads(line))
    return Response(status_code=204)


class RefineComplete(BaseModel):
    refined_spec: str = Field(..., min_length=1)


class FailBody(BaseModel):
    error: str = ""


class LimitReachedBody(BaseModel):
    error: str = ""
    kind: Optional[str] = None  # 'refine' or 'execute', used by Retry dispatch


class SessionBody(BaseModel):
    session_id: Optional[str] = None
    # 'claude' | 'codex' — which agent produced session_id. Optional so an
    # older worker that doesn't send it leaves the stored value alone.
    backend: Optional[str] = None


class ReplyBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=10000)


@router.post("/{task_id}/refine/complete")
def refine_complete(task_id: int, body: RefineComplete):
    t = tasks_db.complete_refine(task_id, redact(body.refined_spec))
    if not t:
        existing = tasks_db.get_task(task_id)
        if not existing:
            raise HTTPException(404, "task not found")
        raise HTTPException(409, f"task not in refining (status={existing['status']!r})")
    return t


@router.post("/{task_id}/refine/fail")
def refine_fail(task_id: int, body: FailBody):
    if not tasks_db.get_task(task_id):
        raise HTTPException(404, "task not found")
    t = tasks_db.fail_task(task_id, redact(body.error))
    if not t:
        raise HTTPException(409, "could not mark task failed")
    return t


@router.post("/{task_id}/limit_reached")
def limit_reached(task_id: int, body: LimitReachedBody):
    """Worker callback — Anthropic usage limit hit mid-session. Marks the
    task with a distinct status so the UI can offer a clean Retry; state
    (refined_spec, pending_reply, session_id) is preserved for resume.
    `kind` records which agent was running so the UI dispatches the
    correct retry endpoint."""
    if not tasks_db.get_task(task_id):
        raise HTTPException(404, "task not found")
    t = tasks_db.mark_limit_reached(task_id, redact(body.error), body.kind)
    if not t:
        raise HTTPException(409, "could not mark task limit_reached")
    return t


@router.post("/{task_id}/session", status_code=204)
def set_session(task_id: int, body: SessionBody):
    """Worker callback — persists the agent session id captured from the
    `init` message so the user (and the worker on a reply turn) can resume
    the same conversation. `backend` records which agent produced it: a
    Codex thread id is not resumable by the Claude SDK, so the worker
    starts a fresh turn when the task's model was switched across
    backends in between."""
    if not tasks_db.set_session_id(task_id, body.session_id, body.backend):
        raise HTTPException(404, "task not found")
    return Response(status_code=204)


@router.post("/{task_id}/reply")
def reply_to_task(task_id: int, body: ReplyBody):
    """User chat reply on a refined task — queues `pending_reply` and
    flips status='refining'. The worker recognises the pending_reply,
    resumes the Claude session via session_id, sends the user's text,
    and re-emits an updated refined_spec on completion. Different from
    /refine: refined_spec is preserved (the conversation builds on it),
    no truncation of refine.jsonl."""
    existing = tasks_db.get_task(task_id)
    if not existing:
        raise HTTPException(404, "task not found")
    t = tasks_db.queue_reply(task_id, body.text)
    if not t:
        if existing.get("status") != "refined":
            raise HTTPException(
                409,
                f"cannot reply in status '{existing['status']}' — task must be refined",
            )
        if not (existing.get("session_id") or "").strip():
            raise HTTPException(
                409,
                "task has no chat session — run a fresh Refine first to start one",
            )
        raise HTTPException(409, "could not queue reply")
    return t


# ---- Cost tally --------------------------------------------------------
# Reads the per-task session JSONLs and sums the `cost_usd` from each
# `result` event. Cheap enough to compute on-demand (one task ≈ a few
# KB) but kept out of the list endpoint so /api/tasks stays O(1) DB.

@router.get("/{task_id}/cost")
def get_cost(task_id: int):
    """Sum of cost_usd over every stream of the task: refine/execute (simple
    mode), plan and stage_<n> (pipeline). Every run also reports its tokens
    (input+output from the result usage). A stream that ran but has no $
    figure (Codex without a price table) is listed as unpriced, not as $0."""
    if not tasks_db.get_task(task_id):
        raise HTTPException(404, "task not found")
    total = 0.0
    total_tokens = 0
    runs: list[dict[str, Any]] = []
    unpriced: list[str] = []
    sdir = tasks_db.session_dir(task_id)
    paths = sorted(sdir.glob("*.jsonl")) if sdir.exists() else []
    for path in paths:
        kind = path.stem
        if not tasks_db.is_stream_kind(kind):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            continue
        started = False
        priced = False
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            ev = obj.get("event")
            if ev == "start":
                started = True
            if ev != "result":
                continue
            cost = obj.get("cost_usd")
            if cost is not None:
                priced = True
            total += float(cost or 0.0)
            tokens = tasks_db._usage_tokens(obj.get("usage"))
            total_tokens += tokens
            runs.append({
                "ts": obj.get("ts"),
                "kind": kind,
                "cost_usd": float(cost or 0.0),
                "priced": cost is not None,
                "tokens": tokens or None,
                "backend": obj.get("backend"),
                "duration_ms": obj.get("duration_ms"),
                "num_turns": obj.get("num_turns"),
            })
        if started and not priced:
            unpriced.append(kind)
    runs.sort(key=lambda r: r.get("ts") or "")
    return {
        "total_usd": total,
        "total_tokens": total_tokens,
        "runs_count": len(runs),
        "runs": runs,
        "unpriced_runs": len(unpriced),
        "unpriced_kinds": unpriced,
    }


# ---- SSE stream of agent events ------------------------------------------

_TERMINAL_STATUS = {"refined", "done", "failed", "limit_reached"}


def _terminal_summary(task_id: int, kind: str) -> Optional[dict]:
    """Non-None once the run a stream belongs to is over — the SSE tail
    emits it as the `done` frame and closes shortly after. refine/execute
    key off the task status as before; plan ends when the task leaves
    `planning`; stage_<n> ends when that stage row leaves `running`."""
    t = tasks_db.get_task(task_id)
    if not t:
        return {"status": "deleted"}
    status = t["status"]
    summary: dict[str, Any] = {
        "status": status,
        "refined_spec": t.get("refined_spec"),
        "execute_summary": t.get("execute_summary"),
        "error": t.get("error"),
        "stop_reason": t.get("stop_reason"),
    }
    if kind in ("refine", "execute"):
        over = status in _TERMINAL_STATUS
    elif kind == "plan":
        over = status != "planning"
    else:  # stage_<seq>
        seq = int(kind.split("_", 1)[1])
        stage = tasks_db.get_stage(task_id, seq)
        if stage is None:
            over = status != "running"
        else:
            over = stage["status"] != "running"
            summary["stage"] = {
                k: stage.get(k) for k in (
                    "seq", "role", "status", "verdict", "summary", "questions",
                    "attempt", "cost_usd", "metered", "error",
                )
            }
    return summary if over else None


def _sse(event: str, data: str) -> bytes:
    payload = "\n".join(f"data: {line}" for line in data.splitlines()) or "data: "
    return f"event: {event}\n{payload}\n\n".encode("utf-8")


async def _tail_jsonl(
    request: Request, path: Path, task_id: int, kind: str, project: projects.Project
) -> "asyncio.AsyncIterator[bytes]":
    """Tail <path> and yield SSE frames. Replays existing content first, then
    follows appends. Ends when the task's status enters a terminal state
    *and* the file has no more bytes to read. Sends keep-alive every 15s."""
    projects.set_current(project)
    # Wait briefly for the worker to create the file if the trigger just fired.
    for _ in range(20):
        if path.exists():
            break
        await asyncio.sleep(0.1)

    pos = 0
    buf = b""
    last_keepalive = asyncio.get_event_loop().time()
    terminal_seen_at: float | None = None

    # Emit a first frame so the client knows the stream is alive even if the
    # worker hasn't written anything yet.
    yield _sse("hello", json.dumps({"task_id": task_id, "kind": kind}))

    while True:
        if await request.is_disconnected():
            break

        if path.exists():
            try:
                with path.open("rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
            except FileNotFoundError:
                chunk = b""
            if chunk:
                buf += chunk
                # Each line in the JSONL is one SSE 'event' frame; partial line
                # stays in buf for the next tick.
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line.decode("utf-8", errors="replace"))
                        ev = obj.get("event") or "msg"
                        yield _sse(ev, json.dumps(obj))
                    except Exception:
                        yield _sse("raw", line.decode("utf-8", errors="replace"))

        # Check terminal status (don't hit SQLite every loop — every ~1s).
        now = asyncio.get_event_loop().time()
        if terminal_seen_at is None:
            summary = _terminal_summary(task_id, kind)
            if summary is not None:
                terminal_seen_at = now
                yield _sse("done", json.dumps(summary, default=str))
        else:
            # Hold the stream open briefly after a terminal status flip so
            # any post-completion side-effects from the worker (auto-fill
            # details, repos reconciliation) land before the SSE closes —
            # otherwise the frontend's final poll wouldn't see them.
            if now - terminal_seen_at > 3.0 and not buf:
                break

        if now - last_keepalive > 15:
            yield b": keep-alive\n\n"
            last_keepalive = now

        await asyncio.sleep(0.5)


@router.get("/{task_id}/stream/{kind}")
async def stream_session(task_id: int, kind: str, request: Request):
    if not tasks_db.is_stream_kind(kind):
        raise HTTPException(400, f"unknown stream kind: {kind}")
    t = tasks_db.get_task(task_id)
    if not t:
        raise HTTPException(404, "task not found")
    path = tasks_db.session_jsonl_path(task_id, kind)
    return StreamingResponse(
        _tail_jsonl(request, path, task_id, kind, projects.current()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---- Pipeline mode ---------------------------------------------------------
# Role-based multi-stage runs. State machine + gates live in tasks_db; this
# is HTTP plumbing. User-facing endpoints map a refused transition to 409
# with the reason; worker callbacks carry the stage `attempt` so a callback
# from a run that was stopped / retried in the meantime is rejected.

class StageSpec(BaseModel):
    role: str = Field(..., min_length=1, max_length=64)
    goal: str = Field("", max_length=4000)
    model: Optional[str] = Field(None, max_length=100)


class PlanUpdateBody(BaseModel):
    stages: list[StageSpec] = Field(default_factory=list)
    exit_criteria: Optional[list[str]] = None
    max_rounds: Optional[int] = Field(None, ge=1, le=50)
    max_stages: Optional[int] = Field(None, ge=1, le=100)
    budget_usd: Optional[float] = Field(None, ge=0)


class PlanCompleteBody(BaseModel):
    text: str = Field(..., min_length=1)


class StageCompleteBody(BaseModel):
    attempt: int = Field(..., ge=1)
    output: str = ""
    # Gate numbers the worker computed from the backtest files
    # ({"metrics": {...}, "file": ...} or {"error": ...}).
    evidence: Optional[dict] = None


class StageFailBody(BaseModel):
    attempt: int = Field(..., ge=1)
    error: str = ""


class StageSessionBody(BaseModel):
    attempt: int = Field(..., ge=1)
    session_id: Optional[str] = None
    backend: Optional[str] = None


class AnswerBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=10000)


class AutopilotBody(BaseModel):
    level: str = Field(..., min_length=1, max_length=16)


def _transition(task_id: int, fn, *args, what: str, **kwargs):
    """Run a tasks_db transition and translate its outcomes: None -> 409
    with the current status, TransitionError -> 409 with its message."""
    existing = tasks_db.get_task(task_id)
    if not existing:
        raise HTTPException(404, "task not found")
    try:
        t = fn(task_id, *args, **kwargs)
    except tasks_db.TransitionError as e:
        raise HTTPException(409, str(e))
    if not t:
        raise HTTPException(409, f"cannot {what} in status '{existing['status']}'")
    return t


def _truncate_stream(task_id: int, kind: str) -> None:
    """Fresh run => fresh JSONL, so SSE clients don't replay the old one."""
    try:
        tasks_db.session_jsonl_path(task_id, kind).write_text("")
    except Exception:
        pass


@router.get("/{task_id}/stages")
def list_stages(task_id: int):
    if not tasks_db.get_task(task_id):
        raise HTTPException(404, "task not found")
    return tasks_db.list_stages(task_id)


@router.post("/{task_id}/plan")
def request_plan(task_id: int, request: Request):
    """Switch the task to pipeline mode and queue an orchestrator run. From
    plan_ready/blocked this re-plans the pending tail.

    Tasks v2: starting is the user's click — callers on the internal network
    (workers, chat agents incl. the lead) are refused."""
    if getattr(request.state, "user", None) == "internal":
        raise HTTPException(403, "Starten nur per Klick in der App (Tasks v2) — schlage den Task "
                                 "stattdessen vor: POST /api/tasks/<id>/propose")
    cur = tasks_db.get_task(task_id) or {}
    if cur.get("status") in ("done", "closed"):
        # e.g. a stale "Freigeben & starten" card: say what happened, not "cannot plan in status".
        raise HTTPException(409, f"Task #{task_id} ist schon {'erledigt' if cur['status'] == 'done' else 'geschlossen'}"
                                 " — nichts mehr zu starten.")
    if cur.get("status") in ("planning", "running", "awaiting_input", "refining"):
        raise HTTPException(409, f"Task #{task_id} läuft bereits (Status {cur['status']}).")
    first_start = cur.get("mode") != "pipeline"
    t = _transition(task_id, tasks_db.request_plan, what="plan")
    _truncate_stream(task_id, "plan")
    # The task's own chat starts with the approval (Tasks v2).
    pid = projects.current().id
    if projects.lead_channel(pid) is not None:
        ch = projects.register_task_channel(pid, task_id, t["title"])
        if first_start:
            mirror._post(ch.id, "alert", mirror.intro(t))
    return t


@router.put("/{task_id}/plan")
def update_plan(task_id: int, body: PlanUpdateBody):
    """User edit: replace the pending stages (order, roles, goals, per-stage
    model) and the limits. Completed stages are history and stay put."""
    return _transition(
        task_id, tasks_db.update_plan,
        [s.model_dump() for s in body.stages],
        what="edit plan",
        exit_criteria=body.exit_criteria,
        max_rounds=body.max_rounds,
        max_stages=body.max_stages,
        budget_usd=body.budget_usd,
    )


@router.post("/{task_id}/plan/complete")
def plan_complete(task_id: int, body: PlanCompleteBody):
    """Worker callback with the orchestrator's full final text."""
    existing = tasks_db.get_task(task_id)
    if not existing:
        raise HTTPException(404, "task not found")
    t = tasks_db.complete_plan(task_id, redact(body.text))
    if not t:
        raise HTTPException(409, f"task not in planning (status={existing['status']!r})")
    return t


@router.post("/{task_id}/plan/fail")
def plan_fail(task_id: int, body: FailBody):
    existing = tasks_db.get_task(task_id)
    if not existing:
        raise HTTPException(404, "task not found")
    t = tasks_db.plan_fail(task_id, redact(body.error))
    if not t:
        raise HTTPException(409, f"task not in planning (status={existing['status']!r})")
    return t


@router.post("/{task_id}/stages/next")
def next_stage(task_id: int):
    """Start the first pending stage (or finish / re-plan when none is left)."""
    return _transition(task_id, tasks_db.request_next_stage, what="run the next stage")


@router.post("/{task_id}/stages/{seq}/complete")
def stage_complete(task_id: int, seq: int, body: StageCompleteBody):
    """Worker callback with the stage's full final text. The backend parses
    the `# Stage result` block and applies the gates."""
    existing = tasks_db.get_task(task_id)
    if not existing:
        raise HTTPException(404, "task not found")
    t = tasks_db.complete_stage(task_id, seq, body.attempt, redact(body.output), body.evidence)
    if not t:
        raise HTTPException(
            409,
            f"stage {seq} attempt {body.attempt} is not the running stage "
            f"(task status={existing['status']!r}) — stale callback ignored",
        )
    return t


@router.post("/{task_id}/stages/{seq}/fail")
def stage_fail(task_id: int, seq: int, body: StageFailBody):
    existing = tasks_db.get_task(task_id)
    if not existing:
        raise HTTPException(404, "task not found")
    t = tasks_db.fail_stage(task_id, seq, body.attempt, redact(body.error))
    if not t:
        raise HTTPException(
            409,
            f"stage {seq} attempt {body.attempt} is not the running stage "
            f"(task status={existing['status']!r}) — stale callback ignored",
        )
    return t


@router.post("/{task_id}/stages/{seq}/session", status_code=204)
def stage_session(task_id: int, seq: int, body: StageSessionBody):
    if not tasks_db.get_task(task_id):
        raise HTTPException(404, "task not found")
    if not tasks_db.set_stage_session(task_id, seq, body.attempt, body.session_id, body.backend):
        raise HTTPException(409, "stage/attempt not found")
    return Response(status_code=204)


@router.post("/{task_id}/stages/{seq}/answer")
def stage_answer(task_id: int, seq: int, body: AnswerBody):
    """Answer an awaiting_input stage; it resumes with the answer."""
    t = _transition(task_id, tasks_db.answer_stage, seq, body.text, what="answer")
    decisions.record(projects.current().control_dir, f"#{task_id} Stufe {seq} — Antwort: {body.text}", task_id=task_id)
    return t


class OptionBody(BaseModel):
    index: int = Field(..., ge=0, le=9)


@router.post("/{task_id}/stages/{seq}/answer-option")
def stage_answer_option(task_id: int, seq: int, body: OptionBody):
    """The user tapped one of the answers the stage offered (task chat card)."""
    stage = tasks_db.get_stage(task_id, seq)
    if not stage:
        raise HTTPException(404, "stage not found")
    opts = stage.get("options") or []
    if body.index >= len(opts):
        raise HTTPException(409, "no such option")
    t = _transition(task_id, tasks_db.answer_stage, seq, f"Entscheidung des Users: {opts[body.index]}",
                    what="answer")
    decisions.record(projects.current().control_dir, f"#{task_id} Stufe {seq} — gewählt: {opts[body.index]}",
                     task_id=task_id)
    return t


@router.post("/{task_id}/stages/{seq}/skip")
def stage_skip(task_id: int, seq: int):
    return _transition(task_id, tasks_db.skip_stage, seq, what="skip stage")


@router.post("/{task_id}/stages/{seq}/retry")
def stage_retry(task_id: int, seq: int):
    t = _transition(task_id, tasks_db.retry_stage, seq, what="retry stage")
    _truncate_stream(task_id, f"stage_{seq}")
    return t


@router.post("/{task_id}/autopilot")
def set_autopilot(task_id: int, body: AutopilotBody):
    return _transition(task_id, tasks_db.set_autopilot, body.level, what="set autopilot")


@router.post("/{task_id}/stop")
def stop_pipeline(task_id: int):
    """Mark the pipeline blocked. A live agent cannot be cancelled; its
    callback is rejected because the stage is no longer running."""
    return _transition(task_id, tasks_db.stop_pipeline, what="stop")


@router.post("/{task_id}/resume")
def resume_pipeline(task_id: int):
    return _transition(task_id, tasks_db.resume_pipeline, what="resume")

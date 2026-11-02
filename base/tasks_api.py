"""HTTP client for the agent-team tasks API (``/p/<project>/api/tasks``).

The backend owns the tasks DB; the workers are just HTTP clients (like the React webapp). The container reaches
the backend by name over a shared docker network.

Base URL: TASKS_API_URL from the environment (the worker), overridable per
call site with `using(url)` — the bridge serves several projects in one
process and sets each bot's project URL around its calls.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from json import dumps, loads
from typing import Any, Iterator, Optional

BASE_URL = os.environ.get("TASKS_API_URL", "http://127.0.0.1:8701")
TIMEOUT_S = 5

_base_url: ContextVar[str] = ContextVar("tasks_api_base_url", default=BASE_URL)


@contextmanager
def using(base_url: str) -> Iterator[None]:
    """Route the calls made inside the block to `base_url`."""
    token = _base_url.set(base_url.rstrip("/"))
    try:
        yield
    finally:
        _base_url.reset(token)


class TasksApiError(Exception):
    """Raised on any non-2xx response from the tasks API."""


def _request(method: str, path: str, payload: Optional[dict] = None) -> Any:
    url = f"{_base_url.get()}{path}"
    data = None
    headers = {}
    if payload is not None:
        data = dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = resp.read()
            if resp.status == 204 or not body:
                return None
            return loads(body)
    except urllib.error.HTTPError as e:
        try:
            detail = loads(e.read()).get("detail") or e.reason
        except Exception:
            detail = e.reason
        raise TasksApiError(f"{e.code} {detail}") from e
    except urllib.error.URLError as e:
        raise TasksApiError(f"connection failed: {e.reason}") from e


def list_tasks(status: Optional[str] = None) -> list[dict]:
    qs = f"?status={urllib.parse.quote(status)}" if status else ""
    return _request("GET", f"/api/tasks{qs}") or []


def get_task(task_id: int) -> Optional[dict]:
    try:
        return _request("GET", f"/api/tasks/{task_id}")
    except TasksApiError as e:
        if str(e).startswith("404"):
            return None
        raise


def create_task(title: str, details: str = "", prio: int = 2, repos: Optional[list[str]] = None) -> dict:
    return _request(
        "POST",
        "/api/tasks",
        {"title": title, "details": details, "prio": prio, "repos": repos or []},
    )


def update_task(task_id: int, **fields) -> dict:
    return _request("PATCH", f"/api/tasks/{task_id}", fields)


def delete_task(task_id: int) -> None:
    _request("DELETE", f"/api/tasks/{task_id}")


# ---- Worker-side helpers --------------------------------------------------

def request_refine(task_id: int) -> dict:
    """Flip status to 'refining' (idempotent for the caller — 409 means the
    task is in a state where refine isn't allowed)."""
    return _request("POST", f"/api/tasks/{task_id}/refine")


def append_event(task_id: int, kind: str, event: dict) -> None:
    """Append one JSON event to sessions/<id>/<kind>.jsonl on the monitoring
    container's filesystem. Called per assistant/tool message."""
    _request("POST", f"/api/tasks/{task_id}/event/{kind}", event)


def refine_complete(task_id: int, refined_spec: str) -> dict:
    return _request(
        "POST", f"/api/tasks/{task_id}/refine/complete", {"refined_spec": refined_spec}
    )


def refine_fail(task_id: int, error: str) -> dict:
    return _request("POST", f"/api/tasks/{task_id}/refine/fail", {"error": error})


def mark_limit_reached(task_id: int, error: str, kind: Optional[str] = None) -> dict:
    """Anthropic usage-limit hit. Distinct from failed — task state is
    preserved so a retry after the monthly reset resumes cleanly. `kind`
    ('refine' or 'execute') records which agent was running so the UI's
    Retry dispatches to the correct endpoint."""
    payload: dict = {"error": error}
    if kind:
        payload["kind"] = kind
    return _request("POST", f"/api/tasks/{task_id}/limit_reached", payload)


def reopen_task(task_id: int) -> dict:
    """Flip a done/failed task back to 'new' for a fresh iteration."""
    return _request("POST", f"/api/tasks/{task_id}/reopen")


def set_session_id(
    task_id: int,
    session_id: Optional[str],
    backend: Optional[str] = None,
) -> None:
    """Persist the agent session id on the task row so a later reply can
    resume the same conversation. `backend` ('claude' | 'codex') records
    which agent minted the id — the two id spaces aren't interchangeable,
    so the worker starts a fresh turn instead of resuming when the task's
    model was switched to the other backend in between."""
    payload: dict = {"session_id": session_id}
    if backend:
        payload["backend"] = backend
    _request("POST", f"/api/tasks/{task_id}/session", payload)


def queue_reply(task_id: int, text: str) -> dict:
    """User chat reply — queues `pending_reply` and flips status to
    'refining' so the worker picks it up as a continuation turn."""
    return _request("POST", f"/api/tasks/{task_id}/reply", {"text": text})


# ---- Pipeline mode (role-based multi-stage runs) --------------------------
# The backend owns the state machine; the worker only reports what the
# agent produced. `attempt` travels with every stage callback so a callback
# from a run that was stopped / retried in the meantime is rejected (409).

def list_stages(task_id: int) -> list[dict]:
    return _request("GET", f"/api/tasks/{task_id}/stages") or []


def plan_complete(task_id: int, text: str) -> dict:
    """Orchestrator's full final text; the backend parses `# Plan`."""
    return _request("POST", f"/api/tasks/{task_id}/plan/complete", {"text": text})


def plan_fail(task_id: int, error: str) -> dict:
    return _request("POST", f"/api/tasks/{task_id}/plan/fail", {"error": error})


def stage_complete(task_id: int, seq: int, attempt: int, output: str,
                   evidence: Optional[dict] = None) -> dict:
    """Stage agent's full final text; the backend parses `# Stage result`
    and applies the gates (needs_input / fail / pass / budget ...).
    `evidence`: gate numbers the worker computed from the backtest files."""
    body: dict = {"attempt": attempt, "output": output}
    if evidence is not None:
        body["evidence"] = evidence
    return _request("POST", f"/api/tasks/{task_id}/stages/{seq}/complete", body)


def stage_fail(task_id: int, seq: int, attempt: int, error: str) -> dict:
    return _request(
        "POST", f"/api/tasks/{task_id}/stages/{seq}/fail",
        {"attempt": attempt, "error": error},
    )


def set_stage_session(
    task_id: int, seq: int, attempt: int, session_id: Optional[str], backend: Optional[str],
) -> None:
    payload: dict = {"attempt": attempt, "session_id": session_id}
    if backend:
        payload["backend"] = backend
    _request("POST", f"/api/tasks/{task_id}/stages/{seq}/session", payload)

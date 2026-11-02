"""File exchange with agent-team-web for the Tasks UI's model catalog,
worker defaults, the Claude login and the "upgrade CLIs" button.

The web app has no Docker access, so the worker writes the answers into the
project's control dir (``TASKS_CONTROL_DIR`` = agent-team
data/projects/<id>/control, mounted into both):

    worker-env.json         at start: the env defaults /worker-defaults shows
    model-catalog.json      model_catalog.py output, every CATALOG_EVERY_S and
                            when catalog.request appears (?refresh=1)
    cli-upgrade.request     written by monitoring ({"id": …}) → worker runs
    cli-upgrade.result.json   cli-upgrade.sh and answers {id, exit_code, log}

Polled from worker.poll_loop via tick(); everything fails soft.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from ..chat import claude_login
from ..log_redaction import redact

log = logging.getLogger("tasks-worker")

CONTROL_DIR = Path(os.environ.get("TASKS_CONTROL_DIR", "/control"))
CATALOG_EVERY_S = float(os.environ.get("MODEL_CATALOG_TTL_S", "3600"))
BASE_DIR = os.environ.get("AT_BASE_DIR", "/app")   # base/ (tasks_api, model_catalog, cli-upgrade)
CATALOG_CMD = ["python3", f"{BASE_DIR}/model_catalog.py"]
UPGRADE_CMD = ["bash", os.environ.get("WORKER_CLI_UPGRADE_SCRIPT", f"{BASE_DIR}/cli-upgrade.sh")]
ENV_KEYS = ("WORKER_MODEL", "WORKER_REFINE_MODEL", "WORKER_EXECUTE_MODEL",
            "WORKER_ORCHESTRATOR_MODEL", "TASKS_AGENT_BACKEND")

_catalog_at = -CATALOG_EVERY_S  # first tick refreshes
_busy: asyncio.Task | None = None
_auth_busy: asyncio.Task | None = None


def _write_json(name: str, payload: dict) -> None:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONTROL_DIR / f".{name}.tmp"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, CONTROL_DIR / name)


def write_env() -> None:
    try:
        _write_json("worker-env.json", {k: os.environ.get(k, "") for k in ENV_KEYS})
    except OSError as exc:
        log.warning("monitor control: cannot write worker-env.json: %s", exc)


async def _run(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", f"timed out after {timeout:.0f} s"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def _refresh_catalog() -> None:
    global _catalog_at
    _catalog_at = time.monotonic()
    code, out, err = await _run(CATALOG_CMD, timeout=120)
    try:
        payload = json.loads(out) if code == 0 else None
    except json.JSONDecodeError as exc:
        err, payload = f"output not JSON: {exc}", None
    if payload is None:
        log.warning("monitor control: model_catalog.py exit %s: %s", code, redact(err.strip()[-400:]))
        payload = {"error": redact(f"model_catalog.py exit {code}: {err.strip()[-400:]}")}
    _write_json("model-catalog.json", {**payload, "written_at": time.time()})


async def _upgrade(request_id: str) -> None:
    code, out, err = await _run(UPGRADE_CMD, timeout=900)
    lines = [ln.strip() for ln in redact(err + out).splitlines() if ln.strip()]
    _write_json("cli-upgrade.result.json", {"id": request_id, "exit_code": code, "log": lines[-10:]})
    log.info("monitor control: CLI upgrade %s finished (exit %s)", request_id, code)
    await _refresh_catalog()


async def _job(coro) -> None:
    try:
        await coro
    except Exception as exc:  # noqa: BLE001 — never take the poll loop down
        log.warning("monitor control failed: %r", exc)


def tick() -> None:
    """Start at most one catalog refresh / CLI upgrade in the background."""
    global _busy, _auth_busy
    # Login runs independently: a catalog refresh/upgrade must not block the
    # browser handshake for minutes, nor may it create duplicate logins.
    auth_req = CONTROL_DIR / "claude-login.request"
    try:
        if (_auth_busy is None or _auth_busy.done()) and auth_req.exists():
            request = json.loads(auth_req.read_text(encoding="utf-8"))
            auth_req.unlink()
            if time.time() - float(request.get("at", 0)) < 30:
                _auth_busy = asyncio.create_task(claude_login.login(CONTROL_DIR, str(request["id"])))
    except (OSError, ValueError, KeyError):
        log.warning("monitor control: could not consume Claude login request")
    if _busy is not None and not _busy.done():
        return
    upgrade_req = CONTROL_DIR / "cli-upgrade.request"
    catalog_req = CONTROL_DIR / "catalog.request"
    try:
        if upgrade_req.exists():
            request_id = str(json.loads(upgrade_req.read_text(encoding="utf-8")).get("id", ""))
            upgrade_req.unlink()
            log.info("monitor control: CLI upgrade %s requested from the Tasks UI", request_id)
            _busy = asyncio.create_task(_job(_upgrade(request_id)))
        elif catalog_req.exists() or time.monotonic() - _catalog_at >= CATALOG_EVERY_S:
            catalog_req.unlink(missing_ok=True)
            _busy = asyncio.create_task(_job(_refresh_catalog()))
    except (OSError, ValueError) as exc:
        log.warning("monitor control: %s", exc)

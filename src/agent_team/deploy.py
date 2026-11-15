"""Deploy button: the user redeploys agent-team itself from the web app.

Agents get Docker only through a restricted socket proxy, so they cannot
redeploy their own services (a changed compose file could hand them the raw
socket back). Instead a host-side deployer (not part of this repo) watches a
queue directory that only this web app mounts:

* ``status.json`` (written by the deployer): deployed commit, the branch head,
  commits in between, the last job with its log.
* ``request.json`` (written here on a click): ``{id, by, commit, ts}`` — the
  deployer builds exactly that commit, the one the user saw.

Env ``AT_DEPLOY_DIR`` enables it (unset = the settings page hides the section).
Requests need a real user (Access login or local mode), never the internal or
notify networks.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request



router = APIRouter()
_DIR = os.environ.get("AT_DEPLOY_DIR", "").strip()
DEPLOY_DIR: Optional[Path] = Path(_DIR) if _DIR else None
NOT_A_USER = {"internal", "notify"}


def _read(name: str) -> dict:
    if DEPLOY_DIR is None:
        return {}
    try:
        return json.loads((DEPLOY_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


async def _busy() -> list[dict]:
    """Chats with a running turn and tasks whose worker is busy — a deploy
    restarts the chat service and the workers and cuts them off."""
    from .chat import web as chat_web
    out: list[dict] = []
    try:
        for c in await chat_web.channels():
            live = c.get("live") or {}
            if live.get("busy"):
                out.append({"kind": "chat", "id": c["id"], "label": c.get("label") or c["id"], "project": c["project"]})
            elif (c.get("task") or {}).get("working"):
                out.append({"kind": "task", "id": c["id"], "label": c.get("label") or c["id"], "project": c["project"]})
    except Exception as e:  # chat service down: no info, not an error
        out.append({"kind": "unknown", "id": "-", "label": f"Chat-Dienst nicht erreichbar ({e})", "project": ""})
    return out


@router.get("/api/deploy")
async def deploy_status() -> dict:
    if DEPLOY_DIR is None:
        return {"enabled": False}
    st = _read("status.json")
    req = _read("request.json")
    job = st.get("job") or {}
    queued = bool(req.get("id")) and req.get("id") != job.get("id")
    return {
        "enabled": True,
        "pending": st.get("pending"),
        "job": job or None,
        "queued": req if queued else None,
        "updated": st.get("updated"),
        "error": st.get("error"),
        "services": st.get("services") or [],
        "busy": await _busy(),
    }


@router.post("/api/deploy")
async def deploy_request(request: Request) -> dict:
    if DEPLOY_DIR is None:
        raise HTTPException(404, "deploy not configured")
    user = getattr(request.state, "user", None)
    if not user or user in NOT_A_USER:
        raise HTTPException(403, "only the user can deploy")
    body = await request.json()
    commit = str(body.get("commit") or "")
    st = _read("status.json")
    head = (st.get("pending") or {}).get("head")
    if not head or commit != head:
        raise HTTPException(409, "Stand hat sich geändert — Seite neu laden")
    if (st.get("job") or {}).get("state") == "running":
        raise HTTPException(409, "Ein Deploy läuft schon")
    req = {"id": uuid.uuid4().hex, "by": user, "commit": commit, "ts": time.time()}
    tmp = DEPLOY_DIR / "request.json.tmp"
    tmp.write_text(json.dumps(req), encoding="utf-8")
    os.replace(tmp, DEPLOY_DIR / "request.json")
    return {"queued": req}




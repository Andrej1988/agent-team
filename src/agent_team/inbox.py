"""Inbox — everything across all projects that waits for a human click.

Derived from the task stores (no own table): a task is in the inbox while it
sits in a status only the user resolves. A background loop pushes a
notification when an item appears; which items were already announced is
kept in ``<data>/inbox_seen.json`` so a restart does not re-announce the
whole inbox (the very first run announces nothing).

New tasks ('new', e.g. scout proposals) are not inbox items: they are a
backlog to triage, not a question.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Optional

import time

from . import projects, push
from .chat import log as chatlog
from .tasks import db as tasks_db

log = logging.getLogger("agent_team.inbox")

SEEN_FILE = projects.DATA_ROOT / "inbox_seen.json"
POLL_S = 15.0

# status -> (kind, reason shown in the inbox)
_INBOX_STATUS: dict[str, tuple[str, str]] = {
    "awaiting_input": ("question", "Rückfrage einer Stage"),
    "plan_ready": ("approval", "Wartet auf Freigabe der nächsten Stage"),
    "refined": ("review", "Spec fertig — prüfen und ausführen"),
    "blocked": ("problem", "Pipeline gestoppt"),
    "failed": ("problem", "Fehlgeschlagen"),
    "limit_reached": ("problem", "Nutzungslimit erreicht"),
}


def _detail(task: dict) -> Optional[str]:
    status = task["status"]
    if status in ("blocked",):
        return task.get("stop_reason")
    if status in ("failed", "limit_reached"):
        return task.get("error")
    if status in ("awaiting_input", "plan_ready") and task.get("mode") == "pipeline":
        stages = tasks_db.list_stages(task["id"])
        if status == "awaiting_input":
            for s in stages:
                if s["status"] == "awaiting_input":
                    qs = s.get("questions") or []
                    return f"{s['role']}: " + (" · ".join(qs) if qs else (s.get("summary") or ""))
        nxt = next((s for s in stages if s["status"] == "pending"), None)
        if nxt is not None:
            return f"nächste Stage {nxt['seq']}: {nxt['role']} ({nxt['kind']})"
    return None


def items_for(project: projects.Project) -> list[dict[str, Any]]:
    if not project.tasks:
        return []
    with projects.use(project):
        if not tasks_db.db_path().exists():
            return []
        out = []
        # Started tasks have their own chat (Tasks v2): approvals, questions and
        # stops are answered there, so the item (and its push) opens the chat.
        chats = {c.task_id for c in projects.task_channels() if c.project == project.id}
        for t in tasks_db.list_tasks():
            if t["status"] not in _INBOX_STATUS:
                continue
            # An archived task chat means "I'm done with it" — whichever way it
            # was archived (header button, old app), it stops asking.
            if t["id"] in chats and chatlog.archived(projects.task_channel_id(project.id, t["id"])):
                continue
            if t["status"] == "plan_ready" and (t.get("paused_reason") or ""):
                continue  # autopilot parked it (budget / wait period) — resumes by itself
            kind, reason = _INBOX_STATUS[t["status"]]
            out.append({
                "project": project.id,
                "project_name": project.name,
                "link": (f"/chat/{projects.task_channel_id(project.id, t['id'])}" if t["id"] in chats
                         else f"/p/{project.id}/tasks?task={t['id']}"),
                "key": f"task:{project.id}:{t['id']}:{t['status']}:{t['updated_at']}",
                "task_id": t["id"],
                "title": t["title"],
                "status": t["status"],
                "kind": kind,
                "reason": reason,
                "detail": (_detail(t) or "")[:500] or None,
                "updated_at": t["updated_at"],
                "prio": t["prio"],
            })
        return out


# A channel open in the app within this window counts as "user is reading":
# its replies are not pushed (still listed until read).
CHAT_ACTIVE_S = 120.0


def chat_items(project: projects.Project) -> list[dict[str, Any]]:
    """Per channel: open approvals/questions, unread alerts, and an unread
    agent reply (one item per channel, the latest turn)."""
    out: list[dict[str, Any]] = []
    # Own chats behave like configured channels (replies reach the inbox);
    # task and check chats do not (their tasks / alerts already do).
    for ch in list(project.channels) + [c for c in projects.custom_channels() if c.project == project.id]:
        base = {"project": project.id, "project_name": project.name, "channel": ch.id,
                "link": f"/chat/{ch.id}", "task_id": None, "prio": 2}
        for ev in chatlog.open_requests(ch.id):
            if ev["type"] == "permission":
                detail = f"{ev.get('title') or ''}\n{ev.get('detail') or ''}".strip()
                kind, reason = "approval", "Freigabe im Chat"
            else:
                qs = ev.get("questions") or []
                detail = " · ".join(q.get("question") or "" for q in qs)
                kind, reason = "question", "Frage im Chat"
            out.append({**base, "key": f"chat:{ch.id}:{ev['seq']}", "title": ch.label,
                        "status": ev["type"], "kind": kind, "reason": reason,
                        "detail": detail[:500] or None, "updated_at": ev["ts"]})
        read = chatlog.read_seq(ch.id)
        last_reply = None
        for ev in chatlog.iter_tail(ch.id, max_bytes=500_000):
            if ev["seq"] <= read:
                continue
            if ev["type"] == "alert":
                out.append({**base, "key": f"chat:{ch.id}:{ev['seq']}", "seq": ev["seq"], "title": ch.label,
                            "status": "alert", "kind": "alert",
                            "reason": ev.get("title") or "Meldung",
                            "detail": (ev.get("text") or "")[:500] or None, "updated_at": ev["ts"]})
            elif ev["type"] in ("assistant", "error"):
                last_reply = ev
        if last_reply is not None:
            out.append({**base, "key": f"chat:{ch.id}:reply:{last_reply['seq']}", "title": ch.label,
                        "status": "reply", "kind": "reply", "reason": "Neue Antwort",
                        "detail": (last_reply.get("text") or "")[:500] or None,
                        "updated_at": last_reply["ts"],
                        "quiet": time.time() - chatlog.read_at(ch.id) < CHAT_ACTIVE_S})
    return out


def auth_items() -> list[dict[str, Any]]:
    """Claude logged out (shared login of every agent) -> one item."""
    import httpx

    from .chat.web import CHAT_URL
    try:
        st = httpx.get(f"{CHAT_URL}/auth/claude", timeout=30).json()
    except Exception:
        return []
    if st.get("logged_in") is not False:
        return []
    return [{"project": "agent-team", "project_name": "agent-team", "link": "/settings",
             "key": "auth:claude:logged-out", "task_id": None, "prio": 1,
             "title": "Claude-Anmeldung", "status": "auth", "kind": "problem",
             "reason": "Claude ist abgemeldet — alle Agenten stehen",
             "detail": "In den Einstellungen neu anmelden.", "updated_at": ""}]


def all_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = auth_items()
    for p in projects.all_projects():
        try:
            items.extend(items_for(p))
            items.extend(chat_items(p))
        except Exception as e:  # one broken project must not hide the others
            log.warning("inbox: project %s unreadable: %s", p.id, e)
    # Questions first, then by priority, newest first.
    order = {"question": 0, "approval": 1, "alert": 2, "problem": 3, "reply": 4, "review": 5}
    items.sort(key=lambda i: i["updated_at"], reverse=True)
    items.sort(key=lambda i: (order.get(i["kind"], 9), i["prio"]))  # stable
    return items


def _key(item: dict[str, Any]) -> str:
    # Tasks: a task re-entering the same status later (new question) has a
    # new updated_at -> announced again. Chat: the event seq.
    return item["key"]


def _load_seen() -> Optional[set[str]]:
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except FileNotFoundError:
        return None
    except ValueError:
        return set()


def _save_seen(seen: set[str]) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SEEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(seen)))
    tmp.replace(SEEN_FILE)


def check_and_notify() -> list[dict[str, Any]]:
    """Announce inbox items not seen before. Returns the announced items."""
    items = all_items()
    keys = {_key(i): i for i in items}
    seen = _load_seen()
    if seen is None:  # first run: everything that is there now counts as known
        _save_seen(set(keys))
        return []
    new = [i for k, i in keys.items() if k not in seen]
    for i in new:
        if i.get("quiet"):
            log.info("push %s skipped (channel open)", i["key"])
            continue
        title = f"{i['project_name']}: {i['reason']}"
        body = f"#{i['task_id']} {i['title']}" if i.get("task_id") else i["title"]
        if i.get("detail"):
            body += f"\n{i['detail'][:200]}"
        try:
            results = push.send_all(title, body, i["link"])
            log.info("push %s -> %s", i["key"], [r.get("ok") for r in results])
        except Exception as e:
            log.warning("inbox push failed: %s", e)
    # Keep only keys that are still in the inbox (bounded file).
    _save_seen(set(keys))
    return new


async def notify_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(check_and_notify)
        except Exception as e:  # never let the loop die
            log.warning("inbox loop: %s", e)
        await asyncio.sleep(POLL_S)

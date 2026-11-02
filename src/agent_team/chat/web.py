"""Chat endpoints of the web app.

Reads come straight from the channel transcripts in the shared data dir
(history, SSE tail); commands are forwarded to agent-team-chat on the
internal network. The notify endpoint is how services post alerts into a
channel.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .. import projects
from . import log as chatlog

CHAT_URL = os.environ.get("AT_CHAT_URL", "http://agent-team-chat:8001").rstrip("/")

router = APIRouter()


def _channel(channel: str) -> projects.Channel:
    try:
        return projects.get_channel(channel)
    except projects.UnknownProject:
        raise HTTPException(404, f"unknown channel {channel!r}")


async def _forward(method: str, path: str, body: Optional[dict] = None) -> Any:
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.request(method, f"{CHAT_URL}{path}", json=body)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"chat service unreachable: {e}")
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text
        raise HTTPException(r.status_code, detail or "chat service error")
    return r.json()


# Task statuses in which the task worker has an agent running. A task chat's
# own chat agent is usually idle meanwhile, so `live.busy` alone says nothing.
TASK_WORKING = frozenset({"refining", "planning", "running"})


def _task_state(ch: projects.Channel) -> Optional[dict]:
    if ch.task_id is None:
        return None
    from ..tasks import db as tasks_db  # tasks -> chat.web import cycle
    try:
        with projects.use(ch.project):
            t = tasks_db.get_task(ch.task_id)
    except Exception:
        return None
    if not t:
        return None
    return {"status": t["status"], "working": t["status"] in TASK_WORKING}


def channel_summary(ch: projects.Channel) -> dict:
    read = chatlog.read_seq(ch.id)
    last_seq, unread, last_ts, preview = 0, 0, None, None
    for ev in chatlog.iter_tail(ch.id, max_bytes=300_000):
        last_seq = ev["seq"]
        last_ts = ev.get("ts")
        if ev["type"] in ("assistant", "alert", "user", "error"):
            preview = (ev.get("title") or ev.get("text") or "")[:140]
        if ev["seq"] > read and ev["type"] in ("assistant", "alert", "permission", "question", "error"):
            unread += 1
    return {
        "id": ch.id, "project": ch.project, "label": ch.label, "cwd": ch.cwd, "lead": ch.lead, "task_id": ch.task_id,
        "check_of": ch.check_of, "custom": ch.custom,
        "archived": chatlog.archived(ch.id),
        "last_seq": last_seq, "read_seq": read, "unread": unread, "last_ts": last_ts,
        "preview": preview, "open_requests": len(chatlog.open_requests(ch.id)),
        "task": _task_state(ch),
    }


@router.get("/api/chat/channels")
async def channels() -> list[dict]:
    out = [channel_summary(ch) for ch in projects.all_channels()]
    try:
        live = {i["id"]: i for i in await _forward("GET", "/channels")}
    except HTTPException:
        live = {}
    for c in out:
        c["live"] = live.get(c["id"])
    return out


@router.get("/api/chat/new-options")
def new_chat_options() -> list[dict]:
    """"Neuer Chat": per project the root folder and its repos (team.yaml)."""
    import yaml
    out = []
    for p in projects.all_projects():
        base = projects.project_root(p.id)
        if base is None:
            continue
        repos: list[str] = []
        try:
            repos = [str(r) for r in (yaml.safe_load((p.team_dir / "team.yaml").read_text()) or {}).get("repos") or []]
        except (OSError, ValueError):
            pass
        out.append({"project": p.id, "name": p.name, "root": base.cwd.rsplit("/", 1)[-1], "repos": sorted(repos)})
    return out


@router.post("/api/chat/custom")
async def new_custom_chat(request: Request) -> dict:
    """Open an own chat for a project root or one of its repos; the optional
    first message starts the conversation."""
    body = await request.json()
    pid, repo = str(body.get("project") or ""), str(body.get("repo") or "")
    title = " ".join(str(body.get("title") or "").split())[:60]
    opts = {o["project"]: o for o in new_chat_options()}
    if pid not in opts or not title:
        raise HTTPException(400, "project and title are required")
    if repo and repo not in opts[pid]["repos"]:
        raise HTTPException(400, f"unknown repo {repo!r} in {pid}")
    ch = projects.register_custom_channel(pid, repo, title)
    first = str(body.get("message") or "").strip()
    if first:
        await _forward("POST", f"/channels/{ch.id}/message", {"text": first})
    return channel_summary(ch)


@router.get("/api/chat/{channel}")
async def channel_info(channel: str) -> dict:
    ch = _channel(channel)
    info = channel_summary(ch)
    try:
        info["live"] = await _forward("GET", f"/channels/{channel}")
    except HTTPException:
        info["live"] = None
    return info


@router.get("/api/chat/{channel}/events")
def events(channel: str, after: int = 0, limit: int = 400) -> list[dict]:
    _channel(channel)
    return chatlog.read_events(channel, after=after, limit=min(max(limit, 1), 5000))


@router.get("/api/chat/{channel}/stream")
async def stream(channel: str, request: Request, after: int = 0):
    """SSE: every new transcript event after ``after``; keep-alive every 15 s.
    Deleted events are skipped. Each event carries its seq as SSE id, so an
    automatic reconnect (which reuses the original URL) resumes from
    Last-Event-ID instead of replaying everything since the page loaded."""
    _channel(channel)
    try:
        after = max(after, int(request.headers.get("last-event-id", "")))
    except ValueError:
        pass
    path = chatlog.channel_dir(channel) / "log.jsonl"

    async def gen():
        last = after
        pos = 0
        buf = b""
        idle = 0.0
        yield b"event: hello\ndata: {}\n\n"
        while not await request.is_disconnected():
            if path.exists():
                with path.open("rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                buf += chunk
                hidden = chatlog.deleted_seqs(channel) if chunk else frozenset()
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("seq", 0) > last:
                        last = ev["seq"]
                        if last in hidden:
                            continue
                        yield f"id: {last}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()
                        idle = 0.0
            await asyncio.sleep(0.4)
            idle += 0.4
            if idle >= 15:
                idle = 0.0
                yield b": keep-alive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@router.post("/api/chat/{channel}/read")
async def mark_read(channel: str, request: Request) -> dict:
    _channel(channel)
    seq = int((await request.json()).get("seq") or 0)
    chatlog.mark_read(channel, seq)
    return {"read_seq": chatlog.read_seq(channel)}


CHECK_PROMPT = """Prüfe bitte diese Meldung aus dem Kanal „{label}“ (Projekt {project}, {ts}):

{title}
{text}

Vorgehen:
1. Finde die Ursache: Logs, Code, Konfiguration, Container-Zustand. Nur lesen — nichts ändern, nichts neu starten, nichts deployen.
2. Schätze ein, ob das ein echtes Problem ist, wie dringend, und ob es wiederkommt.
3. Antworte mir hier im Chat: kurz Ursache und Einschätzung, dann eine klare Empfehlung in einer Zeile:
   **Fixen** — was genau zu tun ist und wie aufwendig (ich entscheide dann, ob du es hier fixt oder einen Task anlegst), oder
   **Löschen** — harmlos/erwartet, mit Begründung.
Wichtig: Ich sehe nur deine Chat-Antwort, keine Dateien — die Analyse gehört in die Antwort, nicht (nur) in SESSION.md.
Dann warte auf meine Entscheidung."""


IDEA_PROMPT = """Neue Idee per Link (Knopf „Mit Agent lesen“ in der App). Bitte lies den Link selbst und lege daraus EINE Task-Idee an.

Link: {link}
{shared}{note}
Vorgehen:
1. Inhalt holen. X/Twitter: https://api.fxtwitter.com/<user>/status/<id> liefert Text, Zitat und Bild-URLs als JSON — lade die Bilder und sieh sie dir an (oft steckt dort der eigentliche Inhalt, z. B. ein Paper-Abstract). Sonst die Seite direkt (curl/WebFetch). Kommst du nicht an den Inhalt, sag das und lege die Idee trotzdem mit dem an, was du hast.
2. Kern in 3–6 Stichpunkten; dazu, wo es im Projekt ansetzt (vorhandene Strategien/Module, was schon da ist, was fehlt) und wo es zu einfach oder Hype ist.
3. Anlegen über die Tasks-API (create, Status Idee — NICHT vorschlagen/starten): Titel „Untersuchen: …“ (konkret), Details mit Quelle, Kernaussagen, Auftrag „Zu untersuchen: 1. … 2. … 3. Empfehlung …“, passende Repos (leer, wenn es alle betrifft), Prio 2 (1 nur bei klarem, dringendem Nutzen).
4. Antworte kurz hier: „Idee #<Nummer> angelegt: <Titel>“ plus 2–3 Sätze, warum es (nicht) lohnt."""


@router.post("/api/chat/idea")
async def idea_from_link(request: Request) -> dict:
    """"Mit Agent lesen" on the Idee page: the project's lead reads the link and
    files a task idea; the app opens the lead chat to show the result."""
    body = await request.json()
    try:
        project = projects.get(str(body.get("project") or ""))
    except projects.UnknownProject:
        raise HTTPException(404, "unknown project")
    link = str(body.get("link") or "").strip()
    if not link.startswith(("http://", "https://")):
        raise HTTPException(400, "link must be an http(s) URL")
    lead = projects.lead_channel(project.id)
    if lead is None:
        raise HTTPException(400, f"project {project.id!r} has no lead channel")
    shared = str(body.get("text") or "").strip()
    note = str(body.get("note") or "").strip()
    text = IDEA_PROMPT.format(link=link[:2000],
                              shared=f"Mitgeteilter Text: {shared[:2000]}\n" if shared else "",
                              note=f"Meine Notiz: {note[:2000]}\n" if note else "")
    await _forward("POST", f"/channels/{lead.id}/message", {"text": text})
    return {"channel": lead.id}


@router.post("/api/chat/{channel}/check")
async def check_alert(channel: str, request: Request) -> dict:
    """"Prüfen" on an alert: open (or reuse) a check chat whose agent
    examines the alert and recommends fixing or deleting it."""
    src = _channel(channel)
    if src.service:
        raise HTTPException(400, "service channels have no agent")
    try:
        seq = int((await request.json()).get("seq"))
    except (TypeError, ValueError):
        raise HTTPException(400, "seq must be an integer")
    ev = next((e for e in chatlog.read_events(channel, after=seq - 1, limit=None) if e.get("seq") == seq), None)
    if ev is None or ev.get("type") != "alert":
        raise HTTPException(404, f"no alert {seq} in {channel!r}")
    title = str(ev.get("title") or "Meldung")
    ch = projects.register_check_channel(src, seq, title[:80])
    if not chatlog.read_events(ch.id, limit=1):  # first tap: brief the agent
        text = CHECK_PROMPT.format(label=src.label, project=src.project, ts=ev.get("ts", ""),
                                   title=title, text=str(ev.get("text") or ""))
        await _forward("POST", f"/channels/{ch.id}/message", {"text": text[:50_000]})
    return {"channel": ch.id}


@router.post("/api/chat/{channel}/archive")
async def archive(channel: str, request: Request) -> dict:
    """Hide the channel from the list ({archived: true}) or bring it back."""
    _channel(channel)
    on = bool((await request.json()).get("archived", True))
    chatlog.set_archived(channel, on)
    return {"archived": on}


@router.post("/api/chat/{channel}/delete")
async def delete_events(channel: str, request: Request) -> dict:
    """Hide messages: {seqs: [..]}, {all_alerts: true} and/or {all: true}."""
    _channel(channel)
    body = await request.json()
    try:
        seqs = [int(x) for x in (body.get("seqs") or [])]
    except (TypeError, ValueError):
        raise HTTPException(400, "seqs must be integers")
    return {"deleted": chatlog.delete(channel, seqs, bool(body.get("all_alerts")), bool(body.get("all")))}


@router.post("/api/chat/{channel}/{action}")
async def command(channel: str, action: str, request: Request) -> Any:
    _channel(channel)
    if action not in ("message", "settings", "stop", "reset"):
        raise HTTPException(404)
    body = await request.json() if action in ("message", "settings") else None
    return await _forward("POST", f"/channels/{channel}/{action}", body)


@router.post("/api/chat/{channel}/{kind}/{req_id}")
async def answer(channel: str, kind: str, req_id: str, request: Request) -> Any:
    _channel(channel)
    if kind not in ("permission", "question"):
        raise HTTPException(404)
    return await _forward("POST", f"/channels/{channel}/{kind}/{req_id}", await request.json())


@router.get("/api/chat-models")
async def chat_models(refresh: bool = False) -> Any:
    return await _forward("GET", f"/models{'?refresh=1' if refresh else ''}")


# ---- Claude login (runs in agent-team-chat) -----------------------------------

@router.get("/api/claude-auth")
async def claude_auth_status(fresh: bool = False) -> Any:
    return await _forward("GET", f"/auth/claude{'?fresh=1' if fresh else ''}")


@router.post("/api/claude-auth/{action}")
async def claude_auth(action: str, request: Request) -> Any:
    if action == "start":
        return await _forward("POST", "/auth/claude")
    if action == "code":
        return await _forward("POST", "/auth/claude/code", await request.json())
    if action == "cancel":
        return await _forward("POST", "/auth/claude/cancel")
    raise HTTPException(404)


# ---- alerts from services (internal network) ---------------------------------

@router.post("/p/{project}/api/notify/{channel}")
async def notify(project: str, channel: str, request: Request) -> Any:
    ch = _channel(channel)
    if ch.project != project:
        raise HTTPException(404, f"channel {channel!r} is not in project {project!r}")
    body = await request.json()
    if not isinstance(body, dict) or not str(body.get("text") or "").strip():
        raise HTTPException(400, "body must be {text, title?, source?}")
    out = {"text": str(body["text"]), "title": body.get("title"), "source": body.get("source")}
    if body.get("buttons"):
        out["buttons"] = body["buttons"]  # e.g. the lead's park / merge cards
    return await _forward("POST", f"/channels/{channel}/alert", out)

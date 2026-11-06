"""Task chat mirror (Tasks v2) — a started task's pipeline as a readable chat.

Two feeds into the task chat (``task-<project>-<id>``, see projects.py):

* ``forward_event`` — called for every stream event the worker posts
  (``/event/<kind>``): the stage agent's messages become ``assistant`` events
  with the role as author, its tool calls ``tool_use`` events (the chat folds
  them into a block). Thinking, tool results and the final result block stay
  in the stage log.
* ``tick`` — every few seconds: compares each task's state with the last one
  seen and posts what changed as short alerts: plan, ▶ stage started, ✓/✗
  stage finished, ❓ question, ⏸ waits for a click (button "Nächste Stufe
  starten"), pause, ⛔ stopped, ✅ done.

The chat is written only through agent-team-chat (the transcripts' single
writer); the last-seen state lives in ``<data>/chat/_task_mirror.json``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

from .. import projects
from . import agent_roles
from . import db as tasks_db

log = logging.getLogger("agent_team.mirror")

_RESULT_MARKERS = ("# Stage result", "# Plan", "# Refined spec")
_client: Optional[httpx.Client] = None


def _chat_url() -> str:
    from ..chat.web import CHAT_URL
    return CHAT_URL


def _post(channel: str, path: str, body: dict) -> None:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=15)
    try:
        r = _client.post(f"{_chat_url()}/channels/{channel}/{path}", json=body)
        if r.status_code >= 400:
            log.warning("mirror %s/%s: HTTP %s %s", channel, path, r.status_code, r.text[:200])
    except httpx.HTTPError as e:
        log.warning("mirror %s/%s: %s", channel, path, e)


def _role_label(role: str) -> str:
    r = agent_roles.ROLE_BY_ID.get(role)
    return r.label if r else role


def fmt_tool(name: str, inp: dict) -> str:
    if name in ("Read", "Write", "Edit", "NotebookEdit"):
        detail = inp.get("file_path", "")
    elif name == "Bash":
        detail = inp.get("command", "")
    elif name == "Grep":
        detail = f"{inp.get('pattern', '')} in {inp.get('path', '.')}"
    elif name == "Glob":
        detail = inp.get("pattern", "")
    elif name in ("WebFetch", "WebSearch"):
        detail = inp.get("url") or inp.get("query") or ""
    else:
        detail = str(next(iter(inp.values()), "")) if inp else ""
    detail = " ".join(str(detail).split())
    if len(detail) > 300:
        detail = detail[:300] + "…"
    return f"{name}({detail})" if detail else name


def _strip_result(text: str) -> str:
    for m in _RESULT_MARKERS:
        i = text.find(m)
        if i >= 0:
            text = text[:i]
    return text.strip()


# ---- stream events --------------------------------------------------------------

def forward_event(project_id: str, task_id: int, kind: str, event: dict[str, Any]) -> None:
    """Background task of the /event route (project context is passed in:
    it runs after the response)."""
    channel = projects.task_channel_id(project_id, task_id)
    try:
        projects.get_channel(channel)
    except projects.UnknownProject:
        return  # task started before task chats existed
    with projects.use(project_id):
        if kind == "plan":
            author = "Planung"
        elif kind.startswith("stage_"):
            stage = tasks_db.get_stage(task_id, int(kind[6:]))
            author = f"Stufe {kind[6:]} · {_role_label(stage['role'])}" if stage else kind
        else:
            return
    ev = event.get("event")
    if ev == "text":
        text = _strip_result(str(event.get("text") or ""))
        if text:
            _post(channel, "feed", {"type": "assistant", "text": text[:20_000], "author": author})
    elif ev == "tool_use":
        name = str(event.get("name") or "tool")
        _post(channel, "feed", {"type": "tool_use", "name": name,
                                "text": fmt_tool(name, event.get("input") or {})})


# ---- state changes ----------------------------------------------------------------

def _state_path():
    return projects.DATA_ROOT / "chat" / "_task_mirror.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False))
    tmp.replace(path)


def snapshot(task: dict, stages: list[dict]) -> dict:
    return {"status": task["status"], "paused": task.get("paused_reason") or "",
            "stages": {str(s["seq"]): [s["status"], s.get("verdict") or ""] for s in stages}}


def _short(text: Optional[str], n: int = 700) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def changes(task: dict, stages: list[dict], old: Optional[dict]) -> list[dict]:
    """Alerts (title, text, buttons?) for what changed since ``old``."""
    out: list[dict] = []
    tid = task["id"]
    new = snapshot(task, stages)
    old_stages = (old or {}).get("stages") or {}
    plan_new = stages and not old_stages
    if plan_new:
        lines = [f"{s['seq']}. {_role_label(s['role'])} — {_short(s.get('goal'), 160)}" for s in stages]
        out.append({"title": f"Plan für #{tid}: {len(stages)} Stufen", "text": "\n".join(lines)})
    for s in stages:
        seq = str(s["seq"])
        st, verdict = new["stages"][seq]
        before = old_stages.get(seq, ["pending", ""])
        if [st, verdict] == before:
            continue
        label = f"Stufe {seq} · {_role_label(s['role'])}"
        if st == "running" and before[0] != "running":
            out.append({"title": f"▶ {label}", "text": _short(s.get("goal"), 400) or "läuft"})
        elif st == "done":
            mark = "✓" if verdict in ("pass", "") else "✗"
            out.append({"title": f"{mark} {label} — {verdict or 'fertig'}",
                        "text": _short(s.get("summary")) or "fertig"})
        elif st == "failed":
            out.append({"title": f"✗ {label} fehlgeschlagen", "text": _short(s.get("error")) or "Fehler"})
        elif st == "awaiting_input":
            qs = s.get("questions") or []
            q = "\n".join(f"• {x.get('question') if isinstance(x, dict) else x}" for x in qs) or _short(s.get("summary"))
            opts = [str(o) for o in (s.get("options") or [])][:4]
            card = {"title": f"❓ {label} fragt", "buttons": button_rows(available_actions(task, stages))}
            if opts:
                listed = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(opts))
                card["text"] = f"{q}\n\n{listed}\n\nTipp eine Antwort an — oder schreib hier, der Lead gibt es weiter."
            else:
                card["text"] = f"{q}\n\nAntworte hier — der Lead gibt es weiter."
            out.append(card)
        elif st == "skipped":
            out.append({"title": f"↷ {label} übersprungen", "text": "übersprungen"})
    status, paused = new["status"], new["paused"]
    if old is not None and status == old.get("status") and paused == old.get("paused"):
        return out
    if status == "plan_ready" and not paused:
        nxt = next((s for s in stages if s["status"] == "pending"), None)
        if nxt is not None and nxt["role"] in agent_roles.CONFIRM_ROLES:
            # Deploy / live: say so plainly — this click changes the live system.
            label = _role_label(nxt["role"])
            out.append({"title": f"🔐 Freigabe nötig: Stufe {nxt['seq']} · {label} — ändert das Live-System",
                        "text": (_short(nxt.get("goal"), 900) or label)
                        + ("\n\nVorher spielt der Worker Branch task/%d in den Live-Ordner ein; deployt wird aus "
                           "diesem Stand (ohne Push)." % tid if worktree_state(tid) is not None
                           else "\n\nDanach committet diese Stufe die Änderungen des Tasks (ohne Push)."),
                        "buttons": button_rows(available_actions(task, stages))})
        elif nxt is not None:
            out.append({"title": f"⏸ Wartet auf dich: Stufe {nxt['seq']} · {_role_label(nxt['role'])}",
                        "text": _short(nxt.get("goal"), 400) or "nächste Stufe",
                        "buttons": button_rows(available_actions(task, stages))})
    elif paused and paused != (old or {}).get("paused"):
        out.append({"title": "⏸ Pausiert", "text": paused})
    elif status == "blocked":
        out.append(stuck_card(task, stages, "⛔ Gestoppt", task.get("stop_reason") or "gestoppt"))
    elif status == "done":
        out.append(done_card(task, stages))
    elif status == "failed":
        out.append(stuck_card(task, stages, "✗ Fehler", task.get("error") or "Fehler"))
    elif status == "limit_reached":
        out.append(stuck_card(task, stages, "⏳ Nutzungslimit erreicht", task.get("error") or "Nutzungslimit erreicht"))
    return out


EXPLAIN_PROMPT = """[Automatic message, not typed by the user] Task #{id} just stopped
(status: {status}). Recorded reason: {reason}

Look at the task and its stages (API in your rules; the stage summaries and,
if needed, the stage logs), then tell the user in German, short enough for a
phone, exactly these three points:
1. **Ergebnis:** what the task found or built so far — concrete, with numbers
   if there are any.
2. **Warum gestoppt:** in plain words — a stage could not continue, a limit,
   or only a formal rule (e.g. a research question answered, just negatively,
   or the last stage was not a review). Say whether it is a real problem.
3. **Empfehlung:** one of the buttons on the card above or in the status bar
   of this chat — e.g. „Stufe erneut versuchen“, „Ergebnis übernehmen“, „Neu
   planen lassen“, „Als erledigt abhaken“ or „Schließen“ — and why; for
   re-planning, what should be different.
No English, no further sections. Change nothing (no API writes)."""


def explain_request(task: dict, old: Optional[dict]) -> Optional[dict]:
    """When a task just stopped or failed, the task chat's lead explains in
    German what came out, what is missing and which way out it recommends —
    the stop reason alone is the planner's (often English) technical note
    (user 2026-10-30). Not for the user's own "Stopp"."""
    status = task.get("status")
    if old is None or old.get("status") == status or status not in ("blocked", "failed"):
        return None
    reason = (task.get("stop_reason") if status == "blocked" else task.get("error")) or ""
    if reason.strip() == "stopped by user":
        return None
    return {"text": EXPLAIN_PROMPT.format(id=task["id"], status=status, reason=_short(reason, 1500) or "(none)"),
            "note": f"Lead schaut sich an, warum #{task['id']} {'gestoppt' if status == 'blocked' else 'fehlgeschlagen'} ist …"}


def _action(label: str, data: str, kind: str = "secondary", confirm: Optional[str] = None) -> dict:
    out = {"label": label, "data": data, "kind": kind}
    if confirm:
        out["confirm"] = confirm
    return out


def available_actions(task: dict, stages: list[dict]) -> list[dict]:
    """Everything the user can do with the task right now, most useful first.
    The one list behind the cards, the task chat's status bar and the lead's
    offers, so none of them shows a way out the backend refuses or misses one
    it allows. ``data`` is what a tap sends (Chat.tsx pressButton)."""
    tid, status = task["id"], task["status"]
    if status == "refining":
        return []
    if status in ("planning", "running"):
        return [_action("Task stoppen", f"stop:{tid}", "danger",
                        "Task stoppen? Die laufende Stufe gilt dann als abgebrochen.")]
    if status == "done":
        steps = [b for row in done_card(task, stages)["buttons"] for b in row]
        return [_action(b["label"], b["data"], "primary" if i == 0 and len(steps) > 1 else "secondary")
                for i, b in enumerate(steps)]
    if status in ("closed", "backlog"):
        return [_action("Wieder öffnen", f"restore:{tid}")]
    ending = [_action("Als erledigt abhaken", f"done:{tid}"),
              _action("Schließen – wird nicht gemacht", f"close:{tid}")]
    if status in ("new", "refined") or (not stages and task.get("mode") != "pipeline"):
        return [_action("Freigeben & starten", f"start:{tid}", "primary"),
                _action("Ins Backlog", f"park:{tid}"), *ending]
    replan = [_action("Neu planen lassen", f"replan:{tid}"),
              _action("Neu planen mit Hinweis", f"replannote:{tid}")]
    asking = next((s for s in stages if s["status"] == "awaiting_input"), None)
    if asking is not None and status in ("awaiting_input", "plan_ready"):
        seq = asking["seq"]
        out = [_action(f"{i + 1}. {o}" if len(o) <= 56 else f"{i + 1}. {o[:54]}…", f"opt:{tid}:{seq}:{i}", "primary")
               for i, o in enumerate(str(x) for x in (asking.get("options") or [])[:4])]
        if asking["role"] not in agent_roles.GATE_ROLES:
            out.append(_action(f"Stufe {seq} überspringen", f"skip:{tid}:{seq}"))
        return out + [_action("Task stoppen", f"stop:{tid}", "danger", "Task stoppen?")]
    nxt = next((s for s in stages if s["status"] == "pending"), None)
    if status == "plan_ready":
        out = []
        paused = task.get("paused_reason") or ""
        if not paused:
            confirm = nxt is not None and nxt["role"] in agent_roles.CONFIRM_ROLES
            label = f"{_role_label(nxt['role'])} freigeben" if confirm else "Nächste Stufe starten" if nxt else "Weiter"
            out.append(_action(label, f"next:{tid}", "primary"))
        elif paused.startswith("Budget"):
            out.append(_action("Trotzdem jetzt weiter", f"next:{tid}", "primary"))
        if nxt is not None and nxt["role"] not in agent_roles.GATE_ROLES:
            out.append(_action(f"Stufe {nxt['seq']} überspringen", f"skip:{tid}:{nxt['seq']}"))
        return out + replan + ending
    # blocked / failed / limit_reached
    out = []
    last = next((s for s in reversed(stages) if s["status"] not in ("pending", "skipped")), None)
    if last is not None and tasks_db.stage_retryable(status, last, stages):
        out.append(_action(f"Stufe {last['seq']} erneut versuchen", f"retry:{tid}:{last['seq']}", "primary"))
    if status == "blocked" and nxt is not None and not any(s["status"] in ("failed", "awaiting_input") for s in stages):
        out.append(_action("Weitermachen", f"resume:{tid}", "primary"))
    if (worktree_state(tid) or {}).get("pending"):
        out.append(_action("Ergebnis übernehmen (mergen, ohne Deploy)", f"apply:{tid}", "primary"))
    if last is not None and last["status"] == "failed" and last["role"] not in agent_roles.GATE_ROLES:
        out.append(_action(f"Stufe {last['seq']} überspringen", f"skip:{tid}:{last['seq']}"))
    return out + replan + ending


def button_rows(actions: list[dict]) -> list[list[dict]]:
    """Card buttons: main ways out on their own row, the rest two per row."""
    rows: list[list[dict]] = []
    pair: list[dict] = []
    for a in actions:
        b = {k: a[k] for k in ("label", "data", "confirm") if k in a}
        if a.get("kind") == "primary":
            rows.append([b])
            continue
        pair.append(b)
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    return rows + ([pair] if pair else [])


def stuck_card(task: dict, stages: list[dict], title: str, why: str) -> dict:
    """Stopped / failed task: say why and offer every way out as a button —
    a card without one left the task sitting there (user 2026-10-29)."""
    actions = available_actions(task, stages)
    kinds = {a["data"].split(":")[0]: a for a in actions}
    hints = []
    if "retry" in kinds:
        hints.append(f"{kinds['retry']['label']}: sinnvoll, wenn das Hindernis inzwischen behoben ist.")
    if "apply" in kinds:
        hints.append("Ergebnis übernehmen: holt den Stand des Task-Branches in den Live-Ordner, auch wenn der Task gestoppt ist.")
    hints.append("Neu planen: der Planer sieht den Stand und schlägt die fehlenden Schritte vor. "
                 "Mit Hinweis: du schreibst dazu, was anders laufen soll.")
    hints.append("Oder frag hier im Chat, was los ist.")
    return {"title": title, "text": _short(why, 900) + "\n\nWas jetzt?\n" + "\n".join(f"· {h}" for h in hints),
            "buttons": button_rows(actions)}


def worktree_state(task_id: int) -> Optional[dict]:
    """What the worker wrote about the task's branch (worker/workspace.py),
    None for a task that ran in the live checkout."""
    try:
        path = projects.current().control_dir / "worktrees" / f"{int(task_id)}.json"
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, LookupError, RuntimeError):
        return None


def done_card(task: dict, stages: list[dict]) -> dict:
    """✅ card: one short paragraph per stage (readable on the phone) and, when
    no release stage committed the work, the question whether to commit."""
    tid = task["id"]
    parts = []
    for s in stages:
        if s["status"] == "skipped":
            continue
        mark = "✓" if s["status"] == "done" and s.get("verdict") in ("pass", None, "") else "✗"
        parts.append(f"{mark} {s['seq']}. {_role_label(s['role'])}\n{_short(s.get('summary') or s.get('error'), 420)}")
    released = any(s["status"] == "done" and s["role"] in agent_roles.CONFIRM_ROLES for s in stages)
    # Steps in order, each button saying exactly what it does; "Abschließen"
    # (archive the chat) is always last — the task itself is already done.
    finish = [{"label": "Abschließen – Chat archivieren", "data": f"archive:{tid}"}]
    deploy = [{"label": "Deployen lassen (falls nötig)", "data": f"deploy:{tid}"}]
    buttons = [finish]
    st = worktree_state(tid)
    if st is not None:
        if st.get("pending"):
            repos = {repo: r for repo, r in (st.get("repos") or {}).items() if r.get("ahead")}
            lines = [f"· {repo}: {r.get('ahead')} Commit(s){', ' + r['stat'] if r.get('stat') else ''}"
                     for repo, r in repos.items()]
            bases = {r.get("base") for r in repos.values() if r.get("base")}
            target = bases.pop() if len(bases) == 1 else "den Live-Branch"
            parts.append(f"Die Änderungen liegen auf Branch {st.get('branch')}, noch nicht im Live-Ordner:\n"
                         + "\n".join(lines)
                         + f"\n\nNächster Schritt: in {target} übernehmen = lokal mergen (kein Push, kein Deploy). "
                           "Danach kannst du deployen lassen.")
            buttons = [[{"label": f"In {target} übernehmen (mergen, ohne Deploy)", "data": f"apply:{tid}"}], *buttons]
        elif st.get("applied"):
            parts.append("Übernommen: " + ", ".join(st["applied"]))
            if not released:
                buttons = [deploy, *buttons]
    elif not released and task.get("repos"):
        parts.append("Die Änderungen sind noch nicht committet (Repos mit Live-Bots: die Stufen committen nicht selbst). "
                     "Nächster Schritt: committen (nur die Dateien dieses Tasks, kein Push), danach bei Bedarf deployen.")
        buttons = [[{"label": "Committen (nur dieser Task, kein Push)", "data": f"commit:{tid}"}], deploy, *buttons]
    return {"title": f"✅ #{tid} erledigt", "text": "\n\n".join(parts) or "fertig", "buttons": buttons}


def tick() -> None:
    state = _load_state()
    changed = False
    for ch in projects.task_channels():
        key = f"{ch.project}:{ch.task_id}"
        with projects.use(ch.project):  # role labels are per project
            task = tasks_db.get_task(ch.task_id)
            if task is None:
                continue
            stages = tasks_db.list_stages(ch.task_id)
            snap = snapshot(task, stages)
            if state.get(key) == snap:
                continue
            for a in changes(task, stages, state.get(key)):
                _post(ch.id, "alert", {**a, "source": "pipeline"})
            ask = explain_request(task, state.get(key))
            if ask:
                _post(ch.id, "message", ask)
        state[key] = snap
        changed = True
    if changed:
        _save_state(state)


def intro(task: dict) -> dict:
    """First message of a task chat: what was agreed."""
    parts = [_short(task.get("lead_note"), 1500)] if task.get("lead_note") else []
    parts.append(_short(task.get("refined_spec") or task.get("details"), 2500))
    parts.append("Freigegeben. Hier siehst du ab jetzt jede Stufe; frag mich jederzeit, was gerade passiert.")
    return {"title": f"#{task['id']} {task['title']}"[:200], "text": "\n\n".join(p for p in parts if p),
            "source": "lead"}


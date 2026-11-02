"""Lead run — the team lead picks what to start next (Tasks v2).

``team.yaml``::

    scouts:
      - id: lead
        role: lead                 # roles/<role>.md is the persona
        lead: true                 # this module instead of a plain scout run
        every_hours: 24
        model: opus
        max_proposals: 3           # recommendations per run (incl. still open ones)
        channel: myproject-lead     # the project's lead channel

Runs on the scout schedule (one read-only headless session, budget pacer as
for scouts). It sees the ideas (``new`` / ``refined``), the running
pipelines, recent results and the budget, and answers with a ``# Lead`` JSON
block. The worker then

- proposes the recommended tasks (``POST /propose`` — status "Zur Freigabe",
  a card with "Freigeben & starten" in the lead channel),
- tidies agent-made ideas: merge (``/merge``, closes it), done (``/done``),
  close — will not be done (``/close``), or park for later (``/park``,
  backlog); the user's own tasks get a card with that action instead,
- creates follow-up ideas that recently finished tasks name ("Folgetask",
  open points of the review) — each finished task is looked at once,
- looks into the open watchdog alerts (``GET /api/watchdog``): a real
  problem becomes a fix task with a "Freigeben & starten" card; noise or
  something already resolved becomes a card "Meldungen löschen" with the
  reason — deleting stays the user's tap (``/api/watchdog/dismiss/<key>``),
- posts one summary message to the lead channel.

Starting stays the user's click — the start route refuses internal callers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

import tasks_api

from .. import decisions
from . import pipeline, scouts

log = logging.getLogger("tasks-worker")

LEAD_RE = re.compile(r"^#\s*Lead\s*\n```(?:json)?\s*\n(.*?)\n```", re.M | re.S)
SCOUT_MARK = "(Vorschlag des Scouts"
FOLLOW_MARK = "(Folge-Task von #"
FOLLOW_DAYS = 14
WATCHDOG_MAX = 120  # newest open alerts shown per run
ACTIVE = ("planning", "running", "plan_ready", "awaiting_input", "blocked")


def from_scout(task: dict) -> bool:
    """Agent-made ideas (scout proposals, lead follow-ups) — the lead may park them."""
    d = task.get("details") or ""
    return SCOUT_MARK in d or FOLLOW_MARK in d


def parse_lead(text: str) -> Optional[dict]:
    """The last ``# Lead`` block -> {recommend: [{id, note}], park: [{id, reason}],
    summary}, or None when there is none (the run is then not counted)."""
    blocks = LEAD_RE.findall(text or "")
    if not blocks:
        return None
    try:
        raw = json.loads(blocks[-1])
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None

    def rows(key: str, field: str) -> list[dict]:
        out = []
        for r in raw.get(key) or []:
            if isinstance(r, dict) and isinstance(r.get("id"), int) and str(r.get(field) or "").strip():
                out.append({"id": r["id"], field: str(r[field]).strip()})
        return out

    create = []
    for c in raw.get("create") or []:
        if isinstance(c, dict) and str(c.get("title") or "").strip() and isinstance(c.get("from"), int):
            prio = c.get("prio")
            create.append({"title": str(c["title"]).strip()[:200], "details": str(c.get("details") or "").strip(),
                           "prio": prio if prio in (1, 2, 3) else 2, "from": c["from"],
                           "repos": [str(x) for x in (c.get("repos") or []) if isinstance(x, str)]})
    park = []
    for r_ in raw.get("park") or []:
        if isinstance(r_, dict) and isinstance(r_.get("id"), int) and str(r_.get("reason") or "").strip():
            park.append({"id": r_["id"], "reason": str(r_["reason"]).strip(),
                         "into": r_["into"] if isinstance(r_.get("into"), int) else None,
                         "done": bool(r_.get("done")), "close": bool(r_.get("close"))})
    watchdog = []
    for w in raw.get("watchdog") or []:
        if not isinstance(w, dict) or w.get("action") not in ("fix", "dismiss") or not str(w.get("reason") or "").strip():
            continue
        alerts = [a for a in (w.get("alerts") or []) if isinstance(a, int)]
        if not alerts:
            continue
        task = w.get("task") if isinstance(w.get("task"), dict) else {}
        if w["action"] == "fix" and not str(task.get("title") or "").strip():
            continue
        watchdog.append({"action": w["action"], "alerts": alerts, "reason": str(w["reason"]).strip(),
                         "group": str(w.get("group") or "").strip()[:120],
                         "task": {"title": str(task.get("title") or "").strip()[:200],
                                  "details": str(task.get("details") or "").strip(),
                                  "prio": task.get("prio") if task.get("prio") in (1, 2, 3) else 2,
                                  "repos": [str(x) for x in (task.get("repos") or []) if isinstance(x, str)]}})
    return {"recommend": rows("recommend", "note"), "park": park, "create": create, "watchdog": watchdog,
            "summary": str(raw.get("summary") or "").strip()}


def _line(t: dict, with_details: bool = False) -> str:
    origin = "Scout" if from_scout(t) else "User"
    head = f"- #{t['id']} [{t['status']}, prio {t['prio']}, {origin}] {t['title']}"
    if t.get("proposed_at"):
        head += " — schon vorgeschlagen, wartet auf Freigabe"
    if not with_details:
        return head
    body = " ".join(((t.get("refined_spec") or t.get("details") or "").split()))[:700]
    return f"{head}\n  {body}" if body else head


def _alert_line(a: dict) -> str:
    text = " ".join(str(a.get("text") or "").split())[:300]
    mark = " (Prüf-Chat offen)" if a.get("checked") else ""
    return f"- [{a['id']}] {str(a.get('ts') or '')[:16]} {a.get('title') or ''}{mark}\n  {text}"


def build_prompt(scout: dict, label: str, ideas: list[dict], active: list[dict], ledger: str,
                 budget: dict, assignment: str, persona: str, finished: Optional[list[dict]] = None,
                 decided: str = "", alerts: Optional[list[dict]] = None) -> str:
    limit = int(scout.get("max_proposals") or 3)
    waiting = sum(1 for t in ideas if t.get("proposed_at"))
    return f"""You are the team lead for {label}. You run unattended once a day;
your output goes to the user's lead channel. Investigate read-only (no file
edits, no git mutations, no deploys, never print secrets) — read code, logs
and the tasks below as needed.

{persona}

{assignment}

## Ideas (new / refined) — your backlog
{chr(10).join(_line(t, True) for t in ideas) or "(none)"}

## Running pipelines
{chr(10).join(_line(t) for t in active) or "(none)"}

## Just finished — follow-ups they name (each shown to you once)
{chr(10).join(f"- #{t['id']} {t['title']}{chr(10)}  " + " ".join((t.get("execute_summary") or "").split())[:1500] for t in (finished or [])) or "(none)"}

## Recent results
{ledger}

## Open watchdog alerts (not yet handled by you; ids in [brackets])
{chr(10).join(_alert_line(a) for a in (alerts or [])) or "(none)"}

## Weekly budget (pacer)
{json.dumps(budget, ensure_ascii=False)}
{decided}
## Output contract
Recommend at most {limit} new task(s) to start ({waiting} earlier recommendation(s)
still wait for the user's click — do not repeat them) — the ones with the best
value for the budget left; zero is fine. For each, a German note for the phone: why now, rough plan,
risk, effort. For the just-finished tasks: create an idea for each follow-up
they explicitly name (a "Folgetask", an open point the review left, an
inventory of remaining work) unless it already exists as an idea — check
the repo for the details (e.g. an inventory file the task wrote). Tidy the ideas: every entry in "park" needs a
reason and exactly one outcome —
- "into": <id>  same topic as another task: merged there (content appended), closed;
- "done": true  the work already exists: checked off as done;
- "close": true will not be done (duplicate without a target, obsolete,
  no longer wanted): closed, kept for reference;
- none of these: still worth doing, but LATER — parked in the backlog.
Backlog means "do it later", so never park what is obsolete — close it.
Agent-made ideas (Scout / Folge-Task) are handled right away; for the user's
own tasks each entry becomes a card where the user taps the action /
Löschen / Behalten — so list them there, not only in the summary.

Watchdog alerts: go through every one above. Group repeats (same check,
same cause). For each group investigate read-only — current logs, state,
code, memory notes — and decide:
- "fix": a real problem that needs a change: describe the fix as a task
  (problem with evidence, scope, how to verify); it gets a start card. If an
  idea or running task already covers it, say so in the reason and use
  "dismiss" instead.
- "dismiss": noise, expected behaviour, transient, or already fixed — the
  user gets a card to delete these alerts; the reason must say why (with the
  evidence you checked).
Every listed alert id belongs to exactly one group.

Only report what is still true NOW: the watchdog report and logs cover a past
window — before you mention a finding, check the current logs / state and the
memory notes (a note may say it was fixed). Drop what is resolved.
End with exactly this block, nothing after it:

# Lead
```json
{{"recommend": [{{"id": 12, "note": "..."}}], "park": [{{"id": 3, "reason": "...", "into": 7}}, {{"id": 4, "reason": "...", "done": true}}, {{"id": 5, "reason": "...", "close": true}}, {{"id": 6, "reason": "..."}}], "create": [{{"from": 109, "title": "...", "details": "problem, scope, how to verify", "prio": 2, "repos": ["..."]}}], "watchdog": [{{"group": "kurzer Name", "alerts": [101, 102], "action": "dismiss", "reason": "..."}}, {{"group": "...", "alerts": [103], "action": "fix", "reason": "...", "task": {{"title": "...", "details": "...", "prio": 1, "repos": ["..."]}}}}], "summary": "2-4 Sätze auf Deutsch: Lage, was du empfiehlst, was du aufgeräumt / neu angelegt hast"}}
```
"""


def _get(api_base: str, path: str) -> Any:
    with urllib.request.urlopen(f"{api_base}{path}", timeout=30) as r:
        return json.loads(r.read() or b"null")


def _post(api_base: str, path: str, body: dict) -> Any:
    req = urllib.request.Request(f"{api_base}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


def _budget(api_base: str) -> dict:
    try:
        with urllib.request.urlopen(f"{api_base}/api/budget", timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


async def run_lead(scout: dict, team_dir: Path, control: Path, label: str, cwd: str, api_base: str) -> None:
    sid = str(scout["id"])
    state = scouts.load_state(control, sid)
    persona = pipeline.load_role(str(scout.get("role") or "lead")).body
    assignment = ""
    path = team_dir / "scouts" / f"{sid}.md"
    if path.exists():
        assignment = path.read_text(encoding="utf-8").strip()
    tasks = await asyncio.to_thread(tasks_api.list_tasks)
    # The default list hides done tasks — fetch them for results + follow-ups.
    done = [t for t in await asyncio.to_thread(tasks_api.list_tasks, "done") if t.get("status") == "done"]
    ideas = sorted((t for t in tasks if t["status"] in ("new", "refined")), key=lambda t: (t["prio"], t["id"]))
    active = [t for t in tasks if t["status"] in ACTIVE]
    finished = done + [t for t in tasks if t["status"] == "blocked"]
    ledger = scouts.ledger_text(finished, limit=30)
    seen_done = set(state.get("followups_seen") or [])
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - FOLLOW_DAYS * 86400))
    just_done = [t for t in done if t.get("mode") == "pipeline"
                 and t["id"] not in seen_done and (t.get("updated_at") or "") >= cutoff]
    budget = await asyncio.to_thread(_budget, api_base)
    handled = set(state.get("watchdog_handled") or [])
    try:
        raw_alerts = await asyncio.to_thread(_get, api_base, "/api/watchdog")
    except Exception as e:  # noqa: BLE001
        log.warning("[lead %s] watchdog alerts unavailable: %s", sid, e)
        raw_alerts = []
    alerts = [{**a, "id": a["seq"], "key": f"{a['channel']}:{a['seq']}"} for a in raw_alerts
              if f"{a['channel']}:{a['seq']}" not in handled][-WATCHDOG_MAX:]
    prompt = build_prompt(scout, label, ideas, active, ledger, budget, assignment, persona, just_done,
                          decisions.prompt_section(control), alerts)
    log.info("[lead %s] starting (%d ideas, %d running)", sid, len(ideas), len(active))
    text, tools = await scouts._run_agent(prompt, scout.get("model"), cwd, None)
    (control / "scouts").mkdir(parents=True, exist_ok=True)
    (control / "scouts" / f"{sid}.last.md").write_text(text or "", encoding="utf-8")
    out = parse_lead(text)
    if out is None:
        log.warning("[lead %s] no parseable # Lead block — not counted, retry next round", sid)
        state.update(last_error="no # Lead block", last_attempt=time.time())
        scouts.save_state(control, sid, state)
        return

    by_id = {t["id"]: t for t in ideas}
    limit = int(scout.get("max_proposals") or 3)  # new per run; earlier ones may still wait
    proposed, parked, closed, ticked, kept = [], [], [], [], []
    for r in out["recommend"][:limit]:
        t = by_id.get(r["id"])
        if t is None or t.get("proposed_at"):
            continue
        try:
            await asyncio.to_thread(_post, api_base, f"/api/tasks/{t['id']}/propose", {"note": r["note"]})
            proposed.append(t)
        except Exception as e:  # noqa: BLE001
            log.warning("[lead %s] propose #%s failed: %s", sid, t["id"], e)
    for r in out["park"]:
        t = by_id.get(r["id"])
        if t is None:
            continue
        if r["into"] is not None and r["into"] not in by_id and r["into"] not in {x["id"] for x in tasks}:
            r["into"] = None  # unknown target: park instead
        if not from_scout(t):
            kept.append((t, r))
            continue
        try:
            if r["into"] is not None:
                await asyncio.to_thread(_post, api_base, f"/api/tasks/{t['id']}/merge",
                                        {"into": r["into"], "note": r["reason"]})
                closed.append((t, f"in #{r['into']} zusammengelegt — {r['reason']}"))
            elif r["done"]:
                await asyncio.to_thread(_post, api_base, f"/api/tasks/{t['id']}/done", {})
                ticked.append((t, r["reason"]))
            elif r["close"]:
                await asyncio.to_thread(_post, api_base, f"/api/tasks/{t['id']}/close", {"reason": r["reason"]})
                closed.append((t, r["reason"]))
            else:
                await asyncio.to_thread(_post, api_base, f"/api/tasks/{t['id']}/park", {"reason": r["reason"]})
                parked.append((t, r["reason"]))
        except Exception as e:  # noqa: BLE001
            log.warning("[lead %s] park #%s failed: %s", sid, t["id"], e)

    created = []
    for c in out["create"][:5]:
        try:
            t = await asyncio.to_thread(tasks_api.create_task, c["title"],
                                        f"{c['details']}\n\n{FOLLOW_MARK}{c['from']}, angelegt vom Lead)",
                                        c["prio"], c["repos"])
            created.append(t)
        except Exception as e:  # noqa: BLE001
            log.warning("[lead %s] create follow-up failed: %s", sid, e)
    by_alert = {a["id"]: a for a in alerts}
    fixes, dismiss = [], []
    for w in out["watchdog"]:
        items = [by_alert[i] for i in w["alerts"] if i in by_alert]
        if not items:
            continue
        handled |= {a["key"] for a in items}
        if w["action"] == "fix":
            src = ", ".join(f"{a['channel']} #{a['seq']}" for a in items[:5])
            try:
                t = await asyncio.to_thread(tasks_api.create_task, w["task"]["title"],
                                            f"{w['task']['details']}\n\n{SCOUT_MARK} lead · Watchdog {src})",
                                            w["task"]["prio"], w["task"]["repos"])
                await asyncio.to_thread(_post, api_base, f"/api/tasks/{t['id']}/propose",
                                        {"note": f"Watchdog: {w['reason']}"})
                fixes.append((t, w, items))
            except Exception as e:  # noqa: BLE001
                log.warning("[lead %s] watchdog fix task failed: %s", sid, e)
        else:
            dismiss.append((w, items))
    state["watchdog_handled"] = sorted(handled)[-3000:]
    state["followups_seen"] = sorted(seen_done | {t["id"] for t in just_done})[-500:]
    state.update(last_run=time.time(), last_error=None, last_tools=tools)
    scouts.save_state(control, sid, state)
    log.info("[lead %s] done: proposed %s, parked %s, closed %s, ticked %s", sid, [t["id"] for t in proposed],
             [t["id"] for t, _ in parked], [t["id"] for t, _ in closed], [t["id"] for t, _ in ticked])

    channel = scout.get("channel")
    if not channel:
        return
    lines = [out["summary"]] if out["summary"] else []
    if proposed:
        lines.append("Zur Freigabe: " + ", ".join(f"#{t['id']}" for t in proposed)
                     + " — Karten mit „Freigeben & starten“ stehen darüber.")
    if created:
        lines.append("Neue Folge-Ideen:\n" + "\n".join(f"#{t['id']} {t['title']}" for t in created))
    if closed:
        lines.append("Geschlossen (wird nicht gemacht):\n" + "\n".join(f"#{t['id']} {t['title']} — {why}" for t, why in closed))
    if ticked:
        lines.append("Als erledigt abgehakt:\n" + "\n".join(f"#{t['id']} {t['title']} — {why}" for t, why in ticked))
    if parked:
        lines.append("Ins Backlog (für später):\n" + "\n".join(f"#{t['id']} {t['title']} — {why}" for t, why in parked))
    for t, r in kept:
        # The user's own task: a card, the user decides.
        close = {"label": "Schließen – wird nicht gemacht", "data": f"close:{t['id']}"}
        park = {"label": "Ins Backlog – vielleicht später", "data": f"park:{t['id']}"}
        if r["into"]:
            first = {"label": f"In #{r['into']} zusammenlegen", "data": f"merge:{t['id']}:{r['into']}"}
        elif r["done"]:
            first = {"label": "Als erledigt abhaken", "data": f"done:{t['id']}"}
        else:
            first = close if r["close"] else park
        # The lead's pick on top; the other ways out (closed stays readable,
        # unlike delete) one tap away; deleting last and spelled out.
        rows = [[first], [b for b in (close, park) if b is not first]
                + [{"label": "Behalten – bleibt offen", "data": f"keep:{t['id']}"}]]
        if not r["into"]:
            rows.append([{"label": "Endgültig löschen", "data": f"delete:{t['id']}"}])
        try:
            await asyncio.to_thread(_post, api_base, f"/api/notify/{channel}", {
                "title": f"Aufräumen: #{t['id']} {t['title']}"[:200], "text": r["reason"], "source": "lead",
                "buttons": rows})
        except Exception as e:  # noqa: BLE001
            log.warning("[lead %s] tidy card #%s failed: %s", sid, t["id"], e)
    if kept:
        lines.append(f"{len(kept)} deiner Tasks würde ich aufräumen — Karten darüber.")
    for w, items in dismiss:
        key = f"{time.strftime('%Y%m%d-%H%M%S')}-{abs(hash((w['group'], items[0]['key']))) % 100000}"
        spec = {"alerts": {}, "reason": w["reason"], "group": w["group"], "created": time.time()}
        for a in items:
            spec["alerts"].setdefault(a["channel"], []).append(a["seq"])
        try:
            (control / "watchdog").mkdir(parents=True, exist_ok=True)
            (control / "watchdog" / f"{key}.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            n = len(items)
            await asyncio.to_thread(_post, api_base, f"/api/notify/{channel}", {
                "title": f"Watchdog: {n} Meldung{'en' if n != 1 else ''} löschen? {w['group']}"[:200],
                "text": w["reason"] + "\n\n" + "\n".join(
                    f"· {str(a.get('ts') or '')[11:16]} {a.get('title') or ''}" for a in items[:8])
                        + (f"\n· … und {n - 8} weitere" if n > 8 else ""),
                "source": "lead",
                "buttons": [[{"label": f"Löschen ({n})", "data": f"wdel:{key}"},
                             {"label": "Behalten", "data": f"wkeep:{key}"}]]})
        except Exception as e:  # noqa: BLE001
            log.warning("[lead %s] watchdog card failed: %s", sid, e)
    if fixes:
        lines.append("Watchdog → Fix vorgeschlagen:\n" + "\n".join(
            f"#{t['id']} {t['title']} ({len(items)} Meldung{'en' if len(items) != 1 else ''})" for t, _, items in fixes))
    if dismiss:
        lines.append(f"Watchdog: {sum(len(i) for _, i in dismiss)} Meldungen würde ich löschen "
                     f"({len(dismiss)} Gruppe{'n' if len(dismiss) != 1 else ''}) — Karten mit Begründung darüber.")
    if not lines:
        lines.append("Nichts Neues zum Starten.")
    lines.append("Antworte hier, um die Vorschläge zu besprechen.")
    title = (f"Lead: {len(proposed)} zur Freigabe" + (f", {len(closed)} geschlossen" if closed else "")
             + (f", {len(ticked)} abgehakt" if ticked else "") + (f", {len(parked)} ins Backlog" if parked else "")
             + (f", {len(created)} neue Folge-Ideen" if created else "")
             + (f", {len(fixes)} Watchdog-Fix" if fixes else ""))
    try:
        await asyncio.to_thread(scouts._notify, api_base, channel, title, "\n\n".join(lines), f"lead:{sid}")
    except Exception as e:  # noqa: BLE001
        log.warning("[lead %s] channel notify failed: %s", sid, e)

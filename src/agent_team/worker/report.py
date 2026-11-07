"""Weekly report — the lead sums up the team's week in the lead channel.

``team.yaml``::

    scouts:
      - id: weekly-report
        role: lead              # persona (e.g. product-owner)
        report: true            # this module instead of a scout run
        every_hours: 168
        always: true            # runs even when the pacer says wait (cheap)
        model: sonnet
        channel: myproject-lead

The worker collects the facts (tasks finished / running / waiting, new and
parked ideas, cost of the week's agent runs, commits per author in the team's
repos, the budget) and a read-only session turns them into a short German
report: ``# Wochenbericht`` … posted to the lead channel, kept under
``<control>/reports/<date>.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import tasks_api
import yaml

from . import lead, pipeline, scouts

log = logging.getLogger("tasks-worker")

MARKER = "# Wochenbericht"


def _get(api_base: str, path: str) -> Any:
    with urllib.request.urlopen(f"{api_base}{path}", timeout=30) as r:
        return json.loads(r.read())


def _repo_dirs(team_dir: Path, cwd: str) -> list[Path]:
    try:
        team = yaml.safe_load((team_dir / "team.yaml").read_text()) or {}
    except (OSError, ValueError):
        team = {}
    root = Path(cwd)
    dirs = [root] if (root / ".git").exists() else []
    dirs += [root / r for r in team.get("repos") or [] if (root / r / ".git").exists()]
    return dirs


def commits_since(dirs: list[Path], since: str) -> dict[str, dict[str, int]]:
    """{repo: {author: n}} for commits since ``since`` (git date string)."""
    out: dict[str, dict[str, int]] = {}
    for d in dirs:
        try:
            raw = subprocess.run(["git", "-C", str(d), "log", f"--since={since}", "--format=%an"],
                                 capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        counts: dict[str, int] = {}
        for a in raw.splitlines():
            if a.strip():
                counts[a.strip()] = counts.get(a.strip(), 0) + 1
        if counts:
            out[d.name] = counts
    return out


def _in_week(ts: Optional[str], cutoff: datetime) -> bool:
    try:
        return datetime.fromisoformat(ts or "") >= cutoff
    except ValueError:
        return False


def role_lines(stats: list[dict]) -> str:
    return "\n".join(
        f"- {r['role']}: {r['stages']} Stufen, {r['pass']} pass / {r['fail']} fail"
        + (f" / {r['blocked']} blocked" if r["blocked"] else "") + (f", {r['crashed']} abgebrochen" if r["crashed"] else "")
        + (f", {r['retried']} wiederholt" if r["retried"] else "") + f", ${r['cost_usd']:.2f}, Ø {r['avg_min']} min"
        for r in stats) or "(keine Stufen)"


def facts(active: list[dict], done: list[dict], backlog: list[dict], costs: dict[int, float],
          commits: dict, budget: dict, cutoff: datetime, closed: Optional[list[dict]] = None,
          roles: Optional[list[dict]] = None) -> str:
    def line(t: dict, with_summary: bool = False) -> str:
        s = f"- #{t['id']} [{t['status']}] {t['title']}"
        if t.get("stop_reason"):
            s += f" — gestoppt: {' '.join(t['stop_reason'].split())[:200]}"
        if with_summary:
            body = " ".join((t.get("execute_summary") or "").split())[:900]
            s += f"\n  {body}" if body else ""
        if t["id"] in costs:
            s += f"\n  Kosten: ${costs[t['id']]:.2f}"
        return s

    done_w = [t for t in done if _in_week(t.get("updated_at"), cutoff)]
    ideas = [t for t in active if t["status"] in ("new", "refined")]
    new_ideas = [t for t in ideas if _in_week(t.get("created_at"), cutoff)]
    waiting = [t for t in active if t["status"] in ("awaiting_input", "blocked", "failed", "limit_reached")
               or (t["status"] == "plan_ready" and not t.get("paused_reason"))]
    running = [t for t in active if t["status"] in ("planning", "running")
               or (t["status"] == "plan_ready" and t.get("paused_reason"))]
    proposed = [t for t in ideas if t.get("proposed_at")]
    parked_w = [t for t in backlog if _in_week(t.get("updated_at"), cutoff)]
    closed_w = [t for t in closed or [] if _in_week(t.get("updated_at"), cutoff)]
    commit_lines = [f"- {repo}: " + ", ".join(f"{a} {n}" for a, n in sorted(c.items(), key=lambda x: -x[1]))
                    for repo, c in sorted(commits.items())]
    return f"""## Erledigt diese Woche ({len(done_w)})
{chr(10).join(line(t, True) for t in done_w) or "(nichts)"}

## Läuft gerade ({len(running)})
{chr(10).join(line(t) for t in running) or "(nichts)"}

## Wartet auf den User ({len(waiting)}), dazu {len(proposed)} Vorschläge zur Freigabe
{chr(10).join(line(t) for t in waiting + proposed) or "(nichts)"}

## Ideen: {len(ideas)} offen, {len(new_ideas)} neu diese Woche, {len(parked_w)} ins Backlog (später), {len(closed_w)} geschlossen (wird nicht gemacht)
{chr(10).join(line(t) for t in new_ideas[:15]) or "(keine neuen)"}

## Commits diese Woche (Repo: Autor Anzahl)
{chr(10).join(commit_lines) or "(keine)"}

## Rollen diese Woche (Stufen, Urteile, Kosten, Dauer)
{role_lines(roles or [])}

## Budget (Wochenfenster, alle Teams zusammen)
{json.dumps(budget, ensure_ascii=False)}
"""


def build_prompt(label: str, persona: str, facts_text: str, week: str) -> str:
    return f"""You are the team lead for {label}. Write the weekly report for the user
(KW {week}). Read-only: you may look at code, logs and task details to explain,
but change nothing.

{persona}

# Facts collected by the worker
{facts_text}

# Report
German, for the phone, at most ~25 lines, plain language, no test-name lists:
1. Two or three sentences: how the week went.
2. What was finished and what it brings (one line each, most important first).
3. What is stuck or waits for the user — and what the user should do.
4. Cost/budget in one line (what the team's runs cost, where the budget stands).
   Then one line on the roles, only if something stands out: a gate that
   passes nearly everything in very little time (too lenient?), a role that
   fails or is retried a lot, or one that costs far more than the others.
5. Next week: the 2-3 things you would do, in order.
Start your final message with the line `{MARKER}` and put nothing after the report.
"""


async def run_report(scout: dict, team_dir: Path, control: Path, label: str, cwd: str, api_base: str) -> None:
    sid = str(scout["id"])
    state = scouts.load_state(control, sid)
    persona = pipeline.load_role(str(scout.get("role") or "lead")).body
    now = datetime.now().astimezone()
    cutoff = now - timedelta(days=7)
    active = await asyncio.to_thread(tasks_api.list_tasks)
    done = [t for t in await asyncio.to_thread(tasks_api.list_tasks, "done") if t.get("status") == "done"]
    backlog = await asyncio.to_thread(tasks_api.list_tasks, "backlog")
    closed = await asyncio.to_thread(tasks_api.list_tasks, "closed")
    costs: dict[int, float] = {}
    for t in [t for t in done if _in_week(t.get("updated_at"), cutoff)] + \
            [t for t in active if t["status"] not in ("new", "refined")]:
        try:
            c = await asyncio.to_thread(_get, api_base, f"/api/tasks/{t['id']}/cost")
            costs[t["id"]] = float(c.get("total_usd") or 0)
        except Exception:  # noqa: BLE001
            pass
    commits = await asyncio.to_thread(commits_since, _repo_dirs(team_dir, cwd), "7 days ago")
    budget = await asyncio.to_thread(lead._budget, api_base)
    try:
        roles = await asyncio.to_thread(_get, api_base, "/api/tasks/role-stats?days=7")
    except Exception:  # noqa: BLE001
        roles = []
    if not isinstance(roles, list):
        roles = []
    week = now.strftime("%V")
    prompt = build_prompt(label, persona, facts(active, done, backlog, costs, commits, budget, cutoff, closed, roles), week)
    log.info("[report %s] starting (KW %s)", sid, week)
    text, tools = await scouts._run_agent(prompt, scout.get("model"), cwd, None)
    i = (text or "").find(MARKER)
    if i < 0:
        log.warning("[report %s] no %s in the output — retry next round", sid, MARKER)
        state.update(last_error=f"no {MARKER}", last_attempt=time.time())
        scouts.save_state(control, sid, state)
        return
    body = text[i + len(MARKER):].strip()
    reports = control / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"{now:%Y-%m-%d}-{sid}.md").write_text(f"{MARKER} KW {week}\n\n{body}\n", encoding="utf-8")
    state.update(last_run=time.time(), last_error=None, last_tools=tools)
    scouts.save_state(control, sid, state)
    log.info("[report %s] done (%d chars)", sid, len(body))
    channel = scout.get("channel")
    if channel:
        try:
            project = api_base.rstrip("/").rsplit("/p/", 1)[-1]
            await asyncio.to_thread(scouts._notify, api_base, channel, f"Wochenbericht KW {week} · {project}"[:200],
                                    body[:19_000], f"report:{sid}")
        except Exception as e:  # noqa: BLE001
            log.warning("[report %s] channel notify failed: %s", sid, e)

"""Scouts — autonomous proposals per project (run by the project's worker).

``team.yaml``::

    scouts:
      - id: product-owner          # state + log key
        role: product-owner        # roles/<role>.md is the persona
        every_hours: 24
        model: opus
        max_proposals: 3
        channel: myproject        # chat channel that gets the summary
        browser: true              # optional: headless Chromium via Playwright MCP
        ledger: true               # optional: earlier results (done/blocked tasks) in the prompt
        lead: true                 # optional: the team lead's run instead (worker/lead.py)
        report: true               # optional: the weekly report instead (worker/report.py)
        artifacts: .claude/skills/app-test/laeufe   # optional: where screenshots go (cwd-relative)
        prepare: watchdog-digest.sh  # optional: scouts/<file> builds an input file first
        always: true               # optional: run even when the pacer says wait (not when paused)

and ``scouts/<id>.md`` is the assignment; ``scouts/_common.md`` (optional)
holds rules every scout of the team gets. When a scout is due and the budget
pacer allows autonomous work, the worker runs one read-only headless session
(Write/Edit/NotebookEdit disallowed) with persona + assignment + the open
tasks and earlier proposals (no repeats). Its ``# Proposals`` JSON becomes
tasks in status ``new`` and one message in the channel (inbox + push).
State: ``<control>/scouts/<id>.json``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

import tasks_api

from .. import budget, decisions, guard
from . import pipeline

log = logging.getLogger("tasks-worker")

PROPOSALS_RE = re.compile(r"^#\s*Proposals\s*\n```(?:json)?\s*\n(.*?)\n```", re.M | re.S)
RUN_TIMEOUT_S = float(os.environ.get("SCOUT_TIMEOUT_SECONDS", "1200"))
_running: set[str] = set()


def _state_path(control: Path, sid: str) -> Path:
    return control / "scouts" / f"{sid}.json"


def load_state(control: Path, sid: str) -> dict:
    try:
        return json.loads(_state_path(control, sid).read_text())
    except (OSError, ValueError):
        return {}


def save_state(control: Path, sid: str, state: dict) -> None:
    p = _state_path(control, sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    tmp.replace(p)


def due(scout: dict, state: dict, now: float) -> bool:
    every = float(scout.get("every_hours") or 24) * 3600
    return now - float(state.get("last_run") or 0) >= every


def parse_proposals(text: str, limit: int) -> Optional[list[dict]]:
    """The last ``# Proposals`` JSON block -> [{title, details, prio, repos}],
    or None when there is none (the run is then not counted)."""
    blocks = PROPOSALS_RE.findall(text or "")
    if not blocks:
        return None
    try:
        raw = json.loads(blocks[-1])
    except ValueError:
        return None
    if not isinstance(raw, list):
        return None
    out = []
    for p in raw[:limit]:
        if not isinstance(p, dict) or not str(p.get("title") or "").strip():
            continue
        prio = p.get("prio")
        out.append({
            "title": str(p["title"]).strip()[:200],
            "details": str(p.get("details") or "").strip(),
            "prio": prio if prio in (1, 2, 3) else 2,
            "repos": [str(r) for r in (p.get("repos") or []) if isinstance(r, str)],
        })
    return out


def is_info(p: dict) -> bool:
    """`[INFO] …` proposals are channel messages, not tasks (Tasks v2)."""
    return p["title"].lstrip().upper().startswith("[INFO]")


def ledger_text(finished: list[dict], limit: int = 60) -> str:
    """Earlier results: what was tried and how it ended (done = passed the
    gates; blocked = stopped, with the reason)."""
    rows = sorted(finished, key=lambda t: t.get("updated_at") or "", reverse=True)[:limit]
    out = []
    for t in rows:
        outcome = (t.get("execute_summary") if t["status"] == "done" else t.get("stop_reason")) or ""
        outcome = " ".join(outcome.split())[:400]
        out.append(f"- #{t['id']} [{t['status']}] {t['title']} — {outcome or '(no summary)'}")
    return "\n".join(out) or "(none yet)"


def build_prompt(scout: dict, label: str, open_tasks: list[dict], proposed: list[str],
                 assignment: str, persona: str, ledger: Optional[str] = None, decided: str = "") -> str:
    limit = int(scout.get("max_proposals") or 3)
    tasks = "\n".join(f"- #{t['id']} [{t['status']}] {t['title']}" for t in open_tasks[:150]) or "(none)"
    earlier = "\n".join(f"- {t}" for t in proposed[-100:]) or "(none)"
    ledger_part = f"""
## Ledger — earlier results (learn from them; never re-test a rejected idea
## unless you name what is materially different this time)
{ledger}
""" if ledger is not None else ""
    return f"""You are a scout for {label}: you run unattended on a schedule,
nobody is watching. Investigate read-only (no file edits, no git mutations, no
deploys, no container restarts, never print secrets or .env files), then
propose new work.

{persona}

{assignment}

## Open tasks (do not duplicate)
{tasks}

## Proposed before (never repeat, not even reworded)
{earlier}
{ledger_part}{decided}
## Output contract
At most {limit} proposals, only genuinely new and valuable ones — zero is a
fine answer. End your message with exactly this block, nothing after it:

# Proposals
```json
[{{"title": "...", "details": "problem, evidence (numbers), proposed change, how to verify", "prio": 2, "repos": ["..."]}}]
```
(`[]` if you found nothing worth proposing.)

A proposal becomes a task — only for something that needs doing. A finding
that needs no action (e.g. a backfill that runs, noise you checked) is not a
proposal: mention it above the block, or, if the user should still hear it,
start its title with `[INFO]` — those go to the channel only, never become
tasks.
"""


PREPARE_TIMEOUT_S = 600
KEEP_PREPARED = 30


async def run_prepare(scout: dict, state: dict, team_dir: Path, control: Path, cwd: str) -> Path:
    """Run ``scouts/<prepare>`` (bash) with SCOUT_SINCE / SCOUT_NOW (UTC ISO;
    the window since the last successful run) and SCOUT_OUT (the file it must
    write). Keeps the last KEEP_PREPARED outputs next to the scout's state."""
    sid = str(scout["id"])
    now = time.time()
    since = float(state.get("last_run") or 0) or now - float(scout.get("every_hours") or 24) * 3600
    out_dir = control / "scouts" / sid
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"input_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))}.log"
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
    env = {**os.environ, "SCOUT_SINCE": iso(since), "SCOUT_NOW": iso(now), "SCOUT_OUT": str(out)}
    proc = await asyncio.create_subprocess_exec(
        "bash", str(team_dir / "scouts" / str(scout["prepare"])), cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        log_out, _ = await asyncio.wait_for(proc.communicate(), PREPARE_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"prepare timed out after {PREPARE_TIMEOUT_S}s")
    if proc.returncode or not out.exists():
        raise RuntimeError(f"prepare exited {proc.returncode}: {log_out.decode(errors='replace')[-500:]}")
    for old in sorted(out_dir.glob("input_*.log"))[:-KEEP_PREPARED]:
        old.unlink(missing_ok=True)
    return out


def browser_mcp_config(output_dir: str, viewport: str = "390x844") -> dict:
    """Playwright MCP over the image's Chromium: headless, phone viewport,
    in-memory profile (every run starts logged out), screenshots to output_dir."""
    return {"mcpServers": {"playwright": {
        "command": "playwright-mcp",
        "args": ["--headless", "--browser", "chromium", "--executable-path", "/usr/bin/chromium",
                 "--no-sandbox", "--isolated", "--viewport-size", viewport, "--output-dir", output_dir],
    }}}


async def _run_agent(prompt: str, model: Optional[str], cwd: str,
                     mcp_config: Optional[Path] = None) -> tuple[str, dict[str, int]]:
    """Run one headless session; returns (final text, tool-use counts)."""
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions",
           "--disallowedTools", "Write", "Edit", "NotebookEdit"]
    if model:
        cmd += ["--model", model]
    settings = guard.cli_settings()
    if settings:
        cmd += ["--settings", settings]
    if mcp_config is not None:
        cmd += ["--mcp-config", str(mcp_config)]
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), RUN_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"scout timed out after {RUN_TIMEOUT_S:.0f}s")
    if proc.returncode:
        raise RuntimeError(f"claude exited {proc.returncode}: {err.decode(errors='replace')[-500:]}")
    return parse_stream(out.decode(errors="replace"))


def parse_stream(raw: str) -> tuple[str, dict[str, int]]:
    """stream-json -> (final result text, {tool name: uses})."""
    tools: dict[str, int] = {}
    result = ""
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "assistant":
            for blk in (ev.get("message") or {}).get("content") or []:
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    tools[blk.get("name") or "?"] = tools.get(blk.get("name") or "?", 0) + 1
        elif ev.get("type") == "result":
            result = str(ev.get("result") or "")
            budget.record_cost(ev.get("total_cost_usd"), "scout")
    return result, tools


def _budget_ok(api_base: str) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"{api_base}/api/budget", timeout=8) as r:
            d = json.loads(r.read())
        return bool(d.get("allowed", True)), str(d.get("reason") or "")
    except Exception as e:  # pacer unreachable: do not block (fail open, like the pacer)
        return True, f"pacer unreachable ({e})"


def _notify(api_base: str, channel: str, title: str, text: str, source: str) -> None:
    req = urllib.request.Request(
        f"{api_base}/api/notify/{channel}",
        data=json.dumps({"title": title, "text": text, "source": source}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=15).read()


async def run_scout(scout: dict, team_dir: Path, control: Path, label: str, cwd: str, api_base: str) -> None:
    sid = str(scout["id"])
    state = load_state(control, sid)
    persona = pipeline.load_role(str(scout.get("role") or sid)).body
    assignment = (team_dir / "scouts" / f"{sid}.md").read_text(encoding="utf-8").strip()
    common = team_dir / "scouts" / "_common.md"
    if common.exists():
        assignment = common.read_text(encoding="utf-8").strip() + "\n\n" + assignment
    open_tasks = await asyncio.to_thread(tasks_api.list_tasks)
    ledger = None
    if scout.get("ledger"):
        finished = (await asyncio.to_thread(tasks_api.list_tasks, "done")) + \
                   (await asyncio.to_thread(tasks_api.list_tasks, "blocked"))
        ledger = ledger_text(finished)
    prompt = build_prompt(scout, label, open_tasks, list(state.get("proposed") or []), assignment, persona, ledger,
                          decisions.prompt_section(control))
    if scout.get("prepare"):
        prepared = await run_prepare(scout, state, team_dir, control, cwd)
        prompt += (f"\n\n## Prepared input\nRead `{prepared}` first — built for this run "
                   f"(window since the last run). Earlier ones are next to it.\n")
    mcp_path = None
    if scout.get("browser"):
        run_dir = Path(cwd) / str(scout.get("artifacts") or f".agent-team/scouts/{sid}") / time.strftime("%Y-%m-%d")
        run_dir.mkdir(parents=True, exist_ok=True)
        mcp_path = control / "scouts" / f"{sid}.mcp.json"
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        mcp_path.write_text(json.dumps(browser_mcp_config(str(run_dir))))
        prompt += (f"\n\n## Browser\nYou have a headless Chromium (Playwright MCP tools, phone "
                   f"viewport 390x844, fresh profile each run). Screenshots go to `{run_dir}` — "
                   f"reference them by path in your proposals.\n")
    log.info("[scout %s] starting (model=%s, browser=%s)", sid, scout.get("model") or "<default>",
             bool(scout.get("browser")))
    text, tools = await _run_agent(prompt, scout.get("model"), cwd, mcp_path)
    browser_uses = sum(n for t, n in tools.items() if t.startswith("mcp__playwright__"))
    log.info("[scout %s] tools used: %s", sid, dict(sorted(tools.items(), key=lambda x: -x[1])))
    state["last_tools"] = tools
    # Keep the full report: a run with zero proposals should still say why.
    (control / "scouts").mkdir(parents=True, exist_ok=True)
    (control / "scouts" / f"{sid}.last.md").write_text(text or "", encoding="utf-8")
    proposals = parse_proposals(text, int(scout.get("max_proposals") or 3))
    if proposals is None:
        log.warning("[scout %s] no parseable # Proposals block — not counted, retry next round", sid)
        state["last_error"] = "no # Proposals block"
        state["last_attempt"] = time.time()
        save_state(control, sid, state)
        return
    created = []
    infos = [p for p in proposals if is_info(p)]
    for p in proposals:
        if is_info(p):
            continue
        t = await asyncio.to_thread(
            tasks_api.create_task, p["title"],
            f"{p['details']}\n\n(Vorschlag des Scouts {sid})", p["prio"], p["repos"])
        created.append((t["id"], p))
    if scout.get("browser") and browser_uses == 0:
        log.warning("[scout %s] browser scout did not use the browser", sid)
    state.update(last_run=time.time(), last_error=None, last_browser_uses=browser_uses,
                 proposed=(list(state.get("proposed") or []) + [p["title"] for p in proposals])[-300:])
    save_state(control, sid, state)
    log.info("[scout %s] done: %d proposal(s) -> tasks %s, %d info(s)", sid, len(created),
             [c[0] for c in created], len(infos))
    channel = scout.get("channel")
    if infos and channel:
        text = "\n\n".join(f"{p['title']}\n{p['details'][:600]}" for p in infos)
        try:
            await asyncio.to_thread(_notify, api_base, channel, f"Info ({sid})", text, f"scout:{sid}")
        except Exception as e:
            log.warning("[scout %s] channel notify failed: %s", sid, e)
    if created and channel:
        lines = [f"#{tid} {p['title']}\n{p['details'][:400]}" for tid, p in created]
        tail = ("Die Vorschläge stehen als Ideen in der App; der Lead wählt daraus aus und "
                "schlägt dir im Lead-Kanal vor, was als Nächstes starten soll.")
        text = "\n\n".join(lines) + "\n\n" + tail + " Antworte hier, um sie zu besprechen."
        try:
            await asyncio.to_thread(_notify, api_base, channel, f"{len(created)} neue Vorschläge ({sid})",
                                    text, f"scout:{sid}")
        except Exception as e:
            log.warning("[scout %s] channel notify failed: %s", sid, e)
    if not created and not infos and channel:
        # Each role has its own chat now (User 2026-11-01): a run that found
        # nothing says so, so the chat shows what the role has been doing.
        try:
            await asyncio.to_thread(_notify, api_base, channel, f"Lauf fertig ({sid})",
                                    "Nichts Neues gefunden — keine Vorschläge.", f"scout:{sid}")
        except Exception as e:
            log.warning("[scout %s] channel notify failed: %s", sid, e)


def tick(team: dict, team_dir: Path, control: Path, label: str, cwd: str, api_base: str,
         bg: set) -> None:
    """Start due scouts (one at a time per scout) when the budget allows."""
    now = time.time()
    scouts_ = team.get("scouts") or []
    always = {str(s.get("id")) for s in scouts_ if s.get("always")}
    for scout in scouts_:
        sid = str(scout.get("id") or "")
        if not sid or sid in _running:
            continue
        # One optional scout at a time per project: they queue up instead of
        # all hitting the 5-h window at once (always-scouts don't count).
        if sid not in always and _running - always:
            continue
        state = load_state(control, sid)
        # "Jetzt laufen lassen" in the app: <control>/scouts/<id>.run — runs
        # now, also when the pacer says wait (the user's own action); only a
        # manual pause holds it.
        trigger = control / "scouts" / f"{sid}.run"
        manual = trigger.exists()
        if not manual:
            if not due(scout, state, now):
                continue
            # A failed attempt (no block / error) waits an hour before retrying.
            if now - float(state.get("last_attempt") or 0) < 3600:
                continue
        ok, reason = _budget_ok(api_base)
        if not ok and not ((manual or scout.get("always")) and reason != "manuell pausiert"):
            continue
        if manual:
            trigger.unlink(missing_ok=True)
            log.info("[scout %s] started by hand", sid)
        state["last_attempt"] = now
        save_state(control, sid, state)

        _running.add(sid)

        async def _go(s=scout, i=sid):
            try:
                if s.get("report"):
                    from .report import run_report  # imports this module
                    await run_report(s, team_dir, control, label, cwd, api_base)
                elif s.get("lead"):
                    from .lead import run_lead  # lead imports this module
                    await run_lead(s, team_dir, control, label, cwd, api_base)
                else:
                    await run_scout(s, team_dir, control, label, cwd, api_base)
            except Exception as e:
                log.exception("[scout %s] failed: %s", i, e)
            finally:
                _running.discard(i)

        bg.add(asyncio.create_task(_go()))

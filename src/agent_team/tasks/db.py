"""SQLite-backed task store — one database per project.

The store lives in the current project's
data dir (``<data>/projects/<id>/tasks.db`` + ``sessions/``), and the plan
rules (e.g. backtest-engineer -> auditor, end with a reviewer) come from the project's team.yaml (``agent_roles.FOLLOW_UP``
/ ``agent_roles.FINAL``).

Workers reach this store over HTTP (``base/tasks_api.py``) — the
FastAPI backend is the single writer.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

from .. import projects
from . import agent_roles, stage_result

BERLIN = ZoneInfo("Europe/Berlin")

# Pipeline-mode statuses (mode='pipeline' tasks only — see the pipeline
# section at the bottom of this module):
#   planning       orchestrator run in flight (busy)
#   plan_ready     pipeline idle with a plan: user can edit the pending
#                  stages / limits and run the next stage (also the paused
#                  state between stages when autopilot is off or
#                  refuses the next stage)
#   running        one stage in flight (busy)
#   awaiting_input a stage asked a question; answer resumes it
#   blocked        stop rule tripped (rounds/stages/budget/repeat finding/
#                  user stop); stop_reason says why; Resume -> plan_ready
PIPELINE_STATUS = {"planning", "plan_ready", "running", "awaiting_input", "blocked"}
VALID_STATUS = {
    "new", "refining", "refined", "done", "failed", "limit_reached", "backlog", "closed",
} | PIPELINE_STATUS
# limit_reached behaves like 'failed' for user actions (edit + retry allowed)
# but keeps refined_spec / pending_reply / session_id intact so a retry after
# the Anthropic monthly limit resets can resume the same chat turn.
# backlog is a parked state ("later"): out of the active list, never claimed
# by the worker, but kept (with spec/history) until restored or deleted.
# Editable so the user can refine the wording while it sits parked.
# closed = will not be done (merged into another task, obsolete, duplicate,
# solved elsewhere); kept for reference with lead_note saying why, reopenable.
EDITABLE_STATUS = {
    "new", "refined", "failed", "limit_reached", "backlog",
    "plan_ready", "awaiting_input", "blocked",
}
BUSY_STATUS = {"refining", "planning", "running"}
VALID_PRIO = {1, 2, 3}  # 1 = high, 2 = normal, 3 = low

STAGE_STATUS = {"pending", "running", "awaiting_input", "done", "failed", "skipped"}
# Two levels (Tasks v2, docs/TASKS-V2.md):
#   off        every stage waits for the user's click ("Jede Stufe bestätigen")
#   full-bots  runs through until a gate ("Läuft bis zur Freigabe"): question,
#              fail -> blocked, budget, wait period, confirm role (deploy).
#              Implement stages on live-bot repos run hands-off (worker:
#              pipeline.is_hands_off — no commits, no running containers).
# The former 'advise' / 'full' are migrated to 'full-bots' (_ensure_db).
AUTOPILOT_LEVELS = ("off", "full-bots")
DEFAULT_AUTOPILOT = os.environ.get("TASKS_DEFAULT_AUTOPILOT", "full-bots").strip().lower()
# Defaults for plans that don't state limits (and what /roles advertises).
# Generous on purpose: the account runs on a subscription, and the hard
# brakes are the gate rules, not these counters. Override per container
# via env, per task in the plan editor.
DEFAULT_MAX_ROUNDS = int(os.environ.get("TASKS_DEFAULT_MAX_ROUNDS", "5"))
DEFAULT_MAX_STAGES = int(os.environ.get("TASKS_DEFAULT_MAX_STAGES", "20"))

# Per-task agent event streams: refine.jsonl / execute.jsonl (simple mode),
# plan.jsonl (orchestrator runs) and stage_<seq>.jsonl (one per stage).
STREAM_KIND_RE = re.compile(r"^(refine|execute|plan|stage_\d+)$")


def is_stream_kind(kind: Optional[str]) -> bool:
    return bool(kind) and STREAM_KIND_RE.match(kind) is not None


class TransitionError(Exception):
    """A state transition was refused for a reason worth telling the user
    (mapped to HTTP 409 by the router). Plain ``None`` returns stay the
    convention for 'not found / wrong status'."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    prio INTEGER NOT NULL DEFAULT 2,
    repos_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    refined_spec TEXT,
    execute_summary TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_updated_at ON tasks(updated_at);
CREATE TABLE IF NOT EXISTS task_stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    kind TEXT NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    attempt INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    session_id TEXT,
    session_backend TEXT,
    output TEXT,
    verdict TEXT,
    summary TEXT,
    findings_json TEXT,
    questions_json TEXT,
    pending_answer TEXT,
    cost_usd REAL NOT NULL DEFAULT 0,
    metered INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    num_turns INTEGER,
    tokens INTEGER,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, seq)
);
"""

_ensured: set[Path] = set()


def tasks_dir() -> Path:
    return projects.current().data_dir


def db_path() -> Path:
    return tasks_dir() / "tasks.db"


def sessions_root() -> Path:
    return tasks_dir() / "sessions"


def _now() -> str:
    return datetime.now(BERLIN).isoformat(timespec="seconds")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    path = _ensure_db()
    cx = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA journal_mode=DELETE")
    cx.execute("PRAGMA synchronous=NORMAL")
    try:
        yield cx
    finally:
        cx.close()


def _ensure_db() -> Path:
    path = db_path()
    if path in _ensured:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    cx = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    try:
        cx.executescript(SCHEMA)
        # Additive migrations — safe to run on every cold start since
        # ALTER TABLE ADD COLUMN is idempotent when guarded by a
        # PRAGMA table_info check.
        cols = {r[1] for r in cx.execute("PRAGMA table_info(tasks)").fetchall()}
        if "session_id" not in cols:
            cx.execute("ALTER TABLE tasks ADD COLUMN session_id TEXT")
        if "pending_reply" not in cols:
            cx.execute("ALTER TABLE tasks ADD COLUMN pending_reply TEXT")
        if "last_busy_kind" not in cols:
            # Records which agent kind ('refine' or 'execute') was running
            # when a limit_reached / failed status was set, so the Retry
            # button can dispatch back to the correct endpoint instead of
            # always defaulting to refine.
            cx.execute("ALTER TABLE tasks ADD COLUMN last_busy_kind TEXT")
        if "refine_model" not in cols:
            # Per-task model override. NULL means "fall back to the
            # worker's WORKER_REFINE_MODEL env var". Set from the Tasks UI.
            cx.execute("ALTER TABLE tasks ADD COLUMN refine_model TEXT")
        if "execute_model" not in cols:
            cx.execute("ALTER TABLE tasks ADD COLUMN execute_model TEXT")
        if "session_backend" not in cols:
            # Which agent backend ('claude' or 'codex') produced session_id.
            # A Codex thread_id cannot be resumed by the Claude SDK and vice
            # versa, so the worker compares this against the backend it
            # resolved for the current run and starts a fresh turn on a
            # mismatch. NULL = pre-migration row of unknown provenance.
            cx.execute("ALTER TABLE tasks ADD COLUMN session_backend TEXT")
        # Pipeline mode (multi-stage role agents). All additive; simple-mode
        # rows keep mode='simple' and never touch the other columns.
        if "mode" not in cols:
            cx.execute("ALTER TABLE tasks ADD COLUMN mode TEXT NOT NULL DEFAULT 'simple'")
        if "plan_json" not in cols:
            # {exit_criteria[], max_rounds, max_stages, rationale} — the
            # stages themselves live in task_stages.
            cx.execute("ALTER TABLE tasks ADD COLUMN plan_json TEXT")
        if "autopilot" not in cols:
            cx.execute("ALTER TABLE tasks ADD COLUMN autopilot TEXT NOT NULL DEFAULT 'off'")
        if "round_count" not in cols:
            cx.execute("ALTER TABLE tasks ADD COLUMN round_count INTEGER NOT NULL DEFAULT 0")
        if "budget_usd" not in cols:
            cx.execute("ALTER TABLE tasks ADD COLUMN budget_usd REAL")
        if "stop_reason" not in cols:
            cx.execute("ALTER TABLE tasks ADD COLUMN stop_reason TEXT")
        if "plan_model" not in cols:
            # Per-task model for the orchestrator (plan / re-plan runs), set
            # from the Tasks UI like refine_model / execute_model. NULL =
            # worker env WORKER_ORCHESTRATOR_MODEL, else the role default.
            cx.execute("ALTER TABLE tasks ADD COLUMN plan_model TEXT")
        if "paused_reason" not in cols:
            # agent-team budget pacer: why autopilot parked this task at
            # plan_ready instead of starting the next stage (NULL = not
            # paused). The web app's resume loop starts it once budget is free.
            cx.execute("ALTER TABLE tasks ADD COLUMN paused_reason TEXT")
        scols = {r[1] for r in cx.execute("PRAGMA table_info(task_stages)").fetchall()}
        if scols and "tokens" not in scols:
            # input+output tokens of the stage's agent run(s), summed from the
            # `result` events' usage. Codex gives no $ figure unless a price
            # table is configured, so tokens are its primary cost signal.
            cx.execute("ALTER TABLE task_stages ADD COLUMN tokens INTEGER")
        if scols and "options_json" not in scols:
            # needs_input: tap-able answers the stage offered (Tasks v2 chat).
            cx.execute("ALTER TABLE task_stages ADD COLUMN options_json TEXT")
        if "proposed_at" not in cols:
            # Tasks v2 lead: when the lead recommended the task for start
            # ("Zur Freigabe") and why (lead_note; also the park reason).
            cx.execute("ALTER TABLE tasks ADD COLUMN proposed_at TEXT")
            cx.execute("ALTER TABLE tasks ADD COLUMN lead_note TEXT")
        if "user_summary" not in cols:
            cx.execute("ALTER TABLE tasks ADD COLUMN user_summary TEXT")
        # Tasks v2: no simple-mode execute any more; two autopilot levels.
        cx.execute("UPDATE tasks SET status='failed', error='Execute entfällt (Tasks v2) — "
                   "bitte als Pipeline starten' WHERE status='executing'")
        cx.execute("UPDATE tasks SET autopilot='full-bots' WHERE autopilot IN ('advise','full')")
        # Merged tasks used to sit in the backlog ("later"); they are closed.
        cx.execute("UPDATE tasks SET status='closed' WHERE status='backlog' AND lead_note LIKE 'Zusammengelegt in #%'")
    finally:
        cx.close()
    _ensured.add(path)
    return path


def _plan_from_json(raw: Optional[str]) -> dict:
    """plan_json with defaults filled in, for the UI and the gate logic."""
    plan: dict = {}
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                plan = loaded
        except Exception:
            plan = {}
    crit = plan.get("exit_criteria")
    nr = plan.get("new_repo")
    new_repo = nr if isinstance(nr, dict) and str(nr.get("name") or "").strip() else None
    return {
        "exit_criteria": [str(c) for c in crit] if isinstance(crit, list) else [],
        "max_rounds": int(plan.get("max_rounds") or DEFAULT_MAX_ROUNDS),
        "max_stages": int(plan.get("max_stages") or DEFAULT_MAX_STAGES),
        "rationale": str(plan.get("rationale") or ""),
        # Orchestrator's proposal for a brand-new sub-repo (name/based_on/
        # reason) when nothing existing fits; the implement stages scaffold it.
        "new_repo": new_repo,
    }


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["repos"] = json.loads(d.pop("repos_json") or "[]")
    d["plan"] = _plan_from_json(d.get("plan_json"))
    return d


def list_tasks(status: Optional[str] = None) -> list[dict]:
    sql = "SELECT * FROM tasks"
    args: tuple = ()
    if status:
        sql += " WHERE status = ?"
        args = (status,)
    else:
        # Default (active) list hides parked backlog, done and closed tasks —
        # each gets its own tab in the UI. Fetch them explicitly by status.
        sql += " WHERE status NOT IN ('backlog','done','closed')"
    # Active tasks (anything but done/failed) first, then by priority, then most recent first.
    sql += " ORDER BY (status IN ('done','failed')) ASC, prio ASC, updated_at DESC"
    with _connect() as cx:
        return [_row_to_dict(r) for r in cx.execute(sql, args).fetchall()]


def recent_done(days: int) -> list[dict]:
    """Done tasks touched in the last ``days`` days, newest first (board column)."""
    cutoff = datetime.now(BERLIN) - timedelta(days=max(0, days))
    with _connect() as cx:
        rows = cx.execute("SELECT * FROM tasks WHERE status = 'done' ORDER BY updated_at DESC").fetchall()
    out = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["updated_at"])
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=BERLIN)
        if ts >= cutoff:
            out.append(_row_to_dict(r))
    return out


def status_counts() -> dict[str, int]:
    with _connect() as cx:
        return {r[0]: r[1] for r in cx.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")}


def stage_progress(task_ids: list[int]) -> dict[int, dict]:
    """Per task: stages done / total (skipped not counted) and the current
    stage — the one running, waiting or failed, else the next pending one."""
    if not task_ids:
        return {}
    marks = ",".join("?" * len(task_ids))
    with _connect() as cx:
        rows = cx.execute(
            f"SELECT task_id, seq, role, status FROM task_stages WHERE task_id IN ({marks}) "
            "ORDER BY task_id, seq", tuple(task_ids)).fetchall()
    by: dict[int, list] = {}
    for r in rows:
        by.setdefault(r["task_id"], []).append(r)
    out = {}
    for tid, stages in by.items():
        live = [s for s in stages if s["status"] != "skipped"]
        if not live:
            continue
        cur = (next((s for s in live if s["status"] in ("running", "awaiting_input", "failed")), None)
               or next((s for s in live if s["status"] == "pending"), None))
        out[tid] = {
            "done": sum(1 for s in live if s["status"] == "done"),
            "total": len(live),
            "current": None if cur is None else {
                "seq": cur["seq"], "index": live.index(cur) + 1, "role": cur["role"], "status": cur["status"]},
        }
    return out


def get_task(task_id: int) -> Optional[dict]:
    with _connect() as cx:
        row = cx.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_dict(row) if row else None


def create_task(
    title: str,
    details: str,
    prio: int,
    repos: list[str],
    refine_model: Optional[str] = None,
    execute_model: Optional[str] = None,
    plan_model: Optional[str] = None,
) -> dict:
    if prio not in VALID_PRIO:
        raise ValueError(f"invalid prio: {prio}")
    now = _now()
    with _connect() as cx:
        cur = cx.execute(
            "INSERT INTO tasks "
            "(title, details, prio, repos_json, status, created_at, updated_at, "
            " refine_model, execute_model, plan_model) "
            "VALUES (?, ?, ?, ?, 'new', ?, ?, ?, ?, ?)",
            (title, details, prio, json.dumps(repos), now, now,
             refine_model or None, execute_model or None, plan_model or None),
        )
        task_id = cur.lastrowid
    result = get_task(task_id)
    assert result is not None
    return result


def update_task(task_id: int, **fields) -> Optional[dict]:
    """Update editable fields. Returns None if task missing or not editable."""
    allowed = {"title", "details", "prio", "repos", "refine_model", "execute_model", "plan_model"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"cannot update fields: {sorted(bad)}")
    if "prio" in fields and fields["prio"] not in VALID_PRIO:
        raise ValueError(f"invalid prio: {fields['prio']}")
    sets: list[str] = []
    args: list = []
    for k, v in fields.items():
        if k == "repos":
            sets.append("repos_json = ?")
            args.append(json.dumps(v))
        elif k in ("refine_model", "execute_model", "plan_model"):
            sets.append(f"{k} = ?")
            args.append((v or None) if isinstance(v, str) else v)
        else:
            sets.append(f"{k} = ?")
            args.append(v)
    sets.append("updated_at = ?")
    args.append(_now())
    args.append(task_id)
    placeholders = ",".join(f"'{s}'" for s in EDITABLE_STATUS)
    with _connect() as cx:
        cur = cx.execute(
            f"UPDATE tasks SET {', '.join(sets)} "
            f"WHERE id = ? AND status IN ({placeholders})",
            args,
        )
        if cur.rowcount == 0:
            return None
    return get_task(task_id)


def delete_task(task_id: int) -> bool:
    """Delete a task unless it is currently being processed by the agent."""
    placeholders = ",".join(f"'{s}'" for s in BUSY_STATUS)
    with _connect() as cx:
        cur = cx.execute(
            f"DELETE FROM tasks WHERE id = ? AND status NOT IN ({placeholders})",
            (task_id,),
        )
        if cur.rowcount > 0:
            cx.execute("DELETE FROM task_stages WHERE task_id = ?", (task_id,))
        return cur.rowcount > 0


# ---- Status transitions used by the agent worker --------------------------
# The webapp flips a task into `refining` to request work;
# the project's worker picks it up, streams events into
# sessions/<id>/refine.jsonl, and on completion sets `refined` (+ spec) or

def request_refine(task_id: int) -> Optional[dict]:
    """Flip task to 'refining' if it is in a state where refine makes sense
    (new / refined / failed / limit_reached). For a fresh refine we clear
    the prior spec/error so the worker's 'pending' filter picks the task
    up again. For a limit-reached retry where a chat reply was queued
    (pending_reply set), we PRESERVE refined_spec so the resumed turn
    builds on the same snapshot — losing it would force a fresh refine
    instead of a continuation."""
    eligible = "('new','refined','failed','limit_reached')"
    now = _now()
    with _connect() as cx:
        row = cx.execute(
            "SELECT status, pending_reply FROM tasks WHERE id=?",
            (task_id,),
        ).fetchone()
        if not row:
            return None
        if row[0] not in {"new", "refined", "failed", "limit_reached"}:
            return None
        keep_spec = bool((row[1] or "").strip())  # active reply continuation
        if keep_spec:
            cur = cx.execute(
                f"UPDATE tasks SET status='refining', updated_at=?, error=NULL "
                f"WHERE id=? AND status IN {eligible}",
                (now, task_id),
            )
        else:
            cur = cx.execute(
                f"UPDATE tasks SET status='refining', updated_at=?, "
                f"refined_spec=NULL, error=NULL "
                f"WHERE id=? AND status IN {eligible}",
                (now, task_id),
            )
        if cur.rowcount == 0:
            return None
    return get_task(task_id)


def claim_pending(kind: str) -> Optional[dict]:
    """Pick the oldest task parked in 'refining' that has *not yet* been
    completed (no spec written). `kind` is 'refine' (the only simple-mode
    agent run left since Tasks v2)."""
    if kind == "refine":
        status = "refining"
        done_col = "refined_spec"
    else:
        raise ValueError(f"unknown kind: {kind!r}")
    with _connect() as cx:
        row = cx.execute(
            f"SELECT * FROM tasks WHERE status=? AND ({done_col} IS NULL OR {done_col}='') "
            f"ORDER BY prio ASC, updated_at ASC LIMIT 1",
            (status,),
        ).fetchone()
        return _row_to_dict(row) if row else None


def complete_refine(task_id: int, refined_spec: str) -> Optional[dict]:
    """Worker writes the refined spec and flips to 'refined'. Also clears
    pending_reply (so a reply turn that just landed doesn't get re-picked
    up) and last_busy_kind (no stale Retry-routing hint)."""
    now = _now()
    with _connect() as cx:
        cur = cx.execute(
            "UPDATE tasks SET refined_spec=?, status='refined', "
            "updated_at=?, error=NULL, pending_reply=NULL, "
            "last_busy_kind=NULL "
            "WHERE id=? AND status='refining'",
            (refined_spec, now, task_id),
        )
        if cur.rowcount == 0:
            return None
    return get_task(task_id)


def set_session_id(
    task_id: int,
    session_id: Optional[str],
    backend: Optional[str] = None,
) -> bool:
    """Internal — bypasses EDITABLE_STATUS so the worker can persist the
    agent session id during refining. Returns True iff the row exists.

    `backend` records which agent produced the id ('claude' = Claude SDK
    session, 'codex' = Codex thread). The worker needs it because the two
    id spaces are not interchangeable; passing None leaves the stored
    value untouched so an older worker can't blank it out."""
    now = _now()
    sets = "session_id=?, updated_at=?"
    args: list = [session_id, now]
    if backend is not None:
        sets += ", session_backend=?"
        args.append(backend or None)
    args.append(task_id)
    with _connect() as cx:
        cur = cx.execute(f"UPDATE tasks SET {sets} WHERE id=?", args)
        return cur.rowcount > 0


def queue_reply(task_id: int, text: str) -> Optional[dict]:
    """User-initiated chat continuation on a refined task. Stores the
    user's message in pending_reply and flips status='refining' so the
    worker picks the task up as a continuation (resume=session_id),
    NOT a fresh refine. refined_spec is preserved — the conversation
    keeps building on it. Requires a session_id to be present."""
    now = _now()
    with _connect() as cx:
        cur = cx.execute(
            "UPDATE tasks SET pending_reply=?, status='refining', "
            "updated_at=?, error=NULL "
            "WHERE id=? AND status='refined' "
            "  AND session_id IS NOT NULL AND length(session_id) > 0",
            (text, now, task_id),
        )
        if cur.rowcount == 0:
            return None
    return get_task(task_id)


def mark_done(task_id: int) -> Optional[dict]:
    """Manually mark a task as 'done' without going through the execute
    agent. Useful when the user did the work outside the loop (manual
    code change, decided no action needed, etc.).

    Allowed from any non-terminal state EXCEPT a live agent run
    (``refining``/``executing``) — those need to be cancelled/cleared
    first so we don't race the worker callback. Clears ``error`` and
    ``last_busy_kind``; refined_spec and execute_summary stay as history.
    """
    now = _now()
    with _connect() as cx:
        cur = cx.execute(
            "UPDATE tasks SET status='done', updated_at=?, "
            "error=NULL, last_busy_kind=NULL, stop_reason=NULL "
            "WHERE id=? AND status IN ('new','refined','failed','limit_reached',"
            "  'plan_ready','awaiting_input','blocked')",
            (now, task_id),
        )
        if cur.rowcount == 0:
            return None
    return get_task(task_id)


def reopen_task(task_id: int) -> Optional[dict]:
    """Flip a terminal task (done/failed) back into the editable cycle.
    If a refined_spec exists, the task lands in 'refined' — both Refine
    and Execute are immediately available, since the spec is still a
    valid contract. Otherwise it lands in 'new' (no spec to act on).
    refined_spec and execute_summary stay in place as history; they are
    overwritten naturally by the next request_refine / pipeline run."""
    now = _now()
    with _connect() as cx:
        cur = cx.execute(
            "UPDATE tasks SET error=NULL, stop_reason=NULL, updated_at=?, status=CASE "
            "  WHEN mode='pipeline' THEN 'plan_ready' "
            "  WHEN refined_spec IS NOT NULL AND length(refined_spec) > 0 "
            "    THEN 'refined' "
            "  ELSE 'new' "
            "END "
            "WHERE id=? AND status IN ('done','failed')",
            (now, task_id),
        )
        if cur.rowcount == 0:
            return None
    return get_task(task_id)


def move_to_backlog(task_id: int) -> Optional[dict]:
    """Park a task in the backlog — out of the active list but not deleted.

    Allowed from the editable, non-busy states (new / refined / failed /
    limit_reached). Refuses while an agent run is live (refining/executing)
    and from terminal 'done'. All state (refined_spec, session_id, error,
    …) is preserved untouched so a later restore picks up exactly where the
    task left off."""
    now = _now()
    with _connect() as cx:
        cur = cx.execute(
            "UPDATE tasks SET status='backlog', proposed_at=NULL, updated_at=? "
            "WHERE id=? AND status IN ('new','refined','failed','limit_reached',"
            "  'plan_ready','awaiting_input','blocked')",
            (now, task_id),
        )
        if cur.rowcount == 0:
            return None
    return get_task(task_id)


def set_user_summary(task_id: int, text: str) -> Optional[dict]:
    """The plain-language summary for the user (task chat, task details);
    execute_summary stays the technical record for lead and scouts."""
    with _connect() as cx:
        cx.execute("UPDATE tasks SET user_summary=? WHERE id=?", (text, task_id))
    return get_task(task_id)


def propose_task(task_id: int, note: str) -> Optional[dict]:
    """The lead recommends starting this task ("Zur Freigabe"). Only ideas
    (new / refined); the start itself stays the user's click."""
    now = _now()
    with _connect() as cx:
        cur = cx.execute(
            "UPDATE tasks SET proposed_at=?, lead_note=?, updated_at=? "
            "WHERE id=? AND status IN ('new','refined')",
            (now, note, now, task_id),
        )
        if cur.rowcount == 0:
            return None
    return get_task(task_id)


def park_task(task_id: int, reason: str) -> Optional[dict]:
    """The lead parks a task (duplicate / obsolete) with a reason."""
    t = move_to_backlog(task_id)
    if t is None:
        return None
    with _connect() as cx:
        cx.execute("UPDATE tasks SET lead_note=? WHERE id=?", (reason, task_id))
    return get_task(task_id)


def close_task(task_id: int, reason: str) -> Optional[dict]:
    """Close a task that will not be done (obsolete, duplicate, solved
    elsewhere) — kept for reference with the reason, reopenable."""
    now = _now()
    with _connect() as cx:
        cur = cx.execute(
            "UPDATE tasks SET status='closed', proposed_at=NULL, lead_note=?, updated_at=?, "
            "error=NULL, last_busy_kind=NULL "
            "WHERE id=? AND status IN ('new','refined','failed','limit_reached','backlog',"
            "  'plan_ready','awaiting_input','blocked')",
            (reason, now, task_id),
        )
        if cur.rowcount == 0:
            return None
    return get_task(task_id)


def merge_task(task_id: int, into: int, note: str = "") -> Optional[dict]:
    """Fold an idea into another task: its title/details are appended to
    ``into``'s details, then it is closed with a pointer. Returns ``into``."""
    src, dst = get_task(task_id), get_task(into)
    if not src or not dst or task_id == into:
        return None
    # An idea only, never twice (a merged task is closed).
    if src["status"] not in ("new", "refined") or dst["status"] in BUSY_STATUS:
        return None
    body = (src.get("details") or "").strip()
    add = f"\n\n---\nAus #{task_id} übernommen: {src['title']}" + (f"\n{body}" if body else "")
    now = _now()
    with _connect() as cx:
        cx.execute("UPDATE tasks SET details=details||?, updated_at=? WHERE id=?", (add, now, into))
        cx.execute("UPDATE tasks SET status='closed', proposed_at=NULL, lead_note=?, updated_at=? WHERE id=?",
                   (f"Zusammengelegt in #{into}" + (f": {note}" if note else ""), now, task_id))
    return get_task(into)


def restore_from_backlog(task_id: int) -> Optional[dict]:
    """Bring a parked or closed task back into the active list. Mirrors
    reopen_task: if a refined_spec is still present the task lands in
    'refined', otherwise 'new'. Only acts on 'backlog' / 'closed'."""
    now = _now()
    with _connect() as cx:
        cur = cx.execute(
            "UPDATE tasks SET updated_at=?, status=CASE "
            "  WHEN mode='pipeline' THEN 'plan_ready' "
            "  WHEN refined_spec IS NOT NULL AND length(refined_spec) > 0 "
            "    THEN 'refined' "
            "  ELSE 'new' "
            "END "
            "WHERE id=? AND status IN ('backlog','closed')",
            (now, task_id),
        )
        if cur.rowcount == 0:
            return None
    return get_task(task_id)


def mark_limit_reached(task_id: int, error: str, kind: Optional[str] = None) -> Optional[dict]:
    """Worker reports an Anthropic usage-limit hit. Distinct from failed:
    we keep refined_spec, pending_reply, and session_id so a retry after
    the monthly limit resets can resume from exactly where we stopped.
    `kind` ('refine' or 'execute') records which agent was running so
    the UI's Retry button dispatches to the right endpoint."""
    now = _now()
    err = (error or "").strip()[:4000]
    safe_kind = kind if is_stream_kind(kind) else None
    with _connect() as cx:
        cur = cx.execute(
            "UPDATE tasks SET status='limit_reached', error=?, "
            "updated_at=?, last_busy_kind=COALESCE(?, last_busy_kind) "
            "WHERE id=? AND status IN ('refining','executing','planning','running')",
            (err, now, safe_kind, task_id),
        )
        if cur.rowcount == 0:
            return None
        # Pipeline: never leave a stage in 'running' — the stream would never
        # close and a later Retry would have nothing well-defined to resume.
        # The stage keeps its session_id; retry_stage starts it again.
        cx.execute(
            "UPDATE task_stages SET status='failed', error=?, finished_at=?, "
            "updated_at=? WHERE task_id=? AND status='running'",
            (err, now, now, task_id),
        )
    return get_task(task_id)


def fail_task(task_id: int, error: str) -> Optional[dict]:
    """Mark a task as failed (from any busy state). Idempotent on terminal.
    Also drops pending_reply so the worker won't retry the queued reply."""
    now = _now()
    err = (error or "").strip()[:4000]
    with _connect() as cx:
        cur = cx.execute(
            "UPDATE tasks SET status='failed', error=?, updated_at=?, "
            "pending_reply=NULL WHERE id=?",
            (err, now, task_id),
        )
        if cur.rowcount == 0:
            return None
    return get_task(task_id)


# ---- Session JSONL paths --------------------------------------------------
# Per-task agent event streams live next to the project's SQLite DB.


def session_jsonl_path(task_id: int, kind: str) -> Path:
    """Return <project data>/sessions/<id>/<kind>.jsonl. `kind` is 'refine',
    'execute', 'plan' or 'stage_<seq>' (see STREAM_KIND_RE). Creates the
    parent dir."""
    if not is_stream_kind(kind):
        raise ValueError(f"unknown kind: {kind!r}")
    d = sessions_root() / str(task_id)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{kind}.jsonl"


def session_dir(task_id: int) -> Path:
    return sessions_root() / str(task_id)


# ===========================================================================
# Pipeline mode — role-based multi-stage runs
# ===========================================================================
# A task with mode='pipeline' is an ordered list of task_stages rows. The
# orchestrator (worker, kind='plan') proposes them; the user edits/approves
# in the UI; the worker runs one stage at a time (kind='stage_<seq>'). All
# gate and stop rules live HERE, not in prompts, so they are testable and a
# confused agent can never advance the pipeline by itself:
#
#   * a stage result that does not parse => needs_input (never auto-advance)
#   * team.yaml follow_up: after every stage of role X a stage of role Y must
#     follow (e.g. backtest-engineer -> backtest-auditor); team.yaml
#     final: the plan always ends with that role (e.g. reviewer),
#     appended if missing
#   * done only when the final role passed AND, for every follow-up pair, the
#     last Y after the last X passed
#   * fail => round_count += 1 and the orchestrator re-plans the pending
#     tail; > max_rounds, > max_stages, budget exceeded, or the same role
#     citing the same unresolved finding twice => blocked
#   * autopilot never starts an implement stage on its own when the task
#     touches a live-bot repo — except at level 'full-bots', whose implement
#     stages the worker runs hands-off (no commit, no running containers)
#
# Every multi-statement transition runs inside one BEGIN IMMEDIATE
# transaction (_tx) because _connect() is autocommit-per-statement.

PLAN_ELIGIBLE_STATUS = ("new", "refined", "failed", "limit_reached", "plan_ready", "blocked")
PLAN_EDITABLE_STATUS = ("plan_ready", "awaiting_input", "blocked", "failed")

@contextmanager
def _tx() -> Iterator[sqlite3.Connection]:
    """One BEGIN IMMEDIATE transaction. A concurrent PUT /plan or /stop must
    not interleave with complete_stage's read-decide-write sequence."""
    with _connect() as cx:
        cx.execute("BEGIN IMMEDIATE")
        try:
            yield cx
        except BaseException:
            cx.execute("ROLLBACK")
            raise
        else:
            cx.execute("COMMIT")


def _task_row(cx: sqlite3.Connection, task_id: int) -> Optional[dict]:
    row = cx.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _row_to_dict(row) if row else None


def _load_list(raw: Optional[str]) -> list:
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except Exception:
        return []
    return v if isinstance(v, list) else []


def _stage_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["findings"] = _load_list(d.pop("findings_json", None))
    d["questions"] = _load_list(d.pop("questions_json", None))
    d["options"] = _load_list(d.pop("options_json", None))
    d["metered"] = bool(d.get("metered"))
    return d


def _stage_rows(cx: sqlite3.Connection, task_id: int) -> list[dict]:
    rows = cx.execute(
        "SELECT * FROM task_stages WHERE task_id=? ORDER BY seq", (task_id,)
    ).fetchall()
    return [_stage_row_to_dict(r) for r in rows]


def _stage_row(cx: sqlite3.Connection, task_id: int, seq: int) -> Optional[dict]:
    row = cx.execute(
        "SELECT * FROM task_stages WHERE task_id=? AND seq=?", (task_id, seq)
    ).fetchone()
    return _stage_row_to_dict(row) if row else None


def list_stages(task_id: int) -> list[dict]:
    with _connect() as cx:
        return _stage_rows(cx, task_id)


def get_stage(task_id: int, seq: int) -> Optional[dict]:
    with _connect() as cx:
        return _stage_row(cx, task_id, seq)


def _set_task(cx: sqlite3.Connection, task_id: int, now: str, **fields: Any) -> None:
    sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
    cx.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), now, task_id))


def _normalise_stages(stages: list[dict]) -> list[dict]:
    """Validate roles against the catalog and enforce the plan invariants on
    a (pending) tail: every follow-up trigger (e.g. backtest-engineer)
    is eventually followed by its follow-up role (backtest-auditor), and the
    tail ends with the final role (reviewer). Returns
    [{role, kind, goal, model}]; raises TransitionError on an unknown role."""
    out: list[dict] = []
    for s in stages or []:
        role = agent_roles.get_role(str((s or {}).get("role") or ""))
        if role is None or not role.is_stage:
            raise TransitionError(f"unknown stage role: {(s or {}).get('role')!r}")
        model = s.get("model")
        model = model.strip() if isinstance(model, str) else None
        out.append({
            "role": role.id,
            "kind": role.kind,
            "goal": str(s.get("goal") or "").strip(),
            "model": model or None,
        })
    if not out:
        return out
    by_id = agent_roles.ROLE_BY_ID
    for trigger, rule in agent_roles.FOLLOW_UP.items():
        open_trigger = False
        for s in out:
            if s["role"] == trigger:
                open_trigger = True
            elif s["role"] == rule.role:
                open_trigger = False
        if open_trigger:
            r = by_id[rule.role]
            out.append({"role": r.id, "kind": r.kind, "goal": rule.goal, "model": None})
    final = agent_roles.FINAL
    if final is not None:
        # "… → deploy → review" (the planner put the deploy first): the user
        # must approve a reviewed result, so the trailing review moves before
        # the deploy block. Paper flows (deploy → paper-reviewer → …) keep
        # their order: there the deploy is not directly before the end.
        k = len(out)
        while k > 0 and out[k - 1]["role"] == final.role:
            k -= 1
        m = k
        while m > 0 and out[m - 1]["role"] in agent_roles.FINAL_MAY_FOLLOW:
            m -= 1
        if k < len(out) and m < k:
            out = out[:m] + out[k:] + out[m:k]
        tail = [s["role"] for s in out]
        follow_ok = agent_roles.FINAL_MAY_FOLLOW
        if tail[-1] in follow_ok:
            # e.g. … reviewer → release-manager: the review must come before
            # the first trailing stage.
            first = next(i for i in range(len(tail)) if all(r in follow_ok for r in tail[i:]))
            if final.role not in tail[:first]:
                r = by_id[final.role]
                out.insert(first, {"role": r.id, "kind": r.kind, "goal": final.goal, "model": None})
        elif tail[-1] != final.role:
            r = by_id[final.role]
            out.append({"role": r.id, "kind": r.kind, "goal": final.goal, "model": None})
    return out


def _insert_stages(
    cx: sqlite3.Connection, task_id: int, stages: list[dict], start_seq: int, now: str
) -> None:
    for i, s in enumerate(stages):
        cx.execute(
            "INSERT INTO task_stages (task_id, seq, role, kind, goal, model, status, "
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (task_id, start_seq + i, s["role"], s["kind"], s["goal"], s["model"], now, now),
        )


def _finish_rule(stages: list[dict]) -> tuple[bool, str]:
    """done iff the final completed stage is a passing final role (e.g.
    reviewer) and, for every follow-up pair that ran (backtest-engineer ->
    backtest-auditor), a follow-up stage completed after the last trigger
    with verdict pass."""
    done = [s for s in stages if s["status"] == "done"]
    if not done:
        return False, "no completed stages"
    last = max(done, key=lambda s: s["seq"])
    final = agent_roles.FINAL
    if final is not None and last["role"] in agent_roles.FINAL_MAY_FOLLOW:
        # A trailing stage (deploy) after the final review: it must pass, and
        # so must the latest final-role stage before it.
        if last["verdict"] != "pass":
            return False, f"{last['role']} verdict is {last['verdict']!r}"
        finals = [s for s in done if s["role"] == final.role and s["seq"] < last["seq"]]
        if not finals:
            return False, f"no {final.role} completed before {last['role']}"
        last = max(finals, key=lambda s: s["seq"])
    if final is not None:
        if last["role"] != final.role:
            return False, f"the final completed stage is not a {final.role}"
        if last["verdict"] != "pass":
            return False, f"{final.role} verdict is {last['verdict']!r}"
    elif last["verdict"] != "pass":
        return False, f"{last['role']} verdict is {last['verdict']!r}"
    for trigger, rule in agent_roles.FOLLOW_UP.items():
        trig = [s for s in done if s["role"] == trigger]
        if not trig:
            continue
        last_t = max(s["seq"] for s in trig)
        fol = [s for s in done if s["role"] == rule.role and s["seq"] > last_t]
        if not fol:
            return False, f"no {rule.role} completed after the last {trigger} stage"
        if max(fol, key=lambda s: s["seq"])["verdict"] != "pass":
            return False, f"the latest {rule.role} verdict is not 'pass'"
    return True, ""


def _pipeline_summary(stages: list[dict]) -> str:
    lines = ["# Pipeline summary"]
    for s in stages:
        if s["status"] == "pending":
            continue
        head = f"- stage {s['seq']} {s['role']}: {s['status']}"
        if s.get("verdict"):
            head += f"/{s['verdict']}"
        summ = (s.get("summary") or s.get("error") or "").strip().replace("\n", " ")
        if summ:
            head += f" — {summ}"
        lines.append(head)
    return "\n".join(lines)


def _usage_tokens(usage: Any) -> int:
    if not isinstance(usage, dict):
        return 0
    n = 0
    for key in ("input_tokens", "output_tokens"):
        try:
            n += int(usage.get(key) or 0)
        except (TypeError, ValueError):
            pass
    return n


def _cost_from_jsonl(
    task_id: int, kind: str,
) -> tuple[float, bool, Optional[int], Optional[int], Optional[int]]:
    """(cost_usd, priced, duration_ms, num_turns, tokens) summed over the
    `result` events of one stream. `priced` is True when at least one result
    carried a $ figure — Claude always does; Codex only when the worker has
    a price table (WORKER_CODEX_PRICES), otherwise tokens are its signal."""
    path = sessions_root() / str(task_id) / f"{kind}.jsonl"
    if not path.exists():
        return 0.0, False, None, None, None
    total = 0.0
    metered = False
    duration: Optional[int] = None
    turns: Optional[int] = None
    tokens: Optional[int] = None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0.0, False, None, None, None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("event") != "result":
            continue
        cost = obj.get("cost_usd")
        if cost is not None:
            metered = True
            try:
                total += float(cost)
            except (TypeError, ValueError):
                pass
        duration = obj.get("duration_ms", duration)
        turns = obj.get("num_turns", turns)
        t = _usage_tokens(obj.get("usage"))
        if t:
            tokens = (tokens or 0) + t
    return total, metered, duration, turns, tokens


# Budget gate for autopilot chaining (agent-team's pacer; set by app.py).
# Returns (allowed, reason). Default: always allowed (tests, no pacer).
budget_gate = lambda: (True, "")  # noqa: E731


def _set_paused(cx: sqlite3.Connection, task_id: int, reason: Optional[str]) -> None:
    """paused_reason without touching updated_at: the resume loop re-parks
    every few minutes, and a new updated_at would re-announce the task in
    the inbox (push) each time."""
    cx.execute("UPDATE tasks SET paused_reason=? WHERE id=?", (reason, task_id))


def _wait_until(cx: sqlite3.Connection, task_id: int, stage: dict) -> Optional[datetime]:
    """For a WAIT_ROLES stage: earliest start (previous stage's finish +
    days), or None when it may start now."""
    days = agent_roles.WAIT_ROLES.get(stage.get("role"))
    if not days:
        return None
    prev = [s for s in _stage_rows(cx, task_id)
            if s["seq"] < stage["seq"] and s["status"] == "done" and s.get("finished_at")]
    if not prev:
        return None
    from datetime import timedelta
    ready = datetime.fromisoformat(max(prev, key=lambda s: s["seq"])["finished_at"]) + timedelta(days=days)
    return ready if datetime.now(BERLIN) < ready else None


def _autopilot_next(cx: sqlite3.Connection, task: dict, next_stage: dict, now: str) -> bool:
    """Start ``next_stage`` if autopilot may; if only a wait period or the
    budget says no, park the task with paused_reason so the resume loop
    picks it up later. Returns True when the stage was started."""
    if not _autopilot_allows(task, next_stage):
        return False
    wait = _wait_until(cx, task["id"], next_stage)
    if wait is not None:
        _set_paused(cx, task["id"], f"Wartet bis {wait.isoformat(timespec='minutes')}: {next_stage['role']}")
        return False
    ok, reason = budget_gate()
    if not ok:
        _set_paused(cx, task["id"], f"Budget: {reason}")
        return False
    _set_paused(cx, task["id"], None)
    _start_stage(cx, task["id"], next_stage, now)
    return True


def _autopilot_allows(task: dict, next_stage: dict) -> bool:
    """May the pipeline start `next_stage` without a click? Implement stages
    need level 'full' (never on a live-bot repo) or 'full-bots' (live-bot
    repos too; the worker runs those stages hands-off). Neither chains an
    unscoped task (the worker's global execute lock) or an unknown repo."""
    level = (task.get("autopilot") or "off").strip().lower()
    if level == "off":
        return False
    if next_stage.get("role") in agent_roles.CONFIRM_ROLES:
        return False  # e.g. deploy: always the user's click
    if next_stage["kind"] == agent_roles.KIND_IMPLEMENT:
        if level not in ("full", "full-bots"):
            return False
        repos = set(task.get("repos") or [])
        # Unscoped (global execute lock) or a repo we don't know (a proposed
        # new bot repo) => the user clicks, at every level.
        if not repos or not repos <= agent_roles.KNOWN_REPOS:
            return False
        if level == "full" and (repos & agent_roles.LIVE_BOT_REPOS):
            return False
    return True


def _start_stage(
    cx: sqlite3.Connection, task_id: int, stage: dict, now: str,
    pending_answer: Optional[str] = None,
) -> None:
    cx.execute(
        "UPDATE task_stages SET status='running', attempt=attempt+1, started_at=?, "
        "finished_at=NULL, error=NULL, pending_answer=?, updated_at=? WHERE id=?",
        (now, pending_answer, now, stage["id"]),
    )
    _set_task(
        cx, task_id, now, status="running", error=None, stop_reason=None,
        last_busy_kind=f"stage_{stage['seq']}",
    )


def _finish_or_regate(cx: sqlite3.Connection, task: dict, stages: list[dict], now: str) -> None:
    ok, reason = _finish_rule(stages)
    if ok:
        _set_task(
            cx, task["id"], now, status="done", execute_summary=_pipeline_summary(stages),
            error=None, stop_reason=None, last_busy_kind=None,
        )
    else:
        # Plan exhausted without meeting the finish rule: let the orchestrator
        # propose what is missing. complete_plan blocks the task if it has
        # nothing to add, so this cannot loop.
        _set_task(cx, task["id"], now, status="planning", last_busy_kind="plan", error=None)


# ---- User-driven transitions ---------------------------------------------

def request_plan(task_id: int) -> Optional[dict]:
    """Flip into pipeline mode and ask the orchestrator for a plan. From
    plan_ready/blocked this is a re-plan: the orchestrator sees the current
    pending tail and replaces it (done/failed/skipped stages are history)."""
    now = _now()
    with _tx() as cx:
        task = _task_row(cx, task_id)
        if not task or task["status"] not in PLAN_ELIGIBLE_STATUS:
            return None
        extra = {}
        if task["mode"] != "pipeline" and DEFAULT_AUTOPILOT in AUTOPILOT_LEVELS:
            extra["autopilot"] = DEFAULT_AUTOPILOT
        _set_task(
            cx, task_id, now, mode="pipeline", status="planning", stop_reason=None,
            error=None, last_busy_kind="plan", **extra,
        )
    return get_task(task_id)


def update_plan(
    task_id: int,
    stages: list[dict],
    *,
    exit_criteria: Optional[list[str]] = None,
    max_rounds: Optional[int] = None,
    max_stages: Optional[int] = None,
    budget_usd: Optional[float] = None,
) -> Optional[dict]:
    """User edit of the plan: replaces the pending tail (renumbered after the
    last non-pending stage) and the limits. Completed stages are immutable."""
    now = _now()
    with _tx() as cx:
        task = _task_row(cx, task_id)
        if not task or task["mode"] != "pipeline" or task["status"] not in PLAN_EDITABLE_STATUS:
            return None
        tail = _normalise_stages(stages)
        plan = task["plan"]
        rounds = int(max_rounds or plan["max_rounds"])
        stage_cap = int(max_stages or plan["max_stages"])
        if rounds < 1 or stage_cap < 1:
            raise TransitionError("max_rounds and max_stages must be >= 1")
        history = [s for s in _stage_rows(cx, task_id) if s["status"] != "pending"]
        if len(history) + len(tail) > stage_cap:
            raise TransitionError(
                f"plan would have {len(history) + len(tail)} stages, limit is {stage_cap}"
            )
        cx.execute("DELETE FROM task_stages WHERE task_id=? AND status='pending'", (task_id,))
        start_seq = (max(s["seq"] for s in history) if history else 0) + 1
        _insert_stages(cx, task_id, tail, start_seq, now)
        plan_json = json.dumps({
            "exit_criteria": list(exit_criteria) if exit_criteria is not None else plan["exit_criteria"],
            "max_rounds": rounds,
            "max_stages": stage_cap,
            "rationale": plan["rationale"],
            "new_repo": plan.get("new_repo"),
        })
        _set_task(cx, task_id, now, plan_json=plan_json, budget_usd=budget_usd)
    return get_task(task_id)


def request_next_stage(task_id: int) -> Optional[dict]:
    """Start the first pending stage. Refuses while a stage is failed or
    awaiting input (retry / answer / skip it first). With nothing pending the
    finish rule decides between done and a re-plan."""
    now = _now()
    with _tx() as cx:
        task = _task_row(cx, task_id)
        if not task or task["status"] != "plan_ready":
            return None
        stages = _stage_rows(cx, task_id)
        for s in stages:
            if s["status"] in ("failed", "awaiting_input"):
                raise TransitionError(
                    f"stage {s['seq']} ({s['role']}) is {s['status']} — retry, answer or skip it first"
                )
        pending = [s for s in stages if s["status"] == "pending"]
        if pending:
            wait = _wait_until(cx, task_id, pending[0])
            if wait is not None:
                raise TransitionError(
                    f"{pending[0]['role']} startet frühestens am {wait.strftime('%d.%m.%Y %H:%M')} "
                    f"(Mindestdauer seit der vorigen Stufe) — oder die Stufe überspringen"
                )
        _set_task(cx, task_id, now, paused_reason=None)
        if pending:
            _start_stage(cx, task_id, pending[0], now)
        else:
            _finish_or_regate(cx, task, stages, now)
    return get_task(task_id)


def answer_stage(task_id: int, seq: int, text: str) -> Optional[dict]:
    """Answer a stage's questions; the stage resumes (attempt+1) with the
    answer in pending_answer. Also valid from plan_ready so a task restored
    from backlog / reopened with an awaiting stage is not stranded."""
    now = _now()
    text = (text or "").strip()
    if not text:
        raise TransitionError("answer must not be empty")
    with _tx() as cx:
        task = _task_row(cx, task_id)
        if not task or task["status"] not in ("awaiting_input", "plan_ready"):
            return None
        stage = _stage_row(cx, task_id, seq)
        if not stage or stage["status"] != "awaiting_input":
            return None
        if any(s["status"] == "running" for s in _stage_rows(cx, task_id)):
            raise TransitionError("another stage is running")
        _start_stage(cx, task_id, stage, now, pending_answer=text)
    return get_task(task_id)


def skip_stage(task_id: int, seq: int) -> Optional[dict]:
    """Skip a pending / awaiting / failed stage. Gate roles (auditor,
    reviewer) cannot be skipped — that is the whole point of them."""
    now = _now()
    with _tx() as cx:
        task = _task_row(cx, task_id)
        if not task or task["status"] not in PLAN_EDITABLE_STATUS:
            return None
        stage = _stage_row(cx, task_id, seq)
        if not stage or stage["status"] not in ("pending", "awaiting_input", "failed"):
            return None
        if stage["role"] in agent_roles.GATE_ROLES:
            raise TransitionError(f"{stage['role']} is a gate stage and cannot be skipped")
        cx.execute(
            "UPDATE task_stages SET status='skipped', pending_answer=NULL, finished_at=?, "
            "updated_at=? WHERE id=?",
            (now, now, stage["id"]),
        )
        if task["status"] in ("awaiting_input", "failed"):
            remaining = [
                s for s in _stage_rows(cx, task_id)
                if s["status"] in ("awaiting_input", "failed")
            ]
            if not remaining:
                _set_task(cx, task_id, now, status="plan_ready", error=None)
    return get_task(task_id)


def stage_retryable(task_status: str, stage: dict, stages: list[dict]) -> bool:
    """Can the user run this stage again? A failed stage (agent crash, limit,
    user stop) or one with verdict `blocked`, as long as the task is halted.
    Verdict `fail` only on a stopped task and only for the stage it stopped
    at: there the cause can lie outside the code (a dashboard switch the user
    has since flipped). An older `fail` was already answered by later stages."""
    if task_status not in ("failed", "limit_reached", "plan_ready", "blocked"):
        return False
    if stage["status"] == "failed":
        return True
    if stage["status"] != "done":
        return False
    if stage.get("verdict") == "blocked":
        return True
    last = next((s for s in reversed(stages) if s["status"] not in ("pending", "skipped")), None)
    return (stage.get("verdict") == "fail" and task_status == "blocked"
            and last is not None and last["seq"] == stage["seq"])


def retry_stage(task_id: int, seq: int) -> Optional[dict]:
    """Re-run a stage the user may retry (see stage_retryable). Fresh attempt;
    the worker may resume the stored session."""
    now = _now()
    with _tx() as cx:
        task = _task_row(cx, task_id)
        if not task:
            return None
        stage = _stage_row(cx, task_id, seq)
        stages = _stage_rows(cx, task_id)
        if not stage or not stage_retryable(task["status"], stage, stages):
            return None
        if any(s["status"] == "running" for s in stages):
            raise TransitionError("another stage is running")
        _start_stage(cx, task_id, stage, now)
    return get_task(task_id)


def set_autopilot(task_id: int, level: str) -> Optional[dict]:
    level = (level or "").strip().lower()
    if level in ("advise", "full"):  # pre-v2 clients
        level = "full-bots"
    if level not in AUTOPILOT_LEVELS:
        raise TransitionError(f"invalid autopilot level {level!r}")
    now = _now()
    with _connect() as cx:
        cur = cx.execute(
            "UPDATE tasks SET autopilot=?, updated_at=? WHERE id=? AND mode='pipeline'",
            (level, now, task_id),
        )
        if cur.rowcount == 0:
            return None
    return get_task(task_id)


def stop_pipeline(task_id: int) -> Optional[dict]:
    """User stop. No way to cancel a live agent, so the running stage is
    marked failed and its late callback is rejected by the attempt guard
    (the attempt only changes on retry — the guard is the task status)."""
    now = _now()
    with _tx() as cx:
        task = _task_row(cx, task_id)
        if not task or task["mode"] != "pipeline" or task["status"] not in (
            "planning", "plan_ready", "running", "awaiting_input",
        ):
            return None
        cx.execute(
            "UPDATE task_stages SET status='failed', error='stopped by user', finished_at=?, "
            "pending_answer=NULL, updated_at=? WHERE task_id=? AND status='running'",
            (now, now, task_id),
        )
        _set_task(cx, task_id, now, status="blocked", stop_reason="stopped by user")
    return get_task(task_id)


def resume_pipeline(task_id: int) -> Optional[dict]:
    now = _now()
    with _connect() as cx:
        cur = cx.execute(
            "UPDATE tasks SET status='plan_ready', stop_reason=NULL, updated_at=? "
            "WHERE id=? AND status='blocked' AND mode='pipeline'",
            (now, task_id),
        )
        if cur.rowcount == 0:
            return None
    return get_task(task_id)


def _reconcile_repos(task: dict, parsed: dict) -> Optional[list[str]]:
    """Repos the orchestrator declared (like refine's `# Repos:` line):
    known names win (short forms tolerated, unknown ones dropped); a valid
    `new_repo` proposal is the only way an unknown name gets onto the task.
    Returns None when the plan said nothing about repos."""
    declared = parsed.get("repos")
    nr = parsed.get("new_repo")
    if declared is None and nr is None:
        return None
    out: list[str] = []
    for r in declared or []:
        n = agent_roles.normalise_repo(r)
        if n and n not in out:
            out.append(n)
    if nr:
        name = str(nr.get("name") or "").strip().lower()
        if (
            agent_roles.NEW_REPO_RE.match(name)
            and name not in agent_roles.KNOWN_REPOS
            and name not in out
        ):
            out.append(name)
    # A new-repo name the task already carries (accepted earlier) survives a
    # re-plan that doesn't mention it again.
    for r in task.get("repos") or []:
        if r not in agent_roles.KNOWN_REPOS and agent_roles.NEW_REPO_RE.match(r) and r not in out:
            out.append(r)
    if declared is None:
        # Only a new_repo was given: add it to the current selection.
        base = list(task.get("repos") or [])
        for r in out:
            if r not in base:
                base.append(r)
        return base
    return out


# ---- Worker callbacks ------------------------------------------------------

def complete_plan(task_id: int, plan_text: str) -> Optional[dict]:
    """Orchestrator finished. Initial plan: insert the stages, plan_ready.
    Gate (history exists): replace the pending tail; an empty tail means
    'nothing more to do' and is only accepted when the finish rule holds,
    otherwise the task blocks (no planning loop)."""
    now = _now()
    parsed = stage_result.parse_plan(plan_text)
    with _tx() as cx:
        task = _task_row(cx, task_id)
        if not task or task["status"] != "planning":
            return None
        if parsed is None:
            _set_task(
                cx, task_id, now, status="failed",
                error="Orchestrator produced no parseable `# Plan` JSON block — see the plan log, then Plan again.",
            )
            return _task_row(cx, task_id)
        stages_all = _stage_rows(cx, task_id)
        history = [s for s in stages_all if s["status"] != "pending"]
        gate = bool(history)
        plan = task["plan"]
        plan_json = json.dumps({
            "exit_criteria": parsed["exit_criteria"] or plan["exit_criteria"],
            # Limits are user territory once the plan exists: a re-gate must
            # not quietly raise them.
            "max_rounds": plan["max_rounds"] if gate else int(parsed["max_rounds"] or plan["max_rounds"]),
            "max_stages": plan["max_stages"] if gate else int(parsed["max_stages"] or plan["max_stages"]),
            "rationale": parsed["rationale"] or plan["rationale"],
            "new_repo": parsed.get("new_repo") or plan.get("new_repo"),
        })
        limits = _plan_from_json(plan_json)
        repos = _reconcile_repos(task, parsed)
        if repos is not None and sorted(repos) != sorted(task.get("repos") or []):
            _set_task(cx, task_id, now, repos_json=json.dumps(repos))
        cx.execute("DELETE FROM task_stages WHERE task_id=? AND status='pending'", (task_id,))
        if not parsed["stages"]:
            if not gate:
                _set_task(
                    cx, task_id, now, status="failed", plan_json=plan_json,
                    error="Orchestrator returned an empty plan — see the plan log, then Plan again.",
                )
                return _task_row(cx, task_id)
            ok, reason = _finish_rule(history)
            if ok and parsed["conclusion"] != "blocked":
                _set_task(
                    cx, task_id, now, status="done", plan_json=plan_json,
                    execute_summary=_pipeline_summary(history), error=None,
                    stop_reason=None, last_busy_kind=None,
                )
            else:
                # The planner's own explanation first (often "question answered,
                # result negative"); the rule that failed only as a footnote —
                # #131 showed just "the final completed stage is not a reviewer".
                why = reason or "orchestrator concluded blocked"
                stop = f"exit criteria unmet, orchestrator proposed no further stages ({why})"
                if parsed["rationale"]:
                    stop = f"{parsed['rationale'].strip()}\n\n({stop})"
                _set_task(
                    cx, task_id, now, status="blocked", plan_json=plan_json,
                    stop_reason=stop, last_busy_kind=None,
                )
            return _task_row(cx, task_id)
        try:
            tail = _normalise_stages(parsed["stages"])
        except TransitionError as e:
            _set_task(cx, task_id, now, status="failed", plan_json=plan_json, error=f"Orchestrator plan rejected: {e}")
            return _task_row(cx, task_id)
        total = len(history) + len(tail)
        if total > limits["max_stages"]:
            _set_task(
                cx, task_id, now, status="blocked", plan_json=plan_json,
                stop_reason=f"max_stages exceeded: plan would have {total} stages (limit {limits['max_stages']})",
                last_busy_kind=None,
            )
            return _task_row(cx, task_id)
        start_seq = (max(s["seq"] for s in history) if history else 0) + 1
        _insert_stages(cx, task_id, tail, start_seq, now)
        budget = task["budget_usd"]
        if budget is None and not gate and parsed["budget_usd"] is not None:
            budget = parsed["budget_usd"]
        _set_task(
            cx, task_id, now, status="plan_ready", plan_json=plan_json, budget_usd=budget,
            error=None, stop_reason=None, last_busy_kind=None,
        )
        # Autopilot: run straight into the first stage (initial plan and
        # re-gate alike) instead of parking at plan_ready.
        task = _task_row(cx, task_id)
        nxt = next((s for s in _stage_rows(cx, task_id) if s["status"] == "pending"), None)
        if nxt is not None:
            _autopilot_next(cx, task, nxt, now)
    return get_task(task_id)


def plan_fail(task_id: int, error: str) -> Optional[dict]:
    now = _now()
    err = (error or "").strip()[:4000]
    with _connect() as cx:
        cur = cx.execute(
            "UPDATE tasks SET status='failed', error=?, updated_at=? "
            "WHERE id=? AND status='planning'",
            (err, now, task_id),
        )
        if cur.rowcount == 0:
            return None
    return get_task(task_id)


def set_stage_session(
    task_id: int, seq: int, attempt: int, session_id: Optional[str], backend: Optional[str],
) -> bool:
    now = _now()
    with _connect() as cx:
        cur = cx.execute(
            "UPDATE task_stages SET session_id=?, session_backend=COALESCE(?, session_backend), "
            "updated_at=? WHERE task_id=? AND seq=? AND attempt=?",
            (session_id, (backend or None), now, task_id, seq, attempt),
        )
        return cur.rowcount > 0


def _finding_prefix(role: str) -> str:
    return (role or "F").upper().replace("-", "")[:4] or "F"


def complete_stage(task_id: int, seq: int, attempt: int, output: str,
                   evidence: Optional[dict] = None) -> Optional[dict]:
    """Worker finished a stage. Parses the result and applies the gates.
    Returns None when the callback is stale (task not running, stage not
    running, or attempt mismatch after a stop/retry)."""
    now = _now()
    with _tx() as cx:
        task = _task_row(cx, task_id)
        if not task or task["status"] != "running":
            return None
        stage = _stage_row(cx, task_id, seq)
        if not stage or stage["status"] != "running" or int(stage["attempt"]) != int(attempt):
            return None
        res = stage_result.parse_stage_result(output or "", _finding_prefix(stage["role"]))
        role_id = stage["role"]
        gate = agent_roles.METRIC_GATES.get(role_id)
        rule = agent_roles.VARIANT_PENALTY.get(role_id)
        variants = 0
        if gate and rule:
            # Multiple testing: every backtest run in this task is one look at
            # the out-of-sample numbers; each extra one raises the bar.
            variants = int(cx.execute(
                "SELECT COUNT(*) FROM task_stages WHERE task_id=? AND role=? AND status='done'",
                (task_id, rule.count_role)).fetchone()[0])
            gate = agent_roles.raised_gate(gate, rule, variants)
        agent_metrics = dict(res.metrics)
        file_metrics: dict = {}
        if gate and res.verdict == "pass":
            failed: list[str] = []
            if role_id in agent_roles.METRIC_FILES:
                ev = evidence or {}
                file_metrics = {k: float(v) for k, v in (ev.get("metrics") or {}).items()
                                if isinstance(v, (int, float))}
                if not file_metrics:
                    failed.append("Rohdaten: " + str(ev.get("error") or "keine Backtest-Dateien gemeldet")
                                  + " (docs/tasks/task-<id>/oos_equity.csv + oos_trades.csv)")
                res.metrics = agent_roles.worse_of(agent_metrics, file_metrics, gate)
            failed += agent_roles.check_metrics(res.metrics, gate)
            if failed:
                # Code, not the agent, decides: a pass that misses the
                # thresholds is a fail. Stable ids -> a repeat blocks.
                res.verdict = "fail"
                bar = f" (Variante {variants}, Hürden angehoben)" if variants > 1 and rule else ""
                res.findings = list(res.findings) + [
                    {"id": f"GATE-{c.split(' ', 1)[0].rstrip(':')}", "severity": "high",
                     "text": f"Kennzahl-Prüfung nicht bestanden{bar}: {c}", "resolved": False}
                    for c in failed
                ]
                res.summary = (res.summary + " — Kennzahl-Prüfung: " + "; ".join(failed)).strip(" —")
        if res.metrics:
            src = (f" (aus {evidence['file']} gerechnet; Auditor: "
                   + ", ".join(f"{k}={v:g}" for k, v in agent_metrics.items()) + ")"
                   if file_metrics and evidence and evidence.get("file") else "")
            res.summary = (res.summary + " — Kennzahlen: " + ", ".join(
                f"{k}={v:g}" for k, v in res.metrics.items()) + src).strip(" —")
        cost, metered, duration_ms, num_turns, tokens = _cost_from_jsonl(task_id, f"stage_{seq}")
        cx.execute(
            "UPDATE task_stages SET output=?, verdict=?, summary=?, findings_json=?, "
            "questions_json=?, options_json=?, cost_usd=?, metered=?, duration_ms=?, num_turns=?, tokens=?, "
            "pending_answer=NULL, error=NULL, updated_at=? WHERE id=?",
            (
                output, res.verdict, res.summary, json.dumps(res.findings),
                json.dumps(res.questions), json.dumps(res.options), cost, int(metered), duration_ms, num_turns, tokens,
                now, stage["id"],
            ),
        )
        if res.verdict == "needs_input":
            cx.execute(
                "UPDATE task_stages SET status='awaiting_input' WHERE id=?", (stage["id"],)
            )
            _set_task(cx, task_id, now, status="awaiting_input", last_busy_kind=None)
            return _task_row(cx, task_id)

        cx.execute(
            "UPDATE task_stages SET status='done', finished_at=? WHERE id=?", (now, stage["id"])
        )
        stages = _stage_rows(cx, task_id)
        plan = task["plan"]

        def _block(reason: str) -> None:
            _set_task(cx, task_id, now, status="blocked", stop_reason=reason, last_busy_kind=None)

        if res.verdict == "blocked":
            _block(f"stage {seq} ({stage['role']}) reported blocked: {res.summary or 'no reason given'}")
            return _task_row(cx, task_id)

        # Budget: stages with a $ figure count (Claude always; Codex when the
        # worker has a price table). Unpriced stages show their tokens instead.
        if task["budget_usd"] is not None:
            spent = sum(float(s["cost_usd"] or 0.0) for s in stages if s["metered"])
            if spent > float(task["budget_usd"]):
                _block(f"budget exceeded: ${spent:.2f} metered > ${float(task['budget_usd']):.2f}")
                return _task_row(cx, task_id)

        if res.verdict == "fail":
            rounds = int(task["round_count"] or 0) + 1
            _set_task(cx, task_id, now, round_count=rounds)
            if rule and gate and rule.max_variants and variants >= rule.max_variants:
                _block(f"{variants} Varianten geprüft, keine hat das Audit bestanden — weitere "
                       "Versuche wären Anpassung an die Testdaten (multiple testing). Idee verwerfen "
                       "oder mit neuer Hypothese als eigenen Task starten.")
                return _task_row(cx, task_id)
            prev = [
                s for s in stages
                if s["role"] == stage["role"] and s["status"] == "done" and s["seq"] < seq
            ]
            if prev:
                prev_open = {
                    f.get("id") for f in max(prev, key=lambda s: s["seq"])["findings"]
                    if isinstance(f, dict) and not f.get("resolved")
                }
                cur_open = {f["id"] for f in res.findings if not f.get("resolved")}
                repeated = sorted(x for x in (prev_open & cur_open) if x)
                if repeated:
                    _block(
                        f"{stage['role']} cited the same unresolved finding(s) twice: "
                        + ", ".join(repeated)
                    )
                    return _task_row(cx, task_id)
            if rounds > int(plan["max_rounds"]):
                _block(f"max_rounds exceeded: {rounds} failed stages > {plan['max_rounds']}")
                return _task_row(cx, task_id)
            # Re-gate: the orchestrator replaces the pending tail with
            # remediation + re-check stages.
            _set_task(cx, task_id, now, status="planning", last_busy_kind="plan")
            return _task_row(cx, task_id)

        # pass
        pending = [s for s in stages if s["status"] == "pending"]
        if not pending:
            _finish_or_regate(cx, task, stages, now)
            return _task_row(cx, task_id)
        nxt = pending[0]
        _set_task(cx, task_id, now, status="plan_ready", last_busy_kind=None)
        _autopilot_next(cx, _task_row(cx, task_id), nxt, now)
        return _task_row(cx, task_id)


def fail_stage(task_id: int, seq: int, attempt: int, error: str) -> Optional[dict]:
    """Agent crashed / no output. Same staleness guard as complete_stage."""
    now = _now()
    err = (error or "").strip()[:4000]
    with _tx() as cx:
        task = _task_row(cx, task_id)
        if not task or task["status"] != "running":
            return None
        stage = _stage_row(cx, task_id, seq)
        if not stage or stage["status"] != "running" or int(stage["attempt"]) != int(attempt):
            return None
        cx.execute(
            "UPDATE task_stages SET status='failed', error=?, finished_at=?, pending_answer=NULL, "
            "updated_at=? WHERE id=?",
            (err, now, now, stage["id"]),
        )
        _set_task(cx, task_id, now, status="failed", error=err)
    return get_task(task_id)


def budget_paused_tasks() -> list[dict]:
    """Tasks autopilot parked at plan_ready — budget or a wait period
    (resume candidates; resume_budget_paused re-checks both)."""
    with _connect() as cx:
        rows = cx.execute(
            "SELECT * FROM tasks WHERE status='plan_ready' AND paused_reason IS NOT NULL "
            "AND paused_reason <> '' ORDER BY prio ASC, updated_at ASC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def resume_budget_paused(task_id: int) -> Optional[dict]:
    """Start the next stage of a budget-paused task (the pacer said go).
    Re-checks autopilot rules; clears the pause either way."""
    now = _now()
    with _tx() as cx:
        task = _task_row(cx, task_id)
        if not task or task["status"] != "plan_ready" or not (task.get("paused_reason") or ""):
            return None
        stages = _stage_rows(cx, task_id)
        if any(s["status"] in ("failed", "awaiting_input", "running") for s in stages):
            _set_task(cx, task_id, now, paused_reason=None)
            return _task_row(cx, task_id)
        nxt = next((s for s in stages if s["status"] == "pending"), None)
        if nxt is None:
            _set_task(cx, task_id, now, paused_reason=None)
            return _task_row(cx, task_id)
        _autopilot_next(cx, task, nxt, now)
    return get_task(task_id)

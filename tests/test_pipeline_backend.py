"""Pipeline mode (role-based multi-stage tasks) — backend state machine.

Everything that can advance, pause or stop a pipeline is decided in
``backend/data/tasks_db.py`` from the agent's raw text, never by the agent
itself. These tests pin that contract:

* an unparseable stage result never advances (=> needs_input),
* the plan invariants (auditor after backtest, reviewer last),
* the stop rules (rounds, stages, budget, repeated unresolved finding,
  orchestrator with nothing to add),
* autopilot never starts an implement stage on a live-bot repo,
* stale worker callbacks (after stop / retry) are rejected by the attempt
  guard,
* simple-mode refine is untouched (execute removed in Tasks v2),
* the role catalog matches the prompt files of the team (tests/fixtures/team-trading).
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from agent_team.tasks import agent_roles, stage_result

from conftest import DEMO_TEAM

_ROLES_DIR = DEMO_TEAM / "roles"
_TASKS_TSX = Path(__file__).resolve().parents[1] / "frontend" / "src" / "pages" / "Tasks.tsx"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path, monkeypatch):
    from agent_team.tasks import db as tasks_db
    tasks_db._ensured.clear()
    # Most tests drive stages by hand; the autopilot default has own tests.
    monkeypatch.setattr(tasks_db, "DEFAULT_AUTOPILOT", "off")
    return tasks_db


def stage_text(verdict="pass", summary="ok", findings=None, questions=None, scratch=""):
    body = {
        "verdict": verdict, "summary": summary,
        "findings": findings or [], "questions": questions or [],
        "artifacts": [], "next_hint": "",
    }
    return f"{scratch}\n# Stage result\n```json\n{json.dumps(body)}\n```\n"


def plan_text(stages, exit_criteria=None, max_rounds=3, max_stages=12, budget=None,
              conclusion=None, rationale="because", repos=None, new_repo=None):
    body = {
        "exit_criteria": exit_criteria or ["tests green"],
        "max_rounds": max_rounds, "max_stages": max_stages, "budget_usd": budget,
        "stages": stages, "rationale": rationale, "conclusion": conclusion,
    }
    if repos is not None:
        body["repos"] = repos
    if new_repo is not None:
        body["new_repo"] = new_repo
    return f"thinking...\n# Plan\n```json\n{json.dumps(body)}\n```\n"


def _stages(db, tid):
    return {s["seq"]: s for s in db.list_stages(tid)}


def _running(db, tid):
    return [s for s in db.list_stages(tid) if s["status"] == "running"]


def make_pipeline(db, stages, repos=("demo-monitoring",), autopilot="off", **plan_kw):
    """Create a task, plan it with the given stage roles, return (task, stages)."""
    t = db.create_task("t", "details", 2, list(repos))
    assert db.request_plan(t["id"])["status"] == "planning"
    t = db.complete_plan(t["id"], plan_text([{"role": r, "goal": "g"} for r in stages], **plan_kw))
    assert t["status"] == "plan_ready", t
    if autopilot != "off":
        db.set_autopilot(t["id"], autopilot)
    return db.get_task(t["id"]), _stages(db, t["id"])


def run_and_complete(db, tid, verdict="pass", **kw):
    """request_next_stage + complete it with the given verdict."""
    t = db.request_next_stage(tid)
    assert t is not None and t["status"] == "running", t
    st = _running(db, tid)[0]
    return db.complete_stage(tid, st["seq"], st["attempt"], stage_text(verdict, **kw))


def write_result_event(db, tid, kind, cost, usage=None):
    p = db.session_jsonl_path(tid, kind)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"event": "start"}) + "\n")
        ev = {"event": "result", "cost_usd": cost, "duration_ms": 10, "num_turns": 2}
        if usage is not None:
            ev["usage"] = usage
        f.write(json.dumps(ev) + "\n")


# ---------------------------------------------------------------------------
# Migration + simple-mode regression
# ---------------------------------------------------------------------------

def test_migration_adds_pipeline_columns_and_stage_table(tmp_path, monkeypatch):
    from agent_team.tasks import db as tasks_db

    db_path = tmp_path / "tasks.db"
    cx = sqlite3.connect(str(db_path))
    cx.executescript(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, "
        "details TEXT NOT NULL DEFAULT '', prio INTEGER NOT NULL DEFAULT 2, "
        "repos_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'new', "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, refined_spec TEXT, "
        "execute_summary TEXT, error TEXT);"
    )
    cx.execute(
        "INSERT INTO tasks (title, details, prio, repos_json, status, created_at, updated_at) "
        "VALUES ('old', '', 2, '[]', 'refined', 'x', 'x')"
    )
    cx.commit()
    cx.close()
    tasks_db._ensured.clear()

    rows = tasks_db.list_tasks()
    assert len(rows) == 1
    row = rows[0]
    assert row["mode"] == "simple"
    assert row["autopilot"] == "off"
    assert row["round_count"] == 0
    assert row["budget_usd"] is None
    assert row["stop_reason"] is None
    assert row["plan_model"] is None
    assert row["plan"]["max_rounds"] == tasks_db.DEFAULT_MAX_ROUNDS
    assert row["plan"]["new_repo"] is None
    assert tasks_db.list_stages(row["id"]) == []
    # idempotent
    tasks_db._ensured.clear()
    tasks_db._ensure_db()
    assert tasks_db.list_tasks()[0]["title"] == "old"


def test_stage_table_migration_adds_tokens(tmp_path, monkeypatch):
    """A task_stages table created before the tokens column gets it added."""
    from agent_team.tasks import db as tasks_db

    db_path = tmp_path / "tasks.db"
    cx = sqlite3.connect(str(db_path))
    cx.executescript(tasks_db.SCHEMA.replace("    tokens INTEGER,\n", ""))
    cx.execute(
        "INSERT INTO task_stages (task_id, seq, role, kind, created_at, updated_at) "
        "VALUES (1, 1, 'quant', 'advise', 'x', 'x')"
    )
    cx.commit()
    cx.close()
    tasks_db._ensured.clear()
    assert tasks_db.list_stages(1)[0]["tokens"] is None


def test_refine_then_pipeline(db):
    """Tasks v2: refine stays ("Ausarbeiten"), execute is gone — a refined
    task starts as a pipeline."""
    t = db.create_task("s", "d", 2, ["demo-monitoring"])
    assert t["mode"] == "simple"
    assert db.request_refine(t["id"])["status"] == "refining"
    assert db.complete_refine(t["id"], "# Refined spec\nx")["status"] == "refined"
    assert not hasattr(db, "request_execute")
    assert db.list_stages(t["id"]) == []
    # pipeline-only transitions refuse simple tasks
    assert db.set_autopilot(t["id"], "full-bots") is None
    assert db.stop_pipeline(t["id"]) is None
    assert db.request_next_stage(t["id"]) is None
    p = db.request_plan(t["id"])
    assert p["status"] == "planning" and p["mode"] == "pipeline"


def test_autopilot_two_levels(db):
    t, _ = make_pipeline(db, ["implementation-engineer"])
    assert db.set_autopilot(t["id"], "off")["autopilot"] == "off"
    assert db.set_autopilot(t["id"], "full")["autopilot"] == "full-bots"  # pre-v2 client
    with pytest.raises(db.TransitionError):
        db.set_autopilot(t["id"], "bogus")


def test_stream_kinds(db):
    for ok in ("refine", "execute", "plan", "stage_1", "stage_42"):
        assert db.is_stream_kind(ok), ok
        assert db.session_jsonl_path(7, ok).name == f"{ok}.jsonl"
    for bad in ("", None, "stage_", "stage_x", "refine.jsonl", "../etc"):
        assert not db.is_stream_kind(bad), bad
    with pytest.raises(ValueError):
        db.session_jsonl_path(7, "bogus")


def test_delete_task_cascades_stages(db):
    t, stages = make_pipeline(db, ["implementation-engineer"])
    assert stages
    assert db.delete_task(t["id"]) is True
    assert db.list_stages(t["id"]) == []


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def test_parse_stage_result_happy_path():
    r = stage_result.parse_stage_result(stage_text("fail", "bad", findings=[
        {"id": "AUD-1", "severity": "high", "text": "look-ahead"},
        "plain string finding",
    ]), role_prefix="AUD")
    assert r.parsed and r.verdict == "fail" and r.summary == "bad"
    assert r.findings[0] == {"id": "AUD-1", "severity": "high", "text": "look-ahead", "resolved": False}
    assert r.findings[1]["id"] == "AUD-2" and r.findings[1]["text"] == "plain string finding"


@pytest.mark.parametrize("text", [
    "",
    "I did lots of work but forgot the block.",
    "# Stage result\nno json here",
    "# Stage result\n```json\n{not json}\n```",
    "# Stage result\n```json\n[1,2]\n```",
])
def test_unparseable_stage_result_is_needs_input(text):
    r = stage_result.parse_stage_result(text)
    assert r.verdict == "needs_input" and r.parsed is False
    assert r.questions == [stage_result.UNPARSEABLE_QUESTION]


def test_shell_escaped_apostrophes_are_repaired():
    # Task #92 stage 7: a complete reviewer block with `'\''` inside a string.
    text = "# Stage result\n```json\n" + r"""{"verdict": "fail", "summary": "states '\''2-8x'\'' and it\'s wrong"}""" + "\n```"
    r = stage_result.parse_stage_result(text)
    assert r.parsed and r.verdict == "fail"
    assert r.summary == "states '2-8x' and it's wrong"


def test_valid_escaped_backslash_before_apostrophe_is_untouched():
    text = "# Stage result\n```json\n" + r"""{"verdict": "pass", "summary": "C:\\'x"}""" + "\n```"
    r = stage_result.parse_stage_result(text)
    assert r.parsed and r.summary == "C:\\'x"


def test_unknown_verdict_is_needs_input():
    r = stage_result.parse_stage_result(stage_text("maybe"))
    assert r.verdict == "needs_input" and r.parsed is False
    assert "maybe" in r.questions[0]


def test_last_stage_result_block_wins_over_echoed_template():
    template = stage_text("pass | fail | needs_input | blocked", "template", scratch="Here is the format:")
    real = stage_text("pass", "real one")
    r = stage_result.parse_stage_result(template + "\n...work...\n" + real)
    assert r.parsed and r.verdict == "pass" and r.summary == "real one"


def test_needs_input_without_questions_gets_a_generic_one():
    r = stage_result.parse_stage_result(stage_text("needs_input"))
    assert r.verdict == "needs_input" and r.questions


def test_parse_plan():
    p = stage_result.parse_plan(plan_text(
        [{"role": "Quant", "goal": "g", "model": " claude-opus-5 "}, "reviewer"],
        exit_criteria=["a", "b"], budget=12.5,
    ))
    assert p["stages"] == [
        {"role": "quant", "goal": "g", "model": "claude-opus-5"},
        {"role": "reviewer", "goal": "", "model": None},
    ]
    assert p["exit_criteria"] == ["a", "b"] and p["budget_usd"] == 12.5
    assert p["max_rounds"] == 3 and p["conclusion"] is None
    assert p["repos"] is None and p["new_repo"] is None  # keys absent
    p2 = stage_result.parse_plan(plan_text([], repos="intraday, demo-base", new_repo={"name": "demo-Newsflow", "reason": "no fit"}))
    assert p2["repos"] == ["intraday", "demo-base"]
    assert p2["new_repo"] == {"name": "demo-newsflow", "based_on": "", "reason": "no fit"}
    assert stage_result.parse_plan(plan_text([], repos=[]))["repos"] == []
    assert stage_result.parse_plan("no plan at all") is None
    assert stage_result.parse_plan("# Plan\n```json\n{\"stages\": [], \"conclusion\": \"DONE\"}\n```")["conclusion"] == "done"


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def test_request_plan_flips_mode_and_is_status_guarded(db):
    t = db.create_task("t", "d", 2, [])
    p = db.request_plan(t["id"])
    assert p["mode"] == "pipeline" and p["status"] == "planning" and p["last_busy_kind"] == "plan"
    assert db.request_plan(t["id"]) is None  # already planning


def test_complete_plan_enforces_auditor_and_reviewer(db):
    t, stages = make_pipeline(
        db, ["quant", "backtest-engineer", "implementation-engineer"],
        exit_criteria=["OOS Sharpe > 1"], budget=20,
    )
    roles = [stages[i]["role"] for i in sorted(stages)]
    assert roles == ["quant", "backtest-engineer", "implementation-engineer", "backtest-auditor", "reviewer"]
    assert stages[2]["kind"] == "implement" and stages[1]["kind"] == "advise"
    assert all(s["status"] == "pending" and s["attempt"] == 0 for s in stages.values())
    assert t["plan"]["exit_criteria"] == ["OOS Sharpe > 1"]
    assert t["budget_usd"] == 20 and t["stop_reason"] is None


def test_complete_plan_failures(db):
    t = db.create_task("t", "d", 2, [])
    db.request_plan(t["id"])
    f = db.complete_plan(t["id"], "I could not decide.")
    assert f["status"] == "failed" and "Plan" in f["error"]
    assert db.request_plan(t["id"])["status"] == "planning"
    f = db.complete_plan(t["id"], plan_text([{"role": "wizard"}]))
    assert f["status"] == "failed" and "wizard" in f["error"]
    assert db.request_plan(t["id"])["status"] == "planning"
    f = db.complete_plan(t["id"], plan_text([]))
    assert f["status"] == "failed" and "empty" in f["error"]
    # stale callback (not planning) is ignored
    assert db.complete_plan(t["id"], plan_text([{"role": "reviewer"}])) is None


def test_complete_plan_reconciles_repos_like_refine(db):
    """The orchestrator declares the repos its stages touch; known names win
    (short forms tolerated, junk dropped), a valid new_repo proposal is the
    one way an unknown name lands on the task, and silence leaves the
    user's selection alone."""
    t = db.create_task("t", "", 2, ["demo-monitoring"])
    db.request_plan(t["id"])
    r = db.complete_plan(t["id"], plan_text([{"role": "reviewer"}], repos=["intraday", "demo-base", "nonsense"]))
    assert r["repos"] == ["demo-intraday", "demo-base"]
    # re-plan without a repos key: untouched
    db.request_plan(t["id"])
    assert db.complete_plan(t["id"], plan_text([{"role": "reviewer"}]))["repos"] == ["demo-intraday", "demo-base"]
    # new repo proposal (bad names rejected, good ones appended, kept on re-plan)
    db.request_plan(t["id"])
    r = db.complete_plan(t["id"], plan_text([{"role": "reviewer"}], repos=["base"], new_repo={"name": "Demo-NewsFlow", "reason": "nothing fits"}))
    assert r["repos"] == ["demo-base", "demo-newsflow"]
    assert r["plan"]["new_repo"]["name"] == "demo-newsflow" and r["plan"]["new_repo"]["reason"] == "nothing fits"
    db.request_plan(t["id"])
    r = db.complete_plan(t["id"], plan_text([{"role": "reviewer"}], repos=["monitoring"]))
    assert r["repos"] == ["demo-monitoring", "demo-newsflow"]
    db.request_plan(t["id"])
    r = db.complete_plan(t["id"], plan_text([{"role": "reviewer"}], new_repo={"name": "../etc", "reason": "x"}))
    assert r["repos"] == ["demo-monitoring", "demo-newsflow"]
    # explicit empty list clears known repos (`# Repos: -` semantics)
    t2 = db.create_task("t2", "", 2, ["demo-swing"])
    db.request_plan(t2["id"])
    assert db.complete_plan(t2["id"], plan_text([{"role": "reviewer"}], repos=[]))["repos"] == []


def test_autopilot_never_chains_implement_on_unknown_repo(db):
    """A proposed new bot repo is a future live bot: click, don't chain."""
    t = db.create_task("t", "", 2, ["demo-monitoring"])
    db.request_plan(t["id"])
    db.complete_plan(t["id"], plan_text(
        [{"role": "quant"}, {"role": "implementation-engineer"}],
        repos=["monitoring"], new_repo={"name": "demo-newbot", "reason": "r"},
    ))
    db.set_autopilot(t["id"], "full")
    r = run_and_complete(db, t["id"], "pass")
    assert r["status"] == "plan_ready"


def test_tokens_are_recorded_from_result_usage(db):
    t, _ = make_pipeline(db, ["quant"])
    db.request_next_stage(t["id"])
    write_result_event(db, t["id"], "stage_1", None, usage={"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 50})
    r = db.complete_stage(t["id"], 1, 1, stage_text("pass"))
    st = _stages(db, t["id"])[1]
    assert st["metered"] is False and st["tokens"] == 1050 and st["cost_usd"] == 0
    assert r["status"] == "plan_ready"


def test_complete_plan_over_max_stages_blocks(db):
    t = db.create_task("t", "d", 2, [])
    db.request_plan(t["id"])
    b = db.complete_plan(t["id"], plan_text([{"role": "implementation-engineer"}] * 5, max_stages=3))
    assert b["status"] == "blocked" and "max_stages" in b["stop_reason"]
    assert db.list_stages(t["id"]) == []


def test_update_plan_replaces_pending_tail_only(db):
    t, stages = make_pipeline(db, ["implementation-engineer", "test-engineer"])
    run_and_complete(db, t["id"], "pass")  # stage 1 done
    u = db.update_plan(
        t["id"], [{"role": "integration-ops", "goal": "wire"}],
        exit_criteria=["x"], max_rounds=2, max_stages=6, budget_usd=3.0,
    )
    got = _stages(db, t["id"])
    assert got[1]["status"] == "done" and got[1]["role"] == "implementation-engineer"
    # deploy after the review (integration-ops may follow the final reviewer)
    assert [got[i]["role"] for i in sorted(got) if i > 1] == ["reviewer", "integration-ops"]
    assert u["plan"] == {"exit_criteria": ["x"], "max_rounds": 2, "max_stages": 6, "rationale": "because", "new_repo": None}
    assert u["budget_usd"] == 3.0
    with pytest.raises(db.TransitionError):
        db.update_plan(t["id"], [{"role": "reviewer"}] * 7, max_stages=6)
    with pytest.raises(db.TransitionError):
        db.update_plan(t["id"], [{"role": "nope"}])


# ---------------------------------------------------------------------------
# Running stages
# ---------------------------------------------------------------------------

def test_run_next_and_pass_pauses_when_autopilot_off(db):
    t, _ = make_pipeline(db, ["implementation-engineer", "test-engineer"])
    r = db.request_next_stage(t["id"])
    assert r["status"] == "running" and r["last_busy_kind"] == "stage_1"
    st = _stages(db, t["id"])[1]
    assert st["status"] == "running" and st["attempt"] == 1 and st["started_at"]
    assert db.request_next_stage(t["id"]) is None  # busy
    done = db.complete_stage(t["id"], 1, 1, stage_text("pass", "did it"))
    assert done["status"] == "plan_ready"
    st = _stages(db, t["id"])[1]
    assert st["status"] == "done" and st["verdict"] == "pass" and st["summary"] == "did it"
    assert _stages(db, t["id"])[2]["status"] == "pending"


def test_autopilot_unscoped_task_needs_the_click(db):
    t, _ = make_pipeline(db, ["quant", "implementation-engineer"], repos=[], autopilot="full-bots")
    r = run_and_complete(db, t["id"], "pass")
    assert r["status"] == "plan_ready"  # unscoped => global lock => click


def test_autopilot_full_bots_chains_implement_on_live_bot_repo(db):
    t, _ = make_pipeline(db, ["quant", "implementation-engineer"], repos=["demo-lstm"], autopilot="full-bots")
    r = run_and_complete(db, t["id"], "pass")
    assert r["status"] == "running" and _running(db, t["id"])[0]["seq"] == 2
    # Still a click for an unscoped task or an unknown (proposed new) repo.
    t2, _ = make_pipeline(db, ["quant", "implementation-engineer"], repos=[], autopilot="full-bots")
    assert run_and_complete(db, t2["id"], "pass")["status"] == "plan_ready"


def test_autopilot_full_bots_halts_on_unknown_repo(db):
    t, _ = make_pipeline(db, ["quant", "implementation-engineer"], repos=["demo-monitoring"], autopilot="full-bots")
    task = db.get_task(t["id"])
    task["repos"] = ["demo-newbot"]
    assert db._autopilot_allows(task, {"kind": "implement"}) is False
    task["repos"] = ["demo-swing"]
    assert db._autopilot_allows(task, {"kind": "implement"}) is True
    task["autopilot"] = "full"
    assert db._autopilot_allows(task, {"kind": "implement"}) is False


def test_needs_input_pauses_and_answer_resumes_with_new_attempt(db):
    t, _ = make_pipeline(db, ["quant"])
    r = run_and_complete(db, t["id"], "needs_input", questions=["A or B?"])
    assert r["status"] == "awaiting_input"
    st = _stages(db, t["id"])[1]
    assert st["status"] == "awaiting_input" and st["questions"] == ["A or B?"]
    assert db.request_next_stage(t["id"]) is None  # not plan_ready
    with pytest.raises(db.TransitionError):
        db.answer_stage(t["id"], 1, "   ")
    a = db.answer_stage(t["id"], 1, "B")
    assert a["status"] == "running"
    st = _stages(db, t["id"])[1]
    assert st["status"] == "running" and st["attempt"] == 2 and st["pending_answer"] == "B"
    # stale callback from attempt 1 is ignored; attempt 2 completes
    assert db.complete_stage(t["id"], 1, 1, stage_text("pass")) is None
    r = db.complete_stage(t["id"], 1, 2, stage_text("pass"))
    assert r["status"] == "plan_ready"
    assert _stages(db, t["id"])[1]["pending_answer"] is None


def test_unparseable_output_never_advances(db):
    t, _ = make_pipeline(db, ["implementation-engineer"], autopilot="full")
    r = run_and_complete(db, t["id"], scratch="")  # default pass
    assert r["status"] == "running"  # autopilot moved on to reviewer
    r = db.complete_stage(t["id"], 2, 1, "Looks great, all done!")
    assert r["status"] == "awaiting_input"
    assert _stages(db, t["id"])[2]["questions"] == [stage_result.UNPARSEABLE_QUESTION]


def test_fail_regates_and_rounds_block(db):
    t, _ = make_pipeline(db, ["implementation-engineer", "test-engineer"], max_rounds=2)
    r = run_and_complete(db, t["id"], "fail", findings=[{"id": "IMP-1", "text": "x"}])
    assert r["status"] == "planning" and r["round_count"] == 1
    # gate: orchestrator replaces the pending tail
    g = db.complete_plan(t["id"], plan_text([{"role": "implementation-engineer", "goal": "fix"}], max_rounds=99))
    assert g["status"] == "plan_ready"
    assert g["plan"]["max_rounds"] == 2  # gate cannot raise limits
    got = _stages(db, t["id"])
    assert [got[i]["role"] for i in sorted(got)] == ["implementation-engineer", "implementation-engineer", "reviewer"]
    assert got[1]["status"] == "done" and got[2]["status"] == "pending" and got[2]["seq"] == 2
    r = run_and_complete(db, t["id"], "fail")
    assert r["status"] == "planning" and r["round_count"] == 2
    db.complete_plan(t["id"], plan_text([{"role": "implementation-engineer"}]))
    r = run_and_complete(db, t["id"], "fail")
    assert r["status"] == "blocked" and "max_rounds" in r["stop_reason"]


def test_default_autopilot_runs_plan_straight_into_stage_1(db, monkeypatch):
    monkeypatch.setattr(db, "DEFAULT_AUTOPILOT", "full-bots")
    t = db.create_task("t", "d", 2, ["demo-monitoring"])
    assert db.request_plan(t["id"])["autopilot"] == "full-bots"
    r = db.complete_plan(t["id"], plan_text([{"role": "quant"}]))
    assert r["status"] == "running" and _running(db, t["id"])[0]["seq"] == 1
    # live-bot implement chains too — the worker runs it hands-off
    t2 = db.create_task("t2", "d", 2, ["demo-swing"])
    db.request_plan(t2["id"])
    r = db.complete_plan(t2["id"], plan_text([{"role": "implementation-engineer"}]))
    assert r["status"] == "running"


def test_autopilot_resumes_after_regate(db):
    t, _ = make_pipeline(db, ["implementation-engineer", "test-engineer"], autopilot="full")
    r = run_and_complete(db, t["id"], "fail", findings=[{"id": "IMP-1", "text": "x"}])
    assert r["status"] == "planning"
    g = db.complete_plan(t["id"], plan_text([{"role": "implementation-engineer", "goal": "fix"}]))
    assert g["status"] == "running" and _running(db, t["id"])[0]["seq"] == 2


def test_same_unresolved_finding_twice_blocks(db):
    t, _ = make_pipeline(db, ["backtest-engineer"])
    run_and_complete(db, t["id"], "pass")  # backtest-engineer
    r = run_and_complete(db, t["id"], "fail", findings=[{"id": "AUD-1", "text": "look-ahead"}])  # auditor
    assert r["status"] == "planning"
    db.complete_plan(t["id"], plan_text([{"role": "backtest-engineer", "goal": "fix"}]))
    run_and_complete(db, t["id"], "pass")  # fixed backtest
    r = run_and_complete(db, t["id"], "fail", findings=[{"id": "AUD-1", "text": "still look-ahead"}])
    assert r["status"] == "blocked" and "AUD-1" in r["stop_reason"]
    # a *different* finding would have re-gated instead
    t2, _ = make_pipeline(db, ["backtest-engineer"])
    run_and_complete(db, t2["id"], "pass")
    run_and_complete(db, t2["id"], "fail", findings=[{"id": "AUD-1", "text": "a", "resolved": True}])
    db.complete_plan(t2["id"], plan_text([{"role": "backtest-engineer"}]))
    run_and_complete(db, t2["id"], "pass")
    r = run_and_complete(db, t2["id"], "fail", findings=[{"id": "AUD-1", "text": "a"}])
    assert r["status"] == "planning"  # previous AUD-1 was marked resolved


def test_budget_blocks_only_on_metered_cost(db):
    t, _ = make_pipeline(db, ["quant", "data-engineer"], budget=1.0)
    db.request_next_stage(t["id"])
    write_result_event(db, t["id"], "stage_1", 0.4)
    r = db.complete_stage(t["id"], 1, 1, stage_text("pass"))
    assert r["status"] == "plan_ready"
    st = _stages(db, t["id"])[1]
    assert st["cost_usd"] == 0.4 and st["metered"] is True
    db.request_next_stage(t["id"])
    write_result_event(db, t["id"], "stage_2", 0.7)
    r = db.complete_stage(t["id"], 2, 1, stage_text("pass"))
    assert r["status"] == "blocked" and "budget" in r["stop_reason"]
    # unmetered (Codex) stage: no result event => $0, no block
    t2, _ = make_pipeline(db, ["quant"], budget=0.01)
    r = run_and_complete(db, t2["id"], "pass")
    assert r["status"] == "plan_ready" and _stages(db, t2["id"])[1]["metered"] is False


def test_finish_rule_marks_done_after_reviewer_pass(db):
    t, _ = make_pipeline(db, ["implementation-engineer"])
    run_and_complete(db, t["id"], "pass")
    r = run_and_complete(db, t["id"], "pass", summary="LGTM")  # reviewer
    assert r["status"] == "done"
    assert "Pipeline summary" in r["execute_summary"] and "LGTM" in r["execute_summary"]


def test_finish_rule_details():
    from agent_team.tasks.db import _finish_rule

    def s(seq, role, status="done", verdict="pass"):
        return {"seq": seq, "role": role, "status": status, "verdict": verdict}

    assert _finish_rule([s(1, "implementation-engineer"), s(2, "reviewer")]) == (True, "")
    assert _finish_rule([s(1, "reviewer"), s(2, "implementation-engineer")])[0] is False
    assert _finish_rule([s(1, "reviewer", verdict="fail")])[0] is False
    assert _finish_rule([s(1, "backtest-engineer"), s(2, "reviewer")])[0] is False
    assert _finish_rule([s(1, "backtest-auditor"), s(2, "backtest-engineer"), s(3, "reviewer")])[0] is False
    assert _finish_rule([s(1, "backtest-engineer"), s(2, "backtest-auditor"), s(3, "reviewer")]) == (True, "")
    assert _finish_rule([s(1, "backtest-engineer"), s(2, "backtest-auditor", verdict="fail"), s(3, "reviewer")])[0] is False
    assert _finish_rule([s(1, "reviewer", status="skipped")])[0] is False


def test_gate_with_nothing_to_add_blocks_when_rule_unmet(db):
    t, _ = make_pipeline(db, ["implementation-engineer"])
    run_and_complete(db, t["id"], "pass")
    r = run_and_complete(db, t["id"], "fail")  # reviewer fails -> planning
    assert r["status"] == "planning"
    g = db.complete_plan(t["id"], plan_text([], conclusion="done", rationale="Frage beantwortet: Weekly behalten."))
    assert g["status"] == "blocked" and "no further stages" in g["stop_reason"]
    assert g["stop_reason"].startswith("Frage beantwortet: Weekly behalten.")  # the planner's why first
    # but with the rule met, an empty gate plan finishes the task
    t2, _ = make_pipeline(db, ["implementation-engineer"])
    run_and_complete(db, t2["id"], "pass")
    run_and_complete(db, t2["id"], "pass")
    assert db.get_task(t2["id"])["status"] == "done"


def test_exhausted_plan_without_reviewer_regates(db):
    t, _ = make_pipeline(db, ["implementation-engineer"])
    # user skips nothing, but pretend the reviewer got skipped via a direct
    # DB edit is not possible (gate role) — instead exhaust via a plan whose
    # reviewer failed and whose re-gate adds a non-reviewer tail.
    run_and_complete(db, t["id"], "pass")
    run_and_complete(db, t["id"], "fail")  # reviewer fail -> planning
    db.complete_plan(t["id"], plan_text([{"role": "test-engineer"}]))  # reviewer auto-appended
    got = _stages(db, t["id"])
    assert [got[i]["role"] for i in sorted(got)][-2:] == ["test-engineer", "reviewer"]


def test_stop_marks_running_stage_failed_and_rejects_late_callback(db):
    t, _ = make_pipeline(db, ["implementation-engineer"])
    db.request_next_stage(t["id"])
    s = db.stop_pipeline(t["id"])
    assert s["status"] == "blocked" and s["stop_reason"] == "stopped by user"
    st = _stages(db, t["id"])[1]
    assert st["status"] == "failed" and st["error"] == "stopped by user"
    assert db.complete_stage(t["id"], 1, 1, stage_text("pass")) is None  # late callback
    assert db.fail_stage(t["id"], 1, 1, "boom") is None
    r = db.resume_pipeline(t["id"])
    assert r["status"] == "plan_ready" and r["stop_reason"] is None
    with pytest.raises(db.TransitionError):
        db.request_next_stage(t["id"])  # failed stage must be retried/skipped first
    r = db.retry_stage(t["id"], 1)
    assert r["status"] == "running" and _stages(db, t["id"])[1]["attempt"] == 2
    assert db.complete_stage(t["id"], 1, 1, stage_text("pass")) is None  # old attempt
    assert db.complete_stage(t["id"], 1, 2, stage_text("pass"))["status"] == "plan_ready"


def test_fail_stage_and_retry(db):
    t, _ = make_pipeline(db, ["quant"])
    db.request_next_stage(t["id"])
    f = db.fail_stage(t["id"], 1, 1, "agent crashed")
    assert f["status"] == "failed" and f["error"] == "agent crashed"
    assert _stages(db, t["id"])[1]["status"] == "failed"
    assert db.retry_stage(t["id"], 1)["status"] == "running"


def test_retry_stage_that_reported_blocked(db):
    t, _ = make_pipeline(db, ["quant"])
    r = run_and_complete(db, t["id"], "blocked")
    assert r["status"] == "blocked" and _stages(db, t["id"])[1]["status"] == "done"
    r = db.retry_stage(t["id"], 1)
    assert r["status"] == "running" and _stages(db, t["id"])[1]["attempt"] == 2


def test_skip_refuses_gate_roles(db):
    t, _ = make_pipeline(db, ["quant", "backtest-engineer"])
    got = _stages(db, t["id"])
    aud = next(s for s in got.values() if s["role"] == "backtest-auditor")
    rev = next(s for s in got.values() if s["role"] == "reviewer")
    for gate in (aud, rev):
        with pytest.raises(db.TransitionError):
            db.skip_stage(t["id"], gate["seq"])
    assert _stages(db, t["id"])[1]["status"] == "pending"
    assert db.skip_stage(t["id"], 1) is not None
    assert _stages(db, t["id"])[1]["status"] == "skipped"
    # skipping the awaiting stage releases awaiting_input
    r = run_and_complete(db, t["id"], "needs_input", questions=["?"])  # backtest-engineer asks
    assert r["status"] == "awaiting_input"
    assert db.skip_stage(t["id"], 2)["status"] == "plan_ready"


def test_limit_reached_during_stage_marks_stage_failed(db):
    t, _ = make_pipeline(db, ["quant"])
    db.request_next_stage(t["id"])
    r = db.mark_limit_reached(t["id"], "spend limit", "stage_1")
    assert r["status"] == "limit_reached" and r["last_busy_kind"] == "stage_1"
    assert _stages(db, t["id"])[1]["status"] == "failed"
    assert db.retry_stage(t["id"], 1)["status"] == "running"
    # planning too
    t2 = db.create_task("p", "", 2, [])
    db.request_plan(t2["id"])
    assert db.mark_limit_reached(t2["id"], "limit", "plan")["last_busy_kind"] == "plan"
    assert db.request_plan(t2["id"])["status"] == "planning"


def test_reopen_restore_and_mark_done_for_pipeline(db):
    t, _ = make_pipeline(db, ["implementation-engineer"])
    run_and_complete(db, t["id"], "pass")
    run_and_complete(db, t["id"], "pass")
    assert db.get_task(t["id"])["status"] == "done"
    assert db.reopen_task(t["id"])["status"] == "plan_ready"
    assert db.move_to_backlog(t["id"])["status"] == "backlog"
    assert db.restore_from_backlog(t["id"])["status"] == "plan_ready"
    assert db.mark_done(t["id"])["status"] == "done"
    # answer allowed from plan_ready after a reopen strands an awaiting stage
    t2, _ = make_pipeline(db, ["quant"])
    run_and_complete(db, t2["id"], "needs_input", questions=["?"])
    db.move_to_backlog(t2["id"])
    assert db.restore_from_backlog(t2["id"])["status"] == "plan_ready"
    assert db.answer_stage(t2["id"], 1, "go")["status"] == "running"


def test_set_autopilot_validates(db):
    t, _ = make_pipeline(db, ["quant"])
    with pytest.raises(db.TransitionError):
        db.set_autopilot(t["id"], "turbo")
    assert db.set_autopilot(t["id"], "FULL")["autopilot"] == "full-bots"  # pre-v2 level
    assert db.set_autopilot(t["id"], "OFF")["autopilot"] == "off"
    assert db.set_autopilot(t["id"], "full-bots")["autopilot"] == "full-bots"


def test_set_stage_session_is_attempt_scoped(db):
    t, _ = make_pipeline(db, ["quant"])
    db.request_next_stage(t["id"])
    assert db.set_stage_session(t["id"], 1, 1, "sess-1", "claude") is True
    assert db.set_stage_session(t["id"], 1, 2, "sess-x", "codex") is False
    st = _stages(db, t["id"])[1]
    assert st["session_id"] == "sess-1" and st["session_backend"] == "claude"


# ---------------------------------------------------------------------------
# Router-level bits
# ---------------------------------------------------------------------------

def test_cost_endpoint_sums_all_streams_and_counts_unmetered(db):
    from agent_team.tasks import router as tasks_router

    t, _ = make_pipeline(db, ["quant", "implementation-engineer"])
    write_result_event(db, t["id"], "plan", 0.1)
    write_result_event(db, t["id"], "stage_1", 0.25)
    db.session_jsonl_path(t["id"], "stage_2").write_text(json.dumps({"event": "start"}) + "\n")
    write_result_event(db, t["id"], "stage_3", None, usage={"input_tokens": 10, "output_tokens": 5})
    c = tasks_router.get_cost(t["id"])
    assert c["total_usd"] == pytest.approx(0.35)
    assert sorted(r["kind"] for r in c["runs"]) == ["plan", "stage_1", "stage_3"]
    assert c["unpriced_runs"] == 2 and c["unpriced_kinds"] == ["stage_2", "stage_3"]
    assert c["total_tokens"] == 15
    by_kind = {r["kind"]: r for r in c["runs"]}
    assert by_kind["stage_1"]["priced"] is True and by_kind["stage_3"]["priced"] is False


def test_plan_model_is_a_per_task_override(db):
    """Like refine_model / execute_model: set from the Tasks UI, NULL means
    the worker cascade (WORKER_ORCHESTRATOR_MODEL -> role default)."""
    t = db.create_task("t", "", 2, [], plan_model="claude-sonnet-5")
    assert t["plan_model"] == "claude-sonnet-5"
    assert db.update_task(t["id"], plan_model="")["plan_model"] is None
    assert db.update_task(t["id"], plan_model="gpt-5.6-terra")["plan_model"] == "gpt-5.6-terra"


def test_worker_defaults_expose_the_orchestrator_model():
    import inspect
    from agent_team.tasks import router as tasks_router

    src = inspect.getsource(tasks_router.worker_defaults)
    assert '"plan"' in src and '"plan_backend"' in src
    assert "WORKER_ORCHESTRATOR_MODEL" in src


def test_roles_endpoint_exposes_catalog():
    from agent_team.tasks import router as tasks_router

    body = tasks_router.list_roles()
    ids = [r["id"] for r in body["roles"]]
    assert "orchestrator" in ids and "reviewer" in ids
    assert body["gate_roles"] == ["backtest-auditor", "reviewer"]
    assert "demo-swing" in body["live_bot_repos"]


def test_router_registers_roles_before_task_id():
    from agent_team.tasks import router as tasks_router

    paths = [r.path for r in tasks_router.router.routes]
    assert paths.index("/roles") < paths.index("/{task_id}")


# ---------------------------------------------------------------------------
# Cross-repo drift guards
# ---------------------------------------------------------------------------

def _front_matter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    assert m, f"{path.name}: missing front matter"
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def test_role_catalog_matches_prompt_files():
    """The catalog is built from team.yaml + the role files' front-matter;
    every prompt file the worker can load must be in team.yaml's role list
    (or the UI could never schedule it)."""
    files = {p.stem: p for p in _ROLES_DIR.glob("*.md") if p.stem != "common-prompt"}
    assert set(files) == set(agent_roles.ROLE_BY_ID), (set(files) ^ set(agent_roles.ROLE_BY_ID))
    for role_id, path in files.items():
        fm = _front_matter(path)
        role = agent_roles.ROLE_BY_ID[role_id]
        assert fm["id"] == role.id, path.name
        assert fm["label"] == role.label, path.name
        assert fm["kind"] == role.kind, path.name
        assert (fm.get("default_model") or None) == role.default_model, path.name
    assert (_ROLES_DIR / "common-prompt.md").exists()


def test_live_bot_repos_are_known_sub_repos():
    # The UI's repo picker comes from /roles (team.yaml), not a TSX constant.
    src = _TASKS_TSX.read_text(encoding="utf-8")
    assert "const SUB_REPOS = roles?.repos ?? []" in src
    from agent_team.tasks import router as tasks_router
    assert tasks_router.list_roles()["repos"] == sorted(agent_roles.KNOWN_REPOS)
    assert agent_roles.LIVE_BOT_REPOS <= agent_roles.KNOWN_REPOS
    assert "demo-monitoring" not in agent_roles.LIVE_BOT_REPOS
    assert agent_roles.normalise_repo("intraday") == "demo-intraday"
    assert agent_roles.normalise_repo("demo") == "demo"
    assert agent_roles.normalise_repo("nope") is None
    assert agent_roles.NEW_REPO_RE.match("demo-newsflow") and not agent_roles.NEW_REPO_RE.match("newsflow")


def test_gate_roles_and_kinds_are_consistent():
    assert agent_roles.GATE_ROLES <= set(agent_roles.STAGE_ROLE_IDS)
    for r in agent_roles.ROLES:
        assert r.kind in agent_roles.STAGE_KINDS
        if r.kind == agent_roles.KIND_ADVISE:
            # advise = read-only, which Codex cannot enforce => Claude default
            m = r.default_model or ""
            assert m.startswith("claude-") or m in {"opus", "sonnet", "haiku", "fable"}, r.id


def test_pipeline_summary_keeps_full_stage_summaries(db):
    long = "x" * 1000 + " END"
    out = db._pipeline_summary([
        {"seq": 1, "role": "reviewer", "status": "done", "verdict": "pass", "summary": long},
    ])
    assert out.endswith(long)


def test_deploy_planned_before_the_review_moves_after_it(db):
    out = db._normalise_stages([{"role": "implementation-engineer"}, {"role": "integration-ops"},
                                {"role": "reviewer"}])
    assert [s["role"] for s in out] == ["implementation-engineer", "reviewer", "integration-ops"]
    paper = db._normalise_stages([{"role": "implementation-engineer"}, {"role": "integration-ops"},
                                  {"role": "paper-reviewer"}, {"role": "reviewer"}, {"role": "live-release"}])
    assert [s["role"] for s in paper] == ["implementation-engineer", "integration-ops", "paper-reviewer",
                                          "reviewer", "live-release"]

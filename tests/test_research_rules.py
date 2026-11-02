"""Metric gates and wait roles (demo strategy research)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from agent_team import projects
from agent_team.tasks import agent_roles


def test_check_metrics():
    gate = {"oos_sharpe": (">=", 1.0), "max_drawdown_pct": ("<=", 25.0)}
    assert agent_roles.check_metrics({"oos_sharpe": 1.4, "max_drawdown_pct": 12}, gate) == []
    out = agent_roles.check_metrics({"oos_sharpe": 0.6}, gate)
    assert any("oos_sharpe = 0.6" in o for o in out) and any("max_drawdown_pct: fehlt" in o for o in out)
    with pytest.raises(ValueError):
        agent_roles._parse_threshold("mehr als 1")


@pytest.fixture()
def research(tmp_path, monkeypatch):
    """demo catalog plus a metric gate on the auditor and a 14-day wait
    on a paper-reviewer-like role (data-engineer stands in)."""
    from agent_team.tasks import db
    real = agent_roles.current()
    import dataclasses
    cat = dataclasses.replace(real, METRIC_GATES={"backtest-auditor": {"oos_sharpe": (">=", 1.0)}},
                              WAIT_ROLES={"data-engineer": 14})
    monkeypatch.setattr(agent_roles, "current", lambda: cat)
    monkeypatch.setattr(db, "DEFAULT_AUTOPILOT", "off")
    return db


def test_auditor_pass_below_threshold_becomes_fail(research):
    from test_pipeline_backend import make_pipeline, stage_text, _running
    db = research
    t, _ = make_pipeline(db, ["quant", "backtest-engineer", "backtest-auditor"])
    for verdict in ("pass", "pass"):
        db.request_next_stage(t["id"])
        st = _running(db, t["id"])[0]
        db.complete_stage(t["id"], st["seq"], st["attempt"], stage_text(verdict))
    db.request_next_stage(t["id"])
    st = _running(db, t["id"])[0]
    assert st["role"] == "backtest-auditor"
    text = stage_text("pass").replace('"next_hint": ""', '"next_hint": "", "metrics": {"oos_sharpe": 0.4}')
    t2 = db.complete_stage(t["id"], st["seq"], st["attempt"], text)
    done = {s["seq"]: s for s in db.list_stages(t["id"])}[st["seq"]]
    assert done["verdict"] == "fail"
    assert any(f["id"] == "GATE-oos_sharpe" for f in done["findings"])
    assert "oos_sharpe=0.4" in done["summary"]
    assert t2["status"] in ("planning", "blocked")   # re-gate, not done


def test_wait_role_parks_until_due(research):
    from test_pipeline_backend import make_pipeline, stage_text, _running
    db = research
    t, _ = make_pipeline(db, ["quant", "data-engineer"], autopilot="advise")
    db.request_next_stage(t["id"])
    st = _running(db, t["id"])[0]
    t2 = db.complete_stage(t["id"], st["seq"], st["attempt"], stage_text("pass"))
    assert t2["status"] == "plan_ready" and t2["paused_reason"].startswith("Wartet bis")
    with pytest.raises(db.TransitionError):
        db.request_next_stage(t["id"])                 # a click cannot jump the wait
    # 15 days later the resume path starts it
    with db._connect() as cx:
        past = (datetime.now(db.BERLIN) - timedelta(days=15)).isoformat(timespec="seconds")
        cx.execute("UPDATE task_stages SET finished_at=? WHERE task_id=? AND role='quant'", (past, t["id"]))
    t3 = db.resume_budget_paused(t["id"])
    assert t3["status"] == "running" and _running(db, t["id"])[0]["role"] == "data-engineer"


def test_raised_gate_and_worse_of():
    rule = agent_roles.VariantRule("backtest-engineer", {"oos_sharpe": 0.25, "max_drawdown_pct": 5}, 3)
    gate = {"oos_sharpe": (">=", 1.0), "max_drawdown_pct": ("<=", 25.0), "oos_trades": (">=", 30.0)}
    assert agent_roles.raised_gate(gate, rule, 1) == gate
    g3 = agent_roles.raised_gate(gate, rule, 3)
    assert g3["oos_sharpe"] == (">=", 1.5) and g3["max_drawdown_pct"] == ("<=", 15.0)
    assert g3["oos_trades"] == (">=", 30.0)
    m = agent_roles.worse_of({"oos_sharpe": 1.8, "max_drawdown_pct": 10, "note": 1},
                             {"oos_sharpe": 0.9, "max_drawdown_pct": 12, "oos_trades": 50}, gate)
    assert m == {"oos_sharpe": 0.9, "max_drawdown_pct": 12, "oos_trades": 50, "note": 1}


@pytest.fixture()
def files_gate(tmp_path, monkeypatch):
    """Auditor gate needs file evidence; +0.5 Sharpe per extra variant, max 2."""
    from agent_team.tasks import db
    import dataclasses
    real = agent_roles.current()
    cat = dataclasses.replace(
        real, METRIC_GATES={"backtest-auditor": {"oos_sharpe": (">=", 1.0)}},
        METRIC_FILES=frozenset({"backtest-auditor"}),
        VARIANT_PENALTY={"backtest-auditor": agent_roles.VariantRule("backtest-engineer", {"oos_sharpe": 0.5}, 2)})
    monkeypatch.setattr(agent_roles, "current", lambda: cat)
    monkeypatch.setattr(db, "DEFAULT_AUTOPILOT", "off")
    return db


def _run_until_auditor(db, tid):
    from test_pipeline_backend import stage_text, _running
    while True:
        db.request_next_stage(tid)
        st = _running(db, tid)[0]
        if st["role"] == "backtest-auditor":
            return st
        db.complete_stage(tid, st["seq"], st["attempt"], stage_text("pass"))


def _auditor_pass(sharpe):
    from test_pipeline_backend import stage_text
    return stage_text("pass").replace('"next_hint": ""', '"next_hint": "", "metrics": {"oos_sharpe": %s}' % sharpe)


def test_file_numbers_override_a_flattering_auditor(files_gate):
    from test_pipeline_backend import make_pipeline
    db = files_gate
    t, _ = make_pipeline(db, ["backtest-engineer", "backtest-auditor"])
    st = _run_until_auditor(db, t["id"])
    # no files -> fail even with great self-reported numbers
    db.complete_stage(t["id"], st["seq"], st["attempt"], _auditor_pass(2.0), {"error": "nicht gefunden"})
    done = {s["seq"]: s for s in db.list_stages(t["id"])}[st["seq"]]
    assert done["verdict"] == "fail" and any(f["id"] == "GATE-Rohdaten" for f in done["findings"])


def test_file_numbers_count_and_pass(files_gate):
    from test_pipeline_backend import make_pipeline
    db = files_gate
    t, _ = make_pipeline(db, ["backtest-engineer", "backtest-auditor"])
    st = _run_until_auditor(db, t["id"])
    ev = {"metrics": {"oos_sharpe": 0.7}, "file": "swing/docs/tasks/task-1"}
    db.complete_stage(t["id"], st["seq"], st["attempt"], _auditor_pass(2.0), ev)
    done = {s["seq"]: s for s in db.list_stages(t["id"])}[st["seq"]]
    assert done["verdict"] == "fail" and "oos_sharpe=0.7" in done["summary"]
    assert "aus swing/docs/tasks/task-1 gerechnet" in done["summary"]
    t2, _ = make_pipeline(db, ["backtest-engineer", "backtest-auditor"])
    st = _run_until_auditor(db, t2["id"])
    db.complete_stage(t2["id"], st["seq"], st["attempt"], _auditor_pass(1.4),
                      {"metrics": {"oos_sharpe": 1.2}, "file": "x"})
    done = {s["seq"]: s for s in db.list_stages(t2["id"])}[st["seq"]]
    assert done["verdict"] == "pass"


def test_second_variant_needs_a_higher_bar_and_the_last_one_stops(files_gate):
    from test_pipeline_backend import make_pipeline
    db = files_gate
    t, _ = make_pipeline(db, ["backtest-engineer", "backtest-auditor", "backtest-engineer", "backtest-auditor"])
    st = _run_until_auditor(db, t["id"])
    db.complete_stage(t["id"], st["seq"], st["attempt"], _auditor_pass(1.2), {"metrics": {"oos_sharpe": 1.2}})
    assert {s["seq"]: s for s in db.list_stages(t["id"])}[st["seq"]]["verdict"] == "pass"
    st = _run_until_auditor(db, t["id"])
    # variant 2: bar is 1.5 now, 1.2 no longer passes -> and 2 = max -> stop
    t3 = db.complete_stage(t["id"], st["seq"], st["attempt"], _auditor_pass(1.2), {"metrics": {"oos_sharpe": 1.2}})
    done = {s["seq"]: s for s in db.list_stages(t["id"])}[st["seq"]]
    assert done["verdict"] == "fail" and "Variante 2" in done["findings"][-1]["text"]
    assert t3["status"] == "blocked" and "2 Varianten" in t3["stop_reason"]

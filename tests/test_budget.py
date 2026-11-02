"""Budget pacer: policy, sample recording, autopilot pause/resume."""

from __future__ import annotations

import time

import pytest

from agent_team import budget, projects

DAY = 86400


def sample(week_util, five_util=0.1, resets_in=3 * DAY, ts=None):
    now = ts or time.time()
    return {"ts": now, "seven_day": {"util": week_util, "resets_at": now + resets_in},
            "five_hour": {"util": five_util, "resets_at": now + 3600}}


def test_even_spend_line():
    cfg = budget.Config(early_lead_pct=0)
    now = time.time()
    # mid-week (3.5 days left of 7): target ≈ 85% * (3.5d / 6.5d)
    t, rel = budget.week_target(now, now + 3.5 * DAY, cfg)
    assert not rel and 0.45 < t < 0.47
    t, rel = budget.week_target(now, now + 6 * 3600, cfg)  # inside the release window
    assert rel and t == 1.0


def test_soll_line_is_generous_early_and_tightens():
    cfg = budget.Config()                                   # 20 % head start, 15 % reserve
    now = time.time()
    t0, _ = budget.week_target(now, now + 7 * DAY, cfg)     # right after the reset
    assert abs(t0 - 0.85 * 0.20) < 0.01                     # 17 % allowed, not 0 %
    t_mid, _ = budget.week_target(now, now + 3.5 * DAY, cfg)
    assert 0.53 < t_mid < 0.54                              # ahead of the straight line (≈ 0.46)
    t_end, _ = budget.week_target(now, now + 12 * 3600 + 1, cfg)
    assert abs(t_end - 0.85) < 0.01                         # head start used up at the line's end
    assert budget.evaluate(sample=sample(0.03, resets_in=7 * DAY - 3600), cfg=cfg).allowed   # 3 % on day 1


def test_evaluate_rules():
    cfg = budget.Config()
    assert budget.evaluate(sample=sample(0.20), cfg=cfg).allowed                     # behind the line
    d = budget.evaluate(sample=sample(0.80), cfg=cfg)                                # ahead of the line
    assert not d.allowed and "Woche" in d.reason
    assert not budget.evaluate(sample=sample(0.2, five_util=0.9), cfg=cfg).allowed  # 5-h window full
    assert not budget.evaluate(sample=sample(0.2), last_user_activity=time.time() - 60, cfg=cfg).allowed
    assert budget.evaluate(sample=sample(0.8, resets_in=3600), cfg=cfg).allowed     # reserve released
    old = sample(0.99, ts=time.time() - 4 * 3600)
    assert budget.evaluate(sample=old, cfg=cfg).allowed                              # stale -> fail open
    assert not budget.evaluate(sample=sample(0.1), cfg=budget.Config(paused=True)).allowed
    assert budget.evaluate(None, cfg=cfg, sample={}).allowed                  # no data -> open


def test_record_and_latest(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "DATA_ROOT", tmp_path)
    raw = {"unifiedWindows": {"five_hour": {"utilization": 0.38, "resetsAt": 1, "status": "allowed"},
                              "seven_day": {"utilization": 0.22, "resetsAt": 2, "status": "allowed"}}}
    assert budget.record(raw, "test")["seven_day"]["util"] == 0.22
    assert budget.latest()["five_hour"]["util"] == 0.38
    assert budget.record({"status": "allowed"}, "test") is None   # no windows -> ignored
    assert not budget.probe_due()


def test_autopilot_pauses_on_budget_and_resumes(monkeypatch):
    from agent_team.tasks import db
    from test_pipeline_backend import make_pipeline, stage_text, _running

    monkeypatch.setattr(db, "DEFAULT_AUTOPILOT", "off")
    t, stages = make_pipeline(db, ["quant", "data-engineer"], autopilot="advise")
    db.request_next_stage(t["id"])
    st = _running(db, t["id"])[0]
    monkeypatch.setattr(db, "budget_gate", lambda: (False, "Wochenbudget voraus"))
    t2 = db.complete_stage(t["id"], st["seq"], st["attempt"], stage_text("pass"))
    assert t2["status"] == "plan_ready" and t2["paused_reason"].startswith("Budget:")
    assert [x["id"] for x in db.budget_paused_tasks()] == [t["id"]]
    monkeypatch.setattr(db, "budget_gate", lambda: (True, ""))
    t3 = db.resume_budget_paused(t["id"])
    assert t3["status"] == "running" and t3["paused_reason"] is None


def test_repark_does_not_touch_updated_at(monkeypatch):
    from agent_team.tasks import db
    from test_pipeline_backend import make_pipeline, stage_text, _running

    monkeypatch.setattr(db, "DEFAULT_AUTOPILOT", "off")
    t, _ = make_pipeline(db, ["quant", "data-engineer"], autopilot="advise")
    db.request_next_stage(t["id"])
    st = _running(db, t["id"])[0]
    monkeypatch.setattr(db, "budget_gate", lambda: (False, "voraus"))
    t2 = db.complete_stage(t["id"], st["seq"], st["attempt"], stage_text("pass"))
    stamp = t2["updated_at"]
    for _ in range(3):
        assert db.resume_budget_paused(t["id"])["updated_at"] == stamp   # inbox sees no change


def test_user_activity_pauses_scouts_not_approved_tasks(monkeypatch):
    from agent_team import budget
    monkeypatch.setattr(budget, "user_last_active", lambda: __import__("time").time())
    monkeypatch.setattr(budget, "latest", lambda: {"ts": __import__("time").time()})
    monkeypatch.setattr(budget.Config, "load", classmethod(lambda cls: budget.Config()))
    assert budget.decision().reason == "du arbeitest gerade selbst"
    assert budget.decision(for_tasks=True).allowed

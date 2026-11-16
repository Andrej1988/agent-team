"""Stages stopped by an expired login or the usage limit run again by themselves."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

import pytest

pytest.importorskip("claude_agent_sdk")  # chat/worker image only
from agent_team.chat import claude_login  # noqa: E402
from agent_team.worker import worker  # noqa: E402


def _iso(ts):
    return datetime.fromtimestamp(ts, worker._TZ).isoformat(timespec="seconds")


def test_reset_time_is_read_from_the_limit_notice():
    assert worker._limit_reset_note("You've hit your limit · resets 5pm (Europe/Berlin)") == f"{worker.RESET_NOTE}17:00"
    assert worker._limit_reset_note("limit reached, try again at 3:05 AM") == f"{worker.RESET_NOTE}03:05"
    assert worker._limit_reset_note("Usage limit reached. Reset at 12am") == f"{worker.RESET_NOTE}00:00"
    assert worker._limit_reset_note("usage limit reached") == ""
    assert worker._limit_reason("limit hit, resets 5pm", None).endswith("17:00")


def test_reset_due_is_the_next_such_time_after_the_failure():
    failed = datetime(2026, 11, 16, 18, 0, tzinfo=worker._TZ).timestamp()
    due = worker._reset_due(f"x{worker.RESET_NOTE}17:00", failed)
    assert datetime.fromtimestamp(due, worker._TZ).day == 17  # 17:00 already passed that day
    assert worker._reset_due("no reset named", failed) is None


class FakeApi:
    def __init__(self, failed, limited, stages):
        self.failed, self.limited, self.stages, self.retried = failed, limited, stages, []

    def list_tasks(self, status):
        return {"failed": self.failed, "limit_reached": self.limited}.get(status, [])

    def list_stages(self, tid):
        return self.stages[tid]

    def retry_stage(self, tid, seq):
        self.retried.append((tid, seq))
        return {}

    def retry_plan(self, tid):
        self.retried.append((tid, "plan"))
        return {}


def _run(monkeypatch, api, creds):
    monkeypatch.setattr(worker, "tasks_api", api)
    monkeypatch.setattr(claude_login, "credentials_mtime", lambda: creds)

    async def no_append(*a):
        return None

    monkeypatch.setattr(worker, "_safe_append", no_append)
    monkeypatch.setattr(worker, "_auto_retry_last", 0.0)
    monkeypatch.setitem(worker._limit_probe, "tid", None)
    monkeypatch.setitem(worker._limit_probe, "at", 0.0)
    asyncio.run(worker._auto_retry(10_000.0))
    return api.retried


def test_login_failures_run_again_once_the_login_changed(monkeypatch):
    t = time.time() - 600
    auth = claude_login.AUTH_ERROR
    stages = {
        1: [{"seq": 1, "status": "done"}, {"seq": 2, "status": "failed", "error": auth, "finished_at": _iso(t)}],
        2: [{"seq": 1, "status": "failed", "error": "tests broke", "finished_at": _iso(t)}],
    }
    failed = [{"id": 1, "status": "failed", "error": auth}, {"id": 2, "status": "failed", "error": "tests broke"}]
    assert _run(monkeypatch, FakeApi(failed, [], stages), t - 60) == []        # login older than the failure
    assert _run(monkeypatch, FakeApi(failed, [], stages), t + 60) == [(1, 2)]  # re-login: only the auth failure


def test_usage_limit_waits_for_the_named_reset_or_probes_one_task(monkeypatch):
    now = time.time()
    old = now - 2 * worker.LIMIT_PROBE_S
    st = lambda ts: [{"seq": 3, "status": "failed", "error": "limit", "finished_at": _iso(ts)}]  # noqa: E731
    future = datetime.fromtimestamp(now + 7200, worker._TZ).strftime("%H:%M")
    limited = [
        {"id": 5, "status": "limit_reached", "error": f"limit{worker.RESET_NOTE}{future}", "prio": 1},
        {"id": 6, "status": "limit_reached", "error": "limit", "prio": 2},
        {"id": 7, "status": "limit_reached", "error": "limit", "prio": 2},
        {"id": 8, "status": "limit_reached", "error": "Model x is blocked by this account's monthly spend limit", "prio": 0},
    ]
    stages = {5: st(now - 60), 6: st(old), 7: st(old), 8: st(old)}
    # 5 waits for its reset, 8 needs another model, of 6/7 only one probe
    assert _run(monkeypatch, FakeApi([], limited, stages), None) == [(6, 3)]


def test_planning_stopped_by_the_login_plans_again(monkeypatch):
    t = time.time() - 600
    failed = [{"id": 9, "status": "failed", "error": claude_login.AUTH_ERROR,
               "last_busy_kind": "plan", "updated_at": _iso(t)}]
    assert _run(monkeypatch, FakeApi(failed, [], {}), t + 60) == [(9, "plan")]
    assert _run(monkeypatch, FakeApi(failed, [], {}), t - 60) == []

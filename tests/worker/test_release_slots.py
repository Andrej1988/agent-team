"""One release (confirm-role stage) per project at a time, and stage slots
are claimed before the stage coroutine runs."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("claude_agent_sdk")  # chat/worker image only
from agent_team.worker import pipeline, worker  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_slots():
    for s in (worker._execute_slots, worker._release_slots, worker._release_wait_noted,
              worker._in_flight_stage_tasks, worker._in_flight_advise):
        s.clear()
    yield
    for s in (worker._execute_slots, worker._release_slots, worker._release_wait_noted,
              worker._in_flight_stage_tasks, worker._in_flight_advise):
        s.clear()


def _stage(seq, role, kind=pipeline.KIND_IMPLEMENT):
    return {"seq": seq, "role": role, "kind": kind}


def test_a_running_release_blocks_releases_of_other_tasks_even_in_other_repos():
    assert "integration-ops" in worker.CONFIRM_ROLES
    a = {"id": 1, "repos": ["shop-app"]}
    claimed = worker._claim_stage_slot(a, _stage(3, "integration-ops"))
    assert worker._release_blocker(2) == 1
    assert worker._release_blocker(1) is None
    # disjoint repos would pass the repo gate — only the release lock holds it
    assert worker._can_acquire_execute({"id": 2, "repos": ["shop-data"]})
    worker._release_stage_slot(a, _stage(3, "integration-ops"), *claimed)
    assert worker._release_blocker(2) is None and not worker._execute_slots


def test_slots_are_taken_before_the_stage_coroutine_runs():
    a = {"id": 1, "repos": ["shop-app"]}
    worker._claim_stage_slot(a, _stage(2, "developer"))
    assert not worker._can_acquire_execute({"id": 2, "repos": ["shop-app"]})


def test_waiting_release_is_noted_once(monkeypatch):
    posted = []

    async def fake_append(tid, kind, event):
        posted.append((tid, kind, event["text"]))

    monkeypatch.setattr(worker, "_safe_append", fake_append)
    b, st = {"id": 2}, _stage(4, "live-release")

    async def go():
        await worker._note_release_wait(b, st, 1)
        await worker._note_release_wait(b, st, 1)

    asyncio.run(go())
    assert len(posted) == 1 and posted[0][:2] == (2, "stage_4") and "#1" in posted[0][2]

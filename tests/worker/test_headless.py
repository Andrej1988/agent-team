"""Headless worker runs: no background jobs, and one nudge when a stage ends
its turn without the result block ("ich melde mich" after a background job).

Needs the worker image deps (claude_agent_sdk, tasks_api); skipped elsewhere.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("claude_agent_sdk")  # chat/worker image only
import claude_agent_sdk as sdk  # noqa: E402
from agent_team.worker import pipeline, worker  # noqa: E402


def _bash_hook():
    matcher = next(m for m in worker.headless_hooks()["PreToolUse"] if m.matcher == "Bash")
    return matcher.hooks[0]


def test_background_bash_is_refused_foreground_allowed():
    hook = _bash_hook()
    out = asyncio.run(hook({"tool_name": "Bash", "tool_input": {"command": "sleep 900", "run_in_background": True}},
                           None, None))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "foreground" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert asyncio.run(hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, None, None)) == {}


def test_monitor_and_wakeups_are_not_offered():
    assert {"Monitor", "ScheduleWakeup"} <= set(pipeline.HEADLESS_DISALLOWED_TOOLS)


class _Client:
    """Fake SDK client: one scripted answer per query."""

    def __init__(self, answers):
        self.answers, self.queries = list(answers), []

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def query(self, text):
        self.queries.append(text)

    async def receive_response(self):
        text = self.answers.pop(0)
        yield sdk.AssistantMessage(content=[sdk.TextBlock(text=text)], model="m")
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                num_turns=1, session_id="s")


def _run(monkeypatch, answers):
    client = _Client(answers)
    monkeypatch.setattr(worker, "ClaudeSDKClient", lambda options: client)

    async def _noop(*_a, **_k):
        return None
    monkeypatch.setattr(worker, "_safe_append", _noop)
    monkeypatch.setattr(worker.budget, "record_cost", lambda *a, **k: None)
    done, failed = [], []
    asyncio.run(worker._run_agent_session(
        {"id": 1, "title": "t"}, kind="stage_2", prompt="p", options=worker.ClaudeAgentOptions(),
        extract=lambda full: pipeline.extract_last_section(full, pipeline.STAGE_RESULT_MARKER),
        require_marker=pipeline.STAGE_RESULT_MARKER,
        complete_fn=lambda tid, text: done.append(text), fail_fn=lambda *a: failed.append(a),
        no_output_msg="none", complete_label="complete", complete_field="result_chars", backend="claude"))
    return client, done, failed


RESULT = '# Stage result\n```json\n{"verdict": "pass", "summary": "fertig"}\n```'


def test_stage_that_promised_to_report_back_is_nudged_once(monkeypatch):
    client, done, failed = _run(monkeypatch, ["Die Messung läuft noch, ich melde mich.", RESULT])
    assert len(client.queries) == 2 and "Automatic message from the worker" in client.queries[1]
    assert done and '"verdict": "pass"' in done[0] and not failed


def test_no_nudge_when_the_result_is_there_and_only_one_nudge(monkeypatch):
    client, _, _ = _run(monkeypatch, [RESULT])
    assert len(client.queries) == 1
    client, _, _ = _run(monkeypatch, ["ich melde mich", "immer noch nicht"])
    assert len(client.queries) == 2   # then the stage goes to the user as before

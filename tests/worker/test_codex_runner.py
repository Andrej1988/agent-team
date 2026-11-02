"""worker._run_codex_agent_session — no wall-clock cap, no orphaned Codex.

Needs the worker image deps (claude_agent_sdk, tasks_api); skipped elsewhere.
Run inside the image: tools/test.sh runs the worker tests in agent-team-chat.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("claude_agent_sdk")  # chat/worker image only
from agent_team.worker import worker  # noqa: E402


def _fake_codex(tmp_path: Path, monkeypatch, body: str) -> Path:
    """Put a `codex` stub first on PATH; it records its pid, then runs body."""
    pid_file = tmp_path / "codex.pid"
    stub = tmp_path / "codex"
    stub.write_text(f"#!/bin/sh\necho $$ > {pid_file}\ncat >/dev/null\n{body}\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(worker, "CWD", str(tmp_path))
    return pid_file


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _run(fail_calls: list) -> "asyncio.Task":
    async def _noop_append(*_a, **_k):
        return None

    worker._safe_append = _noop_append  # tasks API is not reachable in tests
    options = worker.ClaudeAgentOptions(model="gpt-test")
    return asyncio.ensure_future(worker._run_codex_agent_session(
        {"id": 1, "title": "t"}, kind="stage_1", prompt="p", options=options,
        extract=lambda s: s, complete_fn=lambda *a: None,
        fail_fn=lambda *a: fail_calls.append(a), no_output_msg="none",
        complete_label="done", complete_field="output",
    ))


def test_no_wall_clock_cap():
    import inspect
    src = inspect.getsource(worker._run_codex_agent_session)
    assert "timeout=" not in src


def test_cancel_kills_codex_subprocess(tmp_path, monkeypatch):
    pid_file = _fake_codex(tmp_path, monkeypatch, "sleep 60")

    async def scenario():
        task = _run([])
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            await asyncio.sleep(0.05)
        pid = int(pid_file.read_text())
        assert _alive(pid)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return pid

    pid = asyncio.run(scenario())
    assert not _alive(pid)


def test_nonzero_exit_marks_failed(tmp_path, monkeypatch):
    _fake_codex(tmp_path, monkeypatch, "echo boom >&2; exit 3")
    fails: list = []

    async def scenario():
        return await _run(fails)

    section, _ = asyncio.run(scenario())
    assert section is None
    assert fails and "boom" in fails[0][1]

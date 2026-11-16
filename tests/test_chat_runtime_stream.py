"""The agent's stream breaking mid-turn (e.g. a message over the SDK buffer
limit) must end the turn with an error and drop the client — not spin."""

from __future__ import annotations

import asyncio

import pytest

runtime = pytest.importorskip("agent_team.chat.runtime")  # needs claude_agent_sdk (chat image)

from agent_team import projects  # noqa: E402
from agent_team.chat import log as chatlog  # noqa: E402


class DeadClient:
    """receive_messages() raises at once, like the SDK after an oversized line."""

    def __init__(self):
        self.reads = 0
        self.disconnected = False

    async def query(self, prompt):
        pass

    async def receive_messages(self):
        self.reads += 1
        raise RuntimeError("JSON message exceeded maximum buffer size")
        yield  # pragma: no cover — makes this an async generator

    async def disconnect(self):
        self.disconnected = True


def test_broken_stream_ends_turn_and_drops_client(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "DATA_ROOT", tmp_path)
    ch = projects.Channel(id="demo-chat", project="demo", label="Demo", cwd="/tmp")
    rt = runtime.ChannelRuntime(ch, None)
    dead = DeadClient()

    async def fake_get_client():
        if rt.client is None:
            rt.client = dead
            rt.consumer = asyncio.create_task(rt._consume_stream(dead))
        return rt.client

    monkeypatch.setattr(rt, "get_client", fake_get_client)

    async def run():
        await asyncio.wait_for(rt._turn("hallo"), timeout=5)  # used to hang forever
        await asyncio.sleep(0.05)

    asyncio.run(run())
    events = chatlog.read_events("demo-chat")
    assert any(e["type"] == "error" and "abgerissen" in e["text"] for e in events)
    assert [e["type"] for e in events][-2:] == ["busy", "turn_end"]
    assert rt.client is None and dead.disconnected
    assert dead.reads == 1  # no re-read loop


def test_idle_session_is_closed_but_keeps_its_id(tmp_path, monkeypatch):
    """Dozens of open sessions filled the container (OOM killed running
    turns): an unused one is closed, the session id stays for resuming."""
    monkeypatch.setattr(projects, "DATA_ROOT", tmp_path)
    ch = projects.Channel(id="demo-chat", project="demo", label="Demo", cwd="/tmp")
    rt = runtime.ChannelRuntime(ch, None)
    rt._set_session("sess-1")
    client = DeadClient()
    rt.client = client

    async def run():
        assert not await rt.close_if_idle(60)          # just used
        rt.last_active -= 120
        rt.pending["x"] = object()
        assert not await rt.close_if_idle(60)          # waits for the user's answer
        rt.pending.clear()
        async with rt.lock:
            assert not await rt.close_if_idle(60)      # a turn is running
        assert await rt.close_if_idle(60)

    asyncio.run(run())
    assert rt.client is None and client.disconnected
    assert rt.state.session_ids.get("claude") == "sess-1"


def test_turn_broken_by_expired_login_runs_again_after_relogin(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "DATA_ROOT", tmp_path)
    ch = projects.Channel(id="demo-chat", project="demo", label="Demo", cwd="/tmp")
    rt = runtime.ChannelRuntime(ch, None)
    prompts = []

    async def broken(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            raise RuntimeError("Failed to authenticate: OAuth session expired and could not be refreshed")

    monkeypatch.setattr(rt, "_claude_turn", broken)

    async def run():
        await rt._turn("check the bot")
        assert rt.auth_retry and rt.auth_retry[0] == "check the bot"
        failed_at = rt.auth_retry[1]
        assert not await rt.retry_after_login(failed_at - 5)   # login not renewed yet
        assert not await rt.retry_after_login(None)
        assert await rt.retry_after_login(failed_at + 5)
        await rt.turn_task
        assert not await rt.retry_after_login(failed_at + 10)  # only once

    asyncio.run(run())
    assert prompts == ["check the bot", "check the bot"]
    texts = [e.get("text") or "" for e in chatlog.read_events(ch.id)]
    assert any("wieder angemeldet" in t for t in texts)

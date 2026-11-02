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

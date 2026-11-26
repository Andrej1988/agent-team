from __future__ import annotations

from agent_team import projects
from agent_team.chat import runtime


def test_browser_mcp_channel_option():
    chs = projects._channels("demo", {"channels": [
        {"id": "demo-a", "cwd": "/tmp", "browser_mcp": "http://browser-demo:8931/mcp"},
        {"id": "demo-b", "cwd": "/tmp"},
    ]})
    a, b = chs
    assert a.browser_mcp == "http://browser-demo:8931/mcp" and b.browser_mcp is None
    assert runtime.browser_options(a) == {"mcp_servers": {"browser": {"type": "http",
                                                                      "url": "http://browser-demo:8931/mcp"}}}
    assert runtime.browser_options(b) == {}
    assert "take over" in runtime.BROWSER_NOTE and "approved" in runtime.BROWSER_NOTE

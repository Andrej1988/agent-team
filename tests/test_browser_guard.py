"""The logged-in browser: actions in the user's name need their approval,
tools that bypass the check are refused; unattended runs never act."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys

from agent_team import browser_guard as bg, guard

P = "mcp__browser__"


def test_classify():
    assert bg.classify(P + "browser_snapshot", {}) == (None, "")
    assert bg.classify(P + "browser_navigate", {"url": "https://example.invalid"})[0] is None
    assert bg.classify(P + "browser_click", {"element": "Kommentare anzeigen"})[0] is None
    assert bg.classify(P + "browser_click", {"element": "Post button"})[0] == "ask"
    assert bg.classify(P + "browser_click", {"element": "Absenden"})[0] == "ask"
    assert bg.classify(P + "browser_click", {"element": "Upvote"})[0] == "ask"
    assert bg.classify(P + "browser_type", {"element": "Suche", "text": "x"})[0] is None
    assert bg.classify(P + "browser_type", {"element": "Kommentar", "text": "x", "submit": True})[0] == "ask"
    assert bg.classify(P + "browser_press_key", {"key": "Enter"})[0] == "ask"
    assert bg.classify(P + "browser_press_key", {"key": "Control+Enter"})[0] == "ask"
    assert bg.classify(P + "browser_press_key", {"key": "ArrowDown"})[0] is None
    assert bg.classify(P + "browser_file_upload", {"paths": ["/tmp/a.png"]})[0] == "ask"
    assert bg.classify(P + "browser_handle_dialog", {"accept": True})[0] == "ask"
    assert bg.classify(P + "browser_handle_dialog", {"accept": False})[0] is None
    for t in ("browser_evaluate", "browser_run_code_unsafe", "browser_network_request", "browser_network_requests"):
        assert bg.classify(P + t, {})[0] == "deny"
    assert bg.classify("mcp__playwright__browser_evaluate", {}) == (None, "")  # throwaway browser


def test_typed_text_for_the_approval():
    assert bg.typed_text(P + "browser_type", {"element": "Kommentar", "text": "Hallo"}) == "Kommentar: Hallo"
    assert bg.typed_text(P + "browser_fill_form", {"fields": [{"name": "Titel", "value": "T"}]}) == "Titel: T"
    assert bg.typed_text(P + "browser_click", {}) is None


def test_headless_hook_refuses(tmp_path):
    def run(tool, inp):
        out = subprocess.run([sys.executable, "-m", "agent_team.browser_guard"], capture_output=True, text=True,
                             input=json.dumps({"tool_name": tool, "tool_input": inp}), check=True).stdout
        return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out.strip() else None
    assert run(P + "browser_click", {"element": "Antworten"}) == "deny"
    assert run(P + "browser_evaluate", {"function": "() => 1"}) == "deny"
    assert run(P + "browser_snapshot", {}) is None


def test_cli_settings_include_browser_guard():
    hooks = json.loads(guard.cli_settings())["hooks"]["PreToolUse"]
    assert any(h["matcher"] == bg.MATCHER and "agent_team.browser_guard" in h["hooks"][0]["command"] for h in hooks)


def test_chat_hook_asks_and_remembers_typed_text():
    from agent_team.chat.runtime import ChannelRuntime

    asked = []

    class Fake:
        async def ask_approval(self, tool, title, detail, **kw):
            asked.append(detail)
            return answers.pop(0)

    answers = ["deny", "once"]
    f = Fake()
    f.ch = type("Ch", (), {"project": "demo-none"})()
    hook = ChannelRuntime.make_browser_hook(f)
    run = lambda tool, inp: asyncio.run(hook({"tool_name": P + tool, "tool_input": inp}, None, None))  # noqa: E731
    assert run("browser_type", {"element": "Kommentar", "text": "Tolle App"}) == {}
    out = run("browser_click", {"element": "Kommentieren"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Tolle App" in asked[0]
    assert run("browser_click", {"element": "Kommentieren"}) == {}
    assert run("browser_evaluate", {})["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert len(asked) == 2


def test_input_lock_while_user_views(tmp_path, monkeypatch):
    from agent_team import projects
    monkeypatch.setattr(projects, "DATA_ROOT", tmp_path)
    assert bg.busy_reason(P + "browser_click", "demo") is None
    bg.set_viewing("demo", True)
    assert bg.busy_reason(P + "browser_click", "demo")
    assert bg.busy_reason(P + "browser_snapshot", "demo") is None
    assert bg.busy_reason(P + "browser_click", "other") is None
    bg.set_viewing("demo", False)
    assert bg.busy_reason(P + "browser_type", "demo") is None

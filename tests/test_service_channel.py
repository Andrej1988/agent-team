"""Service channels: a program behind a chat channel (buttons + commands)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_team import projects
from agent_team.chat import log as chatlog


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    import agent_team.app as mod
    from agent_team.chat import web

    monkeypatch.setattr(projects, "DATA_ROOT", tmp_path)
    chans = (projects.Channel(id="demo-svc", project="demo", label="Svc", cwd="/tmp", service=True),
             projects.Channel(id="demo-chat", project="demo", label="Chat", cwd="/tmp"))
    p = projects.Project(id="demo", name="Demo", data_dir=tmp_path / "projects" / "demo",
                         team_dir=tmp_path / "teams" / "demo", channels=chans, tasks=False)
    projects.register(p)
    monkeypatch.setattr(projects, "all_projects", lambda: [p])
    calls = []
    lg = chatlog.ChannelLog("demo-svc")

    async def fake_forward(method, path, body=None):
        calls.append((method, path, body))
        if path.endswith("/command"):
            return lg.append("command", **body)
        return lg.append("alert", **body)

    monkeypatch.setattr(web, "_forward", fake_forward)
    return mod, calls, lg


def test_program_sends_buttons_and_polls_commands_from_notify_net(svc, monkeypatch):
    mod, calls, lg = svc
    monkeypatch.setattr(mod, "_notify_client", lambda r: True)
    c = TestClient(mod.app)
    buttons = [[{"label": "Long", "data": "p:long"}, {"label": "Short", "data": "p:short"}]]
    assert c.post("/p/demo/api/service/demo-svc/send", json={"text": "Brief", "buttons": buttons}).status_code == 200
    assert calls[0] == ("POST", "/channels/demo-svc/alert",
                        {"text": "Brief", "title": None, "source": "demo-svc", "buttons": buttons})
    start = c.get("/p/demo/api/service/demo-svc/commands?after=-1").json()
    assert start == {"commands": [], "last_seq": 1}           # old events are never replayed
    lg.append("command", text="p:long", label="Long", button_seq=1)
    lg.append("assistant", text="not a command")
    lg.append("command", text="/status", label=None, button_seq=None)
    got = c.get("/p/demo/api/service/demo-svc/commands?after=1&wait=0").json()
    assert [(x["seq"], x["text"]) for x in got["commands"]] == [(2, "p:long"), (4, "/status")]
    assert got["last_seq"] == 4
    assert c.get("/p/demo/api/service/demo-svc/commands?after=4&wait=0").json() == {"commands": [], "last_seq": 4}
    assert c.post("/p/demo/api/service/demo-svc/menu", json={"commands": [["status", "Stand"]]}).status_code == 200
    # notify net reaches nothing else, and only service channels
    assert c.post("/api/service/demo-svc/command", json={"text": "confirm"}).status_code == 403
    assert c.get("/api/service-channels").status_code == 403
    assert c.post("/p/demo/api/service/demo-chat/send", json={"text": "x"}).status_code == 404
    assert c.post("/p/other/api/service/demo-svc/send", json={"text": "x"}).status_code == 404


def test_app_sends_commands_and_lists_menus(svc, monkeypatch):
    mod, calls, lg = svc
    monkeypatch.setattr(mod, "access_identity", lambda r: "me@example.com")
    monkeypatch.setattr(mod, "_jwks", object())
    c = TestClient(mod.app)
    assert c.post("/api/service/demo-svc/command", json={"text": "confirm", "label": "Bestätigen", "button_seq": 3}).status_code == 200
    assert calls[-1] == ("POST", "/channels/demo-svc/command", {"text": "confirm", "label": "Bestätigen", "button_seq": 3})
    assert c.post("/api/service/demo-chat/command", json={"text": "x"}).status_code == 404
    assert c.post("/api/service/demo-svc/command", json={"text": " "}).status_code == 400
    (chatlog.channel_dir("demo-svc") / "service.json").write_text('{"menu": [["status", "Stand"]]}')
    assert c.get("/api/service-channels").json() == [{"id": "demo-svc", "menu": [["status", "Stand"]]}]


def test_service_flag_parsed(tmp_path):
    cfg = tmp_path / "projects.yaml"
    cfg.write_text("projects:\n  xx:\n    tasks: false\n    chat:\n      channels:\n"
                   "        - {id: x-svc, cwd: /w, service: true}\n        - {id: x-main, cwd: /w}\n")
    chans = projects.load(cfg)["xx"].channels
    assert [c.service for c in chans] == [True, False]


def test_tap_records_stay_in_service_channels(svc):
    mod, calls, lg = svc
    lg.append("alert", text="Order?", buttons=[[{"label": "Kaufen", "data": "buy"}]])  # 1
    lg.append("command", text="buy", label="Kaufen", button_seq=1)                     # 2
    assert chatlog.delete("demo-svc", [2]) == []  # would re-enable the order button
    other = chatlog.ChannelLog("demo-chat")
    other.append("command", text="start:1", label="Freigeben & starten", button_seq=1)
    assert chatlog.delete("demo-chat", [1]) == [1]

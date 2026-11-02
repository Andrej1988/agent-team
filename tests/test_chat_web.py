"""Chat on the web side: transcript log, open requests, inbox items, notify
routing. (The agent runtime itself needs the SDK and runs in the chat image.)"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from agent_team import inbox, projects
from agent_team.chat import log as chatlog


@pytest.fixture()
def chat_project(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "DATA_ROOT", tmp_path)
    ch = projects.Channel(id="demo-chat", project="demo", label="Demo", cwd="/tmp")
    p = projects.Project(id="demo", name="Demo", data_dir=tmp_path / "projects" / "demo",
                         team_dir=tmp_path / "teams" / "demo", channels=(ch,), tasks=False)
    projects.register(p)
    monkeypatch.setattr(projects, "all_projects", lambda: [p])
    return p


def test_log_seq_and_open_requests(chat_project):
    lg = chatlog.ChannelLog("demo-chat")
    assert lg.append("user", text="hi")["seq"] == 1
    lg.append("permission", req_id="r1", title="Bash", detail="ls")
    lg.append("question", req_id="r2", questions=[{"question": "A or B?", "options": []}])
    lg.append("resolved", req_id="r1", outcome="einmal erlaubt")
    assert [e["req_id"] for e in chatlog.open_requests("demo-chat")] == ["r2"]
    # a second writer (restart) continues the sequence
    assert chatlog.ChannelLog("demo-chat").append("assistant", text="x")["seq"] == 5
    assert [e["seq"] for e in chatlog.read_events("demo-chat", after=3)] == [4, 5]


def test_inbox_lists_chat_requests_alerts_and_unread_reply(chat_project, monkeypatch):
    lg = chatlog.ChannelLog("demo-chat")
    lg.append("user", text="hi")
    lg.append("assistant", text="answer")
    lg.append("turn_end")
    lg.append("alert", text="disk 95%", title="Watchdog")
    lg.append("permission", req_id="r1", title="Bash", detail="rm -rf build")
    kinds = sorted(i["kind"] for i in inbox.chat_items(chat_project))
    assert kinds == ["alert", "approval", "reply"]
    chatlog.mark_read("demo-chat", 5)
    # read: alert + reply gone, the open approval stays
    assert [i["kind"] for i in inbox.chat_items(chat_project)] == ["approval"]


def test_reply_push_is_quiet_while_channel_open(chat_project, monkeypatch):
    sent = []
    monkeypatch.setattr(inbox, "SEEN_FILE", chat_project.data_dir / "seen.json")
    monkeypatch.setattr(inbox.push, "send_all", lambda *a, **k: sent.append(a))
    inbox.check_and_notify()  # baseline
    lg = chatlog.ChannelLog("demo-chat")
    chatlog.mark_read("demo-chat", 0)  # user has the channel open right now
    lg.append("assistant", text="done")
    inbox.check_and_notify()
    assert sent == []
    lg.append("alert", text="boom", title="Watchdog")  # alerts always push
    inbox.check_and_notify()
    assert len(sent) == 1 and "Watchdog" in sent[0][0]


def test_notify_route_checks_project_and_forwards(chat_project, monkeypatch):
    import agent_team.app as mod
    from agent_team.chat import web

    calls = []

    async def fake_forward(method, path, body=None):
        calls.append((method, path, body))
        return {"seq": 1}

    monkeypatch.setattr(web, "_forward", fake_forward)
    monkeypatch.setattr(mod, "_internal_client", lambda r: True)
    c = TestClient(mod.app)
    assert c.post("/p/demo/api/notify/demo-chat", json={"text": "disk full", "title": "Watchdog"}).status_code == 200
    assert calls == [("POST", "/channels/demo-chat/alert", {"text": "disk full", "title": "Watchdog", "source": None})]
    assert c.post("/p/other/api/notify/demo-chat", json={"text": "x"}).status_code == 404
    assert c.post("/p/demo/api/notify/demo-chat", json={"text": " "}).status_code == 400


def test_channels_config_parsing(tmp_path, monkeypatch):
    cfg = tmp_path / "projects.yaml"
    cfg.write_text("""
projects:
  xx:
    name: X
    tasks: false
    chat:
      context_file: /ctx.md
      defaults: {backend: claude, claude_model: opus}
      channels:
        - {id: x-main, cwd: /w/x}
        - {id: x-sub, cwd: /w/x/sub, backend: codex, label: Sub}
  yy:
    chat: {enabled: false, channels: [{id: y-main, cwd: /w/y}]}
""")
    reg = projects.load(cfg)
    chans = reg["xx"].channels
    assert [c.id for c in chans] == ["x-main", "x-sub"]
    assert chans[0].context_file == "/ctx.md" and chans[0].claude_model == "opus"
    assert chans[1].backend == "codex" and chans[1].label == "Sub"
    assert reg["yy"].channels == () and reg["xx"].tasks is False


def test_notify_network_reaches_notify_only(chat_project, monkeypatch):
    import agent_team.app as mod
    from agent_team.chat import web

    async def fake_forward(method, path, body=None):
        return {"seq": 1}

    monkeypatch.setattr(web, "_forward", fake_forward)
    monkeypatch.setattr(mod, "_notify_client", lambda r: True)
    c = TestClient(mod.app)
    assert c.post("/p/demo/api/notify/demo-chat", json={"text": "x"}).status_code == 200
    assert c.get("/p/demo/api/tasks").status_code == 403          # no tasks API
    assert c.post("/p/demo/api/tasks", json={"title": "t"}).status_code == 403
    assert c.get("/api/inbox").status_code == 403


def test_delete_hides_messages_but_not_open_requests(chat_project):
    lg = chatlog.ChannelLog("demo-chat")
    lg.append("alert", text="disk 95%", title="Watchdog")          # 1
    lg.append("alert", text="cpu high", title="Monitoring")        # 2
    lg.append("assistant", text="hi")                               # 3
    lg.append("permission", req_id="r1", title="Bash", detail="x")  # 4
    assert chatlog.delete("demo-chat", [3, 4]) == [3]              # open request stays
    assert [e["seq"] for e in chatlog.read_events("demo-chat")] == [1, 2, 4]
    assert chatlog.delete("demo-chat", all_alerts=True) == [1, 2]
    assert [e["seq"] for e in chatlog.read_events("demo-chat")] == [4]
    # deleted alerts leave the inbox; the open approval does not
    assert [i["kind"] for i in inbox.chat_items(chat_project)] == ["approval"]
    # new events after a delete are visible
    lg.append("alert", text="new", title="Watchdog")
    assert [e["seq"] for e in chatlog.read_events("demo-chat", after=4)] == [5]


def test_delete_resolved_requests(chat_project):
    lg = chatlog.ChannelLog("demo-chat")
    lg.append("permission", req_id="r1", title="Bash", detail="x")  # 1
    lg.append("question", req_id="r2", questions=[])                # 2
    lg.append("resolved", req_id="r1", outcome="einmal erlaubt")    # 3
    assert chatlog.delete("demo-chat", [1, 2]) == [1]               # r2 still open
    assert chatlog.delete("demo-chat", [1]) == [1]                  # already hidden: still reported
    lg.append("resolved", req_id="r2", outcome="Birne")             # 4
    assert chatlog.delete("demo-chat", [2]) == [2]


def test_delete_everything_keeps_open_requests(chat_project):
    lg = chatlog.ChannelLog("demo-chat")
    lg.append("user", text="hi")                                    # 1
    lg.append("tool_use", text="Bash(ls)")                          # 2
    lg.append("bg", text="bg task done")                            # 3
    lg.append("permission", req_id="r1", title="Bash", detail="x")  # 4
    assert chatlog.delete("demo-chat", everything=True) == [1, 2, 3]
    assert [e["seq"] for e in chatlog.read_events("demo-chat")] == [4]
    assert chatlog.delete("demo-chat", everything=True) == []       # nothing new


def test_delete_endpoint(chat_project, monkeypatch):
    import agent_team.app as mod
    monkeypatch.setattr(mod, "_jwks", object())
    monkeypatch.setattr(mod, "access_identity", lambda r: "me")
    lg = chatlog.ChannelLog("demo-chat")
    lg.append("alert", text="a")
    c = TestClient(mod.app)
    r = c.post("/api/chat/demo-chat/delete", json={"seqs": [1]})
    assert r.status_code == 200 and r.json() == {"deleted": [1]}
    assert c.post("/api/chat/demo-chat/delete", json={"seqs": ["x"]}).status_code == 400


def test_stream_skips_deleted_and_resumes_from_last_event_id(chat_project):
    import asyncio

    from agent_team.chat import web

    lg = chatlog.ChannelLog("demo-chat")
    for t in ("a", "b", "c"):
        lg.append("assistant", text=t)                              # 1..3
    chatlog.delete("demo-chat", [2])

    class Req:  # disconnects after one poll round
        def __init__(self, headers):
            self.headers, self.n = headers, 0

        async def is_disconnected(self):
            self.n += 1
            return self.n > 1

    async def collect(headers):
        resp = await web.stream("demo-chat", Req(headers), after=0)
        return b"".join([c async for c in resp.body_iterator]).decode()

    out = asyncio.run(collect({}))
    assert "id: 1\n" in out and "id: 2\n" not in out and "id: 3\n" in out
    out = asyncio.run(collect({"last-event-id": "1"}))              # reconnect
    assert "id: 1\n" not in out and "id: 3\n" in out


def test_archive_endpoint(chat_project, monkeypatch):
    import agent_team.app as mod
    from agent_team.chat import web
    monkeypatch.setattr(mod, "_jwks", object())
    monkeypatch.setattr(mod, "access_identity", lambda r: "me")
    c = TestClient(mod.app)
    assert c.post("/api/chat/demo-chat/archive", json={"archived": True}).json() == {"archived": True}
    assert web.channel_summary(projects.get_channel("demo-chat"))["archived"] is True
    c.post("/api/chat/demo-chat/archive", json={"archived": False})
    assert chatlog.archived("demo-chat") is False


def test_check_opens_check_chat_once(chat_project, monkeypatch):
    import agent_team.app as mod
    from agent_team.chat import web
    monkeypatch.setattr(mod, "_jwks", object())
    monkeypatch.setattr(mod, "access_identity", lambda r: "me")
    calls = []

    async def fake_forward(method, path, body=None):
        calls.append((method, path, body))
        chatlog.ChannelLog(path.split("/")[2]).append("user", text=body["text"])
        return {}

    monkeypatch.setattr(web, "_forward", fake_forward)
    lg = chatlog.ChannelLog("demo-chat")
    lg.append("assistant", text="hi")                                   # 1
    lg.append("alert", title="Disk voll", text="95 %", source="watchdog")  # 2
    c = TestClient(mod.app)
    assert c.post("/api/chat/demo-chat/check", json={"seq": 1}).status_code == 404  # not an alert
    r = c.post("/api/chat/demo-chat/check", json={"seq": 2})
    assert r.status_code == 200 and r.json() == {"channel": "check-demo-chat-2"}
    ch = projects.get_channel("check-demo-chat-2")
    assert ch.check_of == "demo-chat:2" and ch.project == "demo" and ch.cwd == "/tmp"
    assert len(calls) == 1 and "Disk voll" in calls[0][2]["text"] and "**Löschen**" in calls[0][2]["text"]
    assert c.post("/api/chat/demo-chat/check", json={"seq": 2}).json() == {"channel": "check-demo-chat-2"}
    assert len(calls) == 1  # second tap reopens, no second briefing
    assert web.channel_summary(ch)["check_of"] == "demo-chat:2"


def test_custom_chat_in_a_repo(chat_project, monkeypatch, tmp_path):
    import agent_team.app as mod
    from agent_team.chat import web
    (chat_project.team_dir).mkdir(parents=True)
    (chat_project.team_dir / "team.yaml").write_text("repos: [demo-app]\n")
    monkeypatch.setattr(projects, "_custom_cache", (-1.0, {}))
    monkeypatch.setattr(mod, "_jwks", object())
    monkeypatch.setattr(mod, "access_identity", lambda r: "me")
    sent = []

    async def fake_forward(method, path, body=None):
        sent.append((path, body))
        return {}
    monkeypatch.setattr(web, "_forward", fake_forward)
    c = TestClient(mod.app)
    assert c.get("/api/chat/new-options").json() == [{"project": "demo", "name": "Demo", "root": "tmp",
                                                      "repos": ["demo-app"]}]
    assert c.post("/api/chat/custom", json={"project": "demo", "repo": "nope", "title": "x"}).status_code == 400
    r = c.post("/api/chat/custom", json={"project": "demo", "repo": "demo-app", "title": "Umbau Swing",
                                         "message": "Los geht's"}).json()
    ch = projects.get_channel(r["id"])
    assert ch.custom and ch.cwd == "/tmp/demo-app" and ch.label == "Umbau Swing" and r["custom"] is True
    assert sent == [(f"/channels/{ch.id}/message", {"text": "Los geht's"})]
    # replies of own chats reach the inbox like configured channels
    chatlog.ChannelLog(ch.id).append("assistant", text="Antwort")
    assert any(i["channel"] == ch.id for i in inbox.chat_items(chat_project))


def test_idea_from_link_goes_to_the_lead(chat_project, monkeypatch):
    import dataclasses

    import agent_team.app as mod
    from agent_team.chat import web
    monkeypatch.setattr(mod, "_jwks", object())
    monkeypatch.setattr(mod, "access_identity", lambda r: "me")
    calls = []

    async def fake_forward(method, path, body=None):
        calls.append((method, path, body))
        return {"started": True}

    monkeypatch.setattr(web, "_forward", fake_forward)
    c = TestClient(mod.app)
    body = {"project": "demo", "link": "https://x.com/a/status/1", "text": "Paper!", "note": "für weekly?"}
    assert c.post("/api/chat/idea", json=body).status_code == 400            # no lead channel yet
    lead = projects.Channel(id="demo-lead", project="demo", label="Lead", cwd="/tmp", lead=True)
    p = dataclasses.replace(chat_project, channels=(*chat_project.channels, lead))
    projects.register(p)
    monkeypatch.setattr(projects, "all_projects", lambda: [p])
    assert c.post("/api/chat/idea", json={**body, "link": "ftp://x"}).status_code == 400
    r = c.post("/api/chat/idea", json=body)
    assert r.status_code == 200 and r.json() == {"channel": "demo-lead"}
    method, path, sent = calls[-1]
    assert path == "/channels/demo-lead/message"
    assert "https://x.com/a/status/1" in sent["text"] and "Paper!" in sent["text"] and "für weekly?" in sent["text"]
    assert "api.fxtwitter.com" in sent["text"] and "NICHT vorschlagen/starten" in sent["text"]

"""Tasks v2 lead: propose / park, the start route refuses agents, the lead
channel records button taps."""

from __future__ import annotations

import dataclasses
import importlib

import pytest
from fastapi.testclient import TestClient

from agent_team import projects
from agent_team.tasks import db


@pytest.fixture()
def app_client(monkeypatch, demo_project):
    monkeypatch.setenv("AT_ACCESS_TEAM", "example.cloudflareaccess.com")
    monkeypatch.setenv("AT_ACCESS_AUD", "aud")
    import agent_team.app as mod
    mod = importlib.reload(mod)
    from agent_team.chat import web

    from agent_team.tasks import mirror
    monkeypatch.setattr(projects, "DATA_ROOT", demo_project.data_dir)
    monkeypatch.setattr(projects, "_task_cache", (-1.0, {}))
    monkeypatch.setattr(mirror, "_post", lambda *a: None)
    lead = projects.Channel(id="demo-lead", project="demo", label="Lead", cwd="/tmp", lead=True)
    p = dataclasses.replace(demo_project, channels=(lead,))
    projects.register(p)
    monkeypatch.setattr(projects, "all_projects", lambda: [p])
    calls = []

    async def fake_forward(method, path, body=None):
        calls.append((method, path, body))
        return {"seq": len(calls)}

    monkeypatch.setattr(web, "_forward", fake_forward)
    return mod, TestClient(mod.app), calls


def test_propose_posts_card_and_shows_zur_freigabe(app_client, monkeypatch):
    mod, c, calls = app_client
    monkeypatch.setattr(mod, "_internal_client", lambda r: True)
    tid = c.post("/p/demo/api/tasks", json={"title": "Idee", "repos": ["demo-monitoring"]}).json()["id"]
    r = c.post(f"/p/demo/api/tasks/{tid}/propose", json={"note": "lohnt sich, weil …"})
    assert r.status_code == 200, r.text
    assert r.json()["proposed_at"] and r.json()["lead_note"] == "lohnt sich, weil …"
    method, path, body = calls[-1]
    assert path == "/channels/demo-lead/alert"
    assert [b["data"] for row in body["buttons"] for b in row] == [f"start:{tid}", f"discuss:{tid}", f"park:{tid}"]


def test_agents_cannot_start_a_task(app_client, monkeypatch):
    mod, c, _ = app_client
    monkeypatch.setattr(mod, "_internal_client", lambda r: True)
    tid = c.post("/p/demo/api/tasks", json={"title": "Idee", "repos": ["demo-monitoring"]}).json()["id"]
    r = c.post(f"/p/demo/api/tasks/{tid}/plan")
    assert r.status_code == 403 and "Klick" in r.json()["detail"]
    # the user (Access) can
    monkeypatch.setattr(mod, "_internal_client", lambda r: False)
    monkeypatch.setattr(mod, "access_identity", lambda r: "me@example.com")
    assert c.post(f"/p/demo/api/tasks/{tid}/plan").json()["status"] == "planning"


def test_lead_channel_records_taps(app_client, monkeypatch):
    mod, c, calls = app_client
    monkeypatch.setattr(mod, "_internal_client", lambda r: False)
    monkeypatch.setattr(mod, "access_identity", lambda r: "me@example.com")
    r = c.post("/api/service/demo-lead/command", json={"text": "start:3", "label": "Freigeben & starten",
                                                           "button_seq": 9})
    assert r.status_code == 200, r.text
    assert calls[-1][1] == "/channels/demo-lead/command"


def test_park_and_propose_rules():
    t = db.create_task("x", "d", 2, [])
    assert db.propose_task(t["id"], "why")["proposed_at"]
    parked = db.park_task(t["id"], "Duplikat von #1")
    assert parked["status"] == "backlog" and parked["proposed_at"] is None
    assert parked["lead_note"] == "Duplikat von #1"
    assert db.propose_task(t["id"], "again") is None  # only new / refined


def test_merge_agents_only_their_own_ideas(app_client, monkeypatch):
    mod, c, _ = app_client
    monkeypatch.setattr(mod, "_internal_client", lambda r: True)
    target = db.create_task("Ziel", "a", 2, [])
    user = db.create_task("Meins", "b", 2, [])
    scout = db.create_task("Scout", "c\n\n(Vorschlag des Scouts tests)", 2, [])
    assert c.post(f"/p/demo/api/tasks/{user['id']}/merge", json={"into": target["id"]}).status_code == 403
    r = c.post(f"/p/demo/api/tasks/{scout['id']}/merge", json={"into": target["id"], "note": "Duplikat"})
    assert r.status_code == 200 and "Aus #%d übernommen: Scout" % scout["id"] in r.json()["details"]
    merged = db.get_task(scout["id"])
    assert merged["status"] == "closed" and merged["lead_note"] == f"Zusammengelegt in #{target['id']}: Duplikat"
    # the user's click may merge the user's own task
    monkeypatch.setattr(mod, "_internal_client", lambda r: False)
    monkeypatch.setattr(mod, "access_identity", lambda r: "me@example.com")
    assert c.post(f"/p/demo/api/tasks/{user['id']}/merge", json={"into": target["id"]}).status_code == 200


def test_close_agents_only_their_own_ideas(app_client, monkeypatch):
    mod, c, _ = app_client
    monkeypatch.setattr(mod, "_internal_client", lambda r: True)
    user = db.create_task("Meins", "b", 2, [])
    scout = db.create_task("Scout", "c\n\n(Folge-Task von #3, angelegt vom Lead)", 2, [])
    assert c.post(f"/p/demo/api/tasks/{user['id']}/close", json={"reason": "überholt"}).status_code == 403
    r = c.post(f"/p/demo/api/tasks/{scout['id']}/close", json={"reason": "überholt"})
    assert r.status_code == 200 and r.json()["status"] == "closed" and r.json()["lead_note"] == "überholt"
    # closed is its own list, not the active one and not the backlog
    assert scout["id"] not in [t["id"] for t in c.get("/p/demo/api/tasks").json()]
    assert scout["id"] not in [t["id"] for t in c.get("/p/demo/api/tasks?status=backlog").json()]
    assert [t["id"] for t in c.get("/p/demo/api/tasks?status=closed").json()] == [scout["id"]]
    # the user may close their own (no body needed), and reopen
    monkeypatch.setattr(mod, "_internal_client", lambda r: False)
    monkeypatch.setattr(mod, "access_identity", lambda r: "me@example.com")
    assert c.post(f"/p/demo/api/tasks/{user['id']}/close").json()["status"] == "closed"
    assert c.post(f"/p/demo/api/tasks/{user['id']}/restore").json()["status"] == "new"


def test_run_lead_now_writes_trigger(app_client, monkeypatch, demo_project):
    mod, c, _ = app_client
    monkeypatch.setattr(mod, "_internal_client", lambda r: True)
    assert c.post("/p/demo/api/scouts/lead/run").status_code == 403  # agents can't
    monkeypatch.setattr(mod, "_internal_client", lambda r: False)
    monkeypatch.setattr(mod, "access_identity", lambda r: "me@example.com")
    ids = {s["id"]: s for s in c.get("/p/demo/api/scouts").json()}
    assert ids["lead"]["lead"] is True
    assert c.post("/p/demo/api/scouts/nope/run").status_code == 404
    assert c.post("/p/demo/api/scouts/lead/run").json() == {"requested": True}
    assert (demo_project.control_dir / "scouts" / "lead.run").exists()
    assert c.get("/p/demo/api/scouts").json()[[s["id"] for s in c.get("/p/demo/api/scouts").json()].index("lead")]["requested"]


def test_watchdog_dismiss_is_the_users_tap(app_client, monkeypatch, demo_project):
    import json as _json

    from agent_team.chat import log as chatlog
    mod, c, _ = app_client
    wd = projects.Channel(id="demo-watchdog", project="demo", label="Watchdog", cwd="/tmp")
    p = dataclasses.replace(projects.get("demo"), channels=(*projects.get("demo").channels, wd))
    projects.register(p)
    monkeypatch.setattr(projects, "all_projects", lambda: [p])
    log = chatlog.ChannelLog("demo-watchdog")
    a1 = log.append("alert", text="stale", title="Stale")
    a2 = log.append("alert", text="db", title="DB")
    monkeypatch.setattr(mod, "_internal_client", lambda r: True)
    assert [a["seq"] for a in c.get("/p/demo/api/watchdog").json()] == [a1["seq"], a2["seq"]]
    d = demo_project.control_dir / "watchdog"
    d.mkdir(parents=True)
    (d / "k1.json").write_text(_json.dumps({"alerts": {"demo-watchdog": [a1["seq"]], "other-channel": [1]}}))
    assert c.post("/p/demo/api/watchdog/dismiss/k1").status_code == 403  # agents can't
    monkeypatch.setattr(mod, "_internal_client", lambda r: False)
    monkeypatch.setattr(mod, "access_identity", lambda r: "me@example.com")
    assert c.post("/p/demo/api/watchdog/dismiss/k1").json() == {"deleted": 1, "already": 0}
    assert c.post("/p/demo/api/watchdog/dismiss/k1").json() == {"deleted": 0, "already": 1}  # second tap
    assert c.post("/p/demo/api/watchdog/dismiss/nope").status_code == 404
    monkeypatch.setattr(mod, "_internal_client", lambda r: True)
    assert [a["seq"] for a in c.get("/p/demo/api/watchdog").json()] == [a2["seq"]]


def test_apply_branch_is_the_users_tap(app_client, monkeypatch, demo_project):
    import json as _json

    from agent_team.tasks import mirror
    mod, c, _ = app_client
    monkeypatch.setattr(mod, "_internal_client", lambda r: False)
    monkeypatch.setattr(mod, "access_identity", lambda r: "me@example.com")
    tid = c.post("/p/demo/api/tasks", json={"title": "Idee", "repos": ["demo-swing"]}).json()["id"]
    assert c.get(f"/p/demo/api/tasks/{tid}/changes").json() == {"state": None, "diff": ""}
    assert c.post(f"/p/demo/api/tasks/{tid}/apply").status_code == 409   # ran in the live checkout
    d = demo_project.control_dir / "worktrees"
    d.mkdir(parents=True, exist_ok=True)
    st = {"branch": f"task/{tid}", "pending": True,
          "repos": {"demo-swing": {"base": "main", "ahead": 2, "stat": "1 file changed"}}}
    (d / f"{tid}.json").write_text(_json.dumps(st))
    (d / f"{tid}.diff").write_text("+x = 2\n")
    assert c.get(f"/p/demo/api/tasks/{tid}/changes").json() == {"state": st, "diff": "+x = 2\n"}
    monkeypatch.setattr(mod, "_internal_client", lambda r: True)
    assert c.post(f"/p/demo/api/tasks/{tid}/apply").status_code == 403   # agents never merge
    monkeypatch.setattr(mod, "_internal_client", lambda r: False)
    assert c.post(f"/p/demo/api/tasks/{tid}/apply").json() == {"requested": True}
    assert (d / f"{tid}.apply").exists()
    with projects.use("demo"):
        card = mirror.done_card(db.get_task(tid), [])
    assert [b["data"] for row in card["buttons"] for b in row] == [f"apply:{tid}", f"archive:{tid}"]
    assert card["buttons"][0][0]["label"] == "In main übernehmen (mergen, ohne Deploy)"
    assert "task/" in card["text"] and "Committen" not in str(card["buttons"])

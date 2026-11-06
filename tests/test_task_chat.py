"""Task chats (Tasks v2): created on start, pipeline mirrored into them."""

from __future__ import annotations

import dataclasses
import importlib

import pytest
from fastapi.testclient import TestClient

from agent_team import projects
from agent_team.tasks import db, mirror


@pytest.fixture()
def lead_project(monkeypatch, tmp_path, demo_project):
    monkeypatch.setattr(projects, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(projects, "_task_cache", (-1.0, {}))
    lead = projects.Channel(id="demo-lead", project="demo", label="Lead", cwd="/w", lead=True,
                            persona="/w/lead.md")
    p = dataclasses.replace(demo_project, channels=(lead,))
    projects.register(p)
    posts = []
    monkeypatch.setattr(mirror, "_post", lambda ch, path, body: posts.append((ch, path, body)))
    return p, posts


def test_start_creates_task_chat_with_intro(lead_project, monkeypatch):
    p, posts = lead_project
    monkeypatch.setenv("AT_ACCESS_TEAM", "example.cloudflareaccess.com")
    monkeypatch.setenv("AT_ACCESS_AUD", "aud")
    import agent_team.app as mod
    mod = importlib.reload(mod)
    monkeypatch.setattr(mod, "_internal_client", lambda r: False)
    monkeypatch.setattr(mod, "access_identity", lambda r: "me@example.com")
    c = TestClient(mod.app)
    t = db.create_task("Test-DB", "d", 2, ["demo-lstm"])
    db.propose_task(t["id"], "weil sicherer")
    assert c.post(f"/p/demo/api/tasks/{t['id']}/plan").status_code == 200
    ch = projects.get_channel(f"task-demo-{t['id']}")
    assert ch.task_id == t["id"] and ch.lead and ch.persona == "/w/lead.md"
    assert projects.lead_channel("demo").id == "demo-lead"  # task chats are not the lead channel
    assert posts[-1][0] == ch.id and "weil sicherer" in posts[-1][2]["text"]


def test_changes_describe_the_pipeline():
    task = {"id": 7, "status": "running", "paused_reason": None}
    stages = [{"seq": 1, "role": "implementation-engineer", "goal": "bauen", "status": "running"},
              {"seq": 2, "role": "integration-ops", "goal": "deployen", "status": "pending"}]
    first = mirror.changes(task, stages, None)
    assert [a["title"] for a in first][:2] == ["Plan für #7: 2 Stufen", "▶ Stufe 1 · Implementation engineer"] \
        or first[0]["title"].startswith("Plan für #7")
    old = mirror.snapshot(task, stages)
    stages[0].update(status="done", verdict="pass", summary="fertig gebaut")
    task.update(status="plan_ready")
    out = mirror.changes(task, stages, old)
    assert out[0]["title"].startswith("✓ Stufe 1") and out[0]["text"] == "fertig gebaut"
    assert out[1]["title"].startswith("🔐 Freigabe nötig: Stufe 2")  # integration-ops = confirm role
    assert out[1]["buttons"][0][0]["data"] == "next:7"
    assert mirror.changes(task, stages, mirror.snapshot(task, stages)) == []


def test_tick_posts_once(lead_project):
    p, posts = lead_project
    t = db.create_task("x", "d", 2, [])
    db.fail_task(t["id"], "kaputt")
    projects.register_task_channel("demo", t["id"], "x")
    token = projects._current.set(None)  # the loop runs without a project context
    mirror.tick()
    projects._current.reset(token)
    assert posts, "first sight posts the current state"
    n = len(posts)
    token = projects._current.set(None)
    mirror.tick()
    projects._current.reset(token)
    assert len(posts) == n  # nothing changed -> nothing posted


def test_forward_event_maps_text_and_tools(lead_project):
    p, posts = lead_project
    t = db.create_task("x", "d", 2, ["demo-monitoring"])
    projects.register_task_channel("demo", t["id"], "x")
    mirror.forward_event("demo", t["id"], "plan", {"event": "text", "text": "Ich plane.\n# Plan\n```json{}```"})
    mirror.forward_event("demo", t["id"], "plan", {"event": "tool_use", "name": "Bash", "input": {"command": "ls"}})
    mirror.forward_event("demo", t["id"], "plan", {"event": "thinking"})
    assert [b for _, _, b in posts] == [
        {"type": "assistant", "text": "Ich plane.", "author": "Planung"},
        {"type": "tool_use", "name": "Bash", "text": "Bash(ls)"},
    ]


def test_confirm_role_card_says_deploy(lead_project):
    task = {"id": 8, "status": "plan_ready", "paused_reason": None}
    stages = [{"seq": 1, "role": "reviewer", "goal": "prüfen", "status": "done", "verdict": "pass", "summary": "ok"},
              {"seq": 2, "role": "integration-ops", "goal": "Container starten", "status": "pending"}]
    old = {"status": "running", "paused": "", "stages": {"1": ["running", ""], "2": ["pending", ""]}}
    card = mirror.changes(task, stages, old)[-1]
    assert card["title"].startswith("🔐 Freigabe nötig: Stufe 2") and "Live-System" in card["title"]
    assert card["buttons"][0][0]["data"] == "next:8"


def test_channel_summary_shows_task_working(lead_project):
    from agent_team.chat import web

    t = db.create_task("x", "d", 2, [])
    ch = projects.register_task_channel("demo", t["id"], "x")
    assert web.channel_summary(ch)["task"] == {"status": "new", "working": False}
    db.request_plan(t["id"])  # the worker's planning agent runs now
    assert web.channel_summary(ch)["task"] == {"status": "planning", "working": True}
    assert web.channel_summary(projects.get_channel("demo-lead"))["task"] is None


def test_question_with_options_becomes_buttons_and_answers(lead_project, monkeypatch):
    from agent_team.tasks import stage_result
    res = stage_result.parse_stage_result('# Stage result\n```json\n{"verdict": "needs_input", "summary": "s", '
                             '"questions": ["Wer führt die Migration aus?"], '
                             '"options": ["Stufe darf sie selbst ausführen", "Ich mache es selbst"]}\n```', "TE")
    assert res.options == ["Stufe darf sie selbst ausführen", "Ich mache es selbst"]
    task = {"id": 9, "status": "awaiting_input", "paused_reason": None}
    stages = [{"seq": 4, "role": "test-engineer", "status": "awaiting_input", "questions": res.questions,
               "options": res.options}]
    card = mirror.changes(task, stages, {"status": "running", "paused": "", "stages": {"4": ["running", ""]}})[0]
    assert [r[0]["data"] for r in card["buttons"]][:2] == ["opt:9:4:0", "opt:9:4:1"]
    assert "1. Stufe darf sie selbst ausführen" in card["text"]


def test_done_card_is_readable_and_asks_to_commit(lead_project):
    task = {"id": 113, "status": "done", "repos": ["demo-swing"]}
    stages = [{"seq": 1, "role": "implementation-engineer", "status": "done", "verdict": "pass", "summary": "Umgestellt."},
              {"seq": 2, "role": "reviewer", "status": "done", "verdict": "pass", "summary": "Passt."}]
    card = mirror.done_card(task, stages)
    assert "✓ 1. Implementation Engineer\nUmgestellt." in card["text"]
    assert [r[0]["data"] for r in card["buttons"]] == ["commit:113", "deploy:113", "archive:113"]
    assert card["buttons"][-1][0]["label"] == "Abschließen – Chat archivieren"
    stages.append({"seq": 3, "role": "integration-ops", "status": "done", "verdict": "pass", "summary": "Committet."})
    assert [r[0]["data"] for r in mirror.done_card(task, stages)["buttons"]] == ["archive:113"]


def test_start_of_finished_task_says_so(lead_project, monkeypatch):
    monkeypatch.setenv("AT_ACCESS_TEAM", "example.cloudflareaccess.com")
    monkeypatch.setenv("AT_ACCESS_AUD", "aud")
    import agent_team.app as mod
    mod = importlib.reload(mod)
    monkeypatch.setattr(mod, "_internal_client", lambda r: False)
    monkeypatch.setattr(mod, "access_identity", lambda r: "me@example.com")
    c = TestClient(mod.app)
    t = db.create_task("x", "d", 2, ["demo-lstm"])
    assert c.post(f"/p/demo/api/tasks/{t['id']}/plan").status_code == 200
    r = c.post(f"/p/demo/api/tasks/{t['id']}/plan")              # second tap while planning
    assert r.status_code == 409 and "läuft bereits" in r.json()["detail"]
    done = db.create_task("y", "d", 2, ["demo-lstm"])
    assert db.mark_done(done["id"])["status"] == "done"
    r = c.post(f"/p/demo/api/tasks/{done['id']}/plan")
    assert r.status_code == 409 and "schon erledigt" in r.json()["detail"]


def test_stopped_task_card_offers_ways_out(lead_project):
    task = {"id": 131, "status": "blocked", "stop_reason": "exit criteria unmet, orchestrator proposed no further stages"}
    stages = [{"seq": 1, "role": "implementation-engineer", "status": "done", "verdict": "pass", "summary": "ok"},
              {"seq": 2, "role": "reviewer", "status": "done", "verdict": "blocked", "summary": "fehlt was"}]
    card = mirror.stuck_card(task, stages, "⛔ Gestoppt", task["stop_reason"])
    assert [b["data"] for row in card["buttons"] for b in row] == ["retry:131:2", "replan:131", "replannote:131", "done:131", "close:131"]
    assert "Was jetzt?" in card["text"] and "exit criteria unmet" in card["text"]
    stages[1].update(verdict="fail")
    card = mirror.stuck_card(task, stages, "⛔ Gestoppt", "x")
    assert card["buttons"][0][0]["data"] == "retry:131:2"
    stages[1].update(verdict="pass")
    card = mirror.stuck_card(task, stages, "⛔ Gestoppt", "x")
    assert [b["data"] for row in card["buttons"] for b in row] == ["replan:131", "replannote:131", "done:131", "close:131"]
    out = mirror.changes(task, stages, {"status": "running", "paused": "", "stages": mirror.snapshot(task, stages)["stages"]})
    assert out[-1]["title"] == "⛔ Gestoppt" and out[-1]["buttons"]


def test_inbox_item_of_started_task_opens_its_chat(lead_project):
    from agent_team import inbox
    p, _ = lead_project
    with_chat = db.create_task("mit Chat", "d", 2, [])
    without = db.create_task("ohne Chat", "d", 2, [])
    projects.register_task_channel("demo", with_chat["id"], "mit Chat")
    db.fail_task(with_chat["id"], "kaputt")
    db.fail_task(without["id"], "kaputt")
    links = {i["task_id"]: i["link"] for i in inbox.items_for(p)}
    assert links[with_chat["id"]] == f"/chat/task-demo-{with_chat['id']}"
    assert links[without["id"]] == f"/p/demo/tasks?task={without['id']}"
    from agent_team.chat import log as chatlog
    chatlog.set_archived(f"task-demo-{with_chat['id']}", True)     # archived chat: no more nagging
    assert [i["task_id"] for i in inbox.items_for(p)] == [without["id"]]


def test_stopped_task_gets_a_german_explanation_once(lead_project):
    running = {"status": "running", "paused": "", "stages": {}}
    task = {"id": 131, "status": "blocked", "stop_reason": "exit criteria unmet (the final stage is not a reviewer)"}
    ask = mirror.explain_request(task, running)
    assert "#131" in ask["note"] and "gestoppt" in ask["note"]
    assert "the final stage is not a reviewer" in ask["text"] and "German" in ask["text"]
    assert mirror.explain_request(task, {**running, "status": "blocked"}) is None   # already stopped
    assert mirror.explain_request(task, None) is None                                # first sight
    assert mirror.explain_request({**task, "stop_reason": "stopped by user"}, running) is None
    failed = mirror.explain_request({"id": 7, "status": "failed", "error": "boom"}, running)
    assert "fehlgeschlagen" in failed["note"] and "boom" in failed["text"]
    assert mirror.explain_request({"id": 7, "status": "done"}, running) is None


def _data(actions):
    return [a["data"] for a in actions]


def _task(status, **kw):
    return {"id": 5, "status": status, "mode": "pipeline", "paused_reason": None, "repos": [], **kw}


def test_every_stuck_or_waiting_state_has_a_way_out(lead_project):
    impl = {"seq": 1, "role": "implementation-engineer", "status": "done", "verdict": "pass"}
    deploy = {"seq": 2, "role": "integration-ops", "status": "pending", "goal": "live"}
    review = {"seq": 2, "role": "reviewer", "status": "done", "verdict": "fail"}
    # approval of a live stage: not the only answer any more
    a = mirror.available_actions(_task("plan_ready"), [impl, deploy])
    assert a[0]["data"] == "next:5" and a[0]["label"].endswith("freigeben") and a[0]["kind"] == "primary"
    assert {"skip:5:2", "replan:5", "replannote:5", "done:5", "close:5"} <= set(_data(a))
    # a reviewer's fail at the stop can be rerun, a gate stage never skipped
    a = _data(mirror.available_actions(_task("blocked"), [impl, review]))
    assert a[0] == "retry:5:2" and not any(d.startswith("skip:") for d in a)
    # question: answers plus leaving it
    q = {"seq": 2, "role": "data-engineer", "status": "awaiting_input", "options": ["A", "B"]}
    a = mirror.available_actions(_task("awaiting_input"), [impl, q])
    assert _data(a) == ["opt:5:2:0", "opt:5:2:1", "skip:5:2", "stop:5"] and a[-1]["confirm"]
    # stopped by the user with stages left
    assert "resume:5" in _data(mirror.available_actions(_task("blocked"), [impl, deploy]))
    # running: only stopping, and it asks first
    a = mirror.available_actions(_task("running"), [impl])
    assert _data(a) == ["stop:5"] and a[0]["confirm"]
    # budget pause: may go on by hand; a time wait may not
    assert "next:5" in _data(mirror.available_actions(_task("plan_ready", paused_reason="Budget: Tag"), [impl, deploy]))
    assert "next:5" not in _data(mirror.available_actions(_task("plan_ready", paused_reason="Wartet bis 2026"), [impl, deploy]))
    assert mirror.available_actions(_task("closed"), [])[0]["data"] == "restore:5"


def test_limit_gets_a_card(lead_project):
    task = _task("limit_reached", error="limit")
    stages = [{"seq": 1, "role": "reviewer", "status": "failed", "error": "limit"}]
    old = {"status": "running", "paused": "", "stages": mirror.snapshot(task, stages)["stages"]}
    card = mirror.changes(task, stages, old)[-1]
    assert card["title"].startswith("⏳") and card["buttons"][0][0]["data"] == "retry:5:1"


def test_stopped_task_with_branch_offers_taking_it_over(lead_project, monkeypatch):
    monkeypatch.setattr(mirror, "worktree_state", lambda tid: {"pending": True, "repos": {}})
    stages = [{"seq": 1, "role": "implementation-engineer", "status": "done", "verdict": "pass"}]
    assert "apply:5" in _data(mirror.available_actions(_task("blocked"), stages))


def test_button_rows_keep_the_confirm_and_drop_the_kind():
    rows = mirror.button_rows([{"label": "a", "data": "next:1", "kind": "primary"},
                               {"label": "b", "data": "x:1", "kind": "secondary"},
                               {"label": "c", "data": "y:1", "kind": "secondary"},
                               {"label": "d", "data": "stop:1", "kind": "danger", "confirm": "sicher?"}])
    assert rows == [[{"label": "a", "data": "next:1"}], [{"label": "b", "data": "x:1"}, {"label": "c", "data": "y:1"}],
                    [{"label": "d", "data": "stop:1", "confirm": "sicher?"}]]


def test_lead_summarises_before_release_and_at_the_end(lead_project):
    done = {"seq": 1, "role": "implementation-engineer", "status": "done", "verdict": "pass"}
    deploy = {"seq": 2, "role": "integration-ops", "status": "pending"}
    running = {"status": "running", "paused": "", "stages": {}}
    ask = mirror.summary_request(_task("plan_ready"), [done, deploy], running)
    assert "Bei Freigabe passiert" in ask["text"] and "vor der Freigabe" in ask["note"]
    ask = mirror.summary_request(_task("done"), [done], running)
    assert "**Offen:**" in ask["text"]
    # once per state; nothing for a plain next stage, a pause, or a task ticked off by hand
    assert mirror.summary_request(_task("plan_ready"), [done, deploy], {"status": "plan_ready", "paused": ""}) is None
    impl = {"seq": 2, "role": "implementation-engineer", "status": "pending"}
    assert mirror.summary_request(_task("plan_ready"), [done, impl], running) is None
    assert mirror.summary_request(_task("plan_ready", paused_reason="Budget: x"), [done, deploy], running) is None
    assert mirror.summary_request(_task("done"), [done], {"status": "blocked", "paused": ""}) is None
    # budget pause over: the approval is new, so the summary comes now
    assert mirror.summary_request(_task("plan_ready"), [done, deploy], {"status": "plan_ready", "paused": "Budget: x"})


def test_summary_can_be_requested_until_there_is_one(lead_project):
    done = {"seq": 1, "role": "implementation-engineer", "status": "done", "verdict": "pass"}
    assert _data(mirror.summary_action(_task("blocked"), [done])) == ["summarize:5"]
    assert mirror.summary_action(_task("blocked", user_summary="x"), [done]) == []
    assert mirror.summary_action(_task("running"), [done]) == []

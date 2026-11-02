import importlib

from fastapi.testclient import TestClient


def _client(monkeypatch, configured: bool) -> TestClient:
    if configured:
        monkeypatch.setenv("AT_ACCESS_TEAM", "example.cloudflareaccess.com")
        monkeypatch.setenv("AT_ACCESS_AUD", "aud")
    else:
        monkeypatch.delenv("AT_ACCESS_TEAM", raising=False)
        monkeypatch.delenv("AT_ACCESS_AUD", raising=False)
    import agent_team.app as mod

    return TestClient(importlib.reload(mod).app)


def test_healthz_is_public(monkeypatch):
    c = _client(monkeypatch, configured=True)
    r = c.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


def test_healthz_subpath_is_not_public(monkeypatch):
    c = _client(monkeypatch, configured=True)
    assert c.get("/healthz/x").status_code == 403


def test_without_token_forbidden(monkeypatch):
    c = _client(monkeypatch, configured=True)
    assert c.get("/").status_code == 403
    assert c.get("/api/me", headers={"cf-access-jwt-assertion": "garbage"}).status_code == 403


def test_unconfigured_fails_closed(monkeypatch):
    c = _client(monkeypatch, configured=False)
    assert c.get("/").status_code == 503
    assert c.get("/healthz").status_code == 200


def test_push_endpoints_need_access(monkeypatch):
    c = _client(monkeypatch, configured=True)
    assert c.get("/api/push/key").status_code == 403
    assert c.post("/api/push/test", json={}).status_code == 403
    assert c.get("/sw.js").status_code == 403


def test_vapid_key_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("AT_DATA_DIR", str(tmp_path))
    import agent_team.push as push

    push = importlib.reload(push)
    k1 = push.public_key()
    assert len(k1) == 87 and push.public_key() == k1  # 65-byte P-256 point, stable once generated
    assert push.subscribe({"endpoint": "https://x/y", "keys": {"p256dh": "a", "auth": "b"}}, "me", "ua") == 1


def test_internal_network_reaches_project_api_only(monkeypatch):
    import ipaddress

    import agent_team.app as mod
    c = _client(monkeypatch, configured=True)
    # TestClient's peer is "testclient" (not an IP) -> never internal.
    assert c.get("/p/demo/api/tasks").status_code == 403
    monkeypatch.setattr(mod, "INTERNAL_NET", ipaddress.ip_network("10.0.0.0/8"))
    monkeypatch.setattr(mod, "_internal_client", lambda r: True)
    assert c.get("/api/me").status_code == 403          # internal: only /p/<id>/api/
    assert c.get("/p/nope/api/tasks").status_code == 404  # unknown project


def test_project_context_reaches_sync_endpoints_and_stream(monkeypatch, demo_project):
    """The async path dependency sets the project; sync endpoints (thread
    pool) and the SSE generator must see it — tasks land in the project's
    data dir."""
    import agent_team.app as mod
    from agent_team import projects
    c = _client(monkeypatch, configured=True)
    monkeypatch.setattr(mod, "_internal_client", lambda r: True)
    # Clear the test thread's project so only the path dependency can set it.
    token = projects._current.set(None)
    r = c.post("/p/demo/api/tasks", json={"title": "hello", "repos": ["demo-monitoring"]})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert (demo_project.data_dir / "tasks.db").exists()
    assert c.get(f"/p/demo/api/tasks/{tid}").json()["title"] == "hello"
    assert c.post(f"/p/demo/api/tasks/{tid}/event/refine", json={"event": "start"}).status_code == 204
    assert (demo_project.data_dir / "sessions" / str(tid) / "refine.jsonl").exists()
    assert "demo-swing" in c.get("/p/demo/api/tasks/roles").json()["live_bot_repos"]
    # worker liveness from internal polls
    assert c.get("/p/demo/api/tasks/worker-defaults").json()["container_running"] is True
    c.post(f"/p/demo/api/tasks/{tid}/done")
    with c.stream("GET", f"/p/demo/api/tasks/{tid}/stream/refine") as resp:
        body = b"".join(resp.iter_bytes())
    assert b"event: hello" in body and b"event: done" in body
    projects._current.reset(token)


def test_inbox_lists_tasks_waiting_for_me(monkeypatch, demo_project):
    from agent_team import inbox, projects
    from agent_team.tasks import db

    with projects.use(demo_project):
        a = db.create_task("needs review", "d", 2, [])
        db.request_refine(a["id"])
        db.complete_refine(a["id"], "spec")
        db.create_task("just new", "d", 2, [])
    monkeypatch.setattr(projects, "all_projects", lambda: [demo_project])
    items = inbox.all_items()
    assert [(i["task_id"], i["kind"]) for i in items] == [(a["id"], "review")]

    sent = []
    monkeypatch.setattr(inbox, "SEEN_FILE", demo_project.data_dir / "seen.json")
    monkeypatch.setattr(inbox.push, "send_all", lambda *a, **k: sent.append(a))
    assert inbox.check_and_notify() == [] and sent == []      # first run: silent
    with projects.use(demo_project):
        b = db.create_task("failing", "d", 2, [])
        db.fail_task(b["id"], "boom")
    new = inbox.check_and_notify()
    assert [i["task_id"] for i in new] == [b["id"]] and len(sent) == 1
    assert inbox.check_and_notify() == []                       # announced once


def test_version_endpoint_hashes_index(monkeypatch, tmp_path):
    import agent_team.app as mod
    monkeypatch.setattr(mod, "_jwks", object())
    monkeypatch.setattr(mod, "access_identity", lambda r: "me")
    (tmp_path / "index.html").write_text("<script src=/assets/index-abc.js>")
    monkeypatch.setattr(mod, "WEB_DIST", tmp_path)
    from fastapi.testclient import TestClient
    c = TestClient(mod.app)
    a = c.get("/api/version").json()["build"]
    (tmp_path / "index.html").write_text("<script src=/assets/index-def.js>")
    b = c.get("/api/version").json()["build"]
    assert a and b and a != b

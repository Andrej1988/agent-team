import importlib
import json

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from agent_team import guard

HEAD = "a" * 40


def _client(monkeypatch, tmp_path, status=None):
    monkeypatch.setenv("AT_DEPLOY_DIR", str(tmp_path))
    import agent_team.deploy as mod
    mod = importlib.reload(mod)

    async def no_busy():
        return []

    monkeypatch.setattr(mod, "_busy", no_busy)
    if status is not None:
        (tmp_path / "status.json").write_text(json.dumps(status))
    app = FastAPI()

    @app.middleware("http")
    async def user(request: Request, call_next):
        request.state.user = request.headers.get("x-user")
        return await call_next(request)

    app.include_router(mod.router)
    return TestClient(app)


def test_disabled_without_env(monkeypatch):
    monkeypatch.delenv("AT_DEPLOY_DIR", raising=False)
    import agent_team.deploy as mod
    mod = importlib.reload(mod)
    app = FastAPI()
    app.include_router(mod.router)
    assert TestClient(app).get("/api/deploy").json() == {"enabled": False}


def test_status_and_request(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path, {"pending": {"head": HEAD, "deployed": "b" * 40, "commits": []}})
    st = c.get("/api/deploy").json()
    assert st["enabled"] and st["pending"]["head"] == HEAD and st["queued"] is None
    r = c.post("/api/deploy", json={"commit": HEAD}, headers={"x-user": "me@example.invalid"})
    assert r.status_code == 200
    req = json.loads((tmp_path / "request.json").read_text())
    assert req["commit"] == HEAD and req["by"] == "me@example.invalid"
    assert c.get("/api/deploy").json()["queued"]["id"] == req["id"]


def test_request_needs_real_user_and_current_head(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path, {"pending": {"head": HEAD}})
    for who in ("internal", "notify"):
        assert c.post("/api/deploy", json={"commit": HEAD}, headers={"x-user": who}).status_code == 403
    assert c.post("/api/deploy", json={"commit": HEAD}).status_code == 403
    assert c.post("/api/deploy", json={"commit": "c" * 40}, headers={"x-user": "me"}).status_code == 409
    assert not (tmp_path / "request.json").exists()


def test_no_second_request_while_running(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path, {"pending": {"head": HEAD}, "job": {"id": "x", "state": "running"}})
    assert c.post("/api/deploy", json={"commit": HEAD}, headers={"x-user": "me"}).status_code == 409


def test_docker_env(monkeypatch):
    monkeypatch.delenv(guard.SOCK_DIR_ENV, raising=False)
    assert guard.docker_env("shop") == {}
    monkeypatch.setenv(guard.SOCK_DIR_ENV, "/run/x/")
    assert guard.docker_env("shop") == {"DOCKER_HOST": "unix:///run/x/shop/docker.sock"}

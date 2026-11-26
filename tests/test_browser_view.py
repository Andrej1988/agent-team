"""Live view of a project's browser: noVNC files and socket via the web
app, for the user only (never agents, never notify clients)."""
from __future__ import annotations

import importlib

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_team import browser_view, projects


@pytest.fixture()
def client(monkeypatch, demo_project):
    monkeypatch.setenv("AT_ACCESS_TEAM", "example.cloudflareaccess.com")
    monkeypatch.setenv("AT_ACCESS_AUD", "aud")
    monkeypatch.setenv("AT_BROWSER_VIEWS", '{"demo": "http://browser-demo:6080"}')
    import agent_team.app as mod
    mod = importlib.reload(mod)
    monkeypatch.setattr(projects, "all_projects", lambda: [demo_project])
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        return httpx.Response(200, content=b"<html>novnc</html>", headers={"content-type": "text/html"})

    real = httpx.AsyncClient
    monkeypatch.setattr(browser_view.httpx, "AsyncClient",
                        lambda timeout=15: real(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(mod, "_internal_client", lambda r: False)
    monkeypatch.setattr(mod, "_notify_client", lambda r: False)
    monkeypatch.setattr(mod, "access_identity", lambda r: "me@example.com")
    return mod, TestClient(mod.app), seen


def test_views_list_and_files(client):
    mod, c, seen = client
    assert c.get("/api/browser").json() == [{"project": "demo", "url": browser_view.view_url("demo")}]
    r = c.get("/browser/demo/vnc.html")
    assert r.status_code == 200 and b"novnc" in r.content
    assert seen == ["http://browser-demo:6080/vnc.html"]
    assert c.get("/browser/other/vnc.html").status_code == 404
    assert c.get("/browser/demo/a%20b").status_code == 400


def test_agents_get_no_view(client, monkeypatch):
    mod, c, _ = client
    monkeypatch.setattr(mod, "_internal_client", lambda r: True)
    assert c.get("/browser/demo/vnc.html").status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/browser/demo/websockify"):
            pass


def test_socket_needs_an_identity(client, monkeypatch):
    mod, c, _ = client
    monkeypatch.setattr(mod, "access_identity", lambda r: None)
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/browser/demo/websockify"):
            pass
    monkeypatch.setattr(mod, "access_identity", lambda r: "me@example.com")
    with pytest.raises(WebSocketDisconnect):  # unknown project
        with c.websocket_connect("/browser/other/websockify"):
            pass

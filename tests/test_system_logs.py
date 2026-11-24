"""Container logs in the web app: Docker stream demux, one clock per line,
masked secrets, grouping, and the endpoints (users only, never agents)."""

from __future__ import annotations

import importlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_team import projects
from agent_team.system import docker_ro, logs


def frame(stream: int, text: str) -> bytes:
    b = text.encode()
    return bytes([stream, 0, 0, 0]) + len(b).to_bytes(4, "big") + b


def test_demux_framed_and_raw_streams():
    d = docker_ro.Demux()
    data = frame(1, "eins\nzw") + frame(2, "ei\n") + frame(1, "drei")
    assert d.feed(data[:5]) == []                # header not complete yet
    assert d.feed(data[5:]) == ["eins", "zwei"]
    assert d.flush() == ["drei"]
    raw = docker_ro.Demux()
    assert raw.feed(b"tty line one\ntty line two\npart") == ["tty line one", "tty line two"]
    assert raw.flush() == ["part"]


def test_normalize_keeps_one_clock_and_masks_secrets():
    # the app's own stamp wins over Docker's
    assert logs.normalize("2026-11-24T10:00:00.123Z 2026-11-24 11:00:00 | INFO | ok") == "2026-11-24 11:00:00 | INFO | ok"
    # no app stamp: Docker's UTC in local time (Berlin, winter = +1 h)
    assert logs.normalize("2026-11-24T10:00:00.123456789Z plain") == "2026-11-24 11:00:00 plain"
    out = logs.normalize("2026-11-24T10:00:00Z token=sk-ant-api03-" + "x" * 40)
    assert "sk-ant-api03-xxxx" not in out


def test_groups_default_and_configured(monkeypatch):
    monkeypatch.delenv("AT_LOG_GROUPS", raising=False)
    gs = logs.groups([("shop", "Shop")])
    assert [g["id"] for g in gs] == ["shop", "agent-team", "other"]
    assert logs.group_of("shop-api", gs) == "shop" and logs.group_of("agent-team-web", gs) == "agent-team"
    assert logs.group_of("media-server", gs) == "other"
    monkeypatch.setenv("AT_LOG_GROUPS", json.dumps([{"id": "media", "label": "Medien", "prefixes": ["media-"]}]))
    gs = logs.groups([("shop", "Shop")])
    assert [g["id"] for g in gs] == ["media", "other"] and logs.group_of("media-server", gs) == "media"


CONTAINERS = [
    {"Names": ["/demo-api"], "State": "running", "Status": "Up 2 hours", "Image": "demo-api:latest",
     "Labels": {"com.docker.compose.project": "demo", "com.docker.compose.service": "api"}},
    {"Names": ["/media-server"], "State": "exited", "Status": "Exited (0)", "Image": "m", "Labels": {}},
]


@pytest.fixture()
def client(monkeypatch, demo_project):
    monkeypatch.setenv("AT_ACCESS_TEAM", "example.cloudflareaccess.com")
    monkeypatch.setenv("AT_ACCESS_AUD", "aud")
    monkeypatch.setenv("AT_DOCKER_RO_URL", "http://docker-ro:2375")
    monkeypatch.delenv("AT_LOG_GROUPS", raising=False)
    import agent_team.app as mod
    mod = importlib.reload(mod)
    monkeypatch.setattr(projects, "all_projects", lambda: [demo_project])
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path == "/containers/json":
            return httpx.Response(200, json=CONTAINERS)
        if req.url.path == "/containers/demo-api/logs":
            return httpx.Response(200, content=frame(1, "2026-11-24T10:00:00Z hallo\n"))
        return httpx.Response(404, json={"message": "no such container"})

    real = httpx.AsyncClient
    monkeypatch.setattr(docker_ro, "_client",
                        lambda timeout=10.0: real(base_url="http://docker-ro:2375", transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(mod, "_internal_client", lambda r: False)
    monkeypatch.setattr(mod, "access_identity", lambda r: "me@example.com")
    return mod, TestClient(mod.app), seen


def test_containers_and_logs_for_the_user(client):
    mod, c, seen = client
    r = c.get("/api/system/containers").json()
    assert [g["id"] for g in r["groups"]] == ["demo", "agent-team", "other"]
    assert {x["name"]: x["group"] for x in r["containers"]} == {"demo-api": "demo", "media-server": "other"}
    assert c.get("/api/system/logs/demo-api?tail=50").json()["lines"] == ["2026-11-24 11:00:00 hallo"]
    assert c.get("/api/system/logs/nope").status_code == 404
    assert c.get("/api/system/logs/..%2Fetc").status_code in (400, 404)
    assert all(not p.endswith("/json") or p == "/containers/json" for p in seen)  # never inspect


def test_agents_cannot_read_system_logs(client, monkeypatch):
    mod, c, _ = client
    monkeypatch.setattr(mod, "_internal_client", lambda r: True)
    assert c.get("/api/system/containers").status_code == 403


def test_not_configured_is_503(client, monkeypatch):
    mod, c, _ = client
    monkeypatch.delenv("AT_DOCKER_RO_URL")
    monkeypatch.setattr(docker_ro, "_client", lambda timeout=10.0: docker_ro.httpx.AsyncClient(base_url=docker_ro.base_url()))
    assert c.get("/api/system/containers").status_code == 503


# ---- Docker and host metrics ---------------------------------------------------

from agent_team.system import docker_metrics  # noqa: E402


def test_stats_numbers_and_disk_summary():
    raw = {"cpu_stats": {"cpu_usage": {"total_usage": 300}, "system_cpu_usage": 2000, "online_cpus": 4},
           "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
           "memory_stats": {"usage": 512 * 1048576, "limit": 1024 * 1048576}}
    assert docker_metrics.stats_numbers(raw) == {"cpu_pct": 80.0, "mem_mb": 512.0, "mem_pct": 50.0}
    assert docker_metrics.stats_numbers({})["cpu_pct"] == 0.0
    df = {"Images": [{"Size": 1048576 * 10}], "Containers": [{"SizeRw": 1048576}],
          "Volumes": [{"UsageData": {"Size": -1}}, {"UsageData": {"Size": 1048576 * 2}}], "BuildCache": []}
    out = docker_metrics.disk_summary(df)
    assert out["images"] == {"count": 1, "size_mb": 10.0} and out["volumes"]["size_mb"] == 2.0 and out["total_mb"] == 13.0
    assert docker_metrics.started_at("Exited (0) 2 hours ago") == ""
    assert docker_metrics.started_at("Up 3 hours (healthy)").endswith("Z")


def test_host_metrics_need_the_collector(client, monkeypatch):
    mod, c, _ = client
    monkeypatch.delenv("AT_SYSMON_URL", raising=False)
    assert c.get("/api/system/host").status_code == 503
    assert c.get("/api/system/host/unknown").status_code == 404

"""The host metrics collector serves the same shapes the System page reads."""

import pytest

pytest.importorskip("psutil")
from fastapi.testclient import TestClient  # noqa: E402

from agent_team.sysmon import host, server  # noqa: E402


def test_disk_labels_from_env_and_api(monkeypatch):
    monkeypatch.setattr(host, "_DISK_LABELS", {"/": "System"})
    with TestClient(server.app) as c:
        h = c.get("/host").json()
        assert {"cpu", "memory", "disks", "gpus", "temperatures", "network"} <= set(h)
        assert c.get("/host/load-history?hours=1").status_code == 200
        assert c.get("/healthz").json() == {"ok": True}

"""Native run (agent_team.local) and the usd budget mode."""

from __future__ import annotations

import pytest
import json
import time

from agent_team import budget, local

pytestmark = pytest.mark.generic


def test_processes_for_a_local_project(tmp_path, monkeypatch):
    # processes() points the project registry at the local config: restore it
    from agent_team import projects
    for name in ("DATA_ROOT", "TEAMS_ROOT"):
        monkeypatch.setattr(projects, name, getattr(projects, name))
    monkeypatch.setattr(projects, "_registry", dict(projects._registry))
    monkeypatch.setenv("AT_PROJECTS_FILE", str(tmp_path / "projects.yaml"))
    monkeypatch.setenv("AT_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "git" / "app"
    repo.mkdir(parents=True)
    (tmp_path / "projects.yaml").write_text(
        "projects:\n  app:\n    team_dir: " + str(tmp_path / "teams" / "app") + "\n"
        "    chat:\n      channels:\n        - {id: app, label: System, cwd: " + str(repo) + "}\n"
        "        - {id: app-lead, label: Lead, cwd: " + str(repo) + ", lead: true}\n")
    (tmp_path / "local.yaml").write_text(
        f"data_dir: {tmp_path / 'data'}\nworktree_root: {tmp_path / 'wt'}\n"
        "budget: {usd_per_day: 7}\nprojects:\n  app:\n    env: {AT_LOG_GLOBS: /logs/*.log}\n")
    cfg = local.load_config(tmp_path / "local.yaml")
    procs = local.processes(cfg)
    assert set(procs) == {"web", "chat", "worker-app"}
    _, env = procs["worker-app"]
    assert env["TASKS_API_URL"] == "http://127.0.0.1:8701/p/app"
    assert env["WORKER_HOST_ROOT"] == str(repo) and env["WORKER_WORKTREE_ROOT"] == str(tmp_path / "wt" / "app")
    assert env["WORKER_TEAM_DIR"] == str(tmp_path / "teams" / "app")
    assert env["AT_LOCAL"] == "1" and env["WORKER_APPLY_MODE"] == "pr" and env["AT_GIT_IDENTITY"] == "user"
    assert env["AT_BUDGET_MODE"] == "usd" and env["AT_BUDGET_USD_DAY"] == "7"
    assert env["AT_LOG_GLOBS"] == "/logs/*.log" and env["WORKER_CLI_REFRESH_H"] == "0"


def test_usd_budget_stops_autonomous_work_at_the_day_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(budget.projects, "DATA_ROOT", tmp_path)
    monkeypatch.setenv("AT_BUDGET_MODE", "usd")
    budget.record_cost(3.5, "chat:x")
    budget.record_cost(2.0, "worker")
    cfg = budget.Config(mode="usd", usd_per_day=5)
    d = budget.evaluate(cfg=cfg)
    assert not d.allowed and "Tagesbudget" in d.reason and d.usd_today == 5.5
    assert budget.evaluate(cfg=budget.Config(mode="usd", usd_per_day=10)).allowed
    # last week's spend counts for the week cap only
    with (tmp_path / "budget" / "costs.jsonl").open("a") as f:
        f.write(json.dumps({"ts": time.time() - 3 * 86400, "usd": 30}) + "\n")
    d = budget.evaluate(cfg=budget.Config(mode="usd", usd_per_day=10, usd_per_week=30))
    assert not d.allowed and "Wochenbudget" in d.reason
    assert budget.probe_due() is False


def test_record_cost_is_off_outside_usd_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(budget.projects, "DATA_ROOT", tmp_path)
    monkeypatch.delenv("AT_BUDGET_MODE", raising=False)
    budget.record_cost(1.0, "x")
    assert not (tmp_path / "budget" / "costs.jsonl").exists()

"""Deploy guard (agent_team.guard): agents never deploy on the work laptop."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import pytest

from agent_team import guard, local

pytestmark = pytest.mark.generic

SPEC = {"paths": ["/Volumes/deploy"], "profiles": ["deploy", "prod"],
        "patterns": [r"\bmvn\w*\b.*\sdeploy(\s|$)"]}


@pytest.fixture()
def g():
    return guard.load(json.dumps(SPEC))


@pytest.mark.parametrize("cmd", [
    "cp target/app.war /Volumes/deploy/app/",
    "rsync -a dist/ /Volumes/deploy",
    "mvn -Pdeploy package",
    "./mvnw -B -P dev,prod clean install",
    "mvn --activate-profiles=deploy package",
    "cd backend && mvn -P deploy verify",
    "mvn -B clean deploy",
])
def test_blocks_deploys(g, cmd):
    assert g.violation("Bash", {"command": cmd})


@pytest.mark.parametrize("cmd", [
    "mvn -B verify",
    "mvn -Pdev test",
    "./mvnw -pl backend -am verify",
    "mvn -P '!deploy' package",
    "mvn -P dev,!prod verify",
    "npx ng test --watch=false --browsers=ChromeHeadless",
    "ls /Volumes/logs/app",
    "grep -r deploy src/",
    "npm run build -- --deploy-url /app/",
])
def test_allows_normal_work(g, cmd):
    assert g.violation("Bash", {"command": cmd}) is None


def test_file_tools(g):
    assert g.violation("Write", {"file_path": "/Volumes/deploy/app/x.war"})
    assert g.violation("Edit", {"file_path": "/Volumes/deploy"})
    assert g.violation("NotebookEdit", {"notebook_path": "/Volumes/deploy/n.ipynb"})
    assert g.violation("Write", {"file_path": "/Volumes/deployment/x"}) is None
    assert g.violation("Write", {"file_path": "/Users/me/git/app/src/A.java"}) is None
    assert g.violation("Read", {"file_path": "/Volumes/deploy/app/x.war"}) is None


def test_symlinked_path_is_guarded(tmp_path):
    share = tmp_path / "share"
    share.mkdir()
    (tmp_path / "alias").symlink_to(share)
    gg = guard.load(json.dumps({"paths": [str(tmp_path / "alias")]}))
    assert gg.violation("Write", {"file_path": str(share / "a.war")})
    assert gg.violation("Bash", {"command": f"cp a.war {share}/"})


def test_off_without_config(monkeypatch):
    monkeypatch.delenv(guard.ENV, raising=False)
    assert guard.load() is None
    assert guard.load('{"paths": [], "profiles": []}') is None
    # the worktree check has no config and stays on
    assert guard.cli_settings() is not None


@pytest.mark.parametrize("cmd, cwd", [
    ("cd .worktrees/shop-api-x && docker compose up -d shop-api", "/repo"),
    ("docker compose -f /repo/.worktrees/x/docker-compose.yml build svc", "/repo"),
    ("docker compose up -d --build svc", "/home/u/git/.task-worktrees/shop/206/shop-api"),
    ("docker build -t shop-api:latest .", "/repo/.worktrees/x"),
    ("git status; docker-compose up -d", "/repo/.worktrees/x"),
    ("docker build .", "/repo/.worktrees/x"),                                  # untagged = latest
    ("docker build -t shop-api .", "/repo/.worktrees/x"),                      # implicit latest
    ("docker build -t shop-api:exp1 -t shop-api:latest .", "/repo/.worktrees/x"),
    ("docker build -t reg:5000/shop-api .", "/repo/.worktrees/x"),
])
def test_no_build_or_deploy_from_a_worktree(cmd, cwd):
    assert guard.worktree_violation("Bash", {"command": cmd}, cwd)


@pytest.mark.parametrize("cmd, cwd", [
    ("docker compose up -d --no-deps svc", "/repo"),                          # live checkout
    ("docker compose -p task-62 up -d db", "/x/.task-worktrees/e/62"),       # throwaway stack
    ("docker run --rm -v $PWD:/app img pytest", "/x/.task-worktrees/e/62"),  # tests
    ("docker compose logs --tail 20 svc", "/repo/.worktrees/x"),
    ("git -C .worktrees/x log --oneline -3", "/repo"),
    ("docker build -t shop-api:experiment-1 .", "/repo/.worktrees/x"),         # side image
    ("docker buildx build --tag=reg:5000/shop-api:exp2 -f D .", "/repo/.worktrees/x"),
])
def test_worktree_check_lets_normal_work_through(cmd, cwd):
    assert guard.worktree_violation("Bash", {"command": cmd}, cwd) is None


def test_sdk_hook_refuses_worktree_builds_without_config(monkeypatch):
    pytest.importorskip("claude_agent_sdk")
    monkeypatch.delenv(guard.ENV, raising=False)
    (m,) = guard.sdk_matchers()
    out = asyncio.run(m.hooks[0]({"tool_name": "Bash", "cwd": "/r/.worktrees/x",
                                  "tool_input": {"command": "docker compose up -d svc"}}, None, None))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Arbeitsbaum" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_sdk_hook_denies(monkeypatch):
    pytest.importorskip("claude_agent_sdk")
    monkeypatch.setenv(guard.ENV, json.dumps(SPEC))
    (m,) = guard.sdk_hooks()["PreToolUse"]
    hook = m.hooks[0]
    out = asyncio.run(hook({"tool_name": "Bash", "tool_input": {"command": "mvn -Pdeploy package"}}, None, None))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Deploy-Sperre" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert asyncio.run(hook({"tool_name": "Bash", "tool_input": {"command": "mvn verify"}}, None, None)) == {}


def test_cli_hook_command(monkeypatch):
    monkeypatch.setenv(guard.ENV, json.dumps(SPEC))
    settings = json.loads(guard.cli_settings())
    cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert cmd.endswith("-m agent_team.guard")
    env = {"PATH": "/usr/bin:/bin", guard.ENV: json.dumps(SPEC), "PYTHONPATH": ":".join(sys.path)}
    deny = subprocess.run([sys.executable, "-m", "agent_team.guard"], env=env, capture_output=True, text=True,
                          input=json.dumps({"tool_name": "Write", "tool_input": {"file_path": "/Volumes/deploy/a"}}))
    assert json.loads(deny.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    ok = subprocess.run([sys.executable, "-m", "agent_team.guard"], env=env, capture_output=True, text=True,
                        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "mvn verify"}}))
    assert ok.stdout.strip() == "" and ok.returncode == 0


def test_local_merges_and_requires_guard(tmp_path):
    (tmp_path / "local.yaml").write_text(
        "deploy_guard: {paths: [/Volumes/deploy], profiles: [deploy]}\n"
        "projects:\n  app:\n    deploy_guard: {profiles: [deploy, ship], patterns: ['deploy\\.sh']}\n")
    cfg = local.load_config(tmp_path / "local.yaml")
    spec = json.loads(local.base_env(cfg)[guard.ENV])
    assert spec == {"paths": ["/Volumes/deploy"], "profiles": ["deploy", "ship"], "patterns": [r"deploy\.sh"]}
    assert not any("deploy_guard" in p for p in local.check(cfg))
    (tmp_path / "bare.yaml").write_text("data_dir: " + str(tmp_path / "d") + "\n")
    assert any("deploy_guard fehlt" in p for p in local.check(local.load_config(tmp_path / "bare.yaml")))
    (tmp_path / "none.yaml").write_text("deploy_guard: {none: true}\n")
    assert not any("deploy_guard" in p for p in local.check(local.load_config(tmp_path / "none.yaml")))
    (tmp_path / "bad.yaml").write_text("deploy_guard: {patterns: ['(']}\n")
    assert any("ungültig" in p for p in local.check(local.load_config(tmp_path / "bad.yaml")))


# -- container memory limits (AT_DOCKER_LIMITS) ---------------------------------
LIM = guard.DockerLimits(max_memory="16g")


@pytest.mark.parametrize("cmd", [
    "docker run --rm --name task-1-bt img python -m src.backtests.x --start-date 2016-01-01",
    "docker run -d --rm img",
    "cd /w && docker run --rm -v /w:/app img pytest -q",
    "docker container run --rm img",
    "docker create img",
    "docker run --rm --memory 32g img",
    "docker run --rm -m32g img",
    "docker run --rm --memory=64G img",
    "docker run --rm img python -m 4g",        # -m after the image is the command's
    "echo ok; docker run -it img sh",
])
def test_limits_block(cmd):
    assert LIM.violation("Bash", {"command": cmd})


@pytest.mark.parametrize("cmd", [
    "docker run --rm --memory 8g --name task-1-bt img python -m src.backtests.x",
    "docker run --rm -m 16g img",
    "docker run -dit -m16g img",
    "docker run --rm --memory=1.5GB -e X=1 img",
    "docker ps -a",
    "docker exec demo-app python -m x",
    "docker compose -p task-3 run --rm app pytest",
    "docker logs task-1-bt | tail",
])
def test_limits_allow(cmd):
    assert LIM.violation("Bash", {"command": cmd}) is None


def test_limits_cgroup_parent():
    lim = guard.DockerLimits(max_memory="16g", cgroup_parent="agent-tasks.slice")
    assert lim.violation("Bash", {"command": "docker run -m 8g img"})
    assert lim.violation("Bash", {"command": "docker run -m 8g --cgroup-parent agent-tasks.slice img"}) is None


def test_limits_load(monkeypatch):
    monkeypatch.delenv(guard.LIMITS_ENV, raising=False)
    assert guard.load_limits() is None
    assert guard.load_limits('{"max_memory": "16g"}').max_memory == "16g"
    with pytest.raises(ValueError):
        guard.load_limits('{"max_memory": "lots"}')


def test_sdk_hook_denies_unlimited_docker_run(monkeypatch):
    monkeypatch.delenv(guard.ENV, raising=False)
    monkeypatch.setenv(guard.LIMITS_ENV, '{"max_memory": "16g"}')
    hook = guard.sdk_matchers()[0].hooks[0]
    out = asyncio.run(hook({"tool_name": "Bash", "tool_input": {"command": "docker run --rm img"}}, None, None))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "--memory 16g" in out["hookSpecificOutput"]["permissionDecisionReason"]
    ok = asyncio.run(hook({"tool_name": "Bash", "tool_input": {"command": "docker run -m 8g img"}}, None, None))
    assert ok == {}


def test_cli_hook_denies_unlimited_docker_run(monkeypatch):
    env = {"AT_DOCKER_LIMITS": '{"max_memory": "16g"}', "PYTHONPATH": "src"}
    d = json.dumps({"tool_name": "Bash", "tool_input": {"command": "docker run img"}})
    import os
    r = subprocess.run([sys.executable, "-m", "agent_team.guard"], input=d, capture_output=True,
                       text=True, env={**os.environ, **env})
    assert "deny" in r.stdout

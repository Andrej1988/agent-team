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
    assert guard.sdk_hooks() is None and guard.sdk_matchers() == [] and guard.cli_settings() is None


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

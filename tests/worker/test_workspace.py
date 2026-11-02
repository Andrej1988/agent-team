"""Task worktrees: branch per task, commit per stage, apply = rebase + ff."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_team.worker import workspace

pytestmark = pytest.mark.generic

ENV = {"GIT_AUTHOR_NAME": "Test Engineer", "GIT_AUTHOR_EMAIL": "te@agents.test",
       "GIT_COMMITTER_NAME": "Test Engineer", "GIT_COMMITTER_EMAIL": "te@agents.test"}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**ENV, "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd)}).stdout


def make_repo(path: Path, files: dict[str, str], ignore: str = ".env\n") -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / ".gitignore").write_text(ignore)
    for k, v in files.items():
        (path / k).parent.mkdir(parents=True, exist_ok=True)
        (path / k).write_text(v)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture()
def system(tmp_path, monkeypatch):
    """Monorepo-like: the root is a folder of repos, not a repo."""
    main = tmp_path / "system"
    make_repo(main / "swing", {"a.py": "x = 1\n"})
    make_repo(main / "base", {"b.py": "y = 1\n"})
    (main / "swing" / ".env").write_text("SECRET=1\n")
    (main / "AGENTS.md").write_text("rules\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    return workspace.Layout(main, tmp_path / "wt" / "proj", tmp_path / "control")


def test_ensure_worktree_for_task_repos_links_for_the_rest(system):
    ws = workspace.ensure(system, 7, ["swing"])
    assert (ws / "swing" / ".git").is_file()
    assert git(ws / "swing", "branch", "--show-current").strip() == "task/7"
    assert (ws / "base").is_symlink() and (ws / "AGENTS.md").is_symlink()
    assert (ws / "swing" / ".env").read_text() == "SECRET=1\n"   # copied for tests
    # a repo added to the task later becomes a worktree
    workspace.ensure(system, 7, ["swing", "base"])
    assert not (ws / "base").is_symlink() and (ws / "base" / ".git").is_file()
    # idempotent
    assert workspace.ensure(system, 7, ["swing", "base"]) == ws


def test_commit_apply_cleanup(system):
    ws = workspace.ensure(system, 8, ["swing"])
    (ws / "swing" / "a.py").write_text("x = 2\n")
    (ws / "swing" / "leak.env").write_text("TOKEN=1\n")
    assert workspace.commit(system, 8, "Task #8 Stufe 1", ENV) == ["swing"]
    assert "leak.env" not in git(ws / "swing", "show", "--name-only", "HEAD")
    assert workspace.commit(system, 8, "nothing", ENV) == []
    # the live checkout is untouched until apply
    assert (system.main_root / "swing" / "a.py").read_text() == "x = 1\n"
    st = workspace.write_state(system, 8)
    assert st["pending"] and st["repos"]["swing"]["ahead"] == 1
    assert "x = 2" in system.state_path(8).with_suffix(".diff").read_text()

    # someone commits on main meanwhile + leaves unrelated uncommitted work
    (system.main_root / "swing" / "c.py").write_text("z = 1\n")
    git(system.main_root / "swing", "add", "c.py")
    git(system.main_root / "swing", "commit", "-q", "-m", "other")
    (system.main_root / "swing" / "wip.py").write_text("mine\n")

    out = workspace.apply(system, 8, ENV)
    assert out and out[0].startswith("swing → main")
    assert (system.main_root / "swing" / "a.py").read_text() == "x = 2\n"
    assert (system.main_root / "swing" / "wip.py").read_text() == "mine\n"   # left alone
    assert not workspace.state(system, 8)["pending"]

    workspace.cleanup(system, 8)
    assert not system.ws(8).exists()
    assert "task/8" not in git(system.main_root / "swing", "branch")   # merged → gone


def test_apply_conflict_leaves_live_checkout_alone(system):
    ws = workspace.ensure(system, 9, ["swing"])
    (ws / "swing" / "a.py").write_text("x = 'task'\n")
    workspace.commit(system, 9, "task", ENV)
    (system.main_root / "swing" / "a.py").write_text("x = 'live'\n")
    git(system.main_root / "swing", "commit", "-qam", "live")
    with pytest.raises(workspace.GitError, match="Konflikt"):
        workspace.apply(system, 9, ENV)
    assert (system.main_root / "swing" / "a.py").read_text() == "x = 'live'\n"
    assert workspace.state(system, 9)["pending"]   # branch still there, untouched


def test_apply_refuses_to_overwrite_local_changes(system):
    ws = workspace.ensure(system, 10, ["swing"])
    (ws / "swing" / "a.py").write_text("x = 3\n")
    workspace.commit(system, 10, "task", ENV)
    (system.main_root / "swing" / "a.py").write_text("x = 'uncommitted'\n")
    with pytest.raises(workspace.GitError, match="abgelehnt"):
        workspace.apply(system, 10, ENV)
    assert (system.main_root / "swing" / "a.py").read_text() == "x = 'uncommitted'\n"


def test_apply_sets_live_session_notes_aside(system):
    """A branch that untracks the session notes merges although the live copy
    is modified (agents rewrite it constantly); the live notes survive."""
    sw = system.main_root / "swing"
    (sw / ".claude").mkdir(exist_ok=True)
    (sw / ".claude" / "SESSION.md").write_text("alt\n")
    git(sw, "add", ".claude/SESSION.md")
    git(sw, "commit", "-qm", "notes")
    ws = workspace.ensure(system, 12, ["swing"])
    git(ws / "swing", "rm", "-q", "--cached", ".claude/SESSION.md")
    (ws / "swing" / ".gitignore").write_text(".claude/SESSION.md\n")
    workspace.commit(system, 12, "untrack notes", ENV)
    (sw / ".claude" / "SESSION.md").write_text("live, neu\n")       # an agent just wrote it
    assert workspace.apply(system, 12, ENV)
    assert (sw / ".claude" / "SESSION.md").read_text() == "live, neu\n"
    assert ".claude/SESSION.md" not in git(sw, "ls-files")


def test_closed_task_drops_branch(system):
    ws = workspace.ensure(system, 11, ["swing"])
    (ws / "swing" / "a.py").write_text("x = 4\n")
    workspace.commit(system, 11, "task", ENV)
    workspace.cleanup(system, 11, drop_branch=True)
    assert "task/11" not in git(system.main_root / "swing", "branch")
    assert workspace.task_ids(system) == []


def test_root_repo_project(tmp_path, monkeypatch):
    """Single-repo-like: the root is a repo, sub-repos are ignored in it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    main = make_repo(tmp_path / "shop", {"README.md": "root\n"}, ignore="/app/\n/data/\n.env\n")
    make_repo(main / "app", {"main.py": "print(1)\n"})
    make_repo(main / "data", {"d.py": "print(2)\n"})
    lay = workspace.Layout(main, tmp_path / "wt", tmp_path / "control")
    ws = workspace.ensure(lay, 3, ["app"])
    assert (ws / ".git").is_file() and (ws / "app" / ".git").is_file() and (ws / "data").is_symlink()
    (ws / "app" / "main.py").write_text("print(3)\n")
    (ws / "README.md").write_text("root 2\n")
    assert sorted(workspace.commit(lay, 3, "t", ENV)) == [".", "app"]
    # the link to the live data repo is never committed into the root repo
    assert "data" not in git(ws, "show", "--name-only", "HEAD").split()
    assert len(workspace.apply(lay, 3, ENV)) == 2
    assert (main / "app" / "main.py").read_text() == "print(3)\n"
    assert (main / "README.md").read_text() == "root 2\n"
    workspace.cleanup(lay, 3)
    assert not ws.exists()


def test_stage_env_points_host_root_vars_at_the_workspace(system, monkeypatch):
    monkeypatch.setenv("HOST_DEMO_ROOT", str(system.main_root))
    monkeypatch.setenv("WORKER_HOST_ROOT", str(system.main_root))
    env = workspace.stage_env(system, 5, {"GIT_AUTHOR_NAME": "x"})
    assert env["HOST_DEMO_ROOT"] == str(system.ws(5)) == env["AT_TASK_WORKSPACE"]
    assert "WORKER_HOST_ROOT" not in env and env["GIT_AUTHOR_NAME"] == "x"


def test_publish_pr_pushes_and_opens_a_draft(tmp_path, monkeypatch):
    """apply_mode pr: rebase on origin, push the task branch, `gh pr create
    --draft` once; a second call finds the PR instead of creating another."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    main = make_repo(tmp_path / "app", {"main.py": "print(1)\n"})
    git(main, "remote", "add", "origin", str(origin))
    git(main, "push", "-q", "origin", "main")
    git(main, "branch", "--set-upstream-to=origin/main")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "gh.log"
    (bin_dir / "gh").write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {calls}\n'
        'case "$1 $2" in\n'
        f'  "pr view") grep -q create {calls} && echo https://ghe.test/org/app/pull/7; exit 0;;\n'
        '  "pr create") echo https://ghe.test/org/app/pull/7;;\n'
        "esac\n")
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin:/usr/local/bin")
    lay = workspace.Layout(main, tmp_path / "wt", tmp_path / "control")
    ws = workspace.ensure(lay, 4, [])
    (ws / "main.py").write_text("print(2)\n")
    workspace.commit(lay, 4, "change", ENV)
    out = workspace.publish_pr(lay, 4, "Titel", "Text", ENV)
    assert out == [". → https://ghe.test/org/app/pull/7"]
    assert workspace.branch(4) in git(origin, "branch")
    assert "--draft" in calls.read_text()
    assert workspace.publish_pr(lay, 4, "Titel", "Text", ENV) == out
    assert calls.read_text().count("pr create") == 1
    assert (main / "main.py").read_text() == "print(1)\n"      # live checkout untouched


def test_isolate_repos_only_the_projects_repos(system):
    """A shared git folder (all of the user's repos): only the project's repos
    appear in the workspace — no links to the others, no root files."""
    make_repo(system.main_root / "frontend", {"app.ts": "x\n"})
    ws = workspace.ensure(system, 9, [], frozenset({"swing", "frontend"}))
    assert (ws / "swing" / ".git").is_file() and (ws / "frontend" / ".git").is_file()
    assert not (ws / "base").exists() and not (ws / "AGENTS.md").exists()
    ws = workspace.ensure(system, 10, ["frontend"], frozenset({"swing", "frontend"}))
    assert (ws / "frontend" / ".git").is_file() and (ws / "swing").is_symlink()   # the app's other repo, to read
    assert {name for name, _ in workspace.worktrees(system, 10)} == {"frontend"}

"""Chats stay in their own project (chat/scope.py)."""

from __future__ import annotations

import pytest

from agent_team import projects
from agent_team.chat import scope


@pytest.fixture()
def four_projects(tmp_path, monkeypatch):
    def proj(pid, cwd):
        ch = projects.Channel(id=f"{pid}-main", project=pid, label=pid, cwd=cwd)
        return projects.Project(id=pid, name=pid, data_dir=tmp_path / pid, team_dir=tmp_path / "t" / pid,
                                channels=(ch,), tasks=False)
    ps = [proj("agent-team", "/workspaces/agent-team"), proj("demo", "/workspaces/demo-system"),
          proj("shop", "/workspaces/shop"), proj("other", "/home/user/git/other-app")]
    for p in ps:
        projects.register(p)
    monkeypatch.setattr(projects, "all_projects", lambda: ps)
    monkeypatch.setattr(scope, "HOST_GIT", "/home/user/git")   # the host layout these cases describe
    return ps


def test_other_projects_are_off_limits(four_projects):
    v = scope.violation
    # the incident: one project's chat editing another project through docker
    assert v("demo", "Bash", {"command": "docker run --rm -v /home/user/git/other-app:/src alpine ls"}) \
        == "/home/user/git/other-app"
    assert v("demo", "Edit", {"file_path": "/workspaces/shop/app/x.py"}) == "/workspaces/shop"
    assert v("demo", "Read", {"file_path": "/home/user/git/shop/README.md"}) == "/home/user/git/shop"
    assert v("shop", "Grep", {"path": "/workspaces/demo-system"}) == "/workspaces/demo-system"
    # own folders under both names, data dir, tmp: fine
    assert v("demo", "Bash", {"command": "docker run -v /home/user/git/demo-system/x:/app img"}) is None
    assert v("demo", "Edit", {"file_path": "/workspaces/demo-system/a.py"}) is None
    assert v("demo", "Bash", {"command": "cat /data/projects/demo/tasks.db; ls /tmp"}) is None
    assert v("other", "Bash", {"command": "cd /home/user/git/other-app && git status"}) is None
    # a name that merely starts like another root is not that root
    assert v("demo", "Bash", {"command": "ls /workspaces/shop-old"}) is None
    # the operator's own channel works across projects
    assert v("agent-team", "Bash", {"command": "ls /workspaces/demo-system /home/user/git/other-app"}) is None


def test_prompt_note_names_the_other_projects(four_projects):
    note = scope.prompt_note("demo")
    assert "'demo'" in note and "/workspaces/shop" in note and "/home/user/git/other-app" in note
    assert scope.prompt_note("agent-team") == ""

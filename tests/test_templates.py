"""The shipped team templates load and are complete (roles, scouts, prompts)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_team.tasks import agent_roles

pytestmark = pytest.mark.generic

TEMPLATES = sorted((Path(__file__).resolve().parents[1] / "templates").glob("team-*"))


@pytest.mark.parametrize("team_dir", TEMPLATES, ids=lambda p: p.name)
def test_template_is_complete(team_dir):
    cat = agent_roles.load_catalog(team_dir)
    team = yaml.safe_load((team_dir / "team.yaml").read_text())
    ids = {r.id for r in cat.ROLES}
    assert {"orchestrator", "lead"} <= ids and cat.FINAL is not None
    assert (team_dir / "roles" / "common-prompt.md").is_file()
    for s in team.get("scouts") or []:
        assert s["role"] in ids, s
        if not (s.get("report") or s.get("lead")):
            assert (team_dir / "scouts" / f"{s['id']}.md").is_file(), s
        if s.get("lead"):
            assert (team_dir / "scouts" / "lead.md").is_file()
        if s.get("prepare"):
            assert (team_dir / "scouts" / s["prepare"]).is_file(), s


def test_there_are_templates():
    assert {p.name for p in TEMPLATES} >= {"team-basic", "team-webapp"}

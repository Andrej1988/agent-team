"""Team rules beyond demo's: confirm_roles (deploy never on autopilot)
and final.may_follow (deploy after the final review) — checked against the
shop fixture team (tests/fixtures/team-shop)."""

from __future__ import annotations

import pytest

from agent_team import projects
from agent_team.tasks import agent_roles

from conftest import SHOP_TEAM


@pytest.fixture()
def shop(tmp_path):
    p = projects.Project(id="shop", name="shop", data_dir=tmp_path / "shop",
                         team_dir=SHOP_TEAM)
    with projects.use(p):
        from agent_team.tasks import db
        yield db


def test_catalog(shop):
    cat = agent_roles.current()
    assert {r.id for r in cat.ROLES} == {
        "orchestrator", "product-owner", "implementation-engineer", "data-engineer",
        "test-engineer", "reviewer", "release-manager", "qa-tester", "legal"}
    assert cat.CONFIRM_ROLES == {"release-manager"}
    assert cat.FINAL.role == "reviewer" and cat.FINAL_MAY_FOLLOW == {"release-manager"}
    assert cat.normalise_repo("app") == "shop-app"
    assert cat.LIVE_BOT_REPOS == frozenset()


def test_review_is_inserted_before_a_deploy(shop):
    out = shop._normalise_stages([{"role": "implementation-engineer"}, {"role": "release-manager"}])
    assert [s["role"] for s in out] == ["implementation-engineer", "reviewer", "release-manager"]
    out = shop._normalise_stages([{"role": "implementation-engineer"}])
    assert [s["role"] for s in out] == ["implementation-engineer", "reviewer"]
    out = shop._normalise_stages([{"role": "implementation-engineer"}, {"role": "reviewer"}, {"role": "release-manager"}])
    assert [s["role"] for s in out] == ["implementation-engineer", "reviewer", "release-manager"]


def test_deploy_waits_for_click_then_finishes(shop, monkeypatch):
    from test_pipeline_backend import plan_text, stage_text, _running

    monkeypatch.setattr(shop, "DEFAULT_AUTOPILOT", "full-bots")
    t = shop.create_task("feature", "d", 2, ["shop-app"])
    shop.request_plan(t["id"])
    t = shop.complete_plan(t["id"], plan_text([
        {"role": "implementation-engineer", "goal": "g"},
        {"role": "reviewer", "goal": "g"},
        {"role": "release-manager", "goal": "deploy"},
    ]))
    # full autopilot: implement + reviewer chain on their own …
    for _ in range(2):
        st = _running(shop, t["id"])[0]
        t = shop.complete_stage(t["id"], st["seq"], st["attempt"], stage_text("pass"))
    # … but the deploy waits for the user (inbox approval)
    assert t["status"] == "plan_ready" and not _running(shop, t["id"])
    t = shop.request_next_stage(t["id"])          # the user's click
    st = _running(shop, t["id"])[0]
    assert st["role"] == "release-manager"
    t = shop.complete_stage(t["id"], st["seq"], st["attempt"], stage_text("pass"))
    assert t["status"] == "done"


def test_failed_deploy_is_not_done(shop):
    ok, reason = shop._finish_rule([
        {"seq": 1, "role": "reviewer", "status": "done", "verdict": "pass"},
        {"seq": 2, "role": "release-manager", "status": "done", "verdict": "fail"},
    ])
    assert not ok and "release-manager" in reason
    ok, _ = shop._finish_rule([
        {"seq": 1, "role": "reviewer", "status": "done", "verdict": "fail"},
        {"seq": 2, "role": "release-manager", "status": "done", "verdict": "pass"},
    ])
    assert not ok

"""Test setup: every test runs inside a temporary "demo" project whose team
definition is the fixture team in ``tests/fixtures/team-trading`` (a
trading-style team that exercises every pipeline rule) and whose data dir is
the test's tmp_path."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_team import projects

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DEMO_TEAM = FIXTURES / "team-trading"
SHOP_TEAM = FIXTURES / "team-shop"


def pytest_configure(config):
    config.addinivalue_line("markers", "generic: does not depend on the demo fixture team")


@pytest.fixture(autouse=True)
def demo_project(tmp_path):
    p = projects.Project(id="demo", name="demo-system", data_dir=tmp_path,
                         team_dir=DEMO_TEAM, worker="demo-worker")
    projects.register(p)
    with projects.use(p):
        yield p

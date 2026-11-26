"""Test suites the worker runs itself (suites.py) and the task gates (test_gate.py)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_team.worker import suites, test_gate, workspace

from test_workspace import ENV, git, make_repo

pytestmark = pytest.mark.generic

PYTEST_RED = """\
tests/test_a.py .F.                                                      [100%]
=================================== FAILURES ===================================
____________________________________ test_b ____________________________________
=========================== short test summary info ============================
FAILED tests/test_a.py::test_b - assert 1 == 2
ERROR tests/test_c.py::test_d - fixture missing
========================= 1 failed, 2 passed, 1 error in 0.12s =========================
"""


def suite(repo: str, cmd: str, timeout_min: float = 1) -> suites.Suite:
    return suites.load({"test_suites": {repo: {"run": cmd, "timeout_min": timeout_min}}})[repo]


def test_load_accepts_string_or_dict_and_skips_empty():
    cfg = suites.load({"test_suites": {"a": "make test", "b": {"run": "x  \n y", "timeout_min": 2},
                                       "c": {"timeout_min": 3}}})
    assert set(cfg) == {"a", "b"}
    assert cfg["a"].timeout_s == suites.DEFAULT_TIMEOUT_MIN * 60
    assert cfg["b"].run == "x y" and cfg["b"].timeout_s == 120
    assert suites.load({}) == {}
    team = suites.load({"test_suites": {"a": "bash {team}/run.sh {repo}"}}, Path("/t d"))
    assert team["a"].run == "bash '/t d'/run.sh {repo}"


def test_parse_pytest_output():
    summary, failed = suites.parse(PYTEST_RED)
    assert summary == "1 failed, 2 passed, 1 error in 0.12s"
    assert failed == ["tests/test_a.py::test_b", "tests/test_c.py::test_d"]
    assert suites.parse("boom\n") == ("boom", [])


def test_placeholders_are_quoted(tmp_path):
    s = suite("r", "echo {repo} {name}")
    assert suites.command(s, Path("/x y/r"), "task-1-suite-r") == "echo '/x y/r' task-1-suite-r"
    assert suites.container_name("live", ".") == "task-live-suite-root"
    assert suites.container_name("12", "My_Repo") == "task-12-suite-my-repo"


def test_run_green_red_and_timeout(tmp_path):
    ok = suites.run(suite("r", "echo '3 passed in 0.1s'"), tmp_path, "n")
    assert ok.ok and ok.code == 0 and ok.summary == "3 passed in 0.1s"
    (tmp_path / "out.txt").write_text(PYTEST_RED)
    red = suites.run(suite("r", "cat {repo}/out.txt; exit 1"), tmp_path, "n")
    assert not red.ok and red.code == 1 and len(red.failed) == 2
    slow = suites.run(suites.Suite("r", "sleep 5", timeout_s=1), tmp_path, "task-test-suite-none")
    assert not slow.ok and slow.code is None and "Zeitüberschreitung" in slow.tail
    ev = suites.evidence([ok, red])
    assert ev["ok"] is False and ev["repos"]["r"]["ok"] is False
    assert "tests/test_a.py::test_b" in suites.report([red])


def test_live_due_once_a_day_from_the_hour():
    at = lambda h: time.strptime(f"2026-01-02 {h:02d}", "%Y-%m-%d %H")  # noqa: E731
    assert not suites.live_due({}, 3, at(2))
    assert suites.live_due({}, 3, at(3))
    assert suites.live_due({"day": "2026-01-01"}, 3, at(23))
    assert not suites.live_due({"day": "2026-01-02"}, 3, at(23))


def test_live_message_red_every_day_green_once():
    red = suites.Result("app", False, 1, 1.0, "1 failed", ["t::x"])
    green = suites.Result("app", True, 0, 1.0, "5 passed")
    title, text, level = suites.live_message([red], {})
    assert "app" in title and level == "important" and "t::x" in text
    assert suites.live_message([red], {"repos": {"app": {"ok": False}}})[2] == "important"
    healed = suites.live_message([green], {"repos": {"app": {"ok": False}}})
    assert healed and healed[2] == "info" and "wieder grün" in healed[0]
    assert suites.live_message([green], {"repos": {"app": {"ok": True}}}) is None


# ---- task gates --------------------------------------------------------------

@pytest.fixture()
def system(tmp_path, monkeypatch):
    main = tmp_path / "system"
    make_repo(main / "app", {"a.py": "x = 1\n", "check.sh": "grep -q 'x = 1' a.py\n"})
    make_repo(main / "lib", {"b.py": "y = 1\n"})
    monkeypatch.setenv("HOME", str(tmp_path))
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    return workspace.Layout(main, tmp_path / "wt" / "proj", tmp_path / "control")


# "Tests" of app: green while a.py says x = 1.
CFG = {"app": suites.Suite("app", "cd {repo} && sh check.sh && echo '1 passed' || { echo 'FAILED t::x'; exit 1; }")}


def test_baseline_green_goes_and_is_recorded(system):
    workspace.ensure(system, 1, ["app"])
    assert test_gate.baseline(system, 1, {"repos": ["app"]}, CFG, "") == ("go", "")
    st = test_gate.read_state(system, 1)
    assert st["suites_baseline"]["ok"] is True and st["suites"]["app"]["ok"] is True


def test_baseline_without_named_repos_is_skipped(system):
    workspace.ensure(system, 2, [])
    assert test_gate.baseline(system, 2, {"repos": []}, CFG, "") == ("go", "")
    assert test_gate.read_state(system, 2)["suites_baseline"]["skipped"] is True


def test_baseline_red_asks_then_repairs_or_stops(system):
    (system.main_root / "app" / "a.py").write_text("x = 2\n")
    git(system.main_root / "app", "commit", "-qam", "break")
    workspace.ensure(system, 3, ["app"])
    action, out = test_gate.baseline(system, 3, {"repos": ["app"]}, CFG, "")
    assert action == "ask" and '"needs_input"' in out and test_gate.REPAIR_OPTION in out
    # the user picks "repair": go, the prompt says what is red
    action, note = test_gate.baseline(system, 3, {"repos": ["app"]}, CFG,
                                      f"Entscheidung des Users: {test_gate.REPAIR_OPTION}")
    assert action == "go" and "t::x" in note
    # accepted sticks (a later retry does not ask again)
    assert test_gate.baseline(system, 3, {"repos": ["app"]}, CFG, "")[0] == "go"

    workspace.ensure(system, 4, ["app"])
    test_gate.baseline(system, 4, {"repos": ["app"]}, CFG, "")
    action, out = test_gate.baseline(system, 4, {"repos": ["app"]}, CFG,
                                     f"Entscheidung des Users: {test_gate.STOP_OPTION}")
    assert action == "stop" and '"blocked"' in out


def test_end_gate_runs_changed_repos_only(system):
    ws = workspace.ensure(system, 5, ["app", "lib"])
    assert test_gate.end_gate(system, 5, CFG) is None          # nothing changed
    (ws / "app" / "a.py").write_text("x = 3\n")
    ev = test_gate.end_gate(system, 5, CFG)
    assert ev["ok"] is False and ev["repos"]["app"]["failed"] == ["t::x"]
    (ws / "lib" / "b.py").write_text("y = 2\n")                 # no suite for lib
    (ws / "app" / "a.py").write_text("x = 1\nz = 1\n")
    assert test_gate.end_gate(system, 5, CFG)["ok"] is True


def test_apply_runs_the_suite_after_the_rebase_and_refuses_red(system):
    ws = workspace.ensure(system, 6, ["app"])
    (ws / "app" / "c.py").write_text("c = 1\n")
    workspace.commit(system, 6, "task", ENV)
    assert test_gate.end_gate(system, 6, CFG)["ok"] is True
    # live moves on and breaks the suite in combination
    (system.main_root / "app" / "a.py").write_text("x = 9\n")
    git(system.main_root / "app", "commit", "-qam", "other")
    ran = []
    check = test_gate.apply_check(system, 6, CFG, ENV, on_run=ran.append)
    with pytest.raises(workspace.GitError, match="Test-Suite rot"):
        workspace.apply(system, 6, ENV, check=check)
    assert ran == ["app"]
    assert not (system.main_root / "app" / "c.py").exists()     # live untouched


def test_apply_skips_a_commit_that_was_green_already(system):
    ws = workspace.ensure(system, 7, ["app"])
    (ws / "app" / "c.py").write_text("c = 1\n")
    workspace.commit(system, 7, "task", ENV)
    test_gate.end_gate(system, 7, CFG)
    ran = []
    out = workspace.apply(system, 7, ENV, check=test_gate.apply_check(system, 7, CFG, ENV, on_run=ran.append))
    assert out and ran == []                                    # same commit, nothing rebased
    assert (system.main_root / "app" / "c.py").exists()


def test_write_state_keeps_suite_results(system):
    workspace.ensure(system, 8, ["app"])
    test_gate.save(system, 8, suites={"app": {"head": "h", "ok": True}}, suites_baseline={"ok": True})
    st = workspace.write_state(system, 8)
    assert st["suites"]["app"]["ok"] is True and st["suites_baseline"] == {"ok": True}

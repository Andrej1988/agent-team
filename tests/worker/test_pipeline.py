"""pipeline.py — role loading, prompt assembly, model/tool resolution.

Runs anywhere with pytest: pipeline.py has no claude_agent_sdk import. The
roles come from the fixture team (tests/fixtures/team-trading).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_team.worker import pipeline  # noqa: E402

ROLE_IDS = [
    "orchestrator", "lead", "quant", "data-engineer", "backtest-engineer", "backtest-auditor",
    "implementation-engineer", "test-engineer", "integration-ops", "reviewer",
    "paper-reviewer", "live-release",
]


def _task(**kw):
    base = {
        "id": 42, "title": "Build a thing", "prio": 2, "repos": ["demo-swing"],
        "details": "do it well", "refined_spec": "", "round_count": 1,
        "budget_usd": 10.0, "autopilot": "advise", "stop_reason": None,
        "plan": {"exit_criteria": ["OOS Sharpe > 1"], "max_rounds": 3, "max_stages": 12, "rationale": ""},
    }
    base.update(kw)
    return base


def _stage(seq, role, status="pending", **kw):
    s = {
        "seq": seq, "role": role, "kind": "advise", "status": status, "attempt": 1,
        "goal": f"goal {seq}", "model": None, "summary": "", "verdict": None,
        "findings": [], "questions": [], "output": "", "error": None,
        "pending_answer": None, "session_id": None, "session_backend": None,
    }
    s.update(kw)
    return s


# ---------------------------------------------------------------------------
# Role files
# ---------------------------------------------------------------------------

def test_front_matter_parsing():
    meta, body = pipeline.parse_front_matter("---\nid: x\nlabel: X Y\nkind: implement\ndefault_model:\n---\n## body\n")
    assert meta == {"id": "x", "label": "X Y", "kind": "implement", "default_model": ""}
    assert body == "## body\n"
    meta, body = pipeline.parse_front_matter("no front matter")
    assert meta == {} and body == "no front matter"


@pytest.mark.parametrize("role_id", ROLE_IDS)
def test_every_catalog_role_loads(role_id):
    role = pipeline.load_role(role_id)
    assert role.id == role_id
    assert role.kind in ("advise", "implement")
    assert role.body and "## Role" in role.body
    if role.kind == "advise":
        # Claude = a claude-* id or one of the CLI aliases (opus/sonnet/...),
        # which track the current generation across CLI upgrades.
        m = role.default_model or ""
        assert m.startswith("claude-") or m in {"opus", "sonnet", "haiku", "fable"}, "advise roles default to Claude"


def test_load_catalog_and_common_prompt():
    ids = {r.id for r in pipeline.load_catalog()}
    assert ids == set(ROLE_IDS)
    common = pipeline.load_common_prompt()
    assert pipeline.STAGE_RESULT_MARKER in common
    assert '"verdict"' in common


def test_load_role_rejects_bad_ids():
    for bad in ("", "../etc", "common-prompt", "Quant Role", "nope"):
        with pytest.raises((ValueError, FileNotFoundError)):
            pipeline.load_role(bad)


def test_role_system_prompt_layout():
    common = "COMMON RULES"
    quant = pipeline.load_role("quant")
    orch = pipeline.load_role("orchestrator")
    assert pipeline.role_system_prompt(quant, common).startswith("COMMON RULES\n\n")
    assert pipeline.role_system_prompt(orch, common) == orch.body  # self-contained
    assert pipeline.PLAN_MARKER in orch.body and pipeline.STAGE_RESULT_MARKER not in orch.body


# ---------------------------------------------------------------------------
# Tools / models / extraction
# ---------------------------------------------------------------------------

def test_stage_tools_by_kind():
    assert pipeline.stage_disallowed_tools("advise") == ["Write", "Edit", "NotebookEdit"]
    assert "Bash" not in pipeline.stage_disallowed_tools("advise")
    assert pipeline.stage_disallowed_tools("implement") == []


def test_full_bots_implement_stage_is_hands_off():
    bots = _task(autopilot="full-bots")
    tools = pipeline.stage_disallowed_tools("implement", bots)
    assert "Bash(git commit:*)" in tools and "Bash(docker compose up:*)" in tools
    # Files stay writable — full-bots implement is not a read-only run.
    assert not {"Write", "Edit", "NotebookEdit"} & set(tools)
    assert pipeline.stage_disallowed_tools("implement", _task(autopilot="full")) == []
    assert pipeline.stage_disallowed_tools("advise", bots) == ["Write", "Edit", "NotebookEdit"]

    role = pipeline.load_role("integration-ops")
    stages = [_stage(1, "integration-ops", "running")]
    p = pipeline.build_stage_prompt(bots, stages[0], stages, role)
    assert "hands-off rules" in p and "No `git commit`" in p
    assert "hands-off" not in pipeline.build_stage_prompt(_task(autopilot="full"), stages[0], stages, role)
    auditor = pipeline.load_role("backtest-auditor")
    assert "hands-off" not in pipeline.build_stage_prompt(bots, stages[0], stages, auditor)


def test_confirm_role_never_hands_off():
    # A confirm role only starts on the user's click, so full-bots must not
    # forbid what that click approved (deploy).
    bots = _task(autopilot="full-bots")
    confirm = frozenset({"integration-ops"})
    assert pipeline.stage_disallowed_tools("implement", bots, "integration-ops", confirm) == []
    assert pipeline.stage_disallowed_tools("implement", bots, "other", confirm) != []
    role = pipeline.load_role("integration-ops")
    stages = [_stage(1, "integration-ops", "running")]
    assert "hands-off" not in pipeline.build_stage_prompt(bots, stages[0], stages, role, confirm)


def test_stage_model_resolution(monkeypatch):
    quant = pipeline.load_role("quant")
    impl = pipeline.load_role("implementation-engineer")
    assert pipeline.stage_model({"model": " gpt-5.6-terra "}, quant, "r", "e") == "gpt-5.6-terra"
    assert pipeline.stage_model({"model": ""}, quant, "r", "e") == quant.default_model
    assert pipeline.stage_model({}, impl, "refine-default", "execute-default") == "execute-default"
    monkeypatch.delenv("WORKER_ORCHESTRATOR_MODEL", raising=False)
    orch = pipeline.load_role("orchestrator")
    backend_of = lambda m: "codex" if m.startswith("gpt-") else "claude"  # noqa: E731
    pin = dict(backend_of=backend_of)
    # Claude plan: a gpt-* env default (or override) never reaches a stage
    assert pipeline.stage_model({}, impl, "gpt-5.6-luna", "gpt-5.6-terra", plan_model="opus", **pin) == "opus"
    assert pipeline.stage_model({"model": "gpt-5.6-sol"}, impl, "r", "gpt-5.6-terra",
                                plan_model="claude-opus-5-5", **pin) == "claude-opus-5-5"
    assert pipeline.stage_model({}, quant, "gpt-5.6-luna", "e", plan_model="opus", **pin) == quant.default_model
    assert pipeline.stage_model({"model": "claude-sonnet-5"}, impl, "r", "e", plan_model="opus", **pin) == "claude-sonnet-5"
    # Codex plan: Claude role defaults are skipped for the codex env default
    assert pipeline.stage_model({}, quant, "gpt-5.6-luna", "gpt-5.6-terra", plan_model="gpt-6-sol", **pin) == "gpt-5.6-luna"
    assert pipeline.stage_model({}, quant, "claude-x", "claude-y", plan_model="gpt-6-sol", **pin) == "gpt-6-sol"
    assert pipeline.orchestrator_model(orch, "r") == orch.default_model
    monkeypatch.setenv("WORKER_ORCHESTRATOR_MODEL", "claude-haiku-4-5")
    assert pipeline.orchestrator_model(orch, "r") == "claude-haiku-4-5"
    # the task's own Plan-model override (Tasks UI) beats env and role default
    assert pipeline.orchestrator_model(orch, "r", "gpt-5.6-terra") == "gpt-5.6-terra"
    assert pipeline.orchestrator_model(orch, "r", "  ") == "claude-haiku-4-5"


def test_extract_last_section_prefers_last_marker():
    text = "# Stage result\n(template)\n\nwork...\n\n# Stage result\n```json\n{\"verdict\":\"pass\"}\n```"
    sec = pipeline.extract_last_section(text, pipeline.STAGE_RESULT_MARKER)
    assert sec.startswith("# Stage result\n```json") and "(template)" not in sec
    assert pipeline.extract_last_section("no marker here", pipeline.STAGE_RESULT_MARKER) == "no marker here"
    assert pipeline.extract_last_section("   ", pipeline.STAGE_RESULT_MARKER) is None


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def test_dossier_contains_history_findings_and_pending_tail():
    stages = [
        _stage(1, "quant", "done", verdict="pass", summary="edge exists",
               findings=[{"id": "QNT-1", "severity": "high", "text": "capacity", "resolved": False}],
               output="full quant output"),
        _stage(2, "backtest-auditor", "done", verdict="fail", summary="look-ahead", output="x" * 20000),
        _stage(3, "implementation-engineer", "pending", kind="implement", model="gpt-5.6-terra"),
    ]
    d = pipeline.format_dossier(_task(), stages)
    assert "## Task #42 — Build a thing" in d
    assert "OOS Sharpe > 1" in d and "rounds used: 1/3" in d
    assert "1. quant — done/pass" in d and "[QNT-1 high open] capacity" in d
    assert "full quant output" in d
    assert "…(truncated)…" in d  # long output clipped
    assert "3. implementation-engineer (implement)  model=gpt-5.6-terra — goal 3" in d


def test_plan_prompt_modes():
    catalog = pipeline.load_catalog()
    p = pipeline.build_plan_prompt(_task(), [], catalog)
    assert "PLAN mode" in p and pipeline.PLAN_MARKER in p
    assert "- `quant` (advise)" in p and "`orchestrator`" not in p
    g = pipeline.build_plan_prompt(_task(), [_stage(1, "quant", "done", verdict="fail")], catalog)
    assert "GATE mode" in g and "REPLACEMENT" in g


def test_stage_prompt_and_resume_variants():
    role = pipeline.load_role("backtest-auditor")
    stages = [_stage(1, "backtest-engineer", "done", verdict="pass"), _stage(2, "backtest-auditor", "running", attempt=2)]
    p = pipeline.build_stage_prompt(_task(), stages[1], stages, role)
    assert "## Your stage: 2/2 — Backtest Auditor (advise, attempt 2)" in p
    assert "### Goal\ngoal 2" in p and pipeline.STAGE_RESULT_MARKER in p
    resume = pipeline.build_resume_prompt(stages[1], "pick B", fresh=False)
    assert resume.startswith("[User answer") and "pick B" in resume
    st = _stage(2, "backtest-auditor", "running", questions=["A or B?"], output="I asked stuff")
    fresh = pipeline.build_resume_prompt(st, "pick B", fresh=True)
    assert "- A or B?" in fresh and "I asked stuff" in fresh and "pick B" in fresh


# ---------------------------------------------------------------------------
# Codex usage -> tokens / $
# ---------------------------------------------------------------------------

def test_usage_accumulation_and_tokens():
    total: dict = {}
    pipeline.accumulate_usage(total, {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 10})
    pipeline.accumulate_usage(total, {"input_tokens": 50, "output_tokens": 5, "reasoning_output_tokens": 3})
    pipeline.accumulate_usage(total, None)
    assert total["input_tokens"] == 150 and total["cached_input_tokens"] == 60
    assert total["output_tokens"] == 15 and total["reasoning_output_tokens"] == 3
    assert pipeline.usage_tokens(total) == 165
    assert pipeline.usage_tokens(None) == 0


def test_codex_cost_needs_a_price_table(monkeypatch):
    usage = {"input_tokens": 1_000_000, "cached_input_tokens": 500_000, "output_tokens": 100_000}
    monkeypatch.delenv("WORKER_CODEX_PRICES", raising=False)
    assert pipeline.codex_cost_usd("gpt-5.6-terra", usage) is None
    monkeypatch.setenv("WORKER_CODEX_PRICES", '{"gpt-5.6-terra": {"input": 2, "cached_input": 0.5, "output": 8}}')
    # 500k uncached*2 + 500k cached*0.5 + 100k out*8 = 1.0 + 0.25 + 0.8
    assert pipeline.codex_cost_usd("gpt-5.6-terra", usage) == 2.05
    assert pipeline.codex_cost_usd("gpt-5.6-luna", usage) is None  # unpriced model
    monkeypatch.setenv("WORKER_CODEX_PRICES", '{"default": {"input": 1, "output": 1}}')
    assert pipeline.codex_cost_usd("gpt-5.6-luna", usage) == 1.1  # cached falls back to input price
    monkeypatch.setenv("WORKER_CODEX_PRICES", "not json")
    assert pipeline.codex_cost_usd("gpt-5.6-luna", usage) is None


def test_release_stage_gets_commit_rules():
    role = pipeline.load_role("integration-ops")
    task = {"id": 5, "title": "Test-DB", "details": "", "repos": ["demo-infrastructure"], "autopilot": "full-bots"}
    stage = {"seq": 3, "goal": "deploy", "attempt": 1, "role": "integration-ops", "kind": "implement", "status": "running"}
    p = pipeline.build_stage_prompt(task, stage, [stage], role, frozenset({"integration-ops"}))
    assert "Task #5: Test-DB" in p and "Do not push" in p
    assert "Do not push" not in pipeline.build_stage_prompt(task, stage, [stage], role, frozenset())


def test_hands_off_allow_is_listed():
    role = pipeline.load_role("implementation-engineer")
    task = {"id": 5, "title": "x", "details": "", "repos": ["demo-lstm"], "autopilot": "full-bots"}
    stage = {"seq": 1, "goal": "g", "attempt": 1, "role": role.id, "kind": "implement", "status": "running"}
    p = pipeline.build_stage_prompt(task, stage, [stage], role, frozenset(), ("demo-test-db: ok",))
    assert "hands-off rules" in p and "- demo-test-db: ok" in p

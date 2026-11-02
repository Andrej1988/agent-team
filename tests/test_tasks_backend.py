"""Unit tests for per-task agent-backend routing.

The Tasks UI lets each task pick its own model, and the model ID alone decides
which agent runs it: ``claude-*`` goes through the Claude SDK, ``gpt-*`` through
the Codex CLI. That coupling is the whole feature, so these tests pin:

* the routing table itself, including the fallback for IDs we can't classify
  (a custom ``WORKER_MODEL`` must not be silently rerouted);
* that the worker's copy of the routing rules hasn't drifted from the API's —
  the two live in different repos and share no filesystem, so nothing but a
  test keeps them honest;
* that a session id records which backend minted it, because a Codex thread
  cannot be resumed by the Claude SDK and resuming one as the other would
  strand the user's refine chat.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from pathlib import Path

import pytest

from agent_team.tasks import router as tasks_router


# ---------------------------------------------------------------------------
# Model ID -> backend routing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model,expected",
    [
        ("claude-haiku-4-5", "claude"),
        ("claude-sonnet-5", "claude"),
        ("claude-opus-5", "claude"),
        ("claude-opus-5-5", "claude"),
        ("claude-fable-5-1", "claude"),
        ("opus", "claude"),
        ("sonnet", "claude"),
        ("haiku", "claude"),
        ("gpt-5.6-luna", "codex"),
        ("gpt-5.6-terra", "codex"),
        ("gpt-6-sol", "codex"),
        ("gpt-6-astra", "codex"),
        ("codex-mini", "codex"),
        ("o3", "codex"),
    ],
)
def test_resolve_backend_routes_known_models(model, expected):
    # The env default is deliberately the *opposite* backend in each case, so
    # a passing test can only mean the model ID did the routing.
    other = "codex" if expected == "claude" else "claude"
    assert tasks_router.resolve_backend(model, other) == expected


def test_resolve_backend_is_case_and_whitespace_insensitive():
    assert tasks_router.resolve_backend("  Claude-Opus-5 ", "codex") == "claude"
    assert tasks_router.resolve_backend("GPT-5.6-Luna", "claude") == "codex"


@pytest.mark.parametrize("env_default", ["claude", "codex"])
def test_empty_model_falls_back_to_env_default(env_default):
    for empty in ("", "   ", None):
        assert tasks_router.resolve_backend(empty, env_default) == env_default


@pytest.mark.parametrize("env_default", ["claude", "codex"])
def test_unknown_model_falls_back_rather_than_guessing(env_default):
    """A custom WORKER_MODEL must keep working: we don't invent a backend for
    an ID we don't recognise, we use the configured default."""
    assert tasks_router.resolve_backend("some-private-model", env_default) == env_default


def test_normalise_backend_accepts_the_env_aliases():
    for alias in ("codex", "CODEX", " gpt ", "chatgpt"):
        assert tasks_router._normalise_backend(alias) == "codex"
    for alias in ("claude", "", None, "nonsense"):
        assert tasks_router._normalise_backend(alias) == "claude"


# ---------------------------------------------------------------------------
# Cross-repo drift guard
# ---------------------------------------------------------------------------


_WORKER_PY = Path(__file__).resolve().parents[1] / "src" / "agent_team" / "worker" / "worker.py"


def _literal_from(source: str, name: str):
    """Pull a module-level literal assignment out of source without importing
    it — worker.py imports claude_agent_sdk, which only exists in the bridge."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found")


@pytest.mark.skipif(not _WORKER_PY.exists(), reason="worker source missing")
def test_worker_routing_table_matches_the_api_copy():
    """worker.py and routers/tasks.py each carry the routing table because the
    bridge container and the monitoring container share no filesystem. If one
    side gains a model family and the other doesn't, the UI would promise a
    backend the worker won't actually use."""
    src = _WORKER_PY.read_text(encoding="utf-8")
    assert _literal_from(src, "_CLAUDE_ALIASES") == tasks_router._CLAUDE_ALIASES
    assert _literal_from(src, "_CODEX_PREFIXES") == tasks_router._CODEX_PREFIXES


@pytest.mark.skipif(not _WORKER_PY.exists(), reason="worker source missing")
def test_worker_no_longer_downgrades_claude_models_unconditionally():
    """Regression guard for the bug that made this feature impossible: the
    Codex runner used to rewrite any claude-* override to the Codex default
    before dispatch, so a Claude model could never reach the Claude SDK."""
    src = _WORKER_PY.read_text(encoding="utf-8")
    assert 'if model.startswith("claude-") or model in {"haiku", "sonnet", "opus"}:' not in src
    # The dispatch must key off the resolved backend, not the global env var.
    assert 'if (backend or TASKS_AGENT_BACKEND) == "codex":' in src


@pytest.mark.skipif(not _WORKER_PY.exists(), reason="worker source missing")
def test_worker_passes_a_resolved_backend_for_both_kinds():
    src = _WORKER_PY.read_text(encoding="utf-8")
    assert "backend = _backend_for(refine_model)" in src


# ---------------------------------------------------------------------------
# Session/backend stickiness
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path, monkeypatch):
    from agent_team.tasks import db as tasks_db
    tasks_db._ensured.clear()
    return tasks_db


def test_session_backend_column_is_created(db):
    t = db.create_task("t", "", 2, [])
    assert "session_backend" in db.get_task(t["id"])


def test_set_session_id_records_the_minting_backend(db):
    t = db.create_task("t", "", 2, [])
    assert db.set_session_id(t["id"], "thread-abc", "codex")
    row = db.get_task(t["id"])
    assert row["session_id"] == "thread-abc"
    assert row["session_backend"] == "codex"


def test_set_session_id_without_backend_leaves_the_stored_value_alone(db):
    """An older worker that doesn't send `backend` must not blank the field —
    that would make a known-Codex session look like unknown provenance and
    let the Claude SDK try to resume it."""
    t = db.create_task("t", "", 2, [])
    db.set_session_id(t["id"], "thread-abc", "codex")
    db.set_session_id(t["id"], "thread-def")
    row = db.get_task(t["id"])
    assert row["session_id"] == "thread-def"
    assert row["session_backend"] == "codex"


def test_set_session_id_returns_false_for_a_missing_task(db):
    assert db.set_session_id(9999, "x", "claude") is False


def test_migration_is_idempotent_on_an_existing_db(db, tmp_path):
    """The column is added by an additive migration that reruns on every cold
    start; a second run must not fail or clobber an existing value."""
    t = db.create_task("t", "", 2, [])
    db.set_session_id(t["id"], "sess-1", "claude")
    db._ensured.clear()
    db._ensure_db()
    assert db.get_task(t["id"])["session_backend"] == "claude"


def test_session_backend_survives_a_real_pre_migration_row(tmp_path, monkeypatch):
    """Simulate a DB created before this feature: the column is missing and
    rows exist. Migration must add it as NULL, not drop the rows."""
    from agent_team.tasks import db as tasks_db

    db_path = tmp_path / "tasks.db"
    cx = sqlite3.connect(str(db_path))
    cx.executescript(tasks_db.SCHEMA)
    cx.execute(
        "INSERT INTO tasks (title, details, prio, repos_json, status, "
        "created_at, updated_at) VALUES ('old', '', 2, '[]', 'refined', 'x', 'x')"
    )
    cx.commit()
    cx.close()
    tasks_db._ensured.clear()

    rows = tasks_db.list_tasks()
    assert len(rows) == 1
    assert rows[0]["title"] == "old"
    assert rows[0]["session_backend"] is None


# ---------------------------------------------------------------------------
# UI contract
# ---------------------------------------------------------------------------

_TASKS_TSX = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "tasks" / "shared.tsx"


def test_dropdown_offers_both_backends():
    """The point of the feature: the picker must list Claude *and* Codex
    models. It regressed to Codex-only once already."""
    src = _TASKS_TSX.read_text(encoding="utf-8")
    claude_block = re.search(r"const CLAUDE_MODELS.*?^\]", src, re.S | re.M)
    codex_block = re.search(r"const CODEX_MODELS.*?^\]", src, re.S | re.M)
    assert claude_block and codex_block
    assert "claude-opus-5" in claude_block.group(0)
    assert "gpt-5.6-terra" in codex_block.group(0)


def test_dropdown_is_fed_by_the_live_catalog():
    """The static lists are only the fallback: the picker must read the
    catalog context so a new generation shows up without a frontend edit."""
    src = _TASKS_TSX.read_text(encoding="utf-8")
    assert "useContext(ModelCatalogContext)" in src
    assert "catalogToOptions(catalog) ?? MODEL_OPTIONS" in src
    page = _TASKS_TSX.parents[2] / "pages" / "Tasks.tsx"
    assert "<ModelCatalogProvider catalog={modelCatalog}>" in page.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# /api/tasks/models — catalog/upgrade via files the worker answers
# (claude-agent/monitor_control.py); no docker exec from here
# ---------------------------------------------------------------------------

@pytest.fixture
def control(monkeypatch, tmp_path):
    # No Docker here: the worker counts as running when it polled recently.
    monkeypatch.setattr(tasks_router, "_control_dir", lambda: tmp_path)
    monkeypatch.setattr(tasks_router, "_worker_running", lambda: True)
    monkeypatch.setattr(tasks_router.time, "sleep", lambda _s: None)
    return tmp_path


_CATALOG_PAYLOAD = {
    "generated_at": "2026-09-24T15:00:00+00:00",
    "claude": {"cli_version": "2.1.267", "latest_version": "2.1.281", "update_available": True,
               "models": [{"value": "claude-opus-5", "label": "Opus 5", "description": "", "disabled": False,
                           "hint": "", "backend": "claude", "aliases": ["opus"]},
                          {"value": "", "label": "Opus 5.5 (disabled)", "description": "u",
                           "disabled": True, "hint": "Update to 2.1.280+ to use Opus 5.5", "backend": "claude"}],
               "aliases": {"opus": "claude-opus-5"}, "default_resolved": "claude-sonnet-5", "error": None},
    "codex": {"cli_version": "0.154.0", "latest_version": "0.156.1", "update_available": True,
              "models": [{"value": "gpt-6-astra", "label": "GPT-6-Astra", "description": "", "disabled": False,
                          "hint": "", "backend": "codex", "aliases": []}],
              "fetched_at": "2026-09-24T14:30:32Z", "error": None},
}


def test_models_endpoint_serves_the_workers_catalog(control):
    (control / "model-catalog.json").write_text(json.dumps({**_CATALOG_PAYLOAD, "written_at": 1.0}))
    body = tasks_router.model_catalog()
    assert body["cached"] is True
    assert body["claude"]["update_available"] is True
    assert body["claude"]["models"][1]["disabled"] is True
    assert body["codex"]["models"][0]["value"] == "gpt-6-astra"
    assert body["error"] is None
    assert not (control / "catalog.request").exists()


def test_models_refresh_asks_the_worker(control, monkeypatch):
    monkeypatch.setattr(tasks_router, "_CATALOG_REFRESH_WAIT_S", 0.0)
    body = tasks_router.model_catalog(refresh=True)
    assert (control / "catalog.request").exists()
    assert body["cached"] is False


def test_models_endpoint_fails_soft_without_a_catalog(control):
    body = tasks_router.model_catalog()
    assert body["claude"]["models"] == [] and body["codex"]["models"] == []
    assert "demo-worker" in body["error"]


def test_models_endpoint_reports_script_failures(control):
    (control / "model-catalog.json").write_text(json.dumps({"error": "model_catalog.py exit 1: boom"}))
    body = tasks_router.model_catalog()
    assert body["claude"]["models"] == []
    assert "exit 1" in body["error"] and "boom" in body["error"]


def test_upgrade_endpoint_waits_for_the_workers_answer(control, monkeypatch):
    def _worker_answers(_s):
        req = control / "cli-upgrade.request"
        if req.exists():
            rid = json.loads(req.read_text())["id"]
            req.unlink()
            (control / "cli-upgrade.result.json").write_text(json.dumps(
                {"id": rid, "exit_code": 0, "log": ["[cli-upgrade] claude CLI 2.1.270 → 2.1.281 (user prefix)"]}))
            (control / "model-catalog.json").write_text(json.dumps({**_CATALOG_PAYLOAD, "written_at": 9e12}))

    monkeypatch.setattr(tasks_router.time, "sleep", _worker_answers)
    body = tasks_router.upgrade_clis()
    assert body["ok"] is True
    assert body["log"] == ["[cli-upgrade] claude CLI 2.1.270 → 2.1.281 (user prefix)"]
    assert body["catalog"]["codex"]["models"][0]["value"] == "gpt-6-astra"


def test_upgrade_endpoint_ignores_an_old_result(control, monkeypatch):
    from fastapi import HTTPException

    (control / "cli-upgrade.result.json").write_text(json.dumps({"id": "1.0", "exit_code": 0, "log": []}))
    monkeypatch.setattr(tasks_router, "_UPGRADE_WAIT_S", 0.0)
    with pytest.raises(HTTPException) as exc:
        tasks_router.upgrade_clis()
    assert exc.value.status_code == 504


def test_upgrade_endpoint_refuses_when_the_worker_is_down(control, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(tasks_router, "_worker_running", lambda: False)
    with pytest.raises(HTTPException) as exc:
        tasks_router.upgrade_clis()
    assert exc.value.status_code == 409
    assert not (control / "cli-upgrade.request").exists()


def test_worker_defaults_read_the_published_env(control):
    (control / "worker-env.json").write_text(json.dumps(
        {"WORKER_REFINE_MODEL": "gpt-5.6-luna", "WORKER_EXECUTE_MODEL": "", "WORKER_MODEL": "claude-opus-5",
         "TASKS_AGENT_BACKEND": "codex", "WORKER_ORCHESTRATOR_MODEL": ""}))
    body = tasks_router.worker_defaults()
    assert body["error"] is None and body["container_running"] is True
    assert body["refine"] == "gpt-5.6-luna" and body["execute"] == "claude-opus-5"


def test_worker_defaults_endpoint_exposes_the_backend():
    """The UI labels the Default entry with the backend it resolves to; that
    only works if the endpoint returns it."""
    import inspect

    src = inspect.getsource(tasks_router.worker_defaults)
    for key in ('"backend"', '"refine_backend"', '"execute_backend"'):
        assert key in src

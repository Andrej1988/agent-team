#!/usr/bin/env bash
# Start agent-team natively (no Docker): web, chat and one worker per project.
# Config: ~/.agent-team/local.yaml (or --config / AT_LOCAL_CONFIG); see
# docs/SETUP.md. `tools/run-local.sh --check` only checks.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${AT_PYTHON:-.venv/bin/python}"
PYTHONPATH="src:base" exec "$PY" -m agent_team.local "$@"

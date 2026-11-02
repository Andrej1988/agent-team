#!/usr/bin/env bash
# agent-team-worker-<project> entrypoint: refresh the agent CLIs (like the
# bridge/chat do at every start), then run the tasks worker for the project
# given by TASKS_API_URL / WORKER_TEAM_DIR / WORKER_CWD.
set -u
cd /app
# shellcheck source=/dev/null
source /app/cli-upgrade.sh
exec python -u -m agent_team.worker.worker

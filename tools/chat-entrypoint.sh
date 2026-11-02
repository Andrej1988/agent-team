#!/usr/bin/env bash
# agent-team-chat entrypoint: refresh the agent CLIs (as the bridge does at
# every start; falls back to the image-baked ones), then serve the chat API.
set -u
cd /app
# shellcheck source=/dev/null
source /app/cli-upgrade.sh
exec uvicorn agent_team.chat.server:app --host 0.0.0.0 --port 8001 --log-level info --no-access-log

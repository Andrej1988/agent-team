import os

import uvicorn

uvicorn.run("agent_team.sysmon.server:app", host="0.0.0.0", port=int(os.environ.get("AT_SYSMON_PORT", "8010")),
            log_level="warning")

"""Worker tests: the worker reads its team definition at import time — point
it at the demo fixture team (WORKER_TEAM_DIR) before any test module imports
it."""

import os
from pathlib import Path

os.environ.setdefault("WORKER_TEAM_DIR", str(Path(__file__).resolve().parents[1] / "fixtures" / "team-trading"))

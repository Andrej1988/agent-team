"""Remove a task's throwaway containers once the task is finished.

Stages start test containers named ``task-<id>-...``. Hands-off stages may
not run ``docker rm`` (the rule protects live services), so a container
started without ``--rm`` used to linger until someone removed it by hand.
The worker removes them when it removes the task's worktree. It uses the
same Docker endpoint as the agents (``DOCKER_HOST``), so whatever policy
sits in front of Docker still decides what this project may remove.
"""

from __future__ import annotations

import logging
import re
import subprocess
from typing import Callable, Sequence

log = logging.getLogger(__name__)

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=60)


def names_for(tid: int, names: Sequence[str]) -> list[str]:
    """Names that belong to task ``tid`` (``task-<tid>-...``, exact id)."""
    rx = re.compile(rf"^task-{int(tid)}-[A-Za-z0-9_.-]+$")
    return sorted(n for n in names if rx.match(n))


def remove(tid: int, run: Runner = _run) -> list[str]:
    """Force-remove all containers of task ``tid``; returns the removed names.

    Failures (no docker CLI, endpoint refuses a container) are logged and
    otherwise ignored: cleanup must never block the workspace sweep."""
    try:
        ps = run(["docker", "ps", "-a", "--filter", f"name=^task-{int(tid)}-",
                  "--format", "{{.Names}}"])
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("[task %s] container cleanup: %s", tid, e)
        return []
    if ps.returncode != 0:
        log.warning("[task %s] container cleanup: docker ps: %s", tid, (ps.stderr or "").strip()[:300])
        return []
    removed = []
    for name in names_for(tid, ps.stdout.split()):
        try:
            r = run(["docker", "rm", "-f", name])
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("[task %s] container %s: %s", tid, name, e)
            continue
        if r.returncode == 0:
            removed.append(name)
        else:
            log.warning("[task %s] container %s not removed: %s", tid, name, (r.stderr or "").strip()[:300])
    if removed:
        log.info("[task %s] removed containers: %s", tid, ", ".join(removed))
    return removed

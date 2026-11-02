"""One memory per project for every agent of that project.

Claude keeps its auto-memory per working directory
(``~/.claude/projects/<cwd with / → ->/memory``). Agents in a sub-repo
(``/workspaces/system/backend``) would build their own, nearly empty
memory and never see the project's (``/workspaces/system``).
``share(cwd, root)`` makes the sub-repo's memory directory a symlink to the
project's — notes already written there move over first, their index lines
are appended to the project's ``MEMORY.md``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

log = logging.getLogger("agent_team.memory")

PROJECTS = Path(os.environ.get("CLAUDE_PROJECTS_DIR", str(Path.home() / ".claude" / "projects")))


def memory_dir(cwd: str) -> Path:
    return PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", cwd.rstrip("/")) / "memory"


def share(cwd: str, root: str) -> bool:
    """Link ``cwd``'s memory to ``root``'s. True when a link exists afterwards."""
    cwd, root = cwd.rstrip("/"), root.rstrip("/")
    if cwd == root or not cwd.startswith(root + "/"):
        return False
    sub, target = memory_dir(cwd), memory_dir(root)
    target.mkdir(parents=True, exist_ok=True)
    if sub.is_symlink():
        return True
    if sub.is_dir():
        index = target / "MEMORY.md"
        lines = index.read_text().splitlines() if index.exists() else []
        for f in sorted(sub.iterdir()):
            if f.name == "MEMORY.md":
                for ln in f.read_text().splitlines():
                    if ln.strip() and ln not in lines:
                        lines.append(ln)
                continue
            dest = target / f.name
            if dest.exists():
                dest = target / f"{sub.parent.name.rsplit('-', 1)[-1]}_{f.name}"
            shutil.move(str(f), str(dest))
        index.write_text("\n".join(lines) + "\n")
        shutil.rmtree(sub)
    sub.parent.mkdir(parents=True, exist_ok=True)
    sub.symlink_to(target, target_is_directory=True)
    log.info("memory of %s now shared with %s", cwd, root)
    return True

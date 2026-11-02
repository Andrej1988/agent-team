"""Keep a chat inside its own project.

All chats run in one container that mounts every project's repos (and the
Docker socket), so a chat of project A could edit, deploy and commit
project B via ``docker run -v`` — and once did. A
PreToolUse hook (runtime.py) refuses any tool call that names another
project's folder: file tools by path, Bash by the command text, which also
catches ``docker run -v ~/git/<other>:…``. The agent-team channel
is the operator's own and keeps working across projects.

Best effort, not a sandbox: the Docker socket can reach anything. It stops
the honest mistake, and the prompt says the same.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from .. import projects

HOST_GIT = os.environ.get("AT_HOST_GIT") or os.path.expanduser("~/git")   # host path of the repos (what docker -v uses)
UNRESTRICTED = frozenset({"agent-team"})
_PATH_KEYS = ("file_path", "path", "notebook_path")


def _aliases(path: str) -> set[str]:
    """A repo root under both names: the container mount and the host path."""
    p = path.rstrip("/")
    name = Path(p).name
    return {p, f"/workspaces/{name}", f"{HOST_GIT}/{name}"}


def foreign_roots(project_id: str) -> list[str]:
    own = projects.project_root(project_id)
    mine = _aliases(own.cwd) if own else set()
    out: set[str] = set()
    for p in projects.all_projects():
        if p.id == project_id:
            continue
        root = projects.project_root(p.id)
        if root is not None:
            out |= _aliases(root.cwd)
    return sorted(out - mine, key=len, reverse=True)


def violation(project_id: str, tool_name: str, tool_input: dict) -> Optional[str]:
    """The other project's folder a tool call touches, or None."""
    if project_id in UNRESTRICTED:
        return None
    if tool_name == "Bash":
        text = str(tool_input.get("command") or "")
    else:
        text = " ".join(str(tool_input.get(k) or "") for k in _PATH_KEYS)
    for root in foreign_roots(project_id):
        if re.search(re.escape(root) + r"(?![\w.-])", text):
            return root
    return None


def deny_reason(project_id: str, root: str) -> str:
    return (f"Blocked: this chat belongs to project {project_id!r} and must not touch {root} "
            "(not even read-only or via docker run -v). Tell the user this belongs in that project's "
            "own chat and stop — do not work around it.")


def prompt_note(project_id: str) -> str:
    if project_id in UNRESTRICTED:
        return ""
    roots = [r.cwd for p in projects.all_projects() if p.id != project_id
             for r in [projects.project_root(p.id)] if r is not None]
    if not roots:
        return ""
    return ("\n\nScope: you work only in project " + repr(project_id) + ". Other projects ("
            + ", ".join(roots) + ") have their own chats — never read, change, deploy or commit there, "
            "also not via docker run -v. If asked, say which chat it belongs in.")

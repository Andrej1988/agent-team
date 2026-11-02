"""Deploy guard: agents never deploy, enforced as a PreToolUse hook.

On the work laptop the deploy target (a mounted network share the WAR files
are copied to) is writable by the user, and the agents run with the user's
rights. A prompt rule is not enough, so every agent session — task stages,
planning, scouts, chats — gets a hook that refuses

* Write / Edit / MultiEdit / NotebookEdit on a file under a guarded path,
* Bash commands that name a guarded path,
* Maven runs that activate a guarded profile (``-Pdeploy``, ``-P a,deploy``,
  ``--activate-profiles=deploy``),
* Bash commands matching a configured regex (e.g. ``\\bmvn\\b.*\\bdeploy\\b``
  for the deploy goal or ``-Ddeploy.target=``).

Hooks run in every permission mode, ``bypassPermissions`` included. This is a
tripwire for the realistic accidents (wrong profile, copying the WAR), not a
sandbox: a script that computes the path at run time is not caught.

Config: env ``AT_DEPLOY_GUARD`` = JSON ``{"paths": [...], "profiles": [...],
"patterns": [...]}`` (set by agent_team.local from ``deploy_guard`` in
local.yaml). Unset or empty = no guard.

``python -m agent_team.guard`` is the same check as a Claude Code command
hook (reads the hook JSON on stdin) for the CLI-run scouts.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ENV = "AT_DEPLOY_GUARD"
FILE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
              "NotebookEdit": "notebook_path"}
MATCHER = "Bash|Write|Edit|MultiEdit|NotebookEdit"
_MVN = re.compile(r"(^|[\s/;&|(])(mvn|mvnw|mvnd)(\.cmd)?(\s|$)")


@dataclass
class Guard:
    paths: list[str] = field(default_factory=list)
    profiles: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        forms: set[str] = set()
        for p in self.paths:
            raw = os.path.expanduser(str(p)).rstrip("/")
            if not raw:
                continue
            forms.add(raw)
            real = os.path.realpath(raw)
            forms.add(real)
            # macOS: /Volumes/x and /private/var/... spellings of one place
            if real.startswith("/private/"):
                forms.add(real[len("/private"):])
        self._paths = sorted(forms, key=len, reverse=True)
        self._profiles = {p.strip() for p in self.profiles if str(p).strip()}
        self._patterns = [re.compile(p) for p in self.patterns if str(p).strip()]

    def __bool__(self) -> bool:
        return bool(self._paths or self._profiles or self._patterns)

    # -- checks ---------------------------------------------------------------
    def _under(self, path: str) -> Optional[str]:
        p = os.path.expanduser(path)
        cands = {p.rstrip("/"), os.path.realpath(p)}
        for root in self._paths:
            for c in cands:
                if c == root or c.startswith(root + "/"):
                    return root
        return None

    def _bash(self, cmd: str) -> Optional[str]:
        for root in self._paths:
            if root in cmd:
                return f"der Befehl nennt den Deploy-Pfad {root}"
        if self._profiles and _MVN.search(cmd):
            hit = sorted(self._maven_profiles(cmd) & self._profiles)
            if hit:
                return f"Maven mit Deploy-Profil {', '.join(hit)}"
        for rx in self._patterns:
            if rx.search(cmd):
                return f"der Befehl passt auf die Sperrregel /{rx.pattern}/"
        return None

    @staticmethod
    def _maven_profiles(cmd: str) -> set[str]:
        try:
            toks = shlex.split(cmd, posix=True)
        except ValueError:
            toks = cmd.split()
        lists: list[str] = []
        for i, t in enumerate(toks):
            if t in ("-P", "--activate-profiles") and i + 1 < len(toks):
                lists.append(toks[i + 1])
            elif t.startswith("--activate-profiles="):
                lists.append(t.split("=", 1)[1])
            elif t.startswith("-P") and len(t) > 2 and not t.startswith("-Pl"):
                lists.append(t[2:])
        out = set()
        for lst in lists:
            for name in lst.split(","):
                name = name.strip()
                if name.startswith(("!", "-")):     # deactivated profile
                    continue
                name = name.lstrip("?+").strip()
                if name:
                    out.add(name)
        return out

    def violation(self, tool: str, tool_input: dict) -> Optional[str]:
        """German reason why the call is refused, or None."""
        if not self:
            return None
        tool_input = tool_input or {}
        if tool == "Bash":
            return self._bash(str(tool_input.get("command") or ""))
        key = FILE_TOOLS.get(tool)
        if key and tool_input.get(key):
            root = self._under(str(tool_input[key]))
            if root:
                return f"die Datei liegt im Deploy-Pfad {root}"
        return None


def deny_reason(why: str) -> str:
    return (f"Blockiert (Deploy-Sperre): {why}. Agenten deployen nie — kein Deploy-Profil, "
            "nichts auf das Deploy-Laufwerk. Baue und teste ohne Deploy-Profil "
            "(z. B. `mvn verify`); deployt wird vom Nutzer selbst nach dem Merge.")


def load(raw: Optional[str] = None) -> Optional[Guard]:
    raw = os.environ.get(ENV, "") if raw is None else raw
    if not raw.strip():
        return None
    data = json.loads(raw)
    g = Guard(paths=list(data.get("paths") or []), profiles=list(data.get("profiles") or []),
              patterns=list(data.get("patterns") or []))
    return g or None


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def sdk_matchers() -> list:
    """HookMatcher list for ClaudeAgentOptions(hooks={"PreToolUse": ...}); [] without a guard."""
    g = load()
    if g is None:
        return []
    from claude_agent_sdk import HookMatcher

    async def hook(input_data: dict, tool_use_id, context):
        d = input_data or {}
        why = g.violation(d.get("tool_name") or "", d.get("tool_input") or {})
        return _deny(deny_reason(why)) if why else {}

    return [HookMatcher(matcher=MATCHER, hooks=[hook])]


def sdk_hooks() -> Optional[dict]:
    """hooks= value for ClaudeAgentOptions, or None without a guard."""
    m = sdk_matchers()
    return {"PreToolUse": m} if m else None


def cli_settings() -> Optional[str]:
    """--settings JSON for a `claude -p` run, or None without a guard."""
    if load() is None:
        return None
    cmd = f"{shlex.quote(sys.executable)} -m agent_team.guard"
    return json.dumps({"hooks": {"PreToolUse": [
        {"matcher": MATCHER, "hooks": [{"type": "command", "command": cmd}]}]}})


def main() -> None:
    try:
        d = json.load(sys.stdin)
    except ValueError:
        return
    g = load()
    why = g.violation(d.get("tool_name") or "", d.get("tool_input") or {}) if g else None
    if why:
        print(json.dumps(_deny(deny_reason(why))))


if __name__ == "__main__":
    main()

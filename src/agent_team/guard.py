"""Deploy guard: agents never deploy, enforced as a PreToolUse hook.

Always on, without config: no `docker compose build/up` or `docker build`
from a worktree (``worktree_violation``) — see below for the configurable part.
Optional: memory limits for `docker run` (``DockerLimits``, env AT_DOCKER_LIMITS).

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


# Always on, no config: building or starting services from a worktree. An
# image built from a side branch silently reverts every fix that reached the
# live branch since the branch was cut. Throwaway stacks with their own
# project name (`-p task-62 …`) stay allowed: they cannot replace a service.
_COMPOSE_BUILD_UP = re.compile(
    r"\bdocker[\s-]+compose\b(?:(?!&&|\|\||;|\|).)*?\s(?:build|up)\b")
_COMPOSE_OWN_PROJECT = re.compile(r"\s(?:-p|--project-name)[\s=]+\S+")
_DOCKER_BUILD = re.compile(r"\bdocker\s+(?:buildx\s+)?build\b")
_WORKTREE = re.compile(r"(?:^|/)\.(?:task-)?worktrees(?:/|$)")


def worktree_violation(tool: str, tool_input: dict, cwd: Optional[str] = None) -> Optional[str]:
    """German reason why a build/deploy from a worktree is refused, or None."""
    if tool != "Bash":
        return None
    cmd = str((tool_input or {}).get("command") or "")
    for part in re.split(r"&&|\|\||;|\n", cmd):
        builds = (_COMPOSE_BUILD_UP.search(" " + part) and not _COMPOSE_OWN_PROJECT.search(part)) \
            or _DOCKER_BUILD.search(part)
        if not builds:
            continue
        where = [t for t in re.split(r"[\s=]+", cmd) if _WORKTREE.search(t)]
        if where or (cwd and _WORKTREE.search(cwd)):
            return (f"Bauen/Starten aus einem Arbeitsbaum ({(where or [cwd])[0]}). Ein Image aus einem "
                    "Nebenzweig macht Fixes rückgängig, die seitdem auf dem Hauptzweig gelandet sind. "
                    "Erst mergen (vorher den Hauptzweig in den Zweig holen, Tests), dann aus dem "
                    "Live-Ordner bauen. Wegwerf-Stacks mit eigenem `-p <name>` sind erlaubt")
    return None


# Configurable: memory limits for containers the agents start themselves.
# 2026-11: three backtests started with a bare `docker run` took ~14 GB each
# and pushed the host into a global OOM. Env ``AT_DOCKER_LIMITS`` = JSON
# ``{"max_memory": "16g", "cgroup_parent": "agent-tasks.slice"}``: every
# `docker run` / `docker create` must set ``--memory`` (at most max_memory)
# and, if configured, ``--cgroup-parent`` to that slice (a shared cap for all
# agent containers together). Unset = no check. `docker compose run` is not
# checked: compose services carry their own limits.
LIMITS_ENV = "AT_DOCKER_LIMITS"
_RUN_BOOL_SHORT = set("diPqt")
_RUN_BOOL_LONG = {
    "--detach", "--rm", "--interactive", "--tty", "--init", "--privileged",
    "--publish-all", "--read-only", "--no-healthcheck", "--oom-kill-disable",
    "--quiet", "--disable-content-trust", "--sig-proxy", "--help",
}
_SIZE = re.compile(r"^(\d+(?:\.\d+)?)([kmgt]?)i?b?$", re.I)


def parse_size(val: str) -> Optional[int]:
    """Docker memory size ("512m", "16g", "1.5GB") in bytes, or None."""
    m = _SIZE.match(str(val).strip())
    if not m:
        return None
    mult = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}[m.group(2).lower()]
    return int(float(m.group(1)) * mult)


def _run_options(toks: list[str]) -> Optional[dict[str, str]]:
    """Options of the first `docker run|create` in toks ({flag: value}), or None."""
    for i, t in enumerate(toks):
        if os.path.basename(t) != "docker":
            continue
        rest = toks[i + 1:]
        if rest[:1] == ["container"]:
            rest = rest[1:]
        if rest[:1] not in (["run"], ["create"]):
            continue
        opts: dict[str, str] = {}
        j = 1
        while j < len(rest):
            a = rest[j]
            if a == "--" or not a.startswith("-") or a == "-":
                break                                   # image reached
            if a.startswith("--"):
                name, eq, val = a.partition("=")
                if not eq and name not in _RUN_BOOL_LONG:
                    val = rest[j + 1] if j + 1 < len(rest) else ""
                    j += 1
                opts[name] = val
            elif all(c in _RUN_BOOL_SHORT for c in a[1:]):
                pass                                    # -d, -it, -dit
            else:
                name, val = a[:2], a[2:]
                if not val:
                    val = rest[j + 1] if j + 1 < len(rest) else ""
                    j += 1
                opts[name] = val
            j += 1
        return opts
    return None


@dataclass
class DockerLimits:
    max_memory: str = ""
    cgroup_parent: str = ""

    def violation(self, tool: str, tool_input: dict) -> Optional[str]:
        if tool != "Bash":
            return None
        cmd = str((tool_input or {}).get("command") or "")
        limit = parse_size(self.max_memory) if self.max_memory else None
        for part in re.split(r"&&|\|\||;|\||\n", cmd):
            try:
                toks = shlex.split(part, posix=True)
            except ValueError:
                toks = part.split()
            opts = _run_options(toks)
            if opts is None:
                continue
            mem = opts.get("--memory", opts.get("-m"))
            if limit:
                size = parse_size(mem) if mem else None
                if not size:
                    return f"`docker run` ohne Speicherlimit — setze `--memory {self.max_memory}` (oder weniger)"
                if size > limit:
                    return f"`--memory {mem}` liegt über dem Höchstwert {self.max_memory}"
            if self.cgroup_parent and opts.get("--cgroup-parent") != self.cgroup_parent:
                return f"`docker run` ohne `--cgroup-parent {self.cgroup_parent}` (gemeinsames Speicherbudget aller Agenten-Container)"
        return None


def limits_reason(why: str, lim: DockerLimits) -> str:
    flags = " ".join(f for f in (f"--memory {lim.max_memory}" if lim.max_memory else "",
                                 f"--cgroup-parent {lim.cgroup_parent}" if lim.cgroup_parent else "") if f)
    return (f"Blockiert (Speicherlimit): {why}. Container ohne Limit haben den Host schon in "
            f"den OOM getrieben. Starte ihn mit `docker run {flags} …`; braucht der Lauf mehr, "
            "verkleinere ihn (Zeitraum/Symbole teilen, nacheinander statt parallel).")


def load_limits(raw: Optional[str] = None) -> Optional[DockerLimits]:
    raw = os.environ.get(LIMITS_ENV, "") if raw is None else raw
    if not raw.strip():
        return None
    data = json.loads(raw)
    lim = DockerLimits(max_memory=str(data.get("max_memory") or ""),
                       cgroup_parent=str(data.get("cgroup_parent") or ""))
    if lim.max_memory and not parse_size(lim.max_memory):
        raise ValueError(f"{LIMITS_ENV}: max_memory {lim.max_memory!r} ist keine Größe")
    return lim if (lim.max_memory or lim.cgroup_parent) else None


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


def _check(d: dict, g: Optional[Guard], lim: Optional[DockerLimits] = None) -> Optional[str]:
    """Deny reason for one hook input (deploy guard, worktree builds, container limits)."""
    tool, inp = d.get("tool_name") or "", d.get("tool_input") or {}
    why = g.violation(tool, inp) if g else None
    if why:
        return deny_reason(why)
    why = worktree_violation(tool, inp, d.get("cwd"))
    if why:
        return f"Blockiert: {why}."
    why = lim.violation(tool, inp) if lim else None
    return limits_reason(why, lim) if why else None


def sdk_matchers() -> list:
    """HookMatcher list for ClaudeAgentOptions(hooks={"PreToolUse": ...}): the
    worktree check always, the deploy guard when configured."""
    g, lim = load(), load_limits()
    from claude_agent_sdk import HookMatcher

    async def hook(input_data: dict, tool_use_id, context):
        why = _check(input_data or {}, g, lim)
        return _deny(why) if why else {}

    return [HookMatcher(matcher=MATCHER, hooks=[hook])]


def sdk_hooks() -> Optional[dict]:
    """hooks= value for ClaudeAgentOptions, or None without a guard."""
    m = sdk_matchers()
    return {"PreToolUse": m} if m else None


def cli_settings() -> Optional[str]:
    """--settings JSON for a `claude -p` run (always: the worktree check)."""
    cmd = f"{shlex.quote(sys.executable)} -m agent_team.guard"
    return json.dumps({"hooks": {"PreToolUse": [
        {"matcher": MATCHER, "hooks": [{"type": "command", "command": cmd}]}]}})


def main() -> None:
    try:
        d = json.load(sys.stdin)
    except ValueError:
        return
    why = _check(d, load(), load_limits())
    if why:
        print(json.dumps(_deny(why)))


if __name__ == "__main__":
    main()

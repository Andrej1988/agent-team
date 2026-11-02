"""Project registry — one tenant per git project.

Each project has its own data directory (``tasks.db``, ``sessions/``,
``control/``) and its own team definition (``team.yaml`` + ``roles/*.md``),
mounted read-only from the project repo. Request handling sets the current
project in a ContextVar; ``tasks.db`` and ``tasks.agent_roles`` resolve
their paths and catalog through it.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import yaml

CONFIG_FILE = Path(os.environ.get("AT_PROJECTS_FILE", "/app/config/projects.yaml"))
DATA_ROOT = Path(os.environ.get("AT_DATA_DIR", "/data"))
TEAMS_ROOT = Path(os.environ.get("AT_TEAMS_DIR", "/teams"))

PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


@dataclass(frozen=True)
class Channel:
    """A chat channel: one agent conversation with
    its own working directory. ``id`` is unique across projects (the old bot
    name, so session state can be imported)."""
    id: str
    project: str
    label: str
    cwd: str
    context_file: Optional[str] = None
    backend: str = "claude"
    claude_model: Optional[str] = None
    codex_model: Optional[str] = None
    perm: str = "bypassPermissions"  # user 2026-10-27: every chat bypass by default
    # Service channel: a program (e.g. a bot asking for confirmations) posts messages
    # with buttons; my taps and typed commands go to that program, not to an
    # agent (chat/service.py).
    service: bool = False
    # Lead channel (Tasks v2, docs/TASKS-V2.md): the team lead's conversation
    # with me. Recommendations land here as cards with "Freigeben & starten";
    # the lead agent may create / propose / park tasks but never start one.
    lead: bool = False
    # Optional role file (path inside agent-team-chat) prepended as persona.
    persona: Optional[str] = None
    # Task chat (Tasks v2): the chat of one started task — the lead answers
    # there and the pipeline's progress is mirrored into it (tasks/mirror.py).
    task_id: Optional[int] = None
    # Check chat: an agent examines one alert ("<source channel>:<seq>") and
    # recommends fixing or deleting it.
    check_of: Optional[str] = None
    # Own chat the user opened for a repo (longer-running work), see
    # register_custom_channel.
    custom: bool = False


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    data_dir: Path
    team_dir: Path
    # Container name of the project's task worker (shown in the UI only;
    # liveness comes from its API polls, the web app has no Docker socket).
    worker: str = ""
    # Chat channels served by agent-team-chat (empty until the project's
    # earlier bot setups moved over).
    channels: tuple[Channel, ...] = ()
    # Has a tasks backend / team (tasks.db + team.yaml)?
    tasks: bool = True

    @property
    def control_dir(self) -> Path:
        return self.data_dir / "control"


_registry: dict[str, Project] = {}
_current: ContextVar[Optional[Project]] = ContextVar("agent_team_project", default=None)


class UnknownProject(LookupError):
    pass


def load(config_file: Path = CONFIG_FILE) -> dict[str, Project]:
    """(Re)load the registry from projects.yaml."""
    raw = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    out: dict[str, Project] = {}
    for pid, cfg in (raw.get("projects") or {}).items():
        if not PROJECT_ID_RE.match(pid):
            raise ValueError(f"invalid project id {pid!r}")
        cfg = cfg or {}
        out[pid] = Project(
            id=pid,
            name=str(cfg.get("name") or pid),
            data_dir=DATA_ROOT / "projects" / pid,
            team_dir=Path(os.path.expanduser(str(cfg["team_dir"]))) if cfg.get("team_dir") else TEAMS_ROOT / pid,
            worker=str(cfg.get("worker") or ""),
            channels=_channels(pid, cfg.get("chat") or {}),
            tasks=bool(cfg.get("tasks", True)),
        )
    _registry.clear()
    _registry.update(out)
    return dict(out)


CHANNEL_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,47}$")


def _home(path: Optional[str]) -> Optional[str]:
    return os.path.expanduser(str(path)) if path else None


def _channels(pid: str, chat: dict) -> tuple[Channel, ...]:
    if not chat.get("enabled", True):
        return ()
    d = chat.get("defaults") or {}
    out = []
    for c in chat.get("channels") or []:
        cid = str(c["id"])
        if not CHANNEL_ID_RE.match(cid):
            raise ValueError(f"invalid channel id {cid!r}")
        merged = {**d, **c}
        out.append(Channel(
            id=cid,
            project=pid,
            label=str(merged.get("label") or cid),
            cwd=os.path.expanduser(str(merged["cwd"])),
            context_file=_home(merged.get("context_file") or chat.get("context_file")),
            backend=str(merged.get("backend") or "claude"),
            claude_model=merged.get("claude_model") or None,
            codex_model=merged.get("codex_model") or None,
            perm=str(merged.get("perm") or "bypassPermissions"),
            service=bool(merged.get("service")),
            lead=bool(merged.get("lead")),
            persona=os.path.expanduser(merged["persona"]) if merged.get("persona") else None,
        ))
    return tuple(out)


def all_channels() -> list[Channel]:
    """Configured channels plus the task and check chats."""
    return [c for p in all_projects() for c in p.channels] + task_channels() + check_channels() + custom_channels()


# ---- task chats (Tasks v2) ---------------------------------------------------
# Registry <data>/chat/_task_channels.json {project: {task_id: title}}, written
# by the web app when a task starts (the only writer), read by both services.

def _task_registry_path() -> Path:
    return DATA_ROOT / "chat" / "_task_channels.json"


_task_cache: tuple[float, dict] = (-1.0, {})


def _task_registry() -> dict:
    global _task_cache
    path = _task_registry_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if mtime != _task_cache[0]:
        try:
            import json
            _task_cache = (mtime, json.loads(path.read_text()))
        except (OSError, ValueError):
            return _task_cache[1]
    return _task_cache[1]


def task_channel_id(project_id: str, task_id: int) -> str:
    return f"task-{project_id}-{task_id}"


def task_channels() -> list[Channel]:
    out = []
    for pid, tasks in _task_registry().items():
        if pid not in _registry:
            continue
        lead = next((c for c in _registry[pid].channels if c.lead), None)
        if lead is None:
            continue
        for tid, title in tasks.items():
            out.append(Channel(
                id=task_channel_id(pid, int(tid)), project=pid, label=f"#{tid} {title}"[:60],
                cwd=lead.cwd, context_file=lead.context_file, backend=lead.backend,
                claude_model=lead.claude_model, codex_model=lead.codex_model, perm=lead.perm,
                lead=True, persona=lead.persona, task_id=int(tid)))
    return out


def register_task_channel(project_id: str, task_id: int, title: str) -> Channel:
    """Create (or rename) the task's chat. Web app only."""
    import json
    reg = {k: dict(v) for k, v in _task_registry().items()}
    reg.setdefault(project_id, {})[str(task_id)] = title
    path = _task_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1))
    tmp.replace(path)
    return get_channel(task_channel_id(project_id, task_id))


# ---- check chats -------------------------------------------------------------
# Registry <data>/chat/_check_channels.json {channel_id: {source, seq, title}},
# written by the web app when I tap "Prüfen" on an alert, read by both
# services. A check chat inherits the source channel's project, cwd and agent.

def _check_registry_path() -> Path:
    return DATA_ROOT / "chat" / "_check_channels.json"


_check_cache: tuple[float, dict] = (-1.0, {})


def _check_registry() -> dict:
    global _check_cache
    path = _check_registry_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if mtime != _check_cache[0]:
        try:
            import json
            _check_cache = (mtime, json.loads(path.read_text()))
        except (OSError, ValueError):
            return _check_cache[1]
    return _check_cache[1]


def check_channel_id(source_id: str, seq: int) -> str:
    return f"check-{source_id}-{seq}"[:48]


def check_channels() -> list[Channel]:
    sources = {c.id: c for p in all_projects() for c in p.channels}
    sources.update({c.id: c for c in task_channels()})
    out = []
    for cid, meta in _check_registry().items():
        src = sources.get(meta.get("source"))
        if src is None:
            continue
        out.append(Channel(
            id=cid, project=src.project, label=f"Prüfen: {meta.get('title') or 'Meldung'}"[:60],
            cwd=src.cwd, context_file=src.context_file, backend=src.backend,
            claude_model=src.claude_model, codex_model=src.codex_model, perm=src.perm,
            check_of=f"{src.id}:{meta.get('seq')}"))
    return out


def register_check_channel(source: Channel, seq: int, title: str) -> Channel:
    """Create the check chat for alert ``seq`` of ``source``. Web app only."""
    import json
    cid = check_channel_id(source.id, seq)
    reg = dict(_check_registry())
    reg[cid] = {"source": source.id, "seq": seq, "title": title}
    path = _check_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1))
    tmp.replace(path)
    return get_channel(cid)


# ---- own chats ---------------------------------------------------------------
# Registry <data>/chat/_custom_channels.json {channel_id: {project, repo, title}},
# written by the web app ("Neuer Chat"), read by both services. The chat runs in
# the project's root directory or one of its repos, with the project's defaults
# (taken from the project's first configured channel).

def _custom_registry_path() -> Path:
    return DATA_ROOT / "chat" / "_custom_channels.json"


_custom_cache: tuple[float, dict] = (-1.0, {})


def _custom_registry() -> dict:
    global _custom_cache
    path = _custom_registry_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if mtime != _custom_cache[0]:
        try:
            import json
            _custom_cache = (mtime, json.loads(path.read_text()))
        except (OSError, ValueError):
            return _custom_cache[1]
    return _custom_cache[1]


def project_root(project_id: str) -> Optional[Channel]:
    """The configured channel whose cwd is the project's root (the shortest
    cwd among its agent channels) — template for own chats."""
    p = _registry.get(project_id)
    chans = [c for c in (p.channels if p else ()) if not c.service]
    return min(chans, key=lambda c: len(c.cwd)) if chans else None


def custom_channels() -> list[Channel]:
    out = []
    for cid, meta in _custom_registry().items():
        base = project_root(meta.get("project") or "")
        if base is None:
            continue
        repo = (meta.get("repo") or "").strip("/")
        out.append(Channel(
            id=cid, project=base.project, label=str(meta.get("title") or cid)[:60],
            cwd=f"{base.cwd}/{repo}" if repo else base.cwd, context_file=base.context_file,
            backend=base.backend, claude_model=base.claude_model, codex_model=base.codex_model,
            perm=base.perm, custom=True))
    return out


def register_custom_channel(project_id: str, repo: str, title: str) -> Channel:
    """Create an own chat. Web app only."""
    import json
    import re as _re
    import time as _time
    slug = _re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:20] or "chat"
    cid = f"c-{project_id}-{slug}-{int(_time.time()) % 100000}"[:48].rstrip("-")
    reg = dict(_custom_registry())
    reg[cid] = {"project": project_id, "repo": repo, "title": title}
    path = _custom_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1))
    tmp.replace(path)
    return get_channel(cid)


def lead_channel(project_id: str) -> Optional[Channel]:
    """The project's lead channel (Tasks v2), if it has one."""
    for c in all_channels():
        if c.project == project_id and c.lead and c.task_id is None:
            return c
    return None


def get_channel(channel_id: str) -> Channel:
    for c in all_channels():
        if c.id == channel_id:
            return c
    raise UnknownProject(channel_id)


def register(project: Project) -> None:
    """Add a project programmatically (tests)."""
    _registry[project.id] = project


def all_projects() -> list[Project]:
    if not _registry and CONFIG_FILE.exists():
        load()
    return list(_registry.values())


def get(project_id: str) -> Project:
    if not _registry and CONFIG_FILE.exists():
        load()
    try:
        return _registry[project_id]
    except KeyError:
        raise UnknownProject(project_id) from None


def current() -> Project:
    p = _current.get()
    if p is None:
        raise RuntimeError("no current project (missing projects.use()/set_current())")
    return p


def set_current(project: Project) -> None:
    """Set the project for the rest of the current context (request task)."""
    _current.set(project)


@contextmanager
def use(project: Project | str) -> Iterator[Project]:
    p = get(project) if isinstance(project, str) else project
    token = _current.set(p)
    try:
        yield p
    finally:
        _current.reset(token)

"""Agent sessions for chat channels.

One ``ChannelRuntime`` per channel (one agent conversation + its single
chat). The session machinery is the bridge's, unchanged in substance:

* Claude via the Agent SDK (``ClaudeSDKClient``), resumed from the stored
  session id; a persistent consumer task owns ``receive_messages()`` and
  routes messages into the running turn's queue, or — between turns — straight
  to the transcript (background task completions, Monitor lines).
* Steering: a message sent while a turn runs is ``query()``-ed into it.
* Tool approvals (``can_use_tool``) and ``AskUserQuestion`` (PreToolUse hook,
  works in every permission mode) become ``permission`` / ``question`` events;
  the web app answers them via ``answer_permission`` / ``answer_question``.
* Codex via ``codex exec --json`` (final text only).

Messages are transcript events
(``log.ChannelLog``); per-chat dicts became per-channel attributes; the
``task:`` chat commands are gone (the web app has the tasks UI); verbosity is
gone (the transcript keeps everything, the UI decides what to show).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from .. import budget, git_identity, guard, memory_share, projects
from ..agent_notes import QUIET_NOTE
from ..log_redaction import redact
from . import claude_login, scope
from .log import ChannelLog, channel_dir, open_requests

log = logging.getLogger("agent_team.chat")

BACKENDS = ("claude", "codex")
PERM_MODES = ("default", "acceptEdits", "bypassPermissions")
# How long an approval / question waits for an answer before it is denied
# (bridge: 5 min — the web app pushes to the phone, so give it longer).
APPROVAL_TIMEOUT = 900
# See bot.py: after a turn that received a steering message, wait this long
# for a follow-up turn before releasing the channel.
STEER_GRACE_SECONDS = 3.0
# ScheduleWakeup stays blocked: autonomous wakeup turns would post unprompted.
DISALLOWED_TOOLS: list[str] = ["ScheduleWakeup"]
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# One stream-json line can carry a whole image (Read of a screenshot: base64,
# twice); the SDK's 1 MB default broke the stream on a 1.09 MB line.
SDK_MAX_BUFFER_BYTES = 64 * 1024 * 1024
# Put into the turn's queue when the agent's stream is gone.
_STREAM_LOST = object()

CAPABILITIES_BLURB = (
    "You are running inside the agent-team-chat container (based on the "
    "base image), reached from a chat channel of the agent-team web "
    "app — the user often reads on a phone. "
    "What you have access to:\n"
    "  - File RW on your working directory (the project's repo tree).\n"
    "  - Stdlib python + claude-agent-sdk. NOT the project's own dependencies.\n"
    "  - The Docker socket is mounted, so `docker exec`, `docker run`, "
    "`docker logs`, `docker ps` and `docker compose` work against the host "
    "daemon — this is your delegation path for anything that needs the "
    "project's dependencies (tests, scripts, DB access, GPU).\n"
    "  - No direct GPU access (no /dev/nvidia*); HTTP to the public internet.\n"
    "\n"
    "Execution pattern: if a task needs the project's deps, DON'T try to run "
    "it in-process — `pip install` won't help and you'll waste tokens. "
    "`docker exec` into a running project container or `docker run --rm` "
    "the project's image instead. Never recreate the `agent-team-chat` "
    "container yourself — that kills this session and every other channel's.\n"
    "\n"
    "If a real task can't be done with file edits + docker delegation — "
    "e.g. it needs an interactive TUI or a host-side service — tell the user "
    "explicitly and stop. Don't silently fake it."
)

# Work laptop (AT_LOCAL=1, agent_team.local): no container, no Docker socket.
LOCAL_CAPABILITIES_BLURB = (
    "You are running natively on the user's work laptop (macOS), started by "
    "agent-team's local launcher and reached from a chat channel of its local "
    "web app. What you have access to:\n"
    "  - File RW on your working directory (the project's repo).\n"
    "  - The machine's own toolchains (node, java, python, gh, …) exactly as the "
    "user has them — run builds and tests directly, no Docker delegation.\n"
    "  - `gh` is logged in to the company's GitHub Enterprise: read issues, "
    "Dependabot alerts and PRs; never merge and never push to a default branch.\n"
    "\n"
    "If a real task can't be done this way, tell the user explicitly and stop. "
    "Don't silently fake it."
)
if os.environ.get("AT_LOCAL") == "1":
    CAPABILITIES_BLURB = LOCAL_CAPABILITIES_BLURB
WEB_INTERNAL_URL = os.environ.get("AT_WEB_INTERNAL_URL", "http://agent-team-web:8000").rstrip("/")


LEAD_RULES = """\
## You are the team lead in this channel (Tasks v2)

The user decides what gets built; you prepare that decision. Ideas and scout
proposals sit as tasks; you pick what is worth doing next, explain it, and
the user starts it with a click ("Freigeben & starten" under your proposal).

Tasks API (internal, no auth): {api}
  list            GET  {api}            (optional ?status=new)
  one task        GET  {api}/<id>
  create          POST {api}            {{"title","details","prio":1-3,"repos":[...]}}
  ausarbeiten     POST {api}/<id>/refine          (read-only agent writes a spec)
  edit            PATCH {api}/<id>       {{"title"?,"details"?,"prio"?,"repos"?}}
  propose         POST {api}/<id>/propose {{"note": "why now, rough plan, risk, effort"}}
                  -> card with "Freigeben & starten" in this channel
  park            POST {api}/<id>/park    {{"reason": "..."}}   (backlog: worth doing LATER)
  close           POST {api}/<id>/close   {{"reason": "..."}}   (will not be done: duplicate,
                  obsolete, solved elsewhere — only agent-made ideas; for the
                  user's own tasks ask them)
  merge           POST {api}/<id>/merge   {{"into": <id>, "note": "..."}} (folds it in, closes it)
  budget          GET  {base}/api/budget

Discussing a proposal (the user taps "Besprechen" — their message quotes the
card): read the task, ask what you need to know, and work their wishes into
the task itself — PATCH the details (append a short "Abgesprochen:" section;
change title/prio/repos if agreed), or run "ausarbeiten" when a spec helps.
The pipeline plans from the task, not from this chat. When you agree, propose
it again (POST propose with the updated note) — that posts a fresh card;
say that the start button is on the new card.

Rules:
- You can NOT start a task (the start route refuses agents) — propose it and
  let the user tap the button. Never try to work around that.
- Propose only what fits the weekly budget; say so when it doesn't.
- Park only scout proposals (details end with "(Vorschlag des Scouts …)");
  tasks the user wrote stay — suggest parking them instead.
- Talk German, short, the user reads on the phone.
"""


TASK_CHAT_RULES = """\
## This is the chat of task #{id} (already approved and started)

The pipeline posts its progress here: ▶ / ✓ / ✗ lines per stage and the stage
agents' own messages (author = role). You are not one of those stages — you
explain and steer for the user:
  task + stages   GET  {api}/{id}   and   GET {api}/{id}/stages
  stage log       /data/projects/<project>/sessions/{id}/stage_<seq>.jsonl
  answer a stage question (the user tells you the answer)
                  POST {api}/{id}/stages/<seq>/answer {{"text": "..."}}
  change the not-yet-run stages / limits: tell the user to use "Details" in
  the task (plan editor) — you do not edit the plan.
Do not edit the repo yourself in this chat; changes go through the pipeline.
Own branch: if /data/projects/<project>/control/worktrees/{id}.json exists,
the stages worked in their own worktree on branch task/{id} and the worker
committed there; the diff is in …/worktrees/{id}.diff. "Übernehmen" (button
or in the task) brings it into the live checkout — you do not commit or
merge for such a task.
Deploying when the user asks (button "Deployen lassen" or in words), after the
changes are in the live checkout (übernommen / committed): work out what the
changed files feed (compose services, images, mounted scripts — the repo's
CLAUDE.md / deploy notes say how it deploys), rebuild/recreate ONLY those, via
the repo's own deploy path. Nothing to deploy (e.g. a script read live) → say
so. Never push. Report what you rebuilt/restarted and how you checked it runs.
Otherwise (older task in the live checkout) — committing when the user asks (button "Committen" or in words):
  per repo of the task (`git status`), stage ONLY files this task changed
  (`git add <paths>` / `git add -p`; the stage logs and summaries say which) —
  other uncommitted work belongs to someone else; never `.env` / secrets.
  Message `Task #{id}: <title>` + short German body. No push. Report the
  commits (repo + hash) and what you left uncommitted and why.
"""


# ---- formatting (bot.py) -----------------------------------------------------

def fmt_tool_use(blk: ToolUseBlock) -> str:
    name = blk.name
    inp = blk.input or {}
    if name in ("Read", "Write", "Edit", "NotebookEdit"):
        detail = inp.get("file_path", "")
    elif name == "Bash":
        detail = inp.get("command", "")
    elif name == "Grep":
        detail = f"{inp.get('pattern', '')} in {inp.get('path', '.')}"
    elif name == "Glob":
        detail = inp.get("pattern", "")
    elif name in ("WebFetch", "WebSearch"):
        detail = inp.get("url") or inp.get("query") or ""
    elif name == "TodoWrite":
        detail = f"{len(inp.get('todos') or [])} items"
    elif name in ("Task", "Agent"):
        detail = inp.get("description") or inp.get("subagent_type") or ""
    else:
        detail = str(next(iter(inp.values()), "")) if inp else ""
    detail = str(detail).strip()
    if len(detail) > 500:
        detail = detail[:500] + "…"
    return f"{name}({detail})" if detail else name


def fmt_tool_result(blk: ToolResultBlock) -> str:
    content = getattr(blk, "content", None)
    if isinstance(content, list):
        text = " ".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    else:
        text = str(content) if content else ""
    text = text.strip()
    return text[:2000] + "…" if len(text) > 2000 else text


_TASK_STATUS_SYM = {"completed": "✅", "failed": "❌", "stopped": "🛑"}


def fmt_bg(msg) -> Optional[str]:
    if isinstance(msg, TaskNotificationMessage):
        sym = _TASK_STATUS_SYM.get(msg.status, "·")
        summary = (msg.summary or "").strip()[:300]
        head = f"{sym} bg task {msg.task_id} {msg.status}"
        return f"{head} — {summary}" if summary else head
    if isinstance(msg, TaskStartedMessage):
        desc = (msg.description or "").strip()[:200]
        head = f"▶ bg task {msg.task_id} started"
        return f"{head} — {desc}" if desc else head
    if isinstance(msg, TaskProgressMessage):
        line = ""
        data = msg.data or {}
        for key in ("text", "stdout", "event", "line"):
            v = data.get(key)
            if isinstance(v, str) and v.strip():
                line = v.strip()
                break
        if not line:
            line = (msg.description or "").strip()
        return f"· bg {msg.task_id}: {line[:600]}" if line else f"· bg {msg.task_id}"
    return None


def _tool_detail(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Bash":
        d = tool_input.get("command", "")
    elif tool_name in ("Read", "Write", "Edit", "NotebookEdit"):
        d = tool_input.get("file_path", "")
    elif tool_name in ("WebFetch", "WebSearch"):
        d = tool_input.get("url") or tool_input.get("query") or ""
    else:
        d = ", ".join(f"{k}={v!r}" for k, v in (tool_input or {}).items())
    d = (d or "").strip()
    return d[:1500] + " …" if len(d) > 1500 else d


def _suggestion_rules(suggestions: list) -> list[str]:
    out: list[str] = []
    for s in suggestions or []:
        if getattr(s, "type", None) not in ("addRules", "replaceRules"):
            continue
        for r in getattr(s, "rules", None) or []:
            tn = getattr(r, "tool_name", "?")
            rc = getattr(r, "rule_content", None)
            out.append(f"{tn}({rc})" if rc else tn)
    return out


def _opt_label(opt: dict, i: int) -> str:
    return (str(opt.get("label") or "").strip()) or f"Option {i + 1}"


def _clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _hook_deny(reason: str) -> dict:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


# ---- per-channel state (bridge state.json equivalent) -------------------------

@dataclass
class ChannelState:
    backend: Optional[str] = None                 # override of the channel default
    models: dict[str, str] = field(default_factory=dict)       # backend -> model override
    perm: Optional[str] = None
    session_ids: dict[str, str] = field(default_factory=dict)  # backend -> session/thread id

    @classmethod
    def load(cls, path: Path) -> "ChannelState":
        try:
            raw = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            return cls()
        return cls(
            backend=raw.get("backend"), models=dict(raw.get("models") or {}),
            perm=raw.get("perm"), session_ids=dict(raw.get("session_ids") or {}),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__, indent=2))
        tmp.replace(path)


def import_bridge_state(channel_id: str, bridge_state: Path) -> Optional[ChannelState]:
    """The bridge's state.json entry for the bot of the same name (its single
    chat), so the conversation continues where the legacy setup left off."""
    try:
        bot = json.loads(bridge_state.read_text()).get(channel_id) or {}
    except (OSError, ValueError):
        return None
    if not bot:
        return None

    def first(key: str) -> Optional[str]:
        vals = [v for v in (bot.get(key) or {}).values() if isinstance(v, str) and v]
        return vals[0] if vals else None

    st = ChannelState(backend=first("backends"), perm=first("perms"))
    for b, mkey, skey in (("claude", "models", "session_ids"), ("codex", "codex_models", "codex_session_ids")):
        if first(mkey):
            st.models[b] = first(mkey)
        if first(skey):
            st.session_ids[b] = first(skey)
    return st


@dataclass
class _Pending:
    future: asyncio.Future
    kind: str  # permission | question


class ChannelRuntime:
    def __init__(self, channel: projects.Channel, bridge_state: Optional[Path] = None):
        self.ch = channel
        self.log = ChannelLog(channel.id)
        self.state_path = channel_dir(channel.id) / "state.json"
        if not self.state_path.exists() and bridge_state is not None:
            imported = import_bridge_state(channel.id, bridge_state)
            if imported is not None:
                imported.save(self.state_path)
                log.info("[%s] imported bridge state (sessions: %s)", channel.id, imported.session_ids)
        self.state = ChannelState.load(self.state_path)
        self.client: Optional[ClaudeSDKClient] = None
        self.codex_proc: Optional[asyncio.subprocess.Process] = None
        self.lock = asyncio.Lock()
        self.in_turn_q: asyncio.Queue = asyncio.Queue()
        self.steers = 0
        self.consumer: Optional[asyncio.Task] = None
        self.active_model: Optional[str] = None
        self.pending: dict[str, _Pending] = {}
        self.turn_task: Optional[asyncio.Task] = None
        self._auth_failed = False

    # ---- settings ------------------------------------------------------------

    @property
    def backend(self) -> str:
        return self.state.backend or self.ch.backend

    def model(self) -> Optional[str]:
        default = self.ch.claude_model if self.backend == "claude" else self.ch.codex_model
        return self.state.models.get(self.backend) or default

    @property
    def perm(self) -> str:
        return self.state.perm or self.ch.perm

    def info(self) -> dict:
        return {
            "id": self.ch.id, "project": self.ch.project, "label": self.ch.label, "cwd": self.ch.cwd,
            "backend": self.backend, "model": self.model(),
            "model_override": self.state.models.get(self.backend), "active_model": self.active_model,
            "perm": self.perm, "busy": self.lock.locked(),
            "session": self.state.session_ids.get(self.backend),
            "defaults": {"backend": self.ch.backend, "claude_model": self.ch.claude_model,
                         "codex_model": self.ch.codex_model, "perm": self.ch.perm},
        }

    def _save(self) -> None:
        self.state.save(self.state_path)

    def _set_session(self, sid: Optional[str]) -> None:
        if sid:
            self.state.session_ids[self.backend] = sid
        else:
            self.state.session_ids.pop(self.backend, None)
        self._save()

    async def update_settings(self, backend: Optional[str] = None, model: Optional[str] = None,
                              perm: Optional[str] = None) -> dict:
        """Settings as in /backend, /model, /perm. Empty string = back to the
        channel default. Refused while a turn runs."""
        if self.lock.locked():
            raise RuntimeError("busy — finish the current turn first")
        changes = []
        if backend is not None:
            b = backend.strip().lower() or None
            if b is not None and b not in BACKENDS:
                raise ValueError(f"unknown backend {backend!r}")
            if (b or self.ch.backend) != self.backend:
                await self.drop_client(keep_session=True)
            self.state.backend = b if b and b != self.ch.backend else None
            changes.append(f"Backend: {self.backend}")
        if model is not None:
            m = model.strip() or None
            if m:
                self.state.models[self.backend] = m
            else:
                self.state.models.pop(self.backend, None)
            await self.drop_client(keep_session=True)
            changes.append(f"Modell: {self.model() or 'CLI-Default'}")
        if perm is not None:
            p = perm.strip() or None
            if p is not None and p not in PERM_MODES:
                raise ValueError(f"unknown permission mode {perm!r}")
            self.state.perm = p
            await self.drop_client(keep_session=True)
            changes.append(f"Rechte: {self.perm}")
        self._save()
        if changes:
            self.log.append("system", text=" · ".join(changes) + " — gilt ab der nächsten Nachricht.")
        return self.info()

    # ---- system prompt -------------------------------------------------------

    def _project_context(self) -> Optional[str]:
        if not self.ch.context_file:
            return None
        path = Path(self.ch.context_file)
        try:
            text = path.read_text().strip()
        except OSError:
            log.warning("[%s] context file %s not readable", self.ch.id, path)
            return None
        return f'<project-context path="{path}">\n{text}\n</project-context>' if text else None

    def handoff_append(self) -> str:
        """bot.py handoff_append: capabilities + project context + the repo's
        session handoff file, and the instruction to keep it updated."""
        handoff_path = Path(self.ch.cwd) / ".claude" / "SESSION.md"
        parts: list[str] = [CAPABILITIES_BLURB, ""]
        ctx = self._project_context()
        if ctx:
            parts += [ctx, ""]
        if self.ch.persona:
            try:
                parts += [f'<persona path="{self.ch.persona}">', Path(self.ch.persona).read_text().strip(),
                          "</persona>", ""]
            except OSError:
                log.warning("[%s] persona %s not readable", self.ch.id, self.ch.persona)
        if self.ch.lead:
            base = f"{WEB_INTERNAL_URL}/p/{self.ch.project}"
            parts += [LEAD_RULES.format(api=f"{base}/api/tasks", base=base)]
            if self.ch.task_id is not None:
                parts += [TASK_CHAT_RULES.format(id=self.ch.task_id, api=f"{base}/api/tasks")]
            # The lead's state is the task list, not the repo's SESSION.md
            # (shared with the other channels of the repo).
            return "\n".join(parts)
        try:
            if handoff_path.is_file():
                content = handoff_path.read_text().strip()
                if content:
                    parts += [f'<session-handoff path="{handoff_path}">', content, "</session-handoff>", ""]
        except Exception:
            log.exception("[%s] failed to read %s", self.ch.id, handoff_path)
        parts.append(
            f"Before this turn ends, update {handoff_path} (create the directory if "
            f"missing; overwrite the file — do not append). Skip ONLY if this turn "
            f"was a pure greeting or a trivial yes/no — in any other case (any tool "
            f"call, any recommendation, any non-trivial answer) you must update it. "
            f"Include: timestamp in Europe/Berlin "
            f"(run `TZ=Europe/Berlin date '+%Y-%m-%d %H:%M:%S'`), "
            f"source: agent-team:{self.ch.id}, what you did, current state, next steps. "
            f"Keep it short so the next session — whether Claude Code or one of the "
            f"chat channels — can read the whole thing at a glance."
        )
        return "\n".join(parts)

    # ---- approvals / questions ----------------------------------------------

    def _new_pending(self, kind: str) -> tuple[str, asyncio.Future]:
        req_id = uuid.uuid4().hex[:12]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[req_id] = _Pending(fut, kind)
        return req_id, fut

    def answer_permission(self, req_id: str, decision: str) -> bool:
        p = self.pending.get(req_id)
        if p is None or p.kind != "permission" or p.future.done():
            return False
        if decision not in ("once", "always", "deny"):
            raise ValueError("decision must be once|always|deny")
        p.future.set_result(decision)
        return True

    def answer_question(self, req_id: str, answers: list[dict]) -> bool:
        """answers: one {selected: [idx...], text?} per question."""
        p = self.pending.get(req_id)
        if p is None or p.kind != "question" or p.future.done():
            return False
        p.future.set_result(answers)
        return True

    def make_permission_cb(self):
        async def can_use_tool(tool_name: str, tool_input: dict, ctx: ToolPermissionContext):
            if tool_name == "AskUserQuestion":
                return PermissionResultDeny(
                    message="Ask your question and the options as plain text in your "
                    "reply; the user will answer in a normal message."
                )
            req_id, fut = self._new_pending("permission")
            suggestions = ctx.suggestions or []
            self.log.append(
                "permission", req_id=req_id, tool=tool_name,
                title=ctx.title or f"Claude möchte {tool_name} benutzen",
                detail=redact(_tool_detail(tool_name, tool_input)),
                blocked_path=ctx.blocked_path, rules=_suggestion_rules(suggestions),
                can_always=bool(suggestions),
            )
            log.info("[%s] approval requested tool=%s id=%s", self.ch.id, tool_name, req_id)
            try:
                decision = await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                decision = "timeout"
            finally:
                self.pending.pop(req_id, None)
            outcome = {"once": "einmal erlaubt", "always": "immer erlaubt", "deny": "abgelehnt",
                       "timeout": "keine Antwort — abgelehnt"}.get(decision, decision)
            self.log.append("resolved", req_id=req_id, outcome=outcome)
            if decision == "once":
                return PermissionResultAllow(updated_input=tool_input)
            if decision == "always":
                return PermissionResultAllow(updated_input=tool_input, updated_permissions=suggestions)
            if decision == "timeout":
                return PermissionResultDeny(
                    message=f"No response within {APPROVAL_TIMEOUT // 60} min — denied. "
                    "Ask the user to approve, or try another approach."
                )
            return PermissionResultDeny(message="The user denied this. Don't retry it; ask them or try another approach.")

        return can_use_tool

    def make_scope_hook(self):
        """Refuse tool calls that reach into another project's folders (scope.py)."""
        async def hook(input_data: dict, tool_use_id: Optional[str], context):
            d = input_data or {}
            root = scope.violation(self.ch.project, d.get("tool_name") or "", d.get("tool_input") or {})
            if root is None:
                return {}
            log.info("[%s] blocked %s touching %s", self.ch.id, d.get("tool_name"), root)
            return _hook_deny(scope.deny_reason(self.ch.project, root))
        return hook

    def make_question_hook(self):
        async def hook(input_data: dict, tool_use_id: Optional[str], context):
            if (input_data or {}).get("tool_name") != "AskUserQuestion":
                return {}
            questions = list(((input_data.get("tool_input") or {}).get("questions")) or [])
            if not questions:
                return _hook_deny("AskUserQuestion had no questions. Ask in plain text instead.")
            req_id, fut = self._new_pending("question")
            self.log.append("question", req_id=req_id, questions=[{
                "question": q.get("question") or "", "header": q.get("header") or "",
                "multi": bool(q.get("multiSelect")),
                "options": [{"label": _opt_label(o, i), "description": o.get("description") or ""}
                            for i, o in enumerate(q.get("options") or [])],
            } for q in questions])
            try:
                answers = await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                answers = None
            finally:
                self.pending.pop(req_id, None)
            if not answers:
                self.log.append("resolved", req_id=req_id, outcome="keine Antwort")
                return _hook_deny(
                    f"The user did not answer within {APPROVAL_TIMEOUT // 60} min. Do NOT pick an "
                    "option for them and do NOT call AskUserQuestion again — instead briefly tell the "
                    "user the question is still open (they can reply with their choice any time) and "
                    "stop here. Do not start the work."
                )
            lines, shown = [], []
            for qi, q in enumerate(questions):
                a = answers[qi] if qi < len(answers) else {}
                opts = q.get("options") or []
                chosen = [_opt_label(opts[i], i) for i in (a.get("selected") or []) if 0 <= i < len(opts)]
                if (a.get("text") or "").strip():
                    chosen.append(a["text"].strip())
                hdr = (q.get("header") or "").strip()
                label = f"Q{qi + 1}" + (f" [{hdr}]" if hdr else "")
                lines.append(f'{label} "{_clip(q.get("question") or "", 300)}": '
                             + (", ".join(chosen) if chosen else "(no answer)"))
                shown.append(", ".join(chosen) or "—")
            self.log.append("resolved", req_id=req_id, outcome=" | ".join(shown))
            return _hook_deny(
                "AskUserQuestion was answered out-of-band via the chat UI. The user's answer(s):\n"
                + "\n".join(lines)
                + "\n\nProceed with these. Do not call AskUserQuestion again for these questions."
            )

        return hook

    # ---- Claude session ------------------------------------------------------

    def _record_usage(self, msg: RateLimitEvent) -> None:
        try:
            budget.record(msg.rate_limit_info.raw or {}, f"chat:{self.ch.id}")
        except Exception:  # never let bookkeeping break a turn
            log.exception("[%s] could not record usage", self.ch.id)

    async def _push_bg(self, msg) -> None:
        text = fmt_bg(msg)
        if text:
            self.log.append("bg", text=redact(text))

    async def _consume_stream(self, client: ClaudeSDKClient) -> None:
        """bot.py _consume_stream: the only reader of receive_messages();
        in-turn messages go to the turn's queue, others to the transcript.

        When the stream ends or breaks (CLI exited, a message over the SDK's
        buffer limit …) the client is dead: re-reading it returns at once,
        which used to spin a core and leave the running turn waiting forever.
        Now the client is dropped (the next message resumes the session) and
        the turn is told."""
        try:
            async for msg in client.receive_messages():
                if isinstance(msg, RateLimitEvent):
                    self._record_usage(msg)
                    continue
                if self.lock.locked():
                    await self.in_turn_q.put(msg)
                else:
                    await self._push_bg(msg)
            log.warning("[%s] receive_messages() ended", self.ch.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("[%s] stream from the agent broke", self.ch.id)
        if self.client is client:
            self.client, self.consumer, self.active_model = None, None, None
            asyncio.create_task(self._close_dead(client))
        if self.lock.locked():
            await self.in_turn_q.put(_STREAM_LOST)

    async def _close_dead(self, client: ClaudeSDKClient) -> None:
        try:
            await client.disconnect()
        except Exception:
            log.warning("[%s] error disconnecting a dead client", self.ch.id, exc_info=True)

    async def get_client(self) -> ClaudeSDKClient:
        if self.client is not None:
            return self.client
        # Sub-repo channels share the project's memory (memory_share.py).
        root = projects.project_root(self.ch.project)
        if root is not None:
            try:
                memory_share.share(self.ch.cwd, root.cwd)
            except OSError:
                log.warning("[%s] could not share project memory", self.ch.id, exc_info=True)
        model, perm = self.model(), self.perm

        def opts(resume: Optional[str]) -> ClaudeAgentOptions:
            return ClaudeAgentOptions(
                permission_mode=perm, cwd=self.ch.cwd,
                can_use_tool=self.make_permission_cb(),
                hooks={"PreToolUse": [
                    HookMatcher(matcher="AskUserQuestion", hooks=[self.make_question_hook()]),
                    HookMatcher(matcher="Bash|Read|Write|Edit|MultiEdit|NotebookEdit|Grep|Glob",
                                hooks=[self.make_scope_hook()]),
                    *guard.sdk_matchers(),
                ]},
                disallowed_tools=DISALLOWED_TOOLS, model=model, resume=resume,
                # Attached images live outside the cwd; let the agent read them
                # without an approval prompt.
                add_dirs=[str(channel_dir(self.ch.id) / "inbox")],
                system_prompt={"type": "preset", "preset": "claude_code", "append": self.handoff_append() + QUIET_NOTE + scope.prompt_note(self.ch.project)},
                env=git_identity.chat(self.ch.id),
                max_buffer_size=SDK_MAX_BUFFER_BYTES,
            )

        resume_id = self.state.session_ids.get("claude")
        try:
            client = ClaudeSDKClient(options=opts(resume_id))
            await client.connect()
        except Exception:
            if not resume_id:
                raise
            log.warning("[%s] resume %s failed, starting fresh", self.ch.id, resume_id, exc_info=True)
            self._set_session(None)
            client = ClaudeSDKClient(options=opts(None))
            await client.connect()
        self.client = client
        self.in_turn_q = asyncio.Queue()
        self.consumer = asyncio.create_task(self._consume_stream(client), name=f"consumer-{self.ch.id}")
        log.info("[%s] opened Claude session model=%s perm=%s resume=%s", self.ch.id, model, perm, resume_id)
        return client

    async def drop_client(self, keep_session: bool = False) -> None:
        proc, self.codex_proc = self.codex_proc, None
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        client, self.client = self.client, None
        self.active_model = None
        self.steers = 0
        consumer, self.consumer = self.consumer, None
        if consumer is not None and not consumer.done():
            consumer.cancel()
            try:
                await consumer
            except (asyncio.CancelledError, Exception):
                pass
        if not keep_session:
            self._set_session(None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                log.exception("[%s] error disconnecting", self.ch.id)

    # ---- Codex ---------------------------------------------------------------

    async def run_codex_turn(self, prompt: str) -> str:
        """bot.py run_codex_turn: run/resume one `codex exec --json` turn."""

        async def once(session_id: Optional[str]):
            cmd = ["codex", "exec"] + (["resume", session_id] if session_id else []) + [
                "-", "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
            model = self.model()
            if model:
                cmd += ["--model", model]
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=self.ch.cwd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **git_identity.chat(self.ch.id)})
            self.codex_proc = proc
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=1200)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError("Codex turn timed out after 1200 seconds")
            finally:
                self.codex_proc = None
            messages, errors, thread_id = [], [], None
            for raw in stdout.decode(errors="replace").splitlines():
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "thread.started":
                    thread_id = event.get("thread_id")
                err = (event.get("message") if kind == "error"
                       else (event.get("error") or {}).get("message") if kind == "turn.failed" else None)
                if err and err not in errors:
                    errors.append(err)
                item = event.get("item") or {}
                if kind == "item.completed" and item.get("type") == "agent_message" and (item.get("text") or "").strip():
                    messages.append(item["text"].strip())
            err_text = stderr.decode(errors="replace")
            if not err_text.strip() and errors:
                err_text = "\n".join(errors)
            return proc.returncode or 0, "\n\n".join(messages), err_text, thread_id

        session_id = self.state.session_ids.get("codex")
        rc, answer, stderr, thread_id = await once(session_id)
        if rc and session_id:
            log.warning("[%s] Codex resume failed (%s); starting fresh", self.ch.id, session_id)
            self._set_session(None)
            rc, answer, stderr, thread_id = await once(None)
        if rc:
            raise RuntimeError(f"Codex failed: {redact(stderr.strip())[-1000:] or f'exit code {rc}'}")
        if thread_id and self.state.session_ids.get("codex") != thread_id:
            self._set_session(thread_id)
        self.active_model = self.model() or "Codex CLI default"
        return answer or "(Codex completed without a text response.)"

    # ---- a user message ------------------------------------------------------

    def save_images(self, images: list[dict]) -> list[str]:
        """[{name, data: base64 or data URL}] -> absolute paths under the
        channel's inbox, so the agent can Read them."""
        out = []
        d = channel_dir(self.ch.id) / "inbox"
        d.mkdir(parents=True, exist_ok=True)
        for img in images or []:
            data = str(img.get("data") or "")
            if data.startswith("data:"):
                data = data.split(",", 1)[-1]
            raw = base64.b64decode(data)
            if len(raw) > MAX_IMAGE_BYTES:
                raise ValueError("image too large (max 10 MB)")
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(img.get("name") or "image.jpg"))[:80]
            path = d / f"{uuid.uuid4().hex[:8]}_{safe}"
            path.write_bytes(raw)
            out.append(str(path))
        return out

    async def submit(self, text: str, quote: Optional[str] = None, images: Optional[list[dict]] = None,
                     note: Optional[str] = None) -> dict:
        """Accept a message: start a turn, or steer the running one. ``note``:
        an automatic prompt (not typed by the user) — the transcript shows the
        note as a system line instead of the prompt as the user's bubble."""
        text = (text or "").strip()
        image_paths = self.save_images(images or [])
        if not text and not image_paths:
            raise ValueError("empty message")
        if note:
            self.log.append("system", text=note)
        else:
            self.log.append("user", text=text, quote=quote, images=image_paths or None)
        prompt = text
        if quote:
            prompt = f"[The user is replying to this earlier message:]\n{quote}\n\n[Their reply:]\n{text}"
        if image_paths:
            attach = "\n\n".join(f"[Attached image at {p} — use the Read tool to view it.]" for p in image_paths)
            prompt = f"{prompt}\n\n{attach}" if prompt else f"(no caption — image only)\n\n{attach}"

        if self.lock.locked():
            if self.backend == "codex" or self.client is None:
                self.log.append("system", text="Läuft noch — bitte warten, bis die Antwort fertig ist.")
                return {"steered": False, "queued": False}
            try:
                await self.client.query(prompt)
            except Exception as e:  # noqa: BLE001
                self.log.append("error", text=f"Konnte nicht in den laufenden Turn einfügen: {e}")
                return {"steered": False}
            self.steers += 1
            self.log.append("system", text="📝 in den laufenden Turn eingefügt")
            return {"steered": True}

        self.turn_task = asyncio.create_task(self._turn(prompt), name=f"turn-{self.ch.id}")
        return {"started": True}

    async def _turn(self, prompt: str) -> None:
        async with self.lock:
            self.log.append("busy", busy=True)
            try:
                if self.backend == "codex":
                    answer = await self.run_codex_turn(prompt)
                    self.log.append("assistant", text=redact(answer))
                else:
                    await self._claude_turn(prompt)
            except Exception as e:  # noqa: BLE001
                log.exception("[%s] turn failed", self.ch.id)
                self.log.append("error", text=redact(f"Fehler: {e}"))
                self._check_auth(str(e))
            finally:
                self.log.append("busy", busy=False)
                self.log.append("turn_end")
            if self._auth_failed:
                self._auth_failed = False
                await self.drop_client(keep_session=True)

    def _check_auth(self, text: str) -> None:
        """An expired login surfaces as assistant text; tell the user where to
        fix it and drop the session process so the next turn re-reads the
        credentials."""
        if claude_login.is_auth_error(text):
            self.log.append("error", text="Claude ist nicht angemeldet — in der App unter Einstellungen → "
                                          "Claude-Anmeldung neu anmelden, dann die Nachricht erneut senden.",
                            auth=True)
            self._auth_failed = True

    async def _claude_turn(self, prompt: str) -> None:
        client = await self.get_client()
        await client.query(prompt)
        q = self.in_turn_q
        self.steers = 0
        auq_ids: set[str] = set()
        grace = False
        while True:
            if grace:
                try:
                    msg = await asyncio.wait_for(q.get(), STEER_GRACE_SECONDS)
                except asyncio.TimeoutError:
                    break
                if not isinstance(msg, (TaskNotificationMessage, TaskProgressMessage, TaskStartedMessage)):
                    grace = False
            else:
                msg = await q.get()
            if msg is _STREAM_LOST:
                self.log.append("error", text="Die Verbindung zum Agenten ist abgerissen — seine Antwort kam nicht "
                                              "mehr an. Bitte die Nachricht noch einmal senden; die Sitzung bleibt erhalten.")
                break
            if isinstance(msg, AssistantMessage):
                for blk in msg.content:
                    if isinstance(blk, TextBlock) and blk.text.strip():
                        self.log.append("assistant", text=redact(blk.text.rstrip()))
                        self._check_auth(blk.text)
                    elif isinstance(blk, ToolUseBlock):
                        if blk.name == "AskUserQuestion":
                            auq_ids.add(blk.id)
                        else:
                            self.log.append("tool_use", name=blk.name, text=redact(fmt_tool_use(blk)))
            elif isinstance(msg, UserMessage):
                for blk in getattr(msg, "content", []) or []:
                    if isinstance(blk, ToolResultBlock) and getattr(blk, "tool_use_id", None) not in auq_ids:
                        self.log.append("tool_result", text=redact(fmt_tool_result(blk)),
                                        error=bool(getattr(blk, "is_error", False)))
            elif isinstance(msg, SystemMessage) and msg.subtype == "init":
                data = msg.data or {}
                if data.get("model"):
                    self.active_model = data["model"]
                sid = data.get("session_id")
                if isinstance(sid, str) and sid and self.state.session_ids.get("claude") != sid:
                    self._set_session(sid)
            elif isinstance(msg, (TaskNotificationMessage, TaskProgressMessage, TaskStartedMessage)):
                await self._push_bg(msg)
            elif isinstance(msg, ResultMessage):
                budget.record_cost(getattr(msg, "total_cost_usd", None), f"chat:{self.ch.id}")
                if self.steers:
                    self.steers = 0
                    grace = True
                    continue
                break

    async def stop(self) -> bool:
        if not self.lock.locked():
            return False
        if self.codex_proc is not None and self.codex_proc.returncode is None:
            self.codex_proc.terminate()
        elif self.client is not None:
            await self.client.interrupt()
        self.log.append("system", text="⏹ Turn wird abgebrochen …")
        return True

    async def reset(self) -> None:
        await self.drop_client()
        self.log.append("system", text="Sitzung zurückgesetzt — die nächste Nachricht startet eine neue.")

    def alert(self, text: str, title: Optional[str] = None, source: Optional[str] = None) -> dict:
        return self.log.append("alert", text=redact(text), title=title, source=source)

    def expire_stale_requests(self) -> None:
        """Requests from a previous process can never be answered."""
        for ev in open_requests(self.ch.id):
            self.log.append("resolved", req_id=ev["req_id"], outcome="abgelaufen (Dienst neu gestartet)")

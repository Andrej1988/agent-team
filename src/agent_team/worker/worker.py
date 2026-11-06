"""Tasks agent worker — one container per project (agent-team-worker-<id>).

Polls the project's tasks API
(``TASKS_API_URL=http://agent-team-web:8000/p/<project>``) for refine /
execute / plan / stage work, runs a Claude (SDK) or Codex (CLI) session per
item, streams every event back via POST /event/<kind> and reports the result.

What is project-specific comes from the project's team definition
(``WORKER_TEAM_DIR``, = /teams/<project>): ``team.yaml`` (repos, repo_prefix,
``worker.label`` / ``worker.execute_conventions``) and ``roles/*.md``.

The image is agent-team-chat's (claude_agent_sdk, both agent CLIs,
tasks_api.py and model_catalog.py under /app).
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
import signal
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import tasks_api  # base/tasks_api.py, on PYTHONPATH

import yaml

from .. import log_redaction
from .. import budget, git_identity, guard
from ..agent_notes import QUIET_NOTE
from ..chat import claude_login
from . import evidence, monitor_control, pipeline, scouts, workspace

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

# ---- Logging --------------------------------------------------------------

_TZ = ZoneInfo("Europe/Berlin")


class _BerlinFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):  # noqa: N802
        return datetime.fromtimestamp(record.created, tz=_TZ).strftime(
            datefmt or "%Y-%m-%d %H:%M:%S"
        )


log_redaction.install()  # mask credentials in every log record
_handler = logging.StreamHandler()
_handler.setFormatter(
    _BerlinFormatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger("tasks-worker")

# tasks_api wraps HTTP/URL errors in TasksApiError, but a socket timeout or
# reset *while reading the body* escapes as a bare OSError (TimeoutError,
# ConnectionResetError). The poll loop must survive both: on 10-01 a slow
# monitoring backend killed the worker with an uncaught TimeoutError.
_API_ERRORS = (tasks_api.TasksApiError, OSError)


# ---- Config ---------------------------------------------------------------

CWD = os.environ.get("WORKER_CWD", "/workspace")
TEAM_DIR = Path(os.environ.get("WORKER_TEAM_DIR", "/team"))


def _load_team() -> dict:
    try:
        return yaml.safe_load((TEAM_DIR / "team.yaml").read_text(encoding="utf-8")) or {}
    except OSError:
        return {}


TEAM = _load_team()
_WORKER_CFG = TEAM.get("worker") or {}
# "the shop monorepo", "the docs workspace", …
# Project id from TASKS_API_URL (…/p/<id>): names the agents' git identity.
_api_url = os.environ.get("TASKS_API_URL", "").rstrip("/")
PROJECT_ID = _api_url.rsplit("/p/", 1)[1] if "/p/" in _api_url else "project"
PROJECT_LABEL = str(_WORKER_CFG.get("label") or f"the {TEAM.get('project') or 'project'} repository")
EXECUTE_CONVENTIONS = " ".join(str(_WORKER_CFG.get("execute_conventions") or "").split())
REPO_PREFIX = str(TEAM.get("repo_prefix") or "")
# Default model — used as the fallback when the kind-specific overrides
# aren't set. None => Claude CLI's default (currently Fable 5 — the
# top-tier model; set WORKER_*_MODEL to avoid its high per-token cost).
MODEL = os.environ.get("WORKER_MODEL") or None
# Per-kind overrides. Recommended cost setup: cheap Haiku for refine
# (read-only Read/Grep/Glob exploration), default Sonnet/Opus for
# execute (multi-file edits + tests). Example:
#   WORKER_REFINE_MODEL=claude-haiku-4-5
#   WORKER_EXECUTE_MODEL=claude-sonnet-5
REFINE_MODEL = os.environ.get("WORKER_REFINE_MODEL") or MODEL
EXECUTE_MODEL = os.environ.get("WORKER_EXECUTE_MODEL") or MODEL


def _normalise_backend(raw: str | None) -> str:
    b = (raw or "claude").strip().lower()
    return "codex" if b in {"codex", "gpt", "chatgpt"} else "claude"


# Fallback backend for tasks whose model override is empty (or an ID we
# don't recognise). Per-task overrides now pick the backend themselves —
# see _backend_for() — so this only decides the "Default (env)" case.
TASKS_AGENT_BACKEND = _normalise_backend(os.environ.get("TASKS_AGENT_BACKEND"))

# Model-ID -> backend routing. Bare aliases ("opus", "sonnet") are Claude
# CLI shorthands; everything gpt/o-series/codex goes to the Codex CLI.
# Kept in sync with resolve_backend() in the monitoring backend's
# routers/tasks.py (the two processes share no filesystem).
_CLAUDE_ALIASES = {"haiku", "sonnet", "opus", "fable", "default"}
_CODEX_PREFIXES = ("gpt-", "gpt5", "gpt6", "o1", "o3", "o4", "codex")


def _backend_for(model: str | None) -> str:
    """Which agent backend runs `model`.

    This is what makes the Tasks UI's per-task model picker able to mix
    Claude and Codex: the backend is a property of the chosen model, not a
    process-wide env var. An empty override (or an ID we can't classify,
    e.g. a custom WORKER_MODEL) falls back to TASKS_AGENT_BACKEND rather
    than guessing."""
    m = (model or "").strip().lower()
    if not m:
        return TASKS_AGENT_BACKEND
    if m.startswith("claude-") or m in _CLAUDE_ALIASES:
        return "claude"
    if m.startswith(_CODEX_PREFIXES):
        return "codex"
    log.warning(
        "unrecognised model %r — falling back to backend %r", model, TASKS_AGENT_BACKEND
    )
    return TASKS_AGENT_BACKEND
POLL_INTERVAL_S = float(os.environ.get("WORKER_POLL_INTERVAL", "2.0"))

# Concurrency caps.
#   Refines are read-only, so multiple sessions are safe — limit only to
#   avoid runaway parallel-token costs.
#   Executes write to the repo. We gate them by sub-repo so two tasks that
#   touch disjoint sub-repos can still run together; MAX_PARALLEL_EXECUTES
#   is just a sanity cap.
MAX_PARALLEL_REFINES = int(os.environ.get("WORKER_MAX_REFINES", "2"))
MAX_PARALLEL_EXECUTES = int(os.environ.get("WORKER_MAX_EXECUTES", "4"))

# Substrings that indicate the Claude SDK was rejected because the org hit
# its Anthropic usage limit. We surface this distinctly from generic
# failures so the UI can offer a clean Retry once the monthly cap resets,
# and so we don't trash refined_spec / pending_reply / session_id.
#
# Two tiers, because we match against two very different sources:
#   _LIMIT_PATTERNS_EXC  — a raised exception / SDK error string. The text
#     comes from the SDK, never from the model, so broad matching ("rate
#     limit") is safe.
#   _LIMIT_PATTERNS_TEXT — the agent's own assistant text (some SDK/model
#     paths surface the usage-limit notice as plain text instead of raising).
#     The model legitimately *writes about* rate limits when implementing API
#     clients, so this set must be strict: only unambiguous "you hit the cap"
#     phrasings. Notably it must NOT contain bare "rate limit" — that caused a
#     completed run whose summary mentioned a CoinGecko "rate limit" to be
#     mis-flagged as limit_reached (and its summary dropped).
_LIMIT_PATTERNS_EXC: tuple[str, ...] = (
    "monthly usage limit",
    "monthly spend limit",
    "org's monthly",
    "rate_limit_error",
    "usage_limit_exceeded",
    "rate limit",
)
# Machine-generated markers. These cannot occur in prose the model writes
# about someone else's API, so they're safe to match anywhere in the text.
# `cc_cli_limit_message` is the telemetry tag the Claude CLI appends to the
# billing URL in its own limit notice.
_LIMIT_MARKERS_TEXT: tuple[str, ...] = (
    "cc_cli_limit_message",
    "usage_limit_exceeded",
)
# Prose phrasings. The model legitimately *writes about* limits — this repo
# has tasks about the watchdog's own spend-limit handling — so these are
# matched only inside a short trailing message (see _limit_phrase_in_text).
# This set must NOT contain bare "rate limit" or bare "spend limit": the
# first already mis-flagged a finished run whose summary mentioned a
# CoinGecko rate limit, and the second would fire on any task about
# budget caps.
_LIMIT_PATTERNS_TEXT: tuple[str, ...] = (
    "monthly usage limit",
    "monthly spend limit",
    "org's monthly",
    "usage limit reached",
    "claude usage limit",
    "hit your monthly",
    "manage usage credits",
)
# The CLI's limit notice replaces the response and is ~150 chars. A real
# refined spec / execute summary is far longer, so length separates "the
# run was cut short by a cap" from "the agent wrote about caps".
_LIMIT_NOTICE_MAX_CHARS = 600

# The CLI uses this phrasing when *the chosen model* is the blocker rather
# than the account: the plan covers the cheaper models, but a top-tier one
# draws on extra usage credits and the monthly spend cap refuses it. The
# distinction matters because the two need opposite reactions — an
# account-wide cap resets on its own and a retry later works, whereas this
# one will keep failing instantly (1 turn, $0.00) until the user picks a
# different model or raises the cap. Verified 2026-09-11: haiku/sonnet/opus
# answered normally while claude-fable-5-1 returned this notice.
_MODEL_BLOCKED_MARKERS: tuple[str, ...] = (
    "switch to another model",
    "manage usage credits",
)


def _limit_reason(full: str, model: str | None) -> str:
    """Actionable error text for a usage/spend-limit stop.

    Storing the CLI's raw notice tells the user to wait for a reset, which is
    wrong guidance when only the selected model is blocked."""
    s = (full or "").lower()
    if any(m in s for m in _MODEL_BLOCKED_MARKERS):
        name = (model or "").strip() or "the selected model"
        return (
            f"Model {name} is blocked by this account's monthly spend limit for "
            "extra usage credits — the account itself is not out of quota. "
            "Pick a cheaper Claude model (Haiku/Sonnet/Opus are covered by the "
            "plan) or a Codex model for this task, or raise the cap at "
            "claude.ai/settings/usage. Retrying the same model will fail again "
            "immediately."
        )
    return "Anthropic usage/spend limit reached mid-session — partial work preserved"


def _is_limit_error(
    err: BaseException | str,
    patterns: tuple[str, ...] = _LIMIT_PATTERNS_EXC,
) -> bool:
    s = str(err).lower()
    return any(p in s for p in patterns)


def _limit_phrase_in_text(full: str, last_block: str = "") -> bool:
    """True when assistant text looks like the CLI's own usage/spend-limit
    notice rather than the agent writing about limits.

    Some SDK paths surface the notice as plain assistant text instead of
    raising, and `_extract_section` falls back to returning the whole text
    when the expected marker is missing — so without this the notice gets
    stored as the task's summary and the task flips to done. That is
    exactly how task #85 ended up "done" with a spend-limit message as its
    execute summary."""
    s = (full or "").lower()
    if any(m in s for m in _LIMIT_MARKERS_TEXT):
        return True
    # Prose match only in a short final message (or a short run overall).
    tail = (last_block or full or "").strip().lower()
    if len(tail) > _LIMIT_NOTICE_MAX_CHARS:
        return False
    return any(p in tail for p in _LIMIT_PATTERNS_TEXT)


# The project's sub-repos (team.yaml `repos`): validates the agent's declared
# `# Repos:` line so we don't write garbage into the task's repos field.
VALID_REPOS: frozenset[str] = frozenset(TEAM.get("repos") or [])
# team.yaml `isolate_repos: true`: the project root is a shared folder (all of
# the user's repos); task workspaces contain only the project's own repos.
ISOLATE_REPOS: frozenset[str] | None = VALID_REPOS if TEAM.get("isolate_repos") else None
# Roles that only start on the user's click (e.g. release-manager); they
# never run hands-off, even on autopilot full-bots.
# One stream-json line can carry a whole image (an agent reading a
# screenshot); the SDK's 1 MB default breaks the stream on such a line.
SDK_MAX_BUFFER_BYTES = 64 * 1024 * 1024
CONFIRM_ROLES: frozenset[str] = frozenset(str(r) for r in TEAM.get("confirm_roles") or [])
HANDS_OFF_ALLOW: tuple[str, ...] = tuple(str(a) for a in TEAM.get("hands_off_allow") or [])
# Task worktrees (workspace.py): None = off, stages run in the live checkout.
WS = workspace.layout(monitor_control.CONTROL_DIR)
_REPO_LIST = ", ".join(f"`{r}`" for r in sorted(VALID_REPOS)) or "(none configured)"
_REPO_EXAMPLE = ", ".join(sorted(VALID_REPOS)[:2]) or "repo-a, repo-b"

REFINE_SYSTEM = (
    f"You are a refinement agent for {PROJECT_LABEL}. "
    "A user has filed a task with a short title and optional details. "
    "Your job: read enough of the codebase to decide *what to do*, then "
    "write a short conclusion the implementation agent can execute. "
    "Do NOT make code changes. Use Read/Grep/Glob freely; avoid Bash "
    "unless you need to check runtime state.\n\n"
    "Output rules — the spec is the deliverable, keep it tight:\n"
    "- If the Details field below is marked `(empty)`, the user gave "
    "only a title. Before `# Refined spec`, write a `# Details:` "
    "section: 3-5 terse bullets or a short paragraph that captures "
    "what the change should achieve and any constraints you inferred "
    "from the codebase. ≤6 lines. The bridge auto-fills the task's "
    "Details field from this section so the user can review. If "
    "Details is NOT empty, do NOT write a `# Details:` section — "
    "leave the user's wording alone.\n"
    "- Lead the actual spec with `# Refined spec` followed by a "
    "one-sentence summary of the change.\n"
    "- The very next line MUST be:\n"
    f"    `# Repos: {_REPO_EXAMPLE}`\n"
    "  listing every sub-repo your plan will touch, using exact "
    f"directory names from {CWD}/ (one of: {_REPO_LIST}). The user's task may "
    "already have a subset of repos selected — re-list the actual "
    "ones you need; the bridge will reconcile if they differ.\n"
    "  Use `# Repos: -` if no specific sub-repo applies (rare).\n"
    "- Then a short bullet list: files to touch (with paths), the "
    "concrete change in each, and any non-obvious constraint.\n"
    "- No background, no rationale, no walking through the codebase, "
    "no recap of the user's wording. The next agent reads the same "
    "repo and sees the same task title — do not re-explain.\n"
    "- Aim for under ~40 lines in the spec section. Hard cap ~80.\n"
    "- If the task is genuinely ambiguous, write 1-3 specific "
    "clarifying questions under `## Open questions` instead of "
    "guessing — and keep the spec short.\n\n"
    "Everything you write before `# Details:` or `# Refined spec` is "
    "scratch and is discarded; only `# Details:` (if emitted) is "
    "saved to the details field, and only `# Refined spec` to end-of-"
    "message is saved as refined_spec."
)

REPOS_LINE_RE = re.compile(r"^#\s*Repos\s*:\s*(.+?)\s*$", re.MULTILINE)
DETAILS_BLOCK_RE = re.compile(
    r"^#\s*Details\s*:\s*\n?(.*?)(?=^#\s|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _extract_declared_details(full_text: str) -> str | None:
    """Pull the `# Details:` section out of the agent's full assistant
    text (scratch + spec). Returns the trimmed block, or None if missing.
    Called only when the task's details field was empty going in."""
    if not full_text:
        return None
    m = DETAILS_BLOCK_RE.search(full_text)
    if not m:
        return None
    block = m.group(1).strip()
    return block or None


def _extract_declared_repos(spec: str) -> list[str] | None:
    """Pull the `# Repos: ...` line out of a refined spec. Returns:
      * list of repos (possibly empty, if agent wrote `# Repos: -`)
      * None if the line is missing entirely (older spec or agent
        ignored the instruction) — caller should leave repos alone
    """
    if not spec:
        return None
    m = REPOS_LINE_RE.search(spec)
    if not m:
        return None
    raw = m.group(1).strip()
    if raw in ("-", "none", "(none)", ""):
        return []
    parts = [p.strip() for p in raw.split(",")]
    out: list[str] = []
    for p in parts:
        if not p:
            continue
        # Tolerate the short form ("intraday" → "demo-intraday")
        norm = p if p in VALID_REPOS else f"{REPO_PREFIX}{p}"
        if norm in VALID_REPOS and norm not in out:
            out.append(norm)
    return out

REFINE_SPEC_MARKER = "# Refined spec"

# ---- Worker core ----------------------------------------------------------

def _format_task_prompt(task: dict) -> str:
    repos = ", ".join(task.get("repos") or []) or "(none specified)"
    details = (task.get("details") or "").strip()
    parts = [
        f"## Task #{task['id']} — {task['title']}",
        f"Priority: {task['prio']}  Sub-repos: {repos}",
        "",
        "### Details",
        details if details
            else "(empty) — user submitted only a title. Emit a `# Details:` "
                 "section per the output rules so the bridge fills the field.",
    ]
    parts += [
        "",
        "Refine this into a spec. Read what you need from the repo, then "
        f"write `{REFINE_SPEC_MARKER}` followed by the plan.",
    ]
    return "\n".join(parts)


def _now_iso() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def _event(kind: str, **fields) -> dict:
    """Build a JSONL event with ts + event kind + extra fields."""
    out = {"ts": _now_iso(), "event": kind}
    out.update(fields)
    return out


def _serialise_tool_use(blk: ToolUseBlock) -> dict:
    inp = blk.input or {}
    return {"id": blk.id, "name": blk.name, "input": inp}


def _serialise_tool_result(blk: ToolResultBlock) -> dict:
    content = blk.content
    if isinstance(content, list):
        text = "\n".join(
            c.get("text", "") if isinstance(c, dict) else str(c) for c in content
        )
    else:
        text = str(content) if content is not None else ""
    return {
        "tool_use_id": getattr(blk, "tool_use_id", None),
        "is_error": bool(getattr(blk, "is_error", False)),
        "text": text[:8000],
    }


def _extract_section(full_text: str, marker: str) -> str | None:
    """Pull `marker` section (and everything after) out of the agent's
    final assistant text. Falls back to the whole text if marker is
    absent — the user can still review it."""
    if not full_text.strip():
        return None
    idx = full_text.find(marker)
    if idx >= 0:
        section = full_text[idx:].strip()
        return section or None
    return full_text.strip()


def _extract_spec(full_text: str) -> str | None:
    return _extract_section(full_text, REFINE_SPEC_MARKER)


def _system_prompt_from_options(options: ClaudeAgentOptions, kind: str) -> str:
    """The role/system text a run was configured with. The Claude SDK reads
    it from `options.system_prompt`; the Codex runner has to pass it over
    stdin itself, and must not re-derive it from `kind` — pipeline stages
    carry their own role prompt in the same field."""
    sp = getattr(options, "system_prompt", None)
    if isinstance(sp, dict):
        append = sp.get("append")
        if isinstance(append, str) and append.strip():
            return append
    elif isinstance(sp, str) and sp.strip():
        return sp
    return REFINE_SYSTEM


def _is_readonly_run(kind: str, options: ClaudeAgentOptions) -> bool:
    """refine and advise stages disallow the file-mutating tools; that is
    the signal for 'cheap read-only model' fallbacks and for the Codex
    runtime note (Codex cannot enforce the tool list)."""
    # Keyed on the file tools, not on "any deny list": full-bots implement
    # stages carry Bash(...) denies but still write files.
    disallowed = getattr(options, "disallowed_tools", None) or ()
    return kind == "refine" or "Write" in disallowed


async def _safe_append(task_id: int, kind: str, event: dict) -> None:
    try:
        await asyncio.to_thread(tasks_api.append_event, task_id, kind, event)
    except Exception:
        log.exception(
            "[task %s] failed to append %s event %s", task_id, kind, event.get("event")
        )


async def _run_agent_session(
    task: dict,
    *,
    kind: str,
    prompt: str,
    options: ClaudeAgentOptions,
    extract: "callable[[str], str | None]",
    complete_fn: "callable[[int, str], object]",
    fail_fn: "callable[[int, str], object]",
    no_output_msg: str,
    complete_label: str,
    complete_field: str,
    backend: str | None = None,
    on_session: "callable[[str, str], object] | None" = None,
) -> tuple[str | None, str]:
    """Drive one Claude session for a task, streaming events as <kind>.jsonl
    and posting the extracted final section to the monitoring backend on
    success (or marking failed). Returns (section, full_text); section is
    the post-marker block (or None on failure), full_text is the full
    assistant output including scratch — useful for parsing
    pre-marker directives like `# Details:`. Shared by refine_one,
    plan_one and stage_one.

    `on_session(session_id, backend)` is called once the agent minted its
    session id (pipeline stages persist it per stage so a needs_input
    answer can resume); refine keeps its task-level persistence."""
    # Backend comes from the caller, which resolved it from the task's own
    # model override (_backend_for). Falls back to the env default only when
    # the caller didn't resolve one.
    if (backend or TASKS_AGENT_BACKEND) == "codex":
        return await _run_codex_agent_session(
            task, kind=kind, prompt=prompt, options=options, extract=extract,
            complete_fn=complete_fn, fail_fn=fail_fn, no_output_msg=no_output_msg,
            complete_label=complete_label, complete_field=complete_field,
            on_session=on_session,
        )
    tid = task["id"]
    log.info("%s task #%s — %r", kind, tid, task["title"])
    await _safe_append(tid, kind, _event("start", task_id=tid, title=task["title"]))

    final_text_parts: list[str] = []
    # Terminal state reported by the SDK's ResultMessage. Far more reliable
    # than sniffing assistant text, which is why it is checked first below.
    result_meta: dict[str, object] = {}
    client = ClaudeSDKClient(options=options)
    try:
        await client.connect()
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, SystemMessage) and msg.subtype == "init":
                data = msg.data or {}
                model = data.get("model")
                sid = data.get("session_id")
                # Persist the Claude SDK session_id so the user can reply
                # to a refined task and we resume the same conversation.
                # Only meaningful for refine — execute is single-shot.
                if kind == "refine" and sid and task.get("session_id") != sid:
                    try:
                        await asyncio.to_thread(
                            tasks_api.set_session_id, tid, sid, "claude"
                        )
                        task["session_id"] = sid
                        task["session_backend"] = "claude"
                    except Exception:
                        log.exception("[task %s] failed to persist session_id", tid)
                elif on_session is not None and sid:
                    try:
                        await asyncio.to_thread(on_session, sid, "claude")
                    except Exception:
                        log.exception("[task %s] %s: failed to persist session_id", tid, kind)
                await _safe_append(tid, kind, _event("init", model=model, session_id=sid))
            elif isinstance(msg, AssistantMessage):
                for blk in msg.content:
                    if isinstance(blk, TextBlock):
                        text = blk.text.rstrip()
                        if text:
                            final_text_parts.append(text)
                            await _safe_append(tid, kind, _event("text", text=text))
                    elif isinstance(blk, ToolUseBlock):
                        await _safe_append(
                            tid, kind, _event("tool_use", **_serialise_tool_use(blk))
                        )
                    elif isinstance(blk, ThinkingBlock):
                        await _safe_append(tid, kind, _event("thinking"))
            elif isinstance(msg, UserMessage):
                for blk in getattr(msg, "content", []) or []:
                    if isinstance(blk, ToolResultBlock):
                        await _safe_append(
                            tid, kind, _event("tool_result", **_serialise_tool_result(blk))
                        )
            elif isinstance(msg, ResultMessage):
                budget.record_cost(getattr(msg, "total_cost_usd", None), f"{PROJECT_ID}:task-{tid}:{kind}")
                result_meta = {
                    "is_error": bool(getattr(msg, "is_error", False)),
                    "subtype": getattr(msg, "subtype", None),
                    "stop_reason": getattr(msg, "stop_reason", None),
                    "api_error_status": getattr(msg, "api_error_status", None),
                    "terminal_reason": getattr(msg, "terminal_reason", None),
                }
                usage = getattr(msg, "usage", None)
                await _safe_append(
                    tid,
                    kind,
                    _event(
                        "result",
                        duration_ms=getattr(msg, "duration_ms", None),
                        num_turns=getattr(msg, "num_turns", None),
                        cost_usd=getattr(msg, "total_cost_usd", None),
                        usage=dict(usage) if isinstance(usage, dict) else None,
                        backend="claude",
                        **result_meta,
                    ),
                )
    except Exception as e:
        if _is_limit_error(e):
            log.warning("[task %s] %s hit Anthropic usage limit: %s", tid, kind, e)
            await _safe_append(
                tid, kind,
                _event("limit_reached", message=str(e)),
            )
            try:
                await asyncio.to_thread(
                    tasks_api.mark_limit_reached,
                    tid,
                    _limit_reason(str(e), getattr(options, "model", None)),
                    kind,
                )
            except Exception:
                log.exception(
                    "[task %s] limit_reached callback failed, falling back to fail",
                    tid,
                )
                try:
                    await asyncio.to_thread(fail_fn, tid, str(e))
                except Exception:
                    log.exception(
                        "[task %s] failed to mark %s failed after limit fallback",
                        tid, kind,
                    )
            return None, ""

        message = claude_login.AUTH_ERROR if claude_login.is_auth_error(str(e)) else str(e)
        log.exception("[task %s] %s failed", tid, kind)
        await _safe_append(tid, kind, _event("error", message=message))
        try:
            await asyncio.to_thread(fail_fn, tid, message)
        except Exception:
            log.exception("[task %s] failed to mark %s failed", tid, kind)
        return None, ""
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    full = "\n\n".join(final_text_parts).strip()
    last_block = final_text_parts[-1] if final_text_parts else ""

    # A session can end badly in three ways, none of which may complete the
    # task. They're checked before extract() because _extract_section falls
    # back to returning the whole assistant text when the expected marker is
    # missing — so anything the run printed instead of a spec/summary would
    # otherwise be stored as if it were the finished artefact.
    #
    #   1. The SDK reported a terminal error on the ResultMessage. This is
    #      authoritative; prefer it over reading the prose.
    #   2. The usage/spend-limit notice arrived as plain assistant text
    #      instead of raising (see _limit_phrase_in_text).
    #   3. HTTP 429 from the API surfaced as api_error_status.
    result_is_error = bool(result_meta.get("is_error"))
    api_status = result_meta.get("api_error_status")
    subtype = str(result_meta.get("subtype") or "")
    limit_from_result = (
        api_status == 429
        or _is_limit_error(subtype)
        or _is_limit_error(str(result_meta.get("terminal_reason") or ""))
    )
    limit_from_text = _limit_phrase_in_text(full, last_block)

    if limit_from_result or limit_from_text:
        why = "result status" if limit_from_result else "assistant text"
        log.warning(
            "[task %s] %s — usage/spend limit detected via %s "
            "(subtype=%r api_status=%r); routing to limit_reached",
            tid, kind, why, subtype or None, api_status,
        )
        await _safe_append(
            tid, kind,
            _event(
                "limit_reached",
                message=f"usage/spend limit surfaced via {why} (mid-session)",
            ),
        )
        reason = _limit_reason(full, getattr(options, "model", None))
        try:
            await asyncio.to_thread(tasks_api.mark_limit_reached, tid, reason, kind)
        except Exception:
            log.exception("[task %s] limit_reached callback failed", tid)
            try:
                await asyncio.to_thread(fail_fn, tid, reason)
            except Exception:
                pass
        return None, full

    if result_is_error:
        # Errored run that isn't a usage cap (max turns, execution error).
        # Fail it rather than storing whatever text happened to be emitted.
        msg = (claude_login.AUTH_ERROR if api_status == 401 or claude_login.is_auth_error(full)
               else f"agent run ended in error (subtype={subtype or '?'})")
        log.warning("[task %s] %s — %s", tid, kind, msg)
        await _safe_append(tid, kind, _event("error", message=msg))
        try:
            await asyncio.to_thread(fail_fn, tid, msg)
        except Exception:
            log.exception("[task %s] failed to mark %s failed", tid, kind)
        return None, full


    section = extract(full)
    if not section:
        log.warning("[task %s] %s", tid, no_output_msg)
        await _safe_append(tid, kind, _event("error", message=no_output_msg))
        try:
            await asyncio.to_thread(fail_fn, tid, no_output_msg)
        except Exception:
            log.exception("[task %s] failed to mark %s failed", tid, kind)
        return None, full
    try:
        await asyncio.to_thread(complete_fn, tid, section)
        await _safe_append(tid, kind, _event(complete_label, **{complete_field: len(section)}))
        log.info("%sd task #%s (%d chars)", kind, tid, len(section))
        return section, full
    except Exception as e:
        log.exception("[task %s] %s complete POST failed", tid, kind)
        await _safe_append(tid, kind, _event("error", message=f"complete POST failed: {e}"))
        try:
            await asyncio.to_thread(fail_fn, tid, str(e))
        except Exception:
            pass
        return None, full


async def _run_codex_agent_session(
    task: dict,
    *,
    kind: str,
    prompt: str,
    options: ClaudeAgentOptions,
    extract: "callable[[str], str | None]",
    complete_fn: "callable[[int, str], object]",
    fail_fn: "callable[[int, str], object]",
    no_output_msg: str,
    complete_label: str,
    complete_field: str,
    on_session: "callable[[str, str], object] | None" = None,
) -> tuple[str | None, str]:
    """Codex CLI equivalent of the legacy Claude-SDK task runner."""
    tid = task["id"]
    model = (getattr(options, "model", None) or "").strip()
    readonly = _is_readonly_run(kind, options)
    # We only land here when _backend_for() routed this model to Codex, so a
    # Claude ID can normally no longer reach us. The guard stays for the one
    # case that still can: TASKS_AGENT_BACKEND=codex with an unclassifiable
    # WORKER_*_MODEL that happens to be a Claude ID. Sending that to Codex
    # would be a hard CLI error, so fall back to the Codex default.
    if _backend_for(model) == "claude":
        fallback = REFINE_MODEL if readonly else EXECUTE_MODEL
        log.warning(
            "[task %s] %s: Claude model %r routed to Codex — using %r instead",
            tid, kind, model, fallback or "<cli default>",
        )
        model = (fallback or "").strip()
        if _backend_for(model) == "claude":
            model = ""  # env default is Claude too — let the Codex CLI decide
    system = _system_prompt_from_options(options, kind)
    if readonly and kind != "refine":
        # Codex has no per-tool allow list, so the advise-stage read-only
        # guarantee is prompt-only here. Say it once more, explicitly.
        system += (
            "\n\n[Runtime note] File-mutating tools are disabled by policy for "
            "this run: do not create, edit or delete project files, do not "
            "commit, and use shell commands only for read-only inspection or "
            "delegated analysis."
        )
    # The bridge itself is an isolated Docker container. This host disallows
    # bwrap user namespaces, so do not nest Codex's sandbox inside it.
    cmd = ["codex", "exec", "-", "--json", "--skip-git-repo-check",
           "--dangerously-bypass-approvals-and-sandbox"]
    if model:
        cmd += ["--model", model]
    log.info("%s task #%s — %r (codex model=%s)", kind, tid, task["title"], model or "default")
    await _safe_append(tid, kind, _event("start", task_id=tid, title=task["title"]))
    t_start = datetime.now(_TZ)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=getattr(options, "cwd", None) or CWD, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **(getattr(options, "env", None) or {})},
            start_new_session=True,  # own process group: kill takes its tool shells too
        )
        # No wall-clock cap, same as Claude sessions: a stage may legitimately
        # wait on a long delegated backtest.
        stdout, stderr = await proc.communicate(f"{system}\n\n----\n\n{prompt}".encode())
        if proc.returncode:
            raise RuntimeError((stderr.decode(errors="replace") or stdout.decode(errors="replace"))[-1000:])
    except BaseException as exc:
        # Never leave an orphaned Codex editing files after the stage has
        # been marked failed (or the worker is shutting down).
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
        if not isinstance(exc, Exception):
            raise
        log.exception("[task %s] %s Codex failed", tid, kind)
        await _safe_append(tid, kind, _event("error", message=str(exc)))
        try:
            await asyncio.to_thread(fail_fn, tid, str(exc))
        except Exception:
            log.exception("[task %s] failed to mark %s failed", tid, kind)
        return None, ""

    text_parts: list[str] = []
    thread_id: str | None = None
    usage: dict = {}
    turns = 0
    for raw in stdout.decode(errors="replace").splitlines():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            await _safe_append(tid, kind, _event("init", model=model or None, session_id=thread_id))
        elif event.get("type") == "turn.completed":
            turns += 1
            pipeline.accumulate_usage(usage, event.get("usage"))
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            text = (item.get("text") or "").strip()
            if text:
                text_parts.append(text)
                await _safe_append(tid, kind, _event("text", text=text))
    # Codex is subscription-limited like Claude, so its runs are metered the
    # same way: tokens from the turn usage, $ when WORKER_CODEX_PRICES prices
    # the model (cost_usd stays null otherwise => "unpriced" in the UI).
    await _safe_append(
        tid, kind,
        _event(
            "result",
            duration_ms=int((datetime.now(_TZ) - t_start).total_seconds() * 1000),
            num_turns=turns or None,
            cost_usd=pipeline.codex_cost_usd(model, usage),
            usage=usage or None,
            backend="codex",
            model=model or None,
        ),
    )
    if kind == "refine" and thread_id:
        try:
            await asyncio.to_thread(
                tasks_api.set_session_id, tid, thread_id, "codex"
            )
            task["session_backend"] = "codex"
        except Exception:
            log.exception("[task %s] failed to persist Codex session id", tid)
    elif on_session is not None and thread_id:
        try:
            await asyncio.to_thread(on_session, thread_id, "codex")
        except Exception:
            log.exception("[task %s] %s: failed to persist Codex session id", tid, kind)
    full = "\n\n".join(text_parts).strip()

    # Same trap as the Claude path: extract() falls back to the whole text
    # when the marker is missing, so a plan-limit notice would be stored as
    # the finished spec/summary and flip the task to done.
    if _limit_phrase_in_text(full, text_parts[-1] if text_parts else ""):
        log.warning(
            "[task %s] %s — usage/spend limit detected in Codex output; "
            "routing to limit_reached", tid, kind,
        )
        await _safe_append(
            tid, kind,
            _event("limit_reached", message="usage/spend limit surfaced in Codex output"),
        )
        try:
            await asyncio.to_thread(
                tasks_api.mark_limit_reached, tid, _limit_reason(full, model), kind
            )
        except Exception:
            log.exception("[task %s] limit_reached callback failed", tid)
            try:
                await asyncio.to_thread(fail_fn, tid, "usage limit reached mid-session")
            except Exception:
                pass
        return None, full

    section = extract(full)
    if not section:
        await _safe_append(tid, kind, _event("error", message=no_output_msg))
        await asyncio.to_thread(fail_fn, tid, no_output_msg)
        return None, full
    try:
        await asyncio.to_thread(complete_fn, tid, section)
        await _safe_append(tid, kind, _event(complete_label, **{complete_field: len(section)}))
        log.info("%sd task #%s (%d chars, codex)", kind, tid, len(section))
        return section, full
    except Exception as exc:
        log.exception("[task %s] %s complete POST failed", tid, kind)
        await _safe_append(tid, kind, _event("error", message=f"complete POST failed: {exc}"))
        await asyncio.to_thread(fail_fn, tid, str(exc))
        return None, full

async def refine_one(task: dict) -> None:
    pending = (task.get("pending_reply") or "").strip()
    sid = (task.get("session_id") or "").strip()
    is_reply = bool(pending) and bool(sid)
    # Per-task model override (set from the Tasks UI). Empty string / None
    # means "use the env default". The chosen model also decides which
    # agent backend runs this turn.
    refine_model = (task.get("refine_model") or "").strip() or REFINE_MODEL
    backend = _backend_for(refine_model)

    # A session id belongs to the backend that minted it: a Codex thread
    # cannot be resumed by the Claude SDK and vice versa. If the user
    # switched the model across backends between the refine and the reply,
    # resume is impossible — carry the saved spec into a fresh turn instead.
    session_backend = (task.get("session_backend") or "").strip().lower()
    can_resume = is_reply and backend == "claude" and session_backend != "codex"

    if can_resume:
        # Continuation turn — resume the prior Claude session and just
        # send the user's reply as the next message. We tell the agent to
        # always re-emit the spec so refined_spec stays the canonical
        # snapshot even if the answer doesn't materially change the plan.
        prompt = (
            f"[User reply in this refine chat]\n\n{pending}\n\n"
            "Continue refining. After your reply, re-emit the full "
            f"`{REFINE_SPEC_MARKER}` section (including the `# Repos:` "
            "line) so the saved spec stays current. If your reply "
            "doesn't change the plan, re-emit the same spec unchanged."
        )
        opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            hooks=guard.sdk_hooks(),
            max_buffer_size=SDK_MAX_BUFFER_BYTES,
            cwd=CWD,
            model=refine_model,
            resume=sid,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": REFINE_SYSTEM + QUIET_NOTE,
            },
            disallowed_tools=["Write", "Edit", "NotebookEdit", "Bash"],
        )
        # Record the user's input in the JSONL so the SSE viewer can
        # render it inline with the agent's response.
        await _safe_append(
            task["id"], "refine",
            _event("user_reply", text=pending),
        )
    else:
        prompt = _format_task_prompt(task)
        opts = ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            hooks=guard.sdk_hooks(),
            max_buffer_size=SDK_MAX_BUFFER_BYTES,
            cwd=CWD,
            model=refine_model,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": REFINE_SYSTEM + QUIET_NOTE,
            },
            # Refine is read-only by convention — keep the agent from
            # mutating the repo even if its prompt drifts.
            disallowed_tools=["Write", "Edit", "NotebookEdit", "Bash"],
        )

    # Reply we can't resume — the Codex runner always starts a fresh task
    # turn, and a backend switch invalidates the stored id either way. Carry
    # the saved spec in the prompt so the conversation isn't lost.
    if is_reply and not can_resume:
        if session_backend and session_backend != backend:
            log.info(
                "[task %s] refine reply: session is %s but model %r runs on %s "
                "— starting a fresh turn",
                task["id"], session_backend, refine_model or "<default>", backend,
            )
        prompt = (
            f"{_format_task_prompt(task)}\n\n### Previously refined spec\n"
            f"{task.get('refined_spec') or '(none saved)'}\n\n"
            f"### User reply\n{pending}\n\nRe-emit the complete {REFINE_SPEC_MARKER} section."
        )
        await _safe_append(task["id"], "refine", _event("user_reply", text=pending))

    spec, full = await _run_agent_session(
        task,
        kind="refine",
        prompt=prompt,
        options=opts,
        extract=_extract_spec,
        complete_fn=tasks_api.refine_complete,
        fail_fn=tasks_api.refine_fail,
        no_output_msg="Agent produced no final text — nothing to save as refined spec.",
        complete_label="complete",
        complete_field="spec_chars",
        backend=backend,
    )
    if not spec:
        return

    tid = task["id"]

    # Auto-fill details ONLY on the first refine turn (not on reply
    # turns) and only if the user submitted with empty details. We use
    # the raw `full` text (pre-marker) so we can read the agent's
    # `# Details:` block; the spec proper is just the `# Refined spec`
    # section onward.
    if not is_reply and not (task.get("details") or "").strip():
        details = _extract_declared_details(full)
        if details:
            try:
                await asyncio.to_thread(tasks_api.update_task, tid, details=details)
                await _safe_append(
                    tid, "refine",
                    _event("details_updated", chars=len(details)),
                )
                log.info("[task %s] auto-filled details (%d chars)", tid, len(details))
            except Exception as e:
                log.exception("[task %s] failed to auto-fill details", tid)
                await _safe_append(
                    tid, "refine",
                    _event("error", message=f"details auto-fill failed: {e}"),
                )

    # Reconcile sub-repo selection with what the agent declared.
    declared = _extract_declared_repos(spec)
    if declared is None:
        # Agent didn't emit a `# Repos:` line — leave the user's
        # selection alone rather than guess.
        return
    current = list(task.get("repos") or [])
    if sorted(declared) == sorted(current):
        return
    try:
        await asyncio.to_thread(tasks_api.update_task, tid, repos=declared)
        await _safe_append(
            tid, "refine",
            _event("repos_updated", before=current, after=declared),
        )
        log.info("[task %s] auto-updated repos: %s -> %s", tid, current, declared)
    except Exception as e:
        log.exception("[task %s] failed to auto-update repos", tid)
        await _safe_append(
            tid, "refine",
            _event("error", message=f"repos auto-update failed: {e}"),
        )



async def plan_one(task: dict) -> None:
    tid = task["id"]
    try:
        role = pipeline.load_role("orchestrator")
        catalog = pipeline.load_catalog()
        stages = await asyncio.to_thread(tasks_api.list_stages, tid)
    except Exception as e:
        msg = f"cannot load orchestrator role/catalog: {e}"
        log.exception("[task %s] plan: %s", tid, msg)
        try:
            await asyncio.to_thread(tasks_api.plan_fail, tid, msg)
        except Exception:
            log.exception("[task %s] failed to mark plan failed", tid)
        return
    model = pipeline.orchestrator_model(role, REFINE_MODEL, task.get("plan_model"))
    backend = _backend_for(model)
    prompt = pipeline.build_plan_prompt(task, stages, catalog)
    opts = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        hooks=guard.sdk_hooks(),
        max_buffer_size=SDK_MAX_BUFFER_BYTES,
        cwd=str(WS.ws(tid)) if WS and WS.ws(tid).is_dir() else CWD,
        model=model,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": pipeline.role_system_prompt(role, None) + QUIET_NOTE,
        },
        # Planning is read-only by construction.
        disallowed_tools=list(pipeline.ADVISE_DISALLOWED_TOOLS),
    )
    await _run_agent_session(
        task,
        kind="plan",
        prompt=prompt,
        options=opts,
        extract=lambda full: pipeline.extract_last_section(full, pipeline.PLAN_MARKER),
        complete_fn=tasks_api.plan_complete,
        fail_fn=tasks_api.plan_fail,
        no_output_msg="Orchestrator produced no final text — nothing to save as plan.",
        complete_label="complete",
        complete_field="plan_chars",
        backend=backend,
    )


async def stage_one(task: dict, stage: dict) -> None:
    tid = task["id"]
    seq = int(stage["seq"])
    attempt = int(stage.get("attempt") or 1)
    kind = f"stage_{seq}"

    def _complete(t: int, section: str):
        # Gate numbers from the raw backtest files, computed here by code
        # (the backend only uses them for roles with metric_files).
        return tasks_api.stage_complete(t, seq, attempt, section, evidence.collect(Path(cwd), tid))

    def _fail(t: int, err: str):
        return tasks_api.stage_fail(t, seq, attempt, err)

    def _on_session(sid: str, backend_name: str):
        return tasks_api.set_stage_session(tid, seq, attempt, sid, backend_name)

    try:
        role = pipeline.load_role(stage["role"])
        common = pipeline.load_common_prompt()
        stages = await asyncio.to_thread(tasks_api.list_stages, tid)
    except Exception as e:
        msg = f"cannot load role {stage.get('role')!r}: {e}"
        log.exception("[task %s] %s: %s", tid, kind, msg)
        try:
            await asyncio.to_thread(_fail, tid, msg)
        except Exception:
            log.exception("[task %s] failed to mark %s failed", tid, kind)
        return

    plan_model = pipeline.orchestrator_model(
        pipeline.load_role("orchestrator"), REFINE_MODEL, task.get("plan_model"),
    )
    model = pipeline.stage_model(
        stage, role, REFINE_MODEL, EXECUTE_MODEL, plan_model=plan_model, backend_of=_backend_for,
    )
    if (stage.get("model") or "").strip() and model != stage["model"].strip():
        log.warning(
            "[task %s] stage %s: model %r is not on the plan's backend (%s) — using %r",
            tid, seq, stage["model"], _backend_for(plan_model), model,
        )
    backend = _backend_for(model)
    answer = (stage.get("pending_answer") or "").strip()
    sid = (stage.get("session_id") or "").strip()
    session_backend = (stage.get("session_backend") or "").strip().lower()
    # Resume the same agent session only on Claude with a Claude-minted id;
    # everything else (Codex, cross-backend switch) starts fresh with the
    # previous output + questions + answer in the prompt.
    can_resume = bool(answer) and bool(sid) and backend == "claude" and session_backend != "codex"
    if can_resume:
        prompt = pipeline.build_resume_prompt(stage, answer, fresh=False)
    else:
        prompt = pipeline.build_stage_prompt(task, stage, stages, role, CONFIRM_ROLES, HANDS_OFF_ALLOW)
        if answer:
            prompt += pipeline.build_resume_prompt(stage, answer, fresh=True)

    # Task worktree: implement/advise stages work in it; a confirm stage
    # (release) first brings the branch into the live checkout and deploys
    # from there.
    identity = git_identity.stage(PROJECT_ID, role.id)
    cwd, env, isolated, applied = CWD, identity, False, None
    if _uses_workspace(task, stages):
        try:
            if role.id in CONFIRM_ROLES and workspace.APPLY_MODE != "pr":
                applied = await asyncio.to_thread(_apply_branch, tid, identity)
            else:
                await asyncio.to_thread(workspace.ensure, WS, tid, task.get("repos") or [], ISOLATE_REPOS)
                cwd, env, isolated = str(WS.ws(tid)), workspace.stage_env(WS, tid, identity), True
        except (workspace.GitError, OSError) as e:
            msg = (f"Branch {workspace.branch(tid)} nicht übernommen: {e}" if role.id in CONFIRM_ROLES
                   else f"Arbeitsordner für den Task nicht angelegt: {e}")
            log.warning("[task %s] %s: %s", tid, kind, msg)
            await asyncio.to_thread(_fail, tid, msg[:4000])
            return
        if applied:
            await asyncio.to_thread(_notify_task, tid, "⇢ In den Live-Ordner übernommen",
                                    "\n".join(applied) + "\n\nDeployt wird jetzt aus diesem Stand.")
    if isolated:
        prompt += "\n\n" + workspace.prompt_section(WS, tid)
    elif applied is not None:
        prompt += "\n\n" + pipeline.RELEASE_APPLIED_NOTE.format(branch=workspace.branch(tid))

    disallowed = pipeline.stage_disallowed_tools(role.kind, task, role.id, CONFIRM_ROLES)
    opt_kwargs: dict = dict(
        permission_mode="bypassPermissions",
        hooks=guard.sdk_hooks(),
        max_buffer_size=SDK_MAX_BUFFER_BYTES,
        cwd=cwd,
        model=model,
        env=env,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": pipeline.role_system_prompt(role, common) + QUIET_NOTE,
        },
    )
    if disallowed:
        opt_kwargs["disallowed_tools"] = disallowed
    if can_resume:
        opt_kwargs["resume"] = sid
    opts = ClaudeAgentOptions(**opt_kwargs)

    log.info(
        "[task %s] %s %s %s model=%s backend=%s resume=%s",
        tid, kind, role.id, role.kind, model or "<cli default>", backend, can_resume,
    )
    if answer:
        await _safe_append(tid, kind, _event("user_reply", text=answer))
    await _run_agent_session(
        task,
        kind=kind,
        prompt=prompt,
        options=opts,
        extract=lambda full: pipeline.extract_last_section(full, pipeline.STAGE_RESULT_MARKER),
        complete_fn=_complete,
        fail_fn=_fail,
        no_output_msg="Agent produced no final text — nothing to save as stage result.",
        complete_label="complete",
        complete_field="result_chars",
        backend=backend,
        on_session=_on_session,
    )
    if isolated and role.kind == pipeline.KIND_IMPLEMENT:
        goal = (stage.get("goal") or "").strip()[:1500]
        try:
            repos = await asyncio.to_thread(
                workspace.commit, WS, tid, f"Task #{tid} Stufe {seq} · {role.label}\n\n{goal}".strip(), identity)
            await asyncio.to_thread(workspace.write_state, WS, tid)
            if repos:
                log.info("[task %s] %s committed on %s: %s", tid, kind, workspace.branch(tid), ", ".join(repos))
        except Exception:  # noqa: BLE001 — never lose the stage result over this
            log.exception("[task %s] %s: commit on the task branch failed", tid, kind)


def _uses_workspace(task: dict, stages: list[dict]) -> bool:
    """Tasks whose stages already ran in the live checkout before worktrees
    existed finish there; everything else gets its own worktree."""
    if WS is None:
        return False
    tid = task["id"]
    sp = WS.state_path(tid)
    if WS.ws(tid).exists() or (sp is not None and sp.exists()):
        return True
    return not any(s.get("status") in ("done", "failed", "skipped", "awaiting_input") for s in stages)


def _apply_branch(tid: int, identity: dict) -> list[str]:
    if not WS.ws(tid).exists():
        return []
    if workspace.APPLY_MODE == "pr":
        task = tasks_api.get_task(tid) or {}
        title = f"{task.get('title') or f'Task #{tid}'}"
        body = (f"{(task.get('details') or '').strip()[:4000]}\n\n"
                f"_Entwurf von agent-team (Task #{tid}), Branch `{workspace.branch(tid)}`._").strip()
        out = workspace.publish_pr(WS, tid, title, body, identity)
    else:
        out = workspace.apply(WS, tid, identity)
    workspace.write_state(WS, tid, {"applied": out, "applied_at": _now_iso()} if out else None)
    return out


def _notify_task(tid: int, title: str, text: str, buttons: list | None = None) -> None:
    body = {"title": title[:200], "text": text[:3500] or title, "source": "pipeline"}
    if buttons:
        body["buttons"] = buttons
    req = urllib.request.Request(f"{_api_url}/api/notify/task-{PROJECT_ID}-{tid}",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20).close()
    except Exception as e:  # noqa: BLE001
        log.warning("[task %s] chat notice failed: %s", tid, e)


_ws_sweep_last = 0.0


def _workspace_tick() -> None:
    """"Übernehmen" taps (control/worktrees/<id>.apply from the web app) and
    the sweep that removes workspaces of finished / closed / deleted tasks."""
    global _ws_sweep_last
    if WS is None or WS.control is None:
        return
    reqs = WS.control / "worktrees"
    for f in sorted(reqs.glob("*.apply")) if reqs.is_dir() else []:
        try:
            tid = int(f.stem)
        except ValueError:
            f.unlink(missing_ok=True)
            continue
        if tid in _in_flight_stage_tasks:
            continue  # a stage is still writing; next poll
        f.unlink(missing_ok=True)
        try:
            out = _apply_branch(tid, git_identity.stage(PROJECT_ID, "lead"))
            if out and workspace.APPLY_MODE == "pr":
                _notify_task(tid, "⇢ Draft-PR bereit", "\n".join(out) + "\n\nGepusht, PR als Entwurf. "
                             "Review und Merge machst du auf GitHub; danach räumt der Worker den Arbeitsordner auf.",
                             [[{"label": "Abschließen – Chat archivieren", "data": f"archive:{tid}"}]])
            elif out:
                _notify_task(tid, "⇢ Übernommen", "\n".join(out) + "\n\nLokal gemergt — noch nicht deployt, kein Push.",
                             [[{"label": "Deployen lassen (falls nötig)", "data": f"deploy:{tid}"}],
                              [{"label": "Abschließen – Chat archivieren", "data": f"archive:{tid}"}]])
            else:
                _notify_task(tid, "Nichts zu übernehmen",
                             f"Branch {workspace.branch(tid)} hat nichts, was nicht schon im Live-Ordner ist.",
                             [[{"label": "Abschließen – Chat archivieren", "data": f"archive:{tid}"}]])
        except (workspace.GitError, OSError) as e:
            log.warning("[task %s] apply failed: %s", tid, e)
            _notify_task(tid, "✗ Übernehmen fehlgeschlagen",
                         f"{e}\n\nDer Live-Ordner ist unverändert (außer den oben als übernommen genannten Repos). "
                         "Der Lead kann prüfen, woran es liegt (lokale Änderungen im Live-Ordner, Konflikt) und "
                         "was zu tun ist. Ist das Hindernis weg: erneut übernehmen.",
                         [[{"label": "Lead soll das klären", "data": f"askfix:{tid}"}],
                          [{"label": "Erneut übernehmen", "data": f"apply:{tid}"}]])
    if time.monotonic() - _ws_sweep_last < 600:
        return
    _ws_sweep_last = time.monotonic()
    for tid in workspace.task_ids(WS):
        if tid in _in_flight_stage_tasks or tid in _in_flight_plans:
            continue
        try:
            task = tasks_api.get_task(tid)
            if task is None or task.get("status") == "closed":
                workspace.cleanup(WS, tid, drop_branch=True)
                log.info("[task %s] workspace removed (task %s)", tid, "deleted" if task is None else "closed")
            elif task.get("status") == "done" and workspace.APPLY_MODE == "pr":
                prs = workspace.pr_states(WS, tid)
                if prs and all(v in ("MERGED", "CLOSED") for v in prs.values()):
                    workspace.write_state(WS, tid, {"prs": prs})
                    workspace.cleanup(WS, tid, drop_branch=True)
                    log.info("[task %s] workspace removed (done, PRs %s)", tid, prs)
            elif task.get("status") == "done" and not workspace.state(WS, tid)["pending"]:
                workspace.write_state(WS, tid)
                workspace.cleanup(WS, tid)
                log.info("[task %s] workspace removed (done, merged)", tid)
        except Exception as e:  # noqa: BLE001
            log.warning("[task %s] workspace sweep: %s", tid, e)


# ---- Concurrency state (module-global, accessed only from poll_loop) ------

# Set of task IDs currently being refined / executed, to keep us from
# double-claiming a row before the API's status flip propagates back.
_in_flight_refines: set[int] = set()
# Each entry is a tuple (task_id, frozenset_of_repos | None). None means
# "unknown scope" — task had no sub-repos selected. A None entry holds an
# implicit global lock against any other execute starting.
_execute_slots: list[tuple[int, frozenset[str] | None]] = []
# Pipeline runs. Orchestrator runs and advise stages are read-only and share
# the refine cap; implement stages take an execute slot (per-sub-repo lock)
# for the duration of that stage only — a pipeline can sit idle for hours
# between stages, so holding the repo for the whole pipeline is not an option.
_in_flight_plans: set[int] = set()
_in_flight_advise: set[tuple[int, int]] = set()   # (task_id, seq)
_in_flight_stage_tasks: set[int] = set()


def _readonly_slots_used() -> int:
    return len(_in_flight_refines) + len(_in_flight_plans) + len(_in_flight_advise)


# ---- Periodic CLI refresh -------------------------------------------------
# The entrypoint upgrades both agent CLIs at container start, but a bridge
# that stays up for weeks keeps the CLI it booted with (it sat on 2.1.270
# for ten days while Opus 5.5 needed 2.1.280+). So the worker re-runs the
# same cli-upgrade.sh every WORKER_CLI_REFRESH_H hours — only while nothing
# is in flight, because a session that is mid-run keeps the binary it
# loaded anyway and we'd rather not race an install against a spawn.
# 0 disables. Each `claude`/`codex` spawn resolves the binary afresh, so an
# upgrade reaches the next task without a restart.
CLI_REFRESH_H = float(os.environ.get("WORKER_CLI_REFRESH_H", "24"))
CLI_UPGRADE_SCRIPT = os.environ.get("WORKER_CLI_UPGRADE_SCRIPT", "/app/cli-upgrade.sh")
_cli_refresh_last = 0.0
_cli_refresh_running = False


def _worker_idle() -> bool:
    return (
        _readonly_slots_used() == 0
        and not _execute_slots
        and not _in_flight_stage_tasks
    )


async def _maybe_refresh_clis(now: float) -> None:
    """Fire-and-forget CLI upgrade when due and idle (see above)."""
    global _cli_refresh_last, _cli_refresh_running
    if CLI_REFRESH_H <= 0 or _cli_refresh_running:
        return
    if now - _cli_refresh_last < CLI_REFRESH_H * 3600:
        return
    if not _worker_idle() or not os.path.exists(CLI_UPGRADE_SCRIPT):
        return
    _cli_refresh_running = True
    _cli_refresh_last = now
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", CLI_UPGRADE_SCRIPT,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=900)
        except asyncio.TimeoutError:
            proc.kill()
            log.warning("cli refresh timed out after 15 min — killed")
            return
        for line in (err or b"").decode("utf-8", "replace").splitlines():
            if line.strip():
                log.info("cli refresh: %s", line.strip())
        if proc.returncode != 0:
            log.warning("cli refresh exited %s (keeping installed CLIs)", proc.returncode)
    except Exception as exc:  # noqa: BLE001 — never let this take the loop down
        log.warning("cli refresh failed: %r", exc)
    finally:
        _cli_refresh_running = False


def _can_acquire_execute(task: dict, isolated: bool = False) -> bool:
    """Per-sub-repo gate. Returns True iff `task` can start executing
    without colliding with anything already in flight. A stage in its own
    worktree collides with nothing — only the cap applies."""
    if len(_execute_slots) >= MAX_PARALLEL_EXECUTES:
        return False
    if isolated:
        return True
    new_repos: frozenset[str] = frozenset(task.get("repos") or [])
    if not new_repos:
        # New task has unknown scope — it can only run alone.
        return not _execute_slots
    for _tid, claimed in _execute_slots:
        if claimed is None:
            # An in-flight unknown-scope execute holds everything.
            return False
        if new_repos & claimed:
            return False
    return True


async def _run_refine_slot(task: dict) -> None:
    tid = task["id"]
    _in_flight_refines.add(tid)
    try:
        await refine_one(task)
    except Exception:
        log.exception("[task %s] refine background failed", tid)
    finally:
        _in_flight_refines.discard(tid)


async def _run_plan_slot(task: dict) -> None:
    tid = task["id"]
    _in_flight_plans.add(tid)
    try:
        await plan_one(task)
    except Exception:
        log.exception("[task %s] plan background failed", tid)
    finally:
        _in_flight_plans.discard(tid)


async def _run_stage_slot(task: dict, stage: dict, isolated: bool = False) -> None:
    tid = task["id"]
    seq = int(stage["seq"])
    implement = stage.get("kind") == pipeline.KIND_IMPLEMENT
    repos: frozenset[str] | None = frozenset() if isolated else (frozenset(task.get("repos") or []) or None)
    slot = (tid, repos)
    _in_flight_stage_tasks.add(tid)
    if implement:
        _execute_slots.append(slot)
    else:
        _in_flight_advise.add((tid, seq))
    try:
        await stage_one(task, stage)
    except Exception:
        log.exception("[task %s] stage %s background failed", tid, seq)
    finally:
        _in_flight_stage_tasks.discard(tid)
        if implement:
            try:
                _execute_slots.remove(slot)
            except ValueError:
                pass
        else:
            _in_flight_advise.discard((tid, seq))


async def poll_loop() -> None:
    """Main loop. Spawns refines, orchestrator runs and pipeline stages
    (implement stages gated per sub-repo) as background tasks."""
    log.info(
        "worker starting (cwd=%s, refine_model=%s [%s], execute_model=%s [%s], "
        "default_backend=%s, poll=%.1fs, max_refines=%d, max_executes=%d, base=%s, "
        "roles_dir=%s)",
        CWD,
        REFINE_MODEL or "<cli default>",
        _backend_for(REFINE_MODEL),
        EXECUTE_MODEL or "<cli default>",
        _backend_for(EXECUTE_MODEL),
        TASKS_AGENT_BACKEND,
        POLL_INTERVAL_S,
        MAX_PARALLEL_REFINES, MAX_PARALLEL_EXECUTES,
        os.environ.get("TASKS_API_URL", "(TASKS_API_URL unset)"),
        pipeline.roles_dir(),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    bg: set[asyncio.Task] = set()
    api_was_up = True
    pipeline_api_ok = True
    # The entrypoint just upgraded the CLIs; the first periodic refresh is
    # due CLI_REFRESH_H from now, not immediately.
    global _cli_refresh_last
    _cli_refresh_last = loop.time()
    monitor_control.write_env()
    while not stop.is_set():
        # Reap finished background tasks.
        for t in [t for t in bg if t.done()]:
            bg.discard(t)
            exc = t.exception()
            if exc is not None:
                log.error("background task crashed: %r", exc)

        await _maybe_refresh_clis(loop.time())
        monitor_control.tick()
        if api_was_up:
            try:
                await asyncio.to_thread(_workspace_tick)
            except Exception:  # noqa: BLE001
                log.exception("workspace tick failed")
        if TEAM.get("scouts") and api_was_up:
            scouts.tick(TEAM, TEAM_DIR, monitor_control.CONTROL_DIR, PROJECT_LABEL, CWD,
                        os.environ.get("TASKS_API_URL", ""), bg)

        try:
            refining = await asyncio.to_thread(tasks_api.list_tasks, "refining")
            if not api_was_up:
                log.info("tasks API reachable again")
                api_was_up = True
        except _API_ERRORS as e:
            if api_was_up:
                log.warning("tasks API unreachable: %s — will retry every %.1fs", e, POLL_INTERVAL_S)
                api_was_up = False
            refining = []

        # Pipeline statuses in their own try: a monitoring backend that
        # predates pipeline mode answers 400 for them, and that must not
        # take simple-mode refine down with it.
        planning: list[dict] = []
        running: list[dict] = []
        if api_was_up:
            try:
                planning = await asyncio.to_thread(tasks_api.list_tasks, "planning")
                running = await asyncio.to_thread(tasks_api.list_tasks, "running")
                if not pipeline_api_ok:
                    log.info("pipeline statuses reachable again")
                    pipeline_api_ok = True
            except _API_ERRORS as e:
                if pipeline_api_ok:
                    log.warning(
                        "pipeline statuses unavailable (%s) — backend without pipeline "
                        "mode? simple mode continues", e,
                    )
                    pipeline_api_ok = False

        # --- Pipeline: orchestrator runs (read-only, share the refine cap)
        plan_cands = [t for t in planning if t["id"] not in _in_flight_plans]
        plan_cands.sort(key=lambda t: (t["prio"], t["updated_at"]))
        for task in plan_cands:
            if _readonly_slots_used() >= MAX_PARALLEL_REFINES:
                break
            bg.add(asyncio.create_task(_run_plan_slot(task)))

        # --- Pipeline: the one running stage per task
        stage_cands = [t for t in running if t["id"] not in _in_flight_stage_tasks]
        stage_cands.sort(key=lambda t: (t["prio"], t["updated_at"]))
        for task in stage_cands:
            try:
                stages = await asyncio.to_thread(tasks_api.list_stages, task["id"])
            except _API_ERRORS as e:
                log.warning("[task %s] cannot list stages: %s", task["id"], e)
                continue
            stage = next((s for s in stages if s.get("status") == "running"), None)
            if stage is None:
                continue  # backend flipped it meanwhile; next poll re-checks
            isolated = stage.get("role") not in CONFIRM_ROLES and _uses_workspace(task, stages)
            if stage.get("kind") == pipeline.KIND_IMPLEMENT:
                if not _can_acquire_execute(task, isolated):
                    continue  # repo held by another execute/implement stage
            elif _readonly_slots_used() >= MAX_PARALLEL_REFINES:
                continue
            bg.add(asyncio.create_task(_run_stage_slot(task, stage, isolated)))

        # --- Refines: global cap, no per-repo gating (read-only).
        # A task is pickup-eligible if either:
        #   * refined_spec is empty (a fresh refine just kicked off), OR
        #   * pending_reply is set (user queued a chat continuation —
        #     refined_spec stays populated as the running snapshot)
        refine_cands = [
            t for t in refining
            if t["id"] not in _in_flight_refines
            and (
                not (t.get("refined_spec") or "").strip()
                or (t.get("pending_reply") or "").strip()
            )
        ]
        refine_cands.sort(key=lambda t: (t["prio"], t["updated_at"]))
        for task in refine_cands:
            if _readonly_slots_used() >= MAX_PARALLEL_REFINES:
                break
            bt = asyncio.create_task(_run_refine_slot(task))
            bg.add(bt)

        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_S)
        except asyncio.TimeoutError:
            pass

    log.info("worker stopping (%d background tasks in flight)", len(bg))
    if bg:
        # Give in-flight work a chance to finish so we don't leave half-
        # written specs / summaries behind. The Claude sessions will get
        # SIGTERM via the SDK's disconnect, so this usually returns
        # quickly.
        try:
            await asyncio.wait_for(asyncio.gather(*bg, return_exceptions=True), timeout=30)
        except asyncio.TimeoutError:
            log.warning("shutdown: gave up waiting for %d background tasks", len(bg))


# Exclusive flock so a second worker can't poll the same tasks DB. The kernel
# releases the lock automatically when this process exits (clean or crashed),
# so there is no stale-lock cleanup. Kept on /tmp so the host's tmpfs scrubs
# anything weird across reboots.
_LOCK_PATH = "/tmp/agent-team-worker.lock"
_lock_fh = None  # module-global so the FD stays open for the process lifetime


def _acquire_singleton_lock() -> None:
    """Acquire an exclusive flock on _LOCK_PATH or exit. Without this, two
    worker processes pollute the same tasks DB and both run Claude sessions
    for the same task — doubling tokens and racing the refine_complete /
    execute_complete callbacks (see git log for the 2026-05-28 incident)."""
    global _lock_fh
    _lock_fh = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            holder = Path(_LOCK_PATH).read_text().strip() or "<unknown>"
        except Exception:
            holder = "<unknown>"
        log.error(
            "another worker already holds %s (pid=%s) — exiting. "
            "If that PID is dead, remove the lock file and restart.",
            _LOCK_PATH, holder,
        )
        sys.exit(2)
    _lock_fh.truncate(0)
    _lock_fh.write(f"{os.getpid()}\n")
    _lock_fh.flush()


def main() -> None:
    _acquire_singleton_lock()
    asyncio.run(poll_loop())


if __name__ == "__main__":
    main()

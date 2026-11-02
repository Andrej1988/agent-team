"""Pipeline-mode helpers for worker.py — role prompts, dossier and prompt
assembly, model/tool resolution.

Deliberately free of claude_agent_sdk imports so it can be unit-tested in
the devcontainer (tests/test_pipeline.py); worker.py is the only importer.

Role prompts come from the project's team definition
(``WORKER_TEAM_DIR/roles``, bind-mounted read-only: prompt edits take effect
on the next stage without a rebuild).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

CWD = os.environ.get("WORKER_CWD", "/workspace")
# The project's role prompts (its team definition, mounted read-only); there
# is no image copy any more — the team dir is the single source.
_IMAGE_ROLES_DIR = Path(os.environ.get("WORKER_TEAM_DIR", "/team")) / "roles"
_WORKSPACE_ROLES_DIR = Path(os.environ.get("WORKER_ROLES_DIR") or _IMAGE_ROLES_DIR)

STAGE_RESULT_MARKER = "# Stage result"
PLAN_MARKER = "# Plan"

KIND_ADVISE = "advise"
KIND_IMPLEMENT = "implement"
# Advise stages must not mutate the repo. Bash stays available (quant /
# auditor need psql + docker exec for numbers); the prompt restricts it to
# read-only use. On the Claude SDK this list is enforced; Codex ignores it,
# which is why the catalog defaults every advise role to a Claude model.
ADVISE_DISALLOWED_TOOLS: tuple[str, ...] = ("Write", "Edit", "NotebookEdit")

# Autopilot level that also chains implement stages on live-bot repos
# (monitoring tasks_db._autopilot_allows). The price: every implement stage
# of such a task is hands-off — no commits, nothing that touches a running
# container. Enforced by the prompt section below on both backends; the
# Bash patterns are a best-effort second fence on the Claude SDK only
# (prefix match, so `git -C repo commit` slips through — the prompt is the
# contract). `docker rm` is denied too: test containers use `run --rm`.
AUTOPILOT_FULL_BOTS = "full-bots"
HANDS_OFF_DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash(git commit:*)",
    "Bash(git push:*)",
    "Bash(git reset:*)",
    "Bash(git checkout:*)",
    "Bash(git stash:*)",
    "Bash(docker compose up:*)",
    "Bash(docker compose down:*)",
    "Bash(docker compose stop:*)",
    "Bash(docker compose restart:*)",
    "Bash(docker compose rm:*)",
    "Bash(docker restart:*)",
    "Bash(docker stop:*)",
    "Bash(docker kill:*)",
    "Bash(docker rm:*)",
    "Bash(docker update:*)",
)
_HANDS_OFF_RULES = """### Autopilot `full-bots` — hands-off rules (binding, override your role text)
This stage may have been started without a human click on a repo that runs
real bots. Edit files in the working tree only:
- No `git commit`, `push`, `reset`, `checkout`, `stash` or any other git
  mutation. The worker commits your changes on the task branch (or, for a
  task without its own worktree, the user reviews and commits the diff).
- Do not touch existing containers: no `docker compose up/down/stop/restart`,
  no `docker restart/stop/kill/rm/update`, no `docker exec` that changes
  state (only read-only commands and test runs inside them).
- New throwaway containers for tests are fine: `docker run --rm` with a
  unique `--name task-<id>-...`, no `--restart`, no broker keys / `.env`
  of a live or paper bot, and never a name of an existing service.
- No deploy, not even paper. List every deploy/recreate step the user must
  run in `next_hint`."""


_RELEASE_COMMIT_RULES = """### You are the release stage — commit the task's work
The user approved this stage (that click is the permission). Earlier stages
on live-bot repos did not commit, so their changes are still uncommitted.
After the deploy works, commit them in every repo this task touched:
- `git status` per repo; stage only files this task changed (`git add <paths>`,
  `git add -p` for mixed files) — other uncommitted work in the repo belongs
  to someone else and stays as it is. Never commit `.env` or other secrets.
- Message: `Task #{id}: {title}` plus a short body (what changed, how it was
  verified). Your git identity is already set; do not change git config.
- Do not push. List the commits (repo + hash) in your stage summary."""


# Appended to a release (confirm) stage of a task that worked in its own
# worktree: the worker merged the branch before the stage started.
RELEASE_APPLIED_NOTE = """### The task's branch is already in the live checkout (binding)
The worker brought branch `{branch}` into the live checkout (rebase +
fast-forward) right before your stage — that is what you deploy. The task's
work is committed already: ignore the commit instructions above for it and
commit only fixes you make during the deploy (same rules: only your files,
no secrets, no push)."""


def is_hands_off(task: dict, kind: str, role_id: Optional[str] = None,
                 confirm_roles: frozenset[str] = frozenset()) -> bool:
    """Confirm roles (team.yaml `confirm_roles`, e.g. release-manager) never
    run hands-off: autopilot never chains them, they only start after the
    user approved the stage — that click is the permission the rules stand
    in for."""
    if role_id is not None and role_id in confirm_roles:
        return False
    level = (task.get("autopilot") or "").strip().lower()
    return kind == KIND_IMPLEMENT and level == AUTOPILOT_FULL_BOTS

# How much of the history goes into a stage's dossier verbatim. Summaries and
# findings of every stage are always included; full outputs only for the
# most recent completed ones (they can be long).
FULL_OUTPUT_LAST_N = 2
MAX_FULL_OUTPUT_CHARS = 12000

_FRONT_MATTER_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.S)


@dataclass(frozen=True)
class Role:
    id: str
    label: str
    kind: str
    default_model: Optional[str]
    body: str

    @property
    def is_stage(self) -> bool:
        # The lead (Tasks v2) picks tasks; it is never a pipeline stage.
        return self.id not in ("orchestrator", "lead")


def roles_dir() -> Path:
    """Workspace copy first (hot-editable), image copy as fallback."""
    if (_WORKSPACE_ROLES_DIR / "common-prompt.md").exists():
        return _WORKSPACE_ROLES_DIR
    return _IMAGE_ROLES_DIR


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """``key: value`` lines between the leading ``---`` fences. No YAML lib
    in the bridge image, and none is needed for four flat keys."""
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip().lower()] = v.strip()
    return meta, text[m.end():]


def load_role(role_id: str) -> Role:
    rid = (role_id or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9-]+", rid) or rid == "common-prompt":
        raise ValueError(f"invalid role id {role_id!r}")
    path = roles_dir() / f"{rid}.md"
    if not path.exists():
        raise FileNotFoundError(f"no role prompt for {rid!r} at {path}")
    meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
    kind = (meta.get("kind") or KIND_ADVISE).lower()
    if kind not in (KIND_ADVISE, KIND_IMPLEMENT):
        raise ValueError(f"role {rid!r}: unknown kind {kind!r}")
    return Role(
        id=meta.get("id") or rid,
        label=meta.get("label") or rid,
        kind=kind,
        default_model=(meta.get("default_model") or None),
        body=body.strip(),
    )


def load_common_prompt() -> str:
    return (roles_dir() / "common-prompt.md").read_text(encoding="utf-8").strip()


def load_catalog() -> list[Role]:
    out: list[Role] = []
    for path in sorted(roles_dir().glob("*.md")):
        if path.stem == "common-prompt":
            continue
        try:
            out.append(load_role(path.stem))
        except (ValueError, FileNotFoundError):
            continue
    return out


def role_system_prompt(role: Role, common: Optional[str]) -> str:
    """System-prompt suffix for a role run. Stages get the shared rules +
    output contract first; the orchestrator's file is self-contained (its
    output block is `# Plan`, not `# Stage result`)."""
    if role.id == "orchestrator" or not common:
        return role.body
    return f"{common}\n\n{role.body}"


def stage_disallowed_tools(kind: str, task: Optional[dict] = None, role_id: Optional[str] = None,
                           confirm_roles: frozenset[str] = frozenset()) -> list[str]:
    if kind == KIND_ADVISE:
        return list(ADVISE_DISALLOWED_TOOLS)
    if task is not None and is_hands_off(task, kind, role_id, confirm_roles):
        return list(HANDS_OFF_DISALLOWED_TOOLS)
    return []


def stage_model(
    stage: dict, role: Role, refine_default: Optional[str], execute_default: Optional[str],
    plan_model: Optional[str] = None, backend_of: Optional[Callable[[str], str]] = None,
) -> Optional[str]:
    """Per-stage override -> role default -> worker env default for the kind
    (advise = WORKER_REFINE_MODEL, implement = WORKER_EXECUTE_MODEL).

    With ``plan_model`` + ``backend_of`` the pipeline is pinned to the plan
    model's backend: the first candidate on that backend wins, and when none
    is (e.g. Claude plan, but WORKER_EXECUTE_MODEL is a gpt-* id) the stage
    runs on the plan model itself. Picking Claude for the plan must never
    bill a stage to Codex, or vice versa."""
    override = (stage.get("model") or "").strip()
    kind_default = refine_default if role.kind == KIND_ADVISE else execute_default
    candidates = [c for c in (override, role.default_model, kind_default) if c]
    if not (plan_model and backend_of):
        return candidates[0] if candidates else None
    want = backend_of(plan_model)
    for c in candidates:
        if backend_of(c) == want:
            return c
    return plan_model


def orchestrator_model(
    role: Role, refine_default: Optional[str], task_override: Optional[str] = None,
) -> Optional[str]:
    """Task's plan_model (Tasks UI) -> WORKER_ORCHESTRATOR_MODEL -> role
    default -> worker refine default."""
    override = (task_override or "").strip()
    if override:
        return override
    env = (os.environ.get("WORKER_ORCHESTRATOR_MODEL") or "").strip()
    return env or role.default_model or refine_default


def extract_last_section(full_text: str, marker: str) -> Optional[str]:
    """Section from the LAST `marker` to end-of-message. Role prompts quote
    the template, so the first occurrence is often an echo. Falls back to
    the whole text when the marker is absent — the backend then treats it
    as unparseable (needs_input / plan failed), which is the safe outcome."""
    if not full_text or not full_text.strip():
        return None
    idx = full_text.rfind(marker)
    if idx >= 0:
        section = full_text[idx:].strip()
        return section or None
    return full_text.strip()


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

def _fmt_findings(findings: Iterable[dict]) -> list[str]:
    out = []
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        flag = "resolved" if f.get("resolved") else "open"
        out.append(f"    - [{f.get('id', '?')} {f.get('severity', 'med')} {flag}] {f.get('text', '')}")
    return out


def _task_header(task: dict) -> list[str]:
    repos = ", ".join(task.get("repos") or []) or "(none specified)"
    plan = task.get("plan") or {}
    lines = [
        f"## Task #{task['id']} — {task['title']}",
        f"Priority: {task.get('prio')}  Sub-repos: {repos}",
        "",
        "### Details",
        (task.get("details") or "").strip() or "(empty)",
    ]
    spec = (task.get("refined_spec") or "").strip()
    if spec:
        lines += ["", "### Refined spec (from an earlier refine run)", spec]
    crit = plan.get("exit_criteria") or []
    lines += ["", "### Exit criteria"]
    lines += [f"- {c}" for c in crit] or ["- (none set yet)"]
    lines += [
        "",
        "### Limits",
        f"rounds used: {task.get('round_count', 0)}/{plan.get('max_rounds', '?')}  "
        f"max stages: {plan.get('max_stages', '?')}  "
        f"budget: {task.get('budget_usd') if task.get('budget_usd') is not None else 'none'} USD  "
        f"autopilot: {task.get('autopilot', 'off')}",
    ]
    if task.get("stop_reason"):
        lines += [f"last stop reason: {task['stop_reason']}"]
    return lines


def format_dossier(task: dict, stages: list[dict], *, include_pending: bool = True) -> str:
    """Everything a stage or the orchestrator needs to know about the task
    and what happened before: header, per-stage verdict/summary/findings,
    full text of the last completed stages, and the pending tail."""
    lines = _task_header(task)
    history = [s for s in stages if s.get("status") != "pending"]
    pending = [s for s in stages if s.get("status") == "pending"]
    lines += ["", "### Stage history"]
    if not history:
        lines.append("(no stages have run yet)")
    for s in history:
        head = f"{s['seq']}. {s['role']} — {s['status']}"
        if s.get("verdict"):
            head += f"/{s['verdict']}"
        head += f" (attempt {s.get('attempt', 0)})"
        lines.append(head)
        if s.get("goal"):
            lines.append(f"   goal: {s['goal']}")
        if s.get("summary"):
            lines.append(f"   summary: {s['summary']}")
        if s.get("error"):
            lines.append(f"   error: {s['error']}")
        if s.get("questions"):
            lines.append("   questions: " + " | ".join(str(q) for q in s["questions"]))
        lines += _fmt_findings(s.get("findings") or [])
    recent = [s for s in history if s.get("output")][-FULL_OUTPUT_LAST_N:]
    if recent:
        lines += ["", f"### Full output of the last {len(recent)} completed stage(s)"]
        for s in recent:
            out = (s.get("output") or "").strip()
            if len(out) > MAX_FULL_OUTPUT_CHARS:
                out = "…(truncated)…\n" + out[-MAX_FULL_OUTPUT_CHARS:]
            lines += ["", f"#### Stage {s['seq']} {s['role']} ({s.get('verdict') or s['status']})", out]
    if include_pending:
        lines += ["", "### Pending stages (planned, not run yet)"]
        if not pending:
            lines.append("(none)")
        for s in pending:
            model = f"  model={s['model']}" if s.get("model") else ""
            lines.append(f"{s['seq']}. {s['role']} ({s['kind']}){model} — {s.get('goal') or '(no goal)'}")
    return "\n".join(lines)


def build_plan_prompt(task: dict, stages: list[dict], catalog: list[Role]) -> str:
    history = [s for s in stages if s.get("status") != "pending"]
    mode = "GATE" if history else "PLAN"
    lines = [format_dossier(task, stages, include_pending=True), ""]
    lines += [f"## Orchestrator — {mode} mode"]
    if mode == "PLAN":
        lines += [
            "No stage has run. Size the work, then emit the initial plan: the "
            "ordered stages with a concrete goal each, measurable exit "
            "criteria, and limits.",
        ]
    else:
        lines += [
            "A gate failed or the plan is exhausted. Read the findings of the "
            "latest stage(s) above and the pending tail. Emit a REPLACEMENT "
            "for the pending tail (remediation first, then the re-check, then "
            "whatever of the old tail still applies). Emit `\"stages\": []` "
            "with a `conclusion` only if nothing is left to do or the task "
            "cannot succeed within the limits.",
        ]
    lines += ["", "### Roles available (`role` values)"]
    for r in catalog:
        if r.is_stage:
            lines.append(f"- `{r.id}` ({r.kind}) — {r.label}")
    lines += ["", f"End your turn with `{PLAN_MARKER}` followed by the JSON block."]
    return "\n".join(lines)


def build_stage_prompt(task: dict, stage: dict, stages: list[dict], role: Role,
                       confirm_roles: frozenset[str] = frozenset(),
                       hands_off_allow: tuple[str, ...] = ()) -> str:
    total = len(stages)
    lines = [format_dossier(task, stages, include_pending=True), ""]
    lines += [
        f"## Your stage: {stage['seq']}/{total} — {role.label} ({role.kind}, attempt {stage.get('attempt', 1)})",
        "",
        "### Goal",
        (stage.get("goal") or "").strip() or "(no goal given — apply your role to the task as described)",
        "",
    ]
    if is_hands_off(task, role.kind, role.id, confirm_roles):
        lines += [_HANDS_OFF_RULES]
        if hands_off_allow:
            # team.yaml hands_off_allow: services that are fair game anyway
            # (e.g. a shared test DB no bot uses).
            lines += ["Exceptions (team.yaml, binding as well):"] + [f"- {a}" for a in hands_off_allow]
        lines += [""]
    if role.id in confirm_roles:
        lines += [_RELEASE_COMMIT_RULES.format(id=task["id"], title=task.get("title") or ""), ""]
    lines += [
        "Do the work for this stage now. Then end your turn with "
        f"`{STAGE_RESULT_MARKER}` followed by the JSON block from the output contract.",
    ]
    return "\n".join(lines)


def build_resume_prompt(stage: dict, answer: str, *, fresh: bool) -> str:
    """Continuation after the user answered a needs_input. `fresh=False`
    resumes the same agent session (Claude only): the answer is enough.
    `fresh=True` starts a new session (Codex, or the session was minted by
    the other backend): repeat what the previous run produced and asked."""
    answer = (answer or "").strip()
    if not fresh:
        return (
            "[User answer to your questions]\n\n"
            f"{answer}\n\n"
            "Continue this stage with the answer above. When done, end your "
            f"turn with a complete, updated `{STAGE_RESULT_MARKER}` block."
        )
    qs = "\n".join(f"- {q}" for q in (stage.get("questions") or [])) or "- (see previous output)"
    prev = (stage.get("output") or "").strip()
    if len(prev) > MAX_FULL_OUTPUT_CHARS:
        prev = "…(truncated)…\n" + prev[-MAX_FULL_OUTPUT_CHARS:]
    return (
        "\n\n### Your previous run of this stage ended with questions\n"
        f"{prev or '(no output recorded)'}\n\n"
        "### Questions you asked\n"
        f"{qs}\n\n"
        "### User answer\n"
        f"{answer}\n\n"
        "Continue the stage with this answer (do not ask the same questions "
        f"again). End your turn with a complete `{STAGE_RESULT_MARKER}` block."
    )


# ---------------------------------------------------------------------------
# Codex usage -> tokens / $ (Claude reports cost_usd itself)
# ---------------------------------------------------------------------------
# `codex exec --json` emits one `turn.completed` per turn with
# usage={input_tokens, cached_input_tokens, cache_write_input_tokens,
# output_tokens, reasoning_output_tokens}. Both plans are subscription-
# limited, so Codex runs are metered like Claude ones: tokens always, a $
# figure when WORKER_CODEX_PRICES maps the model to per-1M-token prices:
#   WORKER_CODEX_PRICES='{"gpt-5.6-terra": {"input": 2.0, "cached_input": 0.5, "output": 8.0}}'
_USAGE_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
               "output_tokens", "reasoning_output_tokens")


def accumulate_usage(total: dict, usage: Optional[dict]) -> dict:
    if isinstance(usage, dict):
        for k in _USAGE_KEYS:
            try:
                total[k] = int(total.get(k) or 0) + int(usage.get(k) or 0)
            except (TypeError, ValueError):
                pass
    return total


def usage_tokens(usage: Optional[dict]) -> int:
    if not isinstance(usage, dict):
        return 0
    try:
        return int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    except (TypeError, ValueError):
        return 0


def codex_price_table() -> dict:
    raw = (os.environ.get("WORKER_CODEX_PRICES") or "").strip()
    if not raw:
        return {}
    try:
        table = json.loads(raw)
    except ValueError:
        return {}
    return table if isinstance(table, dict) else {}


def codex_cost_usd(model: Optional[str], usage: Optional[dict], table: Optional[dict] = None) -> Optional[float]:
    """USD estimate from per-1M prices, or None when the model is not priced.
    Cached input is billed at `cached_input` (falls back to `input`)."""
    if not isinstance(usage, dict):
        return None
    table = codex_price_table() if table is None else table
    prices = table.get((model or "").strip().lower()) or table.get("default")
    if not isinstance(prices, dict):
        return None
    try:
        p_in = float(prices.get("input") or 0.0)
        p_cached = float(prices.get("cached_input", prices.get("input") or 0.0) or 0.0)
        p_out = float(prices.get("output") or 0.0)
        cached = int(usage.get("cached_input_tokens") or 0)
        uncached = max(int(usage.get("input_tokens") or 0) - cached, 0)
        out = int(usage.get("output_tokens") or 0)
    except (TypeError, ValueError):
        return None
    return round((uncached * p_in + cached * p_cached + out * p_out) / 1_000_000, 6)


def describe_stage(stage: dict) -> str:
    return f"stage {stage.get('seq')} {stage.get('role')} (attempt {stage.get('attempt')})"


def to_json(obj) -> str:  # small helper for logging
    return json.dumps(obj, ensure_ascii=False, default=str)

"""Parsers for the machine-readable tail of a pipeline agent's output.

Every stage ends its turn with ``# Stage result`` followed by a fenced JSON
block; the orchestrator ends with ``# Plan`` + JSON. Both are parsed here,
on the backend, so the state machine in ``tasks_db`` can be unit-tested
without the worker and so the worker stays a thin "run agent, post text"
loop.

Fallback rule (same failure class as the spend-limit notice that once
completed a task): anything that does not parse must **never** advance the
pipeline. An unparseable stage result becomes ``needs_input`` with a
generated question; an unparseable plan fails the planning run.

The *last* marker and the *last* JSON fence after it win: role prompts quote
the output template, and models routinely echo it in their scratch text
before writing the real one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

STAGE_RESULT_MARKER = "# Stage result"
PLAN_MARKER = "# Plan"

VERDICTS = ("pass", "fail", "needs_input", "blocked")

UNPARSEABLE_QUESTION = (
    "The stage did not end with a parseable `# Stage result` JSON block. "
    "Review its log, then answer with guidance (the stage resumes with your "
    "answer and must re-emit the result block) — or skip the stage."
)

_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*[ \t]*\r?\n(.*?)```", re.S)


@dataclass
class StageResult:
    verdict: str
    summary: str = ""
    findings: list[dict] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    # needs_input: ready-made answers the user can tap (first = recommendation).
    options: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    # Numbers a metric gate checks (team.yaml metric_gates), e.g.
    # {"oos_sharpe": 1.3, "max_drawdown_pct": 18.0}. Non-numeric values dropped.
    metrics: dict = field(default_factory=dict)
    next_hint: str = ""
    # False when the fallback fired (no marker / no JSON / bad verdict).
    parsed: bool = True


def _last_json_object(text: str, marker: str) -> Optional[dict]:
    """The last fenced JSON object after the last occurrence of ``marker``.
    Falls back to the outermost ``{...}`` in that tail when the model forgot
    the fence."""
    if not text:
        return None
    idx = text.rfind(marker)
    if idx < 0:
        return None
    tail = text[idx + len(marker):]
    for block in reversed(_FENCE_RE.findall(tail)):
        obj = _loads(block)
        if isinstance(obj, dict):
            return obj
    start = tail.find("{")
    end = tail.rfind("}")
    if 0 <= start < end:
        obj = _loads(tail[start:end + 1])
        if isinstance(obj, dict):
            return obj
    return None


# Models writing JSON sometimes shell-escape apostrophes (`'\''` or `\'`),
# which is invalid JSON and once parked a finished reviewer stage as
# needs_input (task #92 stage 7). Only an *unescaped* backslash before `'`
# is rewritten, so a valid `\\'` (literal backslash + quote) is untouched.
_SHELL_QUOTE_RE = re.compile(r"'\\''")
_BAD_APOS_ESCAPE_RE = re.compile(r"(?<!\\)((?:\\\\)*)\\'")


def _loads(s: str) -> Any:
    s = s.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    repaired = _BAD_APOS_ESCAPE_RE.sub(r"\1'", _SHELL_QUOTE_RE.sub("'", s))
    if repaired == s:
        return None
    try:
        return json.loads(repaired)
    except Exception:
        return None


def _str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, (list, tuple)):
        out: list[str] = []
        for item in v:
            if isinstance(item, str):
                if item.strip():
                    out.append(item.strip())
            elif isinstance(item, dict):
                # tolerate {"text": ...} / {"question": ...}
                t = item.get("text") or item.get("question") or item.get("path")
                if isinstance(t, str) and t.strip():
                    out.append(t.strip())
        return out
    return [str(v)]


def _normalise_findings(v: Any, role_prefix: str = "F") -> list[dict]:
    """Findings need stable ids so a gate can notice the same unresolved
    issue being raised twice. Strings get synthetic ids; dicts keep theirs."""
    if not isinstance(v, (list, tuple)):
        return []
    out: list[dict] = []
    for i, item in enumerate(v, start=1):
        if isinstance(item, str):
            if item.strip():
                out.append({"id": f"{role_prefix}-{i}", "severity": "med",
                            "text": item.strip(), "resolved": False})
            continue
        if not isinstance(item, dict):
            continue
        fid = str(item.get("id") or f"{role_prefix}-{i}").strip()
        text = item.get("text") or item.get("finding") or item.get("summary") or ""
        sev = str(item.get("severity") or "med").strip().lower()
        if sev not in ("high", "med", "low"):
            sev = "med"
        out.append({
            "id": fid,
            "severity": sev,
            "text": str(text).strip(),
            "resolved": bool(item.get("resolved", False)),
        })
    return out


def parse_stage_result(text: str, role_prefix: str = "F") -> StageResult:
    """Parse the ``# Stage result`` block. Never raises; the fallback is a
    ``needs_input`` verdict carrying ``UNPARSEABLE_QUESTION``."""
    obj = _last_json_object(text or "", STAGE_RESULT_MARKER)
    if obj is None:
        return StageResult(
            verdict="needs_input", summary="(no parseable stage result)",
            questions=[UNPARSEABLE_QUESTION], parsed=False,
        )
    verdict = str(obj.get("verdict") or "").strip().lower()
    if verdict not in VERDICTS:
        return StageResult(
            verdict="needs_input",
            summary=str(obj.get("summary") or "(unknown verdict)").strip(),
            findings=_normalise_findings(obj.get("findings"), role_prefix),
            questions=[
                f"Stage reported an unknown verdict {verdict!r} (expected one of "
                f"{', '.join(VERDICTS)}). " + UNPARSEABLE_QUESTION
            ],
            artifacts=_str_list(obj.get("artifacts")),
            parsed=False,
        )
    questions = _str_list(obj.get("questions"))
    if verdict == "needs_input" and not questions:
        questions = ["The stage asked for input but listed no questions — review its log and answer."]
    return StageResult(
        verdict=verdict,
        summary=str(obj.get("summary") or "").strip(),
        findings=_normalise_findings(obj.get("findings"), role_prefix),
        questions=questions,
        options=_str_list(obj.get("options"))[:4] if verdict == "needs_input" else [],
        artifacts=_str_list(obj.get("artifacts")),
        next_hint=str(obj.get("next_hint") or "").strip(),
        parsed=True,
        metrics=_metrics(obj.get("metrics")),
    )


def _metrics(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        if isinstance(v, bool):
            continue
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# Orchestrator plan
# ---------------------------------------------------------------------------

def _int_or_none(v: Any) -> Optional[int]:
    try:
        if v is None or v == "":
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def _float_or_none(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_plan(text: str) -> Optional[dict]:
    """Parse the ``# Plan`` block. Returns ``None`` when there is nothing
    usable (caller fails the planning run). Roles are *not* validated here —
    ``tasks_db._normalise_stages`` does that against the catalog."""
    obj = _last_json_object(text or "", PLAN_MARKER)
    if obj is None:
        return None
    raw_stages = obj.get("stages")
    stages: list[dict] = []
    if isinstance(raw_stages, (list, tuple)):
        for item in raw_stages:
            if isinstance(item, str):
                stages.append({"role": item.strip().lower(), "goal": "", "model": None})
            elif isinstance(item, dict):
                role = str(item.get("role") or "").strip().lower()
                if not role:
                    continue
                model = item.get("model")
                stages.append({
                    "role": role,
                    "goal": str(item.get("goal") or "").strip(),
                    "model": (str(model).strip() or None) if isinstance(model, str) else None,
                })
    conclusion = obj.get("conclusion")
    conclusion = str(conclusion).strip().lower() if isinstance(conclusion, str) else None
    if conclusion not in ("done", "blocked"):
        conclusion = None
    # Repos the plan touches (like refine's `# Repos:` line). None = key
    # absent (leave the task's selection alone); [] = explicitly none.
    raw_repos = obj.get("repos")
    repos: Optional[list[str]]
    if raw_repos is None:
        repos = None
    elif isinstance(raw_repos, str):
        repos = [r.strip() for r in raw_repos.split(",") if r.strip() and r.strip() != "-"]
    else:
        repos = _str_list(raw_repos)
    # Optional proposal for a brand-new sub-repo when nothing existing fits.
    new_repo: Optional[dict] = None
    nr = obj.get("new_repo")
    if isinstance(nr, str) and nr.strip():
        new_repo = {"name": nr.strip().lower(), "based_on": "", "reason": ""}
    elif isinstance(nr, dict) and str(nr.get("name") or "").strip():
        new_repo = {
            "name": str(nr.get("name")).strip().lower(),
            "based_on": str(nr.get("based_on") or "").strip().lower(),
            "reason": str(nr.get("reason") or "").strip(),
        }
    return {
        "stages": stages,
        "repos": repos,
        "new_repo": new_repo,
        "exit_criteria": _str_list(obj.get("exit_criteria")),
        "max_rounds": _int_or_none(obj.get("max_rounds")),
        "max_stages": _int_or_none(obj.get("max_stages")),
        "budget_usd": _float_or_none(obj.get("budget_usd")),
        "rationale": str(obj.get("rationale") or obj.get("reason") or "").strip(),
        "conclusion": conclusion,
    }

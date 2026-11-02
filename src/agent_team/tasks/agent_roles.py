"""Role catalog for pipeline-mode tasks — per project.

A pipeline task is an ordered list of *stages*; every stage is one run of a
role agent. The orchestrator plans those stages but is not a stage itself.

Source of truth is the project's team definition (mounted read-only):

    <team_dir>/team.yaml      repos, live repos, gate roles, stage rules,
                              role order + UI descriptions
    <team_dir>/roles/<id>.md  the role prompt; its ``key: value`` front-matter
                              carries id / label / kind / default_model

The prompts the worker runs and the catalog the
UI shows now come from the same files.

The module keeps the old attribute API (``agent_roles.KNOWN_REPOS``,
``agent_roles.get_role(...)`` …) through a module ``__getattr__`` that
resolves against the current project's catalog, so the ported state machine
in ``tasks.db`` reads like the original.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .. import projects

# Stage kinds. ``advise`` stages never get Write/Edit/NotebookEdit (enforced
# by the worker on the Claude SDK). ``implement`` stages get the full tool set
# and go through the worker's per-repo execute lock.
KIND_ADVISE = "advise"
KIND_IMPLEMENT = "implement"
STAGE_KINDS = frozenset({KIND_ADVISE, KIND_IMPLEMENT})


@dataclass(frozen=True)
class AgentRole:
    id: str
    label: str
    description: str
    kind: str
    # None = worker env default for the kind.
    default_model: Optional[str]
    is_stage: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class StageRule:
    """A stage that must exist in a plan: after ``after`` (follow-up) or at
    the end (final). ``goal`` is used when the rule inserts it."""
    role: str
    goal: str


@dataclass(frozen=True)
class VariantRule:
    count_role: str                 # whose stages count as variants (backtest-engineer)
    step: dict[str, float]          # metric -> bar shift per extra variant
    max_variants: int               # a failed gate at this count stops the task


@dataclass(frozen=True)
class Catalog:
    ROLES: tuple[AgentRole, ...]
    GATE_ROLES: frozenset[str]
    KNOWN_REPOS: frozenset[str]
    LIVE_BOT_REPOS: frozenset[str]
    NEW_REPO_RE: re.Pattern
    # role id -> rule: every stage of that role is followed by rule.role
    # (e.g. backtest-engineer -> backtest-auditor).
    FOLLOW_UP: dict[str, StageRule] = field(default_factory=dict)
    # The plan always ends with this role (e.g. reviewer) — or with one
    # of FINAL_MAY_FOLLOW after it (e.g. a release-manager deploys
    # after the review).
    FINAL: Optional[StageRule] = None
    FINAL_MAY_FOLLOW: frozenset[str] = frozenset()
    # Roles whose stages never start on autopilot: the user approves each one
    # from the inbox (e.g. release-manager = deploy).
    CONFIRM_ROLES: frozenset[str] = frozenset()
    # role -> {metric: (op, threshold)}: a `pass` of that role only counts if
    # its result's `metrics` meet every threshold (e.g. backtest-auditor).
    METRIC_GATES: dict[str, dict[str, tuple[str, float]]] = field(default_factory=dict)
    # Roles whose gate numbers must also come from code: the worker computes
    # them from the raw files the backtest saved (worker/evidence.py); the
    # worse of file and agent value counts, no files = fail.
    METRIC_FILES: frozenset[str] = frozenset()
    # role -> VariantRule: multiple testing. Every stage of rule.count_role in
    # the task is one variant; each one after the first raises the bars.
    VARIANT_PENALTY: dict[str, "VariantRule"] = field(default_factory=dict)
    # role -> days: the stage starts no earlier than that long after the
    # previous stage finished (e.g. paper-reviewer after a paper deploy).
    WAIT_ROLES: dict[str, float] = field(default_factory=dict)
    REPO_PREFIX: str = ""

    @property
    def ROLE_BY_ID(self) -> dict[str, AgentRole]:
        return {r.id: r for r in self.ROLES}

    @property
    def STAGE_ROLE_IDS(self) -> tuple[str, ...]:
        return tuple(r.id for r in self.ROLES if r.is_stage)

    def normalise_repo(self, name: str) -> Optional[str]:
        """'intraday' / 'demo-intraday' -> 'demo-intraday' (repo_prefix 'demo-'); None if unknown."""
        n = (name or "").strip().lower()
        if not n:
            return None
        if n in self.KNOWN_REPOS:
            return n
        if self.REPO_PREFIX and f"{self.REPO_PREFIX}{n}" in self.KNOWN_REPOS:
            return f"{self.REPO_PREFIX}{n}"
        return None

    def catalog(self) -> list[dict]:
        return [r.as_dict() for r in self.ROLES]

    def get_role(self, role_id: str) -> Optional[AgentRole]:
        return self.ROLE_BY_ID.get((role_id or "").strip().lower())


_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def front_matter(path: Path) -> dict[str, str]:
    """``key: value`` lines between the leading ``---`` fences."""
    m = _FM_RE.match(path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    if not m:
        return out
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def load_catalog(team_dir: Path) -> Catalog:
    team: dict[str, Any] = yaml.safe_load((team_dir / "team.yaml").read_text(encoding="utf-8")) or {}
    roles: list[AgentRole] = []
    for spec in team.get("roles") or []:
        rid = str(spec["id"])
        fm = front_matter(team_dir / "roles" / f"{rid}.md")
        if fm.get("id") != rid:
            raise ValueError(f"roles/{rid}.md front-matter id is {fm.get('id')!r}")
        kind = fm.get("kind", "")
        if kind not in STAGE_KINDS:
            raise ValueError(f"roles/{rid}.md: invalid kind {kind!r}")
        model = fm.get("default_model") or None
        roles.append(AgentRole(
            id=rid,
            label=fm.get("label") or rid,
            description=str(spec.get("description") or ""),
            kind=kind,
            default_model=None if model in (None, "", "none", "null") else model,
            is_stage=bool(spec.get("is_stage", True)),
        ))
    ids = {r.id for r in roles}

    def _rule(raw: Any) -> StageRule:
        rule = StageRule(role=str(raw["role"]), goal=" ".join(str(raw.get("goal") or "").split()))
        if rule.role not in ids:
            raise ValueError(f"team.yaml: stage rule names unknown role {rule.role!r}")
        return rule

    follow = {str(k): _rule(v) for k, v in (team.get("follow_up") or {}).items()}
    final = _rule(team["final"]) if team.get("final") else None
    may_follow = frozenset(str(r) for r in ((team.get("final") or {}).get("may_follow") or []))
    confirm = frozenset(str(r) for r in team.get("confirm_roles") or [])
    metric_gates: dict[str, dict[str, tuple[str, float]]] = {}
    for role_id, spec in (team.get("metric_gates") or {}).items():
        if str(role_id) not in ids:
            raise ValueError(f"team.yaml: metric_gates for unknown role {role_id!r}")
        metric_gates[str(role_id)] = {str(m): _parse_threshold(str(cond)) for m, cond in (spec or {}).items()}
    metric_files = frozenset(str(r) for r in team.get("metric_files") or [])
    if not metric_files <= set(metric_gates):
        raise ValueError(f"team.yaml: metric_files without metric_gates {sorted(metric_files - set(metric_gates))}")
    penalty: dict[str, VariantRule] = {}
    for role_id, spec in (team.get("variant_penalty") or {}).items():
        spec = spec or {}
        rule = VariantRule(count_role=str(spec.get("count") or ""),
                           step={str(m): float(v) for m, v in (spec.get("step") or {}).items()},
                           max_variants=int(spec.get("max_variants") or 0))
        if str(role_id) not in metric_gates or rule.count_role not in ids:
            raise ValueError(f"team.yaml: variant_penalty {role_id!r} needs a metric gate and a known count role")
        if not set(rule.step) <= set(metric_gates[str(role_id)]):
            raise ValueError(f"team.yaml: variant_penalty {role_id!r} steps metrics without a threshold")
        penalty[str(role_id)] = rule
    waits = {str(r): float((v or {}).get("days") if isinstance(v, dict) else v)
             for r, v in (team.get("wait_roles") or {}).items()}
    if not set(waits) <= ids:
        raise ValueError(f"team.yaml: unknown wait_roles {sorted(set(waits) - ids)}")
    if not (may_follow | confirm) <= ids:
        raise ValueError(f"team.yaml: unknown roles in may_follow/confirm_roles {sorted((may_follow | confirm) - ids)}")
    gates = frozenset(str(g) for g in team.get("gate_roles") or [])
    if not gates <= ids:
        raise ValueError(f"team.yaml: unknown gate roles {sorted(gates - ids)}")
    return Catalog(
        ROLES=tuple(roles),
        GATE_ROLES=gates,
        KNOWN_REPOS=frozenset(team.get("repos") or []),
        LIVE_BOT_REPOS=frozenset(team.get("live_repos") or []),
        NEW_REPO_RE=re.compile(team.get("new_repo_pattern") or r"(?!)"),
        FOLLOW_UP=follow,
        FINAL=final,
        FINAL_MAY_FOLLOW=may_follow,
        CONFIRM_ROLES=confirm,
        METRIC_GATES=metric_gates,
        METRIC_FILES=metric_files,
        VARIANT_PENALTY=penalty,
        WAIT_ROLES=waits,
        REPO_PREFIX=str(team.get("repo_prefix") or ""),
    )


_THRESHOLD_RE = re.compile(r"^\s*(>=|<=|>|<|==)\s*(-?\d+(?:\.\d+)?)\s*$")


def _parse_threshold(cond: str) -> tuple[str, float]:
    m = _THRESHOLD_RE.match(cond)
    if not m:
        raise ValueError(f"team.yaml: bad metric threshold {cond!r} (use e.g. '>= 1.0')")
    return m.group(1), float(m.group(2))


def check_metrics(metrics: dict, gate: dict[str, tuple[str, float]]) -> list[str]:
    """Failed conditions as text ([] = all met); a missing metric fails."""
    ops = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
           "<": lambda a, b: a < b, "==": lambda a, b: a == b}
    out = []
    for name, (op, thr) in gate.items():
        v = metrics.get(name)
        if v is None:
            out.append(f"{name}: fehlt (verlangt {op} {thr:g})")
        elif not ops[op](float(v), thr):
            out.append(f"{name} = {float(v):g} (verlangt {op} {thr:g})")
    return out


def raised_gate(gate: dict[str, tuple[str, float]], rule: Optional[VariantRule],
                variants: int) -> dict[str, tuple[str, float]]:
    """The thresholds for the n-th variant: each one after the first moves
    the stepped bars by `step` in the strict direction."""
    if not rule or variants <= 1:
        return dict(gate)
    out = dict(gate)
    for name, step in rule.step.items():
        op, thr = gate[name]
        shift = step * (variants - 1)
        out[name] = (op, thr + shift if op in (">=", ">") else thr - shift if op in ("<=", "<") else thr)
    return out


def worse_of(agent: dict, code: dict, gate: dict[str, tuple[str, float]]) -> dict:
    """Merge agent-reported and code-computed metrics; for gated metrics
    the value that is worse against its threshold wins."""
    out = {**agent, **code}
    for name, (op, _) in gate.items():
        vals = [float(d[name]) for d in (agent, code) if d.get(name) is not None]
        if len(vals) == 2:
            out[name] = min(vals) if op in (">=", ">") else max(vals) if op in ("<=", "<") else float(code[name])
    return out


# Reload when team.yaml or a role file changes (they are edited in the
# project repo; no restart needed).
_cache: dict[Path, tuple[float, Catalog]] = {}


def _stamp(team_dir: Path) -> float:
    files = [team_dir / "team.yaml", *sorted((team_dir / "roles").glob("*.md"))]
    return max((f.stat().st_mtime for f in files if f.exists()), default=0.0)


def for_project(project: projects.Project) -> Catalog:
    stamp = _stamp(project.team_dir)
    hit = _cache.get(project.team_dir)
    if hit and hit[0] == stamp:
        return hit[1]
    cat = load_catalog(project.team_dir)
    _cache[project.team_dir] = (stamp, cat)
    return cat


def current() -> Catalog:
    return for_project(projects.current())


def __getattr__(name: str) -> Any:
    # agent_roles.KNOWN_REPOS, agent_roles.get_role(...), … -> current project.
    if name.startswith("__"):
        raise AttributeError(name)
    return getattr(current(), name)

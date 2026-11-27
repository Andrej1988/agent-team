"""The task's test gates (``suites.py`` runs, this decides): starting point
green, the last implement stage green, "Übernehmen" only when green.

Results live in the task's workspace state file (``suites_baseline``,
``suites``: repo -> {head, ok}) so "Übernehmen" can skip a suite whose
exact commit was already green. Free of SDK imports (tests/test_test_gate.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from . import suites, workspace

REPAIR_OPTION = "Mitreparieren – der Task macht weiter und muss am Ende alles grün haben"
STOP_OPTION = "Anhalten – ich repariere den Ausgangsstand selbst"


def read_state(lay: workspace.Layout, tid: int) -> dict:
    p = lay.state_path(tid)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p else {}
    except (OSError, ValueError):
        return {}


def save(lay: workspace.Layout, tid: int, **keys) -> None:
    """Merge keys into the state file (without recomputing the git state)."""
    p = lay.state_path(tid)
    if p is None:
        return
    st = read_state(lay, tid)
    st.update(keys)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def suite_paths(lay: workspace.Layout, tid: int, cfg: dict[str, suites.Suite],
                names: Optional[set[str]] = None, only_changed: bool = False) -> dict[str, Path]:
    """repo -> worktree path of the task's repos that have a suite
    (``only_changed``: with commits or local changes vs the live branch)."""
    repos = workspace.state(lay, tid)["repos"] if only_changed else {}
    out = {}
    for name, p in workspace.worktrees(lay, tid):
        key = name or "."
        if key not in cfg or (names is not None and key not in names):
            continue
        if only_changed and not ((repos.get(key) or {}).get("ahead") or (repos.get(key) or {}).get("dirty")):
            continue
        out[key] = p
    return out


def _record(lay: workspace.Layout, tid: int, results: list[suites.Result]) -> None:
    known = dict(read_state(lay, tid).get("suites") or {})
    known.update({r.repo: {"head": r.head, "ok": r.ok} for r in results})
    save(lay, tid, suites=known)


def stage_output(verdict: str, summary: str, questions: Optional[list[str]] = None,
                 options: Optional[list[str]] = None) -> str:
    """A ``# Stage result`` the worker writes itself (no agent ran)."""
    obj: dict = {"verdict": verdict, "summary": summary}
    if questions:
        obj["questions"] = questions
    if options:
        obj["options"] = options
    return "# Stage result\n```json\n" + json.dumps(obj, ensure_ascii=False) + "\n```"


def baseline(lay: workspace.Layout, tid: int, task: dict, cfg: dict[str, suites.Suite],
             answer: str, env: Optional[dict] = None) -> tuple[str, str]:
    """Check the task's starting point before its first implement stage.

    Returns ``("go", note for the prompt)``, ``("ask", stage output with the
    question)`` or ``("stop", stage output, verdict blocked)``."""
    base = read_state(lay, tid).get("suites_baseline")
    if base is None:
        names = {str(r) for r in task.get("repos") or []}
        paths = suite_paths(lay, tid, cfg, names=names) if names else {}
        if not paths:
            save(lay, tid, suites_baseline={"ok": True, "skipped": True})
            return "go", ""
        results = suites.run_many(cfg, paths, str(tid), env)
        _record(lay, tid, results)
        base = suites.evidence(results)
        save(lay, tid, suites_baseline=base)
        if base["ok"]:
            return "go", ""
    if base.get("ok") or base.get("accepted"):
        return "go", _note(base)
    if not answer:
        red = ", ".join(k for k, v in (base.get("repos") or {}).items() if not v.get("ok"))
        question = (f"Der Ausgangsstand ist schon rot ({red}), bevor dieser Task etwas geändert hat:\n\n"
                    f"{base.get('report') or ''}\n\nEin Task kann nur durch, wenn am Ende die ganzen "
                    "Suiten grün sind. Soll er das mitreparieren, oder hältst du ihn an?")
        return "ask", stage_output("needs_input", f"Ausgangsstand rot: {red}", [question],
                                   [REPAIR_OPTION, STOP_OPTION])
    low = answer.lower()
    if "anhalten" in low and "mitreparier" not in low:
        return "stop", stage_output("blocked", "Ausgangsstand rot — vom Nutzer angehalten, er repariert ihn selbst.")
    base["accepted"] = True
    save(lay, tid, suites_baseline=base)
    return "go", _note(base)


def _note(base: dict) -> str:
    if base.get("ok") or base.get("skipped"):
        return ""
    return ("### Starting point is red (binding)\nThese test suites were already red before this task "
            "changed anything. The worker runs the whole suites after your stage; the stage only passes "
            "when they are green — repair these failures too (never weaken an assertion):\n\n"
            + str(base.get("report") or ""))


def last_implement(stages: list[dict], seq: int, is_implement: Callable[[str], bool]) -> bool:
    """True when no implement stage is still pending after ``seq``: the
    suites run once at the end of the work, not after every intermediate
    stage. A fix stage the re-plan adds after a red run is the new last one."""
    return not any(int(s.get("seq") or 0) > seq and s.get("status") == "pending"
                   and is_implement(str(s.get("role") or "")) for s in stages)


def end_gate(lay: workspace.Layout, tid: int, cfg: dict[str, suites.Suite],
             env: Optional[dict] = None) -> Optional[dict]:
    """Run the suites of the repos the task changed; evidence for the
    backend gate, or None when no changed repo has a suite."""
    paths = suite_paths(lay, tid, cfg, only_changed=True)
    if not paths:
        return None
    results = suites.run_many(cfg, paths, str(tid), env)
    _record(lay, tid, results)
    return suites.evidence(results)


def apply_check(lay: workspace.Layout, tid: int, cfg: dict[str, suites.Suite],
                env: Optional[dict] = None,
                on_run: Optional[Callable[[str], None]] = None) -> Callable[[list[tuple[str, Path]]], None]:
    """Check for ``workspace.apply``: after the rebase, before the live
    checkout moves. Skips a repo whose exact commit was green already."""
    def check(todo: list[tuple[str, Path]]) -> None:
        known = read_state(lay, tid).get("suites") or {}
        paths = {}
        for key, p in todo:
            if key not in cfg:
                continue
            rec = known.get(key) or {}
            if rec.get("ok") and rec.get("head") and rec.get("head") == suites.git_head(p):
                continue
            paths[key] = p
        if not paths:
            return
        if on_run:
            on_run(", ".join(sorted(paths)))
        results = suites.run_many(cfg, paths, str(tid), env)
        _record(lay, tid, results)
        if not suites.all_green(results):
            raise workspace.GitError("Test-Suite rot — nichts übernommen:\n" + suites.report(results, tail=False))
    return check

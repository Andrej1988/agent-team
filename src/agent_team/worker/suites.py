"""Test suites the worker runs itself — code decides, not the agent.

team.yaml::

    test_suites:
      backend:                      # repo (sub-repo folder; "." = the project root repo)
        run: >-
          docker run --rm --name {name} --memory 4g -v {repo}:/app:ro -w /app
          backend:latest python -m pytest -q -p no:cacheprovider
        timeout_min: 15             # default 20
    test_suites_live:               # daily run on the live checkout
      hour: 3                       # local time, default 3
      channel: myproject-tests      # where a red suite is reported

``{repo}`` is the repo's absolute host path (shell-quoted), ``{name}`` a
container name prefix the worker kills on a timeout (``{name}`` and
``{name}-…``), ``{team}`` the team folder
(e.g. ``bash {team}/run-suite.sh {repo} {name} backend:latest``). The whole suite, never a
subset: a test that is not run cannot fail.

Where they run:

- **start of a task** (first implement stage, repos the task names): the
  starting point must be green. Red → the stage asks the user whether the
  task repairs it too or stops.
- **end of the last implement stage** (repos the task changed): red turns the
  stage's verdict into ``fail`` (backend, ``tasks/db.complete_stage``), which
  sends the task back for a fix; the same repo red twice blocks it.
- **"Übernehmen"** (after the rebase onto the live branch, before the live
  checkout moves): exactly the code that lands must be green.
- **daily on the live checkout**: catches commits made outside tasks.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("agent-team")

DEFAULT_TIMEOUT_MIN = 20
TAIL_LINES = 40
MAX_FAILED = 30
_FAILED_RE = re.compile(r"^(FAILED|ERROR)\s+(\S+)")
_SUMMARY_RE = re.compile(r"\b\d+ (passed|failed|errors?|error)\b")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Suite:
    repo: str
    run: str
    timeout_s: int = DEFAULT_TIMEOUT_MIN * 60


@dataclass
class Result:
    repo: str
    ok: bool
    code: Optional[int]
    seconds: float
    summary: str
    failed: list[str] = field(default_factory=list)
    tail: str = ""
    head: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def load(team: dict, team_dir: Optional[Path] = None) -> dict[str, Suite]:
    """repo -> Suite from team.yaml ``test_suites`` (bad entries are skipped).
    ``{team}`` in a command becomes the team folder (for a helper script there)."""
    out: dict[str, Suite] = {}
    for repo, spec in (team.get("test_suites") or {}).items():
        if isinstance(spec, str):
            spec = {"run": spec}
        if not isinstance(spec, dict) or not str(spec.get("run") or "").strip():
            log.warning("team.yaml test_suites.%s: no run command — skipped", repo)
            continue
        try:
            minutes = float(spec.get("timeout_min") or DEFAULT_TIMEOUT_MIN)
        except (TypeError, ValueError):
            minutes = DEFAULT_TIMEOUT_MIN
        cmd = " ".join(str(spec["run"]).split())
        if team_dir is not None:
            cmd = cmd.replace("{team}", shlex.quote(str(team_dir)))
        out[str(repo)] = Suite(str(repo), cmd, int(minutes * 60))
    return out


def container_name(scope: str, repo: str) -> str:
    """Names the docker proxy accepts for throwaway containers (``task-…``)."""
    slug = _SLUG_RE.sub("-", (repo if repo != "." else "root").lower()).strip("-") or "repo"
    return f"task-{scope}-suite-{slug}"[:120]


def command(suite: Suite, path: Path, name: str) -> str:
    return suite.run.replace("{repo}", shlex.quote(str(path))).replace("{name}", shlex.quote(name))


def git_head(path: Path) -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(path), capture_output=True, text=True, timeout=60)
    return r.stdout.strip() if r.returncode == 0 else ""


def parse(output: str) -> tuple[str, list[str]]:
    """(pytest's final line or the last output line, failed test ids)."""
    lines = [ln.rstrip() for ln in (output or "").splitlines() if ln.strip()]
    failed = []
    for ln in lines:
        m = _FAILED_RE.match(ln)
        if m and m.group(2) not in failed:
            failed.append(m.group(2))
    summary = next((ln.strip("= ").strip() for ln in reversed(lines) if _SUMMARY_RE.search(ln)), "")
    if not summary and lines:
        summary = lines[-1][:300]
    return summary, failed[:MAX_FAILED]


def _kill(name: str) -> None:
    """Stop the suite's containers: ``{name}`` and any ``{name}-…`` (a command
    may run several, e.g. one per sub-folder with its own image)."""
    try:
        r = subprocess.run(["docker", "ps", "-q", "--filter", f"name=^/{name}"],
                           capture_output=True, text=True, timeout=60)
        ids = r.stdout.split() if r.returncode == 0 else []
        subprocess.run(["docker", "kill", *(ids or [name])], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def run(suite: Suite, path: Path, name: str, env: Optional[dict] = None) -> Result:
    """Run one suite; never raises."""
    head = ""
    try:
        head = git_head(path)
    except (OSError, subprocess.SubprocessError):
        pass
    t0 = time.monotonic()
    cmd = command(suite, path, name)
    try:
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                           timeout=suite.timeout_s, env={**os.environ, **(env or {})})
        out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        code: Optional[int] = r.returncode
    except subprocess.TimeoutExpired as e:
        _kill(name)
        raw = e.stdout or b""
        out = (raw.decode(errors="replace") if isinstance(raw, bytes) else raw)
        out += f"\nZeitüberschreitung nach {suite.timeout_s // 60} min — abgebrochen."
        code = None
    except OSError as e:
        out, code = f"Suite nicht startbar: {e}", None
    summary, failed = parse(out)
    tail = "\n".join(out.strip().splitlines()[-TAIL_LINES:])
    res = Result(suite.repo, code == 0, code, round(time.monotonic() - t0, 1), summary, failed, tail, head)
    log.info("suite %s (%s): %s in %.0fs — %s", suite.repo, path, "green" if res.ok else "RED",
             res.seconds, summary)
    return res


def run_many(suites: dict[str, Suite], paths: dict[str, Path], scope: str,
             env: Optional[dict] = None) -> list[Result]:
    """Run the suites of the given repos one after another (sorted)."""
    return [run(suites[r], paths[r], container_name(scope, r), env) for r in sorted(paths) if r in suites]


def all_green(results: list[Result]) -> bool:
    return all(r.ok for r in results)


def short(results: list[Result]) -> str:
    """One line per repo for the chat."""
    return "\n".join(f"{'✅' if r.ok else '❌'} {r.repo}: {r.summary or ('grün' if r.ok else 'rot')}"
                     f" ({r.seconds:.0f} s)" for r in results)


def report(results: list[Result], tail: bool = True) -> str:
    """Readable report; failing repos with their failed tests (and output tail)."""
    parts = [short(results)]
    for r in results:
        if r.ok:
            continue
        block = f"\n**{r.repo}** — Exit {r.code if r.code is not None else '–'}"
        if r.failed:
            block += "\n" + "\n".join(f"- {t}" for t in r.failed)
        if tail and r.tail:
            block += "\n```\n" + r.tail[-2500:] + "\n```"
        parts.append(block)
    return "\n".join(parts)


def evidence(results: list[Result]) -> dict:
    """What the backend's stage gate reads (``tasks/db.complete_stage``)."""
    return {"ok": all_green(results),
            "repos": {r.repo: {"ok": r.ok, "summary": r.summary, "failed": r.failed, "head": r.head}
                      for r in results},
            "report": report(results)[:6000]}


# ---- daily run on the live checkout ----------------------------------------

def live_config(team: dict) -> dict:
    cfg = team.get("test_suites_live") or {}
    try:
        hour = int(cfg.get("hour", 3))
    except (TypeError, ValueError):
        hour = 3
    return {"hour": min(max(hour, 0), 23), "channel": str(cfg.get("channel") or "").strip()}


def live_state_path(control: Path) -> Path:
    return control / "suites" / "live.json"


def load_live_state(control: Path) -> dict:
    try:
        return json.loads(live_state_path(control).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_live_state(control: Path, st: dict) -> None:
    p = live_state_path(control)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def live_due(state: dict, hour: int, now: Optional[time.struct_time] = None) -> bool:
    """Once per local day, from ``hour`` on (also catches up after a restart).
    ``<control>/suites/live.run`` (manual trigger) is handled by the caller."""
    now = now or time.localtime()
    today = time.strftime("%Y-%m-%d", now)
    return state.get("day") != today and now.tm_hour >= hour


def live_message(results: list[Result], before: dict) -> Optional[tuple[str, str, str]]:
    """(title, text, level) for the channel, or None (all green, as before).
    Red is reported every day until it is fixed; a repo that turned green
    again gets one "wieder grün" note."""
    red = [r for r in results if not r.ok]
    was_red = {k for k, v in (before.get("repos") or {}).items() if not v.get("ok", True)}
    healed = sorted(r.repo for r in results if r.ok and r.repo in was_red)
    if red:
        title = "Tests rot im Live-Stand: " + ", ".join(r.repo for r in red)
        text = (report(results) + "\n\nDer Live-Stand ist rot — das muss vor jedem Task in diesen Repos "
                "repariert werden (Tasks prüfen ihren Ausgangsstand und fragen dann nach).")
        if healed:
            text += "\n\nWieder grün: " + ", ".join(healed)
        return title, text, "important"
    if healed:
        return "Tests wieder grün: " + ", ".join(healed), short(results), "info"
    return None

"""Budget pacer — spend the Claude subscription's weekly limit evenly.

Measurement: every Claude CLI/SDK run reports ``rate_limit_event`` with the
unified windows (``five_hour`` / ``seven_day``: utilization 0..1 + resetsAt).
agent-team-chat records the ones its sessions see and, when nothing ran for a
while, probes with a one-token haiku call (``probe_due``). Samples go to
``<data>/budget/samples.jsonl``.

Policy (``evaluate``) — may *autonomous* work start now (scouts, autopilot
chaining; never what the user triggers)?

* week: allowed while used <= the soll line from the window start to
  ``1 - reserve`` at the reset; the reserve is released in the last
  ``release_hours`` before the reset ("use it or lose it"). The line starts
  with a head start (``early_lead_pct`` of the budget) that shrinks to zero by
  its end: generous early in the week, stricter day by day — a straight line
  from 0 % stalled the teams right after the reset (user 2026-10-30).
* five-hour window above ``five_hour_max`` → pause (keeps the user's own
  sessions responsive).
* the user chatted within ``user_active_minutes`` → pause scouts (not the
  pipelines of tasks the user approved, and talking to a lead channel does
  not count — that is steering the team, not working alongside it).
* no fresh sample → allowed (fail open: a broken probe must not stall the
  teams; the limit itself still stops runs).

Mode ``usd`` (``AT_BUDGET_MODE=usd``; work laptop on Bedrock, billed per
token — no subscription windows to measure): every session's cost goes to
``<data>/budget/costs.jsonl`` (``record_cost``), autonomous work stops once
today's spend (Europe/Berlin) reaches ``usd_per_day`` or the last seven days'
reaches ``usd_per_week`` (0 = no cap). No probe runs.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from . import projects

WEEK_S = 7 * 24 * 3600
SAMPLE_STALE_S = 3 * 3600
PROBE_EVERY_S = 30 * 60
_lock = threading.Lock()


def budget_dir() -> Path:
    return projects.DATA_ROOT / "budget"


@dataclass
class Config:
    reserve_pct: float = 15.0        # held back for the user until release_hours before reset
    early_lead_pct: float = 20.0     # head start of the soll line at the week's start, 0 by its end
    release_hours: float = 12.0
    five_hour_max: float = 80.0      # pause autonomous work above this 5-h utilization (%)
    user_active_minutes: float = 15.0
    paused: bool = False             # manual stop for all autonomous work
    mode: str = os.environ.get("AT_BUDGET_MODE", "subscription")   # subscription | usd
    usd_per_day: float = float(os.environ.get("AT_BUDGET_USD_DAY") or 0)
    usd_per_week: float = float(os.environ.get("AT_BUDGET_USD_WEEK") or 0)

    @classmethod
    def load(cls) -> "Config":
        try:
            raw = json.loads((budget_dir() / "config.json").read_text())
        except (OSError, ValueError):
            return cls()
        known = {k: raw[k] for k in cls.__dataclass_fields__ if k in raw}
        return cls(**known)

    def save(self) -> None:
        d = budget_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / ".config.json.tmp"
        tmp.write_text(json.dumps(asdict(self), indent=1))
        tmp.replace(d / "config.json")


# ---- samples -------------------------------------------------------------------

def _window(raw: dict, name: str) -> Optional[dict]:
    w = ((raw or {}).get("unifiedWindows") or {}).get(name)
    if not isinstance(w, dict) or w.get("utilization") is None:
        return None
    return {"util": float(w["utilization"]), "resets_at": w.get("resetsAt"), "status": w.get("status")}


def record(raw: dict, source: str) -> Optional[dict]:
    """Store one rate_limit_event's info (the ``rate_limit_info`` dict)."""
    five, week = _window(raw, "five_hour"), _window(raw, "seven_day")
    if five is None and week is None:
        return None
    sample = {"ts": time.time(), "source": source, "five_hour": five, "seven_day": week}
    d = budget_dir()
    with _lock:
        d.mkdir(parents=True, exist_ok=True)
        with (d / "samples.jsonl").open("a") as f:
            f.write(json.dumps(sample) + "\n")
    return sample


def samples(since_s: float = WEEK_S) -> list[dict]:
    path = budget_dir() / "samples.jsonl"
    if not path.exists():
        return []
    cutoff = time.time() - since_s
    out = []
    with path.open() as f:
        for raw in f:
            try:
                s = json.loads(raw)
            except ValueError:
                continue
            if s.get("ts", 0) >= cutoff:
                out.append(s)
    return out


def latest() -> Optional[dict]:
    path = budget_dir() / "samples.jsonl"
    if not path.exists():
        return None
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 8192))
        lines = f.read().splitlines()
    for raw in reversed(lines):
        try:
            return json.loads(raw)
        except ValueError:
            continue
    return None


def usd_mode() -> bool:
    return os.environ.get("AT_BUDGET_MODE", "subscription") == "usd" or Config.load().mode == "usd"


def record_cost(usd: Optional[float], source: str) -> None:
    """Ledger entry for usd mode (no-op otherwise, so subscription setups keep no
    second bookkeeping)."""
    if not usd or os.environ.get("AT_BUDGET_MODE") != "usd":
        return
    d = budget_dir()
    with _lock:
        d.mkdir(parents=True, exist_ok=True)
        with (d / "costs.jsonl").open("a") as f:
            f.write(json.dumps({"ts": time.time(), "usd": round(float(usd), 6), "source": source}) + "\n")


def spend(now: Optional[float] = None) -> tuple[float, float]:
    """(today in Europe/Berlin, last 7 days) in USD."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = now or time.time()
    midnight = datetime.fromtimestamp(now, ZoneInfo("Europe/Berlin")).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    day = week = 0.0
    path = budget_dir() / "costs.jsonl"
    if not path.exists():
        return 0.0, 0.0
    with path.open() as f:
        for raw in f:
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            ts, usd = float(e.get("ts", 0)), float(e.get("usd") or 0)
            if ts >= now - WEEK_S:
                week += usd
                if ts >= midnight:
                    day += usd
    return round(day, 2), round(week, 2)


def probe_due(now: Optional[float] = None) -> bool:
    if usd_mode():
        return False
    s = latest()
    return s is None or (now or time.time()) - s.get("ts", 0) >= PROBE_EVERY_S


def prune(keep_s: float = 3 * WEEK_S) -> None:
    path = budget_dir() / "samples.jsonl"
    if not path.exists() or path.stat().st_size < 2_000_000:
        return
    keep = samples(keep_s)
    with _lock:
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(s) + "\n" for s in keep))
        tmp.replace(path)


# ---- policy --------------------------------------------------------------------

@dataclass
class Decision:
    allowed: bool
    reason: str
    week_used: Optional[float] = None       # %
    week_target: Optional[float] = None     # % allowed by now (even-spend line)
    week_resets_at: Optional[float] = None
    five_hour_used: Optional[float] = None  # %
    five_hour_resets_at: Optional[float] = None
    sample_age_s: Optional[float] = None
    reserve_released: bool = False
    usd_today: Optional[float] = None
    usd_week: Optional[float] = None

    def as_dict(self) -> dict:
        return asdict(self)


def week_target(now: float, resets_at: float, cfg: Config) -> tuple[float, bool]:
    """Utilization (0..1) the soll line allows at ``now``, and whether the
    reserve is already released. Line: ceiling * (frac + lead * (1 - frac))."""
    start = resets_at - WEEK_S
    released = now >= resets_at - cfg.release_hours * 3600
    ceiling = 1.0 if released else 1.0 - cfg.reserve_pct / 100
    if released:
        return 1.0, True
    end = resets_at - cfg.release_hours * 3600  # the line reaches the ceiling here
    frac = min(max((now - start) / max(end - start, 1), 0.0), 1.0)
    lead = min(max(cfg.early_lead_pct / 100, 0.0), 1.0)
    return ceiling * min(1.0, frac + lead * (1 - frac)), False


def evaluate(now: Optional[float] = None, last_user_activity: Optional[float] = None,
             cfg: Optional[Config] = None, sample: Optional[dict] = None) -> Decision:
    now = now or time.time()
    cfg = cfg or Config.load()
    s = sample if sample is not None else latest()
    if cfg.paused:
        return Decision(False, "manuell pausiert")
    if cfg.mode == "usd":
        day, week_usd = spend(now)
        d = Decision(True, "im Plan", usd_today=day, usd_week=week_usd)
        if last_user_activity and now - last_user_activity < cfg.user_active_minutes * 60:
            d.allowed, d.reason = False, "du arbeitest gerade selbst"
        if cfg.usd_per_day and day >= cfg.usd_per_day:
            d.allowed, d.reason = False, f"Tagesbudget erreicht (${day:.2f} von ${cfg.usd_per_day:.0f})"
        if cfg.usd_per_week and week_usd >= cfg.usd_per_week:
            d.allowed, d.reason = False, f"Wochenbudget erreicht (${week_usd:.2f} von ${cfg.usd_per_week:.0f})"
        return d
    if not s:
        return Decision(True, "noch keine Messung")
    age = now - s.get("ts", 0)
    week, five = s.get("seven_day"), s.get("five_hour")
    d = Decision(True, "im Plan", sample_age_s=age)
    if five:
        d.five_hour_used = round(five["util"] * 100, 1)
        d.five_hour_resets_at = five.get("resets_at")
    if week:
        d.week_used = round(week["util"] * 100, 1)
        d.week_resets_at = week.get("resets_at")
    if age > SAMPLE_STALE_S:
        d.reason = "Messung veraltet — frei"
        return d
    if last_user_activity and now - last_user_activity < cfg.user_active_minutes * 60:
        d.allowed, d.reason = False, "du arbeitest gerade selbst"
    if five and five["util"] * 100 > cfg.five_hour_max and not (
            five.get("resets_at") and five["resets_at"] <= now):
        d.allowed, d.reason = False, f"5-h-Fenster über {cfg.five_hour_max:.0f} %"
    if week and week.get("resets_at"):
        target, released = week_target(now, float(week["resets_at"]), cfg)
        d.week_target, d.reserve_released = round(target * 100, 1), released
        if week["resets_at"] > now and week["util"] > target:
            d.allowed, d.reason = False, "Wochenbudget voraus — wartet auf die Soll-Linie"
    return d


def user_last_active() -> Optional[float]:
    """Newest ``user`` message across the chat channels except lead channels
    (unix time)."""
    from datetime import datetime

    from .chat import log as chatlog

    newest: Optional[float] = None
    for ch in projects.all_channels():
        if ch.lead:
            continue
        for ev in chatlog.iter_tail(ch.id, max_bytes=60_000):
            if ev.get("type") == "user":
                try:
                    t = datetime.fromisoformat(ev["ts"]).timestamp()
                except (KeyError, ValueError):
                    continue
                newest = t if newest is None or t > newest else newest
    return newest


def decision(for_tasks: bool = False) -> Decision:
    """for_tasks: chaining stages of a task the user started — the
    user-activity pause does not apply, the week / 5-h limits do."""
    return evaluate(last_user_activity=None if for_tasks else user_last_active())


def summary() -> dict[str, Any]:
    return {"decision": decision().as_dict(), "config": asdict(Config.load()), "latest": latest()}

"""Gate numbers computed by code, not reported by the agent.

The backtest-engineer saves the raw out-of-sample result (after costs) next to
its report:

    <repo>/docs/tasks/task-<id>/oos_equity.csv   date,equity   (one row per bar
                                                  or day, flat periods included)
    <repo>/docs/tasks/task-<id>/oos_trades.csv   one row per closed trade

When a stage finishes, the worker computes oos_sharpe, max_drawdown_pct and
oos_trades from these files and sends them with the result; the backend's
metric gate takes the worse of these and the auditor's numbers. Stdlib only
(the worker image has no pandas).
"""

from __future__ import annotations

import csv
import math
from datetime import date
from pathlib import Path
from typing import Optional

EQUITY = "oos_equity.csv"
TRADES = "oos_trades.csv"
MIN_DAYS = 30        # shorter windows give no meaningful Sharpe
MIN_RETURNS = 20


class EvidenceError(ValueError):
    pass


def find(root: Path, task_id: int) -> Optional[Path]:
    """The newest docs/tasks/task-<id>/ holding an equity file, in the root
    repo or any sub-repo (links included: legacy tasks run in the live tree)."""
    rel = Path("docs") / "tasks" / f"task-{task_id}" / EQUITY
    hits = [p for p in [root / rel, *root.glob(f"*/{rel}")] if p.is_file()]
    return max(hits, key=lambda p: p.stat().st_mtime).parent if hits else None


def _day(raw: str) -> date:
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        raise EvidenceError(f"{EQUITY}: Datum {raw!r} ist kein ISO-Datum") from None


def equity_metrics(path: Path) -> dict:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows or not {"date", "equity"} <= set(rows[0]):
        raise EvidenceError(f"{EQUITY}: Spalten 'date,equity' fehlen")
    points = [(_day(r["date"]), float(r["equity"])) for r in rows if (r.get("equity") or "").strip()]
    points.sort(key=lambda p: p[0])
    if any(v <= 0 for _, v in points):
        raise EvidenceError(f"{EQUITY}: Equity muss positiv sein (Kontostand, nicht PnL)")
    peak, mdd = 0.0, 0.0
    for _, v in points:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)
    daily: dict[date, float] = {}
    for d, v in points:
        daily[d] = v                                   # last value of the day
    days = sorted(daily)
    rets = [daily[b] / daily[a] - 1 for a, b in zip(days, days[1:])]
    span = (days[-1] - days[0]).days if days else 0
    if span < MIN_DAYS or len(rets) < MIN_RETURNS:
        raise EvidenceError(f"{EQUITY}: zu kurz ({span} Tage, {len(rets)} Tagesrenditen)")
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    per_year = len(rets) / (span / 365.25)             # 252 for stocks, 365 for crypto
    sharpe = mean / sd * math.sqrt(per_year) if sd > 0 else 0.0
    return {"oos_sharpe": round(sharpe, 3), "max_drawdown_pct": round(mdd * 100, 2)}


def trade_count(path: Path) -> int:
    with path.open(newline="", encoding="utf-8") as f:
        return sum(1 for row in csv.reader(f) if any(c.strip() for c in row)) - 1


def collect(root: Path, task_id: int) -> dict:
    """{"metrics": {...}, "file": rel} or {"error": "..."}; never raises."""
    folder = find(root, task_id)
    if folder is None:
        return {"error": f"docs/tasks/task-{task_id}/{EQUITY} nicht gefunden"}
    try:
        rel = str(folder.relative_to(root))
    except ValueError:
        rel = str(folder)
    try:
        metrics = equity_metrics(folder / EQUITY)
        if (folder / TRADES).is_file():
            metrics["oos_trades"] = max(trade_count(folder / TRADES), 0)
        return {"metrics": metrics, "file": rel}
    except (EvidenceError, OSError, ValueError, KeyError) as e:
        return {"error": f"{rel}: {e}"}

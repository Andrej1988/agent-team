"""Gate numbers computed by code, not reported by the agent.

The backtest-engineer saves the raw out-of-sample result (after costs) next to
its report:

    <repo>/docs/tasks/task-<id>/oos_equity.csv   date,equity   (one row per bar
                                                  or day, flat periods included)
    <repo>/docs/tasks/task-<id>/oos_trades.csv   one row per closed trade

    <repo>/docs/tasks/task-<id>/oos_benchmark.csv date,equity of the matching
                                                  benchmark (buy & hold, same
                                                  window; optional)

When a stage finishes, the worker computes oos_sharpe, max_drawdown_pct and
oos_trades from these files (with a benchmark also benchmark_sharpe, alpha_pct
and beta) and sends them with the result. With a metric gate the backend takes
the worse of these and the auditor's numbers; without one (metric_files only)
they are shown next to the auditor's verdict. Stdlib only (the worker image
has no pandas).
"""

from __future__ import annotations

import csv
import math
from datetime import date
from pathlib import Path
from typing import Optional

EQUITY = "oos_equity.csv"
TRADES = "oos_trades.csv"
BENCHMARK = "oos_benchmark.csv"
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


def _daily(path: Path) -> tuple[dict[date, float], float]:
    """Last equity per day and the max drawdown over all rows."""
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows or not {"date", "equity"} <= set(rows[0]):
        raise EvidenceError(f"{path.name}: Spalten 'date,equity' fehlen")
    points = [(_day(r["date"]), float(r["equity"])) for r in rows if (r.get("equity") or "").strip()]
    points.sort(key=lambda p: p[0])
    if any(v <= 0 for _, v in points):
        raise EvidenceError(f"{path.name}: Equity muss positiv sein (Kontostand, nicht PnL)")
    peak, mdd = 0.0, 0.0
    for _, v in points:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)
    daily: dict[date, float] = {}
    for d, v in points:
        daily[d] = v                                   # last value of the day
    return daily, mdd


def _returns(daily: dict[date, float], days: list[date]) -> list[float]:
    return [daily[b] / daily[a] - 1 for a, b in zip(days, days[1:])]


def _sharpe(rets: list[float], per_year: float) -> float:
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    return mean / sd * math.sqrt(per_year) if sd > 0 else 0.0


def equity_metrics(path: Path, benchmark: Optional[Path] = None) -> dict:
    daily, mdd = _daily(path)
    days = sorted(daily)
    rets = _returns(daily, days)
    span = (days[-1] - days[0]).days if days else 0
    if span < MIN_DAYS or len(rets) < MIN_RETURNS:
        raise EvidenceError(f"{EQUITY}: zu kurz ({span} Tage, {len(rets)} Tagesrenditen)")
    per_year = len(rets) / (span / 365.25)             # 252 for stocks, 365 for crypto
    out = {"oos_sharpe": round(_sharpe(rets, per_year), 3), "max_drawdown_pct": round(mdd * 100, 2)}
    if benchmark is not None:
        out.update(benchmark_metrics(daily, benchmark, per_year))
    return out


def benchmark_metrics(daily: dict[date, float], path: Path, per_year: float) -> dict:
    """Benchmark Sharpe plus alpha (annualised, %) and beta of the strategy
    against it, on the days both series have (no risk-free rate)."""
    bench, _ = _daily(path)
    days = sorted(set(daily) & set(bench))
    if len(days) <= MIN_RETURNS:
        raise EvidenceError(f"{BENCHMARK}: zu wenig gemeinsame Tage mit {EQUITY} ({len(days)})")
    s, b = _returns(daily, days), _returns(bench, days)
    ms, mb = sum(s) / len(s), sum(b) / len(b)
    var_b = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    beta = sum((x - ms) * (y - mb) for x, y in zip(s, b)) / (len(b) - 1) / var_b if var_b > 0 else 0.0
    alpha = (ms - beta * mb) * per_year
    return {"benchmark_sharpe": round(_sharpe(b, per_year), 3),
            "alpha_pct": round(alpha * 100, 2), "beta": round(beta, 3)}


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
        bench = folder / BENCHMARK
        metrics = equity_metrics(folder / EQUITY, bench if bench.is_file() else None)
        if (folder / TRADES).is_file():
            metrics["oos_trades"] = max(trade_count(folder / TRADES), 0)
        return {"metrics": metrics, "file": rel}
    except (EvidenceError, OSError, ValueError, KeyError) as e:
        return {"error": f"{rel}: {e}"}

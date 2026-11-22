"""Gate numbers computed by code from the backtest's raw files."""

from __future__ import annotations

import pytest
from datetime import date, timedelta

from agent_team.worker import evidence

pytestmark = pytest.mark.generic


def write_run(folder, values, trades=40):
    folder.mkdir(parents=True)
    start = date(2025, 1, 1)
    lines = ["date,equity"] + [f"{start + timedelta(days=i)},{v}" for i, v in enumerate(values)]
    (folder / evidence.EQUITY).write_text("\n".join(lines) + "\n")
    (folder / evidence.TRADES).write_text("id,pnl\n" + "".join(f"{i},1\n" for i in range(trades)))


def test_metrics_from_files(tmp_path):
    # steady +0.1 %/day with a dip: positive Sharpe, 10 % drawdown, 40 trades
    vals = [100 * 1.001 ** i for i in range(200)]
    vals[100:110] = [vals[99] * 0.9] * 10
    write_run(tmp_path / "swing" / "docs" / "tasks" / "task-5", vals)
    out = evidence.collect(tmp_path, 5)
    assert out["file"] == "swing/docs/tasks/task-5"
    m = out["metrics"]
    assert m["oos_trades"] == 40
    assert abs(m["max_drawdown_pct"] - 10.0) < 0.01
    assert m["oos_sharpe"] > 1


def test_missing_or_bad_files_are_errors(tmp_path):
    assert "nicht gefunden" in evidence.collect(tmp_path, 6)["error"]
    write_run(tmp_path / "docs" / "tasks" / "task-7", [100, 101, 102])
    assert "zu kurz" in evidence.collect(tmp_path, 7)["error"]
    f = tmp_path / "x" / "docs" / "tasks" / "task-8"
    f.mkdir(parents=True)
    (f / evidence.EQUITY).write_text("day,pnl\n2025-01-01,5\n")
    assert "Spalten" in evidence.collect(tmp_path, 8)["error"]


def test_benchmark_alpha_beta(tmp_path):
    # strategy = 0.5 x benchmark + 0.05 %/day: beta 0.5, positive alpha
    import math
    bench = [100.0]
    for i in range(1, 300):
        bench.append(bench[-1] * (1 + 0.01 * math.sin(i * 1.7)))
    strat = [100.0]
    for a, b in zip(bench, bench[1:]):
        strat.append(strat[-1] * (1 + 0.5 * (b / a - 1) + 0.0005))
    folder = tmp_path / "docs" / "tasks" / "task-9"
    write_run(folder, strat)
    start = date(2025, 1, 1)
    (folder / evidence.BENCHMARK).write_text("date,equity\n" + "".join(
        f"{start + timedelta(days=i)},{v}\n" for i, v in enumerate(bench)))
    m = evidence.collect(tmp_path, 9)["metrics"]
    assert abs(m["beta"] - 0.5) < 0.01
    assert abs(m["alpha_pct"] - 0.05 * 365) < 1.0
    assert "benchmark_sharpe" in m


def test_without_benchmark_no_alpha(tmp_path):
    write_run(tmp_path / "docs" / "tasks" / "task-10", [100 * 1.001 ** i for i in range(100)])
    m = evidence.collect(tmp_path, 10)["metrics"]
    assert "beta" not in m and "oos_sharpe" in m

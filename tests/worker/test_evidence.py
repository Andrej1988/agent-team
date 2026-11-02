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

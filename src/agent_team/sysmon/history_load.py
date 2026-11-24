"""Background sampler that snapshots CPU usage % and system load average.

Held in-process — survives a route call but not a container restart, which
is fine for short-window monitoring (24 h) without taking on a time-series
DB dependency.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Deque

import psutil

logger = logging.getLogger(__name__)

# 60 s × 1440 samples = 24 h.
SAMPLE_INTERVAL_S = 60
MAX_SAMPLES = 24 * 60

_LOCK = threading.Lock()
# Each entry: (ts, cpu_pct, load1, load5, load15)
_HISTORY: Deque[tuple[float, float, float, float, float]] = deque(maxlen=MAX_SAMPLES)
_THREAD: threading.Thread | None = None
_STOP = threading.Event()


def _sample_once() -> None:
    ts = time.time()
    # Blocking 0.5 s measurement — fine on a background thread, and independent
    # of other psutil.cpu_percent() callers.
    try:
        cpu_pct = psutil.cpu_percent(interval=0.5)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("load sampler: cpu_percent failed: %s", exc)
        cpu_pct = 0.0
    try:
        load1, load5, load15 = os.getloadavg()
    except (OSError, AttributeError) as exc:
        logger.debug("load sampler: getloadavg failed: %s", exc)
        return
    with _LOCK:
        _HISTORY.append(
            (ts, round(cpu_pct, 1), round(load1, 2), round(load5, 2), round(load15, 2))
        )


def _loop() -> None:
    # Take an immediate sample so the API has something to return right away.
    _sample_once()
    while not _STOP.wait(SAMPLE_INTERVAL_S):
        _sample_once()


def start_sampler() -> None:
    global _THREAD
    if _THREAD is not None:
        return
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, name="load-history-sampler", daemon=True)
    _THREAD.start()


def stop_sampler() -> None:
    global _THREAD
    _STOP.set()
    _THREAD = None


def get_history(hours: float = 24.0) -> dict:
    """Return CPU-usage / load-average sample series since `hours` ago.

    Output: {"cores": int, "samples": [{"ts": float, "cpu_pct": float,
             "load1": float, "load5": float, "load15": float}, ...]}
    """
    cutoff = time.time() - hours * 3600
    with _LOCK:
        samples = [
            {"ts": t, "cpu_pct": cp, "load1": l1, "load5": l5, "load15": l15}
            for (t, cp, l1, l5, l15) in _HISTORY
            if t >= cutoff
        ]
    return {
        "cores": os.cpu_count() or 1,
        "samples": samples,
    }

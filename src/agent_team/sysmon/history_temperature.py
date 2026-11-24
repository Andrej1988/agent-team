"""Background sampler that snapshots temperatures into a per-sensor ring buffer.

Held in-process — survives a route call but not a container restart, which
is fine for short-window monitoring (24 h) without taking on a time-series
DB dependency.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from typing import Deque

from .host import get_temperatures

logger = logging.getLogger(__name__)

# 60 s × 1440 samples = 24 h per sensor.
SAMPLE_INTERVAL_S = 60
MAX_SAMPLES = 24 * 60

_LOCK = threading.Lock()
_HISTORY: dict[str, Deque[tuple[float, float, str]]] = defaultdict(
    lambda: deque(maxlen=MAX_SAMPLES)
)
_THREAD: threading.Thread | None = None
_STOP = threading.Event()


def _sample_once(allow_drive_scan: bool = False) -> None:
    ts = time.time()
    try:
        readings = get_temperatures(allow_drive_scan=allow_drive_scan)
    except Exception as exc:
        logger.debug("temperature sampler: get_temperatures failed: %s", exc)
        return
    with _LOCK:
        for entry in readings:
            name = entry.get("name")
            temp = entry.get("temp")
            cat = entry.get("category", "")
            if name is None or temp is None:
                continue
            _HISTORY[name].append((ts, float(temp), str(cat)))


def _loop() -> None:
    # First sample is fast (skips the SMART scan) so the API has data right away.
    _sample_once(allow_drive_scan=False)
    while not _STOP.wait(SAMPLE_INTERVAL_S):
        # Subsequent samples include SSD/HDD temps and keep the SMART cache warm.
        _sample_once(allow_drive_scan=True)


def start_sampler() -> None:
    global _THREAD
    if _THREAD is not None:
        return
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, name="temp-history-sampler", daemon=True)
    _THREAD.start()


def stop_sampler() -> None:
    global _THREAD
    _STOP.set()
    _THREAD = None


def get_history(hours: float = 24.0) -> dict[str, dict]:
    """Return per-sensor sample series since `hours` ago.

    Output: {sensor_name: {"category": "...", "samples": [{"ts": float, "temp": float}, ...]}}
    """
    cutoff = time.time() - hours * 3600
    out: dict[str, dict] = {}
    with _LOCK:
        for name, buf in _HISTORY.items():
            samples = [(t, v, c) for (t, v, c) in buf if t >= cutoff]
            if not samples:
                continue
            out[name] = {
                "category": samples[-1][2],
                "samples": [{"ts": t, "temp": v} for (t, v, _c) in samples],
            }
    return out

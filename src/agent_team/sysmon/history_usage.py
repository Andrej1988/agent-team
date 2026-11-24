"""Background sampler that snapshots RAM, GPU and disk usage history.

Held in-process — survives a route call but not a container restart, which
is fine for short-window monitoring (24 h) without taking on a time-series
DB dependency. Mirrors the structure of ``load_history`` / ``temperature_history``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Deque

from .host import get_disks, get_gpus, get_memory

logger = logging.getLogger(__name__)

# 60 s × 1440 samples = 24 h per series.
SAMPLE_INTERVAL_S = 60
MAX_SAMPLES = 24 * 60

_LOCK = threading.Lock()
# Each entry: (ts, percent, used_gb)
_MEMORY: Deque[tuple[float, float, float]] = deque(maxlen=MAX_SAMPLES)
# GPU keyed by index; each value tracks latest name + samples deque of (ts, util_pct, mem_pct)
_GPUS: dict[int, dict] = {}
# Disk keyed by mountpoint; each value tracks device/fstype + samples deque of (ts, percent, used_gb)
_DISKS: dict[str, dict] = {}
_THREAD: threading.Thread | None = None
_STOP = threading.Event()


def _sample_once() -> None:
    ts = time.time()

    try:
        mem = get_memory()
        with _LOCK:
            _MEMORY.append((ts, float(mem["percent"]), float(mem["used_gb"])))
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("usage sampler: get_memory failed: %s", exc)

    try:
        gpus = get_gpus()
        with _LOCK:
            for gpu in gpus:
                idx = int(gpu["index"])
                bucket = _GPUS.get(idx)
                if bucket is None:
                    bucket = {"name": gpu["name"], "samples": deque(maxlen=MAX_SAMPLES)}
                    _GPUS[idx] = bucket
                bucket["name"] = gpu["name"]
                bucket["samples"].append(
                    (ts, float(gpu["util_pct"]), float(gpu["mem_pct"]))
                )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("usage sampler: get_gpus failed: %s", exc)

    try:
        disks = get_disks()
        with _LOCK:
            for d in disks:
                mp = d["mountpoint"]
                bucket = _DISKS.get(mp)
                if bucket is None:
                    bucket = {
                        "label": d.get("label"),
                        "device": d.get("device"),
                        "fstype": d.get("fstype"),
                        "total_gb": d.get("total_gb"),
                        "samples": deque(maxlen=MAX_SAMPLES),
                    }
                    _DISKS[mp] = bucket
                bucket["label"] = d.get("label")
                bucket["device"] = d.get("device")
                bucket["fstype"] = d.get("fstype")
                bucket["total_gb"] = d.get("total_gb")
                bucket["samples"].append(
                    (ts, float(d["percent"]), float(d["used_gb"]))
                )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("usage sampler: get_disks failed: %s", exc)


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
    _THREAD = threading.Thread(target=_loop, name="usage-history-sampler", daemon=True)
    _THREAD.start()


def stop_sampler() -> None:
    global _THREAD
    _STOP.set()
    _THREAD = None


def get_history(hours: float = 24.0) -> dict:
    """Return RAM / per-GPU / per-disk usage series since ``hours`` ago.

    Output::

        {
          "memory": {"samples": [{"ts", "percent", "used_gb"}, ...]},
          "gpus":   [{"index", "name", "samples": [{"ts", "util_pct", "mem_pct"}, ...]}, ...],
          "disks":  [{"mountpoint", "device", "fstype", "total_gb",
                      "samples": [{"ts", "percent", "used_gb"}, ...]}, ...],
        }
    """
    cutoff = time.time() - hours * 3600
    with _LOCK:
        memory_samples = [
            {"ts": t, "percent": p, "used_gb": u}
            for (t, p, u) in _MEMORY
            if t >= cutoff
        ]
        gpus: list[dict] = []
        for idx in sorted(_GPUS.keys()):
            samples = [
                {"ts": t, "util_pct": u, "mem_pct": m}
                for (t, u, m) in _GPUS[idx]["samples"]
                if t >= cutoff
            ]
            if not samples:
                continue
            gpus.append({"index": idx, "name": _GPUS[idx]["name"], "samples": samples})
        disks: list[dict] = []
        for mp in sorted(_DISKS.keys()):
            bucket = _DISKS[mp]
            samples = [
                {"ts": t, "percent": p, "used_gb": u}
                for (t, p, u) in bucket["samples"]
                if t >= cutoff
            ]
            if not samples:
                continue
            disks.append(
                {
                    "mountpoint": mp,
                    "label": bucket.get("label"),
                    "device": bucket.get("device"),
                    "fstype": bucket.get("fstype"),
                    "total_gb": bucket.get("total_gb"),
                    "samples": samples,
                }
            )
    return {
        "memory": {"samples": memory_samples},
        "gpus": gpus,
        "disks": disks,
    }

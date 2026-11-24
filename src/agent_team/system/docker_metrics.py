"""Container CPU/RAM and Docker disk usage through the read-only proxy."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone

from . import docker_ro

_UP_RE = re.compile(r"^Up (?:About an? |(\d+) )(second|minute|hour|day|week|month|year)")
_UNIT_S = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800,
           "month": 2592000, "year": 31536000}
_cache: dict[str, tuple[float, object]] = {}
_locks: dict[str, asyncio.Lock] = {}
CONTAINERS_TTL_S, DISK_TTL_S = 30.0, 60.0


def started_at(status: str) -> str:
    """Approximate start from the list Status (the exact one needs inspect)."""
    if not (status or "").startswith("Up "):
        return ""
    m = _UP_RE.match(status)
    secs = int(m.group(1) or 1) * _UNIT_S[m.group(2)] if m else 0
    return (datetime.now(timezone.utc) - timedelta(seconds=secs)).strftime("%Y-%m-%dT%H:%M:%SZ")


def stats_numbers(raw: dict) -> dict:
    try:
        cpu, pre = raw["cpu_stats"], raw["precpu_stats"]
        cpu_delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or [1])
        cpu_pct = cpu_delta / sys_delta * ncpu * 100 if sys_delta > 0 else 0.0
        mem = raw.get("memory_stats") or {}
        usage, limit = mem.get("usage", 0), mem.get("limit", 0)
        return {"cpu_pct": round(cpu_pct, 2), "mem_mb": round(usage / 1048576, 1),
                "mem_pct": round(usage / limit * 100, 2) if limit else 0.0}
    except (KeyError, TypeError, ZeroDivisionError):
        return {"cpu_pct": 0.0, "mem_mb": 0.0, "mem_pct": 0.0}


async def _cached(key: str, ttl: float, make):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    async with _locks.setdefault(key, asyncio.Lock()):
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        val = await make()
        _cache[key] = (time.monotonic(), val)
        return val


async def _stats(c, name: str, sem: asyncio.Semaphore) -> dict:
    async with sem:
        try:
            r = await c.get(f"/containers/{name}/stats", params={"stream": "false"})
            r.raise_for_status()
            return stats_numbers(r.json())
        except Exception:  # noqa: BLE001 — one container must not spoil the list
            return {"cpu_pct": 0.0, "mem_mb": 0.0, "mem_pct": 0.0}


async def _containers_with_stats() -> list[dict]:
    raw = await docker_ro.containers()
    rows = []
    for c in raw:
        labels = c.get("Labels") or {}
        name = ((c.get("Names") or ["?"])[0]).lstrip("/")
        rows.append({
            "id": name,
            "name": name,
            "status": c.get("State"),
            "status_text": c.get("Status"),
            "image": c.get("Image", ""),
            "started_at": started_at(c.get("Status", "")),
            "project": labels.get("com.docker.compose.project"),
            "service": labels.get("com.docker.compose.service"),
        })
    sem = asyncio.Semaphore(32)
    running = [r for r in rows if r["status"] == "running"]
    async with docker_ro._client(timeout=15.0) as client:
        stats = await asyncio.gather(*(_stats(client, r["name"], sem) for r in running))
    empty = {"cpu_pct": 0.0, "mem_mb": 0.0, "mem_pct": 0.0}
    by_name = {r["name"]: s for r, s in zip(running, stats)}
    return [{**r, **by_name.get(r["name"], empty)} for r in rows]


async def containers() -> list[dict]:
    return await _cached("containers", CONTAINERS_TTL_S, _containers_with_stats)


def disk_summary(info: dict) -> dict:
    def total(items, *keys):
        for k in keys:
            s = sum(i.get(k, 0) for i in items if isinstance(i.get(k), int) and i.get(k) > 0)
            if s:
                return s
        return 0

    imgs, cons = info.get("Images") or [], info.get("Containers") or []
    vols, cache = info.get("Volumes") or [], info.get("BuildCache") or []
    sizes = {
        "images": total(imgs, "SharedSize", "Size"),
        "containers": total(cons, "SizeRw", "SizeRootFs"),
        "volumes": sum((v.get("UsageData") or {}).get("Size", 0) for v in vols
                       if isinstance((v.get("UsageData") or {}).get("Size"), int) and v["UsageData"]["Size"] > 0),
        "build_cache": total(cache, "Size"),
    }
    counts = {"images": len(imgs), "containers": len(cons), "volumes": len(vols), "build_cache": len(cache)}
    mb = lambda b: round(b / 1048576, 1)  # noqa: E731
    out = {k: {"count": counts[k], "size_mb": mb(sizes[k])} for k in sizes}
    out["total_mb"] = mb(sum(sizes.values()))
    return out


async def _disk() -> dict:
    async with docker_ro._client(timeout=60.0) as c:
        r = await c.get("/system/df")
        r.raise_for_status()
        return disk_summary(r.json())


async def disk() -> dict:
    return await _cached("disk", DISK_TTL_S, _disk)

"""
Drive health using smartctl directly.

Scans block devices (/dev/sda..sdz, /dev/nvme0..9) via smartctl.
Results are cached for CACHE_TTL_SECONDS (1 hour) so we don't hammer drives.

smartctl needs the raw disks plus CAP_SYS_RAWIO/CAP_SYS_ADMIN, and the web
app should have neither. So in the container the scan runs in its own service
(`smart`, no network, see docker-compose.yml):

    python -m agent_team.sysmon.drives /smart/drives.json

writes the parsed drives every CACHE_TTL_SECONDS, and the app reads that file
(SMART_FILE). Without SMART_FILE (dev) the app scans itself as before.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import subprocess
import time
from typing import Optional

import psutil

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 3600  # 1 hour
SMART_FILE = os.environ.get("SMART_FILE", "")
FILE_TTL_SECONDS = 60     # re-read the collector's file at most this often
STALE_AFTER_SECONDS = 3 * CACHE_TTL_SECONDS

_cache: dict = {}
_cache_ts: float = 0.0


# ---------------------------------------------------------------------------
# Friendly drive nicknames (keyed by device path)
# ---------------------------------------------------------------------------

# AT_SYSMON_DRIVE_NAMES="/dev/sda=Archiv-HDD,/dev/nvme0=System-SSD" (optional;
# the SMART file of a separate scanner may already carry names).
_DRIVE_NICKNAMES: dict[str, str] = dict(
    pair.split("=", 1) for pair in os.environ.get("AT_SYSMON_DRIVE_NAMES", "").split(",") if "=" in pair
)


# ---------------------------------------------------------------------------
# Device discovery
# ---------------------------------------------------------------------------

def _find_drives() -> list[str]:
    """Return unique physical block devices (whole-disk nodes, no partitions)."""
    candidates: list[str] = []
    # SATA/SAS HDDs
    for path in sorted(glob.glob("/dev/sd?")):
        candidates.append(path)
    # NVMe
    for path in sorted(glob.glob("/dev/nvme?")):
        candidates.append(path)
    return candidates


# ---------------------------------------------------------------------------
# smartctl parsing
# ---------------------------------------------------------------------------

def _run_smartctl(device: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["smartctl", "-a", device],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout
    except FileNotFoundError:
        logger.error("smartctl not found — ensure smartmontools is installed")
        return None
    except Exception as exc:
        logger.warning("smartctl failed for %s: %s", device, exc)
        return None


def _attr(output: str, attr_id: int) -> Optional[int]:
    """Extract the RAW_VALUE for a numeric SMART attribute ID."""
    # Columns: ID ATTR_NAME FLAG VALUE WORST THRESH TYPE UPDATED WHEN_FAILED RAW_VALUE
    m = re.search(rf"^\s*{attr_id}\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+(\S+)",
                  output, re.MULTILINE)
    if not m:
        return None
    # Raw value may be like "70728 (161 90 0)" — take first integer
    raw = m.group(1).split("(")[0].strip()
    try:
        return int(raw)
    except ValueError:
        return None


def _nvme_field(output: str, label: str) -> Optional[str]:
    m = re.search(rf"^{re.escape(label)}:\s+(.+)$", output, re.MULTILINE)
    return m.group(1).strip() if m else None


def _parse_sata(device: str, output: str) -> dict:
    info: dict = {"device": device, "drive_type": "HDD/SSD", "nickname": _DRIVE_NICKNAMES.get(device)}

    # Model
    m = re.search(r"Device Model:\s+(.+)", output)
    info["model"] = m.group(1).strip() if m else None

    # Overall health
    m = re.search(r"SMART overall-health self-assessment test result:\s+(\w+)", output)
    info["overall_health"] = m.group(1) if m else "UNKNOWN"

    # Power-on hours (attr 9)
    info["power_on_hours"] = _attr(output, 9)

    # Temperature (attr 194, fallback 190)
    temp = _attr(output, 194)
    if temp is None:
        temp = _attr(output, 190)
    # Attr 194/190 raw value can be "27 (0 14 0 0 0)" — already handled in _attr
    info["temperature_c"] = temp

    # Raw Read Error Rate (attr 1) — Seagate uses huge numbers; show as-is
    info["raw_read_errors"] = _attr(output, 1)

    # Reallocated sectors (attr 5)
    info["reallocated_sectors"] = _attr(output, 5)

    # Pending sectors (attr 197)
    info["pending_sectors"] = _attr(output, 197)

    # Uncorrectable sectors (attr 198)
    info["uncorrectable_sectors"] = _attr(output, 198)

    # Spin retry count (attr 10)
    info["spin_retry_count"] = _attr(output, 10)

    # Reported uncorrectable (attr 187)
    info["reported_uncorrectable"] = _attr(output, 187)

    # Drive age from power-on hours
    poh = info["power_on_hours"]
    info["age_years"] = round(poh / 8760, 1) if poh else None

    info.update(_assess_sata(info))
    return info


def _assess_sata(info: dict) -> dict:
    health = info.get("overall_health") or "UNKNOWN"
    if health not in ("PASSED", "UNKNOWN"):
        return {"estimated_health": "FAILED — SMART reports failure", "health_level": "error"}

    poh = info.get("power_on_hours") or 0
    realloc = info.get("reallocated_sectors") or 0
    pending = info.get("pending_sectors") or 0
    uncorr = info.get("uncorrectable_sectors") or 0
    spin_retry = info.get("spin_retry_count") or 0

    warnings = []
    if uncorr > 0:
        warnings.append(f"{uncorr} uncorrectable sector(s)")
    if pending > 0:
        warnings.append(f"{pending} pending sector(s)")
    if spin_retry > 0:
        warnings.append(f"spin retry count {spin_retry}")
    if realloc > 0:
        warnings.append(f"{realloc} reallocated sector(s)")
    if poh > 70000:
        warnings.append(f"very high usage ({poh:,} h)")
    elif poh > 50000:
        warnings.append(f"high usage ({poh:,} h) — consider replacing soon")

    if uncorr > 0 or pending > 5:
        level = "error"
        msg = "Critical: " + ", ".join(warnings)
    elif warnings:
        level = "warning"
        msg = "Warning: " + ", ".join(warnings)
    elif health == "UNKNOWN":
        level = "unknown"
        msg = "Could not read SMART data"
    else:
        level = "good"
        msg = "Good"

    return {"estimated_health": msg, "health_level": level}


def _parse_nvme(device: str, output: str) -> dict:
    info: dict = {"device": device, "drive_type": "NVMe", "nickname": _DRIVE_NICKNAMES.get(device)}

    m = re.search(r"Model Number:\s+(.+)", output)
    info["model"] = m.group(1).strip() if m else None

    m = re.search(r"SMART overall-health self-assessment test result:\s+(\w+)", output)
    info["overall_health"] = m.group(1) if m else "UNKNOWN"

    # NVMe uses its own field names
    m = re.search(r"Temperature:\s+(\d+)\s+Celsius", output)
    info["temperature_c"] = int(m.group(1)) if m else None

    m = re.search(r"Power On Hours:\s+([\d,]+)", output)
    info["power_on_hours"] = int(m.group(1).replace(",", "")) if m else None

    m = re.search(r"Percentage Used:\s+(\d+)%", output)
    info["percentage_used"] = int(m.group(1)) if m else None

    m = re.search(r"Available Spare:\s+(\d+)%", output)
    info["available_spare_pct"] = int(m.group(1)) if m else None

    m = re.search(r"Media and Data Integrity Errors:\s+(\d+)", output)
    info["media_errors"] = int(m.group(1)) if m else None

    m = re.search(r"Critical Warning:\s+(0x\w+|\d+)", output)
    crit_raw = m.group(1) if m else "0x00"
    try:
        info["critical_warning"] = int(crit_raw, 16) if crit_raw.startswith("0x") else int(crit_raw)
    except ValueError:
        info["critical_warning"] = 0

    m = re.search(r"Data Units Read:\s+([\.\d,]+)\s+\[([^\]]+)\]", output)
    info["data_read"] = m.group(2).strip() if m else None

    m = re.search(r"Data Units Written:\s+([\.\d,]+)\s+\[([^\]]+)\]", output)
    info["data_written"] = m.group(2).strip() if m else None

    m = re.search(r"Power Cycles:\s+([\d,]+)", output)
    info["power_cycles"] = int(m.group(1).replace(",", "")) if m else None

    m = re.search(r"Unsafe Shutdowns:\s+([\d,]+)", output)
    info["unsafe_shutdowns"] = int(m.group(1).replace(",", "")) if m else None

    m = re.search(r"Error Information Log Entries:\s+([\d,]+)", output)
    info["error_log_entries"] = int(m.group(1).replace(",", "")) if m else None

    poh = info.get("power_on_hours")
    info["age_years"] = round(poh / 8760, 1) if poh else None

    # Fields not applicable for NVMe
    info.update({
        "raw_read_errors": None,
        "reallocated_sectors": None,
        "pending_sectors": None,
        "uncorrectable_sectors": None,
        "spin_retry_count": None,
        "reported_uncorrectable": None,
    })

    info.update(_assess_nvme(info))
    return info


def _assess_nvme(info: dict) -> dict:
    health = info.get("overall_health") or "UNKNOWN"
    if health not in ("PASSED", "UNKNOWN"):
        return {"estimated_health": "FAILED — SMART reports failure", "health_level": "error"}

    crit = info.get("critical_warning") or 0
    spare = info.get("available_spare_pct")
    used_pct = info.get("percentage_used") or 0
    media_err = info.get("media_errors") or 0

    warnings = []
    if crit:
        warnings.append(f"critical warning flag 0x{crit:02x}")
    if spare is not None and spare < 10:
        warnings.append(f"available spare {spare}% (low)")
    if media_err > 0:
        warnings.append(f"{media_err} media error(s)")
    if used_pct >= 90:
        warnings.append(f"drive {used_pct}% worn")
    elif used_pct >= 70:
        warnings.append(f"drive {used_pct}% worn — monitor closely")

    if crit or media_err > 10:
        level = "error"
        msg = "Critical: " + ", ".join(warnings)
    elif warnings:
        level = "warning"
        msg = "Warning: " + ", ".join(warnings)
    elif health == "UNKNOWN":
        level = "unknown"
        msg = "Could not read SMART data (permission denied?)"
    else:
        level = "good"
        msg = "Good"

    return {"estimated_health": msg, "health_level": level}


# ---------------------------------------------------------------------------
# Disk space from psutil (covers /, data mounts, etc.)
# ---------------------------------------------------------------------------

def _get_disk_space() -> list[dict]:
    """Return used/available for meaningful storage partitions."""
    SKIP_FS = {"tmpfs", "devtmpfs", "devpts", "sysfs", "proc", "cgroup", "overlay", "squashfs"}
    KEEP_PREFIXES = ("/", "/mnt", "/media", "/data", "/home", "/opt")
    FILE_SUFFIXES = (".so", ".bin", ".conf", ".json", ".csv", ".txt", ".log", ".py", ".sh", ".yaml", ".yml")

    seen: set[str] = set()
    result = []
    for part in psutil.disk_partitions(all=False):
        mp = part.mountpoint
        if part.fstype in SKIP_FS:
            continue
        if any(mp.endswith(s) or f"{s}." in mp for s in FILE_SUFFIXES):
            continue
        if not any(mp == p or mp.startswith(p + "/") for p in KEEP_PREFIXES):
            continue
        if part.device and part.device in seen:
            continue
        try:
            u = psutil.disk_usage(mp)
            if part.device:
                seen.add(part.device)

            def _fmt(b: int) -> str:
                for unit, div in (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2)):
                    if b >= div:
                        return f"{b / div:.1f}{unit}"
                return f"{b}B"

            result.append({
                "path": mp,
                "used": _fmt(u.used),
                "available": _fmt(u.free),
                "total": _fmt(u.total),
                "percent": round(u.percent, 1),
            })
        except Exception:
            pass
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _scan_drives() -> list[dict]:
    drives = []
    for device in _find_drives():
        output = _run_smartctl(device)
        if output is None:
            drives.append({
                "device": device, "overall_health": "UNKNOWN",
                "estimated_health": "smartctl unavailable",
                "health_level": "unknown",
            })
            continue
        if "NVMe Version" in output or "NVMe Log" in output:
            drives.append(_parse_nvme(device, output))
        else:
            drives.append(_parse_sata(device, output))
    return drives


def _read_smart_file() -> tuple[list[dict], Optional[str]]:
    """Drives + last_run from the collector's file; a missing or stale file
    turns every drive "unknown" so the UI shows it (alerts only fire on
    "error", so a dead collector never pages as a drive failure)."""
    try:
        with open(SMART_FILE, encoding="utf-8") as f:
            payload = json.load(f)
        age = time.time() - os.path.getmtime(SMART_FILE)
    except (OSError, ValueError) as exc:
        logger.warning("SMART file %s unreadable: %s", SMART_FILE, exc)
        return [], None
    drives = payload.get("drives", [])
    if age > STALE_AFTER_SECONDS:
        msg = f"SMART data stale ({age / 3600:.0f} h old)"
        drives = [{**d, "health_level": "unknown", "estimated_health": msg} for d in drives]
    return drives, payload.get("last_run")


def get_drive_health() -> dict:
    """Scan drives with smartctl and return structured health data (1h cache)."""
    global _cache, _cache_ts

    now = time.monotonic()
    ttl = FILE_TTL_SECONDS if SMART_FILE else CACHE_TTL_SECONDS
    if _cache and (now - _cache_ts) < ttl:
        return _cache

    if SMART_FILE:
        drives, last_run = _read_smart_file()
    else:
        drives = _scan_drives()
        last_run = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

    data = {
        "available": True,
        "last_run": last_run,
        "disk_space": _get_disk_space(),
        "drives": drives,
    }

    _cache = data
    _cache_ts = now
    return _cache


def collect_forever(path: str) -> None:
    """Collector loop of the `smart` service: scan, write atomically, sleep."""
    while True:
        try:
            payload = {
                "last_run": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
                "drives": _scan_drives(),
            }
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
            logger.info("SMART scan written: %d drives", len(payload["drives"]))
        except Exception as exc:  # noqa: BLE001 — keep collecting
            logger.error("SMART scan failed: %s", exc)
        time.sleep(CACHE_TTL_SECONDS)


def get_drive_temperatures(refresh: bool = False) -> list[dict]:
    """Return per-drive temperatures derived from the cached SMART scan.

    Output: ``[{"name": "<nickname or device>", "temp": float, "category": "Storage"}, ...]``

    With ``refresh=True`` it (re)runs the scan when the cache is empty or stale
    (still subject to the 1 h TTL inside :func:`get_drive_health`). Callers on a
    request path should pass ``refresh=False`` so they never block on smartctl —
    they just get whatever is currently cached (possibly nothing on a cold start).
    """
    if refresh or not _cache:
        try:
            get_drive_health()
        except Exception as exc:  # never let a scan failure break temp collection
            logger.debug("get_drive_temperatures: SMART scan failed: %s", exc)

    entries: list[dict] = []
    for drive in (_cache.get("drives", []) if _cache else []):
        temp = drive.get("temperature_c")
        if temp is None:
            continue
        try:
            temp_f = float(temp)
        except (TypeError, ValueError):
            continue
        if temp_f <= 0 or temp_f > 120:  # discard obviously bogus readings
            continue
        name = drive.get("nickname") or (drive.get("device") or "drive").rsplit("/", 1)[-1]
        entries.append({
            "name": name,
            "temp": round(temp_f, 1),
            "category": "Storage",
            "drive_type": drive.get("drive_type"),  # "NVMe" or "SATA"
        })
    return entries


if __name__ == "__main__":
    import sys

    from ..logging_setup import setup_logging

    setup_logging()
    collect_forever(sys.argv[1])

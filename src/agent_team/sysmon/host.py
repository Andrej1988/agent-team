"""
Host machine metrics using psutil and /sys/class/thermal.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import psutil

from .drives import get_drive_temperatures

logger = logging.getLogger(__name__)

# Friendly names for mounted disks: AT_SYSMON_DISK_LABELS="/host-data=Daten,/mnt/x=Archiv"
_DISK_LABELS = dict(
    pair.split("=", 1) for pair in os.environ.get("AT_SYSMON_DISK_LABELS", "").split(",") if "=" in pair
)


def get_cpu() -> dict:
    return {
        "overall": round(psutil.cpu_percent(interval=0.5), 1),
        "per_core": [round(p, 1) for p in psutil.cpu_percent(interval=0.5, percpu=True)],
        "load_avg": [round(x, 2) for x in os.getloadavg()],
        "count_logical": psutil.cpu_count(logical=True),
        "count_physical": psutil.cpu_count(logical=False),
    }


def get_memory() -> dict:
    m = psutil.virtual_memory()
    return {
        "total_gb": round(m.total / 1024**3, 2),
        "used_gb": round(m.used / 1024**3, 2),
        "available_gb": round(m.available / 1024**3, 2),
        "percent": round(m.percent, 1),
        "cached_gb": round(getattr(m, "cached", 0) / 1024**3, 2),
    }


def get_disks() -> list[dict]:
    """Return disk usage for meaningful storage partitions only.

    Filters out individual file bind-mounts (e.g. nvidia libs, system config
    files) that appear in containerised environments.
    """
    # Mountpoint prefixes we care about
    KEEP_PREFIXES = (
        "/",
        "/data",
        "/mnt",
        "/media",
        "/home",
        "/opt",
        "/var",
        "/workspaces",
        "/app",
        "/vscode",
        "/tmp",
        "/host-data",
    )
    # Suffixes that indicate a single-file bind mount, not a directory
    FILE_SUFFIXES = (
        ".so", ".so.", ".bin", ".conf", ".json", ".csv",
        ".txt", ".log", ".py", ".sh", ".yaml", ".yml",
    )

    seen: set[str] = set()
    disks = []
    for part in psutil.disk_partitions(all=False):
        mp = part.mountpoint
        # Skip file bind-mounts
        if any(mp.endswith(suffix) or f"{suffix}." in mp for suffix in FILE_SUFFIXES):
            continue
        # Skip virtual / irrelevant filesystems
        if part.fstype in ("tmpfs", "devtmpfs", "devpts", "sysfs", "proc", "cgroup", "overlay"):
            continue
        # Only keep allowed prefixes
        if not any(mp == pfx or mp.startswith(pfx + "/") for pfx in KEEP_PREFIXES):
            continue
        # De-duplicate by device (same underlying storage, different mountpoints)
        if part.device and part.device in seen:
            continue
        try:
            usage = psutil.disk_usage(mp)
            if part.device:
                seen.add(part.device)
            disks.append(
                {
                    "mountpoint": mp,
                    "label": _DISK_LABELS.get(mp, "Host"),
                    "device": part.device,
                    "fstype": part.fstype,
                    "total_gb": round(usage.total / 1024**3, 2),
                    "used_gb": round(usage.used / 1024**3, 2),
                    "free_gb": round(usage.free / 1024**3, 2),
                    "percent": round(usage.percent, 1),
                }
            )
        except Exception:
            pass
    return disks


# ---------------------------------------------------------------------------
# Sensor name → (friendly_name, category) mapping
# ---------------------------------------------------------------------------
# Each entry: (chip_prefix_lower, label_substring_lower, friendly_name, category)
# Empty label_substring matches any label.
# Categories: CPU | Storage | GPU | Motherboard | System
_SENSOR_MAP: list[tuple[str, str, str, str]] = [
    # --- AMD CPU (k10temp) ---
    ("k10temp",   "tdie",      "CPU Die",              "CPU"),
    ("k10temp",   "tctl",      "CPU Temp",             "CPU"),
    ("k10temp",   "tccd",      "CPU Die",              "CPU"),   # tccd1/2 → "CPU Die 1/2"
    # --- Intel CPU (coretemp) ---
    ("coretemp",  "package",   "CPU Package",          "CPU"),
    ("coretemp",  "core",      "",                     "CPU"),   # "" → use label verbatim
    # --- Motherboard CPU sensor ---
    ("nct",       "cputin",    "CPU (Board Sensor)",   "CPU"),
    ("it8",       "cpu",       "CPU (Board Sensor)",   "CPU"),
    # --- NVMe drives ---
    ("nvme",      "composite", "NVMe SSD",             "Storage"),
    ("nvme",      "sensor 1",  "NVMe Sensor 1",        "Storage"),
    ("nvme",      "sensor 2",  "NVMe Sensor 2",        "Storage"),
    # --- SATA / spinning drives (drivetemp kernel module) ---
    ("drivetemp", "",          "Drive",                "Storage"),
    # --- Motherboard / Super-IO (NCT6xxx, IT87xx) ---
    # SYSTIN / IT87 temp1 ("System") deliberately omitted — on most boards it
    # reports a stub value (commonly stuck at 30 °C) and isn't actionable.
    ("nct",       "tsi0_temp", "CPU Platform Temp",    "Motherboard"),
    ("nct",       "auxtin0",   "Aux Sensor 1",         "Motherboard"),
    ("nct",       "auxtin1",   "Aux Sensor 2",         "Motherboard"),
    ("nct",       "auxtin2",   "Aux Sensor 3",         "Motherboard"),
    ("nct",       "auxtin3",   "Aux Sensor 4",         "Motherboard"),
    ("nct",       "auxtin4",   "Aux Sensor 5",         "Motherboard"),
    ("it8",       "temp2",     "Aux Sensor 1",         "Motherboard"),
    ("it8",       "temp3",     "Aux Sensor 2",         "Motherboard"),
    ("acpitz",    "",          "ACPI Zone",            "Motherboard"),
    ("thinkpad",  "",          "ThinkPad Zone",        "Motherboard"),
    # --- GPU ---
    ("amdgpu",    "edge",      "GPU Temp",             "GPU"),
    ("amdgpu",    "junction",  "GPU Junction",         "GPU"),
    ("amdgpu",    "mem",       "GPU Memory",           "GPU"),
    ("radeon",    "",          "GPU Temp",             "GPU"),
    ("nouveau",   "",          "GPU Temp",             "GPU"),
    ("nvidia",    "",          "GPU Temp",             "GPU"),
]

# Exact label names (lower-cased) to always skip — unpopulated / unreliable sensors
_SKIP_LABELS: frozenset[str] = frozenset({
    "pch_chip_temp", "pch_cpu_temp", "pch_mch_temp",
    "pch_chip_cpu_max_temp",
    "smbusmaster 0", "smbusmaster 1", "smbusmaster 2", "smbusmaster 3", "smbusmaster 4",
})


def _resolve_temp(chip: str, label: str, temp: float) -> dict | None:
    """Map a raw sensor reading to a friendly entry, or return None to skip it."""
    chip_l = chip.lower()
    label_l = (label or "").lower().strip()

    # Filter clearly invalid temperatures
    if temp < -40.0:
        return None

    # Skip known-bad labels
    if label_l in _SKIP_LABELS:
        return None

    for chip_prefix, lbl_match, fname, cat in _SENSOR_MAP:
        if not chip_l.startswith(chip_prefix.lower()):
            continue
        if lbl_match and lbl_match not in label_l:
            continue

        # Build friendly name
        friendly = fname

        # Intel Core N → "CPU Core 0", "CPU Core 1" …
        if chip_prefix == "coretemp" and lbl_match == "core" and fname == "":
            friendly = f"CPU {label}"

        # AMD Tccd1/Tccd2 → "CPU Die 1", "CPU Die 2" …
        if chip_prefix == "k10temp" and lbl_match == "tccd":
            suffix = label_l.replace("tccd", "")
            friendly = f"CPU Die {suffix}" if suffix.isdigit() else "CPU Die"

        # Multiple NVMe drives: nvme0 = Drive 1, nvme1 = Drive 2 …
        if chip_prefix == "nvme":
            idx_str = chip_l.replace("nvme", "").split("-")[0]
            if idx_str.isdigit() and int(idx_str) > 0:
                n = int(idx_str) + 1
                friendly = friendly.replace("NVMe SSD", f"NVMe SSD {n}").replace(
                    "NVMe Sensor", f"NVMe {n} Sensor"
                )

        # Drive temp: use label (model/name) as device identifier
        if chip_prefix == "drivetemp" and label:
            friendly = f"Drive ({label})"

        # Skip 0 °C for Motherboard sensors — almost always means unpopulated
        if cat == "Motherboard" and temp == 0.0:
            return None

        return {"name": friendly, "temp": round(temp, 1), "category": cat}

    # Sensor not in map — omit to keep output clean
    return None


def _short_gpu_name(name: str) -> str:
    # "NVIDIA GeForce <model>" -> "<model>"
    return name.replace("NVIDIA GeForce ", "").replace("NVIDIA ", "")


def _get_gpu_temps() -> list[dict]:
    """Query GPU temperatures via nvidia-smi if available."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,temperature.gpu", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        entries = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            try:
                idx, name, temp = parts[0], parts[1], float(parts[2])
                entries.append({
                    "name": f"{_short_gpu_name(name)} (GPU {idx})",
                    "temp": temp,
                    "category": "GPU",
                })
            except (ValueError, IndexError):
                pass
        return entries
    except (FileNotFoundError, Exception):
        return []


def get_gpus() -> list[dict]:
    """Query per-GPU utilization and VRAM usage via nvidia-smi.

    Returns ``[]`` when nvidia-smi is missing or fails — caller can treat that
    as "no NVIDIA GPUs visible to this container".
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        gpus = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5:
                continue
            try:
                idx = int(parts[0])
                name = _short_gpu_name(parts[1])
                util_pct = float(parts[2])
                mem_used_mb = float(parts[3])
                mem_total_mb = float(parts[4])
                mem_pct = round(mem_used_mb / mem_total_mb * 100, 1) if mem_total_mb else 0.0
                gpus.append({
                    "index": idx,
                    "name": name,
                    "util_pct": round(util_pct, 1),
                    "mem_used_mb": round(mem_used_mb, 1),
                    "mem_total_mb": round(mem_total_mb, 1),
                    "mem_pct": mem_pct,
                })
            except (ValueError, IndexError):
                pass
        return gpus
    except (FileNotFoundError, Exception):
        return []


def get_temperatures(allow_drive_scan: bool = False) -> list[dict]:
    """Return labelled, categorised temperature readings.

    Sources, in order: psutil hwmon sensors (CPU / motherboard / GPU / drives),
    nvidia-smi (GPU), and smartctl (SSD / HDD temps — see ``drive_health``). In
    containerised setups the kernel ``drivetemp`` / ``nvme`` hwmon nodes are
    often not exposed, so SMART is the reliable source for storage temps.

    ``allow_drive_scan`` lets the background sampler trigger a (1 h-cached) SMART
    scan; request handlers pass ``False`` so they never block on smartctl.
    """
    result: list[dict] = []
    storage_from_hwmon: list[dict] = []

    try:
        sensors = psutil.sensors_temperatures()
    except AttributeError:
        sensors = {}

    if sensors:
        for chip_name, readings in sensors.items():
            for r in readings:
                entry = _resolve_temp(chip_name, r.label or "", r.current)
                if not entry:
                    continue
                if entry["category"] == "Storage":
                    storage_from_hwmon.append(entry)
                else:
                    result.append(entry)

    # Append GPU temps from nvidia-smi
    result.extend(_get_gpu_temps())

    # Storage temps: prefer smartctl (covers every drive, friendly names);
    # fall back to whatever the kernel hwmon nodes exposed.
    drive_temps = get_drive_temperatures(refresh=allow_drive_scan)
    result.extend(drive_temps if drive_temps else storage_from_hwmon)

    if result:
        return result

    # Fallback: /sys/class/thermal (container bind-mount path)
    for thermal_root in (Path("/sys/thermal"), Path("/sys/class/thermal")):
        if not thermal_root.exists():
            continue
        for zone in sorted(thermal_root.glob("thermal_zone*")):
            try:
                raw_temp = int((zone / "temp").read_text().strip()) / 1000
                zone_type = (
                    (zone / "type").read_text().strip()
                    if (zone / "type").exists()
                    else zone.name
                )
                result.append({"name": zone_type, "temp": round(raw_temp, 1), "category": "System"})
            except Exception:
                pass
        if result:
            break

    return result


def get_network() -> dict:
    net = psutil.net_io_counters()
    return {
        "bytes_sent_mb": round(net.bytes_sent / 1024**2, 2),
        "bytes_recv_mb": round(net.bytes_recv / 1024**2, 2),
        "packets_sent": net.packets_sent,
        "packets_recv": net.packets_recv,
        "err_in": net.errin,
        "err_out": net.errout,
    }


def get_all() -> dict:
    return {
        "cpu": get_cpu(),
        "memory": get_memory(),
        "disks": get_disks(),
        "gpus": get_gpus(),
        "temperatures": get_temperatures(),
        "network": get_network(),
    }

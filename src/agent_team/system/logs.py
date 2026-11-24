"""Container log lines for display: credentials masked, Docker's UTC stamp
folded into one local clock, containers grouped by project."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..log_redaction import redact

_TZ = ZoneInfo(os.environ.get("TZ") or "Europe/Berlin")

# Docker's --timestamps prefix, e.g. "2026-05-12T06:07:00.649010543Z ".
_DOCKER_TS_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T(?P<time>\d{2}:\d{2}:\d{2})"
    r"(?:\.\d+)?(?P<offset>Z|[+-]\d{2}:\d{2})\s?(?P<rest>.*)$", re.DOTALL)
# The app already stamps its lines ("2026-05-12 06:07:00 | INFO", "[06:07:00]").
_APP_TS_RE = re.compile(r"^\[?(?:\d{4}-\d{2}-\d{2}[ T])?\d{2}:\d{2}:\d{2}\b")


def _local(date: str, time: str, offset: str) -> str:
    try:
        naive = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M:%S")
        if offset == "Z":
            tz = timezone.utc
        else:
            sign = 1 if offset[0] == "+" else -1
            tz = timezone(sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6])))
        return naive.replace(tzinfo=tz).astimezone(_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return f"{date} {time}"


def normalize(line: str) -> str:
    """Mask secrets; keep the app's own timestamp, else show Docker's in
    local time — one clock per line, all lines on the same timeline."""
    line = redact(line)
    m = _DOCKER_TS_RE.match(line)
    if not m:
        return line
    rest = m.group("rest")
    if _APP_TS_RE.match(rest):
        return rest
    return f"{_local(m.group('date'), m.group('time'), m.group('offset'))} {rest}"


# ---- grouping -----------------------------------------------------------------

OTHER = {"id": "other", "label": "Sonstige", "prefixes": []}


def groups(project_ids: list[tuple[str, str]]) -> list[dict]:
    """Log groups: AT_LOG_GROUPS (JSON list of {id, label, prefixes}) or one
    per agent-team project (container names starting with "<id>-"/"<id>_"),
    plus the agent team's own containers; everything else is "Sonstige"."""
    raw = os.environ.get("AT_LOG_GROUPS")
    if raw:
        try:
            out = [{"id": str(g["id"]), "label": str(g.get("label") or g["id"]),
                    "prefixes": [str(p) for p in g.get("prefixes") or []]} for g in json.loads(raw)]
            return out + [OTHER]
        except (ValueError, KeyError, TypeError):
            pass
    out = [{"id": pid, "label": label, "prefixes": [f"{pid}-", f"{pid}_"]} for pid, label in project_ids]
    out.append({"id": "agent-team", "label": "agent-team", "prefixes": ["agent-team-"]})
    return out + [OTHER]


def group_of(name: str, gs: list[dict]) -> str:
    for g in gs:
        if any(name.startswith(p) for p in g["prefixes"]):
            return g["id"]
    return OTHER["id"]


def container_rows(raw: list[dict], gs: list[dict]) -> list[dict]:
    rows = []
    for c in raw:
        name = ((c.get("Names") or ["?"])[0]).lstrip("/")
        labels = c.get("Labels") or {}
        rows.append({
            "name": name,
            "group": group_of(name, gs),
            "compose_project": labels.get("com.docker.compose.project"),
            "service": labels.get("com.docker.compose.service"),
            "state": c.get("State"),
            "status": c.get("Status"),
            "image": c.get("Image"),
        })
    return sorted(rows, key=lambda r: (r["compose_project"] or "~", r["name"]))

"""The user's decisions per project — so the lead and the scouts learn them.

Every tap on a lead / task-chat card (start, park, merge, delete, keep,
deploy, answer option) and every answer to a stage question lands as one
line in ``<project data>/control/decisions.jsonl``. The lead's daily run and
every scout get the recent ones in their prompt ("respect them": don't
re-propose what was parked or deleted, follow the user's answers).
The web app and agent-team-chat write (both mount the data dir), the
project's worker reads it as ``/control/decisions.jsonl``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

FILE = "decisions.jsonl"
KEEP = 2000


def record(control_dir: Path, text: str, **meta) -> None:
    control_dir.mkdir(parents=True, exist_ok=True)
    line = {"ts": time.strftime("%Y-%m-%d %H:%M"), "text": " ".join(text.split())[:500], **meta}
    with (control_dir / FILE).open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def recent(control_dir: Path, n: int = 60) -> list[dict]:
    try:
        rows = (control_dir / FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for raw in rows[-n:]:
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def prompt_section(control_dir: Path, n: int = 60) -> str:
    rows = recent(control_dir, n)
    body = "\n".join(f"- {r['ts']}: {r['text']}" for r in rows) or "(none yet)"
    return f"""
## The user's recent decisions — respect them
(Card taps and answers. Do not re-propose what the user parked, deleted or
merged unless something materially changed — then say what. Follow the
direction their answers set.)
{body}
"""

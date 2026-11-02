"""Per-channel event log — the transcript of one chat channel.

``<data>/chat/<channel>/log.jsonl``, one JSON event per line with a
monotonically increasing ``seq``. The chat service (agent-team-chat) is the
only writer; the web app reads it (history, SSE tail, inbox). Event types:

    user          {text, quote?, images?}       a message I sent
    assistant     {text}                        agent text
    tool_use      {name, text}                  "-> Bash(ls)"
    tool_result   {text, error}                 "<- …"
    bg            {text}                        background task event
    permission    {req_id, title, detail, rules, can_always}
    question      {req_id, questions: [{question, header, options, multi}]}
    resolved      {req_id, outcome}             answer to permission/question
    alert         {title?, text, source?}       posted by a service (notify API)
    system        {text}                        settings changes, info
    error         {text}
    busy          {busy: bool}                  turn started / ended
    turn_end      {}                            agent finished answering

The web app's read marker and the set of deleted events live next to it
(``read.json``, ``deleted.json`` — both web-owned; deleting only hides an
event, the transcript itself stays append-only).
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

from .. import projects

BERLIN = ZoneInfo("Europe/Berlin")
CHANNEL_RE_STR = r"^[a-z][a-z0-9-]{1,47}$"


def chat_root() -> Path:
    return projects.DATA_ROOT / "chat"


def channel_dir(channel: str) -> Path:
    return chat_root() / channel


class ChannelLog:
    """Append-only event log of one channel (single writer per process)."""

    def __init__(self, channel: str):
        self.channel = channel
        self.dir = channel_dir(channel)
        self.path = self.dir / "log.jsonl"
        self._lock = threading.Lock()
        self._seq: Optional[int] = None

    def _last_seq(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            lines = f.read().splitlines()
        for raw in reversed(lines):
            try:
                return int(json.loads(raw)["seq"])
            except Exception:
                continue
        return 0

    def append(self, type_: str, **data: Any) -> dict:
        with self._lock:
            if self._seq is None:
                self.dir.mkdir(parents=True, exist_ok=True)
                self._seq = self._last_seq()
            self._seq += 1
            ev = {"seq": self._seq, "ts": datetime.now(BERLIN).isoformat(timespec="seconds"),
                  "type": type_, **data}
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
            return ev


def read_events(channel: str, after: int = 0, limit: Optional[int] = None) -> list[dict]:
    """Events with seq > after (all of them, or the last ``limit``)."""
    path = channel_dir(channel) / "log.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for raw in f:
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if int(ev.get("seq", 0)) > after:
                out.append(ev)
    hidden = deleted_seqs(channel)
    if hidden:
        out = [e for e in out if e.get("seq") not in hidden]
    return out[-limit:] if limit else out


def iter_tail(channel: str, max_bytes: int = 2_000_000) -> Iterator[dict]:
    """The newest events (up to ~max_bytes of log), oldest first."""
    path = channel_dir(channel) / "log.jsonl"
    if not path.exists():
        return
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    lines = data.splitlines()
    if size > max_bytes:
        lines = lines[1:]  # first line is probably cut
    hidden = deleted_seqs(channel)
    for raw in lines:
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if ev.get("seq") not in hidden:
            yield ev


def open_requests(channel: str) -> list[dict]:
    """permission/question events without a matching ``resolved``."""
    pending: dict[str, dict] = {}
    for ev in iter_tail(channel):
        t = ev.get("type")
        if t in ("permission", "question"):
            pending[ev["req_id"]] = ev
        elif t == "resolved":
            pending.pop(ev.get("req_id"), None)
    return list(pending.values())


# ---- read marker (web-owned) -------------------------------------------------

def read_seq(channel: str) -> int:
    try:
        return int(json.loads((channel_dir(channel) / "read.json").read_text())["seq"])
    except Exception:
        return 0


def read_at(channel: str) -> float:
    """When the channel was last open in the app (unix time, 0 = never)."""
    try:
        return float(json.loads((channel_dir(channel) / "read.json").read_text()).get("at") or 0)
    except Exception:
        return 0.0


def mark_read(channel: str, seq: int) -> None:
    """Also called while the channel is merely open (same seq): ``at`` tells
    the inbox the user is looking, so it does not push replies."""
    d = channel_dir(channel)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ".read.json.tmp"
    tmp.write_text(json.dumps({"seq": max(int(seq), read_seq(channel)), "at": time.time()}))
    tmp.replace(d / "read.json")


# ---- archived marker (web-owned) ---------------------------------------------
# Hidden from the channel list (e.g. a finished task chat), transcript kept.

def archived(channel: str) -> bool:
    return (channel_dir(channel) / "archived.json").exists()


def set_archived(channel: str, on: bool) -> None:
    path = channel_dir(channel) / "archived.json"
    if on:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": time.time()}))
    else:
        path.unlink(missing_ok=True)


# ---- deleted events (web-owned) ----------------------------------------------

# What may be deleted: messages and notes. Approvals/questions only once
# resolved, so an open request can never vanish unanswered.
DELETABLE = frozenset({"alert", "assistant", "user", "error", "system", "bg", "tool_use", "tool_result"})
REQUESTS = frozenset({"permission", "question"})


def deleted_seqs(channel: str) -> frozenset[int]:
    try:
        return frozenset(int(x) for x in json.loads((channel_dir(channel) / "deleted.json").read_text()))
    except Exception:
        return frozenset()


def delete(channel: str, seqs: list[int] | None = None, all_alerts: bool = False,
           everything: bool = False) -> list[int]:
    """Hide events by seq (DELETABLE types, plus resolved REQUESTS);
    ``all_alerts`` hides every alert, ``everything`` every deletable event
    (open requests stay). Returns the requested seqs that are hidden now —
    also ones hidden earlier, so a client showing a stale copy drops them."""
    want = {int(x) for x in (seqs or [])}
    hidden = set(deleted_seqs(channel))
    # A tap record (command) may go — except in service channels: there it
    # keeps the card's buttons disabled, and a re-enabled order button could
    # send the same order twice.
    from .. import projects
    try:
        commands_ok = not projects.get_channel(channel).service
    except projects.UnknownProject:
        commands_ok = False
    already = [s for s in want if s in hidden]
    resolved = {ev.get("req_id") for ev in iter_tail(channel, max_bytes=50_000_000) if ev.get("type") == "resolved"}
    newly = []
    for ev in iter_tail(channel, max_bytes=50_000_000):
        seq = ev.get("seq")
        kind = ev.get("type")
        if kind not in DELETABLE and not (kind in REQUESTS and ev.get("req_id") in resolved) \
                and not (kind == "command" and commands_ok):
            continue
        if seq in hidden:
            continue
        if everything or seq in want or (all_alerts and kind == "alert"):
            hidden.add(seq)
            newly.append(seq)
    if newly:
        d = channel_dir(channel)
        tmp = d / ".deleted.json.tmp"
        tmp.write_text(json.dumps(sorted(hidden)))
        tmp.replace(d / "deleted.json")
    return sorted(newly + already)

"""Minimal client for a read-only Docker API proxy (AT_DOCKER_RO_URL).

The proxy passes only GET of the container list, logs, stats, df and
version — never inspect (a container's inspect carries its environment,
i.e. API keys). So nothing here may call /containers/<id>/json.
"""

from __future__ import annotations

import os
import re
from typing import AsyncIterator, Optional

import httpx

NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")


class NotConfigured(RuntimeError):
    pass


def base_url() -> str:
    url = (os.environ.get("AT_DOCKER_RO_URL") or "").rstrip("/")
    if not url:
        raise NotConfigured("AT_DOCKER_RO_URL ist nicht gesetzt — Docker-Überwachung nicht eingerichtet")
    return url


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or ""))


def _client(timeout: Optional[float] = 10.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url(), timeout=timeout)


async def containers() -> list[dict]:
    """All containers (running and stopped), as the list endpoint gives them."""
    async with _client() as c:
        r = await c.get("/containers/json", params={"all": "1"})
        r.raise_for_status()
        return r.json()


class Demux:
    """Splits Docker's log stream into lines. Non-TTY containers send 8-byte
    framed chunks (stream id, 0,0,0, big-endian length); TTY containers send
    raw text. Without inspect we cannot ask which, so the first bytes decide."""

    def __init__(self) -> None:
        self.buf = b""
        self.framed: Optional[bool] = None
        self.text = b""

    def feed(self, data: bytes) -> list[str]:
        self.buf += data
        if self.framed is None and len(self.buf) >= 8:
            self.framed = self.buf[0] in (0, 1, 2) and self.buf[1:4] == b"\0\0\0"
        if self.framed is None:
            return []
        if self.framed:
            while len(self.buf) >= 8:
                size = int.from_bytes(self.buf[4:8], "big")
                if len(self.buf) < 8 + size:
                    break
                self.text += self.buf[8:8 + size]
                self.buf = self.buf[8 + size:]
        else:
            self.text += self.buf
            self.buf = b""
        *lines, self.text = self.text.split(b"\n")
        return [ln.decode("utf-8", "replace").rstrip("\r") for ln in lines]

    def flush(self) -> list[str]:
        if self.framed is None:  # fewer than 8 bytes: plain text
            self.text += self.buf
            self.buf = b""
        rest, self.text = self.text, b""
        return [rest.decode("utf-8", "replace")] if rest.strip() else []


async def logs(name: str, tail: int = 200) -> list[str]:
    if not valid_name(name):
        raise ValueError("ungültiger Containername")
    async with _client(timeout=20.0) as c:
        r = await c.get(f"/containers/{name}/logs",
                        params={"stdout": "1", "stderr": "1", "timestamps": "1", "tail": str(tail)})
        if r.status_code == 404:
            raise LookupError(f"Container {name} nicht gefunden")
        r.raise_for_status()
        d = Demux()
        return d.feed(r.content) + d.flush()


async def follow(name: str, tail: int = 0) -> AsyncIterator[str]:
    """Live lines of a container until the client goes away."""
    if not valid_name(name):
        raise ValueError("ungültiger Containername")
    async with _client(timeout=None) as c:
        async with c.stream("GET", f"/containers/{name}/logs",
                            params={"stdout": "1", "stderr": "1", "timestamps": "1",
                                    "follow": "1", "tail": str(tail)}) as r:
            if r.status_code == 404:
                raise LookupError(f"Container {name} nicht gefunden")
            r.raise_for_status()
            d = Demux()
            async for chunk in r.aiter_raw():
                for line in d.feed(chunk):
                    yield line
            for line in d.flush():
                yield line

"""Short-lived browser login (`claude auth login`) driven by the web app.

agent-team-chat runs it; it shares the Claude login volume with the
workers.

Only the authorization URL and fixed status messages leave this process.
CLI output and submitted codes must never be logged or retained in results.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import re
import signal
import struct
import termios
import time
from pathlib import Path
from urllib.parse import urlsplit

AUTH_ERROR = "Claude-Anmeldung abgelaufen oder ungültig. Bitte Claude erneut anmelden und danach den fehlgeschlagenen Lauf wiederholen."


def is_auth_error(text: str) -> bool:
    text = text.lower()
    return any(phrase in text for phrase in (
        "oauth session expired", "oauth token has expired", "oauth token expired",
        "failed to authenticate", "invalid authentication credentials",
        "claude-anmeldung abgelaufen oder ungültig",
    ))


def authorization_url(output: str) -> str | None:
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    for url in re.findall(r"https://[^\s\x00-\x20<>]+", output):
        parsed = urlsplit(url)
        allowed = ((parsed.hostname in {"claude.ai", "console.anthropic.com", "platform.claude.com"}
                    and parsed.path == "/oauth/authorize")
                   or (parsed.hostname == "claude.com" and parsed.path == "/cai/oauth/authorize"))
        if (allowed and not parsed.username
                and parsed.port in {None, 443}):
            return url
    return None


def write_private(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp, path)


async def login(control: Path, request_id: str, timeout: float = 600,
                startup_timeout: float = 30) -> None:
    result = control / "claude-login.result.json"
    code_path = control / "claude-login.code"
    cancel_path = control / "claude-login.cancel"
    expires_at = time.time() + timeout
    startup_deadline = time.monotonic() + startup_timeout

    def publish(status: str, message: str, url: str | None = None):
        write_private(result, {"id": request_id, "status": status,
                              "message": message, "url": url, "expires_at": expires_at})

    master, slave = pty.openpty()
    # Disable code echo and prevent terminal wrapping of the authorization URL.
    attrs = termios.tcgetattr(slave)
    attrs[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 4096, 0, 0))
    os.set_blocking(master, False)
    proc = None
    try:
        publish("starting", "Claude-Anmeldung wird gestartet …")
        env = {**os.environ, "BROWSER": "/bin/true"}
        proc = await asyncio.create_subprocess_exec(
            "claude", "auth", "login", "--claudeai", stdin=slave, stdout=slave,
            stderr=slave, env=env, start_new_session=True,
        )
        os.close(slave)
        slave = -1
        output = ""
        url = None
        while time.time() < expires_at:
            if cancel_path.exists():
                cancel = json.loads(cancel_path.read_text())
                cancel_path.unlink(missing_ok=True)
                if cancel.get("id") == request_id:
                    publish("cancelled", "Anmeldung abgebrochen.")
                    return
            try:
                chunk = os.read(master, 16384)
                output = (output + chunk.decode("utf-8", "replace"))[-65536:]
            except (BlockingIOError, OSError):
                pass
            found = authorization_url(output)
            if found and url is None:
                url = found
                publish("awaiting_code", "Im Browser anmelden und den Bestätigungscode hier eingeben.", url)
            if url is None and time.monotonic() >= startup_deadline:
                publish("failed", "Claude hat keinen Anmeldelink bereitgestellt. Bitte erneut starten.")
                return
            if code_path.exists():
                submission = json.loads(code_path.read_text())
                code_path.unlink(missing_ok=True)
                code = submission.get("code", "")
                if (submission.get("id") == request_id and url
                        and isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_#=./+:-]{1,2048}", code)):
                    os.write(master, (code + "\n").encode())
                    publish("verifying", "Anmeldung wird geprüft …")
            if proc.returncode is not None:
                if proc.returncode == 0:
                    # Exit 0 alone is insufficient: confirm saved credentials.
                    probe = await asyncio.create_subprocess_exec(
                        "claude", "auth", "status", stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    try:
                        stdout, _ = await asyncio.wait_for(probe.communicate(), 15)
                        authenticated = probe.returncode == 0 and json.loads(stdout).get("loggedIn") is True
                    except (asyncio.TimeoutError, ValueError):
                        if probe.returncode is None:
                            probe.kill()
                            await probe.wait()
                        authenticated = False
                    if authenticated:
                        publish("success", "Claude ist wieder angemeldet.")
                        return
                publish("failed", "Claude-Anmeldung fehlgeschlagen. Bitte erneut starten.")
                return
            await asyncio.sleep(0.2)
        publish("expired", "Anmeldelink abgelaufen. Bitte die Anmeldung erneut starten.")
    except Exception:
        # Never persist exception text: auth responses can contain credentials.
        publish("failed", "Claude-Anmeldung konnte nicht abgeschlossen werden. Bitte erneut starten.")
    finally:
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
        os.close(master)
        if slave >= 0:
            os.close(slave)
        code_path.unlink(missing_ok=True)
        cancel_path.unlink(missing_ok=True)

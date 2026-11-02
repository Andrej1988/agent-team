#!/usr/bin/env python3
"""Model catalog of the two agent CLIs installed in this image.

The Tasks UI used to ship a hard-coded model list, so
every new Claude / GPT generation had to be typed into ``shared.tsx`` by
hand. Both CLIs know their own catalog, so ask them instead:

* **Claude Code** answers the ``list_models`` control request over its
  stream-json protocol — the same catalog the interactive ``/model`` picker
  shows, including alias resolution (``opus`` → ``claude-opus-5-5``) and
  entries the installed CLI is too old for (``disabled: true`` with an
  "Update to 2.1.280+" hint). No prompt is sent, so no tokens are spent.
* **Codex** persists the server-side catalog it fetched on its last run in
  ``$CODEX_HOME/models_cache.json`` (slug, display name, visibility,
  priority). We read that file; ``visibility == "list"`` are the entries
  the ``/model`` picker offers.

On top, the npm registry is asked for the latest published CLI versions so
the UI can say "rebuild the bridge image" when a model is gated behind a
newer CLI. Every source fails soft — the caller gets ``error`` strings and
empty lists rather than an exception, and the UI keeps its static fallback.

Usage: ``python3 model_catalog.py`` prints the catalog as JSON. The
monitoring backend runs exactly that through ``docker exec`` in the bridge
container (``routers/tasks.py`` → ``GET /api/tasks/models``).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# The entrypoints upgrade both CLIs into ~/.npm-global (cli-upgrade.sh) and
# prepend that to PATH for the processes they start. `docker exec` (how the
# monitoring backend runs this script) does NOT inherit that PATH, so
# without this we'd report the image-baked CLI instead of the one the
# worker actually spawns.
_USER_BIN = Path(os.environ.get("NPM_CONFIG_PREFIX") or (Path.home() / ".npm-global")) / "bin"
if _USER_BIN.is_dir() and str(_USER_BIN) not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = f"{_USER_BIN}{os.pathsep}{os.environ.get('PATH', '')}"

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
CLAUDE_NPM_PKG = "@anthropic-ai/claude-code"
CODEX_NPM_PKG = "@openai/codex"

# Claude Code alias entries the picker offers (``value`` field). Anything
# else in the response is a concrete model id (possibly with a ``[1m]``
# context suffix) or a placeholder such as ``cc-update-required-1``.
_CLAUDE_ALIASES = ("default", "opus", "sonnet", "haiku", "fable")


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

def _run_version(bin_: str, timeout: float = 15.0) -> Optional[str]:
    """``<bin> --version`` → bare version string ("2.1.281"), None if the
    binary is missing or prints something we can't parse."""
    if shutil.which(bin_) is None:
        return None
    try:
        out = subprocess.run(
            [bin_, "--version"], capture_output=True, text=True, timeout=timeout,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+\.\d+\.\d+)", out or "")
    return m.group(1) if m else None


def _claude_list_models_raw(timeout: float = 40.0) -> dict[str, Any]:
    """Spawn the CLI in stream-json mode and send the ``list_models``
    control request. Returns the raw ``response`` dict.

    ``-p`` without a prompt keeps the process waiting on stdin; the control
    request is answered without any model call. ``--permission-mode plan``
    is belt-and-braces so nothing could run even if a prompt slipped in.
    cwd is $HOME so project-level hooks/settings of a mounted workspace
    don't fire for what is a pure catalog query."""
    if shutil.which(CLAUDE_BIN) is None:
        raise FileNotFoundError(f"{CLAUDE_BIN} not on PATH")
    cmd = [
        CLAUDE_BIN, "--input-format", "stream-json", "--output-format", "stream-json",
        "--verbose", "-p", "--permission-mode", "plan",
    ]
    proc = subprocess.Popen(
        cmd, cwd=str(Path.home()), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    request_id = str(uuid.uuid4())
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps({
            "type": "control_request", "request_id": request_id,
            "request": {"subtype": "list_models"},
        }) + "\n")
        proc.stdin.flush()
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("type") != "control_response":
                continue
            resp = msg.get("response") or {}
            if resp.get("request_id") not in (None, request_id):
                continue
            if resp.get("subtype") == "error":
                raise RuntimeError(resp.get("error") or "list_models failed")
            return resp.get("response") or {}
        err = (proc.stderr.read() if proc.stderr else "") or ""
        raise RuntimeError(
            "claude exited without answering list_models"
            + (f": {err.strip()[:300]}" if err.strip() else "")
        )
    finally:
        timer.cancel()
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait(timeout=5)


def parse_claude_models(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise the ``list_models`` response into picker entries.

    * one entry per *resolved* model id (aliases fold into it and are listed
      in ``aliases`` so the UI can show "opus → Opus 5.5");
    * placeholder entries (``disabled: true``) are kept with an empty
      ``value`` and their hint, so the UI can grey them out and explain
      *why* a model is missing (usually: CLI too old);
    * ``default_resolved`` = what the bare CLI default runs on right now.
    """
    entries: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    disabled: list[dict[str, Any]] = []
    aliases: dict[str, str] = {}
    default_resolved: Optional[str] = None
    for m in raw.get("models") or []:
        if not isinstance(m, dict):
            continue
        value = str(m.get("value") or "").strip()
        resolved = str(m.get("resolvedModel") or "").strip()
        display = str(m.get("displayName") or "").strip()
        desc = str(m.get("description") or "").strip()
        if m.get("disabled"):
            disabled.append({
                "value": "", "label": display or resolved or value, "description": desc,
                "disabled": True, "hint": desc, "backend": "claude",
            })
            continue
        if not resolved:
            continue
        if value == "default":
            default_resolved = resolved
            continue
        # "claude-fable-5-1[1m]" / resolved "claude-opus-5-5[1m]": the [1m]
        # suffix is the CLI's 1M-context flavour. Keep the resolved id exactly
        # as reported (that is what `--model` gets), but expose the bare id
        # as an alias so task rows that stored "claude-opus-5-5" still map
        # onto this entry in the UI.
        bare = re.sub(r"\[.*?\]$", "", value)
        resolved_bare = re.sub(r"\[.*?\]$", "", resolved)
        if value in _CLAUDE_ALIASES:
            aliases[value] = resolved
        # Older CLIs: description "Opus 5.5 · Best for everyday, complex
        # tasks" (generation name before the separator). Since ~2.1.285 the
        # name sits in displayName and the description is only the blurb.
        gen, sep, tail = desc.partition("·")
        if sep:
            label = gen.strip() or display or resolved
        else:
            label, tail = display or resolved, desc
        entry = entries.get(resolved)
        if entry is None:
            entry = {
                "value": resolved, "label": label, "description": tail.strip(),
                "disabled": False, "hint": "", "backend": "claude", "aliases": [],
            }
            entries[resolved] = entry
            order.append(resolved)
        if value in _CLAUDE_ALIASES:
            entry["aliases"].append(value)
        elif bare != resolved and bare not in entry["aliases"]:
            entry["aliases"].append(bare)
        if resolved_bare != resolved and resolved_bare not in entry["aliases"]:
            entry["aliases"].append(resolved_bare)
    return {
        "models": [entries[k] for k in order] + disabled,
        "aliases": aliases,
        "default_resolved": default_resolved,
    }


def claude_catalog() -> dict[str, Any]:
    out: dict[str, Any] = {
        "cli_version": _run_version(CLAUDE_BIN), "models": [], "aliases": {},
        "default_resolved": None, "error": None,
    }
    try:
        out.update(parse_claude_models(_claude_list_models_raw()))
    except Exception as exc:  # noqa: BLE001 — fail soft, report in payload
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

def codex_cache_path() -> Path:
    home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return Path(home) / "models_cache.json"


def parse_codex_cache(raw: dict[str, Any]) -> dict[str, Any]:
    """``models_cache.json`` → picker entries (listed models only, in the
    server's priority order)."""
    models: list[dict[str, Any]] = []
    for m in raw.get("models") or []:
        if not isinstance(m, dict) or m.get("visibility") != "list":
            continue
        slug = str(m.get("slug") or "").strip()
        if not slug:
            continue
        level = m.get("default_reasoning_level")
        models.append({
            "value": slug,
            "label": str(m.get("display_name") or slug),
            "description": f"default reasoning: {level}" if level else "",
            "disabled": False, "hint": "", "backend": "codex", "aliases": [],
            "priority": m.get("priority"),
        })
    models.sort(key=lambda e: (e["priority"] is None, e["priority"] or 0))
    for e in models:
        e.pop("priority", None)
    return {"models": models, "fetched_at": raw.get("fetched_at")}


def codex_catalog() -> dict[str, Any]:
    out: dict[str, Any] = {
        "cli_version": _run_version(CODEX_BIN), "models": [], "fetched_at": None,
        "error": None,
    }
    path = codex_cache_path()
    try:
        out.update(parse_codex_cache(json.loads(path.read_text(encoding="utf-8"))))
    except FileNotFoundError:
        out["error"] = f"{path} missing — run any `codex exec` once so the CLI caches its model list"
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# ---------------------------------------------------------------------------
# npm registry — is a newer CLI published?
# ---------------------------------------------------------------------------

def npm_latest(pkg: str, timeout: float = 5.0) -> Optional[str]:
    url = f"https://registry.npmjs.org/{pkg.replace('/', '%2F')}/latest"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return (json.load(r) or {}).get("version")
    except Exception:  # noqa: BLE001 — offline / registry hiccup = unknown
        return None


def _vtuple(v: Optional[str]) -> tuple[int, ...]:
    if not v:
        return ()
    return tuple(int(p) for p in re.findall(r"\d+", v)[:3])


def update_available(installed: Optional[str], latest: Optional[str]) -> bool:
    return bool(installed and latest and _vtuple(latest) > _vtuple(installed))


# ---------------------------------------------------------------------------

def build(check_npm: bool = True) -> dict[str, Any]:
    claude = claude_catalog()
    codex = codex_catalog()
    if check_npm:
        claude["latest_version"] = npm_latest(CLAUDE_NPM_PKG)
        codex["latest_version"] = npm_latest(CODEX_NPM_PKG)
    else:
        claude["latest_version"] = codex["latest_version"] = None
    claude["update_available"] = update_available(claude["cli_version"], claude["latest_version"])
    codex["update_available"] = update_available(codex["cli_version"], codex["latest_version"])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "claude": claude,
        "codex": codex,
    }


if __name__ == "__main__":
    json.dump(build(check_npm="--no-npm" not in sys.argv), sys.stdout, indent=1)
    sys.stdout.write("\n")

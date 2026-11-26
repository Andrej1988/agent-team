"""Guard for the persistent browser's tools (``mcp__browser__*``).

The browser is logged in to the user's accounts, so anything that acts in
their name — posting, commenting, voting, sending, signing up, accepting,
uploading — needs the user's approval for that one action. Tools that could
get around the check or read credentials are refused outright.

- chat: a PreToolUse hook asks the user (the chat's approval card) — see
  ``chat/runtime.py``;
- unattended runs (scouts, ``claude -p``): ``python -m agent_team.browser_guard``
  as a command hook refuses every action that would need an approval.

The click check reads the element description the agent passes, so it is a
safety net, not proof: the prompt rules (explicit approval in the chat) stay
the primary control.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from typing import Optional

MATCHER = r"mcp__browser__.*"
PREFIX = "mcp__browser__"

# Arbitrary page JS could submit forms or read cookies; network details carry
# cookies and auth headers; drop moves local files into the page.
DENY = {
    "browser_evaluate": "beliebiges JavaScript im Browser",
    "browser_run_code_unsafe": "beliebiger Playwright-Code",
    "browser_network_request": "Netzwerk-Details (Cookies, Header)",
    "browser_network_requests": "Netzwerk-Details (Cookies, Header)",
    "browser_drop": "Dateien in die Seite ziehen",
}

SUBMIT_RE = re.compile(
    r"(post|absend|senden|\bsend|submit|veröffentlich|publish|tweet|reply|antwort|kommentier|comment"
    r"|registrier|sign ?up|create account|konto erstellen|bestätig|confirm|akzeptier|accept|zustimm|agree"
    r"|vote|abstimm|\blike|gefällt|folgen|follow|beitreten|\bjoin|abonnier|subscribe|kaufen|\bbuy|bestell"
    r"|\border|bezahl|\bpay|lösch|delete|teilen|share|repost|retweet|melden|report|einreichen)",
    re.IGNORECASE)
ENTER_RE = re.compile(r"(^|\+)(enter|return)$", re.IGNORECASE)


# While the user has the live view open, the agent may only look.
LOOK_ONLY = {"browser_snapshot", "browser_take_screenshot", "browser_find", "browser_wait_for",
             "browser_console_messages"}
VIEW_FRESH_S = 30


def _view_flag(project: str):
    from . import projects
    return projects.DATA_ROOT / "browser" / f"{project}.viewing"


def set_viewing(project: str, on: bool) -> None:
    """Called by the live view's socket (touch every few seconds / remove)."""
    f = _view_flag(project)
    if on:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.touch()
    else:
        f.unlink(missing_ok=True)


def user_viewing(project: str) -> bool:
    import time
    try:
        return time.time() - _view_flag(project).stat().st_mtime < VIEW_FRESH_S
    except OSError:
        return False


def busy_reason(tool: str, project: str) -> Optional[str]:
    """Refusal while the user drives the browser (input lock)."""
    if tool.startswith(PREFIX) and tool.removeprefix(PREFIX) not in LOOK_ONLY and user_viewing(project):
        return ("Der Nutzer bedient den Browser gerade selbst (Live-Ansicht offen). Nicht eingreifen — "
                "warte, bis er im Chat „fertig“ schreibt; nur ansehen (Snapshot/Screenshot) ist erlaubt.")
    return None


def classify(tool: str, inp: dict) -> tuple[Optional[str], str]:
    """(None | "deny" | "ask", German label of the action) for one tool call."""
    name = tool.removeprefix(PREFIX)
    if not tool.startswith(PREFIX):
        return None, ""
    if name in DENY:
        return "deny", DENY[name]
    element = str(inp.get("element") or "")
    if name == "browser_click" and SUBMIT_RE.search(element):
        return "ask", f"Klick auf „{element}“"
    if name == "browser_type" and inp.get("submit"):
        return "ask", f"Text in „{element}“ eingeben und absenden"
    if name == "browser_press_key" and ENTER_RE.search(str(inp.get("key") or "").strip()):
        return "ask", f"Taste {inp.get('key')} (kann absenden)"
    if name == "browser_file_upload":
        return "ask", "Datei hochladen: " + ", ".join(map(str, inp.get("paths") or []))
    if name == "browser_handle_dialog" and inp.get("accept"):
        return "ask", "Dialog bestätigen" + (f" ({inp.get('promptText')})" if inp.get("promptText") else "")
    return None, ""


def typed_text(tool: str, inp: dict) -> Optional[str]:
    """Text the agent puts into the page (shown with the next approval)."""
    name = tool.removeprefix(PREFIX)
    if name == "browser_type":
        return f"{inp.get('element') or 'Feld'}: {inp.get('text') or ''}"
    if name == "browser_fill_form":
        return "\n".join(f"{f.get('name') or 'Feld'}: {f.get('value') or ''}"
                         for f in inp.get("fields") or [] if isinstance(f, dict))
    return None


def deny_output(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def headless_reason(tool: str, inp: dict) -> Optional[str]:
    kind, label = classify(tool, inp)
    if kind == "deny":
        return f"Blockiert: {label} ist im angemeldeten Browser nicht erlaubt."
    if kind == "ask":
        return (f"Blockiert: {label} handelt im Namen des Nutzers und braucht seine Freigabe — "
                "in einem unbeaufsichtigten Lauf nicht möglich. Schlage es im Bericht vor.")
    return None


def cli_settings_hook() -> dict:
    """PreToolUse entry for a ``claude -p`` --settings JSON."""
    return {"matcher": MATCHER,
            "hooks": [{"type": "command", "command": f"{shlex.quote(sys.executable)} -m agent_team.browser_guard"}]}


def main() -> None:
    try:
        d = json.load(sys.stdin)
    except ValueError:
        return
    why = headless_reason(d.get("tool_name") or "", d.get("tool_input") or {})
    if why:
        print(json.dumps(deny_output(why)))


if __name__ == "__main__":
    main()

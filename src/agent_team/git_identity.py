"""Git identity per agent, so `git log` shows who did what.

Every agent session gets GIT_AUTHOR_* / GIT_COMMITTER_* in its environment
(git prefers these over any config), so a commit made by a chat channel or a
pipeline stage carries that agent's name — no git config is touched. The
addresses are under a subdomain without mailboxes; they only label commits.

``AT_GIT_IDENTITY=user`` (work laptop: GitHub Enterprise accepts only known
addresses) sets nothing: commits carry the user's own git config. The
worker's stage commits still name the role in the message. ``AT_GIT_DOMAIN``
changes the agents' mail domain.
"""

from __future__ import annotations

import os
import re

DOMAIN = os.environ.get("AT_GIT_DOMAIN") or "agents.invalid"
MODE = (os.environ.get("AT_GIT_IDENTITY") or "agents").strip().lower()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9.-]+", "-", text.lower()).strip("-.") or "agent"


def env(name: str, mailbox: str) -> dict[str, str]:
    if MODE == "user":
        return {}
    email = f"{_slug(mailbox)}@{DOMAIN}"
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}


def chat(channel_id: str) -> dict[str, str]:
    """A chat channel's agent, e.g. "shop-backend (Chat-Agent)"."""
    return env(f"{channel_id} (Chat-Agent)", f"chat.{channel_id}")


def stage(project: str, role: str) -> dict[str, str]:
    """A pipeline stage's agent, e.g. "shop data-engineer (Agent)"."""
    return env(f"{project} {role} (Agent)", f"{project}.{role}")

"""System-prompt notes shared by chat channels and task agents."""

# The CLI injects reminders about claude.ai connectors / plugin MCP servers that
# need a login or failed to connect, and the global session hooks ask every
# agent to update SESSION.md. Relayed in every reply, both are just noise for
# the user reading on a phone.
QUIET_NOTE = (
    "\n\nStatus noise — do not relay it:\n"
    "  - Never tell the user which MCP servers or connectors need authentication, "
    "are still connecting or failed to connect, unless the user's current request "
    "actually needs one of them.\n"
    "  - If you cannot update SESSION.md (e.g. you have no write tools), skip it "
    "silently — don't mention it."
)

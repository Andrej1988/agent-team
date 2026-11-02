"""Credential redaction for log output.

Self-contained (stdlib only) so other repos can vendor a copy.

``install()`` wraps the process-wide ``logging`` record factory, so every
record -- any logger, any handler, including third-party libraries that log
full request URLs (httpx "HTTP Request: POST https://api.telegram.org/bot<token>/...",
urllib3/requests "Max retries exceeded with url: /query?...&apikey=...") -- has
credentials masked in its message, exception text and stack info before any
handler formats it. It also redacts uncaught-exception tracebacks printed by
``sys.excepthook`` / ``threading.excepthook``.

``redact()`` is for text that leaves the process outside logging: DB error
columns, alert bodies, log files read back by a consumer.

CLI: ``python log_redaction.py`` filters stdin to stdout line by line;
``python log_redaction.py --in-place FILE...`` scrubs existing files.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
import traceback

REDACTED = "<redacted>"

# One character of a credential value. Excludes whitespace, quotes, separators
# and backslash, so a match never swallows a JSON escape or a closing quote.
_V = r"[^\s\"'`,;&#<>(){}\[\]\\|]"

# An optional quote around a name or value, also JSON-escaped (\" inside a JSON string).
_Q = r"(?:\\?[\"'])?"

# Names whose value is a credential wherever they appear as name=value /
# name: value (free text, dict reprs, JSON, HTTP headers, env dumps).
_NAMES = (
    r"api[_-]?key|api[_-]?secret|secret[_-]?key|access[_-]?key|"
    r"access[_-]?token|auth[_-]?token|bot[_-]?token|oauth[_-]?token|"
    r"refresh[_-]?token|id[_-]?token|client[_-]?secret|private[_-]?key|"
    r"password|passwd|passphrase"
)
# Values that are not secrets and are worth keeping readable: placeholders,
# already-masked values ("PKQB***"), code as it shows up in traceback source
# lines and config dumps (`password = os.getenv(...)`, `api_key: Optional[str]`,
# `self.api_key = api_key or settings.X`), env-var names (`api_key_env:
# ALPACA_API_KEY`) and capitalised prose ("DB_PASSWORD: Database password").
# Real keys mix digits into letters, which none of these shapes allow.
_NOT_A_VALUE = (
    r"(?!(?:none|null|nil|true|false|missing|unset|empty|not)\b)"
    rf"(?!{_V}*\*\*\*)"
    r"(?![\w.]+[\[(])"
    r"(?!(?-i:[a-z_]+(?:\.[a-z_]+)*|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|[A-Z][a-z]+)\b(?![-/+=]))"
)

# (needles, pattern, replacement). A pattern only runs when one of its
# lower-case needles occurs in the lower-cased text -- most log lines contain
# none, which keeps redaction at a few microseconds per record.
_PATTERNS: list[tuple[tuple[str, ...], re.Pattern[str], str]] = [
    # Telegram bot token, e.g. https://api.telegram.org/bot<id>:<secret>/getUpdates
    (("bot", "token", "telegram", ":aa"),
     re.compile(r"(?<![0-9])[0-9]{6,12}:[A-Za-z0-9_-]{30,}"), REDACTED),
    # Password in URL userinfo: postgresql://user:<password>@host
    (("://",),
     re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s/?#@:\"'\\{}]+:)[^\s/?#@\"'\\{}]+@"), rf"\1{REDACTED}@"),
    # Credential query/form parameters: ?apikey=... &token=... &signature=...
    (("key=", "token=", "secret=", "sig=", "sign=", "signature=", "auth=", "passw", "passphrase="),
     re.compile(rf"(?i)([?&](?:{_NAMES}|apikey|key|token|secret|signature|sign|sig|auth)=){_V}+"),
     rf"\1{REDACTED}"),
    # name=value / name: value for unambiguous credential names, incl. header
    # names (X-MBX-APIKEY, APCA-API-KEY-ID, X-BAPI-API-KEY)
    (("apikey", "api_key", "api-key", "accesskey", "access_key", "access-key", "privatekey",
      "private_key", "private-key", "secret", "token", "passw", "passphrase"),
     re.compile(rf"(?i)((?:{_NAMES}|apikey)[\w.-]*{_Q}\s*[:=]\s*{_Q}){_NOT_A_VALUE}{_V}{{6,}}"),
     rf"\1{REDACTED}"),
    # Bare `secret` / `token` keys (ccxt-style config dicts); the longer minimum
    # value keeps words like "token: BTCUSDT" readable.
    (("secret", "token"),
     re.compile(rf"(?i)((?<![\w-]){_Q}(?:secret|token){_Q}\s*[:=]\s*{_Q}){_NOT_A_VALUE}{_V}{{12,}}"),
     rf"\1{REDACTED}"),
    # Authorization headers
    (("authorization",),
     re.compile(rf"(?i)(\bauthorization{_Q}\s*[:=]\s*{_Q}(?:bearer\s+|basic\s+|token\s+|bot\s+)?){_V}{{8,}}"),
     rf"\1{REDACTED}"),
    (("bearer",),
     re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/=-]{16,}"), rf"\1{REDACTED}"),
    # Well-known key shapes (Anthropic/OpenAI, GitHub)
    (("sk-",),
     re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"), REDACTED),
    (("ghp_", "gho_", "ghu_", "ghs_", "ghr_"),
     re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), REDACTED),
]

# Literal secret values: from secret-named env vars, plus register_secret().
_ENV_NAME_RE = re.compile(r"(?i)api_?key|secret|token|password|passwd|passphrase|private_?key|credential")
_MIN_LITERAL_LEN = 12
_LITERAL_REFRESH_SECONDS = 60.0

_registered: set[str] = set()
_literal_re: re.Pattern[str] | None = None
_literal_sig: tuple[int, int] | None = None
_literal_built_at = 0.0


def _looks_like_secret(value: str) -> bool:
    # Real keys/tokens mix digits and letters; this skips plain words such as
    # a DB password "postgres" or a mode name, which would redact prose.
    return (
        len(value) >= _MIN_LITERAL_LEN
        and not any(c.isspace() for c in value)
        and any(c.isdigit() for c in value)
        and any(c.isalpha() for c in value)
    )


def register_secret(value: str | None) -> None:
    """Mask this exact value everywhere (for secrets not held in os.environ)."""
    global _literal_sig
    if value and _looks_like_secret(value.strip()):
        _registered.add(value.strip())
        _literal_sig = None  # force a rebuild on the next redact()


def _literal_pattern() -> re.Pattern[str] | None:
    global _literal_re, _literal_sig, _literal_built_at
    now = time.monotonic()
    sig = (len(os.environ), len(_registered))
    if sig == _literal_sig and now - _literal_built_at < _LITERAL_REFRESH_SECONDS:
        return _literal_re
    values = set(_registered)
    for name, value in list(os.environ.items()):
        value = value.strip().strip("\"'")
        if _ENV_NAME_RE.search(name) and _looks_like_secret(value):
            values.add(value)
    _literal_re = (
        re.compile("|".join(re.escape(v) for v in sorted(values, key=len, reverse=True)))
        if values else None
    )
    _literal_sig, _literal_built_at = sig, now
    return _literal_re


def redact(text: str) -> str:
    """Return ``text`` with credentials replaced by ``<redacted>``."""
    if not text:
        return text
    literal = _literal_pattern()
    if literal is not None:
        text = literal.sub(REDACTED, text)
    lower = text.lower()
    for needles, pattern, replacement in _PATTERNS:
        if any(needle in lower for needle in needles):
            text = pattern.sub(replacement, text)
    return text


_plain_formatter = logging.Formatter()


def _redact_record(record: logging.LogRecord) -> None:
    message = record.getMessage()
    clean = redact(message)
    if clean != message:
        record.msg, record.args = clean, None
    if record.exc_info and not record.exc_text:
        text = _plain_formatter.formatException(record.exc_info)
        clean = redact(text)
        if clean != text:
            # Formatter.format() uses a pre-set exc_text instead of re-rendering.
            record.exc_text = clean
    if record.stack_info:
        record.stack_info = redact(record.stack_info)


def install() -> None:
    """Redact every log record and uncaught traceback in this process. Idempotent."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_agent_team_redacting", False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            _redact_record(record)
        except Exception:  # never let redaction break a log call
            pass
        return record

    factory._agent_team_redacting = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)

    if sys.excepthook is sys.__excepthook__:
        def excepthook(exc_type, exc, tb):
            try:
                sys.stderr.write(redact("".join(traceback.format_exception(exc_type, exc, tb))))
                sys.stderr.flush()
            except Exception:
                sys.__excepthook__(exc_type, exc, tb)

        sys.excepthook = excepthook

    if threading.excepthook is threading.__excepthook__:
        def thread_excepthook(args):
            if args.exc_type is SystemExit:
                return
            try:
                name = args.thread.name if args.thread is not None else threading.get_ident()
                text = f"Exception in thread {name}:\n" + "".join(
                    traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
                )
                stream = sys.stderr
                if stream is not None:
                    stream.write(redact(text))
                    stream.flush()
            except Exception:
                threading.__excepthook__(args)

        threading.excepthook = thread_excepthook


def _main(argv: list[str]) -> int:
    if argv and argv[0] == "--in-place":
        changed = 0
        for path in argv[1:]:
            with open(path, encoding="utf-8", errors="surrogateescape") as fh:
                original = fh.read()
            clean = redact(original)
            if clean != original:
                tmp = f"{path}.redact-tmp"
                with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as fh:
                    fh.write(clean)
                os.chmod(tmp, os.stat(path).st_mode & 0o7777)
                os.replace(tmp, path)
                changed += 1
                print(f"redacted: {path}", file=sys.stderr)
        print(f"{changed} file(s) changed", file=sys.stderr)
        return 0
    if argv:
        print("usage: log_redaction.py [--in-place FILE...]  (no args: filter stdin)", file=sys.stderr)
        return 2
    stdin = open(sys.stdin.fileno(), encoding="utf-8", errors="replace", closefd=False)
    for line in stdin:
        sys.stdout.write(redact(line))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))

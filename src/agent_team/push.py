"""Web Push (VAPID) — keys and subscriptions live in DATA_DIR.

No third-party account: the browser's own push service (FCM, Apple, Mozilla)
delivers; the server only makes outbound HTTPS calls to it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid01
from pywebpush import WebPushException, webpush

log = logging.getLogger("agent_team.push")

DATA_DIR = Path(os.environ.get("AT_DATA_DIR", "/data"))
KEY_FILE = DATA_DIR / "vapid_private.pem"
SUBS_FILE = DATA_DIR / "push_subscriptions.json"
# VAPID "sub" claim: a contact URL for the push services (no e-mail needed).
VAPID_SUB = os.environ.get("AT_VAPID_SUB", "mailto:agent-team@localhost")

# Urgency high + a day of TTL: with "normal" urgency FCM holds messages while
# an idle Android phone dozes, and a 1 h TTL then drops them — the app's own
# "test in 15 s" (phone still awake) worked, real notifications never came.
TTL_S = 24 * 3600

_lock = threading.Lock()


def _vapid() -> Vapid01:
    if not KEY_FILE.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        v = Vapid01()
        v.generate_keys()
        v.save_key(str(KEY_FILE))
        KEY_FILE.chmod(0o600)
        log.info("generated new VAPID key pair")
    return Vapid01.from_file(str(KEY_FILE))


def public_key() -> str:
    """Application server key for PushManager.subscribe (base64url, no padding)."""
    raw = _vapid().public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _load() -> dict[str, dict]:
    try:
        return json.loads(SUBS_FILE.read_text())
    except FileNotFoundError:
        return {}


def _save(subs: dict[str, dict]) -> None:
    tmp = SUBS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(subs, indent=1))
    tmp.chmod(0o600)
    tmp.replace(SUBS_FILE)


def subscribe(sub: dict, user: str, user_agent: str) -> int:
    if not (isinstance(sub, dict) and sub.get("endpoint") and sub.get("keys")):
        raise ValueError("invalid subscription")
    with _lock:
        subs = _load()
        subs[sub["endpoint"]] = {"sub": sub, "user": user, "ua": user_agent[:200]}
        _save(subs)
        return len(subs)


def send_all(title: str, body: str, url: str = "/") -> list[dict]:
    """Send to every stored subscription; drop the ones the push service says are gone."""
    payload = json.dumps({"title": title, "body": body, "url": url})
    results, gone = [], []
    with _lock:
        subs = _load()
    for endpoint, rec in subs.items():
        host = endpoint.split("/")[2] if "//" in endpoint else "?"
        try:
            webpush(
                rec["sub"], payload,
                vapid_private_key=str(KEY_FILE), vapid_claims={"sub": VAPID_SUB},
                ttl=TTL_S, headers={"Urgency": "high"},
            )
            results.append({"push_service": host, "ok": True})
        except WebPushException as e:
            status = e.response.status_code if e.response is not None else None
            if status in (404, 410):
                gone.append(endpoint)
            results.append({"push_service": host, "ok": False, "status": status})
            log.warning("push to %s failed: %s", host, status)
    if gone:
        with _lock:
            subs = _load()
            for g in gone:
                subs.pop(g, None)
            _save(subs)
    return results

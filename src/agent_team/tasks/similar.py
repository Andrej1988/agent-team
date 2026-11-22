"""Near-duplicate task titles: a cheap word-overlap check before an agent
(scout, lead) files a task that already exists in other words.

Dice coefficient over normalised content words (accents folded, stop words
and pure numbers dropped, crude German/English suffix stripping). Tuned on
real task lists: 0.6 catches restated tasks ("Login live mit echtem Konto
prüfen" / "Login einmal mit echtem Konto vom Handy prüfen") while
related-but-different tasks stay below. Short titles that differ in one
key word can still collide — callers therefore offer an override."""

from __future__ import annotations

import re
import unicodedata

THRESHOLD = 0.6

_STOP = frozenset("""
der die das den dem des ein eine einer eines einem einen und oder nicht nur auch mit ohne
fur von vom zum zur im in am an auf aus bei bis nach vor uber unter als wie wird werden ist
sind statt noch schon jetzt beim alle allen the and for with from into not are was this that
""".split())
_SUFFIXES = ("ungen", "ung", "en", "er", "es", "e", "s", "n")


def words(title: str) -> frozenset[str]:
    t = unicodedata.normalize("NFKD", (title or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    out = set()
    for w in re.findall(r"[a-z0-9_]+", t):
        if len(w) < 3 or w in _STOP or w.isdigit():
            continue
        for suf in _SUFFIXES:
            if len(w) > len(suf) + 3 and w.endswith(suf):
                w = w[: -len(suf)]
                break
        out.add(w)
    return frozenset(out)


def score(a: str, b: str) -> float:
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0.0
    return 2 * len(wa & wb) / (len(wa) + len(wb))


def find(title: str, tasks: list[dict], threshold: float = THRESHOLD) -> list[dict]:
    """Tasks whose title is close to `title`, best first (each with `similarity`)."""
    hits = []
    for t in tasks:
        s = score(title, t.get("title") or "")
        if s >= threshold:
            hits.append({"id": t["id"], "title": t.get("title"), "status": t.get("status"),
                         "similarity": round(s, 2)})
    return sorted(hits, key=lambda h: -h["similarity"])

#!/usr/bin/env bash
# prepare for the logs scout: error lines since SCOUT_SINCE from the files
# matching AT_LOG_GLOBS (space-separated globs, e.g. on a mounted network
# share: "/Volumes/logs/my-webapp/*.log"), grouped by fingerprint.
set -uo pipefail
python3 - "$SCOUT_SINCE" "$SCOUT_OUT" <<'PY'
import calendar, glob, os, re, sys, time
from collections import OrderedDict
since = calendar.timegm(time.strptime(sys.argv[1], "%Y-%m-%dT%H:%M:%SZ"))
out = sys.argv[2]
globs = os.environ.get("AT_LOG_GLOBS", "").split()
if not globs:
    open(out, "w").write("AT_LOG_GLOBS not set — no logs configured\n"); sys.exit(0)
err = re.compile(r"\b(ERROR|FATAL|SEVERE|Exception|Traceback)\b")
groups = OrderedDict()
for pattern in globs:
    for path in glob.glob(os.path.expanduser(pattern)):
        try:
            if os.path.getmtime(path) < since:
                continue
            with open(path, errors="replace") as f:
                for line in f:
                    if not err.search(line):
                        continue
                    fp = re.sub(r"\d+|[0-9a-f]{8,}", "#", line.strip())[:200]
                    g = groups.setdefault(fp, {"n": 0, "first": line.strip()[:400], "file": path})
                    g["n"] += 1
                    g["last"] = line.strip()[:400]
        except OSError as e:
            groups.setdefault(f"unreadable {path}", {"n": 1, "first": str(e), "file": path, "last": ""})
with open(out, "w") as f:
    if not groups:
        f.write("no error lines since %s\n" % sys.argv[1])
    for fp, g in sorted(groups.items(), key=lambda kv: -kv[1]["n"])[:60]:
        f.write(f"[{g['n']}x] {g['file']}\n  first: {g['first']}\n  last:  {g.get('last','')}\n")
PY

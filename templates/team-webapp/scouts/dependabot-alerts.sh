#!/usr/bin/env bash
# prepare for the dependabot scout: open alerts of the repo in the cwd,
# fetched by code (gh resolves {owner}/{repo} from the git remote; GH_HOST
# selects GitHub Enterprise). Writes one compact JSON object per alert.
set -uo pipefail
if ! gh api "repos/{owner}/{repo}/dependabot/alerts?state=open&per_page=100" --paginate \
    --jq '.[] | {number, severity: .security_advisory.severity, package: .dependency.package.name,
                ecosystem: .dependency.package.ecosystem, manifest: .dependency.manifest_path,
                scope: .dependency.scope, vulnerable: .security_vulnerability.vulnerable_version_range,
                patched: .security_vulnerability.first_patched_version.identifier,
                ghsa: .security_advisory.ghsa_id, summary: .security_advisory.summary}' \
    -c > "$SCOUT_OUT" 2> "$SCOUT_OUT.err"; then
  { echo "FETCH FAILED:"; cat "$SCOUT_OUT.err"; } > "$SCOUT_OUT"
fi
rm -f "$SCOUT_OUT.err"
[ -s "$SCOUT_OUT" ] || echo "no open alerts" > "$SCOUT_OUT"

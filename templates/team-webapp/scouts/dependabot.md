## Scout assignment — Dependabot alerts

The prepared input lists this repo's **open Dependabot alerts** (fetched by
code via `gh api`; one JSON object per line: number, severity, package,
ecosystem, manifest, vulnerable range, first patched version, GHSA id,
summary).

Turn them into tasks:
- One proposal per package (several alerts on the same package / manifest
  belong together), not one per alert.
- Skip alerts that an open task or an earlier proposal (below) already covers.
- Priority: critical/high → 1, medium → 2, low → 3.
- Title: `Dependabot: <package> auf ≥ <patched version> (<severity>)`.
- Details: alert numbers, manifest(s), vulnerable range → patched version,
  one line on what the advisory is about, and whether it looks like a direct
  or transitive dependency.

If the input says the fetch failed, propose nothing and say why in one line.
Write every proposal **in German**.

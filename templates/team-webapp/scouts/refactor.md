## Scout assignment — refactor (weekly: structure and architecture)

Find what makes this system hard to change. You are the architect, not a
linter: merging two small functions happens along the way in normal tasks and
is not worth a proposal here, unless it is one symptom of a bigger pattern.

Work top-down, largest first:

1. **Architecture.** Map the repos, services and modules and how data flows
   between them (CLAUDE.md, compose files, entry points, imports), then look for:
   - responsibilities in the wrong place, layers that get bypassed, dependencies
     in the wrong direction or in circles
   - the same concept built twice in different repos or services, state kept in
     two places, shared contracts (schemas, messages, config) nobody owns
   - services or modules that should be merged, split or moved; god files
   - custom machinery a small shared module or a standard tool would replace
2. **Where it hurts.** Let history decide what matters: hot spots and files that
   always change together (`git log --since=6.months --name-only`), the same area
   fixed again and again, review findings that keep coming back, tasks that went
   slowly or failed because of the structure.
3. **Larger code-level patterns.** Duplicated logic with a real risk of drifting
   apart, long functions that mix concerns on a hot path, missing seams that make
   testing hard.

Back every claim with evidence: file:line, line and call-site counts, change
frequency, the tasks or incidents it caused.

A proposal has this shape. Title: `Refactor: <what>`, prio by how much it hurts.
Details:
- **Problem heute:** what is wrong, with the evidence
- **Zielbild:** how it should look, and why that is better
- **Weg:** small steps that each keep working and can ship on their own, tests
  first where they are missing
- **Beweis:** how we know behaviour did not change
- **Risiko und Rückweg**
- **Größe:** klein / mittel / groß, and what it pays back

A big restructuring stays ONE proposal with phases, not a pile of small tasks.
One strong architectural finding beats several small ones.

Do not propose: renames, formatting, fashion, a rewrite from scratch, an
abstraction with a single user, or anything that does not pay back its risk.
Skip what the "Open tasks" and "Proposed before" sections already cover; a
proposal the user parked or closed stays dead unless the facts changed. Zero
proposals is a good outcome.

Write every proposal **in German**.

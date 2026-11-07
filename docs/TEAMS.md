# Ein Team bauen — für jedes Thema

Ein **Team** gehört zu genau einem Projekt (einem Git-Repo oder einer Gruppe
von Repos). Es besteht aus Dateien, nicht aus Code:

```
teams/<projekt>/
  team.yaml            Repos, Rollen, Regeln, Gates, Scouts
  roles/<rolle>.md     ein Prompt pro Rolle (Front-Matter + Text)
  roles/common-prompt.md   Regeln und Ausgabeformat für jede Stufe
  scouts/<scout>.md    Auftrag eines Scouts
  scouts/_common.md    optional: Regeln für alle Scouts
  scouts/<datei>.sh    optional: Vorbereitungs-Skript (`prepare`)
```

Vorlagen:
- `templates/team-basic`: vier Rollen, für jedes Thema der Startpunkt
- `templates/team-webapp`: Software mit Build, Tests, Dependabot und Logs

## Vorgehen mit dem Nutzer (für Claude)

1. **Ziel klären:** Wofür ist das Repo da? Was ist ein guter Task: ein Text,
   eine Analyse, ein Fix, ein Datensatz, ein Kapitel? Woran erkennt man,
   dass er fertig und gut ist?
2. **Rollen ableiten:** Wer würde das in einem echten Team tun? Pro Rolle
   gilt: eine klare Verantwortung, `implement` (ändert Dateien) oder
   `advise` (liest und urteilt). Immer dabei sind `orchestrator`, `lead`
   und ein abschließender Prüfer (`reviewer` o. Ä.). Klein anfangen, meist
   reichen 2–4 Stufenrollen.
   Der abschließende Prüfer bekommt das stärkste Modell, nicht das billigste:
   Er muss den Diff ganz lesen und die Tests selbst ausführen, sonst winkt er
   durch. Für größere Umbauten lohnt ein `architect` (advise), der Zielbild und
   kleine Schritte entwirft, bevor jemand Code ändert.
3. **Gates festlegen:** Welche Prüfung darf nicht übersprungen werden?
   - `gate_roles` + `final`: Ein Prüfer muss am Ende bestehen.
   - `follow_up`: Nach Rolle X kommt immer Prüfer Y.
   - `metric_gates`: Zahlen müssen Schwellen erreichen.
   - `confirm_roles`: Diese Stufe startet nur nach Klick des Nutzers.
     Für alles, was nach außen wirkt: Deploy, Veröffentlichen, Versenden.
4. **Scouts festlegen:** Woher kommen Ideen? Den Lead-Lauf und den
   Wochenbericht gibt es immer. Dazu je nach Datenquelle eigene Scouts: das
   Repo selbst, Logs, Alerts oder eine API, die ein `prepare`-Skript abholt.
   Code-Projekte bekommen außerdem die wöchentlichen Scouts `refactor`
   (Struktur und Architektur) und `security` (Angriffsfläche, Rechte,
   Eingaben, Secrets) aus `templates/team-webapp`. Läuft etwas im Betrieb,
   gehört ein Scout für wiederkehrende Störungen dazu (Logs, Alerts, Backups).
5. **Prompts schreiben:** Pro Rolle das Vorgehen in wenigen Punkten, das
   Finding-Präfix (z. B. `EDT` für eine Lektorin) und was in die
   Zusammenfassung gehört. Deutsch oder Englisch, wie der Nutzer will.
6. **Sperren:** Was dürfen Agenten nie tun? Das gehört in
   `execute_conventions`, in `common-prompt.md` und, wo es technisch geht,
   in `deploy_guard` (`config/local.example.yaml`).

Beispiele für Rollen-Sets:

| Thema | Stufenrollen | Gate |
|---|---|---|
| Sachbuch / Doku | `author` (implement), `fact-checker` (advise), `editor` (advise, final) | Editor muss bestehen |
| Datenanalyse | `analyst` (implement), `methods-reviewer` (advise, final) | Zahlen per `metric_gates` |
| Website | `developer`, `content-writer` (implement), `reviewer` (final), `publisher` (`confirm_roles`) | Veröffentlichen nur per Klick |
| Trading-Strategien | `researcher`, `backtest-engineer`, `backtest-auditor` (`follow_up`, `metric_gates`, `metric_files`), `reviewer` | Kennzahlen aus Rohdaten berechnet |

## `team.yaml` — alle Schlüssel

```yaml
project: myproject              # = Projekt-ID in projects.yaml

worker:
  label: the my-repo repository (…)    # wie die Agenten das Projekt nennen
  execute_conventions: >-              # verbindliche Regeln für jede Stufe
    …

repos: [my-repo]                # Repo-Ordner unter dem Projekt-Root
repo_prefix: ""                 # Kurzform: "intraday" → "<prefix>intraday"
new_repo_pattern: '(?!)'        # Regex für Repos, die ein Task neu anlegen darf ('(?!)' = keine)
live_repos: []                  # Repos mit Live-Wirkung: Autopilot „full“ arbeitet dort nicht ohne Klick
isolate_repos: false            # true: Root ist ein gemeinsamer Ordner, Tasks sehen nur `repos`

roles:                          # Reihenfolge = Anzeige; jede Rolle braucht roles/<id>.md
  - id: worker
    description: Kurzbeschreibung für UI und Orchestrator
  - id: lead
    is_stage: false             # orchestrator und lead sind nie Stufen

gate_roles: [reviewer]          # deren Urteil gated, im UI nicht überspringbar
final:                          # jeder Plan endet mit dieser Rolle
  role: reviewer
  goal: >-
    …
  may_follow: [publisher]       # optional: diese Rollen dürfen nach dem Final kommen
follow_up:                      # nach jeder Stufe von X kommt automatisch Y
  author:
    role: fact-checker
    goal: >-
      …
confirm_roles: [publisher]      # starten nie auf Autopilot, nur per Klick
wait_roles:                     # frühestens N Tage nach der vorigen Stufe
  paper-reviewer: {days: 14}
hands_off_allow:                # Ausnahmen von den Autopilot-Regeln (Freitext)
  - "the shared test DB may be reset"

metric_gates:                   # ein pass zählt nur, wenn die Zahlen passen
  methods-reviewer:
    sample_size: ">= 500"
    p_value: "<= 0.05"
metric_files: []                # Rollen, deren Zahlen zusätzlich aus Dateien berechnet werden
                                # (heute nur Backtest-Equity, worker/evidence.py)
variant_penalty: {}             # Mehrfachtests: Schwellen steigen pro weiterer Variante

scouts:
  - id: ideas                   # Schlüssel; Auftrag in scouts/ideas.md
    role: worker                # roles/worker.md ist die Persona
    every_hours: 48
    model: sonnet
    max_proposals: 3
    channel: myproject          # Kanal, der die Zusammenfassung bekommt
    # optional:
    prepare: fetch.sh           # scouts/fetch.sh holt zuerst Daten (ohne LLM) und schreibt
                                # sie nach $SCOUT_OUT (Zeitfenster: $SCOUT_SINCE … $SCOUT_NOW)
    browser: true               # headless Chromium (nur im Docker-Betrieb)
    ledger: true                # erledigte/blockierte Tasks im Prompt
    always: true                # läuft auch, wenn das Budget „warten“ sagt (nicht bei Pause)
  - id: lead                    # Lead-Lauf: empfiehlt, was als Nächstes startet
    role: lead
    lead: true
    every_hours: 24
    channel: myproject-lead
  - id: weekly-report           # Wochenbericht im Lead-Kanal
    role: lead
    report: true
    every_hours: 168
    channel: myproject-lead
```

## Rollen-Datei `roles/<id>.md`

```markdown
---
id: author
label: Autorin
kind: implement            # implement = darf Dateien ändern, advise = nur lesen
default_model:             # leer = Standard; sonst opus / sonnet / haiku
---
## Role — Author (implement)

Was die Rolle tut, in wenigen Punkten. Finding-id prefix: `AUT`.
Was in die Zusammenfassung gehört.
```

`roles/common-prompt.md` wird jeder Stufe vorangestellt. Es enthält die
harten Regeln und das Ausgabeformat `# Stage result`. Den JSON-Block nicht
umbauen, das Backend parst ihn. Der Orchestrator braucht in
`roles/orchestrator.md` die Tabelle der planbaren Rollen und das Format
`# Plan`. Beides aus einer Vorlage übernehmen und nur die Rollen anpassen.

## Prüfen

```bash
PYTHONPATH=src .venv/bin/python -c \
  "from pathlib import Path; from agent_team.tasks import agent_roles as a; \
   c=a.load_catalog(Path('~/.agent-team/teams/<id>').expanduser()); print([r.id for r in c.ROLES])"
tools/run-local.sh --check
```

Danach einen kleinen echten Task durchlaufen lassen und den Prompt nach dem
ersten Ergebnis schärfen. Gute Teams entstehen durch zwei, drei solche
Runden, nicht beim ersten Entwurf.

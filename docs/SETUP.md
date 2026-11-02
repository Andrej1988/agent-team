# agent-team einrichten (lokal, ohne Docker; macOS oder Linux)

Anleitung für Claude (oder einen Menschen), der agent-team auf einem Rechner
einrichtet. Ergebnis:
- eine Web-App unter `http://127.0.0.1:8700`, die nur lokal erreichbar ist
- pro Projekt (Git-Repo) ein Agenten-Team, das Vorschläge macht, Tasks nach
  Freigabe umsetzt und das Ergebnis als Branch, Draft-PR oder lokalen Merge
  übergibt

Was agent-team ist und wie ein Task abläuft, steht in `README.md`. Wie man
ein Team für ein beliebiges Thema baut, steht in `docs/TEAMS.md`.

**Ändere für die Einrichtung keinen Code im Repo.** Alles Persönliche liegt
in `~/.agent-team/`. Fehlt eine Funktion, sag es dem Nutzer.

## Was wo liegt

| Was | Wo |
|---|---|
| Code | dieses Repo, z. B. `~/git/agent-team` |
| Konfiguration | `~/.agent-team/local.yaml`, `~/.agent-team/projects.yaml` |
| Teams (Rollen, Scouts) | `~/.agent-team/teams/<projekt>/`, aus `templates/` kopiert (nicht in den Projekt-Repos) |
| Daten, Logs, Kostenbuch | `~/.agent-team/data/` (`logs/web.log`, `logs/worker-<projekt>.log`, …) |
| Task-Arbeitsordner | `~/.agent-team/worktrees/<projekt>/<task>/` (Branch `agent/<task>`) |

Prozesse (`agent_team/local.py` startet und überwacht sie):
- **web**, zwei Ports: 8700 für den Browser, 8701 für die Agenten. Über den
  Agenten-Port kann niemand Tasks starten oder übernehmen.
- **chat** auf Port 8702.
- **worker-<projekt>** pro Projekt.

## Den Nutzer fragen (nicht raten)

1. **Projekte:** Welche Repos sollen ein Team bekommen, und wo liegen die
   Checkouts (z. B. `~/git/<repo>`)? Gibt es eine Workspace-Datei
   (`*.code-workspace`, IntelliJ-Projekt), die Repo-Liste dort vorschlagen.
   Gehören mehrere Repos zu einem Projekt (z. B. Backend und Frontend)?
2. **Thema pro Projekt:** Worum geht es, und was sollen die Agenten tun?
   - Webanwendung oder sonstige Software mit Build und Tests →
     `templates/team-webapp`
   - alles andere → `templates/team-basic`, dann mit `docs/TEAMS.md`
     zusammen mit dem Nutzer ausbauen
3. **Zugang zu Claude**, genau einer:
   - a) Claude-Abo (Pro/Max): `claude` einmal starten und anmelden.
     Budget-Modus `subscription`.
   - b) API-Key: `ANTHROPIC_API_KEY` in der Shell exportieren, nie in eine
     Datei im Repo oder in `local.yaml`. Budget-Modus `usd`.
   - c) Amazon Bedrock: AWS-Profil, Region und die Modell- bzw.
     Inference-Profile des Kontos (Opus, Sonnet, Haiku). Budget-Modus `usd`.
4. **Übernehmen:**
   - `pr`: Branch pushen und Draft-PR anlegen. Braucht `gh`; bei GitHub
     Enterprise den Host für `GH_HOST`.
   - `merge`: lokal in den Checkout mergen, nichts wird gepusht.
5. **Budget:** bei `usd` Dollar pro Tag und Woche für die autonome Arbeit.
6. **Was Agenten nie anfassen dürfen** (`deploy_guard`): Deploy-Laufwerke
   oder -Ordner, Deploy-Befehle oder -Profile, z. B. Maven-Profile aus den
   `pom.xml`. Gibt es nichts, `deploy_guard: {none: true}`.
7. Optional: Log-Dateien pro Projekt, für den Log-Scout der Webapp-Vorlage.

Secrets kommen **nie** in Konfigurationsdateien. Claude nutzt den Login oder
die Env-Variable, AWS das Profil bzw. den SSO-Login, `gh` den Schlüsselbund.

## 1. Voraussetzungen prüfen

```bash
python3 --version          # ≥ 3.11
node --version             # ≥ 20 (nur für den Frontend-Build)
git --version
claude --version           # Claude Code CLI im PATH (Scouts rufen `claude` direkt)
claude -p "Antworte nur mit ok" --model sonnet    # Zugang funktioniert?
gh auth status             # nur bei apply_mode pr (GHE: --hostname <host>)
```

Bedrock testen, mit den IDs aus „Fragen“:

```bash
CLAUDE_CODE_USE_BEDROCK=1 AWS_PROFILE=<profil> AWS_REGION=<region> \
ANTHROPIC_DEFAULT_SONNET_MODEL=<sonnet-id> claude -p "Antworte nur mit ok" --model sonnet
```

Für den Dependabot-Scout der Webapp-Vorlage braucht `gh` Lesezugriff auf
die Alerts: `gh api "repos/<org>/<repo>/dependabot/alerts?per_page=1"` muss
JSON liefern. Sonst `gh auth refresh -s security_events` mit dem Nutzer
klären; in Firmen gilt, was die Firma vorgibt.

## 2. Installieren

```bash
cd ~/git/agent-team
python3 -m venv .venv
.venv/bin/pip install -r requirements-local.txt
(cd frontend && npm ci && npm run build)          # → frontend/dist
mkdir -p ~/.agent-team/teams
cp config/local.example.yaml ~/.agent-team/local.yaml
cp config/projects.local.example.yaml ~/.agent-team/projects.yaml
```

Hinter einem Proxy oder einer eigenen Registry `pip` und `npm` so
einstellen, wie es dort üblich ist (nicht im Repo).

## 3. Konfigurieren

### `~/.agent-team/local.yaml`

Die Kommentare der Vorlage erklären jeden Schlüssel. Einzutragen:
- Zugang im Block `env`. Für Abo oder API-Key die Bedrock-Zeilen löschen.
- `apply_mode`, bei GHE `GH_HOST`
- `budget`
- `deploy_guard`
- pro Projekt `env`, z. B. `AT_LOG_GLOBS`

**`deploy_guard`:** Pflicht, das Starten scheitert sonst. Jeder Tool-Aufruf
eines Agenten wird geprüft, und abgewiesen wird:
- `paths`: Schreiben unter diesen Pfaden und Bash-Befehle, die sie nennen.
  Den Pfad so eintragen, wie er im Terminal heißt (`mount` zeigt
  Netzlaufwerke).
- `profiles`: Maven-Läufe mit diesen Profilen. Dazu die `pom.xml` lesen:
  ein `<profile>`, das kopiert oder deployt (`maven-resources-plugin`,
  `maven-antrun-plugin`, `outputDirectory` auf ein Laufwerk …).
- `patterns`: weitere Befehle als Regex, z. B. `deploy\.sh` oder
  `kubectl apply`.

Ist ein Deploy-Profil `activeByDefault` oder wird es über eine Property
aktiviert, dem Nutzer sagen: Dann deployt schon ein normales `mvn package`.

Prüfen, ohne etwas auszulösen:
```bash
AT_DEPLOY_GUARD='<deploy_guard als JSON>' PYTHONPATH=src .venv/bin/python -c \
  "from agent_team import guard; g=guard.load(); print(g.violation('Bash', {'command': '<verbotener Befehl>'}))"
```
Es muss ein Grund ausgegeben werden, nicht `None`.

### Projekte und Teams

**Layout:**
- a) Ein Repo pro Projekt: `cwd` ist das Repo.
- b) Mehrere Repos unter einem gemeinsamen Ordner (z. B. Backend und
  Frontend in `~/git`): `cwd` ist der Ordner, in `team.yaml` stehen die
  Repos des Projekts unter `repos:`, dazu `isolate_repos: true`. Ein
  Task-Arbeitsordner enthält dann nur diese Repos. „Übernehmen“ legt einen
  PR pro geändertem Repo an.

**Pro Projekt:**
1. In `~/.agent-team/projects.yaml` einen Eintrag nach dem Muster der
   Vorlage anlegen. Die Projekt-ID ist kurz, klein und mit Bindestrichen.
   Die Kanäle heißen `<id>` und `<id>-lead`.
2. Das Team kopieren, z. B. `cp -R templates/team-basic ~/.agent-team/teams/<id>`.
3. In `teams/<id>/team.yaml` die Platzhalter ersetzen (siehe Kopf der
   Datei):
   - Projekt-ID, Kanäle und Repo-Namen
   - `worker.label`
   - `execute_conventions`: die verbindlichen Regeln des Projekts, also
     womit gebaut und geprüft wird, was verboten ist und welche Ports
     belegt sind. Bei Software die Befehle aus `pom.xml`, `package.json`,
     `Makefile` … ablesen und einmal selbst ausprobieren.
4. Rollen, Scouts und Gates mit dem Nutzer an das Thema anpassen
   (`docs/TEAMS.md`). Klein anfangen: lieber zwei gute Rollen als sechs
   vage. Scouts ohne Datenquelle löschen.
5. Die Ausgabeformate (`# Stage result`, `# Plan`, `# Proposals`) nicht
   ändern, das Backend parst sie.

## 4. Prüfen und starten

```bash
tools/run-local.sh --check     # listet Prozesse und Probleme, startet nichts
tools/run-local.sh             # läuft im Vordergrund; Ctrl-C stoppt alles
```

Dann `http://127.0.0.1:8700` im Browser öffnen. Smoke-Test:
1. Mit „Idee“ einen kleinen Task anlegen (z. B. „README: Abschnitt X
   ergänzen“).
2. Im Lead-Kanal besprechen und auf „Freigeben & starten“ drücken.
3. Es laufen Planer → Stufen → Reviewer. Danach „Übernehmen“: je nach
   `apply_mode` ein Draft-PR oder ein lokaler Merge.
4. Bei `usd` stehen die Kosten in `~/.agent-team/data/budget/costs.jsonl`.
5. Gibt es einen `deploy_guard`: Im Chat einen Agenten bitten, einen
   gesperrten Befehl auszuführen. Er muss „Blockiert (Deploy-Sperre)“
   zurückbekommen.

Bei Problemen zuerst in die Logs unter `~/.agent-team/data/logs/` schauen.

## 5. Optional: Autostart

Nur wenn der Nutzer es will. Der Dienst startet `<repo>/tools/run-local.sh`
und schreibt die Logs nach `~/.agent-team/data/logs/`. `PATH` muss `claude`,
`gh`, `node` und `git` enthalten, denn Dienste erben die Shell-Umgebung
nicht.
- **macOS:** `~/Library/LaunchAgents/de.agent-team.plist` mit
  `ProgramArguments`, `RunAtLoad` und `KeepAlive` true,
  `StandardOutPath`/`StandardErrorPath` und `EnvironmentVariables` → `PATH`.
  Laden mit `launchctl load -w …`.
- **Linux:** systemd-User-Unit `~/.config/systemd/user/agent-team.service`
  mit `ExecStart=`, `Restart=on-failure` und `Environment=PATH=…`. Aktivieren
  mit `systemctl --user enable --now agent-team`; für den Start ohne Login
  `loginctl enable-linger`.

## Grenzen (dem Nutzer so sagen)

- Agenten laufen mit den Rechten des Nutzers (`bypassPermissions`), genau
  wie Claude Code.
  - Die Port-Trennung und die Deploy-Sperre verhindern die üblichen
    Versehen, sind aber keine Sandbox. Ein Skript, das einen gesperrten
    Pfad erst zur Laufzeit zusammensetzt, fällt nicht auf. Deshalb stehen
    die Regeln zusätzlich in den Rollen-Prompts.
  - Starten und Übernehmen bleiben immer ein Klick des Nutzers.
- Jeder Task hat eigene Arbeitsordner. Abhängigkeiten wie `node_modules`
  werden dort einmal neu installiert. Maven nutzt das gemeinsame `~/.m2`.
- **Login abgelaufen** (Abo oder AWS-SSO): Stufen schlagen mit einer
  Fehlermeldung fehl. Dann neu anmelden und im Task „erneut versuchen“.
- **Lokal nicht eingerichtet:** Browser-Tests (Playwright-Scout), der
  Codex-Backend, Web-Push aufs Handy und der öffentliche Zugang über
  Cloudflare. Das ist der Docker-Betrieb ([DOCKER.md](DOCKER.md)).
- Bei Dependabot-Fixes prüft der Reviewer Lockfile, Dependency-Tree und
  Tests. Ein hartes Tor aus Code gibt es dafür noch nicht.

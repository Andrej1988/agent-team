# agent-team

Agenten-Teams pro Git-Projekt mit eigener Web-App (Tasks, Chats, Freigaben).
Überblick: `README.md`. Task-Ablauf: `docs/TASKS-V2.md`. Teams für ein
beliebiges Thema bauen: `docs/TEAMS.md`. Docker-Betrieb: `docs/DOCKER.md`.

- **Einrichten auf einem Rechner (macOS/Linux, ohne Docker):** Folge
  `docs/SETUP.md`. Konfiguration und Teams liegen in `~/.agent-team/`, nie
  im Repo. Für die Einrichtung keinen Code ändern.
- **Dieses Repo ist öffentlich.** Keine persönlichen Daten in Code, Tests,
  Vorlagen, Doku oder Commit-Nachrichten: keine echten Projekt- oder
  Personennamen, Pfade, Domains, IPs, Mail-Adressen, Secrets. Beispiele
  neutral halten (`myproject`, `demo`, `shop`, `/home/user`, `example.invalid`).
  Deployments (Compose-Dateien, `projects.yaml`, Daten) liegen in einem
  eigenen Repo bzw. Ordner außerhalb.
- Tests: `PYTHONPATH=src:base python -m pytest tests` (eigene Fixtures unter
  `tests/fixtures/`).

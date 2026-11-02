# Betrieb mit Docker (Server)

Für den Einstieg ist der lokale Start einfacher ([SETUP.md](SETUP.md)).
Docker lohnt sich, wenn agent-team dauerhaft auf einem Server läuft und du
es von unterwegs als PWA mit Push nutzen willst. Die Images bauen aus diesem
Repo:

```bash
docker build -t agent-team-web:latest .                         # Web-API + App
docker build -f Dockerfile.chat -t agent-team-chat:latest .     # Chat-Dienst und Worker
```

Deine Compose-Datei, `projects.yaml` und die Daten gehören in ein eigenes
(privates) Repo bzw. einen eigenen Ordner, nicht hierher.

## Dienste

| Dienst | Image | Aufgabe |
|---|---|---|
| web | `agent-team-web` | API, App, Tasks-DB, Inbox, Alarme |
| chat | `agent-team-chat` | Chat-Sitzungen (Claude Agent SDK) |
| worker-<projekt> | `agent-team-chat`, `command: /opt/agent-team/worker-entrypoint.sh` | Stufen, Scouts, Lead eines Projekts |
| Tunnel (optional) | z. B. `cloudflare/cloudflared` | Zugang von außen |

## Zugriff und Rechte

Im Docker-Betrieb prüft die Web-App:
- **Browser:** Er muss ein gültiges **Cloudflare-Access-JWT** mitbringen
  (`AT_ACCESS_TEAM`, `AT_ACCESS_AUD`). Ohne Access gibt es keinen Zugang über
  den Browser. Starten und Übernehmen gehen nur so.
- **Worker und Chat:** Sie kommen über ein internes Docker-Netz
  (`AT_INTERNAL_NET`, CIDR) ohne JWT. Starten und Übernehmen können sie
  nicht.
- **Alarm-Absender:** Programme aus `AT_NOTIFY_NET` dürfen nur
  `POST /p/<projekt>/api/notify/<kanal>`.

Den web-Container nicht direkt ins Internet stellen, nur hinter dem
Tunnel bzw. Access.

## Gerüst

Vorlage zum Anpassen, nicht 1:1 lauffähig: Pfade, Netze und Access-Werte
sind deine.

```yaml
name: agent-team
services:
  web:
    image: agent-team-web:latest
    user: "1000:1000"
    environment:
      AT_ACCESS_TEAM: <team>.cloudflareaccess.com
      AT_ACCESS_AUD: <application audience tag>
      AT_INTERNAL_NET: 172.30.0.0/24          # Subnetz von "internal"
    volumes:
      - ./config:/app/config:ro               # projects.yaml
      - ./data:/data
      - /srv/git/myproject/.agents/team:/teams/myproject:ro
    networks: [internal, public]
    read_only: true
    tmpfs: [/tmp]

  chat:
    image: agent-team-chat:latest
    environment:
      AT_DATA_DIR: /data
      AT_HOST_GIT: /srv/git                   # Host-Pfad der Repos (für die Scope-Prüfung)
    volumes:
      - ./config:/opt/agent-team/config:ro
      - ./data:/data
      - /srv/git/myproject:/workspaces/myproject
      - claude-config:/home/devuser/.claude   # Claude-Login, mit den Workern geteilt
    networks: [internal]

  worker-myproject:
    image: agent-team-chat:latest
    command: ["/opt/agent-team/worker-entrypoint.sh"]
    environment:
      TASKS_API_URL: http://web:8000/p/myproject
      TASKS_CONTROL_DIR: /control
      WORKER_CWD: /workspaces/myproject
      WORKER_TEAM_DIR: /teams/myproject
      WORKER_HOST_ROOT: /srv/git/myproject             # gleiche Pfade innen wie außen,
      WORKER_WORKTREE_ROOT: /srv/git/.task-worktrees/myproject   # damit Worktrees stimmen
    volumes:
      - /srv/git/myproject:/workspaces/myproject
      - /srv/git/myproject:/srv/git/myproject
      - /srv/git/.task-worktrees/myproject:/srv/git/.task-worktrees/myproject
      - ./data/projects/myproject/control:/control
      - /srv/git/myproject/.agents/team:/teams/myproject:ro
      - claude-config:/home/devuser/.claude
    networks: [internal]

networks:
  internal: {ipam: {config: [{subnet: 172.30.0.0/24}]}}
  public: {}
volumes:
  claude-config: {}
```

Weitere Env-Variablen (Auswahl):
- `AT_BUDGET_MODE` (`subscription` | `usd`), `AT_BUDGET_USD_DAY` /
  `AT_BUDGET_USD_WEEK`
- `WORKER_APPLY_MODE` (`merge` | `pr`), `AT_TASK_BRANCH_PREFIX`
- `AT_GIT_IDENTITY` (`agents` | `user`), `AT_GIT_DOMAIN`
- `AT_DEPLOY_GUARD` (JSON, siehe `src/agent_team/guard.py`)
- `TASKS_AGENT_BACKEND`, `WORKER_REFINE_MODEL`, `WORKER_EXECUTE_MODEL`
- `AT_VAPID_SUB` (Kontakt für Web-Push)

Docker-Socket für Worker und Chat nur einbinden, wenn Stufen selbst
Container starten sollen, z. B. für Tests mit den Abhängigkeiten des
Projekts. Wer den Socket hat, hat Root auf dem Host.

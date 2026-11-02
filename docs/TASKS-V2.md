# Task-Ablauf: Lead, Freigabe, Chat pro Task, Worktrees

So läuft ein Task in agent-team, und das sind die Regeln, die das Backend
durchsetzt.

Grundsätze:
1. Pro Team gibt es eine **Lead-Rolle**, die aus den Vorschlägen auswählt.
   Ein Task startet erst, wenn der Nutzer ihn **mit dem Lead besprochen und
   freigegeben** hat.
2. **Jeder gestartete Task hat einen eigenen Chat.** Dort steht lesbar, was
   passiert.
3. **„Ausarbeiten“** macht aus einer kurzen Idee eine Spezifikation, und
   zwar vor der Freigabe.
4. Die autonome Arbeit hält sich an das **Budget**. Der Lead schlägt nur so
   viel vor, wie das Budget hergibt.

## Ablauf eines Tasks

```
 Idee ──(Ausarbeiten, optional)──▶ Zur Freigabe ──(User: Freigeben)──▶ In Arbeit ──▶ Erledigt
   ▲                                    │                                 │  ▲
   │ Scout / User / Lead legt an        │ Lead schlägt vor, User          ▼  │ Antwort / Klick
   │                                    │ bespricht im Lead-Chat     Wartet auf dich
   └──── Geparkt (später) ◀─────────────┤ parken                      (Frage, Deploy-Freigabe, Fehler)
         Geschlossen        ◀─────────────┘ zusammenlegen / überholt
```

Sichtbare Zustände (sie sagen, wer dran ist):

| Anzeige          | intern                                            | wer ist dran |
|------------------|---------------------------------------------------|--------------|
| Idee             | `new`                                             | Lead/User    |
| Wird ausgearbeitet | `refining`                                      | Agent        |
| Zur Freigabe     | `refined`, oder vom Lead empfohlen (`proposed_at`) | **User**     |
| In Arbeit        | `planning`, `running`, `plan_ready` mit Autopilot | Agenten      |
| Wartet auf dich  | `awaiting_input`, `blocked`, `failed`, `limit_reached`, wartende Freigabe-Stufe | **User** |
| Erledigt         | `done`                                            | —            |
| Geparkt          | `backlog`: soll **später** noch gemacht werden    | —            |
| Geschlossen      | `closed`: wird nicht gemacht (zusammengelegt, überholt, Duplikat, anderweitig gelöst). `lead_note` sagt warum, „Wieder öffnen“ ist möglich | — |

Die Tasks-Seite zeigt das als **Board**, von links nach rechts wie ein Task
läuft: Ideen (Geparkte eingeklappt darunter), Zur Freigabe, In Arbeit,
Wartet auf dich (hervorgehoben), Erledigt (letzte 7 Tage). Ideen und Erledigt
sind auf 8 Karten eingeklappt. Jede Karte nennt den
nächsten Schritt (z. B. „Frage offen · Stufe 3/5“, „nicht übernommen“) und
bietet die passende Aktion als Link in den Task- bzw. Lead-Chat. Ziehen per
Drag & Drop gibt es absichtlich nicht, denn die Übergänge sind geschützt.
Alle Erledigten und Geschlossen sind eigene Listen. Daten:
`GET /p/<projekt>/api/tasks/board`.

## Lead-Kanal und Chat pro Task

**Vor der Freigabe** wird im **Lead-Kanal des Projekts** (`<projekt>-lead`)
besprochen. Dort stehen die Empfehlungen des Lead, jede mit dem Knopf
„Freigeben & starten“. Man kann mehrere Vorschläge in einem Gespräch
durchgehen, umformulieren lassen oder verwerfen.

**Ab der Freigabe** hat der Task einen eigenen Chat-Kanal. Er ist virtuell
(`task-<projekt>-<id>`) und wird vom Chat-Dienst bei Bedarf angelegt.
Arbeitsordner ist die Projekt-Wurzel, die Persona der Lead. Die erste
Nachricht fasst die Besprechung und die Ausarbeitung zusammen. Danach steht
dort **alles chronologisch**:
- pro Stufe „▶ Stufe 2 · test-engineer: <Ziel>“ und am Ende
  „✓/✗ <Zusammenfassung>“; die Werkzeug-Schritte stehen zusammengeklappt
  darunter
- Fragen eines Stufen-Agenten, beantwortet per Button oder im Eingabefeld
- Deploy-Freigaben als Knopf
- die Abschluss-Zusammenfassung

Im selben Chat kann man jederzeit den Lead fragen, z. B. „was ist gerade
los?“ oder „warum blockiert?“. Er liest dafür die Stufen-Logs und den Task.

**Freigabe** ist immer ein Klick des Users: „Freigeben & starten“ unter dem
Vorschlag oder „Starten“ am Task. Agenten können keinen Task starten, denn
die Start-Route nimmt nur Aufrufe aus der App an. Der Lead darf:
- Tasks anlegen und ausarbeiten
- eigene Ideen (Scout-Vorschläge, Folge-Tasks) zusammenlegen, schließen
  oder parken
- Prioritäten setzen und Vorschläge machen

Die Stufen-Ansicht (PipelinePanel) liegt unter „Details“: Limits, Plan
bearbeiten, Stufe wiederholen oder überspringen.

## Lead-Rolle

- `roles/lead.md` in jedem Team. Ein Team kann die Rolle auch einer
  bestehenden Persona geben, z. B. einem Product Owner.
- **Täglicher Lauf** (Scout-Mechanik, `lead: true`):
  - liest alle Ideen und Vorschläge, die erledigten und blockierten Tasks
    der letzten Wochen und das Budget
  - wählt die nächsten 1–3 Tasks, die ins Budget passen
  - schreibt pro Task eine Empfehlung in den Lead-Kanal: warum jetzt, grober
    Plan, Risiko, Aufwand

  Die Tasks gehen auf „Zur Freigabe“, dazu kommt eine Sammel-Push.
- **Aufräumen**, jeweils mit Begründung:
  - zusammenlegen (→ Geschlossen)
  - schon erledigt (→ Erledigt)
  - überholt (→ Geschlossen)
  - später (→ Geparkt)

  Für Tasks, die der User selbst angelegt hat, gibt es eine Karte, und der
  User entscheidet.
- **Alarme:** Jeder Lead-Lauf liest die offenen Meldungen der
  `*-watchdog`-Kanäle (`GET /p/<p>/api/watchdog`), gruppiert sie und prüft
  sie nach.
  - Echtes Problem: Fix-Task mit der Karte „Freigeben & starten“.
  - Rauschen oder schon behoben: Karte „Watchdog: n Meldungen löschen?“ mit
    Begründung. Gelöscht wird nur per Klick des Users.

  Jede Meldung wird dem Lead nur einmal gezeigt.
- **Folge-Tasks:** Aus erledigten Tasks legt der Lead Folge-Ideen an.

## Autopilot

Pro Task gibt es zwei Stufen:
- **Läuft bis zur Freigabe** (Standard): Nach „Freigeben“ laufen Plan und
  alle Stufen durch. Halt gibt es nur bei einer Frage, bei Fehler oder
  `blocked`, beim Budget, bei einer Wartezeit (`wait_roles`) oder bei einer
  Freigabe-Rolle.
- **Jede Stufe bestätigen.**

Fest eingebaut und nicht einstellbar:
- Freigabe-Rollen (`confirm_roles`, z. B. Deploy) starten nur per Klick.
- Stufen auf Live-Repos (`live_repos`) laufen „hands-off“ bis zur
  Freigabe-Stufe: kein Commit, keine laufenden Dienste, kein Deploy.
  Ausnahmen stehen in `hands_off_allow`.
- Unbekannte Repos brauchen einen Klick.

## Infos sind keine Tasks

Scouts legen nur Tasks an, wenn etwas zu tun ist. Reine Infos gehen in den
Kanal. Umgesetzt ist das als Prompt-Regel und als Filter im Worker: Ein
Titel mit `[INFO]` geht nur in den Kanal.

## Isolation: Branch + Worktree pro Task

- **Start der ersten Stufe:** Der Worker legt einen Arbeitsordner pro Task
  an (`worker/workspace.py`).
  - Die Repos des Tasks (`task.repos`, ohne Angabe alle bzw. bei
    `isolate_repos` die des Projekts) sind `git worktree`s auf dem Branch
    `task/<id>` bzw. `<branch_prefix><id>`.
  - Der Rest sind Links auf den Live-Ordner, nur zum Lesen.
  - Ist die Projekt-Wurzel selbst ein Repo, wird sie mit ausgecheckt.
  - Ignorierte `.env`-Dateien werden hineinkopiert, damit Tests laufen.
- **Stufen** laufen dort. Env-Variablen, die auf den Live-Ordner zeigen,
  zeigen für sie auf den Arbeitsordner. Nach jeder Umsetzungs-Stufe
  committet der Worker auf den Task-Branch, ohne `.env`, Schlüssel und Links.
- **Review:** `GET /api/tasks/<id>/changes` liefert Stand und Diff des
  Branches.
- **Übernehmen** (nur per Klick aus der App):
  - `merge`: Rebase des Task-Branches auf den Live-Branch im Worktree, dann
    `merge --ff-only` im Live-Ordner. Ein Konflikt oder lokale Änderungen
    an denselben Dateien brechen ab, der Live-Ordner bleibt unverändert. Das
    passiert auch automatisch vor einer Freigabe-Stufe, die aus dem
    Live-Ordner deployt.
  - `pr`: Branch pushen und einen Draft-PR pro Repo anlegen bzw.
    aktualisieren.
- **Aufräumen** alle 10 Minuten:
  - erledigt und übernommen (bzw. alle PRs gemergt oder geschlossen):
    Worktree weg, Branch gelöscht
  - geschlossen oder gelöscht: Worktree und Branch weg
  - erledigt, aber nicht übernommen: bleibt, bis du übernimmst oder
    schließt
- Weil sich Tasks nicht in die Quere kommen, braucht es keine Sperre pro
  Repo. Es gilt nur die Obergrenze paralleler Stufen.

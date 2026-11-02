"""Scout helpers (no agent run): proposal parsing, schedule, prompt."""

import time

import pytest

pytest.importorskip("tasks_api")  # bridge module: chat/worker image only
from agent_team.worker import scouts  # noqa: E402


def test_parse_proposals():
    text = 'thinking…\n# Proposals\n```json\n[{"title": "A", "details": "x", "prio": 1, "repos": ["shop-app"]},' \
           ' {"title": "", "details": "skip"}, {"title": "B", "prio": 9}]\n```\n'
    got = scouts.parse_proposals(text, limit=3)
    assert [(p["title"], p["prio"]) for p in got] == [("A", 1), ("B", 2)]
    assert scouts.parse_proposals("# Proposals\n```json\n[]\n```", 3) == []
    assert scouts.parse_proposals("nothing here", 3) is None
    assert len(scouts.parse_proposals("# Proposals\n```json\n" + str([{"title": f"t{i}"} for i in range(5)]).replace("'", '"') + "\n```", 2)) == 2


def test_due_and_state(tmp_path):
    now = time.time()
    assert scouts.due({"every_hours": 24}, {}, now)
    assert not scouts.due({"every_hours": 24}, {"last_run": now - 3600}, now)
    scouts.save_state(tmp_path, "po", {"last_run": 1, "proposed": ["x"]})
    assert scouts.load_state(tmp_path, "po")["proposed"] == ["x"]


def test_prompt_lists_open_tasks_and_earlier_proposals():
    p = scouts.build_prompt({"max_proposals": 2}, "the app", [{"id": 7, "status": "new", "title": "Fix login"}],
                            ["Old idea"], "ASSIGNMENT", "PERSONA")
    assert "#7 [new] Fix login" in p and "Old idea" in p and "At most 2 proposals" in p
    assert "PERSONA" in p and "ASSIGNMENT" in p and "# Proposals" in p


def test_parse_stream_counts_tools():
    raw = "\n".join([
        '{"type":"system","subtype":"init"}',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash"},{"type":"text","text":"x"}]}}',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"mcp__playwright__browser_navigate"}]}}',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"mcp__playwright__browser_navigate"}]}}',
        '{"type":"result","result":"# Proposals\\n```json\\n[]\\n```"}',
    ])
    text, tools = scouts.parse_stream(raw)
    assert tools == {"Bash": 1, "mcp__playwright__browser_navigate": 2}
    assert text.startswith("# Proposals")


def test_ledger_text():
    rows = [
        {"id": 1, "status": "done", "title": "Momentum", "execute_summary": "OOS Sharpe 1.2, live", "updated_at": "2"},
        {"id": 2, "status": "blocked", "title": "Mean reversion", "stop_reason": "GATE oos_sharpe = 0.3", "updated_at": "3"},
    ]
    text = scouts.ledger_text(rows)
    assert text.splitlines()[0].startswith("- #2 [blocked] Mean reversion — GATE")
    assert "OOS Sharpe 1.2" in text
    p = scouts.build_prompt({}, "x", [], [], "A", "P", ledger=text)
    assert "Ledger" in p and "Mean reversion" in p


def test_prepare_writes_input_with_window(tmp_path):
    import asyncio
    team = tmp_path / "team"
    (team / "scouts").mkdir(parents=True)
    (team / "scouts" / "digest.sh").write_text('printf "%s -> %s\\n" "$SCOUT_SINCE" "$SCOUT_NOW" > "$SCOUT_OUT"\n')
    out = asyncio.run(scouts.run_prepare({"id": "wd", "prepare": "digest.sh", "every_hours": 4},
                                         {"last_run": 0}, team, tmp_path / "control", str(tmp_path)))
    since, now = out.read_text().strip().split(" -> ")
    assert out.parent == tmp_path / "control" / "scouts" / "wd" and since < now


def test_prepare_failure_raises(tmp_path):
    import asyncio
    (tmp_path / "scouts").mkdir()
    (tmp_path / "scouts" / "bad.sh").write_text("echo boom; exit 3\n")
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(scouts.run_prepare({"id": "wd", "prepare": "bad.sh"}, {}, tmp_path, tmp_path / "c", str(tmp_path)))


def test_always_scout_ignores_pacer_but_not_manual_pause(tmp_path, monkeypatch):
    import asyncio
    started = []

    async def fake_run(s, *a):
        started.append(s["id"])
    monkeypatch.setattr(scouts, "run_scout", fake_run)
    team = {"scouts": [{"id": "wd", "always": True}, {"id": "po"}]}

    async def go(reason):
        monkeypatch.setattr(scouts, "_budget_ok", lambda _b: (False, reason))
        bg: set = set()
        scouts.tick(team, tmp_path, tmp_path / reason.replace(" ", "_"), "x", str(tmp_path), "http://x", bg)
        await asyncio.gather(*bg)

    asyncio.run(go("Wochenbudget voraus"))
    asyncio.run(go("manuell pausiert"))
    assert started == ["wd"]


def test_one_optional_scout_at_a_time(tmp_path, monkeypatch):
    import asyncio
    started = []

    async def fake_run(s, *a):
        started.append(s["id"])
        await asyncio.sleep(0.05)
    monkeypatch.setattr(scouts, "run_scout", fake_run)
    monkeypatch.setattr(scouts, "_budget_ok", lambda _b: (True, ""))
    team = {"scouts": [{"id": "a"}, {"id": "b"}, {"id": "wd", "always": True}]}

    async def go():
        bg: set = set()
        scouts.tick(team, tmp_path, tmp_path / "c", "x", str(tmp_path), "http://x", bg)
        await asyncio.sleep(0)
        await asyncio.gather(*bg)
    asyncio.run(go())
    assert started == ["a", "wd"]


def test_info_proposals_go_to_channel_not_tasks(tmp_path, monkeypatch):
    import asyncio
    team = tmp_path / "team"
    (team / "scouts").mkdir(parents=True)
    (team / "scouts" / "wd.md").write_text("ASSIGNMENT")
    monkeypatch.setattr(scouts.pipeline, "load_role", lambda r: type("R", (), {"body": "P"})())
    monkeypatch.setattr(scouts.tasks_api, "list_tasks", lambda *a: [])
    created, notes = [], []
    monkeypatch.setattr(scouts.tasks_api, "create_task",
                        lambda title, *a: created.append(title) or {"id": len(created)})
    monkeypatch.setattr(scouts, "_notify", lambda base, ch, title, text, src: notes.append((title, text)))
    out = ('# Proposals\n```json\n[{"title": "[INFO] Backfill läuft", "details": "ok"},'
           ' {"title": "Fetcher-Schleife fixen", "details": "x"}]\n```')

    async def fake_agent(*a, **k):
        return out, {}
    monkeypatch.setattr(scouts, "_run_agent", fake_agent)
    asyncio.run(scouts.run_scout({"id": "wd", "channel": "c"}, team, tmp_path / "ctl", "x", str(tmp_path), "http://api"))
    assert created == ["Fetcher-Schleife fixen"]
    assert [t for t, _ in notes] == ["Info (wd)", "1 neue Vorschläge (wd)"]
    assert "Backfill läuft" in notes[0][1]


def test_lead_run_proposes_and_parks_only_scout_tasks(tmp_path, monkeypatch):
    import asyncio

    from agent_team.worker import lead
    team = tmp_path / "team"
    (team / "scouts").mkdir(parents=True)
    monkeypatch.setattr(lead.pipeline, "load_role", lambda r: type("R", (), {"body": "LEAD"})())
    ideas = [
        {"id": 1, "status": "new", "prio": 2, "title": "A", "details": "x (Vorschlag des Scouts tests)"},
        {"id": 2, "status": "new", "prio": 2, "title": "B", "details": "von mir"},
        {"id": 3, "status": "refined", "prio": 1, "title": "C", "details": "y (Vorschlag des Scouts wd)"},
        {"id": 4, "status": "new", "prio": 3, "title": "D", "details": "", "proposed_at": "2026-10-27"},
    ]
    monkeypatch.setattr(lead.tasks_api, "list_tasks", lambda *a: ideas)
    monkeypatch.setattr(lead, "_budget", lambda base: {"allowed": True})
    posts, notes = [], []
    monkeypatch.setattr(lead, "_post", lambda base, path, body: posts.append((path, body)))
    monkeypatch.setattr(lead.scouts, "_notify", lambda base, ch, title, text, src: notes.append((title, text)))
    out = ('# Lead\n```json\n{"recommend": [{"id": 3, "note": "zuerst C"}, {"id": 2, "note": "dann B"},'
           ' {"id": 4, "note": "schon da"}], "park": [{"id": 1, "reason": "Duplikat", "into": 3},'
           ' {"id": 2, "reason": "alt"}], "summary": "Lage ok."}\n```')
    seen = {}

    async def fake_agent(prompt, *a, **k):
        seen["prompt"] = prompt
        return out, {}
    monkeypatch.setattr(lead.scouts, "_run_agent", fake_agent)
    asyncio.run(lead.run_lead({"id": "lead", "channel": "demo-lead", "max_proposals": 3}, team,
                              tmp_path / "ctl", "x", str(tmp_path), "http://api"))
    # no cap on waiting cards: 3 new per run (#4 already waits and is skipped);
    # scout idea #1 is merged right away, the user's #2 becomes a card
    assert posts[:3] == [("/api/tasks/3/propose", {"note": "zuerst C"}), ("/api/tasks/2/propose", {"note": "dann B"}),
                         ("/api/tasks/1/merge", {"into": 3, "note": "Duplikat"})]
    path, card = posts[3]
    assert path == "/api/notify/demo-lead" and card["title"].startswith("Aufräumen: #2")
    assert [b["data"] for row in card["buttons"] for b in row] == ["park:2", "close:2", "keep:2", "delete:2"]
    assert card["buttons"][0][0]["label"] == "Parken – vielleicht später"
    assert card["buttons"][-1][0]["label"] == "Endgültig löschen"
    assert "Recommend at most 3 new" in seen["prompt"] and "[new, prio 2, User] B" in seen["prompt"]
    title, text = notes[0]
    assert title == "Lead: 2 zur Freigabe, 1 geschlossen"  # merged = closed, not backlog
    assert "Geschlossen (wird nicht gemacht):\n#1 A — in #3 zusammengelegt" in text
    assert "1 deiner Tasks würde ich aufräumen" in text


def test_lead_creates_follow_ups_once(tmp_path, monkeypatch):
    import asyncio

    from agent_team.worker import lead
    team = tmp_path / "team"
    (team / "scouts").mkdir(parents=True)
    monkeypatch.setattr(lead.pipeline, "load_role", lambda r: type("R", (), {"body": "LEAD"})())
    tasks = [{"id": 109, "status": "done", "mode": "pipeline", "prio": 2, "title": "Test-DB",
              "updated_at": "2099-01-01T00:00:00", "execute_summary": "Folgetask: übrige Repos umstellen"}]
    monkeypatch.setattr(lead.tasks_api, "list_tasks", lambda *a: tasks)
    created = []
    monkeypatch.setattr(lead.tasks_api, "create_task",
                        lambda title, details, prio, repos: created.append((title, details)) or {"id": 200, "title": title})
    monkeypatch.setattr(lead, "_budget", lambda base: {})
    monkeypatch.setattr(lead, "_post", lambda *a: None)
    monkeypatch.setattr(lead.scouts, "_notify", lambda *a: None)
    prompts = []
    out = ('# Lead\n```json\n{"recommend": [], "park": [], "create": [{"from": 109, "title": "Real-DB-Tests umstellen",'
           ' "details": "laut Inventar"}], "summary": "ok"}\n```')

    async def fake_agent(prompt, *a, **k):
        prompts.append(prompt)
        return out, {}
    monkeypatch.setattr(lead.scouts, "_run_agent", fake_agent)
    scout = {"id": "lead", "channel": "c"}
    asyncio.run(lead.run_lead(scout, team, tmp_path / "ctl", "x", str(tmp_path), "http://api"))
    assert created[0][0] == "Real-DB-Tests umstellen" and "(Folge-Task von #109" in created[0][1]
    assert "#109 Test-DB" in prompts[0]
    asyncio.run(lead.run_lead(scout, team, tmp_path / "ctl", "x", str(tmp_path), "http://api"))
    assert "#109 Test-DB" not in prompts[1].split("## Just finished")[1].split("## Recent")[0]
    assert lead.from_scout({"details": created[0][1]})


def test_weekly_report_collects_facts_and_posts(tmp_path, monkeypatch):
    import asyncio
    import subprocess as sp

    from agent_team.worker import report
    team = tmp_path / "team"
    team.mkdir()
    (team / "team.yaml").write_text("repos: [app]\n")
    repo = tmp_path / "ws" / "app"
    repo.mkdir(parents=True)
    env = {"GIT_AUTHOR_NAME": "shop data-engineer (Agent)", "GIT_AUTHOR_EMAIL": "a@x",
           "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@x", "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    sp.run(["git", "init", "-q", str(repo)], check=True, env=env)
    sp.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "x"], check=True, env=env)
    monkeypatch.setattr(report.pipeline, "load_role", lambda r: type("R", (), {"body": "PO"})())
    now = __import__("datetime").datetime.now().astimezone().isoformat()
    lists = {None: [{"id": 5, "status": "new", "title": "Idee", "created_at": now, "updated_at": now}],
             "done": [{"id": 3, "status": "done", "title": "Fertig", "updated_at": now, "execute_summary": "gut"}],
             "backlog": [], "closed": []}
    monkeypatch.setattr(report.tasks_api, "list_tasks", lambda status=None: lists[status])
    monkeypatch.setattr(report, "_get", lambda base, path: {"total_usd": 1.5})
    monkeypatch.setattr(report.lead, "_budget", lambda base: {"week_used": 40})
    notes, seen = [], {}
    monkeypatch.setattr(report.scouts, "_notify", lambda base, ch, title, text, src: notes.append((title, text)))

    async def fake_agent(prompt, *a, **k):
        seen["p"] = prompt
        return "Vorspann\n# Wochenbericht\nGute Woche.", {}
    monkeypatch.setattr(report.scouts, "_run_agent", fake_agent)
    asyncio.run(report.run_report({"id": "weekly-report", "channel": "shop-lead"}, team, tmp_path / "ctl", "x",
                                  str(tmp_path / "ws"), "http://web/p/shop"))
    assert "#3 [done] Fertig" in seen["p"] and "Kosten: $1.50" in seen["p"]
    assert "app: shop data-engineer (Agent) 1" in seen["p"]
    assert notes[0][0].startswith("Wochenbericht KW") and notes[0][0].endswith("· shop")
    assert notes[0][1] == "Gute Woche."
    assert list((tmp_path / "ctl" / "reports").glob("*-weekly-report.md"))


def test_decisions_reach_scout_and_lead_prompts(tmp_path):
    from agent_team import decisions
    from agent_team.worker import lead
    decisions.record(tmp_path, "Aufräumen: #33 Fibonacci: In #71 zusammenlegen → #33 geparkt", channel="demo-lead")
    section = decisions.prompt_section(tmp_path)
    assert "respect them" in section and "#33 Fibonacci" in section
    p = scouts.build_prompt({}, "x", [], [], "A", "P", decided=section)
    assert p.index("#33 Fibonacci") < p.index("## Output contract")
    lp = lead.build_prompt({}, "x", [], [], "", {}, "A", "P", decided=section)
    assert lp.index("#33 Fibonacci") < lp.index("## Output contract")


def test_manual_trigger_runs_despite_schedule_and_pacer(tmp_path, monkeypatch):
    import asyncio
    scouts._running.clear()
    started = []

    async def fake_run(s, *a):
        started.append(s["id"])
    monkeypatch.setattr(scouts, "run_scout", fake_run)
    monkeypatch.setattr(scouts, "_budget_ok", lambda base: (False, "du arbeitest gerade selbst"))
    scouts.save_state(tmp_path, "po", {"last_run": __import__("time").time()})  # not due
    team = {"scouts": [{"id": "po", "every_hours": 24}]}

    async def go():
        bg = set()
        scouts.tick(team, tmp_path, tmp_path, "x", str(tmp_path), "http://api", bg)
        await asyncio.gather(*bg)
    asyncio.run(go())
    assert started == []
    (tmp_path / "scouts" / "po.run").write_text("1")
    asyncio.run(go())
    assert started == ["po"] and not (tmp_path / "scouts" / "po.run").exists()
    monkeypatch.setattr(scouts, "_budget_ok", lambda base: (False, "manuell pausiert"))
    (tmp_path / "scouts" / "po.run").write_text("1")
    asyncio.run(go())
    assert started == ["po"]  # a manual pause holds even a manual run


def test_lead_parse_close_flag():
    from agent_team.worker import lead  # noqa: E402
    out = lead.parse_lead('# Lead\n```json\n{"park": [{"id": 4, "reason": "alt", "close": true}, {"id": 5, "reason": "später"}]}\n```')
    assert [(r["id"], r["close"]) for r in out["park"]] == [(4, True), (5, False)]


def test_lead_handles_watchdog_alerts(tmp_path, monkeypatch):
    import asyncio
    import json as _json

    from agent_team.worker import lead
    team = tmp_path / "team"
    (team / "scouts").mkdir(parents=True)
    monkeypatch.setattr(lead.pipeline, "load_role", lambda r: type("R", (), {"body": "LEAD"})())
    monkeypatch.setattr(lead.tasks_api, "list_tasks", lambda *a: [])
    monkeypatch.setattr(lead, "_budget", lambda base: {"allowed": True})
    alerts = [{"channel": "demo-watchdog", "seq": s, "ts": f"2026-10-28T1{s - 100}:00", "title": t, "text": "x",
               "checked": False} for s, t in ((101, "Stale live data"), (102, "Stale live data"), (103, "DB error"))]
    monkeypatch.setattr(lead, "_get", lambda base, path: alerts)
    created, posts, notes = [], [], []
    monkeypatch.setattr(lead.tasks_api, "create_task",
                        lambda title, details, prio, repos: created.append((title, details)) or {"id": 50, "title": title})
    monkeypatch.setattr(lead, "_post", lambda base, path, body: posts.append((path, body)))
    monkeypatch.setattr(lead.scouts, "_notify", lambda base, ch, title, text, src: notes.append((title, text)))
    out = ('# Lead\n```json\n{"watchdog": [{"group": "Stale", "alerts": [101, 102], "action": "dismiss",'
           ' "reason": "Feed-Lücken nach Börsenschluss"}, {"group": "DB", "alerts": [103], "action": "fix",'
           ' "reason": "Spalte fehlt", "task": {"title": "ba.name fixen", "details": "d", "prio": 1}}],'
           ' "summary": "ok"}\n```')
    prompts = []

    async def fake_agent(prompt, *a, **k):
        prompts.append(prompt)
        return out, {}
    monkeypatch.setattr(lead.scouts, "_run_agent", fake_agent)
    ctl = tmp_path / "ctl"
    run = lambda: asyncio.run(lead.run_lead({"id": "lead", "channel": "demo-lead"}, team, ctl, "x", str(tmp_path),  # noqa: E731
                                            "http://api"))
    run()
    assert "[101]" in prompts[0] and "[103]" in prompts[0]
    # fix: an agent-made task with a start card
    assert created[0][0] == "ba.name fixen" and lead.from_scout({"details": created[0][1]})
    assert ("/api/tasks/50/propose", {"note": "Watchdog: Spalte fehlt"}) in posts
    # dismiss: a card; deleting stays the user's tap via the stored key
    card = next(b for p, b in posts if p == "/api/notify/demo-lead")
    key = card["buttons"][0][0]["data"].removeprefix("wdel:")
    assert card["title"].startswith("Watchdog: 2 Meldungen löschen?")
    spec = _json.loads((ctl / "watchdog" / f"{key}.json").read_text())
    assert spec["alerts"] == {"demo-watchdog": [101, 102]} and "Börsenschluss" in spec["reason"]
    assert "1 Watchdog-Fix" in notes[0][0]
    # handled alerts are not shown again
    run()
    assert "[101]" not in prompts[1] and "(none)" in prompts[1].split("## Open watchdog alerts")[1][:200]

"""Run agent-team natively, without Docker (work laptop, macOS or Linux).

    PYTHONPATH=src:base python -m agent_team.local [--config ~/.agent-team/local.yaml]

or ``tools/run-local.sh``. Starts the same processes the Docker setup runs as
containers — web app, chat service, one task worker per project with
``tasks: true`` — as child processes, restarts any that dies, logs each to
``<data_dir>/logs/<name>.log`` and stops them all on Ctrl-C / SIGTERM.

Everything listens on 127.0.0.1 only. The web app serves the browser on
``port`` and the workers / agents on ``internal_port`` (app.py, local mode);
the chat service is on ``chat_port``. Example config:
``config/local.example.yaml``; setup steps: ``docs/SETUP.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = Path("~/.agent-team/local.yaml").expanduser()
RESTART_DELAY_S = 5.0


def _path(raw: Any) -> Path:
    return Path(os.path.expanduser(str(raw))).resolve()


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data = _path(cfg.get("data_dir") or "~/.agent-team/data")
    return {
        "data_dir": data,
        "projects_file": _path(cfg.get("projects_file") or path.parent / "projects.yaml"),
        "port": int(cfg.get("port") or 8700),
        "internal_port": int(cfg.get("internal_port") or 8701),
        "chat_port": int(cfg.get("chat_port") or 8702),
        "worktree_root": _path(cfg.get("worktree_root") or "~/.agent-team/worktrees"),
        "apply_mode": str(cfg.get("apply_mode") or "pr"),
        "git_identity": str(cfg.get("git_identity") or "user"),
        "branch_prefix": str(cfg.get("branch_prefix") or "task/"),
        "budget": cfg.get("budget") or {},
        "models": cfg.get("models") or {},
        "env": {str(k): str(v) for k, v in (cfg.get("env") or {}).items()},
        "projects": cfg.get("projects") or {},
        "deploy_guard": cfg.get("deploy_guard") or {},
    }


def base_env(cfg: dict) -> dict[str, str]:
    """Environment shared by every child process."""
    b = cfg["budget"]
    env = {
        **os.environ,
        **cfg["env"],
        "PYTHONPATH": os.pathsep.join([str(REPO / "src"), str(REPO / "base")]),
        "PYTHONUNBUFFERED": "1",
        "AT_LOCAL": "1",
        "AT_DATA_DIR": str(cfg["data_dir"]),
        "AT_PROJECTS_FILE": str(cfg["projects_file"]),
        "AT_TEAMS_DIR": str(cfg["data_dir"] / "teams"),
        "AT_WEB_DIST": str(REPO / "frontend" / "dist"),
        "AT_BASE_DIR": str(REPO / "base"),
        "AT_INTERNAL_PORT": str(cfg["internal_port"]),
        "AT_CHAT_URL": f"http://127.0.0.1:{cfg['chat_port']}",
        "AT_WEB_INTERNAL_URL": f"http://127.0.0.1:{cfg['internal_port']}",
        "AT_GIT_IDENTITY": cfg["git_identity"],
        "AT_TASK_BRANCH_PREFIX": cfg["branch_prefix"],
        "AT_BUDGET_MODE": str(b.get("mode") or "usd"),
        "AT_BUDGET_USD_DAY": str(b.get("usd_per_day") or 0),
        "AT_BUDGET_USD_WEEK": str(b.get("usd_per_week") or 0),
        "WORKER_APPLY_MODE": cfg["apply_mode"],
        "AT_DEPLOY_GUARD": json.dumps(_guard_spec(cfg)),
        # The company manages the CLIs; never npm-install them from here.
        "WORKER_CLI_REFRESH_H": "0",
        "WORKER_CLI_UPGRADE_SCRIPT": "",
    }
    return env


def _guard_spec(cfg: dict) -> dict:
    """deploy_guard from local.yaml (global) plus every projects.<id>.deploy_guard, merged."""
    out: dict[str, list[str]] = {"paths": [], "profiles": [], "patterns": []}
    parts = [cfg["deploy_guard"]] + [(p or {}).get("deploy_guard") or {} for p in cfg["projects"].values()]
    for part in parts:
        for key in out:
            for v in part.get(key) or []:
                if str(v) not in out[key]:
                    out[key].append(str(v))
    return out


def processes(cfg: dict) -> dict[str, tuple[list[str], dict[str, str]]]:
    """name -> (argv, env) of every child."""
    env = base_env(cfg)
    py = sys.executable
    out = {
        "web": ([py, "-m", "agent_team.local", "--serve-web", str(cfg["port"]), str(cfg["internal_port"])], env),
        "chat": ([py, "-m", "uvicorn", "agent_team.chat.server:app", "--host", "127.0.0.1",
                  "--port", str(cfg["chat_port"]), "--log-level", "info", "--no-access-log"], env),
    }
    os.environ["AT_PROJECTS_FILE"] = str(cfg["projects_file"])
    os.environ["AT_DATA_DIR"] = str(cfg["data_dir"])
    from . import projects
    projects.DATA_ROOT = cfg["data_dir"]
    projects.TEAMS_ROOT = cfg["data_dir"] / "teams"
    models = cfg["models"]
    for p in projects.load(cfg["projects_file"]).values():
        if not p.tasks:
            continue
        root = projects.project_root(p.id)
        if root is None:
            raise SystemExit(f"projects.yaml: project {p.id!r} needs a chat channel (its cwd is the project root)")
        extra = {str(k): str(v) for k, v in ((cfg["projects"].get(p.id) or {}).get("env") or {}).items()}
        out[f"worker-{p.id}"] = ([py, "-u", "-m", "agent_team.worker.worker"], {
            **env,
            "TASKS_API_URL": f"http://127.0.0.1:{cfg['internal_port']}/p/{p.id}",
            "TASKS_CONTROL_DIR": str(p.control_dir),
            "WORKER_CWD": root.cwd,
            "WORKER_TEAM_DIR": str(p.team_dir),
            "WORKER_HOST_ROOT": root.cwd,
            "WORKER_WORKTREE_ROOT": str(cfg["worktree_root"] / p.id),
            "TASKS_AGENT_BACKEND": "claude",
            "WORKER_REFINE_MODEL": str(models.get("refine") or "sonnet"),
            "WORKER_EXECUTE_MODEL": str(models.get("execute") or "opus"),
            "CLAUDE_MODEL": str(models.get("execute") or "opus"),
            **extra,
        })
    return out


def check(cfg: dict) -> list[str]:
    """Problems that would make a child crash-loop, as readable lines."""
    problems = []
    if not (REPO / "frontend" / "dist" / "index.html").is_file():
        problems.append("frontend/dist fehlt: cd frontend && npm ci && npm run build")
    if not cfg["projects_file"].is_file():
        problems.append(f"{cfg['projects_file']} fehlt (Vorlage: config/projects.local.example.yaml)")
    spec = _guard_spec(cfg)
    try:
        from .guard import load
        load(json.dumps(spec))
    except (ValueError, TypeError, re.error) as e:   # bad regex in patterns
        problems.append(f"deploy_guard ungültig: {e}")
    if not any(spec.values()) and not cfg["deploy_guard"].get("none"):
        problems.append("deploy_guard fehlt in local.yaml: Deploy-Pfad und Deploy-Profile eintragen "
                        "(oder bewusst `deploy_guard: {none: true}` setzen)")
    for tool in ("git", "claude") + (("gh",) if cfg["apply_mode"] == "pr" else ()):
        if subprocess.run(["sh", "-c", f"command -v {tool}"], capture_output=True).returncode:
            problems.append(f"`{tool}` nicht im PATH")
    return problems


def serve_web(port: int, internal_port: int) -> None:
    """The web app on two loopback ports in one process (browser / agents)."""
    import uvicorn

    from .app import app

    async def main() -> None:
        servers = [uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=p, log_level="info",
                                                 access_log=(p == port)))
                   for p in (port, internal_port)]
        await asyncio.gather(*(s.serve() for s in servers))

    asyncio.run(main())


def supervise(cfg: dict) -> None:
    logs = cfg["data_dir"] / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for sub in ("teams", "chat"):
        (cfg["data_dir"] / sub).mkdir(parents=True, exist_ok=True)
    procs = processes(cfg)
    running: dict[str, subprocess.Popen] = {}
    stopping = False

    def start(name: str) -> None:
        argv, env = procs[name]
        log = (logs / f"{name}.log").open("ab")
        log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} start {name}\n".encode())
        log.flush()
        running[name] = subprocess.Popen(argv, env=env, cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
                                         start_new_session=True)

    def stop(*_: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    for name in procs:
        start(name)
    print(f"agent-team läuft: http://127.0.0.1:{cfg['port']}  (Logs: {logs})", flush=True)
    died: dict[str, float] = {}
    while not stopping:
        time.sleep(1)
        for name, proc in list(running.items()):
            if proc.poll() is None:
                continue
            if name not in died:
                print(f"{name} beendet (Code {proc.returncode}) — Neustart in {RESTART_DELAY_S:.0f} s, "
                      f"siehe {logs / (name + '.log')}", flush=True)
                died[name] = time.monotonic()
            elif time.monotonic() - died[name] >= RESTART_DELAY_S:
                del died[name]
                start(name)
    print("Stoppe …", flush=True)
    for proc in running.values():
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + 20
    for proc in running.values():
        try:
            proc.wait(timeout=max(deadline - time.monotonic(), 0.1))
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", type=Path, default=Path(os.environ.get("AT_LOCAL_CONFIG") or DEFAULT_CONFIG))
    ap.add_argument("--check", action="store_true", help="nur prüfen und die Prozesse auflisten")
    ap.add_argument("--serve-web", nargs=2, type=int, metavar=("PORT", "INTERNAL_PORT"), help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.serve_web:
        serve_web(*args.serve_web)
        return
    cfg = load_config(args.config.expanduser())
    problems = check(cfg)
    if args.check:
        for name, (argv, env) in processes(cfg).items():
            print(f"{name}: {' '.join(argv[1:])}  cwd={env.get('WORKER_CWD', REPO)}")
        print("\n".join(problems) or "ok")
        sys.exit(1 if problems else 0)
    if problems:
        raise SystemExit("Nicht startbereit:\n  " + "\n  ".join(problems))
    supervise(cfg)


if __name__ == "__main__":
    main()

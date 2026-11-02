"""Task worktrees: every started task works on its own branch ``task/<id>``
in its own folder, never in the live checkout.

Layout (host paths, mounted 1:1 into the worker so ``docker run -v`` from a
stage and the worktrees' ``.git`` pointers work on both sides)::

    WORKER_HOST_ROOT      /home/user/git/system             live checkout
    WORKER_WORKTREE_ROOT  /home/user/git/.task-worktrees/system
        124/                                                 the task's workspace
            backend/          git worktree, branch task/124
            shared-lib  -> /home/user/git/system/shared-lib  (read only)
            AGENTS.md      -> …

The task's repos (``task.repos``; all repos when none are named) are
worktrees, everything else is a link to the live checkout. A project whose
root is a repo itself gets the root as a worktree too, the
sub-repos inside it. Ignored ``.env`` files are copied in so tests run.

After each implement stage the worker commits what changed (``commit``).
"Übernehmen" (``apply``) rebases the branch onto the live branch inside the
worktree and fast-forwards the live checkout — the live tree never sees a
conflict, and uncommitted work of others there stays as it is (git refuses
the fast-forward if it would overwrite it). ``cleanup`` removes the worktree
(and the branch once merged, or when the task was closed/deleted).

Without both env vars the feature is off and stages run in the live checkout
as before.

``WORKER_APPLY_MODE=pr`` (work laptop, GitHub Enterprise): "Übernehmen"
pushes the branch and opens / updates a draft pull request per repo
(``publish_pr``) instead of merging locally; the sweep cleans up once every
PR is merged or closed. ``AT_TASK_BRANCH_PREFIX`` changes the ``task/``
prefix (e.g. ``agent/``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

MAX_DIFF_CHARS = 400_000
# Never committed from a worktree, whatever .gitignore says.
_SECRET_RE = re.compile(r"(^|/)(\.env(\.[^/]*)?|[^/]*\.env|[^/]*\.(pem|key)|id_rsa[^/]*|[^/]*\.secret)$")


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Layout:
    main_root: Path       # live checkout (host path)
    wt_root: Path         # this project's worktrees (host path)
    control: Optional[Path] = None   # state + diff files for the web app

    def ws(self, task_id: int) -> Path:
        return self.wt_root / str(int(task_id))

    def state_path(self, task_id: int) -> Optional[Path]:
        return self.control / "worktrees" / f"{int(task_id)}.json" if self.control else None


def layout(control: Optional[Path] = None) -> Optional[Layout]:
    main = (os.environ.get("WORKER_HOST_ROOT") or "").strip()
    wt = (os.environ.get("WORKER_WORKTREE_ROOT") or "").strip()
    if not main or not wt or not Path(main).is_dir():
        return None
    return Layout(Path(main), Path(wt), control)


APPLY_MODE = (os.environ.get("WORKER_APPLY_MODE") or "merge").strip().lower()
BRANCH_PREFIX = os.environ.get("AT_TASK_BRANCH_PREFIX") or "task/"


def branch(task_id: int) -> str:
    return f"{BRANCH_PREFIX}{int(task_id)}"


def _git(cwd: Path, *args: str, env: Optional[dict] = None, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                       env={**os.environ, "GIT_EDITOR": "true", **(env or {})}, timeout=300)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args)} ({cwd}): {(r.stderr or r.stdout).strip()[:1500]}")
    return r.stdout


def _ok(cwd: Path, *args: str) -> bool:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, timeout=120).returncode == 0


def _is_repo(p: Path) -> bool:
    return (p / ".git").exists()


def _is_worktree(p: Path) -> bool:
    return (p / ".git").is_file() and not p.is_symlink()


def sub_repos(main_root: Path) -> list[str]:
    return sorted(d.name for d in main_root.iterdir()
                  if d.is_dir() and not d.is_symlink() and not d.name.startswith(".") and _is_repo(d))


def _main_repo(lay: Layout, name: str) -> Path:
    return lay.main_root / name if name else lay.main_root


def live_branch(repo: Path) -> str:
    b = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip()
    if not b:
        raise GitError(f"{repo}: live checkout is not on a branch (detached HEAD)")
    return b


def _copy_env_files(repo: Path, dst: Path) -> None:
    out = _git(repo, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", check=False)
    for rel in out.splitlines():
        if rel.endswith("/"):
            continue
        name = rel.rsplit("/", 1)[-1]
        src = repo / rel
        if (name == ".env" or name.endswith(".env")) and src.is_file() and not (dst / rel).exists():
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst / rel)


def _add_worktree(repo: Path, path: Path, task_id: int) -> None:
    b = branch(task_id)
    _git(repo, "worktree", "prune", check=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    if _ok(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{b}"):
        _git(repo, "worktree", "add", "--quiet", str(path), b)
    else:
        _git(repo, "worktree", "add", "--quiet", "-b", b, str(path), live_branch(repo))
    _copy_env_files(repo, path)


def ensure(lay: Layout, task_id: int, repos: list[str], only: Optional[frozenset[str]] = None) -> Path:
    """Create / complete the task's workspace. Idempotent: repos added to the
    task later become worktrees on the next call (their link is replaced).

    ``only`` (team.yaml ``isolate_repos``): the project's repos are just these
    sub-repos of a shared root (e.g. ``~/git`` with every repo of the user) —
    the others are neither worktrees nor linked into the workspace."""
    ws = lay.ws(task_id)
    subs = [r for r in sub_repos(lay.main_root) if only is None or r in only]
    wanted = [r for r in repos or [] if r in subs] or subs
    if _is_repo(lay.main_root):
        if not _is_worktree(ws):
            if ws.exists() and not any(ws.iterdir()):
                ws.rmdir()
            _add_worktree(lay.main_root, ws, task_id)
    elif only is not None:
        ws.mkdir(parents=True, exist_ok=True)
    else:
        ws.mkdir(parents=True, exist_ok=True)
        for f in lay.main_root.iterdir():
            if f.is_file() and not f.name.startswith(".") and not (ws / f.name).exists():
                (ws / f.name).symlink_to(f)
    for r in subs:
        target = ws / r
        if r in wanted:
            if target.is_symlink():
                target.unlink()
            if not _is_worktree(target):
                _add_worktree(lay.main_root / r, target, task_id)
        elif not target.exists() and not target.is_symlink():
            target.symlink_to(lay.main_root / r)
    return ws


def worktrees(lay: Layout, task_id: int) -> list[tuple[str, Path]]:
    """(repo name, path) of the task's worktrees; '' = the project root."""
    ws = lay.ws(task_id)
    out = [("", ws)] if _is_worktree(ws) else []
    if ws.is_dir():
        out += [(d.name, d) for d in sorted(ws.iterdir()) if d.is_dir() and _is_worktree(d)]
    return out


def commit(lay: Layout, task_id: int, message: str, env: Optional[dict] = None) -> list[str]:
    """Commit everything a stage left in the task's worktrees (without
    secrets and the links to the live checkout). Returns the repos committed."""
    done = []
    for name, p in worktrees(lay, task_id):
        _git(p, "add", "-A", env=env)
        staged = [s for s in _git(p, "diff", "--cached", "--name-only", "-z").split("\0") if s]
        drop = [s for s in staged if _SECRET_RE.search(s) or ((p / s).is_symlink()
                and str(os.readlink(p / s)).startswith(str(lay.main_root)))]
        if drop:
            _git(p, "reset", "-q", "--", *drop)
        if _ok(p, "diff", "--cached", "--quiet"):
            continue
        _git(p, "commit", "-q", "-m", message, env=env)
        done.append(name or ".")
    return done


def state(lay: Layout, task_id: int) -> dict:
    """What the branch holds compared to the live branch, per repo."""
    b = branch(task_id)
    repos = {}
    for name, p in worktrees(lay, task_id):
        main = _main_repo(lay, name)
        try:
            base = live_branch(main)
        except GitError as e:
            repos[name or "."] = {"error": str(e)}
            continue
        ahead = int(_git(main, "rev-list", "--count", f"{base}..{b}").strip() or 0)
        behind = int(_git(main, "rev-list", "--count", f"{b}..{base}").strip() or 0)
        stat = _git(main, "diff", "--shortstat", f"{base}...{b}").strip()
        dirty = bool(_git(p, "status", "--porcelain").strip())
        repos[name or "."] = {"base": base, "ahead": ahead, "behind": behind, "stat": stat, "dirty": dirty}
    return {"branch": b, "workspace": str(lay.ws(task_id)), "repos": repos,
            "pending": any(r.get("ahead") for r in repos.values())}


def diff(lay: Layout, task_id: int) -> str:
    b = branch(task_id)
    parts = []
    for name, p in worktrees(lay, task_id):
        main = _main_repo(lay, name)
        try:
            base = live_branch(main)
        except GitError:
            continue
        d = _git(main, "diff", "--stat", "--patch", f"{base}...{b}", check=False)
        if d.strip():
            parts.append(f"##### {name or '.'} ({base}...{b})\n{d}")
    out = "\n".join(parts)
    return out if len(out) <= MAX_DIFF_CHARS else out[:MAX_DIFF_CHARS] + "\n…(gekürzt)…\n"


def write_state(lay: Layout, task_id: int, extra: Optional[dict] = None) -> dict:
    st = state(lay, task_id)
    if extra:
        st.update(extra)
    path = lay.state_path(task_id)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        old = {}
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        for k in ("applied", "applied_at"):  # history survives later writes
            if k in old and k not in st:
                st[k] = old[k]
        path.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        path.with_suffix(".diff").write_text(diff(lay, task_id), encoding="utf-8")
    return st


# Session notes the agents rewrite all the time in the live checkout. A task
# that touches them (e.g. untracks them) must not be refused
# because the live copy is "locally modified": set them aside, merge, and put
# the live content back (it is newer than anything on the branch).
NOTE_PATHS = (".claude/SESSION.md", ".claude/session.log")


def _set_notes_aside(main: Path) -> dict[str, bytes]:
    saved: dict[str, bytes] = {}
    for rel in NOTE_PATHS:
        f = main / rel
        dirty = _git(main, "status", "--porcelain", "--", rel, check=False).strip()
        if f.is_file() and dirty and not dirty.startswith("??"):
            saved[rel] = f.read_bytes()
            _git(main, "checkout", "--", rel, check=False)
    return saved


def _put_notes_back(main: Path, saved: dict[str, bytes]) -> None:
    for rel, data in saved.items():
        (main / rel).parent.mkdir(parents=True, exist_ok=True)
        (main / rel).write_bytes(data)


def apply(lay: Layout, task_id: int, env: Optional[dict] = None) -> list[str]:
    """Bring the task's branch into the live checkout: rebase onto the live
    branch in the worktree, then fast-forward the live checkout. Raises
    GitError with a readable reason (conflict, would overwrite local changes)
    before touching the live checkout of any repo if a rebase fails."""
    b = branch(task_id)
    commit(lay, task_id, f"Task #{task_id}: letzte Änderungen vor dem Übernehmen", env)
    todo = []
    for name, p in worktrees(lay, task_id):
        main = _main_repo(lay, name)
        base = live_branch(main)
        if int(_git(main, "rev-list", "--count", f"{base}..{b}").strip() or 0) == 0:
            continue
        try:
            _git(p, "rebase", "--quiet", base, env=env)
        except GitError as e:
            _git(p, "rebase", "--abort", check=False)
            raise GitError(f"{name or '.'}: Konflikt mit dem aktuellen {base} — {e}") from None
        todo.append((name, main, base))
    out = []
    for name, main, base in todo:
        notes = _set_notes_aside(main)
        try:
            _git(main, "merge", "--ff-only", "--quiet", b, env=env)
        except GitError as e:
            raise GitError(f"{name or '.'}: Übernehmen nach {base} abgelehnt (lokale Änderungen im Live-Ordner?) — {e}"
                           + (f" — bereits übernommen: {', '.join(out)}" if out else "")) from None
        finally:
            _put_notes_back(main, notes)
        head = _git(main, "rev-parse", "--short", "HEAD").strip()
        out.append(f"{name or '.'} → {base} {head}")
    return out


def _gh(cwd: Path, *args: str, env: Optional[dict] = None, check: bool = True) -> str:
    r = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True,
                       env={**os.environ, **(env or {})})
    if check and r.returncode != 0:
        raise GitError((r.stderr or r.stdout).strip()[-1500:] or f"gh {args[0]} failed")
    return r.stdout


def publish_pr(lay: Layout, task_id: int, title: str, body: str, env: Optional[dict] = None) -> list[str]:
    """Push the task branch of every repo with commits and open a draft PR
    (or update the existing one). Rebases onto the remote base first, so the
    PR shows only the task's changes. Returns "repo → url" lines."""
    b = branch(task_id)
    commit(lay, task_id, f"Task #{task_id}: letzte Änderungen vor dem PR", env)
    out = []
    for name, p in worktrees(lay, task_id):
        main = _main_repo(lay, name)
        base = live_branch(main)
        _git(p, "fetch", "--quiet", "origin", base, env=env)
        if int(_git(p, "rev-list", "--count", f"origin/{base}..{b}").strip() or 0) == 0:
            continue
        try:
            _git(p, "rebase", "--quiet", f"origin/{base}", env=env)
        except GitError as e:
            _git(p, "rebase", "--abort", check=False)
            raise GitError(f"{name or '.'}: Konflikt mit origin/{base} — {e}") from None
        _git(p, "push", "--quiet", "--force-with-lease", "-u", "origin", f"{b}:{b}", env=env)
        url = _gh(p, "pr", "view", b, "--json", "url", "--jq", ".url", env=env, check=False).strip()
        if not url:
            url = _gh(p, "pr", "create", "--draft", "--base", base, "--head", b,
                      "--title", title[:250], "--body", body[:60000], env=env).strip().splitlines()[-1]
        out.append(f"{name or '.'} → {url}")
    return out


def pr_states(lay: Layout, task_id: int) -> dict[str, str]:
    """repo -> PR state (OPEN / MERGED / CLOSED) for repos whose branch has one."""
    b = branch(task_id)
    out = {}
    for name, p in worktrees(lay, task_id):
        st = _gh(p, "pr", "view", b, "--json", "state", "--jq", ".state", check=False).strip()
        if st:
            out[name or "."] = st
    return out


def cleanup(lay: Layout, task_id: int, drop_branch: bool = False) -> None:
    """Remove the workspace. The branch goes too when it is merged, or when
    ``drop_branch`` (task closed / deleted)."""
    b = branch(task_id)
    trees = worktrees(lay, task_id)
    for name, p in sorted(trees, key=lambda t: t[0] == ""):   # sub-repos before the root
        main = _main_repo(lay, name)
        _git(main, "worktree", "remove", "--force", str(p), check=False)
        _git(main, "branch", "-D" if drop_branch else "-d", b, check=False)
    shutil.rmtree(lay.ws(task_id), ignore_errors=True)
    for name in [""] + sub_repos(lay.main_root):
        if _is_repo(_main_repo(lay, name)):
            _git(_main_repo(lay, name), "worktree", "prune", check=False)


def task_ids(lay: Layout) -> list[int]:
    if not lay.wt_root.is_dir():
        return []
    return sorted(int(d.name) for d in lay.wt_root.iterdir() if d.name.isdigit())


def stage_env(lay: Layout, task_id: int, base: dict) -> dict:
    """Stage environment: env vars pointing at the live checkout (e.g.
    HOST_<PROJECT>_ROOT, used for ``docker run -v``) point at the workspace."""
    ws = str(lay.ws(task_id))
    main = str(lay.main_root).rstrip("/")
    env = dict(base)
    for k, v in os.environ.items():
        if k != "WORKER_HOST_ROOT" and v.rstrip("/") == main:
            env[k] = ws
    env["AT_TASK_WORKSPACE"] = ws
    return env


def prompt_section(lay: Layout, task_id: int) -> str:
    ws = lay.ws(task_id)
    b = branch(task_id)
    trees = [n or "(Projekt-Wurzel)" for n, _ in worktrees(lay, task_id)]
    return f"""### Your workspace — isolated (binding, overrides paths in your role text)
You work in `{ws}` on branch `{b}`: a git worktree per repo of this task
({", ".join(trees) or "none"}), not the live checkout `{lay.main_root}`.
Other folders in it are links to the live checkout: read them, never write
into them. To change another repo, first make it a worktree:
`rm {ws}/<repo> && git -C {lay.main_root}/<repo> worktree add {ws}/<repo> {b} 2>/dev/null || git -C {lay.main_root}/<repo> worktree add -b {b} {ws}/<repo>`.
- Paths in your role text under `{lay.main_root}` or `/workspaces/...` mean
  this workspace (`$AT_TASK_WORKSPACE`); test containers mount from here,
  e.g. `docker run --rm -v {ws}/<repo>:/app …`.
- You need not commit: the worker commits what you changed on `{b}` after
  your stage. Never push, never merge into the live branch, never open a
  pull request — the user's "Übernehmen" does that. If your goal is
  to resolve a conflict with the live branch: `git -C {ws}/<repo> rebase <live
  branch>` in the worktree, resolve, `git rebase --continue`.
- Never start, build or recreate live services from here (`docker compose
  up/build` of a real service): the live system runs from the live checkout.
  Deploying is the release stage's job after the user approved merging."""

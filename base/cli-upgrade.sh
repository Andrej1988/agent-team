#!/usr/bin/env bash
# Upgrade both agent CLIs (claude-code, codex) to the latest npm release, in
# place, into a user-writable prefix.
#
# Sourced by the container entrypoints at start, and callable later while a
# container runs:
#   * worker.py runs it every WORKER_CLI_REFRESH_H hours when no
#     task is in flight, so a long-lived container doesn't sit on the CLI it
#     booted with (it sat on 2.1.270 for 10 days while Opus 5.5 needed 2.1.280+);
#   * the Tasks UI "upgrade CLIs" button runs it via docker exec
#     (POST /api/tasks/models/upgrade).
#
# Why a user prefix: the image-baked CLI lives in /usr/lib/node_modules
# (root-owned) and devuser can't touch it. ~/.npm-global/bin is prepended to
# PATH and shadows the system CLI once a per-user install succeeded. Every
# spawn of `claude` / `codex` resolves the binary afresh, so an in-place
# upgrade reaches the next session without a restart; sessions already
# running keep the binary they loaded.
#
# Failures are tolerated: no network = keep whatever is installed. Exit code
# is 0 unless *both* upgrades failed, so callers can log but needn't abort.
# Output goes to stderr, one line per CLI, prefixed [cli-upgrade].
export NPM_CONFIG_PREFIX="${NPM_CONFIG_PREFIX:-$HOME/.npm-global}"
mkdir -p "$NPM_CONFIG_PREFIX/bin" 2>/dev/null || true
case ":$PATH:" in
  *":$NPM_CONFIG_PREFIX/bin:"*) ;;
  *) export PATH="$NPM_CONFIG_PREFIX/bin:$PATH" ;;
esac

_cli_upgrade_all() {
  local spec bin pkg cur new ok=0 fail=0
  if ! command -v npm >/dev/null 2>&1; then
    echo "[cli-upgrade] npm not found — keeping image-baked CLIs" >&2
    return 1
  fi
  for spec in claude:@anthropic-ai/claude-code codex:@openai/codex; do
    bin="${spec%%:*}"; pkg="${spec#*:}"
    cur="$("$bin" --version 2>/dev/null | head -n1)"
    if npm install -g --no-fund --no-audit --silent "${pkg}@latest" >/dev/null 2>&1; then
      new="$("$bin" --version 2>/dev/null | head -n1)"
      if [ "$new" = "$cur" ]; then
        echo "[cli-upgrade] $bin CLI unchanged at ${new:-unknown} (user prefix)" >&2
      else
        echo "[cli-upgrade] $bin CLI ${cur:-unknown} → ${new:-unknown} (user prefix)" >&2
      fi
      ok=$((ok + 1))
    else
      echo "[cli-upgrade] $bin CLI upgrade failed — continuing with ${cur:-the image-baked CLI}" >&2
      fail=$((fail + 1))
    fi
  done
  [ "$ok" -gt 0 ]
}

# Sourced (entrypoints) → only define + fix PATH, then run; executed
# (worker / docker exec) → run and propagate the exit code.
_cli_upgrade_all
_cli_upgrade_rc=$?
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  exit "$_cli_upgrade_rc"
fi

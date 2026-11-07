#!/usr/bin/env bash
# Commit guard for a public repo: refuses commits whose added lines, commit
# message or author/committer identity contain a term from your private
# denylist, or something that looks like a secret.
#
# Install (once per clone): tools/check-private.sh --install
#   links it as pre-commit, commit-msg and pre-push and creates an empty
#   denylist to fill in. pre-push checks every commit about to be pushed again,
#   so a commit made with --no-verify or in a clone without the hook is caught.
# Denylist (one case-insensitive extended regex per line, # = comment), kept
# OUT of the repo: $AT_PRIVATE_DENYLIST or <git-dir>/info/private-denylist.
set -euo pipefail

gitdir=$(git rev-parse --git-common-dir)
list=${AT_PRIVATE_DENYLIST:-$gitdir/info/private-denylist}
hook=$(basename "$0")

if [ "${1:-}" = "--install" ]; then
    hooks=$(git rev-parse --git-path hooks)
    mkdir -p "$hooks" "$(dirname "$list")"
    for h in pre-commit commit-msg pre-push; do
        ln -sf "$(git rev-parse --show-toplevel)/tools/check-private.sh" "$hooks/$h"
    done
    if [ ! -f "$list" ]; then
        printf '%s\n' "# One case-insensitive extended regex per line: your names, user names," \
            "# home paths, domains, IPs, mail addresses, project names. Never commit this file." >"$list"
    fi
    echo "check-private: Hooks installiert (pre-commit, commit-msg, pre-push); Sperrliste: $list"
    exit 0
fi

if ! grep -qvE '^[[:space:]]*(#|$)' "$list" 2>/dev/null; then
    echo "check-private: WARNUNG — keine Sperrliste ($list). Nur Secrets werden geprüft," >&2
    echo "  persönliche Begriffe nicht. Anlegen: tools/check-private.sh --install, dann füllen." >&2
fi

secret_re='(BEGIN [A-Z ]*PRIVATE KEY|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-ant-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|[0-9]{8,10}:AA[A-Za-z0-9_-]{30,})'

check() {   # $1 = label, stdin = text
    local text hits
    text=$(cat)
    if [ -f "$list" ]; then
        while IFS= read -r term; do
            [[ -z "$term" || "$term" == \#* ]] && continue
            hits=$(grep -inE -- "$term" <<<"$text" | head -3 || true)
            if [ -n "$hits" ]; then
                echo "check-private: $1 enthält einen privaten Begriff (/$term/):" >&2
                echo "$hits" | cut -c1-160 >&2
                return 1
            fi
        done <"$list"
    fi
    hits=$(grep -nE -- "$secret_re" <<<"$text" | head -3 || true)
    if [ -n "$hits" ]; then
        echo "check-private: $1 sieht nach einem Secret aus:" >&2
        echo "$hits" | cut -c1-160 >&2
        return 1
    fi
}

case "$hook" in
  commit-msg)
    check "Commit-Nachricht" <"$1"
    ;;
  pre-push)
    zero=0000000000000000000000000000000000000000
    while read -r _lref lsha _rref rsha; do
        [ "$lsha" = "$zero" ] && continue                     # deleting a remote branch
        if [ "$rsha" != "$zero" ] && git cat-file -e "$rsha^{commit}" 2>/dev/null; then
            commits=$(git rev-list "$rsha..$lsha")
        else
            commits=$(git rev-list "$lsha" --not --remotes)     # new branch: what no remote has
        fi
        for c in $commits; do
            git show --no-color -U0 --format= "$c" | { grep -E '^\+[^+]|^\+$' || true; } | check "Commit ${c:0:9}, Diff" || exit 1
            git show --no-color --name-only --format= "$c" | check "Commit ${c:0:9}, Dateiname" || exit 1
            git log -1 --format='%an <%ae>%n%cn <%ce>%n%B' "$c" | check "Commit ${c:0:9}, Nachricht/Autor" || exit 1
        done
    done
    ;;
  *)
    git diff --cached --no-color -U0 --diff-filter=ACMR | { grep -E '^\+[^+]|^\+$' || true; } | check "Diff" || exit 1
    git diff --cached --name-only --diff-filter=ACMR | check "Dateiname" || exit 1
    printf '%s\n%s\n' "$(git var GIT_AUTHOR_IDENT)" "$(git var GIT_COMMITTER_IDENT)" | check "Autor/Committer" || exit 1
    ;;
esac

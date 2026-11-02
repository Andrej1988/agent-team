#!/usr/bin/env bash
# Commit guard for a public repo: refuses commits whose added lines, commit
# message or author/committer identity contain a term from your private
# denylist, or something that looks like a secret.
#
# Install (once per checkout):
#   ln -sf ../../tools/check-private.sh .git/hooks/pre-commit
#   ln -sf ../../tools/check-private.sh .git/hooks/commit-msg
# Denylist (one case-insensitive extended regex per line, # = comment), kept
# OUT of the repo: $AT_PRIVATE_DENYLIST or <git-dir>/info/private-denylist.
set -euo pipefail

gitdir=$(git rev-parse --git-common-dir)
list=${AT_PRIVATE_DENYLIST:-$gitdir/info/private-denylist}
hook=$(basename "$0")

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
  *)
    git diff --cached --no-color -U0 --diff-filter=ACMR | grep -E '^\+[^+]|^\+$' | check "Diff" || exit 1
    git diff --cached --name-only --diff-filter=ACMR | check "Dateiname" || exit 1
    printf '%s\n%s\n' "$(git var GIT_AUTHOR_IDENT)" "$(git var GIT_COMMITTER_IDENT)" | check "Autor/Committer" || exit 1
    ;;
esac

#!/usr/bin/env bash
# git-guard: commits a nombre de David y sin atribución a IA. Uso: git-guard.sh [setup|check|fix]
NAME="David Santiago Rincón Bautista"
EMAIL="davidsantiagorb@ufps.edu.co"
AI_RE='^[A-Za-z-]*-[Bb]y:.*([Cc]laude|[Aa]nthropic)|^[Cc]laude-[Ss]ession:|[Gg]enerated with .*[Cc]laude'
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

setup() {
  git config user.name "$NAME" && git config user.email "$EMAIL" || return 1
  if [ "${CLAUDE_CODE_REMOTE:-}" = true ] || [ "$(git config --global user.email)" = noreply@anthropic.com ]; then
    git config commit.gpgsign false   # la nube firma con la clave de Anthropic
  fi
  local top hp d; top="$(git rev-parse --show-toplevel)"; hp="$(git config core.hooksPath)"
  [ -d "$top/.husky" ] && wire "$top/.husky"   # husky corre .husky/<gancho>, aunque se instale después
  if [ -n "$hp" ]; then   # con core.hooksPath, git ignora .git/hooks
    case "$hp" in /*) d="$hp" ;; *) d="$top/$hp" ;; esac
    [ "$d" = "$top/.husky/_" ] || wire "$d"
    return 0
  fi
  d="$(git rev-parse --git-common-dir)/hooks"; mkdir -p "$d"
  if [ ! -e "$d/commit-msg" ] || grep -q git-guard "$d/commit-msg"; then
    printf '#!/bin/sh\n# git-guard\ngrep -vE %s "$1" > "$1.tmp"; mv "$1.tmp" "$1"\n' "'$AI_RE'" > "$d/commit-msg"
    chmod +x "$d/commit-msg"
  fi
  if [ ! -e "$d/pre-push" ] || grep -q git-guard "$d/pre-push"; then
    # busca el script en el repo que hace push (sirve en worktrees ya borrados) y si no, donde corrió setup
    printf '#!/bin/sh\n# git-guard\nfor g in "$(git rev-parse --show-toplevel)/.claude/hooks/git-guard.sh" "$(git rev-parse --git-common-dir)/git-guard.sh" "%s"; do\n  [ -f "$g" ] && exec bash "$g" pre-push\ndone\nexit 0\n' "$SELF" > "$d/pre-push"
    chmod +x "$d/pre-push"
  fi
}

wire() {   # ganchos versionados (husky u otra carpeta): llaman a este script; en un repo colaborativo no se tocan
  case "$SELF" in
    */.claude/hooks/git-guard.sh) ;;
    *) echo "git-guard: los ganchos de $1 no se tocan en un repo colaborativo; corre check antes de push" >&2; return 0 ;;
  esac
  local h f
  for h in commit-msg pre-push; do
    f="$1/$h"; grep -qs git-guard "$f" && continue
    [ -s "$f" ] || printf '#!/bin/sh\n' > "$f"
    printf 'bash "$(git rev-parse --show-toplevel)/.claude/hooks/git-guard.sh" %s "$@"   # git-guard\n' "$h" >> "$f"
    chmod +x "$f"
  done
}

check() {   # revisa solo commits que no están en ningún remoto
  [ $# -eq 0 ] && set -- HEAD
  local c bad=0
  for c in $(git rev-list "$@" --not --remotes 2>/dev/null); do
    if [ "$(git log -1 --format='%ae|%ce' "$c")" != "$EMAIL|$EMAIL" ] \
       || git log -1 --format='%an%n%cn' "$c" | grep -qiE 'claude|anthropic'; then
      echo "git-guard: $(git log -1 --format='%h autor: %an <%ae> | committer: %cn <%ce>' "$c")"; bad=1
    fi
    if git log -1 --format=%B "$c" | grep -qE "$AI_RE"; then
      echo "git-guard: $(git log -1 --format=%h "$c") menciona a la IA en el mensaje"; bad=1
    fi
  done
  if [ "$bad" = 0 ]; then echo "git-guard: autoría OK"; else echo "git-guard: corrige con: bash \"$SELF\" fix"; fi
  return "$bad"
}

pre_push() {   # git entrega por stdin: <ref local> <sha local> <ref remoto> <sha remoto>
  local l s r rs revs=()
  while read -r l s r rs; do case "$s" in *[!0]*) revs+=("$s") ;; esac; done
  [ ${#revs[@]} -eq 0 ] || check "${revs[@]}"
}

fix() {   # reescribe solo commits sin publicar y lineales
  setup
  local commits oldest base copy
  commits="$(git rev-list HEAD --not --remotes)"
  [ -z "$commits" ] && { echo "git-guard: no hay commits sin publicar"; return 0; }
  if [ -n "$(git rev-list --merges HEAD --not --remotes)" ]; then
    echo "git-guard: hay merges sin publicar; corrígelos a mano"; return 1
  fi
  oldest="$(printf '%s\n' "$commits" | tail -n 1)"
  if git rev-parse -q --verify "$oldest^" >/dev/null; then base="$oldest^"; else base="--root"; fi
  copy="$(git rev-parse --git-dir)/git-guard-fix.sh"; cp "$SELF" "$copy"   # el script puede no existir en commits viejos
  git rebase --autostash --exec "bash \"$copy\" amend-one" "$base"
}

amend_one() {
  local m; m="$(git rev-parse --git-dir)/GIT_GUARD_MSG"
  git log -1 --format=%B | grep -vE "$AI_RE" > "$m"
  git commit --amend --no-verify --no-gpg-sign --reset-author -q -F "$m" && rm -f "$m"
}

case "${1:-setup}" in
  setup) setup && echo "git-guard: commits como $NAME <$EMAIL>" ;;
  check) shift; check "$@" ;;
  pre-push) pre_push ;;
  commit-msg) [ ! -f "${2:-}" ] || { grep -vE "$AI_RE" "$2" > "$2.tmp"; mv "$2.tmp" "$2"; } ;;
  fix) fix && check ;;
  amend-one) amend_one ;;
  *) echo "uso: git-guard.sh [setup|check|fix]" >&2; exit 2 ;;
esac

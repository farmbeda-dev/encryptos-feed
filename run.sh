#!/usr/bin/env bash
# ENCRYPTOS — run.sh (repo sem historico: 1 commit unico sempre)
set -uo pipefail
cd /opt/encryptos || exit 1
export HOME=/root

# 1) coleta
/usr/bin/python3 collector.py

# 2) publica reescrevendo o historico (repo fica leve pra sempre)
if [ -n "$(git status --porcelain out)" ]; then
  git checkout --orphan tmp-branch
  git add -A
  git commit -q -m "feed $(date -u +%FT%TZ)"
  git branch -D main 2>/dev/null || true
  git branch -m main
  if git push -qf origin main; then
    echo "$(date -u +%FT%TZ) publicado ok (sem historico)"
  else
    echo "$(date -u +%FT%TZ) ERRO no push"
  fi
else
  echo "$(date -u +%FT%TZ) nada novo p/ publicar"
fi

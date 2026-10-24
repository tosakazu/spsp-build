#!/usr/bin/env bash
# tests/run_all.sh — 速いテストを全部回す (Python plain-assert)。
#   bash tests/run_all.sh
# フロント (HTML/JS) と、ビルド出力をフロントが読めるかの突き合わせ (tests/split・tests/sim 相当) は spsp-web の npm test / test:all (2026-09-29 にフロントを spsp-web へ移した)。
# ビルド全体の同一性 (golden 比較、約 10 分) は tests/golden/build_and_compare.sh を別途。
set -u
cd "$(dirname "$0")/.."
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
export TZ=Asia/Tokyo   # 本番と同じ (build_v4_full.py / deploy/paths.sh も固定)
# venv: .venv (repo 直下、requirements.lock.txt で作る) → ranking_eval/.venv (旧置き場) の順 (deploy/paths.sh と同じ)
PY=.venv/bin/python3; [ -x "$PY" ] || PY=ranking_eval/.venv/bin/python3
[ -x "$PY" ] || { echo "ERROR: venv が無い (.venv)。python3 -m venv .venv && .venv/bin/pip install -r requirements.lock.txt"; exit 2; }
WHAT="${1:-all}"
fail=0; n=0

if [[ "$WHAT" == all || "$WHAT" == py ]]; then
  echo "── Python (plain assert) ──"
  for t in tests/meta/*.py tests/learn/test_*.py tests/post/test_char_vote.py; do
    n=$((n+1))
    if out=$("$PY" "$t" 2>&1); then echo "  ok   $t"; else echo "  FAIL $t"; echo "$out" | tail -5 | sed 's/^/       /'; fail=$((fail+1)); fi
  done
fi

if [[ "$WHAT" == all || "$WHAT" == deploy ]]; then
  echo "── deploy (rclone の local backend に向けて。rclone が無ければ飛ばす) ──"
  for t in tests/deploy/test_*.sh; do
    if [ ! -x "${RCLONE:-$HOME/bin/rclone}" ]; then echo "  skip $t (rclone が無い)"; continue; fi
    n=$((n+1))
    if out=$(bash "$t" 2>&1); then echo "  ok   $t"; else echo "  FAIL $t"; echo "$out" | grep -v '^      ' | tail -8 | sed 's/^/       /'; fail=$((fail+1)); fi
  done
fi

echo "── $((n-fail))/$n ok ──"
exit $(( fail > 0 ))

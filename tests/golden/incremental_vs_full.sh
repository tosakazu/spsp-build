#!/usr/bin/env bash
# tests/golden/incremental_vs_full.sh — フル学習と、日付で分割して保存・再開した差分学習の出力が完全一致するかを検査する。
#
#   bash tests/golden/incremental_vs_full.sh --inputs ~/spsp-golden/20260905 --work ~/spsp-verify/inc_test [--date 2026-09-05] [--no-wait] [--lag 45]
#   --lag: 差分側の最後のチェックポイントを何日前に置くか (既定 45 = ConoHa。GitHub Actions は 14)
#
# やること (本番の update_and_deploy.sh [3/5] と同じ手順):
#   FULL: --phase learn (最初から全部) → --phase snap → --phase build → aux 8 本 → deployed_full/
#   INC : --until 2019-12-31 → --until 2021-12-31 → … → --until (eval_date-45日) を state で繋ぎ、
#         最後に --phase learn --state-in で残りを学習 → snap → build → aux → deployed_inc/
#   比較: deployed_full と deployed_inc が一致 (compare_trees.py: JSON は内容一致、他はバイト一致、生成時刻 3 ファイルは時刻を除く)
#   STALE: チェックポイント以前の大会の standings.json を書き換えたデータで再開 → 終了コード 3 で止まること
# 所要 約 20 分。本番の site/ やデータには書かない。
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INPUTS=""; WORK=""; DATE=""; WAIT=1; LAG=45
while [[ $# -gt 0 ]]; do
  case "$1" in
    --inputs) INPUTS="$2"; shift 2;; --work) WORK="$2"; shift 2;; --date) DATE="$2"; shift 2;; --no-wait) WAIT=0; shift;; --lag) LAG="$2"; shift 2;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done
[[ -n "$INPUTS" && -n "$WORK" ]] || { sed -n '2,14p' "$0"; exit 2; }
log(){ echo "[$(date '+%F %T')] $*"; }
if [[ $WAIT -eq 1 ]]; then
  while :; do m=$(( ($(date +%-H) % 3) * 60 + $(date +%-M) )); [ $m -ge 25 ] && break; log "nightly の時間帯 — 待機"; sleep 120; done
fi
[[ -z "$DATE" ]] && DATE=$(date +%F)
R="$WORK/repo"; mkdir -p "$WORK"
log "コードをコピー → $R"
rsync -a --delete --exclude .git --exclude 'site/players' --exclude 'site/tournaments' --exclude experiments/simlab \
  --exclude smash_db_tournament --exclude smash_db_user --exclude ranking_eval --exclude .venv --exclude __pycache__ "$REPO/" "$R/"
# 凍結入力は旧レイアウト (startgg/events/Japan/ と startgg/ 直下の users.jsonl 等)。今のコードは startgg/Japan/{events,users.jsonl,...} を読むので割り付ける
PY="$R/.venv/bin/python3"
link_startgg() {   # $1 = 入力の data_tournament_startgg, $2 = 作業 repo の data/startgg
  rm -rf "$2"; mkdir -p "$2/Japan"
  # events はコピー (凍結入力に derived.json を書かないため。cp -a で mtime を保つ)。判定 (derive) は本番と同じ手順で走らせる
  cp -a "$1/events/Japan" "$2/Japan/events"
  ln -sfn "$REPO/smash_db_tournament/scripts" "$(dirname "$2")/../scripts"    # smash_db_tournament/scripts (判定モジュール)
  for f in done.csv done_events.csv tournaments.jsonl users.jsonl; do [ -e "$1/$f" ] && ln -sfn "$1/$f" "$2/Japan/$f"; done
  [ -d "$1/manual" ] && ln -sfn "$1/manual" "$2/Japan/manual"    # 手動管理の表 (user_merges / overseas_manual / awaiting_resume)
  # 判定 (derived.json) を本番と同じ手順で作る (凍結入力には無い)。判定モジュールは今の作業ツリーのもの
  "$PY" -u "$R/smash_db_tournament/scripts/common/derive.py" --region Japan --events-root "$2/Japan/events" --users-file-path "$2/Japan/users.jsonl" --all --quiet || { echo "ERROR: curate 失敗"; exit 13; }
  return 0
}
mkdir -p "$R/smash_db_tournament/data" "$R/smash_db_user/data" "$R/site/players" "$R/site/tournaments" "$R/site/data" "$R/build/data"
ln -sfn "$REPO/.venv" "$R/.venv"
link_startgg "$INPUTS/data_tournament_startgg" "$R/smash_db_tournament/data/startgg"
ln -sfn "$INPUTS/data_user_startgg" "$R/smash_db_user/data/startgg"
cp "$INPUTS/build/data/overseas.json" "$INPUTS/build/data/upcoming.json" "$INPUTS/build/data/upcoming_entrants.json" "$R/site/data/"
cp "$INPUTS/build_inputs/char_votes.json" "$R/build/data/"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 TZ=Asia/Tokyo
unset PYTHONHASHSEED
for v in $(env | grep -oE '^(SPSP_|BT_|PROV_|WARMUP)[A-Z0-9_]*'); do unset "$v"; done
cd "$R"
AUX="build_tournaments_index build_series_json build_player_prefectures build_tournament_prefectures build_player_subranks build_discriminators_json build_user_merges_json"   # last_activity はビルドが書く
finish_build() {   # $1 = BUILD_DIR (中に _learn_state.pkl), $2 = deployed dir
  "$PY" -u spsp/cli/build_full.py "$DATE" "$1" --phase snap --state-in "$1/_learn_state.pkl" > "$WORK/$(basename "$2")_snap.log" 2>&1 || { log "snap 失敗 → $WORK/$(basename "$2")_snap.log"; exit 11; }
  "$PY" -u spsp/cli/build_full.py "$DATE" "$1" --phase build > "$WORK/$(basename "$2")_build.log" 2>&1 || { log "build 失敗 → $WORK/$(basename "$2")_build.log"; exit 11; }
  # aux 8 本 (spsp/cli/build_aux.py が本番と同じ順で呼ぶ)。rc=2 = 任意ステップだけの失敗 (凍結入力に upcoming.json が
  # 無いなど) は続ける (build_and_compare.sh と同じ扱い。両側とも同じ失敗なので比較には影響しない)
  local arc=0; "$PY" -u spsp/cli/build_aux.py --build-dir "$1" >> "$WORK/$(basename "$2")_aux.log" 2>&1 || arc=$?
  [[ $arc -eq 0 || $arc -eq 2 ]] || { log "aux 失敗 (rc=$arc)"; exit 12; }
  rsync -aL --delete --exclude .netlify --exclude .DS_Store --exclude demo --exclude '*.pkl' "$1/" "$2/"
}

# ── FULL ──
BF="$WORK/build_full"; rm -rf "$BF"
# FULL 側も 1 プロセスだと約 260 秒で CPU 制限 (300 秒) に近いので 2023 年末で 2 プロセスに分ける
# (INC 側は 45 日前のチェックポイントから。分け方が違えば「フル = 差分」の検証として十分)
log "FULL learn (〜2023-12-31)"; "$PY" -u spsp/cli/build_full.py "$DATE" "$BF" --phase learn --until 2023-12-31 --state-out "$WORK/_full_2023.pkl" > "$WORK/full_learn1.log" 2>&1 || { log "FULL learn(1) 失敗 → $WORK/full_learn1.log"; exit 10; }
log "FULL learn (残り)"; "$PY" -u spsp/cli/build_full.py "$DATE" "$BF" --phase learn --state-in "$WORK/_full_2023.pkl" --state-out "$BF/_learn_state.pkl" > "$WORK/full_learn.log" 2>&1 || { log "FULL learn 失敗 → $WORK/full_learn.log"; exit 10; }
rm -f "$WORK/_full_2023.pkl"
log "FULL snap + build + aux"; finish_build "$BF" "$WORK/deployed_full"

# ── INC ──
BI="$WORK/build_inc"; CK="$WORK/ck"; rm -rf "$BI" "$CK"; mkdir -p "$CK"
CK_DATE=$(date -d "$DATE -${LAG} days" +%F)
CUTS=""; for y in $(seq 2019 $(( ${CK_DATE:0:4} - 1 ))); do c="$y-12-31"; [[ "$c" < "$CK_DATE" ]] && CUTS="$CUTS $c"; done; CUTS="$CUTS $CK_DATE"
SIN=""
for c in $CUTS; do
  log "INC learn until $c ${SIN:+(from $(basename "$SIN"))}"
  args=(--phase learn --until "$c" --state-out "$CK/state_$c.pkl"); [[ -n "$SIN" ]] && args+=(--state-in "$SIN")
  "$PY" -u spsp/cli/build_full.py "$DATE" "$BI" "${args[@]}" > "$WORK/inc_learn_$c.log" 2>&1 || { log "INC learn until $c 失敗 (rc=$?) → $WORK/inc_learn_$c.log"; exit 13; }
  SIN="$CK/state_$c.pkl"
done
log "INC learn (rest, from $(basename "$SIN"))"
"$PY" -u spsp/cli/build_full.py "$DATE" "$BI" --phase learn --state-in "$SIN" --state-out "$BI/_learn_state.pkl" > "$WORK/inc_learn_final.log" 2>&1 || { log "INC learn 失敗 → $WORK/inc_learn_final.log"; exit 14; }
log "INC snap + build + aux"; finish_build "$BI" "$WORK/deployed_inc"

# ── 比較 ──
log "比較 FULL vs INC (内容一致: tests/golden/compare_trees.py)"
"$PY" "$REPO/tests/golden/compare_trees.py" "$WORK/deployed_full" "$WORK/deployed_inc" --label 'OK: フル学習と差分学習の出力が完全一致'
RC=$?
[[ $RC -eq 0 ]] || { log "DONE rc=$RC"; exit $RC; }

# ── STALE: チェックポイント以前の大会を書き換えると再開を拒否する ──
log "STALE テスト"
DM="$WORK/data_mod"; rm -rf "$DM"
# ハードリンクで写す (速い)。作業場所が別のファイルシステムでリンクできなければ普通に写す
cp -al "$INPUTS/data_tournament_startgg" "$DM" 2>/dev/null || { rm -rf "$DM"; cp -a "$INPUTS/data_tournament_startgg" "$DM"; }
# 書き換える大会 = 最初のチェックポイント (2019-12-31) より前で、学習に載る大会 (class_phases 以外、8 人以上)
first=$(echo $CUTS | awk '{print $1}'); second=$(echo $CUTS | awk '{print $2}')
TARGET=""
for f in $(find "$DM/events/Japan/${first:0:4}" -name standings.json -not -path '*class_phases*' | sort); do
  n=$("$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); r=d['data'] if isinstance(d,dict) and 'data' in d else d; print(len(r))" "$f" 2>/dev/null || echo 0)
  [[ "$n" -ge 8 ]] && { TARGET="$f"; break; }
done
[[ -n "$TARGET" ]] || { log "${first:0:4} 年の standings.json が見つからない"; exit 15; }
"$PY" - "$TARGET" <<'PYEOF'
import json, sys, os
p = sys.argv[1]; d = json.load(open(p))
rows = d['data'] if isinstance(d, dict) and 'data' in d else d
assert len(rows) >= 2
rows.pop()                      # 最下位を 1 人消す (= 内容が変わる)
tmp = p + '.new'
with open(tmp, 'w', encoding='utf-8') as f: json.dump(d, f, ensure_ascii=False)
os.replace(tmp, p)              # hardlink を切って置き換える (元データは無傷)
PYEOF
link_startgg "$DM" "$R/smash_db_tournament/data/startgg"
"$PY" -u spsp/cli/build_full.py "$DATE" "$BI" --phase learn --until "$second" --state-in "$CK/state_$first.pkl" --state-out "$WORK/state_stale_test.pkl" > "$WORK/stale.log" 2>&1
SRC=$?
link_startgg "$INPUTS/data_tournament_startgg" "$R/smash_db_tournament/data/startgg"
if [[ $SRC -eq 3 ]]; then echo "OK: 過去大会 ($TARGET) を書き換えたデータでは再開を拒否 (rc=3)"; grep -m1 "ERROR" "$WORK/stale.log"
else echo "NG: 再開を拒否しなかった (rc=$SRC)"; tail -5 "$WORK/stale.log"; RC=1; fi
log "DONE rc=$RC"; exit $RC

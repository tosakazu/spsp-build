#!/usr/bin/env bash
# tests/golden/build_and_compare.sh — 凍結した入力から本番と同じ手順でビルドし、凍結した出力と内容比較する。
#
#   bash tests/golden/build_and_compare.sh --inputs ~/spsp-golden/20260905 \
#        --expect ~/spsp-golden/20260905-refactor/build --work ~/spsp-verify/mytest [--date 2026-09-05] [--no-wait]
#        [--extra-args "--snapshot-grid legacy"]   (build_full.py へ渡す追加引数)
#
# --inputs: 凍結した入力。以下を含むこと
#     data_tournament_startgg/   (= smash_db_tournament/data/startgg)
#     data_user_startgg/         (= smash_db_user/data/startgg)
#     build/data/{overseas.json,upcoming.json,upcoming_entrants.json}   (ビルド前に生成される site/data の入力)
#     build_inputs/char_votes.json
# --expect: 期待する出力 (deploy_v4.sh が rsync する形 = BUILD_DIR を rsync -aL したもの)
# --work:   作業ディレクトリ (本番の site/ やデータには一切書かない。コードは今の作業ツリーをコピーする)
#
# 合格 = JSON は内容一致 (キー順・型込み。空白と浮動小数の表記だけ無視: compare_trees.py)、それ以外はバイト一致、
#        meta.json / data/tournaments.json / data/series.json は生成時刻を除いて一致。
# 所要 約 12 分 (ConoHa の CPU 制限のため learn / snap / build は本番同様 3 プロセス)。
# nightly (3 時間おき、0 分〜25 分) と重ならないよう既定で待つ (--no-wait で無効)。
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INPUTS=""; EXPECT=""; WORK=""; DATE=""; WAIT=1; EXTRA=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --inputs) INPUTS="$2"; shift 2;; --expect) EXPECT="$2"; shift 2;; --work) WORK="$2"; shift 2;;
    --date) DATE="$2"; shift 2;; --no-wait) WAIT=0; shift;;
    --extra-args) EXTRA="$2"; shift 2;;   # build_full.py に足す引数 (例: "--snapshot-grid legacy")
    *) echo "unknown arg: $1"; exit 2;;
  esac
done
[[ -n "$INPUTS" && -n "$EXPECT" && -n "$WORK" ]] || { sed -n '2,20p' "$0"; exit 2; }
for d in data_tournament_startgg data_user_startgg build/data build_inputs; do [[ -e "$INPUTS/$d" ]] || { echo "ERROR: $INPUTS/$d が無い"; exit 2; }; done
[[ -f "$EXPECT/meta.json" ]] || { echo "ERROR: $EXPECT/meta.json が無い"; exit 2; }
[[ -z "$DATE" ]] && DATE=$("$REPO/.venv/bin/python3" -c "import json;print(json.load(open('$EXPECT/meta.json'))['eval_date'])")
log(){ echo "[$(date '+%F %T')] $*"; }
if [[ $WAIT -eq 1 ]]; then
  while :; do m=$(( ($(date +%-H) % 3) * 60 + $(date +%-M) )); [ $m -ge 25 ] && break; log "nightly の時間帯 — 待機"; sleep 120; done
fi

# 1. 作業ツリーのコードを隔離コピー + 凍結入力を symlink
R="$WORK/repo"; B="$WORK/build"; mkdir -p "$WORK"
log "コードをコピー → $R"
rsync -a --delete --exclude .git --exclude 'site/players' --exclude 'site/tournaments' --exclude experiments/simlab \
  --exclude smash_db_tournament --exclude smash_db_user --exclude ranking_eval --exclude .venv --exclude __pycache__ "$REPO/" "$R/"
mkdir -p "$R/smash_db_tournament/data" "$R/smash_db_user/data" "$R/site/players" "$R/site/tournaments" "$R/build/data"
# 凍結入力は旧レイアウト (startgg/events/Japan/ と startgg/ 直下の users.jsonl 等)。今のコードは startgg/Japan/{events,users.jsonl,...} を読むので割り付ける
rm -rf "$R/smash_db_tournament/data/startgg"; mkdir -p "$R/smash_db_tournament/data/startgg/Japan"
cp -a "$INPUTS/data_tournament_startgg/events/Japan" "$R/smash_db_tournament/data/startgg/Japan/events"   # コピー (凍結入力に derived.json を書かない)
ln -sfn "$REPO/smash_db_tournament/scripts" "$R/smash_db_tournament/scripts"    # 判定モジュール (scripts/Japan/classify.py)
for f in done.csv done_events.csv tournaments.jsonl users.jsonl; do [ -e "$INPUTS/data_tournament_startgg/$f" ] && ln -sfn "$INPUTS/data_tournament_startgg/$f" "$R/smash_db_tournament/data/startgg/Japan/$f"; done
[ -d "$INPUTS/data_tournament_startgg/manual" ] && ln -sfn "$INPUTS/data_tournament_startgg/manual" "$R/smash_db_tournament/data/startgg/Japan/manual"    # 手動管理の表
ln -sfn "$INPUTS/data_user_startgg" "$R/smash_db_user/data/startgg"
ln -sfn "$REPO/.venv" "$R/.venv"
mkdir -p "$R/site/data" && cp "$INPUTS/build/data/overseas.json" "$INPUTS/build/data/upcoming.json" "$INPUTS/build/data/upcoming_entrants.json" "$R/site/data/"
cp "$INPUTS/build_inputs/char_votes.json" "$R/build/data/"
PY="$R/.venv/bin/python3"
# 判定 (derived.json) を本番と同じ手順で作る (凍結入力には無い)。判定モジュールは今の作業ツリーのもの
"$PY" -u "$R/smash_db_tournament/scripts/common/derive.py" --region Japan --events-root "$R/smash_db_tournament/data/startgg/Japan/events" --users-file-path "$R/smash_db_tournament/data/startgg/Japan/users.jsonl" --all --quiet || { echo "ERROR: curate 失敗"; exit 13; }
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 TZ=Asia/Tokyo
unset PYTHONHASHSEED
for v in $(env | grep -oE '^(SPSP_|BT_|PROV_|WARMUP)[A-Z0-9_]*'); do unset "$v"; done   # 本番と同じく環境変数は無し

# 2. 本番と同じ手順 ([3/5] learn → snap → build、[4/5] aux)
cd "$R"; rm -rf "$B"
# 全期間の学習は 1 プロセスだと約 260 秒で CPU 制限 (300 秒) に近いので、2023 年末で区切って 2 プロセス
# (= 本番の年ごとの再学習と同じ形。チェックポイントからの再開も兼ねて検証する)
log "learn (〜2023-12-31)"; "$PY" -u spsp/cli/build_full.py "$DATE" "$B" $EXTRA --phase learn --until 2023-12-31 --state-out "$WORK/_state_2023.pkl" > "$WORK/learn1.log" 2>&1 || { log "learn(1) 失敗 (rc=$?) → $WORK/learn1.log"; exit 10; }
log "learn (残り)";  "$PY" -u spsp/cli/build_full.py "$DATE" "$B" $EXTRA --phase learn --state-in "$WORK/_state_2023.pkl" --state-out "$B/_learn_state.pkl" > "$WORK/learn.log" 2>&1 || { log "learn 失敗 (rc=$?) → $WORK/learn.log"; exit 10; }
rm -f "$WORK/_state_2023.pkl"
cp "$B/_learn_state.pkl" "$WORK/_learn_state.pkl"
log "snap (cold)"; "$PY" -u spsp/cli/build_full.py "$DATE" "$B" $EXTRA --phase snap --state-in "$B/_learn_state.pkl" --snap-cache "$WORK/snap_cache" > "$WORK/snap.log" 2>&1 || { log "snap 失敗 (rc=$?) → $WORK/snap.log"; exit 10; }
# キャッシュありでもう一度 snap → inter のスナップショット部分が cold と一致するか (順序込み)
cp "$B/_inter_full.pkl" "$WORK/_inter_cold.pkl"; cp "$WORK/_learn_state.pkl" "$B/_learn_state.pkl"
log "snap (warm)"; "$PY" -u spsp/cli/build_full.py "$DATE" "$B" $EXTRA --phase snap --state-in "$B/_learn_state.pkl" --snap-cache "$WORK/snap_cache" > "$WORK/snap_warm.log" 2>&1 || { log "snap(warm) 失敗 → $WORK/snap_warm.log"; exit 10; }
"$PY" - "$WORK/_inter_cold.pkl" "$B/_inter_full.pkl" <<'PYEOF' || exit 10
import json, sys, hashlib, gc
sys.path.insert(0, '.')   # inter の中の Tournament (spsp.data_loader) を復元するため
from spsp.learn_state import load_inter   # 値ごとに別の pickle (旧形式も読める)
KEYS = ('cascade_snapshots', 'peak180_at_day', 'measured_at_day', 'per_tour_by_lv')
def digest(path):   # 2 つを同時にメモリに持たない (300 MB の pickle × 2 だと Killed)
    d = load_inter(open(path, 'rb'))
    out = {k: hashlib.blake2b(json.dumps(d[k], sort_keys=False, default=sorted).encode(), digest_size=16).hexdigest() for k in KEYS}
    del d; gc.collect(); return out
a = digest(sys.argv[1]); b = digest(sys.argv[2])
for k in KEYS:
    assert a[k] == b[k], f'snap cache: {k} が cold と違う'
print('  snap-cache: warm == cold (cascade_snapshots / peak180_at_day / measured / per_tour_by_lv, 順序込み)')
PYEOF
grep -o "\[snap-cache\] [0-9]* hit / [0-9]* points" "$WORK/snap_warm.log" | sed 's/^/  warm: /'
log "build";  "$PY" -u spsp/cli/build_full.py "$DATE" "$B" $EXTRA --phase build > "$WORK/build.log" 2>&1 || { log "build 失敗 (rc=$?) → $WORK/build.log"; exit 11; }
log "aux"
# rc=2 = 任意ステップ (build_upcoming_json など、本番では前夜分で凌ぐもの) だけの失敗。凍結入力には
# smash_db_tournament の upcoming.json が無いので必ず出る。upcoming.json は入力側の写しをそのまま使うので比較は成立する
"$PY" -u spsp/cli/build_aux.py --build-dir "$B" >> "$WORK/aux.log" 2>&1; rc=$?
if [[ $rc -eq 2 ]]; then log "aux: 任意ステップの失敗のみ (続行) → $WORK/aux.log"; elif [[ $rc -ne 0 ]]; then log "aux 失敗 (rc=$rc) → $WORK/aux.log"; exit 12; fi
rsync -aL --delete --exclude .netlify --exclude .DS_Store --exclude demo --exclude '*.pkl' "$B/" "$WORK/deployed/"

# 3. 比較
log "比較 (内容一致: tests/golden/compare_trees.py)"
"$PY" "$REPO/tests/golden/compare_trees.py" "$EXPECT" "$WORK/deployed" --label 'OK: 出力は期待と一致'
rc=$?; log "DONE rc=$rc"; exit $rc

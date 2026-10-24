#!/usr/bin/env bash
# deploy/ci/build_na.sh [eval-date] [build-dir] — 北米版のビルド (と R2 への同期)。
#
# 前提: smash_db_tournament が data-North_America の checkout (北米のデータは担当者が取得・派生ファイル込みで push する
# ので、ここでは取得しない)。site/latest_tjpr_full.jsonl と site/players/ が前回の北米ビルド分 (海外タグの入力)。
#   SPSP_LEARN_CUTS="2026-07-15 2026-07-31 ..."  学習の状態を保存する日付 (次回の差分学習の再開点)。未指定なら SPSP_LEARN_CHUNK
#                                              (既定 1500) 大会ごとに自動で決める。学習は 1 プロセスで、区切りの日ごとに保存して
#                                              続ける (2026-10-18 まで区切りごとに別プロセスで、大会の読み込み 約 75 秒 × 区切りの数
#                                              がかかっていた)。
#   SPSP_DEPLOY_R2=0                           R2 に同期しない (既定 1)
#   SPSP_NA_REUSE_BUILD=1                      ビルドせず、手元に残っている最新の北米ビルド出力 (meta.json があるもの) を同期だけする
#                                              (deploy=diff で送らなかった回を出し直す。2026-10-17)
set -euo pipefail
cd "$(dirname "$0")/../.."
# shellcheck source=deploy/paths.sh
source deploy/paths.sh
export SPSP_REGION=North_America
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
PY=$(spsp_python "$(pwd)")
EVAL_DATE=${1:-$(date +%F)}
OUT=${2:-$SPSP_BUILD_ROOT/na_$(date +%Y%m%d)}
if [ "${SPSP_NA_REUSE_BUILD:-0}" = 1 ]; then
  OUT=$(ls -d "$SPSP_BUILD_ROOT"/na_* 2>/dev/null | while read -r d; do [ -s "$d/meta.json" ] && [ -d "$d/players" ] && echo "$d"; done | sort | tail -1)
  [ -n "$OUT" ] || { echo "ERROR: 同期できる北米ビルド出力が $SPSP_BUILD_ROOT に無い" >&2; exit 1; }
  echo "北米: ビルドせず $OUT を同期だけする ($(grep -o '"eval_date": *"[^"]*"' "$OUT/meta.json"))"
  [ "${SPSP_DEPLOY_R2:-1}" = 1 ] || { echo "SPSP_DEPLOY_R2=0 なので何もしない"; exit 0; }
  RCLONE="${RCLONE:-$HOME/bin/rclone}"; [ -x "$RCLONE" ] || RCLONE=$(command -v rclone); export RCLONE
  exec bash deploy/sync_r2.sh "$OUT" na
fi
mkdir -p "$OUT"
echo "北米ビルド: eval-date $EVAL_DATE → $OUT (データ $(git -C smash_db_tournament log -1 --format='%h %ad' --date=short))"

echo "[$(date +%T)] 海外タグ (前回の北米ランキングから)"
"$PY" -u spsp/cli/build_overseas_json.py || echo "  WARN: 海外タグの再生成に失敗 (前回の overseas.json を使う)"
[ -s site/data/overseas.json ] || { echo "ERROR: site/data/overseas.json が無い" >&2; exit 1; }

# 各段の所要時間と最大メモリを出す (GNU time があれば)
TIME=(); [ -x /usr/bin/time ] && TIME=(/usr/bin/time -f '    (%e s, 最大メモリ %M KB)')
run() { "${TIME[@]}" "$PY" -u spsp/cli/build_full.py "$EVAL_DATE" "$OUT" "$@"; }
if [ -z "${SPSP_LEARN_CUTS:-}" ]; then
  # 大会の日付を数えて、SPSP_LEARN_CHUNK 大会ごとの区切り日を作る (最後の区切りは eval-date より前)
  SPSP_LEARN_CUTS=$("$PY" - "$EVAL_DATE" "${SPSP_LEARN_CHUNK:-1500}" <<'PYEOF'
import collections, os, sys
eval_date, chunk = sys.argv[1], int(sys.argv[2])
root = 'smash_db_tournament/data/startgg/North_America/events'
days = collections.Counter()
for y in sorted(os.listdir(root)):
    for m in sorted(os.listdir(f'{root}/{y}')):
        for d in sorted(os.listdir(f'{root}/{y}/{m}')):
            days[f'{y}-{m}-{d}'] += len(os.listdir(f'{root}/{y}/{m}/{d}'))
cuts, acc = [], 0
for day in sorted(days):
    acc += days[day]
    if acc >= chunk and day < eval_date:
        cuts.append(day); acc = 0
print(' '.join(cuts[:-1] if cuts and cuts[-1] >= eval_date else cuts))
PYEOF
)
  echo "学習の区切り ($(echo $SPSP_LEARN_CUTS | wc -w) 箇所): $SPSP_LEARN_CUTS"
fi
# 学習のチェックポイント (区切りごとの状態)。$CK_DIR に残して次回はそこから差分で学習する (GitHub Actions では
# actions/cache が出し入れする)。区切りより前の大会が変わっていれば (北米担当者が過去の月を足したときなど)
# build_full.py が終了コード 3 を返すので、さらに古いチェックポイント、最後は最初から学習し直す。
CK_DIR=${SPSP_NA_LEARN_DIR:-$SPSP_STATE_ROOT/learn}
mkdir -p "$CK_DIR"
# チェックポイントは新しい 2 つだけ残す (2026-10-16: 選手 19 万人で 1 つ数 GB。学習の最後だけで消していたら、
# 区切りごとに溜まって Mac の VM のディスクが埋まった)
prune_ck() { ls -1 "$CK_DIR"/na_state_????-??-??.pkl 2>/dev/null | sort -r | tail -n +3 | xargs -r rm -f; }
learn_from() {   # learn_from <再開元 state (空 = 最初から)> : eval-date まで 1 プロセスで学習し、その日付より後の区切りごとに保存する
  local sin=$1 d="" cuts=()
  [ -n "$sin" ] && { d=$(basename "$sin" .pkl); d=${d#na_state_}; }
  for cut in ${SPSP_LEARN_CUTS:-}; do
    if [ -z "$d" ] || [[ "$cut" > "$d" ]]; then cuts+=("$cut"); fi
  done
  echo "[$(date +%T)] learn until $EVAL_DATE${sin:+ (from $(basename "$sin"))}、区切り ${#cuts[@]} 箇所で保存"
  # 保存したチェックポイントは build_full.py が新しい 2 つだけ残す (--save-at-keep。区切りごとに数 GB)
  args=(--phase learn --state-out "$OUT/_learn_state.pkl" --save-at "${cuts[*]}" --save-at-template "$CK_DIR/na_state_{cut}.pkl")
  [ -n "$sin" ] && args+=(--state-in "$sin")
  run "${args[@]}"
}
prune_ck
# 前回までのビルド出力 (1 回数 GB) も今回の分以外は消す (セルフホストはディスクが残る)
for d in "$SPSP_BUILD_ROOT"/na_*; do [ -d "$d" ] && [ "$d" != "$OUT" ] && rm -rf "$d"; done
echo "ディスク: $(df -h "$CK_DIR" | awk 'NR==2 {print $4 " 空き / " $2}')、チェックポイント $(du -sh "$CK_DIR" | cut -f1)"
done_learn=0
for f in $(ls -1 "$CK_DIR"/na_state_????-??-??.pkl 2>/dev/null | sort -r); do
  echo "再開を試す: $(basename "$f")"
  rc=0; learn_from "$f" || rc=$?
  if [ $rc -eq 0 ]; then done_learn=1; break
  elif [ $rc -eq 3 ]; then echo "  → $(basename "$f") 以前のデータが変わっている。さらに古いものを試す"; rm -f "$f"
  else exit $rc; fi
done
[ $done_learn = 1 ] || learn_from ""
prune_ck
# snap の再開元もこの実行の learn が作ったもの (台帳の照合は上の最初の再開で済んでいる)
# snap キャッシュは 7 日使われなかった点を消す (当たると更新時刻を付け直す、spsp/learner._SnapCache.get)
mkdir -p "$SPSP_SNAP_CACHE"; find "$SPSP_SNAP_CACHE" -name '*.pkl' -mtime +7 -delete 2>/dev/null
echo "[$(date +%T)] snap"; run --phase snap --state-in "$OUT/_learn_state.pkl" --trust-ledger --snap-cache "$SPSP_SNAP_CACHE"
# 大会ファイルは一度書いたら大会そのもののデータが変わらない限り書き直さない (spsp/tour_cache.py、日本の Actions と同じ)。
# 前回分は $SPSP_STATE_ROOT/tourcache_na に残す (前回のビルド出力は上で消すので)。無ければ全部作る
export SPSP_TOUR_CACHE=1 SPSP_TOUR_CACHE_PREV="$SPSP_STATE_ROOT/tourcache_na"
# 選手の書き出しを 3 プロセスで (spsp/output.py、出力は 1 プロセスと同じ)。2026-10-23 の計測: 599 → 265 秒、Pss の合計 9.9 → 18.1 GB。
# Mac (31 GB) 用。16 GB のランナーで回すときは 1 にする (2 でも 15.5 GB)
export SPSP_PLAYER_WORKERS=${SPSP_PLAYER_WORKERS:-3}
echo "[$(date +%T)] build"; run --phase build
echo "[$(date +%T)] aux"
AUX_RC=0; "$PY" -u spsp/cli/build_aux.py --build-dir "$OUT" || AUX_RC=$?
[ $AUX_RC -eq 1 ] && { echo "ERROR: aux (必須) 失敗" >&2; exit 1; }
[ $AUX_RC -ne 0 ] && echo "  WARN: aux の一部が失敗"
echo "北米ビルド完了: $(wc -l < "$OUT/latest_tjpr_full.jsonl") 人"
# 今回の大会ファイルと一覧を次の回の前回分にする (ハードリンクで写す。一覧が無い = 途中で落ちた回は残さない)
if [ -f "$OUT/_tourcache.json.gz" ] && [ -d "$OUT/tournaments" ]; then
  rm -rf "$SPSP_TOUR_CACHE_PREV.new" && mkdir -p "$SPSP_TOUR_CACHE_PREV.new"
  cp -al "$OUT/tournaments" "$SPSP_TOUR_CACHE_PREV.new/tournaments"
  cp -p "$OUT/_tourcache.json.gz" "$SPSP_TOUR_CACHE_PREV.new/"
  rm -rf "$SPSP_TOUR_CACHE_PREV" && mv "$SPSP_TOUR_CACHE_PREV.new" "$SPSP_TOUR_CACHE_PREV"
fi

if [ "${SPSP_DEPLOY_R2:-1}" = 1 ]; then
  RCLONE="${RCLONE:-$HOME/bin/rclone}"; [ -x "$RCLONE" ] || RCLONE=$(command -v rclone); export RCLONE
  bash deploy/sync_r2.sh "$OUT" na
else
  echo "R2 への同期 ... SKIPPED (SPSP_DEPLOY_R2=0)"
fi

#!/usr/bin/env bash
# deploy/sync_r2.sh — ビルド出力 (配信ディレクトリ) の JSON を Cloudflare R2 に差分同期する (2026-09-22、Cloudflare 移行の ②)。
#
#   bash deploy/sync_r2.sh <配信ディレクトリ> [<地域コード: jp|na|b/<name>>]   (既定: jp)
#
# 前提: ~/bin/rclone と rclone の remote "r2" (設定は ~/.config/rclone/rclone.conf。作り方は下)。
#   rclone config create r2 s3 provider=Cloudflare access_key_id=… secret_access_key=… endpoint=https://<account_id>.r2.cloudflarestorage.com acl=private
#   (キーは R2 の API トークン (Object Read & Write、バケット spsp-data に限定)。このファイル以外に書かない)
# バケット: spsp-data。キーは <地域>/players/… <地域>/history/… <地域>/data/… <地域>/meta.json … (フロントの dataRoot = https://data.spsp.games/<地域>/)。
# 同期するのは JSON / JSONL だけ (HTML / JS / CSS は Worker の static assets = spsp-web の deploy)。
# rclone sync は中身 (MD5) が変わったファイルだけ送る (--checksum。history/ の日次全件も内容が同じなら送らない)。
# 2026-10-21 から、前回送ったものの一覧 (<地域>/_sync_manifest.json.gz、deploy/r2_manifest.py) と手元を比べて変わったものだけ送る
# (R2 の一覧を取らない)。7 日に 1 回は従来の全件 sync で R2 と突き合わせる (下の MODE)。試験 = tests/deploy/test_sync_r2_manifest.sh
# Cache-Control はプレフィックスごと: players/ tournaments/ history/ は 1 日、それ以外 (players_current.json / meta.json / data/ / *.jsonl) は 5 分。
# 揮発側に stale-while-revalidate は付けない: 付けるとブラウザが古い一覧を最大 1 時間使い回す (09-22 に絵文字の更新が見えなかった)。5 分ごとの再検証は edge が 304 で返すので安い。
set -eu
SRC=${1:?配信ディレクトリ}; REGION=${2:-jp}
RCLONE=${RCLONE:-$HOME/bin/rclone}
BUCKET=${R2_BUCKET:-spsp-data}
# R2_REMOTE: rclone の remote の名前 (既定 r2)。試験では手元のディレクトリを remote にして使う (tests/deploy/test_sync_r2_manifest.sh)
R2_REMOTE=${R2_REMOTE:-r2}
DEST="$R2_REMOTE:$BUCKET/$REGION"
# rclone は Go 製。ConoHa WING のスレッド上限 (esbuild / tsc と同じ話) に当たらないよう OS スレッドを 1 本に。ネットワーク待ちが大半なので遅くならない
export GOMAXPROCS=${GOMAXPROCS:-1}
# 同じ地域の同期は 1 本ずつ (初回の全件アップロードは数十分かかる。その間に次の nightly が来たら終わるまで待つ。1 時間で諦める)
LOCK_DIR=${SPSP_STATE_ROOT:-$HOME/spsp-state}
mkdir -p "$LOCK_DIR"
exec 8>"$LOCK_DIR/r2_sync_${REGION//\//_}.lock"
flock -w 3600 8 || { echo "前回の同期 ($REGION) が 1 時間たっても終わらない — 今回は見送り" >&2; exit 3; }
echo "[$(date '+%F %T')] sync start $SRC -> $DEST/"
[ -d "$SRC/players" ] || { echo "players/ が無い: $SRC" >&2; exit 2; }
"$RCLONE" listremotes | grep -q "^$R2_REMOTE:" || { echo "rclone の remote $R2_REMOTE が無い (rclone config create r2 s3 …)" >&2; exit 2; }
PY=$( [ -x "$(dirname "$0")/../.venv/bin/python" ] && echo "$(dirname "$0")/../.venv/bin/python" || command -v python3 )
# --checksum: 中身 (MD5) で比較する。ビルドは毎回ファイルを書き直して更新時刻が変わるので、既定 (サイズ + 更新時刻) だと
# 中身が同じ 49,000 ファイルを 3 時間ごとに全部送ってしまう (R2 の PUT は月 100 万回まで無料、超えると課金)。
# --fast-list: 一覧を 1000 件ずつまとめて取る (LIST の回数を減らす)
# --max-delete: 1 回の sync で消すのは 1 プレフィックスあたり ${SPSP_R2_MAX_DELETE:-1000} 件まで。ビルドは players/ などを消してから書き直すので、
# 書き直しの途中の BUILD_DIR を同期すると本番の R2 から大量に消えうる (2026-09-28 のレビュー)。超えたら rclone はそれ以上消さずにエラーで終わる
# (退会の整理などで本当に大量に消すときは SPSP_R2_MAX_DELETE=-1 で手で流す)
# 並列数: ConoHa は GOMAXPROCS=1 なので 8。GitHub Actions は 1 本あたりが遅い (90 KB/s 前後) ので SPSP_R2_TRANSFERS=32
TRANSFERS=${SPSP_R2_TRANSFERS:-8}
COMMON=(--checksum --fast-list --transfers "$TRANSFERS" --checkers "$TRANSFERS" --s3-no-check-bucket --stats-one-line --stats 30s --stats-log-level NOTICE --max-delete "${SPSP_R2_MAX_DELETE:-1000}")

# SPSP_R2_DIFF_ONLY=1: 送らずに「いま配信中のものと比べて何が変わるか」だけ数えて終わる (読み取りだけ。GitHub Actions の deploy=diff)。
# 同じ = 送らない / 変更・追加 = 送る / 削除 = 配信側にだけあるので sync で消える。data/ と一覧は変わったファイル名も出す。
if [ "${SPSP_R2_DIFF_ONLY:-0}" = 1 ]; then
  TMPD=$(mktemp -d)
  diff_part() {   # diff_part <表示名> <src> <dst> [rclone のフィルタ…]
    local label=$1 src=$2 dst=$3; shift 3
    [ -e "$src" ] || return 0
    "$RCLONE" check "$src" "$dst" --checksum --fast-list --s3-no-check-bucket --combined "$TMPD/c" "$@" >/dev/null 2>&1 || true
    printf '  %-26s 同じ %6d / 変更 %6d / 追加 %6d / 削除 %6d\n' "$label" \
      "$(grep -c '^=' "$TMPD/c" || true)" "$(grep -c '^\*' "$TMPD/c" || true)" "$(grep -c '^+' "$TMPD/c" || true)" "$(grep -c '^-' "$TMPD/c" || true)"
    case "$label" in players/*|tournaments/*|history/*) ;; *) grep -E '^[*+-]' "$TMPD/c" | head -12 | sed 's/^/      /' ;; esac
    # 変更の例 (先頭 2 件) について、値が違う場所を出す (何が変わるのかを見るため)
    grep '^\* ' "$TMPD/c" | head -2 | cut -c3- | while read -r f; do
      "$RCLONE" cat "$dst/$f" > "$TMPD/old" 2>/dev/null || continue
      echo "      例 $f:"
      "${PY:-python3}" "$(dirname "$0")/r2_json_diff.py" "$TMPD/old" "$src/$f" 2>&1 | sed 's/^/        /'
    done
  }
  echo "差分 (送らない): $SRC と $DEST/"
  for d in players tournaments history; do
    if [ "$d" != tournaments ] && [ -f "$SRC/_unbundled_players.txt" ]; then
      diff_part "$d/" "$SRC/$d" "$DEST/$d" --files-from "$SRC/_unbundled_players.txt"
    else
      diff_part "$d/" "$SRC/$d" "$DEST/$d" --include '*.json'
    fi
  done
  diff_part "data/" "$SRC/data" "$DEST/data" --include '*.json'
  diff_part "一覧・meta (直下)" "$SRC" "$DEST" --max-depth 1 --include 'latest_*.jsonl' --include players_current.json --include meta.json
  rm -rf "$TMPD"
  echo "[$(date '+%F %T')] diff only (何も送っていない)"
  exit 0
fi
# 前回送ったものの一覧 (マニフェスト、deploy/r2_manifest.py) があれば、手元の MD5 と比べて変わったファイルだけ送る (R2 の一覧を取らない。
# 何も変わらない回の日本で 90 秒 → 数秒)。マニフェストが無い・SPSP_R2_FULL_SYNC=1・前回の全件 sync から SPSP_R2_FULL_SYNC_DAYS
# (既定 7) 日たった回は、従来どおり R2 と全件を比べる sync をしてマニフェストを作り直す (ほかの経路で R2 を書き換えたずれを直す)。
# SPSP_R2_MANIFEST=0 で使わない (従来の全件 sync だけ。マニフェストも書かない)。既定は日本 (jp)・北米 (na) で 1、ほかの地域 (プレビュー b/<name> など) は 0 (北米は 2026-10-24 から。全件 sync が約 2 時間かかっていた)
# ずれない約束: R2 の <地域>/ に書くのはこのスクリプトだけ。送り始める前に R2 のマニフェストを消し、全部送り終えてから新しいものを置く。
# 途中で落ちた回 (一部だけ送った) の次はマニフェストが無いので全件 sync になる (古いマニフェストのまま差分を取ると、
# 送ったファイルが次の回に元の中身へ戻ったとき送り直さずに R2 だけ新しいまま残る)。SPSP_R2_MANIFEST=0 の回も消す (その回の変更はマニフェストに載らない)
MANIFEST_KEY=_sync_manifest.json.gz
case $REGION in jp|na) USE_MANIFEST=${SPSP_R2_MANIFEST:-1} ;; *) USE_MANIFEST=${SPSP_R2_MANIFEST:-0} ;; esac
CC_STABLE='Cache-Control: public, max-age=86400, stale-while-revalidate=604800'
CC_VOLATILE='Cache-Control: public, max-age=300'
PLAN=$(mktemp -d)
trap 'rm -rf "$PLAN"' EXIT
MODE=full
if [ "$USE_MANIFEST" = 1 ] && [ "${SPSP_R2_FULL_SYNC:-0}" != 1 ]; then
  "$RCLONE" copyto "$DEST/$MANIFEST_KEY" "$PLAN/old.json.gz" --s3-no-check-bucket 2>/dev/null || true
  if [ ! -f "$PLAN/old.json.gz" ]; then
    echo "  マニフェストが無い → 全件 sync"
  elif "$PY" "$(dirname "$0")/r2_manifest.py" due "$PLAN/old.json.gz" "${SPSP_R2_FULL_SYNC_DAYS:-7}"; then
    echo "  前回の全件 sync から ${SPSP_R2_FULL_SYNC_DAYS:-7} 日以上 → 全件 sync"
  else
    rc=0; "$PY" "$(dirname "$0")/r2_manifest.py" plan "$SRC" "$PLAN/old.json.gz" "$PLAN/p" > "$PLAN/plan.out" 2>&1 || rc=$?
    sed 's/^/  /' "$PLAN/plan.out"
    case $rc in
      0) MODE=manifest ;;
      3) echo "  マニフェストが読めない → 全件 sync" ;;
      *) echo "  計画できない (rc=$rc) — 今回は送らない" >&2; exit "$rc" ;;
    esac
  fi
fi
# 送り始める前に R2 のマニフェストを消す (上の「ずれない約束」)。消せない (無いのか確かめられない) ときは何も送らずに止める
if ! "$RCLONE" deletefile "$DEST/$MANIFEST_KEY" --s3-no-check-bucket 2>/dev/null; then
  # rclone の終了コード 3 = 送り先のディレクトリが無い (初回。マニフェストも無い)
  ls_rc=0; ls_out=$("$RCLONE" lsf --files-only --max-depth 1 "$DEST/" --s3-no-check-bucket 2>/dev/null) || ls_rc=$?
  if [ "$ls_rc" != 0 ] && [ "$ls_rc" != 3 ]; then
    echo "R2 のマニフェストを消せず、有無も確かめられない (rclone lsf rc=$ls_rc) — 今回は送らない" >&2; exit 5
  fi
  if printf '%s\n' "$ls_out" | grep -qx "$MANIFEST_KEY"; then
    echo "R2 のマニフェストを消せない — 今回は送らない" >&2; exit 5
  fi
fi
# 組ごとの根と Cache-Control (送る順。まとめ (data/views/bundle/) は一覧 (players_index・current/) より先に送る: 一覧が先に届くと、
# まとめた選手のページがまだ無いまとめを読みに行く)
group_root() { case $1 in bundle) echo data/views/bundle ;; *) echo "$1" ;; esac; }
group_cc() { case $1 in players|tournaments|history) echo "$CC_STABLE" ;; *) echo "$CC_VOLATILE" ;; esac; }
if [ "$MODE" = manifest ]; then
  for g in bundle players tournaments history data; do
    root=$(group_root "$g")
    if [ -s "$PLAN/p/$g.upload" ]; then
      # --no-check-dest: 送り先を見ずに送る (変わったものだけの一覧なので)
      "$RCLONE" copy "$SRC/$root" "$DEST/$root" --files-from "$PLAN/p/$g.upload" --no-check-dest \
        --transfers "$TRANSFERS" --s3-no-check-bucket --stats-one-line --stats 30s --stats-log-level NOTICE \
        --header-upload "$(group_cc "$g")"
    fi
    if [ -s "$PLAN/p/$g.delete" ]; then
      "$RCLONE" delete "$DEST/$root" --files-from "$PLAN/p/$g.delete" --no-traverse --s3-no-check-bucket
    fi
  done
else
  # 安定部分 (内容が変わらないファイルが大半): 1 日キャッシュ
  # 参加の少ない選手を data/views/bundle/ にまとめた回 (SPSP_BUNDLE_SMALL=1、build_split_views) は、players/ と history/ は
  # まとめなかった選手 ($SRC/_unbundled_players.txt) だけ送る。--files-from に無いファイルは同期の対象外なので R2 からも
  # 消さない (前に個別に置いた分は残る。古いページ用。片付けは別途)
  UNBUNDLED="$SRC/_unbundled_players.txt"
  # (data/ の sync はまとめの後で同じ中身を見て送り直さない)
  if [ -d "$SRC/data/views/bundle" ]; then
    "$RCLONE" sync "$SRC/data/views/bundle" "$DEST/data/views/bundle" "${COMMON[@]}" --include '*.json' \
      --header-upload "$CC_VOLATILE"
  fi
  for d in players tournaments history; do
    [ -d "$SRC/$d" ] || continue
    filt=(--include '*.json')
    if [ "$d" != tournaments ] && [ -f "$UNBUNDLED" ]; then filt=(--files-from "$UNBUNDLED"); fi
    "$RCLONE" sync "$SRC/$d" "$DEST/$d" "${COMMON[@]}" "${filt[@]}" \
      --header-upload "$CC_STABLE"
  done
  # 揮発部分: 5 分キャッシュ。data/ (索引・タグ・geo など) と一覧ファイル
  "$RCLONE" sync "$SRC/data" "$DEST/data" "${COMMON[@]}" --include '*.json' \
    --header-upload "$CC_VOLATILE"
  if [ "$USE_MANIFEST" = 1 ]; then
    "$PY" "$(dirname "$0")/r2_manifest.py" full "$SRC" - "$PLAN/p" >/dev/null
  fi
fi
for f in players_current.json meta.json; do   # お知らせ (news.json) は 2026-09-29 から spsp-web がサイトと一緒に配信する
  [ -f "$SRC/$f" ] && "$RCLONE" copyto "$SRC/$f" "$DEST/$f" --s3-no-check-bucket \
    --header-upload 'Cache-Control: public, max-age=300'
done
# 一覧 (.jsonl、13 MB) は text/plain で置く: Cloudflare の edge 圧縮 (Brotli) は content-type で決まり、
# application/x-ndjson は圧縮されず 13 MB のまま届く (09-22 に確認)。text/plain は圧縮対象。フロントは res.text() で読むので型は見ていない
for f in "$SRC"/latest_*.jsonl; do
  [ -f "$f" ] && "$RCLONE" copyto "$f" "$DEST/$(basename "$f")" --s3-no-check-bucket \
    --header-upload 'Cache-Control: public, max-age=300' \
    --header-upload 'Content-Type: text/plain; charset=utf-8'
done
# マニフェストは全部送り終えてから置く (途中で落ちたら R2 にマニフェストが無いまま = 次の回は全件 sync)
if [ -f "$PLAN/p/manifest.json.gz" ]; then
  "$RCLONE" copyto "$PLAN/p/manifest.json.gz" "$DEST/$MANIFEST_KEY" --s3-no-check-bucket \
    --header-upload 'Cache-Control: no-store'
fi
echo "[$(date '+%F %T')] synced $SRC -> $DEST/ ($MODE)"

#!/usr/bin/env bash
# tests/deploy/test_sync_r2_manifest.sh — deploy/sync_r2.sh のマニフェスト同期 (deploy/r2_manifest.py) を、R2 の代わりに手元の
# ディレクトリ (rclone の local backend) に向けて確かめる。R2 には触らない。
#
#   bash tests/deploy/test_sync_r2_manifest.sh     (rclone は $RCLONE か ~/bin/rclone)
#
# 確かめること: 初回 (マニフェスト無し) は全件 sync してマニフェストを置く / 2 回目からは変わったファイルだけ送り・消したファイルを消す /
# まとめた回 (_unbundled_players.txt) は一覧に無い選手を R2 から消さない / 消す数が上限を超えたら何も送らずに止まる /
# SPSP_R2_FULL_SYNC=1 は R2 と全件比べて、マニフェストに無いずれ (手で置いたファイル) も消す / 途中で落ちた回の次は全件 sync /
# 日本以外は既定でマニフェストを使わない / 送った後の R2 は手元と同じ
set -u
REPO=$(cd "$(dirname "$0")/../.." && pwd)
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
export RCLONE=${RCLONE:-$HOME/bin/rclone}
export RCLONE_CONFIG_TESTR2_TYPE=local R2_REMOTE=testr2 R2_BUCKET="$T/bucket" SPSP_STATE_ROOT="$T/state"
export GOMAXPROCS=1
DEST="$T/bucket/jp"
SRC="$T/src"
FAIL=0
ok() { echo "  ok: $*"; }
ng() { echo "  NG: $*"; FAIL=1; }
put() { mkdir -p "$(dirname "$SRC/$1")"; printf '%s' "$2" > "$SRC/$1"; }
run() { bash "$REPO/deploy/sync_r2.sh" "$SRC" jp > "$T/out" 2>&1; local rc=$?; cat "$T/out" | sed 's/^/      /'; return $rc; }
# 送り先が手元と同じか (同期の対象のファイルだけ。まとめた回の一覧外の選手は見ない)
same_tree() {
  local d
  for d in players tournaments history data; do
    [ -d "$SRC/$d" ] || continue
    if ! diff -r -q -x '*.txt' "$SRC/$d" "$DEST/$d" > "$T/diff" 2>&1; then
      if [ "${1:-}" = listed ] && [ "$d" != tournaments ] && [ "$d" != data ]; then
        grep -v "^Only in $DEST/$d" "$T/diff" > "$T/diff2" || true
        [ -s "$T/diff2" ] && { cat "$T/diff2"; return 1; }
        continue
      fi
      cat "$T/diff"; return 1
    fi
  done
  for f in players_current.json meta.json latest_tjpr_full.jsonl; do
    cmp -s "$SRC/$f" "$DEST/$f" || { echo "直下 $f が違う"; return 1; }
  done
}

put players/1.json '{"a":1}'; put players/2.json '{"a":2}'; put history/1.json '[1]'; put history/2.json '[2]'
put tournaments/10.json '{"t":10}'; put tournaments/11.json '{"t":11}'
put data/a.json '{"x":1}'; put data/views/bundle/0.json '{"b":0}'; put data/views/players_index.json '{"i":1}'
put players_current.json '{"c":1}'; put meta.json '{"m":1}'; put latest_tjpr_full.jsonl '{"r":1}'

echo "1. 初回 (マニフェスト無し)"
run || ng "rc=$?"
grep -q '(full)' "$T/out" && ok "全件 sync" || ng "全件 sync でない"
[ -f "$DEST/_sync_manifest.json.gz" ] && ok "マニフェストを置いた" || ng "マニフェストが無い"
same_tree && ok "送り先 = 手元" || ng "送り先が違う"

echo "2. 2 回目 (1 ファイル変更・1 ファイル追加・1 ファイル削除)"
put players/1.json '{"a":10}'; put data/b.json '{"x":2}'; rm "$SRC/tournaments/11.json"
run || ng "rc=$?"
grep -q '(manifest)' "$T/out" && ok "マニフェストで同期" || ng "マニフェストで同期していない"
grep -q 'players .*同じ      1 / 送る      1 / 消す      0' "$T/out" && ok "players は 1 件だけ送る" || ng "players の数"
grep -q 'tournaments .*同じ      1 / 送る      0 / 消す      1' "$T/out" && ok "tournaments は 1 件消す" || ng "tournaments の数"
grep -q 'data .*送る      1 / 消す      0' "$T/out" && ok "data は 1 件送る (bundle は data で数えない)" || ng "data の数"
[ ! -e "$DEST/tournaments/11.json" ] && ok "消したファイルは送り先からも消える" || ng "tournaments/11.json が残っている"
same_tree && ok "送り先 = 手元" || ng "送り先が違う"

echo "3. 何も変わらない回"
run || ng "rc=$?"
grep -q 'players .*送る      0 / 消す      0' "$T/out" && grep -q 'history .*送る      0' "$T/out" && ok "何も送らない" || ng "送っている"

echo "4. まとめた回 (選手 1 は一覧に無い・手元のファイルも無い)"
printf '2.json\n' > "$SRC/_unbundled_players.txt"; rm "$SRC/players/1.json" "$SRC/history/1.json"; put players/2.json '{"a":20}'
run || ng "rc=$?"
[ -e "$DEST/players/1.json" ] && [ -e "$DEST/history/1.json" ] && ok "一覧に無い選手は消さない" || ng "一覧に無い選手を消した"
cmp -s "$SRC/players/2.json" "$DEST/players/2.json" && ok "一覧の選手は送る" || ng "players/2.json が古い"
same_tree listed && ok "送り先 = 手元 (一覧の分)" || ng "送り先が違う"
rm "$SRC/_unbundled_players.txt"

echo "5. 消す数が上限を超える (SPSP_R2_MAX_DELETE=0)"
rm "$SRC/data/b.json"; put players/2.json '{"a":21}'
before=$(md5sum "$DEST/_sync_manifest.json.gz" | cut -c1-32)
SPSP_R2_MAX_DELETE=0 run; rc=$?
[ $rc = 4 ] && ok "rc=4 で止まる" || ng "rc=$rc"
[ -e "$DEST/data/b.json" ] && ! cmp -s "$SRC/players/2.json" "$DEST/players/2.json" && ok "何も送っていない・消していない" || ng "送った / 消した"
[ "$(md5sum "$DEST/_sync_manifest.json.gz" | cut -c1-32)" = "$before" ] && ok "マニフェストは前回のまま" || ng "マニフェストが変わった"

echo "6. SPSP_R2_FULL_SYNC=1 (送り先にだけある手で置いたファイルも消える)"
mkdir -p "$DEST/tournaments"; printf 'x' > "$DEST/tournaments/99.json"
SPSP_R2_FULL_SYNC=1 run || ng "rc=$?"
grep -q '(full)' "$T/out" && ok "全件 sync" || ng "全件 sync でない"
[ ! -e "$DEST/tournaments/99.json" ] && [ ! -e "$DEST/data/b.json" ] && ok "ずれを直した" || ng "ずれが残っている"
same_tree && ok "送り先 = 手元" || ng "送り先が違う"

echo "7. 全件 sync の時期 (SPSP_R2_FULL_SYNC_DAYS=0)"
SPSP_R2_FULL_SYNC_DAYS=0 run || ng "rc=$?"
grep -q '(full)' "$T/out" && ok "期限が来たら全件 sync" || ng "全件 sync でない"
run || ng "rc=$?"
grep -q '(manifest)' "$T/out" && ok "その次はマニフェスト" || ng "マニフェストで同期していない"

echo "8. SPSP_R2_MANIFEST=0 は従来どおりの全件 sync・R2 のマニフェストは消す (その回の変更が載らないので)"
put tournaments/12.json '{"t":12}'
SPSP_R2_MANIFEST=0 run || ng "rc=$?"
grep -q '(full)' "$T/out" && [ ! -e "$DEST/_sync_manifest.json.gz" ] && ok "全件 sync・マニフェストは消えた" || ng "違う"
run || ng "rc=$?"
grep -q '(full)' "$T/out" && [ -e "$DEST/_sync_manifest.json.gz" ] && ok "次の回は全件 sync でマニフェストを作り直す" || ng "全件 sync でない"
same_tree && ok "送り先 = 手元" || ng "送り先が違う"

echo "9. 途中で落ちた回 (players は送れた・tournaments で失敗) の次は全件 sync"
orig=$(cat "$SRC/players/2.json")
put players/2.json '{"a":22}'; put tournaments/10.json '{"t":100}'
chmod a-w "$DEST/tournaments"
run; rc=$?
chmod u+w "$DEST/tournaments"
[ $rc != 0 ] && ok "失敗で終わる (rc=$rc)" || ng "成功してしまった"
[ "$(cat "$DEST/players/2.json")" = '{"a":22}' ] && ok "players は送れている (一部だけ送った状態)" || ng "players が送れていない (試験の前提が崩れた)"
[ ! -e "$DEST/_sync_manifest.json.gz" ] && ok "R2 にマニフェストが無い" || ng "古いマニフェストが残っている"
put players/2.json "$orig"; put tournaments/10.json '{"t":10}'   # 次の回で元の中身に戻る (古いマニフェストなら送らないケース)
run || ng "rc=$?"
grep -q '(full)' "$T/out" && ok "全件 sync" || ng "全件 sync でない"
same_tree && ok "送り先 = 手元 (元に戻った players/2.json も)" || ng "送り先が違う"

echo "10. 北米 (na) も既定でマニフェストを使う・プレビュー (b/<name>) は使わない"
bash "$REPO/deploy/sync_r2.sh" "$SRC" na > "$T/out" 2>&1 || ng "rc=$?"
grep -q '(full)' "$T/out" && [ -e "$T/bucket/na/_sync_manifest.json.gz" ] && ok "na の初回は全件 sync・マニフェストを置く" || ng "na の初回がおかしい"
bash "$REPO/deploy/sync_r2.sh" "$SRC" na > "$T/out" 2>&1 || ng "rc=$?"
grep -q '(manifest)' "$T/out" && ok "na の 2 回目はマニフェスト" || ng "na の 2 回目がマニフェストでない"
bash "$REPO/deploy/sync_r2.sh" "$SRC" b/test > "$T/out" 2>&1 || ng "rc=$?"
grep -q '(full)' "$T/out" && [ ! -e "$T/bucket/b/test/_sync_manifest.json.gz" ] && ok "b/test は全件 sync・マニフェスト無し" || ng "b/test がマニフェストを使った"

[ $FAIL = 0 ] && echo "OK: sync_r2 のマニフェスト同期" || { echo "FAILED"; exit 1; }

#!/usr/bin/env bash
# deploy/ci/trigger.sh <download-jp|build-jp|build-na> — GitHub Actions のワークフローを起動する (workflow_dispatch)。
#
# 2026-09-29 から定時起動は Cloudflare Worker (deploy/ci/dispatch-worker) が行う。これは手動・予備用。
# 以前はサーバ (ConoHa) の cron から呼んでいた。計算はしない (GitHub に「実行」を送るだけ)。GitHub の schedule は 2026-09-28 に
# 追加から 5 時間たっても一度も起動しなかったので、起動はこちらで行う。crontab (サーバの時刻 = JST):
#   3 */3 * * *           deploy/ci/trigger.sh download-jp   日本の大会データ取得 (smash_database、3 時間ごと、無料)
#   20 0,6,12,18,21 * * * deploy/ci/trigger.sh build-jp      日本のビルド → spsp.games (1 日 5 回)
#   40 1,7,10,13,19 * * * deploy/ci/trigger.sh build-na      北米のビルド (ニューヨーク夏時間 0,6,12,18,21 時)。
#                                                           データが前回ビルドと同じなら Actions 側で数十秒で終わる
# 鍵: gh の認証 (~/.config/gh/hosts.yml) を使う。ログ: ~/.local/log/spsp_nightly/actions_trigger.log
set -u
GH=${GH:-$HOME/bin/gh}
LOG=${SPSP_LOG_DIR:-$HOME/.local/log/spsp_nightly}/actions_trigger.log
mkdir -p "$(dirname "$LOG")"
case "${1:-}" in
  download-jp) args=(download-japan -R tosakazu/smash_database --ref main -f push=true) ;;
  build-jp)    args=(build -R tosakazu/spsp-build --ref main -f region=jp -f deploy=r2) ;;
  build-na)    args=(build -R tosakazu/spsp-build --ref main -f region=na -f deploy=r2 -f only_if_changed=true) ;;
  *) echo "usage: $0 download-jp|build-jp|build-na" >&2; exit 2 ;;
esac
for i in 1 2 3; do
  if out=$("$GH" workflow run "${args[@]}" 2>&1); then
    echo "$(date '+%F %T') ok   $1 $(echo "$out" | tail -1)" >> "$LOG"; exit 0
  fi
  sleep 30
done
echo "$(date '+%F %T') FAIL $1 $(echo "$out" | tail -1)" >> "$LOG"; exit 1

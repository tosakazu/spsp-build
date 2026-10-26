#!/usr/bin/env bash
# build.yml の最後: この地域の古い actions/cache を消す (2026-10-27〜)。
# 日本は 15 分ごとに回り、1 回で学習・大会ファイル・snap の約 170 MB を保存する。リポジトリのキャッシュは 10 GB までで、
# 超えると古く使われていないものから消される = 何もしないと北米の分 (1 回 2.5 GB、作り直すと 4 時間以上) が先に追い出される。
#
# 使い方: prune_caches.sh <jp|na> <この回の run_id>   (GH_TOKEN に actions: write の権限)
#   spsp-{learn,tour,snap}-<地域>-<run_id> … 新しい 2 つだけ残す。この回の分が無い種類は触らない (保存に失敗した回が
#                                           前回分を消さないように)
#   spsp-<地域>-built-<sha>                 … 新しい 5 つだけ残す
# KEEP_RUNS / KEEP_BUILT で残す数を変えられる。DRY_RUN=1 なら消さずに表示だけ。
set -euo pipefail
region=${1:?region}; run_id=${2:?run_id}
repo=${GITHUB_REPOSITORY:-tosakazu/spsp-build}
keep_runs=${KEEP_RUNS:-2}; keep_built=${KEEP_BUILT:-5}

# 前方一致の鍵を新しい順に (作られた時刻の降順)
keys() { gh cache list -R "$repo" --key "$1" -L 100 --sort created_at --order desc --json key -q '.[].key'; }
del() {
  if [ "${DRY_RUN:-0}" = 1 ]; then echo "  (dry) 消す $1"; return; fi
  gh cache delete "$1" -R "$repo" >/dev/null && echo "  消した $1" || echo "  消せなかった $1"
}

for kind in learn tour snap; do
  pre="spsp-$kind-$region-"
  list=$(keys "$pre")
  # 前方一致なので spsp-learn-jp- は spsp-learn-jp-… だけに当たる (地域をまたがない)
  if ! grep -qx "$pre$run_id" <<<"$list"; then
    echo "$pre: この回の分が無いので触らない"; continue
  fi
  echo "$pre: $(grep -c . <<<"$list") 件、新しい $keep_runs 件を残す"
  tail -n +"$((keep_runs + 1))" <<<"$list" | while read -r k; do [ -n "$k" ] && del "$k"; done
done

pre="spsp-$region-built-"
list=$(keys "$pre")
echo "$pre: $(grep -c . <<<"$list" || true) 件、新しい $keep_built 件を残す"
tail -n +"$((keep_built + 1))" <<<"$list" | while read -r k; do [ -n "$k" ] && del "$k"; done

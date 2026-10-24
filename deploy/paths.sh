#!/usr/bin/env bash
# deploy/paths.sh — 夜間パイプラインが使う「リポジトリの外」のパスと環境値の定義 (唯一の置き場所)。
# nightly_cron.sh / update_and_deploy.sh / deploy_v4.sh / tests/golden/*.sh が source する。
#
# 状態を持つものは全部 $SPSP_STATE_ROOT (既定 $HOME/spsp-state) 配下に置く。/tmp は OS の掃除で消える
# (2026-07 に gh-pages clone の object が消えてデプロイが 3 日止まった)。
#
# 環境変数で上書きできるもの (本番は全部既定値。別ホスト・別アカウントで試すときに使う):
#   SPSP_STATE_ROOT      状態の置き場 (チェックポイント・snap キャッシュ・BUILD_DIR・gh-pages clone・ロック)
#   SPSP_SECRETS_DIR     秘密ファイルの置き場 (STARTGG_TOKEN, startgg_oauth_client_secret)。既定 $HOME/spsp-secrets
#   SPSP_GHPAGES_REMOTE  gh-pages の push 先。SPSP_GHPAGES_AUTHOR_NAME / _EMAIL は commit の author
#   SPSP_SITE_URL        デプロイ後の反映確認に見る URL (gh-pages 側)
#   STARTGG_TOKEN        start.gg API トークン (無ければ $SPSP_SECRETS_DIR/STARTGG_TOKEN 等のファイルから読む)
#   STARTGG_CLIENT_SECRET キャラ投票の取り込み鍵の導出元 (無ければ $SPSP_SECRETS_DIR/startgg_oauth_client_secret)
# ランキングの設定 (SPSP_BT_* 等) はここでは扱わない: 本番は v4/settings.py の Settings() 既定値で、環境変数は読まない。
export TZ=Asia/Tokyo   # 日付・週末判定は JST 前提 (build_v4_full.py も固定する)
SPSP_STATE_ROOT="${SPSP_STATE_ROOT:-$HOME/spsp-state}"
SPSP_GHPAGES_CLONE="$SPSP_STATE_ROOT/tosakazu.github.io"   # gh-pages ブランチの作業 clone (BUILD_DIR → ここ/spsp に rsync → push)
SPSP_BUILD_ROOT="$SPSP_STATE_ROOT/builds"                   # BUILD_DIR の置き場 (nightly_YYYYMMDD、7 日で掃除)
SPSP_LOCK_FILE="$SPSP_STATE_ROOT/nightly.lock"              # 多重起動ガード
SPSP_GHPAGES_RETIRED_FLAG="$SPSP_STATE_ROOT/ghpages_retired"   # これがあれば旧サイト (gh-pages) を閉じた扱い: nightly は gh-pages に出さず、日曜の clone 取り直しもしない (spsp.games への転送ページに切り替えた後。手順 = spsp-web tools/ghpages_redirect/RUNBOOK.md)
SPSP_LEARN_DIR="$SPSP_STATE_ROOT/learn"                     # 学習チェックポイント state_YYYY-MM-DD.pkl
SPSP_SNAP_CACHE="$SPSP_STATE_ROOT/snap_cache"               # スナップショット復元のキャッシュ (使われずに日本 2 日・北米 7 日で掃除)
SPSP_TMP_DIR="$SPSP_STATE_ROOT/tmp"                         # 作業中の一時ファイル (取得のステージング等)
SPSP_LOG_DIR="${SPSP_LOG_DIR:-$HOME/.local/log/spsp_nightly}"   # ~/logs は ConoHa WING 管理で書けないのでこちら
SPSP_SECRETS_DIR="${SPSP_SECRETS_DIR:-$HOME/spsp-secrets}"
SPSP_GHPAGES_REMOTE="${SPSP_GHPAGES_REMOTE:-git@github.com:tosakazu/tosakazu.github.io.git}"
SPSP_SITE_URL="${SPSP_SITE_URL:-https://tosakazu.github.io/spsp}"
# gh-pages に commit する author (clone を作り直したときに設定する。既存 clone のローカル設定と同じ値)
SPSP_GHPAGES_AUTHOR_NAME="${SPSP_GHPAGES_AUTHOR_NAME:-tosakazu}"
SPSP_GHPAGES_AUTHOR_EMAIL="${SPSP_GHPAGES_AUTHOR_EMAIL:-tosakazu@users.noreply.github.com}"
mkdir -p "$SPSP_STATE_ROOT" "$SPSP_BUILD_ROOT" "$SPSP_LOG_DIR" "$SPSP_LEARN_DIR" "$SPSP_TMP_DIR"

# venv の python3。update_and_deploy.sh と deploy_v4.sh で同じ順序で探す (別々に書くと食い違う)。
# システム python3 へのフォールバックは置かない (このホストには無く、空文字のまま黙って進むと事故になる)。
spsp_python() {
  local root="$1"
  if [ -x "$root/.venv/bin/python3" ]; then echo "$root/.venv/bin/python3"                      # 本番 (repo 直下、requirements.lock.txt から作る)
  elif [ -x "$root/ranking_eval/.venv/bin/python3" ]; then echo "$root/ranking_eval/.venv/bin/python3"   # 旧置き場 (2026-09-08 まで)
  else echo "ERROR: venv python3 not found (= $root/.venv)" >&2; return 1
  fi
}

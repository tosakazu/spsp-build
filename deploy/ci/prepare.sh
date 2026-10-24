#!/usr/bin/env bash
# deploy/ci/prepare.sh <jp|na> — GitHub Actions で、ConoHa の作業ディスクにあるものを揃える。
#
# ConoHa では前回ビルドの出力・秘密ファイル・rclone の設定がディスクに残っているが、Actions の実行環境は毎回まっさら。
# ここで次を用意する (パスは deploy/paths.sh の既定 = $HOME 配下に合わせるので、パイプライン側は何も変えずに動く):
#   - $HOME/spsp-secrets/startgg_oauth_client_secret(_cf)   キャラ投票の取り込み鍵の元 (Secrets にあるときだけ)
#   - $HOME/.config/rclone/rclone.conf の remote r2          R2 の鍵が Secrets にあるときだけ
#   - site/latest_tjpr_full.jsonl, site/data/overseas.json, site/data/upcoming_entrants.json
#       = 前回デプロイ分。海外タグ (= 前回ランキング掲載者 ∩ smash_database の overseas_status.json) と参加予定の
#         差分取得の入力。R2 の鍵があれば R2 から、無ければ公開 URL から
# 学習のチェックポイント ($HOME/spsp-state/learn) とキャラ投票の差分取得の起点 (build/data/char_votes_*.json) は
# ワークフローの actions/cache が出し入れする。値はログに出さない (Secrets は Actions が伏せ字にする)。
set -euo pipefail
REGION=${1:?jp|na}
cd "$(dirname "$0")/../.."
umask 077

# ── 秘密ファイル ──
mkdir -p "$HOME/spsp-secrets"
[ -n "${STARTGG_OAUTH_CLIENT_SECRET:-}" ] && printf '%s' "$STARTGG_OAUTH_CLIENT_SECRET" > "$HOME/spsp-secrets/startgg_oauth_client_secret"
[ -n "${STARTGG_OAUTH_CLIENT_SECRET_CF:-}" ] && printf '%s' "$STARTGG_OAUTH_CLIENT_SECRET_CF" > "$HOME/spsp-secrets/startgg_oauth_client_secret_cf"

# ── rclone (R2) ──
HAVE_R2=0
if [ -n "${R2_ACCESS_KEY_ID:-}" ] && [ -n "${R2_SECRET_ACCESS_KEY:-}" ] && [ -n "${R2_ENDPOINT:-}" ]; then
  # ConoHa と同じ版を公式から取り、SHA-256 を照合してから使う。Ubuntu の apt の rclone (1.60) は R2 への
  # アップロードが全部 "501 NotImplemented" で失敗し、再試行を繰り返して止まった (2026-09-28)
  RCLONE_VER=v1.75.1
  # downloads.rclone.org/$RCLONE_VER/SHA256SUMS。arm64 は北米を回す Mac の Linux VM (セルフホストランナー、2026-10-16〜)
  case "$(uname -m)" in
    aarch64|arm64) RARCH=linux-arm64; RCLONE_SHA256=03f2504174034b6d004152ed7369251c9a9ec1f7e0836eda420f5c7a5ec0dff9 ;;
    *)             RARCH=linux-amd64; RCLONE_SHA256=982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab ;;
  esac
  RZIP="${RUNNER_TEMP:-/tmp}/rclone.zip"
  curl -fsSL "https://downloads.rclone.org/$RCLONE_VER/rclone-$RCLONE_VER-$RARCH.zip" -o "$RZIP"
  echo "$RCLONE_SHA256  $RZIP" | sha256sum -c --quiet - || { echo "ERROR: rclone の SHA-256 が一致しない" >&2; exit 1; }
  unzip -q -j -o "$RZIP" "rclone-$RCLONE_VER-$RARCH/rclone" -d "$HOME/bin" && chmod 755 "$HOME/bin/rclone"
  export PATH="$HOME/bin:$PATH"
  mkdir -p "$HOME/.config/rclone"
  cat > "$HOME/.config/rclone/rclone.conf" <<CONF
[r2]
type = s3
provider = Cloudflare
access_key_id = $R2_ACCESS_KEY_ID
secret_access_key = $R2_SECRET_ACCESS_KEY
endpoint = $R2_ENDPOINT
acl = private
CONF
  HAVE_R2=1
  echo "rclone: remote r2 を設定 ($(rclone version | head -1))"
fi
umask 022

# ── 前回デプロイ分 (site/ 配下の入力) ──
mkdir -p site/data
PUBLIC=https://data.spsp.games/$REGION
if [ "$HAVE_R2" = 1 ]; then
  RC=(rclone copy --s3-no-check-bucket --transfers 32 --checkers 32 --fast-list)
  "${RC[@]}" "r2:spsp-data/$REGION/latest_tjpr_full.jsonl" site/
  "${RC[@]}" "r2:spsp-data/$REGION/data" site/data --include overseas.json --include upcoming_entrants.json
  echo "前回分: R2 から取得"
else
  # Cloudflare 配下は既定の User-Agent を弾くことがあるので名乗る
  UA='spsp-ci (github actions)'
  curl -fsSL -A "$UA" "$PUBLIC/latest_tjpr_full.jsonl" -o site/latest_tjpr_full.jsonl
  curl -fsSL -A "$UA" "$PUBLIC/data/overseas.json" -o site/data/overseas.json
  if [ "$REGION" = jp ]; then curl -fsSL -A "$UA" "$PUBLIC/data/upcoming_entrants.json" -o site/data/upcoming_entrants.json || true; fi
  echo "前回分: 公開 URL から取得 (R2 の鍵なし)"
fi
[ -s site/data/overseas.json ] || { echo "ERROR: 前回の overseas.json を取れなかった" >&2; exit 1; }

echo "prepare ($REGION) done"

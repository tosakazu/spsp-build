#!/bin/bash
# Deploy V4 SPSP site (= /spsp/) to GitHub Pages.
# Usage: ./deploy_v4.sh <build_dir> [commit_message]
#   - source は site/ ではなく V4 BUILD_DIR (= build_v4_full.py + [4/5] の出力)
#   - rsync -aL --delete で BUILD_DIR と gh-pages の spsp/ を完全に一致させる
#     (symlink は実体化。BUILD_DIR に無いファイルは gh-pages から消える。消えた一覧はログに出す)
#   - __SITE_VERSION__ を「html/js/css の内容ハッシュ · 時刻」に置換してから commit + push + 反映確認
#   - commit 前に origin/gh-pages を fetch して HEAD を合わせる (他所から push されていても non-fast-forward で止まらない)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=deploy/paths.sh
source "$REPO_ROOT/deploy/paths.sh"

BUILD_DIR="${1:-}"
MSG="${2:-Deploy V4}"
SITE="$SPSP_GHPAGES_CLONE"
DST="$SITE/spsp"

if [ -z "$BUILD_DIR" ] || [ ! -d "$BUILD_DIR" ]; then
  echo "ERROR: build dir not found: '$BUILD_DIR'"
  echo "  Usage: $0 <build_dir> [commit_message]"
  echo "  Run V4 build first: $REPO_ROOT/spsp/cli/build_full.py YYYY-MM-DD <build_dir> --phase learn && ... --phase build"
  exit 1
fi
if [ ! -f "$BUILD_DIR/meta.json" ] || [ ! -f "$BUILD_DIR/latest_tjpr_full.jsonl" ] || [ ! -d "$BUILD_DIR/players" ] \
   || [ ! -d "$BUILD_DIR/history" ] || [ ! -f "$BUILD_DIR/players_current.json" ]; then
  echo "ERROR: $BUILD_DIR にビルド生成物 (meta.json / latest_tjpr_full.jsonl / players/ / history/ / players_current.json) が揃っていない — 中止"
  exit 1
fi

# venv python3 (探索順は deploy/paths.sh の spsp_python に一本化。見つからなければ止める:
# 空の PY で `$PY - <<EOF` が黙って走ると __SITE_VERSION__ が未置換のまま配信される)
PY=$(spsp_python "$REPO_ROOT") || exit 1

# gh-pages clone が無ければ作る (以前は /tmp にあり、掃除で消えるたびに壊れていた)
if [ ! -d "$SITE/.git" ]; then
  echo "[heal] $SITE missing — cloning gh-pages ..."
  rm -rf "$SITE"
  # --depth 1: 毎晩数万ファイルを commit する branch なので履歴を取ると巨大 (数十 GB) になり、CPU 制限でも落ちる
  git clone --depth 1 --branch gh-pages --single-branch "$SPSP_GHPAGES_REMOTE" "$SITE"
  git -C "$SITE" config gc.auto 0   # gc は月次の別ジョブで (deploy 中に走ると CPU 制限で落ちる)
fi
# clone を作り直した直後は commit の author が無い (2026-09-06 のリハーサルで rc=128) → 無ければ設定する
if [ -z "$(git -C "$SITE" config --local --get user.name || true)" ]; then
  git -C "$SITE" config user.name "$SPSP_GHPAGES_AUTHOR_NAME"
  git -C "$SITE" config user.email "$SPSP_GHPAGES_AUTHOR_EMAIL"
  echo "[heal] git author を設定 ($SPSP_GHPAGES_AUTHOR_NAME)"
fi
# Self-heal git state: HEAD ファイルが消失していたら復元する
if [ ! -f "$SITE/.git/HEAD" ]; then
  echo "[heal] $SITE/.git/HEAD missing — restoring (gh-pages)"
  echo "ref: refs/heads/gh-pages" > "$SITE/.git/HEAD"
fi

# ── __SITE_VERSION__ の置換 ──
# 内容ハッシュ = players/ 以外の html/js/css を連結した sha256 の先頭 7 桁。
# フロントが「同じ版か」を見分けるのに使う (players/ の html は対象外)。
stamp_site_version() {   # $1 = 配信ツリー (spsp/)。標準出力に VERSION 文字列を返す
  local dst="$1" ts hash ver sha_cmd
  ts=$(date -u +'%Y-%m-%d %H:%M UTC')
  # sha256sum (Linux) → shasum (Mac, BusyBox 等) の順で fallback
  if command -v sha256sum >/dev/null 2>&1; then sha_cmd="sha256sum"
  elif command -v shasum >/dev/null 2>&1; then sha_cmd="shasum -a 256"
  else echo "ERROR: sha256sum / shasum 共に見つからない" >&2; return 1; fi
  hash=$(find "$dst" -path "$dst/players" -prune -o -path "$dst/history" -prune -o \
           \( -name '*.html' -o -name '*.js' -o -name '*.css' \) -print 2>/dev/null \
         | sort | xargs cat 2>/dev/null | $sha_cmd | cut -c1-7)
  ver="v ${hash} · ${ts}"
  DST="$dst" VER="$ver" "$PY" - <<'PYEOF'
import os
dst = os.environ["DST"]
ver = os.environ["VER"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
for root, _, files in os.walk(dst):
    if "/players" in root or "/history" in root: continue
    for f in files:
        if not f.endswith(".html"): continue
        p = os.path.join(root, f)
        s = open(p, encoding="utf-8").read()
        if "__SITE_VERSION__" in s:
            open(p, "w", encoding="utf-8").write(s.replace("__SITE_VERSION__", ver))
PYEOF
  echo "$ver"
}

echo "[$(date +%H:%M:%S)] [1/4] rsync $BUILD_DIR -> $DST (--delete) ..."
# -L: symlinks を dereference (= site/c 等への symlink を実体コピー)
# --delete: BUILD_DIR に無いものを gh-pages から消す (退会/統合で消えた選手ページ、移動した js 等)。
#   除外 (demo 等) は消さない。消したものは itemize 出力から拾ってログに出す。
mkdir -p "$DST"
RSYNC_LOG=$(mktemp)
# rsync 単独で走らせて終了コードを見る (以前は | tee | grep -c で rc が消え、途中で死んでも --delete 半適用のまま commit していた)
rsync -aL --delete --itemize-changes \
  --exclude '.netlify' --exclude '.DS_Store' --exclude 'demo' --exclude '*.pkl' --exclude '.*.done' --exclude 'blog' \
  "$BUILD_DIR/" "$DST/" > "$RSYNC_LOG" || { echo "ERROR: rsync failed (rc=$?) — 半端な状態なので commit しない"; rm -f "$RSYNC_LOG"; exit 1; }
echo "  changed entries: $(grep -c . "$RSYNC_LOG" || true)"
DELETED=$(grep '^\*deleting' "$RSYNC_LOG" | awk '{print $2}' || true)
if [ -n "$DELETED" ]; then
  echo "  deleted from gh-pages: $(echo "$DELETED" | wc -l) (players/: $(echo "$DELETED" | grep -c '^players/' || true), history/: $(echo "$DELETED" | grep -c '^history/' || true), tournaments/: $(echo "$DELETED" | grep -c '^tournaments/' || true))"
  echo "$DELETED" | grep -vE '^(players|history|tournaments)/' | sed 's/^/    /' || true
fi
rm -f "$RSYNC_LOG"

echo "[$(date +%H:%M:%S)] [2/4] stamp __SITE_VERSION__ ..."
VERSION=$(stamp_site_version "$DST")
CONTENT_HASH=$(echo "$VERSION" | awk '{print $2}')
echo "  → $VERSION"

echo "[$(date +%H:%M:%S)] [3/4] commit + push ..."
cd "$SITE"

# Self-heal: 古い loose object が消えることがある (2026-07-12〜14 に /tmp 掃除で
# commit が "invalid object ... Error building trees" 落ちし 3 日間デプロイ停止)。
# index が参照する blob が消えていたら working tree の実ファイルから再生成する
# (= 内容が変わっていなければ同一 hash で復元される。変わっていれば後続の git add が
#    新 blob を書くので放置で OK)。
MISSING_BLOBS=$(git ls-files -s | awk '{print $2}' | sort -u \
  | git cat-file --batch-check 2>/dev/null | awk '$2=="missing"{print $1}')
if [ -n "$MISSING_BLOBS" ]; then
  echo "[heal] index が参照する loose object が $(echo "$MISSING_BLOBS" | wc -l) 個消失 — 再生成"
  for h in $MISSING_BLOBS; do
    git ls-files -s | awk -F'\t' -v h="$h" 'index($1, h){print $2}' | while IFS= read -r p; do
      NH=$(git hash-object -w -- "$p" 2>/dev/null || true)
      if [ "$NH" = "$h" ]; then
        echo "  [heal] restored $h  $p"
      else
        echo "  [heal] $p は内容変更あり (index=$h) — git add で更新される"
      fi
    done
  done
fi

# 他所 (別マシン・手動) から gh-pages が進んでいても non-fast-forward で止まらないよう、origin の先端に HEAD を合わせる。
# --mixed: index を origin の tree にし、作業 tree (spsp/ は rsync 済 = 望む状態) はそのまま。spsp/ 以外の差は stage しないので触らない。
if git fetch -q --depth 1 origin gh-pages 2>/dev/null; then
  if [ "$(git rev-parse HEAD)" != "$(git rev-parse FETCH_HEAD)" ]; then
    echo "  origin/gh-pages が進んでいた ($(git rev-parse --short HEAD) → $(git rev-parse --short FETCH_HEAD)) — HEAD を合わせる"
    git reset -q --mixed FETCH_HEAD
  fi
else
  echo "  [warn] origin/gh-pages を fetch できない — ローカル HEAD のまま続行 (push が拒否されたら次回に持ち越し)"
fi
git add -A spsp/
[ -d "$SITE/banzuke" ] && git add -A banzuke/ || true
if git diff --cached --quiet; then
  echo "  → no changes, nothing to commit"
  exit 0
fi
git commit -m "$MSG"
git push origin gh-pages

echo "[$(date +%H:%M:%S)] [4/4] verify live site ..."
TARGET="$CONTENT_HASH"
SECONDS_WAITED=0
# curl の出力は変数で受ける: `curl | grep -q` だと grep が先に閉じた時に curl が rc=23 になり、
# pipefail 下では一致していても「未反映」扱いになる (2026-09-07 08:34〜09-08 00:18 の全回が偽 timeout)
until LIVE_HTML=$(curl -fsS "$SPSP_SITE_URL/index.html?$(date +%s)" 2>/dev/null) && grep -q "$TARGET" <<<"$LIVE_HTML"; do
  sleep 10
  SECONDS_WAITED=$((SECONDS_WAITED + 10))
  if [ "$SECONDS_WAITED" -gt 240 ]; then
    echo "  → timeout waiting for $TARGET (deployed but not live yet — give it a minute)"
    exit 0
  fi
done
echo "  → live (${SECONDS_WAITED}s)"
echo
echo "✅ deployed: $VERSION"

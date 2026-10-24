#!/usr/bin/env bash
# update_and_deploy.sh — 大会データ取得 → V4 ビルド → デプロイ を一連実行
#
# 使い方:
#   bash deploy/update_and_deploy.sh ["commit メッセージ"] [--days 7] [--eval-date YYYY-MM-DD]
#
# 引数:
#   $1: deploy 時の commit メッセージ (= 省略時 "定期更新 YYYY-MM-DD")
#   --days N: 直近 N 日分の大会を取得 (= 省略時 7)
#   --eval-date YYYY-MM-DD: V4 ビルドの評価基準日 (= 省略時 today)
#   --build-dir PATH: V4 build 出力先 (= 省略時 $SPSP_BUILD_ROOT/manual_<TS>、deploy/paths.sh)
#   --skip-download: ダウンロードを skip (= 既存データでビルドのみ)
#   --build-only: 取得と入力生成 ([1/5]〜[2.8/5]) を全部 skip し、今あるデータと入力でビルド以降だけ
#   --skip-deploy: デプロイを skip (= ビルドまで止める)
#
# 環境変数:
#   STARTGG_TOKEN: API トークン (= 省略時 ./STARTGG_TOKEN 等のファイルから読む)。取得を skip するときは無くてよい
#   SPSP_DEPLOY_GHPAGES: 0 で gh-pages へのデプロイを飛ばす (既定 1)
#   SPSP_DEPLOY_R2: 0 で R2 への同期を飛ばす (既定 1 = rclone の remote r2 があるときだけ)
#   SPSP_R2_SYNC_WAIT: 1 で R2 同期をこの場で待つ (既定 0 = 切り離して起動)。GitHub Actions ではジョブの終了で
#                      切り離したプロセスごと消えるので 1 にする (deploy/ci/README.md)
#   RCLONE: rclone の実行ファイル (既定 $HOME/bin/rclone、無ければ PATH の rclone)
#
# 流れ:
#   1. start.gg から直近 N 日分の大会データ取得
#   2. fetch_upcoming で未来大会一覧 (= seed picker / upcoming.json) を更新
#   3. build_v4_full.py で V4 ranking を <build-dir> に生成 (learn / build の 2 プロセス)
#   4. build/build_*.py で aux JSON を <build-dir>/data に生成、4.5 で site/ に反映
#   5. deploy_v4.sh で gh-pages branch に rsync --delete + push

set -euo pipefail
cd "$(dirname "$0")/.."  # = repo root
mkdir -p site/data   # site/ は git 追跡なし (2026-09-29 からフロントも絵文字表も spsp-web)。新しい checkout でも書き込み先を用意する
# shellcheck source=deploy/paths.sh
source deploy/paths.sh

# ── 引数パース ──
COMMIT_MSG=""
# 14 日: 列挙・窓判定は endAt 基準 (download.py / queries.py, 2026-07-21〜)。
# startAt 基準だった頃の既知の取得漏れ:
#   - 7 日窓: endAt が startAt+6日 の週次大会 (例: シカブラ) で「初回取得(空)の
#     翌日に窓から外れて永久に空のまま」(2026-07 シカブラ#99/#100, FlyHigh#27)。
#   - 14 日窓: TO が startAt を告知/登録開始日に設定した大会 (例: 船スマ 7/19
#     開催分 = startAt 6/8) が開催完了時には窓外 (2026-07-21 発覚)。
# endAt 基準なら両ケースとも「終了日が直近 14 日」に入るため取得できる。
# champion-missing retry (終了+7日) も endAt 基準なので 14 日窓で覆える。
DAYS=14
EVAL_DATE=""
BUILD_DIR=""
SKIP_DOWNLOAD=0
SKIP_INPUTS=0
SKIP_DEPLOY=0
FAILED_STEPS=()   # 失敗しても前回分で続行したステップ (最後にまとめて出す)
print_failed_steps() {   # 途中で exit しても必ず出す (EXIT trap)
  if [[ ${#FAILED_STEPS[@]} -gt 0 ]]; then
    echo " ⚠️  失敗して前回分のまま進めたステップ (${#FAILED_STEPS[@]}):"
    for s in "${FAILED_STEPS[@]}"; do echo "   - $s"; done
  fi
}
trap 'rc=$?; print_failed_steps; exit $rc' EXIT
while [[ $# -gt 0 ]]; do
  case "$1" in
    --days) DAYS="$2"; shift 2;;
    --eval-date) EVAL_DATE="$2"; shift 2;;
    --build-dir) BUILD_DIR="$2"; shift 2;;
    --skip-download) SKIP_DOWNLOAD=1; shift;;
    --build-only) SKIP_DOWNLOAD=1; SKIP_INPUTS=1; shift;;
    --skip-deploy) SKIP_DEPLOY=1; shift;;
    *)
      if [[ -z "$COMMIT_MSG" ]]; then COMMIT_MSG="$1"; fi
      shift;;
  esac
done

# 多重起動ガード。nightly_cron.sh 経由ならそちらが同じロックを持っている (SPSP_LOCK_HELD=1)。
# 手動実行が nightly と同じ BUILD_DIR・site/・gh-pages clone を触らないようにする。
if [[ -z "${SPSP_LOCK_HELD:-}" ]]; then
  exec 9>"$SPSP_LOCK_FILE"
  flock -n 9 || { echo "ERROR: 別の nightly / 手動実行が動作中 ($SPSP_LOCK_FILE)" >&2; exit 1; }
fi
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1   # cgroup nproc=1 で OpenBLAS が死ぬ

TODAY=$(date +%Y-%m-%d)
START_DATE=$(date -v-${DAYS}d +%Y-%m-%d 2>/dev/null || date -d "-${DAYS} days" +%Y-%m-%d)
[[ -n "$START_DATE" ]] || { echo "ERROR: 取得開始日を計算できない (date -v / date -d が両方失敗)" >&2; exit 1; }
[[ -z "$COMMIT_MSG" ]] && COMMIT_MSG="定期更新 $TODAY"
[[ -z "$EVAL_DATE" ]] && EVAL_DATE="$TODAY"
[[ -z "$BUILD_DIR" ]] && BUILD_DIR="$SPSP_BUILD_ROOT/manual_$(date +%Y%m%d_%H%M%S)"

# ── token ──
# env → $SPSP_SECRETS_DIR → repo 内ファイル → 旧 repo (= ~/spsp-ranking) の順で探す。値はログ・argv に出さない。
if [[ -z "${STARTGG_TOKEN:-}" ]]; then
  for tok_path in "$SPSP_SECRETS_DIR/STARTGG_TOKEN" STARTGG_TOKEN .startgg_token \
                  "$HOME/spsp-ranking/STARTGG_TOKEN" "$HOME/spsp-ranking/.startgg_token"; do
    if [[ -f "$tok_path" ]]; then
      STARTGG_TOKEN=$(tr -d '[:space:]' < "$tok_path")
      break
    fi
  done
  if [[ -z "${STARTGG_TOKEN:-}" ]]; then
    if [[ "$SKIP_DOWNLOAD" -eq 0 ]]; then
      echo "ERROR: STARTGG_TOKEN env nor token file found." >&2
      exit 1
    fi
    echo "  (STARTGG_TOKEN なし: 取得は skip なので続行)"
  fi
fi

# ── Python venv (探索順は deploy/paths.sh の spsp_python に一本化) ──
VENV_PY=$(spsp_python "$(pwd)") || exit 1

echo "════════════════════════════════════════════════════════════"
echo " SPSP V4 update + build + deploy パイプライン"
echo " repo: $(pwd) ($(git branch --show-current 2>/dev/null || echo '?'))"
echo " 取得期間: $START_DATE 〜 $TODAY (= --days $DAYS)"
echo " eval-date: $EVAL_DATE"
echo " build-dir: $BUILD_DIR"
echo " commit msg: \"$COMMIT_MSG\""
echo "════════════════════════════════════════════════════════════"

# ── 1. Download (= Japan のみ) ──
# ⚠️ 重要: download.py は --startgg-dir のデフォルトが CWD/data/startgg (イベントは <root>/Japan/events/...) なので、
# smash_db_tournament/ に cd してから実行する必要がある.
# トークンは argv に載せず (ps で他ユーザーに見える) 環境変数で子プロセスに渡す
export STARTGG_TOKEN
if [[ "$SKIP_DOWNLOAD" -eq 0 ]]; then
  echo
  echo "[$(date +%H:%M:%S)] [1/5] 📥 過去大会のダウンロード ($START_DATE 〜 $TODAY, country_code=JP) ..."
  # download.py は大会単位の失敗 (FetchError) を印字して続行し rc=0 で終わるので、件数を数えて要約に出す
  # (2026-09-12: 同名関数の上書きで全大会 FetchError になったまま 3 回 rc=0 で通った事故の再発防止)。
  DL_OUT="$(mktemp)"
  (
    cd smash_db_tournament
    "$VENV_PY" -u scripts/common/download.py \
      --start-date "$TODAY" --finish-date "$START_DATE" \
      --country-code JP \
      --startgg-dir data/startgg \
      --done-file-path data/startgg/Japan/done.csv \
      --users-file-path data/startgg/Japan/users.jsonl \
      --tournament-file-path data/startgg/Japan/tournaments.jsonl \
      --awaiting-file data/startgg/Japan/manual/awaiting_resume.json 2>&1 | tee "$DL_OUT"
  )
  DL_ERRS=$(grep -c "FetchError" "$DL_OUT" || true); rm -f "$DL_OUT"
  if [[ "$DL_ERRS" -gt 0 ]]; then
    echo "  WARN: download で FetchError $DL_ERRS 件 (該当大会は done にならず次回取り直し。ログの 'FetchError on tournament' 行を見る)"
    FAILED_STEPS+=("download: FetchError $DL_ERRS 件 (該当大会は次回取り直し。ログの FetchError 行を見る)")
  fi
  echo
  echo "[$(date +%H:%M:%S)] [2/5] 📅 未来大会一覧の取得 (= seed picker 用 upcoming.json, country=JP, 21 日先) ..."
  # 開催予定はデータ側 (data/startgg/Japan/upcoming.json) が持ち場。site/data へは [4/5] の build_upcoming_json が
  # 地域 DB から写す (2026-09-16〜。北米も同じ経路で入るように、ここでの cp はやめた)
  "$VENV_PY" -u smash_db_tournament/scripts/common/fetch_upcoming.py \
    --country JP --lookahead-days 21
  echo
  echo "[$(date +%H:%M:%S)] [2.5/5] 🏷  クラス bracket データ更新 (= phases.json + class virtual 大会生成) ..."
  # 失敗してもビルドは止めない (= クラス分離が 1 日遅れるだけ)
  (
    cd smash_db_tournament
    "$VENV_PY" -u scripts/common/update_class_data.py --region Japan --since-days 30
  ) || { echo "  WARN: update_class_data failed (continuing)"; FAILED_STEPS+=("update_class_data (クラス大会データは前回分)"); }
  echo
  echo "[$(date +%H:%M:%S)] [2.55/5] 🧭 判定 (derived.json: 1on1 / 大会名パターン / 暦 / クラス bracket) の更新 ..."
  # 判定は smash_database 側 (scripts/Japan/classify.py)。spsp は読むだけで、無いイベントがあるとビルドが止まる
  # (推測しない) ので、ここは失敗したら止める。更新のあったイベントだけ書く (内容が同じなら触らない)。
  (
    cd smash_db_tournament
    "$VENV_PY" -u scripts/common/derive.py --region Japan
  ) || { echo "ERROR: derive failed — derived.json が揃わないのでビルドしない" >&2; exit 1; }
  echo
  echo "[$(date +%H:%M:%S)] [2.57/5] 🔎 データ整合性チェック (validate_data.py、種別ごとの件数を baseline と比較) ..."
  # 既知の不整合 (user_id を持たない参加者・bracket を回していない大会など) は常に数百件あり、ランキングは
  # それを許容している。ここで見たいのは「急に増えていないか」だけなので、種別ごとの件数を
  # data/startgg/Japan/validation_baseline.json と突き合わせ、許容幅 (既定 10) 以内なら基準を現在値に寄せる。
  # 超えたときだけ WARN。ビルドは止めない (2 秒で終わる読み取りだけの処理)。
  (
    cd smash_db_tournament
    "$VENV_PY" -u scripts/common/fix/validate_data.py --region Japan \
      --baseline data/startgg/Japan/validation_baseline.json
  ) || { echo "  WARN: validate_data が種別ごとの増加を検出 (continuing)"; FAILED_STEPS+=("validate_data (データ不整合が baseline より増えた — 上のログの REGRESSION 行を見る)"); }

  # NOTE: キャラ選択(games)は [1/5] の download.py が with_games 統合クエリで
  # matches.json の details に直接書き込むため、専用の fetch_character_games /
  # merge_character_games パス (旧 [2.7/5]) は不要になった (= 同じ sets の二重取得を解消).
  # 過去大会の一括 backfill が必要なときだけ scripts/fetch/{fetch,merge}_character_games.py を手動実行する.
else
  echo "[$(date +%H:%M:%S)] [1/5] 📥 過去大会のダウンロード ... SKIPPED"
  echo "[$(date +%H:%M:%S)] [2/5] 📅 未来大会一覧の取得 ... SKIPPED"
  echo "[$(date +%H:%M:%S)] [2.5/5] 🏷  クラス bracket データ更新 ... SKIPPED"
fi

# ── 2.6. スコアシミュレーション用の参加予定者 = smash_database の data/startgg/Japan/upcoming_entrants.json を写す ──
# 2026-10-08 から取得は smash_database の download-japan (fetch_upcoming_entrants + annotate_upcoming) がやる。ビルドは
# smash_database のコードを動かさず、start.gg のトークンも持たない。無いとき (古いデータ) は前回分 (site/data) のまま
UE_SRC=smash_db_tournament/data/startgg/Japan/upcoming_entrants.json
echo
if [[ -s "$UE_SRC" ]]; then
  echo "[$(date +%H:%M:%S)] [2.6/5] 🔮 スコアシミュレーション用エントラント: $UE_SRC → site/data/upcoming_entrants.json"
  mkdir -p site/data && cp "$UE_SRC" site/data/upcoming_entrants.json
else
  echo "[$(date +%H:%M:%S)] [2.6/5] 🔮 スコアシミュレーション用エントラント ... WARN: $UE_SRC が無い (前回分のまま)"
  FAILED_STEPS+=("upcoming_entrants (データに無い。参加予定は前回分)")
fi

# ── 2.65. データ repo (smash_database) に当夜の取得分を commit & push ──
# 作業ツリーだけが進んで git から離れないように、data/startgg の変化を毎晩 1 commit にして origin/data-Japan へ push する。
# smash_db_tournament は data-Japan ブランチの checkout (= main の scripts/docs + data/startgg)。scripts の更新は main を merge して取り込む。
# 変化が無ければ commit しない (未 push の commit が残っていれば push だけする)。失敗してもビルド・デプロイは止めない (一覧に載せる)。
if [[ "$SKIP_DOWNLOAD" -eq 0 ]]; then
  echo
  echo "[$(date +%H:%M:%S)] [2.65/5] 💾 データブランチ (data-Japan) に commit (smash_db_tournament = data-Japan の checkout, 対象は data/startgg だけ) ..."
  if git -C smash_db_tournament add -A -- data/startgg && ! git -C smash_db_tournament diff --cached --quiet; then
    git -C smash_db_tournament commit -q -m "data: $TODAY nightly" \
      && echo "  → $(git -C smash_db_tournament log --oneline -1 | cut -c1-60) ($(git -C smash_db_tournament diff --stat HEAD~1 | tail -1))" \
      || { echo "  WARN: data commit failed (continuing)"; FAILED_STEPS+=("data repo の commit (作業ツリーは更新済み)"); }
  else
    echo "  → 変化なし"
  fi
  # push: origin/data-Japan より進んでいる時だけ。他地域のブランチはここでは触らない
  if [[ -n "$(git -C smash_db_tournament rev-list origin/data-Japan..data-Japan 2>/dev/null)" ]]; then
    git -C smash_db_tournament push -q origin data-Japan:data-Japan \
      && echo "  → push 済 (origin/data-Japan = $(git -C smash_db_tournament rev-parse --short origin/data-Japan))" \
      || { echo "  WARN: data push failed (continuing)"; FAILED_STEPS+=("data repo の push (commit は済み)"); }
  fi
  # push のたびに pack が 1 つ増え、pack をまたぐと差分圧縮が効かない (2026-09-09 に 10 個 = .git 208MB)。
  # gc.autoPackLimit=8 を設定してあるので、閾値を超えた回だけここで 1 つにまとめる (数秒〜数分、失敗は無視)。
  git -C smash_db_tournament gc --auto --quiet || echo "  WARN: git gc --auto failed (continuing)"
fi

# ── 2.7. 海外タグ再生成 (= site/data/overseas.json) ──
# v4/common.py (OVERSEAS_UIDS) が import 時に読むため、V4 ビルドより先に生成する。
# 失敗しても前回生成分がそのまま使われるだけなので止めない (= 1 日 stale になるだけ)。
if [[ "$SKIP_INPUTS" -eq 0 ]]; then
echo
echo "[$(date +%H:%M:%S)] [2.7/5] 🌏 海外タグ再生成 (= build_overseas_json.py → site/data/overseas.json) ..."
"$VENV_PY" -u spsp/cli/build_overseas_json.py || { echo "  WARN: build_overseas_json failed (continuing)"; FAILED_STEPS+=("build_overseas_json (海外タグは前回分)"); }
# 前回分も無い (= 新しい checkout) なら空集合で黙って順位が変わるので止める
[[ -s site/data/overseas.json ]] || { echo "ERROR: site/data/overseas.json が無い — 海外タグ無しではビルドしない" >&2; exit 1; }

# ── 2.8. キャラ投票の取り込み (= build/data/char_votes.json) ──
# メインキャラ判定 (V4 ビルドの char_usage) が読むので、必ずビルドより先に走らせる。
# 取得に失敗したときは前回取得分のまま進む (= 投票の反映が 1 回分遅れるだけ)。
# その場合ビルドログに古い fetched_at が出るので、放置しても気づける。
echo
echo "[$(date +%H:%M:%S)] [2.8/5] 🎮 キャラ投票の取り込み (= fetch_char_votes.py → build/data/char_votes.json) ..."
# 2026-09-22〜: 受け口は 2 つ。旧サイト (tosakazu.github.io、公開中) = GAS のシート、spsp.games (公開前) = Cloudflare Worker (D1)。
# 取得元ごとに別ファイルへ落とし (差分取得の起点をそれぞれ持つ)、merge_char_votes.py が build/data/char_votes.json に畳む (GAS の status 優先)。
# 片方が失敗してもその側は前回分のまま統合する。spsp.games 公開後に GAS を止めたら GAS 側の行を外す。
# 2026-09-29: GAS (旧サイトの投票の受け口) は止めた。GAS にしか無かった 7 件は D1 に移したので、取得は Worker だけ
"$VENV_PY" -u spsp/cli/fetch_char_votes.py --out build/data/char_votes_cf.json --endpoint https://spsp.games/api --secret-file "$HOME/spsp-secrets/startgg_oauth_client_secret_cf" || { [ -s build/data/char_votes_cf.json ] || { echo "ERROR: fetch_char_votes (Worker) failed で前回取得分も無い — 投票なしで出すとメインキャラが変わるのでビルドしない" >&2; exit 1; }
     echo "  WARN: fetch_char_votes (Worker) failed (前回取得分のまま続行)"; FAILED_STEPS+=("fetch_char_votes Worker (spsp.games の投票は前回取得分)"); }
"$VENV_PY" -u spsp/cli/merge_char_votes.py build/data/char_votes_cf.json || { echo "  WARN: merge_char_votes failed (前回の char_votes.json のまま続行)"; FAILED_STEPS+=("merge_char_votes"); }
else
  echo "[$(date +%H:%M:%S)] [2.7/5] 🌏 海外タグ再生成 ... SKIPPED (--build-only)"
  echo "[$(date +%H:%M:%S)] [2.8/5] 🎮 キャラ投票の取り込み ... SKIPPED (--build-only)"
fi

# ── 3. V4 Build ──
# 学習 (learn) は大会を時系列に逐次処理するので、状態を日付でチェックポイント保存し、次回はそこから
# 差分で進める (spsp/learn_state.py)。CPU 制限 (~300s/プロセス) 対策と所要時間短縮の両方。
#   - チェックポイント: $SPSP_STATE_ROOT/learn/state_YYYY-MM-DD.pkl (= その日まで学習済み、毎晩 1 つ。45 日前のものは最新 1 つ・月初 400 日・年末は永久)
#   - CK_DATE = EVAL_DATE - CK_LAG_DAYS。それより前の大会は download の窓 (14 日) の外なので普通は変わらない。
#     変わっていれば build_v4_full.py が終了コード 3 で止まるので、最初から年単位に分割して学習し直す。
#   - 最後に CK_DATE 以降の大会 (数十〜数百) を学習 (learn) → スナップショット (snap) → build。
# ランキングの設定は spsp/settings.py の既定値 (環境変数は読まない)。
echo
echo "[$(date +%H:%M:%S)] [3/5] 🛠  V4 ビルド (build_v4_full.py $EVAL_DATE $BUILD_DIR) ..."
BUILD_START=$SECONDS
CK_DIR="$SPSP_LEARN_DIR"
CK_LAG_DAYS=${SPSP_CK_LAG_DAYS:-45}   # ダウンロードの窓 (--days、既定 14) より十分長いこと: 窓の外の大会は普通変わらない前提
# SPSP_CK_LAG_DAYS: GitHub Actions は 14 (毎回学習し直す範囲を短くして実行時間を減らす。2026-09-28 ユーザー判断)。
#   それより古い大会のデータが変わった回は build_full.py が終了コード 3 を返し、さらに古いチェックポイント
#   (月初・年末) から学習し直すので、結果は常に同じ (時間が長くなる回があるだけ)。
# 取得もこのスクリプトでする (ConoHa) ときは、取り直しの窓より長くないと毎回やり直しになるので従来どおり止める。
if [[ "$SKIP_DOWNLOAD" -eq 0 ]]; then
  (( DAYS < CK_LAG_DAYS )) || { echo "ERROR: --days ($DAYS) はチェックポイントの遅れ ($CK_LAG_DAYS 日) より短くすること" >&2; exit 1; }
fi
FIRST_YEAR=2019         # 最初から学習し直すときの年分割の起点 (データは 2018 から。最初の区切りは 2019-12-31)
CK_DATE=$(date -d "$EVAL_DATE -${CK_LAG_DAYS} days" +%F 2>/dev/null || date -v-${CK_LAG_DAYS}d -j -f %Y-%m-%d "$EVAL_DATE" +%F)
# チェックポイントの保持 (日付は毎日 1 つ進む):
#   45 日前のもの (state_YYYY-MM-DD) は最新の 1 つだけ = 翌日の一歩 (46 日前 → 45 日前) と同日再実行に使う。
#   古いデータが変わったときの再開点は、その大会より前に取った状態しか役に立たないので、
#   月初 (state_YYYY-MM-01) 400 日 / 年末 (state_YYYY-12-31) 永久 を残す。
# (ファイル名は自分で付けた state_YYYY-MM-DD.pkl だけなので名前順 = 日付順)
# (プロセス置換 <(...) は /dev/fd の無い環境で落ちるので使わない: 2026-09-07)
# 該当ファイルが無いと ls / grep が非 0 になり、set -e -o pipefail の下では代入で落ちる (テスト環境で発覚) → || true
_CKS_LIST=$(ls -1 "$CK_DIR"/state_????-??-??.pkl 2>/dev/null | grep -vE 'state_[0-9]{4}-(12-31|[0-9]{2}-01)\.pkl$' | sort -r | tail -n +2 || true)
for f in $_CKS_LIST; do rm -f "$f"; done
find "$CK_DIR" -name 'state_*-01.pkl' -mtime +400 -delete 2>/dev/null
CK_FINAL="$CK_DIR/state_$CK_DATE.pkl"
# スナップショットのキャッシュ (点は暦に固定なので、その点より前のデータが変わらなければ翌日以降も当たる。
# 当たった点は mtime を更新するので、2 日使われなかったもの = 古いデータの点だけが消える)
mkdir -p "$SPSP_SNAP_CACHE"; find "$SPSP_SNAP_CACHE" -name '*.pkl' -mtime +2 -delete 2>/dev/null
run_build() {   # 引数はそのまま build_v4_full.py へ
  "$VENV_PY" -u spsp/cli/build_full.py "$EVAL_DATE" "$BUILD_DIR" "$@"
}
learn_until() {   # $1 = この日まで, $2 = 再開元 state (空 = 最初から)
  local args=(--phase learn --until "$1" --state-out "$CK_DIR/state_$1.pkl")
  [[ -n "$2" ]] && args+=(--state-in "$2")
  echo "  learn until $1 ${2:+(from $(basename "$2"))}"
  run_build "${args[@]}"
}
# 再開元 $2 (空 = 最初から) から $1 (= CK_DATE) まで学習する。年末ごとに別プロセスに分ける (ConoHa の
# ~300s/プロセス CPU 制限)。古いチェックポイントから 1 プロセスで数年ぶんを学習して kill された (2026-09-16 15:00、
# 2021 年末から 246s で辛うじて通った直後に build が落ちた) ので、最初からのときと同じ年分割にした。
# 再開元が古すぎて使えない (rc=3) ときは最初の 1 プロセスがそれを返すので、呼び出し側はさらに古いものを試す。
learn_chain() {   # $1 = 目標日, $2 = 再開元 state (空 = 最初から)
  local target="$1" sin="$2" d="" cut y rc
  [[ -n "$sin" ]] && { d=$(basename "$sin" .pkl); d=${d#state_}; }
  for y in $(seq "$FIRST_YEAR" $(( ${target:0:4} - 1 ))); do
    cut="$y-12-31"
    [[ "$cut" < "$target" ]] || continue
    [[ -z "$d" || "$cut" > "$d" ]] || continue     # 再開元より後の年末だけ
    learn_until "$cut" "$sin" || return $?
    sin="$CK_DIR/state_$cut.pkl"
  done
  learn_until "$target" "$sin"
}
ensure_checkpoint() {   # $CK_FINAL (= CK_DATE まで学習済みの状態) を用意する。無ければ古いものから、それも無ければ最初から
LEARN_RC=0
if [[ ! -f "$CK_FINAL" ]]; then
  # CK_DATE 以前のチェックポイントを新しい順に試す (以前のデータが変わっていれば rc=3 → さらに古いものへ)
  PREV=""
  for f in $(ls -1 "$CK_DIR"/state_????-??-??.pkl 2>/dev/null | sort -r || true); do
    d=$(basename "$f" .pkl); d=${d#state_}
    [[ "$d" < "$CK_DATE" ]] || continue
    # set -e の下では失敗した関数呼び出しで即終了してしまうので、&&/|| で終了コードを受ける
    learn_chain "$CK_DATE" "$f" && LEARN_RC=0 || LEARN_RC=$?
    if [[ $LEARN_RC -eq 0 ]]; then PREV="$f"; break
    elif [[ $LEARN_RC -eq 3 ]]; then
      echo "  ⚠️  $(basename "$f") から再開できない (rc=3: 大会データの変更か、状態の形式/設定の変更。理由は直前のログ) → さらに古いものを試す"
      FAILED_STEPS+=("learn: $(basename "$f") から再開不可 (rc=3、理由はログ)")
    else
      echo "❌ V4 学習失敗 (rc=$LEARN_RC) — デプロイ中止"; exit "$LEARN_RC"
    fi
  done
  if [[ -z "$PREV" ]]; then
    # 最初から (年ごとに別プロセス)
    learn_chain "$CK_DATE" "" || { echo "❌ V4 学習失敗 (最初から)"; exit 1; }
  fi
else
  echo "  checkpoint $CK_FINAL あり (同日の再実行)"
fi
}
ensure_checkpoint
# CK_DATE 以降を学習 (learn) → スナップショット等を作って inter を退避 (snap) → build。各々別プロセス (CPU 制限)
BUILD_RC=0
run_build --phase learn --state-in "$CK_FINAL" --state-out "$BUILD_DIR/_learn_state.pkl" && LEARN_RC=0 || LEARN_RC=$?
if [[ $LEARN_RC -eq 3 ]]; then
  # 同日の再実行で、当日作ったチェックポイント以前の大会を取り直していた場合。翌日まで全部失敗し続けないよう 1 回だけ作り直す
  echo "  ⚠️  $(basename "$CK_FINAL") から再開できない (rc=3: データ変更か状態の形式/設定の変更) → 作り直して再試行"
  FAILED_STEPS+=("learn: 当日のチェックポイントから再開できず作り直した (rc=3、理由はログ)")
  rm -f "$CK_FINAL"; ensure_checkpoint
  run_build --phase learn --state-in "$CK_FINAL" --state-out "$BUILD_DIR/_learn_state.pkl" && LEARN_RC=0 || LEARN_RC=$?
fi
if [[ $LEARN_RC -ne 0 ]]; then BUILD_RC=$LEARN_RC
else
  run_build --phase snap --state-in "$BUILD_DIR/_learn_state.pkl" --snap-cache "$SPSP_SNAP_CACHE" \
    && run_build --phase build || BUILD_RC=$?
fi
echo "  elapsed: $((SECONDS - BUILD_START))s (rc=$BUILD_RC)"
# ビルド失敗 (learn/build どちらか) ならデプロイへ進まず中止 (= 壊れた/古いデータを push しない)
if [[ "$BUILD_RC" -ne 0 ]]; then
  echo "❌ V4 ビルド失敗 (rc=$BUILD_RC) — デプロイ中止"
  exit "$BUILD_RC"
fi

# ── 4. Aux JSON (= tournaments.json / series.json / 都道府県 / サブ順位 / discriminator / 最終参加日 / 統合表) ──
# すべて $BUILD_DIR を基点に読み書きする (meta.json / latest_tjpr_full.jsonl / players/ / tournaments/ は
# 当夜ビルド分、出力は $BUILD_DIR/data/)。配信中の site/ にはここでは触らず、[4.5/5] でまとめて反映する。
# 失敗したものは前夜分が配信され続けるだけなのでデプロイは止めない (tournaments/series だけは必須)。
echo
echo "[$(date +%H:%M:%S)] [4/5] 📝 aux JSON → $BUILD_DIR/data ..."
# 7 本を 1 プロセスで (spsp/cli/build_aux.py)。必須 (tournaments/series) の失敗は rc=1 で止まり、任意のものの失敗は rc=2 で続く
AUX_RC=0
"$VENV_PY" -u spsp/cli/build_aux.py --build-dir "$BUILD_DIR" || AUX_RC=$?
if [[ $AUX_RC -eq 1 ]]; then echo "❌ aux JSON (必須) 失敗 — デプロイ中止"; exit 1
elif [[ $AUX_RC -ne 0 ]]; then echo "  WARN: aux JSON の一部が失敗 (ログの [aux] WARN 参照)"; FAILED_STEPS+=("aux JSON の一部 (前夜分のまま。ログの [aux] WARN 参照)"); fi

# ── 4.5. site/ への反映 (spsp.games は site/ を直接配信している) ──
# ビルド生成物と aux JSON を一括で写す。players/ tournaments/ は site 側が gh-pages clone への
# symlink なので [5/5] のデプロイで更新される。
echo
echo "[$(date +%H:%M:%S)] [4.5/5] 📂 site/ に反映 (meta.json, latest_tjpr_full.jsonl, players_current.json, data/*.json) ..."
# 配信中のファイルは tmp → mv で置き換える (cp の上書き中に読まれると切れたファイルが見える)
cp "$BUILD_DIR/meta.json" site/meta.json.tmp && mv site/meta.json.tmp site/meta.json
cp "$BUILD_DIR/latest_tjpr_full.jsonl" site/latest_tjpr_full.jsonl.tmp && mv site/latest_tjpr_full.jsonl.tmp site/latest_tjpr_full.jsonl
# 全選手の揮発部分 (順位・スコア・ピーク。docs/refactor/04_player_json_split.md)。players/ history/ tournaments/ は
# gh-pages clone への symlink なので [5/5] のデプロイで更新される
if [[ -f "$BUILD_DIR/players_current.json" ]]; then
  cp "$BUILD_DIR/players_current.json" site/players_current.json.tmp && mv site/players_current.json.tmp site/players_current.json
else
  echo "  WARN: $BUILD_DIR/players_current.json が無い (前夜分のまま)"; FAILED_STEPS+=("site/players_current.json (前夜分のまま)")
fi
# site/history は site/players と同じく gh-pages clone への symlink (無ければ players と同じ先に作る)
if [[ ! -e site/history && -L site/players ]]; then
  _players_target=$(readlink site/players)
  ln -s "${_players_target%/players}/history" site/history && echo "  site/history → ${_players_target%/players}/history (symlink 作成)"
fi
for f in tournaments series geo upcoming player_prefectures tournament_prefectures player_subranks \
         discriminators last_activity user_merges character_index; do
  if [[ -f "$BUILD_DIR/data/$f.json" ]]; then
    cp "$BUILD_DIR/data/$f.json" "site/data/$f.json.tmp" && mv "site/data/$f.json.tmp" "site/data/$f.json"
  else
    echo "  WARN: $BUILD_DIR/data/$f.json が無い (前夜分のまま)"; FAILED_STEPS+=("site/data/$f.json (前夜分のまま)")
  fi
done

# 大会 → 大会アイコンの表 (カードの裏面のシールの絵、2026-10-20〜)。R2 への同期で一緒に配る。失敗したら前回の表のまま
echo "[$(date +%H:%M:%S)] [4.9/5] 🖼️  大会アイコンの表 (deploy/build_event_icons.py → $BUILD_DIR/data/event_icons.json)"
"$VENV_PY" deploy/build_event_icons.py "$BUILD_DIR" || FAILED_STEPS+=("大会アイコンの表 (前回分のまま)")

# ── 5. Deploy ──
if [[ "$SKIP_DEPLOY" -eq 0 ]]; then
  echo
  # [5.5/5] Cloudflare R2 へ JSON を差分同期 (2026-09-22〜、Cloudflare 移行の ②)。rclone の remote r2 が設定されているときだけ。
  # nightly とは切り離して (setsid) 別ログで走らせる: 09-22 03:00 の回で、この場で同期を待っていたら nightly の
  # プロセス木ごと黙って消えた (終了行なし。原因は不明、WING 側の kill と思われる)。切り離せば nightly は先に終わり、
  # 同期は自分のログ (r2_sync_<日付>.log) に start / synced を残す。同じ地域の同期は sync_r2.sh の flock で 1 本ずつ。
  # 9>&- : nightly のロック (fd 9) を同期に引き継がせない。引き継ぐと同期が終わるまでロックが外れず、
  # 全件が変わった日 (設定変更で全履歴を学習し直した 2026-09-25 など) は同期が 3 時間を超えて次の nightly が
  # "already running" で飛ばされた (09-25 21:00)。
  RCLONE="${RCLONE:-$HOME/bin/rclone}"; [ -x "$RCLONE" ] || RCLONE=$(command -v rclone || true); export RCLONE
  if [[ "${SPSP_DEPLOY_R2:-1}" != 1 ]]; then
    echo "[$(date +%H:%M:%S)] [5.5/5] ☁️  R2 同期 ... SKIPPED (SPSP_DEPLOY_R2=0)"
  elif [ -n "$RCLONE" ] && [ -x "$RCLONE" ] && "$RCLONE" listremotes 2>/dev/null | grep -q '^r2:'; then
    R2_LOG="$SPSP_LOG_DIR/r2_sync_$(date +%Y%m%d).log"
    if [[ "${SPSP_R2_SYNC_WAIT:-0}" == 1 ]]; then
    echo "[$(date +%H:%M:%S)] [5.5/5] ☁️  R2 同期 (この場で待つ: deploy/sync_r2.sh $BUILD_DIR jp)"
    bash deploy/sync_r2.sh "$BUILD_DIR" jp 9>&- || { echo "  WARN: R2 同期に失敗"; FAILED_STEPS+=("R2 同期 (spsp.games は前回分のまま)"); }
    else
    echo "[$(date +%H:%M:%S)] [5.5/5] ☁️  R2 同期を切り離して開始 (deploy/sync_r2.sh $BUILD_DIR jp → ログ $R2_LOG)"
    setsid nohup bash deploy/sync_r2.sh "$BUILD_DIR" jp >> "$R2_LOG" 2>&1 < /dev/null 9>&- &
    disown 2>/dev/null || true
    fi
    # 前回分の結果をここに 1 行残す (synced が無ければ途中で止まっている)
    # ログが無い (GitHub Actions は同期をこの場で待つので r2_sync_*.log を作らない) と ls が非 0 → set -e で黙って終わる。
    # 09-28〜09-30 の Actions はここで終了し、以降の [5.6/5]・完了表示・rc=2 の報告が出ていなかった
    # (ls の rc=2 がワークフローの「rc=2 は成功扱い」に当たって気付かなかった) → || true
    prev_log=$(ls -t "$SPSP_LOG_DIR"/r2_sync_*.log 2>/dev/null | head -1 || true)
    [ -n "$prev_log" ] && echo "[$(date +%H:%M:%S)] [5.5/5]    前回までの同期: $(grep -c '] synced' "$prev_log") 回完了 / $(grep -c '] sync start' "$prev_log") 回開始 ($prev_log)"
    # [5.6/5] 大会アイコン (2026-09-30〜): smash_database の tournament_images.jsonl のうち R2 に無いものだけを
    # start.gg から元解像度で取って r2:spsp-data/images/tournament/ に置く (中身で重複をまとめ、対応表 index.json。普段は新しい大会の数枚)。失敗してもサイトは壊れない
    if [[ "${SPSP_R2_DIFF_ONLY:-0}" != 1 ]]; then
      echo "[$(date +%H:%M:%S)] [5.6/5] 🖼️  大会アイコンを R2 へ (deploy/sync_tournament_images.py)"
      "$VENV_PY" deploy/sync_tournament_images.py 9>&- || { echo "  WARN: 大会アイコンの同期に失敗"; FAILED_STEPS+=("大会アイコンの R2 同期 (次回また試す)"); }
    fi
  else
    echo "[$(date +%H:%M:%S)] [5.5/5] ☁️  R2 同期 ... SKIPPED (rclone か remote r2 が無い)"
    FAILED_STEPS+=("R2 同期 (rclone / remote r2 が見つからない。spsp.games は前回分のまま)")
  fi
  # [5/5] 旧サイト (gh-pages)。2026-09-28: R2 (spsp.games) の後にした。push が失敗しても nightly は止めない (以前は set -e で落ちて R2 同期まで止まっていた)。
  # 旧サイトを spsp.games への転送ページに切り替えたら $SPSP_GHPAGES_RETIRED_FLAG を置く (以後は gh-pages に一切書かない。転送ページを上書きしないため)
  if [[ -e "$SPSP_GHPAGES_RETIRED_FLAG" ]]; then
    echo "[$(date +%H:%M:%S)] [5/5] 🚀 gh-pages へのデプロイ ... SKIPPED (旧サイトは閉じた: $SPSP_GHPAGES_RETIRED_FLAG)"
  elif [[ "${SPSP_DEPLOY_GHPAGES:-1}" == 1 ]]; then
    echo "[$(date +%H:%M:%S)] [5/5] 🚀 デプロイ (deploy_v4.sh → gh-pages へ rsync --delete + push) ..."
    # gh-pages clone ($SPSP_GHPAGES_CLONE) が無ければ deploy_v4.sh が clone する
    bash deploy/deploy_v4.sh "$BUILD_DIR" "$COMMIT_MSG" || { echo "  WARN: gh-pages へのデプロイに失敗 (旧サイトは前回分のまま。spsp.games は影響なし)"; FAILED_STEPS+=("gh-pages へのデプロイ (旧サイトは前回分のまま)"); }
  else
    echo "[$(date +%H:%M:%S)] [5/5] 🚀 gh-pages へのデプロイ ... SKIPPED (SPSP_DEPLOY_GHPAGES=0)"
  fi
else
  echo "[$(date +%H:%M:%S)] [5/5] 🚀 デプロイ ... SKIPPED (--skip-deploy)"
fi

echo
echo "════════════════════════════════════════════════════════════"
echo " ✅ 完了: $(date +%H:%M:%S)"
echo " build-dir: $BUILD_DIR"
echo "════════════════════════════════════════════════════════════"
# 失敗して前回分で凌いだステップがあれば rc=2 (デプロイは済んでいる)。cron ログの rc= で拾える
[[ ${#FAILED_STEPS[@]} -eq 0 ]] || exit 2

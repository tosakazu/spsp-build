# GitHub Actions でのビルドとデプロイ

2026-09-28 作成。**日本は 1 日 5 回 (0・6・12・18・21 時 JST) の定時実行で spsp.games (R2) に配信する**
(3・9・15 時はユーザー判断で省略。無料枠 2,000 分/月に収めるため)。日本の大会データの取得は smash_database の
download-japan ワークフローが 3 時間ごとに data-Japan に push する (公開リポジトリなので無料)。
**定時の起動は Cloudflare Worker (`deploy/ci/dispatch-worker/`) が workflow_dispatch を送って行う** (GitHub の schedule は
このアカウントでは起動しなかったので使わない)。手動・予備の起動は `deploy/ci/trigger.sh`。
北米も 1 日 5 回 (ニューヨーク時間 0・6・12・18・21 時、夏時間基準) の定時実行だが、北米担当者が data-North_America に
push してデータが変わったときだけビルドする (前回 spsp.games に出したデータのコミットと同じなら数十秒で終わる)。

## 動かし方

GitHub の Actions タブで「build」を選び、Run workflow。

| 入力 | 意味 |
|---|---|
| region | jp = 日本 (deploy/update_and_deploy.sh をそのまま使う)、na = 北米 (deploy/ci/build_na.sh) |
| deploy | none = ビルドだけ (配信しない)、diff = spsp.games (R2) に今あるものと比べて、送ることになるファイルを数えるだけ (読み取りのみ)、r2 = spsp.games (R2) |

CLI からは `gh workflow run build -R tosakazu/spsp-build -f region=jp -f deploy=none`。

## Secrets (Settings → Environments → production。main ブランチからだけ使える)

使う入力のときだけ要る。無くてもビルドだけ (deploy=none) は動く。2026-10-08 からビルドは大会データの取得も参加予定者の取得もしない (smash_database の download-japan だけが取得する) ので、start.gg のトークンとデータへの書き込み鍵は要らない。

| 名前 | 使うとき | 中身 |
|---|---|---|
| STARTGG_OAUTH_CLIENT_SECRET_CF | jp | spsp.games の投票の取り込み鍵の元 (start.gg アプリ 619 の client secret) |
| R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_ENDPOINT | deploy に r2、または前回分を R2 から取るとき | R2 の書き込み鍵 |
| TITLE_SYNC_KEY | jp で deploy に r2 (二つ名の記録の更新、spsp-web tools/titles/sync_owned.mjs) | Worker spsp-web の secret TITLE_SYNC_KEY と同じ値 (preview の鍵とは別)。無ければ更新を飛ばす |

投票は毎回全件取り直す (キャッシュに置かない)。取り込みに失敗したらビルドを止める (投票なしで出すとメインキャラが変わるため)。R2 の鍵が無いときは前回分を
公開 URL (data.spsp.games) から取り、海外タグは前回の overseas.json をそのまま使う。

## 公開リポジトリでの約束

このリポジトリは公開している (ログも誰でも読める)。鍵と、公開していないデータ (キャラ投票の中身) を守るために次を守る。

- 鍵は Environment `production` だけに置く (main ブランチからしか使えない)。リポジトリ全体の Secrets は 0 件。
  main 以外のブランチから build を起動しても鍵は渡らない。
- 鍵を表示しない。prepare.sh はファイルを 600 で書く。Actions はログ中の Secrets を伏せ字にする。
  Secrets から計算して作る値 (投票の取り込み鍵) は `::add-mask::` で伏せ字に登録する。
- キャッシュ (actions/cache) には公開データから計算したもの (学習の状態・snap・大会ファイル) だけを置く。
  公開リポジトリでは PR やほかのブランチのワークフローも main のキャッシュを読めるため。キャラ投票は置かない。
- ビルドの出力を artifact としてアップロードしない。外に出るのは配信 (R2) だけ。
- build に `pull_request` などの外部から起動できるトリガーは付けない (workflow_dispatch だけ)。PR で走るのは
  tests (鍵なし・読むだけ) で、外の人の PR は所有者が承認してから走る。
- セルフホストランナーは付けない (外の人のコードが手元の機械で走りうる)。北米も GitHub の標準ランナー (4 コア・16 GB) で回す。
- 大会データのブランチにあるコードは実行しない (データとして読むだけ)。

## ConoHa との違い

- CPU 時間の制限 (~300 秒/プロセス) が無いので、北米は学習を分割しない (`SPSP_LEARN_CUTS` を空のまま)。
  ConoHa で北米を回すときは `SPSP_LEARN_CUTS="2026-07-15 2026-07-31 ..."` を付ける。
- R2 の同期は切り離さずにその場で待つ (`SPSP_R2_SYNC_WAIT=1`)。Actions はジョブの終了で子プロセスも消えるため。
- R2 への同期は中身 (MD5) を比べて変わったファイルだけ送る (差分デプロイ)。送った件数はログの `Transferred:` 行に出る。
  `deploy=diff` (`SPSP_R2_DIFF_ONLY=1`) なら送らずに件数と変わるファイル名 (data/ と一覧だけ) を出す。
- 学習のチェックポイントはキャッシュから戻す。無ければ (初回・7 日使わず消えた) 最初から年ごとに学習し直す。
- 毎回学習し直す範囲は直近 14 日 (`SPSP_CK_LAG_DAYS=14`、ConoHa は 45 日)。それより古い大会のデータが変わった回は
  自動でさらに古いチェックポイント (月初・年末) から学習し直す。結果は同じで、時間が長い回があるだけ。
- 海外タグは smash_database の overseas_status.json と前回のランキング (1 ファイル) から作るので、前回の選手ページは取らない。
- 実行時間の目安は日本の通常回で 5〜10 分。公開リポジトリの標準ランナーは無料。

## 開催中の大会の速報 (deploy/live-worker、Actions ではない)

Cloudflare Worker `spsp-live` が 1 分ごとに start.gg から開催中の部門を取り、R2 の `live/jp/` に書く (中身は `deploy/live-worker/live.js` の冒頭)。
ビルドとは別の経路で、ここの workflow は関わらない。R2 の同期 (`deploy/sync_r2.sh`) は `<地域>/` の下だけなので `live/` は消さない。
デプロイは `(cd deploy/live-worker && wrangler deploy)`、鍵は `wrangler secret put STARTGG_TOKEN` (速報専用の start.gg トークン)。
`data.spsp.games/live/` は Cloudflare のキャッシュルールで Worker の Cache-Control (30 秒) のまま配る (ほかのパスは 5 分 / 1 日に上書きされる)。
手元で 1 回分: `STARTGG_TOKEN_FILE=… node deploy/live-worker/run_local.mjs <出力ディレクトリ> [国コード]`。試験: `bash tests/run_all.sh deploy`。

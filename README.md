# spsp-build — SPSP ランキングのビルド

スマブラ SP のプレイヤーランキング [spsp.games](https://spsp.games/) (日本・北米) のデータを作るスクリプト。
大会データ ([smash_database](https://github.com/tosakazu/smash_database)) を読み、選手・大会ごとの JSON を作って
spsp.games の配信先 (Cloudflare R2) に送る。画面 (フロント) は [spsp-web](https://github.com/tosakazu/spsp-web)。

*English summary: build scripts for the SPSP rankings on spsp.games. Reads tournament data from smash_database,
computes ratings and writes the JSON files the site (spsp-web) reads. Runs on GitHub Actions.*

## 流れ

```
smash_database の地域ブランチ (data-Japan / data-North_America: 大会ごとの matches.json / standings.json / derived.json)
   │
   ▼ spsp/cli/build_full.py --phase learn   大会を時系列に学習 (前回のチェックポイントから差分で)
   ▼ spsp/cli/build_full.py --phase snap    時点ごとのスナップショット
   ▼ spsp/cli/build_full.py --phase build   latest_tjpr_full.jsonl / players/ / history/ / tournaments/ / players_current.json
   ▼ spsp/cli/build_aux.py                  data/*.json (大会一覧・シリーズ・都道府県など)
   ▼ deploy/sync_r2.sh                      変わったファイルだけ R2 (data.spsp.games) へ
```

日本は `deploy/update_and_deploy.sh`、北米は `deploy/ci/build_na.sh` がこの順に呼ぶ。
GitHub Actions のワークフローは `.github/workflows/build.yml` (説明は `deploy/ci/README.md`)。

## 構成

| 場所 | 中身 |
|---|---|
| `spsp/` | ランキング計算の本体 (パッケージ。構成と決まりごとは `spsp/README.md`) |
| `spsp/settings.py` | 本番の設定。`Settings()` の既定値が本番値で、本番ビルドは環境変数を読まない |
| `deploy/` | ビルドと配信のシェルスクリプト、R2 同期 |
| `deploy/ci/` | Actions 用の準備 (`prepare.sh`)・北米のビルド (`build_na.sh`)・定時起動の Cloudflare Worker (`dispatch-worker/`) |
| `build/data/` | ビルドの入力のうちこのリポジトリで持つもの |
| `tests/` | 速いテスト (`tests/run_all.sh`) とビルド全体の一致検査 (`tests/golden/`) |

## 手元で動かす

```bash
# Python 3.13 (.python-version)。出力を本番と揃えるには requirements.lock.txt の版を入れる
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock.txt

# 大会データ (数 GB) を smash_db_tournament/ に置く (パスは spsp/paths.py)
git clone --depth 1 -b data-Japan https://github.com/tosakazu/smash_database.git smash_db_tournament

# テスト (数分)
bash tests/run_all.sh

# ビルドだけ (配信しない)。出力先は必ず指定する
export OPENBLAS_NUM_THREADS=1 TZ=Asia/Tokyo
B=/tmp/spsp_build; D=$(date +%F)
.venv/bin/python3 -u spsp/cli/build_full.py $D $B --phase learn --state-out $B/_learn_state.pkl
.venv/bin/python3 -u spsp/cli/build_full.py $D $B --phase snap --state-in $B/_learn_state.pkl
.venv/bin/python3 -u spsp/cli/build_full.py $D $B --phase build
.venv/bin/python3 -u spsp/cli/build_aux.py --build-dir $B
```

北米は `SPSP_REGION=North_America` を付け、`data-North_America` を置く (選手 20 万人規模で、メモリは 10 GB ほど要る)。

## 変更するとき

- 変更は PR で。main は保護されていて、所有者の承認が要る (`.github/CODEOWNERS`)。
- ランキングの数字が変わる変更 (設定・計算) は、変わらない変更と分けて出す。変わらないはずの変更は、
  変更の前後でビルドの出力が一致することを確かめる (`tests/golden/`)。
- 鍵・個人情報・サーバのパスを書かない。

## ライセンス

[SPSP Noncommercial License 1.0](LICENSE.md)。営利目的の利用と、他人が使える形でのホスティングには許可が要る。

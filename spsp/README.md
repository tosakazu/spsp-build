# spsp/ — 本番のランキング計算 (パッケージ)

2026-09-08 に `v3/v4/` と `build/*.py` から移した。ここにあるものだけが夜間ビルド (`deploy/update_and_deploy.sh`) で動く。

| ファイル | 役割 |
|---|---|
| `settings.py` | 本番の設定 (`Settings()` の既定値が本番値。環境変数は本番では読まない) |
| `paths.py` | リポジトリ内のパス定義 (唯一の置き場所。副作用なし) |
| `data_loader.py` | 大会データ (`smash_db_tournament/data/startgg`) の読み込みと統合アカウントの適用 |
| `meta.py` `tournament_utils.py` `tjpr_cascade.py` `score.py` `rolling.py` | 大会メタ情報、順位評価 (TJPR) のカスケード、スコア |
| `learner.py` `learn.py` `learn_state.py` `raters/` | 直対評価 (Glicko-2) の学習、差分学習の状態 (チェックポイント) |
| `build.py` `output.py` `overlay.py` | スナップショットからのランキング生成、選手/大会ページの JSON、フロントの上書き |
| `char_*.py` `country_ja.py` `prefecture_resolver.py` `user_merges.py` | キャラ集計、国名、都道府県、アカウント統合表 |
| `cli/build_full.py` | ビルドの入口 (`--phase learn/snap/build`)。`deploy/update_and_deploy.sh` が呼ぶ |
| `cli/build_aux.py` と `cli/build_*.py` | 補助 JSON (大会一覧、シリーズ、都道府県、サブランク、識別子、最終活動、統合表) |
| `cli/build_overseas_json.py` `cli/fetch_char_votes.py` `cli/awaiting_resume.py` | 海外タグ、キャラ投票の取り込み、再開待ち大会の登録簿 |

## 決まりごと

- `spsp/` の中から `spsp/` の外のスクリプトを import しない。
- `sys.path` の細工は `cli/*.py` の先頭 1 行 (リポジトリ直下を足す) だけ。中のモジュールには書かない。
  この 1 行は意図的に残している: golden 比較や別 worktree はコードをコピーして走らせるので、コピー先が自分の `spsp/` を読む必要がある
  (`pip install -e .` を本番 venv に入れると本番ツリーの方を読んでしまう)。`pyproject.toml` は開発用。
- import 時に状態を変えない。設定の適用は `cli/build_full.py` が `apply_settings(Settings(), lock=True)` で行い、
  以後の `apply_settings` は無視される。
- 学習状態 (pickle) が参照するモジュール名は `learn_state.MODULE_ALIASES` / `MODULE_PREFIX_ALIASES` で読み替える。
  クラスを別モジュールへ動かすときは、動かすのと同じコミットで読み替えを足す。

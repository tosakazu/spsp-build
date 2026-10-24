# golden 比較 (ビルド全体の同一性)

「同じ入力から同じ出力が出るか」をリファクタリングの物差しにする。

- 入力と出力の凍結の取り方と、これまでの検証記録: `docs/refactor/01_idempotency_baseline.md`
- 現在の基準: 入力 `~/spsp-golden/20260905/`、期待出力 **`~/spsp-golden/20260930-classperf/build/`** (2026-09-30、下位クラスの行に
  パフォーマンスと全国順位の変動を入れた後。20260930-achpts との差は players/*.json の下位クラスの行のその欄だけ。`--date` は付けない。
  注意: history/ の格子はビルドした時刻で 1 点増減することがある)
- 以前の基準: 入力 `~/spsp-golden/20260905/`、期待出力 **`~/spsp-golden/20260930-achpts/build/`** (2026-09-30、大会の実績に tjpr_pts
  (もとの大会ごとの [日付, 順位評価の素点]) を足した後。20260929-chartable との差は players/*.json のその欄だけ。`--date` は付けない)
- 以前の基準: 期待出力 `~/spsp-golden/20260929-chartable/build/` (2026-09-29、キャラ絵文字表を spsp-web だけに移した後。
  20260929-nofront との差はランキング一覧の各行の main_char_emoji と data/char_emoji.json が無いことだけ。`--date` は付けない (既定の日付で作った))
- 以前の基準: 期待出力 `~/spsp-golden/20260929-nofront/build/` (2026-09-29、フロント (HTML/JS/CSS) を
  spsp-web に移した後。出力は JSON だけ。20260928-charid との差はフロントのファイルと news.json (お知らせも spsp-web に移した) が無いことだけ)
- 以前の基準: 入力 `~/spsp-golden/20260905/`、期待出力 **`~/spsp-golden/20260922-fields/build/`** (2026-09-22、実績の kind/params・
  bracket_type / tags / series_id・generated_at の offset を足した形式。curate は data-Japan の判定規則 (CLASSIFIER v9) 当日分。
  旧基準 `20260906-split` は判定規則の変更 (合宿の身内化など) で選手 1 人と順位がずれるので、もう一致しない)
- 旧基準: 期待出力 `~/spsp-golden/20260906-split/build/` (2026-09-13、配信 JSON の分割
  (docs/refactor/04_player_json_split.md) 後の形式。旧形式の基準 `20260906-legacy-order` は分割の同値性テスト
  `tests/split/test_assemble.mjs --legacy` の旧側としてだけ使う)
  (同一 timestamp の並びを凍結表で本番と同じにしたビルド。本番 2026-09-05 の出力と全選手・全大会でバイト一致)

```sh
bash tests/golden/build_and_compare.sh \
  --inputs ~/spsp-golden/20260905 \
  --expect ~/spsp-golden/20260930-classperf/build \
  --work   ~/spsp-verify/$(date +%Y%m%d)_mytest
```

約 10 分。本番の `site/` やデータには書かない (作業ツリーのコードを `--work` にコピーして走らせる)。
合格 = `compare_trees.py` が OK を出すこと: JSON は内容一致 (両方を orjson で読んで標準 json で書き直した文字列を比べる。
無視されるのは空白と浮動小数の表記だけで、キー順・int/float の区別・不正 JSON は検出される)、それ以外はバイト一致、
`meta.json` / `data/tournaments.json` / `data/series.json` は生成時刻を除いて一致。

**バイト一致ではなく内容一致にしている理由 (2026-09-07)**: 書き出しを orjson にした (区切りが `,` `:` でスペース無し、
1.4 GB の書き出しが 9 秒 → 2 秒、ファイル 10% 減)。本番 2026-09-05 の golden とはバイトが違うが、ブラウザは JSON.parse した
値しか見ないので、この方式で無視する差はサイトから観測できない。「同じ値なのにバイトが違う」変化はこの比較では
見えない点に注意 (orjson 書式で golden を取り直せばバイト一致に戻せる)。

期待出力の `site/` 由来の静的ファイル (HTML / JS) を意図して変えたときは、変えたファイルだけ期待側にコピーして更新する
(2026-09-07: `p/index.html`, `seed/index.html`, `t/index.html`)。JSON 側は触らない。

新しい golden を取るときは、その日の nightly の入力 (`smash_db_tournament/data/startgg` 等) と
BUILD_DIR を上の layout (`build_and_compare.sh` 冒頭のコメント) でコピーし、コードの commit を
`refactor_commit.txt` に残す。日付付きで複数持ってよい。

## フル学習 vs 差分学習

```sh
bash tests/golden/incremental_vs_full.sh --inputs ~/spsp-golden/20260905 --work ~/spsp-verify/$(date +%Y%m%d)_inc --date 2026-09-05
```

最初から全部学習したビルドと、年ごとに `--until` で保存・再開して最後を `--state-in` で学習したビルドを
`compare_trees.py` で比較する (上と同じ判定)。続けて、チェックポイント以前の大会の standings.json を
書き換えたデータで再開すると終了コード 3 で止まることも確かめる。約 20 分。
学習の仕組みを触ったら必ず通すこと。

"""V4 ランキングビルド用パッケージ.

build_tjpr_ranking.py (v2 prod) から段階的に依存を切り出し、
最終的に build_v4_full.py が build_tjpr_ranking に依存しない形を目指す.

各モジュール:
  common  — constants / regexes / 共通ユーティリティ
  meta    — tournament メタ解析 (シリーズ名 / クラス / seed)
  learn   — BT / cascade 学習関数
  score   — スコア・ランク・achievements 等の derive ロジック
  output  — JSON / JSONL writer (= record assemble + ファイル出力)
"""

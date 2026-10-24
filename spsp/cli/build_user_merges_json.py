"""smash_database の manual/user_merges.json (手動管理) を <build-dir>/data/user_merges.json に書き出す (--build-dir 既定 site/).

シードツール (site/seed) が start.gg から取得した entrant の user_id を
正規 uid に変換するために使う (= 旧アカウントでエントリーされても
ランキング参照・再対戦履歴が正しく引ける)。

連鎖 ({A: B, B: C}) は解決済みの形 ({A: C, B: C}) で出力する。
出力形式: {"<old_uid>": <new_uid>, ...} (JSON のキーは文字列なので old 側のみ str)。
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # リポジトリ直下 (spsp パッケージ)
from spsp.paths import SITE_DIR  # noqa: E402
from spsp.fileio import write_json_atomic  # noqa: E402

from spsp.data_loader import _resolve_merge_chains
from spsp.derived import load_user_merges  # noqa: E402


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--build-dir', default=str(SITE_DIR), help='data/user_merges.json を書く場所 (既定 site/)')
    args = ap.parse_args(argv)
    resolved = _resolve_merge_chains(load_user_merges())
    out_path = os.path.join(args.build_dir, 'data', 'user_merges.json')
    write_json_atomic(out_path, {str(k): v for k, v in sorted(resolved.items())},
                      ensure_ascii=False, indent=0, sort_keys=True, trailing_newline=True)
    print(f"[build_user_merges_json] wrote {os.path.normpath(out_path)}: "
          f"{len(resolved)} mapping(s)")


if __name__ == '__main__':
    main()

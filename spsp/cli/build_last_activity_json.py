# -*- coding: utf-8 -*-
"""ランキング収録者の uid -> 最終大会参加日 を生成 (優先枠作成ページの直近参加フィルタ用).

※ 2026-09 からは build_v4_full.py が書き出し時に BUILD_DIR/data/last_activity.json を作る (同じ内容)。
   このスクリプトは players/*.json から作り直す手動用。夜間の [4/5] では実行しない。

出力: { "<user_id>": "YYYY-MM-DD", ... }  (tournaments が 1 件以上ある選手のみ)
入力: players/<uid>.json の tournaments[].date の最大値。

使い方: python build/build_last_activity_json.py [--build-dir DIR] [--players-dir DIR] [--out PATH]
  --players-dir 省略時は <build-dir>/players、--out 省略時は <build-dir>/data/last_activity.json。
  --build-dir 省略時は site/ (site/players は前夜デプロイの gh-pages clone への symlink)。
  nightly では [4/5] で --build-dir $BUILD_DIR を渡して当夜分を反映する。
"""
import argparse
import json
import os
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # リポジトリ直下 (spsp パッケージ)
from spsp.paths import SITE_DIR  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--build-dir', default=str(SITE_DIR), help='players/ を読み data/last_activity.json を書く場所 (既定 site/)')
    ap.add_argument('--players-dir', default=None, help='既定 <build-dir>/players')
    ap.add_argument('--out', default=None, help='既定 <build-dir>/data/last_activity.json')
    args = ap.parse_args(argv)
    if args.players_dir is None:
        args.players_dir = os.path.join(args.build_dir, 'players')
    if args.out is None:
        args.out = os.path.join(args.build_dir, 'data', 'last_activity.json')

    out = {}
    n_files = 0
    with os.scandir(args.players_dir) as it:
        for e in it:
            if not e.name.endswith('.json'):
                continue
            n_files += 1
            try:
                with open(e.path, encoding='utf-8') as f:
                    d = json.load(f)
                dates = [t['date'] for t in (d.get('tournaments') or []) if t.get('date')]
                if dates:
                    out[e.name[:-5]] = max(dates)
            except Exception:
                continue
    if not out:
        print(f"ERROR: {args.players_dir} から 1 件も読めない — 生成中止", flush=True)
        return 1

    tmp = args.out + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(out, f, separators=(',', ':'), sort_keys=True)  # キー順を実行間で固定
    os.replace(tmp, args.out)
    print(f"wrote {args.out}: {len(out)}/{n_files} 選手")
    return 0


if __name__ == '__main__':
    sys.exit(main())

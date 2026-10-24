"""開催予定の大会一覧 (site/data/upcoming.json) を書き出す.

定義元はデータ側: smash_database の fetch_upcoming.py が data/startgg/<地域>/upcoming.json を作る
(日本は deploy の [2/5]、北米は担当者の run_region.sh)。ここはそれを検証して <build-dir>/data/upcoming.json に
写すだけ (2026-09-16〜。以前は deploy が日本のものを site/data に cp していた)。
使い方: python spsp/cli/build_upcoming_json.py [--build-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from spsp.paths import REGION_DB, SITE_DIR, REGION  # noqa: E402

SRC = REGION_DB / 'upcoming.json'


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--build-dir', default=str(SITE_DIR), help='data/upcoming.json を書く場所 (既定 site/)')
    args = ap.parse_args(argv)
    try:
        d = json.loads(SRC.read_bytes())
    except FileNotFoundError:
        print(f"ERROR: {SRC} が無い — smash_database で fetch_upcoming.py --region {REGION} を実行する", file=sys.stderr)
        return 1
    if not isinstance(d, dict) or 'tournaments' not in d or 'generated_at' not in d:
        print(f"ERROR: {SRC} の形が違う (tournaments / generated_at が無い)", file=sys.stderr)
        return 1
    out_file = Path(args.build_dir) / 'data' / 'upcoming.json'
    out_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_file.with_suffix('.json.tmp')
    tmp.write_bytes(SRC.read_bytes())   # 内容はそのまま (データ側の生成物を改変しない)
    tmp.replace(out_file)
    print(f"upcoming.json: {d.get('count', len(d['tournaments']))} tournaments (generated {d['generated_at']}) → {out_file}")
    return 0


if __name__ == '__main__':
    sys.exit(main())

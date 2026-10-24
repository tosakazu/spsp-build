"""地理単位のカタログ (site/data/geo.json) を書き出す.

定義元は smash_database の scripts/<地域>/geo.py で、derive.py が data/startgg/<地域>/geo.json に書く。
ここはそれを検証して <build-dir>/data/geo.json に写すだけ。フロントは都道府県の一覧・並び順・
グループ (地方・シード用のまとめ) をこのファイルから読む (自前の一覧を持たない)。
使い方: python spsp/cli/build_geo_json.py [--build-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from spsp.paths import GEO_JSON, SITE_DIR, REGION  # noqa: E402

REQUIRED = ("region", "unit", "provisional", "names", "units", "groups", "seed_groups")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--build-dir', default=str(SITE_DIR), help='data/geo.json を書く場所 (既定 site/)')
    args = ap.parse_args(argv)
    try:
        cat = json.loads(GEO_JSON.read_bytes())
    except FileNotFoundError:
        print(f"ERROR: {GEO_JSON} が無い — smash_database で `python3 scripts/common/derive.py --region {REGION}` を実行する", file=sys.stderr)
        return 1
    missing = [k for k in REQUIRED if k not in cat]
    if missing or not cat["units"]:
        print(f"ERROR: {GEO_JSON} が契約を満たさない (missing={missing}, units={len(cat.get('units') or [])})", file=sys.stderr)
        return 1
    out_file = Path(args.build_dir) / 'data' / 'geo.json'
    out_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_file.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(cat, ensure_ascii=False, indent=1) + "\n", encoding='utf-8')
    tmp.replace(out_file)
    print(f"geo.json: {cat['region']} {cat['unit']} {len(cat['units'])} units, {len(cat['groups'])} groups, "
          f"{len(cat['seed_groups'])} seed groups{' (provisional)' if cat['provisional'] else ''} → {out_file}")
    return 0


if __name__ == '__main__':
    sys.exit(main())

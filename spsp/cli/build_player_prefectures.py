"""選手の都道府県 (site/data/player_prefectures.json) を書き出す.

判定 (居住地の自由記述 → 地理単位) は smash_database 側 (scripts/<地域>/classify.py) が行い、
data/startgg/<地域>/users_derived.jsonl の geo / geo_reason に入っている。ここはそれを {"<user_id>": "<geo>"} に写すだけ (2026-09-08〜)。
出力: 特定できた選手のみ。値は geo.json (site/data/geo.json) の unit id (日本: 都道府県の漢字名、北米: 州コード)。
ファイル名は互換のため player_prefectures.json のまま。
使い方: python spsp/cli/build_player_prefectures.py [--build-dir DIR]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # リポジトリ直下 (spsp パッケージ)
from spsp.paths import SITE_DIR  # noqa: E402
from spsp.fileio import write_json_atomic  # noqa: E402
from spsp.derived import iter_users_derived  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--build-dir', default=str(SITE_DIR), help='data/player_prefectures.json を書く場所 (既定 site/)')
    args = ap.parse_args(argv)
    out_file = Path(args.build_dir) / 'data' / 'player_prefectures.json'
    out = {}
    n = 0
    reasons = {}
    for r in iter_users_derived():
        n += 1
        why = r.get('geo_reason')
        reasons[why] = reasons.get(why, 0) + 1
        if r.get('geo'):
            out[str(r['user_id'])] = r['geo']
    out_file.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(out_file, out, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    rate = len(out) / n * 100 if n else 0
    print(f"JP登録 city記入 {n} / 都道府県特定 {len(out)} ({rate:.1f}%)")
    print("  理由内訳: " + ", ".join(f"{k}={v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])))
    print(f"  wrote: {out_file} ({out_file.stat().st_size} bytes, {len(out)} 選手)")


if __name__ == '__main__':
    main()

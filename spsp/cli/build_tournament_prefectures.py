"""大会の都道府県 (site/data/tournament_prefectures.json) を書き出す.

判定 (start.gg の venue / city → 地理単位) は smash_database 側 (scripts/<地域>/classify.py) が行い、
各イベントの derived.json の place.geo に入っている。ここはそれを {"<event_id>": "<geo>"} に写すだけ (2026-09-08〜)。
値は geo.json の unit id (日本: 都道府県の漢字名)。ファイル名は互換のため tournament_prefectures.json のまま。
使い方: python spsp/cli/build_tournament_prefectures.py [--build-dir DIR]
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # リポジトリ直下 (spsp パッケージ)
from spsp.paths import EVENTS_ROOT, SITE_DIR  # noqa: E402
from spsp.fileio import write_json_atomic  # noqa: E402
from spsp import derived  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--build-dir', default=str(SITE_DIR), help='data/tournament_prefectures.json を書く場所 (既定 site/)')
    args = ap.parse_args(argv)
    out_file = Path(args.build_dir) / 'data' / 'tournament_prefectures.json'
    out = {}
    scanned = 0
    for dirpath, _dirs, files in os.walk(EVENTS_ROOT):
        if 'attr.json' not in files:
            continue
        scanned += 1
        cur = derived.load(dirpath)
        eid = cur.get('event_id')
        if eid is None:
            continue
        pref = (cur.get('place') or {}).get('geo')
        if pref:
            out[str(eid)] = pref
    out_file.parent.mkdir(parents=True, exist_ok=True)
    if scanned == 0:   # 入力ディレクトリが無い/空 (mount 外れ・パス違い) を「成功して空 JSON」にしない
        raise SystemExit(f"ERROR: {EVENTS_ROOT} に attr.json が 1 件も無い — 出力しない")
    write_json_atomic(out_file, out, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    print(f"attr.json {scanned} 件走査 / 都道府県特定 {len(out)} 大会")
    print(f"  wrote: {out_file} ({out_file.stat().st_size} bytes)")


if __name__ == '__main__':
    main()

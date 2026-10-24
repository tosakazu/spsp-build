#!/usr/bin/env python3
"""build_aux.py — 補助 JSON (BUILD_DIR/data/*.json) を 1 プロセスでまとめて作る。

以前は update_and_deploy.sh が build_*.py を 7 回起動していた (geo.json は 2026-09-15 に追加) (interpreter 起動 7 回、latest_tjpr_full.jsonl の
読み直し 5 回、events の走査 3 回)。各スクリプトは main(argv) を持つのでここから順に呼ぶ。出力は各スクリプトのまま。

  build_aux.py --build-dir BUILD_DIR [--only name,name]

順序依存: player_subranks と split_views は player_prefectures と character_index (ビルド生成) を読む。
必須 (tournaments_index, series) が失敗したら終了コード 1、任意のものが失敗したら続行して終了コード 2
(update_and_deploy.sh はその JSON を前夜分のまま配信し、失敗ステップの一覧に載せる)。
last_activity.json はビルド (build_v4_full.py) が書くのでここには無い。
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # リポジトリ直下 (spsp パッケージ)

HERE = Path(__file__).resolve().parent

# (名前, 必須か)。順序を変えないこと。
STEPS = [
    ('build_tournaments_index', True),
    ('build_series_json', True),
    ('build_geo_json', False),
    ('build_upcoming_json', False),
    ('build_player_prefectures', False),
    ('build_tournament_prefectures', False),
    ('build_player_subranks', False),
    ('build_split_views', False),     # data/views/ (ページ用に分けた一覧。player_prefectures・character_index の後)
    ('build_discriminators_json', False),
    ('build_user_merges_json', False),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--build-dir', required=True)
    ap.add_argument('--only', default='', help='カンマ区切りでこの名前だけ (例: build_series_json)')
    args = ap.parse_args(argv)
    only = {s for s in args.only.split(',') if s}
    import importlib
    failed_optional = []
    for name, required in STEPS:
        if only and name not in only:
            continue
        t0 = time.perf_counter()
        print(f"[aux] {name} ...", flush=True)
        try:
            mod = importlib.import_module('spsp.cli.' + name)
            rc = mod.main(['--build-dir', args.build_dir])
            if rc not in (None, 0):
                raise SystemExit(rc)
        except SystemExit as e:
            if e.code in (None, 0):
                pass
            elif required:
                print(f"[aux] ERROR: {name} failed (rc={e.code}) — 必須なので中止", flush=True)
                return 1
            else:
                print(f"[aux] WARN: {name} failed (rc={e.code}) — 続行 (この JSON は前夜分)", flush=True)
                failed_optional.append(name)
        except Exception:
            traceback.print_exc()
            if required:
                print(f"[aux] ERROR: {name} failed — 必須なので中止", flush=True)
                return 1
            print(f"[aux] WARN: {name} failed — 続行 (この JSON は前夜分)", flush=True)
            failed_optional.append(name)
        print(f"[aux] {name} done ({time.perf_counter() - t0:.1f}s)", flush=True)
    if failed_optional:
        print(f"[aux] failed (optional): {' '.join(failed_optional)}", flush=True)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())

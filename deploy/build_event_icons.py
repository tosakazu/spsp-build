#!/usr/bin/env python3
"""大会 (event_id) → 大会アイコンの表を作る (2026-10-20〜、カードの裏面に貼る大会シールの絵)。

smash_database の tournaments.jsonl (大会 → その中のイベント) と、R2 の大会アイコンの対応表
(https://data.spsp.games/images/tournament/index.json、deploy/sync_tournament_images.py が [5.6/5] で書く) から、
<出力先>/data/event_icons.json を書く。R2 への同期 ([5.5/5]) でほかのデータと一緒に配信する (フロントは SPSP.data から読む)。

形: {"f": [アイコンのファイル名 (images/tournament/h/ の中)…], "e": {"<event_id>": f の番号}}。アイコンのある大会だけ。
下位クラスのシールは親の event_id で引く (フロントの js/card_stickers.js)。

アイコンの同期 ([5.6/5]) はこのあとなので、新しい大会のアイコンは次の回の表に入る。対応表が読めないときは、今配信している表
(https://data.spsp.games/jp/data/event_icons.json) を書き戻す (R2 の同期は出力に無いファイルを消すので、書かないと表が消える)。

使い方: .venv/bin/python deploy/build_event_icons.py <出力先 (ビルドの出力ディレクトリ)> [--tournaments <tournaments.jsonl>]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOURNAMENTS = ROOT / 'smash_db_tournament/data/startgg/Japan/tournaments.jsonl'
INDEX_URL = 'https://data.spsp.games/images/tournament/index.json'
CURRENT_URL = 'https://data.spsp.games/jp/data/event_icons.json'


def build(tournaments: Path, index: dict) -> dict:
    files: list[str] = []
    pos: dict[str, int] = {}
    ev: dict[str, int] = {}
    with open(tournaments, encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            t = json.loads(line)
            ent = index.get(str(t.get('tournament_id')))
            if not ent or not ent.get('file'):
                continue
            name = str(ent['file']).removeprefix('h/')
            if name not in pos:
                pos[name] = len(files)
                files.append(name)
            for e in t.get('events') or []:
                if e.get('event_id') is not None:
                    ev[str(e['event_id'])] = pos[name]
    return {'f': files, 'e': ev}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('out_dir')
    ap.add_argument('--tournaments', default=str(DEFAULT_TOURNAMENTS))
    a = ap.parse_args()
    dest = Path(a.out_dir) / 'data' / 'event_icons.json'
    try:
        r = requests.get(INDEX_URL, timeout=30)
        r.raise_for_status()
        out = build(Path(a.tournaments), r.json())
    except Exception as e:
        # 作れなければ今配信している表を書き戻す (それも読めなければ書かない = 配信側から消える。フロントはシールの絵を出さないだけ)
        print(f'  WARN: 大会アイコンの表を作れない ({e})。今配信している表を書き戻す', file=sys.stderr)
        try:
            r = requests.get(CURRENT_URL, timeout=30)
            r.raise_for_status()
            out = r.json()
            assert isinstance(out, dict) and 'f' in out and 'e' in out
        except Exception as e2:
            print(f'  WARN: 今の表も読めない ({e2})', file=sys.stderr)
            return 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(out, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(out, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    tmp.replace(dest)
    print(f'  大会 {len(out["e"])} / アイコン {len(out["f"])} → {dest}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

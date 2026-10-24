#!/usr/bin/env python3
"""tests/split/fixtures を作る (分割前後のビルド出力から数人・数大会だけ切り出す)。

    python3 tests/split/make_fixture.py --legacy <旧形式の deployed> --new <新形式の deployed> [--out tests/split/fixtures]

新側は旧側と同じスナップショット点でビルドしたもの (build_full.py --snapshot-grid legacy) を渡すと
tests/split/test_assemble.mjs が厳密比較になる。選ぶ選手: 大きすぎない (旧 JSON 60 KB 未満)、
順位評価スコアが 0 でない (旧 jsonl の同点順の不定に当たらない)、class bracket 出場のある人を 1 人以上。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

N_PLAYERS = 8
N_TOURS = 3
MAX_LEGACY_BYTES = 60_000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--legacy', required=True)
    ap.add_argument('--new', required=True)
    ap.add_argument('--out', default=str(Path(__file__).resolve().parent / 'fixtures'))
    a = ap.parse_args()
    L, N, O = Path(a.legacy), Path(a.new), Path(a.out)

    # 候補選手: 上限サイズ以下で大きい順 (大会・試合が多いほうが分岐を踏む)。class bracket 出場者を 2 人は入れる
    cands = []
    for f in os.listdir(L / 'players'):
        p = L / 'players' / f
        sz = p.stat().st_size
        if sz > MAX_LEGACY_BYTES or sz < 8_000 or not (N / 'players' / f).exists():
            continue
        cands.append((sz, f))
    cands.sort(reverse=True)
    picked, with_class = [], 0
    for sz, f in cands:
        d = json.loads((L / 'players' / f).read_text())
        if not (d.get('scores') or {}).get('tjpr_score'):
            continue
        has_class = any(t.get('class_letter') for t in d.get('tournaments') or [])
        if has_class and with_class < 2:
            picked.append(f); with_class += 1
        elif len(picked) - with_class < N_PLAYERS - 2:
            picked.append(f)
        if len(picked) >= N_PLAYERS:
            break
    uids = [json.loads((L / 'players' / f).read_text())['user_id'] for f in picked]

    # 大会: 選んだ選手が出ている小さめの大会
    tours = []
    for f in picked:
        d = json.loads((L / 'players' / f).read_text())
        for t in d.get('tournaments') or []:
            eid = t.get('event_id')
            if eid is None:
                continue
            tp = L / 'tournaments' / f'{eid}.json'
            if tp.exists() and tp.stat().st_size < 120_000 and f'{eid}.json' not in tours:
                tours.append(f'{eid}.json')
            if len(tours) >= N_TOURS:
                break
        if len(tours) >= N_TOURS:
            break

    for side, src in (('legacy', L), ('new', N)):
        dst = O / side
        if dst.exists():
            shutil.rmtree(dst)
        (dst / 'players').mkdir(parents=True)
        (dst / 'tournaments').mkdir()
        for f in picked:
            shutil.copy(src / 'players' / f, dst / 'players' / f)
        for f in tours:
            shutil.copy(src / 'tournaments' / f, dst / 'tournaments' / f)
        meta = json.loads((src / 'meta.json').read_text())
        meta.pop('generated_at', None); meta.pop('computation_time_s', None)
        (dst / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False))
        # jsonl は選んだ選手の行だけ
        with open(src / 'latest_tjpr_full.jsonl') as fi, open(dst / 'latest_tjpr_full.jsonl', 'w') as fo:
            for line in fi:
                if not line.strip():
                    continue
                if json.loads(line)['user_id'] in uids:
                    fo.write(line)
        if side == 'new':
            (dst / 'history').mkdir()
            for f in picked:
                shutil.copy(src / 'history' / f, dst / 'history' / f)
            cur = json.loads((src / 'players_current.json').read_text())
            cur['players'] = {str(u): cur['players'][str(u)] for u in uids}
            (dst / 'players_current.json').write_text(json.dumps(cur, ensure_ascii=False))
    print(f'players: {uids}\ntournaments: {tours}\n→ {O}')


if __name__ == '__main__':
    main()

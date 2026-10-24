#!/usr/bin/env python3
"""キャラ投票の 2 つの取得元 (旧サイト = GAS のシート、spsp.games = Cloudflare Worker の D1) を 1 つに畳む.

    .venv/bin/python3 spsp/cli/merge_char_votes.py [--out build/data/char_votes.json] <取得ファイル>...

spsp.games を公開するまでは旧サイト (tosakazu.github.io) が本番なので、投票は両方の受け口に入る.
fetch_char_votes.py は取得元ごとに別ファイル (char_votes_gas.json / char_votes_cf.json) へ落とし、
ビルドが読む build/data/char_votes.json はこのスクリプトが作る (採用規則は spsp/char_vote.py、変更なし).

畳み方: 同じ行 = (ts, user_id, char_id)。両方にあれば**先に渡したファイルの status** を採る
(GAS を先に渡す = 人がシートで書き換えた status を優先。D1 の写しは投入時点のもの).
取得元のファイルが無ければ飛ばす (取得に失敗した側は前回分がそのまま残っている前提).
入力が 1 つも無ければ非 0 で終了し、出力は書き換えない.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / 'build' / 'data' / 'char_votes.json'


def vote_key(v: dict) -> tuple:
    return (v['ts'], v['user_id'], v['char_id'])


def merge_sources(sources: list[dict]) -> list[dict]:
    """先に渡した取得元を優先して和集合を取る (ts, user_id 昇順)."""
    by_key: dict[tuple, dict] = {}
    for src in sources:
        for v in src.get('votes') or []:
            by_key.setdefault(vote_key(v), v)
    out = list(by_key.values())
    out.sort(key=lambda r: (r['ts'], r['user_id']))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description='キャラ投票の取得元を 1 つに畳む')
    ap.add_argument('inputs', nargs='+', help='fetch_char_votes.py の出力 (優先したい順)')
    ap.add_argument('--out', default=str(DEFAULT_OUT))
    args = ap.parse_args()

    sources, used = [], []
    for p in args.inputs:
        path = Path(p)
        if not path.exists():
            print(f'  merge_char_votes: {path} が無い (飛ばす)')
            continue
        sources.append(json.loads(path.read_text(encoding='utf-8')))
        used.append(path)
    if not sources:
        print('ERROR: 取得元のファイルが 1 つも無い', file=sys.stderr)
        return 1
    votes = merge_sources(sources)
    payload = {
        'note': ('キャラ投票の統合 (旧サイト = GAS シート + spsp.games = Worker/D1)。'
                 'spsp/cli/merge_char_votes.py で再生成。採用規則は spsp/char_vote.py'),
        'fetched_at': max(str(s.get('fetched_at') or '') for s in sources),
        'merged_at': dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec='seconds'),
        'sources': [{'file': str(p), 'fetched_at': s.get('fetched_at'), 'mode': s.get('mode'),
                     'rows': len(s.get('votes') or [])} for p, s in zip(used, sources)],
        'votes': votes,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    os.replace(tmp, out)
    parts = ' + '.join(f'{p.name} {len(s.get("votes") or [])}' for p, s in zip(used, sources))
    print(f'char_votes: 統合 {parts} → {len(votes)} 行 → {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

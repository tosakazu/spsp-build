#!/usr/bin/env python3
"""キャラ投票まわりの失敗ログ (errors シート) を読む。

成功は char_votes に残るが、失敗はどこにも残らず「認証エラーになる」と言われても
原因が追えなかったため用意した (2026-08-16)。

    python3 build/fetch_error_log.py [--limit 100] [--uid 3282167]

鍵は fetch_char_votes.py と同じ導出鍵 (client secret から HMAC)。秘密は表示しない。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # リポジトリ直下 (spsp パッケージ)

from spsp.cli import fetch_char_votes as F  # noqa: E402


def _call(endpoint: str, key: str, limit: int) -> dict:
    """export_errors を叩く。送り方は fetch_char_votes.call_export と同じ。"""
    import urllib.request
    body = json.dumps({'action': 'export_errors', 'key': key, 'limit': limit}).encode('utf-8')
    req = urllib.request.Request(endpoint, data=body, headers={'Content-Type': 'text/plain'})
    with urllib.request.urlopen(req, timeout=F.TIMEOUT_S) as res:
        return json.loads(res.read().decode('utf-8'))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=100, help='末尾から何件読むか (既定 100)')
    ap.add_argument('--uid', help='この user_id の行だけ表示する')
    ap.add_argument('--json', action='store_true', help='生の JSON を出す')
    args = ap.parse_args()

    res = _call(F.read_endpoint(), F.read_key(F.DEFAULT_SECRET_FILE), args.limit)
    if not res.get('ok'):
        print('取得に失敗しました: ' + json.dumps(res.get('error'), ensure_ascii=False),
              file=sys.stderr)
        return 1

    rows = res.get('errors') or []
    if args.uid:
        rows = [r for r in rows if str(r.get('userId')) == str(args.uid)]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0

    print(f"失敗ログ {len(rows)} 件 (シート全体 {res.get('total')} 件)")
    if not rows:
        print('  該当なし')
        return 0
    print(f"{'時刻':<20} {'元':<7} {'action':<14} {'code':<18} {'uid':<10} note")
    for r in rows:
        print(f"{r['ts'][:19]:<20} {r['source']:<7} {r['action']:<14} "
              f"{r['code']:<18} {r['userId'] or '-':<10} {r['note']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

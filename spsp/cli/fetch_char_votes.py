#!/usr/bin/env python3
"""キャラ投票をビルド用に取り込む.

投票の受け口は spsp.games の Worker (/api、保存先は Cloudflare D1。コードは spsp-web の worker/)。
ビルドは Worker の export (action="export_votes") で全行を取得し build/data/char_votes.json に落とす
(2026-09-29 に GAS とスプレッドシートは止めた。GAS にしか無かった行は D1 に移してある)。

    OPENBLAS_NUM_THREADS=1 .venv/bin/python3 spsp/cli/fetch_char_votes.py --out build/data/char_votes_cf.json

設定:
  エンドポイント  既定 https://spsp.games/api (--endpoint で上書き)。
  鍵              保存しない. start.gg アプリ 619 の client secret
                  (~/spsp-secrets/startgg_oauth_client_secret_cf、環境変数 STARTGG_CLIENT_SECRET でも可) からラベル付き HMAC で導出する.
                  Worker (spsp-web worker/src/api/export.ts) が同じ値を導出して照合する.
                  どちらの値も表示しない (エラーメッセージにも載せない).

既定は差分取得: 手元のファイルにある最新のタイムスタンプを `since` として渡し,
それ以降の行だけ受け取って既存分にマージする. 境界は「以降」(>=) なので, 同じ秒に
複数人が投票しても取りこぼさない (重複は (ts, user_id, char_id) で畳む).
同じ行が差分に含まれていたら**新しく取ったほうで上書き**するので, 人間が status を
書き換えた分も, その行が `since` 以降にあれば拾える.

全件取り直すのは次のとき:
  - `--full` を付けたとき
  - 手元にファイルが無い (= 初回)
  - **シートの行数が前回より減っていた** (= 行が消された. 差分では削除を追えない)

失敗したら**書き換えずに** 非 0 で終了する (= 前回取得分がそのまま残る).
取得できたのに 0 件だった場合と, 取得に失敗した場合を取り違えないため.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # リポジトリ直下 (spsp パッケージ)

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / 'build' / 'data' / 'char_votes.json'
DEFAULT_ENDPOINT = 'https://spsp.games/api'   # spsp.games の Worker (以前は site/js/post_config.js の GAS_ENDPOINT を読んでいた)
DEFAULT_SECRET_FILE = Path.home() / 'spsp-secrets' / 'startgg_oauth_client_secret_cf'
TIMEOUT_S = 60

# 用途を分けるためのラベル. Worker (spsp-web worker/src/api/export.ts) と同じ値にすること
# (tests/post/page.test.cjs が一致を検査する). 変えると両方が揃うまで auth_failed.
EXPORT_KEY_LABEL = 'spsp:export_votes:v1'

# 採用しうる status (= 実際の採否は v4/char_vote.py が決める).
# 'debug' 行は GAS 側でも落としているが、保存の時点でも入れない.
KEEP_STATUS = ('pending', 'approved', 'rejected')


def read_endpoint() -> str:
    """既定のエンドポイント (spsp.games の Worker)。"""
    return DEFAULT_ENDPOINT


def derive_key(secret: str) -> str:
    """エクスポート鍵を導出する (Worker の export と同じ計算。以前の GAS gas/export.gs exportKey_ とも同じ).

        base64url( HMAC-SHA256(key = client secret, msg = ラベル) )  末尾 '=' は落とす
    """
    sig = hmac.new(secret.encode('utf-8'), EXPORT_KEY_LABEL.encode('utf-8'),
                   hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode('ascii').rstrip('=')


def read_key(secret_file: Path) -> str:
    """エクスポート鍵。元になる client secret は環境変数優先、無ければファイル。

    どちらの値も表示しない (エラーメッセージにも載せない).
    """
    secret = os.environ.get('STARTGG_CLIENT_SECRET') or ''
    if not secret.strip() and secret_file.exists():
        secret = secret_file.read_text(encoding='utf-8')
    secret = secret.strip()
    if not secret:
        raise SystemExit(
            'ERROR: start.gg client secret が見つかりません '
            f'(環境変数 STARTGG_CLIENT_SECRET か {secret_file})。docs/DEPLOY.md 参照')
    key = derive_key(secret)
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        # 導出した鍵は Secrets に無いので Actions は自動では伏せない。万一ログに出ても伏せ字になるよう登録する
        print(f'::add-mask::{key}', flush=True)
    return key


def call_export(endpoint: str, key: str, since: str = '') -> dict:
    """GAS を叩いて JSON を返す.

    since (ISO 8601) を渡すとその時刻以降の行だけ返る (差分取得).

    GAS の /exec は 302 で googleusercontent へ飛ばし、本体はそちらが返す.
    urllib のリダイレクト処理は POST を GET に変えるので、そのままで正しく届く
    (curl で -X POST を付けると POST のまま飛んで 404 になるのと同じ話).
    """
    payload = {'action': 'export_votes', 'key': key}
    if since:
        payload['since'] = since
    body = json.dumps(payload).encode('utf-8')
    # GAS 側は e.postData.contents を JSON.parse するだけ。
    # text/plain にするのはクライアントと同じ理由 (CORS preflight 回避) で、
    # 送る形を 1 つに揃えておく。
    req = urllib.request.Request(
        endpoint, data=body,
        # User-Agent: Cloudflare (spsp.games の Worker) は既定の Python-urllib を 403 で弾くので名乗る
        headers={'Content-Type': 'text/plain', 'User-Agent': 'spsp-fetch-char-votes/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as res:
            raw = res.read().decode('utf-8')
    except (urllib.error.URLError, OSError) as e:
        raise SystemExit(f'ERROR: エクスポート API に到達できません: {e}')
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # HTML (ログイン画面 / 404) が返ってきたときに気づけるよう頭だけ出す。
        raise SystemExit(f'ERROR: JSON が返りませんでした: {raw[:200]!r}')
    if not data.get('ok'):
        err = data.get('error') or {}
        raise SystemExit(
            f"ERROR: エクスポート API がエラーを返しました: "
            f"{err.get('code')} {err.get('message')}")
    return data


def normalize(data: dict) -> list[dict]:
    """API のレスポンスを保存形式にする (uid / char_id は int, ts 昇順)."""
    out = []
    for v in data.get('votes') or []:
        status = str(v.get('status') or '')
        if status not in KEEP_STATUS:
            continue
        try:
            uid = int(str(v.get('userId')).strip())
            cid = int(str(v.get('charId')).strip())
        except (TypeError, ValueError):
            continue
        out.append({
            'ts': str(v.get('ts') or ''),
            'user_id': uid,
            'char_id': cid,
            'char_name': str(v.get('charName') or ''),
            'status': status,
        })
    out.sort(key=lambda r: (r['ts'], r['user_id']))
    return out


def vote_key(v: dict) -> tuple:
    """同じ投票行を指すキー. status だけが違う行は「同じ行の更新」とみなす."""
    return (v['ts'], v['user_id'], v['char_id'])


def merge(prev: list[dict], fresh: list[dict]) -> list[dict]:
    """既存 + 差分. 同じ行が両方にあれば **新しく取ったほう** を採る
    (= 人間が status を書き換えた分を反映できる)."""
    by_key = {vote_key(v): v for v in prev}
    for v in fresh:
        by_key[vote_key(v)] = v
    out = list(by_key.values())
    out.sort(key=lambda r: (r['ts'], r['user_id']))
    return out


def read_existing(path: Path) -> dict:
    """前回の取得結果. 無ければ空. 壊れていたら例外 (黙って全消ししない)."""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding='utf-8'))


def main() -> int:
    ap = argparse.ArgumentParser(description='キャラ投票を GAS から取得する')
    ap.add_argument('--out', default=str(DEFAULT_OUT))
    ap.add_argument('--endpoint', default=None, help='既定は post_config.js の値')
    ap.add_argument('--secret-file', default=str(DEFAULT_SECRET_FILE),
                    help='鍵の元にする start.gg client secret のファイル')
    ap.add_argument('--full', action='store_true',
                    help='差分ではなく全件取り直す (既定は差分)')
    args = ap.parse_args()

    endpoint = args.endpoint or read_endpoint()
    key = read_key(Path(args.secret_file))
    out_path = Path(args.out)

    prev = read_existing(out_path)
    prev_votes = prev.get('votes') or []
    prev_total = prev.get('source_rows')
    # 差分の起点 = 手元にある最新のタイムスタンプ. 境界は GAS 側が「以降」で扱う
    # (秒単位なので、同じ秒の別の行を取りこぼさないため). 重複は merge が畳む.
    since = max((str(v.get('ts') or '') for v in prev_votes), default='')

    if args.full or not prev_votes or not since:
        mode, data = 'full', call_export(endpoint, key)
        votes = normalize(data)
    else:
        mode, data = 'diff', call_export(endpoint, key, since)
        # シートの行数が前回より減っている = 行が消された (却下・整理).
        # 差分では削除を追えないので、その場合だけ全件取り直す.
        if isinstance(prev_total, int) and isinstance(data.get('total'), int) \
                and data['total'] < prev_total:
            print(f'  シートの行数が減っています ({prev_total} → {data["total"]})'
                  ' — 差分では追えないので全件取り直します')
            mode, data = 'full-resync', call_export(endpoint, key)
            votes = normalize(data)
        else:
            votes = merge(prev_votes, normalize(data))

    payload = {
        'note': ('キャラ投票 (非公開シート char_votes) の写し。'
                 'build/fetch_char_votes.py で再生成。採用規則は '
                 'docs/post_feature_design.md §12 / v3/v4/char_vote.py'),
        'fetched_at': dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(
            timespec='seconds'),
        'source_rows': data.get('total'),
        'mode': mode,
        'votes': votes,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + '\n',
                   encoding='utf-8')
    os.replace(tmp, out_path)

    n_by_status: dict[str, int] = {}
    for v in votes:
        n_by_status[v['status']] = n_by_status.get(v['status'], 0) + 1
    detail = ', '.join(f'{k}={v}' for k, v in sorted(n_by_status.items())) or '0'
    got = len(data.get('votes') or [])
    print(f'char_votes: {mode} 取得 {got} 行 → 累計 {len(votes)} 行 ({detail}) / '
          f'シート {data.get("total")} 行 → {out_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

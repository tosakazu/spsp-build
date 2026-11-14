"""スマッチ (オンラインのレート対戦サイト) に渡す一覧を <build-dir>/data/partner_smatch.json に書く (2026-11-15〜、日本だけ)。

spsp-web の Worker が GET /api/partner/smatch/players (Bearer トークン) でこのファイルをそのまま返す (docs は spsp-web の docs/smatch_partner.md)。
中身は公開済みの情報だけ (全国順位・オフラインの使用キャラ)。discriminator が分かっている選手だけ。

出力:
  { "version": 1, "generated_at": <秒>, "counted_at": "<集計日>", "ranked_total": <順位の付いている人数>,
    "url_template": "https://spsp.games/jp/p/?d={discriminator}",
    "players": [ { "discriminators": ["<今のアカウント>", "<統合済みの旧アカウント>", ...], "rank": 35, "characters": ["マリオ"] } ] }
  characters = 日本語のキャラ名 (主に使うキャラ → ほかに使うキャラの順。無ければ空)。rank = 総合評価の順位 (無ければ null)

入力 (どれも <build-dir>): latest_tjpr_full.jsonl、meta.json、data/discriminators.json・user_merges.json・character_index.json
(build_discriminators_json・build_user_merges_json の後に動かす)。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # リポジトリ直下 (spsp パッケージ)
from spsp.paths import SITE_DIR  # noqa: E402
from spsp.fileio import write_json_atomic  # noqa: E402

URL_TEMPLATE = 'https://spsp.games/jp/p/?d={discriminator}'


def build(rows: list[dict], discs: dict, merges: dict, char_index: dict, counted_at: str, now: int) -> dict:
    """rows = latest_tjpr_full.jsonl の行、discs = {uid: discriminator}、merges = {旧 uid: 新 uid}、char_index = character_index.json。"""
    name_of = {int(c['id']): c['name'] for c in char_index.get('characters') or []}
    chars: dict[int, list[str]] = {}
    for key in ('main_by_char', 'sub_by_char'):   # 主に使うキャラが先
        for cid, uids in sorted((char_index.get(key) or {}).items(), key=lambda kv: int(kv[0])):
            nm = name_of.get(int(cid))
            if not nm:
                continue
            for u in uids:
                lst = chars.setdefault(int(u), [])
                if nm not in lst:
                    lst.append(nm)
    old_of: dict[int, list[int]] = {}
    for old, new in merges.items():
        old_of.setdefault(int(new), []).append(int(old))
    players = []
    ranked_total = 0
    for r in rows:
        uid = int(r['user_id'])
        rank = (r.get('ranks') or {}).get('ensemble')
        if rank is not None:
            ranked_total += 1
        d = discs.get(str(uid))
        if not d:
            continue
        ds = [d] + [x for x in (discs.get(str(o)) for o in sorted(old_of.get(uid, []))) if x and x != d]
        players.append({'discriminators': list(dict.fromkeys(ds)), 'rank': rank, 'characters': chars.get(uid, [])})
    players.sort(key=lambda p: (p['rank'] is None, p['rank'] or 0, p['discriminators'][0]))
    return {'version': 1, 'generated_at': now, 'counted_at': counted_at, 'ranked_total': ranked_total,
            'url_template': URL_TEMPLATE, 'players': players}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--build-dir', default=str(SITE_DIR))
    args = ap.parse_args(argv)
    if os.environ.get('SPSP_REGION', 'Japan') != 'Japan':
        print('[build_partner_smatch] 日本だけ (この地域では書かない)')
        return
    b = args.build_dir

    def load(rel, default=None):
        try:
            with open(os.path.join(b, rel), encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            if default is None:
                raise
            return default

    rows = []
    with open(os.path.join(b, 'latest_tjpr_full.jsonl'), encoding='utf-8') as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    meta = load('meta.json', {})
    out = build(rows, load('data/discriminators.json'), load('data/user_merges.json', {}), load('data/character_index.json', {}),
                str(meta.get('eval_date') or time.strftime('%Y-%m-%d')), int(time.time()))
    path = os.path.join(b, 'data', 'partner_smatch.json')
    write_json_atomic(path, out, ensure_ascii=False, separators=(',', ':'))
    print(f"[build_partner_smatch] wrote {os.path.normpath(path)}: {len(out['players'])} 人 (順位あり {out['ranked_total']})")


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""プレイヤーページ用の「サブ順位」を事前計算して JSON 出力する。

プレイヤーページで メインキャラ / 居住地(都道府県) の右隣に
「使い手ランキングで何位 / 都道府県別ランキングで何位」を表示するため、
ランキングページ (c/ranking.html, pref/ranking.html) と同じロジックで順位を計算する。

入力 (<build-dir> 既定 site/):
  <build-dir>/latest_tjpr_full.jsonl        全プレイヤー master (ranks.ensemble = 全国順位)
  <build-dir>/data/character_index.json     main_by_char[char_id] = そのキャラを主とする uid 配列
  <build-dir>/data/player_prefectures.json  uid -> 都道府県漢字
  <build-dir>/data/overseas.json            海外タグ勢 uid 配列 (= 使い手ランキングから除外)

出力:
  <build-dir>/data/player_subranks.json
    { uid_str: { "char": {"id": <canonical>, "rank": M},   # 使い手ランキング内順位
                 "pref": {"name": "兵庫県", "rank": N} } }  # 都道府県別ランキング内順位
  ※ char / pref は該当データが無ければ省略。

順位ロジック (= フロントと一致):
  - 各グループを ranks.ensemble (= 全国 ensemble 順位) 昇順でソートし 1 始まりで再付番。
    ensemble 無し (falsy) は末尾。同順位タイはフロントの安定ソートと同じ並び
    (char = main_by_char 配列順 / pref = latest_tjpr_full.jsonl 行順) を保つ。
  - 使い手 (char) は overseas.json の uid を除外。
  - 都道府県 (pref) は除外なし。
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # リポジトリ直下 (spsp パッケージ)
from spsp.paths import SITE_DIR  # noqa: E402
from spsp.fileio import write_json_atomic  # noqa: E402


def _load_jsonl(path):
    recs = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    return recs


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--build-dir', default=str(SITE_DIR), help='入出力の基点 (既定 site/)')
    args = ap.parse_args(argv)
    site = args.build_dir
    master_path = os.path.join(site, 'latest_tjpr_full.jsonl')
    idx_path = os.path.join(site, 'data', 'character_index.json')
    pref_path = os.path.join(site, 'data', 'player_prefectures.json')
    overseas_path = os.path.join(site, 'data', 'overseas.json')
    out_path = os.path.join(site, 'data', 'player_subranks.json')

    master = _load_jsonl(master_path)
    # uid -> 全国 ensemble 順位 (= ソートキー)。フロント同様 falsy は末尾扱い。
    master_uids = []          # jsonl 行順 (= フロント masterMap の挿入順)
    ens = {}
    for r in master:
        uid = r.get('user_id')
        if uid is None:
            continue
        master_uids.append(uid)
        ens[uid] = (r.get('ranks') or {}).get('ensemble')

    idx = json.load(open(idx_path, encoding='utf-8'))
    pref_map = json.load(open(pref_path, encoding='utf-8'))
    try:
        # overseas.json は {"uids": [...]} 形式 (spsp-web の c/ranking と同じ読み方)
        overseas = set(json.load(open(overseas_path, encoding='utf-8')).get('uids') or [])
    except Exception:
        overseas = set()

    BIG = 10 ** 9
    sort_key = lambda u: ens.get(u) or BIG  # noqa: E731  (falsy rank → 末尾)
    out = {}

    # ── 使い手ランキング内順位 ──────────────────────────────
    for char_id, uids in idx.get('main_by_char', {}).items():
        elig = [u for u in uids if u not in overseas and u in ens]
        elig.sort(key=sort_key)  # 安定ソート: タイは main_by_char 配列順のまま
        for i, u in enumerate(elig):
            out.setdefault(str(u), {})['char'] = {'id': int(char_id), 'rank': i + 1}

    # ── 都道府県別ランキング内順位 ────────────────────────────
    # フロント (pref/ranking.html) は masterMap (= jsonl 行順) を走査して安定ソート
    # するため、タイの並びを合わせるのに jsonl 行順で集める。
    by_pref = {}
    for uid in master_uids:
        pref = pref_map.get(str(uid))
        if not pref:
            continue
        by_pref.setdefault(pref, []).append(uid)
    for pref, uids in by_pref.items():
        uids.sort(key=sort_key)  # 安定ソート: タイは jsonl 行順のまま
        for i, u in enumerate(uids):
            out.setdefault(str(u), {})['pref'] = {'name': pref, 'rank': i + 1}

    write_json_atomic(out_path, out, ensure_ascii=False, separators=(',', ':'), sort_keys=True)  # キー順を実行間で固定

    n_char = sum(1 for v in out.values() if 'char' in v)
    n_pref = sum(1 for v in out.values() if 'pref' in v)
    print(f'[build_player_subranks] wrote {out_path}: {len(out)} players '
          f'(char={n_char}, pref={n_pref})')


if __name__ == '__main__':
    sys.exit(main())

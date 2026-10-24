"""キャラ別ランキング用 index データ生成.

入力:
    /tmp/spsp_v4_iter*/players/*.json (= characters フィールドあり)
        characters[0] = メインキャラ (pct 最大)

出力 (= /data/character_index.json):
    {
      "characters": [
        {"id": 1405, "name": "Mr. Game & Watch", "n_main": 42},
        ...                  # n_main 降順 sort
      ],
      "main_by_char": {
        "1405": [1941489, 12345, ...]   # 各 uid を ens rank 昇順 sort (Inf は末尾)
      },
      "sub_by_char": {
        "1405": [...]                   # サブで使う uid (並びは main_by_char と同じ)
      }
    }

メインキャラ判定: characters[0] (= pct 最大). 同率なら最初に出現した方.
サブ: characters[1:] (使用 top-3 の 2・3 番目) のうち使用率 SUB_MIN_PCT 以上で、メインと同じキャラ (統合後) でないもの。
使い手ランキングの「サブ使用も表示」で使う。

統合・除外ルール (= 使い手ランキング画面でのみ適用. プレイヤーページの characters
フィールドはそのまま分離維持):
  - シモン (1411) + リヒター (1412) → "シモン / リヒター" (canonical=1411)
  - ピット (1320) + ブラックピット (1278) → "ピット / ブラックピット" (canonical=1320)
  - サムス (1328) + ダークサムス (1408) → "サムス / ダークサムス" (canonical=1328)
  - ピーチ (1317) + デイジー (1277) → "ピーチ / デイジー" (canonical=1317)
  - Random Character (1746) は集計から除外.
"""
from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path


# メインキャラ集計用: 別 char_id を 1 つの canonical id にマージする.
# {member_id: (canonical_id, display_name)}.
# echo fighter は派生側を統合先 (canonical) のキャラ名で代表する.
_MERGE_TO_CANONICAL: dict[int, tuple[int, str]] = {
    1411: (1411, "シモン / リヒター"),     # シモン (canonical)
    1412: (1411, "シモン / リヒター"),     # リヒター → シモン側に統合
    1320: (1320, "ピット / ブラックピット"),  # ピット (canonical)
    1278: (1320, "ピット / ブラックピット"),  # ブラックピット → ピット側に統合
    1328: (1328, "サムス / ダークサムス"), # サムス (canonical)
    1408: (1328, "サムス / ダークサムス"), # ダークサムス → サムス側に統合
    1317: (1317, "ピーチ / デイジー"),     # ピーチ (canonical)
    1277: (1317, "ピーチ / デイジー"),     # デイジー → ピーチ側に統合
}

# 集計対象外 char_id (= ランダム選択など、メインキャラ判定に意味のないもの).
_EXCLUDED_IDS: set[int] = {1746}  # Random Character

# サブとみなす使用率の下限 (セット単位の pct)。たまに出しただけのキャラは入れない。
SUB_MIN_PCT = 0.20


def _canonical(cid: int, cname: str) -> tuple[int, str]:
    return _MERGE_TO_CANONICAL.get(cid, (cid, cname))


def canonical_char_id(cid: int) -> int:
    """統合後の id (使い手ランキングのキャラ)。ビルドの data/views/char_sub/ も使う"""
    return _MERGE_TO_CANONICAL.get(cid, (cid, ''))[0]


def build_character_index(players_dir: Path) -> dict:
    """players/*.json を読んで集計 (ファイル版)。ビルド中は書き出した dict から
    build_character_index_from_records で作る (読み直しの 9 秒を省く)。順序は同じ (ファイル名の文字列順)。"""
    def _rows():
        for src_path in sorted(players_dir.iterdir()):
            if not src_path.name.endswith('.json'):
                continue
            with open(src_path) as f:
                yield src_path.name, json.load(f)
    return build_character_index_from_records(_rows())


def build_character_index_from_records(rows) -> dict:
    """rows = (ファイル名, rec) の並び。rec は characters / user_id / ranks.ensemble を持てばよい。
    ファイル名の文字列順 (= sorted(players_dir.iterdir()) と同じ) に並べてから集計する。"""
    main_by_char: dict[int, list[tuple[int, int]]] = defaultdict(list)  # cid -> [(ens_rank, uid)]
    sub_by_char: dict[int, list[tuple[int, int]]] = defaultdict(list)
    char_name: dict[int, str] = {}
    # 統合 canonical_id → 統合表示名. 統合グループは個別キャラ名より優先して上書き.
    canonical_display = {
        canonical_id: display
        for canonical_id, display in _MERGE_TO_CANONICAL.values()
    }

    # characters の並びと main_by_char のキー順が走査順に依存するので、名前 (uid) 順に固定する。
    for _name, rec in sorted(rows, key=lambda r: r[0]):
        chars = rec.get('characters') or []
        if not chars:
            continue
        main = chars[0]
        cid = main.get('id')
        cname = main.get('name')
        if cid is None or not cname:
            continue
        cid = int(cid)
        if cid in _EXCLUDED_IDS:
            continue
        # 統合: シモン/リヒター, ピーチ/デイジー など別 char_id を 1 つに集約.
        cid, cname = _canonical(cid, cname)
        uid = rec.get('user_id')
        ens_rank = (rec.get('ranks') or {}).get('ensemble') or 0
        if ens_rank == 0:
            ens_rank = 10**9  # unranked → tail
        main_by_char[cid].append((int(ens_rank), int(uid)))
        char_name[cid] = canonical_display.get(cid, cname)
        subs = set()
        for c in chars[1:]:
            sid, sname = c.get('id'), c.get('name')
            if sid is None or not sname or int(sid) in _EXCLUDED_IDS:
                continue
            if not isinstance(c.get('pct'), (int, float)) or c['pct'] < SUB_MIN_PCT:
                continue
            sid, sname = _canonical(int(sid), sname)
            if sid == cid or sid in subs:
                continue
            subs.add(sid)
            sub_by_char[sid].append((int(ens_rank), int(uid)))
            char_name.setdefault(sid, canonical_display.get(sid, sname))

    # sort uids per char by ens rank asc (= 上位順)
    out_main: dict[str, list[int]] = {}
    for cid, pairs in main_by_char.items():
        pairs.sort()
        out_main[str(cid)] = [uid for _, uid in pairs]
    out_sub: dict[str, list[int]] = {}
    for cid, pairs in sub_by_char.items():
        if cid not in main_by_char:
            continue   # メインの使い手がいないキャラは一覧に無い (使い手ランキングのページも無い)
        pairs.sort()
        out_sub[str(cid)] = [uid for _, uid in pairs]

    characters = [
        {'id': cid, 'name': char_name[cid], 'n_main': len(out_main[str(cid)]), 'n_sub': len(out_sub.get(str(cid), []))}
        for cid in main_by_char
    ]
    characters.sort(key=lambda c: (-c['n_main'], c['name']))

    return {
        'characters': characters,
        'main_by_char': out_main,
        'sub_by_char': out_sub,
    }

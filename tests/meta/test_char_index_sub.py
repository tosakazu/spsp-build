"""spsp/char_index.py の sub_by_char (使い手ランキングの「サブ使用も表示」) の検査。
実行: .venv/bin/python3 tests/meta/test_char_index_sub.py
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)
from spsp.char_index import build_character_index_from_records  # noqa: E402


def rec(uid, ens, chars):
    return (f'{uid}.json', {'user_id': uid, 'ranks': {'ensemble': ens},
                            'characters': [{'id': c, 'name': f'c{c}', 'pct': p} for c, p in chars]})


def main():
    rows = [
        rec(1, 5, [(10, 0.5), (20, 0.3), (30, 0.2)]),       # 20 と 30 がサブ (20% ちょうども入る)
        rec(2, 1, [(20, 0.81), (10, 0.19)]),                 # 19% はサブに入らない
        rec(3, 3, [(1317, 0.4), (1277, 0.4), (20, 0.2)]),   # デイジーはメイン (ピーチ) と同じキャラ → サブにしない
        rec(4, 0, [(30, 0.5), (1277, 0.5)]),                # 順位なし → 末尾。デイジーはピーチのサブ
        rec(5, 2, [(10, 0.8), (1746, 0.2)]),                # ランダムは数えない
    ]
    idx = build_character_index_from_records(rows)
    assert idx['main_by_char'] == {'10': [5, 1], '20': [2], '1317': [3], '30': [4]}, idx['main_by_char']
    assert idx['sub_by_char'] == {'20': [3, 1], '30': [1], '1317': [4]}, idx['sub_by_char']
    n_sub = {c['id']: c['n_sub'] for c in idx['characters']}
    assert n_sub == {10: 0, 20: 2, 1317: 1, 30: 1}, n_sub
    # メインの使い手がいないキャラのサブは出さない (使い手ランキングのページが無い)
    idx2 = build_character_index_from_records([rec(1, 1, [(10, 0.5), (40, 0.5)])])
    assert idx2['sub_by_char'] == {}, idx2
    print('ok')


if __name__ == '__main__':
    main()

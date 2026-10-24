"""bt_per_match per-occurrence 化 (同一イベント×同方向の複数対戦) の回帰テスト.

対象:
- v4.overlay._as_occurrence_list (旧 flat 形式互換)
- v4.overlay._bracket_order_key_dict (表示行の進行順整列が
  v4.common._bracket_order_key (学習側の記録順) と一致すること)
- build_v4_full._serialize_bt_per_match (per-occurrence 直列化)

実行: PYTHONPATH=v3:build python3 tests/meta/test_bt_per_match_occurrences.py
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
for p in (str(REPO),):
    if p not in sys.path:
        sys.path.insert(0, p)

from spsp.overlay import _as_occurrence_list, _bracket_order_key_dict  # noqa: E402
from spsp.common import _bracket_order_key  # noqa: E402
from spsp.cli.build_full import _serialize_bt_per_match  # noqa: E402


class FakeMatch:
    def __init__(self, d):
        self.global_bracket_label = d.get('global_bracket_label')
        self.round_text = d.get('round_text')
        self.global_top_x = d.get('global_top_x')


def test_as_occurrence_list():
    # 旧 flat 形式は 1 occurrence に包む
    assert _as_occurrence_list([0.5, -0.2, True, True]) == [[0.5, -0.2, True, True]]
    # 新形式はそのまま (list に正規化)
    assert _as_occurrence_list([[0.5, 0, 1, 0], [0.1, 0, 1, 0]]) == [[0.5, 0, 1, 0], [0.1, 0, 1, 0]]
    assert _as_occurrence_list(None) is None
    assert _as_occurrence_list([]) is None


def test_bracket_order_key_dict_matches_object_version():
    # WB 表 → LB 裏 → GF → GF Reset の進行順が object 版と一致すること
    rows = [
        {'global_bracket_label': 'Winners TOP 32', 'round_text': 'Winners Quarter-Final', 'global_top_x': 32},
        {'global_bracket_label': 'Winners TOP 2', 'round_text': 'Grand Final', 'global_top_x': 2},
        {'global_bracket_label': 'Losers TOP 3', 'round_text': 'Losers Final', 'global_top_x': 3},
        {'global_bracket_label': 'Winners TOP 2', 'round_text': 'Grand Final Reset', 'global_top_x': 2},
        {'global_bracket_label': None, 'round_text': 'Pools R1', 'global_top_x': None},
    ]
    got_dict = [r['round_text'] for r in sorted(rows, key=_bracket_order_key_dict)]
    got_obj = [m.round_text for m in sorted((FakeMatch(r) for r in rows), key=_bracket_order_key)]
    assert got_dict == got_obj, (got_dict, got_obj)
    assert got_dict == ['Pools R1', 'Winners Quarter-Final', 'Losers Final',
                        'Grand Final', 'Grand Final Reset'], got_dict


def test_serialize_per_occurrence():
    bt_per_match = {
        (100, 1, 2): [[0.512345, 0.0, True, False], [0.1, 0.0, True, False]],
        (100, 2, 1): [[0.3, -0.2, True, True]],
    }
    out = _serialize_bt_per_match(bt_per_match)
    assert out == {'100': {
        '1-2': [[0.5123, 0.0, 1, 0], [0.1, 0.0, 1, 0]],
        '2-1': [[0.3, -0.2, 1, 1]],
    }}, out


if __name__ == '__main__':
    test_as_occurrence_list()
    test_bracket_order_key_dict_matches_object_version()
    test_serialize_per_occurrence()
    print('test_bt_per_match_occurrences: all assertions passed')

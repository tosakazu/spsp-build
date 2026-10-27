"""フェーズの旧ラベル (matches.json の legacy_global_*、2026-10-27) のテスト (plain assert)。

- 学習の並び (_bracket_order_key) は旧ラベルで決まり、表示のラベル (global_*) を直しても変わらない
- 表示用の行 (dict) の並び (overlay._bracket_order_key_dict) も同じ並び。ORDER_KEY は strip_order で消える
- 旧ラベルの値が None のときも、並びのキーは None と同じ ('' / 0 で持つ)
"""
import os, sys
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from spsp import data_loader as D  # noqa: E402
from spsp.common import _bracket_order_key, _bracket_order_key_parts, order_fields, ORDER_KEY  # noqa: E402
from spsp.overlay import _bracket_order_key_dict, strip_order  # noqa: E402


def _row(label, top, legacy=None, rt='Winners Round 1', w=1, l=2):
    r = {'winner_id': w, 'loser_id': l, 'round': 1, 'round_text': rt, 'phase_bracket_type': 'DOUBLE_ELIMINATION',
         'global_top_x': top, 'global_bracket_label': label}
    if legacy is not None:
        r['legacy_global_bracket_label'], r['legacy_global_top_x'] = legacy
    return r


def _m(row):
    return D._match_from_row(row, 0, Path('/x/events/e'), False)


def test_learn_order_uses_legacy():
    # TOP96 の 1 回戦 (直した表示 TOP 32、旧 TOP 4) と TOP8 の 1 回戦 (直した表示 TOP 4、旧 TOP 8)
    a = _m(_row('Winners TOP 32', 32, ('Winners TOP 4', 4)))
    b = _m(_row('Winners TOP 4', 4, ('Winners TOP 8', 8)))
    c = _m(_row('Winners TOP 64', 64))   # 旧ラベルなし (表示と同じ)
    assert sorted([a, b, c], key=_bracket_order_key) == [c, b, a]   # 旧の並び: TOP 64 → 旧 TOP 8 → 旧 TOP 4
    rows = []
    for m in (a, b, c):
        rows.append({'global_bracket_label': m.global_bracket_label, 'global_top_x': m.global_top_x,
                     'round_text': m.round_text, **order_fields(m)})
    assert [r['global_top_x'] for r in sorted(rows, key=_bracket_order_key_dict)] == [64, 4, 32]
    strip_order(rows)
    assert all(ORDER_KEY not in r for r in rows)
    assert ORDER_KEY not in {**order_fields(c)}


def test_legacy_none_values():
    m = _m(_row('Winners TOP 32', 32, (None, None)))
    assert (m.legacy_global_bracket_label, m.legacy_global_top_x) == ('', 0)
    assert _bracket_order_key(m) == _bracket_order_key_parts(None, m.round_text, None)


def test_switch_off_reads_display_labels():
    saved = D._PHASE_ORDER_BY_SIZE
    D._PHASE_ORDER_BY_SIZE = True
    try:
        m = _m(_row('Winners TOP 32', 32, ('Winners TOP 4', 4)))
    finally:
        D._PHASE_ORDER_BY_SIZE = saved
    assert m.legacy_global_bracket_label is None and m.legacy_global_top_x is None
    assert _bracket_order_key(m) == _bracket_order_key_parts('Winners TOP 32', m.round_text, 32)


if __name__ == '__main__':
    test_learn_order_uses_legacy()
    test_legacy_none_values()
    test_switch_off_reads_display_labels()
    print('ok')

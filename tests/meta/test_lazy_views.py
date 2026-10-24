"""spsp/lazy_views.py (生の学習結果を直列化した形として見せる窓) が旧方式の dict と同じに振る舞うか。

- 値・1 段目の並び・len・in・get (当たり / 外れ) が _serialize_* の結果と同じ
- 引くキーが str(int) の正規形でないもの ('007', '+7', ' 7', 7 (int), '1-02' …) は旧方式でも当たらない
- 生のキーに int 以外があれば serialized_views は None (呼び出し側が旧方式に戻る)
- inter の中間ファイル (dump_inter / load_inter) が値ごとの pickle でも旧形式でも読める

実行: python3 tests/meta/test_lazy_views.py
"""
import io
import pickle
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from spsp.lazy_views import (serialized_views, overlay_inputs, G2TimelineView,  # noqa: E402
                             _serialize_bt_timeline, _serialize_bt_per_tour, _serialize_bt_per_match)
from spsp.learn_state import dump_inter, load_inter  # noqa: E402


def _raw():
    tl = {7: [(100, 1500.123456, 300.5, 50.000049), (200, 1510.0, 290.0, 50.33333)], 12: [], -3: [(5, 1.0, 2.0, 3.0)]}
    pt = {
        (7, 1001): {'bt_used': True, 'bt_d': 0.123456, 'bt_internal_d': -0.00005, 'after': 50.1, 'peak_after': 51.0},
        (12, 1001): {'bt_used': False, 'bt_d': 0.0, 'bt_internal_d': 0.0, 'after': 49.0, 'peak_after': 49.0},
        (7, 1002): {'bt_used': True, 'bt_d': 1.0, 'bt_internal_d': 1.0, 'after': 51.1, 'peak_after': 51.1},
        (-3, 5): {'bt_used': False, 'bt_d': 0.0, 'bt_internal_d': 0.0, 'after': 1.0, 'peak_after': 1.0},
    }
    pm = {
        (1001, 7, 12): [[0.123456, -0.11111, True, True], [0.0, 0.0, False, False]],
        (1002, 12, 7): [[0.5, -0.5, False, True]],
        (1001, 12, 7): [[0.25, -0.75, True, False]],
        (5, -3, 7): [[0.1, -0.1, True, True]],
        (5, 7, -3): [],
    }
    return tl, pt, pm


def _deep(m):
    """Mapping (窓) を普通の dict / list にする (並び込み)。"""
    if hasattr(m, 'keys') and not isinstance(m, list):
        return {k: _deep(m[k]) for k in m}
    return m


def _same_types(a, b):
    assert type(a) is type(b) or (isinstance(a, dict) and hasattr(b, 'keys')), (type(a), type(b))
    if isinstance(a, dict):
        assert list(a) == list(b)
        for k in a:
            _same_types(a[k], b[k])
    elif isinstance(a, list):
        assert len(a) == len(b)
        for x, y in zip(a, b):
            _same_types(x, y)
    else:
        assert a == b and type(a) is type(b), (a, b)


def test_views_equal_serialized():
    tl, pt, pm = _raw()
    v_tl, v_pt, v_pm = serialized_views(tl, pt, pm)
    s_tl, s_pt, s_pm = _serialize_bt_timeline(tl), _serialize_bt_per_tour(pt), _serialize_bt_per_match(pm)
    for v, s in ((v_tl, s_tl), (v_pt, s_pt), (v_pm, s_pm)):
        assert list(v) == list(s)
        assert len(v) == len(s) and bool(v) == bool(s)
        assert _deep(v) == s
        _same_types(s, _deep(v))
    # 2 段目の get / in (外れも含む)
    for uid_s in list(s_pt) + ['999', '007', '+7', ' 7', '7 ', '', 'x', '-0']:
        a = s_pt.get(uid_s, {}); b = v_pt.get(uid_s, {})
        for eid_s in ['1001', '1002', '5', '01001', '9', '+5', '-5']:
            assert a.get(eid_s) == b.get(eid_s), (uid_s, eid_s)
            assert (eid_s in a) == (eid_s in b)
    for eid_s in list(s_pm) + ['999', '01001', '+5', '5 ']:
        a = s_pm.get(eid_s); b = v_pm.get(eid_s)
        assert (a is None) == (b is None), eid_s
        if a is None:
            continue
        assert bool(a) == bool(b)
        for key in ['7-12', '12-7', '-3-7', '7--3', '07-12', '7-012', '7-12 ', '+7-12', '7_12', '7', '-0-7']:
            assert a.get(key) == b.get(key), (eid_s, key)
            assert (key in a) == (key in b)
    for key in list(s_tl) + ['007', '+7', 7, 12, '12.0']:
        assert s_tl.get(key) == v_tl.get(key), key
        assert (key in s_tl) == (key in v_tl)
    # int キーで引くと旧方式 (str キーの dict) では当たらない
    assert v_pt.get(7) is None and v_pm.get(1001) is None and v_tl.get(7) is None


def test_compacted_raw_same_views():
    """snap の最後に詰めた (compact_inter_raw) 生の dict でも、窓の値と per_tour_fields / per_match_occurrences は同じ。"""
    import copy
    from spsp.lazy_views import compact_inter_raw, per_tour_fields, per_match_occurrences
    tl, pt, pm = _raw()
    pt[(99, 1)] = {'bt_used': True, 'bt_d': 1.0, 'after': 2.0, 'peak_after': 3.0}   # bt_internal_d 無し (dict のまま)
    pm[(1003, 7, 12)] = [0.5, -0.5, True, False]                                    # 旧 flat 形式 (そのまま)
    cpt, cpm = copy.deepcopy(pt), copy.deepcopy(pm)
    compact_inter_raw(cpt, cpm)
    assert type(cpt[(7, 1001)]) is tuple and type(cpt[(99, 1)]) is dict
    assert cpm[(1002, 12, 7)] == (0.5, -0.5, False, True) and cpm[(5, 7, -3)] == []
    assert type(cpm[(1001, 7, 12)][0]) is tuple and cpm[(1003, 7, 12)] == [0.5, -0.5, True, False]
    for k in pt:
        a = pt[k]
        exp = (a['bt_used'], a.get('bt_internal_d', a['bt_d']), a['bt_d'], a['after'], a['peak_after'])
        assert per_tour_fields(a) == exp and per_tour_fields(cpt[k]) == exp
    for k in pm:
        a = [list(o) for o in per_match_occurrences(pm[k])]
        b = [list(o) for o in per_match_occurrences(cpm[k])]
        assert a == b, k
    del pt[(99, 1)]; del cpt[(99, 1)]
    s_pt, s_pm = _serialize_bt_per_tour(pt), _serialize_bt_per_match(pm)
    _, v_pt, v_pm = serialized_views(tl, cpt, cpm)
    assert _deep(v_pt) == s_pt and _deep(v_pm) == s_pm
    _same_types(s_pt, _deep(v_pt)); _same_types(s_pm, _deep(v_pm))


def test_values_are_fresh():
    tl, pt, pm = _raw()
    v_tl, v_pt, v_pm = serialized_views(tl, pt, pm)
    v_pm['1001']['7-12'][0][0] = 99.0
    assert pm[(1001, 7, 12)][0][0] == 0.123456
    assert v_pm['1001']['7-12'][0][0] == round(0.123456, 4)


def test_non_int_keys_fallback():
    tl, pt, pm = _raw()
    pt[('7', 1003)] = pt[(7, 1001)]
    assert serialized_views(tl, pt, pm) is None
    inter = {'_bt_timeline_raw': tl, '_bt_per_tour_raw': pt, '_bt_per_match_raw': pm}
    a, b, c = overlay_inputs(inter)
    assert isinstance(b, dict) and b == _serialize_bt_per_tour(pt)
    tl[True] = []
    assert serialized_views(tl, {}, {}) is None


def test_overlay_inputs_old_inter():
    tl, pt, pm = _raw()
    inter = {'bt_timeline': {'x': 1}, 'bt_per_tour': {'y': 2}, 'bt_per_match': {'z': 3},
             '_bt_timeline_raw': tl, '_bt_per_tour_raw': pt, '_bt_per_match_raw': pm}
    assert overlay_inputs(inter) == ({'x': 1}, {'y': 2}, {'z': 3})


def test_g2_timeline_view():
    tl, _, _ = _raw()
    g = G2TimelineView(tl)
    old = {uid: [(ts, mu, sigma) for (ts, mu, sigma, _o) in e] for uid, e in tl.items()}
    for uid in list(tl) + [99, '7']:
        assert g.get(uid, []) == old.get(uid, [])


def test_inter_file_roundtrip():
    tl, pt, pm = _raw()
    inter = {'a': [1, 2, {'x': (3, 4)}], '_bt_per_match_raw': pm, 'eval_date': '2026-10-17', 'none': None}
    buf = io.BytesIO(); dump_inter(inter, buf); buf.seek(0)
    got = load_inter(buf)
    assert got == inter and list(got) == list(inter)
    # 旧形式 (inter 全体を 1 つの pickle)
    buf = io.BytesIO(); pickle.dump(inter, buf, protocol=pickle.HIGHEST_PROTOCOL); buf.seek(0)
    got = load_inter(buf)
    assert got == inter and list(got) == list(inter)
    # 旧形式で、たまたま format / keys というキーを持つ inter も旧形式として読む
    odd = {'format': 'x', 'keys': ['a']}
    buf = io.BytesIO(); pickle.dump(odd, buf); buf.seek(0)
    assert load_inter(buf) == odd


def test_int_key_view():
    from spsp.lazy_views import IntKeyView
    d = {'377602': 56.6671, '12': 1.0, '-5': 2.0}
    ref = {int(k): v for k, v in d.items()}
    v = IntKeyView(d)
    assert list(v) == list(ref) and v.items() == list(ref.items()) and list(v.values()) == list(ref.values())
    assert len(v) == len(ref) and bool(v) == bool(ref) and bool(IntKeyView({})) is False
    for k in (377602, 12, -5, 7, '12', None, 12.5):
        assert v.get(k) == ref.get(k) and (k in v) == (k in ref) and v.get(k, 'x') == ref.get(k, 'x'), k
    assert sorted(v.items(), key=lambda x: (-x[1], x[0])) == sorted(ref.items(), key=lambda x: (-x[1], x[0]))


def test_rolling_raw_at():
    from spsp.rolling import raw_at_only, rolling_raw_at
    full = {11: {1: {'tjpr_d': None, 'bt_d': 2, 'ens_d': None, 'tjpr_rb': None, 'bt_rb': 3, 'ens_rb': None,
                     'raw_at': 0.0, 'bt_ord_at': None},
                 2: {'tjpr_d': 1, 'bt_d': None, 'ens_d': 1, 'tjpr_rb': 5, 'bt_rb': None, 'ens_rb': 4,
                     'raw_at': 12.5, 'bt_ord_at': 0.25}},
            12: {}}
    slim = raw_at_only(full)
    assert slim == {11: {1: 0.0, 2: 12.5}, 12: {}}
    for tid in (11, 12, 99):
        for uid in (1, 2, 3):
            old_out = ((full.get(tid, {}) or {}).get(uid) or {}).get('raw_at')          # output._overlay_at_time_entry
            old_score = float((full.get(tid, {}).get(uid) or {}).get('raw_at', 0.0))    # score.build_at_time_per_tournament
            for d in (full, slim):
                assert rolling_raw_at(d.get(tid), uid) == old_out
                assert rolling_raw_at(d.get(tid, {}), uid) == old_out
                assert float(rolling_raw_at(d.get(tid, {}), uid, 0.0)) == old_score
    assert rolling_raw_at(None, 1) is None and rolling_raw_at({}, 1, 0.0) == 0.0


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
    print('ok')

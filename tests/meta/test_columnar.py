"""spsp/columnar.py (学習の記録の配列の表) が、元の dict と同じキーの並び・値・型に見えること。

実行: python3 tests/meta/test_columnar.py
"""
import pickle
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from spsp.columnar import (AnchorTable, BtTimeline, BtUsedView, GatedHist, MatchLog, PerTourByLv, MatchWins, PerTourLog, PlayerMatches, RatingHist,  # noqa: E402
                           StrUidTable, TourMatchDeltas, UidIndex, UidMap, compact_str_table, compact_uid_map)


def test_columnar():
    random.seed(1)
    d = {}; log = MatchLog()
    for _ in range(20000):
        k = (random.randint(1, 300), random.randint(1, 50), random.randint(1, 50))
        v = [round(random.uniform(-1, 1), 6), -0.0 if random.random()<.1 else round(random.uniform(-1,0),6), random.random()<.5, random.random()<.5]
        d.setdefault(k, []).append(v); log.add(k[0], k[1], k[2], *v)
    assert list(log) == list(d)
    for (k, a), (k2, b) in zip(d.items(), log.items()):
        assert k == k2 and [tuple(x) for x in a] == b, (k, a, b)
        assert all(type(x) is type(y) for p, q in zip(a, b) for x, y in zip(p, q))
    for k in list(d)[:500]:
        assert log[k] == [tuple(x) for x in d[k]] and k in log
    assert log.get((999, 1, 1)) is None and (999, 1, 1) not in log and len(log) == len(d)
    # get3 (= get((eid, w, l))): 有るキー・無いキー (成分ごとに無い / 組だけ無い)
    for _ in range(3000):
        k = (random.randint(0, 301), random.randint(0, 51), random.randint(0, 51))
        assert log.get3(*k) == log.get(k), k
    l2 = pickle.loads(pickle.dumps(log)); assert list(l2.items()) == list(log.items())
    assert list(MatchLog.from_dict(d).items()) == list(log.items())
    # per tour with overwrite
    d = {}; log = PerTourLog()
    for _ in range(20000):
        k = (random.randint(1, 500), random.randint(1, 100))
        v = (random.random() < .5, round(random.gauss(0, 1), 6), round(random.gauss(0, 1), 6), round(random.gauss(0, 1), 6), round(random.gauss(0, 1), 6))
        d[k] = v; log.add(*k, *v)
    assert list(log) == list(d) and list(log.items()) == list(d.items())
    assert all(log[k] == d[k] for k in list(d)[:300])
    assert all(type(x) is type(y) for k in list(d)[:300] for x, y in zip(log[k], d[k]))
    e = MatchLog(); assert len(e) == 0 and list(e.items()) == [] and not e


def test_anchor_table():
    random.seed(2)
    d = {}
    for _ in range(300):
        u = random.randint(1, 10**7)
        e = [random.randint(0, 6), random.choice([None, random.randint(1, 300000)]), random.randint(1, 5), None]
        if random.random() < .7:
            e += [random.choice([None, random.randint(1, 300000)]), 3, None]
        d[u] = e
    a = AnchorTable(d)
    assert list(a) == list(d) and a.items() == list(d.items()) and len(a) == len(d) and bool(a)
    for u in d:
        assert a[u] == d[u] and a.get(u) == d[u] and u in a
    assert a.get(-5) is None and -5 not in a and a.get('x') is None
    assert pickle.loads(pickle.dumps(a)).items() == a.items()
    e = AnchorTable({}); assert not e and e.items() == [] and e.get(1) is None


def test_series():
    random.seed(3)
    d = {}; log = BtTimeline(); rh = RatingHist(); dr = {}
    for i in range(5000):
        u = random.randint(1, 400)
        e = (1000 + i, random.gauss(1500, 100), random.uniform(50, 350), random.gauss(0, 50))
        d.setdefault(u, []).append(e); log.add(u, *e)
        dr.setdefault(u, []).append((e[0], e[1])); rh.add(u, e[0], e[1])
    assert list(log) == list(d) and list(log.items()) == list(d.items()) and dict(log) == d
    assert all(log[u] == d[u] and log.get(u) == d[u] and u in log for u in d)
    assert all(type(x) is type(y) for u in d for a, b in zip(log[u], d[u]) for x, y in zip(a, b))
    assert log.get(-1) is None and log.get(-1, ()) == () and -1 not in log and (1,) not in log
    assert list(rh.items()) == list(dr.items()) and rh.get(next(iter(dr))) == dr[next(iter(dr))]
    assert list(BtTimeline.from_dict(d).items()) == list(d.items()) and BtTimeline.from_dict({1: []}) is None
    assert list(pickle.loads(pickle.dumps(log)).items()) == list(d.items())


def test_views_over_log():
    """spsp/lazy_views の窓が、生の dict でも配列の表でも同じ値を返す (1 回目は 1 件ずつ、2 回目からは選手・大会ごとの dict)。"""
    from spsp.lazy_views import BtPerMatchView, BtPerTourView
    random.seed(4)
    dm = {}; lm = MatchLog(); dt_ = {}; lt = PerTourLog()
    for _ in range(8000):
        k = (random.randint(1, 120), random.randint(1, 60), random.randint(1, 60))
        v = (random.uniform(-1, 1), random.uniform(-1, 0), random.random() < .5, random.random() < .5)
        dm.setdefault(k, []).append(v); lm.add(*k, *v)
        u, e = random.randint(1, 80), random.randint(1, 150)
        w = (random.random() < .5, random.gauss(0, 1), random.gauss(0, 1), random.gauss(0, 1), random.gauss(0, 1))
        dt_[(u, e)] = w; lt.add(u, e, *w)
    assert lm.groups_with_first(-5) == {} and lt.groups_with_first(1.0) == {}
    for va, vb in ((BtPerMatchView(dm), BtPerMatchView(lm)), (BtPerTourView(dt_), BtPerTourView(lt))):
        assert list(va) == list(vb)
        for k1 in list(va) + ['0', '01', 'x']:
            if k1 not in va:
                assert k1 not in vb; continue
            a, b = va[k1], vb[k1]
            probes = [f'{w}-{l}' for w in range(0, 62) for l in range(0, 62, 7)] if isinstance(va, BtPerMatchView) \
                else [str(e) for e in range(0, 152)] + ['01', '+3', 'a']
            for q in probes:   # 1 件目は 1 件ずつ、2 件目からは dict
                assert (q in a) == (q in b), (k1, q)
                assert a.get(q) == b.get(q), (k1, q)


def test_player_matches():
    """build の player_matches / match_wins (spsp/build.py が以前 dict に足していたのと同じ値・並び・型)。
    試合は Match の代わりに表示に読む項目だけを持つ MatchView (spsp/columnar._VIEW_FIELDS が同じ値)。"""
    import math
    from collections import defaultdict
    from spsp.columnar import _VIEW_FIELDS
    random.seed(5)

    class M:
        def __init__(self):
            for k in _VIEW_FIELDS:
                setattr(self, k, random.choice([None, 1, 2, 'Winners Round 1', 'Losers Final', 1.0, True, 0]))

    def norm(t):   # 試合を比べられる形に
        return tuple(tuple((type(getattr(x, k)), getattr(x, k)) for k in _VIEW_FIELDS) if x is not None and not
                     isinstance(x, (int, float, bool)) else x for x in t)
    pm = PlayerMatches(); dpm = defaultdict(list); dmw = defaultdict(list)
    for _ in range(20000):
        w, l = random.sample(range(1, 300), 2)
        ts, tid = random.randint(0, 10**9), random.randint(1, 10**12)
        m = M() if random.random() < .9 else None
        d_w = random.choice([random.gauss(0, 1), 0.0, -0.0]); d_l = random.choice([random.gauss(0, 1), -0.0])
        cls, tw, tl = random.random() < .2, random.random() < .5, random.random() < .5
        mi = pm.add_match(m)
        pm.add(w, ts, tid, l, True, max(0.0, d_w), cls, mi, tw); pm.add(l, ts, tid, w, False, min(0.0, d_l), cls, mi, tl)
        if d_w > 0:
            dmw[w].append((tid, l, d_w, m))
        dpm[w].append((ts, tid, l, True, max(0.0, d_w), cls, m, tw))
        dpm[l].append((ts, tid, w, False, min(0.0, d_l), cls, m, tl))
    mw = MatchWins(pm)
    assert list(pm) == list(dpm) and pm.get(1.5) is None and pm.get(-1, []) == []
    for u in range(0, 302):
        a, b = dpm.get(u, []), pm.get(u, [])
        assert [norm(p) for p in a] == [norm(q) for q in b], u
        assert all(type(x) is type(y) for p, q in zip(a, b) for j, (x, y) in enumerate(zip(p, q)) if j != 6)
        assert all((p[6] is None) == (q[6] is None) and math.copysign(1, p[4]) == math.copysign(1, q[4]) for p, q in zip(a, b))
        assert [norm(p) for p in dmw.get(u, [])] == [norm(q) for q in mw.get(u, [])], u


def test_player_matches_seq():
    """seq 付きで足すと、uid ごとの並びと uid の並びが seq の順 (足した順ではない)。並びを決めたら足せない。"""
    from collections import defaultdict
    from spsp.columnar import _VIEW_FIELDS
    random.seed(7)

    class M:
        def __init__(self):
            for k in _VIEW_FIELDS:
                setattr(self, k, random.choice([None, 1, 1.0, True, 'Losers Final']))

    rows = []
    for q in range(3000):
        w, l = random.sample(range(1, 80), 2)
        rows.append((q, w, l, M() if random.random() < .9 else None, random.gauss(0, 1)))
    pm = PlayerMatches()
    for q, w, l, m, d in random.sample(rows, len(rows)):   # 足す順はばらばら
        mi = pm.add_match(m)
        pm.add(w, 10, 1, l, True, d, False, mi, True, seq=2 * q)       # spsp/build.py と同じ: 敗者は +1
        pm.add(l, 10, 1, w, False, -d, False, mi, False, seq=2 * q + 1)
    want = defaultdict(list)
    for q, w, l, m, d in rows:                              # seq の順
        want[w].append((l, d, m)); want[l].append((w, -d, m))
    assert list(pm) == list(want)
    for u, lst in want.items():
        got = pm.get(u)
        assert [(r[2], r[4]) for r in got] == [(o, d) for o, d, _ in lst], u
        for r, (_, _, m) in zip(got, lst):
            assert (r[6] is None) == (m is None)
            if m is not None:
                assert all(type(getattr(r[6], k)) is type(getattr(m, k)) and getattr(r[6], k) == getattr(m, k)
                           for k in _VIEW_FIELDS)
    try:
        pm.add_match(None)
    except RuntimeError:
        pass
    else:
        raise AssertionError('並びを決めた後に足せてしまう')


def test_day_tables():
    """日ごとの {uid: 数} (UidMap) と {str(uid): 数 / [数, …]} (StrUidTable) が元の dict と同じに見える。"""
    import numpy as np
    random.seed(6)
    ix = UidIndex()
    for t in range(12):
        us = random.sample(range(1, 4000), random.randint(1, 2000))
        d = {u: (random.randint(1, 9999) if t % 2 else random.random()) for u in us}
        m = compact_uid_map(d, ix)
        assert type(m) is UidMap and list(m) == list(d) and m.items() == list(d.items()) and m.values() == list(d.values())
        for k in list(range(0, 4001)) + [True, np.int64(us[0]), 1.0, 'x']:
            assert (k in d) == (k in m) and d.get(k) == m.get(k), k
        assert all(type(m[u]) is type(d[u]) for u in us)
        if t % 2:
            ds = {str(u): [random.randint(0, 8), random.randint(1, 4000), round(random.random() * 99, 4)] for u in us}
        else:
            ds = {str(u): round(random.random(), 4) for u in us}
        s = pickle.loads(pickle.dumps(compact_str_table(ds, ix)))
        assert type(s) is StrUidTable and list(s) == list(ds) and s.items() == list(ds.items())
        for k in [str(u) for u in range(0, 4001)] + ['01', '+1', 1, 'x']:
            assert (k in ds) == (k in s) and ds.get(k) == s.get(k), k
    assert type(compact_uid_map({1: 1, 2: 2.0}, ix)) is dict and type(compact_uid_map({1: True}, ix)) is dict
    assert type(compact_str_table({'1': [1, 2.0], '2': [1.0, 2.0]}, ix)) is dict
    assert type(compact_str_table({'01': 1.0}, ix)) is dict


def test_gated_hist_and_deltas():
    random.seed(4)
    hist = {}; used = {}
    for u in random.sample(range(1, 10**6), 300):
        tids = random.sample(range(10**9, 10**9 + 5000), random.randint(1, 30))
        hist[u] = [(t, random.uniform(-5, 5), random.uniform(-1, 1), -0.0 if random.random() < .1 else random.uniform(-1, 1),
                    random.uniform(0, 5)) for t in tids]
        used[u] = {t: random.random() < .5 for t in reversed(tids)}
    g = GatedHist()
    for u, h in hist.items():
        for e in h:
            g.add(u, *e, 1 if used[u][e[0]] else 0)
    bu = BtUsedView(g)
    assert list(g) == list(hist) and list(g.items()) == list(hist.items()) and len(g) == len(hist)
    for u in hist:
        assert g.get(u, []) == hist[u] and g[u] == hist[u]
        assert all(type(x) is type(y) for a, b in zip(g[u], hist[u]) for x, y in zip(a, b))
        assert bu.get(u, {}) == used[u] and bu[u] == used[u] and u in bu
        assert all(bu.get(u, {}).get(t, False) is used[u][t] for t in used[u])
    assert g.get(-1, []) == [] and bu.get(-1, {}) == {} and -1 not in bu and list(bu) == list(used)

    class T:
        def __init__(self, ms): self.matches = ms
    class M:
        def __init__(self):
            self.winner_id, self.loser_id = random.randint(1, 99), random.randint(100, 199)
    ts = [T([M() for _ in range(random.randint(0, 8))]) for _ in range(200)]
    d = {}
    for t in ts:
        for m in t.matches:
            if random.random() < .8:
                d[id(m)] = (random.uniform(0, 1), -0.0 if random.random() < .1 else random.uniform(-1, 0),
                            random.random() < .5, random.random() < .5)
    md = TourMatchDeltas(d, ts)
    for t in ts:
        got = md.of_tournament(t)
        assert got == [d.get(id(m)) for m in t.matches]
        assert all(type(x) is type(y) for a, b in zip(got, [d.get(id(m)) for m in t.matches]) if a for x, y in zip(a, b))
    assert md.of_tournament(T([])) == []
    t0 = next(t for t in ts if len(t.matches) >= 2); t0.matches.reverse()
    try:
        md.of_tournament(t0); raise AssertionError('並べ替えを見逃した')
    except RuntimeError:
        pass


def test_per_tour_by_lv_and_contrib():
    from collections import defaultdict
    import numpy as np
    from spsp.learn import Contrib
    random.seed(5)
    d = defaultdict(lambda: defaultdict(dict))
    uids = random.sample(range(1, 10**6), 200); tids = random.sample(range(10**9, 10**9 + 3000), 400)
    for lv in range(1, 6):
        for u in uids[:200 // lv]:
            for t in random.sample(tids, random.randint(1, 20)):
                f = (lambda x: np.float64(x)) if random.random() < .5 else (lambda x: x)
                d[u][t][lv] = [f(round(random.uniform(0, 9), 4)), round(random.random(), 6), f(round(random.uniform(0, 9), 4)), random.randint(0, 1)]
    p = PerTourByLv.from_nested(d)
    assert p is not None and list(p) == list(d) and len(p) == len(d)
    for u in d:
        tv = p.get(u, {})
        assert set(tv) == set(d[u]) and len(tv) == len(d[u]) and u in p
        for t in d[u]:
            got = dict(tv.get(t, {}))
            assert got == d[u][t] and list(got) == list(d[u][t])
            assert all(type(a) is type(b) for k in got for a, b in zip(got[k], d[u][t][k]))
        assert dict(tv.get(-5, {})) == {}
    assert p.get(-1, {}) == {} and -1 not in p
    assert PerTourByLv.from_nested({1: {2: {1: [1.0, 1.0, 1.0, True]}}}) is None

    c = Contrib((7, 1.5, 0.9, 1.0, 1.35, 3))
    ref = {'t_id': 7, 'raw_points': 1.5, 'age_decay': 0.9, 'pos_factor': 1.0, 'weighted': 1.35, 'rank_in_player': 3}
    assert all(c[k] == v and c.get(k) == v for k, v in ref.items())
    assert c.get('nope', 0) == 0 and c[0] == 7
    try:
        c['nope']; raise AssertionError
    except KeyError:
        pass


def test_at_time_of_tour():
    from spsp.columnar import AtTimeOfTour
    from spsp.score import AtTimeRec
    random.seed(6)
    d = {}
    for u in random.sample(range(1, 10**6), 300):
        d[u] = AtTimeRec((random.randint(0, 5), *[None if random.random() < .2 else random.randint(1, 9000) for _ in range(6)],
                          random.random() < .5))
    a = AtTimeOfTour.compact(d, AtTimeRec)
    assert list(a) == list(d) and len(a) == len(d)
    for u, r in d.items():
        g = a.get(u)
        assert g == r and type(g) is AtTimeRec and all(type(x) is type(y) for x, y in zip(g, r)) and u in a
        assert g.get('ens_before') == r.get('ens_before')
    assert a.get(-1) is None and -1 not in a
    assert AtTimeOfTour.compact({}, AtTimeRec) is None
    assert AtTimeOfTour.compact({1: AtTimeRec((1, 2.0, 3, 4, 5, 6, 7, True))}, AtTimeRec) is None


def test_str_per_tour_by_lv():
    import pickle as _p
    import numpy as np
    from spsp.columnar import StrPerTourByLv
    random.seed(7)
    d = {}
    keys = [str(random.randint(10**5, 10**7)) for _ in range(300)] + [f"cv:{random.randint(1, 999)}:B" for _ in range(30)]
    for u in random.sample(range(1, 10**6), 250):
        em = {}
        for k in random.sample(keys, random.randint(1, 15)):
            em[k] = {str(lv): [np.float64(round(random.uniform(0, 9), 4)) if random.random() < .5 else round(random.uniform(0, 9), 4),
                               round(random.random(), 6), round(random.uniform(0, 9), 4), random.randint(0, 1)]
                     for lv in sorted(random.sample(range(1, 6), random.randint(1, 3)), reverse=random.random() < .3)}
        d[str(u)] = em
    t = StrPerTourByLv.from_nested(d)
    for m in (t, _p.loads(_p.dumps(t))):
        assert list(m) == list(d) and len(m) == len(d)
        for (u, em), (u2, em2) in zip(d.items(), m.items()):
            assert u == u2 and list(em2) == list(em) and len(em2) == len(em)
            for (k, lm), (k2, lm2) in zip(em.items(), em2.items()):
                assert k == k2 and list(lm2) == list(lm) and dict(lm2) == lm and bool(lm2)
                for lv in lm:
                    assert lv in lm2 and lm2[lv] == lm[lv] and lm2.get(lv) == lm[lv]
                    assert [type(x) for x in lm2[lv]] == [type(x) for x in lm[lv]]
                assert '9' not in lm2 and lm2.get('9') is None
            assert m.get(u, {}).get(next(iter(em))) is not None and m.get(u, {}).get('nope') is None
        assert m.get('nope', {}) == {} and '0' not in m and m.get('01') is None
    assert StrPerTourByLv.from_nested({'1': {'2': {'1': [1.0, 1.0, 1.0, True]}}}) is None


def test_int_counts():
    from collections import defaultdict
    from spsp.columnar import IntCounts, compact_int_counts
    random.seed(8)
    dd = defaultdict(lambda: defaultdict(int))
    for _ in range(3000):
        dd[random.randint(1, 50)][random.randint(1, 10**6)] += 1
    dd[99] = defaultdict(int)
    c = compact_int_counts(dd)
    assert list(c) == list(dd)
    for tid, d in dd.items():
        x = c[tid]
        assert list(x) == list(d) and len(x) == len(d) and sum(x.values()) == sum(d.values())
        assert all(x.get(u, 0) == v and type(x.get(u)) is int for u, v in d.items()) and x.get(-1, 0) == 0
    assert type(c[99]) is not IntCounts and IntCounts.compact({1: 1.0}) is None


def test_tour_entry_store():
    import numpy as np
    from spsp.columnar import TourEntryStore
    random.seed(9)
    ref = {}; st = TourEntryStore(); rows = {}
    def rnd_row(u):
        f = lambda: (np.float64(random.uniform(0, 9)) if random.random() < .5 else random.uniform(-1, 1))
        o = lambda: None if random.random() < .2 else random.randint(0, 9000)
        return (u, o(), o(), o(), random.randint(1, 900), o(), o(), f(), f(), random.randint(0, 5), random.random() < .5,
                f(), f(), random.random() < .5)
    for u in random.sample(range(1, 10**6), 300):
        for tid in random.sample(range(10**9, 10**9 + 80), random.randint(1, 10)):
            r = rnd_row(u)
            ref.setdefault(tid, {})[u] = r
            rows[(tid, u)] = st.add(tid, r)
        tid = random.choice([k for (k, uu) in rows if uu == u])
        r = rnd_row(u); ref[tid][u] = r; st.replace(rows[(tid, u)], r)
    odd = (5, 'x', None, None, 1, 1, 1, 0.0, 0.0, 0, False, 0.0, 0.0, False)
    ref.setdefault(10**9 + 99, {})[5] = odd; st.add(10**9 + 99, odd)
    assert st.n_odd == 1 and len(st) == len(ref)
    for tid, d in ref.items():
        g = st.get(tid, {})
        assert list(g) == list(d) and g == d
        assert all(type(x) is type(y) for u in d for x, y in zip(g[u], d[u]))
    assert st.get(-1, {}) == {}


def test_contrib_table():
    """ContribTable / LvContribs: 引いた list が以前の defaultdict(list) of Contrib と同じ (値・型・並び)。"""
    import numpy as np
    from spsp.learn import Contrib, ContribTable, LvContribs
    random.seed(11)
    ref = {}; tab = ContribTable()
    for u in random.sample(range(1, 10**6), 300):
        rows = []
        for i in range(random.randint(0, 12)):
            f = (lambda x: np.float64(x)) if random.random() < .3 else float
            rows.append((random.randint(1, 1 << 46), f(random.uniform(0, 9)), random.random(), 0.9 ** i, f(random.uniform(0, 5))))
        tab.add(u, rows)
        ref[u] = [Contrib((*r, i)) for i, r in enumerate(rows)]
    assert list(tab) == list(ref) and len(tab) == len(ref)
    for u, lst in ref.items():
        got = tab.get(u, [])
        assert got == lst and tab[u] == lst and u in tab
        assert all(type(a) is type(b) for g, r in zip(got, lst) for a, b in zip(g, r))
        assert all(g['rank_in_player'] == i and g.get('weighted') == r['weighted'] for i, (g, r) in enumerate(zip(got, lst)))
    assert dict(tab.items()) == ref
    assert tab.get(-1, []) == [] and -1 not in tab
    try:
        tab.add(next(iter(ref)), []); raise AssertionError
    except ValueError:
        pass
    lv2 = ContribTable(); some = list(ref)[:50]
    for u in some:
        lv2.add(u, [(1, 1.0, 1.0, 1.0, 1.0)])
    plv = {u: (2 if u in set(some) else 1) for u in ref}
    view = LvContribs(plv, [tab, lv2])
    assert list(view) == list(plv) and len(view) == len(plv)
    for u in ref:
        assert view[u] == (lv2[u] if plv[u] == 2 else ref[u]) and view.get(u) == view[u]
    assert view.get(-1, []) == [] and -1 not in view


def test_tour_uid_table():
    """TourUidTable: 大会ごとの {uid: 値} の並び・値・型・合計が元の dict と同じ。表せない大会だけ dict のまま。"""
    from collections import defaultdict
    from spsp.columnar import TourUidTable
    from spsp.meta import SeedInfo
    from spsp.score import AtTimeRec
    random.seed(12)
    pl = {}; si = {}; at = {}; cnt = {}
    ri = lambda: random.choice([None, random.randint(1, 600)])
    for k in range(300):
        tid = random.randint(1, 1 << 46)
        us = random.sample(range(1, 10**7), random.randint(0, 40))
        pl[tid] = {u: ri() for u in us}
        si[tid] = {u: SeedInfo((random.randint(1, 600), random.randint(1, 600), random.choice([None, random.randint(-9, 9)])))
                   for u in us if random.random() < .7}
        at[tid] = {u: AtTimeRec((random.randint(1, 5), ri(), ri(), ri(), ri(), ri(), ri(), random.random() < .5)) for u in us}
        c = defaultdict(int)
        for u in us:
            c[u] += random.randint(1, 4)
        cnt[tid] = c
    odd = random.choice(list(pl)); pl[odd] = {**pl[odd], 'x': 1}
    cases = ((pl, TourUidTable.compact(pl)), (si, TourUidTable.compact(si, 'iii', SeedInfo)),
             (at, TourUidTable.compact(at, 'iiiiiiib', AtTimeRec)), (cnt, TourUidTable.compact(cnt, 'i', totals=True)))
    for ref, tab in cases:
        assert list(tab) == list(ref) and len(tab) == len(ref)
        for tid, d in ref.items():
            v = tab.get(tid, {})
            assert list(v) == list(d) and list(v.keys()) == list(d) and list(v.items()) == list(d.items())
            assert list(v.values()) == list(d.values()) and len(v) == len(d) and bool(v) == bool(d) and v == d and tid in tab
            for u, x in d.items():
                assert v.get(u) == x and v[u] == x and u in v and type(v.get(u)) is type(x)
                if isinstance(x, tuple):
                    assert all(type(a) is type(b) for a, b in zip(v[u], x))
            assert v.get(-3, 'no') == 'no' and -3 not in v and v.get('1') is None
            if ref is cnt:
                assert v.total() == sum(d.values())
        assert tab.get(-1, {}) == {} and -1 not in tab
    assert cases[0][1].n_odd == 1 and cases[0][1][odd] is pl[odd] and cases[1][1].n_odd == 0
    for dd, codes in (({1: {2: 1.5}}, 'i'), ({1: {2: 1 << 40}}, 'i'), ({1: {2: None}}, 'd'), ({1: {2: 1}}, 'd'), ({1: {2: 1}}, 'b')):
        t = TourUidTable.compact(dd, codes)
        assert t.n_odd == 1 and t[1] is dd[1]
    fl = {7: {3: 0.25, 1: -1.5}, 8: {}}
    ft = TourUidTable.compact(fl, 'd')
    assert ft.n_odd == 0 and list(ft[7].items()) == list(fl[7].items()) and type(ft[7][3]) is float and not ft[8]
    try:
        ft.add(7, {}); raise AssertionError
    except ValueError:
        pass

def test_top_ufs():
    """TopUfs: 選手ページで引く上位 (sorted(..., key=top_uf_key)[:TOP_UFS_N]) が全件の list と同じ。"""
    from spsp.score import TOP_UFS_N, TopUfs, top_uf_key
    random.seed(3)
    d = {}
    for u in range(200):
        d[u] = [{'uf': random.randint(1, 5), 'opp_uid': random.randint(1, 99), 'event_id': random.randint(1, 9),
                 'date': random.choice([None, '2024-0%d-1%d' % (random.randint(1, 9), random.randint(0, 9))]),
                 'is_pre': random.random() < .5} for _ in range(random.randint(0, 60))]
    d[999] = [{'uf': 1, 'date': None}]   # キーの違う dict はその選手だけ dict のまま
    t = TopUfs(d)
    for u, lst in d.items():
        got = sorted(t.get(u, []), key=top_uf_key)[:TOP_UFS_N]
        ref = sorted(lst, key=top_uf_key)[:TOP_UFS_N]
        assert got == ref and [list(x) for x in got] == [list(x) for x in ref]
    assert t.get(-1, []) == [] and len(t) == len(d) and list(t) == list(d)


def test_scaled_map():
    from spsp.columnar import SHARED_UIDS, ScaledMap, compact_uid_map
    random.seed(4)
    d = {random.randint(1, 10**7): random.uniform(0, 50) for _ in range(500)}
    m = ScaledMap(compact_uid_map(d, SHARED_UIDS), 0.35)
    ref = {u: x * 0.35 for u, x in d.items()}
    assert list(m) == list(ref) and m.items() == list(ref.items()) and m.values() == list(ref.values()) and len(m) == len(ref)
    assert all(m.get(u) == x and m[u] == x and u in m for u, x in ref.items()) and m.get(-1) is None and -1 not in m


def test_gated_from_per_tour():
    """GatedHist.from_per_tour が spsp/build._v4_adapt_bt_outputs の以前の作り方 (uid ごとの list → GatedHist) と同じ。"""
    from collections import defaultdict
    random.seed(5)
    log = PerTourLog()
    eids = list(range(1, 60))
    eid_to_tid = {e: 1000 + e for e in eids if e % 7}          # 7 の倍数の大会は無い (行を落とす)
    tid_to_ts = {t: random.choice([10, 20, 20, 30, 40]) for t in eid_to_tid.values()}   # 同時刻あり
    for _ in range(3000):
        log.add(random.randint(1, 80), random.choice(eids), random.random() < 0.5,
                random.uniform(-1, 1), random.uniform(-1, 1), random.uniform(0, 30), random.uniform(0, 30))
    bt_used = defaultdict(dict); rh = defaultdict(list)
    for (uid, eid), (u, a, b, c, d) in log.items():
        tid = eid_to_tid.get(eid)
        if tid is None:
            continue
        bt_used[uid][tid] = bool(u)
        rh[uid].append((tid, float(c), float(a), float(b), float(d)))
    for uid in rh:
        rh[uid].sort(key=lambda e: tid_to_ts.get(e[0], 0))
    ref = GatedHist()
    for uid, hist in rh.items():
        for e in hist:
            ref.add(uid, *e, 1 if bt_used[uid][e[0]] else 0)
    cur = {uid: hist[-1][1] for uid, hist in rh.items()}
    got, got_cur = GatedHist.from_per_tour(log, eid_to_tid, tid_to_ts)
    assert list(got.items()) == list(ref.items()) and list(got_cur.items()) == list(cur.items())
    assert all(type(x) is float for x in got_cur.values())
    assert all(BtUsedView(got)[u] == bt_used[u] for u in rh) and list(BtUsedView(got)) == list(rh)
    assert GatedHist.from_per_tour(PerTourLog(), eid_to_tid, tid_to_ts) is None


def test_per_tour_by_lv_from_contrib_tables():
    """PerTourByLv.from_contrib_tables が「3 段の dict → from_nested」(spsp/build の以前の作り方) と同じ。"""
    import numpy as np
    from collections import defaultdict
    from spsp.learn import ContribTable
    random.seed(6)
    tables = []
    for lv in range(1, 5):
        tab = ContribTable()
        for uid in random.sample(range(1, 300), 120):
            tids = random.sample(range(10_000, 10_060), random.randint(0, 12))
            rows = []
            for t in tids:
                fs = [random.uniform(0, 100) for _ in range(4)]
                if random.random() < 0.1:
                    fs[random.randrange(4)] = np.float64(fs[0] * 1.5)
                rows.append((t, *fs))
            tab.add(uid, rows)
        tables.append(tab)
    thresh = [3, 5, 5, 5]
    nested = defaultdict(lambda: defaultdict(dict))
    for i, tab in enumerate(tables):
        for uid, cs in tab.items():
            for c in cs:
                nested[uid][c['t_id']][i + 1] = [round(c['raw_points'], 4), round(c['pos_factor'], 6),
                                                round(c['weighted'], 4), int(c['rank_in_player'] < thresh[i])]
    ref = PerTourByLv.from_nested(nested)
    got = PerTourByLv.from_contrib_tables(tables, thresh)
    assert list(got) == list(ref) == list(nested)
    assert got.n_tours_players() == (sum(len(t) for t in nested.values()), len(nested))
    for uid, tmap in nested.items():
        g, r = got[uid], ref[uid]
        assert sorted(g) == sorted(r) == sorted(tmap)
        for tid, lvmap in tmap.items():
            a, b = dict(g[tid]), dict(r.get(tid))
            assert list(a.items()) == list(b.items()) == list(lvmap.items())
            assert [type(x) for v in a.values() for x in v] == [type(x) for v in lvmap.values() for x in v]


def test_spr_uf_accumulators():
    """spsp/score の OppCounts・_TourHistoryAcc・番狂わせの上位だけ残す足し方が、以前の dict と同じ結果。"""
    from collections import defaultdict
    from types import SimpleNamespace
    from spsp import score as S
    random.seed(7)
    pairs = [(random.randint(1, 50), random.randint(1, 50)) for _ in range(3000)]
    oc = S.OppCounts(); ref = defaultdict(lambda: defaultdict(int))
    for u, v in pairs:
        oc.add(u, v); ref[u][v] += 1
    want = {u: max(d.values()) for u, d in ref.items() if d}
    got = oc.max_per_player()
    assert list(got.items()) == list(want.items()) and all(type(x) is int for x in got.values())
    oc2 = S.OppCounts()
    for u, v in pairs[:10] + [('x', 1)] + pairs[10:]:
        oc2.add(u, v)
    ref['x'][1] += 1
    assert oc2.max_per_player() == {u: max(d.values()) for u, d in ref.items() if d}
    assert S.OppCounts().max_per_player() == {}
    acc = S._TourHistoryAcc(); href = defaultdict(list)
    for _ in range(2000):
        u, ts, tid = random.randint(1, 80), random.randint(0, 99), random.randint(10**6, 10**6 + 99)
        acc.append(u, ts, tid); href[u].append((ts, tid))
    assert list(acc.result().items()) == list(href.items())
    # 番狂わせ: 上位 TOP_UFS_N 件は全件を並べたときと同じ (同じ UF・同じ日付の並びも)
    top = defaultdict(list); full = defaultdict(list)
    meta = {'event_id': 1, 'tname': 't', 'ename': 'e', 'is_weekend': True}
    for i in range(5000):
        w = random.randint(1, 5)
        m = SimpleNamespace(winner_id=w, loser_id=i, winner_score=1, loser_score=0)
        sw, sl = random.choice([(9, 2), (13, 3), (17, 1), (25, 4), (49, 8)])
        md = dict(meta, date=f'2025-0{random.randint(1, 9)}-1{random.randint(0, 9)}')
        S._record_gated_uf(m, sw, sl, md, {}, top)
        S._record_gated_uf(m, sw, sl, md, {}, None)
        uf = S.placement_to_w2w(sw) - S.placement_to_w2w(sl)
        if uf > 0:
            full[w].append({'uf': uf, 'opp_uid': i, 'date': md['date']})
    for w in full:
        a = [(x['uf'], x['opp_uid'], x['date']) for x in sorted(top[w], key=S.top_uf_key)[:S.TOP_UFS_N]]
        b = [(x['uf'], x['opp_uid'], x['date']) for x in sorted(full[w], key=S.top_uf_key)[:S.TOP_UFS_N]]
        assert a == b and len(top[w]) < 2 * S.TOP_UFS_N


def test_gated_d_lookup():
    """GatedHist.d_lookup が {uid: {tid: float(e[3])}} の .get(uid, {}).get(tid, 0.0) と同じ。"""
    import numpy as np
    from collections import defaultdict
    random.seed(8)
    g = GatedHist()
    tids = [10**12 + 7 * i for i in range(40)]
    ref = defaultdict(dict)
    for uid in random.sample(range(1, 10**6), 60):
        for t in random.sample(tids, random.randint(1, 8)) + [random.choice(tids)]:   # 同じ大会が 2 回あることも
            d = random.uniform(-2, 2)
            g.add(uid, t, 1.0, 0.5, d, 2.0, 1)
            ref[uid][t] = d
    get = g.d_lookup({t: i for i, t in enumerate(tids)})
    for uid in list(ref) + [5, 10**7]:
        for t in tids + [3]:
            want = ref.get(uid, {}).get(t, 0.0)
            assert get(uid, t) == want and type(get(uid, t)) is float
            assert get(np.int64(uid), t) == want


def test_str_per_tour_by_lv_from_contrib_tables():
    """StrPerTourByLv.from_contrib_tables が spsp/cli/build_full._compute_per_tour_by_lv の以前の作り方
    (寄与の dict → {str(uid): {key: {str(lv): [...]}}} → from_nested) と同じ。spsp/learner._add_contribs も見る。"""
    import numpy as np
    from collections import defaultdict
    from spsp.columnar import StrPerTourByLv
    from spsp.learn import ContribTable
    from spsp import learner as LR
    random.seed(9)
    tids = list(range(10**12, 10**12 + 70))
    tid_to_key = {t: (f'cv:{t % 9}:B' if t % 11 == 0 else str(t - 10**12 + 500)) for t in tids if t % 13}
    tables, dicts = [], []
    for lv in range(1, 5):
        tab, dd = ContribTable(), {}
        for uid in random.sample(range(1, 400), 150):
            rows = []
            for t in random.sample(tids, random.randint(0, 10)):
                fs = [random.uniform(0, 50), random.random(), random.choice([1.0, 0.9, 0.81]), random.uniform(0, 40)]
                if random.random() < 0.1:
                    fs[0] = np.float64(fs[0])
                rows.append((t, *fs))
            tab = LR._add_contribs(tab, uid, rows)
            dd = LR._add_contribs(dd, uid, rows)
        assert type(tab) is ContribTable and type(dd) is dict
        tables.append(tab); dicts.append(dd)
    thresh = [3, 5, 5, 5]
    nested = defaultdict(lambda: defaultdict(dict))
    for i, dd in enumerate(dicts):
        for uid, lst in dd.items():
            for c in lst:
                key = tid_to_key.get(c['t_id'])
                if key is None:
                    continue
                nested[str(uid)][key][str(i + 1)] = [round(c['raw_points'], 4), round(c['pos_factor'], 6),
                                                     round(c['weighted'], 4), int(c['rank_in_player'] < thresh[i])]
    nested = {u: dict(d) for u, d in nested.items()}
    ref = StrPerTourByLv.from_nested(nested)
    got = StrPerTourByLv.from_contrib_tables(list(tables), thresh, tid_to_key)
    assert list(got) == list(ref) == list(nested) and got.n_players() == len(nested)
    for u, emap in nested.items():
        assert list(got[u]) == list(ref[u]) == list(emap)
        for k, lvmap in emap.items():
            a = {lv: got[u][k][lv] for lv in got[u][k]}
            assert list(a.items()) == list(lvmap.items())
            assert [type(x) for v in a.values() for x in v] == [type(x) for v in lvmap.values() for x in v]
    # 表せない値 (int) が来たら dict に戻る (それまでの分も同じ中身)
    tab = ContribTable()
    tab = LR._add_contribs(tab, 1, [(5, 1.0, 1.0, 1.0, 2.0)])
    tab = LR._add_contribs(tab, 2, [(6, 1, 1.0, 1.0, 2.0)])
    assert tab == {1: [LR._contrib_dict(5, 1.0, 1.0, 2.0, 0)], 2: [LR._contrib_dict(6, 1, 1.0, 2.0, 0)]}


def test_player_matches_compact_cols():
    """並びを決めた後に列を小さくしても (_compact_cols)、引いた tuple は値も型も同じ (大きい uid・tid でも)。"""
    random.seed(10)
    for big in (False, True):
        tours = [(10**12 + 977 * i, 1_600_000_000 + 3600 * i) for i in range(30)]   # (tid, ts)
        rows = []
        for q in range(2000):
            w, l = random.sample(range(1, 90), 2)
            if big:
                w += 1 << 33
            rows.append((q, w, l, random.choice(tours), random.gauss(0, 1), random.random() < .3, random.random() < .5))
        pm = PlayerMatches()
        want = {}
        for q, w, l, (tid, ts), d, cls, tr in rows:
            mi = pm.add_match(None)
            pm.add(w, ts, tid, l, True, d, cls, mi, tr, seq=2 * q)
            pm.add(l, ts, tid, w, False, -d, cls, mi, not tr, seq=2 * q + 1)
            want.setdefault(w, []).append((ts, tid, l, True, d, cls, None, tr))
            want.setdefault(l, []).append((ts, tid, w, False, -d, cls, None, not tr))
        assert list(pm) == list(want)
        assert pm._tix is not None and (pm._uid.typecode == ('q' if big else 'i'))
        for u, lst in want.items():
            got = pm[u]
            assert got == lst and [tuple(map(type, r)) for r in got] == [tuple(map(type, r)) for r in lst]


if __name__ == '__main__':
    test_player_matches_compact_cols()
    test_str_per_tour_by_lv_from_contrib_tables()
    test_gated_d_lookup()
    test_spr_uf_accumulators()
    test_per_tour_by_lv_from_contrib_tables()
    test_gated_from_per_tour()
    test_scaled_map()
    test_top_ufs()
    test_tour_uid_table()
    test_contrib_table()
    test_tour_entry_store()
    test_int_counts()
    test_str_per_tour_by_lv()
    test_at_time_of_tour()
    test_per_tour_by_lv_and_contrib()
    test_gated_hist_and_deltas()
    test_day_tables()
    test_player_matches()
    test_player_matches_seq()
    test_views_over_log()
    test_series()
    test_columnar()
    test_anchor_table()
    print('ok')

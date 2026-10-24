"""学習ループの並べ替えの近道 (2026-10-17) が、全体を並べ直した結果と同じ並びを返すことのテスト (plain assert)。

- learner._uid_perm: 前回の配列が先頭と同じなら差し込みで作る → np.argsort(stable) と同じ
- learner._rank_desc_then_uid_inc: 前回の並びから値が変わった分だけ差し込む → _rank_desc_then_uid と同じ
- peak_track.PeakTracker._indices: uid の直接表 → 二分探索版と同じ uid → index の対応

選手が後ろに足され、一部の値だけ変わる (学習ループの形) 系列を乱数で作り、同値・-inf・0.0/-0.0 も混ぜる。
"""
import os, sys
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
import spsp.learner as L  # noqa: E402
from spsp.peak_track import PeakTracker  # noqa: E402


def _full_order(uids, vals):
    perm = np.argsort(uids, kind='stable')
    return perm[np.argsort(-vals[perm], kind='stable')]


def test_rank_inc_matches_full():
    rng = np.random.default_rng(12345)
    for trial in range(30):
        L._RANK_INC_CACHE.clear(); L._UID_PERM_CACHE.clear()
        pool = rng.permutation(10 ** 6)[:6000].astype(np.int64)   # uid (重複なし、順不同)
        n = int(rng.integers(1, 50))
        vals = rng.choice([0.0, -0.0, 1.5, 2.0, -np.inf], size=n).astype(np.float64)
        for step in range(60):
            # 追加・変更 (ときどき大量に変えて全体の並べ直しにも入る)
            add = int(rng.integers(0, 40)) if step % 7 else 0
            n2 = min(len(pool), n + add)
            v2 = np.concatenate((vals, rng.choice([0.0, 1.0, 2.0, -np.inf, 3.25], size=n2 - n)))
            k = int(rng.integers(0, max(1, n2 // (3 if step % 5 == 0 else 20))))
            idx = rng.integers(0, n2, size=k)
            v2[idx] = np.where(rng.random(k) < 0.5, rng.choice([0.0, -0.0, 1.0, -np.inf], size=k), rng.normal(size=k))
            uids = pool[:n2].copy()          # 毎回新しい配列 (学習ループと同じ)
            vals = v2
            perm = L._uid_perm(uids)
            assert np.array_equal(perm, np.argsort(uids, kind='stable')), (trial, step)
            got = L._rank_desc_then_uid_inc('t', uids, vals.copy(), perm)
            want = _full_order(uids, vals)
            assert np.array_equal(got, want), (trial, step, n2)
            assert np.array_equal(got, L._rank_desc_then_uid(vals, perm))
            n = n2
        # 先頭が違う配列 (並べ替え・削除) では全体を並べ直す
        uids = pool[:n][::-1].copy()
        got = L._rank_desc_then_uid_inc('t', uids, vals[::-1].copy(), L._uid_perm(uids))
        assert np.array_equal(got, _full_order(uids, vals[::-1].copy()))


def test_peak_track_indices_table_matches_bsearch():
    rng = np.random.default_rng(7)
    a = PeakTracker(); b = PeakTracker()
    for step in range(200):
        u = rng.integers(0, 5000, size=int(rng.integers(1, 300))).astype(np.int64)
        ia = a._indices(u)                    # 表 (uid < 2^26)
        ib = b._indices_bsearch(u)            # 二分探索
        assert np.array_equal(ia, ib), step
        assert a.n == b.n and np.array_equal(a.uids[:a.n], b.uids[:b.n])
    # 表の上限を超える uid が来たら二分探索に切り替わり、それでも同じ対応
    big = np.array([3, (1 << 26) + 5, 17], dtype=np.int64)
    assert np.array_equal(a._indices(big), b._indices_bsearch(big))
    u = rng.integers(0, 5000, size=100).astype(np.int64)
    assert np.array_equal(a._indices(u), b._indices_bsearch(u))


def _ref_cascade(scores, peak, measured):
    """_pt_cascade_inc の仕様どおりの参照実装 (lexsort だけ。2026-10-17 以前の各段の並べ方)。"""
    sc1 = scores[1]
    keys0 = list(sc1); n0 = len(keys0)
    uids0 = np.array(keys0, dtype=np.int64)
    gray0 = np.array([(u in L.OVERSEAS_UIDS) or (u not in measured) for u in keys0], dtype=bool)
    pka0 = np.array([peak.get(u, float('-inf')) for u in keys0], dtype=np.float64)
    pmax = np.zeros(n0, dtype=np.int64); pens = np.zeros(n0, dtype=np.int64); ptj = np.zeros(n0)
    cur = np.arange(n0)
    for lv, _tf, _lp, topn_next in L.LV_DEFS:
        if len(cur) == 0:
            break
        ua = uids0[cur]; m = len(cur)
        tja = np.array([scores[lv].get(int(u), 0.0) for u in ua], dtype=np.float64)
        pka = pka0[cur]; seq = np.arange(1, m + 1)
        btr = np.empty(m, dtype=np.int64); tjr = np.empty(m, dtype=np.int64); er = np.empty(m, dtype=np.int64)
        btr[np.lexsort((ua, -pka))] = seq
        tjr[np.lexsort((ua, -tja))] = seq
        er[np.lexsort((ua, tjr, btr, (btr + tjr) / 2.0))] = seq
        pmax[cur] = lv; pens[cur] = er; ptj[cur] = tja
        if topn_next is not None:
            base = tjr if L.PFILTER_MODE == 'tjpr_only' else er
            order = np.argsort(base, kind='stable')
            ng = (~gray0[cur])[order]
            cur = cur[order[(np.cumsum(ng) - ng) < topn_next]]
    return pmax, pens, ptj, gray0


def test_cascade_and_display_match_lexsort_reference():
    """選手を足しながら一部のスコア・peak を変える系列で、_pt_cascade_inc と _pt_display_ranks が
    lexsort の参照実装と同じ値を返すこと (Lv2 以上の大きい段の近道・差分の並べ替えも通る大きさにする)。"""
    rng = np.random.default_rng(99)
    L._RANK_INC_CACHE.clear(); L._UID_PERM_CACHE.clear()
    lvs = [d[0] for d in L.LV_DEFS]
    scores = {lv: {} for lv in lvs}
    peak = {}
    pool = rng.permutation(10 ** 6)[:6000].tolist()
    nxt = 0
    for step in range(40):
        for _ in range(int(rng.integers(50, 400)) if step < 20 else int(rng.integers(0, 30))):
            if nxt >= len(pool):
                break
            u = pool[nxt]; nxt += 1
            scores[1][u] = float(rng.choice([0.0, rng.random() * 10]))
            if rng.random() < 0.8:
                peak[u] = float(rng.choice([1.0, 2.0, rng.normal()]))
        keys = list(scores[1])
        for u in rng.choice(keys, size=min(len(keys), 60), replace=False).tolist():
            scores[1][u] = float(rng.choice([0.0, 3.0, rng.random() * 10]))
            for lv in lvs[1:]:
                if rng.random() < 0.5:
                    scores[lv][u] = float(rng.choice([0.0, rng.random() * 5]))
            if rng.random() < 0.5:
                peak[u] = float(rng.choice([2.0, rng.normal()]))
        measured = set(rng.choice(keys, size=len(keys) // 3, replace=False).tolist())
        pmax_d, arrays = L._pt_cascade_inc(scores, peak, measured)
        uids0, pmax, pens, ptj, gray0 = arrays
        r_pmax, r_pens, r_ptj, r_gray = _ref_cascade(scores, peak, measured)
        assert np.array_equal(pmax, r_pmax) and np.array_equal(pens, r_pens), step
        assert np.array_equal(ptj, r_ptj) and np.array_equal(gray0, r_gray), step
        assert pmax_d == dict(zip(uids0.tolist(), r_pmax.tolist()))
        got = L._pt_display_ranks(arrays, peak, measured)
        # 表示順位の参照: ens = lexsort((uid, pens, -lv))、tjpr = lexsort((uid, -ptj))、bt = lexsort((uid, -peak)) (peak のキー上)
        ens_o = np.lexsort((uids0, r_pens, -r_pmax)); tj_o = np.lexsort((uids0, -r_ptj))
        pua = np.array(list(peak), dtype=np.int64); pva = np.array(list(peak.values()))
        pg = np.array([(u in L.OVERSEAS_UIDS) or (u not in measured) for u in peak], dtype=bool)
        bt_o = np.lexsort((pua, -pva))
        ens_jp = L._pt_jp_from_order(ens_o, r_gray[ens_o])
        caps = np.array([L._PT_LV_CAP.get(int(lv), 0) or 0 for lv in r_pmax], dtype=np.int64)
        mm = r_gray & (r_pmax >= 2) & (caps > 0) & (ens_jp > caps)
        ens_jp[mm] = caps[mm]
        assert np.array_equal(got[2], ens_jp), step
        assert np.array_equal(got[3], L._pt_jp_from_order(tj_o, r_gray[tj_o])), step
        assert np.array_equal(got[4], pua) and np.array_equal(got[5], L._pt_jp_from_order(bt_o, pg[bt_o])), step


if __name__ == '__main__':
    test_rank_inc_matches_full()
    test_peak_track_indices_table_matches_bsearch()
    test_cascade_and_display_match_lexsort_reference()
    print('test_incremental_sorts: ok')

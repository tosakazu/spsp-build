"""spsp/peak_track.PeakTracker (過去最高順位の追跡) のテスト。

ランダムな順位の列を流し、素朴な計算 (選手ごとに全点の最小、直近 53 週の最小、同順位なら最後の日) と一致するか。
選手の集合は途中で増える (uid の追加 = 配列の伸長) ので、その経路も踏む。
level_tier_values (numpy 版) が _level_tier_achievement (スカラー版) と一致することも確かめる。

実行: OPENBLAS_NUM_THREADS=1 .venv/bin/python3 tests/meta/test_peak_track.py
(リポジトリルートから。unittest 等は使わず plain assert)
"""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
import numpy as np  # noqa: E402
from spsp.peak_track import PeakTracker, WEEKS  # noqa: E402
from spsp.score import level_tier_values, tier_achievement_from_value, _level_tier_achievement  # noqa: E402

random.seed(11)
tr = PeakTracker()
truth = {}   # uid -> list of (day, ens, tj, lv, tier, bt)
day0 = 738000
uids_all = list(range(1000, 1400))
for step in range(300):
    day = day0 + step * 3 + random.randint(0, 2)            # 約 900 日 (= 53 週の窓から落ちる点がある)
    n = min(len(uids_all), 50 + step * 2)                    # 選手は徐々に増える
    uids = uids_all[:n]
    random.shuffle(uids)
    ens = np.array([random.randint(1, 3000) for _ in uids])
    tj = np.array([random.randint(0, 3000) for _ in uids])   # 0 = 順位なし
    lv = np.array([random.randint(1, 5) for _ in uids])
    tier = level_tier_values(lv, ens)
    bt_n = random.randint(0, n)
    bt_uids = random.sample(uids, bt_n)
    bt = np.array([random.randint(1, 2000) for _ in bt_uids])
    tr.update(day * 86400, day, np.array(uids), ens, lv, tj, tier, np.array(bt_uids), bt)
    for u, e, t, l, tv in zip(uids, ens, tj, lv, tier):
        truth.setdefault(u, []).append((day, int(e), int(t), int(l), int(tv), None))
    for u, b in zip(bt_uids, bt):
        truth.setdefault(u, []).append((day, None, None, None, None, int(b)))

now_day = day + 5
snap = tr.snapshot(now_day)
assert set(snap) == set(truth), '選手集合が違う'


def best_of(vals, window=None):
    """(day, rank) の列 → (最小 rank, それを最後に達成した日)。
    window=True は追跡器の 1 年の規則: 週ごとに (最小 rank, それを最後に達成した日) を取り、その日が
    now_day-365 以降の週だけを集めて最小 (同順位なら最後の日)。"""
    pts = [(d, r) for d, r in vals if r is not None and r > 0]
    if not pts:
        return None
    if window:
        weeks = {}
        for d, r in pts:
            w = d // 7
            cur = weeks.get(w)
            if cur is None or r < cur[0] or (r == cur[0] and d > cur[1]):
                weeks[w] = (r, d)
        pts = [(d, r) for (r, d) in weeks.values() if d >= now_day - 365]
        if not pts:
            return None
    m = min(r for _, r in pts)
    return (m, max(d for d, r in pts if r == m))


for u, rows in truth.items():
    rec = snap[u]
    for key, col in (('ensemble', 1), ('tjpr', 2), ('bt_gated', 5)):
        vals = [(r[0], r[col]) for r in rows]
        assert rec[key] == best_of(vals), f'{u} {key}: {rec[key]} != {best_of(vals)}'
        assert rec[key + "_1y"] == best_of(vals, window=True), f'{u} {key}_1y: {rec[key + "_1y"]} != {best_of(vals, window=True)}'
    assert rec['lv_best'] == max(r[3] for r in rows if r[3] is not None), f'{u} lv_best'
    tb = max((r[4] for r in rows if r[4] is not None), default=0)
    assert rec['tier_best'] == tb, f'{u} tier_best {rec["tier_best"]} != {tb}'
    if tb:
        assert rec['tier_day'] == min(r[0] for r in rows if r[4] == tb), f'{u} tier_day'

# ティア: numpy 版とスカラー版
for lv in range(0, 6):
    for rk in (0, 1, 8, 9, 16, 17, 32, 33, 64, 65, 128, 129, 384, 385, 768, 769, 1536, 1537, 3072, 3073, 9999):
        a = _level_tier_achievement(lv, rk)
        v = int(level_tier_values([lv], [rk])[0])
        assert (a['_v'] if a else 0) == v, (lv, rk, a, v)
        if a:
            assert tier_achievement_from_value(v) == {k: x for k, x in a.items() if k != '_v'}, (lv, rk)

# pickle 往復 (チェックポイントに入る)
import pickle  # noqa: E402
tr2 = pickle.loads(pickle.dumps(tr))
assert tr2.snapshot(now_day) == snap
print(f'test_peak_track: all assertions passed ({len(truth)} players, {tr.n_updates} updates, ring {WEEKS} weeks)')

"""ランダムシード大会の判定 (2026-10-12〜)。

シードが強さ順に付いていない大会 (ランダム・登録順など) では、シードを基準にした指標 (UF = アップセット、
SPR = シードに対する順位の上振れ) に意味がないので、集計から外す。

判定: 大会開始時点の総合評価の全国順位 (pt_at_time の ens) を持つ参加者が RANDOM_SEED_MIN_RATED 人以上いて、
その人たちのシード順と順位の順位相関 (Spearman) が RANDOM_SEED_MAX_R 未満ならランダムシード。
大会時点の総合評価が無い大会・ビルド (pt_at_time なし) では、大会開始時点の直対評価のレート (bt_at_t) で代える
(初参加の人 = レートが初期値のままは数えない)。

検証 (2026-10-12, 2018〜2026-08 の 3,985 イベント): 普通にシードされた大会 (16 人以上で相関 0.8 以上) から
5 人を無作為に抜いても相関 0.3 未満になるのは 2.1%、8 人で 0.5%。総合評価の順位はレートよりシードとの
相関が少し高い (中央値 0.879 / 0.863) ので、こちらを使う。
"""
from __future__ import annotations

from spsp.common import ELO_PER_UNIT, TJPR_DEFAULT_RATE
from spsp.meta import load_event_seeds

RANDOM_SEED_MIN_RATED = 5
RANDOM_SEED_MAX_R = 0.3
_NEWCOMER_ORD = TJPR_DEFAULT_RATE / ELO_PER_UNIT   # 学習の初期レート 1500 (learner._get) のまま


def _ranks(vals: list[float]) -> list[float]:
    """同順位は平均順位。"""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    r = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2
        i = j + 1
    return r


def spearman(xs: list[float], ys: list[float]) -> float | None:
    rx, ry = _ranks(xs), _ranks(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    return sxy / (sxx * syy) ** 0.5


def seed_strength_corr(seeds: dict[int, int], strength: dict[int, float]) -> tuple[int, float | None]:
    """(強さが分かるシード済み参加者の数, シード順と強さ順の相関)。strength は小さいほど強い。"""
    known = [u for u in seeds if u in strength]
    if len(known) < 2:
        return len(known), None
    return len(known), spearman([seeds[u] for u in known], [strength[u] for u in known])


def _strength_at(tid, pt_at_time, bt_at_t, meta=None) -> dict[int, float]:
    anchors = pt_at_time.get(tid) if pt_at_time is not None else None
    if meta and (meta.get('is_non_serious') or meta.get('is_gf_missing') or meta.get('is_small')):
        anchors = None   # 集計対象外の大会のアンカー (2026-10-22 から) は使わず、従来どおりレートで判定する
    if anchors:   # 総合評価の対象外の大会 (身内・特殊ルール等) にはアンカーが無いのでレートで代える
        return {u: a[1] for u, a in anchors.items() if a[1] is not None}   # a = [lv, ens 全国順位, ...]
    ords = bt_at_t.get(tid) or {}
    return {u: -o for u, o in ords.items() if abs(o - _NEWCOMER_ORD) > 1e-9}


def mark_random_seed(all_tournaments, bt_at_t: dict, t_meta_by_id: dict, pt_at_time: dict | None = None) -> list:
    """ランダムシードの大会の meta に is_random_seed=True を付け、その大会のリストを返す。"""
    out = []
    for t in all_tournaments:
        strength = _strength_at(id(t), pt_at_time, bt_at_t, t_meta_by_id.get(id(t)))
        if not strength:
            continue
        seeds = load_event_seeds(t.event_path)
        if not seeds:
            continue
        n, r = seed_strength_corr(seeds, strength)
        if n >= RANDOM_SEED_MIN_RATED and r is not None and r < RANDOM_SEED_MAX_R:
            t_meta_by_id[id(t)]['is_random_seed'] = True
            out.append(t)
    return out

"""TJPR カスケード計算と大会メタ情報 (本番ビルド v4/learner.py が使うもの)。

以前は eval/baseline_repro/eval_tjpr_size_correction.py と eval_info_ensemble.py
(精度実験スクリプト) から import していた。本番が実験コードに依存しない形にするため、
本番が使う関数だけをここに移した (2026-09)。関数本体は移動元と同一。
filter_dq_no_shows / EVENTS_ROOT は v4.tournament_utils にある (ここから再 export)。
placement_to_w2w は v4.common 版 (eval/spr_uf_analysis.py 版と同値) を使う。
eval/ 側の複製は ⑧ (重複統合) で片付ける。
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

import numpy as np

from spsp.common import (
    td_exp,
    LV1_PARAMS,
    LV2_PARAMS,
    LV3_FILTER,
    LV4_FILTER,
    LV5_FILTER,
    placement_to_w2w,
)
from spsp.meta import (
    tournament_is_class_virtual,
    tournament_event_id,
    tournament_parent_event_id,
    tournament_class_letter,
)
from spsp.tournament_utils import (
    REPO,
    EVENTS_ROOT,
    filter_dq_no_shows,
    tournament_display_name,
)

from spsp.paths import SITE_DIR
from spsp import derived

TOURNAMENT_DIR = SITE_DIR / "tournaments"

__all__ = [
    "EVENTS_ROOT", "TOURNAMENT_DIR", "TOP_K", "EPS_G",
    "filter_dq_no_shows", "build_t_meta", "load_attendees", "seeds_from_rank", "metrics_full",
    "compute_bz_sized", "build_tjpr_sized", "compute_bz_general", "build_tjpr_general",
    "rv_default", "sm_one",
]


# ── 大会メタ (旧 eval_tjpr_size_correction.py) ──
def _tournament_dir_of(event_path):
    # v4.meta.build_tournament_metadata と同一規則 (同時開催の制限巻き込まれ解除用)
    p = Path(event_path)
    if p.parent.name == "class_phases":
        p = p.parent.parent
    elif p.name.endswith("_virtual"):
        p = p.parent
    return str(p.parent)


def build_t_meta(all_tournaments):
    from spsp.tournament_utils import load_awaiting_resume_ids as _lar
    _awaiting_ids = _lar()
    t_meta = {}
    # pre-pass: ename 自体が制限マッチするイベントを持つ大会 dir (v4.meta と同一規則)
    disp_by_path = {}
    ename_restricted_tdirs = set()
    for t in all_tournaments:
        dsp = tournament_display_name(t.event_path)
        disp_by_path[t.event_path] = dsp
        if derived.names(t.event_path)['restricted_ename']:
            ename_restricted_tdirs.add(_tournament_dir_of(t.event_path))
    for t in all_tournaments:
        tname, ename, nent_attr = disp_by_path[t.event_path]
        cur = derived.load(t.event_path)
        nm = cur['names']
        has_gf = cur['results']['has_gf_recorded']
        is_class_v = tournament_is_class_virtual(t.event_path)
        has_de_phase = cur['results']['has_de_phase']
        nent = len(t.standings) or nent_attr or 0
        is_gf_missing = (has_gf is False and (nent_attr or 0) >= 8 and not is_class_v and has_de_phase)
        if is_gf_missing and t.event_id in _awaiting_ids:   # 再開待ち (build/data/awaiting_resume.json)
            is_gf_missing = False
        d = dt.datetime.fromtimestamp(t.timestamp).date()
        end_d = dt.datetime.fromtimestamp(t.end_timestamp or t.timestamp).date()
        is_wk_real = cur['calendar']['is_weekend_real']
        is_pre = nm['pre']
        is_non_serious = nm['non_serious']
        is_restricted = nm['restricted_tname'] or nm['restricted_ename']
        if (is_restricted and not nm['restricted_ename']
                and _tournament_dir_of(t.event_path) in ename_restricted_tdirs):
            is_restricted = False  # 制限は同時開催の ename 制限イベント側 (v4.meta と同一規則)
        is_lower_class = nm['lower_class']
        if is_class_v:
            is_lower_class = True
        is_wk = is_wk_real and not is_pre
        t_meta[id(t)] = {
            'event_id': tournament_event_id(t.event_path),
            'nent': nent,
            'tname': tname or '', 'ename': ename or '',
            'is_smapa': nm['smapa'],
            'is_weekend': is_wk, 'is_pre': is_pre,
            'is_non_serious': is_non_serious,
            'is_restricted': is_restricted, 'is_lower_class': is_lower_class,
            'is_class_virtual': is_class_v,
            'parent_event_id': tournament_parent_event_id(t.event_path) if is_class_v else None,
            'is_gf_missing': is_gf_missing,
        }
    return t_meta


# ── 大会ごとの参加者・シード・精度指標 (旧 eval_tjpr_size_correction.py) ──
def load_attendees(eid):
    p = TOURNAMENT_DIR / f"{eid}.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text())
    placed = {}
    for s in d.get("standings") or []:
        uid = s.get("user_id")
        place = s.get("place")
        if uid is None or place is None:
            continue
        if uid not in placed:
            placed[uid] = place
    if len(placed) < 2:
        return None
    return list(placed.keys()), placed


def seeds_from_rank(attendees, rank_map):
    rated, unrated = [], []
    for u in attendees:
        if u in rank_map:
            rated.append((rank_map[u], u))
        else:
            unrated.append(u)
    rated.sort()
    unrated.sort()
    out = {}; s = 1
    for _, u in rated: out[u] = s; s += 1
    for u in unrated: out[u] = s; s += 1
    return out


TOP_K = [8, 16, 32, 64, 128]


def metrics_full(attendees, placed, seeds):
    N = len(attendees)
    correct = total = 0
    topk_pa = {K: [0, 0] for K in TOP_K}
    for i in range(N):
        for j in range(i + 1, N):
            a, b = attendees[i], attendees[j]
            pa, pb = placed[a], placed[b]
            sa, sb = seeds[a], seeds[b]
            if pa == pb or sa == sb:
                continue
            total += 1
            ok = (pa < pb) == (sa < sb)
            if ok:
                correct += 1
            mp = min(pa, pb)
            for K in TOP_K:
                if mp <= K:
                    topk_pa[K][1] += 1
                    if ok:
                        topk_pa[K][0] += 1
    pairwise = correct / total if total else float("nan")
    spr_vals_all = [abs(placement_to_w2w(seeds[u]) - placement_to_w2w(placed[u])) for u in attendees]
    spr_abs = float(np.mean(spr_vals_all))
    topk_pa_out = {K: (topk_pa[K][0] / topk_pa[K][1]) if topk_pa[K][1] else None
                   for K in TOP_K}
    topk_spr_out = {}
    for K in TOP_K:
        vals = [abs(placement_to_w2w(seeds[u]) - placement_to_w2w(placed[u]))
                for u in attendees if placed[u] <= K]
        topk_spr_out[K] = float(np.mean(vals)) if vals else None
    return {
        "pairwise": pairwise,
        "spr_abs": spr_abs,
        **{f"top{K}_pa": v for K, v in topk_pa_out.items()},
        **{f"top{K}_spr": v for K, v in topk_spr_out.items()},
    }


# ── TJPR カスケード: 人数補正版 (旧 eval_tjpr_size_correction.py) ──
def compute_bz_sized(cache, base, floor, topN, pd, size_mult_fn,
                     pfilter=None, tfilter=None):
    eps = 0.01
    pe = defaultdict(list)
    for t in cache:
        if tfilter is not None:
            if tfilter.get('min_nent_gt') is not None and t['nent'] <= tfilter['min_nent_gt']:
                continue
            if tfilter.get('exclude_smapa') and t['is_smapa']:
                continue
            if tfilter.get('weekend_only') and not t['is_weekend']:
                continue
            if tfilter.get('exclude_capped') and t.get('is_capped'):
                continue
        age_decay = td_exp(t['days'])
        clamped = np.maximum(t['norm'], floor)
        rp = []
        for idx in t['rounds']:
            if len(idx):
                r = clamped[idx]
                g = 1.0 - 1.0 / (1.0 + np.power(10.0, (r - base) / 400.0))
                g = np.maximum(0, g)
                # info content (= v2 build と同式): -log2(1-g) の平均
                rp.append(-np.log2(np.clip(1.0 - g, 1e-3, 1.0)).mean())
            else:
                rp.append(eps)
        size_w = size_mult_fn(t['nent'])
        for uid, rw, place in t['players']:
            if pfilter is not None and uid not in pfilter:
                continue
            raw_pts = sum(rp[:rw])
            if raw_pts > 0:
                pe[uid].append((raw_pts * size_w, age_decay, t['t_id'], place))
    scores = {}
    for uid, entries in pe.items():
        entries.sort(key=lambda x: -(x[0] * x[1]))
        total = 0.0
        for i, (raw_pts, age_decay, t_id, place) in enumerate(entries):
            pos_factor = 1.0 if i < topN else pd ** (i - topN + 1)
            total += raw_pts * age_decay * pos_factor
        scores[uid] = total
    return scores


def build_tjpr_sized(cache, size_mult_fn):
    """5-cascade with size_mult applied at each level."""
    sc1 = compute_bz_sized(cache, *LV1_PARAMS, size_mult_fn=size_mult_fn)
    filt2 = set(uid for uid, _ in sorted(sc1.items(), key=lambda x: -x[1])[:2048])
    sc2 = compute_bz_sized(cache, *LV2_PARAMS, size_mult_fn=size_mult_fn, pfilter=filt2)
    filt3 = set(uid for uid, _ in sorted(sc2.items(), key=lambda x: -x[1])[:1024])
    sc3 = compute_bz_sized(cache, *LV2_PARAMS, size_mult_fn=size_mult_fn, pfilter=filt3, tfilter=LV3_FILTER)
    filt4 = set(uid for uid, _ in sorted(sc3.items(), key=lambda x: -x[1])[:512])
    sc4 = compute_bz_sized(cache, *LV2_PARAMS, size_mult_fn=size_mult_fn, pfilter=filt4, tfilter=LV4_FILTER)
    filt5 = set(uid for uid, _ in sorted(sc4.items(), key=lambda x: -x[1])[:256])
    sc5 = compute_bz_sized(cache, *LV2_PARAMS, size_mult_fn=size_mult_fn, pfilter=filt5, tfilter=LV5_FILTER)

    # Final cascade rank: take max level per uid (= as in build_tjpr_v1)
    # Actually build_tjpr_v1 returns rank by max-level, breaking by score.
    # For simplicity we use the highest-Lv score that contains the uid.
    final_scores = {}
    final_levels = {}
    for uid, s in sc1.items():
        final_scores[uid] = s; final_levels[uid] = 1
    for src, lv in [(sc2, 2), (sc3, 3), (sc4, 4), (sc5, 5)]:
        for uid, s in src.items():
            if lv > final_levels.get(uid, 0):
                final_levels[uid] = lv; final_scores[uid] = s
            elif lv == final_levels.get(uid):
                final_scores[uid] = s
    sorted_u = sorted(final_scores.items(), key=lambda x: (-final_levels[x[0]], -x[1]))
    ranks = {uid: i + 1 for i, (uid, _) in enumerate(sorted_u)}
    return ranks


# ── TJPR カスケード: round_value / size_mult を差し替えられる版 (旧 eval_info_ensemble.py) ──
def compute_bz_general(cache, base, floor, topN, pd,
                        round_value_fn, size_mult_fn,
                        pfilter=None, tfilter=None):
    pe = defaultdict(list)
    for t in cache:
        if tfilter is not None:
            if tfilter.get('min_nent_gt') is not None and t['nent'] <= tfilter['min_nent_gt']:
                continue
            if tfilter.get('exclude_smapa') and t['is_smapa']:
                continue
            if tfilter.get('weekend_only') and not t['is_weekend']:
                continue
            if tfilter.get('exclude_capped') and t.get('is_capped'):
                continue
        age_decay = td_exp(t['days'])
        clamped = np.maximum(t['norm'], floor)
        rp = []
        for idx in t['rounds']:
            if len(idx):
                r = clamped[idx]
                g = 1.0 - 1.0 / (1.0 + np.power(10.0, (r - base) / 400.0))
                g = np.maximum(0, g)
                rp.append(round_value_fn(g))
            else:
                rp.append(round_value_fn(np.array([0.0])))
        size_w = size_mult_fn(t['nent'])
        for uid, rw, place in t['players']:
            if pfilter is not None and uid not in pfilter:
                continue
            raw_pts = sum(rp[:rw])
            if raw_pts > 0:
                pe[uid].append((raw_pts * size_w, age_decay, t['t_id'], place))
    scores = {}
    for uid, entries in pe.items():
        entries.sort(key=lambda x: -(x[0] * x[1]))
        total = 0.0
        for i, (raw_pts, age_decay, t_id, place) in enumerate(entries):
            pos_factor = 1.0 if i < topN else pd ** (i - topN + 1)
            total += raw_pts * age_decay * pos_factor
        scores[uid] = total
    return scores


def build_tjpr_general(cache, round_value_fn, size_mult_fn):
    sc1 = compute_bz_general(cache, *LV1_PARAMS, round_value_fn=round_value_fn,
                              size_mult_fn=size_mult_fn)
    filt2 = set(uid for uid, _ in sorted(sc1.items(), key=lambda x: -x[1])[:2048])
    sc2 = compute_bz_general(cache, *LV2_PARAMS, round_value_fn=round_value_fn,
                              size_mult_fn=size_mult_fn, pfilter=filt2)
    filt3 = set(uid for uid, _ in sorted(sc2.items(), key=lambda x: -x[1])[:1024])
    sc3 = compute_bz_general(cache, *LV2_PARAMS, round_value_fn=round_value_fn,
                              size_mult_fn=size_mult_fn, pfilter=filt3, tfilter=LV3_FILTER)
    filt4 = set(uid for uid, _ in sorted(sc3.items(), key=lambda x: -x[1])[:512])
    sc4 = compute_bz_general(cache, *LV2_PARAMS, round_value_fn=round_value_fn,
                              size_mult_fn=size_mult_fn, pfilter=filt4, tfilter=LV4_FILTER)
    filt5 = set(uid for uid, _ in sorted(sc4.items(), key=lambda x: -x[1])[:256])
    sc5 = compute_bz_general(cache, *LV2_PARAMS, round_value_fn=round_value_fn,
                              size_mult_fn=size_mult_fn, pfilter=filt5, tfilter=LV5_FILTER)

    final_scores = {}
    final_levels = {}
    for uid, s in sc1.items():
        final_scores[uid] = s; final_levels[uid] = 1
    for src, lv in [(sc2, 2), (sc3, 3), (sc4, 4), (sc5, 5)]:
        for uid, s in src.items():
            if lv > final_levels.get(uid, 0):
                final_levels[uid] = lv; final_scores[uid] = s
            elif lv == final_levels.get(uid):
                final_scores[uid] = s
    sorted_u = sorted(final_scores.items(), key=lambda x: (-final_levels[x[0]], -x[1]))
    return {uid: i + 1 for i, (uid, _) in enumerate(sorted_u)}


EPS_G = 0.01

def rv_default(g): return EPS_G + g.mean()

def sm_one(n):      return 1.0

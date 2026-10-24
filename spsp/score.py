"""V4 スコア・ランク変換モジュール.

build_tjpr_ranking.py から JJPR / BT 表示変換 / アンサンブル / snapshot 計算を移動.

依存:
  - v4.common: ELO_PER_UNIT, BT_ELO_OFFSET, TJPR_DEFAULT_RATE, TJPR_ELO_SCALE,
               TJPR_ELO_OFFSET, GATED_M_TOUR, SIGMA_FLOOR, _common.BT_PEAK_WINDOW_SEC,
               LOOKBACK_DAYS, JJPR_LOOKBACK_DAYS, JJPR_TOP_K, _bracket_order_key
  - v4.learn: Peak180Tracker, precompute_banzuke, build_tjpr_v1_with_detail,
              _apply_inactivity_sigma, _apply_sigma_floor_keep_ordinal
  - v4.raters.openskill_rater: OpenSkillBTRater
  - v4.tournament_utils: jjpr_point
"""
from __future__ import annotations

import datetime as dt
import time
from collections import defaultdict

import numpy as np

from spsp import common as _common  # BT_PEAK_WINDOW_SEC は configure() で変わりうるので属性参照
from spsp.common import LV_PLUS_CUTOFF, level_tier_values, _rolling_lv2_raw_per_tour  # noqa: F401 (学習側へ移した。spsp/common.py)
from spsp.common import (
    ELO_PER_UNIT, BT_ELO_OFFSET,
    OVERSEAS_UIDS,
    TJPR_DEFAULT_RATE, TJPR_ELO_SCALE, TJPR_ELO_OFFSET,
    GATED_M_TOUR, SIGMA_FLOOR,
    LOOKBACK_DAYS, JJPR_LOOKBACK_DAYS, JJPR_TOP_K,
    SNAPSHOT_DAYS,
    LV2_PARAMS,
    _bracket_order_key,
    placement_to_w2w,
    compute_losers_runs,
)
from spsp.learn import (
    Peak180Tracker,
    precompute_banzuke, build_tjpr_v1_with_detail,
    _apply_inactivity_sigma, _apply_sigma_floor_keep_ordinal,
)
from spsp.meta import tournament_award_label, tournament_individual_label, load_event_seeds
from spsp.raters.openskill_rater import OpenSkillBTRater
from spsp.tournament_utils import jjpr_point
from spsp.rolling import rolling_raw_at
from spsp.columnar import GatedHist, TourHistory, TourUidTable
from array import array

import re as _re
from collections.abc import Mapping


# ─── from build_tjpr_ranking (167-202): build_jjpr_v3_with_detail ───
# ── JJPR v3 ──
def build_jjpr_v3_with_detail(prior, eval_date, t_meta_by_id, top_k=6):
    player_entries = defaultdict(list)
    for t in prior:
        meta = t_meta_by_id[id(t)]
        if meta.get('is_non_serious') or meta.get('is_gf_missing') or meta.get('is_small'): continue
        # JJPR は class virtual を除外 (legacy 比較用なので元データのみ)
        if meta.get('is_class_virtual'): continue
        tdate = dt.datetime.fromtimestamp(t.timestamp)
        if tdate > eval_date:
            continue
        days_elapsed = (eval_date - tdate).days
        seen = set()
        for place, uid in t.standings:
            if uid in seen:
                continue
            seen.add(uid)
            pt = jjpr_point(meta['nent'], place, days_elapsed)
            if pt > 0:
                player_entries[uid].append((pt, days_elapsed, id(t), place))
    scores = {}
    contribs = defaultdict(list)
    for uid, entries in player_entries.items():
        entries.sort(key=lambda x: -x[0])
        total = 0.0
        for i, (pt, days, t_id, place) in enumerate(entries):
            counted = i < top_k
            if counted:
                total += pt
            contribs[uid].append({'t_id': t_id, 'points': pt, 'counted': counted, 'place': place})
        scores[uid] = total
    sorted_u = sorted(scores.items(), key=lambda x: -x[1])
    ranks = {u: r + 1 for r, (u, _) in enumerate(sorted_u)}
    return ranks, scores, contribs




# ─── from build_tjpr_ranking (203-243): normalize_bt, bt_to_elo_display, rank_avg, to_rank ───
# ── Normalize ──
# normalize_bt: TJPR の Banzuke gain 入力用に max=2500 シフト (順位に影響しない)
def normalize_bt(bt_ord):
    if not bt_ord:
        return {}
    elo = {uid: r * ELO_PER_UNIT for uid, r in bt_ord.items()}
    max_r = max(elo.values())
    return {uid: r - (max_r - 2500.0) for uid, r in elo.items()}


# bt_to_elo_display: サイト表示用の Elo 線形変換 (ordinal × 29.48 + BT_ELO_OFFSET)
def bt_to_elo_display(bt_ord):
    return {uid: r * ELO_PER_UNIT + BT_ELO_OFFSET for uid, r in bt_ord.items()}


def rank_avg(ranks_list, all_uids, score_lists=None, return_metrics=False):
    """順位平均で sort。score_lists が与えられた場合、tie は score 平均 (大きい順) で解消.

    return_metrics=True で (ranks, avg_rank, avg_score) を返す。
    """
    default_rank = len(all_uids) + 1
    avg_rank = {}
    for u in all_uids:
        avg_rank[u] = sum(rk.get(u, default_rank) for rk in ranks_list) / len(ranks_list)
    avg_score = {}
    if score_lists:
        for u in all_uids:
            avg_score[u] = sum(sc.get(u, 0.0) for sc in score_lists) / len(score_lists)
        sorted_u = sorted(all_uids, key=lambda u: (avg_rank[u], -avg_score[u]))
    else:
        sorted_u = sorted(all_uids, key=lambda u: avg_rank[u])
    ranks = {u: i + 1 for i, u in enumerate(sorted_u)}
    if return_metrics:
        return ranks, avg_rank, avg_score
    return ranks


def to_rank(ratings):
    sorted_u = sorted(ratings.items(), key=lambda x: -x[1])
    return {u: r + 1 for r, (u, _) in enumerate(sorted_u)}



# ─── from build_tjpr_ranking (245-368): compute_ranks_at ───
# ── Snapshot computation (light, no per-tournament details) ──
def compute_ranks_at(all_tournaments, snapshot_date, t_meta_by_id, bt_at_t, global_shift,
                     bt_all_at_snap=None, bt_internal_at_snap=None, peak180_at_snap=None):
    """指定 snapshot_date における 4 種類のランク + TJPR/BT スコアを返す.

    bt_at_t, global_shift は eval 時点で計算した「大会別 BT snapshot」と「正規化シフト」.
    bt_all_at_snap / bt_internal_at_snap / peak180_at_snap は eval 時点で各 snap_ts ごとに
    precompute された BT 状態. これらを使って snap-time の BT rolling を skip する.
    """
    snap_ts = int(snapshot_date.timestamp())
    lookback_ts = int((snapshot_date - dt.timedelta(days=LOOKBACK_DAYS)).timestamp())
    jjpr_lookback_ts = int((snapshot_date - dt.timedelta(days=JJPR_LOOKBACK_DAYS)).timestamp())
    prior = [t for t in all_tournaments if t.timestamp < snap_ts and t.timestamp >= lookback_ts]
    jjpr_prior = [t for t in all_tournaments if t.timestamp < snap_ts and t.timestamp >= jjpr_lookback_ts]
    if not prior:
        return {'tjpr_ranks': {}, 'tjpr_levels': {}, 'bt_gated_ranks': {}, 'ensemble_ranks': {},
                'jjpr_ranks': {}, 'bt_flat_ranks': {},
                'tjpr_scores': {}, 'tjpr_elo': {}, 'bt_gated_elo': {}, 'bt_internal_elo': {},
                'bt_flat_elo': {}, 'jjpr_scores': {}}

    # eval 時に precompute された snap-state を使う (= rolling skip で大幅高速化).
    # precompute されていない場合のみ in-line rolling (= 後方互換のフォールバック).
    if bt_all_at_snap is not None and snap_ts in bt_all_at_snap:
        bt_all_ord = bt_all_at_snap[snap_ts]
    else:
        bt_all = OpenSkillBTRater()
        for t in prior:
            meta = t_meta_by_id[id(t)]
            if meta.get('is_non_serious') or meta.get('is_gf_missing') or meta.get('is_small'): continue
            if meta.get('is_class_virtual'): continue
            for m in sorted(t.matches, key=_bracket_order_key):
                bt_all.update_match(m.winner_id, m.loser_id, m.timestamp)
        bt_all_ord = {uid: bt_all.rating(uid) for uid in bt_all.ratings}
    norm_bt = normalize_bt(bt_all_ord)

    # BT-gated internal ord (= 内部レート at snap)
    if bt_internal_at_snap is not None and snap_ts in bt_internal_at_snap:
        bt_internal_ord = bt_internal_at_snap[snap_ts]
        bt_g_ord = peak180_at_snap[snap_ts] if (peak180_at_snap and snap_ts in peak180_at_snap) else {}
    else:
        # フォールバック: in-line rolling (= 古いパスを保持)
        bt_g = OpenSkillBTRater()
        tour_c = defaultdict(int)
        last_seen_ts: dict[int, int] = {}
        peak180 = defaultdict(Peak180Tracker)
        for t in prior:
            meta = t_meta_by_id[id(t)]
            if meta.get('is_non_serious') or meta.get('is_gf_missing') or meta.get('is_small'): continue
            if meta.get('is_class_virtual'): continue
            is_wk_event = meta['is_weekend'] and not meta.get('is_lower_class', False)
            participants = set()
            for m in t.matches:
                participants.add(m.winner_id); participants.add(m.loser_id)
            for uid in participants:
                _apply_inactivity_sigma(bt_g, uid, last_seen_ts, t.timestamp)
            any_used = {uid: False for uid in participants}
            ordered_matches_snap = sorted(t.matches, key=_bracket_order_key)
            for m in ordered_matches_snap:
                is_wk_for_bt = is_wk_event and not getattr(m, 'is_class', False)
                wc, lc = tour_c[m.winner_id], tour_c[m.loser_id]
                update_w = is_wk_for_bt or (wc <= GATED_M_TOUR)
                update_l = is_wk_for_bt or (lc <= GATED_M_TOUR)
                if update_w or update_l:
                    w_r = bt_g._get(m.winner_id)
                    l_r = bt_g._get(m.loser_id)
                    new_ratings = bt_g.model.rate([[w_r], [l_r]], ranks=[1, 2])
                    if update_w:
                        nw = _apply_sigma_floor_keep_ordinal(bt_g.model, new_ratings[0][0], SIGMA_FLOOR)
                        bt_g.ratings[m.winner_id] = nw
                        any_used[m.winner_id] = True
                    if update_l:
                        nl = _apply_sigma_floor_keep_ordinal(bt_g.model, new_ratings[1][0], SIGMA_FLOOR)
                        bt_g.ratings[m.loser_id] = nl
                        any_used[m.loser_id] = True
            for uid in participants:
                if any_used[uid]:
                    peak180[uid].update(t.timestamp, bt_g._get(uid).ordinal())
                tour_c[uid] += 1
                last_seen_ts[uid] = t.timestamp
        bt_internal_ord = {uid: bt_g.rating(uid) for uid in bt_g.ratings}
        bt_g_ord = {}
        for uid, tr in peak180.items():
            cutoff = snap_ts - _common.BT_PEAK_WINDOW_SEC
            popped_peak = False
            while tr.history and tr.history[0][0] < cutoff:
                _, po = tr.history.popleft()
                if po >= tr.current_peak - 1e-12: popped_peak = True
            if popped_peak:
                tr.current_peak = max((o for _, o in tr.history), default=float('-inf'))
            bt_g_ord[uid] = tr.current_peak if tr.current_peak != float('-inf') else bt_g._get(uid).ordinal()
    norm_bt_g = normalize_bt(bt_internal_ord)  # 表示用 (BT-gated rank の Elo 表示)

    # TJPR v1: bt_at_t + global_shift で大会開催時の BT を固定 raw_pts として使う.
    cache = precompute_banzuke(prior, snapshot_date, bt_at_t, global_shift, t_meta_by_id, TJPR_DEFAULT_RATE)
    tjpr_ranks, tjpr_levels, tjpr_scores, _, _, _, _ = build_tjpr_v1_with_detail(cache)
    # サイト表示用の Elo: シフトなしの線形変換 (順位は不変)
    tjpr_elo = {uid: s * TJPR_ELO_SCALE + TJPR_ELO_OFFSET for uid, s in tjpr_scores.items()}
    # [EXPERIMENTAL] bt_g_ord は Glicko2 rating (Elo-space, そのまま)
    bt_g_elo_disp = dict(bt_g_ord)
    bt_internal_elo_disp = bt_to_elo_display(bt_internal_ord)  # current ベース (= 内部レート)
    bt_all_elo_disp = bt_to_elo_display(bt_all_ord)

    # JJPR v3
    jjpr_ranks, jjpr_scores, _ = build_jjpr_v3_with_detail(jjpr_prior, snapshot_date, t_meta_by_id, top_k=JJPR_TOP_K)

    # Ensemble (tie-break by avg display score) — 直対評価スコア = peak180 ベース
    bt_g_ranks = to_rank(bt_g_ord)
    all_uids = set(tjpr_ranks) | set(bt_g_ranks)
    ens_ranks = rank_avg([tjpr_ranks, bt_g_ranks], all_uids,
                          score_lists=[tjpr_elo, bt_g_elo_disp])
    return {
        'tjpr_ranks': tjpr_ranks,
        'tjpr_levels': tjpr_levels,
        'bt_gated_ranks': bt_g_ranks,
        'ensemble_ranks': ens_ranks,
        'jjpr_ranks': jjpr_ranks,
        'bt_flat_ranks': to_rank(bt_all_ord),
        'tjpr_scores': tjpr_scores,
        'tjpr_elo': tjpr_elo,
        'bt_gated_elo': bt_g_elo_disp,        # peak180
        'bt_internal_elo': bt_internal_elo_disp,  # current ord (= 内部レート)
        'bt_flat_elo': bt_all_elo_disp,
        'jjpr_scores': jjpr_scores,
    }


# ─── from build_tjpr_ranking main() (反復6 で move): build_achievements ───
# プレイヤーごとの実績バッチ (priority 順 sort 済 list) を構築する.
# 「一度でも到達したら剥奪されない」原則: 全期間のピーク値を採用 (snapshot 範囲 6 年).

# 大会別アチーブメント (= tournament tier) 用の共通テーブル. magic priority numbers.
_TIER_PRIORITY_BASE = {'優勝': 10000, '準優勝': 5000, '3位': 3000, '4位': 2000, '5位': 1500,
                       'Top 8 以上達成': 1000, 'Top 16 以上達成': 500,
                       'Top 32 以上達成': 300, 'Top 64 以上達成': 200}
_TIER_ICON = {'優勝': '🏆', '準優勝': '🥈', '3位': '🥉', '4位': '🎖', '5位': '🎖',
              'Top 8 以上達成': '🏅', 'Top 16 以上達成': '🏅',
              'Top 32 以上達成': '🏅', 'Top 64 以上達成': '🏅'}
_TIER_CLS = {'優勝': 'gold', '準優勝': 'silver', '3位': 'bronze', '4位': 'bronze', '5位': '',
             'Top 8 以上達成': '', 'Top 16 以上達成': '',
             'Top 32 以上達成': '', 'Top 64 以上達成': ''}
_TIER_BUCKET_ORDER = ['優勝', '準優勝', '3位', '4位', '5位',
                      'Top 8 以上達成', 'Top 16 以上達成',
                      'Top 32 以上達成', 'Top 64 以上達成']
# 機械可読コード (kind='tour' / 'tour_series' の params['bucket'])。表示側が辞書で label を組み立てる。
_TIER_BUCKET_CODE = {'優勝': 'win', '準優勝': '2nd', '3位': '3rd', '4位': '4th', '5位': '5th',
                     'Top 8 以上達成': 'top8', 'Top 16 以上達成': 'top16',
                     'Top 32 以上達成': 'top32', 'Top 64 以上達成': 'top64'}


def _class_code(class_suffix):
    """_classify_tournament_entry の class_suffix → params['cls'] (None / 'B'..'E' / 'casual')."""
    if not class_suffix:
        return None
    if class_suffix == 'カジュアル':
        return 'casual'
    return class_suffix[0]   # 'Bクラス' → 'B' (全角は分類時に半角化済)


def _highest_tier(value, tiers, base_priority, kind=None):
    """tiers = [(threshold, label, cls), ...] 高い閾値順. 最高ティア 1 つだけ dict を返す (無ければ None).

    kind を渡すと機械可読の識別 ('kind': kind, 'params': {'n': threshold}) を付ける (label / cls / priority は不変)。
    """
    for thr, lbl, c in tiers:
        if value >= thr:
            d = {'label': lbl, 'cls': c, 'priority': base_priority + thr}
            if kind is not None:
                d['kind'] = kind
                d['params'] = {'n': thr}
            return d
    return None


# 全国順位ピークのティア (top 閾値, label, cls, priority)。閾値の低い順に最初に当たったもの。
_PEAK_RANK_TIERS = [
    (1,    '👑 最高 全国 1 位', 'gold',   200000),
    (2,    '🥇 最高 Top 2',    'gold',   180000),
    (3,    '🥈 最高 Top 3',    'gold',   170000),
    (10,   '🌟 最高 Top 10',   'gold',   150000),
    (50,   '🔥 最高 Top 50',   'silver', 130000),
    (100,  '💪 最高 Top 100',  'bronze', 110000),
    (500,  '🎯 最高 Top 500',  'blue',   70000),
    (1000, '🎯 最高 Top 1000', '',       60000),
]


def _peak_rank_achievement(best_ens):
    """全国 ens_rank ピークから 1 個 (なければ None). kind='peak_rank', params={'top': 閾値}."""
    if not best_ens:
        return None
    for top, lbl, cls, prio in _PEAK_RANK_TIERS:
        if best_ens <= top:
            return {'label': lbl, 'cls': cls, 'priority': prio, 'kind': 'peak_rank', 'params': {'top': top}}
    return None


# Lv5 はさらに上位半分ずつ細分化: +(128)→🥉(64)→🥈(32)→🥇(16)→👑(8)。
LV5_MEDAL_CUTOFF = [(8, '👑'), (16, '🥇'), (32, '🥈'), (64, '🥉'), (128, '+')]


def level_tag_suffix(lv, rank):
    """レベルバッジ接尾辞。Lv5 は順位で 👑/🥇/🥈/🥉/+、Lv1-4 は + か空。"""
    if not lv or not rank:
        return ''
    if lv == 5:
        for cut, sfx in LV5_MEDAL_CUTOFF:
            if rank <= cut:
                return sfx
        return ''
    c = LV_PLUS_CUTOFF.get(lv)
    return '+' if (c and rank <= c) else ''


# Lv5 の細分ティア: 接尾辞 (表示) → tier コード (kind='peak_tier' の params['tier'])
_LV5_TIER_CODE = {'👑': 'crown', '🥇': 'gold', '🥈': 'silver', '🥉': 'bronze', '+': 'plus'}


def _level_tier_achievement(lv, rank):
    """(lv, rank) スナップショットの到達ティアを実績 dict で返す (なければ None)。
    '_v' は上位互換比較用の順序値。kind='peak_tier', params={'lv': lv, 'tier': コード}
    (Lv5: crown/gold/silver/bronze/plus/base, Lv1-4: plus/base)。"""
    if not lv or not rank:
        return None
    if lv == 5:
        for cut, sfx, cls, prio in [(8, '👑', 'gold', 170000), (16, '🥇', 'gold', 160000),
                                    (32, '🥈', 'silver', 155000), (64, '🥉', 'bronze', 150000),
                                    (128, '+', 'gold', 145000)]:
            if rank <= cut:
                return {'label': f'最高 Lv5{sfx} 到達', 'cls': cls, 'priority': prio,
                        'kind': 'peak_tier', 'params': {'lv': 5, 'tier': _LV5_TIER_CODE[sfx]}, '_v': prio}
        return {'label': '🥇 最高 Lv5 到達', 'cls': 'gold', 'priority': 140000,
                'kind': 'peak_tier', 'params': {'lv': 5, 'tier': 'base'}, '_v': 140000}
    _meta = {4: ('🥈', 'silver', 100000), 3: ('🥉', 'bronze', 80000),
             2: ('🎖', '', 65000), 1: ('🎖', '', 55000)}
    emoji, cls, prio = _meta[lv]
    c = LV_PLUS_CUTOFF.get(lv)
    if c and rank <= c:
        return {'label': f'{emoji} 最高 Lv{lv}+ 到達', 'cls': cls, 'priority': prio + 5000,
                'kind': 'peak_tier', 'params': {'lv': lv, 'tier': 'plus'}, '_v': prio + 5000}
    if lv >= 2:
        return {'label': f'{emoji} 最高 Lv{lv} 到達', 'cls': cls, 'priority': prio,
                'kind': 'peak_tier', 'params': {'lv': lv, 'tier': 'base'}, '_v': prio}
    return None  # Lv1 base は実績なし


# ── 上の _level_tier_achievement の numpy 版 (学習中の最高ティア追跡 spsp/peak_track 用) ──
# 順序値 (= '_v') だけを返す。表示 dict は tier_achievement_from_value で引く。両者は _level_tier_achievement と
# 同じ閾値でなければならない (tests/meta/test_peak_track.py が突き合わせる)。
_TIER_BY_VALUE: dict = {}


def tier_achievement_from_value(v: int):
    """順序値 → 実績 dict (label / cls / priority / kind / params)。0 なら None。"""
    if not v:
        return None
    if not _TIER_BY_VALUE:
        for lv, rk in ((5, 1), (5, 9), (5, 17), (5, 33), (5, 65), (5, 129), (4, 1), (4, 100000), (3, 1), (3, 100000),
                       (2, 1), (2, 100000), (1, 1)):
            a = _level_tier_achievement(lv, rk)
            if a:
                _TIER_BY_VALUE[a['_v']] = {k: x for k, x in a.items() if k != '_v'}
    return dict(_TIER_BY_VALUE[int(v)])


def _climb_achievement(history, ens_rank):
    """1ヶ月で +N ランク上昇 (最高ティアのみ, なければ None)."""
    h30 = next((h for h in history if h.get('d') == 30), None)
    if not (h30 and h30.get('ens') and ens_rank):
        return None
    climb = h30['ens'] - ens_rank
    climb_tiers = [
        (1000, '🚀 1ヶ月で +1000 ランク上昇', 'gold'),
        (500,  '🚀 1ヶ月で +500 ランク上昇',  'silver'),
        (200,  '🚀 1ヶ月で +200 ランク上昇',  'bronze'),
        (100,  '🚀 1ヶ月で +100 ランク上昇',  'blue'),
        (50,   '🚀 1ヶ月で +50 ランク上昇',   ''),
    ]
    return _highest_tier(climb, climb_tiers, base_priority=90000, kind='climb')


def _uf_achievement(muf_ach):
    """UF (Upset Factor) 単独最高値 1 個 (なければ None)."""
    if not (muf_ach and muf_ach.get('uf', 0) >= 5):
        return None
    uf_v = muf_ach['uf']
    uf_cls = 'gold' if uf_v >= 10 else 'silver' if uf_v >= 8 else 'bronze' if uf_v >= 6 else 'blue'
    return {'label': f'⚡ UF {uf_v} 達成', 'cls': uf_cls, 'priority': 50000 + uf_v * 500,
            'kind': 'uf', 'params': {'v': uf_v}}


def _spr_achievement(msp_ach):
    """SPR (Seed Performance Rating) 単独最高値 1 個 (なければ None)."""
    if not (msp_ach and msp_ach.get('spr', 0) >= 5):
        return None
    spr_v = msp_ach['spr']
    spr_cls = 'gold' if spr_v >= 10 else 'silver' if spr_v >= 8 else 'bronze' if spr_v >= 6 else 'blue'
    return {'label': f'✨ SPR {spr_v} 達成', 'cls': spr_cls, 'priority': 48000 + spr_v * 500,
            'kind': 'spr', 'params': {'v': spr_v}}


def _losers_run_achievement(mlr_ach):
    """ルーザーズラン 単独最高値 1 個 (5 以上, なければ None)."""
    if not (mlr_ach and mlr_ach.get('run', 0) >= 5):
        return None
    lr_v = mlr_ach['run']
    lr_cls = 'gold' if lr_v >= 10 else 'silver' if lr_v >= 8 else 'bronze' if lr_v >= 6 else 'blue'
    return {'label': f'🏃‍♂️ ルーザーズラン {lr_v} 達成', 'cls': lr_cls, 'priority': 46000 + lr_v * 500,
            'kind': 'losers_run', 'params': {'v': lr_v}}


def _activity_achievements(tc_2024, mc_2024, wc_2024, opp_counts):
    """活動量バッチ (大会出場数 / 試合数 / 勝利数 / 同一相手対戦数). 各カテゴリ最高ティアのみ.

    時間とともに自然に伸びるもの = 大会別実績より低い priority (base < 1000).
    """
    out = []
    t = _highest_tier(tc_2024, [
        (500, '🏆 大会出場 500+', 'gold'),
        (300, '🎖️ 大会出場 300+', 'silver'),
        (100, '🏅 大会出場 100+', 'bronze'),
        (50,  '🎫 大会出場 50+', 'blue'),
        (20,  '🎟️ 大会出場 20+', ''),
    ], base_priority=900, kind='tours')
    if t: out.append(t)
    t = _highest_tier(mc_2024, [
        (20000, '⚔ 試合数 20000+', 'gold'),
        (10000, '⚔ 試合数 10000+', 'gold'),
        (5000,  '⚔ 試合数 5000+',  'silver'),
        (3000,  '⚔ 試合数 3000+',  'bronze'),
        (1000,  '⚔ 試合数 1000+',  'blue'),
        (500,   '⚔ 試合数 500+',   ''),
        (200,   '⚔ 試合数 200+',   ''),
    ], base_priority=700, kind='matches')
    if t: out.append(t)
    t = _highest_tier(wc_2024, [
        (10000, '🏅 勝利数 10000+', 'gold'),
        (5000,  '🏅 勝利数 5000+',  'gold'),
        (3000,  '🏅 勝利数 3000+',  'silver'),
        (1500,  '🏅 勝利数 1500+',  'bronze'),
        (500,   '🏅 勝利数 500+',   'blue'),
        (200,   '🏅 勝利数 200+',   ''),
        (100,   '🏅 勝利数 100+',   ''),
    ], base_priority=500, kind='wins')
    if t: out.append(t)
    if opp_counts:
        # spsp/build.py は最大値 (int) だけ渡す
        max_vs = opp_counts if type(opp_counts) is int else max(opp_counts.values())
        t = _highest_tier(max_vs, [
            (50, '🤝 同一相手と対戦 50+', 'gold'),
            (30, '🤝 同一相手と対戦 30+', 'silver'),
            (20, '🤝 同一相手と対戦 20+', 'bronze'),
            (10, '🤝 同一相手と対戦 10+', 'blue'),
            (5,  '🤝 同一相手と対戦 5+', ''),
        ], base_priority=300, kind='same_opp')
        if t: out.append(t)
    return out


def _classify_tournament_entry(e):
    """1 件の tournament_entry を (bucket_name, class_suffix) に分類. 除外なら None を返す."""
    place = e.get('place')
    if place is None or place < 1:
        return None
    nent = e.get('nent') or 0
    is_wk_e = e.get('is_weekend')
    if place > 2:
        if (e.get('tjpr_w') or 0) <= 0: return None
        if nent < 16: return None
        if not is_wk_e: return None
    ev_name = e.get('event') or ''
    t_name = e.get('name') or ''
    class_match = _re.search(r'([BCDEＢＣＤＥ])\s*クラス', ev_name)
    class_suffix = None
    if class_match:
        cl = class_match.group(1).translate(str.maketrans('ＢＣＤＥ', 'BCDE'))
        class_suffix = f'{cl}クラス'
    elif e.get('is_lower_class') and ('カジュアル' in ev_name or 'カジュアル' in t_name):
        class_suffix = 'カジュアル'
    if place == 1: b = '優勝'
    elif place == 2: b = '準優勝'
    elif place == 3: b = '3位'
    elif place == 4: b = '4位'
    elif place == 5: b = '5位'
    elif 6 <= place <= 8: b = 'Top 8 以上達成'
    elif 9 <= place <= 16 and nent >= 128: b = 'Top 16 以上達成'
    elif 17 <= place <= 32 and nent >= 256: b = 'Top 32 以上達成'
    elif 33 <= place <= 64 and nent >= 512: b = 'Top 64 以上達成'
    else: return None
    return (b, class_suffix)


def _series_lower_penalty(entries):
    """entries の中で下位クラス or 制限大会が含まれてる割合に応じた penalty multiplier (1.0 〜 0.2)."""
    if not entries: return 1.0
    low_n = sum(1 for en in entries if en.get('is_lower_class') or en.get('is_restricted'))
    ratio = low_n / len(entries)
    return 1.0 - 0.8 * ratio


def _tjpr_pts(entries):
    """大会別実績のもとになった大会ごとの [日付, 順位評価の素点 tjpr_raw] (素点 0 = 集計対象外は除く、日付順)。
    フロントのプレイヤーカードが大会の実績を「素点 × 0.5^(経過年数)」の最大で並べる (2026-09-30)。
    減衰は表示時に計算する (ここで掛けると、大会に出ていない選手のファイルも日々変わってしまう)"""
    out = []
    for en in entries:
        pts = float(en.get('tjpr_raw') or 0.0)
        if pts > 0 and en.get('date'):
            out.append([en['date'], round(pts, 4)])
    out.sort()
    return out


def _tournament_achievements(tournament_entries):
    """大会別アチーブメント. series ごとに最高位だけ採用 (≥100 nent は個別 badge)."""
    series_buckets = defaultdict(lambda: defaultdict(list))  # nent<100: series 表示名 -> bucket -> entries
    series_parts: dict[str, tuple[str, str | None]] = {}      # series 表示名 -> (series 名, class_suffix)
    individual_emit: list[tuple[dict, str, str | None]] = []  # nent>=100: (entry, bucket, class_suffix)
    for e in tournament_entries:
        cls = _classify_tournament_entry(e)
        if cls is None: continue
        b, class_suffix = cls
        nent_e = e.get('nent') or 0
        t_name = e.get('name') or ''
        # ≥100 nent は series 統合しない (= 個別 badge)
        if nent_e >= 100:
            individual_emit.append((e, b, class_suffix))
        else:
            s = tournament_award_label(t_name)
            if not s: continue
            s_disp = f"{s} {class_suffix}" if class_suffix else s
            series_parts.setdefault(s_disp, (s, class_suffix))
            series_buckets[s_disp][b].append(e)
    out = []
    # 下位クラス / 制限大会 は priority を下げる (= ランキング比重 0.2倍).
    # 通常大会の実績より下に表示されるようにする.
    for s, buckets in series_buckets.items():
        for bucket_name in _TIER_BUCKET_ORDER:
            if bucket_name not in buckets: continue
            entries = buckets[bucket_name]
            count = len(entries)
            suffix = f' ×{count}' if count > 1 else ''
            max_nent = max(int(en.get('nent') or 0) for en in entries)
            base_priority = max_nent * 10 + _TIER_PRIORITY_BASE[bucket_name] + (count - 1) * 50
            priority = int(base_priority * _series_lower_penalty(entries))
            s_name, s_cls = series_parts[s]
            out.append({
                'label': f"{_TIER_ICON[bucket_name]} {s} {bucket_name}{suffix}",
                'cls': _TIER_CLS[bucket_name],
                'priority': priority,
                'kind': 'tour_series',
                'params': {'series': s_name, 'bucket': _TIER_BUCKET_CODE[bucket_name],
                           'cls': _class_code(s_cls), 'count': count},
                'tjpr_pts': _tjpr_pts(entries),
            })
            break
    # ≥100 nent 個別 badge emit (= シリーズ統合せず、各 event を個別 entry に)
    for e, b, class_suffix in individual_emit:
        t_name = e.get('name') or ''
        name_base = tournament_individual_label(t_name)
        label_base = f"{name_base} {class_suffix}" if class_suffix else name_base
        base_priority = (e.get('nent') or 0) * 10 + _TIER_PRIORITY_BASE[b]
        # 下位クラス / 制限大会 priority 下げる
        if e.get('is_lower_class') or e.get('is_restricted'):
            base_priority = int(base_priority * 0.2)
        out.append({
            'label': f"{_TIER_ICON[b]} {label_base} {b}",
            'cls': _TIER_CLS[b],
            'priority': base_priority,
            'kind': 'tour',
            'params': {'name': name_base, 'bucket': _TIER_BUCKET_CODE[b], 'cls': _class_code(class_suffix)},
            'tjpr_pts': _tjpr_pts([e]),
        })
    return out


def build_achievements(
    uid,
    ens_rank,
    tj_level,
    history,
    tournament_entries,
    player_max_uf_ach,
    player_max_spr_ach,
    player_max_losers_run,
    player_tour_count_2024,
    player_match_count_2024,
    player_win_count_2024,
    player_vs_opp_count,
    peak_alltime_max_days=None,
    peak=None,
):
    """Build achievements list for a single player. Returns priority-sorted list of dicts."""
    # 過去最高 (最高順位 / 最高到達ティア): cutoff (= EVO Japan 2020 以降) 内の history スナップショットと現在値の最良。
    # peak (= spsp/peak_track の追跡値: 学習の軌跡上 = serious 大会ごとの点の最小値) があればそれも入れる。
    # スナップショットは大会の無い期間の減衰による動きを、追跡値は大会直後に達した順位を、それぞれ拾う。
    _hist = ([h for h in history if h.get('d') is not None and h['d'] <= peak_alltime_max_days]
             if peak_alltime_max_days is not None else history)
    best_ens = ens_rank if ens_rank else None
    for h in _hist:
        if h.get('ens') and (best_ens is None or h['ens'] < best_ens):
            best_ens = h['ens']
    best_tier = None
    for lv_, ens_ in [(tj_level, ens_rank)] + [(h.get('tjpr_lv'), h.get('ens')) for h in _hist]:
        cand = _level_tier_achievement(lv_, ens_)
        if cand and (best_tier is None or cand['_v'] > best_tier['_v']):
            best_tier = cand
    if peak is not None:
        _pe = peak.get('ensemble')
        if _pe is not None and (best_ens is None or _pe[0] < best_ens):
            best_ens = int(_pe[0])
        _tv = int(peak.get('tier_best') or 0)
        if _tv and (best_tier is None or _tv > best_tier['_v']):
            best_tier = dict(tier_achievement_from_value(_tv), _v=_tv)
    level_ach = {k: v for k, v in best_tier.items() if k != '_v'} if best_tier else None

    ach = []
    # ── 単独最高ティア系 ──
    for item in (
        _peak_rank_achievement(best_ens),
        level_ach,
        _climb_achievement(history, ens_rank),
        _uf_achievement(player_max_uf_ach.get(uid)),
        _spr_achievement(player_max_spr_ach.get(uid)),
        _losers_run_achievement(player_max_losers_run.get(uid)),
    ):
        if item:
            ach.append(item)
    # ── 活動量バッチ (時間とともに自然に伸びるもの) ──
    ach.extend(_activity_achievements(
        player_tour_count_2024.get(uid, 0),
        player_match_count_2024.get(uid, 0),
        player_win_count_2024.get(uid, 0),
        player_vs_opp_count.get(uid, {}),
    ))
    # ── 大会別アチーブメント ──
    ach.extend(_tournament_achievements(tournament_entries))
    # DAY X 進出 実績は **一旦無効化** (= per-event DAY 検出ロジックが完全に揃うまで保留).
    # 計算ロジック (compute_per_player_max_day + _DAY_OVERRIDES) は残してあるが
    # ach.append は実行しない. 復活させる時はこのブロックを uncomment.
    # DAY_TIER_BASE = {2: 40, 3: 60, 4: 80, 5: 100}
    # for e in tournament_entries:
    #     ... (省略, 詳細は git history 参照) ...
    ach.sort(key=lambda x: -x['priority'])
    return ach


# ─── from build_tjpr_ranking main() (反復6 で move): build_dynamic_badges ───
# 動的バッチ (= 実績とは別。一時的なステータス。直近 30/90/180 日でランクが上昇した人).
def history_event_rows(tournament_entries, eval_date) -> list:
    """出場した serious 大会の「結果確定直後」の順位を履歴の点にする (= 順位が変化した日付に点を打つ)。

    グリッドのスナップショット (週次〜四半期) だけだと大会直後に達した順位が点に出ないので、
    tournaments[] の当時アンカー (pretour_ranks = 直前、rank_delta_* = 直前 − 直後) から直後の順位を作る。
    スコア系 (tjpr_e 等) は無いので None (チャートは null を飛ばす)。event_id で印を付ける。
    """
    eval_day = eval_date.date() if isinstance(eval_date, dt.datetime) else eval_date
    rows = []
    for e in tournament_entries or []:
        pre = e.get('pretour_ranks') or {}
        ts = e.get('ts')
        if ts is None or pre.get('ensemble') is None:
            continue
        def _after(k, dk):
            b = pre.get(k); d = e.get(dk)
            return (b - d) if (b is not None and d is not None) else b
        rows.append({
            'd': (eval_day - dt.datetime.fromtimestamp(ts).date()).days,
            'ens': _after('ensemble', 'rank_delta_ensemble'),
            'tjpr_r': _after('tjpr', 'rank_delta_tjpr'),
            'tjpr_lv': e.get('at_lv') or None,
            'bt_g_r': _after('bt_gated', 'rank_delta_bt'),
            'tjpr_s': None, 'tjpr_e': None, 'bt_g_e': None, 'bt_int_e': None, 'bt_f_e': None,
            'shared_lv': e.get('at_lv') or None, 'shared_ens_r': None,
            'event_id': e.get('event_id'),
        })
    return rows


def merge_history(grid_rows: list, event_rows: list) -> list:
    """グリッドの点と大会の点を d 昇順 (= 新しい順) に並べる。同じ d ならグリッドの点が先。"""
    out = [(r.get('d'), 0, i, r) for i, r in enumerate(grid_rows)] + [(r.get('d'), 1, i, r) for i, r in enumerate(event_rows)]
    out.sort(key=lambda x: (x[0] if x[0] is not None else 10 ** 9, x[1], x[2]))
    return [r for _, _, _, r in out]


def build_dynamic_badges(ens_rank, history):
    """Return dynamic badges list (recent climb status) for a single player."""
    dynamic_badges = []
    cur_ens = ens_rank
    if cur_ens is not None and cur_ens > 0:
        def _delta(days):
            h = next((h for h in history if h.get('d') == days), None)
            if not h: return None
            prev = h.get('ens')
            if prev is None or prev <= 0: return None
            return prev - cur_ens
        d30 = _delta(30); d90 = _delta(90); d180 = _delta(180)
        # kind='trend', params={'days': 30|90|180, 'n': 上昇幅, 'surge': 🚀 急上昇か}
        def _badge(label, cls, days, n, surge):
            return {'label': label, 'cls': cls, 'kind': 'trend', 'params': {'days': days, 'n': n, 'surge': surge}}
        if d30 is not None and d30 >= 500:
            dynamic_badges.append(_badge(f'🚀 直近 1 ヶ月で +{d30} 急上昇', 'gold', 30, d30, True))
        elif d30 is not None and d30 >= 200:
            dynamic_badges.append(_badge(f'📈 直近 1 ヶ月で +{d30} 上昇', 'silver', 30, d30, False))
        elif d90 is not None and d90 >= 500:
            dynamic_badges.append(_badge(f'🚀 直近 3 ヶ月で +{d90} 急上昇', 'silver', 90, d90, True))
        elif d90 is not None and d90 >= 200:
            dynamic_badges.append(_badge(f'📈 直近 3 ヶ月で +{d90} 上昇', 'bronze', 90, d90, False))
        elif d180 is not None and d180 >= 500:
            dynamic_badges.append(_badge(f'📈 直近半年で +{d180} 上昇', 'bronze', 180, d180, False))
    return dynamic_badges


# ─── from build_tjpr_ranking main() (反復7 で move): build_radar ───
# レーダーチャート用 6 指標を計算.
# 集計対象: プレイヤーごとに「フィルタを通過した直近 RECENT_N 大会」.
#   - 順位系 (SPR): プレイヤー到達 Lv の対象大会 (passes_level) を通過したもの
#   - 直対系 (UF): 新人=全大会、玄人=土日祝大会のみ
# 母集団: 全プレイヤー (フィルタ通過後 MIN_TOURS 未満なら percentile 計算から除外 → 中立 50)
def _radar_filter_recent_tours(all_tours_u, lv_u, t_meta_by_id, passes_level, n):
    """直近 n 大会フィルタ: Lv 集計対象 ∩ 休日のみ. date 降順の tid list を返す."""
    tours = []
    for ts, tid in all_tours_u:
        mt = t_meta_by_id.get(tid)
        if not mt: continue
        if not mt.get('is_weekend', False): continue
        if not passes_level(lv_u, mt.get('nent', 0), mt.get('is_weekend', False)): continue
        tours.append(tid)
        if len(tours) >= n: break
    return tours


def _radar_player_spr_values(u, tours, t_meta_by_id, t_seed_info, min_tours):
    """SPR radar (上振れ/安定) per-player 計算.

    シード補正: seed=1 → +1 (勝って当たり前), 最下位 tier → -2 (最速敗退して当然).
    """
    upper_count = 0
    stability_count = 0
    for tid in tours:
        info = t_seed_info.get(tid, {}).get(u)
        if not info or info.get('spr') is None: continue   # None = ランダムシード大会
        mt = t_meta_by_id.get(tid)
        spr = info.get('spr', 0) or 0
        seed_v = info.get('seed')
        nent_t = mt.get('nent') if mt else None
        max_w2w_t = placement_to_w2w(nent_t) if nent_t else 0
        if seed_v == 1:
            spr += 1
        elif seed_v and max_w2w_t and placement_to_w2w(seed_v) == max_w2w_t:
            spr -= 2
        if spr >= 1: upper_count += 1
        if spr >= -1: stability_count += 1
    denom = len(tours) if len(tours) >= min_tours else 0
    upper = (upper_count / denom) if denom > 0 else None
    neg   = (stability_count / denom) if denom > 0 else None
    return upper, neg


def _radar_player_uf_values(u, tours, t_seed_info, player_matches, min_tours):
    """UF radar (格上喰い/防衛力) per-player 計算.

    UF >= 1 = W2W(my_seed) と W2W(opp_seed) が異なる tier の組合せのみ集計.
    """
    higher_wins = 0; higher_total = 0
    lower_wins  = 0; lower_total  = 0
    tours_set = set(tours)
    for tup in player_matches.get(u, []):
        ts, tid, opp, won, bt_d = tup[0], tup[1], tup[2], tup[3], tup[4]
        if tid not in tours_set: continue
        si = t_seed_info.get(tid, {})
        my_info = si.get(u); opp_info = si.get(opp)
        if not my_info or not opp_info: continue
        if my_info.get('spr') is None: continue   # ランダムシード大会 (build が spr を None にする)
        my_seed = my_info.get('seed'); opp_seed = opp_info.get('seed')
        if my_seed is None or opp_seed is None: continue
        my_w2w  = placement_to_w2w(my_seed)
        opp_w2w = placement_to_w2w(opp_seed)
        if my_seed > opp_seed and (my_w2w - opp_w2w) >= 1:
            higher_total += 1
            if won: higher_wins += 1
        elif my_seed < opp_seed and (opp_w2w - my_w2w) >= 1:
            lower_total += 1
            if won: lower_wins += 1
    upper = (higher_wins / higher_total) if (len(tours) >= min_tours and higher_total > 0) else None
    lost  = (lower_wins  / lower_total)  if (len(tours) >= min_tours and lower_total  > 0) else None
    return upper, lost


def _radar_percentile_rank(values_dict, uids, higher_better=True, default=50.0):
    """値が None のプレイヤーは percentile 計算から除外し、default (= 50) を返す."""
    ranked_uids = [u for u in uids if values_dict.get(u) is not None]
    ranked = sorted(ranked_uids, key=lambda u: (values_dict[u], u), reverse=higher_better)
    n = len(ranked)
    out = {}
    for i, u in enumerate(ranked):
        out[u] = round(100.0 * (n - i) / n, 1)
    for u in uids:
        if u not in out: out[u] = default
    return out


def _radar_pct_vs_top1024(values_dict, uids, top_1024, higher_better=True, default=50.0):
    """母集団 = トップ 1024 + 自分. 自分が圏外なら 1025 人, 圏内なら 1024 人で percentile."""
    import bisect as _bisect
    top_vals_sorted = sorted(
        [values_dict[u] for u in top_1024 if values_dict.get(u) is not None],
        reverse=higher_better
    )
    n_top = len(top_vals_sorted)
    keyed = [-x for x in top_vals_sorted] if higher_better else top_vals_sorted
    out = {}
    for uid in uids:
        v = values_dict.get(uid)
        if v is None:
            out[uid] = default; continue
        key = -v if higher_better else v
        idx = _bisect.bisect_left(keyed, key)
        in_top = uid in top_1024
        n = n_top if in_top else n_top + 1
        rank = idx + 1
        out[uid] = round(100.0 * (n - rank + 1) / n, 1)
    return out


def build_radar(
    player_tour_history,
    ensemble_ranks,
    tjpr_levels,
    t_meta_by_id,
    t_seed_info,
    player_matches,
    tjpr_elo,
    norm_bt_gated_disp,
    passes_level,
):
    """Compute radar chart values + percentiles for all players.

    Returns:
        (spr_upper_val, spr_neg_val, uf_upper_val, uf_lost_val, pct)
        where pct is a dict with keys:
        'tjpr_pt', 'bt_pt', 'spr_upper', 'spr_stability', 'uf_upper', 'uf_stability'.
    """
    print("Computing radar chart metrics ...", flush=True)

    MIN_TOURS = 3     # フィルタ通過後の大会数しきい値 (これ未満は data 不足)
    RECENT_N  = 5     # 直近何大会を見るか

    spr_upper_val, spr_neg_val = {}, {}
    uf_upper_val, uf_lost_val  = {}, {}
    all_uids = set(player_tour_history.keys()) | set(ensemble_ranks.keys())

    for u in all_uids:
        lv_u = tjpr_levels.get(u, 1)
        all_tours_u = sorted(player_tour_history.get(u, []), key=lambda x: -x[0])
        tours = _radar_filter_recent_tours(all_tours_u, lv_u, t_meta_by_id, passes_level, RECENT_N)
        spr_upper_val[u], spr_neg_val[u] = _radar_player_spr_values(
            u, tours, t_meta_by_id, t_seed_info, MIN_TOURS)
        uf_upper_val[u], uf_lost_val[u]  = _radar_player_uf_values(
            u, tours, t_seed_info, player_matches, MIN_TOURS)

    radar_population = set(all_uids)
    print(f"  radar population: {len(radar_population)} players", flush=True)

    top_1024 = {u for u, rank in ensemble_ranks.items() if isinstance(rank, int) and 0 < rank <= 1024}
    pct = {
        'tjpr_pt':       _radar_percentile_rank(tjpr_elo,           radar_population, higher_better=True),
        'bt_pt':         _radar_percentile_rank(norm_bt_gated_disp, radar_population, higher_better=True),
        'spr_upper':     _radar_pct_vs_top1024(spr_upper_val, radar_population, top_1024, higher_better=True),
        'spr_stability': _radar_pct_vs_top1024(spr_neg_val,   radar_population, top_1024, higher_better=True),
        'uf_upper':      _radar_pct_vs_top1024(uf_upper_val,  radar_population, top_1024, higher_better=True),
        'uf_stability':  _radar_pct_vs_top1024(uf_lost_val,   radar_population, top_1024, higher_better=True),
    }

    return spr_upper_val, spr_neg_val, uf_upper_val, uf_lost_val, pct


# ─── from build_tjpr_ranking main() (反復8 で move): build_history ───
# 各 SNAPSHOT_DAYS 時点での player のランク (ens, tjpr_r, bt_g_r, tjpr_e, bt_g_e, ...)
# を sparse list として構築する.
# データが無い snapshot, または該当 uid が ensemble/tjpr/bt_gated いずれにも入っていない
# snapshot は skip する.
def build_history(uid, snapshots):
    """Build sparse per-player history list of {d, ranks + scores + elo at that snapshot}.

    Args:
        uid: player user id.
        snapshots: dict[days -> snapshot_dict] from compute_ranks_at.

    Returns:
        list[dict] in SNAPSHOT_DAYS order (newest-first as the constant defines),
        skipping snapshots where this uid has no rank data.
    """
    prep = _history_prep(snapshots)
    if prep is not None and type(uid) is int:
        i = _shared_index(uid)
        if i is not None:
            return _build_history_fast(uid, i, prep)
    history = []
    for days in sorted(snapshots):   # 新しい順 (= 日数昇順)。点は build 側の固定グリッド (common.snapshot_grid_days)
        sd = snapshots[days]
        # Only record if player has any data at that snapshot
        if uid in sd['ensemble_ranks'] or uid in sd['tjpr_ranks'] or uid in sd['bt_gated_ranks']:
            history.append({
                'd': days,
                'ens': sd['ensemble_ranks'].get(uid),
                'tjpr_r': sd['tjpr_ranks'].get(uid),
                'tjpr_lv': sd.get('tjpr_levels', {}).get(uid),
                'bt_g_r': sd['bt_gated_ranks'].get(uid),    # peak180 ベース
                'jjpr_r': sd['jjpr_ranks'].get(uid),
                'tjpr_s': sd['tjpr_scores'].get(uid),
                'tjpr_e': sd['tjpr_elo'].get(uid),
                'bt_g_e': sd['bt_gated_elo'].get(uid),       # peak180 Elo (= 直対評価スコア)
                'bt_int_e': sd.get('bt_internal_elo', {}).get(uid),  # current Elo (= 内部レート)
                'bt_f_e': sd['bt_flat_elo'].get(uid),
                'jjpr_s': sd['jjpr_scores'].get(uid),
            })
    return history


# build_history の行の項目 (出力のキー, snapshot の表の名前, 表が無くてよいか)。並びは出力の並び
_HIST_FIELDS = (('ens', 'ensemble_ranks', False), ('tjpr_r', 'tjpr_ranks', False), ('tjpr_lv', 'tjpr_levels', True),
                ('bt_g_r', 'bt_gated_ranks', False), ('jjpr_r', 'jjpr_ranks', False), ('tjpr_s', 'tjpr_scores', False),
                ('tjpr_e', 'tjpr_elo', False), ('bt_g_e', 'bt_gated_elo', False), ('bt_int_e', 'bt_internal_elo', True),
                ('bt_f_e', 'bt_flat_elo', False), ('jjpr_s', 'jjpr_scores', False))
_HIST_PREP: list = [None, None]   # [snapshots, 下ごしらえ] (同じ snapshots で全選手ぶん呼ばれる)


def _shared_index(uid):
    from spsp.columnar import SHARED_UIDS
    return SHARED_UIDS.pos.get(uid)


def _history_prep(snapshots):
    """build_history を 1 人ずつ速く引くための下ごしらえ (snapshots ごとに 1 回)。日ごとの表が共有の番号の
    UidMap (spsp/columnar) なら、位置の配列と値の配列を直接持つ (1 件ごとに .get を呼ぶと、北米で 20 万人 × 61 日 × 11 項目)。
    日ごとの表は以後書き換えない (build が作ったものを読むだけ)。
    日ごとに (日数, 項目の list, 総合・順位評価・直対評価の 3 項目)。項目 = (出力のキー, 種類, a, b, f):
    種類 0 = UidMap (a = 位置, b = 値)、1 = ScaledMap (値 × f)、2 = 空の dict (いつも None)、3 = その他 (a.get(uid))"""
    from spsp.columnar import SHARED_UIDS, ScaledMap, UidMap
    if _HIST_PREP[0] is snapshots:
        return _HIST_PREP[1]
    prep = []
    for days in sorted(snapshots):
        sd = snapshots[days]
        cols = []
        for out_key, name, optional in _HIST_FIELDS:
            t = sd.get(name, {}) if optional else sd.get(name)
            if t is None and not optional:
                prep = None   # 表が無い (元の書き方は KeyError)。元の経路で
                break
            if type(t) is UidMap and t._ix is SHARED_UIDS:
                cols.append((out_key, 0, t._at, t._vals, None))
            elif type(t) is ScaledMap and type(t._base) is UidMap and t._base._ix is SHARED_UIDS:
                cols.append((out_key, 1, t._base._at, t._base._vals, t._f))
            elif type(t) is dict and not t:
                cols.append((out_key, 2, None, None, None))
            else:
                cols.append((out_key, 3, t, None, None))
        if prep is None:
            break
        prep.append((days, cols, (cols[0], cols[1], cols[3])))
    _HIST_PREP[0] = snapshots; _HIST_PREP[1] = prep
    return prep


def _build_history_fast(uid, i, prep):
    """build_history と同じ行 (uid は int、i = 共有の番号での uid の位置)。"""
    history = []
    for days, cols, rank_cols in prep:
        # 総合・順位評価・直対評価のどれかに入っている時点だけ (元の `uid in ...` と同じ)
        for _k, kind, a, _b, _f in rank_cols:
            if kind <= 1:
                if i < len(a) and a[i] >= 0:
                    break
            elif kind == 3 and uid in a:
                break
        else:
            continue
        row = {'d': days}
        for key, kind, a, b, f in cols:
            if kind == 0:
                p = a[i] if i < len(a) else -1
                row[key] = None if p < 0 else b[p]
            elif kind == 1:
                p = a[i] if i < len(a) else -1
                row[key] = None if p < 0 else b[p] * f
            elif kind == 2:
                row[key] = None
            else:
                row[key] = a.get(uid)
        history.append(row)
    return history


# ─── 反復 12: rolling rank deltas (per-tournament impact) ───
# 大会単位のインパクトだけを反映する rank delta を計算.
# 大会前: 各参加者の score を全 prior tournaments で計算したときの順位
# 大会後: 大会の結果を加えた score での順位
# 非参加者の score は固定 (= last-computed value). これにより delta は
# 「この大会」の影響だけを isolate する.
def build_rolling_rank_deltas(all_tournaments, cache, t_meta_by_id, placements_per_t,
                              start_time=None, verbose=True):
    """Compute per-tournament rank deltas for TJPR, BT-gated, and ensemble.

    本体は v4/rolling.RollingState (学習側が大会ごとに増分計算する)。ここは全大会を一括で
    処理する参照実装 (テスト・単体呼び出し用)。build は inject['rolling_deltas'] を使う。

    Args:
        all_tournaments: list of all Tournament objects.
        cache: precomputed banzuke (list of dicts with 't_id', 'norm', 'rounds', 'players', 'days').
        t_meta_by_id: dict[id(t) -> meta dict].
        placements_per_t: dict[tid -> {uid: place}].
        start_time: optional float (time.time()) for elapsed log.
        verbose: print progress.

    Returns:
        dict[tid -> {uid -> {'tjpr_d', 'bt_d', 'ens_d'}}]
    """
    from spsp.rolling import RollingState
    if verbose:
        print("Computing rolling rank deltas (per-tournament impact) ...", flush=True)
    lv2_raw_per_tour = _rolling_lv2_raw_per_tour(cache)
    rs = RollingState()
    chrono_tours = sorted(all_tournaments, key=lambda t: t.timestamp)
    for t_ in chrono_tours:
        tid_ = id(t_)
        meta_ = t_meta_by_id[tid_]
        if meta_.get('is_non_serious') or meta_.get('is_gf_missing') or meta_.get('is_small'):
            continue
        # class virtual の rolling delta は親 event で処理されているので skip
        if meta_.get('is_class_virtual'):
            continue
        raw_map, _ = lv2_raw_per_tour.get(tid_, ({}, 0))
        rs.step(tid_, t_, meta_['is_weekend'], raw_map, placements_per_t.get(tid_, {}))
    if verbose:
        elapsed_s = f" (elapsed {time.time()-start_time:.0f}s)" if start_time is not None else ""
        print(f"  rolling delta computed for {rs.n} tournaments{elapsed_s}", flush=True)
    return rs.deltas_by_path


def _tjpr_counts_at_lv(lv, meta):
    """大会当時のレベル lv で、この大会が TJPR 計上対象に入るか.
    実 cascade の LV tfilter (LV3_FILTER 以上は exclude_capped) に一致させる。
    (build.py passes_level は Lv3 で capped を除外しないので使わない。)"""
    nent = meta.get('nent') or 0
    is_wk = bool(meta.get('is_weekend'))
    is_capped = bool(meta.get('is_restricted') or meta.get('is_lower_class'))
    if not lv or lv <= 0:
        lv = 1  # 未計測 → Lv1 (全て計上)
    if lv >= 5:
        return nent > 48 and is_wk and not is_capped
    if lv == 4:
        return nent > 24 and is_wk and not is_capped
    if lv == 3:
        return nent > 24 and not is_capped       # ← Lv3 (top1024) で制限/下位クラス除外
    return True                                   # Lv1/Lv2: 制限大会も計上


def _jp_at_pos(order_uids_best_first, gray_set):
    """best-first に並べた uid 列 → 各 1-based 順位での jp-counter 値の配列を返す.
    jp_counter_rank と同値: 非灰色は累積非灰色数, 灰色は直前 jp+1."""
    n = len(order_uids_best_first)
    out = np.empty(n, dtype=np.int64)
    cnt = 0
    for i, u in enumerate(order_uids_best_first):
        if u in gray_set:
            out[i] = cnt + 1
        else:
            cnt += 1
            out[i] = cnt
    return out


def _snap_dist_cache(snapshots, measured_at_day=None):
    """snapshot day -> 凍結フィールドの sorted 分布 (searchsorted 用) を lazy 構築.

    raw 順位 (= sorted 配列内の位置) を jp-counter (全国#) に変換するテーブル
    (jp_tj / jp_bt) も持つ。これで隔離デルタを jp-counter 単位で算出でき、
    厳密 before (per-tour アンカー) と基準が揃う (生位置のまま引くと負順位/桁外れになる)。"""
    measured_at_day = measured_at_day or {}
    cache = {}
    def get(day):
        if day in cache:
            return cache[day]
        s = snapshots.get(day) or {}
        tjpr_scores = s.get('tjpr_scores', {})
        bt_elo = s.get('bt_gated_elo', {})
        tjpr_ranks = s.get('tjpr_ranks', {})
        bt_ranks = s.get('bt_gated_ranks', {})
        tj_sorted = np.sort(np.fromiter((float(v) for v in tjpr_scores.values()), dtype=float)) \
            if tjpr_scores else np.empty(0)
        bt_sorted = np.sort(np.fromiter((float(v) for v in bt_elo.values()), dtype=float)) \
            if bt_elo else np.empty(0)
        nmax = (len(set(tjpr_ranks) | set(bt_ranks)) + 1) or 1
        avg_vals = []
        for u in (set(tjpr_ranks) | set(bt_ranks)):
            avg_vals.append((tjpr_ranks.get(u, nmax) + bt_ranks.get(u, nmax)) / 2.0)
        avg_sorted = np.sort(np.array(avg_vals, dtype=float)) if avg_vals else np.empty(0)
        # raw 位置 -> jp-counter 変換テーブル (tjpr/bt 次元)。灰色 = 海外 or 計測中。
        measured_d = measured_at_day.get(day)
        def _gray(u):
            return (u in OVERSEAS_UIDS) or (measured_d is not None and u not in measured_d)
        gray_set = {u for u in (set(tjpr_scores) | set(bt_elo)) if _gray(u)}
        tj_order = sorted(tjpr_scores, key=lambda u: (-float(tjpr_scores[u]), u))
        bt_order = sorted(bt_elo, key=lambda u: (-float(bt_elo[u]), u))
        jp_tj = _jp_at_pos(tj_order, gray_set)
        jp_bt = _jp_at_pos(bt_order, gray_set)
        c = (tjpr_scores, bt_elo, tjpr_ranks, bt_ranks, tj_sorted, bt_sorted,
             avg_sorted, nmax, jp_tj, jp_bt)
        cache[day] = c
        return c
    return get


def _jp_of_rank(jp_arr, rank):
    """raw 1-based rank -> jp-counter 値 (配列範囲にクランプ)."""
    n = len(jp_arr)
    if n == 0 or rank is None:
        return rank
    i = rank - 1
    if i < 0:
        i = 0
    elif i >= n:
        i = n - 1
    return int(jp_arr[i])


def _rank_desc(sorted_asc, val):
    """値が大きいほど上位 (rank1). 1 + #{x > val}."""
    n = len(sorted_asc)
    if n == 0:
        return None
    return n - int(np.searchsorted(sorted_asc, val, side='right')) + 1


def _rank_asc(sorted_asc, val):
    """値が小さいほど上位 (rank1). 1 + #{x < val}."""
    if len(sorted_asc) == 0:
        return None
    return int(np.searchsorted(sorted_asc, val, side='left')) + 1


class AtTimeRec(tuple):
    """build_at_time_per_tournament の 1 人分 (大会 × 参加者)。以前は 8 項目の dict (北米で数百万件、1 件 約 270 バイト → 約 100 バイト)。
    読む側 (spsp/output.py) は .get('ens_before') のように名前で引くだけなので、tuple に名前で引く .get を足したもの。"""
    __slots__ = ()
    _POS = {'lv': 0, 'ens_before': 1, 'ens_after': 2, 'tjpr_before': 3, 'tjpr_after': 4,
            'bt_before': 5, 'bt_after': 6, 'tjpr_counts': 7}

    def get(self, key, default=None):
        i = self._POS.get(key)
        return default if i is None else self[i]


def at_time_base_points(all_tournaments, t_meta_by_id, bt_at_t, at_time_map, eval_date, chunk=2000) -> TourUidTable:
    """大会ページのポイントのベース (tjpr_raw) を当時の情報だけで: {tid: {uid: 点}} (spsp/columnar.TourUidTable)。
    その大会の時点のレート (bt_at_t) で大会の項目を作り (shift 0、spsp/cli/build_full._compute_per_tour_by_lv と同じ)、
    参加者ごとに当時のレベル (at_time_map の lv) の係数・大会の条件で数える (spsp/learner.compute_tjpr_at_lv と同じ。
    値は per_tour_by_lv の raw と同じく小数 4 桁)。当時のレベルが無い・その大会がそのレベルで数えない大会なら 0.0。
    以前は per_tour_by_lv (ビルドの日のカスケード) の今のレベルの値で、選手のレベルや集計の期間が変わると過去の大会の
    値も変わっていた (2026-10-23)。下位クラスの仮想大会はページが無いので作らない。大会の項目は chunk 大会ずつ作って手放す。"""
    from spsp.learner import LV_DEFS, compute_tjpr_at_lv
    lv_def = {lv: (tf, lp) for lv, tf, lp, _tn in LV_DEFS}
    tours = [t for t in all_tournaments
             if not t_meta_by_id[id(t)].get('is_class_virtual') and t_meta_by_id[id(t)].get('event_id') is not None]
    out = TourUidTable('d')
    for k in range(0, len(tours), chunk):
        for e in precompute_banzuke(tours[k:k + chunk], eval_date, bt_at_t, 0.0, t_meta_by_id, TJPR_DEFAULT_RATE):
            tid = e['t_id']
            at = at_time_map.get(tid) or {}
            by_lv: dict = {}
            for uid, _rw, _place in e['players']:
                lv = (at.get(uid) or {}).get('lv') or 0
                if lv in lv_def:
                    by_lv.setdefault(lv, []).append(uid)
            d = {uid: 0.0 for uid, _rw, _place in e['players']}
            for lv, uids in by_lv.items():
                tf, lp = lv_def[lv]
                _, contribs = compute_tjpr_at_lv([e], lp, tf, set(uids), return_contribs=True)
                for uid in uids:
                    rows = contribs.get(uid) if contribs is not None else None
                    if rows:
                        d[uid] = round(rows[0]['raw_points'], 4)   # per_tour_by_lv と同じ丸め (型もそのまま)
            if tid not in out:
                out.add(tid, d)
    return out


def build_at_time_per_tournament(all_tournaments, t_meta_by_id, placements_per_t,
                                 snapshots, snapshot_days, eval_ts,
                                 ensemble_ranks_now, tjpr_levels_now,
                                 rolling_deltas, bt_gated_hist, verbose=True,
                                 pt_at_time=None, measured_at_day=None, match_extra_uids=None):
    """各 (大会, 選手) の「当時の」レベル/順位、および順位変動 (before→after) を返す.

    手法 (厳密アンカーあり = per-tour): before = 大会直前の実カスケード当時順位、
    after = **大会 T の結果確定直後** (= T の戦績で BT 更新 + TJPR 加点した上で T の時刻で
    引き直したカスケード) の順位。どちらも学習側 (pt_at_time の各 entry = [lv, ens_b, tjpr_b,
    bt_b, ens_a, tjpr_a, bt_a]) で厳密に算出済。T 以降の減衰・他大会は未反映なので「T だけで
    どう動いたか」が正確に出る。負順位/桁外れも原理的に無し。下位クラスを持つ本戦の after は
    最後のクラスを処理した直後 (クラスの点も入る)。集計対象外の大会 (身内・小規模・GF なし) は
    [lv, ens, tjpr, bt] だけ (点が動かないので after = before)。
    厳密アンカーが無い大会 (学習より前の大会等) は従来通り、最近傍スナップショットのフィールドに
    その大会の寄与を足して引き直す隔離デルタ (jp-counter 変換済) で近似する。

    Returns dict[tid -> {uid -> {
        'lv','ens_before','ens_after','tjpr_before','tjpr_after','bt_before','bt_after',
        'tjpr_counts'}}].
    """
    if verbose:
        print("Computing per-tournament at-the-time level + isolated rank delta ...", flush=True)
    days_sorted = sorted(snapshot_days)
    get_dist = _snap_dist_cache(snapshots, measured_at_day)
    # bt peak delta lookup: (uid, tid) -> bt_peak_d。配列の表 (spsp/columnar.GatedHist) からはそのまま引く
    # (以前は {uid: {tid: d}} を作っていた。北米で 280 万件の一時的な dict)
    bt_peak_d = None
    if isinstance(bt_gated_hist, GatedHist) and len(all_tournaments) < 1 << 20:
        bt_peak_d = bt_gated_hist.d_lookup({id(t): i for i, t in enumerate(all_tournaments)})
    if bt_peak_d is None:
        bt_peak_by = defaultdict(dict)
        for uid_h, entries in bt_gated_hist.items():
            for e in entries:
                bt_peak_by[uid_h][e[0]] = float(e[3]) if len(e) >= 4 else 0.0

        def bt_peak_d(u, tid):
            return bt_peak_by.get(u, {}).get(tid, 0.0)

    # after 順位は学習側 (pt_at_time の各 entry) に格納済: ex = [lv, ens_b, tjpr_b, bt_b,
    # ens_a, tjpr_a, bt_a]。after = その大会の結果確定直後 (減衰なし) の順位。集計対象外の大会は
    # [lv, ens_b, tjpr_b, bt_b] だけ (len==4) → after = before。

    # 大会ごとの結果は 1 本の配列の表に足していく (spsp/columnar.TourUidTable、引いた値と並びは同じ。表せない大会は dict のまま)
    out = TourUidTable('iiiiiiib', AtTimeRec)
    for t in all_tournaments:
        tid = id(t)
        meta = t_meta_by_id.get(tid) or {}
        if meta.get('is_class_virtual'):
            continue
        days_ago = (eval_ts - t.timestamp) / 86400.0
        before_day = next((d for d in days_sorted if d >= days_ago), None)
        snap_b = snapshots.get(before_day) if before_day is not None else None
        eb = (snap_b or {}).get('ensemble_ranks', {})
        tb = (snap_b or {}).get('tjpr_ranks', {})
        bb = (snap_b or {}).get('bt_gated_ranks', {})
        lvb = (snap_b or {}).get('tjpr_levels', {})
        dist = get_dist(before_day) if before_day is not None else None
        deltas_t = rolling_deltas.get(tid, {})
        # per-tour 厳密アンカー (= この大会直前の実カスケード). あれば最近傍スナップショット
        # より優先 (= 当時のレベル/全国#/各順位が毎大会厳密になる). 隔離デルタは従来通り
        # 最近傍フィールドで計算し、この厳密 before に適用する。
        exact = pt_at_time.get(tid) if pt_at_time else None
        participants = set(placements_per_t.get(tid, {}).keys())
        extra = match_extra_uids.get(tid) if match_extra_uids is not None else None
        if extra is not None:
            # build_spr_uf_aggregates が数えておいた「試合にだけ出てくる参加者」(同じ順に足すので set も同じ)。試合を読まない
            for u in extra:
                participants.add(u)
        else:
            for m in t.matches:
                participants.add(m.winner_id); participants.add(m.loser_id)
        d_out = {}
        for uid in participants:
            ex = exact.get(uid) if exact else None
            if ex is not None:
                lv = ex[0]; ens_b = ex[1]; tjpr_b = ex[2]; bt_b = ex[3]
            elif snap_b is not None:
                lv = lvb.get(uid, 0)
                ens_b = eb.get(uid); tjpr_b = tb.get(uid); bt_b = bb.get(uid)
            else:
                # 当時の順位の記録もスナップショットも無い (学習の最初のころで、まだ誰も計測済みでない) → 当時は
                # 未計測・順位なし (学習の player_lv も空)。以前は今のレベル・順位を入れていて、毎日少しずつ動いていた
                lv = 0
                ens_b = None; tjpr_b = None; bt_b = None
            tjpr_counts = _tjpr_counts_at_lv(lv, meta)
            ens_a = ens_b; tjpr_a = tjpr_b; bt_a = bt_b   # default: 変動なし
            if ex is not None:
                # after = T 結果確定直後の順位 (学習側 second cascade で ex[4:7] に格納済)。len==4 は変動なし
                if len(ex) >= 7:
                    if ex[4] is not None: ens_a = ex[4]
                    if ex[5] is not None: tjpr_a = ex[5]
                    if ex[6] is not None: bt_a = ex[6]
            elif dist is not None:
                tjpr_scores, bt_elo, _, _, tj_sorted, bt_sorted, avg_sorted, nmax, jp_tj, jp_bt = dist
                # この大会の寄与だけを足す (= 隔離). TJPR = 当時計上された raw pt のみ.
                c_tjpr = float(rolling_raw_at(deltas_t, uid, 0.0)) if tjpr_counts else 0.0
                c_bt_elo = bt_peak_d(uid, tid) * ELO_PER_UNIT  # peak180 変化
                tj_s_b = float(tjpr_scores.get(uid, 0.0))
                bt_e_b = float(bt_elo.get(uid, 0.0))
                # 隔離移動量を jp-counter (全国#) 単位で算出する。raw 位置のまま引くと、
                # 厳密 before (jp-counter) と基準が違い負順位/桁外れになる (フィールド ~8000 vs
                # jp-counter ~5000)。位置→jp-counter 変換 (_jp_of_rank) してから差分を取る。
                d_tj_jp = 0; d_bt_jp = 0
                if c_tjpr:
                    jb = _jp_of_rank(jp_tj, _rank_desc(tj_sorted, tj_s_b))
                    ja = _jp_of_rank(jp_tj, _rank_desc(tj_sorted, tj_s_b + c_tjpr))
                    if jb is not None and ja is not None:
                        d_tj_jp = jb - ja
                if c_bt_elo:
                    jb = _jp_of_rank(jp_bt, _rank_desc(bt_sorted, bt_e_b))
                    ja = _jp_of_rank(jp_bt, _rank_desc(bt_sorted, bt_e_b + c_bt_elo))
                    if jb is not None and ja is not None:
                        d_bt_jp = jb - ja
                # 各成分の jp-counter 移動量を厳密 before に適用 (下限 1 でクランプ)。
                if tjpr_b is not None and d_tj_jp:
                    tjpr_a = max(1, tjpr_b - d_tj_jp)
                if bt_b is not None and d_bt_jp:
                    bt_a = max(1, bt_b - d_bt_jp)
                # ens 移動量 = tjpr/bt 成分移動量の平均 (cascade ens = (btr+tjr)/2 構成に対応)。
                if ens_b is not None and (d_tj_jp or d_bt_jp):
                    d_ens_jp = (d_tj_jp + d_bt_jp) / 2.0
                    ens_a = max(1, int(round(ens_b - d_ens_jp)))
            d_out[uid] = AtTimeRec((lv, ens_b, ens_a, tjpr_b, tjpr_a, bt_b, bt_a, tjpr_counts))
        out.add(tid, d_out)
    if verbose:
        print(f"  at-the-time anchors + after-ranks computed for {len(out)} tournaments"
              f" (dict のままの大会 {out.n_odd})", flush=True)
    return out


# ─── 反復 12: SPR/UF aggregates + per-tournament wins/losses ───
# Per-tournament seed/SPR (start.gg の seeds.json から実シードを使う) + per-match UF
# (Upset Factor) を計算してプレイヤー別の max UF を記録.
# UF は BT-gated と同じ gating ロジック (土日 or winner.tour_count <= M で勝った match のみ集計対象).

# 反復 20: build_spr_uf_aggregates を sub-functions に分割
# 元: 246 行モノリス. 論理単位ごとに extract.

# 実績バッチ用カウンター閾値 (全期間、LOOKBACK_DAYS で覆われる範囲全部使う)
# 以前は 2024-01-01 以降だけ集計してたが、より昔の実績 (篝火#1 ~ 2020-12 など) も拾うように変更.
_SINCE_2024_TS = 0  # legacy 変数名のまま 0 にして「全期間」を意味させる


def _collect_per_tournament_outcomes(all_tournaments):
    """大会ごとの wins/losses (全体/class/main) + match_count + DQ uids を集計.

    Returns dict with 10 sub-dicts keyed by id(t):
      per_tournament_wins, per_tournament_losses,
      per_tournament_wins_class, per_tournament_losses_class,
      per_tournament_wins_main, per_tournament_losses_main,
      per_tournament_match_count,
      per_tournament_dq_uids, per_tournament_dq_uids_main, per_tournament_dq_uids_class.
    """
    acc = _OutcomesAcc()
    for t in all_tournaments:
        acc.add(t)
    return acc.result()


class _OutcomesAcc:
    """_collect_per_tournament_outcomes を 1 大会ずつ足す形にしたもの (build_spr_uf_aggregates のメインループで足し、
    全大会の試合を別にもう 1 周しない。足す順が同じなら結果 (値・dict の並び) は同じ)。"""

    def __init__(self):
        self.per_tournament_wins = defaultdict(lambda: defaultdict(int))   # tid -> uid -> wins
        self.per_tournament_losses = defaultdict(lambda: defaultdict(int)) # tid -> uid -> losses
        # class-only バリエーション: parent event id をキーに class match のみ集計.
        # class_virtual entry の wins/losses はこっちを参照する.
        self.per_tournament_wins_class = defaultdict(lambda: defaultdict(int))
        self.per_tournament_losses_class = defaultdict(lambda: defaultdict(int))
        self.per_tournament_wins_main = defaultdict(lambda: defaultdict(int))
        self.per_tournament_losses_main = defaultdict(lambda: defaultdict(int))
        self.per_tournament_match_count = {}
        self.per_tournament_dq_uids = {}
        self.per_tournament_dq_uids_main = {}
        self.per_tournament_dq_uids_class = {}

    def add(self, t):
        tid = id(t)
        self.per_tournament_match_count[tid] = len(t.matches)
        self.per_tournament_dq_uids[tid] = set(t.dq_player_ids or [])
        self.per_tournament_dq_uids_main[tid] = set(getattr(t, 'dq_player_ids_main', None) or [])
        self.per_tournament_dq_uids_class[tid] = set(getattr(t, 'dq_player_ids_class', None) or [])
        for m in t.matches:
            self.per_tournament_wins[tid][m.winner_id] += 1
            self.per_tournament_losses[tid][m.loser_id] += 1
            if bool(getattr(m, 'is_class', False)):
                self.per_tournament_wins_class[tid][m.winner_id] += 1
                self.per_tournament_losses_class[tid][m.loser_id] += 1
            else:
                self.per_tournament_wins_main[tid][m.winner_id] += 1
                self.per_tournament_losses_main[tid][m.loser_id] += 1

    def result(self) -> dict:
        return {
            'per_tournament_wins': self.per_tournament_wins,
            'per_tournament_losses': self.per_tournament_losses,
            'per_tournament_wins_class': self.per_tournament_wins_class,
            'per_tournament_losses_class': self.per_tournament_losses_class,
            'per_tournament_wins_main': self.per_tournament_wins_main,
            'per_tournament_losses_main': self.per_tournament_losses_main,
            'per_tournament_match_count': self.per_tournament_match_count,
            'per_tournament_dq_uids': self.per_tournament_dq_uids,
            'per_tournament_dq_uids_main': self.per_tournament_dq_uids_main,
            'per_tournament_dq_uids_class': self.per_tournament_dq_uids_class,
        }


def _collect_player_first_tour(all_tournaments):
    """デビュー戦検出用: ガチ/非ガチ問わず全ての大会で最古参加を記録. uid -> (ts, tid)."""
    player_first_tour = {}
    for t in all_tournaments:
        _first_tour_add(player_first_tour, t)
    return player_first_tour


def _first_tour_add(player_first_tour: dict, t) -> None:
    """_collect_player_first_tour の 1 大会分 (同じ順に足せば結果は同じ)。"""
    tid_dt = id(t)
    for m in t.matches:
        for u in (m.winner_id, m.loser_id):
            if u not in player_first_tour or t.timestamp < player_first_tour[u][0]:
                player_first_tour[u] = (t.timestamp, tid_dt)


def _record_gated_uf(m, sw, sl, meta_t, player_max_uf, player_top_ufs=None):
    """土日大会の upset match 1 件を gated UF 集計に反映 (max_uf 更新 + top_ufs 追加).

    player_top_ufs=None なら max_uf の更新のみ (= 1 年窓の並行集計用)."""
    uf = placement_to_w2w(sw) - placement_to_w2w(sl)
    if uf <= 0:
        return
    uf_entry = {
        'uf': uf,
        'opp_uid': m.loser_id,
        'winner_seed': sw,
        'opp_seed': sl,
        'event_id': meta_t.get('event_id'),
        'tournament_name': meta_t.get('tname'),
        'event_name': meta_t.get('ename'),
        'date': meta_t.get('date'),
        'is_weekend': meta_t.get('is_weekend'),
        'is_pre': meta_t.get('is_pre', False),
        'is_restricted': meta_t.get('is_restricted', False),
        'is_lower_class': meta_t.get('is_lower_class', False),
        'p_score': m.winner_score,
        'o_score': m.loser_score,
    }
    cur = player_max_uf.get(m.winner_id)
    if cur is None or uf > cur['uf']:
        player_max_uf[m.winner_id] = uf_entry
    if player_top_ufs is not None:
        lst = player_top_ufs[m.winner_id]
        lst.append(uf_entry)
        # 選手ページに出すのは top_uf_key の上位 TOP_UFS_N 件だけ (TopUfs) なので、たまったら上位だけ残す (sorted は安定:
        # 同じ並びの値は足した順のまま、後から足すものは後ろ。最後に並べたときの上位 TOP_UFS_N 件は全件を並べたときと同じ)
        if len(lst) >= 2 * TOP_UFS_N:
            lst.sort(key=top_uf_key)
            del lst[TOP_UFS_N:]


TOP_UFS_N = 20   # 選手ページに出す番狂わせの数 (spsp/output._build_top_ufs_resolved)


def top_uf_key(x):
    """番狂わせの並び: UF の大きい順、同じ UF なら日付の新しい順。"""
    return (-x['uf'], -(int((x.get('date') or '1970-01-01').replace('-', ''))))


class OppCounts:
    """選手ごとの「同じ相手との対戦数」(player_vs_opp_count)。読むのは選手ごとの最大値だけ (max_per_player)。
    以前は {uid: {相手: 数}} の 2 段の dict (北米で 数百万組)。ここでは (uid, 相手) の組を 1 つの int にして配列に足し、
    最後に数える。uid が 0 以上 2^31 未満の int でないものが来たら以前の dict に切り替える。"""

    def __init__(self):
        self._a = array('q')
        self._d = None

    def add(self, u, v):
        if self._d is None:
            if type(u) is int and type(v) is int and 0 <= u < 1 << 31 and 0 <= v < 1 << 31:
                self._a.append(u << 32 | v)
                return
            self._d = defaultdict(lambda: defaultdict(int))
            for c in self._a.tolist():
                self._d[c >> 32][c & 0xFFFFFFFF] += 1
            self._a = None
        self._d[u][v] += 1

    def max_per_player(self) -> dict:
        """{uid: 一番多く対戦した相手との数} (uid は最初に出た順。以前の {u: max(d.values()) for u, d in ...} と同じ)。"""
        if self._d is not None:
            return {u: max(d.values()) for u, d in self._d.items() if d}
        if not len(self._a):
            return {}
        codes = np.frombuffer(self._a, dtype=np.int64)
        pairs, counts = np.unique(codes, return_counts=True)
        pu = pairs >> 32
        starts = np.flatnonzero(np.r_[True, pu[1:] != pu[:-1]])
        mx = np.maximum.reduceat(counts, starts)
        uids = pu[starts]
        _u, first = np.unique(codes >> 32, return_index=True)   # _u == uids (どちらも uid の昇順)
        order = np.argsort(first, kind='stable')
        return dict(zip(uids[order].tolist(), mx[order].tolist()))


class _TourHistoryAcc:
    """player_tour_history (uid → [(ts, tid), ...]) を足しながら配列の表 (spsp/columnar.TourHistory) に持つ。
    以前は dict に tuple の list を作ってから build で詰めていた。int でないものが来たら以前の dict に切り替える。"""

    def __init__(self):
        self._log = TourHistory()
        self._d = None

    def append(self, u, ts, tid):
        if self._d is None:
            if type(u) is int and type(ts) is int and type(tid) is int:
                self._log.add(u, ts, tid)
                return
            self._d = defaultdict(list)
            for k, v in self._log.items():
                self._d[k].extend(v)
            self._log = None
        self._d[u].append((ts, tid))

    def result(self):
        return self._log if self._d is None else self._d


class TopUfs(Mapping):
    """player_top_ufs (uid → 番狂わせの dict の list) のうち、選手ページに出す上位 TOP_UFS_N 件だけを top_uf_key の順で
    持つ (以前は全件を 15 項目の dict で build の最後まで持っていた。北米で 数十万件)。1 件は値の tuple で持ち、引くと
    元と同じキーの並びの dict の list を作る。読む側は sorted(.get(uid, []), key=top_uf_key)[:TOP_UFS_N] だけなので、
    並べ直しても同じ (sorted は安定、上位だけ残しても先頭の TOP_UFS_N 件は同じ)。"""

    def __init__(self, d):
        self._keys = None
        self._rows = {}
        for uid, lst in d.items():
            top = sorted(lst, key=top_uf_key)[:TOP_UFS_N]
            if self._keys is None and top:
                self._keys = tuple(top[0])
            if all(type(x) is dict and tuple(x) == self._keys for x in top):
                self._rows[uid] = tuple(tuple(x.values()) for x in top)
            else:
                self._rows[uid] = top   # キーの違う dict があればその選手だけ dict のまま

    def get(self, uid, default=None):
        r = self._rows.get(uid)
        if r is None:
            return default
        if type(r) is list:
            return r
        k = self._keys
        return [dict(zip(k, x)) for x in r]

    def __getitem__(self, uid):
        r = self.get(uid)
        if r is None:
            raise KeyError(uid)
        return r

    def __contains__(self, uid):
        return uid in self._rows

    def __iter__(self):
        return iter(self._rows)

    def __len__(self):
        return len(self._rows)


def _record_uf_achievement(m, sw, sl, meta_t, player_max_uf_ach):
    """UF achievement (no-gating, all-time): max UF per player."""
    if sw is None or sl is None or sw <= sl:
        return
    uf = placement_to_w2w(sw) - placement_to_w2w(sl)
    if uf <= 0:
        return
    cur = player_max_uf_ach.get(m.winner_id)
    if cur is None or uf > cur['uf']:
        player_max_uf_ach[m.winner_id] = {
            'uf': uf,
            'opp_uid': m.loser_id,
            'winner_seed': sw,
            'opp_seed': sl,
            'event_id': meta_t.get('event_id'),
            'tournament_name': meta_t.get('tname'),
            'date': meta_t.get('date'),
        }


def _advance_2024_counters(t, participants_t, seeds_map, meta_t, *,
                           player_tour_count_2024, player_match_count_2024,
                           player_win_count_2024, player_vs_opp_count,
                           player_max_uf_ach=None):
    """2024+ (= 全期間) のプレイヤー別カウンタを 1 大会分進める.

    player_max_uf_ach が指定された場合は UF achievement も同時更新 (= 休日大会のみ).
    """
    if t.timestamp < _SINCE_2024_TS:
        return
    for uid_p in participants_t:
        player_tour_count_2024[uid_p] += 1
    for m in t.matches:
        player_match_count_2024[m.winner_id] += 1
        player_match_count_2024[m.loser_id] += 1
        player_win_count_2024[m.winner_id] += 1
        player_vs_opp_count.add(m.winner_id, m.loser_id)
        player_vs_opp_count.add(m.loser_id, m.winner_id)
        if player_max_uf_ach is not None:
            sw = seeds_map.get(m.winner_id)
            sl = seeds_map.get(m.loser_id)
            _record_uf_achievement(m, sw, sl, meta_t, player_max_uf_ach)


def _record_losers_runs(t, tid, meta_t, per_player,
                        per_tournament_losers_runs, player_max_losers_run,
                        player_max_losers_run_1y=None):
    """ルーザーズラン (= LB run from WB drop) を 1 大会分集計.

    player_max_losers_run_1y が与えられたら同じ entry で 1 年窓の max も更新する."""
    lr_map = compute_losers_runs(t.matches)
    if not lr_map:
        return
    per_tournament_losers_runs[tid] = lr_map
    for uid_lr, run_len in lr_map.items():
        entry = None
        for d in (player_max_losers_run, player_max_losers_run_1y):
            if d is None:
                continue
            cur_lr = d.get(uid_lr)
            if cur_lr is None or run_len > cur_lr['run']:
                if entry is None:
                    entry = {
                        'run': run_len,
                        'event_id': meta_t.get('event_id'),
                        'tournament_name': meta_t.get('tname'),
                        'date': meta_t.get('date'),
                        'place': (per_player.get(uid_lr) or {}).get('place'),
                        'nent': meta_t.get('nent'),
                    }
                d[uid_lr] = entry


def _record_spr_achievements(meta_t, per_player,
                             player_max_spr_ach, player_min_spr_ach,
                             player_max_spr_1y=None, player_min_spr_1y=None):
    """SPR achievement (上振れ: max spr>0, 下振れ: min spr<0) を 1 大会分集計.

    *_1y が与えられたら同じ entry で 1 年窓の max/min も更新する."""
    for u, info in per_player.items():
        spr = info.get('spr', 0)
        if not spr:      # 0 か None (ランダムシード大会)
            continue
        entry = {
            'spr': spr,
            'seed': info['seed'],
            'place': info['place'],
            'event_id': meta_t.get('event_id'),
            'tournament_name': meta_t.get('tname'),
            'date': meta_t.get('date'),
        }
        if spr > 0:
            for d in (player_max_spr_ach, player_max_spr_1y):
                if d is None:
                    continue
                cur = d.get(u)
                if cur is None or spr > cur['spr']:
                    d[u] = entry
        else:
            for d in (player_min_spr_ach, player_min_spr_1y):
                if d is None:
                    continue
                cur_n = d.get(u)
                if cur_n is None or spr < cur_n['spr']:
                    d[u] = entry


def build_spr_uf_aggregates(all_tournaments, placements_per_t, t_seed_info, t_meta_by_id,
                            verbose=True, recent_cutoff_ts=None):
    """Build SPR/UF/losers-run aggregates per player and per-tournament wins/losses.

    Args:
        all_tournaments: list of Tournament objects.
        placements_per_t: dict[tid -> {uid: place}].
        t_seed_info: dict[tid -> {uid: {'seed', 'place', 'spr', ...}}] (build_placements_seeds 結果).
        t_meta_by_id: dict[id(t) -> meta dict].
        verbose: print progress.
        recent_cutoff_ts: 与えたら max_uf / max_spr / min_spr / max_losers_run の
            「この timestamp 以降のみ」版 (*_1y) も並行集計する
            (= プレイヤーページのハイライトを直近 1 年に絞る用).

    Returns:
        dict containing all per-player and per-tournament aggregates required by
        build_tjpr_ranking.main() downstream. See keys below.
    """
    if verbose:
        print("Loading seeds.json + computing per-tournament SPR + per-match UF (gated) ...", flush=True)
    # 1. 大会別 wins/losses/DQ/match_count と 2. デビュー戦 player_first_tour は、下のメインループで 1 大会ずつ足す
    # (以前はそれぞれ全大会を 1 周していた。試合を大会ごとに読み直すとき (spsp/lazy_matches) に周回が減る。結果は同じ)
    _outcomes = _OutcomesAcc()
    player_first_tour = {}
    # 試合にだけ出てくる参加者 (順位表に無い uid、最初に出た順) — build_at_time_per_tournament が参加者を集めるのに使う
    match_extra_uids: dict = {}

    # ── プレイヤー別 aggregates の入れ物 ──
    player_max_uf = {}                # uid -> dict (gated; for max-UF highlight on player page)
    player_top_ufs = defaultdict(list)  # uid -> list of upset entries
    uf_tour_count = defaultdict(int)  # mirror of learn_bt_gated_tour's tour_count for gating UF
    player_match_count = defaultdict(int)  # uid -> total matches played
    player_max_uf_ach = {}            # all-time, no gating
    player_max_spr_ach = {}           # 上振れ
    player_min_spr_ach = {}           # 下振れ
    player_max_losers_run = {}        # uid -> {'run', ...}
    # 直近 1 年窓 (recent_cutoff_ts 以降のみ; ハイライト表示用)
    player_max_uf_1y = {}
    player_max_spr_1y = {}
    player_min_spr_1y = {}
    player_max_losers_run_1y = {}
    per_tournament_losers_runs = {}   # tid -> {uid: run_length}
    player_tour_count_2024 = defaultdict(int)
    player_match_count_2024 = defaultdict(int)
    player_win_count_2024 = defaultdict(int)
    player_vs_opp_count = OppCounts()
    # ── レーダーチャート用 (各プレイヤーの直近 10 大会、ガチ大会のみ、期間制限なし) ──
    player_tour_history = _TourHistoryAcc()   # uid -> [(timestamp, t_id), ...]

    # 3. メインループ: UF/SPR/match counts/losers-run/achievements を 1 周で全部集計
    n_with_seeds = 0
    for t in all_tournaments:
        tid = id(t)
        _outcomes.add(t)
        _first_tour_add(player_first_tour, t)
        _seen = set(placements_per_t.get(tid, {}).keys())
        _extra = []
        for m in t.matches:
            for u in (m.winner_id, m.loser_id):
                if u not in _seen:
                    _seen.add(u); _extra.append(u)
        match_extra_uids[tid] = tuple(_extra)
        # 小規模大会 (= is_small): ランキング集計対象外. seed/UF/radar/match_count
        # を一切更新しない. (per-tournament JSON や 大会一覧 / local には別途含める.)
        if t_meta_by_id[tid].get('is_small'):
            continue
        placements = placements_per_t.get(tid, {})
        if not placements:
            continue
        # Count matches per player
        for m in t.matches:
            player_match_count[m.winner_id] += 1
            player_match_count[m.loser_id] += 1
        seeds_map = load_event_seeds(t.event_path)  # uid -> seed_num (実シード)
        if seeds_map:
            n_with_seeds += 1
        per_player = t_seed_info.get(tid, {})
        meta_t = t_meta_by_id[tid]
        is_wk = meta_t['is_weekend']
        participants_t = set()
        for m in t.matches:
            participants_t.add(m.winner_id)
            participants_t.add(m.loser_id)
        # Advance tour_count for all participants (mirroring learn_bt_gated_tour)
        for uid in participants_t:
            uf_tour_count[uid] += 1

        if not is_wk:
            # 平日大会: UF/SPR/losers-run/UF-ach は集計対象外。
            # レーダーチャート用 (期間制限なし、ガチ大会のみ) + 2024+ カウンタだけ進める
            if not meta_t.get('is_non_serious') and not meta_t.get('is_gf_missing'):
                for u in participants_t:
                    player_tour_history.append(u, t.timestamp, tid)
            _advance_2024_counters(
                t, participants_t, seeds_map, meta_t,
                player_tour_count_2024=player_tour_count_2024,
                player_match_count_2024=player_match_count_2024,
                player_win_count_2024=player_win_count_2024,
                player_vs_opp_count=player_vs_opp_count,
                player_max_uf_ach=None,
            )
            continue

        # ── 休日大会 ──
        in_1y = recent_cutoff_ts is not None and t.timestamp >= recent_cutoff_ts
        # gated UF: 各 match を見て upset を記録 (ランダムシード大会はシードに意味がないので飛ばす)
        is_rs = meta_t.get('is_random_seed', False)
        for m in ([] if is_rs else t.matches):
            sw = seeds_map.get(m.winner_id)
            sl = seeds_map.get(m.loser_id)
            if sw is None or sl is None or sw <= sl:
                continue
            _record_gated_uf(m, sw, sl, meta_t, player_max_uf, player_top_ufs)
            if in_1y:
                _record_gated_uf(m, sw, sl, meta_t, player_max_uf_1y)
        # レーダーチャート用 (期間制限なし、ガチ大会のみ)
        if not meta_t.get('is_non_serious'):
            for u in participants_t:
                player_tour_history.append(u, t.timestamp, tid)
        # 実績バッジ用カウンター (gating なし、全期間) + UF achievement (休日のみ)
        _advance_2024_counters(
            t, participants_t, seeds_map, meta_t,
            player_tour_count_2024=player_tour_count_2024,
            player_match_count_2024=player_match_count_2024,
            player_win_count_2024=player_win_count_2024,
            player_vs_opp_count=player_vs_opp_count,
            player_max_uf_ach=None if is_rs else player_max_uf_ach,
        )
        # ルーザーズラン + SPR achievement (休日大会、全期間)
        if t.timestamp >= _SINCE_2024_TS:
            _record_losers_runs(
                t, tid, meta_t, per_player,
                per_tournament_losers_runs, player_max_losers_run,
                player_max_losers_run_1y if in_1y else None,
            )
            _record_spr_achievements(
                meta_t, per_player,
                player_max_spr_ach, player_min_spr_ach,
                player_max_spr_1y if in_1y else None,
                player_min_spr_1y if in_1y else None,
            )

    if verbose:
        print(f"  {n_with_seeds}/{len(all_tournaments)} tournaments have seeds.json", flush=True)
        print(f"  {len(player_max_uf)} players have at least one (gated) upset on record", flush=True)
        print(f"  {len(player_max_uf_ach)} / {len(player_max_spr_ach)} players have UF / SPR achievement records (lifetime)", flush=True)
    return {
        'player_max_uf': player_max_uf,
        'player_top_ufs': player_top_ufs,
        'uf_tour_count': uf_tour_count,
        'player_match_count': player_match_count,
        'player_max_uf_ach': player_max_uf_ach,
        'player_max_uf_1y': player_max_uf_1y,
        'player_max_spr_1y': player_max_spr_1y,
        'player_min_spr_1y': player_min_spr_1y,
        'player_max_losers_run_1y': player_max_losers_run_1y,
        'player_max_spr_ach': player_max_spr_ach,
        'player_min_spr_ach': player_min_spr_ach,
        'player_max_losers_run': player_max_losers_run,
        'per_tournament_losers_runs': per_tournament_losers_runs,
        'player_tour_count_2024': player_tour_count_2024,
        'player_match_count_2024': player_match_count_2024,
        'player_win_count_2024': player_win_count_2024,
        'player_vs_opp_count': player_vs_opp_count,
        'player_tour_history': player_tour_history.result(),
        'player_first_tour': player_first_tour,
        'n_with_seeds': n_with_seeds,
        'match_extra_uids': match_extra_uids,
        **_outcomes.result(),
    }

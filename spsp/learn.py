"""V4 BT / cascade 学習関数モジュール.

build_tjpr_ranking.py から BT-all / BT-gated 学習と TJPR cascade 計算を移動.

依存:
  - v4.common: ELO_PER_UNIT, EXCLUDE_TOP1, SIGMA_FLOOR, GATED_M_TOUR,
               BT_PEAK_WINDOW_SEC, INACTIVITY_SIGMA_*, LV*_PARAMS, LV*_FILTER,
               td_exp, _bracket_order_key
  - v4.raters.openskill_rater: OpenSkillBTRater
  - v4.tournament_utils: build_de_losers_round_participants
  - v4.tournament_utils: placement_to_tier
"""
from __future__ import annotations

import datetime as dt
import math
from array import array
from collections import defaultdict, deque
from collections.abc import Mapping

import numpy as np

from spsp import common as _common  # BT_PEAK_WINDOW_SEC は configure() で変わりうるので属性参照
from spsp.common import (
    ELO_PER_UNIT, EXCLUDE_TOP1,
    SIGMA_FLOOR, GATED_M_TOUR,
    INACTIVITY_SIGMA_TARGET, INACTIVITY_SIGMA_INFLECTION_DAYS,
    INACTIVITY_SIGMA_SCALE_DAYS, INACTIVITY_SIGMA_GRACE_DAYS,
    LV1_PARAMS, LV2_PARAMS, LV3_FILTER, LV4_FILTER, LV5_FILTER,
    td_exp, _bracket_order_key,
    OVERSEAS_UIDS, jp_topn_filter,
)
from spsp.raters.openskill_rater import OpenSkillBTRater
from spsp.tournament_utils import build_de_losers_round_participants
from spsp.tournament_utils import placement_to_tier


# ─── from build_tjpr_ranking (135-179): Peak180Tracker ───
# ── BT learners ──

class Peak180Tracker:
    """Per-uid window-peak tracker. Window = BT_PEAK_WINDOW_DAYS.

    history は (ts, ord) を時刻昇順で保持. current_peak は window 内の max.
    update(ts, ord_) を呼ぶたびに、cutoff (= ts - window) より古いエントリを front から prune し
    新しい (ts, ord_) を back に追加、current_peak を再計算.
    """
    __slots__ = ('history', 'current_peak')
    def __init__(self):
        self.history = deque()
        self.current_peak = float('-inf')

    def update(self, ts: int, ord_: float):
        # prune (rolloff)
        cutoff = ts - _common.BT_PEAK_WINDOW_SEC
        popped_peak = False
        while self.history and self.history[0][0] < cutoff:
            _, popped_ord = self.history.popleft()
            if popped_ord >= self.current_peak - 1e-12:
                popped_peak = True
        if popped_peak:
            # recompute
            self.current_peak = max((o for _, o in self.history), default=float('-inf'))
        # add
        self.history.append((ts, ord_))
        if ord_ > self.current_peak:
            self.current_peak = ord_
        return self.current_peak

    def peak_at(self, ts: int) -> float:
        """Recompute peak at given ts (= prune to ts - window, return max). 副作用なし版.
        history は破壊的に変更されるが (= rolloff は不可逆) 副作用最小.
        """
        cutoff = ts - _common.BT_PEAK_WINDOW_SEC
        popped_peak = False
        while self.history and self.history[0][0] < cutoff:
            _, popped_ord = self.history.popleft()
            if popped_ord >= self.current_peak - 1e-12:
                popped_peak = True
        if popped_peak:
            self.current_peak = max((o for _, o in self.history), default=float('-inf'))
        return self.current_peak


# ─── from build_tjpr_ranking (182-217): learn_bt_all ───
def learn_bt_all(prior, t_meta_by_id, snap_ts_list=None):
    """BT-all rolling. snap_ts_list を渡すと各 snap 時点での bt_ord 全 uid snapshot を返す.

    snap_ts_list は ascending sorted で渡すと効率的 (= 1 pass で全 snap 取得).
    返り値: (bt_current_ord, rating_history, bt_at_snap | None)
      bt_at_snap[snap_ts] = {uid: ord} (各 snap 時点での bt_all 状態)
    """
    b = OpenSkillBTRater()
    rating_history = defaultdict(list)
    snap_sorted = sorted(snap_ts_list) if snap_ts_list else []
    snap_idx = 0
    bt_at_snap: dict[int, dict[int, float]] = {} if snap_ts_list else None
    for t in prior:
        # snap_ts < t.timestamp の境界を跨ぐ瞬間に capture (= t を処理する前の state)
        while snap_idx < len(snap_sorted) and t.timestamp >= snap_sorted[snap_idx]:
            bt_at_snap[snap_sorted[snap_idx]] = {uid: b._get(uid).ordinal() for uid in b.ratings}
            snap_idx += 1
        meta = t_meta_by_id[id(t)]
        # ガチ大会じゃないやつは学習対象外
        if meta.get('is_non_serious') or meta.get('is_gf_missing') or meta.get('is_small'): continue
        # クラス virtual は親 event で学習済 → skip
        if meta.get('is_class_virtual'): continue
        match_uids = set()
        for m in t.matches:
            match_uids.add(m.winner_id); match_uids.add(m.loser_id)
        before_ratings = {uid: b._get(uid).ordinal() for uid in match_uids}
        for m in sorted(t.matches, key=_bracket_order_key):
            b.update_match(m.winner_id, m.loser_id, m.timestamp)
        for uid in match_uids:
            after = b.rating(uid)
            rating_history[uid].append((id(t), after, after - before_ratings.get(uid, 0)))
    # 残りの snap_ts (= 全 tournament 処理後の state)
    while snap_idx < len(snap_sorted):
        bt_at_snap[snap_sorted[snap_idx]] = {uid: b._get(uid).ordinal() for uid in b.ratings}
        snap_idx += 1
    return {uid: b.rating(uid) for uid in b.ratings}, rating_history, bt_at_snap


# ─── from build_tjpr_ranking (220-234): _inactivity_sigma_target ───
def _inactivity_sigma_target(days_inactive: float) -> float:
    """不参加期間 (days) から、σ を膨らませる「目標値」を返す.

    Sigmoid 曲線: floor + (target - floor) × sigmoid((days - inflection) / scale)
      - days <= grace: floor (= SIGMA_FLOOR) を返す → 何もしない
      - days = inflection: floor と target の中間
      - days → ∞: target に漸近

    実 σ は max(current_σ, target_value) で適用するため、減らすことは無い.
    """
    if days_inactive <= INACTIVITY_SIGMA_GRACE_DAYS:
        return SIGMA_FLOOR
    x = (days_inactive - INACTIVITY_SIGMA_INFLECTION_DAYS) / INACTIVITY_SIGMA_SCALE_DAYS
    s = 1.0 / (1.0 + math.exp(-x))
    return SIGMA_FLOOR + (INACTIVITY_SIGMA_TARGET - SIGMA_FLOOR) * s


# ─── from build_tjpr_ranking (237-250): _apply_inactivity_sigma ───
def _apply_inactivity_sigma(b, uid: int, last_seen_ts: dict, current_ts: int):
    """Inactive 期間に応じて uid の σ を膨らませる. b.ratings[uid] を in-place 更新."""
    if uid not in b.ratings:
        return
    last = last_seen_ts.get(uid)
    if last is None:
        return
    days = max(0.0, (current_ts - last) / 86400.0)
    if days <= INACTIVITY_SIGMA_GRACE_DAYS:
        return
    target = _inactivity_sigma_target(days)
    r = b.ratings[uid]
    if target > r.sigma:
        b.ratings[uid] = b.model.rating(mu=r.mu, sigma=target)


# ─── from build_tjpr_ranking (253-265): _apply_sigma_floor_keep_ordinal ───
def _apply_sigma_floor_keep_ordinal(model, r, floor):
    """σ < floor の場合、σ を floor に戻しつつ ordinal (= μ - z*σ) を保つよう μ も補正する.

    OpenSkill の ordinal は μ - z * σ (z = model.z, デフォルト 3).
    σ floor で σ↑ しただけだと ordinal が下がってしまい、「勝ったのに ordinal がマイナス」
    という症状が出る。μ も z*Δσ だけ持ち上げれば ordinal は σ 補正前と同じになり、
    試合の自然な勝敗変化 (μ の変化) のみが ordinal に反映される。
    """
    if floor <= 0 or r.sigma >= floor:
        return r
    delta_sigma = floor - r.sigma
    z = getattr(model, 'z', 3.0)
    return model.rating(mu=r.mu + z * delta_sigma, sigma=floor)


# ─── from build_tjpr_ranking (272-465): learn_bt_gated_tour ───
def _gated_capture_snap_state(snap_ts, b, peak180):
    """snap_ts での BT internal ord と peak180 state を capture.

    peak180 tracker は破壊的 rolloff される (= snap_sorted ascending 順なので safe).
    窓内に history が無い uid は player の current ord を carry-forward する.
    """
    bt_internal = {uid: b._get(uid).ordinal() for uid in b.ratings}
    peak_dict = {}
    for uid, tr in peak180.items():
        peak_v = tr.peak_at(snap_ts)
        if peak_v == float('-inf'):
            peak_v = b._get(uid).ordinal()
        peak_dict[uid] = peak_v
    return bt_internal, peak_dict


def _gated_apply_match(m, t, b, tour_count, M, is_wk_event,
                       match_wins, player_matches, match_id_deltas, any_used):
    """1 試合の BT-gated 更新: gating, sigma floor, delta record + player_matches/match_id_deltas 記録."""
    # phase 内クラス bracket (Bクラス等) は休日大会でも平日大会扱い: レアのみ更新.
    is_wk_for_bt = is_wk_event and not getattr(m, 'is_class', False)
    wc, lc = tour_count[m.winner_id], tour_count[m.loser_id]
    update_w = is_wk_for_bt or (wc <= M)
    update_l = is_wk_for_bt or (lc <= M)
    is_cls = bool(getattr(m, 'is_class', False))
    if not (update_w or update_l):
        # Both gated out: still record the match (no rating movement)
        player_matches[m.winner_id].append((t.timestamp, id(t), m.loser_id, True,  0.0, is_cls, m, False))
        player_matches[m.loser_id ].append((t.timestamp, id(t), m.winner_id, False, 0.0, is_cls, m, False))
        match_id_deltas[id(m)] = {'d_w': 0.0, 'd_l': 0.0, 'w_tracked': False, 'l_tracked': False}
        return
    w_r = b._get(m.winner_id)
    l_r = b._get(m.loser_id)
    before_w = w_r.ordinal()
    before_l = l_r.ordinal()
    new_ratings = b.model.rate([[w_r], [l_r]], ranks=[1, 2])
    d_w = 0.0; d_l = 0.0
    if update_w:
        nw = _apply_sigma_floor_keep_ordinal(b.model, new_ratings[0][0], SIGMA_FLOOR)
        d_w = nw.ordinal() - before_w
        if d_w > 0:
            match_wins[m.winner_id].append((id(t), m.loser_id, d_w, m))
        b.ratings[m.winner_id] = nw
        any_used[m.winner_id] = True
    if update_l:
        nl = _apply_sigma_floor_keep_ordinal(b.model, new_ratings[1][0], SIGMA_FLOOR)
        d_l = nl.ordinal() - before_l
        b.ratings[m.loser_id] = nl
        any_used[m.loser_id] = True
    # 表示用 delta: rate() 数値誤差で逆符号になる稀ケースは 0 に clamp.
    d_w_disp = max(0.0, d_w)
    d_l_disp = min(0.0, d_l)
    # tup[8] = bt_tracked (= 実際に BT 学習で更新された)
    player_matches[m.winner_id].append((t.timestamp, id(t), m.loser_id, True,  d_w_disp, is_cls, m, update_w))
    player_matches[m.loser_id ].append((t.timestamp, id(t), m.winner_id, False, d_l_disp, is_cls, m, update_l))
    match_id_deltas[id(m)] = {'d_w': d_w_disp, 'd_l': d_l_disp,
                              'w_tracked': bool(update_w), 'l_tracked': bool(update_l)}


def learn_bt_gated_tour(prior, t_meta_by_id, M=20, snap_ts_list=None):
    """BT-gated rolling. snap_ts_list を渡すと各 snap 時点での bt_internal_ord + peak180 ord を返す.

    返り値の末尾に bt_internal_at_snap (dict[snap_ts -> {uid: ord}]) と
    peak180_at_snap (dict[snap_ts -> {uid: peak_ord}]) を追加.
    """
    b = OpenSkillBTRater()
    tour_count = defaultdict(int)
    last_seen_ts: dict[int, int] = {}  # uid -> last tournament timestamp seen
    bt_used = defaultdict(dict)
    # rating_history[uid] = [(tid, ord_after, current_delta, peak180_delta_tournament, peak180_ord_after), ...]
    rating_history = defaultdict(list)
    # Peak180 tracker per uid (180-day rolling peak ordinal). Updates ONCE per tournament-end
    # with the player's final ord after all matches that tournament — so mid-tournament transient
    # peaks don't pollute the score (user-friendly: 全体負け越し → peak180 上がらない).
    peak180 = defaultdict(Peak180Tracker)
    # bt_timeline[uid] = list of (ts, mu, sigma, ord) appended at each tournament-end where
    # the uid's rating was updated. Used by the player page chart as the single source of truth.
    bt_timeline = defaultdict(list)
    # 試合単位の勝利 BT delta を記録 (winner uid -> [(tid, opp_uid, delta), ...])
    match_wins = defaultdict(list)
    # match_id_deltas[id(m)] = {'d_w': float, 'd_l': float, 'w_tracked': bool, 'l_tracked': bool}
    match_id_deltas = {}
    # 試合単位の全履歴 (勝ち負け両方、各 uid 視点). 直対履歴用.
    player_matches = defaultdict(list)
    # 各大会開始時点の BT ord state (inactivity sigma 適用後、matches 適用前) を capture.
    # bt_at_t[id(t)] = {uid: ord} (= t.standings + t.matches に登場する全 uid のみ保持)
    bt_at_t: dict[int, dict[int, float]] = {}
    # snap_ts ごとの状態 capture (= compute_ranks_at の rolling skip 用)
    snap_sorted = sorted(snap_ts_list) if snap_ts_list else []
    snap_idx = 0
    bt_internal_at_snap: dict[int, dict[int, float]] = {} if snap_ts_list else None
    peak180_at_snap: dict[int, dict[int, float]] = {} if snap_ts_list else None
    # NOTE: class virtual sub-tournaments は親 event で BT 学習済のため
    # 下のループ内で is_class_virtual の場合 skip する.
    for t in prior:
        # snap_ts < t.timestamp の境界で snapshot を取る (= t を処理する前の state).
        while snap_idx < len(snap_sorted) and t.timestamp >= snap_sorted[snap_idx]:
            snap_ts_v = snap_sorted[snap_idx]
            bt_internal_at_snap[snap_ts_v], peak180_at_snap[snap_ts_v] = \
                _gated_capture_snap_state(snap_ts_v, b, peak180)
            snap_idx += 1
        meta = t_meta_by_id[id(t)]
        # ── BT 学習対象判定 ──
        # ガチ大会じゃないやつ (おま5、タイマン等) と クラス virtual は BT 学習スキップ.
        # ただし bt_at_t snapshot は全大会で取る (TJPR 用).
        will_update_bt = not (
            meta.get('is_non_serious') or meta.get('is_gf_missing')
            or meta.get('is_class_virtual') or meta.get('is_small')
        )
        participants = set()
        for m in t.matches:
            participants.add(m.winner_id); participants.add(m.loser_id)
        # Inactivity σ inflation: 不参加期間に応じて σ を膨らませてから BT 更新する.
        if will_update_bt:
            for uid in participants:
                _apply_inactivity_sigma(b, uid, last_seen_ts, t.timestamp)
        # bt_at_t snapshot: standings + matches participants 全員の ord を保存.
        standings_uids = {uid for _, uid in t.standings}
        all_uids_for_t = standings_uids | participants
        bt_at_t[id(t)] = {uid: b._get(uid).ordinal() for uid in all_uids_for_t}
        if not will_update_bt:
            continue
        # 下位クラスイベント (Bクラス等) は直対評価では平日大会と同じ扱い.
        is_wk_event = meta['is_weekend'] and not meta.get('is_lower_class', False)
        before_ratings = {uid: b._get(uid).ordinal() for uid in participants}
        # Peak180 snapshot BEFORE tournament: 大会開始時点 (= rolloff 反映後) の peak180.
        peak180_before: dict[int, float] = {}
        for uid in participants:
            peak_v = peak180[uid].peak_at(t.timestamp)
            peak180_before[uid] = peak_v if peak_v != float('-inf') else b._get(uid).ordinal()
        any_used = {uid: False for uid in participants}
        # トーナメント内の試合を実際の進行順 (WB low→high, LB low→high, GF最後) に並べる.
        ordered_matches = sorted(t.matches, key=_bracket_order_key)
        for m in ordered_matches:
            _gated_apply_match(m, t, b, tour_count, M, is_wk_event,
                               match_wins, player_matches, match_id_deltas, any_used)
        # Per-tournament-end peak180 update: 大会終了時点の最終 ord で peak180 を 1 回更新.
        for uid in participants:
            if any_used[uid]:
                r_obj = b._get(uid)
                ord_after = r_obj.ordinal()
                peak180[uid].update(t.timestamp, ord_after)
                bt_timeline[uid].append((t.timestamp, r_obj.mu, r_obj.sigma, ord_after))
            bt_used[uid][id(t)] = any_used[uid]
            after_disp = b.rating(uid)
            current_delta = after_disp - before_ratings[uid]
            peak180_after_t = peak180[uid].current_peak if peak180[uid].current_peak != float('-inf') else b._get(uid).ordinal()
            peak180_delta_t = peak180_after_t - peak180_before[uid]
            rating_history[uid].append((id(t), after_disp, current_delta, peak180_delta_t, peak180_after_t))
        for uid in participants:
            tour_count[uid] += 1
            last_seen_ts[uid] = t.timestamp
    # 残りの snap_ts (= 全 tournament 処理後の state)
    while snap_idx < len(snap_sorted):
        snap_ts_v = snap_sorted[snap_idx]
        bt_internal_at_snap[snap_ts_v], peak180_at_snap[snap_ts_v] = \
            _gated_capture_snap_state(snap_ts_v, b, peak180)
        snap_idx += 1
    # Final peak180 snapshot at end of training (after applying rolloff at eval cutoff).
    eval_cutoff = max((t.timestamp for t in prior), default=0)
    bt_peak180_ord: dict[int, float] = {}
    for uid, tr in peak180.items():
        peak_v = tr.peak_at(eval_cutoff)
        # 180 日窓が空になった場合は player の current ord を carry-forward.
        bt_peak180_ord[uid] = peak_v if peak_v != float('-inf') else b._get(uid).ordinal()
    bt_current_ord = {uid: b.rating(uid) for uid in b.ratings}
    return bt_current_ord, bt_peak180_ord, bt_used, rating_history, tour_count, match_wins, player_matches, bt_timeline, match_id_deltas, bt_at_t, bt_internal_at_snap, peak180_at_snap


# ─── from build_tjpr_ranking (468-543): precompute_banzuke ───
# ── Banzuke ──
# 大会ごとの「1 勝でもした uid」の控え。precompute_banzuke はスナップショットの時点ごと (snap で 61 回) に全大会を回すので、
# 毎回全試合を数え直さない (値は大会の試合だけで決まる)。キーは id なので、大会と親の試合の list も控えに持って
# 別のオブジェクトに id が使い回されないようにする (試合の list が差し替えられたら数え直す)
_WON_CACHE: dict = {}
# 試合を手放しているとき (spsp/lazy_matches): 試合の塊 (LazyMatches) ごとの (塊, 勝者の集合, 試合があるか,
# 下位クラスの試合の (勝者, 敗者))。塊を作り直したとき (どの集計でも) に作るので、precompute_banzuke のための読み直しが要らない
_WON_LAZY: dict = {}


def _note_lazy(lm, lst) -> None:
    if id(lm) in _WON_LAZY:
        return
    _WON_LAZY[id(lm)] = (lm, frozenset(m.winner_id for m in lst), bool(lst),
                         tuple((m.winner_id, m.loser_id) for m in lst if getattr(m, 'is_class', False)))


def _won_lazy(lm):
    ent = _WON_LAZY.get(id(lm))
    if ent is None or ent[0] is not lm:
        _WON_LAZY.pop(id(lm), None)
        _note_lazy(lm, lm._list())
        ent = _WON_LAZY[id(lm)]
    return ent


def _register_lazy_hook():
    from spsp import lazy_matches
    if lazy_matches.ENABLED and _note_lazy not in lazy_matches.on_unpack:
        lazy_matches.on_unpack.append(_note_lazy)


_register_lazy_hook()


def _won_uids_of(pt_t, meta, parent_matches_by_eid):
    """(その大会で実戦 1 勝でもした uid の集合, 試合の情報があるか)。"""
    from spsp.lazy_matches import LazyMatches
    if meta.get('is_class_virtual'):
        _pm = parent_matches_by_eid.get(meta.get('parent_event_id'))
        key = (id(pt_t), id(_pm))
    else:
        _pm = pt_t.matches   # 試合の list ごと差し替えられたら控えを使わない
        key = (id(pt_t), 0)
    hit = _WON_CACHE.get(key)
    if hit is not None and hit[2] is pt_t and hit[3] is _pm:
        return hit[0], hit[1]   # 読むのは in だけ (frozenset のまま)
    if meta.get('is_class_virtual'):
        _class_set = {uid for _, uid in pt_t.standings}
        if isinstance(_pm, LazyMatches) and _pm:
            _pairs = [(w, l) for w, l in _won_lazy(_pm)[3] if w in _class_set and l in _class_set]
            won_uids = {w for w, _ in _pairs}
            has_match_info = bool(_pairs)
        elif _pm:
            _cls_matches = [m for m in _pm
                            if getattr(m, 'is_class', False)
                            and m.winner_id in _class_set and m.loser_id in _class_set]
            won_uids = {m.winner_id for m in _cls_matches}
            has_match_info = bool(_cls_matches)
        else:
            won_uids = set()
            has_match_info = False
    elif isinstance(_pm, LazyMatches):
        _, won_uids, has_match_info, _ = _won_lazy(_pm) if _pm else (None, frozenset(), False, ())
    else:
        won_uids = {m.winner_id for m in pt_t.matches}
        has_match_info = bool(pt_t.matches)
    won_uids = frozenset(won_uids)
    _WON_CACHE[key] = (won_uids, has_match_info, pt_t, _pm)
    return won_uids, has_match_info


def precompute_banzuke(prior, eval_date, bt_at_t, global_shift, t_meta_by_id, default_rate,
                       allow_future: bool = False):
    """大会別 raw_pts 計算用 cache を構築.

    raw_pts は「その大会開催時点での対戦相手 BT」で固定する (後の snap で再計算しない).
    bt_at_t[id(t)][uid] = 大会 t 開始時点での uid の BT ord.
    global_shift = eval 時点の global max ord × ELO_PER_UNIT - 2500 (= 共通正規化シフト).
      → 各 uid の正規化レート = ord × ELO_PER_UNIT - global_shift.
      固定 shift により、過去スナップショットでも同じ raw_pts が再現される.
    """
    # class_virtual の無勝利判定用: 親 event_id → その event の matches.
    # 下位クラスは自前 matches を持たない (試合は親 event 側) ので、親の matches を引いて、
    # 後段で「両者がそのクラス参加者である is_class 試合」だけに絞り込む (= 下位クラス内判定).
    parent_matches_by_eid = {}
    for _pt in prior:
        _pm = t_meta_by_id[id(_pt)]
        if _pm.get('is_class_virtual'):
            continue
        _pev = _pm.get('event_id')
        if _pev is not None:
            parent_matches_by_eid[_pev] = _pt.matches
    cached = []
    for pt_t in prior:
        meta = t_meta_by_id[id(pt_t)]
        # ガチ大会じゃないやつは TJPR 計算から除外
        if meta.get('is_non_serious') or meta.get('is_gf_missing') or meta.get('is_small'): continue
        nent_p = meta['nent']
        tdate = dt.datetime.fromtimestamp(pt_t.timestamp)
        if tdate > eval_date and not allow_future:
            continue
        days = (eval_date - tdate).days   # allow_future なら負 (減衰係数 > 1)。順位は共通係数なので不変
        # その大会時点での BT snapshot から正規化レートを引く.
        bt_dict = bt_at_t.get(id(pt_t), {})
        def _rate_for(uid):
            ord_v = bt_dict.get(uid)
            if ord_v is None:
                return default_rate
            return ord_v * ELO_PER_UNIT - global_shift
        # EXCLUDE_TOP1: 出場者中で最もレートの高い uid を norm_arr (= 敗者側並べる pool) から除外.
        # players list (= 各プレイヤーの tjpr_w 計算) には全員残す → 該当プレイヤーも自分を除いた
        # opponent pool で round_pts を取れる (= 自己強化の除去).
        top_rated_uid = None
        if EXCLUDE_TOP1:
            best_r = float('-inf')
            for _, uid in pt_t.standings:
                r = _rate_for(uid)
                if r > best_r:
                    best_r = r
                    top_rated_uid = uid
        seen = set(); parts = []
        for _, uid in pt_t.standings:
            if uid in seen:
                continue
            if top_rated_uid is not None and uid == top_rated_uid:
                continue
            seen.add(uid); parts.append((uid, _rate_for(uid)))
        n = len(parts)
        if n < 2:
            continue
        parts.sort(key=lambda x: x[1])
        norm_arr = np.array([p[1] for p in parts])
        lr_rounds = build_de_losers_round_participants(n)
        total_lr = len(lr_rounds)
        if total_lr == 0:
            continue
        round_indices = [np.array([s for s in sr if s < n], dtype=np.int64) for sr in lr_rounds]
        max_tier_n = placement_to_tier(n)
        # 無勝利判定用 won_uids = その大会で実戦 1 勝でもした uid 集合.
        # DQ 勝ち (相手 no-show) は data_loader で matches に含まれないため自動除外され、
        # 「実戦で 1 勝もしていない」人だけが除かれる.
        # class_virtual (クラス仮想サブ大会) は試合を親 event に持つ (自前 matches は空) ので、
        # 親 event の matches を見て無勝利判定する. ただし「下位クラス内」だけで判定するため、
        # 両者がこのクラス参加者 (= pt_t.standings) である is_class 試合に限定する.
        # (親 event 全体の qualifier / 別クラスの勝利を数えてしまわないようにする.)
        won_uids, has_match_info = _won_uids_of(pt_t, meta, parent_matches_by_eid)
        players = []; seen2 = set()
        for place, uid in pt_t.standings:
            if uid in seen2:
                continue
            seen2.add(uid)
            rw = min(max(0, max_tier_n - placement_to_tier(place)), total_lr)
            # 大会を通して 1 勝もしていない人 (= DQ で順位だけ繰り上がった等) は rw=0 (= 0pt).
            # 最速敗退 tier の人と同様、score=0 で cascade には残す (ranks.tjpr 無効化はしない).
            if has_match_info and uid not in won_uids:
                rw = 0
            # rw=0 (= 最速敗退 tier 帯 / 無勝利) の人も score=0 で cascade に残す.
            players.append((uid, rw, place))
        cached.append({
            'norm': norm_arr,
            'rounds': round_indices,
            'players': players,
            'days': days,
            't_id': id(pt_t),
            'nent': nent_p,
            'is_weekend': meta['is_weekend'],
            'is_smapa': meta['is_smapa'],
            'is_capped': bool(meta.get('is_restricted') or meta.get('is_lower_class')),
        })
    return cached


# ─── from build_tjpr_ranking (546-599): compute_bz_with_detail ───
def compute_bz_with_detail(cache, base, floor, topN, pd, pfilter=None, tfilter=None):
    """[EXPERIMENTAL] round_value = -log2(1-g) (情報量) + size_mult = log2(nent)^2.
    現行 prod は default mean(g)+eps の round_value + size=1。"""
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
                # info content of base-level winning: -log2(1-g)
                rp.append(-np.log2(np.clip(1.0 - g, 1e-3, 1.0)).mean())
            else:
                rp.append(eps)
        # size_mult: sqrt(log2(nent))
        size_w = math.sqrt(math.log2(max(t['nent'], 2)))
        for uid, rw, place in t['players']:
            if pfilter is not None and uid not in pfilter:
                continue
            raw_pts = sum(rp[:rw]) * size_w
            # raw_pts=0 (= rw=0 の最速敗退者) も pe に残す → cascade で score=0 として
            # ランクが付き「無効扱い (= ranks.tjpr=0)」にならない.
            pe[uid].append((raw_pts, age_decay, t['t_id'], place))
    scores = {}
    contributions = ContribTable()
    for uid, entries in pe.items():
        entries.sort(key=lambda x: -(x[0] * x[1]))
        total = 0.0
        rows = []
        for i, (raw_pts, age_decay, t_id, place) in enumerate(entries):
            pos_factor = 1.0 if i < topN else pd ** (i - topN + 1)
            weighted = raw_pts * age_decay * pos_factor
            total += weighted
            rows.append((t_id, raw_pts, age_decay, pos_factor, weighted))
        contributions.add(uid, rows)
        scores[uid] = total
    return scores, contributions



class Contrib(tuple):
    """compute_bz_with_detail の寄与 1 件 (選手 × 大会)。以前は 6 項目の dict (北米で約 300 万件、1 件 272 バイト)。
    読む側 (spsp/build.py・spsp/output.py) は c['t_id'] / c.get('weighted', 0) のように名前で引くだけなので、
    tuple に名前で引く [] と .get を足したもの (値は dict のときと同じオブジェクト)。"""
    __slots__ = ()
    _POS = {'t_id': 0, 'raw_points': 1, 'age_decay': 2, 'pos_factor': 3, 'weighted': 4, 'rank_in_player': 5}

    def __getitem__(self, key):
        if type(key) is str:
            return tuple.__getitem__(self, self._POS[key])
        return tuple.__getitem__(self, key)

    def get(self, key, default=None):
        i = self._POS.get(key)
        return default if i is None else tuple.__getitem__(self, i)


class ContribTable(Mapping):
    """compute_bz_with_detail の寄与: uid → [Contrib, ...] (寄与の大きい順)。以前は defaultdict(list) に Contrib を
    持っていた (北米で 選手 21 万人・約 300 万件、1 件 約 200 バイト)。ここでは列ごとの配列 (1 件 40 バイト) に持ち、
    引いたときに Contrib の list を作る。rank_in_player は list の中の位置なので持たない。
    値は入れたものがそのまま戻る (raw_points 等は float / np.float64 が混ざるので、型ごと残す)。
    読む側は .get(uid, []) / [uid] / items() で list を受け取って回すだけ (spsp/build.py・spsp/output.py)。
    同じ選手を続けて引くので、最後に引いた 1 人分の list を取っておく。"""

    def __init__(self):
        self._at = {}   # uid → (最初の行, 件数)。並びは add した順 (= 以前の defaultdict の挿入順)
        self._tid = array('q')
        self._f = (array('d'), array('d'), array('d'), array('d'))   # raw_points, age_decay, pos_factor, weighted
        self._np = {}   # 行 → np.float64 だった列の印 (列ごとのビット)。ほとんど空
        self._last = (None, None)

    def add(self, uid, rows) -> None:
        if uid in self._at:
            raise ValueError(f'ContribTable: 同じ選手を 2 回足した ({uid})')
        r0 = len(self._tid)
        for r, (t_id, *fs) in enumerate(rows, r0):
            self._tid.append(t_id)
            bits = 0
            for j, (col, x) in enumerate(zip(self._f, fs)):
                col.append(x)
                if type(x) is not float:
                    if type(x) is not np.float64:
                        raise TypeError(f'ContribTable: float でない値 ({type(x).__name__})')
                    bits |= 1 << j
            if bits:
                self._np[r] = bits
        self._at[uid] = (r0, len(self._tid) - r0)
        self._last = (None, None)

    def columns(self):
        """({uid: (最初の行, 件数)}, t_id, raw_points, pos_factor, weighted, {行: np.float64 だった列の印})。読むだけ。
        印のビットは raw_points=0・age_decay=1・pos_factor=2・weighted=3。rank_in_player = 行 - 最初の行。"""
        raw, _age, pos, w = self._f
        return self._at, self._tid, raw, pos, w, self._np

    def _list(self, uid, at):
        if self._last[0] is not None and self._last[0] == uid and type(self._last[0]) is type(uid):
            return self._last[1]
        r0, n = at
        tid = self._tid[r0:r0 + n].tolist()
        cols = [c[r0:r0 + n].tolist() for c in self._f]
        npb = self._np
        out = []
        for i in range(n):
            vals = [cols[0][i], cols[1][i], cols[2][i], cols[3][i]]
            bits = npb.get(r0 + i) if npb else None
            if bits:
                for j in range(4):
                    if bits >> j & 1:
                        vals[j] = np.float64(vals[j])
            out.append(Contrib((tid[i], vals[0], vals[1], vals[2], vals[3], i)))
        self._last = (uid, out)
        return out

    def get(self, uid, default=None):
        at = self._at.get(uid)
        return default if at is None else self._list(uid, at)

    def __getitem__(self, uid):
        at = self._at.get(uid)
        if at is None:
            raise KeyError(uid)
        return self._list(uid, at)

    def __contains__(self, uid):
        return uid in self._at

    def __iter__(self):
        return iter(self._at)

    def __len__(self):
        return len(self._at)


class LvContribs(Mapping):
    """build_tjpr_v1_with_detail の plv_contribs (uid → 採用 Lv の寄与の list) を、Lv ごとの ContribTable から
    引くだけの見え方 (以前は dict に list を入れていた。list は ContribTable と同じもの)。並びは plv と同じ。"""

    def __init__(self, plv, level_contribs):
        self._plv = plv
        self._lc = level_contribs

    def get(self, uid, default=None):
        lv = self._plv.get(uid)
        return default if lv is None else self._lc[lv - 1][uid]

    def __getitem__(self, uid):
        return self._lc[self._plv[uid] - 1][uid]

    def __contains__(self, uid):
        return uid in self._plv

    def __iter__(self):
        return iter(self._plv)

    def __len__(self):
        return len(self._plv)


# ─── from build_tjpr_ranking (602-636): build_tjpr_v1_with_detail ───
def _topn_jp(scores, n, measured_uids=None):
    """scores (= dict[uid -> score]) を score 降順で並べ、JP top-N + 該当灰色勢の UID 集合を返す."""
    rank_dict = {u: i + 1 for i, (u, _) in enumerate(sorted(scores.items(), key=lambda x: (-x[1], x[0])))}
    return jp_topn_filter(rank_dict, n, OVERSEAS_UIDS, measured_uids=measured_uids)


def build_tjpr_v1_with_detail(cache, measured_uids=None):
    # 5 層 cascade: Lv1 全員 → Lv2 top2048 → Lv3 top1024 (nent>24) → Lv4 top512 (nent>24+wk) → Lv5 top256 (nent>48+wk)
    # top-N は「JP top-N + 該当灰色勢 (= 海外 / 計測中)」(= 表示順位 jp-counter で top-N).
    sc1, ct1 = compute_bz_with_detail(cache, *LV1_PARAMS)
    filt2 = _topn_jp(sc1, 2048, measured_uids)
    sc2, ct2 = compute_bz_with_detail(cache, *LV2_PARAMS, pfilter=filt2)
    filt3 = _topn_jp(sc2, 1024, measured_uids)
    sc3, ct3 = compute_bz_with_detail(cache, *LV2_PARAMS, pfilter=filt3, tfilter=LV3_FILTER)
    filt4 = _topn_jp(sc3, 512, measured_uids)
    sc4, ct4 = compute_bz_with_detail(cache, *LV2_PARAMS, pfilter=filt4, tfilter=LV4_FILTER)
    filt5 = _topn_jp(sc4, 256, measured_uids)
    sc5, ct5 = compute_bz_with_detail(cache, *LV2_PARAMS, pfilter=filt5, tfilter=LV5_FILTER)
    level_scores = [sc1, sc2, sc3, sc4, sc5]
    level_contribs = [ct1, ct2, ct3, ct4, ct5]
    plv = {}
    plv_score = {}
    # Per-level scores for ALL players (not just their highest reached level)
    all_level_scores = defaultdict(lambda: {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0})
    for uid in level_scores[0].keys():
        for i in range(len(level_scores) - 1, -1, -1):
            if uid in level_scores[i]:
                plv[uid] = i + 1
                plv_score[uid] = level_scores[i][uid]
                break
        for i in range(len(level_scores)):
            if uid in level_scores[i]:
                all_level_scores[uid][i + 1] = level_scores[i][uid]
    final = sorted(plv.items(), key=lambda x: (-x[1], -plv_score[x[0]]))
    ranks = {u: r for r, (u, _) in enumerate(final, 1)}
    # ct1 (Lv1 contribs) も別途返す: 上位 Lv プレイヤーでも class virtual 大会の出場履歴/実績を
    # 表示するために使用する.
    # level_contribs (= [ct1..ct5]) も返す: postprocess (= BT_LV_shared cascade) が
    # 採用 LV を shared_cascade_lv に差し替えるとき、per-tour 値を引き直すのに使う.
    plv_contribs = LvContribs(plv, level_contribs)   # 採用 Lv の寄与 (引くたびに Lv の表から)
    return ranks, plv, plv_score, plv_contribs, dict(all_level_scores), ct1, level_contribs


# ─── from build_tjpr_ranking (843-941): _run_g2_parallel ───
def _run_g2_parallel(all_tournaments, t_meta_by_id, snap_ts_list, t0):
    """[EXPERIMENTAL] Train standard Glicko2 (τ=0.5 default, no rd_floor) in parallel for bt_gated rank.
    Result: dict {uid: Glicko2 rating}. Override OpenSkill peak180 for the bt_gated rank only;
    keep OpenSkill bt_internal_ord for display/chart/cache.
    Returns (bt_gated_peak180_g2, _g2_timeline, _g2_peak180_at_snap).
    """
    print("Training BT-gated Glicko2 (standard, τ=0.5) [EXPERIMENTAL] ...", flush=True)
    from glicko2 import Player as _G2Player
    class _G2Peak:
        __slots__ = ('history', 'current_peak')
        def __init__(self):
            self.history = deque()
            self.current_peak = float('-inf')
        def update(self, ts, r):
            cutoff = ts - _common.BT_PEAK_WINDOW_SEC
            popped_peak = False
            while self.history and self.history[0][0] < cutoff:
                _, old = self.history.popleft()
                if old >= self.current_peak - 1e-12: popped_peak = True
            if popped_peak:
                self.current_peak = max((x for _, x in self.history), default=float('-inf'))
            self.history.append((ts, r))
            if r > self.current_peak: self.current_peak = r
    _g2_ratings: dict[int, _G2Player] = {}
    _g2_tour_count = defaultdict(int)
    _g2_peak180 = defaultdict(_G2Peak)
    _g2_snap_sorted = sorted(snap_ts_list) if snap_ts_list else []
    _g2_snap_idx = 0
    _g2_peak180_at_snap = {} if snap_ts_list else None
    _g2_timeline: dict[int, list] = defaultdict(list)
    _G2_RD_INFL_TARGET = 128.0
    _G2_RD_INFL_INFLECTION_DAYS = 540.0
    _G2_RD_INFL_SCALE_DAYS = 180.0
    _G2_RD_INFL_GRACE_DAYS = 30.0
    _g2_last_seen_ts: dict[int, int] = {}
    def _g2_inactivity_rd_target(days: float) -> float:
        if days <= _G2_RD_INFL_GRACE_DAYS: return 0.0
        x = (days - _G2_RD_INFL_INFLECTION_DAYS) / _G2_RD_INFL_SCALE_DAYS
        s = 1.0 / (1.0 + math.exp(-x))
        return _G2_RD_INFL_TARGET * s
    def _g2_apply_inactivity_rd(uid: int, current_ts: int):
        last = _g2_last_seen_ts.get(uid)
        if last is None: return
        days = max(0.0, (current_ts - last) / 86400.0)
        if days <= _G2_RD_INFL_GRACE_DAYS: return
        target = _g2_inactivity_rd_target(days)
        if target <= 0: return
        r = _g2_ratings.get(uid)
        if r is not None and r.rd < target:
            r.rd = target
    def _g2_get(uid):
        if uid not in _g2_ratings:
            _g2_ratings[uid] = _G2Player(rating=1500.0, rd=350.0, vol=0.06)
        return _g2_ratings[uid]
    for t in all_tournaments:
        while (_g2_snap_idx < len(_g2_snap_sorted) and t.timestamp >= _g2_snap_sorted[_g2_snap_idx]):
            _stv = _g2_snap_sorted[_g2_snap_idx]
            _d = {}
            for uid, tr in _g2_peak180.items():
                v = max((x for ts_, x in tr.history if ts_ >= _stv - _common.BT_PEAK_WINDOW_SEC), default=float('-inf'))
                if v == float('-inf'): v = _g2_get(uid).rating
                _d[uid] = v
            _g2_peak180_at_snap[_stv] = _d
            _g2_snap_idx += 1
        _m = t_meta_by_id[id(t)]
        if _m.get('is_non_serious') or _m.get('is_gf_missing') or _m.get('is_class_virtual') or _m.get('is_small'):
            continue
        _wk = _m['is_weekend'] and not _m.get('is_lower_class', False)
        _parts = set()
        for _mt in t.matches:
            _parts.add(_mt.winner_id); _parts.add(_mt.loser_id)
        for uid in _parts:
            _g2_get(uid)
            _g2_apply_inactivity_rd(uid, t.timestamp)
        _ordered = sorted(t.matches, key=_bracket_order_key)
        for _mt in _ordered:
            _is_wk = _wk and not getattr(_mt, 'is_class', False)
            _wc, _lc = _g2_tour_count[_mt.winner_id], _g2_tour_count[_mt.loser_id]
            if (_is_wk or _wc <= GATED_M_TOUR) and (_is_wk or _lc <= GATED_M_TOUR):
                _w = _g2_get(_mt.winner_id); _l = _g2_get(_mt.loser_id)
                _wr, _wrd, _lr, _lrd = _w.rating, _w.rd, _l.rating, _l.rd
                _w.update_player([_lr], [_lrd], [1])
                _l.update_player([_wr], [_wrd], [0])
        for uid in _parts:
            _r = _g2_get(uid)
            _g2_peak180[uid].update(t.timestamp, _r.rating)
            _g2_timeline[uid].append((t.timestamp, _r.rating, _r.rd))
            _g2_last_seen_ts[uid] = t.timestamp
        for uid in {u for _, u in t.standings}:
            _g2_tour_count[uid] += 1
    while _g2_snap_idx < len(_g2_snap_sorted):
        _stv = _g2_snap_sorted[_g2_snap_idx]
        _d = {uid: (tr.current_peak if tr.current_peak != float('-inf') else _g2_get(uid).rating)
              for uid, tr in _g2_peak180.items()}
        _g2_peak180_at_snap[_stv] = _d
        _g2_snap_idx += 1
    bt_gated_peak180_g2 = {uid: (tr.current_peak if tr.current_peak != float('-inf') else _g2_get(uid).rating)
                            for uid, tr in _g2_peak180.items()}
    return bt_gated_peak180_g2, dict(_g2_timeline), _g2_peak180_at_snap


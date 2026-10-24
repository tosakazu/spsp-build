"""rolling rank delta (大会ごとの順位変動・display anchor) の増分状態。

以前は build の build_rolling_rank_deltas が全大会を時系列に舐めて毎回計算していた (約 19 秒)。
順位評価の表の正規化シフトが GLOBAL_SHIFT_FROZEN で定数になった (2026-09-07) ので、各大会の
獲得 raw 点は大会時点の情報だけで決まり、学習 (g2_learn_advance) が大会ごとに step() を進めて
状態に貯められる。build は G2LearnState.rolling_view() を受け取り再計算しない。

ループ本体は score.build_rolling_rank_deltas の実装をそのまま移したもの (出力はバイト一致を golden で確認)。
openskill の rating オブジェクトと SortedList は pickle しない: (mu, sigma) と running dict から再構築する
(rating は mu/sigma だけで決まり、SortedList は (-score, uid) の集合なので同じ並びに戻る)。
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

from spsp.common import LV2_PARAMS, SIGMA_FLOOR, GATED_M_TOUR
from spsp.raters.openskill_rater import OpenSkillBTRater


def raw_at_only(deltas_by_tid: dict) -> dict:
    """{tid: {uid: {tjpr_d, bt_d, ens_d, tjpr_rb, bt_rb, ens_rb, raw_at, bt_ord_at}}} → {tid: {uid: raw_at}}。

    build が大会ごとの順位変動から読むのは raw_at (当時その大会で獲得した raw 点) だけ (output._overlay_at_time_entry と
    score.build_at_time_per_tournament。2026-10-17 時点)。snap 段で raw_at だけにして inter に入れる (1 件 8 項目の dict を
    全 (大会, 選手) ぶん持っていた。北米で 約 1.5 GB)。学習状態 (RollingState.deltas_by_path) は全項目のまま。
    読む側は rolling_raw_at で引く (どちらの形でも同じ値)。"""
    return {tid: (d if all(type(e) is float for e in d.values())   # 学習状態が raw_at だけの形ならそのまま (写さない)
                  else {u: (e if type(e) is float else e['raw_at']) for u, e in d.items()})
            for tid, d in deltas_by_tid.items()}


def rolling_raw_at(deltas_t, uid, default=None):
    """大会 1 つ分の順位変動 deltas_t ({uid: dict} または raw_at_only の {uid: float}) から uid の raw_at。
    uid が無ければ default (以前の `(deltas_t.get(uid) or {}).get('raw_at', default)` と同じ)。"""
    e = deltas_t.get(uid) if deltas_t else None
    if type(e) is float:
        return e
    return (e or {}).get('raw_at', default)


def _apply_sigma_floor_keep_ordinal(model, r, floor):
    # v4.learn と同じ (循環 import を避けるため複製しない: 遅延 import)
    from spsp.learn import _apply_sigma_floor_keep_ordinal as _f
    return _f(model, r, floor)


def _rolling_rank_of(p, running, sorted_l):
    """SortedList から (-score, uid) を引いた 1-based rank. 未登録/未発見は None."""
    if p not in running:
        return None
    try:
        return sorted_l.index((-running[p], p)) + 1
    except ValueError:
        return None


def _rolling_ens_delta(tjpr_d, bt_d):
    """TJPR/BT delta から ensemble delta を計算. 両方 None なら None."""
    if tjpr_d is not None and bt_d is not None:
        return int(round((tjpr_d + bt_d) / 2))
    if tjpr_d is not None:
        return tjpr_d
    if bt_d is not None:
        return bt_d
    return None


def rolling_participants(t, placements_t):
    """参加者集合 (matches + standings 観戦枠取りこぼし防止)。
    set の反復順が出力 dict の並びになるので、構築順 (matches → placements) を固定する。"""
    participants = set()
    for m in t.matches:
        participants.add(m.winner_id); participants.add(m.loser_id)
    for uid_ in placements_t.keys():
        participants.add(uid_)
    return participants


def placements_of(t):
    """standings から重複排除した {uid: place} (meta.build_placements_seeds と同じ)."""
    place_map = {}
    for p, u in t.standings:
        if u in place_map:
            continue
        place_map[u] = p
    return place_map


class RollingState:
    def __init__(self):
        self.tour_c = defaultdict(int)
        self.running_tjpr = {}
        self.running_bt = {}
        self.player_tour_list = defaultdict(list)   # uid -> [(raw_pts, ts)]
        # rolling ensemble rank 用の安定インデックス配列 (割当順が argsort(stable) の同点順に効くので保存する)
        self.eidx = {}
        self.ens = {'n': 0, 'cap': 4096, 'tj': np.full(4096, -np.inf), 'bt': np.full(4096, -np.inf)}
        self.bt_ratings = {}          # uid -> (mu, sigma)  pickle 用
        self.deltas_by_path = {}      # rel_event_path -> {uid: raw_at}  (処理順。2026-10-18 より前の状態は {uid: {8 項目}})
        self.n = 0
        self._rater = None            # 実行時のみ
        self._sorted_tjpr = None
        self._sorted_bt = None

    # ── pickle ──
    def __getstate__(self):
        d = self.__dict__.copy()
        if self._rater is not None:
            d['bt_ratings'] = {u: (r.mu, r.sigma) for u, r in self._rater.ratings.items()}
        d['_rater'] = None; d['_sorted_tjpr'] = None; d['_sorted_bt'] = None
        return d

    def _ensure_runtime(self):
        if self._rater is not None:
            return
        from sortedcontainers import SortedList
        r = OpenSkillBTRater()
        for u, (mu, sg) in self.bt_ratings.items():
            r.ratings[u] = r.model.rating(mu=mu, sigma=sg)
        self._rater = r
        self._sorted_tjpr = SortedList((-s, u) for u, s in self.running_tjpr.items())
        self._sorted_bt = SortedList((-s, u) for u, s in self.running_bt.items())

    # ── build_rolling_rank_deltas のクロージャ群 ──
    def _eidx(self, uid):
        eidx = self.eidx; ens_state = self.ens
        i = eidx.get(uid)
        if i is not None:
            return i
        i = ens_state['n']
        if i >= ens_state['cap']:
            ncap = ens_state['cap'] * 2
            ntj = np.full(ncap, -np.inf); ntj[:ens_state['cap']] = ens_state['tj']
            nbt = np.full(ncap, -np.inf); nbt[:ens_state['cap']] = ens_state['bt']
            ens_state.update({'cap': ncap, 'tj': ntj, 'bt': nbt})
        eidx[uid] = i
        ens_state['n'] = i + 1
        return i

    def _rolling_ens_before(self, participants):
        """大会前時点の参加者アンサンブル順位 dict. (tjpr_rank + bt_rank)/2 の昇順.

        未登録 (= まだ一度も計測されてない) プレイヤーは含めない (None 相当)."""
        eidx = self.eidx; ens_state = self.ens
        n_e = ens_state['n']
        if n_e == 0:
            return {}
        st = ens_state['tj'][:n_e]; sb = ens_state['bt'][:n_e]
        # 順位 = (値 降順、同値は index 昇順) = argsort(-値, stable)。前回からの差分で並べる
        # (learner._rank_desc_then_uid_inc: 全体を並べ直した結果と同じ。index は後ろに足されるだけ)。2026-10-17
        from spsp.learner import _rank_desc_then_uid_inc
        ix = np.arange(n_e)
        seq = np.arange(1, n_e + 1)
        tr = np.empty(n_e); tr[_rank_desc_then_uid_inc('rolling_tj', ix, st, ix)] = seq
        br = np.empty(n_e); br[_rank_desc_then_uid_inc('rolling_bt', ix, sb, ix)] = seq
        # 平均順位 (tr + br) / 2 の中での順位 = 1 + (自分より小さい人数)。tr + br は 2..2n の整数 (float で正確) なので
        # 数え上げで出す (以前は np.sort + searchsorted(left)。同じ値)
        s = (tr + br).astype(np.int64)
        below = np.cumsum(np.bincount(s, minlength=2 * n_e + 2))   # below[v] = s <= v の人数
        out = {}
        for p in participants:
            ip = eidx.get(p)
            if ip is not None and ip < n_e:
                out[p] = int(below[s[ip] - 1]) + 1
        return out

    def _recompute_tjpr(self, uid, T):
        _, _, LV2_TOPN, LV2_PD = LV2_PARAMS
        tours = self.player_tour_list[uid]
        if not tours: return 0.0
        weights = [raw * (0.97 ** ((T - ts_) / 86400.0 / 30.0)) for raw, ts_ in tours]
        weights.sort(reverse=True)
        total = 0.0
        for i, w in enumerate(weights):
            pos = 1.0 if i < LV2_TOPN else LV2_PD ** (i - LV2_TOPN + 1)
            total += w * pos
        return total

    def _apply_bt_matches(self, t_, is_wk_):
        """大会内 matches を rolling BT-gated rater へ適用 (learn_bt_gated_tour と同じ gating)."""
        tour_c_roll = self.tour_c; bt_g_roll = self._rater
        for m in t_.matches:
            wc, lc = tour_c_roll[m.winner_id], tour_c_roll[m.loser_id]
            update_w = is_wk_ or (wc <= GATED_M_TOUR)
            update_l = is_wk_ or (lc <= GATED_M_TOUR)
            if not (update_w or update_l):
                continue
            w_r = bt_g_roll._get(m.winner_id)
            l_r = bt_g_roll._get(m.loser_id)
            new_ratings = bt_g_roll.model.rate([[w_r], [l_r]], ranks=[1, 2])
            if update_w:
                bt_g_roll.ratings[m.winner_id] = _apply_sigma_floor_keep_ordinal(
                    bt_g_roll.model, new_ratings[0][0], SIGMA_FLOOR)
            if update_l:
                bt_g_roll.ratings[m.loser_id] = _apply_sigma_floor_keep_ordinal(
                    bt_g_roll.model, new_ratings[1][0], SIGMA_FLOOR)

    # ── 1 大会ぶん進める ──
    def step(self, key, t_, is_wk_, raw_map, placements_t):
        """key = 結果の保存キー (rel_event_path、または id(t))。raw_map = {uid: その大会の raw TJPR pt}
        (= _rolling_lv2_raw_per_tour の per_dict、表に無い大会は {})。呼び出し側で serious/非仮想に絞る。"""
        self._ensure_runtime()
        running_tjpr = self.running_tjpr; running_bt = self.running_bt
        sorted_tjpr_l = self._sorted_tjpr; sorted_bt_l = self._sorted_bt
        player_tour_list = self.player_tour_list; ens_state = self.ens
        bt_g_roll = self._rater; tour_c_roll = self.tour_c
        T_ = t_.timestamp
        participants = rolling_participants(t_, placements_t)
        # Rank before (= 参加前). tjpr / bt / ensemble いずれも大会前の絶対順位.
        rb_tjpr = {p: _rolling_rank_of(p, running_tjpr, sorted_tjpr_l) for p in participants}
        rb_bt   = {p: _rolling_rank_of(p, running_bt,   sorted_bt_l)   for p in participants}
        rb_ens  = self._rolling_ens_before(participants)
        # 大会前の BT 内部レート (= 当時のレート変動量を出すため). None = 初出場.
        bt_ord_before = {p: running_bt.get(p) for p in participants}
        # Apply TJPR update for participants
        for p, raw in raw_map.items():
            player_tour_list[p].append((raw, T_))
            new_score = self._recompute_tjpr(p, T_)
            if p in running_tjpr:
                try: sorted_tjpr_l.remove((-running_tjpr[p], p))
                except ValueError: pass
            running_tjpr[p] = new_score
            sorted_tjpr_l.add((-new_score, p))
            _ix = self._eidx(p); ens_state['tj'][_ix] = new_score
        # Apply BT update (mirror learn_bt_gated_tour gating)
        self._apply_bt_matches(t_, is_wk_)
        for uid_p in participants:
            tour_c_roll[uid_p] += 1
        # BT sorted update for participants
        for p in participants:
            new_ord = bt_g_roll.rating(p) if p in bt_g_roll.ratings else 0.0
            if p in running_bt:
                try: sorted_bt_l.remove((-running_bt[p], p))
                except ValueError: pass
            running_bt[p] = new_ord
            sorted_bt_l.add((-new_ord, p))
            _ix = self._eidx(p); ens_state['bt'][_ix] = new_ord
        # Rank after + delta
        deltas_t = {}
        for p in participants:
            ra_tjpr = _rolling_rank_of(p, running_tjpr, sorted_tjpr_l)
            ra_bt   = _rolling_rank_of(p, running_bt,   sorted_bt_l)
            tjpr_d = (rb_tjpr[p] - ra_tjpr) if (rb_tjpr[p] is not None and ra_tjpr is not None) else None
            bt_d   = (rb_bt[p]   - ra_bt)   if (rb_bt[p]   is not None and ra_bt   is not None) else None
            # 当時の変動量 (レート/ポイント). raw_at = その大会で獲得した raw TJPR pt
            # (= age decay 前の素点), bt_ord_at = 当時の BT 内部レート増分.
            _ob = bt_ord_before.get(p)
            bt_ord_at = (running_bt.get(p) - _ob) if (_ob is not None and running_bt.get(p) is not None) else None
            deltas_t[p] = {'tjpr_d': tjpr_d, 'bt_d': bt_d, 'ens_d': _rolling_ens_delta(tjpr_d, bt_d),
                           # 参加前の絶対順位 (display anchor). None = 大会前は未計測.
                           'tjpr_rb': rb_tjpr[p], 'bt_rb': rb_bt[p], 'ens_rb': rb_ens.get(p),
                           # 当時の獲得ポイント / レート変動 (display 用).
                           'raw_at': float(raw_map.get(p, 0.0)), 'bt_ord_at': bt_ord_at}
        # 状態に残すのは raw_at だけ (build が読むのはこれだけ: raw_at_only / rolling_raw_at。学習も読み返さない)。
        # 以前は 8 項目の dict を全 (大会, 選手) ぶん持っていた (北米で 約 1.2 GB。2026-10-18)
        self.deltas_by_path[key] = {p: e['raw_at'] for p, e in deltas_t.items()}
        self.n += 1
        return deltas_t

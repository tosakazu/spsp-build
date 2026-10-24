"""過去最高順位の追跡 (学習の軌跡上の真の最小値)。

以前の「過去最高順位」は 59 点のスナップショット (評価日から N 日前) の中の最良値だったので、点と点の
間に達した順位は拾えず、評価日が 1 日ずれるだけで値が変わった。学習 (spsp/learner.g2_learn_advance) は
serious 大会ごとに全選手の表示順位 (jp-counter の総合 / 順位評価 / 直対評価) を計算しているので、
そこで走らせながら最小値を取れば、大会単位の粒度で真の最高順位が (ほぼタダで) 出る。

持つもの (選手ごと、numpy 配列):
  best[k]      全期間の最良順位 (k = 0 総合 / 1 順位評価 / 2 直対評価) と、それを最後に達成した日 (best_day)
  ring[k]      直近 54 週ぶんの週ごとの最良順位とその日 (1 年ピーク用。窓は達成日で日単位に切る)。
               メモリ上は (K, WEEKS, 選手) の並び (大会ごとに 1 つの週の列を全選手ぶん読み書きするので連続にする。2026-10-17)。
               保存 (pickle) の形は以前と同じ (K, 選手, WEEKS)
  lv_best      最高到達レベル、tier_best = 最高到達ティア (score._level_tier_achievement の順序値)
「日」は暦日の序数 (date.toordinal()、ビルドのローカル時刻 = JST)。'now' (現在値が最良) の判定はビルド側が現在の順位と比べて行う。

集計開始日 (cutoff、EVO Japan 2020) より前の大会は数えない (以前の peak_alltime_max_days と同じ意味)。
チェックポイント (learn_state) に入るので、形式を変えたら STATE_VERSION を上げること。
"""
from __future__ import annotations

import numpy as np

INF = np.int32(2 ** 31 - 1)
WEEKS = 54   # 365 日の窓は週境界に揃えると最大 54 週にまたがる
K = 3   # ensemble, tjpr, bt_gated
KEYS = ('ensemble', 'tjpr', 'bt_gated')


class PeakTracker:
    __slots__ = ('n', 'cap', 'uids', 'sorted_uids', 'sorted_to_idx', 'best', 'best_day',
                 'ring', 'ring_day', 'ring_week', 'lv_best', 'tier_best', 'tier_day', 'last_ts', 'n_updates',
                 '_tbl', '_sorted_n', '_last_idx')

    def __init__(self):
        self.n = 0
        self.cap = 0
        self.uids = np.zeros(0, dtype=np.int64)
        self.sorted_uids = np.zeros(0, dtype=np.int64)
        self.sorted_to_idx = np.zeros(0, dtype=np.int64)
        self.best = np.zeros((K, 0), dtype=np.int32)
        self.best_day = np.zeros((K, 0), dtype=np.int32)
        self.ring = np.zeros((K, WEEKS, 0), dtype=np.int32)
        self.ring_day = np.zeros((K, WEEKS, 0), dtype=np.int32)
        self.ring_week = np.full(WEEKS, -1, dtype=np.int64)
        self.lv_best = np.zeros(0, dtype=np.int8)
        self.tier_best = np.zeros(0, dtype=np.int32)
        self.tier_day = np.zeros(0, dtype=np.int32)
        self.last_ts = 0
        self.n_updates = 0
        self._tbl = None        # uid → 内部 index の直接表 (-1 = 未登録)。保存しない (実行時だけ)
        self._sorted_n = 0      # sorted_uids / sorted_to_idx が uids[:_sorted_n] を表している
        self._last_idx = {}     # 呼び出し場所ごとの前回の (uid 配列, index 配列)。保存しない (実行時だけ)

    # ── pickle (チェックポイント): 容量ぶんではなく使っている分だけ保存する ──
    def __getstate__(self):
        n = self.n
        return {'n': n, 'uids': self.uids[:n].copy(), 'best': self.best[:, :n].copy(), 'best_day': self.best_day[:, :n].copy(),
                'ring': np.ascontiguousarray(self.ring[:, :, :n].transpose(0, 2, 1)),
                'ring_day': np.ascontiguousarray(self.ring_day[:, :, :n].transpose(0, 2, 1)), 'ring_week': self.ring_week.copy(),
                'lv_best': self.lv_best[:n].copy(), 'tier_best': self.tier_best[:n].copy(), 'tier_day': self.tier_day[:n].copy(),
                'last_ts': self.last_ts, 'n_updates': self.n_updates}

    def __setstate__(self, st):
        self.n = self.cap = st['n']
        for k in ('uids', 'best', 'best_day', 'ring', 'ring_day', 'ring_week', 'lv_best', 'tier_best', 'tier_day', 'last_ts', 'n_updates'):
            setattr(self, k, st[k])
        self.ring = np.ascontiguousarray(self.ring.transpose(0, 2, 1))           # 保存形 (K, 選手, WEEKS) → (K, WEEKS, 選手)
        self.ring_day = np.ascontiguousarray(self.ring_day.transpose(0, 2, 1))
        order = np.argsort(self.uids[:self.n], kind='stable')
        self.sorted_uids = self.uids[:self.n][order]
        self.sorted_to_idx = order
        self._sorted_n = self.n
        self._tbl = None
        self._last_idx = {}

    # ── 容量 ──
    def _grow(self, need: int) -> None:
        if need <= self.cap:
            return
        cap = max(need, self.cap * 2, 1024)
        def ext(a, shape, fill):
            b = np.full(shape, fill, dtype=a.dtype)
            if a.ndim == 1:
                b[:self.n] = a[:self.n]
            elif a.ndim == 2:
                b[:, :self.n] = a[:, :self.n]
            else:
                b[:, :, :self.n] = a[:, :, :self.n]
            return b
        self.uids = ext(self.uids, (cap,), 0)
        self.best = ext(self.best, (K, cap), INF)
        self.best_day = ext(self.best_day, (K, cap), -1)
        self.ring = ext(self.ring, (K, WEEKS, cap), INF)
        self.ring_day = ext(self.ring_day, (K, WEEKS, cap), -1)
        self.lv_best = ext(self.lv_best, (cap,), 0)
        self.tier_best = ext(self.tier_best, (cap,), 0)
        self.tier_day = ext(self.tier_day, (cap,), -1)
        self.cap = cap

    # uid → index の直接表を使う uid の上限 (これ以上の uid があれば二分探索の方を使う)
    _TBL_MAX_UID = 1 << 26

    def _indices(self, uids_arr: np.ndarray) -> np.ndarray:
        """uid 配列 → 内部 index 配列。未知の uid は末尾に追加する (vectorized)。

        uid が 0 以上 _TBL_MAX_UID 未満なら uid を添字にした表で引く (以前は大会ごとに全選手を二分探索していた:
        北米 20 万人で学習時間の 1 割。2026-10-17)。未知の uid の足し方 (np.unique = 昇順で末尾に) は下の
        二分探索版と同じなので、uid → index の対応は同じ。"""
        if len(uids_arr) == 0:
            return np.zeros(0, dtype=np.int64)
        lo = int(uids_arr.min()); hi = int(uids_arr.max())
        # 表があれば登録済みの uid は全部その中 (表を作るときに全部入れ、以後の追加も表に入れる)
        if lo >= 0 and hi < self._TBL_MAX_UID and (
                self._tbl is not None or self.n == 0 or int(self.uids[:self.n].min()) >= 0 and int(self.uids[:self.n].max()) < self._TBL_MAX_UID):
            return self._indices_tbl(uids_arr, hi)
        return self._indices_bsearch(uids_arr)

    def _indices_tbl(self, uids_arr: np.ndarray, hi: int) -> np.ndarray:
        tbl = self._tbl
        if tbl is None or len(tbl) <= hi:
            size = max(hi + 1, int(self.uids[:self.n].max(initial=0)) + 1 if self.n else 0, 2 * len(tbl) if tbl is not None else 0)
            size = min(max(size, 1024), self._TBL_MAX_UID)
            tbl = np.full(size, -1, dtype=np.int64)
            tbl[self.uids[:self.n]] = np.arange(self.n, dtype=np.int64)
            self._tbl = tbl
        idx = tbl[uids_arr]
        miss = idx < 0
        if miss.any():
            new = np.unique(uids_arr[miss])
            self._grow(self.n + len(new))
            self.uids[self.n:self.n + len(new)] = new
            tbl[new] = np.arange(self.n, self.n + len(new), dtype=np.int64)
            self.n += len(new)
            idx = tbl[uids_arr]
        return idx

    def _indices_bsearch(self, uids_arr: np.ndarray) -> np.ndarray:
        if self._sorted_n != self.n:   # 表の方で足した uid がある → 並べ直す
            order = np.argsort(self.uids[:self.n], kind='stable')
            self.sorted_uids = self.uids[:self.n][order]
            self.sorted_to_idx = order
            self._sorted_n = self.n
        self._tbl = None               # 以後の追加は表に反映しないので捨てる (次に表を使うとき作り直す)
        if self.n:
            pos = np.searchsorted(self.sorted_uids, uids_arr)
            pos_c = np.minimum(pos, self.n - 1)
            found = self.sorted_uids[pos_c] == uids_arr
        else:
            pos_c = np.zeros(len(uids_arr), dtype=np.int64)
            found = np.zeros(len(uids_arr), dtype=bool)
        if not found.all():
            new = np.unique(uids_arr[~found])
            self._grow(self.n + len(new))
            self.uids[self.n:self.n + len(new)] = new
            self.n += len(new)
            order = np.argsort(self.uids[:self.n], kind='stable')
            self.sorted_uids = self.uids[:self.n][order]
            self.sorted_to_idx = order
            self._sorted_n = self.n
            pos = np.searchsorted(self.sorted_uids, uids_arr)
            pos_c = pos
        return self.sorted_to_idx[pos_c]

    def _indices_cached(self, key, uids_arr: np.ndarray) -> np.ndarray:
        """_indices と同じ。前回 (同じ key) の uid 配列がこの配列の先頭とそっくり同じなら、その分の index は前回のものを
        使い、足された分だけ引く (選手は後ろに足されるだけなので、北米 20 万人を毎回表で引き直していた。2026-10-23)。
        先頭の uid は前回登録済みなので、足された分を引いても新しい uid の足し方 (np.unique の順) は全体を引くのと同じ。"""
        prev = self._last_idx.get(key)
        idx = None
        if prev is not None:
            pu, pi = prev
            L = len(pu)
            if 0 < L <= len(uids_arr) and np.array_equal(uids_arr[:L], pu):
                idx = pi if L == len(uids_arr) else np.concatenate((pi, self._indices(uids_arr[L:])))
        if idx is None:
            idx = self._indices(uids_arr)
        self._last_idx[key] = (uids_arr.copy(), idx)
        return idx

    # ── 更新 (serious 大会ごと、大会直前の全選手の表示順位) ──
    def update(self, ts: int, day: int, ens_uids, ens_rank, lv, tj_rank, tier_v, bt_uids, bt_rank) -> None:
        """day = その大会の暦日序数 (date.toordinal())。ens_uids / ens_rank / lv / tj_rank / tier_v は同じ長さ (cascade の全選手)。bt_uids / bt_rank は直対評価側。
        rank は int 配列 (0 や負は「順位なし」として無視)。tier_v は score.level_tier_values の順序値 (0 = なし)。"""
        day = np.int32(day)
        week = int(day) // 7
        slot = int(week % WEEKS)
        if self.ring_week[slot] != week:
            self.ring[:, slot, :] = INF
            self.ring_day[:, slot, :] = -1
            self.ring_week[slot] = week
        # 順位は内部 index の並び (長さ n、順位の無い選手は 0) に一度だけ置いてから、全選手ぶんを連続のまま比べる
        # (以前は index で拾って比べて index で書き戻していた: 北米 20 万人で学習の書き出し側の最大の山。2026-10-24)。
        # 1 つの配列の中で uid は重複しない前提 (cascade の全選手・直対評価の全選手) なので結果は同じ
        ens_uids = np.asarray(ens_uids, dtype=np.int64)
        idx = self._indices_cached('ens', ens_uids)
        n = self.n
        self._apply(0, self._dense(idx, ens_rank, n, np.int32), day, slot)
        self._apply(1, self._dense(idx, tj_rank, n, np.int32), day, slot)
        lvb = self.lv_best[:n]
        np.maximum(lvb, self._dense(idx, lv, n, np.int8), out=lvb)          # 順位の無い選手は 0 (lv_best は 0 以上)
        tv = self._dense(idx, tier_v, n, np.int32)
        m = tv > self.tier_best[:n]
        np.copyto(self.tier_best[:n], tv, where=m)
        np.copyto(self.tier_day[:n], day, where=m)
        if bt_uids is not None and len(bt_uids):
            idx_b = self._indices_cached('bt', np.asarray(bt_uids, dtype=np.int64))
            self._apply(2, self._dense(idx_b, bt_rank, self.n, np.int32), day, slot)
        self.last_ts = int(ts)
        self.n_updates += 1

    def touch(self, ts: int) -> None:
        """update を飛ばしたとき (前回と同じ順位を同じ日に渡す = 値は何も変わらない) に、回数と時刻だけ update と同じに進める。"""
        self.last_ts = int(ts)
        self.n_updates += 1

    @staticmethod
    def _dense(idx: np.ndarray, vals, n: int, dtype) -> np.ndarray:
        out = np.zeros(n, dtype=dtype)
        out[idx] = np.asarray(vals).astype(dtype, copy=False)
        return out

    def _apply(self, k: int, rank: np.ndarray, day, slot: int) -> None:
        """rank = 内部 index の並びの順位 (長さ ≤ n、0 以下は「順位なし」)。"""
        n = len(rank)
        valid = rank > 0
        # 全期間: 同順位なら最後に達成した日を採用 (以前の「最新の日」規則と同じ)
        b = self.best[k, :n]
        m = valid & (rank <= b)
        np.copyto(b, rank, where=m)
        np.copyto(self.best_day[k, :n], day, where=m)
        ring_s = self.ring[k, slot, :n]   # その週の列 (連続、view)
        m = valid & (rank <= ring_s)
        np.copyto(ring_s, rank, where=m)
        np.copyto(self.ring_day[k, slot, :n], day, where=m)

    # ── 読み出し ──
    def snapshot(self, now_day: int) -> dict:
        """uid → {'ensemble': (best, day) | None, 'tjpr': ..., 'bt_gated': ..., 'ensemble_1y': ..., ...,
                  'lv_best': int, 'tier_best': int, 'tier_day': int}。day は暦日序数。"""
        n = self.n
        if n == 0:
            return {}
        out = {}
        uids = self.uids[:n].tolist()
        best = self.best[:, :n]; bday = self.best_day[:, :n]
        r = self.ring[:, :, :n].transpose(0, 2, 1); rd = self.ring_day[:, :, :n].transpose(0, 2, 1)   # (K, n, WEEKS)
        # 1 年の窓: 各週の最良を「それを達成した日」で判定する (日単位)。週の中で窓の外の日に達した最良は
        # 外れる (その週の窓内の別の日の値は残らない = 境界の 1 週だけ粗い)
        r_win = np.where(rd >= int(now_day) - 365, r, INF)
        r1 = r_win.min(axis=2)                              # (K, n)
        # 同順位の週が複数あれば最後に達成した日 (全期間側と同じ規則)
        d1 = np.where(r_win == r1[:, :, None], rd, -1).max(axis=2)
        lvb = self.lv_best[:n].tolist(); tb = self.tier_best[:n].tolist(); td = self.tier_day[:n].tolist()
        best_l = best.tolist(); bday_l = bday.tolist(); r1_l = r1.tolist(); d1_l = d1.tolist()
        for i, u in enumerate(uids):
            rec = {}
            for k, key in enumerate(KEYS):
                b = best_l[k][i]
                rec[key] = (b, bday_l[k][i]) if b < INF else None
                b1 = r1_l[k][i]
                rec[key + '_1y'] = (b1, d1_l[k][i]) if b1 < INF else None
            rec['lv_best'] = lvb[i]; rec['tier_best'] = tb[i]; rec['tier_day'] = td[i]
            out[u] = rec
        return out

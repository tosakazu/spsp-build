"""学習の記録 (試合ごと・選手×大会ごと) を、数値の配列で持つ表 (2026-10-18)。

以前は dict ({(eid, w, l): [[d_w, d_l, t_w, t_l], ...]} / {(uid, eid): {...5 項目...}}) で持っていた。北米では
試合 530 万件・選手×大会 280 万件で、値そのものより dict・list・float のオブジェクトの分が大きく、学習状態・
snap→build の中間ファイル・build の全段で数 GB を使っていた。ここでは 1 行 = 1 回の記録として列ごとの配列
(array.array) に足していき、読むときだけ dict と同じ形で見せる。

読む側から見た振る舞いは元の dict と同じ:
  - キーの並び (iter / items) = そのキーが最初に記録された順 (dict の挿入順)
  - MatchLog の値 = そのキーの記録を記録した順に並べた [(d_w, d_l, t_w, t_l), ...] (元は [[...], ...]。読む側は
    添字でしか読まない: spsp/lazy_views.per_match_occurrences ほか)
  - PerTourLog の値 = (bt_used, bt_internal_d, bt_d, after, peak_after) (spsp/lazy_views.per_tour_fields の並び)。
    同じキーを 2 回記録したら、並びは最初の位置・値は最後のもの (dict に代入し直したときと同じ)
  - 値は Python の int / float / bool (配列は float64・int64・int8 なので、入れた値がそのまま戻る)
書き込みは add() だけ (dict の setdefault(...).append / 代入の代わり)。読むための索引は最初に読んだときに作り、
書き足すと捨てる (学習中は書くだけなので作られない)。
"""
from __future__ import annotations

from array import array
from bisect import bisect_left
from collections.abc import Mapping

import numpy as np

_CHUNK = 1 << 16   # items() で配列から Python の値に直すときの 1 回分の行数


class _KeyedLog(Mapping):
    """キー (int の組) と値 (数値の組) の行を記録順に持つ。KEY_CODES / VAL_CODES は array の型コード。"""

    KEY_CODES: tuple[str, ...] = ()
    VAL_CODES: tuple[str, ...] = ()

    def __init__(self):
        self._k = tuple(array(c) for c in self.KEY_CODES)
        self._v = tuple(array(c) for c in self.VAL_CODES)
        self._ix = None
        self._fast = None

    # ── 書く ──
    def _add_row(self, key, vals):
        for col, x in zip(self._k, key):
            col.append(x)
        for col, x in zip(self._v, vals):
            col.append(x)
        self._ix = None
        self._fast = None

    @property
    def n_rows(self) -> int:
        return len(self._k[0])

    # ── 索引 ──
    def _index(self):
        """(各成分の値の一覧, 組の番号の昇順, その組の行の範囲, 組の並び (最初の行の順), 行を組ごとに並べたもの)。"""
        if self._ix is not None:
            return self._ix
        n = self.n_rows
        comps = []
        code = np.zeros(n, dtype=np.int64)
        span = 1
        for col in self._k:
            a = np.frombuffer(col, dtype=np.int64) if col.typecode == 'q' else np.asarray(col, dtype=np.int64)
            uniq, inv = np.unique(a, return_inverse=True)
            comps.append(uniq)
            span *= max(len(uniq), 1)
            if span >= 1 << 62:
                raise OverflowError(f'{type(self).__name__}: キーの組が多すぎる ({span})')
            code = code * len(uniq) + inv   # 成分ごとに詰めた番号の組 (北米で 16 万 × 21 万 × 21 万 < 2^63)
        rows = np.argsort(code, kind='stable')          # 組の番号順、同じ組の中は記録順
        sc = code[rows]
        starts = np.flatnonzero(np.r_[True, sc[1:] != sc[:-1]]) if n else np.zeros(0, dtype=np.int64)
        ends = np.r_[starts[1:], n] if n else np.zeros(0, dtype=np.int64)
        gcode = sc[starts]
        first_row = rows[starts]
        gorder = np.argsort(first_row, kind='stable')   # dict の挿入順 = 最初に記録された順
        self._ix = (comps, gcode, starts, ends, gorder, rows)
        return self._ix

    def _lookup(self):
        """1 件ずつ引く用の索引: 各成分の値 → 番号の dict と、組の番号の昇順の配列 (bisect 用)。
        numpy の searchsorted は 1 回ごとの手間が大きく、北米の build で 1 件ずつ数百万回引くと数百秒かかった。"""
        if self._fast is None:
            comps, gcode, starts, ends, _go, rows = self._index()
            # starts / ends / rows は memoryview でも持つ (同じ配列。1 件ずつ添字で引くのが numpy より速い)
            self._fast = ([dict(zip(u.tolist(), range(len(u)))) for u in comps],
                          array('q', gcode.astype(np.int64).tobytes()),
                          memoryview(np.ascontiguousarray(starts, dtype=np.int64)),
                          memoryview(np.ascontiguousarray(ends, dtype=np.int64)),
                          memoryview(np.ascontiguousarray(rows, dtype=np.int64)))
        return self._fast

    def _rows_of_group(self, g) -> list:
        """組 g の行 (記録順)。"""
        f = self._fast if self._fast is not None else self._lookup()
        return f[4][f[2][g]:f[3][g]].tolist()

    def _last_row_of_group(self, g) -> int:
        f = self._fast if self._fast is not None else self._lookup()
        return f[4][f[3][g] - 1]

    def _group(self, key):
        """key の組の番号 (無ければ -1)。"""
        if len(key) != len(self._k):
            return -1
        pos, gcode = self._lookup()[:2]
        if len(pos) == 1:   # 成分が 1 つなら、値の番号がそのまま組の番号 (値ごとに組がある)
            x = key[0]
            if type(x) is not int and not isinstance(x, (int, np.integer)):
                return -1
            i = pos[0].get(x)
            return -1 if i is None else i
        code = 0
        for p, x in zip(pos, key):
            if type(x) is not int and not isinstance(x, (int, np.integer)):   # dict と同じく True / 1 は同じキー
                return -1
            i = p.get(x)
            if i is None:
                return -1
            code = code * len(p) + i
        g = bisect_left(gcode, code)
        return g if g < len(gcode) and gcode[g] == code else -1

    def groups_with_first(self, x0) -> dict:
        """キーの最初の成分が x0 の組 → {残りの成分 (1 つなら値そのもの、2 つ以上なら tuple): 組の番号}。
        同じ選手 (または同じ大会) を続けて引くときに、1 件ずつ _group するより速い。"""
        pos, gcode = self._lookup()[:2]
        if type(x0) is not int and not isinstance(x0, (int, np.integer)):
            return {}
        i0 = pos[0].get(x0)
        if i0 is None:
            return {}
        rest = 1
        for p in pos[1:]:
            rest *= len(p)
        lo = bisect_left(gcode, i0 * rest)
        hi = bisect_left(gcode, (i0 + 1) * rest)
        if lo >= hi:
            return {}
        _c, _gc, starts, _e, _go, rows = self._index()
        r_first = rows[starts[lo:hi]]
        cols = [(np.frombuffer(c, dtype=np.int64) if c.typecode == 'q' else np.asarray(c))[r_first].tolist()
                for c in self._k[1:]]
        keys = cols[0] if len(cols) == 1 else list(zip(*cols))
        return dict(zip(keys, range(lo, hi)))

    def value_of_group(self, g):
        return self._value_of_group(g)

    def group_rank(self):
        """組の番号 g → iter / items での位置 (dict の挿入順の何番目か) の配列。"""
        _c, _gc, _s, _e, gorder, _rows = self._index()
        rank = np.empty(len(gorder), dtype=np.int64)
        rank[gorder] = np.arange(len(gorder), dtype=np.int64)
        return rank

    def _vals_at(self, r: int) -> tuple:
        return tuple(col[r] for col in self._v)

    def _key_at(self, r: int) -> tuple:
        return tuple(col[r] for col in self._k)

    # ── Mapping ──
    def __len__(self):
        return len(self._index()[1])

    def __contains__(self, key):
        return type(key) is tuple and self._group(key) >= 0

    def __getitem__(self, key):
        g = self._group(key) if type(key) is tuple else -1
        if g < 0:
            raise KeyError(key)
        return self._value_of_group(g)

    def get(self, key, default=None):
        g = self._group(key) if type(key) is tuple else -1
        return default if g < 0 else self._value_of_group(g)

    def __iter__(self):
        for k, _v in self._iter_items(want_values=False):
            yield k

    def items(self):
        return _ItemsView(self)

    def values(self):
        return (v for _k, v in self._iter_items())

    def _iter_items(self, want_values=True):
        comps, gcode, starts, ends, gorder, rows = self._index()
        kcols = [np.frombuffer(c, dtype=np.int64) if c.typecode == 'q' else np.asarray(c) for c in self._k]
        vcols = [np.asarray(c) for c in self._v] if want_values else []
        for c0 in range(0, len(gorder), _CHUNK):
            gs = gorder[c0:c0 + _CHUNK]
            r_first = rows[starts[gs]]
            keys = list(zip(*[kc[r_first].tolist() for kc in kcols]))
            if not want_values:
                for k in keys:
                    yield k, None
                continue
            vals = self._chunk_values(gs, starts, ends, rows, vcols)
            yield from zip(keys, vals)

    # 値の作り方は子クラスが決める
    def _value_of_group(self, g):
        raise NotImplementedError

    def _chunk_values(self, gs, starts, ends, rows, vcols):
        raise NotImplementedError

    # ── pickle (索引は持たない) ──
    def __getstate__(self):
        return {'k': self._k, 'v': self._v}

    def __setstate__(self, s):
        self._k = s['k']; self._v = s['v']; self._ix = None; self._fast = None

    def __repr__(self):
        return f'<{type(self).__name__} {self.n_rows} rows>'

    def __eq__(self, other):   # Mapping の既定 (dict(self.items()) == dict(other.items())) と同じだが並びも見る
        if not isinstance(other, Mapping):
            return NotImplemented
        return list(self.items()) == list(other.items())

    __hash__ = None


class _ItemsView:
    __slots__ = ('_log',)

    def __init__(self, log):
        self._log = log

    def __iter__(self):
        return self._log._iter_items()

    def __len__(self):
        return len(self._log)


def _bools(col_slice) -> list:
    return [bool(x) for x in col_slice.tolist()]


class MatchLog(_KeyedLog):
    """bt_per_match: (event_id, winner_uid, loser_uid) → [(d_w, d_l, w_tracked, l_tracked), ...] (記録順)。"""

    KEY_CODES = ('q', 'q', 'q')
    VAL_CODES = ('d', 'd', 'b', 'b')

    def add(self, eid, w, l, d_w, d_l, t_w, t_l):
        self._add_row((eid, w, l), (d_w, d_l, 1 if t_w else 0, 1 if t_l else 0))

    def _value_of_group(self, g):
        dw, dl, tw, tl = self._v
        return [(dw[r], dl[r], tw[r] != 0, tl[r] != 0) for r in self._rows_of_group(g)]

    def get3(self, eid: int, w: int, l: int):
        """self.get((eid, w, l)) と同じ (eid・w・l は int、bool ではない)。選手ページの対戦一覧で 1 試合ずつ引くので
        _group を通さずに番号を作る。"""
        f = self._fast if self._fast is not None else self._lookup()
        pe, pw, pl = f[0]
        i = pe.get(eid)
        if i is None: return None
        j = pw.get(w)
        if j is None: return None
        k = pl.get(l)
        if k is None: return None
        code = (i * len(pw) + j) * len(pl) + k
        gcode = f[1]
        g = bisect_left(gcode, code)
        if g >= len(gcode) or gcode[g] != code:
            return None
        dw, dl, tw, tl = self._v
        return [(dw[r], dl[r], tw[r] != 0, tl[r] != 0) for r in f[4][f[2][g]:f[3][g]].tolist()]

    def _chunk_values(self, gs, starts, ends, rows, vcols):
        dw, dl, tw, tl = vcols
        out = []
        for s, e in zip(starts[gs].tolist(), ends[gs].tolist()):
            rr = rows[s:e]
            if e - s == 1:
                r = int(rr[0])
                out.append([(float(dw[r]), float(dl[r]), bool(tw[r]), bool(tl[r]))])
            else:
                out.append(list(zip(dw[rr].tolist(), dl[rr].tolist(), _bools(tw[rr]), _bools(tl[rr]))))
        return out

    @classmethod
    def from_dict(cls, d: dict) -> 'MatchLog':
        """学習側の dict ({(eid, w, l): [[d_w, d_l, t_w, t_l], ...]}、snap が詰めた tuple も可) から。並びはそのまま。"""
        from spsp.lazy_views import per_match_occurrences
        log = cls()
        for (eid, w, l), occs in d.items():
            for o in per_match_occurrences(occs):
                log.add(eid, w, l, o[0], o[1], o[2], o[3])
        return log


class PerTourLog(_KeyedLog):
    """bt_per_tour: (uid, event_id) → (bt_used, bt_internal_d, bt_d, after, peak_after)。"""

    KEY_CODES = ('q', 'q')
    VAL_CODES = ('b', 'd', 'd', 'd', 'd')

    def add(self, uid, eid, bt_used, bt_internal_d, bt_d, after, peak_after):
        self._add_row((uid, eid), (1 if bt_used else 0, bt_internal_d, bt_d, after, peak_after))

    def _value_of_group(self, g):
        r = self._last_row_of_group(g)   # 同じキーの最後の記録 (dict に代入し直したときの値)
        u, a, b, c, d = self._v
        return (u[r] != 0, a[r], b[r], c[r], d[r])

    def _chunk_values(self, gs, starts, ends, rows, vcols):
        rr = rows[ends[gs] - 1]
        u, a, b, c, d = (vc[rr] for vc in vcols)
        return list(zip(_bools(u), a.tolist(), b.tolist(), c.tolist(), d.tolist()))

    @classmethod
    def from_dict(cls, d: dict) -> 'PerTourLog':
        from spsp.lazy_views import per_tour_fields
        log = cls()
        for (uid, eid), info in d.items():
            log.add(uid, eid, *per_tour_fields(info))
        return log


_NONE_I32 = -(1 << 31)   # AnchorTable で None を表す値 (順位・Lv は 0 以上)


class AnchorTable(Mapping):
    """pt_at_time の大会 1 つ分: uid → [lv, ens_b, tjpr_b, bt_b(, ens_a, tjpr_a, bt_a)] (int または None)。

    学習中は大会を処理し終えるまで dict ({uid: list}) のまま (後から after の 3 つを足すため)、終わったら
    これに詰める (学習の次の大会の処理の頭で。spsp/learner.g2_learn_advance)。読む側には元の dict と同じに見える
    (並び = 挿入順、値は引くたびに新しい list)。北米で 15 万大会 × 平均 20 人。"""

    __slots__ = ('_u', '_v', '_o', '_su', '_sp')

    def __init__(self, d: dict):
        self._u = array('q', d.keys())
        self._o = array('i', [0])   # 値の範囲: uid i の値は _v[_o[i]:_o[i+1]]
        self._v = array('i')
        for e in d.values():
            for x in e:
                self._v.append(_NONE_I32 if x is None else x)
            self._o.append(len(self._v))
        order = sorted(range(len(self._u)), key=self._u.__getitem__)
        self._su = array('q', (self._u[i] for i in order))
        self._sp = array('i', order)

    def _pos(self, uid):
        if type(uid) is not int:
            if not isinstance(uid, (int, np.integer)):
                return -1
            uid = int(uid)
        su = self._su
        i = bisect_left(su, uid)
        return self._sp[i] if i < len(su) and su[i] == uid else -1

    def _value(self, i):
        o = self._o
        return [None if x == _NONE_I32 else x for x in self._v[o[i]:o[i + 1]]]

    def __getitem__(self, uid):
        i = self._pos(uid)
        if i < 0:
            raise KeyError(uid)
        return self._value(i)

    def get(self, uid, default=None):
        i = self._pos(uid)
        return default if i < 0 else self._value(i)

    def __contains__(self, uid):
        return self._pos(uid) >= 0

    def __len__(self):
        return len(self._u)

    def __iter__(self):
        return iter(self._u.tolist())

    def items(self):
        v = self._v.tolist(); o = self._o.tolist()
        return [(u, [None if x == _NONE_I32 else x for x in v[o[i]:o[i + 1]]]) for i, u in enumerate(self._u.tolist())]

    def values(self):
        return [e for _u, e in self.items()]

    def keys(self):
        return self._u.tolist()

    def __getstate__(self):
        return (self._u, self._v, self._o, self._su, self._sp)

    def __setstate__(self, s):
        self._u, self._v, self._o, self._su, self._sp = s

    def __eq__(self, other):
        if not isinstance(other, Mapping):
            return NotImplemented
        return list(self.items()) == list(other.items())

    __hash__ = None

    def __repr__(self):
        return f'<AnchorTable {len(self)} players>'


class _SeriesLog(_KeyedLog):
    """uid → [(ts, x, ...), ...] (記録順) の表。元は defaultdict(list) に tuple を append していたもの。
    キーは uid (int) そのもの (MatchLog / PerTourLog は tuple)。値は引くたびに新しい list。"""

    KEY_CODES = ('q',)

    def add(self, uid, *vals):
        self._add_row((uid,), vals)

    def _group(self, key):
        return super()._group((key,)) if not isinstance(key, tuple) else -1

    def __contains__(self, key):
        return not isinstance(key, tuple) and self._group(key) >= 0

    def __getitem__(self, key):
        g = self._group(key)
        if g < 0:
            raise KeyError(key)
        return self._value_of_group(g)

    def get(self, key, default=None):
        g = self._group(key)
        return default if g < 0 else self._value_of_group(g)

    def __iter__(self):
        for k, _v in self._iter_items(want_values=False):
            yield k

    def _iter_items(self, want_values=True):
        for k, v in super()._iter_items(want_values):
            yield (k[0], v)

    _N_OUT = None   # 値に出す列の数 (None = 全部)

    def _value_of_group(self, g):
        rr = self._rows_of_group(g)
        cols = self._v if self._N_OUT is None else self._v[:self._N_OUT]
        return list(zip(*[[c[r] for r in rr] for c in cols]))

    def _chunk_values(self, gs, starts, ends, rows, vcols):
        out = []
        for s, e in zip(starts[gs].tolist(), ends[gs].tolist()):
            rr = rows[s:e]
            out.append(list(zip(*[vc[rr].tolist() for vc in vcols])))
        return out

    @classmethod
    def from_dict(cls, d) -> '_SeriesLog | None':
        """{uid: [(ts, ...), ...]} から (並びはそのまま)。空の list があれば None (この表では表せない: 呼び出し側は dict のまま使う)。"""
        if any(not v for v in d.values()):
            return None
        log = cls()
        for u, entries in d.items():
            for e in entries:
                log.add(u, *e)
        return log


class RatingHist(_SeriesLog):
    """rating_hist: uid → [(ts, rating), ...]。"""
    VAL_CODES = ('q', 'd')


class TourHistory(_SeriesLog):
    """player_tour_history: uid → [(ts, tid), ...]。"""
    VAL_CODES = ('q', 'q')


class BtTimeline(_SeriesLog):
    """bt_timeline: uid → [(ts, mu, sigma, ord), ...]。"""
    VAL_CODES = ('q', 'd', 'd', 'd')


class GatedHist(_SeriesLog):
    """build の rating_history (= bt_gated_hist): uid → [(tid, after, internal_d, d, peak_after), ...] (大会の時刻順)。
    bt_used (uid → {tid: bool}) も同じ行に持つ (BtUsedView)。以前は uid ごとの list と dict (北米で 280 万行)。
    行は uid ごとにまとめて、元の list の並びのまま add する (並びは呼び出し側が決める)。"""
    VAL_CODES = ('q', 'd', 'd', 'd', 'd', 'b')
    _N_OUT = 5   # 6 つ目 (bt_used) は used_of で読む

    def _vals_at(self, r: int) -> tuple:
        v = self._v
        return (v[0][r], v[1][r], v[2][r], v[3][r], v[4][r])

    def _chunk_values(self, gs, starts, ends, rows, vcols):
        return super()._chunk_values(gs, starts, ends, rows, vcols[:5])

    def used_of(self, uid):
        """uid の {tid: bool} (無ければ None)。"""
        g = self._group(uid)
        if g < 0:
            return None
        tid, used = self._v[0], self._v[5]
        return {tid[r]: used[r] != 0 for r in self._rows_of_group(g)}

    def d_lookup(self, tour_index: dict):
        """(uid, tid) → d (行の 4 つ目の値、float) を引く関数 (無ければ 0.0)。tour_index = {tid: 大会の番号 (0 以上 2^20 未満)}。
        spsp/score.build_at_time_per_tournament が以前作っていた {uid: {tid: d}} (北米で 280 万件) の代わり。
        同じ uid・tid の行が 2 つあれば後の行 (dict に代入し直したときと同じ)。tid が tour_index に無い行は引けない (None を返す
        ので、呼び出し側は以前の dict を作る)。"""
        n = self.n_rows
        uid = np.frombuffer(self._k[0], dtype=np.int64)
        tids = self._v[0].tolist()
        tix = [tour_index.get(t) for t in tids]
        if any(x is None for x in tix) or (n and (uid.min() < 0 or uid.max() >= 1 << 40)):
            return None
        code = (uid << 20) | np.array(tix, dtype=np.int64)
        del tids, tix
        order = np.argsort(code, kind='stable')
        sc = code[order]
        last = np.r_[sc[1:] != sc[:-1], True] if n else np.zeros(0, dtype=bool)   # 同じ組は後の行
        keys = array('q', sc[last].tobytes())
        vals = np.asarray(self._v[3])[order[last]].tolist()

        def get(u, tid):
            if type(u) is not int:
                if not isinstance(u, (int, np.integer)):
                    return 0.0
                u = int(u)
            ti = tour_index.get(tid)
            if ti is None or u < 0 or u >= 1 << 40:
                return 0.0
            c = (u << 20) | ti
            i = bisect_left(keys, c)
            return vals[i] if i < len(keys) and keys[i] == c else 0.0
        return get

    @classmethod
    def from_per_tour(cls, log: 'PerTourLog', eid_to_tid: dict, tid_to_ts: dict):
        """bt_per_tour (PerTourLog) から直接作る。(GatedHist, bt_current_ord) か、作れないとき None。

        spsp/build._v4_adapt_bt_outputs が以前作っていた形と同じ: uid ごとに (tid, after, internal_d, d, peak_after, used) を
        大会の時刻順 (同時刻は記録の並び) に、uid は bt_per_tour の並びで最初に出た順。tid が無い (event_id が今の大会に無い) 行は
        落とす。以前は uid ごとの list と {tid: bool} の dict (北米で 280 万行) を作ってから詰めていた。同じ uid・tid の行が
        2 つある (event_id の違う大会が同じ tid) ときは以前の dict の上書きを再現しないので None (以前の作り方に戻す)。"""
        comps, gcode, starts, ends, gorder, rows = log._index()
        if not len(gorder):
            return None
        k_uid = np.frombuffer(log._k[0], dtype=np.int64)
        k_eid = np.frombuffer(log._k[1], dtype=np.int64)
        r_key = rows[starts[gorder]]        # 組の並び (dict の挿入順) で、キーは最初の行
        r_val = rows[ends[gorder] - 1]      # 値は最後の行 (dict に代入し直したときの値)
        uid = k_uid[r_key]
        tids = [eid_to_tid.get(e) for e in k_eid[r_key].tolist()]
        keep = np.fromiter((t is not None for t in tids), dtype=bool, count=len(tids))
        if not keep.any():
            return None
        tid = np.array([t for t in tids if t is not None], dtype=np.int64)
        del tids
        uid = uid[keep]
        r_val = r_val[keep]
        ts = np.array([tid_to_ts.get(t, 0) for t in tid.tolist()], dtype=np.int64)
        if len(np.unique(np.stack([uid, tid], axis=1), axis=0)) != len(uid):
            return None
        _u, first = np.unique(uid, return_index=True)
        urank = np.empty(len(_u), dtype=np.int64)
        urank[np.argsort(first, kind='stable')] = np.arange(len(_u), dtype=np.int64)
        row_rank = urank[np.searchsorted(_u, uid)]
        order = np.lexsort((np.arange(len(uid)), ts, row_rank))   # uid の最初に出た順 → 時刻 → 記録の並び
        used, internal_d, d, after, peak_after = (np.asarray(c)[r_val][order] for c in log._v)
        out = cls()
        cols = (uid[order], tid[order], after, internal_d, d, peak_after, used)
        for dst, src, code in zip(out._k + out._v, cols, cls.KEY_CODES + cls.VAL_CODES):
            dst.frombytes(np.ascontiguousarray(src, dtype=np.dtype(code)).tobytes())
        su = cols[0]
        last = np.flatnonzero(np.r_[su[1:] != su[:-1], True])
        bt_current_ord = dict(zip(su[last].tolist(), after[last].tolist()))
        return out, bt_current_ord


class BtUsedView(Mapping):
    """build の bt_used: uid → {tid: bool} を GatedHist から引く。読む側は 1 人ずつ続けて .get(uid, {}).get(tid) するので、
    最後に引いた 1 人分だけ dict を取っておく (返す dict は読むだけ)。"""

    def __init__(self, hist: GatedHist):
        self._h = hist
        self._last = (None, None)

    def _of(self, uid):
        if self._last[0] is not None and self._last[0] == uid and type(self._last[0]) is type(uid):
            return self._last[1]
        d = self._h.used_of(uid)
        self._last = (uid, d)
        return d

    def get(self, uid, default=None):
        d = self._of(uid)
        return default if d is None else d

    def __getitem__(self, uid):
        d = self._of(uid)
        if d is None:
            raise KeyError(uid)
        return d

    def __contains__(self, uid):
        return uid in self._h

    def __iter__(self):
        return iter(self._h)

    def __len__(self):
        return len(self._h)


def _wl_mark(m) -> int:
    """試合の勝者・敗者の uid から作る 32 ビットの印 (TourMatchDeltas が t.matches の並びを確かめるのに使う)。"""
    return ((m.winner_id * 0x9E3779B1) ^ m.loser_id) & 0xFFFFFFFF


class TourMatchDeltas:
    """build の match_id_deltas ({id(Match): (d_w, d_l, w_tracked, l_tracked)}) を大会ごとの配列にしたもの。
    以前は試合ごとの dict (北米で 550 万件、tuple と float のオブジェクトで約 1 GB)。読むのは
    output._build_tour_matches_out だけで、大会の t.matches の順に引くので、作るときに t.matches の順に並べておく。
    of_tournament(t) = [元の dict の .get(id(m)) for m in t.matches] (無い試合は None)。
    t.matches が作ったときと違えば (並べ替え・足し引き) 黙って違う値を返さずに止まる (勝者・敗者の uid から作る 32 ビットの
    印の並びで確かめる。t.matches を後で作り直しても (spsp/lazy_matches、同じ値の別のオブジェクト) 引ける)。"""

    def __init__(self, d: dict, tournaments, matches_of: dict | None = None):
        """matches_of = {id(t): 試合の list} (d のキーの id(m) を作ったときと同じオブジェクト)。無ければ t.matches。"""
        self._off: dict = {}   # id(t) → (始まり, 終わり)
        self._wl = array('I'); self._dw = array('d'); self._dl = array('d'); self._fl = array('b')
        for t in tournaments:
            self.add_tournament(t, matches_of[id(t)] if matches_of is not None else t.matches, d)

    @classmethod
    def empty(cls) -> 'TourMatchDeltas':
        return cls({}, ())

    def add_tournament(self, t, ms, d: dict) -> None:
        """大会 t の試合 ms (d のキーの id(m) を作ったときと同じオブジェクト) の分を足す (同じ大会は 2 回足さない)。"""
        if id(t) in self._off:
            return
        wl, dw, dl, fl = self._wl, self._dw, self._dl, self._fl
        self._off[id(t)] = (len(wl), len(wl) + len(ms))
        for m in ms:
            wl.append(_wl_mark(m))
            v = d.get(id(m))
            if v is None:
                dw.append(0.0); dl.append(0.0); fl.append(0)
            else:
                dw.append(v[0]); dl.append(v[1]); fl.append(1 | (2 if v[2] else 0) | (4 if v[3] else 0))

    def of_tournament(self, t) -> list:
        se = self._off.get(id(t))
        ms = t.matches
        if se is None:
            if ms:
                raise RuntimeError(f'TourMatchDeltas: 作ったときに無かった大会 ({getattr(t, "name", "?")})')
            return []
        s, e = se
        if e - s != len(ms) or any(self._wl[s + i] != _wl_mark(m) for i, m in enumerate(ms)):
            raise RuntimeError(f'TourMatchDeltas: t.matches が作ったときと違う ({getattr(t, "name", "?")})')
        out = []
        for i in range(s, e):
            f = self._fl[i]
            out.append(None if not f else (self._dw[i], self._dl[i], bool(f & 2), bool(f & 4)))
        return out

    def __len__(self):
        return sum(1 for f in self._fl if f)


class _LvRows(_KeyedLog):
    """PerTourByLv の中身: (uid, tid) → 行 (lv, raw, pos, w, counted, np)、記録順。
    raw / pos / w は float か np.float64 (学習の点数は numpy の値のまま来て round しても np.float64)。
    np = どれが np.float64 だったかの印 (bit 0/1/2)。引くときに同じ型に戻す。"""
    KEY_CODES = ('q', 'q')
    VAL_CODES = ('b', 'd', 'd', 'd', 'b', 'b')

    def add_row(self, uid, tid, lv, raw, pos, w, counted):
        npf = ((1 if type(raw) is np.float64 else 0) | (2 if type(pos) is np.float64 else 0)
               | (4 if type(w) is np.float64 else 0))
        self._add_row((uid, tid), (lv, float(raw), float(pos), float(w), counted, npf))

    def value_list(self, r) -> list:
        """行 r の [raw, pos, w, counted] (元と同じ型)。"""
        v = self._v
        npf = v[5][r]
        if not npf:
            return [v[1][r], v[2][r], v[3][r], v[4][r]]
        f = [v[1][r], v[2][r], v[3][r]]
        return [np.float64(x) if npf & (1 << j) else x for j, x in enumerate(f)] + [v[4][r]]

    def rows_of_group(self, g) -> list:
        return self._rows_of_group(g)

    def _value_of_group(self, g):
        v = self._v
        return {v[0][r]: self.value_list(r) for r in self.rows_of_group(g)}

    def _chunk_values(self, gs, starts, ends, rows, vcols):
        return [self._value_of_group(int(g)) for g in gs]


class _LvOfTour(Mapping):
    """1 人 × 1 大会の {lv: [raw, pos, w, counted]} (引くたびに新しい list)。"""
    __slots__ = ('_log', '_g')

    def __init__(self, log, g):
        self._log = log; self._g = g

    def _rows(self):
        return self._log.rows_of_group(self._g)

    def __getitem__(self, lv):
        v = self._log._v
        for r in self._rows():
            if v[0][r] == lv and (type(lv) is int or isinstance(lv, (int, np.integer))):
                return self._log.value_list(r)
        raise KeyError(lv)

    def __iter__(self):
        lvs = self._log._v[0]
        return iter([lvs[r] for r in self._rows()])

    def __len__(self):
        return len(self._rows())

    def keys(self):   # dict(x) は keys() と [] で写す
        return list(self)

    def to_dict(self) -> dict:
        """dict(self) と同じ (lv ごとに 1 回ずつ行を探し直さない)。同じ lv が 2 行あれば最初の行 (__getitem__ と同じ)。"""
        log = self._log
        lvs = log._v[0]
        out = {}
        for r in self._rows():
            lv = lvs[r]
            if lv not in out:
                out[lv] = log.value_list(r)
        return out


class _ToursOfUid(Mapping):
    """1 人の {tid: _LvOfTour}。1 大会を引くだけなら (uid, tid) の組を直接引く (大会ページは参加者ごとに 1 大会だけ引く)。
    並び (iter / len) が要るときだけ、この人の組を集める (groups_with_first の並び = 以前と同じ)。"""
    __slots__ = ('_log', '_u', '_gs')

    def __init__(self, log, u):
        self._log = log; self._u = u; self._gs = None

    def _groups(self):
        if self._gs is None:
            self._gs = self._log.groups_with_first(self._u)
        return self._gs

    def _group_of(self, tid):
        if self._gs is not None:
            g = self._gs.get(tid)
            return -1 if g is None else g
        return self._log._group((self._u, tid))

    def __getitem__(self, tid):
        g = self._group_of(tid)
        if g < 0:
            raise KeyError(tid)
        return _LvOfTour(self._log, g)

    def get(self, tid, default=None):
        g = self._group_of(tid)
        return default if g < 0 else _LvOfTour(self._log, g)

    def __contains__(self, tid):
        return self._group_of(tid) >= 0

    def __iter__(self):
        return iter(self._groups())

    def __len__(self):
        return len(self._groups())


class PerTourByLv(Mapping):
    """build の per_tour_by_lv: uid → {tid: {lv: [raw, pos, w, counted]}}。以前は 3 段の dict と 4 要素の list
    (北米で 選手 × 大会 約 300 万件、1 件 約 350 バイト)。読む側は per_tour_by_lv.get(uid, {}).get(tid, {}) を
    dict(...) で写すか、そのまま持っておいて後で dict(...) するだけ (spsp/output.py)。並び (lv の順) は元の dict と同じ。
    1 人ずつ続けて引くので、最後に引いた 1 人分の {tid: 組の番号} だけ取っておく。"""

    def __init__(self, log: _LvRows):
        self._log = log
        self._last = (None, None)

    @classmethod
    def from_nested(cls, d) -> 'PerTourByLv | None':
        """{uid: {tid: {lv: [raw, pos, w, counted]}}} から。int でないキー・空の dict があれば None (dict のまま使う)。"""
        log = _LvRows()
        for uid, tmap in d.items():
            if type(uid) is not int or not tmap:
                return None
            for tid, lvmap in tmap.items():
                if type(tid) is not int or not lvmap:
                    return None
                for lv, (raw, pos, w, counted) in lvmap.items():
                    if type(lv) is not int or type(counted) is not int or not all(
                            type(x) is float or type(x) is np.float64 for x in (raw, pos, w)):
                        return None
                    log.add_row(uid, tid, lv, raw, pos, w, counted)
        return cls(log)

    @classmethod
    def from_contrib_tables(cls, tables, counted_thresh) -> 'PerTourByLv | None':
        """Lv ごとの寄与の表 (spsp/learn.ContribTable、tables[i] が Lv i+1) から、spsp/build._build_per_tour_by_lv が
        3 段の dict を作ってから from_nested するのと同じ中身を直接作る (北米で 一時的な dict と list が 1.5 GB だった)。
        値は [round(raw, 4), round(pos, 6), round(w, 4), int(rank_in_player < counted_thresh[i])] (型も同じ)。
        同じ選手の同じ Lv に同じ大会が 2 つある (dict だと上書き) か、int でない uid があれば None (dict で作る)。"""
        log = _LvRows()
        f64 = np.float64
        for lv_idx, tab in enumerate(tables):
            lv = lv_idx + 1
            thresh = counted_thresh[lv_idx]
            at, tid_c, raw_c, pos_c, w_c, npb = tab.columns()
            for uid, (r0, n) in at.items():
                if type(uid) is not int:
                    return None
                tids = tid_c[r0:r0 + n].tolist()
                if len(set(tids)) != n:
                    return None
                raws = raw_c[r0:r0 + n].tolist(); poss = pos_c[r0:r0 + n].tolist(); ws = w_c[r0:r0 + n].tolist()
                for i in range(n):
                    raw, pos, w = raws[i], poss[i], ws[i]
                    bits = npb.get(r0 + i) if npb else 0
                    if bits:
                        if bits & 1: raw = f64(raw)
                        if bits & 4: pos = f64(pos)
                        if bits & 8: w = f64(w)
                    log.add_row(uid, tids[i], lv, round(raw, 4), round(pos, 6), round(w, 4), int(i < thresh))
        return cls(log)

    def n_tours_players(self) -> tuple[int, int]:
        """(選手 × 大会の数, 選手の数)。"""
        n = len(self._log)
        return n, len(np.unique(np.frombuffer(self._log._k[0], dtype=np.int64))) if n else 0

    def _tours(self, uid):
        if self._last[0] is not None and self._last[0] == uid and type(self._last[0]) is type(uid):
            return self._last[1]
        t = None
        if (type(uid) is int or isinstance(uid, np.integer)) and self._log.n_rows \
                and self._log._lookup()[0][0].get(uid) is not None:   # この人の行がある
            t = _ToursOfUid(self._log, uid)
        self._last = (uid, t)
        return t

    def get(self, uid, default=None):
        t = self._tours(uid)
        return default if t is None else t

    def __getitem__(self, uid):
        t = self._tours(uid)
        if t is None:
            raise KeyError(uid)
        return t

    def __contains__(self, uid):
        return self._tours(uid) is not None

    def __iter__(self):
        seen = {}
        for (uid, _tid) in self._log:
            seen.setdefault(uid, None)
        return iter(seen)

    def __len__(self):
        return sum(1 for _ in self)


class _StrLvOfTour(Mapping):
    """StrPerTourByLv の 1 人 × 1 大会: {lv_str: [raw, pos, w, counted]} (並びは元の dict と同じ、引くたびに新しい list)。"""
    __slots__ = ('_log', '_g')

    def __init__(self, log, g):
        self._log = log; self._g = g

    def __getitem__(self, lv_s):
        lv = _canon_int(lv_s)
        if lv is not None:
            v = self._log._v
            for r in self._log.rows_of_group(self._g):
                if v[0][r] == lv:
                    return self._log.value_list(r)
        raise KeyError(lv_s)

    def __iter__(self):
        lvs = self._log._v[0]
        return iter([str(lvs[r]) for r in self._log.rows_of_group(self._g)])

    def __len__(self):
        return len(self._log.rows_of_group(self._g))


class _StrToursOfUid(Mapping):
    """StrPerTourByLv の 1 人: {大会の key (str): _StrLvOfTour} (並びは元の dict と同じ)。
    1 大会を引くだけなら (uid, 大会) の組を直接引く。並び (iter / items / len) が要るときだけ、この人の組を全部集めて並べる
    (大会ページの書き出しは参加者ごとに 1 大会だけ引くので、毎回この人の全大会を並べると遅かった)。"""
    __slots__ = ('_log', '_keys', '_u', '_gs')

    def __init__(self, log, keys, u):
        self._log = log; self._keys = keys; self._u = u; self._gs = None

    def _groups(self):
        """{大会の番号: 組の番号} (元の並び)。"""
        if self._gs is None:
            gs = self._log.groups_with_first(self._u)
            _c, _gc, starts, _e, _go, rows = self._log._index()
            self._gs = dict(sorted(gs.items(), key=lambda kv: rows[starts[kv[1]]]))   # 元の並び (最初に記録された順)
        return self._gs

    def _group_of(self, key):
        c = self._keys.code(key)
        if c is None:
            return -1
        if self._gs is not None:
            g = self._gs.get(c)
            return -1 if g is None else g
        return self._log._group((self._u, c))

    def get(self, key, default=None):
        g = self._group_of(key)
        return default if g < 0 else _StrLvOfTour(self._log, g)

    def __getitem__(self, key):
        v = self.get(key)
        if v is None:
            raise KeyError(key)
        return v

    def __contains__(self, key):
        return self._group_of(key) >= 0

    def __iter__(self):
        names = self._keys.names
        return (names[c] for c in self._groups())

    def items(self):
        names = self._keys.names
        return [(names[c], _StrLvOfTour(self._log, g)) for c, g in self._groups().items()]

    def __len__(self):
        return len(self._groups())


class _KeyNames:
    """大会の key (str) ↔ 通し番号。"""

    def __init__(self, names=None):
        self.names: list = names or []
        self._code = None

    def code(self, key):
        if self._code is None:
            self._code = {k: i for i, k in enumerate(self.names)}
        return self._code.get(key) if type(key) is str else None


class _ItemsOf:
    """m.items() の代わり (1 人ずつ作る。全員分を一度に list にしない)。"""

    def __init__(self, m):
        self._m = m

    def __iter__(self):
        m = self._m
        return ((k, m[k]) for k in m)

    def __len__(self):
        return len(self._m)


class StrPerTourByLv(Mapping):
    """snap の per_tour_by_lv: {uid_str: {大会の key (event_id の str か "cv:親:クラス"): {lv_str: [raw, pos, w, counted]}}}。
    以前は 3 段の dict と 4 要素の list (北米で 選手 × 大会 約 300 万件、中間ファイルを読んだ後に約 1.6 GB)。
    読む側 (spsp/overlay.py) は .get(uid_s, {}).get(key) / .items() / lv の .get・in・[] だけ。並びは元の dict と同じ。
    1 人ずつ続けて引くので、最後に引いた 1 人分だけ取っておく。"""

    def __init__(self, log: _LvRows, keys: _KeyNames):
        self._log = log; self._keys = keys
        self._last = (None, None)

    @classmethod
    def from_nested(cls, d) -> 'StrPerTourByLv | None':
        """元の dict から。表せないもの (uid・lv が int の str でない、空の dict、値の型が違う) があれば None。"""
        log = _LvRows(); names: list = []; code: dict = {}
        for uid_s, emap in d.items():
            u = _canon_int(uid_s)
            if u is None or not emap:
                return None
            for key, lvmap in emap.items():
                if type(key) is not str or not lvmap:
                    return None
                c = code.get(key)
                if c is None:
                    c = code[key] = len(names); names.append(key)
                for lv_s, v in lvmap.items():
                    lv = _canon_int(lv_s)
                    if lv is None or type(v) is not list or len(v) != 4:
                        return None
                    raw, pos, w, counted = v
                    if type(counted) is not int or not all(type(x) is float or type(x) is np.float64 for x in (raw, pos, w)):
                        return None
                    log.add_row(u, c, lv, raw, pos, w, counted)
        return cls(log, _KeyNames(names))

    @classmethod
    def from_contrib_tables(cls, tables: list, counted_thresh, tid_to_key: dict) -> 'StrPerTourByLv | None':
        """Lv ごとの寄与の表 (spsp/learn.ContribTable、tables[i] が Lv i+1) から、spsp/cli/build_full._compute_per_tour_by_lv が
        {str(uid): {key: {str(lv): [...]}}} を作ってから from_nested するのと同じ中身を直接作る (北米で +0.7 GB の一時的な dict)。
        値は [round(raw, 4), round(pos, 6), round(w, 4), int(rank_in_player < counted_thresh[i])]、key = tid_to_key[t_id]
        (無い大会は飛ばす)。変換し終えた Lv の表は tables から外す (None にする)。同じ選手・同じ Lv に同じ key が 2 つある
        (dict だと上書き) か、int でない uid があれば None (呼び出し側は dict で作る。tables は手放していないこと)。"""
        for tab in tables:   # 先に確かめる (作り始めてから dict に戻ると、手放した Lv が無い)
            at, tid_c = tab.columns()[:2]
            for uid, (r0, n) in at.items():
                if type(uid) is not int:
                    return None
                ks = [tid_to_key.get(t) for t in tid_c[r0:r0 + n].tolist()]
                ks = [k for k in ks if k is not None]
                if len(set(ks)) != len(ks) or any(type(k) is not str for k in ks):
                    return None
        log = _LvRows(); names: list = []; code: dict = {}
        f64 = np.float64
        for lv_idx in range(len(tables)):
            tab = tables[lv_idx]
            tables[lv_idx] = None
            lv = lv_idx + 1
            thresh = counted_thresh[lv_idx]
            at, tid_c, raw_c, pos_c, w_c, npb = tab.columns()
            for uid, (r0, n) in at.items():
                tids = tid_c[r0:r0 + n].tolist()
                raws = raw_c[r0:r0 + n].tolist(); poss = pos_c[r0:r0 + n].tolist(); ws = w_c[r0:r0 + n].tolist()
                for i in range(n):
                    key = tid_to_key.get(tids[i])
                    if key is None:
                        continue
                    c = code.get(key)
                    if c is None:
                        c = code[key] = len(names); names.append(key)
                    raw, pos, w = raws[i], poss[i], ws[i]
                    bits = npb.get(r0 + i) if npb else 0
                    if bits:
                        if bits & 1: raw = f64(raw)
                        if bits & 4: pos = f64(pos)
                        if bits & 8: w = f64(w)
                    log.add_row(uid, c, lv, round(raw, 4), round(pos, 6), round(w, 4), int(i < thresh))
            del tab
        return cls(log, _KeyNames(names))

    def n_players(self) -> int:
        return len(np.unique(np.frombuffer(self._log._k[0], dtype=np.int64))) if self._log.n_rows else 0

    def _tours(self, uid_s):
        if self._last[0] is not None and self._last[0] == uid_s:
            return self._last[1]
        u = _canon_int(uid_s)
        t = None
        if u is not None and self._log.n_rows and self._log._lookup()[0][0].get(u) is not None:   # この人の行がある
            t = _StrToursOfUid(self._log, self._keys, u)
        self._last = (uid_s, t)
        return t

    def get(self, uid_s, default=None):
        t = self._tours(uid_s)
        return default if t is None else t

    def __getitem__(self, uid_s):
        t = self._tours(uid_s)
        if t is None:
            raise KeyError(uid_s)
        return t

    def __contains__(self, uid_s):
        return self._tours(uid_s) is not None

    def __iter__(self):
        seen = {}
        for (u, _c) in self._log:
            seen.setdefault(u, None)
        return (str(u) for u in seen)

    def items(self):
        return _ItemsOf(self)

    def __len__(self):
        return len(self._log._index()[0][0])   # uid の種類の数

    def __getstate__(self):
        return {'log': self._log, 'names': self._keys.names}

    def __setstate__(self, s):
        self._log = s['log']; self._keys = _KeyNames(s['names']); self._last = (None, None)


class AtTimeOfTour(Mapping):
    """build_at_time_per_tournament の 1 大会分 {uid: (lv, ens_b, ens_a, tjpr_b, tjpr_a, bt_b, bt_a, tjpr_counts)} を
    uid の昇順の配列で持つ (以前は uid ごとの tuple と int のオブジェクト。北米で 大会 × 参加者 数百万件、約 1 GB)。
    引くと make(tuple) を返す (spsp/score.AtTimeRec)。値は lv = int、順位 6 つ = int か None、tjpr_counts = bool に限る
    (それ以外が混ざれば compact() が None を返すので、呼び出し側は dict のまま使う)。読む側は .get(uid) だけ。"""
    __slots__ = ('_uids', '_cols', '_nul', '_lv', '_cnt', '_make', '_order')

    @classmethod
    def compact(cls, d: dict, make):
        n = len(d)
        if not n:
            return None
        for u, r in d.items():
            if type(u) is not int or len(r) != 8 or type(r[0]) is not int or type(r[7]) is not bool:
                return None
            for x in r[1:7]:
                if x is not None and type(x) is not int:
                    return None
        self = cls.__new__(cls)
        keys = list(d)
        order = sorted(range(n), key=keys.__getitem__)
        self._uids = array('q', [keys[i] for i in order])
        vals = list(d.values())
        self._lv = array('q', [vals[i][0] for i in order])
        self._cnt = array('b', [1 if vals[i][7] else 0 for i in order])
        self._cols = tuple(array('q', [0 if vals[i][j] is None else vals[i][j] for i in order]) for j in range(1, 7))
        self._nul = array('b', [sum(1 << (j - 1) for j in range(1, 7) if vals[i][j] is None) for i in order])
        inv = [0] * n
        for p, i in enumerate(order):
            inv[i] = p
        self._order = array('i', inv)   # 元の dict の i 番目のキーが uid の昇順で何番目か
        self._make = make
        return self

    def _pos(self, uid):
        if type(uid) is not int and not isinstance(uid, (int, np.integer)):
            return -1
        i = bisect_left(self._uids, uid)
        return i if i < len(self._uids) and self._uids[i] == uid else -1

    def _rec(self, i):
        nul = self._nul[i]
        ranks = [None if nul & (1 << j) else self._cols[j][i] for j in range(6)]
        return self._make((self._lv[i], *ranks, bool(self._cnt[i])))

    def __getitem__(self, uid):
        i = self._pos(uid)
        if i < 0:
            raise KeyError(uid)
        return self._rec(i)

    def get(self, uid, default=None):
        i = self._pos(uid)
        return default if i < 0 else self._rec(i)

    def __contains__(self, uid):
        return self._pos(uid) >= 0

    def __iter__(self):   # 元の dict の並び
        return (self._uids[i] for i in self._order)

    def __len__(self):
        return len(self._uids)

    def items(self):   # 元の dict の並び (1 件ずつ引き直さない)
        u, rec = self._uids, self._rec
        return [(u[i], rec(i)) for i in self._order]


class IntCounts(Mapping):
    """{uid: 数 (int)} の読むだけの表 (uid の昇順の配列)。大会ごとの勝ち・負けの数 (spsp/score._collect_per_tournament_outcomes)・
    大会ごとのパフォーマンス順位 (spsp/output の perf_cache) に使う。以前は大会ごとの defaultdict(int) (北米で 大会 × 参加者 数百万件)。読む側は .get(uid, 0) と sum(.values()) だけ。
    並び (iter) は元の dict と同じ。"""
    __slots__ = ('_uids', '_n', '_order', '_total')

    @classmethod
    def compact(cls, d) -> 'IntCounts | None':
        if not d or any(type(u) is not int or type(v) is not int for u, v in d.items()):
            return None
        self = cls.__new__(cls)
        keys = list(d)
        order = sorted(range(len(keys)), key=keys.__getitem__)
        self._uids = array('q', [keys[i] for i in order])
        self._n = array('q', [d[keys[i]] for i in order])
        inv = [0] * len(keys)
        for p, i in enumerate(order):
            inv[i] = p
        self._order = array('i', inv)
        self._total = sum(self._n)
        return self

    def total(self) -> int:
        """sum(self.values()) (作るときに数えておく)。"""
        return self._total

    def _pos(self, uid):
        if type(uid) is not int and not isinstance(uid, (int, np.integer)):
            return -1
        i = bisect_left(self._uids, uid)
        return i if i < len(self._uids) and self._uids[i] == uid else -1

    def __getitem__(self, uid):
        i = self._pos(uid)
        if i < 0:
            raise KeyError(uid)
        return self._n[i]

    def get(self, uid, default=None):
        i = self._pos(uid)
        return default if i < 0 else self._n[i]

    def __contains__(self, uid):
        return self._pos(uid) >= 0

    def __iter__(self):
        return (self._uids[p] for p in self._order)

    def values(self):
        return [self._n[p] for p in self._order]

    def __len__(self):
        return len(self._uids)


def compact_int_counts(dd):
    """{tid: {uid: int}} の各大会を IntCounts に (表せない大会・空の大会はそのまま)。キーの並びは同じ。"""
    return {tid: (IntCounts.compact(d) or d) for tid, d in dd.items()}


class TourEntryStore:
    """build の per_tournament_data ({tid: {uid: 14 項目の tuple}}、spsp/output._accumulate_per_tournament_data) を
    列ごとの配列で持つ (以前は tuple と dict と float のオブジェクトで 1 件 約 300 バイト。北米で 大会 × 参加者 約 300 万件)。
    get(tid) は {uid: tuple} の dict を作って返す (大会ページを 1 つ組み立てる間だけ使う)。並び・値は元と同じ。
    列の型に合わない値 (int / None でない順位、float でない点数 など) が来た行は、元の tuple のまま別に持つ。
    tuple の並び: (uid, place, seed, spr, ens_rank, tj_rank, bt_rank, tjpr_w, tjpr_raw, tjpr_lv, tjpr_counted,
                   bt_d, bt_internal_d, bt_used)"""

    _INT_COLS = (1, 2, 3, 4, 5, 6, 9)     # int か None
    _FLT_COLS = (7, 8, 11, 12)            # float
    _BOOL_COLS = (10, 13)                 # bool
    # float の列は float か np.float64 (学習の点数は numpy の値のまま来る)。どちらだったかを行ごとに持ち、同じ型で返す

    def __init__(self):
        self._tid = array('q'); self._uid = array('q')
        self._i = tuple(array('q') for _ in self._INT_COLS)
        self._f = tuple(array('d') for _ in self._FLT_COLS)
        self._b = array('b')    # bit0 = tjpr_counted, bit1 = bt_used
        self._nul = array('b')  # _INT_COLS の何番目が None か (bit)
        self._np = array('b')   # _FLT_COLS の何番目が np.float64 か (bit)
        self._odd: dict = {}    # 行番号 → 元の tuple (列の型に合わない行)
        self._ix = None

    def _fits(self, r) -> bool:
        if len(r) != 14 or type(r[0]) is not int:
            return False
        for c in self._INT_COLS:
            if r[c] is not None and type(r[c]) is not int:
                return False
        for c in self._FLT_COLS:
            if type(r[c]) is not float and type(r[c]) is not np.float64:
                return False
        return all(type(r[c]) is bool for c in self._BOOL_COLS)

    def add(self, tid: int, r: tuple) -> int:
        """行を足して行番号を返す。"""
        n = len(self._tid)
        self._tid.append(tid)
        if self._fits(r):
            self._uid.append(r[0])
            nul = 0
            for j, c in enumerate(self._INT_COLS):
                v = r[c]
                if v is None:
                    nul |= 1 << j; v = 0
                self._i[j].append(v)
            npf = 0
            for j, c in enumerate(self._FLT_COLS):
                self._f[j].append(float(r[c]))
                if type(r[c]) is np.float64:
                    npf |= 1 << j
            self._b.append((1 if r[10] else 0) | (2 if r[13] else 0))
            self._nul.append(nul); self._np.append(npf)
        else:
            self._uid.append(r[0] if type(r[0]) is int else 0)
            for a in self._i:
                a.append(0)
            for a in self._f:
                a.append(0.0)
            self._b.append(0); self._nul.append(0); self._np.append(0)
            self._odd[n] = r
        self._ix = None
        return n

    def replace(self, row: int, r: tuple) -> None:
        """行 row を r に置き換える (同じ大会に同じ選手を 2 回入れたときの上書き = dict に代入し直すのと同じ)。"""
        tid = self._tid[row]
        tmp = TourEntryStore()
        tmp.add(tid, r)
        self._uid[row] = tmp._uid[0]
        for a, b in zip(self._i, tmp._i):
            a[row] = b[0]
        for a, b in zip(self._f, tmp._f):
            a[row] = b[0]
        self._b[row] = tmp._b[0]; self._nul[row] = tmp._nul[0]; self._np[row] = tmp._np[0]
        if tmp._odd:
            self._odd[row] = r
        else:
            self._odd.pop(row, None)

    def _row(self, k) -> tuple:
        odd = self._odd.get(k)
        if odd is not None:
            return odd
        nul = self._nul[k]
        iv = [None if nul & (1 << j) else a[k] for j, a in enumerate(self._i)]
        npf = self._np[k]
        fv = [np.float64(a[k]) if npf & (1 << j) else a[k] for j, a in enumerate(self._f)]
        b = self._b[k]
        # (uid, place, seed, spr, ens, tj, bt, tjpr_w, tjpr_raw, tjpr_lv, counted, bt_d, bt_internal_d, used)
        return (self._uid[k], iv[0], iv[1], iv[2], iv[3], iv[4], iv[5], fv[0], fv[1], iv[6], bool(b & 1),
                fv[2], fv[3], bool(b & 2))

    def _index(self):
        if self._ix is None:
            t = np.frombuffer(self._tid, dtype=np.int64) if len(self._tid) else np.zeros(0, dtype=np.int64)
            rows = np.argsort(t, kind='stable')   # 大会ごと、同じ大会の中は足した順
            st = t[rows]
            starts = np.flatnonzero(np.r_[True, st[1:] != st[:-1]]) if len(st) else np.zeros(0, dtype=np.int64)
            ends = np.r_[starts[1:], len(st)] if len(st) else np.zeros(0, dtype=np.int64)
            self._ix = (dict(zip(st[starts].tolist(), zip(starts.tolist(), ends.tolist()))), rows)
        return self._ix

    def get(self, tid, default=None):
        span, rows = self._index()
        se = span.get(tid)
        if se is None:
            return default
        out = {}
        for k in rows[se[0]:se[1]].tolist():
            r = self._row(k)
            out[r[0]] = r
        return out

    def __len__(self):
        return len(self._index()[0])

    @property
    def n_odd(self) -> int:
        return len(self._odd)


_VIEW_FIELDS = ('round', 'round_text', 'phase_name', 'phase_bracket_type', 'winners_top', 'losers_top', 'bracket_label',
                'global_round', 'global_top_x', 'global_bracket_label', 'winner_score', 'loser_score',
                'legacy_global_top_x', 'legacy_global_bracket_label')


class MatchView:
    """選手の対戦一覧 (spsp/output._build_recent_matches / _build_top_bt_gains) が試合から読む項目だけを持つもの。
    PlayerMatches が引くたびに作る (Match そのものは持たない)。読む側は getattr(m, 'round_text', None) などで読む。"""
    __slots__ = _VIEW_FIELDS

    def __init__(self, vals):
        for k, v in zip(_VIEW_FIELDS, vals):
            object.__setattr__(self, k, v)


class PlayerMatches(Mapping):
    """build の player_matches: uid → [(ts, tid, opp_uid, won, bt_d, is_class, Match, tracked), ...] (記録順)。

    以前は defaultdict(list) に 8 要素の tuple を足していた (北米で 1,100 万行、Python のオブジェクトで数 GB)。
    ここでは数値を列の配列に、Match は 1 試合 1 個の list に持ち、引いたときだけ同じ tuple の list を作る。
    ts・tid・uid は int に限る (呼び出し側が確かめる)。読む側は .get(uid, []) で引くだけ (spsp/output.py、spsp/score.py)。
    """

    def __init__(self):
        self._uid = array('q'); self._ts = array('q'); self._tid = array('q'); self._opp = array('q')
        self._won = array('b'); self._d = array('d'); self._cls = array('b'); self._mi = array('l'); self._tr = array('b')
        # 試合は Match を持たず、表示に読む項目 (_VIEW_FIELDS) の値の組の番号だけを持つ (試合 1 件 4 バイト。同じ組の試合は
        # 多い。Match を持つと t.matches を手放しても試合が全部メモリに残る)。m が None (試合が見つからない) なら -1
        self._seq = array('q')   # 並びを決めたら (_index) 手放す
        self._mv = array('i')
        self._combo: dict = {}   # (値の組, 型の組) → 組の番号
        self._combo_vals: list = []
        self._views: list = []   # 組の番号 → MatchView (_view が初めて引いたときに作る)
        self._nm = 0
        self._ix = None
        self._tix = None   # 足し終わったら ts・tid の代わりに大会の番号 (_compact_cols)

    def add_match(self, m) -> int:
        """試合を登録して番号を返す (勝者・敗者の 2 行で同じ番号を使う)。"""
        if self._combo is None:
            raise RuntimeError('PlayerMatches: 並びを決めた後に試合を足した')
        if m is None:
            self._mv.append(-1)
        else:
            # 値と型の組で引く (1 と 1.0 と True は別の組)
            vals = tuple([getattr(m, k, None) for k in _VIEW_FIELDS])
            key = (vals, tuple(map(type, vals)))
            c = self._combo.get(key)
            if c is None:
                c = self._combo[key] = len(self._combo_vals)
                self._combo_vals.append(vals)
                self._views.append(None)
            self._mv.append(c)
        self._nm += 1
        return self._nm - 1

    def _view(self, i):
        # 同じ組の試合は同じ MatchView (読むだけで書き換えない)。組の数は試合よりずっと少ない
        c = self._mv[i]
        if c < 0:
            return None
        v = self._views[c]
        if v is None:
            v = self._views[c] = MatchView(self._combo_vals[c])
        return v

    def add(self, uid, ts, tid, opp, won, bt_d, is_cls, m_index, tracked, seq=None):
        """seq: 並びの番号 (渡すなら全行に、行ごとに別の値)。uid ごとの行の並びと uid の並びを、足した順ではなく seq の順にする
        (spsp/build.py が大会ごとに足すとき、以前の「試合の記録の順」に戻すのに使う)。"""
        if seq is not None:
            if self._seq is None:
                raise RuntimeError('PlayerMatches: 並びを決めた後に seq 付きの行を足した')
            self._seq.append(seq)
        self._uid.append(uid); self._ts.append(ts); self._tid.append(tid); self._opp.append(opp)
        self._won.append(1 if won else 0); self._d.append(bt_d); self._cls.append(1 if is_cls else 0)
        self._mi.append(m_index); self._tr.append(1 if tracked else 0)
        self._ix = None

    def _index(self):
        if self._ix is None:
            u = np.frombuffer(self._uid, dtype=np.int64) if len(self._uid) else np.zeros(0, dtype=np.int64)
            if self._seq is None:
                raise RuntimeError('PlayerMatches: 並びを決めた後に行を足した (seq は手放してある)')
            if len(self._seq):
                if len(self._seq) != len(self._uid):
                    raise RuntimeError('PlayerMatches: seq を渡した行と渡さない行が混ざっている')
                sq = np.frombuffer(self._seq, dtype=np.int64)
                rows = np.lexsort((sq, u))          # uid 順、同じ uid の中は seq 順
            else:
                sq = None
                rows = np.argsort(u, kind='stable')   # uid 順、同じ uid の中は記録順
            su = u[rows]
            starts = np.flatnonzero(np.r_[True, su[1:] != su[:-1]]) if len(su) else np.zeros(0, dtype=np.int64)
            ends = np.r_[starts[1:], len(su)] if len(su) else np.zeros(0, dtype=np.int64)
            first = rows[starts]
            order = np.argsort(first if sq is None else sq[first], kind='stable')   # dict の挿入順 = その uid の最初の行の順
            uids = su[starts]
            self._ix = (dict(zip(uids[order].tolist(), zip(starts[order].tolist(), ends[order].tolist()))), rows)
            if sq is not None:
                # seq 付き (spsp/build.py の大会ごとの足し方) は足し終わってから引くので、並びは rows に入れて seq を手放す
                # (北米で 90 MB)。値の番号の表も足すときにだけ使う。以後は足せない
                del sq
                self._seq = None
                self._combo = None
                self._compact_cols()
        return self._ix

    def _compact_cols(self):
        """足し終わった後 (以後は読むだけ) に列を小さくする: uid・相手・試合の番号は 32 bit に収まれば 32 bit に、ts・tid は
        大会の番号 (32 bit) と大会ごとの ts・tid の表に (1 行 40 → 16 バイト、北米で 1,100 万行)。引いた値は同じ int。"""
        def small(a):
            v = np.asarray(a, dtype=np.int64)
            if len(v) and (v.min() < -(1 << 31) or v.max() >= 1 << 31):
                return a
            return array('i', v.astype(np.int32).tobytes())
        self._uid = small(self._uid); self._opp = small(self._opp); self._mi = small(self._mi)
        tid = np.asarray(self._tid, dtype=np.int64); ts = np.asarray(self._ts, dtype=np.int64)
        utid, inv = np.unique(tid, return_inverse=True)
        tts = np.zeros(len(utid), dtype=np.int64)
        tts[inv] = ts
        if len(utid) < 1 << 31 and np.array_equal(tts[inv], ts):   # 同じ大会の行は同じ ts のときだけ
            self._tix = array('i', inv.astype(np.int32).tobytes())
            self._tour = (utid, tts)
            self._ts = self._tid = None

    def _rows_of(self, uid):
        span, rows = self._index()
        se = span.get(uid) if (type(uid) is int or isinstance(uid, np.integer)) else None
        return None if se is None else rows[se[0]:se[1]]

    def _tuples(self, rr):
        view = self._view
        if self._tix is not None:
            tix = np.asarray(self._tix)[rr]
            ts, tid = self._tour[1][tix].tolist(), self._tour[0][tix].tolist()
        else:
            ts, tid = np.asarray(self._ts)[rr].tolist(), np.asarray(self._tid)[rr].tolist()
        opp, won, d, cls, mi, tr = [np.asarray(c)[rr].tolist() for c in (self._opp, self._won, self._d,
                                                                          self._cls, self._mi, self._tr)]
        return [(a, b, c, bool(w), x, bool(k), view(i), bool(t))
                for a, b, c, w, x, k, i, t in zip(ts, tid, opp, won, d, cls, mi, tr)]

    def get(self, uid, default=None):
        rr = self._rows_of(uid)
        return default if rr is None else self._tuples(rr)

    def __getitem__(self, uid):
        rr = self._rows_of(uid)
        if rr is None:
            raise KeyError(uid)
        return self._tuples(rr)

    def __contains__(self, uid):
        return self._rows_of(uid) is not None

    def __iter__(self):
        return iter(self._index()[0])

    def __len__(self):
        return len(self._index()[0])


class MatchWins(Mapping):
    """build の match_wins: uid → [(tid, opp_uid, d_w, Match), ...] = player_matches のうち勝って d_w > 0 の行 (同じ順)。
    以前は player_matches と同じ試合を別の dict に持っていた。"""

    def __init__(self, pm: PlayerMatches):
        self._pm = pm

    def _wins(self, uid):
        lst = self._pm.get(uid)
        if lst is None:
            return None
        return [(tid, opp, d, m) for (_ts, tid, opp, won, d, _c, m, _t) in lst if won and d > 0]

    def get(self, uid, default=None):
        w = self._wins(uid)
        return default if not w else w

    def __getitem__(self, uid):
        w = self._wins(uid)
        if not w:
            raise KeyError(uid)
        return w

    def __contains__(self, uid):
        return bool(self._wins(uid))

    def __iter__(self):
        return (u for u in self._pm if self._wins(u))

    def __len__(self):
        return sum(1 for _ in self)


class UidIndex:
    """uid (int) → 通し番号 (SHARED_UIDS を全体で 1 つ使う)。日ごと・指標ごとの {uid: 値} の表 (UidMap) が共有する (北米で 20 万人 × 61 日 × 数種類)。"""

    def __init__(self):
        self.pos: dict = {}
        self.uids = array('q')

    def index_of(self, uid) -> int:
        i = self.pos.get(uid)
        if i is None:
            i = self.pos[uid] = len(self.uids)
            self.uids.append(uid)
        return i


_ABSENT = -1


class UidMap(Mapping):
    """読むだけの {uid: int} / {uid: float} (元の dict と同じキーの並び・値・型)。
    UidIndex の番号 → この表の位置 (無ければ -1) と、位置ごとの値を配列で持つ。書き換えはできない (dict に戻して使う)。"""

    __slots__ = ('_ix', '_at', '_keys', '_vals')

    def __init__(self, ix: UidIndex, at, keys, vals):
        self._ix = ix; self._at = at; self._keys = keys; self._vals = vals

    def _pos(self, uid):
        if type(uid) is not int:
            if isinstance(uid, (int, np.integer)):   # dict と同じく True / np.int64(1) / 1.0 は 1 と同じキー
                uid = int(uid)
            elif isinstance(uid, (float, np.floating)) and float(uid).is_integer():
                uid = int(uid)
            else:
                return _ABSENT
        i = self._ix.pos.get(uid)
        if i is None or i >= len(self._at):
            return _ABSENT
        return self._at[i]

    def __getitem__(self, uid):
        p = self._pos(uid)
        if p < 0:
            raise KeyError(uid)
        return self._vals[p]

    def get(self, uid, default=None):
        if type(uid) is int:   # 多くはこれ (build で数千万回引く。_pos を呼ぶ手間を省く)
            i = self._ix.pos.get(uid)
            if i is None or i >= len(self._at):
                return default
            p = self._at[i]
        else:
            p = self._pos(uid)
        return default if p < 0 else self._vals[p]

    def __contains__(self, uid):
        if type(uid) is int:
            i = self._ix.pos.get(uid)
            return i is not None and i < len(self._at) and self._at[i] >= 0
        return self._pos(uid) >= 0

    def __iter__(self):
        return iter(_uids_at(self._ix, self._keys))

    def __len__(self):
        return len(self._keys)

    def keys(self):
        return _uids_at(self._ix, self._keys)

    def values(self):
        return self._vals.tolist()

    def items(self):
        return list(zip(_uids_at(self._ix, self._keys), self._vals.tolist()))

    def __repr__(self):
        return f'<UidMap {len(self)} uids>'

    def __eq__(self, other):
        if not isinstance(other, Mapping):
            return NotImplemented
        return list(self.items()) == list(other.items())

    __hash__ = None


def _uids_at(ix: UidIndex, keys) -> list:
    """番号の並び keys の uid (int) の list。"""
    if not len(keys):
        return []
    return np.frombuffer(ix.uids, dtype=np.int64)[np.frombuffer(keys, dtype=np.int32)].tolist()


def tour_value(m, tid, uid, default=None):
    """m.get(tid, {}).get(uid, default) (m = TourUidTable なら 1 大会分の見え方を作らずに引く)。"""
    if type(m) is TourUidTable:
        return m.value(tid, uid, default)
    return m.get(tid, {}).get(uid, default)


def compact_uid_map(d, ix: UidIndex):
    """{uid: 値} の dict を UidMap にする。キーがすべて int (bool 以外)、値がすべて int (bool 以外) か
    すべて float のときだけ。それ以外 (空・型が混ざる・UidMap 済み) はそのまま返す。"""
    if type(d) is not dict or not d:
        return d
    if set(map(type, d)) != {int}:
        return d
    vts = set(map(type, d.values()))
    vt = vts.pop() if len(vts) == 1 else None
    if vt is not int and vt is not float:
        return d
    get = ix.pos.get
    idx = [get(k) for k in d]
    if None in idx:
        idx = [ix.index_of(k) for k in d]
    keys = array('i', idx)
    if vt is int:
        vals = array('q', d.values())
        if len(vals) and -(1 << 31) <= min(vals) and max(vals) < (1 << 31):
            vals = array('i', vals)   # 順位などは 4 バイトで足りる (引いた値は同じ int)
    else:
        vals = array('d', d.values())
    # 同じ日の表はキーの並びが同じものが多い (tjpr_levels / tjpr_scores / tjpr_elo など)。並びが同じなら索引を共有する
    for ix2, k2, at2 in _RECENT_KEYS:
        if ix2 is ix and k2 == keys:
            return UidMap(ix, at2, k2, vals)
    at_np = np.full(len(ix.uids), _ABSENT, dtype=np.int32)
    at_np[np.asarray(idx, dtype=np.int64)] = np.arange(len(idx), dtype=np.int32)
    at = array('i', at_np.tobytes())
    _RECENT_KEYS.append((ix, keys, at))
    del _RECENT_KEYS[:-8]
    return UidMap(ix, at, keys, vals)


_RECENT_KEYS: list = []   # compact_uid_map が最近作った (ix, keys, at) (同じ並びの表で共有する)


SHARED_UIDS = UidIndex()   # build の日ごとの表が共有する uid の番号 (プロセスに 1 つ)


def _canon_int(s):
    """str(k) == s となる int k (無ければ None)。"""
    if type(s) is not str:
        return None
    try:
        k = int(s)
    except ValueError:
        return None
    return k if str(k) == s else None


class StrUidTable(Mapping):
    """読むだけの {str(uid): 数} / {str(uid): [数, 数, …]} (snap の日ごとの表。元の dict と同じキーの並び・値・型)。
    行のときは引くたびに新しい list を返す。書き換えはできない。"""

    __slots__ = ('_ix', '_at', '_keys', '_cols', '_row')

    def __init__(self, ix: UidIndex, at, keys, cols, row: bool):
        self._ix = ix; self._at = at; self._keys = keys; self._cols = cols; self._row = row

    def _pos(self, key):
        k = _canon_int(key)
        if k is None:
            return _ABSENT
        i = self._ix.pos.get(k)
        if i is None or i >= len(self._at):
            return _ABSENT
        return self._at[i]

    def _val(self, p):
        if self._row:
            return [c[p] for c in self._cols]
        return self._cols[0][p]

    def __getitem__(self, key):
        p = self._pos(key)
        if p < 0:
            raise KeyError(key)
        return self._val(p)

    def get(self, key, default=None):
        p = self._pos(key)
        return default if p < 0 else self._val(p)

    def __contains__(self, key):
        return self._pos(key) >= 0

    def __iter__(self):
        return iter(list(map(str, _uids_at(self._ix, self._keys))))

    def __len__(self):
        return len(self._keys)

    def keys(self):
        return list(self)

    def values(self):
        if self._row:
            return [list(r) for r in zip(*(c.tolist() for c in self._cols))]
        return self._cols[0].tolist()

    def items(self):
        return list(zip(self, self.values()))

    def int_items(self) -> list:
        """[(int(uid), 値), ...] (items() のキーを int にしたもの。並びは同じ)。"""
        return list(zip(_uids_at(self._ix, self._keys), self.values()))

    def __repr__(self):
        return f'<StrUidTable {len(self)} uids>'

    def __eq__(self, other):
        if not isinstance(other, Mapping):
            return NotImplemented
        return list(self.items()) == list(other.items())

    __hash__ = None


def _col_code(vals) -> str | None:
    t = None
    for v in vals:
        tv = type(v)
        if tv is not int and tv is not float:
            return None
        if t is None:
            t = tv
        elif tv is not t:
            return None
    return 'q' if t is int else 'd'


def compact_str_table(d, ix: UidIndex):
    """{str(uid): 数} または {str(uid): [数, …] (全部同じ長さ)} の dict を StrUidTable にする。キーが str(int) の正規形、
    列ごとに型がそろう (int だけ / float だけ) ときだけ。それ以外はそのまま返す。"""
    if type(d) is not dict or not d:
        return d
    ks = []
    for k in d:
        i = _canon_int(k)
        if i is None:
            return d
        ks.append(i)
    vs = list(d.values())
    v0 = vs[0]
    if type(v0) is list:
        n = len(v0)
        if any(type(v) is not list or len(v) != n for v in vs):
            return d
        cols_v = list(zip(*vs)) if n else []
        codes = [_col_code(c) for c in cols_v]
        if not n or None in codes:
            return d
        cols = tuple(array(c, col) for c, col in zip(codes, cols_v))
        row = True
    else:
        c = _col_code(vs)
        if c is None:
            return d
        cols = (array(c, vs),)
        row = False
    keys = array('i', (ix.index_of(k) for k in ks))
    at = array('i', [_ABSENT]) * len(ix.uids)
    for p, i in enumerate(keys):
        at[i] = p
    return StrUidTable(ix, at, keys, cols, row)



class _TourUidView(Mapping):
    """TourUidTable の 1 大会分 {uid: 値} (読むだけ)。並び (iter / items) は元の dict と同じ。"""
    __slots__ = ('_t', '_lo', '_hi', '_tot')

    def __init__(self, table, lo, hi, tot=None):
        self._t = table; self._lo = lo; self._hi = hi; self._tot = tot

    def _pos(self, uid):
        if type(uid) is not int and not isinstance(uid, (int, np.integer)):
            return -1
        t = self._t
        i = bisect_left(t._suid, uid, self._lo, self._hi)
        return t._perm[i] if i < self._hi and t._suid[i] == uid else -1

    def __getitem__(self, uid):
        r = self._pos(uid)
        if r < 0:
            raise KeyError(uid)
        return self._t._value(r)

    def get(self, uid, default=None):
        if type(uid) is int:   # _pos と同じ (呼ぶ手間を省く)
            t = self._t
            i = bisect_left(t._suid, uid, self._lo, self._hi)
            return t._value(t._perm[i]) if i < self._hi and t._suid[i] == uid else default
        r = self._pos(uid)
        return default if r < 0 else self._t._value(r)

    def __contains__(self, uid):
        return self._pos(uid) >= 0

    def __iter__(self):
        return iter(self._t._uid[self._lo:self._hi].tolist())

    def __len__(self):
        return self._hi - self._lo

    def keys(self):
        return self._t._uid[self._lo:self._hi].tolist()

    def values(self):
        v = self._t._value
        return [v(r) for r in range(self._lo, self._hi)]

    def items(self):
        v = self._t._value
        return list(zip(self._t._uid[self._lo:self._hi].tolist(), (v(r) for r in range(self._lo, self._hi))))

    def total(self) -> int:
        """sum(self.values()) (totals=True の表だけ。足すときに数えておく)。"""
        if self._tot is None:
            raise TypeError('TourUidTable: totals=True で作った表ではない')
        return self._tot

    def __repr__(self):
        return f'<TourUidView {len(self)} uids>'

    def __eq__(self, other):
        if not isinstance(other, Mapping):
            return NotImplemented
        return list(self.items()) == list(other.items())

    __hash__ = None


class TourUidTable(Mapping):
    """読むだけの {tid: {uid: 値}} (大会 × 参加者。順位・シード・順位変動・大会時点の順位・勝ち負けの数)。以前は
    大会ごとの dict や小さな配列 (北米で 大会 16 万・参加者のべ 約 300 万件、1 件 70〜150 バイト)。全大会の行を
    1 本の配列に続けて持ち、大会ごとに uid の昇順の並び (bisect 用) も持つ。tid を引くと 1 大会分の見え方
    (_TourUidView) を返す。値は 1 つ、または組を make(tuple) したもの (spsp/meta.SeedInfo など)。項目ごとに codes の
    1 文字: 'i' = int (None 可、4 バイト)、'b' = bool、'd' = float (None 不可)。キー・値の並びと型は元の dict と同じ。
    表せない大会 (int でない uid・型の違う値) はその大会だけ元の dict のまま持つ。totals=True なら大会ごとの値の合計を
    数えておく (_TourUidView.total())。add() で 1 大会ずつ足せる (同じ tid は足せない)。"""

    _NONE = -(1 << 31)

    def __init__(self, codes: str = 'i', make=None, totals: bool = False):
        self._at = {}            # tid → (最初の行, 終わりの行, 合計) か、表せない大会の dict。並びは足した順
        self._uid = array('q')   # 行の uid (元の dict の並び)
        self._suid = array('q')  # 大会ごとに uid の昇順
        self._perm = array('i')  # _suid の行 → 元の並びの行
        self._cols = tuple(array('b' if c == 'b' else c) for c in codes)
        self._codes = codes
        self._make = make
        self._totals = totals
        self.n_odd = 0

    @classmethod
    def compact(cls, dd, codes: str = 'i', make=None, totals: bool = False) -> 'TourUidTable':
        """codes = 値の項目ごとの型 (1 文字なら値そのもの、2 文字以上なら make(tuple))。"""
        self = cls(codes, make, totals)
        for tid, d in dd.items():
            self.add(tid, d)
        return self

    def add(self, tid, d) -> None:
        if tid in self._at:
            raise ValueError(f'TourUidTable: 同じ大会を 2 回足した ({tid})')
        lo = len(self._uid)
        if not self._append(d):
            for col in (self._uid, *self._cols):
                del col[lo:]
            self._at[tid] = d
            self.n_odd += 1
            return
        hi = len(self._uid)
        order = sorted(range(lo, hi), key=self._uid.__getitem__)
        self._suid.extend(self._uid[i] for i in order)
        self._perm.extend(order)
        tot = None
        if self._totals:
            c = self._cols[0]
            tot = sum(c[lo:hi])
        self._at[tid] = (lo, hi, tot)

    def _append(self, d) -> bool:
        """d の行を足す (列ごとにまとめて確かめてから足す)。表せない値があれば False (途中まで足した分は呼び出し側が消す)。"""
        if not isinstance(d, dict):
            return False
        if not d:
            return True
        keys = list(d)
        if set(map(type, keys)) != {int}:
            return False
        vals = list(d.values())
        width = len(self._cols)
        if width == 1:
            cols_vals = [vals]
        else:
            if any(not isinstance(v, tuple) or len(v) != width for v in vals):
                return False
            cols_vals = list(zip(*vals))
        nn = self._NONE
        for code, col, xs in zip(self._codes, self._cols, cols_vals):
            ts = set(map(type, xs))
            if code == 'd':
                if ts != {float}:
                    return False
                col.extend(xs)
            elif code == 'b':
                if ts != {bool}:
                    return False
                col.extend([1 if x else 0 for x in xs])
            else:
                if not ts <= {int, type(None)}:
                    return False
                ys = [nn if x is None else x for x in xs]
                if int in ts:
                    xi = [x for x in xs if x is not None]
                    if not (nn < min(xi) and max(xi) < (1 << 31)):
                        return False
                col.extend(ys)
        self._uid.extend(keys)
        if self._totals and width == 1 and None in vals:
            return False
        return True

    def __getstate__(self):
        d = self.__dict__.copy()
        d.pop('_value', None)   # _value_func が作った関数 (pickle できない。読んだ後にまた作る)
        return d

    def _value(self, r):
        # 行 r の値。項目の型 (codes) ごとに組み立てた関数を初めて引いたときに作って、インスタンスに置く (以後はそれが直接呼ばれる)
        f = self._value = self._value_func()
        return f(r)

    def _value_func(self):
        """_value と同じ値を返す関数 (1 件ずつ _one を呼ばずに、項目ごとの変換を 1 つの式にしたもの)。"""
        nn = self._NONE
        cols = self._cols
        if len(cols) == 1:
            c = cols[0]
            code = self._codes[0]
            if code == 'i':
                return lambda r: None if c[r] == nn else c[r]
            if code == 'b':
                return lambda r: c[r] != 0
            return c.__getitem__
        parts = []
        for j, code in enumerate(self._codes):
            if code == 'i':
                parts.append(f'(None if c{j}[r] == nn else c{j}[r])')
            elif code == 'b':
                parts.append(f'(c{j}[r] != 0)')
            else:
                parts.append(f'c{j}[r]')
        env = {f'c{j}': c for j, c in enumerate(cols)}
        env.update(nn=nn, make=self._make)
        return eval(f'lambda r: make(({", ".join(parts)},))', env)

    @staticmethod
    def _one(code, x, nn):
        if code == 'i':
            return None if x == nn else x
        if code == 'b':
            return x != 0
        return x

    def get(self, tid, default=None):
        at = self._at.get(tid)
        if at is None:
            return default
        return _TourUidView(self, *at) if type(at) is tuple else at

    def value(self, tid, uid, default=None):
        """self.get(tid, {}).get(uid, default) と同じ (1 大会分の見え方を作らない)。"""
        at = self._at.get(tid)
        if at is None:
            return default
        if type(at) is not tuple:
            return at.get(uid, default)
        lo, hi = at[0], at[1]
        if type(uid) is int:
            i = bisect_left(self._suid, uid, lo, hi)
            return self._value(self._perm[i]) if i < hi and self._suid[i] == uid else default
        return _TourUidView(self, *at).get(uid, default)

    def __getitem__(self, tid):
        at = self._at[tid]
        return _TourUidView(self, *at) if type(at) is tuple else at

    def __contains__(self, tid):
        return tid in self._at

    def __iter__(self):
        return iter(self._at)

    def __len__(self):
        return len(self._at)


class ScaledMap(Mapping):
    """{uid: 値 × factor} を元の表から引くたびに計算する見え方 (build の日ごとの tjpr_elo = tjpr_scores × 0.35。
    以前は同じ並びの表をもう 1 つ持っていた。北米で 61 日 × 20 万人)。値は以前と同じ式 (x * factor) で同じ float。"""
    __slots__ = ('_base', '_f')

    def __init__(self, base, factor):
        self._base = base; self._f = factor

    def __getitem__(self, uid):
        return self._base[uid] * self._f

    def get(self, uid, default=None):
        x = self._base.get(uid)
        return default if x is None else x * self._f

    def __contains__(self, uid):
        return uid in self._base

    def __iter__(self):
        return iter(self._base)

    def __len__(self):
        return len(self._base)

    def values(self):
        f = self._f
        return [x * f for x in self._base.values()]

    def items(self):
        f = self._f
        return [(u, x * f) for u, x in self._base.items()]

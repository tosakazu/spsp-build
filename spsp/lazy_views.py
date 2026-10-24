"""学習結果 (生の dict) を、ビルドが読む「直列化した形」として見せる読み取り専用の窓 (2026-10-17)。

以前は snap 段で直列化した dict (bt_per_match / bt_per_tour / bt_timeline の str キー・4 桁丸め版) を作り、
生の dict と両方を inter に入れて pickle → build 段で両方を読んでいた (北米で 約 3 GB の二重持ち)。
直列化した形は overlay (players / tournaments の変換) が 1 件ずつ引くだけなので、引かれたときに生の dict から
同じ値を作って返す。値・キーの並び・「無いキー」の判定は旧 _serialize_* (spsp/cli/build_full.py) と同じ:

- 1 段目のキーは str(生のキー)。引くキーが str(int) の正規形 (先頭の 0・符号 + ・空白なし) でなければ「無い」。
  生のキーがすべて int のときだけ使える (from_raw_or_none が確かめる。違えば None を返し、呼び出し側は旧方式で直列化する)。
- 値は引くたびに新しいリスト (旧方式でも呼び出し側は書き換えないが、書き換えても生の dict には響かない)。
- 並び (iter) は旧方式の dict の挿入順 = 生の dict を前から舐めて最初に出た順。
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from spsp.columnar import _KeyedLog

_PAIR_RE = re.compile(r'(-?[0-9]+)-(-?[0-9]+)')


def _int_key(s):
    """str(k) == s となる int k を返す (無ければ None)。旧方式の dict のキーは str(int) なので、それ以外は当たらない。"""
    if type(s) is not str:
        return None
    try:
        k = int(s)
    except ValueError:
        return None
    return k if str(k) == s else None


def _pair_key(s):
    """f'{w}-{l}' == s となる (w, l) を返す (無ければ None)。"""
    if type(s) is not str:
        return None
    m = _PAIR_RE.fullmatch(s)
    if m is None:
        return None
    w, l = int(m.group(1)), int(m.group(2))
    return (w, l) if f'{w}-{l}' == s else None


def _all_int(keys) -> bool:
    for k in keys:
        if type(k) is tuple:
            for x in k:
                if type(x) is not int:
                    return False
        elif type(k) is not int:
            return False
    return True


class _View(Mapping):
    """__contains__ / get を値を作らずに答える Mapping (値は __getitem__ で作る)。"""

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __repr__(self):
        return f'<{type(self).__name__} {len(self)} keys>'


# ─────────── bt_timeline: {uid_str: [[ts, mu, sigma, ord], ...]} (4 桁丸め) ───────────
class BtTimelineView(_View):
    def __init__(self, raw: dict):
        self._raw = raw

    def __getitem__(self, key):
        u = _int_key(key)
        entries = self._raw.get(u) if u is not None else None
        if entries is None:
            raise KeyError(key)
        return [[ts, round(mu, 4), round(sigma, 4), round(ord_v, 4)] for (ts, mu, sigma, ord_v) in entries]

    # get は __getitem__ と同じ値を KeyError を介さずに返す (無いキーが多く、例外を投げて受けると遅い)。以下の見え方も同じ
    def get(self, key, default=None):
        u = _int_key(key)
        entries = self._raw.get(u) if u is not None else None
        if entries is None:
            return default
        return [[ts, round(mu, 4), round(sigma, 4), round(ord_v, 4)] for (ts, mu, sigma, ord_v) in entries]

    def __contains__(self, key):
        u = _int_key(key)
        return u is not None and u in self._raw

    def __iter__(self):
        return (str(u) for u in self._raw)

    def __len__(self):
        return len(self._raw)


# ─────────── bt_per_tour: {uid_str: {eid_str: [bt_used, bt_d, bt_internal_d, after, peak_after]}} ───────────
def per_tour_fields(info):
    """bt_per_tour の値 (学習側の dict、または compact_inter_raw の tuple) → (bt_used, bt_internal_d, bt_d, after, peak_after)。
    dict に bt_internal_d が無ければ bt_d (旧 build._v4_adapt_bt_outputs の info.get('bt_internal_d', info['bt_d']) と同じ)。"""
    if type(info) is tuple:
        return info
    return (info['bt_used'], info.get('bt_internal_d', info['bt_d']), info['bt_d'], info['after'], info['peak_after'])


def _per_tour_value(info):
    if type(info) is tuple:
        bt_used, bt_internal_d, bt_d, after, peak_after = info
    else:
        bt_used, bt_internal_d, bt_d = info['bt_used'], info['bt_internal_d'], info['bt_d']
        after, peak_after = info['after'], info['peak_after']
    return [
        1 if bt_used else 0,
        round(bt_d, 4),
        round(bt_internal_d, 4),
        round(after, 4),
        round(peak_after, 4),
    ]


class _FirstKeyGroups:
    """生の表 (dict または spsp/columnar の表) を、キーの最初の成分を x0 に固定して残りで引く。
    columnar の表なら 2 回目に引いたときに x0 の行だけの dict を作る (1 回だけなら作らない: 1 選手 1 大会だけ
    引く呼び出しが多い)。"""
    __slots__ = ('_raw', '_x0', '_n', '_groups', '_hit')

    def __init__(self, raw, x0):
        self._raw = raw; self._x0 = x0; self._n = 0; self._groups = None
        self._hit = None   # has() で引いた (rest, 組の番号)。続けて get(rest) するときに引き直さない

    def _full(self, rest):
        return (self._x0, rest) if type(rest) is not tuple else (self._x0, *rest)

    def get(self, rest):
        raw = self._raw
        if not isinstance(raw, _KeyedLog):
            return raw.get(self._full(rest))
        if self._groups is None:
            self._n += 1
            if self._n < 2:
                h = self._hit
                if h is not None and h[0] == rest:
                    return None if h[1] < 0 else raw.value_of_group(h[1])
                return raw.get(self._full(rest))
            self._groups = raw.groups_with_first(self._x0)
        g = self._groups.get(rest)
        return None if g is None else raw.value_of_group(g)

    def has(self, rest):
        raw = self._raw
        if not isinstance(raw, _KeyedLog):
            return self._full(rest) in raw
        if self._groups is None:
            full = self._full(rest)
            g = raw._group(full) if type(full) is tuple else -1
            self._hit = (rest, g)
            return g >= 0
        return rest in self._groups


class _PerTourOfUid(_View):
    def __init__(self, raw: dict, uid: int):
        self._raw = raw; self._uid = uid; self._sub = _FirstKeyGroups(raw, uid)

    def __getitem__(self, key):
        e = _int_key(key)
        info = self._sub.get(e) if e is not None else None
        if info is None:
            raise KeyError(key)
        return _per_tour_value(info)

    def get(self, key, default=None):
        e = _int_key(key)
        info = self._sub.get(e) if e is not None else None
        return default if info is None else _per_tour_value(info)

    def __contains__(self, key):
        e = _int_key(key)
        return e is not None and self._sub.has(e)

    def __iter__(self):   # 使われていない (全件を舐めるので遅い)。Mapping として正しく振る舞うためだけ
        u = self._uid
        return (str(e) for (uu, e) in self._raw if uu == u)

    def __len__(self):   # 使われていない (全件を舐める)
        return sum(1 for _ in self)

    def __bool__(self):   # 1 段目にキーがある = 生の dict にその 1 件以上がある (旧方式でも空の dict は作られない)
        return True


class BtPerTourView(_View):
    def __init__(self, raw: dict):
        self._raw = raw
        self._uids = dict.fromkeys(u for (u, _e) in raw)   # 旧方式の 1 段目の並び (最初に出た順)

    def __getitem__(self, key):
        u = _int_key(key)
        if u is None or u not in self._uids:
            raise KeyError(key)
        return _PerTourOfUid(self._raw, u)

    def get(self, key, default=None):
        u = _int_key(key)
        return default if u is None or u not in self._uids else _PerTourOfUid(self._raw, u)

    def __contains__(self, key):
        u = _int_key(key)
        return u is not None and u in self._uids

    def __iter__(self):
        return (str(u) for u in self._uids)

    def __len__(self):
        return len(self._uids)


# ─────────── bt_per_match: {eid_str: {'wuid-luid': [[d_w, d_l, w_tracked, l_tracked], ...]}} ───────────
def per_match_occurrences(occs):
    """bt_per_match の値 → 試合 (occurrence) の並び。学習側の [[d_w, d_l, t_w, t_l], ...]、compact_inter_raw の
    (d_w, d_l, t_w, t_l) (1 試合) / ((…), (…)) (複数)、旧 flat 形式 [d_w, d_l, t_w, t_l] のどれでも。"""
    if occs and not isinstance(occs[0], (list, tuple)):
        return (occs,)
    return occs


def _per_match_value(occs):
    return [[round(o[0], 4), round(o[1], 4), int(o[2]), int(o[3])] for o in per_match_occurrences(occs)]


class _PerMatchOfEvent(_View):
    def __init__(self, raw: dict, eid: int):
        self._raw = raw; self._eid = eid; self._sub = _FirstKeyGroups(raw, eid)

    def __getitem__(self, key):
        p = _pair_key(key)
        occs = self._sub.get(p) if p is not None else None
        if occs is None:
            raise KeyError(key)
        return _per_match_value(occs)

    def get(self, key, default=None):
        p = _pair_key(key)
        occs = self._sub.get(p) if p is not None else None
        return default if occs is None else _per_match_value(occs)

    def __contains__(self, key):
        p = _pair_key(key)
        return p is not None and self._sub.has(p)

    def __iter__(self):   # 使われていない (全件を舐めるので遅い)
        e = self._eid
        return (f'{w}-{l}' for (ee, w, l) in self._raw if ee == e)

    def __len__(self):   # 使われていない (全件を舐める)
        return sum(1 for _ in self)

    def __bool__(self):   # 1 段目にキーがある = 生の dict にその 1 件以上がある (旧方式でも空の dict は作られない)
        return True


class BtPerMatchView(_View):
    def __init__(self, raw: dict):
        self._raw = raw
        self._eids = dict.fromkeys(k[0] for k in raw)
        self._get3 = getattr(raw, 'get3', None)   # columnar.MatchLog なら (eid, w, l) を tuple にせず引ける

    def __getitem__(self, key):
        e = _int_key(key)
        if e is None or e not in self._eids:
            raise KeyError(key)
        return _PerMatchOfEvent(self._raw, e)

    def get(self, key, default=None):
        e = _int_key(key)
        return default if e is None or e not in self._eids else _PerMatchOfEvent(self._raw, e)

    def pair(self, eid: int, w: int, l: int):
        """(self.get(str(eid)) or {}).get(f'{w}-{l}') と同じ (eid・w・l は int)。キーの文字列を作って読み直さない
        (選手ページの対戦一覧で 1 試合ずつ引く)。"""
        if eid not in self._eids:
            return None
        occs = self._get3(eid, w, l) if self._get3 is not None else self._raw.get((eid, w, l))
        return None if occs is None else _per_match_value(occs)

    def __contains__(self, key):
        e = _int_key(key)
        return e is not None and e in self._eids

    def __iter__(self):
        return (str(e) for e in self._eids)

    def __len__(self):
        return len(self._eids)


_PER_TOUR_KEYS = ('bt_used', 'bt_internal_d', 'bt_d', 'after', 'peak_after')


def compact_inter_raw(raw_per_tour: dict, raw_per_match: dict) -> None:
    """inter に入れる生の bt_per_tour / bt_per_match の値を小さい tuple にする (その場で。snap の最後、使い終わってから)。

    - bt_per_tour: {'bt_used', 'bt_internal_d', 'bt_d', 'after', 'peak_after'} の dict → その順の tuple
      (キーがちょうどこの 5 つでないものは dict のまま)。読む側は per_tour_fields
    - bt_per_match: [[d_w, d_l, t_w, t_l]] → (d_w, d_l, t_w, t_l)、2 試合以上は tuple の tuple。読む側は per_match_occurrences
    値 (float / bool のオブジェクト) は同じもの。北米で 280 万件 + 530 万件 (dict・list の分で 約 1 GB。2026-10-17)。
    学習状態のものは変えない (チェックポイントの形式は同じ。snap で学習状態はこの後使わない)。"""
    if type(raw_per_tour) is not dict or type(raw_per_match) is not dict:
        return   # 配列の表 (spsp/columnar.py) は最初から詰まっている
    keys = _PER_TOUR_KEYS
    for k, info in raw_per_tour.items():
        if type(info) is dict and len(info) == 5 and all(x in info for x in keys):
            raw_per_tour[k] = (info['bt_used'], info['bt_internal_d'], info['bt_d'], info['after'], info['peak_after'])
    for k, occs in raw_per_match.items():
        if type(occs) is list and occs and type(occs[0]) is list:
            raw_per_match[k] = tuple(occs[0]) if len(occs) == 1 else tuple(tuple(o) for o in occs)


def serialized_views(raw_timeline, raw_per_tour, raw_per_match):
    """(bt_timeline, bt_per_tour, bt_per_match) の窓。生のキーに int 以外が混ざっていれば None (旧方式で直列化すること)。"""
    if not (_all_int(raw_timeline) and _all_int(raw_per_tour) and _all_int(raw_per_match)):
        return None
    return BtTimelineView(raw_timeline), BtPerTourView(raw_per_tour), BtPerMatchView(raw_per_match)


class G2TimelineView:
    """run_build の g2_timeline ({uid: [(ts, mu, sigma), ...]}) を bt_timeline ({uid: [(ts, mu, sigma, ord), ...]}) から
    引くたびに作る (以前は全選手ぶんのコピーを先に作っていた)。output.build_player_detail は .get(uid, []) しか使わない。"""

    def __init__(self, bt_timeline: dict):
        self._tl = bt_timeline

    def get(self, uid, default=None):
        entries = self._tl.get(uid)
        if entries is None:
            return default
        return [(ts, mu, sigma) for (ts, mu, sigma, _o) in entries]

    def __contains__(self, uid):
        return uid in self._tl

    def __len__(self):
        return len(self._tl)


# ─────────── 旧方式 (全部を先に直列化する)。窓と同じ値になることは tests/meta/test_lazy_views.py が確かめる ───────────
def _serialize_bt_timeline(bt_timeline):
    """{uid_str: [[ts, mu, sigma, ord], ...]} (4 桁 round)"""
    return {
        str(uid): [[ts, round(mu, 4), round(sigma, 4), round(ord_v, 4)]
                   for (ts, mu, sigma, ord_v) in entries]
        for uid, entries in bt_timeline.items()
    }


def _serialize_bt_per_tour(bt_per_tour):
    """{uid_str: {eid_str: [bt_used, bt_d, bt_internal_d, after, peak_after]}}"""
    out = {}
    for (uid, eid), info in bt_per_tour.items():
        out.setdefault(str(uid), {})[str(eid)] = _per_tour_value(info)
    return out


def _serialize_bt_per_match(bt_per_match):
    """{eid_str: {'wuid-luid': [[d_w, d_l, w_tracked, l_tracked], ...]}} (試合進行順)"""
    out = {}
    for (eid, wuid, luid), occs in bt_per_match.items():
        out.setdefault(str(eid), {})[f'{wuid}-{luid}'] = _per_match_value(occs)
    return out


def overlay_inputs(inter: dict):
    """overlay が読む (bt_timeline, bt_per_tour, bt_per_match)。

    旧 inter (直列化した形を持っている) ならそれを、今の inter なら生の dict の窓を返す。生のキーに int 以外が
    混ざっていれば旧方式で直列化する (値はどれも同じ)。"""
    if 'bt_per_match' in inter:
        return inter['bt_timeline'], inter['bt_per_tour'], inter['bt_per_match']
    raw = (inter['_bt_timeline_raw'], inter['_bt_per_tour_raw'], inter['_bt_per_match_raw'])
    views = serialized_views(*raw)
    if views is not None:
        return views
    print("  [lazy_views] 生のキーに int 以外があるので直列化する (旧方式)", flush=True)
    return _serialize_bt_timeline(raw[0]), _serialize_bt_per_tour(raw[1]), _serialize_bt_per_match(raw[2])


class IntKeyView(_View):
    """str(int) キーの dict を int キーの dict として見せる ({int(k): v for k, v in d.items()} と同じ振る舞い、コピーしない)。

    overlay の peak180_at_day (61 日 × 全選手) を int キーにコピーしていた分 (北米で 約 1 GB) を省く。
    d のキーは str(int) の正規形であること (spsp/cli/build_full._serialize_peak180_at_day が str(uid) で作る)。"""

    def __init__(self, d: dict):
        self._d = d

    def __getitem__(self, key):
        if type(key) is not int:
            raise KeyError(key)
        return self._d[str(key)]

    def get(self, key, default=None):
        if type(key) is not int:
            return default
        return self._d.get(str(key), default)

    def __contains__(self, key):
        return type(key) is int and str(key) in self._d

    def __iter__(self):
        from spsp.common import uid_of
        return (uid_of(k) for k in self._d)

    def items(self):
        from spsp.common import uid_of
        return [(uid_of(k), v) for k, v in self._d.items()]

    def values(self):
        return self._d.values()

    def __len__(self):
        return len(self._d)

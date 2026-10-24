"""大会の試合の list を手放し、使うときに作り直す (2026-10-20)。

build は全大会の試合 (北米で 580 万件、Match 1 件 200 バイト) をずっと持っていた。_v4_adapt_bt_outputs (全試合を
一度に使う) の後は、試合を読むのは大会ごとの集計と大会ページの組み立てだけなので、t.matches を LazyMatches に
置き換え、読むたびに作り直す。直前に読んだ 1 大会分だけ取っておく。

作り直す元は、手放すときに詰めておいた大会ごとの圧縮した塊 (Match の項目の tuple の list を pickle して zlib、
1 試合 約 14 バイト。北米で 80 MB、Match のままだと 約 1.1 GB)。作り直しは 1 大会 0.45 ms (日本)。
塊は手放す前の list そのものから作るので並び・値・型は同じ (オブジェクトは別)。

既定で有効 (SPSP_LAZY_MATCHES=0 で以前どおり試合を持ち続ける。手放すのは spsp/cli/build_full と spsp/build)。SPSP_LAZY_MATCHES_CHECK=1 なら手放す前に
全大会の塊を作り直して元と同じ (値・型) か確かめる (遅い。試作の確認用)。

試合が少ない地域 (全部で KEEP_BELOW 件未満。日本は 約 47 万試合 ≒ 100 MB) では、build の初めに塊を 1 回ずつ作り直して
以後は持ち続ける (keep_if_small)。build は全大会の試合を 3 回回す (BT の集計・大会ごとの集計・大会ページ) ので、毎回作り直すと
日本で 1 万 3 千回 (約 6 秒) かかっていた。北米 (580 万試合) は手放したまま。
"""
from __future__ import annotations

import dataclasses
import os
import pickle
import zlib
from collections.abc import Sequence

ENABLED = os.environ.get('SPSP_LAZY_MATCHES', '1') != '0'
PROFILE = os.environ.get('SPSP_LAZY_MATCHES_PROFILE') == '1'
CHECK = os.environ.get('SPSP_LAZY_MATCHES_CHECK') == '1'

# 試合の総数がこれ未満なら build では持ち続ける (keep_if_small)
KEEP_BELOW = int(os.environ.get('SPSP_LAZY_MATCHES_KEEP_BELOW', '2000000'))
_kept = [False]   # keep_if_small で持ち続けることにした (以後 release は何もしない)

_last: list = [None, None]   # (LazyMatches の id, 作り直した list)
stats = {'reloads': 0}
_FIELDS: tuple = ()
# 塊を作り直したときに呼ぶもの (lm, list)。大会ごとの小さな控えを、どこかの集計が試合を読んだついでに作る (spsp/learn)
on_unpack: list = []


def _fields():
    global _FIELDS
    if not _FIELDS:
        from spsp.data_loader import Match
        _FIELDS = tuple(f.name for f in dataclasses.fields(Match))
    return _FIELDS


def _pack(ms) -> bytes:
    fs = _fields()
    return zlib.compress(pickle.dumps([tuple([getattr(m, k) for k in fs]) for m in ms], 5), 1)


def _unpack(blob: bytes) -> list:
    from spsp.data_loader import Match
    stats['reloads'] += 1
    if PROFILE:   # 計測: 読み直しを呼んだ場所ごとの回数 (プロセスの終わりに出す)
        import sys
        f = sys._getframe(3)
        key = f'{f.f_code.co_filename.rsplit("/", 1)[-1]}:{f.f_lineno} {f.f_code.co_name}'
        stats.setdefault('by_caller', {})[key] = stats.get('by_caller', {}).get(key, 0) + 1
    return [Match(*r) for r in pickle.loads(zlib.decompress(blob))]


def raw_rows(lm) -> list:
    """塊の中身 (Match の項目の tuple の list、項目は dataclasses.fields(Match) の並び) を Match を作らずに返す。
    台帳のダイジェスト (spsp/learn_state._matches_repr) は値の並びだけを見るので、全大会ぶんの Match の作り直しを
    省く。on_unpack は呼ばない (呼ぶ先は読んだついでに作る控えで、要るときに自分で作り直す)。"""
    rows = pickle.loads(zlib.decompress(lm._blob))
    if len(rows) != lm._n:
        raise RuntimeError(f'LazyMatches: 作り直した試合の数が違う ({len(rows)} != {lm._n})')
    return rows


class LazyMatches(Sequence):
    __slots__ = ('_blob', '_n')

    def __init__(self, blob: bytes, n: int):
        self._blob = blob
        self._n = n

    def _list(self) -> list:
        if _last[0] == id(self):
            return _last[1]
        lst = _unpack(self._blob)
        if len(lst) != self._n:
            raise RuntimeError(f'LazyMatches: 作り直した試合の数が違う ({len(lst)} != {self._n})')
        _last[0] = id(self); _last[1] = lst
        for f in on_unpack:
            f(self, lst)
        return lst

    def __len__(self):
        return self._n

    def __bool__(self):
        return self._n > 0

    def __getitem__(self, i):
        return self._list()[i]

    def __iter__(self):
        return iter(self._list()) if self._n else iter(())

    def __repr__(self):
        return f'<LazyMatches {self._n}>'


def keep_if_small(tournaments) -> bool:
    """試合の総数が KEEP_BELOW 未満なら、各大会の t.matches (LazyMatches) を作り直した list に戻して持ち続ける
    (以後の release は何もしない)。戻り値: 戻したか。"""
    if sum(len(t.matches) for t in tournaments) >= KEEP_BELOW:
        return False
    for t in tournaments:
        if isinstance(t.matches, LazyMatches):
            t.matches = t.matches._list()
    _last[0] = _last[1] = None
    _kept[0] = True
    return True


def release(tournaments) -> int:
    """各大会の t.matches (list) を LazyMatches にする。戻り値: 手放した試合の数 (keep_if_small で持ち続けることにしたら 0)。"""
    if _kept[0]:
        return 0
    n = 0
    for t in tournaments:
        ms = t.matches
        if isinstance(ms, LazyMatches):
            continue
        blob = _pack(ms)
        if CHECK:
            again = _unpack(blob)
            if again != ms or any(type(getattr(a, k)) is not type(getattr(b, k)) for a, b in zip(again, ms) for k in _FIELDS):
                raise RuntimeError(f'LazyMatches: 作り直した試合が元と違う ({t.event_path})')
        t.matches = LazyMatches(blob, len(ms))
        n += len(ms)
    _last[0] = _last[1] = None
    return n


def _report():
    if stats['reloads']:
        print(f"[lazy matches] このプロセスの読み直し {stats['reloads']} 回", flush=True)
        for k, v in sorted((stats.get('by_caller') or {}).items(), key=lambda kv: -kv[1])[:20]:
            print(f"    {v:8d}  {k}", flush=True)


if ENABLED and PROFILE:
    import atexit
    atexit.register(_report)

"""spsp/tour_cache.py — 大会ファイル (tournaments/<eid>.json) は一度書いたら、大会そのもののデータが変わらない限り書き直さない
(2026-10-22、build の差分化)。

大会ページに出すのは大会当時の値 (当時の順位とその変化・当時の点・直対の増減・表示名) なので、一度書いた後に
変わるのは表示名や今のレベルで引き直す点など、当時のものとして固めてよいものだけ。SPSP_TOUR_CACHE=1 のとき、

  - 大会ごとに、大会そのもののデータ (学習の台帳と同じ tournament_digest = 日時・順位・試合・DQ・大会の情報 + シード) と
    DOC_VERSION・学習の設定・地域から鍵 (blake2b) を作る。参加者のほかの大会の成績・名前・今の順位は入れない
  - 前回の一覧 (<出力>/_tourcache.json.gz、eid → [鍵, 大きさ, 更新時刻 ns]) と鍵が同じで、ファイルも前回のまま
    (大きさ・更新時刻が一覧のとおり) なら作らずにそのまま残す
  - それ以外 (新しい大会・結果が変わった大会 (再開待ちなど)・前回のファイルが無い) は作って書く。最後に、今回出なかった
    大会 (消えた大会) のファイルを消して一覧を書き直す

全部作り直すとき:
  - 大会ファイルの形や中身の作り方を変えたら DOC_VERSION を上げる (上げなければ前回のファイルのまま残る)
  - 学習の設定 (Settings.state_repr) や地域が変わったとき (来季の切り替えなど) は鍵が変わるので全部作る
  - SPSP_TOUR_CACHE_FULL=1 で一覧を使わずに全部作る
前回の出力が別の場所にあるとき (GitHub Actions: 毎回新しい出力先 + actions/cache で戻した前回分) は
SPSP_TOUR_CACHE_PREV=<前回の出力 (tournaments/ と一覧がある所)> を渡す。残す大会は前回のファイルを写す (同じファイル
システムならハードリンク)。キャッシュから戻すと更新時刻が変わりうるので、大きさが同じで更新時刻が違うときは中身の
ハッシュ (一覧に持つ) で確かめる
  - 一覧は読んだらすぐ消し、全部書き終えてから置く。途中で落ちた回の次は一覧が無いので、フォルダを消して全部作る
既定 (SPSP_TOUR_CACHE 無し) は従来どおりフォルダを消して全部作る。
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import pickle
import shutil
import types
from collections.abc import Mapping
from pathlib import Path

FORMAT = 'spsp-tour-cache/4'
MANIFEST = '_tourcache.json.gz'
# 大会ファイルの形・中身の作り方を変えたら上げる (前回までに書いた大会ファイルを全部作り直す)
# 2: ポイントのベース (tjpr_raw) を当時のレベルで・その大会で使ったキャラ (chars) を足す (2026-10-23)
DOC_VERSION = 2


def enabled() -> bool:
    return os.environ.get('SPSP_TOUR_CACHE', '0') == '1'


def epoch() -> str:
    """全大会に共通の鍵の材料: DOC_VERSION・地域・学習の設定。"""
    from spsp.settings import Settings
    return repr((DOC_VERSION, os.environ.get('SPSP_REGION', ''), Settings.from_env().state_repr()))


class _KeyPickler(pickle.Pickler):
    """dict・list・tuple・数・文字列はそのまま (C の速い経路。reducer_override は呼ばれない)。それ以外は
    tuple の子 (SeedInfo など) → tuple、to_dict を持つもの・Mapping (spsp/columnar・lazy_views の見え方) → dict、
    numpy の値・配列 → numpy の pickle (型も入る)。知らない型は TypeError で止める
    (そのまま pickle すると中の大きな表まで入ったり、中身でなく作られ方で鍵が変わったりする)。"""

    def reducer_override(self, obj):
        if isinstance(obj, (type, types.BuiltinFunctionType, types.FunctionType)):
            return NotImplemented   # 名前で入る (numpy の pickle が使う関数・型など)
        if isinstance(obj, tuple):
            return tuple, (tuple(obj),)
        f = getattr(obj, 'to_dict', None)
        if f is not None and not isinstance(obj, type):
            return dict, (f(),)
        if isinstance(obj, Mapping):
            return dict, (dict(obj.items()),)
        if isinstance(obj, list):
            return list, (list(obj),)
        if isinstance(obj, dict):
            return dict, (dict(obj),)
        if type(obj).__module__.split('.')[0] == 'numpy':
            return NotImplemented
        raise TypeError(f'tour-cache: 鍵にできない型 {type(obj).__module__}.{type(obj).__qualname__}')


def key_of(parts) -> str:
    """鍵の材料 → 鍵。同じ値でも pickle の形が違えば (共有の有無など) 別の鍵になるが、それは作り直すだけ。"""
    buf = io.BytesIO()
    _KeyPickler(buf, protocol=5).dump(parts)
    return hashlib.blake2b(buf.getbuffer(), digest_size=16).hexdigest()


def tour_key(epoch_: str, t, meta_t: dict, seeds, digest: str | None = None, extra=None) -> str:
    """大会 1 つの鍵: 大会そのもののデータとシード。digest = 学習の台帳の tournament_digest (日時・順位・試合・DQ と
    学習の時点の大会の情報。読み込み時に今のデータと照合済み)。無ければここで作る (試合を読むので遅い)。build の
    大会の情報 (meta_t、build で足す is_random_seed なども入る) は別に入れる。"""
    if digest is None:
        from spsp.learn_state import tournament_digest
        digest = tournament_digest(t, meta_t)
    return key_of((epoch_, digest, sorted((k, repr(v)) for k, v in meta_t.items()),
                   sorted(dict(seeds).items()) if seeds else None, extra))


def _content_hash(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=16).hexdigest()


class TourDocCache:
    def __init__(self, dir_: Path):
        self.dir = Path(dir_)
        self.manifest = self.dir.parent / MANIFEST
        prev_root = os.environ.get('SPSP_TOUR_CACHE_PREV')
        # 前回の出力 (既定 = 今回と同じ所)
        self.prev_dir = Path(prev_root) / self.dir.name if prev_root else self.dir
        self.prev_manifest = Path(prev_root) / MANIFEST if prev_root else self.manifest
        self.same_place = self.prev_dir.resolve() == self.dir.resolve()
        self.epoch = epoch()
        self.prev: dict = {}
        self.new: dict = {}
        self.n_built = self.n_kept = 0
        self.t_key = 0.0   # 鍵を作るのにかかった秒数 (output が足す)
        reason = None
        m = None if os.environ.get('SPSP_TOUR_CACHE_FULL') == '1' else self._load()
        # 一覧は読んだらすぐ消す (途中で落ちたら次の回は全部作る)。前回の出力が別の所なら、そちらは触らない
        try:
            self.manifest.unlink()
        except FileNotFoundError:
            pass
        if os.environ.get('SPSP_TOUR_CACHE_FULL') == '1':
            reason = 'SPSP_TOUR_CACHE_FULL=1'
        elif m is None:
            reason = '一覧なし'
        elif not self.prev_dir.is_dir():
            reason = '前回のフォルダなし'
        else:
            self.prev = m['files']
        if reason is not None or not self.same_place:
            # 別の所から写すときは、今回の出力先は空から始める
            if self.dir.exists():
                shutil.rmtree(self.dir)
        if reason is not None:
            print(f'  tour-cache: 全部作る ({reason})', flush=True)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _load(self):
        try:
            with gzip.open(self.prev_manifest, 'rt', encoding='utf-8') as f:
                m = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(m, dict) or m.get('format') != FORMAT or not isinstance(m.get('files'), dict):
            return None
        return m

    def keep(self, eid, key: str) -> bool:
        """前回のファイルをそのまま残せるなら True (一覧に載せる)。False なら作って write する。"""
        old = self.prev.get(str(eid))
        if old is None or old[0] != key:
            return False
        src = self.prev_dir / f'{eid}.json'
        try:
            st = os.stat(src)
        except OSError:
            return False
        if st.st_size != old[1]:
            return False
        if st.st_mtime_ns != old[2]:
            # 更新時刻が違う (キャッシュから戻した・手で触った): 中身のハッシュで確かめる
            try:
                if _content_hash(src.read_bytes()) != old[3]:
                    return False
            except OSError:
                return False
        if not self.same_place:
            dst = self.dir / f'{eid}.json'
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)
            st = os.stat(dst)
        self.new[str(eid)] = [key, st.st_size, st.st_mtime_ns, old[3]]
        self.n_kept += 1
        return True

    def write(self, eid, key: str, data: bytes, evict=None) -> None:
        p = self.dir / f'{eid}.json'
        p.write_bytes(data)
        st = os.stat(p)
        self.new[str(eid)] = [key, st.st_size, st.st_mtime_ns, _content_hash(data)]
        self.n_built += 1
        if evict is not None:
            evict(p)

    def finish(self) -> None:
        """今回出なかったファイルを消し、一覧を置く。"""
        n_removed = 0
        if self.prev and self.same_place:
            with os.scandir(self.dir) as it:
                for e in it:
                    if e.name.endswith('.json') and e.name[:-5] in self.new:
                        continue
                    if e.is_dir(follow_symlinks=False):
                        shutil.rmtree(e.path)
                    else:
                        os.unlink(e.path)
                    n_removed += 1
        if not self.same_place:
            n_removed = len(set(self.prev) - set(self.new))   # 前回あって今回出なかった大会 (写さなかった)
        tmp = self.manifest.with_name(self.manifest.name + '.tmp')
        with gzip.open(tmp, 'wt', encoding='utf-8', compresslevel=1) as f:
            json.dump({'format': FORMAT, 'files': self.new}, f, separators=(',', ':'))
        os.replace(tmp, self.manifest)
        print(f'  tour-cache: 作った {self.n_built} / 前回のまま {self.n_kept} / 消した {n_removed} '
              f'(鍵 {self.t_key:.1f} 秒)', flush=True)

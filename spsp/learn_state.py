"""直対評価 (Glicko2) + per-tour レベル判定の学習状態。日付でチェックポイントを取り、そこから再開する。

学習 (v4/learner.g2_learn_advance) は大会を時系列に 1 つずつ処理する逐次計算で、
状態 = (全選手の Glicko2 状態, peak 履歴, レベル, 各大会開始時点のレート, タイムライン, per-tour の
増分スコア, 大会ページ用アンカー, ...)。これを丸ごと pickle して保存し、次回は続きから処理する。

再開が「最初から全部やり直した結果」と bit 単位で一致するための条件:
  1. 状態に eval_date 依存の値を入れない。per-tour スコアの減衰基準日は固定 (PT_DECAY_REF)。
     スナップショット (SNAPSHOT_DAYS 時点のカスケード) は学習の途中で計算せず、学習後に状態から復元する
     (v4/learner.g2_snapshots)。
  2. チェックポイント以前の大会データが変わっていないこと。処理した大会ごとに (event_path, timestamp,
     内容のハッシュ) を台帳 (ledger) に残し、再開時に現在のデータと突き合わせる。違えば StaleCheckpoint
     (終了コード EXIT_STALE) — 呼び出し側は最初から学習し直す。
  3. Settings が同じであること (settings_repr を保存して照合)。海外タグ集合 (overseas.json) も同様。

チェックポイントの粒度: 「YYYY-MM-DD まで (その日の 23:59:59 を含む)」。cutoff_ts_for_date() 参照。
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import operator
import os
import pickle
from collections import defaultdict, deque
from pathlib import Path

from spsp.columnar import AnchorTable, BtTimeline, MatchLog, PerTourLog, RatingHist
from spsp.rolling import RollingState


def overseas_digest() -> str:
    """学習に使う海外タグ集合 (v4.common.OVERSEAS_UIDS) のダイジェスト。
    集合はレベル判定 (JP-only counter) に入るので学習の軌跡を変える。overseas.json は毎晩再生成されるため、
    チェックポイント保存時と違えば再開せず (StaleCheckpoint) 学習し直す。"""
    from spsp.common import OVERSEAS_UIDS
    h = hashlib.blake2b(digest_size=8)
    for u in sorted(OVERSEAS_UIDS):
        h.update(str(u).encode()); h.update(b',')
    return f'{len(OVERSEAS_UIDS)}:{h.hexdigest()}'

STATE_VERSION = 7   # 7: 下位クラスの点を本戦の大会後の順位に入れる・集計対象外の大会にも当時の順位 (2026-10-22、過去の大会も引き直すため全部学習し直す)   # 6: peak_track (過去最高順位の追跡) を追加 (2026-09-13)   # 5: 海外タグ集合そのもの (overseas_uids) を持つ = 処理済み大会に出ていない uid の増減では再開を拒否しない   # 4: 海外タグ集合のダイジェスト   # 3: rolling delta (v4/rolling.RollingState) を状態に含める   # 2: 大会のキーをデータルートからの相対パスに (絶対パスだと置き場所が変わると使えなかった)
EXIT_STALE = 3   # build_v4_full.py がチェックポイント不一致で返す終了コード


class StaleCheckpoint(Exception):
    """チェックポイント以前のデータ / 設定が今と違う (= 再開すると全学習と一致しない)。"""


from spsp.paths import rel_event_path  # noqa: E402,F401  (台帳キー。paths.py に置いて data_loader からも使う)

def cutoff_ts_for_date(d) -> int:
    """「d の日まで (23:59:59 含む) 学習した」を表す排他的境界 timestamp。"""
    if isinstance(d, str):
        d = dt.date.fromisoformat(d)
    if isinstance(d, dt.datetime):
        d = d.date()
    return int(dt.datetime(d.year, d.month, d.day, 23, 59, 59).timestamp()) + 1


_MATCH_FIELDS: dict[type, tuple[str, ...]] = {}


def _match_values(m) -> tuple:
    """dataclasses.astuple(m) と同じ値の並び。Match のフィールドは全部スカラ (int / str / bool / None) なので、
    astuple の再帰的な deepcopy は同じ値を返すだけ。フィールドを直接読んで同じ tuple を作る (大会ごとの
    ダイジェスト計算が 5 倍ほど速くなる。2026-09-28)。スカラ以外のフィールドが入ったら astuple に戻す。"""
    names = _MATCH_FIELDS.get(type(m))
    if names is None:
        fs = dataclasses.fields(m)
        names = tuple(f.name for f in fs)
        scalar = all(isinstance(getattr(m, n), (int, float, str, bool, type(None))) for n in names)
        _MATCH_FIELDS[type(m)] = names if scalar else ()
    if not names:
        return dataclasses.astuple(m)
    return tuple(getattr(m, n) for n in names)


def _norm_path(v):   # Match.event_path 等の絶対パスを相対に
    return rel_event_path(v) if isinstance(v, str) and '/events/' in v else v


_MATCH_GET: dict = {}   # 型 → (全フィールドの attrgetter (2 つ以上のときだけ), event_path の位置 or -1)


def _matches_repr(matches) -> str:
    """repr(tuple(tuple(_norm_path(x) for x in _match_values(m)) for m in matches)) と同じ文字列 (速い版、2026-10-17)。

    再開のたびに処理済みの全大会 (北米 15 万) の台帳を照合する (check_ledger) ので、ここが毎回数分かかっていた。
    試合ごとに、値を attrgetter でまとめて読み、event_path だけ正規化 (大会の中で同じ文字列なので 1 回だけ計算) して
    repr する。それ以外の値に '/events/' を含む文字列があるとき (正規化が要る) は元の式で作り直す。"""
    rows = []
    memo: dict = {}
    raw = _lazy_raw(matches)
    if raw is not None:
        return _rows_repr(raw, memo)
    for m in matches:
        ent = _MATCH_GET.get(type(m))
        if ent is None:
            _match_values(m)                       # フィールド名と「全部スカラか」を決める
            names = _MATCH_FIELDS[type(m)]
            ent = (operator.attrgetter(*names) if len(names) >= 2 else None,
                   names.index('event_path') if 'event_path' in names else -1)
            _MATCH_GET[type(m)] = ent
        get, ip = ent
        if get is None:
            rows.append(repr(tuple(_norm_path(x) for x in _match_values(m))))
            continue
        vals = get(m)
        row = vals
        if ip >= 0:
            ev = vals[ip]
            if ev.__class__ is str:
                nv = memo.get(ev)
                if nv is None:
                    nv = memo[ev] = _norm_path(ev)
                row = vals[:ip] + (nv,) + vals[ip + 1:]
        r = repr(row)
        if '/events/' in r:
            r = repr(tuple(_norm_path(x) for x in vals))
        rows.append(r)
    if not rows:
        return '()'
    if len(rows) == 1:
        return '(' + rows[0] + ',)'
    return '(' + ', '.join(rows) + ')'


def _lazy_raw(matches):
    """matches が手放した試合 (spsp/lazy_matches) なら、塊の値の tuple の list (Match を作らない)。使えなければ None。
    Match の全項目がスカラで項目の並びが塊と同じなら、attrgetter(全項目)(m) は塊の tuple と同じ値の並びなので、
    _rows_repr は通常の経路と同じ文字列を作る (北米の check_ledger で 15 万大会・580 万試合の Match を作らずに済む)。"""
    from spsp import lazy_matches as _lm
    if not isinstance(matches, _lm.LazyMatches) or not len(matches) or _lm._last[0] == id(matches):
        return None
    from spsp.data_loader import Match
    if Match not in _MATCH_GET or _MATCH_GET[Match][0] is None or _MATCH_FIELDS.get(Match) != _lm._fields():
        return None                     # 項目の判定がまだ (最初の 1 大会は通常の経路で決める) か、スカラ以外がある
    return _lm.raw_rows(matches)


def _rows_repr(raw, memo) -> str:
    """_matches_repr の通常の経路の、値の tuple の list 版 (同じ文字列)。"""
    from spsp.data_loader import Match
    ip = _MATCH_GET[Match][1]
    rows = []
    for vals in raw:
        row = vals
        if ip >= 0:
            ev = vals[ip]
            if ev.__class__ is str:
                nv = memo.get(ev)
                if nv is None:
                    nv = memo[ev] = _norm_path(ev)
                row = vals[:ip] + (nv,) + vals[ip + 1:]
        r = repr(row)
        if '/events/' in r:
            r = repr(tuple(_norm_path(x) for x in vals))
        rows.append(r)
    if len(rows) == 1:
        return '(' + rows[0] + ',)'
    return '(' + ', '.join(rows) + ')'


def tournament_digest(t, meta) -> str:
    """大会 1 つの内容 (standings / matches / 学習が見る meta) のハッシュ。データ変更検知用。"""
    parts = [
        repr((t.timestamp, rel_event_path(t.event_path), t.event_id, t.end_timestamp)),
        repr(tuple(t.standings)),
        _matches_repr(t.matches),
        repr(tuple(sorted(getattr(t, 'dq_player_ids', ()) or ()))),
        repr(tuple(sorted(getattr(t, 'dq_player_ids_main', ()) or ()))),
        repr(tuple(sorted((k, repr(v)) for k, v in (meta or {}).items()))),
    ]
    h = hashlib.blake2b(digest_size=16)
    for p in parts:
        h.update(p.encode('utf-8')); h.update(b'\0')
    return h.hexdigest()


class G2LearnState:
    """learn_bt_lv_shared_g2_pure の全ループ状態。id(t) は持たず event_path / uid / event_id / ts でキーする。"""

    def __init__(self, capture_timeline: bool, capture_per_tour: bool, capture_at_time: bool,
                 settings_repr: str):
        self.version = STATE_VERSION
        self.settings_repr = settings_repr
        self.overseas_digest = overseas_digest()
        from spsp.common import OVERSEAS_UIDS as _OV
        self.overseas_uids = tuple(sorted(_OV))   # 保存時の海外タグ集合 (check_overseas が差分を見る)
        self.capture_timeline = capture_timeline
        self.capture_per_tour = capture_per_tour
        self.capture_at_time = capture_at_time
        self.cursor_ts = 0            # timestamp < cursor_ts の大会は処理済み
        self.ledger: list[tuple[str, int, str]] = []   # (rel_event_path, timestamp, digest) 処理順
        # ── Glicko2 / peak ──
        self.ratings: dict = {}
        self.tour_count = defaultdict(int)
        self.last_seen_ts: dict = {}
        self.peak180 = None           # defaultdict(_G2Peak)。v4/learner 側で初期化 (クラスがそちらにある)
        self.pw_store = None          # 区分線形減衰 (pw) 実験用。チェックポイント不可
        self.rating_hist = RatingHist()   # uid -> [(ts, rating)] 全期間 (スナップショット復元用。配列の表 spsp/columnar.py)
        self.first_part_ts: dict = {}          # uid -> 初めて serious 大会に出た ts
        self.part_order: list = []             # 上の挿入順 (peak dict の並び = 出力 JSON の並びを再現する)
        self.player_lv: dict = {}
        self.bt_at_t_by_path: dict = {}        # rel_event_path -> {uid: ord}  (各大会開始時点のレート)
        self.bt_timeline = BtTimeline() if capture_timeline else None   # uid -> [(ts, mu, sigma, ord)]
        # 選手×大会・試合ごとの記録。数値の配列の表 (spsp/columnar.py。読む側には元の dict と同じ形に見える)
        self.bt_per_tour = PerTourLog() if capture_per_tour else None
        self.bt_per_match = MatchLog() if capture_per_tour else None
        # ── per-tour レベル判定の増分構造 ──
        self.pt_scores_entries = None   # {lv: defaultdict(list)} (LV_DEFS 依存なので学習側で初期化)
        self.pt_scores = None
        self.pt_mcounts = defaultdict(int)
        self.pt_mwindow = deque()
        self.pt_meas_set: set = set()
        self.pt_peak_cur: dict = {}
        self.pt_peak_exp = deque()
        self.pt_last_parts: set = set()
        self.pt_at_time_by_path: dict = {} if capture_at_time else None   # event_path -> {uid: [lv, ens, tjr, btr, ...]}
        self.rolling = RollingState()          # 大会ごとの順位変動 (build が使う。rel_event_path キー)
        from spsp.peak_track import PeakTracker
        self.peak_track = PeakTracker()        # 過去最高順位 (学習の軌跡上の最小値。build の peak_ranks / 実績が使う)
        self.gc_stats = {'sets': 0, 'gc_sets': 0, 'gc_games': 0, 'no_score': 0, 'max_set': 0, 'max_gc': 0}

    def upgrade_logs(self) -> None:
        """bt_per_tour / bt_per_match が旧形式 (dict。2026-10-18 より前のチェックポイント) なら配列の表にする。
        並び・値は同じ (spsp/columnar.py) なので、そのまま続きを学習してよい。"""
        if type(self.bt_per_tour) is dict:
            self.bt_per_tour = PerTourLog.from_dict(self.bt_per_tour)
        if type(self.bt_per_match) is dict:
            self.bt_per_match = MatchLog.from_dict(self.bt_per_match)
        # 選手ごとのレート履歴 (rating_hist / bt_timeline)。空の list がある (表せない) ときは dict のまま
        for name, cls in (('rating_hist', RatingHist), ('bt_timeline', BtTimeline)):
            v = getattr(self, name, None)
            if isinstance(v, dict):
                log = cls.from_dict(v)
                if log is not None:
                    setattr(self, name, log)
        # 大会ごとのアンカー (pt_at_time) は配列に詰める (spsp/columnar.AnchorTable)
        pat = getattr(self, 'pt_at_time_by_path', None)
        if pat:
            for k, v in pat.items():
                if type(v) is dict:
                    pat[k] = AnchorTable(v)
        # 順位変動 (rolling) は raw_at だけにする (spsp/rolling.RollingState.step と同じ形)
        roll = getattr(self, 'rolling', None)
        if roll is not None and roll.deltas_by_path:
            first = next((d for d in roll.deltas_by_path.values() if d), {})
            if any(type(e) is dict for e in first.values()):
                roll.deltas_by_path = {k: {u: (e if type(e) is float else e['raw_at']) for u, e in d.items()}
                                       for k, d in roll.deltas_by_path.items()}

    # ── id(t) キーの「見え方」(既存の precompute_banzuke / build 側が id(t) でアクセスするため) ──
    def bt_at_t_view(self, prior) -> dict:
        return {id(t): self.bt_at_t_by_path[rel_event_path(t.event_path)] for t in prior
                if rel_event_path(t.event_path) in self.bt_at_t_by_path}

    def rolling_view(self, prior) -> dict:
        d = self.rolling.deltas_by_path
        return {id(t): d[rel_event_path(t.event_path)] for t in prior
                if rel_event_path(t.event_path) in d}

    def pt_at_time_view(self, prior):
        if self.pt_at_time_by_path is None:
            return None
        return {id(t): self.pt_at_time_by_path[rel_event_path(t.event_path)] for t in prior
                if rel_event_path(t.event_path) in self.pt_at_time_by_path}

    @property
    def cursor_date(self) -> str:
        if not self.cursor_ts:
            return '(none)'
        return dt.datetime.fromtimestamp(self.cursor_ts - 1).date().isoformat()

    def describe(self) -> str:
        return (f"cursor={self.cursor_date} ({len(self.ledger)} 大会処理済), players={len(self.ratings)}, "
                f"settings={self.settings_repr[:60]}...")


def check_ledger(state: G2LearnState, prior, t_meta_by_id) -> None:
    """cursor_ts より前の大会 (今のデータ) が台帳と完全一致するか。違えば StaleCheckpoint。"""
    cur = [(rel_event_path(t.event_path), int(t.timestamp), tournament_digest(t, t_meta_by_id[id(t)]))
           for t in prior if t.timestamp < state.cursor_ts]
    if cur == state.ledger:
        return
    old_by_path = {p: (ts, d) for p, ts, d in state.ledger}
    new_by_path = {p: (ts, d) for p, ts, d in cur}
    added = [p for p in new_by_path if p not in old_by_path]
    removed = [p for p in old_by_path if p not in new_by_path]
    changed = [p for p in new_by_path if p in old_by_path and old_by_path[p] != new_by_path[p]]
    reordered = (not added and not removed and not changed)
    msg = (f"チェックポイント ({state.cursor_date} まで, {len(state.ledger)} 大会) 以前のデータが今と違う: "
           f"追加 {len(added)} / 削除 {len(removed)} / 内容変更 {len(changed)}"
           + (" / 並び順のみ変更" if reordered else ""))
    ex = [f"  追加: {p}" for p in added[:3]] + [f"  削除: {p}" for p in removed[:3]] + [f"  変更: {p}" for p in changed[:3]]
    raise StaleCheckpoint(msg + ("\n" + "\n".join(ex) if ex else ""))


def save_state(state: G2LearnState, path) -> None:
    """tmp + fsync + rename で原子的に保存。"""
    if state.pw_store is not None:
        raise ValueError("peak_decay_mode=pw の学習状態はチェックポイントに対応していない")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'wb') as f:
        _dump_state(state, f)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


# 学習状態のファイル形式 (2026-10-17 から): 先頭に {'format': STATE_FILE_FORMAT, 'keys': [...]}、続けて属性の値を
# 1 つずつ別の pickle で。状態 (北米で 約 10 GB) を 1 回で pickle すると memo (書いたオブジェクトの表) が 1〜2 GB 余分に
# 要り、学習のピークの一部だった。属性をまたいで共有されているオブジェクトは無い (読んだ状態で確認: 短い str 定数 1 つだけ)
# ので、読み直した状態は 1 回で pickle したものと同じ。旧形式 (状態そのものの pickle) も load_state がそのまま読む。
# 注意: この形式のファイルは 2026-10-17 より前のコードでは読めない (形式違いとして最初から学習し直しになる)。
STATE_FILE_FORMAT = 'spsp-state-v2'


def _dump_state(state, f) -> None:
    attrs = vars(state)
    keys = list(attrs)
    pickle.dump({'format': STATE_FILE_FORMAT, 'class': type(state).__name__, 'keys': keys}, f,
                protocol=pickle.HIGHEST_PROTOCOL)
    for k in keys:
        pickle.dump(attrs[k], f, protocol=pickle.HIGHEST_PROTOCOL)


def _load_state_file(path):
    with open(path, 'rb') as f:
        head = _AliasUnpickler(f).load()
        if not (isinstance(head, dict) and head.get('format') == STATE_FILE_FORMAT and set(head) == {'format', 'class', 'keys'}):
            return head   # 旧形式 (状態そのもの)
        if head['class'] != G2LearnState.__name__:
            raise StaleCheckpoint(f"{path}: 学習状態のクラスが違う ({head['class']})")
        st = G2LearnState.__new__(G2LearnState)   # pickle の既定の復元と同じ (__init__ を通さず属性を入れる)
        d = st.__dict__
        for k in head['keys']:
            d[k] = _AliasUnpickler(f).load()
        return st


# 学習状態の pickle が参照するモジュール名の読み替え表 (クラスを移動したときに追記する)。
# 旧名のまま unpickle すると旧モジュール (実験用 shim) が import され、その import 時処理が
# 本番設定を書き換える事故が起きた (2026-09-07)。ここで新名に読み替えて、旧モジュールを一切 import しない。
MODULE_ALIASES = {
    'eval_bt_lv_shared': 'spsp.learner',   # 2026-09-07: 学習器を v3/eval_bt_lv_shared.py → v3/v4/learner.py
    'data_loader': 'spsp.data_loader',     # 2026-09-08: build/data_loader.py → spsp/data_loader.py
}
MODULE_PREFIX_ALIASES = {
    'v4.': 'spsp.',                        # 2026-09-08: v3/v4/ → spsp/
}


def alias_module(module: str) -> str:
    if module in MODULE_ALIASES:
        return MODULE_ALIASES[module]
    for old, new in MODULE_PREFIX_ALIASES.items():
        if module.startswith(old):
            return new + module[len(old):]
    return module


class _AliasUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        return super().find_class(alias_module(module), name)


def load_pickle(path):
    """本番の pickle (学習状態・スナップショットキャッシュ・中間ファイル) はすべてこれで読む
    (旧モジュール名を読み替え、旧モジュールを import しない)。"""
    with open(path, 'rb') as f:
        return _AliasUnpickler(f).load()


# ── snap → build の中間ファイル (_inter_full.pkl) ──
# 2026-10-17 から、先頭に {'format': INTER_FORMAT, 'keys': [...]} を置き、inter の値を 1 つずつ別の pickle として続けて書く。
# pickle は書いたオブジェクトをすべて memo (id → 番号の表) に覚えるので、inter 全体を 1 回で書くと北米で memo だけで
# 数 GB になり、snap 段のピークがここだった。値ごとに書けば memo は一番大きい値の分だけで済む。
# 値をまたいで共有されているオブジェクトは読むと別々になるが、共有は短い str 定数だけ (日本の inter で確認: '0', 'bt_d' 等)
# なので中身は同じ (id(t) キーの構造は _rekey_id_to_idx で大会の番号キーにしてから書く)。
INTER_FORMAT = 'spsp-inter-v2'


def dump_inter(inter: dict, f) -> None:
    keys = list(inter)
    pickle.dump({'format': INTER_FORMAT, 'keys': keys}, f, protocol=pickle.HIGHEST_PROTOCOL)
    for k in keys:
        pickle.dump(inter[k], f, protocol=pickle.HIGHEST_PROTOCOL)


def load_inter(f) -> dict:
    """dump_inter で書いたもの、または旧形式 (inter 全体を 1 つの pickle) を読む。"""
    head = _AliasUnpickler(f).load()
    if isinstance(head, dict) and set(head) == {'format', 'keys'} and head['format'] == INTER_FORMAT:
        return {k: _AliasUnpickler(f).load() for k in head['keys']}
    return head


def load_state(path, settings_repr: str, capture: tuple[bool, bool, bool] | None = None) -> G2LearnState:
    st = _load_state_file(path)   # 属性ごとの形式 (save_state) と旧形式 (状態そのものの pickle) のどちらも
    if not isinstance(st, G2LearnState) or getattr(st, 'version', None) != STATE_VERSION:
        raise StaleCheckpoint(f"{path}: 学習状態の形式が違う (version {getattr(st, 'version', None)} != {STATE_VERSION})")
    if st.settings_repr != settings_repr:
        raise StaleCheckpoint(f"{path}: 保存時の Settings が今と違う\n  保存時: {st.settings_repr}\n  今:     {settings_repr}")
    st.upgrade_logs()   # 旧形式 (dict) の記録は配列の表に (値・並びは同じ)
    # 海外タグ集合の違いは check_overseas (大会データが要る) で見る
    if capture is not None and (st.capture_timeline, st.capture_per_tour, st.capture_at_time) != tuple(capture):
        raise StaleCheckpoint(f"{path}: 保存時の capture 指定 (timeline/per_tour/at_time) が今と違う")
    return st


def check_overseas(state: G2LearnState, all_t) -> None:
    """海外タグ集合 (OVERSEAS_UIDS) が保存時と違うとき、再開してよいかを決める。

    学習で海外タグが効くのは、各大会時点の JP カウンター (run_shared_cascade の pfilter) だけで、そこに入るのは
    その時点で既に TJPR スコアを持つ = それ以前の大会に出ている選手に限る。したがって増減した uid が処理済み大会
    (timestamp < cursor_ts) に 1 つも出ていなければ、保存時までの学習の軌跡は変わらない = 再開してよい。
    出ていれば軌跡が変わるので StaleCheckpoint (最初から学習し直す)。
    再開してよい場合は保存時の集合を今の集合に更新する (次回の比較のため)。

    以前 (STATE_VERSION 4) はダイジェストが違うだけで拒否していたため、新しく収録された海外選手 1 人でも
    毎回フル学習になっていた (2026-09-09 06:00 に発生)。
    """
    from spsp.common import OVERSEAS_UIDS
    saved = frozenset(getattr(state, 'overseas_uids', ()))
    if saved == OVERSEAS_UIDS:
        return
    changed = saved ^ OVERSEAS_UIDS
    for t in all_t:
        if t.timestamp >= state.cursor_ts:
            continue
        seen = {u for _, u in t.standings}
        for m in t.matches:
            seen.add(m.winner_id); seen.add(m.loser_id)
        hit = changed & seen
        if hit:
            raise StaleCheckpoint(
                f"保存時の海外タグ集合が今と違い、変わった uid {sorted(hit)[:5]} が処理済み大会 "
                f"({rel_event_path(t.event_path)}) に出ている (追加 {len(OVERSEAS_UIDS - saved)} / 削除 {len(saved - OVERSEAS_UIDS)})")
    print(f"  [overseas] 海外タグ集合が保存時と違う (追加 {len(OVERSEAS_UIDS - saved)} / 削除 {len(saved - OVERSEAS_UIDS)}) が、"
          f"処理済み大会 (〜{state.cursor_date}) に出ていない選手だけなので再開する", flush=True)
    state.overseas_uids = tuple(sorted(OVERSEAS_UIDS))
    state.overseas_digest = overseas_digest()


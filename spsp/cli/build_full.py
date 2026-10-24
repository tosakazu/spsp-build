#!/usr/bin/env python3
"""V4 single-pass full build (= prod_base subprocess なし、~7 分目標).

設計:
  - V4 BT (Glicko2 single-pass + ens-LV cascade) を v4/learn から直接呼んで一度学習
  - 学習結果を RunConfig.v4_bt_inject 経由で v4/build.run_build() に渡し inproc 呼出
    (= v2 prod の BT-all + BT-gated + Glicko2 並列学習をスキップ)
  - V4 cascade を JSON 出力に overlay
  - フロント overlay (HTML/JS/CSS) をコピー

build_tjpr_ranking との関係 (反復17):
  - 完全に standalone. `import build_tjpr_ranking` は一切なし.
  - 旧 _V4_BT_HOOK 機構は廃止. v4/build.RunConfig.v4_bt_inject= で同等の挙動を実現.
  - v4/build.run_build() を直接呼ぶ.

Usage:
    python build_v4_full.py [eval_date YYYY-MM-DD] [out_dir] [options]

Options:
    --frontend-src DIR     フロント source (default: REPO/site)

Defaults:
    eval_date = today
    out_dir   = /tmp/spsp_v4_full

Note (反復13):
    JJPR v3 は V4 フロントで一切使われないため、本 build pipeline では計算しない
    & JSON 出力からも完全に除外する. v2 prod 単独実行 (= build_tjpr_ranking.py
    --with-jjpr) では従来通り JJPR が出力される.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path


V4_LOAD_MIN_ENTRANTS = 2          # load 時は 2 名以上の大会を全部含める (= 大会一覧 / local 用)
V4_RANKING_MIN_ENTRANTS = 5       # これ未満は is_small=True. ランキング計算 (BT/TJPR/SPR/UF/peak) からスキップ.
                                   # (= V4 旧 V4_MIN_ENTRANTS と同値. V1 prod は 8 だが V4 は 5 大会も学習に使う方針.)

REPO = Path(__file__).resolve().parents[2]

# v4/* モジュールから直接 import. build_tjpr_ranking は import しない (反復17).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # リポジトリ直下 (spsp パッケージ)
from spsp.common import (  # noqa: E402
    GATED_M_TOUR, TJPR_DEFAULT_RATE, snapshot_grid_days, SNAPSHOT_DAYS,
    LV1_PARAMS, LV2_PARAMS,
    measured_uids_at_ts, _bracket_order_key,
)
from spsp.learn import precompute_banzuke  # noqa: E402
from spsp.meta import build_tournament_metadata  # noqa: E402
from spsp.build import RunConfig, run_build  # noqa: E402
from spsp.columnar import SHARED_UIDS, StrPerTourByLv, compact_str_table  # noqa: E402
from spsp.overlay import V4Overlay  # noqa: E402
from spsp.char_usage import compute_char_usage  # noqa: E402
from spsp.char_index import build_character_index  # noqa: E402
from spsp.char_vote import apply_char_votes, load_votes  # noqa: E402

from spsp.data_loader import load_all_tournaments  # noqa: E402
from spsp.tournament_utils import filter_dq_no_shows, EVENTS_ROOT  # noqa: E402
from spsp.learner import (  # noqa: E402
    run_shared_cascade_with_contribs,
    apply_settings as _apply_settings,
    g2_learn_advance,
    g2_snapshots,
)
from spsp.learn_state import (  # noqa: E402
    load_pickle as _load_pickle_file,
    G2LearnState, StaleCheckpoint, save_state, load_state, check_ledger, check_overseas, cutoff_ts_for_date, EXIT_STALE,
    dump_inter, load_inter, rel_event_path,
)
from spsp.rolling import raw_at_only  # noqa: E402
from spsp.lazy_views import compact_inter_raw  # noqa: E402
from spsp.settings import Settings  # noqa: E402
from spsp.paths import CHAR_VOTES_PATH, REGION  # noqa: E402
from spsp.skiplog import report_skips  # noqa: E402

# 本番構成 (v4/settings.py の既定値)。環境変数は見ない。main() で CLI フラグを反映して確定する。
# learn / build どちらのフェーズでも eval_bt_lv_shared に明示的に適用する。
SETTINGS = Settings()
_apply_settings(SETTINGS, lock=True)   # 以後、shim 等からの apply_settings は無視される (設定の固定)

# run_build() に渡す helpers (= eval/ 配下).
from spsp.tournament_utils import tournament_display_name  # noqa: E402
from spsp.tournament_utils import load_users, format_display  # noqa: E402


def _evict_dir_cache(root: Path):
    """root 配下の全ファイルを page cache / ZFS ARC から退避 (posix_fadvise DONTNEED).
    light ビルドの overlay フェーズ前に prod-build 出力を ARC から落とすために使う。"""
    n = 0
    for sub in ('players', 'tournaments', 'history'):
        d = root / sub
        if not d.exists():
            continue
        for p in d.iterdir():
            try:
                fd = os.open(str(p), os.O_RDONLY)
                try:
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                    n += 1
                finally:
                    os.close(fd)
            except Exception:
                pass
    # jsonl 本体も
    for f in root.glob('*.jsonl'):
        try:
            fd = os.open(str(f), os.O_RDONLY)
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            finally:
                os.close(fd)
        except Exception:
            pass
    return n


# id(t) (= Python オブジェクト ID) をキーにした構造は pickle 境界を越えられない
# (復元後に id() が変わる)。--phase learn→build 分割のため、_all_tournaments の
# リストインデックスを安定キーとして rekey する。対象: _ID_KEYED_INTER_KEYS。
_ID_KEYED_INTER_KEYS = ('_bt_at_t', '_t_meta_by_id', '_pt_at_time', '_rolling_deltas', '_tour_digest')


def _rekey_id_to_idx(inter: dict):
    """pickle 前: id(t) キー → _all_tournaments index キー."""
    tours = inter.get('_all_tournaments') or []
    idx_of = {id(t): i for i, t in enumerate(tours)}
    for k in _ID_KEYED_INTER_KEYS:
        d = inter.get(k)
        if d:
            missing = [kk for kk in d if kk not in idx_of]
            if missing:
                # _t_meta_by_id は no-match フィルタで除外される前の全大会ぶん作られるので、除外分 (数件) は余って正常。
                # それ以外のキーで欠落があるのはバグ (黙って捨てると build で値が消える) なので止める。
                if k != '_t_meta_by_id':
                    raise RuntimeError(f"_rekey_id_to_idx: {k} に _all_tournaments に無いキーが {len(missing)} 件")
                print(f"  [rekey] {k}: 除外済み大会 {len(missing)} 件ぶんのメタデータを落とす", flush=True)
            inter[k] = {idx_of[kk]: v for kk, v in d.items() if kk in idx_of}
    inter['_idx_rekeyed'] = True


def _rekey_idx_to_id(inter: dict):
    """pickle 復元後: index キー → id(t) キー (同梱の _all_tournaments を使う)."""
    if not inter.pop('_idx_rekeyed', False):
        return
    tours = inter.get('_all_tournaments') or []
    n = len(tours)
    for k in _ID_KEYED_INTER_KEYS:
        d = inter.get(k)
        if d:
            inter[k] = {id(tours[i]): v for i, v in d.items() if isinstance(i, int) and 0 <= i < n}


def _load_tournaments_and_meta(eval_date: dt.datetime):
    """全 tournaments を load + filter + meta 構築 (timestamp 昇順 sort 済)."""
    print("Loading tournaments ...", flush=True)
    t0 = time.time()
    since = dt.datetime(2018, 1, 1)
    all_t = load_all_tournaments(
        EVENTS_ROOT, REGION,
        since_ts=int(since.timestamp()),
        until_ts=int(eval_date.timestamp()),
        min_entrants=V4_LOAD_MIN_ENTRANTS)
    filter_dq_no_shows(all_t)
    # 反復19: t_meta builder を v4/meta.build_tournament_metadata に一本化.
    t_meta = build_tournament_metadata(
        all_t, tournament_display_name=tournament_display_name,
        min_entrants_for_ranking=V4_RANKING_MIN_ENTRANTS)
    # 「使える試合が 0 件の実大会」を集計対象から除外する (既定 ON, --keep-no-match で温存)。
    # 該当: (a) offline で順位だけ手入力され個別試合が start.gg に未報告 / (b) 試合は存在するが
    # 参加者が start.gg アカウント未連携で winner/loser を user_id に紐付けできずロードで全落ち、
    # の 2 種。どちらも standings 経由で TJPR に寄与してしまう「信頼性の低い順位のみ大会」なので
    # 無効扱いにする。class_virtual は試合を親から引く設計で t.matches 空が正常なため除外しない。
    if not SETTINGS.keep_no_match:
        _before = len(all_t)
        all_t = [t for t in all_t
                 if getattr(t, 'matches', None) or (t_meta.get(id(t)) or {}).get('is_class_virtual')]
        print(f"  [no-match-filter] 使える試合0件の実大会を {_before - len(all_t)} 件除外", flush=True)
    all_t.sort(key=lambda t: t.timestamp)
    print(f"  {len(all_t)} tournaments ({time.time()-t0:.0f}s)", flush=True)
    return all_t, t_meta


def _serialize_cascade_snapshots(cascade_at, snap_ts_by_day):
    """cascade_snapshots[day_str] = {uid_str: [max_lv, ens_r, tjpr_score]}

    tjpr_score = player_tjpr_at_max_lv = V4 cascade sum at that snap day.
    history 用に tjpr_score を含める (None だと UI で時系列描けない).
    """
    return {str(d): _cascade_table(cascade_at.get(ts, ({}, {}, {}))) for d, ts in snap_ts_by_day.items()}


def _cascade_table(cascade):
    """1 時点のカスケード (max_lv, ens_at_max_lv, tjpr_at_max_lv) → {uid_str: [max_lv, ens_r, tjpr_score]}。
    読むだけの配列の表へ (spsp/columnar.StrUidTable、値・並び・型は同じ。北米で 20 万人 × 61 日の list)"""
    max_lv, ens_at_max_lv, tjpr_at_max_lv = cascade
    return compact_str_table({
        str(u): [max_lv[u], ens_at_max_lv[u], round(float(tjpr_at_max_lv.get(u, 0.0)), 4)]
        for u in max_lv
    }, SHARED_UIDS)


def _peak_table(peak_dict):
    """1 時点の peak180 {uid: ord} → {uid_str: 4 桁 round} (spsp/columnar.StrUidTable)"""
    return compact_str_table({str(u): round(v, 4) for u, v in peak_dict.items()}, SHARED_UIDS)


def _load_pickle_compat(f):
    """開いたファイルから本番 pickle を読む (旧モジュール名の読み替え付き)。"""
    from spsp.learn_state import _AliasUnpickler
    return _AliasUnpickler(f).load()


# 直列化した形 (旧 inter の bt_timeline / bt_per_tour / bt_per_match)。今は build 段が spsp/lazy_views.py の窓で引く。
# 関数はテストと、生のキーに int 以外が混ざったときの旧方式 (overlay) のために残す (定義は lazy_views)
from spsp.lazy_views import (_serialize_bt_timeline, _serialize_bt_per_tour,  # noqa: E402,F401
                             _serialize_bt_per_match)


def _serialize_peak180_at_day(peak180_at, snap_ts_by_day):
    """{day_str: {uid_str: peak180_ord}} (4 桁 round。日ごとに spsp/columnar.StrUidTable)"""
    return {str(d): _peak_table(peak180_at.get(ts, {})) for d, ts in snap_ts_by_day.items()}


def _compute_class_bt_split(all_t, t_meta, bt_per_match):
    """選手別・event別の「下位クラス(is_class)試合の内部レート delta 合計」を計算.

    返り値: {uid_str: {event_id(int): [class_internal_sum, class_tracked(bool)]}}
    プレイヤーページで下位クラス(cv)行に「クラス内対戦の BT 収支」を表示し、
    親 event 行は本戦(非class)分のみ表示する (= 二重計上しない) ために使う.
    bt_per_match[(eid, wuid, luid)] = [[d_w, d_l, w_tracked, l_tracked], ...] (試合進行順).
    """
    split: dict = {}

    def _add(uid, eid, d, tracked):
        e = split.setdefault(str(uid), {}).setdefault(eid, [0.0, False])
        e[0] += d
        if tracked:
            e[1] = True

    for t in all_t:
        meta = t_meta.get(id(t), {})
        if meta.get('is_class_virtual'):
            continue
        eid = meta.get('event_id')
        if eid is None:
            continue
        # 学習側の記録順 (= _bracket_order_key 順) で occurrence を対応付ける.
        # counter は class 以外の試合も進める (同 key に class/非class が混在しても整合).
        seen: dict = {}
        for m in sorted(t.matches, key=_bracket_order_key):
            key = (eid, m.winner_id, m.loser_id)
            i = seen.get(key, 0)
            seen[key] = i + 1
            if not getattr(m, 'is_class', False):
                continue
            occs = bt_per_match.get(key)
            if not occs:
                continue
            if not isinstance(occs[0], (list, tuple)):
                occs = [occs]  # 旧 flat 形式互換
            info = occs[i] if i < len(occs) else occs[-1]
            _add(m.winner_id, eid, float(info[0]), bool(info[2]))
            _add(m.loser_id, eid, float(info[1]), bool(info[3]))
    return split


def _compute_per_tour_by_lv(all_t, t_meta, bt_at_t, peak180_at, eval_ts):
    """eval_date 時点での per_tour_by_lv (= cascade 各 Lv の player×event 寄与) を計算."""
    print(f"Computing V4 per_tour_by_lv at eval_date ...", flush=True)
    t2 = time.time()
    snap_date = dt.datetime.fromtimestamp(eval_ts)
    prior_t = [t for t in all_t if t.timestamp < eval_ts]
    cache = precompute_banzuke(prior_t, snap_date, bt_at_t, 0.0, t_meta, TJPR_DEFAULT_RATE)
    peak_dict = peak180_at.get(eval_ts, {})
    # 計測中除外: learner の run_cascade_at と同じ measured 集合を使うことで
    # per_tour_by_lv の LV 割当が cascade_snapshots と一致する。
    # run_cascade_at に合わせ measured_uids_at_ts (= 表示順位と同一定義、実大会 standings ベース)
    # を使う。旧 measured_uids_from_cache は class_virtual を二重カウントして Lv 境界がずれていた。
    measured = measured_uids_at_ts(all_t, t_meta, eval_ts)
    _, _, _, level_contribs = run_shared_cascade_with_contribs(cache, peak_dict,
                                                               measured_uids=measured)
    del cache   # 以後は level_contribs だけ使う (変換の間 表を持たない。北米で数 GB の一時領域)
    # tid -> per_tour_by_lv の key. 通常 event は event_id (str)、
    # class_virtual (下位クラス, event_id=None) は親 event_id + class_letter の合成 key.
    # 合成 key は overlay 側 (postprocess) で同じ形式で組み立てて lookup する.
    tid_to_key = {}
    for pt in prior_t:
        m = t_meta.get(id(pt), {})
        if m.get('is_class_virtual'):
            tid_to_key[id(pt)] = f"cv:{m.get('parent_event_id')}:{m.get('class_letter')}"
        else:
            eid = pt.event_id   # = attr.json の event_id (data_loader が読んだ値)
            if eid is not None:
                tid_to_key[id(pt)] = str(eid)
    # 寄与が配列の表 (spsp/learn.ContribTable) なら、3 段の dict を作らずに直接配列の表にする (中身・並びは同じ)
    from spsp.learn import ContribTable
    if all(type(c) is ContribTable for c in level_contribs):
        table = StrPerTourByLv.from_contrib_tables(
            level_contribs, [LV2_PARAMS[2] if lv >= 2 else LV1_PARAMS[2] for lv in range(1, len(level_contribs) + 1)],
            tid_to_key)
        if table is not None:
            del level_contribs
            print(f"  done ({time.time()-t2:.0f}s, {table.n_players()} players)", flush=True)
            print("  per_tour_by_lv (中間ファイル): 配列の表", flush=True)
            return table
    per_tour_by_lv = defaultdict(lambda: defaultdict(dict))
    for lv_idx in range(len(level_contribs)):
        contribs = level_contribs[lv_idx]
        level_contribs[lv_idx] = None   # 変換し終えた Lv から手放す (変換の順と結果は同じ)
        lv = lv_idx + 1
        counted_thresh = LV2_PARAMS[2] if lv >= 2 else LV1_PARAMS[2]
        for uid, lst in contribs.items():
            for c in lst:
                key = tid_to_key.get(c['t_id'])
                if key is None:
                    continue
                per_tour_by_lv[str(uid)][key][str(lv)] = [
                    round(c['raw_points'], 4),
                    round(c['pos_factor'], 6),
                    round(c['weighted'], 4),
                    int(c['rank_in_player'] < counted_thresh),
                ]
        del contribs
    out = {u: dict(d) for u, d in per_tour_by_lv.items()}
    del level_contribs, per_tour_by_lv
    print(f"  done ({time.time()-t2:.0f}s, {len(out)} players)", flush=True)
    # 以後 (中間ファイル・build・overlay) は読むだけなので配列の表に (spsp/columnar.StrPerTourByLv、引いた値と並びは同じ)
    table = StrPerTourByLv.from_nested(out)
    print(f"  per_tour_by_lv (中間ファイル): {'配列の表' if table is not None else 'dict (配列の表にできない値がある)'}", flush=True)
    return out if table is None else table


# g2_snapshots (と上の取り出しの後) で使わない学習状態の属性。compute_v4_intermediate が復元の前に None にする
# (状態はこの後保存しない。保存は学習の直後に済んでいる)。
_STATE_ATTRS_UNUSED_BY_SNAPSHOTS = (
    'rolling', 'peak_track', 'peak180', 'pt_scores_entries', 'pt_scores', 'pt_mwindow', 'pt_mcounts', 'pt_meas_set',
    'pt_peak_cur', 'pt_peak_exp', 'pt_last_parts', 'ratings', 'tour_count', 'last_seen_ts', 'player_lv',
    'bt_timeline', 'bt_per_tour', 'bt_per_match', 'pt_at_time_by_path',
)


def _release_unused_state(st) -> None:
    for name in _STATE_ATTRS_UNUSED_BY_SNAPSHOTS:
        if hasattr(st, name):
            setattr(st, name, None)


def compute_v4_intermediate(eval_date: dt.datetime, *, state_in=None, state_out=None, until=None,
                            learn_only=False, snap_only=False, snap_cache=None, snapshot_grid='fixed',
                            trust_ledger=False, save_at=(), save_at_template=None, save_at_keep=2):
    """V4 single-pass の結果を 1 つの dict にまとめて返す.

    state_in : 学習状態 (v4/learn_state.py) の pickle。あればそこから再開する (差分学習)。
    state_out: 学習後の状態を保存する先。
    until    : YYYY-MM-DD。この日 (23:59:59 含む) までで学習を止めて state_out に保存し、None を返す
               (スナップショットや inter は作らない)。CPU 制限内で学習を日付で分割するため。
    learn_only: 学習して state_out に保存したら None を返す (--phase learn)。
    snap_only : 学習はせず state_in (eval_date まで学習済み) からスナップショット以降だけ作る (--phase snap)。
               CPU 制限 (~300s/プロセス) のため、本番は learn → snap → build の 3 プロセスに分ける。
    trust_ledger: state_in の台帳と今のデータの照合 (check_ledger) を飛ばす (--trust-ledger)。state_in をこの実行の中で
               同じデータから作ったときだけ使う (北米の区切りごとの学習。照合は一致すれば何も変えないので結果は同じ)。

    Returns:
      dict with keys (= build_v4_site.py の intermediate と互換):
        eval_date, snap_days, snap_ts, cascade_snapshots,
        bt_timeline, bt_per_tour, bt_per_match,
        peak180_at_day, per_tour_by_lv,
      and (= injection 用):
        all_tournaments, t_meta_by_id, bt_at_t, peak180_at_target_raw
    """
    print(f"=== V4 phase ===", flush=True)
    print(f"Eval date: {eval_date.date()}", flush=True)
    # 学習状態は大会より先に読む (2026-10-20)。古い形式 (dict) の状態は読んだ後に配列の表へ変換する (upgrade_logs) ので、
    # 読む間は一時的に 状態の 2 倍近く要る (北米で +9 GB)。大会 (北米で 4.4 GB) を読んだ後だとその上に乗って snap と learn の
    # ピークだった。状態の読み込みは大会を使わない (大会と突き合わせる check_overseas / check_ledger は下で大会を読んだ後)
    _apply_settings(SETTINGS, lock=True)
    st = None
    if state_in or snap_only:
        st = load_state(state_in, SETTINGS.state_repr(), capture=(True, True, True))
        _apply_settings(SETTINGS, lock=True)   # unpickle が別モジュールを import して設定を触っても本番設定に戻す
    all_t, t_meta = _load_tournaments_and_meta(eval_date)
    # 試合は読み込んだ直後に圧縮した塊にして手放し、使うときに大会ごとに作り直す (spsp/lazy_matches、北米で 約 1 GB)。
    # 中間ファイルにも試合は入らない
    from spsp import lazy_matches
    if lazy_matches.ENABLED:
        print(f"  lazy matches: {lazy_matches.release(all_t)} 試合を手放した", flush=True)

    # 履歴スナップショットは固定の暦日グリッド (common.snapshot_grid_days)。日数キー (評価日から N 日前) は
    # 内部の互換のため残すが、点そのものは暦に固定されているので日をまたいでも同じ時刻になる。
    # --snapshot-grid legacy は検証用 (旧 SNAPSHOT_DAYS = 評価日から N 日前。分割の同値性を旧ビルドと突き合わせるとき)
    snap_days_list = (list(SNAPSHOT_DAYS) if snapshot_grid == 'legacy' else snapshot_grid_days(eval_date)) + [0]
    snap_ts_by_day = {d: int((eval_date - dt.timedelta(days=d)).timestamp())
                      for d in snap_days_list}
    snap_ts_sorted = sorted(snap_ts_by_day.values())

    if snap_only:
        print(f"Loaded learn state {state_in}: {st.describe()}", flush=True)
        t1 = time.time()
        check_overseas(st, all_t)               # 海外タグ集合の変化が処理済み大会に効くか
        if not trust_ledger:
            check_ledger(st, all_t, t_meta)      # 学習に使ったデータと今のデータが同じか
        if all_t and st.cursor_ts <= all_t[-1].timestamp:
            raise StaleCheckpoint(f"学習状態は {st.cursor_date} までで、eval_date {eval_date.date()} まで進んでいない")
        print(f"  ledger ok ({time.time()-t1:.0f}s)", flush=True)
    else:
        if st is not None:
            check_overseas(st, all_t)              # 海外タグ集合の変化が処理済み大会に効くか (効かなければ再開)
            print(f"Resuming learn state from {state_in}: {st.describe()}", flush=True)
        else:
            st = G2LearnState(True, True, True, SETTINGS.state_repr())
        until_ts = cutoff_ts_for_date(until) if until else None
        print(f"Training V4 BT-LV-shared ({'until ' + str(until) if until else 'to eval_date'}) ...", flush=True)
        t1 = time.time()
        # --save-at: 区切りの日ごとに状態を保存しながら同じプロセスで続ける (区切りごとに別プロセスにすると大会の読み込み
        # (北米 約 75 秒) と状態の読み直しが区切りの数だけかかっていた。学習は途中で保存・再開しても同じ結果になる作り
        # (g2_learn_advance) なので、続けて回しても各区切りの状態と最後の結果は別プロセスのときと同じ。2026-10-18)
        verify = not trust_ledger
        saved = []
        n_cuts = 0
        for cut in sorted(save_at):
            cut_ts = cutoff_ts_for_date(cut)
            if cut_ts <= st.cursor_ts or (until_ts is not None and cut_ts >= until_ts):
                continue
            t2 = time.time()
            n_cut = g2_learn_advance(st, all_t, t_meta, GATED_M_TOUR, cut_ts, verify_ledger=verify)
            n_cuts += n_cut
            verify = False                       # 2 つ目からはこのプロセスの中で作った状態
            path = save_at_template.format(cut=cut)
            save_state(st, path)
            saved.append(path)
            for old in saved[:-save_at_keep] if save_at_keep else []:
                Path(old).unlink(missing_ok=True)
            del saved[:-save_at_keep or None]
            print(f"  [save-at {cut}] {n_cut} tournaments ({time.time()-t2:.0f}s) → {path}", flush=True)
        n_new = n_cuts + g2_learn_advance(st, all_t, t_meta, GATED_M_TOUR, until_ts, verify_ledger=verify)
        print(f"  learn done ({time.time()-t1:.0f}s, {n_new} new tournaments, {st.describe()})", flush=True)
        if state_out:
            save_state(st, state_out)
            print(f"  saved learn state → {state_out}", flush=True)
        if until or learn_only:
            return None
    # 学習状態から後で使うものを先に取り出し、スナップショットの復元 (g2_snapshots) が使わない部分を手放してから復元する。
    # g2_snapshots が読むのは rating_hist / part_order / first_part_ts / bt_at_t_by_path / ledger だけ。以前は状態を丸ごと
    # 持ったまま復元していて、snap 段のピークの 1 つだった (北米で rolling の途中状態・peak_track・per-tour の増分スコアなど
    # 約 3 GB。2026-10-17)。取り出しはどれも状態を読むだけなので、順番を変えても値は同じ。
    # pt_at_time: per-tour 時のみ {tid: {uid: [lv,ens,tjpr_r,bt_r,...]}}、それ以外 None。
    # 大会ページの「当時」アンカーを毎大会厳密にするのに使う。
    pt_at_time = st.pt_at_time_view(all_t) if SETTINGS.per_tour_lv else None
    # 大会ごとの中身のハッシュ (学習の台帳 = tournament_digest。読み込み時に今のデータと照合済み)。大会ファイルを
    # 書き直すかの鍵に使う (spsp/tour_cache.py。build で試合を読み直して作ると北米で 30 秒)
    _dig = {p: d for p, _ts, d in st.ledger}
    tour_digest = {id(t): _dig[k] for t in all_t if (k := rel_event_path(t.event_path)) in _dig}
    bt_timeline = dict(st.bt_timeline) if isinstance(st.bt_timeline, dict) else st.bt_timeline   # 配列の表はそのまま (写さない)
    bt_per_tour = st.bt_per_tour
    bt_per_match = st.bt_per_match
    # 学習側で増分計算済 (build は再計算しない)。build が読むのは raw_at だけなので、それだけにする (spsp/rolling.py)
    rolling_deltas = raw_at_only(st.rolling_view(all_t))
    # 過去最高順位 (学習中に追跡した真の最小値。uid → {'ensemble': (rank, day), ..., 'lv_best', 'tier_best'})
    peak_track = st.peak_track.snapshot(eval_date.date().toordinal())
    _pt_updates = st.peak_track.n_updates
    _release_unused_state(st)
    t1 = time.time()
    # 時点ごとの結果は、その時点を計算したらすぐ inter に入れる形 (配列の表) にする。生の dict (peak180) を残すのは
    # 一番新しい時点 (= eval_ts) だけ (_compute_per_tour_by_lv と build の bt_peak180_ord が読む)。以前は 61 時点 × 全選手の
    # dict を最後まで持っていた (北米で +1.8 GB)。表に詰める処理は以前と同じ (_cascade_table / _peak_table)
    _last_ts = snap_ts_sorted[-1] if snap_ts_sorted else None
    _peak_tabs: dict = {}

    def _compact_point(ts, peak_dict, cascade):
        _peak_tabs[ts] = _peak_table(peak_dict)
        return (peak_dict if ts == _last_ts else None), _cascade_table(cascade)
    peak180_at, _lv_at, cascade_tabs = g2_snapshots(st, all_t, t_meta, snap_ts_sorted, cache_dir=snap_cache,
                                                    compact=_compact_point)
    del _lv_at   # compact を渡すと作らない (空)
    print(f"  snapshots at {len(snap_ts_sorted)} points ({time.time()-t1:.0f}s)", flush=True)
    bt_at_t = st.bt_at_t_view(all_t)
    print(f"  pt_at_time={len(pt_at_time) if pt_at_time else 0} tours", flush=True)
    print(f"  peak_track: {len(peak_track)} players, {_pt_updates} updates", flush=True)
    # 学習状態の残り (レート履歴・台帳など) はもう使わない。上で取り出したもの (見え方・bt_* ・rolling の結果) だけ残して手放す
    del st
    cascade_snapshots = {str(d): (cascade_tabs[ts] if ts in cascade_tabs else _cascade_table(({}, {}, {})))
                         for d, ts in snap_ts_by_day.items()}
    del cascade_tabs

    print(f"Computing character usage ...", flush=True)
    t_cu = time.time()
    char_usage = compute_char_usage(all_t, t_meta, bt_per_tour)
    print(f"  done ({time.time()-t_cu:.0f}s, {len(char_usage)} players)", flush=True)

    # キャラ投票 (docs/post_feature_design.md §12) を反映。
    # ここで並びを直せば、プレイヤーページ・ランキングの絵文字・使い手ランキングが
    # まとめて追随する (どれも characters[0] をメインとして読むため)。
    # 取得は build/fetch_char_votes.py (= deploy/update_and_deploy.sh のステップ)。
    _votes_path = CHAR_VOTES_PATH
    _votes, _votes_at = load_votes(_votes_path)
    if _votes:
        char_usage, _vstats = apply_char_votes(char_usage, _votes)
        print(f"  char votes: {len(_votes)} rows (fetched {_votes_at or '?'}) -> "
              + (', '.join(f'{k}={v}' for k, v in _vstats.items() if v) or 'no change'),
              flush=True)
    else:
        print(f"  char votes: 0 rows ({_votes_path} 無し or 空)", flush=True)

    # 計測済み uid 集合を snap day ごとに計算 (= jp-counter の計測中灰色化に使う).
    print(f"Computing measured uids per snap day ...", flush=True)
    t_m = time.time()
    measured_at_day = {d: measured_uids_at_ts(all_t, t_meta, ts)
                       for d, ts in snap_ts_by_day.items()}
    print(f"  done ({time.time()-t_m:.0f}s, day0 measured={len(measured_at_day.get(0, set()))})", flush=True)

    eval_ts = snap_ts_by_day[0]
    inter = {
        'eval_date': eval_date.date().isoformat(),
        'peak_track': peak_track,
        'snap_days': snap_days_list,
        'snap_ts': {str(d): snap_ts_by_day[d] for d in snap_days_list},
        'snap_ts_by_day': snap_ts_by_day,
        'cascade_snapshots': cascade_snapshots,
        # bt_timeline / bt_per_tour / bt_per_match の直列化した形 (str キー・4 桁丸め) は inter に入れない:
        # build 段の overlay が下の _bt_*_raw から引くたびに作る (spsp/lazy_views.py、値は _serialize_* と同じ)。
        # 以前は両方を持って pickle していた (北米で 約 3 GB。2026-10-17)
        'peak180_at_day': {str(d): (_peak_tabs[ts] if ts in _peak_tabs else _peak_table({}))
                           for d, ts in snap_ts_by_day.items()},
        # 軽量ビルド (--light): per_tour_by_lv の巨大 transient
        # (全大会 precompute_banzuke + level_contribs) をスキップしピークメモリ削減。
        # プレイヤーページの「大会別 Lv 寄与」表示のみ犠牲 (大会ページ順位変動には不要)。
        'per_tour_by_lv': ({} if SETTINGS.skip_per_match
                           else _compute_per_tour_by_lv(all_t, t_meta, bt_at_t, peak180_at, eval_ts)),
        'class_bt_split': _compute_class_bt_split(all_t, t_meta, bt_per_match),
        'char_usage': char_usage,
        'measured_at_day': measured_at_day,
        # 注入用 (raw in-memory objects, not serialized)
        '_all_tournaments': all_t,
        '_t_meta_by_id': t_meta,
        '_bt_at_t': bt_at_t,
        '_rolling_deltas': rolling_deltas,   # 学習側で増分計算済 (build は再計算しない)
        '_bt_timeline_raw': bt_timeline,
        '_bt_per_tour_raw': bt_per_tour,
        '_bt_per_match_raw': bt_per_match,
        '_peak180_at_target_raw': peak180_at,
        '_eval_ts': eval_ts,
        '_pt_at_time': pt_at_time,  # per-tour 厳密アンカー (None=snapshot粒度)
        '_tour_digest': tour_digest,   # 大会ごとの中身のハッシュ (学習の台帳)
    }
    # snap で bt_per_tour / bt_per_match を読むのは上 (char_usage・class_bt_split) まで。ここで値を小さい tuple に詰める
    # (build は per_tour_fields / per_match_occurrences で読む。spsp/lazy_views.py)
    compact_inter_raw(bt_per_tour, bt_per_match)
    # 時点ごとの peak180 (61 時点 × 全選手) のうち build が読むのは一番新しい時点だけ (build._v4_adapt_bt_outputs の
    # bt_peak180_ord。時点ごとの表示は直列化した peak180_at_day の方)。残りは inter に入れない (北米で 約 1 GB)
    if peak180_at:
        _last = max(peak180_at.keys())
        inter['_peak180_at_target_raw'] = {_last: peak180_at[_last]}
    return inter


def run_prod_build_with_v4_inject(inter: dict, out_dir: Path, eval_date: dt.datetime, overlay=None):
    """v4.build.run_build() を V4 BT 注入で実行 (= 旧 _V4_BT_HOOK 機構の置換).

    RunConfig.v4_bt_inject に V4 single-pass の BT 結果を入れることで、v2 prod の
    BT-all + BT-gated + Glicko2 並列学習をスキップさせる. それ以外
    (placements / seeds / UF / achievements / radar / JSON output) は v2 と同じ.

    反復13: no_jjpr_output=True で JJPR 系フィールドを JSON 出力から除外する
    (= V4 フロントで未使用).
    """
    print(f"\n=== Prod build (with V4 BT injected via run_build) ===", flush=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    # bt_at_t / pt_at_time は run_build が途中 (書き出しの前) で手放す。inter に参照が残ると解放されないので、ここで外して
    # inject にだけ入れる (この後 inter からは読まない。V4Overlay は作成済みで、どちらも使わない)
    inject = {
        'bt_at_t': inter.pop('_bt_at_t'),
        'bt_timeline': inter['_bt_timeline_raw'],
        'bt_per_tour': inter['_bt_per_tour_raw'],
        'bt_per_match': inter['_bt_per_match_raw'],
        'peak180_at_target': inter['_peak180_at_target_raw'],
        'eval_ts': inter['_eval_ts'],
        # V4 cascade ベースで snapshots を populate するための入力 (反復22):
        # build_history が読む key を持つ snapshots を build.run_build 内で構築する.
        # = peak_ranks (postprocess) と achievements (build_achievements) の内部整合.
        'cascade_snapshots': inter['cascade_snapshots'],
        'peak180_at_day': inter['peak180_at_day'],
        'snap_days': inter['snap_days'],
        'measured_at_day': inter['measured_at_day'],
        'pt_at_time': inter.pop('_pt_at_time', None),  # 大会ページ毎大会厳密アンカー
        # 学習側で増分計算した大会ごとの順位変動。build が配列の表にして inject の方を差し替えるので、inter からは外す
        # (残すと元の dict が書き出しの間ずっと残っていた。V4Overlay も読まない)
        'rolling_deltas': inter.pop('_rolling_deltas', None),
        'tour_digest': inter.pop('_tour_digest', None),   # 大会ファイルを書き直すかの鍵 (spsp/tour_cache.py)
    }

    cfg = RunConfig(
        eval_date=eval_date,
        out_path=out_dir / "latest_tjpr_full.jsonl",
        meta_out_path=out_dir / "meta.json",
        no_tournament_json=False,
        no_player_json=False,
        min_entrants_for_ranking=V4_RANKING_MIN_ENTRANTS,
        v4_bt_inject=inject,
        precomputed_all_tournaments=inter['_all_tournaments'],
        precomputed_t_meta=inter['_t_meta_by_id'],
        events_root=EVENTS_ROOT,
        tournament_display_name=tournament_display_name,
        load_users=load_users,
        format_display=format_display,
        # series.json / tournaments.json は v4.output.run_aux_outputs() が site/data に
        # 書き込もうとするので skip (= V4 では別途 overlay).
        run_aux_outputs=False,
        light=SETTINGS.skip_per_match,
        overlay=overlay,
    )
    run_build(cfg)
    print(f"  prod build done", flush=True)


def _overlay_frontend(out_dir: Path, frontend_src: Path):
    """frontend_src (site/) の中身を out_dir に copy/symlink する.

    2026-09-29 からフロント (HTML/JS/CSS) は tosakazu/spsp-web にあり、site/ には何も追跡されていない (キャラ絵文字表とお知らせ news.json も spsp-web へ移した。ビルドのキャラ一覧は spsp/characters.json)。
    ここで写るのもそれだけ (下の SYMLINK_ENTRIES の多くはもう存在しない)."""
    print(f"Frontend overlay from {frontend_src} ...", flush=True)
    if not frontend_src.is_dir():   # 新しい checkout (Actions 等) では site/ が無いことがある: 写すものも無い
        print(f"  (no {frontend_src}: skip)", flush=True)
        return
    # seeding (被り回避JSモジュール) も symlink: 常に working tree の最新が見える。
    # data/ は symlink にしない: ビルドと [4/5] の生成物 (character_index.json 等) を BUILD_DIR/data に
    # 書き、配信中の site/data には [4.5/5] でまとめて反映する (以前は symlink 越しに site/data へ直接書いていた)。
    SYMLINK_ENTRIES = {'events', 'news', 'p', 't', 'c', 'local', 'seed', 'seed-upload', 'seeding'}
    # site/data の中の symlink (upcoming.json 等) は symlink のままコピーする (デプロイの rsync -aL が実体化する)
    KEEP_SYMLINKS_INSIDE = {'data'}
    # ビルドが out_dir に直接書く生成物。site/ 側にも同名の (stale な) copy が
    # 存在するため、絶対に site/ からの上書きコピーをしてはいけない。
    BUILD_OUTPUT_ENTRIES = {'players', 'tournaments', 'history', 'latest_tjpr_full.jsonl', 'meta.json', 'players_current.json'}
    for entry in frontend_src.iterdir():
        target = out_dir / entry.name
        if entry.name in BUILD_OUTPUT_ENTRIES:
            continue  # ビルド生成物: site/ の stale copy で上書きしない
        if entry.name in SYMLINK_ENTRIES:
            # symlink は working tree を指す。checkout の置き場所が変わって参照先が無くなった (2026-09-08 に
            # spsp-ranking-v2 → spsp-ranking の改名で発生) か別の場所を指していれば貼り直す
            if target.is_symlink() and (not target.exists() or target.resolve() != entry.resolve()):
                target.unlink()
            if not (target.exists() or target.is_symlink()):
                os.symlink(entry.resolve(), target)
            continue
        # トップレベルの file / dir (index.html, player-detail.js, ranking-table.*,
        # news.json, pref/ 等) は毎回 working tree の最新で上書きコピーする。
        # 旧実装の「既存ならスキップ」は同日ビルド (= build dir 再利用) で古い copy が
        # 凍結され、フロント修正が何度もデプロイから漏れた (2026-07-21 恒久修正)。
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        if entry.is_dir():
            shutil.copytree(entry, target, symlinks=entry.name in KEEP_SYMLINKS_INSIDE)
        else:
            shutil.copy2(entry, target)


def _write_last_activity(out_dir: Path, last_activity: dict | None):
    """uid -> 最終大会参加日 を BUILD_DIR/data/last_activity.json に書く (優先枠作成ページの直近参加フィルタ用)。
    形式・内容は build/build_last_activity_json.py (players/*.json を読み直す版) と同一。書き出し時に集めるので 11 秒速い。"""
    if not last_activity:
        print("  WARN: last_activity が空 — data/last_activity.json を書かない", flush=True)
        return
    data_dir = out_dir / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    p = data_dir / 'last_activity.json'
    tmp = p.with_suffix('.json.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(last_activity, f, separators=(',', ':'), sort_keys=True)
    os.replace(tmp, p)
    print(f"  last_activity: {len(last_activity)} players", flush=True)


def _overlay_build_character_index(out_dir: Path, rows=None):
    """players/*.json を walk して BUILD_DIR/data/character_index.json を生成 (site/data には [4.5/5] で反映)."""
    players_dir = out_dir / 'players'
    if not players_dir.exists():
        return
    data_dir = out_dir / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    if rows is not None:
        from spsp.char_index import build_character_index_from_records
        idx = build_character_index_from_records(rows)   # 書き出した dict から (players/*.json を読み直さない)
    else:
        idx = build_character_index(players_dir)
    out_path = data_dir / 'character_index.json'
    out_path.write_text(json.dumps(idx, ensure_ascii=False))
    print(f"  character_index: {len(idx['characters'])} chars, "
          f"{sum(len(v) for v in idx['main_by_char'].values())} main entries", flush=True)


def _run_build_and_overlay(inter: dict, out_dir: Path, eval_date: dt.datetime,
                           frontend_src: Path, t_total: float):
    """Phase 6: prod-build (V4 の順位評価/直対評価は各 JSON を書く直前に反映) + フロント/character_index。all / build 共通."""
    _light = SETTINGS.skip_per_match   # 軽量ビルド: 書いた JSON を page cache から退避する (output.set_light は run_build が cfg.light で呼ぶ)
    # 以前は run_build が書いた JSON 一式を overlay が読み直して書き換えていた (生成 → 上書きの二段構え)。
    # 今は V4Overlay を RunConfig.overlay で渡し、run_build が各レコードを書く直前に変換する (1 回だけ書く)。
    print(f"\n=== V4 overlay: preparing transforms ===", flush=True)
    overlay = V4Overlay(inter)
    run_prod_build_with_v4_inject(inter, out_dir, eval_date, overlay=overlay)
    if _light:
        _evict_dir_cache(out_dir)
        print(f"  [light] evicted build output from page cache / ARC", flush=True)
    # 順番注意: _overlay_frontend が data/ を site/data から作り直す (rmtree + copy) ので、その後に character_index を書く
    _overlay_frontend(out_dir, frontend_src)
    _overlay_build_character_index(out_dir, rows=getattr(overlay, 'char_rows', None))
    _write_last_activity(out_dir, getattr(overlay, 'last_activity', None))


def main():
    # 日付・週末判定・スナップショット時刻は地域の時間帯で決める (日本 = Asia/Tokyo = 本番ホスト)。
    # 他のホストでも同じ出力になるよう、地域モジュールの TIMEZONE でプロセスの TZ を固定する。
    from spsp.derived import region_rules as _region_rules
    from zoneinfo import ZoneInfo as _ZoneInfo
    _tz_name = _region_rules()["constants"]["TIMEZONE"]
    os.environ['TZ'] = _tz_name; time.tzset()
    _want = dt.datetime.now(_ZoneInfo(_tz_name)).utcoffset()
    assert dt.datetime.now().astimezone().utcoffset() == _want, f'TZ が {_tz_name} になっていない'
    # 海外タグ集合は学習に入る。無いと空集合で黙って順位が変わるので本番経路では止める (tests は fixture)。
    from spsp.common import OVERSEAS_JSON_PATH
    if not OVERSEAS_JSON_PATH.exists():
        raise SystemExit(f'ERROR: {OVERSEAS_JSON_PATH} が無い (build/build_overseas_json.py で先に生成する)')
    ap = argparse.ArgumentParser()
    ap.add_argument('eval_date', nargs='?', default=None,
                    help='YYYY-MM-DD (default: today)')
    ap.add_argument('out_dir', nargs='?', default='/tmp/spsp_v4_full',
                    help='output directory (default: /tmp/spsp_v4_full)')
    ap.add_argument('--frontend-src', default=str(REPO / 'site'),
                    help='frontend source dir')
    ap.add_argument('--light', action='store_true',
                    help='軽量ビルド: per-match 詳細を捨ててメモリを節約 (旧 SPSP_SKIP_PER_MATCH=1)')
    ap.add_argument('--keep-no-match', action='store_true',
                    help='使える試合が 0 件の大会も集計対象に残す (旧 SPSP_KEEP_NO_MATCH=1)')
    ap.add_argument('--state-in', default=None,
                    help='学習状態 (pickle) から再開する = 差分学習。以前の大会データが変わっていれば終了コード 3 で止まる')
    ap.add_argument('--state-out', default=None,
                    help='学習後の状態をここに保存 (次回の --state-in 用)')
    ap.add_argument('--until', default=None,
                    help='YYYY-MM-DD: この日までで学習を止めて --state-out に保存し終了 (inter は作らない)。'
                         '長い学習を日付で分割するため。--phase learn と併用')
    ap.add_argument('--save-at', default='',
                    help='"YYYY-MM-DD YYYY-MM-DD ..." (空白かカンマ区切り): --phase learn の途中、この日までを学習するたびに状態を '
                         '--save-at-template に保存して同じプロセスで続ける (区切りごとに別プロセスにするのと同じ結果で、大会の読み込みが 1 回で済む)')
    ap.add_argument('--save-at-template', default=None,
                    help='--save-at の保存先。{cut} が日付になる (例: learn/na_state_{cut}.pkl)')
    ap.add_argument('--save-at-keep', type=int, default=2,
                    help='--save-at で保存したもののうち新しい方から残す数 (0 = 全部残す。既定 2)')
    ap.add_argument('--trust-ledger', action='store_true',
                    help='--state-in の台帳と今のデータの照合を飛ばす。--state-in をこの実行の中で同じデータから作ったときだけ '
                         '(deploy/ci/build_na.sh の区切りごとの学習。照合は一致すれば何も変えないので結果は同じ)')
    ap.add_argument('--snapshot-grid', choices=('fixed', 'legacy'), default='fixed',
                    help='履歴スナップショットの点。fixed = 固定の暦日グリッド (本番)、legacy = 評価日から N 日前 (検証用)')
    ap.add_argument('--snap-cache', default=None,
                    help='スナップショット (SNAPSHOT_DAYS 時点のカスケード) のキャッシュ dir。同日の再実行で day 0 以外が当たる')
    ap.add_argument('--phase', choices=['all', 'learn', 'snap', 'build'], default='all',
                    help='all=単一プロセスで全部 (default). learn=学習して --state-out に保存. '
                         'snap=--state-in の学習状態からスナップショット等を作り inter を disk 退避. '
                         'build=退避済 inter を読み prod-build+overlay. '
                         'ConoHa WING の ~300s/プロセス CPU 制限回避のため本番は learn → snap → build の '
                         '3 プロセスに分ける (learn はさらに --until で日付分割できる).')
    args = ap.parse_args()

    eval_date_s = args.eval_date or dt.date.today().isoformat()
    eval_date = dt.datetime.fromisoformat(eval_date_s)
    # eval_date を日付のみ (= 00:00) で渡された場合は「その日の終わり (23:59:59)」扱いにする。
    # こうしないと until_ts = 当日 00:00 となり、当日 (= 開催日) に確定済みの大会が
    # data_loader の `ts >= until_ts` 判定で丸ごと除外されてしまう (= 最新大会がビルドに乗らない)。
    # snap day-0 / 減衰基準も当日終わりになり、当日大会が現在順位に正しく反映される。
    if eval_date.hour == 0 and eval_date.minute == 0 and eval_date.second == 0:
        eval_date = eval_date.replace(hour=23, minute=59, second=59)
    out_dir = Path(args.out_dir)
    frontend_src = Path(args.frontend_src)
    phase = args.phase
    if phase == 'learn' and not args.state_out:
        ap.error('--phase learn には --state-out が必要 (学習状態を保存し、--phase snap が読む)')
    if args.until and phase != 'learn':
        ap.error('--until は --phase learn と併用')
    save_at = [d for d in args.save_at.replace(',', ' ').split() if d]
    if save_at and (phase != 'learn' or not args.save_at_template or '{cut}' not in args.save_at_template):
        ap.error('--save-at は --phase learn と --save-at-template (中に {cut}) と併用')
    if phase == 'snap' and not args.state_in:
        ap.error('--phase snap には --state-in (eval_date まで学習済みの状態) が必要')
    global SETTINGS
    SETTINGS = Settings(skip_per_match=args.light, keep_no_match=args.keep_no_match)
    _apply_settings(SETTINGS, lock=True)

    t_total = time.time()

    _inter_path = out_dir / '_inter_full.pkl'

    if phase == 'build':
        # 退避済 inter を読み込んで prod-build + overlay のみ実行 (別プロセス).
        import pickle
        print(f"=== Phase build: loading inter from {_inter_path} ===", flush=True)
        with open(_inter_path, 'rb') as _f:
            inter = load_inter(_f)
        # 同じ日の BUILD_DIR を使い回すので、別の日付 / 別の設定で作った pkl を黙って使わない
        _stamp = inter.get('_learn_stamp') or {}
        _want = {'eval_date': eval_date.date().isoformat(), 'settings': SETTINGS.state_repr()}
        if {k: _stamp.get(k) for k in _want} != _want:
            raise SystemExit(f"ERROR: {_inter_path} は今回の learn の出力ではない (pkl: {_stamp}, 今回: {_want}) — --phase learn からやり直すこと")
        _rekey_idx_to_id(inter)  # index キー → id(t) キーへ復元
        print(f"  loaded inter ({time.time()-t_total:.0f}s)", flush=True)
        _run_build_and_overlay(inter, out_dir, eval_date, frontend_src, t_total)
        _inter_path.unlink(missing_ok=True)
        report_skips()
        print(f"\n=== Done (build phase) in {time.time()-t_total:.0f}s ===", flush=True)
        print(f"Site: {out_dir}", flush=True)
        return

    # Phase 1-5: V4 BT + cascade + per_tour_by_lv
    try:
        inter = compute_v4_intermediate(eval_date, state_in=args.state_in, state_out=args.state_out,
                                        until=args.until, learn_only=(phase == 'learn'),
                                        snap_only=(phase == 'snap'), snap_cache=args.snap_cache,
                                        snapshot_grid=args.snapshot_grid, trust_ledger=args.trust_ledger,
                                        save_at=save_at, save_at_template=args.save_at_template,
                                        save_at_keep=args.save_at_keep)
    except StaleCheckpoint as e:
        print(f"ERROR: 学習状態を使えない: {e}", flush=True)
        print(f"  → --state-in を外して (or 古い state から) 学習し直すこと (終了コード {EXIT_STALE})", flush=True)
        sys.exit(EXIT_STALE)
    if inter is None:
        report_skips()
        print(f"\n=== Done (learn{' until ' + args.until if args.until else ''}) in {time.time()-t_total:.0f}s ===", flush=True)
        print(f"Next: build_v4_full.py {eval_date_s} {out_dir} --phase snap --state-in {args.state_out}"
              if not args.until else f"Next: --phase learn --state-in {args.state_out} (続き) ...", flush=True)
        return

    if phase == 'snap':
        # inter を disk へ退避して終了 (= 次プロセスの --phase build が読む).
        import pickle
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"=== Phase snap: spilling inter to {_inter_path} ===", flush=True)
        _rekey_id_to_idx(inter)  # id(t) キー → index キー (pickle 越え対応)
        inter['_learn_stamp'] = {'eval_date': eval_date.date().isoformat(), 'settings': SETTINGS.state_repr()}
        # tmp に書いて rename: 途中で落ちても壊れた / 途中までの pkl が残らない (同日再実行で build が古い/壊れた学習結果を読まないため)
        _tmp = _inter_path.with_suffix('.pkl.tmp')
        with open(_tmp, 'wb') as _f:
            dump_inter(inter, _f)   # 値ごとに別の pickle (memo を小さく。spsp/learn_state.py)
            _f.flush(); os.fsync(_f.fileno())
        os.replace(_tmp, _inter_path)
        # 学習状態は inter に取り込んだので、BUILD_DIR の中にあるものは消す (rsync で配信されないように)
        _sp = Path(args.state_in).resolve()
        if _sp.is_relative_to(out_dir.resolve()):
            _sp.unlink(missing_ok=True)
        report_skips()
        print(f"\n=== Done (snap phase) in {time.time()-t_total:.0f}s ===", flush=True)
        print(f"Next: build_v4_full.py {eval_date_s} {out_dir} --phase build", flush=True)
        return

    # phase == 'all': 従来通り単一プロセスで全部
    _run_build_and_overlay(inter, out_dir, eval_date, frontend_src, t_total)
    elapsed = time.time() - t_total
    report_skips()
    print(f"\n=== Done in {elapsed:.0f}s ===", flush=True)
    print(f"Site: {out_dir}", flush=True)
    print(f"Build dir: {out_dir}", flush=True)


if __name__ == '__main__':
    main()

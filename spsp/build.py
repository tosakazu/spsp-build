"""V4 build orchestration.

run_build(cfg) が V4 full pipeline (precomputed BT 注入 → TJPR cascade → JSON 出力) を実行.

build_v4_full.main() が compute_v4_intermediate() で V4 single-pass を走らせた後、
inter dict を v4_bt_inject に渡して run_build() を呼ぶ.

注: v2 prod path (= 旧 build_tjpr_ranking CLI) は反復33 で削除済. v4_bt_inject /
precomputed_all_tournaments / precomputed_t_meta は必須.
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from spsp.common import t_passes  # noqa: F401 (学習側へ移した。spsp/common.py)
from spsp.common import (
    ELO_PER_UNIT, GATED_M_TOUR,
    LV1_PARAMS, LV2_PARAMS, SIGMA_FLOOR,
    TJPR_DEFAULT_RATE, TJPR_ELO_OFFSET, TJPR_ELO_SCALE,
    OVERSEAS_UIDS, jp_counter_rank, _bracket_order_key, uid_of,
    LV_TOPN_CAP, PEAK_ALLTIME_CUTOFF_DATE,
)
from spsp.learn import ContribTable, build_tjpr_v1_with_detail, precompute_banzuke
from spsp.meta import (
    SeedInfo, build_eid_to_parent_tid, build_multiday_max_day, build_placements_seeds,
    tournament_series, tournament_series_number,
)
from spsp.overlay import json_roundtrip
from spsp import tour_cache as _tour_cache
from spsp.columnar import (SHARED_UIDS, BtUsedView, GatedHist, MatchLog, MatchWins, PerTourByLv, PerTourLog,
                           PlayerMatches,
                           ScaledMap, TourHistory, TourMatchDeltas, TourUidTable, UidMap,
                           compact_uid_map)
from spsp.lazy_views import per_tour_fields, per_match_occurrences
from spsp.common import GLOBAL_SHIFT_FROZEN
from spsp.output import (
    build_and_write_all_player_records, build_and_write_all_tournament_docs,
    build_meta_dict, build_tournament_summary, run_aux_outputs, write_meta, set_light,
)
from spsp.score import (
    bt_to_elo_display, build_achievements, build_dynamic_badges, build_history,
    build_at_time_per_tournament, at_time_base_points,
    build_radar, build_rolling_rank_deltas, build_spr_uf_aggregates,
    normalize_bt, rank_avg, to_rank, TopUfs,
)

# 過去最高順位/最高到達Lv (all-time) の cutoff: EVO Japan 2020 (2020-01-24) 以降のみを参照.
PEAK_ALLTIME_CUTOFF = dt.datetime.combine(PEAK_ALLTIME_CUTOFF_DATE, dt.time())   # common と共有

# radar chart 用 clip 値 (集計値の上限 = グラフ max として固定)
RADAR_SPR_CLIP = 2.5
RADAR_UF_CLIP = 1.0


def _all_int_keys(d) -> bool:
    """bt_per_match のキー (eid, w, l) がすべて int (配列の表に入れられる) か。"""
    if isinstance(d, MatchLog):
        return True
    return all(type(e) is int and type(w) is int and type(l) is int for (e, w, l) in d)


def passes_level(level: int, nent: int, is_wk: bool, is_capped: bool = False) -> bool:
    """大会が指定 Lv の TJPR filter を通るか判定."""
    if level is None or level <= 0: return False
    if level >= 5: return (nent or 0) > 48 and is_wk and not is_capped
    if level == 4: return (nent or 0) > 24 and is_wk and not is_capped
    if level == 3: return (nent or 0) > 24
    return True  # Lv1/2: フィルタなし


@dataclass
class RunConfig:
    """run_build() の入力. V4 inject 専用 (v2 prod path は廃止)."""
    eval_date: dt.datetime
    out_path: Path
    meta_out_path: Path
    no_tournament_json: bool = False
    no_player_json: bool = False
    run_aux_outputs: bool = True    # 末尾で series.json / tournaments.json を site/data に生成するか (V4 本番は False = 別途 overlay)
    light: bool = False             # 軽量ビルド (旧 SPSP_SKIP_PER_MATCH=1): g2_timeline を作らず、書き出し後にページキャッシュを捨てる
    overlay: object | None = None   # v4.overlay.V4Overlay: 各 JSON を書く直前に V4 の順位評価/直対評価を反映する (V4 本番は必須)
    min_entrants_for_ranking: int = 8  # meta.json + tournament JSON 表示用 (= 計算側は t_meta 由来)

    # V4 注入 (必須).
    # キー:
    #   bt_at_t             : dict[id(t)] -> {uid: ord}
    #   bt_timeline         : dict[uid] -> [(ts, mu, sigma, ord)]
    #   bt_per_tour         : dict[(uid, eid)] -> {'bt_used', 'bt_d', 'bt_internal_d', 'after', 'peak_after'}
    #   bt_per_match        : dict[(eid, w, l)] -> [[d_w, d_l, t_w, t_l], ...] (試合進行順)
    #   peak180_at_target   : dict[snap_ts] -> {uid: ord}
    #   cascade_snapshots   : dict[day_str] -> {uid_str: [max_lv, ens_r]} (snapshots 復元用)
    #   eval_ts             : eval timestamp
    v4_bt_inject: dict | None = None

    # 動作環境 (必須). compute_v4_intermediate() で precompute 済の objects を渡す.
    events_root: Path | None = None
    tournament_display_name: Callable | None = None
    load_users: Callable | None = None
    format_display: Callable | None = None

    # 反復19: compute_v4_intermediate() で既にロード/フィルタ/メタ構築済の
    # tournaments・t_meta を必須として受け取る (二重 load 防止).
    # id(t) / id(meta) は同一 in-memory object を共有する前提.
    precomputed_all_tournaments: list | None = None
    precomputed_t_meta: dict | None = None


def _v4_adapt_bt_outputs(all_tournaments, t_meta_by_id, snap_ts_list, inject):
    """V4 single-pass の結果を v2 prod (learn_bt_gated_tour) と同じ shape に変換.

    旧 build_tjpr_ranking._v4_adapt_bt_outputs と同じ logic.

    返り値: v2 prod learn_bt_gated_tour と同じ 12 タプル.
    """
    bt_at_t_v4 = inject['bt_at_t']
    bt_timeline_v4 = inject['bt_timeline']
    bt_per_tour_v4 = inject['bt_per_tour']
    bt_per_match_v4 = inject['bt_per_match']
    peak180_at_target_v4 = inject['peak180_at_target']

    # eid -> tid mapping
    eid_to_tid: dict[int, int] = {}
    tid_to_ts: dict[int, int] = {}
    for t in all_tournaments:
        eid = t.event_id   # = attr.json の event_id (data_loader が読んだ値。読み直さない)
        if eid is not None:
            eid_to_tid[eid] = id(t)
        tid_to_ts[id(t)] = t.timestamp

    # 選手ごとのレート推移と「直対に使った大会」は bt_per_tour の配列の表から直接配列の表にする (以前と同じ並び・値。
    # 以前は uid ごとの list と dict を作ってから詰めていて、北米で 1 GB 近い一時的な Python のオブジェクトだった)
    fast_gated = (GatedHist.from_per_tour(bt_per_tour_v4, eid_to_tid, tid_to_ts)
                  if isinstance(bt_per_tour_v4, PerTourLog) else None)
    bt_used: dict[int, dict[int, bool]] = defaultdict(dict)
    rating_history: dict[int, list] = defaultdict(list)
    for (uid, eid), info in (() if fast_gated is not None else bt_per_tour_v4.items()):
        tid = eid_to_tid.get(eid)
        if tid is None: continue
        # info = 学習側の dict または snap が詰めた tuple (spsp/lazy_views.compact_inter_raw)
        i_used, i_internal_d, i_d, i_after, i_peak_after = per_tour_fields(info)
        bt_used[uid][tid] = bool(i_used)
        rating_history[uid].append((tid,
                                    float(i_after),
                                    float(i_internal_d),
                                    float(i_d),
                                    float(i_peak_after)))
    for uid in rating_history:
        rating_history[uid].sort(key=lambda e: tid_to_ts.get(e[0], 0))

    match_id_deltas: dict[int, dict] = {}
    # player_matches は配列の表 (spsp/columnar.PlayerMatches、match_wins はそこから引く)。ts・uid が int でないデータ
    # (古いキャッシュ・テスト用の入力) は以前の dict のまま
    _int_ok = all(type(t.timestamp) is int for t in all_tournaments) and _all_int_keys(bt_per_match_v4)
    pm_table = PlayerMatches() if _int_ok else None
    match_wins: dict[int, list] = defaultdict(list)
    player_matches: dict[int, list] = defaultdict(list)
    _eids = [t.event_id for t in all_tournaments if t.event_id is not None]
    streamed = (pm_table is not None and isinstance(bt_per_match_v4, MatchLog) and len(set(_eids)) == len(_eids))
    if streamed:
        # 大会ごとに試合を 1 回読み (spsp/lazy_matches で読み直すときも 1 大会 1 回)、その大会の試合の記録
        # (bt_per_match の event_id が同じ組) と突き合わせる。全大会の試合を同時に持たない。
        # 選手の対戦一覧の並びは、以前の「bt_per_match を記録の順に回す」順に戻す (seq = (組の位置 × 64 + 何回目か) × 2、
        # 敗者の行は +1: 同じ試合で勝者・敗者がどちらも初めて出るとき、以前と同じく勝者が先)
        rank = bt_per_match_v4.group_rank()
        match_id_deltas = TourMatchDeltas.empty()
        for t in all_tournaments:
            ms_t = list(t.matches)
            d_t: dict = {}
            eid = t.event_id
            groups = bt_per_match_v4.groups_with_first(eid) if eid is not None else {}
            if groups:
                tid = id(t)
                ts = tid_to_ts.get(tid, 0)
                # (w, l) → [Match, ...] 進行順 (= 学習側の記録順と同じ _bracket_order_key 順)。
                # 同一イベントで同方向の対戦が複数ある場合 (GF+リセット連取等) に occurrence を対応付ける。
                local: dict = {}
                for m in sorted(ms_t, key=_bracket_order_key):
                    local.setdefault((m.winner_id, m.loser_id), []).append(m)
                for (w_uid, l_uid), g in groups.items():
                    ms = local.get((w_uid, l_uid)) or []
                    occs = per_match_occurrences(bt_per_match_v4.value_of_group(g))
                    base = int(rank[g]) * 64
                    if len(occs) >= 64:
                        raise RuntimeError(f'_v4_adapt_bt_outputs: 同じ組の試合が多すぎる ({eid}, {w_uid}, {l_uid})')
                    for i, info in enumerate(occs):
                        m = ms[i] if i < len(ms) else (ms[-1] if ms else None)
                        d_w, d_l, t_w, t_l = float(info[0]), float(info[1]), bool(info[2]), bool(info[3])
                        is_cls = bool(getattr(m, 'is_class', False)) if m is not None else False
                        if m is not None:
                            d_t[id(m)] = (max(0.0, d_w), min(0.0, d_l), t_w, t_l)
                        mi = pm_table.add_match(m)
                        pm_table.add(w_uid, ts, tid, l_uid, True,  max(0.0, d_w), is_cls, mi, t_w, seq=2 * (base + i))
                        pm_table.add(l_uid, ts, tid, w_uid, False, min(0.0, d_l), is_cls, mi, t_l, seq=2 * (base + i) + 1)
            match_id_deltas.add_tournament(t, ms_t, d_t)
        del rank
    else:
        # (eid, w, l) → [Match, ...] 進行順 (= 学習側の記録順と同じ _bracket_order_key 順)。
        # 同一イベントで同方向の対戦が複数ある場合 (GF+リセット連取等) に occurrence を対応付ける。
        eid_match_map: dict[tuple[int, int, int], list] = {}
        # 大会ごとの試合の list (t.matches が spsp/lazy_matches の読み直しでも、ここと TourMatchDeltas で同じオブジェクトを使う。
        # この関数の中だけで持つ)
        mats = {id(t): list(t.matches) for t in all_tournaments}
        for t in all_tournaments:
            eid = t.event_id
            if eid is None: continue
            for m in sorted(mats[id(t)], key=_bracket_order_key):
                eid_match_map.setdefault((eid, m.winner_id, m.loser_id), []).append(m)
        for (eid, w_uid, l_uid), occs in bt_per_match_v4.items():
            tid = eid_to_tid.get(eid)
            if tid is None: continue
            ts = tid_to_ts.get(tid, 0)
            ms = eid_match_map.get((eid, w_uid, l_uid)) or []
            # 旧 flat 形式 (per-occurrence 化前のキャッシュ) と snap が詰めた 1 試合の tuple は 1 試合に包む
            occs = per_match_occurrences(occs)
            for i, info in enumerate(occs):
                m = ms[i] if i < len(ms) else (ms[-1] if ms else None)
                d_w, d_l, t_w, t_l = float(info[0]), float(info[1]), bool(info[2]), bool(info[3])
                is_cls = bool(getattr(m, 'is_class', False)) if m is not None else False
                # (d_w, d_l, w_tracked, l_tracked)。以前は同じ 4 つの dict (北米で試合 数百万件ぶん)。読むのは
                # output._build_tour_matches_out だけ
                match_id_deltas[id(m) if m is not None else (eid, w_uid, l_uid, i)] = (
                    max(0.0, d_w), min(0.0, d_l), t_w, t_l)
                if pm_table is not None:
                    mi = pm_table.add_match(m)
                    pm_table.add(w_uid, ts, tid, l_uid, True,  max(0.0, d_w), is_cls, mi, t_w)
                    pm_table.add(l_uid, ts, tid, w_uid, False, min(0.0, d_l), is_cls, mi, t_l)
                    continue
                if d_w > 0:
                    match_wins[w_uid].append((tid, l_uid, d_w, m))
                player_matches[w_uid].append((ts, tid, l_uid, True,  max(0.0, d_w), is_cls, m, t_w))
                player_matches[l_uid].append((ts, tid, w_uid, False, min(0.0, d_l), is_cls, m, t_l))
    if pm_table is not None:
        player_matches, match_wins = pm_table, MatchWins(pm_table)
    print(f"  player_matches: {'配列の表' if pm_table is not None else 'dict'} ({len(player_matches)} players)", flush=True)

    tour_count: dict[int, int] = defaultdict(int)
    for t in all_tournaments:
        meta = t_meta_by_id[id(t)]
        if meta.get('is_non_serious') or meta.get('is_gf_missing') or meta.get('is_class_virtual'):
            continue
        for _, uid in t.standings:
            tour_count[uid] += 1

    bt_current_ord: dict[int, float] = {}
    for uid, hist in rating_history.items():
        if hist:
            bt_current_ord[uid] = hist[-1][1]
    if fast_gated is not None:
        gated, bt_current_ord = fast_gated
        rating_history, bt_used = gated, BtUsedView(gated)
        del fast_gated
    # rating_history・bt_used・match_id_deltas は以後読むだけなので配列の表に (spsp/columnar、引いた値は同じ)。
    # uid が int でないデータ (テスト用の入力) は dict のまま
    if type(rating_history) is not GatedHist and all(type(u) is int for u in rating_history):
        gated = GatedHist()
        for uid, hist in rating_history.items():
            used = bt_used[uid]
            for e in hist:
                gated.add(uid, *e, 1 if used[e[0]] else 0)
        rating_history, bt_used = gated, BtUsedView(gated)
    if not streamed:
        if pm_table is not None:
            match_id_deltas = TourMatchDeltas(match_id_deltas, all_tournaments, mats)
        del mats, eid_match_map
    if peak180_at_target_v4:
        max_ts = max(peak180_at_target_v4.keys())
        bt_peak180_ord = dict(peak180_at_target_v4[max_ts])
    else:
        bt_peak180_ord = {}

    bt_internal_at_snap = peak180_at_target_v4 if snap_ts_list else None
    peak180_at_snap = peak180_at_target_v4 if snap_ts_list else None

    return (
        bt_current_ord, bt_peak180_ord, bt_used, rating_history, tour_count,
        match_wins, player_matches, (dict(bt_timeline_v4) if isinstance(bt_timeline_v4, dict) else bt_timeline_v4),
        match_id_deltas, bt_at_t_v4,
        bt_internal_at_snap, peak180_at_snap,
    )


def _int_items(m) -> list:
    """{str(uid): 値} の表の [(int(uid), 値), ...] (uid_of(u) と同じ値、並びは同じ)。spsp/columnar.StrUidTable なら
    文字列を作らずに番号から引く。"""
    f = getattr(m, 'int_items', None)
    return f() if f is not None else [(uid_of(u), v) for u, v in m.items()]


def _same_dict(own: dict, other) -> dict:
    """other (overlay が作った同じ意味の表) が own とキー・値・並びまで同じなら other を返す (own は捨てる)。
    違えば own。どちらも以後は読むだけなので、同じなら 1 つで足りる (出力は同じ)。"""
    if not isinstance(other, (dict, UidMap)) or len(other) != len(own):
        return own
    # キーの並びと値を別々に比べる (items の tuple を作らない。比べ方は items の比較と同じ)
    if list(other.keys()) == list(own.keys()) and list(other.values()) == list(own.values()):
        return other
    return own


def _build_snapshots_from_v4_cascade(inject: dict, shared_tables: dict | None = None) -> dict:
    """V4 cascade_snapshots + peak180_at_day から snapshots[days] を構築.

    no_snapshots=True で v2 prod の snapshot loop を skip する代わりに、V4 single-pass
    で既に持っている cascade_snapshots (= ens-LV pure cascade) と peak180_at_day から
    build_history / build_achievements が読む snapshots dict を直接 populate する.

    cascade_snapshots schema: {day_str: {uid_str: [max_lv, ens_r, tjpr_score]}}
    peak180_at_day schema:    {day_str: {uid_str: peak180_ord}}

    populate するフィールド:
      ensemble_ranks  : ens_r (cascade のグローバル ens rank)
      tjpr_ranks      : tjpr_score 降順の rank
      bt_gated_ranks  : peak180 降順の rank
      tjpr_levels     : max_lv
      tjpr_scores     : tjpr_score (V4 cascade sum at snap)
      tjpr_elo        : tjpr_score × TJPR_ELO_SCALE (= 0.35). overlay 側で ×50 されて
                        最終 ×17.5 になる (= math.html 記載スケール).
      bt_gated_elo    : peak180_ord × ELO_PER_UNIT
      bt_internal_elo : peak180_ord × ELO_PER_UNIT (V4 では internal = peak180 を表示)
      bt_flat_elo / jjpr_*: V4 不使用、空 dict.
    """
    cascade_snapshots = inject['cascade_snapshots']
    peak180_at_day = inject.get('peak180_at_day', {})
    snap_days = inject['snap_days']
    # 計測中 (= 灰色) 判定用: {day(int): 計測済み uid set}. 無ければ従来通り海外のみ.
    measured_at_day = inject.get('measured_at_day') or {}

    snapshots = {}
    for d in snap_days:
        if d <= 0:
            continue
        snap = cascade_snapshots.get(str(d), {})
        pd_dict = peak180_at_day.get(str(d), {})
        measured_d = measured_at_day.get(d)

        # cascade ens rank: sort by (-max_lv, ens_r) ※ ens_r は raw (= ens_score 順)
        # uid は uid_of で日をまたいで同じ int オブジェクトにする (値は int(u) と同じ。北米 20 万人 × 60 日)
        items = [(u, v[0], v[1], v[2] if len(v) >= 3 else 0.0) for u, v in _int_items(snap)]
        items.sort(key=lambda x: (-x[1], x[2]))
        ens_ranks_raw = {u: i + 1 for i, (u, _, _, _) in enumerate(items)}
        # history / snapshot で表示される rank は JP-only counter に揃える
        # (= 海外勢・計測中が押し下げない. 最新と history で同じ jp-counter 表記になる).
        ens_ranks = jp_counter_rank(ens_ranks_raw, OVERSEAS_UIDS, measured_uids=measured_d)
        # 灰色の総合順位は所属 Lv の枠サイズでクランプ
        # (= Lv(n) 選抜済み灰色が再計算で境界をはみ出す「Lv2 なのに 2049 位」を防ぐ).
        _lv_cap = LV_TOPN_CAP
        for u, lv, _, _ in items:
            gray = u in OVERSEAS_UIDS or (measured_d is not None and u not in measured_d)
            if not gray:
                continue
            cap = _lv_cap.get(lv)
            if cap and ens_ranks.get(u, 0) > cap:
                ens_ranks[u] = cap
        tjpr_levels_d = {u: lv for u, lv, _, _ in items}
        tjpr_scores_d = {u: s for u, _, _, s in items}
        # tjpr_rank = score 降順. tie-break = uid (= score=0 同点ブロックでも
        # snapshot 間で deterministic. ens_r tiebreak は内部 cascade が unstable な
        # ケースで循環するので避ける).
        tjpr_sorted = sorted(items, key=lambda x: (-x[3], x[0]))
        tjpr_ranks_raw = {u: i + 1 for i, (u, _, _, _) in enumerate(tjpr_sorted)}
        tjpr_ranks_d = jp_counter_rank(tjpr_ranks_raw, OVERSEAS_UIDS, measured_uids=measured_d)
        # tjpr_elo (tjpr_scores × TJPR_ELO_SCALE (0.35) のみ。overlay 側で ×50 されて最終 ×17.5 になる) は下で作る

        # BT rank: peak180 降順
        pd_items = _int_items(pd_dict)
        sorted_uids = sorted(pd_items, key=lambda x: -float(x[1]))
        bt_ranks_raw = {u: i + 1 for i, (u, _) in enumerate(sorted_uids)}
        bt_ranks = jp_counter_rank(bt_ranks_raw, OVERSEAS_UIDS, measured_uids=measured_d)
        # bt_gated_elo = peak180_ord × ELO_PER_UNIT (top-level scores と同じ式).
        bt_gated_elo_d = {u: float(o) * ELO_PER_UNIT for u, o in pd_items}
        # V4 では bt_internal も peak180 ベースで表示 (= top-level の bt_internal_elo に追随).
        # 中身は bt_gated_elo と同じ。以前はコピーしていた (どちらも読むだけなので同じ dict を指す。北米で 約 0.5 GB)
        bt_internal_elo_d = bt_gated_elo_d

        if shared_tables:
            ens_ranks = _same_dict(ens_ranks, shared_tables.get('ensemble_ranks', {}).get(d))
            tjpr_ranks_d = _same_dict(tjpr_ranks_d, shared_tables.get('tjpr_ranks', {}).get(d))
            bt_ranks = _same_dict(bt_ranks, shared_tables.get('bt_gated_ranks', {}).get(d))
        # 日ごとの表は読むだけの配列の表に (spsp/columnar.UidMap。overlay と同じものはそのまま)
        ens_ranks, tjpr_ranks_d, bt_ranks, tjpr_levels_d, tjpr_scores_d, bt_gated_elo_d = (
            compact_uid_map(x, SHARED_UIDS) for x in
            (ens_ranks, tjpr_ranks_d, bt_ranks, tjpr_levels_d, tjpr_scores_d, bt_gated_elo_d))
        # tjpr_elo は tjpr_scores × TJPR_ELO_SCALE を引くたびに計算する (spsp/columnar.ScaledMap、値は同じ)
        if all(type(s) is float for s in tjpr_scores_d.values()):
            tjpr_elo_d = ScaledMap(tjpr_scores_d, TJPR_ELO_SCALE)
        else:
            tjpr_elo_d = compact_uid_map({u: s * TJPR_ELO_SCALE for u, s in tjpr_scores_d.items()}, SHARED_UIDS)
        bt_internal_elo_d = bt_gated_elo_d
        snapshots[d] = {
            'ensemble_ranks': ens_ranks,
            'tjpr_ranks': tjpr_ranks_d,
            'bt_gated_ranks': bt_ranks,
            'tjpr_levels': tjpr_levels_d,
            'tjpr_scores': tjpr_scores_d,
            'tjpr_elo': tjpr_elo_d,
            'bt_gated_elo': bt_gated_elo_d,
            'bt_internal_elo': bt_internal_elo_d,
            'bt_flat_elo': {},
            'jjpr_ranks': {},
            'jjpr_scores': {},
        }
    return snapshots


def _adapt_v4_bt_inject(cfg, all_tournaments, t_meta_by_id, t0):
    """V4 single-pass の結果を v2 互換 BT 構造に変換 (旧 v2 prod BT-all + BT-gated 互換).

    Returns 15-tuple: bt_all_ord, bt_all_hist, bt_all_at_snap (3 空 placeholder for compat) +
        12-tuple from _v4_adapt_bt_outputs.
    """
    inject = cfg.v4_bt_inject
    print("[V4 BT INJECT] Adapting V4 single-pass results to v2-compat BT structure ...", flush=True)
    bt_all_ord = {}
    bt_all_hist = defaultdict(list)
    bt_all_at_snap = None  # snapshots は V4 cascade から復元するので不要
    gated = _v4_adapt_bt_outputs(all_tournaments, t_meta_by_id, None, inject)
    bt_peak180_ord = gated[1]
    print(f"  V4 BT adapter: {len(bt_peak180_ord)} players (elapsed {time.time()-t0:.0f}s)", flush=True)
    return (bt_all_ord, bt_all_hist, bt_all_at_snap, *gated)


def _build_per_tour_by_lv(tjpr_level_contribs):
    """tjpr_level_contribs から per_tour_by_lv[uid][tid][lv] = [raw, pos, w, counted] を構築."""
    thresh = [LV2_PARAMS[2] if lv >= 2 else LV1_PARAMS[2] for lv in range(1, len(tjpr_level_contribs) + 1)]
    # Lv ごとの寄与が配列の表 (spsp/learn.ContribTable) なら、3 段の dict を作らずに直接配列の表にする (中身は同じ)
    if all(type(t) is ContribTable for t in tjpr_level_contribs):
        table = PerTourByLv.from_contrib_tables(tjpr_level_contribs, thresh)
        if table is not None:
            n_tours, n_players = table.n_tours_players()
            print(f"  per_tour_by_lv: {n_tours} tour entries across {n_players} players", flush=True)
            print("  per_tour_by_lv: 配列の表", flush=True)
            return table
    per_tour_by_lv = defaultdict(lambda: defaultdict(dict))
    for lv_idx, contribs_by_uid in enumerate(tjpr_level_contribs):
        lv = lv_idx + 1
        counted_thresh = LV2_PARAMS[2] if lv >= 2 else LV1_PARAMS[2]
        for uid, contribs in contribs_by_uid.items():
            for c in contribs:
                per_tour_by_lv[uid][c['t_id']][lv] = [
                    round(c['raw_points'], 4),
                    round(c['pos_factor'], 6),
                    round(c['weighted'], 4),
                    int(c['rank_in_player'] < counted_thresh),
                ]
    print(f"  per_tour_by_lv: {sum(len(t) for t in per_tour_by_lv.values())} tour entries across {len(per_tour_by_lv)} players", flush=True)
    # 以後は読むだけなので配列の表に (spsp/columnar.PerTourByLv、引いた値と並びは同じ)。表せないもの (int でないキー) は dict のまま
    table = PerTourByLv.from_nested(per_tour_by_lv)
    print(f"  per_tour_by_lv: {'配列の表' if table is not None else 'dict (配列の表にできない値がある)'}", flush=True)
    return per_tour_by_lv if table is None else table


def _compute_snapshots_from_inject(cfg):
    """V4 cascade ベースで snapshots を populate.

    build_history が空 list を返さず、build_achievements の peak tier (= history からの
    best_ens / best_lv) が postprocess の peak_ranks と内部整合する.
    """
    if 'cascade_snapshots' not in cfg.v4_bt_inject:
        return {}
    # overlay (V4Overlay.player) が同じ日の順位表を持っていれば、同じ中身のものは使い回す
    _shared = getattr(getattr(cfg.overlay, 'player', None), 'rank_tables', None) if cfg.overlay is not None else None
    snapshots = _build_snapshots_from_v4_cascade(cfg.v4_bt_inject, shared_tables=_shared)
    print(f"  populated {len(snapshots)} snap days from V4 cascade", flush=True)
    return snapshots


def run_build(cfg: RunConfig) -> int:
    """V4 full pipeline. Returns: written player count.

    cfg.v4_bt_inject + precomputed_all_tournaments + precomputed_t_meta は必須.
    """
    eval_date = cfg.eval_date
    set_light(cfg.light)
    print(f"eval_date = {eval_date.date()}", flush=True)
    PEAK_ALLTIME_MAX_DAYS = (eval_date - PEAK_ALLTIME_CUTOFF).days

    for attr_name in ('events_root', 'tournament_display_name', 'load_users', 'format_display',
                      'v4_bt_inject', 'precomputed_all_tournaments', 'precomputed_t_meta'):
        if getattr(cfg, attr_name) is None:
            raise ValueError(f"RunConfig.{attr_name} is required")
    load_users = cfg.load_users
    format_display = cfg.format_display

    t0 = time.time()
    all_tournaments = cfg.precomputed_all_tournaments
    print(f"[V4 INJECT] Reusing {len(all_tournaments)} precomputed tournaments", flush=True)
    # 試合が少ない地域は、手放した試合をここで 1 回ずつ作り直して持ち続ける (spsp/lazy_matches.keep_if_small)
    from spsp import lazy_matches
    if lazy_matches.ENABLED and lazy_matches.keep_if_small(all_tournaments):
        print(f"  lazy matches: 試合が少ない (全部で {lazy_matches.KEEP_BELOW:,} 未満) ので持ち続ける", flush=True)

    t_meta_by_id = cfg.precomputed_t_meta
    print(f"[V4 INJECT] Reusing precomputed t_meta ({len(t_meta_by_id)} entries)", flush=True)

    placements_per_t, t_seed_info = build_placements_seeds(all_tournaments)
    per_tournament_max_day, per_tournament_total_days = build_multiday_max_day(
        all_tournaments, t_meta_by_id)
    eid_to_parent_tid = build_eid_to_parent_tid(t_meta_by_id)

    (bt_all_ord, bt_all_hist, bt_all_at_snap,
     bt_current_ord, bt_peak180_ord, bt_used, bt_gated_hist, tour_count,
     match_wins, player_matches, bt_timeline, match_id_deltas, bt_at_t,
     bt_internal_at_snap, peak180_at_snap) = _adapt_v4_bt_inject(
        cfg, all_tournaments, t_meta_by_id, t0)
    # ランダムシード大会 (シード順と大会時点の総合評価の順位の相関が低い) は UF/SPR の集計から外す
    from spsp.random_seed import mark_random_seed
    _rs = mark_random_seed(all_tournaments, bt_at_t, t_meta_by_id,
                           cfg.v4_bt_inject.get('pt_at_time') if cfg.v4_bt_inject else None)
    for t in _rs:
        per = t_seed_info.get(id(t), {})
        for u, info in per.items():   # 値の差し替え (キーの並びはそのまま)
            per[u] = info.with_spr_none() if hasattr(info, 'with_spr_none') else {**info, 'spr': None}
    print(f"  random seed: {len(_rs)} events (UF/SPR excluded)", flush=True)
    # 以後は読むだけなので、大会 × 参加者の表を 1 本の配列の表に (spsp/columnar.TourUidTable、引いた値と並びは同じ)。
    # 表せない大会 (int でない値) はその大会だけ dict のまま
    placements_per_t = TourUidTable.compact(placements_per_t)
    t_seed_info = TourUidTable.compact(t_seed_info, 'iii', SeedInfo)
    print(f"  placements / seed info: 配列の表 (dict のままの大会 {placements_per_t.n_odd} / {t_seed_info.n_odd})", flush=True)
    norm_bt = normalize_bt(bt_all_ord)

    # V4 では BT timeline をそのまま g2 timeline として再利用 (= μ/σ 同一).
    # 軽量ビルド (cfg.light) では g2_timeline は V4 フロント未使用なので
    # bt_timeline のフルコピーを作らずスキップ (ピークメモリ削減).
    if cfg.light:
        _g2_timeline = {}
    else:
        # 引くたびに作る (以前は全選手ぶんのコピーを先に作っていた。北米で 約 0.5 GB。値は同じ)
        from spsp.lazy_views import G2TimelineView
        _g2_timeline = G2TimelineView(bt_timeline)
    bt_gated_peak180_g2 = bt_peak180_ord
    print(f"  Glicko2 bt_gated (reused from V4): {len(bt_gated_peak180_g2)} players (elapsed {time.time()-t0:.0f}s)", flush=True)

    # 直対評価スコア = peak180.
    bt_gated_ord = bt_peak180_ord
    bt_internal_ord = bt_current_ord
    # 順位評価の表 (cache) の正規化シフト。以前は「その時点の全選手の最大内部レート × ELO_PER_UNIT − 2500」で、
    # トップ選手のレートが動くたびに過去の全大会の獲得点 (tjpr_pts_at 等の表示値) が揺れていた (順位には影響しない:
    # 学習側のカスケードは shift 0 で計算している)。2026-09-07 に 2026-09-05 ビルドの値で凍結 (ユーザー決定)。
    # 来季に 0 へ統一する予定 (表示値が変わる: tjpr_pts_at / SPR 一覧の tjpr_raw / レーダー tjpr_pt / V1 tjpr_level)。
    global_shift = GLOBAL_SHIFT_FROZEN   # v4/common.py
    norm_bt_gated = normalize_bt(bt_internal_ord)
    print(f"  {len(bt_gated_ord)} players (elapsed {time.time()-t0:.0f}s)", flush=True)

    print("Building TJPR v1 Banzuke (using per-tournament BT snapshot for fixed raw_pts) ...", flush=True)
    cache = precompute_banzuke(all_tournaments, eval_date, bt_at_t, global_shift, t_meta_by_id, TJPR_DEFAULT_RATE)
    # 計測中 (= 2年3大会未満) を topn フィルタの JP カウント対象外にする (day 0).
    measured_day0 = (cfg.v4_bt_inject.get('measured_at_day') or {}).get(0)
    tjpr_ranks, tjpr_levels, tjpr_scores, tjpr_contribs, tjpr_level_scores, tjpr_lv1_contribs, tjpr_level_contribs = build_tjpr_v1_with_detail(cache, measured_uids=measured_day0)
    print(f"  {len(tjpr_ranks)} players (elapsed {time.time()-t0:.0f}s)", flush=True)

    per_tour_by_lv = _build_per_tour_by_lv(tjpr_level_contribs)

    # JJPR は V4 では未使用 (no_jjpr_output=True hardcoded).
    jjpr_ranks, jjpr_scores, jjpr_contribs = {}, {}, {}

    tjpr_elo = {uid: s * TJPR_ELO_SCALE + TJPR_ELO_OFFSET for uid, s in tjpr_scores.items()}
    norm_bt_gated_disp = bt_to_elo_display(bt_gated_ord)
    norm_bt_internal_disp = bt_to_elo_display(bt_internal_ord)
    norm_bt_disp = bt_to_elo_display(bt_all_ord)
    max_tjpr = max(tjpr_scores.values()) if tjpr_scores else 1.0
    print(f"  TJPR display Elo: score × {TJPR_ELO_SCALE} (max raw score = {max_tjpr:.2f} → display {max_tjpr*TJPR_ELO_SCALE:.0f})", flush=True)

    print("Computing ensemble rank (tie-break by avg display score) ...", flush=True)
    bt_gated_ranks = to_rank(bt_gated_ord)
    all_uids = set(tjpr_ranks) | set(bt_gated_ranks)
    ensemble_ranks, ensemble_avg_rank, ensemble_avg_score = rank_avg(
        [tjpr_ranks, bt_gated_ranks], all_uids,
        score_lists=[tjpr_elo, norm_bt_gated_disp], return_metrics=True)
    bt_all_ranks = to_rank(bt_all_ord)

    rolling_deltas = (cfg.v4_bt_inject or {}).get('rolling_deltas')
    if rolling_deltas is None:
        rolling_deltas = build_rolling_rank_deltas(
            all_tournaments, cache, t_meta_by_id, placements_per_t,
            start_time=t0,
        )
    else:
        print(f"  rolling delta: 学習側の増分結果を使用 ({len(rolling_deltas)} tournaments)", flush=True)
    # 大会ごとの {uid: raw_at} は読むだけなので配列の表に (TourUidTable、値と並びは同じ)。inject の dict も差し替えて手放す
    rolling_deltas = TourUidTable.compact(rolling_deltas, 'd')
    print(f"  rolling delta: 配列の表 (dict のままの大会 {rolling_deltas.n_odd})", flush=True)
    if cfg.v4_bt_inject is not None and 'rolling_deltas' in cfg.v4_bt_inject:
        cfg.v4_bt_inject['rolling_deltas'] = rolling_deltas
    # banzuke cache はここ (rolling_deltas) が最後の使用。下流の doc 書き出し
    # (build_and_write_all_player_records / _tournament_docs) は cache を読まないので
    # ピーク前に解放する。出力不変 (= dead data の解放のみ)。
    import gc as _gc
    del cache
    _gc.collect()

    # 直近 1 年窓の max_uf/spr/losers_run も並行集計 (= プレイヤーページのハイライト用)
    _spr_uf = build_spr_uf_aggregates(
        all_tournaments, placements_per_t, t_seed_info, t_meta_by_id,
        recent_cutoff_ts=int((eval_date - dt.timedelta(days=365)).timestamp()))
    player_max_uf = _spr_uf['player_max_uf']
    # 番狂わせは選手ページに出す上位だけを小さい組で持つ (spsp/score.TopUfs、出力は同じ)
    player_top_ufs = TopUfs(_spr_uf['player_top_ufs'])
    player_match_count = _spr_uf['player_match_count']
    player_max_uf_ach = _spr_uf['player_max_uf_ach']
    player_max_spr_ach = _spr_uf['player_max_spr_ach']
    player_min_spr_ach = _spr_uf['player_min_spr_ach']
    player_max_losers_run = _spr_uf['player_max_losers_run']
    player_max_uf_1y = _spr_uf['player_max_uf_1y']
    player_max_spr_1y = _spr_uf['player_max_spr_1y']
    player_min_spr_1y = _spr_uf['player_min_spr_1y']
    player_max_losers_run_1y = _spr_uf['player_max_losers_run_1y']
    per_tournament_losers_runs = _spr_uf['per_tournament_losers_runs']
    player_tour_count_2024 = _spr_uf['player_tour_count_2024']
    player_match_count_2024 = _spr_uf['player_match_count_2024']
    player_win_count_2024 = _spr_uf['player_win_count_2024']
    player_vs_opp_count = _spr_uf['player_vs_opp_count']
    player_tour_history = _spr_uf['player_tour_history']
    per_tournament_wins_class = _spr_uf['per_tournament_wins_class']
    per_tournament_losses_class = _spr_uf['per_tournament_losses_class']
    per_tournament_wins_main = _spr_uf['per_tournament_wins_main']
    per_tournament_losses_main = _spr_uf['per_tournament_losses_main']
    # 失格者の集合は大会ごとに空の set を持っていた (北米で 16 万個)。読む側は .get(tid, set()) なので空は持たない (結果は同じ)
    per_tournament_dq_uids_main = {k: v for k, v in _spr_uf['per_tournament_dq_uids_main'].items() if v}
    per_tournament_dq_uids_class = {k: v for k, v in _spr_uf['per_tournament_dq_uids_class'].items() if v}
    player_first_tour = _spr_uf['player_first_tour']
    _match_extra_uids = _spr_uf['match_extra_uids']   # build_at_time_per_tournament だけが使う
    # 残り (per_tournament_wins / losses / match_count / dq_uids の全体版・uf_tour_count) は以後読まないので手放す
    # (北米で 数百 MB。以前は _spr_uf ごと build の最後まで残っていた)
    del _spr_uf
    # 大会ごとの勝ち・負けの数は以後読むだけなので配列の表に (spsp/columnar.TourUidTable、引いた値・合計は同じ)
    (per_tournament_wins_main, per_tournament_losses_main, per_tournament_wins_class, per_tournament_losses_class) = (
        TourUidTable.compact(x, 'i', totals=True) for x in (per_tournament_wins_main, per_tournament_losses_main,
                                                             per_tournament_wins_class, per_tournament_losses_class))
    # 同一相手との対戦数は「一番多い相手の数」しか読まない (spsp/score._activity_achievements) ので最大値だけ残す
    player_vs_opp_count = player_vs_opp_count.max_per_player()
    # 選手ごとの出場 [(ts, tid), ...] は以後読むだけなので配列の表に (spsp/columnar.TourHistory、引いた値は同じ。
    # 集計 (spsp/score.build_spr_uf_aggregates) が足しながら作る。int でないものがあって dict のときはここで)
    if type(player_tour_history) is not TourHistory and all(type(u) is int and all(type(ts) is int and type(tid) is int for ts, tid in h)
           for u, h in player_tour_history.items()):
        player_tour_history = TourHistory.from_dict(player_tour_history) or player_tour_history

    snapshots = _compute_snapshots_from_inject(cfg)

    # 各 (大会, 選手) の「当時の」レベル/全国順位を実 cascade スナップショットから引く.
    # (rolling は実 cascade と大きくずれるため、display anchor / 当時レベルはこちらを使う.)
    # per-tour 厳密アンカー (= 各大会直前の実カスケード). あれば最近傍スナップショット近似の
    # 代わりに「当時のレベル/全国#/各順位」アンカーに使う (本番非破壊: None なら従来通り).
    _pt_at_time = cfg.v4_bt_inject.get('pt_at_time') if cfg.v4_bt_inject else None
    at_time_map = build_at_time_per_tournament(
        all_tournaments, t_meta_by_id, placements_per_t,
        snapshots, sorted(snapshots.keys()), int(eval_date.timestamp()),
        ensemble_ranks, tjpr_levels,
        rolling_deltas, bt_gated_hist,
        pt_at_time=_pt_at_time,
        measured_at_day=(cfg.v4_bt_inject.get('measured_at_day') if cfg.v4_bt_inject else None),
        match_extra_uids=_match_extra_uids,
    )
    del _match_extra_uids
    # 大会ページのポイントのベースは当時のレベル・当時のレートで (bt_at_t を手放す前に作る。spsp/score.at_time_base_points)
    _t_base = time.time()
    at_time_base = at_time_base_points(all_tournaments, t_meta_by_id, bt_at_t, at_time_map, eval_date)
    print(f"  at-time base points: {len(at_time_base)} tournaments ({time.time() - _t_base:.1f}s)", flush=True)
    # 以後 (選手・大会の書き出し) は使わない: 大会開始時点のレート (bt_at_t) と大会ごとの厳密アンカー (pt_at_time)。
    # 書き出しの間ずっと持っていた (北米で 約 1 GB。2026-10-17)。呼び出し側 (build_full.run_prod_build_with_v4_inject)
    # は inter から外して inject にだけ入れて渡すので、ここで inject の参照を外せば解放される。
    del _pt_at_time, bt_at_t
    if cfg.v4_bt_inject:
        cfg.v4_bt_inject['pt_at_time'] = None
        cfg.v4_bt_inject['bt_at_t'] = None

    print("\nLoading user names ...", flush=True)
    # 選手の記録のうち build が読むのは表示名 (spsp/tournament_utils.format_display) と discriminator だけ
    # (spsp/output.py)。他の項目 (国・都市・SNS 等) は持たない (北米で 25 万人、約 0.3 GB)。値は同じ
    users = {u: {'gamer_tag': d.get('gamer_tag'), 'prefix': d.get('prefix'),
                 'startgg_discriminator': d.get('startgg_discriminator')} for u, d in load_users().items()}

    tournament_summary = build_tournament_summary(t_meta_by_id)

    spr_upper_val, spr_neg_val, uf_upper_val, uf_lost_val, pct = build_radar(
        player_tour_history,
        ensemble_ranks,
        tjpr_levels,
        t_meta_by_id,
        t_seed_info,
        player_matches,
        tjpr_elo,
        norm_bt_gated_disp,
        passes_level,
    )

    print("Building per-player JSONL + per-player JSON files ...", flush=True)
    out_path = cfg.out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    players_dir = out_path.parent / "players"
    tournaments_dir = out_path.parent / "tournaments"
    if not cfg.no_player_json:
        if players_dir.exists(): shutil.rmtree(players_dir)
    if not cfg.no_tournament_json and not _tour_cache.enabled():   # 大会ファイルを使い回すときは spsp/tour_cache.py が消す
        if tournaments_dir.exists(): shutil.rmtree(tournaments_dir)
    players_dir.mkdir(parents=True, exist_ok=True)
    tournaments_dir.mkdir(parents=True, exist_ok=True)

    # 全大会の試合を回す集計はここまで (選手の書き出しは試合を読まない。メモリのピークは
    # 選手の書き出しの間)。以後 t.matches は大会ページを組み立てるときに大会ごとに作り直す (spsp/lazy_matches。
    # 普通は spsp/cli/build_full が読み込んだ直後に手放しているので何もしない)
    from spsp import lazy_matches
    if lazy_matches.ENABLED:
        _n = lazy_matches.release(all_tournaments)
        print(f"  lazy matches: {_n} 試合を手放した (以後は大会ごとに読み直す)", flush=True)
    written, per_tournament_data, display_cache = build_and_write_all_player_records(
        out_path, players_dir,
        overlay=cfg.overlay,
        ensemble_ranks=ensemble_ranks,
        ensemble_avg_rank=ensemble_avg_rank,
        ensemble_avg_score=ensemble_avg_score,
        tjpr_ranks=tjpr_ranks,
        tjpr_levels=tjpr_levels,
        tjpr_scores=tjpr_scores,
        tjpr_elo=tjpr_elo,
        tjpr_contribs=tjpr_contribs,
        tjpr_lv1_contribs=tjpr_lv1_contribs,
        tjpr_level_scores=tjpr_level_scores,
        bt_gated_ranks=bt_gated_ranks,
        bt_gated_ord=bt_gated_ord,
        bt_internal_ord=bt_internal_ord,
        bt_all_ord=bt_all_ord,
        bt_all_ranks=bt_all_ranks,
        norm_bt_gated_disp=norm_bt_gated_disp,
        norm_bt_internal_disp=norm_bt_internal_disp,
        norm_bt_disp=norm_bt_disp,
        jjpr_ranks=jjpr_ranks,
        jjpr_scores=jjpr_scores,
        jjpr_contribs=jjpr_contribs,
        tour_count=tour_count,
        bt_used=bt_used,
        bt_gated_hist=bt_gated_hist,
        bt_timeline=bt_timeline,
        g2_timeline=_g2_timeline,
        match_wins=match_wins,
        player_matches=player_matches,
        player_match_count=player_match_count,
        player_tour_history=player_tour_history,
        player_first_tour=player_first_tour,
        player_max_uf=player_max_uf,
        player_top_ufs=player_top_ufs,
        player_max_uf_ach=player_max_uf_ach,
        player_max_spr_ach=player_max_spr_ach,
        player_min_spr_ach=player_min_spr_ach,
        player_max_losers_run=player_max_losers_run,
        player_max_uf_1y=player_max_uf_1y,
        player_max_spr_1y=player_max_spr_1y,
        player_min_spr_1y=player_min_spr_1y,
        player_max_losers_run_1y=player_max_losers_run_1y,
        player_tour_count_2024=player_tour_count_2024,
        player_match_count_2024=player_match_count_2024,
        player_win_count_2024=player_win_count_2024,
        player_vs_opp_count=player_vs_opp_count,
        tournament_summary=tournament_summary,
        placements_per_t=placements_per_t,
        t_seed_info=t_seed_info,
        per_tour_by_lv=per_tour_by_lv,
        rolling_deltas=rolling_deltas,
        at_time_map=at_time_map,
        per_tournament_wins_main=per_tournament_wins_main,
        per_tournament_losses_main=per_tournament_losses_main,
        per_tournament_wins_class=per_tournament_wins_class,
        per_tournament_losses_class=per_tournament_losses_class,
        per_tournament_dq_uids_main=per_tournament_dq_uids_main,
        per_tournament_dq_uids_class=per_tournament_dq_uids_class,
        eid_to_parent_tid=eid_to_parent_tid,
        spr_upper_val=spr_upper_val,
        spr_neg_val=spr_neg_val,
        uf_upper_val=uf_upper_val,
        uf_lost_val=uf_lost_val,
        pct=pct,
        users=users,
        snapshots=snapshots,
        passes_level=passes_level,
        format_display=format_display,
        tournament_series=tournament_series,
        tournament_series_number=tournament_series_number,
        build_history=build_history,
        build_achievements=build_achievements,
        build_dynamic_badges=build_dynamic_badges,
        eval_date=eval_date,
        peak_alltime_max_days=PEAK_ALLTIME_MAX_DAYS,
        no_jjpr_output=True,
        peak_track=(cfg.v4_bt_inject or {}).get('peak_track'),
    )

    print(f"  Wrote {written} players: main JSONL to {out_path}, details to {players_dir}/", flush=True)

    build_and_write_all_tournament_docs(
        tournaments_dir, all_tournaments,
        t_meta_by_id=t_meta_by_id,
        placements_per_t=placements_per_t,
        t_seed_info=t_seed_info,
        per_tournament_data=per_tournament_data,
        per_tour_by_lv=per_tour_by_lv,
        rolling_deltas=rolling_deltas,
        at_time_map=at_time_map,
        at_time_base=at_time_base,
        match_id_deltas=match_id_deltas,
        per_tournament_losers_runs=per_tournament_losers_runs,
        users=users,
        display_cache=display_cache,
        format_display=format_display,
        no_tournament_json=cfg.no_tournament_json,
        overlay=cfg.overlay,
        tour_digest=(cfg.v4_bt_inject or {}).get('tour_digest'),
    )

    # meta.json に is_small 関連も含める (= build_tournaments_index.py が読む).
    n_small = sum(1 for m in t_meta_by_id.values() if m.get('is_small'))
    min_ent = getattr(cfg, 'min_entrants_for_ranking', 8)
    meta = build_meta_dict(
        eval_date=eval_date,
        t0=t0,
        n_tournaments_total=len(all_tournaments),
        n_tournaments_small=n_small,
        min_entrants_for_ranking=min_ent,
        n_players=written,
        tjpr_elo=tjpr_elo,
        norm_bt_gated_disp=norm_bt_gated_disp,
        spr_neg_val=spr_neg_val,
        uf_lost_val=uf_lost_val,
        radar_spr_clip=RADAR_SPR_CLIP,
        radar_uf_clip=RADAR_UF_CLIP,
        max_tjpr=max_tjpr,
        snapshot_days=[d for d in (cfg.v4_bt_inject or {}).get('snap_days', []) if d > 0],
    )
    if cfg.overlay is not None:
        meta = cfg.overlay.meta(json_roundtrip(meta))
    write_meta(cfg.meta_out_path, meta)
    print(f"  Wrote meta to {cfg.meta_out_path}", flush=True)
    if lazy_matches.ENABLED:
        print(f"  lazy matches: 読み直し {lazy_matches.stats['reloads']} 回", flush=True)
        for k, v in sorted((lazy_matches.stats.get('by_caller') or {}).items(), key=lambda kv: -kv[1])[:15]:
            print(f"    {v:7d}  {k}", flush=True)

    if cfg.run_aux_outputs:
        run_aux_outputs()

    print(f"DONE in {time.time()-t0:.0f}s", flush=True)
    return written

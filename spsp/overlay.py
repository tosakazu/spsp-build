"""V4 の順位評価カスケード・直対評価の結果をサイト JSON に反映する変換 (旧 overlay)。

以前は run_build が書いた latest_tjpr_full.jsonl / players/*.json / tournaments/*.json / meta.json を
旧 postprocess_bt_lv_shared (削除済) が読み直して書き換えていた (生成 → 上書きの二段構え)。
2026-09 に、各レコードを「書く直前に変換する」形に統合した (v4/output.py が呼ぶ)。
変換の本体は旧 rewrite_* のループ 1 件分を一字も変えずに移したもの。ディスク経由と同じ入力にするため、
呼び出し側は json_roundtrip() (json.dumps → json.loads) を通してから渡す。

- make_slim_transform      : jsonl の 1 行 (旧 rewrite_jsonl)
- make_player_transform    : players/<uid>.json (旧 rewrite_player_jsons)。top_tours も返す (jsonl の top_tours に使う)
- make_tournament_transform: tournaments/<eid>.json (旧 rewrite_tournament_jsons)。final_ranks = 変換後 jsonl の ranks
- transform_meta           : meta.json (旧 build_v4_full._overlay_update_meta)
- V4Overlay                : inter (学習結果) から上の 4 つをまとめて用意する
"""
from __future__ import annotations

import datetime as dt
import json

import numpy as np
import orjson
import time

from spsp.columnar import SHARED_UIDS, UidMap, compact_uid_map
from spsp.lazy_views import IntKeyView
from spsp.common import (OVERSEAS_UIDS, _bracket_order_key_parts, jp_counter_rank, LV_TOPN_CAP, PEAK_ALLTIME_CUTOFF_DATE, uid_of,
                         flag_tags, series_slug)


def json_roundtrip(obj):
    """ファイルに書いて読み直したのと同じ形にする (int キー → str、tuple → list 等)。変換の入力はこれを通す。

    orjson を使う (標準 json の 3 倍強速い。build 全体で約 15 秒の差)。往復結果が json.loads(json.dumps(obj)) と
    型・値・dict の順序込みで一致することは golden の全出力 (48,583 件) と辺縁ケースで確認済み
    (tests/meta/test_json_roundtrip.py)。既知の違い: NaN/inf は json だと NaN トークン (JSON として不正) になるが
    orjson は null にする。numpy のスカラは orjson が TypeError で止める (json は float 派生なら通す)。
    どちらも今の出力には存在しない。書き出し側は区切り文字 (", " / ": ") が合わないので標準 json のまま。
    """
    return orjson.loads(orjson.dumps(obj, default=_np_scalar_to_py, option=orjson.OPT_NON_STR_KEYS))


def _np_scalar_to_py(o):
    """ビルド中のレコードには numpy.float64 が混ざる (標準 json は float 派生として repr で書く)。
    orjson は numpy 型を拒否するので同じ値の Python float にして渡す (repr は同じ double の最短表現で一致)。
    それ以外の型は標準 json でも書けないものなので、黙って通さず止める。"""
    if isinstance(o, np.floating):
        return float(o)
    raise TypeError(f"Type is not JSON serializable: {type(o).__name__}")


# ───────────────── 順位・peak の補助 (旧 postprocess_bt_lv_shared) ─────────────────
def _bracket_order_key_dict(m: dict):
    """v4.common._bracket_order_key の dict 版 (JSON 化済み match 行用)。

    bt_per_match の per-occurrence リスト (学習側で _bracket_order_key 順に記録) と
    表示行を対応付けるために、同じ順序キーを dict 行から計算する。本体は共通。
    """
    return _bracket_order_key_parts(m.get('global_bracket_label'), m.get('round_text'), m.get('global_top_x'))

_EMPTY: dict = {}   # .get(k, _EMPTY) 用 (読むだけ。毎回 {} を作らない)
_NO_D = object()    # history の行に 'd' が無い (= 現在の点として '0' の表を引く)


def _as_occurrence_list(info):
    """bt_per_match 値の後方互換: 旧 flat [d_w,d_l,t_w,t_l] → [[...]] に正規化."""
    if not info:
        return None
    if not isinstance(info[0], (list, tuple)):
        return [info]
    return list(info)

def _jp_counter_mapping(raw_rank_by_uid: dict, measured_uids: set | None = None) -> dict:
    """raw rank dict (uid -> raw rank 1..N) を jp-counter (= 表示順位) に変換.

    JP (計測済み): jp_counter (1, 2, 3, ...)
    灰色 (= 海外 or 計測中): jp_counter_before + 1 (= 相当順位)

    measured_uids が与えられたら、それに含まれない uid (= 計測中、2年3大会未満) も
    海外と同様にカウント対象外として扱う.
    """
    # 本体は common.jp_counter_rank (以前は同じループがここにもあった)
    return jp_counter_rank(raw_rank_by_uid, OVERSEAS_UIDS, measured_uids)

_LV_TOPN_CAP = LV_TOPN_CAP   # common.LV_TOPN_CAP

def _clamp_gray_ens_rank(rank_out: dict, lv_by_uid: dict, measured_uids: set | None):
    """jp-counter 変換済みの総合順位 dict を、灰色について Lv 枠サイズでクランプ (in-place)."""
    for u, r in rank_out.items():
        gray = u in OVERSEAS_UIDS or (measured_uids is not None and u not in measured_uids)
        if not gray:
            continue
        cap = _LV_TOPN_CAP.get(lv_by_uid.get(u))
        if cap and r > cap:
            rank_out[u] = cap

def _build_main_char(char_usage):
    """char_usage (uid -> 使用降順 [{id, pct, ...}]) から uid(int) -> メイン使用キャラ ID を作る。
    絵文字はフロント (spsp-web) が ID から自分の表で引く (旧サイト用の絵文字欄は 2026-09-29 に廃止)。"""
    ids = {}
    for uid, clist in (char_usage or {}).items():
        if clist and clist[0].get('id') is not None:
            ids[int(uid)] = clist[0]['id']
    return ids

_ELO_PER_UNIT = 29.4809

_BT_ELO_OFFSET = 0.0  # Glicko2 raw rating はそのものが Elo スケール (1500 = baseline)

_BT_PEAK_WIN_SEC = 180 * 86400

def _compact_days(tables: dict) -> dict:
    """{day: {uid: 順位}} の各日の dict を読むだけの配列の表にする (spsp/columnar.UidMap。北米で 20 万人 × 61 日 × 3 種類)。"""
    return {d: compact_uid_map(m, SHARED_UIDS) for d, m in tables.items()}

def _build_rank_at_day(cascade_snapshots: dict, snap_days: list,
                       measured_at_day: dict = None) -> dict:
    """Per-day global cascade rank: rank_at_day[day(int)][uid(int)] = display_rank(int).

    Sort all players in that day's cascade by (-max_lv, ens_r) for raw rank,
    then convert to jp-counter (= 表示順位) so player JSON history と
    「全国 #N」表示が一致する.
    measured_at_day: {day(int): 計測済み uid set}. あれば計測中も灰色扱い.
    """
    rank_at_day = {}
    for day in snap_days:
        snap = cascade_snapshots.get(str(day), {})
        items = [(uid_of(u), v[0], v[1]) for u, v in snap.items()]   # uid_of: 日をまたいで同じ int (値は同じ)
        items.sort(key=lambda x: (-x[1], x[2]))
        raw = {u: i + 1 for i, (u, _, _) in enumerate(items)}
        measured_d = (measured_at_day or {}).get(day)
        out = _jp_counter_mapping(raw, measured_d)
        _clamp_gray_ens_rank(out, {u: lv for u, lv, _ in items}, measured_d)
        rank_at_day[day] = out
    return rank_at_day

def _compute_peak180_at_day(bt_timeline_all: dict, snap_days: list, snap_ts_by_day: dict,
                             precomputed: dict = None) -> dict:
    """peak180[day][uid_int] = max ord in window (ts - 180d, ts] from bt_timeline.

    bt_timeline_all is from BT-LV-shared (= consistent with current BT rank metric).
    precomputed (= V4 build 出力の peak180_at_day): あれば優先採用 (= current rating
    fallback 込みなので 180 日窓外 player にも値が入る).
    """
    if precomputed:
        out = {}
        for day in snap_days:
            day_key = str(day)
            pd = precomputed.get(day_key, {})
            out[day] = IntKeyView(pd)   # = {int(uid_str): val} をコピーせずに (spsp/lazy_views.py)
        return out
    out = {}
    for day in snap_days:
        ts = snap_ts_by_day[day]
        cutoff = ts - _BT_PEAK_WIN_SEC
        pd = {}
        for uid_str, tl in bt_timeline_all.items():
            peak = float('-inf')
            for entry in tl:
                t, _mu, _sig, ord_v = entry
                if cutoff <= t <= ts and ord_v > peak:
                    peak = ord_v
            if peak != float('-inf'):
                pd[int(uid_str)] = peak
        out[day] = pd
    return out

def _build_bt_rank_at_day(peak180_at_day: dict, measured_at_day: dict = None) -> dict:
    """rank_at_day[day][uid] = global BT rank by peak180 desc (= jp-counter 表示順位).

    tie-break = uid (= snapshot 間で deterministic. peak180 tied 帯は default rating の
    inactive 勢が中心 (= ord=40.97 に 55 人, ord=0.0 に 48 人) なので uid で安定化).
    measured_at_day: {day(int): 計測済み uid set}. あれば計測中も灰色扱い.
    """
    out = {}
    for day, pd in peak180_at_day.items():
        items = sorted(pd.items(), key=lambda x: (-x[1], x[0]))
        raw = {u: i + 1 for i, (u, _) in enumerate(items)}
        measured_d = (measured_at_day or {}).get(day)
        out[day] = _jp_counter_mapping(raw, measured_d)
    return out

def _build_tjpr_rank_at_day(cascade_snapshots: dict, snap_days: list,
                             measured_at_day: dict = None) -> dict:
    """Per-day TJPR display rank: rank_at_day[day(int)][uid(int)] = display_rank.

    cascade_snapshots[day_str] = {uid_str: [max_lv, ens_r, tjpr_score]} の
    tjpr_score 降順 (tie は uid) で raw rank を作り jp-counter 表示順位に変換。
    tjpr_score が無い旧フォーマット snapshot は skip (= その日の rank なし)。
    """
    out = {}
    for day in snap_days:
        snap = cascade_snapshots.get(str(day), {})
        if not snap:
            continue
        sample_v = next(iter(snap.values()))
        if len(sample_v) < 3:
            continue
        ordered = sorted(snap.keys(), key=lambda u: (-snap[u][2], int(u)))
        raw = {uid_of(u): i + 1 for i, u in enumerate(ordered)}
        out[day] = _jp_counter_mapping(raw, (measured_at_day or {}).get(day))
    return out

_GRID_PREP: dict = {}   # id(rad) → (rad, snap_days, 下ごしらえ)


def _grid_tables(rad: dict, snap_days: list) -> list:
    """_peak_ranks_from_track が日ごとに引く表 [(day, 表, 位置の配列 or None, 値の配列)] (day > 0 のみ、snap_days の順)。
    同じ rad・snap_days で全選手ぶん呼ばれるので 1 回だけ作る。表は以後書き換えない。"""
    c = _GRID_PREP.get(id(rad))
    if c is not None and c[0] is rad and c[1] is snap_days:
        return c[2]
    out = []
    for day in snap_days:
        if day <= 0:
            continue
        t = rad.get(day, {})
        if type(t) is UidMap and t._ix is SHARED_UIDS:
            out.append((day, t, t._at, t._vals))
        else:
            out.append((day, t, None, None))
    _GRID_PREP[id(rad)] = (rad, snap_days, out)
    return out


def _peak_ranks_from_track(rec: dict, cur_ranks: dict, track: dict | None, eval_day: int,
                           rank_at_day_by_key: dict | None = None, snap_days: list | None = None,
                           peak_alltime_max_days: int | None = None) -> None:
    """rec['peak_ranks'] / rec['peak_ranks_1y'] を、学習中に追跡した真の最高順位 (spsp/peak_track) と
    グリッドのスナップショット、現在の順位のうち最良のものにする。

    追跡値は serious 大会ごとの点 (直前 / 直後) なので、大会の無い期間の減衰による順位の動きは拾わない。
    その隙間はグリッドのスナップショット (週次〜四半期) で補う。以前 (_recompute_peak_ranks) は
    スナップショットだけだったので、大会直後に達した順位を落としていた。
    同順位なら最新 (現在 = 'now' が最優先、次に日付の新しいもの) を採用 (以前の「最新の日」規則と同じ)。

    cur_ranks: {'ensemble': 現在の表示順位 or None, ...}
    track:     peak_track.snapshot() の uid 分 ({'ensemble': (rank, day) | None, 'ensemble_1y': ..., ...}) or None
    eval_day:  評価日の暦日序数 (date.toordinal())
    """
    uid = rec.get('user_id')
    ui = SHARED_UIDS.pos.get(uid) if type(uid) is int else None
    for key in ('ensemble', 'tjpr', 'bt_gated'):
        cur = cur_ranks.get(key)
        cur = cur if (cur is not None and cur > 0) else None
        grid = []   # (days_ago, rank)
        rad = (rank_at_day_by_key or {}).get(key)
        if rad and snap_days:
            for day, t, at, vals in _grid_tables(rad, snap_days):
                if at is not None and ui is not None:   # 共有の番号の UidMap: 位置を直接引く (t.get(uid) と同じ値)
                    p = at[ui] if ui < len(at) else -1
                    r = vals[p] if p >= 0 else None
                else:
                    r = t.get(uid)
                if r is not None and r > 0:
                    grid.append((day, r))
        for suffix, out_key, limit in (('', 'peak_ranks', peak_alltime_max_days), ('_1y', 'peak_ranks_1y', 365)):
            cands = []   # (rank, days_ago)。days_ago 0 = now
            if cur is not None:
                cands.append((cur, 0))
            tr = (track or {}).get(key + suffix)
            if tr is not None:
                cands.append((int(tr[0]), eval_day - int(tr[1])))
            cands.extend((r, d) for d, r in grid if limit is None or d <= limit)
            if not cands:
                continue
            best = min(r for r, _ in cands)
            days = min(d for r, d in cands if r == best)
            rec.setdefault(out_key, {})[key] = {'rank': best, 'when': 'now' if days == 0 else f'{days}日前'}


def _recompute_peak_ranks(rec: dict, rank_at_day_by_key: dict, snap_days: list,
                            peak_alltime_max_days: int | None = None):
    """Replace rec['peak_ranks'] / rec['peak_ranks_1y'] with new-cascade-based peak.

    rank_at_day_by_key: {'ensemble': rank_at_day, 'tjpr': ..., 'bt_gated': ...}
        (各 rank_at_day[day(int)][uid(int)] = display_rank)。
        3 系列すべて V4 の per-day 順位で再計算する。旧実装は ensemble のみ
        再計算しており、順位評価/直対評価の peak が base 値のまま残って
        「現在順位 < 過去最高順位」の矛盾が出ていた (2026-07-21 修正)。

    Tie-break: pick the MOST RECENT day the peak rank was achieved (= smallest d).

    peak_alltime_max_days: alltime peak の cutoff (= eval_date からこの日数までを参照).
        None なら全期間. デフォルトは PEAK_CUTOFF_DATE からの日数を caller が渡す.
    """
    uid = rec.get('user_id')
    for key, rank_at_day in rank_at_day_by_key.items():
        if not rank_at_day:
            continue
        pts = []  # (d_int, rank_int)
        for day in snap_days:
            r = rank_at_day.get(day, {}).get(uid)
            if r is not None:
                pts.append((day, r))
        if not pts:
            continue
        # alltime (= cutoff 内のスナップショットのみ参照)
        pts_alltime = ([(d, r) for d, r in pts if d <= peak_alltime_max_days]
                       if peak_alltime_max_days is not None else pts)
        if pts_alltime:
            best = min(r for _, r in pts_alltime)
            latest_d = min(d for d, r in pts_alltime if r == best)
            when = 'now' if latest_d == 0 else f'{latest_d}日前'
            rec.setdefault('peak_ranks', {})[key] = {'rank': best, 'when': when}
        # 1y
        pts_1y = [(d, r) for d, r in pts if d <= 365]
        if pts_1y:
            best_1y = min(r for _, r in pts_1y)
            latest_1y = min(d for d, r in pts_1y if r == best_1y)
            when_1y = 'now' if latest_1y == 0 else f'{latest_1y}日前'
            rec.setdefault('peak_ranks_1y', {})[key] = {'rank': best_1y, 'when': when_1y}

PEAK_CUTOFF_DATE = PEAK_ALLTIME_CUTOFF_DATE  # 過去最高順位/最高到達Lv の集計開始日 (= EVO Japan 2020、common と共有)

def _player_has_v4_tjpr_at_tour(per_tour_by_lv_v4, uid_s, eid_s):
    """V4 cascade で player が tournament に entry を持つか. False = rw=0 等で TJPR contrib なし.
    BT 表示 hide 判定に使う (= LV>0 でも rw=0 なら BT も hide で TJPR と整合)."""
    if per_tour_by_lv_v4 is None:
        return True  # fallback: 旧挙動
    eid_lv_map = per_tour_by_lv_v4.get(uid_s, {}).get(eid_s)
    return bool(eid_lv_map)



# ───────────────── jsonl の 1 行 (旧 rewrite_jsonl のループ本体) ─────────────────
def make_slim_transform(cascade_latest: dict, peak180_latest: dict, bt_rank_latest: dict,
                        eval_date_s: str, per_tour_by_lv_v4: dict = None,
                        measured_day0: set = None, main_char_id: dict = None):
    """latest_tjpr_full.jsonl の 1 レコード用の変換関数を返す。

    peak180_latest[uid_int] = ord, bt_rank_latest[uid_int] = global BT rank.
    per_tour_by_lv_v4: 渡されたら V4 cascade の sum で tjpr_score を上書き.
    measured_day0: eval 時点の計測済み uid 集合. 渡されたら jp-counter で計測中も
      灰色扱いにし、metadata.provisional フラグを各レコードに書き込む.
    """
    # cascade_latest = {uid_str: [max_lv, ens_r]}
    cascade_for_uid = {int(u): tuple(v) for u, v in cascade_latest.items()}
    ordered = sorted(cascade_for_uid.keys(),
                     key=lambda u: (-cascade_for_uid[u][0], cascade_for_uid[u][1]))
    # 反復34+: cascade_for_uid[u][2] = tjpr_score (= V4 cascade sum). これで TJPR rank を
    # 計算して ranks.tjpr に上書きし history.tjpr_r と整合させる.
    v4_tjpr_rank = {}
    if cascade_for_uid:
        sample_v = next(iter(cascade_for_uid.values()))
        if len(sample_v) >= 3:
            # tie (= tjpr_score 0 の同点ブロック) は uid で決める。プレイヤー JSON / history 側
            # (make_player_transform の cur_tjpr_rank, _build_tjpr_rank_at_day) と同じ規則。以前は
            # ここだけ tie の順が dict の走査順で、jsonl と players/<uid>.json の ranks.tjpr が
            # 8,678 人 (2026-09-06 時点) で食い違っていた。
            tjpr_sorted = sorted(cascade_for_uid.keys(),
                                 key=lambda u: (-cascade_for_uid[u][2], int(u)))
            v4_tjpr_rank = {u: i + 1 for i, u in enumerate(tjpr_sorted)}
    cascade_rank = {u: i + 1 for i, u in enumerate(ordered)}
    print(f"  cascade ranked players: {len(cascade_rank)}", flush=True)

    # jp-counter (= 表示順位) に変換. プレイヤーページや「全国 #N」バッジ等の
    # サーバ側 source-of-truth として ranks.* に直接保存する.
    # (= bt_rank_latest は _build_bt_rank_at_day 側で既に jp-counter 化済み.)
    cascade_rank_jp = _jp_counter_mapping(cascade_rank, measured_day0)
    _clamp_gray_ens_rank(cascade_rank_jp,
                         {u: v[0] for u, v in cascade_for_uid.items()}, measured_day0)
    v4_tjpr_rank_jp = _jp_counter_mapping(v4_tjpr_rank, measured_day0) if v4_tjpr_rank else {}

    # V4 tjpr_score: per_tour_by_lv_v4[uid_str][eid_str][lv_str] = [raw, pos, w, counted]
    # 各 player の shared_cascade_lv における w 合計が V4 tjpr_score.
    v4_tjpr_score_by_uid = {}
    if per_tour_by_lv_v4:
        for uid_s, eid_map in per_tour_by_lv_v4.items():
            uid_int = int(uid_s)
            mlv = cascade_for_uid.get(uid_int)
            if not mlv:
                continue
            lv_key = str(mlv[0])
            total = 0.0
            for eid_s, lv_map in eid_map.items():
                v = lv_map.get(lv_key)
                if v is not None:
                    total += v[2]
            if total > 0:
                v4_tjpr_score_by_uid[uid_int] = total

    n_in = n_out = n_hidden = 0

    def transform(rec):
        nonlocal n_hidden, n_out
        uid = rec.get('user_id')
        if uid in cascade_rank:
            # cascade_for_uid[uid] = [max_lv, ens_r] (旧) または [max_lv, ens_r, tjpr_score] (反復34+)
            _cv = cascade_for_uid[uid]
            max_lv, ens_r = _cv[0], _cv[1]
            # ranks.ensemble / ranks.tjpr / ranks.bt_gated は表示順位 (= jp-counter) を保存.
            # 海外勢込みの raw rank ではなく、JP-only counter を直接持たせることで、
            # フロント (= プレイヤーページ / 全国 #N バッジ / index.html ランキング表) で
            # 一律 ranks.X を読めば表示順位になる.
            rec.setdefault('ranks', {})['ensemble'] = cascade_rank_jp.get(uid, cascade_rank[uid])
            if uid in v4_tjpr_rank:
                rec['ranks']['tjpr'] = v4_tjpr_rank_jp.get(uid, v4_tjpr_rank[uid])
            # 平均順位 / 平均スコアは新方式 (= cascade) では意味を持たないので非表示
            rec.setdefault('scores', {})['ensemble_avg_rank'] = None
            rec['scores']['ensemble_avg_score'] = None
            rec['scores']['shared_cascade_lv'] = max_lv
            rec['scores']['shared_cascade_ens_r'] = ens_r
            # V4 tjpr_score 上書き (= 旧値は v2 prod cascade 由来で per-tour 合計と不整合)
            v4_score = v4_tjpr_score_by_uid.get(uid)
            if v4_score is not None:
                rec['scores']['tjpr_score'] = round(v4_score, 6)
                rec['scores']['tjpr_elo'] = round(v4_score * 17.5, 4)
            elif rec['scores'].get('tjpr_elo') is not None:
                # fallback: 旧 ×50
                rec['scores']['tjpr_elo'] *= 50
            n_out += 1
        else:
            rec.setdefault('ranks', {})['ensemble'] = 0
            n_hidden += 1
        # BT rank / score も新方式に上書き (= history と整合)
        # bt_gated_ordinal は JSONL では未使用なので書かない (= 容量削減)
        cur_ord = peak180_latest.get(uid)
        cur_bt_rank = bt_rank_latest.get(uid)
        if cur_ord is not None:
            rec.setdefault('scores', {})['bt_gated_elo'] = round(cur_ord * _ELO_PER_UNIT + _BT_ELO_OFFSET, 4)
        # bt_rank_latest は _build_bt_rank_at_day 側で jp-counter 化済み.
        rec.setdefault('ranks', {})['bt_gated'] = cur_bt_rank if cur_bt_rank is not None else 0
        # 計測中フラグ (= 2年で集計対象大会 3 未満). フロントの灰色表示 / バッジ用.
        if measured_day0 is not None:
            rec.setdefault('metadata', {})['provisional'] = uid not in measured_day0
        # メイン使用キャラの ID (フロントが自分の表で絵文字を引く。無ければ null)
        if main_char_id is not None:
            rec['main_char_id'] = main_char_id.get(uid)
        return rec
    return transform


# ───────────────── players/<uid>.json (旧 rewrite_player_jsons のループ本体) ─────────────────
def make_player_transform(cascade_snapshots: dict, bt_timeline_all: dict,
                          bt_per_tour_all: dict, bt_per_match_all: dict,
                          snap_days: list, snap_ts_by_day: dict, eval_date_s: str,
                          peak180_at_day_pre: dict = None,
                          per_tour_by_lv_v4: dict = None,
                          char_usage: dict = None,
                          measured_at_day: dict = None,
                          class_bt_split: dict = None,
                          peak_track: dict = None):
    """players/<uid>.json 1 件用の変換関数を返す。transform(rec) -> (rec, top_tours)。
    top_tours = 最終 tjpr_w 順の上位 8 大会 (jsonl の top_tours に使う)。"""
    class_bt_split = class_bt_split or {}
    # cascade_snapshots[day_str] = {uid_str: [max_lv, ens_r]}
    # bt_timeline_all[uid_str] = list of [ts, mu, sigma, ord]
    rank_at_day = _compact_days(_build_rank_at_day(cascade_snapshots, snap_days, measured_at_day))
    cur_ens_rank = rank_at_day.get(0, {})  # uid -> global cascade rank at day 0
    # 過去最高順位 (alltime) の cutoff: eval_date と PEAK_CUTOFF_DATE の日数差.
    # eval_date_s が無い (= test) 場合は None (= cutoff なし) で全期間.
    if eval_date_s:
        peak_alltime_max_days = (dt.datetime.fromisoformat(eval_date_s).date() - PEAK_CUTOFF_DATE).days
    else:
        peak_alltime_max_days = None
    _eval_day_ord = dt.datetime.fromisoformat(eval_date_s).date().toordinal() if eval_date_s else None
    # 反復34+: V4 cascade tjpr_score 降順の rank を day 0 で計算 (= ranks.tjpr 上書き用).
    # 表示順位 (= jp-counter) に変換しておく.
    latest_snap = cascade_snapshots.get('0', {})
    cur_tjpr_rank = {}
    if latest_snap:
        sample_v = next(iter(latest_snap.values()))
        if len(sample_v) >= 3:
            ordered_by_tjpr = sorted(latest_snap.keys(),
                                      key=lambda u: (-latest_snap[u][2], int(u)))
            raw_tjpr = {int(u): i + 1 for i, u in enumerate(ordered_by_tjpr)}
            cur_tjpr_rank = _jp_counter_mapping(raw_tjpr, (measured_at_day or {}).get(0))
    print(f"  computing peak180 + BT rank at {len(snap_days)} snap days ...", flush=True)
    peak180_at_day = _compute_peak180_at_day(bt_timeline_all, snap_days, snap_ts_by_day,
                                              precomputed=peak180_at_day_pre)
    bt_rank_at_day = _compact_days(_build_bt_rank_at_day(peak180_at_day, measured_at_day))
    # per-day TJPR 表示順位 (= 順位評価 peak の再計算用。ensemble と同じ V4 基盤)
    tjpr_rank_at_day = _compact_days(_build_tjpr_rank_at_day(cascade_snapshots, snap_days, measured_at_day))
    n = 0
    n_replaced_timeline = 0
    n_added_lv = 0
    n_peak_updated = 0
    t0 = time.time()
    top_tours_by_uid: dict = {}  # uid_str -> 最終 tjpr_w 順の top_tours (jsonl 上書き用)
    _ts_date: dict = {}  # bt_timeline の時刻 → 'YYYY-MM-DD' (同じ大会の時刻を選手ごとに変換し直さない)
    _day_tabs: dict = {}  # history の d → (cascade_snapshots, peak180, 直対順位, 総合順位 の その日の表)

    def transform(rec):
        nonlocal n_added_lv, n_peak_updated, n_replaced_timeline
        uid = rec.get('user_id')
        uid_str = str(uid)

        # 計測中フラグ (= 2年で集計対象大会 3 未満). jsonl 側と同じ定義.
        if measured_at_day is not None and 0 in measured_at_day:
            rec.setdefault('metadata', {})['provisional'] = uid not in measured_at_day[0]

        # TJPR display scale ×50 (= scores.tjpr_elo + history[i].tjpr_e)
        if rec.get('scores', {}).get('tjpr_elo') is not None:
            rec['scores']['tjpr_elo'] *= 50
        # history[i].tjpr_e も ×50 (下の history の上書きと同じ回で)

        # tournaments[].bt_used / bt_d (= peak180 delta) / bt_internal_d を新方式に上書き
        # info schema: [bt_used, peak_d, internal_d, ord_after, peak_after]
        per_tour = bt_per_tour_all.get(uid_str, {})
        _cls_split = class_bt_split.get(uid_str, {})
        if rec.get('tournaments'):
            for t in rec['tournaments']:
                eid = t.get('event_id')
                if eid is None:
                    # 下位クラス(cv)行: 親 event の class 試合分の内部 delta 収支を表示.
                    # (本戦分は親 event 行へ。直対評価=peak180 は event 単位だが cv 行は
                    #  「クラス内対戦の収支」として同 class 分の内部 delta を出す.)
                    peid = t.get('parent_event_id')
                    cs = (_cls_split.get(peid) or _cls_split.get(str(peid))) if peid is not None else None
                    if cs is not None:
                        t['bt_used'] = bool(cs[1])
                        t['bt_d'] = float(cs[0])
                        t['bt_internal_d'] = float(cs[0])
                    continue
                info = per_tour.get(str(eid))
                if info is None: continue
                t['bt_used'] = bool(info[0])
                t['bt_d'] = float(info[1])           # peak180 delta
                # 本戦(非class)分のみ: event 全体の内部 delta から class 分を引く.
                _cls = _cls_split.get(eid) or _cls_split.get(str(eid))
                t['bt_internal_d'] = float(info[2]) - (float(_cls[0]) if _cls else 0.0)
                t['bt_internal_after'] = float(info[3])
                t['bt_peak_after'] = float(info[4])
        for key in ('top_spr', 'recent_spr'):
            for t in rec.get(key, []) or []:
                eid = t.get('event_id')
                if eid is None: continue
                info = per_tour.get(str(eid))
                if info is None: continue
                t['bt_used'] = bool(info[0])
                t['bt_d'] = float(info[1])
                t['bt_internal_d'] = float(info[2])

        # recent_matches[i] (per-match) を新方式に上書き。
        # 同一イベント×同方向の複数対戦 (GF+リセット連取等) は (eid, key) ごとに
        # 行をグループ化し、進行順 (= 学習側の記録順) に occurrence を割り当てる。
        # 組のキーは、eid・uid・相手がどれも int なら (eid, 勝者, 敗者) の int の組 (bt_per_match_all.pair で文字列を
        # 介さずに引く)、そうでなければ (str(eid), '勝者-敗者')。1 試合だけの組 (ほとんど) は並べ替えない。
        _pm_groups: dict = {}
        _pair = getattr(bt_per_match_all, 'pair', None)
        _uid_int = _pair is not None and type(uid) is int
        for mrec in rec.get('recent_matches', []) or []:
            eid = mrec.get('event_id')
            opp = mrec.get('opp_uid')
            won = mrec.get('won')
            if eid is None or opp is None or won is None: continue
            if _uid_int and type(eid) is int and type(opp) is int:
                gk = (eid, uid, opp) if won else (eid, opp, uid)
            else:
                gk = (str(eid), f'{uid}-{opp}' if won else f'{opp}-{uid}')
            rows = _pm_groups.get(gk)
            if rows is None:
                _pm_groups[gk] = [mrec]
            else:
                rows.append(mrec)
        for gk, rows in _pm_groups.items():
            info = _pair(*gk) if len(gk) == 3 else (bt_per_match_all.get(gk[0]) or {}).get(gk[1])
            if not info: continue
            occs = info if isinstance(info[0], (list, tuple)) else (info,)   # _as_occurrence_list と同じ (写さない)
            if len(rows) > 1:
                rows = sorted(rows, key=_bracket_order_key_dict)
            n_occ = len(occs)
            for i, mrec in enumerate(rows):
                d_w, d_l, t_w, t_l = occs[i] if i < n_occ else occs[-1]
                if mrec.get('won'):
                    d = float(d_w)
                    mrec['bt_d'] = d
                    mrec['bt_internal_d'] = d
                    mrec['bt_tracked'] = bool(t_w)
                else:
                    d = float(d_l)
                    mrec['bt_d'] = d
                    mrec['bt_internal_d'] = d
                    mrec['bt_tracked'] = bool(t_l)
        # top_bt_gains を recent_matches (= V4 BT 反映済) から再構築:
        # v2 prod が pre-compute した list は v2 BT (M-tour gated) ベースで選ばれて
        # おり、V4 BT (LV-gated) で 0 化された試合が表示に残る問題があった.
        # 対策として recent_matches を SOT に直して、won + bt_tracked + bt_d>0
        # の試合から bt_d 降順で top-N を再構築する.
        # 大会レベルの属性 (is_weekend / is_restricted など) は rec['tournaments']
        # から enrichment.
        if rec.get('recent_matches'):
            # 大会属性 lookup を rec['tournaments'] から構築
            tour_meta = {}
            for t in rec.get('tournaments') or []:
                eid = t.get('event_id')
                if eid is None: continue
                tour_meta[eid] = {
                    'is_weekend': t.get('is_weekend'),
                    'is_pre': t.get('is_pre', False),
                    'is_restricted': t.get('is_restricted', False),
                    'is_lower_class': t.get('is_lower_class', False),
                }
            # 2 年以内 (= v2 prod の top_bt_gains 範囲に揃える)
            two_years_ago = (dt.datetime.fromisoformat(eval_date_s) - dt.timedelta(days=730)).date().isoformat() \
                if eval_date_s else ''
            new_gains = []
            for m in rec['recent_matches']:
                if not m.get('won'): continue
                if not m.get('bt_tracked'): continue
                if (m.get('bt_d') or 0) <= 1e-9: continue
                if two_years_ago and (m.get('date') or '') < two_years_ago: continue
                entry = {
                    'opp_uid': m.get('opp_uid'),
                    'opp_display': m.get('opp_display'),
                    'event_id': m.get('event_id'),
                    'tournament_name': m.get('tournament_name'),
                    'event_name': m.get('event_name'),
                    'date': m.get('date'),
                    'bt_d': m.get('bt_d'),
                    'bt_tracked': True,
                    'round': m.get('round'),
                    'round_text': m.get('round_text'),
                    'global_top_x': m.get('global_top_x'),
                    'global_bracket_label': m.get('global_bracket_label'),
                    'bracket_type': m.get('bracket_type'),
                    'p_score': m.get('p_score'),
                    'o_score': m.get('o_score'),
                }
                meta = tour_meta.get(m.get('event_id'), {})
                entry.update(meta)
                new_gains.append(entry)
            new_gains.sort(key=lambda x: -(x.get('bt_d') or 0))
            rec['top_bt_gains'] = new_gains[:30]

        # bt_timeline 置換. V4 は Glicko2 single-pass なので bt_timeline_g2 も同じ
        # ソースから {date, rating, rd} 形式で再生成 (= player-detail.js が useG2 path を選ぶ).
        # 旧コードは bt_timeline_g2 を pop してたが、JS 側で fallback の BT_ELO_OFFSET=1000
        # が誤適用されるバグ (= 0.0 || 1000 → 1000) があったため Glicko2 path を強制する.
        new_tl = bt_timeline_all.get(uid_str)
        if new_tl is not None:
            new_tl_list = []
            new_g2_list = []
            for entry in new_tl:
                ts, mu, sigma, ord_v = entry
                date_s = _ts_date.get(ts)
                if date_s is None:
                    date_s = _ts_date[ts] = dt.datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
                new_tl_list.append({'date': date_s, 'mu': mu, 'sigma': sigma, 'ord': ord_v})
                new_g2_list.append({'date': date_s, 'rating': round(mu, 2), 'rd': round(sigma, 2)})
            rec['bt_timeline'] = new_tl_list
            rec['bt_timeline_g2'] = new_g2_list
            n_replaced_timeline += 1

        # history.shared_lv 追加 + history.bt_g_r / bt_g_e を新方式 peak180 で再計算
        if rec.get('history'):
            for h in rec['history']:
                if h.get('tjpr_e') is not None:
                    h['tjpr_e'] *= 50
                if h.get('event_id') is not None:
                    continue   # 大会の点 (score.history_event_rows): 当時アンカー由来で、ここで上書きしない
                d_int = h.get('d')
                dk = d_int if 'd' in h else _NO_D
                tabs = _day_tabs.get(dk)
                if tabs is None:   # 日ごとの表 (全選手で同じ) は日ごとに 1 回だけ引く
                    tabs = _day_tabs[dk] = (
                        cascade_snapshots.get(str(d_int) if dk is not _NO_D else '0', _EMPTY),
                        peak180_at_day.get(d_int, _EMPTY), bt_rank_at_day.get(d_int, _EMPTY),
                        rank_at_day.get(d_int, _EMPTY))
                sv = tabs[0].get(uid_str)   # 値は [max_lv, ens_r, ...] (None にはならない)。1 回だけ引く
                if sv is not None:
                    h['shared_lv'] = sv[0]
                    h['shared_ens_r'] = sv[1]
                    n_added_lv += 1
                # else: 当時 cascade 未到達 → shared_lv 欠落 (= chart 側で gray 扱い)
                # bt_g_r / bt_g_e を新方式 (= OpenSkill peak180) で更新
                # ens (= 総合評価 global rank) を新 cascade で更新
                if d_int is not None:
                    ord_v = tabs[1].get(uid)
                    r_v = tabs[2].get(uid)
                    h['bt_g_e'] = (round(float(ord_v) * _ELO_PER_UNIT + _BT_ELO_OFFSET, 4)
                                    if ord_v is not None else None)
                    h['bt_g_r'] = r_v
                    h['ens'] = tabs[3].get(uid)

        # player JSON 内の ranks / scores も new cascade で上書き (= player page 表示一致)
        latest = cascade_snapshots.get('0', {})
        if uid_str in latest:
            # 反復34+ schema は [max_lv, ens_r, tjpr_score], 旧 schema は [max_lv, ens_r]
            _lv = latest[uid_str]
            max_lv, ens_r_v = _lv[0], _lv[1]
            rec.setdefault('scores', {})['shared_cascade_lv'] = max_lv
            rec['scores']['shared_cascade_ens_r'] = ens_r_v
            # 平均順位 / 平均スコアは非表示
            rec['scores']['ensemble_avg_rank'] = None
            rec['scores']['ensemble_avg_score'] = None
            # ranks.ensemble を新 cascade global rank に揃える (= history と整合)
            rec.setdefault('ranks', {})['ensemble'] = cur_ens_rank.get(uid, 0)
            # ranks.tjpr を V4 cascade tjpr_score ベースに上書き (= history.tjpr_r と整合)
            if uid in cur_tjpr_rank:
                rec['ranks']['tjpr'] = cur_tjpr_rank[uid]
        else:
            # cascade 圏外 → ranks.ensemble = 0
            rec.setdefault('ranks', {})['ensemble'] = 0
            rec.setdefault('scores', {})['ensemble_avg_rank'] = None
            rec['scores']['ensemble_avg_score'] = None

        # 使用キャラ (直近 1 年, eligible event 限定).
        if char_usage is not None:
            rec['characters'] = char_usage.get(uid, [])

        # 現時点の BT rank / score も新方式 peak180 で上書き (= history と整合)
        cur_ord = peak180_at_day.get(0, {}).get(uid)
        cur_rank = bt_rank_at_day.get(0, {}).get(uid)
        if cur_ord is not None:
            rec.setdefault('scores', {})['bt_gated_ordinal'] = round(cur_ord, 4)
            rec['scores']['bt_gated_elo'] = round(cur_ord * _ELO_PER_UNIT + _BT_ELO_OFFSET, 4)
        rec.setdefault('ranks', {})['bt_gated'] = cur_rank if cur_rank is not None else 0

        # peak_ranks / peak_ranks_1y を新 cascade 順位で再計算 (= tie は最新日採用)
        # alltime peak は PEAK_CUTOFF_DATE = EVO Japan 2020 (2020-01-24) 以降のみ参照
        # 総合 / 順位評価 / 直対評価 の 3 系列とも V4 の per-day 順位で再計算する。
        if peak_track is not None:
            # 学習中に追跡した真の最高順位 (spsp/peak_track) + 現在値
            _peak_ranks_from_track(rec,
                                   {'ensemble': rank_at_day.get(0, {}).get(uid),
                                    'tjpr': tjpr_rank_at_day.get(0, {}).get(uid),
                                    'bt_gated': bt_rank_at_day.get(0, {}).get(uid)},
                                   peak_track.get(uid), _eval_day_ord,
                                   rank_at_day_by_key={'ensemble': rank_at_day, 'tjpr': tjpr_rank_at_day,
                                                       'bt_gated': bt_rank_at_day},
                                   snap_days=snap_days, peak_alltime_max_days=peak_alltime_max_days)
        else:
            _recompute_peak_ranks(rec,
                                    {'ensemble': rank_at_day,
                                     'tjpr': tjpr_rank_at_day,
                                     'bt_gated': bt_rank_at_day},
                                    snap_days,
                                    peak_alltime_max_days=peak_alltime_max_days)
        if uid in rank_at_day.get(0, {}):
            n_peak_updated += 1

        # tournaments[].tjpr_* を shared_cascade_lv ベースに上書き
        # (= build 出力は TJPR 単独 cascade の採用 LV ベース、新方式は BT/TJPR
        #    アンサンブル cascade の LV で集計する).
        # tjpr_by_lv = {lv_str: [raw, pos, w, counted_int]} (build が埋めた lookup).
        # 後で size 節約のため tjpr_by_lv は出力から削る.
        _shared_lv = rec.get('scores', {}).get('shared_cascade_lv')
        _shared_lv_key = str(_shared_lv) if _shared_lv else None
        # V4 per_tour_by_lv (= 引数経由で渡る) を優先使用、なければ embedded tjpr_by_lv
        _v4_by_eid = per_tour_by_lv_v4.get(uid_str, {}) if per_tour_by_lv_v4 else {}
        _v4_tjpr_sum = 0.0
        for _t in rec.get('tournaments', []) or []:
            _embedded_by_lv = _t.pop('tjpr_by_lv', None)
            _eid = _t.get('event_id')
            if _eid is not None:
                _by_lv = _v4_by_eid.get(str(_eid))
            elif _t.get('parent_event_id') is not None:
                # class_virtual (下位クラス): event_id=None なので合成 key で V4 cascade を引く.
                _by_lv = _v4_by_eid.get(f"cv:{_t.get('parent_event_id')}:{_t.get('class_letter')}")
            else:
                _by_lv = None
            if _by_lv is None:
                _by_lv = _embedded_by_lv
            if _shared_lv_key and _by_lv and _shared_lv_key in _by_lv:
                _raw, _pos, _w, _counted = _by_lv[_shared_lv_key]
                _t['tjpr_lv'] = _shared_lv
                _t['tjpr_raw'] = _raw
                _t['tjpr_pos'] = _pos
                _t['tjpr_w'] = _w
                _t['tjpr_counted'] = bool(_counted)
                _v4_tjpr_sum += _w
            else:
                _t['tjpr_lv'] = 0
                _t['tjpr_raw'] = 0.0
                _t['tjpr_pos'] = 0.0
                _t['tjpr_w'] = 0.0
                _t['tjpr_counted'] = False
        # tjpr_score / tjpr_elo を V4 cascade sum で上書き (= 旧値は v2 prod cascade 由来で不整合)
        if _v4_tjpr_sum > 0:
            rec.setdefault('scores', {})['tjpr_score'] = round(_v4_tjpr_sum, 6)
            rec['scores']['tjpr_elo'] = round(_v4_tjpr_sum * 17.5, 4)

        # top_tjpr_contribs を更新後の tournaments[] から再構築 (寄与降順).
        def _slim_tour(e):
            return {
                'event_id': e.get('event_id'),
                'date': e.get('date'), 'name': e.get('name'), 'event': e.get('event'),
                'place': e.get('place'), 'nent': e.get('nent'),
                'seed': e.get('seed'), 'spr': e.get('spr'),
                'tjpr_w': e.get('tjpr_w'), 'tjpr_raw': e.get('tjpr_raw'),
                'tjpr_lv': e.get('tjpr_lv'), 'tjpr_counted': e.get('tjpr_counted'),
                'bt_d': e.get('bt_d'), 'bt_internal_d': e.get('bt_internal_d'),
                'bt_used': e.get('bt_used'),
                'is_weekend': e.get('is_weekend'),
                'is_pre': e.get('is_pre'),
                'is_restricted': e.get('is_restricted'),
                'is_lower_class': e.get('is_lower_class'),
                'is_dq': e.get('is_dq'),
                # 機械可読タグ (この record にある is_* だけから、固定順) — output._slim_tour_entry と同じ契約
                'tags': flag_tags({k: e.get(k) for k in ('is_weekend', 'is_pre', 'is_restricted', 'is_lower_class', 'is_dq')}),
                'parent_event_id': e.get('parent_event_id'),
                'class_letter': e.get('class_letter'),
            }
        _counted_tjpr = [t for t in (rec.get('tournaments') or []) if (t.get('tjpr_w') or 0) > 0]
        _counted_tjpr.sort(key=lambda x: -(x.get('tjpr_w') or 0))
        rec['top_tjpr_contribs'] = [_slim_tour(t) for t in _counted_tjpr]

        # ランキング行プレビュー (top_tours) も最終 tjpr_w 順で再構築して収集.
        # jsonl 側の top_tours は base フェーズの重み順のままなので、caller が
        # この dict で上書きしないと「名前下の上位貢献大会」がプレイヤーページの
        # 順位評価寄与順と食い違う (2026-07-21 発覚)。
        from spsp.meta import tournament_series as _tser, tournament_series_number as _tsnum
        top_tours_by_uid[uid_str] = [
            {'series': _tser(t.get('name') or ''),
             'series_id': series_slug(_tser(t.get('name') or '')),
             'num': _tsnum(t.get('name') or ''),
             'place': t.get('place'), 'nent': t.get('nent'),
             'event_id': t.get('event_id')}
            for t in _counted_tjpr[:8]
        ]

        return rec, top_tours_by_uid[uid_str]
    transform.top_tours_by_uid = top_tours_by_uid
    # build の履歴スナップショット (build._build_snapshots_from_v4_cascade) が同じ表を作るので、中身が同じなら
    # そちらで使い回す (spsp/build.py。読むだけの dict。北米で日数 60 × 20 万人 × 2 表)
    transform.rank_tables = {'ensemble_ranks': rank_at_day, 'tjpr_ranks': tjpr_rank_at_day,
                             'bt_gated_ranks': bt_rank_at_day}
    return transform


# ───────────────── tournaments/<eid>.json (旧 rewrite_tournament_jsons のループ本体) ─────────────────
def make_tournament_transform(cascade_latest: dict, bt_per_tour_all: dict,
                              bt_per_match_all: dict = None, per_tour_by_lv_v4: dict = None,
                              final_ranks: dict = None):
    """tournaments/<eid>.json 1 件用の変換関数を返す。

    standings[] の各 entry には build が tjpr_by_lv lookup を埋めている.
    各 player の shared_cascade_lv (= cascade_latest[uid] の max_lv) で per-tour 値を差し替え.
    matches[] は bt_per_match_all (= 新 BT 学習で記録した per-match delta) で上書き.
    final_ranks (= uid_str → {'ensemble','tjpr','bt_gated'}, 変換後 jsonl の ranks.*) を
    渡すと standings[] / highlights[] の global_ranks を最終表示ランクで上書きする.
    """
    def _override_global_ranks(entry):
        if final_ranks is None: return
        fr = final_ranks.get(str(entry.get('user_id')))
        if fr is not None:
            entry['global_ranks'] = dict(fr)

    uid_to_shared_lv = {uid_s: info[0] for uid_s, info in cascade_latest.items()}

    def transform(tj):
        eid = tj.get('event_id')
        eid_s = str(eid) if eid is not None else None
        for s in tj.get('standings', []) or []:
            uid = s.get('user_id')
            if uid is None: continue
            uid_s = str(uid)
            shared_lv = uid_to_shared_lv.get(uid_s)
            shared_lv_key = str(shared_lv) if shared_lv else None
            embedded_by_lv = s.pop('tjpr_by_lv', None)
            # V4 per_tour_by_lv 優先、なければ embedded fallback
            v4_by_lv = None
            if per_tour_by_lv_v4 is not None and eid_s is not None:
                v4_by_lv = per_tour_by_lv_v4.get(uid_s, {}).get(eid_s)
            by_lv = v4_by_lv if v4_by_lv is not None else embedded_by_lv
            if shared_lv_key and by_lv and shared_lv_key in by_lv:
                raw, pos, w, counted = by_lv[shared_lv_key]
                s['tjpr_lv'] = shared_lv
                s['tjpr_raw'] = raw
                s['tjpr_w'] = w
                s['tjpr_counted'] = bool(counted)
            else:
                s['tjpr_lv'] = 0
                s['tjpr_raw'] = 0.0
                s['tjpr_w'] = 0.0
                s['tjpr_counted'] = False
            # BT override (= 新方式 peak180/internal delta)
            if eid_s is not None:
                info = bt_per_tour_all.get(uid_s, {}).get(eid_s)
                if info is not None:
                    s['bt_used'] = bool(info[0])
                    s['bt_d'] = float(info[1])
                    s['bt_internal_d'] = float(info[2])
            _override_global_ranks(s)
        # highlights[] にも standings entry の複製が入っている (= 同じく stale ランク)
        for hl_list in (tj.get('highlights') or {}).values():
            if not isinstance(hl_list, list): continue
            for e in hl_list:
                if isinstance(e, dict) and 'global_ranks' in e:
                    _override_global_ranks(e)
        # matches[] 上書き: bt_per_match[eid][f'{wid}-{lid}'] = [[d_w, d_l, t_w, t_l], ...]
        # (per-occurrence: 進行順で行と対応付ける。同方向複数対戦の 2 戦目以降にも
        #  それぞれの試合の delta が付く。)
        if bt_per_match_all and eid_s is not None:
            event_matches = bt_per_match_all.get(eid_s, {})
            _seen: dict = {}
            for m in sorted(tj.get('matches', []) or [], key=_bracket_order_key_dict):
                wid = m.get('w_id')
                lid = m.get('l_id')
                if wid is None or lid is None:
                    continue
                key = f'{wid}-{lid}'
                i = _seen.get(key, 0)
                _seen[key] = i + 1
                occs = _as_occurrence_list(event_matches.get(key))
                if occs is None:
                    m['w_d'] = 0.0
                    m['l_d'] = 0.0
                    m['w_tracked'] = False
                    m['l_tracked'] = False
                else:
                    info = occs[i] if i < len(occs) else occs[-1]
                    m['w_d'] = float(info[0])
                    m['l_d'] = float(info[1])
                    m['w_tracked'] = bool(info[2])
                    m['l_tracked'] = bool(info[3])
        return tj
    return transform


# ───────────────── meta.json (旧 build_v4_full._overlay_update_meta) ─────────────────
def transform_meta(meta: dict) -> dict:
    """meta.json に V4 ranking_method マーカーと TJPR_ELO_SCALE x50 を書き込み."""
    meta['ranking_method'] = 'V4_Glicko2_ens_LV_pure_singlepass_LV1_5_LV2_9_LV3wk_LV4_nent32'
    meta['ranking_method_notes'] = (
        'Glicko2 BT 単一パス + 総合評価カスケード + LV1 参加者 5 名以上 + '
        'LV2 参加者 9 名以上 + LV3 休日必須 + LV4 参加者 33 名以上.')
    if 'params' in meta and 'TJPR_ELO_SCALE' in meta['params']:
        meta['params'].setdefault('TJPR_ELO_SCALE_BASE', meta['params']['TJPR_ELO_SCALE'])
        meta['params']['TJPR_ELO_SCALE'] = meta['params']['TJPR_ELO_SCALE_BASE'] * 50
    return meta


class V4Overlay:
    """inter (学習結果) から 4 つの変換を用意する。v4/build.run_build が RunConfig.overlay で受け取る。"""

    def __init__(self, inter: dict):
        # 直列化した形の bt_timeline / bt_per_tour / bt_per_match (今の inter には無く、生の dict の窓。spsp/lazy_views.py)
        from spsp.lazy_views import overlay_inputs
        self._bt_timeline, self._bt_per_tour, self._bt_per_match = overlay_inputs(inter)
        peak180_at_day_0 = _compute_peak180_at_day(
            self._bt_timeline, [0], inter['snap_ts_by_day'],
            precomputed=inter['peak180_at_day'])[0]
        bt_rank_at_day_0 = _build_bt_rank_at_day(
            {0: peak180_at_day_0}, measured_at_day=inter['measured_at_day'])[0]
        cascade_latest = inter['cascade_snapshots'].get('0', {})
        _main_id = _build_main_char(inter.get('char_usage') or {})
        print(f"  main-char: {len(_main_id)} ids", flush=True)
        self.slim = make_slim_transform(cascade_latest, peak180_at_day_0, bt_rank_at_day_0,
                                        inter['eval_date'],
                                        per_tour_by_lv_v4=inter.get('per_tour_by_lv', {}),
                                        measured_day0=inter['measured_at_day'].get(0),
                                        main_char_id=_main_id)
        self.player = make_player_transform(inter['cascade_snapshots'],
                                            self._bt_timeline,
                                            self._bt_per_tour,
                                            self._bt_per_match,
                                            inter['snap_days'],
                                            inter['snap_ts_by_day'],
                                            inter['eval_date'],
                                            peak180_at_day_pre=inter['peak180_at_day'],
                                            per_tour_by_lv_v4=inter.get('per_tour_by_lv', {}),
                                            char_usage=inter.get('char_usage', {}),
                                            measured_at_day=inter['measured_at_day'],
                                            class_bt_split=inter.get('class_bt_split', {}),
                                            peak_track=inter.get('peak_track'))
        self._cascade_latest = cascade_latest
        self._inter = inter
        self.final_ranks: dict = {}   # uid_str -> ranks (変換後の jsonl から。大会 JSON の global_ranks に使う)
        self._tournament = None
        self.meta = transform_meta

    def note_final_ranks(self, slim_final: dict) -> None:
        rk = slim_final.get('ranks') or {}
        self.final_ranks[str(slim_final.get('user_id'))] = {
            'ensemble': rk.get('ensemble'),
            'tjpr': rk.get('tjpr'),
            'bt_gated': rk.get('bt_gated'),
        }

    def tournament(self, tj: dict) -> dict:
        if self._tournament is None:   # 全選手の jsonl 変換 (final_ranks) が終わってから作る
            self._tournament = make_tournament_transform(
                self._cascade_latest, self._bt_per_tour, self._bt_per_match,
                per_tour_by_lv_v4=self._inter.get('per_tour_by_lv', {}), final_ranks=self.final_ranks)
        return self._tournament(tj)

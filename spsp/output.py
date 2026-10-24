"""V4 出力モジュール.

build_tjpr_ranking.py の main() 内 4 つの inline writer ブロックを
関数化して切り出し: jsonl (slim) / per-player JSON / per-tournament JSON / meta.json.

設計方針:
  - 各 writer は「record dict assemble + ファイル write」を担当.
  - record assemble は pure な dict literal で、引数は前計算済の辞書 / list を受ける.
  - main() 側の per-player loop / per-tournament loop は維持し、
    ループ末尾で本モジュールの関数を呼ぶことで output 出力を集約.

依存:
  - v4.common: RADAR clip 値 / TJPR・JJPR / LV パラメータ / Elo offset
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import time
from pathlib import Path

from collections import defaultdict
from collections.abc import Mapping

# 軽量ビルド (RunConfig.light) で 2万超の per-player/tournament JSON 書き出しが
# ZFS ARC / page cache を膨張させ overlay フェーズで cgroup OOM を起こすのを防ぐため、
# 書き込み直後に posix_fadvise(DONTNEED) でキャッシュから追い出す。本番非破壊 (flag OFF で no-op)。
_EVICT_CACHE = False


def set_light(flag: bool) -> None:
    """run_build() が RunConfig.light を反映するために呼ぶ。"""
    global _EVICT_CACHE
    _EVICT_CACHE = bool(flag)


def _evict_path(path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
    except Exception:
        pass

from spsp.common import (
    LV1_PARAMS, LV2_PARAMS,
    LV3_FILTER, LV4_FILTER, LV5_FILTER,
    TJPR_DEFAULT_RATE,
    OSK_BETA, ELO_PER_UNIT,
    GATED_M_TOUR, JJPR_TOP_K,
    TJPR_ELO_SCALE, TJPR_ELO_OFFSET, BT_ELO_OFFSET,
    LOOKBACK_DAYS, JJPR_LOOKBACK_DAYS, snapshot_grid_days,
    placement_to_w2w,
)
from spsp.country_ja import country_ja, load_country_map
from spsp import common as _common
from spsp import tour_cache as _tour_cache
from spsp.overlay import json_roundtrip, _np_scalar_to_py
from spsp.columnar import IntCounts, TourEntryStore, tour_value
from spsp.split_output import split_detail, split_tournament_doc, current_file_dict, encode_current_row
from spsp.score import history_event_rows as _history_event_rows, merge_history as _merge_history
from spsp.score import TOP_UFS_N as _TOP_UFS_N, top_uf_key as _top_uf_key
from spsp.rolling import rolling_raw_at
import orjson


# ─── from build_tjpr_ranking (1407-1421): per-player JSONL slim record ───
def build_slim_record(main_rec: dict) -> dict:
    """JSONL 用の slim record を main_rec から組み立てる.

    index page で最初に読まれる全 player ファイルなのでサイズ削減のため field を絞る.
    per-player page (= players/<uid>.json) には main_rec を含む detail_rec が書き込まれる (= full).
    """
    return {
        'user_id': main_rec['user_id'],
        'display': main_rec['display'],
        'ranks': {k: v for k, v in main_rec['ranks'].items()
                  if k in ('ensemble', 'tjpr', 'bt_gated')},
        'scores': {k: v for k, v in main_rec['scores'].items()
                   if k in ('tjpr_elo', 'tjpr_level', 'tjpr_score',
                            'bt_gated_elo', 'bt_internal_elo', 'bt_internal_d_last',
                            'ensemble_avg_rank', 'ensemble_avg_score',
                            'shared_cascade_lv', 'shared_cascade_ens_r')},
        'metadata': {k: v for k, v in main_rec['metadata'].items()
                     if k in ('tour_count_3y', 'bt_weekday_included')},
        'top_tours': main_rec.get('top_tours'),
    }


# ─── from build_tjpr_ranking (1595-1645): per-player detail JSON record ───
def build_detail_record(
    uid: int,
    display: str,
    main_rec: dict,
    *,
    tjpr_level_scores: dict,
    tournament_entries: list,
    history: list,
    bt_timeline: dict,
    g2_timeline: dict,
    top_tjpr_contribs: list,
    top_bt_gains: list,
    top_spr: list,
    recent_spr: list,
    recent_matches: list,
    pct: dict,
    spr_upper_val: dict,
    spr_neg_val: dict,
    uf_upper_val: dict,
    uf_lost_val: dict,
    peak_ranks: dict,
    peak_ranks_1y: dict,
    max_uf_rec,
    top_ufs_resolved: list,
    max_spr_rec,
    min_spr_rec,
    max_losers_run_rec,
    users: dict,
    achievements: list,
    dynamic_badges: list,
) -> dict:
    """player detail JSON (= players/<uid>.json) の record を組み立てる.

    main_rec 由来の ranks/scores/metadata を埋め込み、トーナメント詳細 / 履歴 /
    radar / achievements / dynamic badges を統合する.
    """
    return {
        'user_id': uid,
        'display': display,
        'ranks': main_rec['ranks'],
        'scores': main_rec['scores'],
        'metadata': main_rec['metadata'],
        'tjpr_lv_breakdown': tjpr_level_scores.get(uid, {}),
        'tournaments': tournament_entries,  # 全期間 (= プレイヤーページで pagination 表示)
        'history': history,
        # 大会終了時点の (date, mu, sigma, ord) timeline. フロントエンドが
        # この dense トラジェクトリから 180 日窓で直対評価スコア (peak180) を描画.
        'bt_timeline': [
            {'date': dt.datetime.fromtimestamp(ts).date().isoformat(),
             'mu': round(mu, 4), 'sigma': round(sigma, 4), 'ord': round(o, 4)}
            for (ts, mu, sigma, o) in bt_timeline.get(uid, [])
        ],
        # [EXPERIMENTAL] Glicko2 timeline (= 直対評価チャート用; Elo-space rating そのまま).
        'bt_timeline_g2': [
            {'date': dt.datetime.fromtimestamp(ts).date().isoformat(),
             'rating': round(r, 2), 'rd': round(rd, 2)}
            for (ts, r, rd) in g2_timeline.get(uid, [])
        ],
        'top_tjpr_contribs': top_tjpr_contribs,
        'top_bt_gains': top_bt_gains,
        'top_spr': top_spr,
        'recent_spr': recent_spr,
        'recent_matches': recent_matches,
        'radar': {
            'tjpr_pt':       pct['tjpr_pt'].get(uid, 0),
            'spr_upper':     pct['spr_upper'].get(uid, 0),
            'spr_stability': pct['spr_stability'].get(uid, 0),
            'bt_pt':         pct['bt_pt'].get(uid, 0),
            'uf_upper':      pct['uf_upper'].get(uid, 0),
            'uf_stability':  pct['uf_stability'].get(uid, 0),
            # 生集計値 (None = データ不足) — radar 表示で対数圧縮に使う
            'spr_upper_raw': spr_upper_val.get(uid),
            'spr_neg_raw':   spr_neg_val.get(uid),
            'uf_upper_raw':  uf_upper_val.get(uid),
            'uf_lost_raw':   uf_lost_val.get(uid),
        },
        'peak_ranks': peak_ranks,
        'peak_ranks_1y': peak_ranks_1y,
        'max_uf': max_uf_rec,
        'top_ufs': top_ufs_resolved,
        'max_spr': max_spr_rec,
        'min_spr': min_spr_rec,
        'max_losers_run': max_losers_run_rec,
        'startgg_discriminator': (users.get(uid) or {}).get('startgg_discriminator'),
        # 国 (smash_db_tournament 側 users.jsonl 由来; start.gg 未登録者は None)
        'country': load_country_map().get(uid),
        'country_ja': country_ja(load_country_map().get(uid)),
        'achievements': achievements,
        'dynamic_badges': dynamic_badges,
    }


# ─── write helpers (= ファイルへの出力) ───
# 書き出しは orjson (2026-09-07〜)。標準 json の約 7 倍速く (1.4 GB で 7.3 秒 → 1.0 秒)、非 ASCII はそのまま
# (ensure_ascii=False と同じ)。区切りが `,` `:` (スペース無し) なので 2026-09-05 以前の出力とはバイトが違うが
# JSON.parse した値は同じ (golden 比較は内容一致: tests/golden/compare_trees.py)。int キーは文字列に
# (OPT_NON_STR_KEYS、標準 json と同じ)、numpy の float は _np_scalar_to_py で Python float に。
_ORJSON_OPT = orjson.OPT_NON_STR_KEYS


def dump_json_bytes(obj) -> bytes:
    return orjson.dumps(obj, default=_np_scalar_to_py, option=_ORJSON_OPT)


def write_player_slim(fp, slim_rec: dict) -> None:
    """JSONL 1 行書き込み (= site/latest_tjpr_full.jsonl)。fp はバイナリモード."""
    fp.write(dump_json_bytes(slim_rec) + b'\n')


def write_player_detail(players_dir: Path, uid: int, detail_rec: dict) -> None:
    """per-player JSON 書き込み (= site/players/<uid>.json)."""
    p = players_dir / f"{uid}.json"
    p.write_bytes(dump_json_bytes(detail_rec))
    if _EVICT_CACHE:
        _evict_path(p)


def write_player_history(history_dir: Path, uid: int, rows: list) -> None:
    """固定グリッド日付のランク履歴 (= site/history/<uid>.json)。"""
    p = history_dir / f"{uid}.json"
    p.write_bytes(dump_json_bytes(rows))
    if _EVICT_CACHE:
        _evict_path(p)


def write_players_current(path: Path, eval_date: dt.datetime, players: dict) -> None:
    """全選手の揮発部分 (= site/players_current.json)。1 ファイル。"""
    path.write_bytes(dump_json_bytes(current_file_dict(eval_date, players, _common.current_decay_r())))
    if _EVICT_CACHE:
        _evict_path(path)


# ─── from build_tjpr_ranking (1799-1818): per-tournament JSON record ───
def build_tournament_doc(
    eid: int,
    meta_t: dict,
    standings: list,
    highlights: dict,
    matches_out: list,
) -> dict:
    """per-tournament JSON (= site/tournaments/<eid>.json) の record を組み立てる."""
    return {
        'event_id': eid,
        'name': meta_t.get('tname', ''),
        'event_name': meta_t.get('ename', ''),
        'startgg_url': meta_t.get('startgg_url'),
        'date': meta_t.get('date'),
        'end_date': meta_t.get('end_date'),
        'nent': meta_t.get('nent'),
        'is_weekend': meta_t.get('is_weekend'),
        'is_weekend_real': meta_t.get('is_weekend_real', meta_t.get('is_weekend')),
        'is_smapa': meta_t.get('is_smapa'),
        'is_pre': meta_t.get('is_pre', False),
        'is_restricted': meta_t.get('is_restricted', False),
        'is_lower_class': meta_t.get('is_lower_class', False),
        'is_random_seed': meta_t.get('is_random_seed', False),   # シードが強さ順でない (UF/SPR なし)
        'is_gf_missing': meta_t.get('is_gf_missing', False),
        'is_awaiting_resume': meta_t.get('is_awaiting_resume', False),  # 再開待ち: 途中結果で暫定集計
        'is_small': meta_t.get('is_small', False),
        'tags': _common.flag_tags(meta_t),
        'standings': standings,
        'highlights': highlights,
        'matches': matches_out,
    }


def write_tournament_doc(tournaments_dir: Path, eid: int, t_doc: dict) -> None:
    """per-tournament JSON 書き込み (= site/tournaments/<eid>.json)."""
    p = tournaments_dir / f"{eid}.json"
    p.write_bytes(dump_json_bytes(t_doc))
    if _EVICT_CACHE:
        _evict_path(p)


# ─── from build_tjpr_ranking (~800-943): per-tournament JSON build + write loop (反復15) ───
def _display_of(uid, users, display_cache, format_display):
    """display_cache fallback → format_display で表示名を解決."""
    return display_cache.get(uid) or format_display(users.get(uid), uid)


def _perf_map_for_t(placements_t: dict, at_time_t: dict) -> dict:
    """uid → パフォーマンス相当全国順位 (perf_rank) のマップ.

    パフォーマンス = その順位が SPSP 換算で全国何位相当か。
    エントラントを大会前の全国順位 (at_time の ens_before) で並べた SPSP シード順を
    基準に、各順位帯 (同 place の人数分のシード枠 p..p+size-1) に入る選手の
    ens_before の中央値 (round)。
      - 自分の SPSP シード位置が獲得順位帯の枠内 (= SPSP シード通りの結果) なら
        帯平均ではなく自分の当時順位をそのまま使う (実シードが SPSP 順とは
        限らないので start.gg の SPR ではなく SPSP シード位置で判定)。
      - 優勝 (place=1) はトップシード 1 枠のみ (= エントラント中最良の順位。
        優勝者自身もエントラントなので自分の当時順位より悪くはならない)。
      - ランク無し (計測中/海外/未収録) はシード最後尾扱いで、中央値からは除外。
        帯内に有ランクの選手がいなければ帯の値は出せない (その uid は出力なし)。
    """
    entries = [(uid_, p) for uid_, p in placements_t.items() if p is not None]
    if not entries:
        return {}
    at = at_time_t or {}

    # 大会前の全国順位は 1 人 1 回だけ引く (at_time_t は配列の表で、引くたびに値を作り直す)
    pre = {uid_: (at.get(uid_) or {}).get('ens_before') for uid_, _ in entries}
    _pre = pre.__getitem__

    def _seed_key(e):
        b = pre[e[0]]
        return (b if b is not None else 10 ** 9, e[0])

    seeded = sorted(entries, key=_seed_key)
    spsp_pos = {uid_: i + 1 for i, (uid_, _) in enumerate(seeded)}
    sizes: dict = {}
    for _, p in entries:
        sizes[p] = sizes.get(p, 0) + 1

    tier_avg: dict = {}
    for place, size in sizes.items():
        if place == 1:
            size = 1
        ranks = [_pre(u) for u, _ in seeded[place - 1: place - 1 + size]]
        ranks = sorted(r for r in ranks if r is not None)
        if ranks:
            # 中央値 (2026-08: 平均から変更)。順位分布は右裾が長く、広い帯では
            # 後方の外れ値 (無名勢の #4000 級など) が平均を系統的に悪化させるため、
            # 「帯に入る典型的な選手の順位」= 中央値の方がパフォーマンスの意図に合う。
            n = len(ranks)
            mid = n // 2
            med = ranks[mid] if n % 2 == 1 else (ranks[mid - 1] + ranks[mid]) / 2
            tier_avg[place] = round(med)

    out = {}
    for uid_, p in entries:
        size = 1 if p == 1 else sizes[p]
        own_pre = _pre(uid_)
        if own_pre is not None and p <= spsp_pos[uid_] <= p + size - 1:
            out[uid_] = own_pre
        elif tier_avg.get(p) is not None:
            out[uid_] = tier_avg[p]
    return out


def _lv_dict(m) -> dict:
    """dict(m) と同じ (spsp/columnar の 1 人 × 1 大会の {lv: [...]} なら to_dict で)。"""
    f = getattr(m, 'to_dict', None)
    return f() if f is not None else dict(m)


def _local_dict(m):
    """読むだけの表 (spsp/columnar の 1 大会分の見え方など) を同じキー・値・並びの dict にする (dict ならそのまま)。"""
    return dict(m.items()) if type(m) is not dict and isinstance(m, Mapping) else m


def _build_tour_standings(placements, rated_data, seed_info_t, deltas_t,
                          users, display_cache, format_display, at_time_t=None, by_lv_of=None):
    """rated + unrated 出席者の standings list.

    sort key: place 昇順 (None / 欠落は最後).
    at_time_t: 実 cascade スナップショット由来の「当時の」状態 dict[uid -> {...}].
      当時のレベル / 全国順位 / 順位変動 (前→後) はこちらを使う (rolling ではない).
    deltas_t: 当時その大会で獲得した raw TJPR pt (raw_at) の取得にのみ使う.
    """
    at_time_t = at_time_t or {}
    perf_map = _perf_map_for_t(placements, at_time_t)
    standings = []
    for uid_, place_ in placements.items():
        if place_ is None: continue
        r = rated_data.get(uid_)
        if r is not None:
            entry = (_per_tournament_entry(r, display_cache[uid_], by_lv_of(uid_) if by_lv_of else {})
                     if type(r) is tuple else r)
        else:
            si_ = seed_info_t.get(uid_)
            entry = {
                'user_id': uid_, 'display': _display_of(uid_, users, display_cache, format_display),
                'place': place_,
                'seed': si_['seed'] if si_ else None,
                'spr': si_['spr'] if si_ else None,
                'global_ranks': {'ensemble': None, 'tjpr': None, 'bt_gated': None},
                'tjpr_w': 0.0, 'tjpr_raw': 0.0, 'tjpr_lv': 0, 'tjpr_counted': False,
                'tjpr_by_lv': {},
                'bt_d': 0.0, 'bt_internal_d': 0.0, 'bt_used': False,
            }
        _overlay_at_time_entry(entry, entry['user_id'], at_time_t, deltas_t)
        entry['perf_rank'] = perf_map.get(entry['user_id'])
        standings.append(entry)
    standings.sort(key=lambda x: x.get('place') or 10**9)
    return standings


def _build_tour_match_upsets(t, seed_info_t, users, display_cache, format_display, hl_max):
    """winner_seed > loser_seed の試合を W2W gap (UF) で sort して top hl_max 件返す.
    ランダムシード大会 (build が spr を None にする) は空."""
    if any(si.get('spr') is None for si in seed_info_t.values()):
        return []
    arr = []
    for m in t.matches:
        sw_info = seed_info_t.get(m.winner_id)
        sl_info = seed_info_t.get(m.loser_id)
        if not sw_info or not sl_info: continue
        sw, sl = sw_info['seed'], sl_info['seed']
        if sw <= sl: continue
        uf = placement_to_w2w(sw) - placement_to_w2w(sl)
        if uf <= 0: continue
        arr.append({
            'winner': {'user_id': m.winner_id,
                       'display': _display_of(m.winner_id, users, display_cache, format_display),
                       'seed': sw, 'place': sw_info.get('place')},
            'loser':  {'user_id': m.loser_id,
                       'display': _display_of(m.loser_id, users, display_cache, format_display),
                       'seed': sl, 'place': sl_info.get('place')},
            'uf': uf,
        })
    arr.sort(key=lambda x: -x['uf'])
    return arr[:hl_max]


def _build_tour_losers_runs(seed_info_t, lr_map_t, users, display_cache, format_display, hl_max):
    """大会内の各プレイヤーの losers run (= LB 連勝 + 該当 GF 勝利) を top hl_max 件返す."""
    if not lr_map_t:
        return []
    arr = []
    for uid_lr, run_len in lr_map_t.items():
        if run_len < 1:
            continue
        si_lr = seed_info_t.get(uid_lr) or {}
        arr.append({
            'user_id': uid_lr,
            'display': _display_of(uid_lr, users, display_cache, format_display),
            'seed': si_lr.get('seed'),
            'place': si_lr.get('place'),
            'losers_run': run_len,
        })
    arr.sort(key=lambda x: -x['losers_run'])
    return arr[:hl_max]


def _set_at_time_base(t_doc, base_t) -> None:
    """順位表とハイライトの tjpr_raw (ポイントのベース) を当時の値に (spsp/score.at_time_base_points)。表に無い選手は 0.0。"""
    for s in t_doc.get('standings') or []:
        if 'tjpr_raw' in s:
            s['tjpr_raw'] = base_t.get(s.get('user_id'), 0.0)
    for lst in (t_doc.get('highlights') or {}).values():
        if isinstance(lst, list):
            for e in lst:
                if isinstance(e, dict) and 'tjpr_raw' in e:
                    e['tjpr_raw'] = base_t.get(e.get('user_id'), 0.0)


def _event_chars(event_path) -> dict:
    """{uid の文字列: [その大会で使ったキャラの id, …]} (セット数の多い順、同じなら game 数の多い順、同じなら id 順)。
    matches.json の試合ごとのキャラ (details、spsp/char_usage と同じ読み方。1 セットで同じキャラは 1 回)。無ければ {}。"""
    from spsp.char_usage import _iter_event_details
    per: dict = {}
    for uid, cid, _name, games in _iter_event_details(event_path):
        e = per.setdefault(uid, {}).setdefault(cid, [0, 0])
        e[0] += 1
        e[1] += games
    return {str(uid): [cid for cid, _ in sorted(chars.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))]
            for uid, chars in sorted(per.items())}


def _event_chars_stamp(event_path):
    """大会ファイルの鍵に足す: matches.json の大きさ (試合ごとのキャラは後から取り直されることがあり、学習の台帳の
    ダイジェストには入らない。中身を読まずに変わったことがわかるように)。"""
    try:
        return os.stat(os.path.join(event_path, 'matches.json')).st_size
    except OSError:
        return None


def _build_tour_highlights(standings, match_upsets, top_losers_runs, hl_max):
    """tournament highlights dict (top_* 7 種)."""
    def _top(field, want_positive=True):
        arr = [s for s in standings
               if s.get(field) is not None and (s.get(field) > 0 if want_positive else True)]
        arr.sort(key=lambda x: -x[field])
        return arr[:hl_max]

    def _top_rank_gain(delta_field, rank_field):
        """順位上昇を after/before 比 (= どこまで縮めたか) で sort.
        例: 20位 → 5位 (ratio=0.25) が 2位 → 1位 (ratio=0.5) より「すごい」.
        before = 参加前順位 (pretour_ranks), after = before - delta.
        """
        arr = []
        for s in standings:
            d = s.get(delta_field)
            if d is None or d <= 0: continue
            ranks = s.get('pretour_ranks') or {}
            before = ranks.get(rank_field)
            if before is None or before <= 0: continue
            after = before - d
            if after <= 0: continue
            arr.append((after / before, s))
        arr.sort(key=lambda x: x[0])
        return [s for _, s in arr[:hl_max]]

    return {
        'top_match_upsets': match_upsets,
        # 順位評価上昇: 当時この大会で順位評価スコアが上がった量 (= tjpr_pts_at)。
        # 旧実装の tjpr_raw (減衰前ベース pt) は placement 順とほぼ同じで
        # ランキングとして無意味だった (2026-07-21 修正)。key 名は互換のため維持。
        'top_contrib_points': _top('tjpr_pts_at'),
        'top_spr_gains': _top('spr'),
        'top_bt_gains': _top('bt_d'),
        'top_losers_runs': top_losers_runs,
        # 順位上昇 (snapshot 補間ベース). after / before 比昇順 = 何 % まで縮めたか.
        'top_rank_gains_ensemble': _top_rank_gain('rank_delta_ensemble', 'ensemble'),
        'top_rank_gains_tjpr':     _top_rank_gain('rank_delta_tjpr',     'tjpr'),
        'top_rank_gains_bt':       _top_rank_gain('rank_delta_bt',       'bt_gated'),
    }


def _build_tour_matches_out(t, match_id_deltas):
    """全 match list (per-player click 詳細表示用). 軽量 schema + per-match BT delta."""
    out = []
    # spsp/build.py は大会ごとの配列 (spsp/columnar.TourMatchDeltas、t.matches の順) で持つ
    mds = (match_id_deltas.of_tournament(t) if hasattr(match_id_deltas, 'of_tournament')
           else [match_id_deltas.get(id(m)) for m in t.matches])
    for m, md_ in zip(t.matches, mds):
        md_ = md_ or {}
        if type(md_) is tuple:   # spsp/build.py は (d_w, d_l, w_tracked, l_tracked) で持つ (dict より小さい。2026-10-17)
            md_ = {'d_w': md_[0], 'd_l': md_[1], 'w_tracked': md_[2], 'l_tracked': md_[3]}
        out.append({
            'w_id': m.winner_id, 'l_id': m.loser_id,
            'w_score': getattr(m, 'winner_score', None),
            'l_score': getattr(m, 'loser_score', None),
            'round_text': getattr(m, 'round_text', None),
            'global_top_x': getattr(m, 'global_top_x', None),
            'global_bracket_label': getattr(m, 'global_bracket_label', None),
            'bracket_type': _common.match_bracket_type(m),
            'is_class': bool(getattr(m, 'is_class', False)),
            'w_d': md_.get('d_w', 0.0), 'l_d': md_.get('d_l', 0.0),
            'w_tracked': md_.get('w_tracked', False),
            'l_tracked': md_.get('l_tracked', False),
        })
    return out


def build_and_write_all_tournament_docs(
    tournaments_dir: Path,
    all_tournaments: list,
    *,
    # ── per-tournament maps ──
    t_meta_by_id: dict,
    placements_per_t: dict,
    t_seed_info: dict,
    per_tournament_data: dict,
    per_tour_by_lv: dict,
    rolling_deltas: dict,
    at_time_map: dict,
    match_id_deltas: dict,
    per_tournament_losers_runs: dict,
    at_time_base=None,   # spsp/score.at_time_base_points ({tid: {uid: 当時のポイントのベース}})。None なら overlay の値のまま
    # ── per-uid maps (display fallback) ──
    users: dict,
    display_cache: dict,
    # ── helpers ──
    format_display,
    # ── flags ──
    no_tournament_json: bool = False,
    overlay=None,
    tour_digest: dict | None = None,
) -> int:
    """全 tournament の per-tournament JSON (= site/tournaments/<eid>.json) を組み立て書き出す.

    各 tournament について:
      1. standings 構築 (rated + unrated 出席者)
      2. match upsets / losers runs / highlights 計算
      3. matches 出力 (BT delta overlay)
      4. build_tournament_doc + write_tournament_doc 呼び出し

    Returns:
        n_tourns_written: 書き出した tournament 数 (= virtual / no-placement は除く)
    """
    print(
        "Skipping per-tournament JSON files (--no-tournament-json) ..."
        if no_tournament_json
        else "Building per-tournament JSON files ...",
        flush=True,
    )
    HL_MAX = 30  # UI: 5 件デフォルト + もっと見る
    n_tourns_written = 0
    _t_tours = time.perf_counter()
    # SPSP_TOUR_CACHE=1: 大会そのもののデータが前回と同じ大会は作らずに前回のファイルを残す (spsp/tour_cache.py)
    cache = _tour_cache.TourDocCache(tournaments_dir) if (not no_tournament_json and _tour_cache.enabled()) else None
    for t in (all_tournaments if not no_tournament_json else []):
        tid = id(t)
        meta_t = t_meta_by_id[tid]
        if meta_t.get('is_class_virtual'):
            continue  # virtual sub-tournament: no per-tournament page
        eid = meta_t.get('event_id')
        if eid is None: continue
        placements = placements_per_t.get(tid, {})
        if not placements: continue
        if cache is not None:
            _tk = time.perf_counter()
            key = _tour_cache.tour_key(cache.epoch, t, meta_t, t_seed_info.get(tid),
                                       digest=tour_digest.get(tid) if tour_digest else None,
                                       extra=_event_chars_stamp(t.event_path))
            cache.t_key += time.perf_counter() - _tk
            if cache.keep(eid, key):
                n_tourns_written += 1
                continue
        # この大会の間に参加者ごとに何度も引く表は dict にしておく (配列の表の 1 大会分は 1 件ずつ二分探索で引く。
        # 1 大会分なので小さく、この大会を書き終えたら手放す)
        seed_info_t = _local_dict(t_seed_info.get(tid, {}))
        standings = _build_tour_standings(
            placements, per_tournament_data.get(tid, {}), seed_info_t,
            _local_dict(rolling_deltas.get(tid, {})), users, display_cache, format_display,
            at_time_t=_local_dict(at_time_map.get(tid, {})),
            by_lv_of=lambda u, _tid=tid: per_tour_by_lv.get(u, {}).get(_tid, {}))
        match_upsets = _build_tour_match_upsets(
            t, seed_info_t, users, display_cache, format_display, HL_MAX)
        top_losers_runs = _build_tour_losers_runs(
            seed_info_t, per_tournament_losers_runs.get(tid, {}),
            users, display_cache, format_display, HL_MAX)
        highlights = _build_tour_highlights(standings, match_upsets, top_losers_runs, HL_MAX)
        matches_out = _build_tour_matches_out(t, match_id_deltas)
        t_doc = build_tournament_doc(eid, meta_t, standings, highlights, matches_out)
        t_doc['chars'] = _event_chars(t.event_path)   # その大会で使ったキャラ (大会ページの絵文字は当時のもの)
        if overlay is not None:
            t_doc = overlay.tournament(json_roundtrip(t_doc))
        if at_time_base is not None:
            _set_at_time_base(t_doc, at_time_base.get(tid) or {})
        # 参加者の現在状態 (global_ranks / tjpr_w …) は落とす。大会ページは players_current.json から引く
        # (docs/refactor/04_player_json_split.md §3.4)
        if cache is None:
            write_tournament_doc(tournaments_dir, eid, split_tournament_doc(t_doc))
        else:
            cache.write(eid, key, dump_json_bytes(split_tournament_doc(t_doc)),
                        evict=_evict_path if _EVICT_CACHE else None)
        n_tourns_written += 1
    if cache is not None:
        cache.finish()
    print(f"  Wrote {n_tourns_written} tournaments to {tournaments_dir}/ ({time.perf_counter() - _t_tours:.1f}s)", flush=True)
    return n_tourns_written


# ─── from build_tjpr_ranking (1822-1869): meta.json ───
def _effective_lv_filters() -> dict:
    """learner.LV_TFILTERS (apply_settings 後) を {lv: {min_nent_gt, weekend_only, exclude_capped}} で。"""
    from spsp import learner as _learner
    out = {}
    for lv, f in sorted((_learner.LV_TFILTERS or {}).items()):
        out[str(lv)] = {k: v for k, v in (f or {}).items() if k in ('min_nent_gt', 'weekend_only', 'exclude_capped')}
    return out


def build_meta_dict(
    *,
    eval_date: dt.datetime,
    t0: float,
    n_tournaments_total: int,
    n_tournaments_small: int = 0,
    min_entrants_for_ranking: int = 8,
    n_players: int,
    tjpr_elo: dict,
    norm_bt_gated_disp: dict,
    spr_neg_val: dict,
    uf_lost_val: dict,
    radar_spr_clip: float,
    radar_uf_clip: float,
    max_tjpr: float,
    snapshot_days: list | None = None,
) -> dict:
    """meta.json (= site/meta.json) の dict を組み立てる.

    radar チャート用 score_ranges + params (= V4 constants) を埋める.
    """
    return {
        'eval_date': eval_date.date().isoformat(),
        # 地域の TIMEZONE (common._TZ) 付き ISO 8601 (2026-09 まで naive な現地時刻だった)
        'generated_at': dt.datetime.now(_common._TZ).isoformat(),
        'computation_time_s': round(time.time() - t0, 1),
        'n_tournaments_total': n_tournaments_total,
        'n_tournaments_small': n_tournaments_small,
        'min_entrants_for_ranking': min_entrants_for_ranking,
        'n_players': n_players,
        'lookback_days': LOOKBACK_DAYS,
        'jjpr_lookback_days': JJPR_LOOKBACK_DAYS,
        # 履歴スナップショットの点 (評価日から N 日前、新しい順)。固定グリッド (common.snapshot_grid_days) なので
        # 日付 (snapshot_dates) も併記。eval_ts は減衰計算の基準 (評価日 23:59:59、= 学習の cutoff と同じ)。
        'snapshot_days': list(snapshot_days) if snapshot_days is not None else snapshot_grid_days(eval_date),
        'snapshot_dates': [(eval_date.date() - dt.timedelta(days=d)).isoformat()
                           for d in (snapshot_days if snapshot_days is not None else snapshot_grid_days(eval_date))],
        'eval_ts': int(eval_date.replace(hour=23, minute=59, second=59).timestamp()),
        'decay_r': _common.current_decay_r(),
        # radar chart 用: 全プレイヤー集計値の max (対数圧縮で max=100 とするため)
        # SPR/UF は集計時に clip 済 → 固定 max でグラフが安定する
        'score_ranges': {
            'tjpr_elo_max':     max(tjpr_elo.values())           if tjpr_elo else 1.0,
            'bt_gated_elo_max': max(norm_bt_gated_disp.values()) if norm_bt_gated_disp else 1.0,
            # [EXPERIMENTAL] radar チャートの軸スケール用 offset/floor (value-based 互換用):
            #   - tjpr_elo_offset: logScale を log(offset + v) で計算 -> 低い裾を持ち上げる (PROD 旧チャート互換)
            #   - bt_gated_elo_floor: linScale を (v - floor) / (max - floor) でマップ -> Glicko2 baseline 強調
            'tjpr_elo_offset':  44.0,
            'bt_gated_elo_floor': 250.0,
            # [v2.0] radar の TJPR 軸 (= 順位評価pt) を rank-based の power curve に切替:
            #   radar = 30 + 70 * (1 - (rank-1)/N)^alpha  with floor=30, alpha=tjpr_radar_alpha
            # alpha=8 で top 1-100 が 96 以上に収まり、トップ間の差を圧縮.
            'tjpr_n_ranked':    sum(1 for v in tjpr_elo.values() if v > 0),
            'tjpr_radar_alpha': 8.0,
            'bt_n_ranked':      sum(1 for v in norm_bt_gated_disp.values() if v > 0),
            'spr_upper_max':    radar_spr_clip,
            'spr_neg_max':      max((v for v in spr_neg_val.values() if v is not None), default=1.0),
            'uf_upper_max':     radar_uf_clip,
            'uf_lost_max':      max((v for v in uf_lost_val.values() if v is not None), default=1.0),
        },
        'params': {
            'TJPR_LV1': {'base': LV1_PARAMS[0], 'floor': LV1_PARAMS[1], 'topN': LV1_PARAMS[2], 'pos_decay': LV1_PARAMS[3]},
            'TJPR_LV2': {'base': LV2_PARAMS[0], 'floor': LV2_PARAMS[1], 'topN': LV2_PARAMS[2], 'pos_decay': LV2_PARAMS[3]},
            'LV3_FILTER': LV3_FILTER,
            'LV4_FILTER': LV4_FILTER,
            'LV5_FILTER': LV5_FILTER,
            # 設定 (Settings) 適用後の実効フィルタ (= 集計対象 / 直対の更新対象の判定に使うもの)。上の LV*_FILTER は
            # 素の定数で、本番は lv3_weekend / lv4_nent_gt で変わるので、ページはこちらを読む
            # (p/index.html の予想、sim/calc.js の LV_FILTERS)。
            'LV_FILTERS_EFFECTIVE': _effective_lv_filters(),
            'TJPR_DEFAULT_RATE': TJPR_DEFAULT_RATE,
            'OSK_BETA': OSK_BETA,
            'ELO_PER_UNIT': round(ELO_PER_UNIT, 4),
            'GATED_M_TOUR': GATED_M_TOUR,
            'JJPR_TOP_K': JJPR_TOP_K,
            'TJPR_ELO_SCALE': TJPR_ELO_SCALE,
            'TJPR_ELO_OFFSET': TJPR_ELO_OFFSET,
            'BT_ELO_OFFSET': BT_ELO_OFFSET,
            'TJPR_MAX_SCORE': max_tjpr,
        },
    }


def write_meta(meta_out_path: Path, meta: dict) -> None:
    """meta.json 書き込み."""
    Path(meta_out_path).write_text(json.dumps(meta, ensure_ascii=False, indent=2))


def build_tournament_summary(t_meta_by_id: dict) -> dict:
    """t_meta_by_id から表示用 tournament_summary dict を構築.

    class_virtual sub-tournament は event_id を None にして frontend のリンクを抑制.
    元 build_tjpr_ranking.main() の inline 構築ループを関数化 (反復16).
    """
    tournament_summary: dict = {}
    for tid, m in t_meta_by_id.items():
        # class virtual: event_id を None にして frontend のリンクを抑制
        ev_id = None if m.get('is_class_virtual') else m.get('event_id')
        tournament_summary[tid] = {
            'event_id': ev_id,
            'ts': m.get('ts'),
            'date': m['date'],
            'end_date': m.get('end_date'),
            'name': m['tname'],
            'event': m['ename'],
            'nent': m['nent'],
            'is_weekend': m['is_weekend'],
            'is_weekend_real': m.get('is_weekend_real', m['is_weekend']),
            'is_smapa': m['is_smapa'],
            'is_pre': m.get('is_pre', False),
            'is_restricted': m.get('is_restricted', False),
            'is_lower_class': m.get('is_lower_class', False),
            'is_random_seed': m.get('is_random_seed', False),
            'is_class_virtual': m.get('is_class_virtual', False),
            'parent_event_id': m.get('parent_event_id'),
            'class_letter': m.get('class_letter'),
            'is_gf_missing': m.get('is_gf_missing', False),
            'is_awaiting_resume': m.get('is_awaiting_resume', False),
        }
    return tournament_summary


def run_aux_outputs() -> None:
    """series.json + tournaments.json (= 大会一覧) の副次的ビルド出力を実行.

    skip したいときは RunConfig.run_aux_outputs=False にする (呼び出し側 run_build が見る).

    build_series_json / build_tournaments_index は build/ 配下の独立 module で、
    importlib 経由で動的 import して main() を実行する.
    Note: tournaments.json (= 大会一覧) は site/tournaments/*.json (per-tournament JSON)
    とは別物の集約 index ファイル.

    元 build_tjpr_ranking.main() 末尾の inline ブロックを関数化 (反復16).
    """
    import importlib

    # ── series.json (ローカルランキング機能用) を生成 ──
    try:
        print("Generating series.json for local ranking...", flush=True)
        bsj = importlib.import_module("build_series_json")
        bsj.main([])  # 既定 = site/ (旧来の挙動)
    except Exception as e:
        print(f"  WARN: series.json 生成失敗: {e}", flush=True)

    # ── tournaments.json (大会一覧ページ用) を生成 ──
    try:
        print("Generating tournaments.json for event list...", flush=True)
        bti = importlib.import_module("build_tournaments_index")
        bti.main([])
    except Exception as e:
        print(f"  WARN: tournaments.json 生成失敗: {e}", flush=True)

    # upcoming.json は download.py で生成する (= --upcoming_out オプション).
    # build pipeline からは外した. ビルドは API call せず純粋なローカル計算のみ.


# ─── from build_tjpr_ranking (722-1325): per-player record assembly + write loop ───
def _bt_after_elo(tid, bt_hist_by_tid: dict) -> tuple:
    """大会後の (cur_elo, peak_elo) を返す. 該当なしは (None, None)."""
    e = bt_hist_by_tid.get(tid)
    if not e: return (None, None)
    cur_after = e[1].ordinal() if hasattr(e[1], 'ordinal') else None
    peak_after = e[4] if len(e) >= 5 else None
    cur_elo = cur_after * ELO_PER_UNIT + BT_ELO_OFFSET if cur_after is not None else None
    peak_elo = peak_after * ELO_PER_UNIT + BT_ELO_OFFSET if peak_after is not None else None
    return (cur_elo, peak_elo)


_SLIM_FLAG_KEYS = ('is_weekend', 'is_pre', 'is_gf_missing', 'is_awaiting_resume',
                   'is_restricted', 'is_lower_class', 'is_dq')


def _slim_tour_entry(e: dict) -> dict:
    """tournament_entry を slim 化 (highlight 用 = top_tjpr_contribs / top_spr / recent_spr)."""
    return {
        'event_id': e.get('event_id'),
        'date': e['date'], 'name': e['name'], 'event': e['event'],
        'place': e['place'], 'nent': e['nent'],
        'seed': e.get('seed'), 'spr': e.get('spr'),
        'tjpr_w': e.get('tjpr_w'), 'tjpr_raw': e.get('tjpr_raw'),
        'tjpr_lv': e.get('tjpr_lv'), 'tjpr_counted': e.get('tjpr_counted'),
        'bt_d': e.get('bt_d'), 'bt_internal_d': e.get('bt_internal_d'), 'bt_used': e.get('bt_used'),
        # フィルタ用に保持
        'is_weekend': e.get('is_weekend'),
        'is_pre': e.get('is_pre'),
        'is_gf_missing': e.get('is_gf_missing'),
        'is_awaiting_resume': e.get('is_awaiting_resume'),
        'is_restricted': e.get('is_restricted'),
        'is_lower_class': e.get('is_lower_class'),
        'is_dq': e.get('is_dq'),
        'tags': _common.flag_tags({k: e.get(k) for k in _SLIM_FLAG_KEYS}),   # この record にある is_* だけから
        # virtual sub-tournament (class bracket) 用: 親 event id と class letter
        'parent_event_id': e.get('parent_event_id'),
        'class_letter': e.get('class_letter'),
    }


def _resolve_tour_seed_wl_dq(
    uid: str, tid: str, tsum: dict, place,
    *,
    t_seed_info: dict, eid_to_parent_tid: dict,
    per_tournament_wins_main: dict, per_tournament_losses_main: dict,
    per_tournament_wins_class: dict, per_tournament_losses_class: dict,
    per_tournament_dq_uids_main: dict, per_tournament_dq_uids_class: dict,
) -> tuple:
    """大会出場記録の (seed_info, wins, losses, is_dq) を解決.

    virtual class entry の場合は parent event の class match を参照する.
    DQ flag は 明示的 DQ + 「matches データはあるが当該プレイヤーは試合無し」の保守的推定.

    Returns: (si, wins_t, losses_t, is_dq)
    """
    is_cv = bool(tsum.get('is_class_virtual'))
    parent_tid_for_cv = eid_to_parent_tid.get(tsum.get('parent_event_id')) if is_cv else None
    if is_cv and parent_tid_for_cv is not None:
        # seed は main bracket の seed を使う (ユーザー指定).
        si = tour_value(t_seed_info, parent_tid_for_cv, uid)
        wins_t = tour_value(per_tournament_wins_class, parent_tid_for_cv, uid, 0)
        losses_t = tour_value(per_tournament_losses_class, parent_tid_for_cv, uid, 0)
        total_t_matches = (_sum_counts(per_tournament_wins_class.get(parent_tid_for_cv, {}))
                           + _sum_counts(per_tournament_losses_class.get(parent_tid_for_cv, {})))
        dq_uids = per_tournament_dq_uids_class.get(parent_tid_for_cv, set())
    else:
        si = tour_value(t_seed_info, tid, uid)
        wins_t = tour_value(per_tournament_wins_main, tid, uid, 0)
        losses_t = tour_value(per_tournament_losses_main, tid, uid, 0)
        total_t_matches = (_sum_counts(per_tournament_wins_main.get(tid, {}))
                           + _sum_counts(per_tournament_losses_main.get(tid, {})))
        dq_uids = per_tournament_dq_uids_main.get(tid, set())
    is_dq_explicit = uid in dq_uids
    # data 欠損 (matches.json に試合が一部しか入ってない) に対して保守的に判定:
    # 「losses < 2」だけだと欠損で false-positive になりがちなので、
    # 「matches データはあるが当該プレイヤーは 1 試合もしていない」場合のみ DQ と推定する.
    # (ふわ@篝火#15 や メロンおじさん@池スマ 等、データ抜けで誤判定したケース対応)
    is_dq_inferred = (
        not is_dq_explicit
        and total_t_matches > 0
        and place is not None
        and place > 2
        and wins_t == 0
        and losses_t == 0
    )
    is_dq = is_dq_explicit or is_dq_inferred
    return (si, wins_t, losses_t, is_dq)


def _tour_entry_common_fields(tsum, place, si):
    """tournament_entries の共通フィールド (event/date/place/seed/is_* 系)."""
    return {
        'event_id': tsum.get('event_id'),
        'parent_event_id': tsum.get('parent_event_id'),
        'class_letter': tsum.get('class_letter'),
        'ts': tsum.get('ts'),
        'date': tsum['date'],
        'name': tsum['name'],
        'event': tsum['event'],
        'nent': tsum['nent'],
        'place': place,
        'seed': si['seed'] if si else None,
        'spr': si['spr'] if si else None,
        'is_weekend': tsum['is_weekend'],
        'is_weekend_real': tsum.get('is_weekend_real', tsum['is_weekend']),
        'is_smapa': tsum['is_smapa'],
        'is_pre': tsum.get('is_pre', False),
        'is_gf_missing': tsum.get('is_gf_missing', False),        # 未完了 (優勝者未確定で集計対象外)
        'is_awaiting_resume': tsum.get('is_awaiting_resume', False),  # 再開待ち (途中結果で暫定集計)
        'is_restricted': tsum.get('is_restricted', False),
        'is_lower_class': tsum.get('is_lower_class', False),
        'is_random_seed': tsum.get('is_random_seed', False),   # シードが強さ順でない (spr は null)
    }


def _sum_counts(d) -> int:
    """sum(d.values()) (spsp/columnar.IntCounts は数えておいた合計を返す)。"""
    return d.total() if hasattr(d, 'total') else sum(d.values())


def _lookup_jjpr_pts_for_tid(uid, tid, jjpr_contribs):
    """jjpr_contribs[uid] から tid に対応する points を返す."""
    for jc in jjpr_contribs.get(uid, []):
        if jc['t_id'] == tid:
            return jc['points']
    return 0.0


def _overlay_at_time_entry(rec, uid, at_time_t, deltas_t):
    """大会 standings / プレイヤー出場一覧 共通: rec に「当時の」表示値を overlay.

    実 cascade スナップショット (at_time_t) ベース:
      at_lv         : 当時の TJPR レベル
      pretour_ranks : 当時 (参加前) の全国 / TJPR / BT 順位
      rank_delta_*  : 当時の順位変動 (前→後 = スナップショット差分)
      tjpr_pts_at   : 当時その大会で獲得した raw TJPR pt. 当時レベルで計上対象外なら None.
    BT のレート変動は rec 既存の bt_d / bt_internal_d (時系列で当時値) をそのまま使う。
    """
    at_ = (at_time_t or {}).get(uid) or {}
    ens_b = at_.get('ens_before'); ens_a = at_.get('ens_after')
    tj_b  = at_.get('tjpr_before'); tj_a = at_.get('tjpr_after')
    bt_b  = at_.get('bt_before');   bt_a = at_.get('bt_after')
    rec['at_lv'] = at_.get('lv') or 0
    rec['pretour_ranks'] = {'ensemble': ens_b, 'tjpr': tj_b, 'bt_gated': bt_b}
    rec['rank_delta_ensemble'] = (ens_b - ens_a) if (ens_b is not None and ens_a is not None) else None
    rec['rank_delta_tjpr']     = (tj_b - tj_a)   if (tj_b  is not None and tj_a  is not None) else None
    rec['rank_delta_bt']       = (bt_b - bt_a)   if (bt_b  is not None and bt_a  is not None) else None
    rec['tjpr_pts_at'] = rolling_raw_at(deltas_t, uid) if at_.get('tjpr_counts') else None


def _build_tournament_entries(
    uid: str, tj_level: int, bt_hist_by_tid: dict,
    *,
    tjpr_contribs: dict, tjpr_lv1_contribs: dict, jjpr_contribs: dict,
    bt_gated_hist: dict, bt_used: dict,
    tournament_summary: dict, placements_per_t: dict, t_seed_info: dict,
    per_tour_by_lv: dict, rolling_deltas: dict, at_time_map: dict, eid_to_parent_tid: dict,
    per_tournament_wins_main: dict, per_tournament_losses_main: dict,
    per_tournament_wins_class: dict, per_tournament_losses_class: dict,
    per_tournament_dq_uids_main: dict, per_tournament_dq_uids_class: dict,
    no_jjpr_output: bool,
    perf_cache: dict | None = None,
) -> list:
    """uid に対する全 tournament_entries を組み立てる.

    3 source の union:
      1. tjpr_contribs (= 採用 Lv の寄与)
      2. bt_gated_hist (= TJPR で拾えない BT のみの大会)
      3. tjpr_lv1_contribs の class_virtual のみ (= 上位 Lv プレイヤーの下位クラス出場)

    日付降順 sort + (V4 build path 時) jjpr_pts 除去.
    """
    seed_wl_kwargs = dict(
        t_seed_info=t_seed_info, eid_to_parent_tid=eid_to_parent_tid,
        per_tournament_wins_main=per_tournament_wins_main,
        per_tournament_losses_main=per_tournament_losses_main,
        per_tournament_wins_class=per_tournament_wins_class,
        per_tournament_losses_class=per_tournament_losses_class,
        per_tournament_dq_uids_main=per_tournament_dq_uids_main,
        per_tournament_dq_uids_class=per_tournament_dq_uids_class,
    )
    tournament_entries: list = []
    seen_tids: set = set()

    # perf_rank (パフォーマンス相当全国順位) は大会単位で計算してキャッシュ。
    # 呼び出し側 (全選手ループ) から perf_cache を渡せば全選手で共有 (以前は選手ごとに作り直していて
    # 同じ大会の計算を 23 万回していた = build 63 秒のうち 58 秒)。
    _perf_cache: dict = perf_cache if perf_cache is not None else {}

    # 下位クラス (本戦から切り出した仮想大会 is_class_virtual) は自分の当時スナップショット (at_time) を持たないので、
    # 親の本戦のものを使う (当時の全国順位 = その日の大会の前後の順位なので本戦と同じ)。
    # これが無いとクラス戦の行のパフォーマンスと全国順位の変動が空になる (2026-09-30)
    def _at_time_for(tid_):
        at_ = (at_time_map or {}).get(tid_)
        if at_:
            return at_
        ts_ = tournament_summary.get(tid_) or {}
        if ts_.get('is_class_virtual'):
            ptid = eid_to_parent_tid.get(ts_.get('parent_event_id'))
            if ptid is not None:
                return (at_time_map or {}).get(ptid)
        return at_

    def _fill_class_ranks(rec_, tid_):
        """クラス戦の行で当時の全国順位が空なら、親の本戦のスナップショットから順位と変動だけ入れる"""
        if (rec_.get('pretour_ranks') or {}).get('ensemble') is not None:
            return
        if not (tournament_summary.get(tid_) or {}).get('is_class_virtual'):
            return
        tmp = {}
        _overlay_at_time_entry(tmp, uid, _at_time_for(tid_), None)
        for k_ in ('pretour_ranks', 'rank_delta_ensemble', 'rank_delta_tjpr', 'rank_delta_bt'):
            rec_[k_] = tmp[k_]

    def _perf_rank_of(tid_, place_, is_dq_):
        if place_ is None or is_dq_:
            return None
        pm = _perf_cache.get(tid_)
        if pm is None:
            pm = _perf_map_for_t(placements_per_t.get(tid_, {}), _at_time_for(tid_))
            # 全選手の書き出しの間 全大会分たまるので配列の表に (値は int。表せなければ dict のまま)
            pm = IntCounts.compact(pm) or pm
            _perf_cache[tid_] = pm
        return pm.get(uid)

    # 選手の履歴は 1 回だけ引く (以前は寄与 1 件ごとに全履歴を先頭から探していた。大会数 n の選手で n² の手間。
    # 同じ大会が 2 回あれば最初のもの = 以前と同じ)
    _hist = bt_gated_hist.get(uid, [])
    _hist_first: dict = {}
    for _e in _hist:
        _hist_first.setdefault(_e[0], _e)
    _used_u = bt_used.get(uid, {})

    # Block 1: tjpr_contribs (採用 Lv)
    for c in tjpr_contribs.get(uid, []):
        tid = c['t_id']
        seen_tids.add(tid)
        tsum = tournament_summary.get(tid)
        if tsum is None: continue
        place = tour_value(placements_per_t, tid, uid)
        _e = _hist_first.get(tid)
        if _e is not None:
            bt_internal_d, bt_peak_d, bt_used_flag = _e[2], (_e[3] if len(_e) >= 4 else 0.0), _used_u.get(tid, False)
        else:
            bt_internal_d, bt_peak_d, bt_used_flag = 0.0, 0.0, False
        cur_elo_after, peak_elo_after = _bt_after_elo(tid, bt_hist_by_tid)
        jjpr_pts = _lookup_jjpr_pts_for_tid(uid, tid, jjpr_contribs)
        si, wins_t, losses_t, is_dq = _resolve_tour_seed_wl_dq(uid, tid, tsum, place, **seed_wl_kwargs)
        rec = _tour_entry_common_fields(tsum, place, si)
        rec.update({
            'tjpr_lv': tj_level,
            'tjpr_raw': c['raw_points'],
            'tjpr_age': c['age_decay'],
            'tjpr_pos': c['pos_factor'],
            'tjpr_w': c['weighted'],
            'tjpr_counted': (c['rank_in_player'] < LV2_PARAMS[2]) if tj_level >= 2 else (c['rank_in_player'] < LV1_PARAMS[2]),
            'tjpr_by_lv': _lv_dict(per_tour_by_lv.get(uid, {}).get(tid, {})),
            'bt_used': bt_used_flag,
            'bt_d': bt_peak_d,                    # = 直対評価スコア (peak180) の変化量
            'bt_internal_d': bt_internal_d,       # = 内部レート (current ord) の変化量
            'bt_internal_after': cur_elo_after,   # 大会後の内部レート Elo (時系列チャート用)
            'bt_peak_after': peak_elo_after,      # 大会後の直対評価スコア Elo (時系列チャート用)
            'jjpr_pts': jjpr_pts,
            'wins': wins_t, 'losses': losses_t, 'is_dq': is_dq,
        })
        rec['tags'] = _common.flag_tags(rec)
        _overlay_at_time_entry(rec, uid, at_time_map.get(tid), rolling_deltas.get(tid))
        _fill_class_ranks(rec, tid)
        rec['perf_rank'] = _perf_rank_of(tid, place, is_dq)
        tournament_entries.append(rec)

    # Block 2: bt_gated_hist (TJPR で拾えない BT のみの大会)
    for entry in _hist:
        tid = entry[0]
        if tid in seen_tids: continue
        tsum = tournament_summary.get(tid)
        if tsum is None: continue
        place = tour_value(placements_per_t, tid, uid)
        cur_elo_after, peak_elo_after = _bt_after_elo(tid, bt_hist_by_tid)
        jjpr_pts = _lookup_jjpr_pts_for_tid(uid, tid, jjpr_contribs)
        si, wins_t, losses_t, is_dq = _resolve_tour_seed_wl_dq(uid, tid, tsum, place, **seed_wl_kwargs)
        rec = _tour_entry_common_fields(tsum, place, si)
        rec.update({
            'tjpr_lv': 0,
            'tjpr_raw': 0.0, 'tjpr_age': 1.0, 'tjpr_pos': 0.0, 'tjpr_w': 0.0,
            'tjpr_counted': False,
            'tjpr_by_lv': _lv_dict(per_tour_by_lv.get(uid, {}).get(tid, {})),
            'bt_used': bt_used.get(uid, {}).get(tid, False),
            'bt_d': entry[3] if len(entry) >= 4 else 0.0,
            'bt_internal_d': entry[2],
            'bt_internal_after': cur_elo_after,
            'bt_peak_after': peak_elo_after,
            'jjpr_pts': jjpr_pts,
            'wins': wins_t, 'losses': losses_t, 'is_dq': is_dq,
        })
        rec['tags'] = _common.flag_tags(rec)
        _overlay_at_time_entry(rec, uid, at_time_map.get(tid), rolling_deltas.get(tid))
        _fill_class_ranks(rec, tid)
        rec['perf_rank'] = _perf_rank_of(tid, place, is_dq)
        tournament_entries.append(rec)

    # Block 3: tjpr_lv1_contribs の class_virtual のみ.
    # 上位 Lv プレイヤーが下位クラス bracket (Bクラス等) に出場した場合、tjpr_contribs に
    # 含まれない (LV4/LV5 で exclude_capped=True のため). 実績/出場履歴の表示用に追加.
    # tjpr_w=0 で記録 (採用 Lv では寄与なし) しつつ raw_points は保持 → 優勝/準優勝判定可.
    for c in tjpr_lv1_contribs.get(uid, []):
        tid = c['t_id']
        if tid in seen_tids: continue
        tsum = tournament_summary.get(tid)
        if tsum is None or not tsum.get('is_class_virtual'): continue
        seen_tids.add(tid)
        place = tour_value(placements_per_t, tid, uid)
        si = tour_value(t_seed_info, tid, uid)
        rec = _tour_entry_common_fields(tsum, place, si)
        rec.update({
            'tjpr_lv': 1,
            'tjpr_raw': c['raw_points'],
            'tjpr_age': c['age_decay'],
            'tjpr_pos': c['pos_factor'],
            'tjpr_w': 0.0, 'tjpr_counted': False,
            'tjpr_by_lv': _lv_dict(per_tour_by_lv.get(uid, {}).get(tid, {})),
            # bt_d / bt_internal_d は overlay (postprocess) が class_bt_split から
            # 「クラス内対戦の内部 delta 収支」を入れる (本戦分は親 event 行へ分離).
            'bt_used': False, 'bt_d': 0.0, 'bt_internal_d': 0.0,
            'jjpr_pts': 0.0,
            'wins': 0, 'losses': 0, 'is_dq': False,
        })
        rec['tags'] = _common.flag_tags(rec)
        _overlay_at_time_entry(rec, uid, at_time_map.get(tid), rolling_deltas.get(tid))
        _fill_class_ranks(rec, tid)
        rec['perf_rank'] = _perf_rank_of(tid, place, False)
        tournament_entries.append(rec)

    # 同日内は ts (開始時刻) で順序付け: 下位クラス virtual は本戦 +1s/letter なので
    # 新しい順リストでは下位クラスが本戦の上 (= 時系列で後) に来る.
    tournament_entries.sort(key=lambda x: (x['date'], x.get('ts') or 0), reverse=True)
    # 反復13: V4 build path で jjpr_pts は不要 → 出力から除外
    if no_jjpr_output:
        for _te in tournament_entries:
            _te.pop('jjpr_pts', None)
    return tournament_entries


def _build_main_rec(
    uid: str, display: str,
    *,
    ens_rank: int, tj_rank: int, bt_rank: int,
    tj_score: float, tj_elo: float, tj_level: int,
    bt_ord: float, bt_elo: float,
    bt_internal_ord_p: float, bt_internal_elo_p: float,
    bt_all_ranks: dict, bt_all_ord: dict, norm_bt_disp: dict,
    ensemble_avg_rank: dict, ensemble_avg_score: dict,
    jj_rank: int, jj_score: float,
    tc: int, player_match_count: dict,
    tour_count_by_period: dict, match_count_by_period: dict,
    debut, top_tours: list,
    no_jjpr_output: bool,
) -> dict:
    """ranking table の main record dict を組み立て.

    JJPR が disabled な V4 path では jjpr_v3 / jjpr_score を出力から除外.
    """
    ranks_rec = {
        'ensemble': ens_rank,
        'tjpr': tj_rank,
        'bt_gated': bt_rank,
        'bt_flat': bt_all_ranks.get(uid, 0),
    }
    scores_rec = {
        'tjpr_score': tj_score,
        'tjpr_elo': tj_elo,
        'tjpr_level': tj_level,
        'bt_gated_ordinal': bt_ord,                 # peak180 ord (= 直対評価スコア)
        'bt_gated_elo': bt_elo,                     # peak180 Elo
        'bt_internal_ordinal': bt_internal_ord_p,    # current ord (= 内部レート)
        'bt_internal_elo': bt_internal_elo_p,        # current Elo
        'bt_all_ordinal': bt_all_ord.get(uid, 0.0),
        'bt_all_elo': norm_bt_disp.get(uid, 0.0),
        'ensemble_avg_rank': ensemble_avg_rank.get(uid),
        'ensemble_avg_score': ensemble_avg_score.get(uid),
    }
    # 反復13: JJPR 系フィールドは V4 では V4 フロントが参照しないため出力スキップ.
    if not no_jjpr_output:
        ranks_rec['jjpr_v3'] = jj_rank
        scores_rec['jjpr_score'] = jj_score
    return {
        'user_id': uid,
        'display': display,
        'ranks': ranks_rec,
        'scores': scores_rec,
        'metadata': {
            'tour_count_3y': tc,                    # backward compat (= 6 年データ窓内 BT-gated 集計)
            'bt_weekday_included': tc <= GATED_M_TOUR,
            'matches_count_3y': player_match_count.get(uid, 0),  # backward compat
            'tour_count_by_period': tour_count_by_period,
            'match_count_by_period': match_count_by_period,
            'debut': debut,
        },
        'top_tours': top_tours,
    }


def _accumulate_per_tournament_data(
    uid: str, display: str, tj_level: int,
    ens_rank: int, tj_rank: int, bt_rank: int,
    per_tournament_data: dict,
    *,
    tjpr_contribs: dict, bt_gated_hist: dict, bt_used: dict,
    tournament_summary: dict, placements_per_t: dict, t_seed_info: dict,
    per_tour_by_lv: dict,
) -> None:
    """per_tournament_data[tid][uid] へ entry を蓄積 (in-place)。

    表示名と tjpr_by_lv は持たない (大会ページを組み立てるときに display_cache / per_tour_by_lv から引き直す。
    どちらも書き出しの間に変わらないので同じ値。北米で 大会 × 参加者 約 300 万件)。display・per_tour_by_lv の引数は
    呼び出し側との互換のため残す。"""
    # per_tournament_data は spsp/columnar.TourEntryStore (列の配列) か {tid: {uid: tuple}} の dict。この選手の分は
    # mine (tid → 行番号) で覚えておく (同じ大会に 2 回入れたら上書き = dict と同じ。他の選手の行は見ない)
    store = per_tournament_data if hasattr(per_tournament_data, 'add') else None
    mine: dict = {}

    def put(tid, r):
        if store is None:
            per_tournament_data[tid][uid] = r
        elif tid in mine:
            store.replace(mine[tid], r)
        else:
            mine[tid] = store.add(tid, r)

    def has(tid):
        return (tid in mine) if store is not None else (tid in per_tournament_data and uid in per_tournament_data[tid])

    hist = bt_gated_hist.get(uid, [])   # 選手 1 人分を 1 回だけ引く (同じ大会が 2 回あれば最初のもの = 以前と同じ)
    hist_by_tid: dict = {}
    for entry in hist:
        hist_by_tid.setdefault(entry[0], entry)
    used_u = None   # bt_used.get(uid, {}) (選手 1 人分を 1 回だけ。履歴のある大会が無ければ引かない)
    for c in tjpr_contribs.get(uid, []):
        tid = c['t_id']
        tsum = tournament_summary.get(tid)
        if tsum is None: continue
        place = tour_value(placements_per_t, tid, uid)
        si = tour_value(t_seed_info, tid, uid)
        bt_internal_d = 0.0; bt_peak_d = 0.0; bt_used_flag = False
        entry = hist_by_tid.get(tid)
        if entry is not None:
            bt_internal_d = entry[2]
            if len(entry) >= 4: bt_peak_d = entry[3]
            if used_u is None:
                used_u = bt_used.get(uid, {})
            bt_used_flag = used_u.get(tid, False)
        put(tid, (
            uid, place,
            si['seed'] if si else None,
            si['spr'] if si else None,
            ens_rank, tj_rank, bt_rank,
            c['weighted'], c['raw_points'],
            tj_level, (c['rank_in_player'] < LV2_PARAMS[2]) if tj_level >= 2 else (c['rank_in_player'] < LV1_PARAMS[2]),
            bt_peak_d, bt_internal_d, bt_used_flag,
        ))
    for entry in hist:
        tid = entry[0]
        if has(tid): continue
        tsum = tournament_summary.get(tid)
        if tsum is None: continue
        place = tour_value(placements_per_t, tid, uid)
        si = tour_value(t_seed_info, tid, uid)
        put(tid, (
            uid, place,
            si['seed'] if si else None,
            si['spr'] if si else None,
            ens_rank, tj_rank, bt_rank,
            0.0, 0.0, 0, False,
            entry[3] if len(entry) >= 4 else 0.0,
            entry[2],
            bt_used.get(uid, {}).get(tid, False),
        ))


def _per_tournament_entry(r: tuple, display, by_lv) -> dict:
    """per_tournament_data の 1 件 (tuple) を大会ページの standings の 1 行 (dict) に戻す。

    以前は蓄積の時点でこの dict (と global_ranks / tjpr_by_lv の dict) を作って全大会ぶん持っていた (北米で数 GB)。
    tuple で持ち、大会ページを組み立てるときにここで作る (キーの並び・値は以前と同じ。tjpr_by_lv は per_tour_by_lv の
    その大会の dict の浅いコピー = 以前と同じ。per_tour_by_lv は書き出しの間に書き換えない)。
    display (表示名) と by_lv (per_tour_by_lv のその選手・大会の分) は呼び出し側が引いて渡す。"""
    (uid, place, seed, spr, ens_rank, tj_rank, bt_rank,
     tjpr_w, tjpr_raw, tjpr_lv, tjpr_counted, bt_d, bt_internal_d, bt_used) = r
    return {
        'user_id': uid, 'display': display, 'place': place,
        'seed': seed,
        'spr': spr,
        'global_ranks': {'ensemble': ens_rank, 'tjpr': tj_rank, 'bt_gated': bt_rank},
        'tjpr_w': tjpr_w, 'tjpr_raw': tjpr_raw,
        'tjpr_lv': tjpr_lv, 'tjpr_counted': tjpr_counted,
        'tjpr_by_lv': dict(by_lv),
        'bt_d': bt_d, 'bt_internal_d': bt_internal_d, 'bt_used': bt_used,
    }


def _compute_periods_and_debut(
    uid: str, eval_date: dt.datetime,
    *,
    player_tour_history: dict, player_matches: dict,
    player_first_tour: dict, tournament_summary: dict,
) -> tuple:
    """eval_date 起点で 大会出場 / 試合数を per-period fold + debut 解決.

    Returns: (tour_count_by_period, match_count_by_period, debut)
    """
    eval_ts_now = int(eval_date.timestamp())
    tour_hist_u = player_tour_history.get(uid, [])
    matches_u = player_matches.get(uid, [])
    PERIODS = [('all', None), ('3y', 1095), ('2y', 730), ('1y', 365), ('6m', 183), ('3m', 92)]
    tour_count_by_period = {}
    match_count_by_period = {}
    for pk, pd_max in PERIODS:
        if pd_max is None:
            tour_count_by_period[pk] = len(tour_hist_u)
            match_count_by_period[pk] = len(matches_u)
        else:
            cutoff = eval_ts_now - pd_max * 86400
            tour_count_by_period[pk] = sum(1 for ts, _ in tour_hist_u if ts >= cutoff)
            match_count_by_period[pk] = sum(1 for tup in matches_u if tup[0] >= cutoff)
    # デビュー戦: ガチ/非ガチ問わず最古の試合参加. データ窓 (6 年) 内でのみ有効.
    debut = None
    ft = player_first_tour.get(uid)
    if ft:
        ts0, tid0 = ft
        tsum0 = tournament_summary.get(tid0) or {}
        debut = {
            'date': tsum0.get('date'),
            'tournament_name': tsum0.get('name'),
            'event_id': tsum0.get('event_id'),
            'event_name': tsum0.get('event'),
        }
    return tour_count_by_period, match_count_by_period, debut


def _build_top_bt_gains(
    uid: str, two_years_ago: str,
    *,
    match_wins: dict, tournament_summary: dict, users: dict, format_display,
) -> list:
    """直近 2 年で BT delta が大きい試合 TOP 30 を構築."""
    match_list = match_wins.get(uid, [])
    scored_matches = []
    for tup in match_list:
        tid_m, opp_uid_m, dlt_m = tup[0], tup[1], tup[2]
        m_ref = tup[3] if len(tup) > 3 else None
        tsum_m = tournament_summary.get(tid_m)
        if not tsum_m: continue
        if (tsum_m.get('date') or '') < two_years_ago: continue
        scored_matches.append((dlt_m, tid_m, opp_uid_m, tsum_m, m_ref))
    scored_matches.sort(key=lambda x: -x[0])
    top_bt_gains = []
    for dlt_m, tid_m, opp_uid_m, tsum_m, m_ref in scored_matches[:30]:
        entry = {
            'opp_uid': opp_uid_m,
            'opp_display': format_display(users.get(opp_uid_m), opp_uid_m),
            'event_id': tsum_m.get('event_id'),
            'tournament_name': tsum_m.get('name', ''),
            'event_name': tsum_m.get('event', ''),
            'date': tsum_m.get('date'),
            'bt_d': dlt_m,
            'is_weekend': tsum_m.get('is_weekend'),
            'is_pre': tsum_m.get('is_pre', False),
            'is_restricted': tsum_m.get('is_restricted', False),
            'is_lower_class': tsum_m.get('is_lower_class', False),
        }
        if m_ref is not None:
            entry.update({
                'global_top_x': getattr(m_ref, 'global_top_x', None),
                'global_bracket_label': getattr(m_ref, 'global_bracket_label', None),
                'bracket_type': _common.match_bracket_type(m_ref),
                'round': getattr(m_ref, 'round', None),
                'round_text': getattr(m_ref, 'round_text', None),
                'p_score': getattr(m_ref, 'winner_score', None),  # uid won
                'o_score': getattr(m_ref, 'loser_score', None),
            })
        top_bt_gains.append(entry)
    return top_bt_gains


def _build_recent_matches(
    uid: str,
    *,
    player_matches: dict, tournament_summary: dict, t_seed_info: dict,
    users: dict, format_display,
) -> list:
    """全期間の対戦履歴 (= BT 直対) を新しい順に組み立て."""
    all_matches = player_matches.get(uid, [])
    all_matches_sorted = sorted(all_matches, key=lambda x: -x[0])
    recent_matches = []
    by_tid: dict = {}   # 大会 → (event_id, 大会名, イベント名, 日付, シード表)。同じ大会の試合で引き直さない
    for tup in all_matches_sorted:
        ts_m, tid_m, opp_uid_m, won, bt_internal_delta = tup[0], tup[1], tup[2], tup[3], tup[4]
        n_tup = len(tup)
        is_cls_m = tup[5] if n_tup > 5 else False
        m_ref = tup[6] if n_tup > 6 else None
        bt_tracked = tup[7] if n_tup > 7 else False
        tt = by_tid.get(tid_m)
        if tt is None:
            tsum_m = tournament_summary.get(tid_m, {})
            tt = by_tid[tid_m] = (tsum_m.get('event_id'), tsum_m.get('name', ''), tsum_m.get('event', ''),
                                  tsum_m.get('date'), t_seed_info.get(tid_m, {}))
        opp_seed_info = tt[4].get(opp_uid_m, {})
        # 直対評価スコア (peak180) は per-tournament-end のみ動くので、per-match 単位の
        # peak180 delta は意味がない. bt_d = bt_internal_d (= 試合単位の内部レート変化) で揃える.
        if m_ref is None:
            rec = {
                'opp_uid': opp_uid_m,
                'opp_display': format_display(users.get(opp_uid_m), opp_uid_m),
                'opp_seed': opp_seed_info.get('seed'),
                'event_id': tt[0],
                'tournament_name': tt[1],
                'event_name': tt[2],
                'date': tt[3],
                'won': won,
                'bt_d': bt_internal_delta,        # per-match は内部レート delta
                'bt_internal_d': bt_internal_delta,  # alias (= 内部レート delta)
                'bt_tracked': bt_tracked,         # True なら BT 学習対象 (= レート換算対象)
                'is_class': is_cls_m,
            }
        else:
            # 上と同じ項目の後に試合の項目 (以前は上の dict に update で足していた。キーの並びは同じ)
            ws = getattr(m_ref, 'winner_score', None)
            ls = getattr(m_ref, 'loser_score', None)
            rec = {
                'opp_uid': opp_uid_m,
                'opp_display': format_display(users.get(opp_uid_m), opp_uid_m),
                'opp_seed': opp_seed_info.get('seed'),
                'event_id': tt[0],
                'tournament_name': tt[1],
                'event_name': tt[2],
                'date': tt[3],
                'won': won,
                'bt_d': bt_internal_delta,
                'bt_internal_d': bt_internal_delta,
                'bt_tracked': bt_tracked,
                'is_class': is_cls_m,
                'round': getattr(m_ref, 'round', None),
                'round_text': getattr(m_ref, 'round_text', None),
                'phase_name': getattr(m_ref, 'phase_name', None),
                'winners_top': getattr(m_ref, 'winners_top', None),
                'losers_top': getattr(m_ref, 'losers_top', None),
                'bracket_label': getattr(m_ref, 'bracket_label', None),
                'global_round': getattr(m_ref, 'global_round', None),
                'global_top_x': getattr(m_ref, 'global_top_x', None),
                'global_bracket_label': getattr(m_ref, 'global_bracket_label', None),
                'bracket_type': _common.match_bracket_type(m_ref),
                'p_score': (ws if won else ls),
                'o_score': (ls if won else ws),
            }
        recent_matches.append(rec)
    return recent_matches


def _build_top_ufs_resolved(
    uid: str,
    *,
    player_top_ufs: dict, users: dict, format_display,
) -> list:
    """player_top_ufs を opp_display 付きで TOP 20 解決."""
    # 同 UF 値内は日付降順 (= 最新が先)
    top_ufs_list = sorted(player_top_ufs.get(uid, []), key=_top_uf_key)[:_TOP_UFS_N]
    top_ufs_resolved = []
    for u in top_ufs_list:
        opp_disp = format_display(users.get(u['opp_uid']), u['opp_uid'])
        top_ufs_resolved.append({**u, 'opp_display': opp_disp})
    return top_ufs_resolved


def _build_top_tours(
    uid: str,
    *,
    tjpr_contribs: dict, tournament_summary: dict, placements_per_t: dict,
    tournament_series, tournament_series_number,
) -> list:
    """top contributing tournaments (= ranking row preview の TOP 8)."""
    contribs_all = tjpr_contribs.get(uid, [])
    contribs_sorted = sorted(contribs_all, key=lambda c: -c.get('weighted', 0))
    top_tours = []
    for c in contribs_sorted[:8]:
        tsum = tournament_summary.get(c['t_id'])
        if not tsum: continue
        raw_name = tsum.get('name') or ''
        series = tournament_series(raw_name)
        num = tournament_series_number(raw_name)
        top_tours.append({
            'series': series,
            'series_id': _common.series_slug(series),
            'num': num,
            'place': placements_per_t.get(c['t_id'], {}).get(uid),
            'nent': tsum.get('nent'),
            'event_id': tsum.get('event_id'),
        })
    return top_tours


def _build_top_tjpr_contribs(tournament_entries):
    """tjpr_w > 0 の全大会を寄与降順で _slim_tour_entry 化."""
    counted_tjpr = [e for e in tournament_entries if (e.get('tjpr_w') or 0) > 0]
    return [_slim_tour_entry(e) for e in sorted(counted_tjpr, key=lambda x: -x['tjpr_w'])]


def _build_top_spr_and_recent(tournament_entries, tj_level, eval_date, passes_level):
    """SPR > 0 の直近 1 年大会から top_spr (SPR 降順 top 20) と recent_spr (直近 10) を返す."""
    one_year_ago = (eval_date - dt.timedelta(days=365)).date().isoformat()
    counted_spr = [
        e for e in tournament_entries
        if e.get('spr') is not None and e['spr'] > 0
        and e.get('date', '') >= one_year_ago
        and passes_level(tj_level, e.get('nent'), e.get('is_weekend'))
    ]
    top_spr = [_slim_tour_entry(e) for e in sorted(
        counted_spr,
        key=lambda x: (-x['spr'], -int((x.get('date') or '1970-01-01').replace('-', '')))
    )[:20]]
    recent_spr = [_slim_tour_entry(e) for e in tournament_entries[:10] if e.get('spr') is not None]
    return top_spr, recent_spr


def _build_peak_ranks_pair(ens_rank, tj_rank, bt_rank, history, peak_alltime_max_days):
    """ensemble / tjpr / bt_gated の peak ranks dict を all-time と 1y で 2 個返す.

    all-time: 初回 JapanMajor (2019-05-01) 以降に限定 (= 古い時代の競技人口希薄期を除外).
    """
    # _peak_rank を 3 系列 × 2 期間の 6 回呼ぶのと同じ結果を、history を 1 回なめて作る
    lims = (peak_alltime_max_days, 365)
    best = [[ens_rank, tj_rank, bt_rank], [ens_rank, tj_rank, bt_rank]]
    when = [['now'] * 3, ['now'] * 3]
    for h in history:
        d = h.get('d', 9999)
        vs = (h.get('ens'), h.get('tjpr_r'), h.get('bt_g_r'))
        for j in (0, 1):
            lim = lims[j]
            if lim is not None and d > lim:
                continue
            bj = best[j]
            for i in (0, 1, 2):
                v = vs[i]
                if v is None: continue
                b = bj[i]
                if b is None or v < b:
                    bj[i] = v; when[j][i] = f"{h['d']}日前"
    ks = ('ensemble', 'tjpr', 'bt_gated')
    peak_ranks = {k: {'rank': best[0][i], 'when': when[0][i]} for i, k in enumerate(ks)}
    peak_ranks_1y = {k: {'rank': best[1][i], 'when': when[1][i]} for i, k in enumerate(ks)}
    return peak_ranks, peak_ranks_1y


def _resolve_max_uf_record(uid, player_max_uf, users, format_display):
    """player_max_uf[uid] に opp_display を付与した dict (or None)."""
    muf = player_max_uf.get(uid)
    if muf is None:
        return None
    opp_disp = format_display(users.get(muf['opp_uid']), muf['opp_uid'])
    return {**muf, 'opp_display': opp_disp}


def _peak_rank(key_hist: str, current, history: list, max_days: int | None = None) -> dict:
    """全 / 1y peak rank を history から計算. None は skip."""
    best_rank = current; best_label = 'now'
    for h in history:
        if max_days is not None and h.get('d', 9999) > max_days:
            continue
        v = h.get(key_hist)
        if v is None: continue
        if best_rank is None or v < best_rank:
            best_rank = v; best_label = f"{h['d']}日前"
    return {'rank': best_rank, 'when': best_label}


# ─── 選手の書き出しの並列 (SPSP_PLAYER_WORKERS、既定 1 = 並列にしない) ───
# fork した子プロセスが選手を塊ごとに受け持ち、ファイル (players/ history/) を書いて、まとめる値を返す。親は元の順
# (sorted_by_ens) に足すので、jsonl・大会×参加者の表・players_current.json などの出力は 1 プロセスのときと同じ。
# 子は親のメモリを fork で写して読むだけ (gc.freeze で GC が古いオブジェクトに触って写しが増えるのを抑える)。
_PLAYER_JOB = None
_PLAYER_CHUNK = 200


def _player_workers(n_players: int) -> int:
    n = int(os.environ.get('SPSP_PLAYER_WORKERS') or 1)
    return n if n > 1 and n_players >= 2 * _PLAYER_CHUNK else 1


class _RowRecorder:
    """子プロセスで per_tournament_data の代わりに行を覚える (TourEntryStore の add / replace と同じ呼び方)。"""

    def __init__(self):
        self.rows = []

    def add(self, tid, r):
        self.rows.append([tid, r])
        return len(self.rows) - 1

    def replace(self, i, r):
        self.rows[i][1] = r


def _player_chunk(chunk):
    out = []
    for uid, ens_rank in chunk:
        rec = _RowRecorder()
        res = _PLAYER_JOB(uid, ens_rank, rec)
        out.append((res, rec.rows))
    return out


def _run_players_forked(job, items, n_workers):
    import gc
    import multiprocessing as mp
    global _PLAYER_JOB
    chunks = [items[k:k + _PLAYER_CHUNK] for k in range(0, len(items), _PLAYER_CHUNK)]
    print(f"  players: {n_workers} プロセスで書く ({len(chunks)} 塊)", flush=True)
    _PLAYER_JOB = job
    gc.collect()
    gc.freeze()
    try:
        with mp.get_context('fork').Pool(n_workers) as pool:
            for out in pool.imap(_player_chunk, chunks):
                yield from out
    finally:
        gc.unfreeze()
        _PLAYER_JOB = None


def build_and_write_all_player_records(
    out_path: Path,
    players_dir: Path,
    *,
    # ── per-uid score / rank maps ──
    ensemble_ranks: dict,
    ensemble_avg_rank: dict,
    ensemble_avg_score: dict,
    tjpr_ranks: dict,
    tjpr_levels: dict,
    tjpr_scores: dict,
    tjpr_elo: dict,
    tjpr_contribs: dict,
    tjpr_lv1_contribs: dict,
    tjpr_level_scores: dict,
    bt_gated_ranks: dict,
    bt_gated_ord: dict,
    bt_internal_ord: dict,
    bt_all_ord: dict,
    bt_all_ranks: dict,
    norm_bt_gated_disp: dict,
    norm_bt_internal_disp: dict,
    norm_bt_disp: dict,
    jjpr_ranks: dict,
    jjpr_scores: dict,
    jjpr_contribs: dict,
    # ── per-uid behavior / history maps ──
    tour_count: dict,
    bt_used: dict,
    bt_gated_hist: dict,
    bt_timeline: dict,
    g2_timeline: dict,
    match_wins: dict,
    player_matches: dict,
    player_match_count: dict,
    player_tour_history: dict,
    player_first_tour: dict,
    player_max_uf: dict,
    player_top_ufs: dict,
    player_max_uf_ach: dict,
    player_max_spr_ach: dict,
    player_min_spr_ach: dict,
    player_max_losers_run: dict,
    player_max_uf_1y: dict,
    player_max_spr_1y: dict,
    player_min_spr_1y: dict,
    player_max_losers_run_1y: dict,
    player_tour_count_2024: dict,
    player_match_count_2024: dict,
    player_win_count_2024: dict,
    player_vs_opp_count: dict,
    # ── per-tournament maps ──
    tournament_summary: dict,
    placements_per_t: dict,
    t_seed_info: dict,
    per_tour_by_lv: dict,
    rolling_deltas: dict,
    at_time_map: dict,
    per_tournament_wins_main: dict,
    per_tournament_losses_main: dict,
    per_tournament_wins_class: dict,
    per_tournament_losses_class: dict,
    per_tournament_dq_uids_main: dict,
    per_tournament_dq_uids_class: dict,
    eid_to_parent_tid: dict,
    # ── radar 集計 (from build_radar) ──
    spr_upper_val: dict,
    spr_neg_val: dict,
    uf_upper_val: dict,
    uf_lost_val: dict,
    pct: dict,
    # ── 雑多 ──
    users: dict,
    snapshots: dict,
    # ── helpers (functions) ──
    passes_level,
    format_display,
    tournament_series,
    tournament_series_number,
    build_history,
    build_achievements,
    build_dynamic_badges,
    # ── config / constants ──
    eval_date: dt.datetime,
    peak_alltime_max_days: int,
    no_jjpr_output: bool = False,
    overlay=None,   # v4.overlay.V4Overlay: 書く直前に V4 の順位評価/直対評価を反映する変換 (None = 素の出力)
    peak_track: dict | None = None,   # spsp/peak_track.snapshot() (uid → 追跡した最高順位)。None なら history から
) -> tuple[int, dict, dict]:
    """全 player の (slim + detail) record を組み立てて JSONL/JSON へ書き出す.

    main() 内 ~600 行のループ (line 722-1325) を関数化.

    Returns:
        (written_count, per_tournament_data, display_cache)
        per_tournament_data: t_id -> { uid -> entry-with-display+global_ranks }
        display_cache:       uid -> display name (= 後続の per-tournament loop で reuse)
    """
    written = 0
    sorted_by_ens = sorted(ensemble_ranks.items(), key=lambda x: x[1])
    perf_cache: dict = {}   # 大会ごとの perf_rank マップ (全選手で共有)
    char_rows: list = []    # character_index 用 (uid ファイル名, 必要項目) — 書いた JSON を読み直さないため
    last_activity: dict = {}   # uid_str -> 最終参加日 (data/last_activity.json 用)
    # Accumulator: t_id -> { uid -> entry-with-display+global_ranks }
    per_tournament_data = TourEntryStore()   # 列の配列 (spsp/columnar。get(tid) は {uid: tuple} の dict)
    # Display name cache for unrated attendees
    display_cache: dict = {}

    history_dir = players_dir.parent / 'history'
    if history_dir.exists():
        shutil.rmtree(history_dir)
    history_dir.mkdir(parents=True, exist_ok=True)
    current_players: dict = {}
    # 選手 1 人分 (この選手のファイルを書き、まとめる値を返す)。per_tournament_data は全体の表か、並列のときは
    # 子プロセスの _RowRecorder (行を覚えて親が同じ順に足す)。
    def _one(uid, ens_rank, per_tournament_data):
        tj_level = tjpr_levels.get(uid, 0)
        tj_score = tjpr_scores.get(uid, 0.0)
        tj_elo = tjpr_elo.get(uid, 0.0)
        tj_rank = tjpr_ranks.get(uid, 0)
        bt_rank = bt_gated_ranks.get(uid, 0)
        bt_ord = bt_gated_ord.get(uid, 0.0)       # = peak180 ord (= 直対評価スコア)
        bt_elo = norm_bt_gated_disp.get(uid, 0.0)  # = peak180 Elo
        bt_internal_ord_p = bt_internal_ord.get(uid, 0.0)   # = 内部レート ord
        bt_internal_elo_p = norm_bt_internal_disp.get(uid, 0.0)  # = 内部レート Elo
        jj_rank = jjpr_ranks.get(uid, 0)
        jj_score = jjpr_scores.get(uid, 0.0)
        tc = tour_count.get(uid, 0)
        display = format_display(users.get(uid), uid)

        # 大会ごとの (ord_after_current, ord_after_peak180) を tid 引きで参照できる lookup
        # チャートの密な BT trajectory 表示に使う.
        bt_hist_by_tid = {entry[0]: entry for entry in bt_gated_hist.get(uid, [])}

        tournament_entries = _build_tournament_entries(
            uid, tj_level, bt_hist_by_tid,
            perf_cache=perf_cache,
            tjpr_contribs=tjpr_contribs, tjpr_lv1_contribs=tjpr_lv1_contribs,
            jjpr_contribs=jjpr_contribs,
            bt_gated_hist=bt_gated_hist, bt_used=bt_used,
            tournament_summary=tournament_summary,
            placements_per_t=placements_per_t,
            t_seed_info=t_seed_info,
            per_tour_by_lv=per_tour_by_lv,
            rolling_deltas=rolling_deltas,
            at_time_map=at_time_map,
            eid_to_parent_tid=eid_to_parent_tid,
            per_tournament_wins_main=per_tournament_wins_main,
            per_tournament_losses_main=per_tournament_losses_main,
            per_tournament_wins_class=per_tournament_wins_class,
            per_tournament_losses_class=per_tournament_losses_class,
            per_tournament_dq_uids_main=per_tournament_dq_uids_main,
            per_tournament_dq_uids_class=per_tournament_dq_uids_class,
            no_jjpr_output=no_jjpr_output,
        )

        # Per-tournament accumulator (= per_tournament_data[tid][uid])
        _accumulate_per_tournament_data(
            uid, display, tj_level, ens_rank, tj_rank, bt_rank, per_tournament_data,
            tjpr_contribs=tjpr_contribs, bt_gated_hist=bt_gated_hist, bt_used=bt_used,
            tournament_summary=tournament_summary,
            placements_per_t=placements_per_t,
            t_seed_info=t_seed_info,
            per_tour_by_lv=per_tour_by_lv,
        )

        # History (sparse): list of {d, ranks + elo at that snapshot}
        history = build_history(uid, snapshots)
        # 反復13: V4 build path で jjpr_r / jjpr_s は不要 → 出力から除外
        if no_jjpr_output:
            for _h in history:
                _h.pop('jjpr_r', None)
                _h.pop('jjpr_s', None)
        # 実績 / バッジの判定はグリッドの点だけで行う (旧挙動)。大会の点 (順位が変化した日付) は
        # 表示用に後で足す (spsp.score.history_event_rows)
        history_grid = history

        # Top contributing tournaments (multi, for main ranking row preview).
        top_tours = _build_top_tours(
            uid,
            tjpr_contribs=tjpr_contribs, tournament_summary=tournament_summary,
            placements_per_t=placements_per_t,
            tournament_series=tournament_series,
            tournament_series_number=tournament_series_number,
        )

        # Per-period 大会出場 / 試合数 + debut.
        tour_count_by_period, match_count_by_period, debut = _compute_periods_and_debut(
            uid, eval_date,
            player_tour_history=player_tour_history,
            player_matches=player_matches,
            player_first_tour=player_first_tour,
            tournament_summary=tournament_summary,
        )

        # Lightweight main record (for the main ranking table)
        main_rec = _build_main_rec(
            uid, display,
            ens_rank=ens_rank, tj_rank=tj_rank, bt_rank=bt_rank,
            tj_score=tj_score, tj_elo=tj_elo, tj_level=tj_level,
            bt_ord=bt_ord, bt_elo=bt_elo,
            bt_internal_ord_p=bt_internal_ord_p, bt_internal_elo_p=bt_internal_elo_p,
            bt_all_ranks=bt_all_ranks, bt_all_ord=bt_all_ord, norm_bt_disp=norm_bt_disp,
            ensemble_avg_rank=ensemble_avg_rank, ensemble_avg_score=ensemble_avg_score,
            jj_rank=jj_rank, jj_score=jj_score,
            tc=tc, player_match_count=player_match_count,
            tour_count_by_period=tour_count_by_period,
            match_count_by_period=match_count_by_period,
            debut=debut, top_tours=top_tours,
            no_jjpr_output=no_jjpr_output,
        )
        # 直前のレート変動 = 最新の「集計対象」大会の内部レート delta。
        # 平日大会はベテランの直対を更新しない (delta=0, bt_used=False) ため、
        # 単純な最新大会ではなく直近の集計対象大会 (bt_used=True) まで遡って符号を取る。
        # bt_gated_hist は時系列 sort 済み。ランキング表の「レート更新中」緑判定に使う。
        _bt_hist = bt_gated_hist.get(uid, [])
        _used_map = bt_used.get(uid, {})
        _d_last = 0.0
        for _ent in reversed(_bt_hist):
            if _used_map.get(_ent[0]):  # _ent[0] = tid (= id(t))
                _d_last = float(_ent[2])
                break
        main_rec['scores']['bt_internal_d_last'] = _d_last
        # ── JSONL (= index page で最初に読まれる全 player ファイル) はサイズ削減のため slim 版 ──
        # per-player page (= players/<uid>.json) には main_rec が後で書き込まれる (= full).
        slim_rec = build_slim_record(main_rec)
        slim_bytes = final_rk = None
        if overlay is None:
            slim_bytes = dump_json_bytes(slim_rec) + b'\n'   # = write_player_slim (この時点の形で書く)

        # Derived highlights for player detail page.
        # tjpr_w > 0 → ランキング寄与あり / bt_used → 直対評価寄与あり.
        # 2 年以内 (top_bt_gains 用): site 側 pagination で広めに見せるため 1 年→2 年.
        two_years_ago = (eval_date - dt.timedelta(days=730)).date().isoformat()
        top_tjpr_contribs = _build_top_tjpr_contribs(tournament_entries)
        top_bt_gains = _build_top_bt_gains(
            uid, two_years_ago,
            match_wins=match_wins, tournament_summary=tournament_summary,
            users=users, format_display=format_display,
        )
        top_spr, recent_spr = _build_top_spr_and_recent(
            tournament_entries, tj_level, eval_date, passes_level)
        recent_matches = _build_recent_matches(
            uid,
            player_matches=player_matches, tournament_summary=tournament_summary,
            t_seed_info=t_seed_info, users=users, format_display=format_display,
        )
        peak_ranks, peak_ranks_1y = _build_peak_ranks_pair(
            ens_rank, tj_rank, bt_rank, history, peak_alltime_max_days)
        ach = build_achievements(
            uid, ens_rank, tj_level, history, tournament_entries,
            player_max_uf_ach, player_max_spr_ach, player_max_losers_run,
            player_tour_count_2024, player_match_count_2024,
            player_win_count_2024, player_vs_opp_count,
            peak_alltime_max_days=peak_alltime_max_days,
            peak=(peak_track or {}).get(uid) if peak_track is not None else None,
        )
        # ハイライト (max_uf / max_spr / min_spr / max_losers_run):
        # 1 年以内に大会参加がある選手は「直近 1 年内の最大」を出す (2026-08-11 ユーザー決定)。
        # 1 年以内に参加がない選手は従来どおり全期間の最大。
        one_year_ago_iso = (eval_date - dt.timedelta(days=365)).date().isoformat()
        recent_active = any((e.get('date') or '') >= one_year_ago_iso
                            for e in tournament_entries)
        if recent_active:
            max_uf_rec = _resolve_max_uf_record(uid, player_max_uf_1y, users, format_display)
            max_spr_rec = player_max_spr_1y.get(uid)
            min_spr_rec = player_min_spr_1y.get(uid)
            max_losers_run_rec = player_max_losers_run_1y.get(uid)
        else:
            max_uf_rec = _resolve_max_uf_record(uid, player_max_uf, users, format_display)
            max_spr_rec = player_max_spr_ach.get(uid)
            min_spr_rec = player_min_spr_ach.get(uid)
            max_losers_run_rec = player_max_losers_run.get(uid)
        # Top-N UFs (= biggest upsets list; こちらは全期間のまま)
        top_ufs_resolved = _build_top_ufs_resolved(
            uid, player_top_ufs=player_top_ufs, users=users, format_display=format_display,
        )
        # ── 動的バッチ — v4/score.build_dynamic_badges ──
        dynamic_badges = build_dynamic_badges(ens_rank, history)
        # Per-player detail JSON (main fields embedded so player page doesn't need to fetch the 7MB JSONL).
        detail_rec = build_detail_record(
            uid, display, main_rec,
            tjpr_level_scores=tjpr_level_scores,
            tournament_entries=tournament_entries,
            history=_merge_history(history_grid, _history_event_rows(tournament_entries, eval_date)),
            bt_timeline=bt_timeline,
            g2_timeline=g2_timeline,
            top_tjpr_contribs=top_tjpr_contribs,
            top_bt_gains=top_bt_gains,
            top_spr=top_spr,
            recent_spr=recent_spr,
            recent_matches=recent_matches,
            pct=pct,
            spr_upper_val=spr_upper_val,
            spr_neg_val=spr_neg_val,
            uf_upper_val=uf_upper_val,
            uf_lost_val=uf_lost_val,
            peak_ranks=peak_ranks,
            peak_ranks_1y=peak_ranks_1y,
            max_uf_rec=max_uf_rec,
            top_ufs_resolved=top_ufs_resolved,
            max_spr_rec=max_spr_rec,
            min_spr_rec=min_spr_rec,
            max_losers_run_rec=max_losers_run_rec,
            users=users,
            achievements=ach,
            dynamic_badges=dynamic_badges,
        )
        if overlay is not None:
            # 以前は書いた後に overlay が読み直して書き換えていた (生成 → 上書き)。今はディスク経由と同じ形
            # (json_roundtrip) にしてから変換し、1 回だけ書く。jsonl の top_tours は選手 JSON 側の最終 tjpr_w 順。
            detail_rec, top_tours_final = overlay.player(json_roundtrip(detail_rec))
            slim_rec = overlay.slim(json_roundtrip(slim_rec))
            if top_tours_final is not None:
                slim_rec['top_tours'] = top_tours_final
            final_rk = slim_rec.get('ranks')   # overlay.note_final_ranks (大会 JSON の global_ranks 用) は呼び出し側で
            slim_bytes = dump_json_bytes(slim_rec) + b'\n'
        # 安定部分 / 揮発部分 / 履歴に分けて書く (docs/refactor/04_player_json_split.md)。
        # detail_rec (完全形) はこの後の char_rows / last_activity にそのまま使う。
        _stable, _current, _hist_rows = split_detail(detail_rec, eval_date)
        write_player_detail(players_dir, uid, _stable)
        write_player_history(history_dir, uid, _hist_rows)
        # 揮発部分は列の順の list にして持つ (書き出すときと同じ形。全選手分を入れ子の dict で持つと北米で 約 0.5 GB)
        cur_row = encode_current_row(_current)
        char_row = (f'{uid}.json', {'characters': detail_rec.get('characters'), 'user_id': detail_rec.get('user_id'),
                                    'ranks': {'ensemble': (detail_rec.get('ranks') or {}).get('ensemble')}})
        # 最終参加日 (= data/last_activity.json。以前は build/build_last_activity_json.py が players/*.json を読み直していた)
        _dates = [t['date'] for t in (detail_rec.get('tournaments') or []) if t.get('date')]
        return (uid, display, slim_bytes, final_rk, cur_row, char_row, max(_dates) if _dates else None)

    def _merge(res, rows=None):
        nonlocal written
        uid, display, slim_bytes, final_rk, cur_row, char_row, last_date = res
        if rows is not None:
            for tid, r in rows:
                per_tournament_data.add(tid, r)
        display_cache[uid] = display
        if final_rk is not None:
            overlay.note_final_ranks({'user_id': uid, 'ranks': final_rk})
        fp.write(slim_bytes)
        current_players[uid] = cur_row
        char_rows.append(char_row)
        if last_date is not None:
            last_activity[str(uid)] = last_date
        written += 1

    n_workers = _player_workers(len(sorted_by_ens))
    with out_path.open('wb') as fp:   # 選手 1 行ずつの bytes を書く (= write_player_slim)
        if n_workers <= 1:
            for uid, ens_rank in sorted_by_ens:
                _merge(_one(uid, ens_rank, per_tournament_data))
        else:
            fp.flush()   # 子プロセスに書きかけの buffer を写さない
            for res, rows in _run_players_forked(_one, sorted_by_ens, n_workers):
                _merge(res, rows)

    write_players_current(out_path.parent / 'players_current.json', eval_date, current_players)
    print(f"  Wrote players_current.json ({len(current_players)} players) and history/ ({len(current_players)} files)", flush=True)

    if overlay is not None:
        overlay.char_rows = char_rows   # build_v4_full._overlay_build_character_index が使う
        overlay.last_activity = last_activity
    print(f"  per_tournament_data: {len(per_tournament_data)} tournaments (列に入らなかった行 {per_tournament_data.n_odd})", flush=True)
    return written, per_tournament_data, display_cache

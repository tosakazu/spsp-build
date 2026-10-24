"""v4/learner.py — 本番のランキング学習器 (Glicko2 + per-tour レベル判定のカスケード)。

2026-09-07 に v3/eval_bt_lv_shared.py から移動した (名前が「実験用」に見えて本番コードだと分かりにくかった)。
v3/eval_bt_lv_shared.py は実験ハーネス向けの互換 shim (このモジュールの属性をそのまま見せる) として残している。

入口:
  - apply_settings(Settings)     設定からモジュール定数 (LV_DEFS 等) を組み立てる。本番は build_v4_full.py が Settings() で呼ぶ。
                                 import 時にも Settings() (本番既定値) を 1 回適用する。実験ハーネスは shim 経由で
                                 Settings.from_env() を適用する。
  - g2_learn_advance(state, ...) 学習状態を [cursor_ts, until_ts) の大会ぶん進める (チェックポイント保存・再開は v4/learn_state.py)
  - g2_snapshots(state, ...)     SNAPSHOT_DAYS 時点のカスケードを状態から復元する (_SnapCache で同日再実行を高速化)
  - learn_bt_lv_shared_g2_pure   上 2 つをまとめて一括で回す薄いラッパ (実験ハーネス用)
  - main()                       実験ハーネス (targets ファイルで精度評価)。本番では使わない。
"""
from __future__ import annotations
import os
import datetime as dt
import json
import math
import sys
import time
import gc
from collections import defaultdict
from functools import partial
from pathlib import Path

import numpy as np

from spsp.paths import REPO, TARGETS_DIR, REGION   # noqa: E402

from spsp.data_loader import load_all_tournaments
from spsp import common as _common  # BT_PEAK_WINDOW_SEC は configure() で変わりうるので属性参照
from spsp.common import (
    td_exp,
    TJPR_DEFAULT_RATE,
    GATED_M_TOUR,
    SIGMA_FLOOR,
    LV1_PARAMS,
    LV2_PARAMS,
    LV3_FILTER,
    LV4_FILTER,
    LV5_FILTER,
    _bracket_order_key,
    ELO_PER_UNIT,
    OVERSEAS_UIDS,
    jp_counter_rank,
    jp_topn_filter,
    measured_uids_at_ts,
    PROV_WINDOW_DAYS,
    PROV_2Y_MIN_TOURS,
)
from spsp.meta import tournament_event_id  # noqa: F401  (eval ハーネスが re-export 経由で使う)
from spsp.learn import (
    ContribTable,
    learn_bt_gated_tour,
    precompute_banzuke,
    Peak180Tracker,
    _apply_inactivity_sigma,
    _apply_sigma_floor_keep_ordinal,
)
from spsp.common import t_passes
from spsp.raters.openskill_rater import OpenSkillBTRater
from spsp.tjpr_cascade import (
    build_t_meta, filter_dq_no_shows, load_attendees,
    seeds_from_rank, metrics_full, build_tjpr_sized, EVENTS_ROOT, TOP_K,
    build_tjpr_general, rv_default, sm_one,
)

from spsp.settings import Settings
from spsp.columnar import AnchorTable
from spsp.learn_state import G2LearnState, check_ledger, tournament_digest, rel_event_path
from spsp.common import GLOBAL_SHIFT_FROZEN
from spsp.common import _rolling_lv2_raw_per_tour, level_tier_values
from spsp.common import PEAK_ALLTIME_CUTOFF_DATE as _PEAK_CUTOFF_DATE
_PEAK_CUTOFF_TS = int(dt.datetime(_PEAK_CUTOFF_DATE.year, _PEAK_CUTOFF_DATE.month, _PEAK_CUTOFF_DATE.day).timestamp())
from spsp.rolling import placements_of

# per-tour 増分スコアの減衰基準日。順位は基準日に依存しない (全大会共通の係数) ので固定値でよい。
# 2026-09-05 = 差分ビルド (チェックポイント再開) を入れたときの本番 eval_date。それ以前のビルドと
# bit 単位で一致させるためにこの日にした。以後の大会は「未来」扱い (減衰係数 > 1) で問題ない。
PT_DECAY_REF = dt.datetime(2026, 9, 5, 23, 59, 59)

# ── 設定 (v4/settings.py) からモジュール定数を組み立てる ──
# 以前はここで os.environ を読んでいた (本番は build_v4_full.py が import 前に環境変数を書き、
# 実験ハーネスは環境変数でアームを切り替えていた)。今は apply_settings() が唯一の入口:
#   - 本番: build_v4_full.py が apply_settings(Settings()) を明示的に呼ぶ (環境変数は見ない)
#   - ハーネス: import 時に Settings.from_env() を 1 回適用 (旧挙動と同じ)。import 後に環境変数を
#     変えた場合は apply_settings(Settings.from_env()) を呼び直す
_S: Settings = None  # type: ignore[assignment]  # 現在適用中の設定 (呼び出し時に参照するフラグ用)


_LOCKED = False   # True の間は apply_settings(lock=False) を無視する (本番ビルドが設定を固定するため)


def lock_settings() -> None:
    """本番ビルド用: 以後、明示的に lock=True を付けない apply_settings を無視する。

    旧コードで保存した学習状態 (クラスが eval_bt_lv_shared にある pickle) を読むと、unpickle 経由で
    v3/eval_bt_lv_shared.py (実験ハーネス向け shim) が import され、その import 時処理が
    Settings.from_env() (= 環境変数なしだと per_tour_lv / lv3_weekend などが全部 off) を適用して
    本番設定を潰していた (2026-09-07 の夜間ビルドで発生)。"""
    global _LOCKED
    _LOCKED = True


def apply_settings(s: Settings, announce: bool = False, *, lock: bool = False) -> None:
    global _S, _LOCKED
    if _LOCKED and not lock:
        print(f"  [settings] locked: apply_settings を無視 (要求: per_tour_lv={s.per_tour_lv}, lv3_weekend={s.lv3_weekend})", flush=True)
        return
    if lock:
        _LOCKED = True
    global _LV3_FILTER_EFFECTIVE, _LV4_FILTER_EFFECTIVE, _LV2_FILTER_EFFECTIVE
    global PFILTER_MODE, BT_GATING_MODE, LV_TFILTERS, _LV3_FILTER_CASCADE, LV_DEFS
    global PROV_1Y_MIN, PROV_2Y_MIN, _PT_LV_AGG, _PT_LV_CAP, WARMUP_M
    global _BT_CLASS_GATE, _BT_PAIR_GATE, _BT_SIZE_K_MODE, _BT_SIZE_K_REF, _BT_SIZE_K_POW
    global _BT_BO5_K, _BT_BO1_K, _BT_LV3WD_K, _BT_ONESIDE_K, _BT_LVGAP_K, _BT_LVGAP_MIN, _BT_LVGAP_SCOPE, _BT_K_ON
    global _BT_GC_LV, _BT_GC_MAX_GAMES, _BT_GC_MODE, _BT_GC_SCOPE
    global _PEAK_DECAY_K, _PEAK_DECAY_MODE, _PEAK_DECAY_LAMBDA, _PEAK_DECAY_BASE, _PEAK_PW_SPEC, _PEAK_PW
    global _PW_STARTS, _PW_SLOPES, _PW_G0, _PW_STARTS_A, _PW_SLOPES_A, _PW_ENDS_A, _PEAK_ALLTIME
    _S = s
    _common.configure(s.bt_peak_days, s.decay_r)  # peak 窓 / 月減衰率 (v4/common.py の定数)
    # LV3 tfilter に weekend_only を付ける (= LV3 が事実上 LV4 と同じ tfilter、pfilter サイズだけ違う)
    _LV3_FILTER_EFFECTIVE = dict(LV3_FILTER)
    if s.lv3_weekend:
        _LV3_FILTER_EFFECTIVE['weekend_only'] = True
    # LV3 / LV4 の nent threshold オーバーライド (= 段階的階層構造実験用)
    if s.lv3_nent_gt is not None:
        _LV3_FILTER_EFFECTIVE['min_nent_gt'] = s.lv3_nent_gt
    _LV4_FILTER_EFFECTIVE = dict(LV4_FILTER)
    if s.lv4_nent_gt is not None:
        _LV4_FILTER_EFFECTIVE['min_nent_gt'] = s.lv4_nent_gt
    # LV2 weekend_only / min_nent_gt 拡張
    _LV2_FILTER_EFFECTIVE = None
    if s.lv2_weekend or s.lv2_nent_gt is not None:
        _LV2_FILTER_EFFECTIVE = {}
        if s.lv2_weekend:
            _LV2_FILTER_EFFECTIVE['weekend_only'] = True
        if s.lv2_nent_gt is not None:
            _LV2_FILTER_EFFECTIVE['min_nent_gt'] = s.lv2_nent_gt
    # B: cascade promotion (= pfilter) を TJPR-only rank で実施 (= prod_E 同様)
    PFILTER_MODE = s.pfilter_mode
    # C: BT 更新 gating を M-tour (= 各 player の tour_count ≥ GATED_M_TOUR) で実施
    BT_GATING_MODE = s.bt_gating_mode
    if announce:
        print(f"PFILTER_MODE={PFILTER_MODE}, BT_GATING_MODE={BT_GATING_MODE}", flush=True)
    LV_TFILTERS = {1: None, 2: _LV2_FILTER_EFFECTIVE, 3: _LV3_FILTER_EFFECTIVE, 4: _LV4_FILTER_EFFECTIVE, 5: LV5_FILTER}
    # lv3_tjpr_weekday: 順位評価 (TJPR cascade = LV_DEFS) 側の LV3 tfilter だけ weekend_only を外す
    # (= Lv3 は平日大会も順位評価に加算)。BT gating (LV_TFILTERS / lv_tournament_passes 経由) は
    # 従来どおり週末のみ。lv3_tjpr_weekday_w で平日大会の順位評価ポイントに係数を掛ける
    # (1.0 = 平日を週末と同格で加算)。compute_tjpr_at_lv で適用。
    _LV3_FILTER_CASCADE = _LV3_FILTER_EFFECTIVE
    if s.lv3_tjpr_weekday:
        _LV3_FILTER_CASCADE = dict(_LV3_FILTER_EFFECTIVE)
        _LV3_FILTER_CASCADE.pop('weekend_only', None)
        if s.lv3_tjpr_weekday_w is not None:
            _LV3_FILTER_CASCADE['weekday_w'] = s.lv3_tjpr_weekday_w
    LV_DEFS = [
        (1, None,                  LV1_PARAMS, 2048),
        (2, _LV2_FILTER_EFFECTIVE, LV2_PARAMS, 1024),
        (3, _LV3_FILTER_CASCADE,   LV2_PARAMS, 512),
        (4, _LV4_FILTER_EFFECTIVE, LV2_PARAMS, 256),
        (5, LV5_FILTER,            LV2_PARAMS, None),
    ]
    # lv6_nent_gt 設定時、ensemble top N (= lv6_topn) を新カスケード帯 LV6 として分離し、
    # その帯の評価対象大会を nent > lv6_nent_gt の週末大会に制限する。順位評価カスケード
    # (LV_DEFS) と BT gating (LV_TFILTERS) の両方に適用。
    if s.lv6_nent_gt is not None:
        _LV6_FILTER = {'min_nent_gt': s.lv6_nent_gt, 'weekend_only': True, 'exclude_capped': True}
        LV_DEFS[4] = (5, LV5_FILTER, LV2_PARAMS, s.lv6_topn)
        LV_DEFS.append((6, _LV6_FILTER, LV2_PARAMS, None))
        LV_TFILTERS[6] = _LV6_FILTER
        if announce:
            print(f"SPSP_LV6: top{s.lv6_topn} band, nent_gt={s.lv6_nent_gt}", flush=True)
    # 計測済み条件: 1年で PROV_1Y_MIN 大会以上 OR 2年で PROV_2Y_MIN 大会以上
    PROV_1Y_MIN = s.prov_1y_min
    PROV_2Y_MIN = s.prov_2y_min
    # per-tour LV 用: (lv, topN, pos_decay) と {lv: その Lv に入るための topn}
    _PT_LV_AGG = [(lv, lp[2], lp[3]) for lv, _tf, lp, _tn in LV_DEFS]
    _PT_LV_CAP = {LV_DEFS[i][0]: LV_DEFS[i - 1][3] for i in range(1, len(LV_DEFS))}
    WARMUP_M = s.warmup_m  # 0 = pure (V4 設定)
    # ── BT 更新の実験フック (説明は各定数の使用箇所付近のコメント) ──
    _BT_CLASS_GATE = s.bt_class_gate
    _BT_PAIR_GATE = s.bt_pair_gate
    _BT_SIZE_K_MODE = s.bt_size_k_mode
    _BT_SIZE_K_REF = s.bt_size_k_ref
    _BT_SIZE_K_POW = s.bt_size_k_pow
    _BT_BO5_K = s.bt_bo5_k
    _BT_BO1_K = s.bt_bo1_k
    _BT_LV3WD_K = s.bt_lv3_weekday_k
    _BT_ONESIDE_K = s.bt_oneside_k
    _BT_LVGAP_K = s.bt_lvgap_k
    _BT_LVGAP_MIN = s.bt_lvgap_min
    _BT_LVGAP_SCOPE = s.bt_lvgap_scope
    _BT_K_ON = bool(_BT_SIZE_K_MODE or _BT_BO5_K or (_BT_BO1_K is not None) or _BT_ONESIDE_K or _BT_LVGAP_K or _BT_LV3WD_K)
    _BT_GC_LV = s.bt_gamecount_lv
    _BT_GC_MAX_GAMES = s.bt_gamecount_max
    _BT_GC_MODE = s.bt_gamecount_mode
    _BT_GC_SCOPE = s.bt_gamecount_scope
    # ── peak の減衰 ──
    _PEAK_DECAY_K = s.peak_decay_k
    _PEAK_DECAY_MODE = s.peak_decay_mode
    _PEAK_DECAY_LAMBDA = s.peak_decay_lambda
    _PEAK_DECAY_BASE = s.peak_decay_base
    _PEAK_PW_SPEC = s.peak_pw_spec
    _PEAK_PW = (_PEAK_DECAY_MODE == 'pw')
    if _PEAK_PW:
        if not _PEAK_PW_SPEC:
            raise ValueError('peak_decay_mode=pw には peak_pw_spec (SPSP_BT_PEAK_PW) が必要')
        _PW_STARTS, _PW_SLOPES, _PW_G0 = _parse_pw(_PEAK_PW_SPEC)
        _PW_STARTS_A = np.asarray(_PW_STARTS, dtype=np.float64)
        _PW_SLOPES_A = np.asarray(_PW_SLOPES, dtype=np.float64)
        # 区間の終わり (最後は inf)
        _PW_ENDS_A = np.asarray(list(_PW_STARTS[1:]) + [float('inf')], dtype=np.float64)
    else:
        _PW_STARTS = _PW_SLOPES = _PW_G0 = None
        _PW_STARTS_A = _PW_SLOPES_A = _PW_ENDS_A = None
    # 窓がデータ全期間より長い = 「全期間の最高レート」。窓から外れる entry が存在しないので、
    # expiry 追跡も peak_at の線形走査も不要になる (SPSP_BT_PEAK_DAYS=36500 等で全期間 peak を試す実験用)。
    _PEAK_ALLTIME = _PEAK_PW or _common.BT_PEAK_WINDOW_SEC >= 7300 * 86400   # 20 年
    # pw は窓ではなく減衰で古い情報を切る設計なので、窓は必ず全期間で使う。
    if _PEAK_PW and _common.BT_PEAK_WINDOW_SEC < 7300 * 86400:
        raise ValueError('peak_decay_mode=pw は SPSP_BT_PEAK_DAYS=36500 等 (全期間) と併用すること')


def lv_tournament_passes(lv: int, meta: dict) -> bool:
    return t_passes(meta, LV_TFILTERS.get(lv))


def lv3_passes_ignoring_weekend(meta: dict) -> bool:
    """Lv3 tfilter の weekend_only だけ無視した判定 (SPSP_BT_LV3_WEEKDAY_K 用)。"""
    f = LV_TFILTERS.get(3)
    if not f:
        return True
    return t_passes(meta, {k: v for k, v in f.items() if k != 'weekend_only'})


# ── 計測中 (provisional) pfilter ──
# PROVISIONAL_PFILTER=1 のとき、cascade の topn フィルタで「計測中」プレイヤーを
# 海外勢と同様に JP カウント対象外にする (= 海外リストとは別管理).
# 計測済み条件: 1年で PROV_1Y_MIN 大会以上 OR 2年で PROV_2Y_MIN 大会以上.
# production デフォルト = 2年3大会 (= 4 シーズン精度計測で唯一 |SPR| 悪化なし.
# 1y 系条件は現役準アクティブ勢まで灰色化して僅かに劣化する).


def measured_uids_from_cache(cache) -> set:
    """cache (= 集計対象大会のみ) から「計測済み」uid 集合を返す.

    days はその snapshot の eval_date からの経過日数なので、snapshot ごとに
    1y/2y 窓が動的に決まる.
    """
    c1 = defaultdict(int)
    c2 = defaultdict(int)
    for t in cache:
        d = t['days']
        if d > 730:
            continue
        in_1y = d <= 365
        for uid, _, _ in t['players']:
            c2[uid] += 1
            if in_1y:
                c1[uid] += 1
    return {u for u, n2 in c2.items() if c1[u] >= PROV_1Y_MIN or n2 >= PROV_2Y_MIN}


def measured_uids_by_lv_from_cache(cache) -> dict:
    """{lv: 計測済み uid 集合} (= lv 2..5). その Lv の対象大会だけカウント.

    フィルタ判定は compute_tjpr_at_lv の tfilter 処理と同一
    (min_nent_gt / weekend_only / exclude_capped).
    PROVISIONAL_PFILTER=2 のとき run_shared_cascade が Lv 遷移ごとに参照する.
    """
    lv_tf = {lv: tf for lv, tf, _, _ in LV_DEFS if lv >= 2}
    c1 = {lv: defaultdict(int) for lv in lv_tf}
    c2 = {lv: defaultdict(int) for lv in lv_tf}
    for t in cache:
        d = t['days']
        if d > 730:
            continue
        in_1y = d <= 365
        passing = []
        for lv, tf in lv_tf.items():
            if tf is not None:
                if tf.get('min_nent_gt') is not None and t['nent'] <= tf['min_nent_gt']:
                    continue
                if tf.get('weekend_only') and not t['is_weekend']:
                    continue
                if tf.get('exclude_capped') and t.get('is_capped'):
                    continue
            passing.append(lv)
        if not passing:
            continue
        for uid, _, _ in t['players']:
            for lv in passing:
                c2[lv][uid] += 1
                if in_1y:
                    c1[lv][uid] += 1
    return {lv: {u for u, n2 in c2[lv].items()
                 if c1[lv][u] >= PROV_1Y_MIN or n2 >= PROV_2Y_MIN}
            for lv in lv_tf}


def sm_sqrtlog(n):
    return math.sqrt(math.log2(max(n, 2)))


def rv_info(g):
    return -np.log2(np.clip(1.0 - g, 1e-3, 1.0)).mean()


def compute_tjpr_at_lv(cache, lv_params, tfilter_dict, pfilter, return_contribs=False):
    base, floor, topN, pd = lv_params
    pe = defaultdict(list)
    for t in cache:
        if tfilter_dict is not None:
            if tfilter_dict.get('min_nent_gt') is not None and t['nent'] <= tfilter_dict['min_nent_gt']:
                continue
            if tfilter_dict.get('weekend_only') and not t['is_weekend']:
                continue
            if tfilter_dict.get('exclude_capped') and t.get('is_capped'):
                continue
        age_decay = td_exp(t['days'])
        # 平日大会の係数 (tfilter に weekday_w がある Lv のみ。既定 1.0 = 従来どおり)
        wk_w = 1.0
        if tfilter_dict is not None and not t['is_weekend']:
            wk_w = float(tfilter_dict.get('weekday_w', 1.0))
        clamped = np.maximum(t['norm'], floor)
        rp = []
        for idx in t['rounds']:
            if len(idx):
                r = clamped[idx]
                g = 1.0 - 1.0 / (1.0 + np.power(10.0, (r - base) / 400.0))
                g = np.maximum(0, g)
                rp.append(rv_info(g))
            else:
                rp.append(rv_info(np.array([0.0])))
        size_w = sm_sqrtlog(t['nent']) * wk_w
        for uid, rw, place in t['players']:
            if pfilter is not None and uid not in pfilter:
                continue
            raw_pts_sum = sum(rp[:rw])
            # raw_pts_sum=0 (= rw=0 の最速敗退) も残す → ranks.tjpr=0 にならない.
            if return_contribs:
                pe[uid].append((raw_pts_sum * size_w, age_decay, t['t_id'], raw_pts_sum))
            else:
                pe[uid].append((raw_pts_sum * size_w, age_decay))
    scores = {}
    # 寄与は配列の表 (spsp/learn.ContribTable、引くと t_id・raw_points・pos_factor・weighted・rank_in_player を名前で引ける
    # Contrib の list) に足す。以前は 1 件ずつ 5 項目の dict (北米の snap で 数百万件)。float / np.float64 でない値が
    # 来たら以前の dict に戻す (_contribs_as_dicts)
    contribs = ContribTable() if return_contribs else None
    # pfilter 入りで LV 対象大会の出場履歴 0 (= raw_pts_sum=0) の uid も score=0 で
    # cascade に残す. これがないと「Lv2 top 1024 入りしたが Lv3 対象大会で 1 点も
    # 取れてない」9 人 (= 例: CS3|Andrik, シッショー, ふわ, etc.) が cascade から
    # 完全に脱落 → 累積 Lv3 が 1024 ではなく 1015 になる. 直対評価ベースの
    # ensemble rank があるので score=0 でも順位はちゃんと付く.
    if pfilter is not None:
        for uid in pfilter:
            if uid not in pe:
                scores[uid] = 0.0
                if return_contribs:
                    contribs = _add_contribs(contribs, uid, [])
    for uid, entries in pe.items():
        entries.sort(key=lambda x: -(x[0] * x[1]))
        total = 0.0
        rows = [] if return_contribs else None
        for i, entry in enumerate(entries):
            if return_contribs:
                weighted_raw, age_decay, t_id, raw_pts = entry
            else:
                weighted_raw, age_decay = entry
            pos_factor = 1.0 if i < topN else pd ** (i - topN + 1)
            weighted = weighted_raw * age_decay * pos_factor
            total += weighted
            if return_contribs:
                rows.append((t_id, weighted_raw, age_decay, pos_factor, weighted))
        if return_contribs:
            contribs = _add_contribs(contribs, uid, rows)
        scores[uid] = total
    if return_contribs:
        return scores, contribs
    return scores


def _contrib_dict(t_id, raw_points, pos_factor, weighted, i):
    return {'t_id': t_id, 'raw_points': raw_points, 'pos_factor': pos_factor, 'weighted': weighted, 'rank_in_player': i}


def _add_contribs(contribs, uid, rows):
    """compute_tjpr_at_lv の寄与 1 人分 (rows = [(t_id, raw_points, age_decay, pos_factor, weighted), ...] 寄与の大きい順) を足す。
    contribs は ContribTable か、表せない値が来た後は以前の {uid: [dict, ...]}。足した後の contribs を返す。"""
    if type(contribs) is ContribTable:
        if all(type(x) is float or type(x) is np.float64 for r in rows for x in r[1:]):
            contribs.add(uid, rows)
            return contribs
        contribs = {u: [_contrib_dict(c['t_id'], c['raw_points'], c['pos_factor'], c['weighted'], c['rank_in_player'])
                        for c in lst] for u, lst in contribs.items()}
    contribs[uid] = [_contrib_dict(t, raw, pos, w, i) for i, (t, raw, _age, pos, w) in enumerate(rows)]
    return contribs


def run_shared_cascade_with_contribs(cache, bt_peak180_snap, measured_uids=None):
    """Same as run_shared_cascade but also captures per-LV per-tour contribs.

    Returns:
      player_max_lv, player_ens_at_max_lv, player_tjpr_at_max_lv,
      level_contribs (= list of dict[uid -> list of contribs], indexed by lv-1)
    """
    player_max_lv = {}
    player_ens_at_max_lv = {}
    player_tjpr_at_max_lv = {}
    level_contribs = []
    prev_pfilter = None
    for lv, tfilter, lv_params, topn_next in LV_DEFS:
        tjpr_scores, contribs = compute_tjpr_at_lv(
            cache, lv_params, tfilter, prev_pfilter, return_contribs=True)
        level_contribs.append(contribs)
        common = set(tjpr_scores)
        if prev_pfilter is not None:
            common &= prev_pfilter
        if not common:
            continue
        # tie-break = uid (= peak180 / tjpr_scores 同点ブロックが snapshot 間で
        # deterministic. これがないと ens_rank も unstable になる).
        bt_in_c = sorted(common, key=lambda u: (-bt_peak180_snap.get(u, float('-inf')), u))
        bt_rank = {u: i + 1 for i, u in enumerate(bt_in_c)}
        tj_in_c = sorted(common, key=lambda u: (-tjpr_scores[u], u))
        tj_rank = {u: i + 1 for i, u in enumerate(tj_in_c)}
        ens_score = {u: (bt_rank[u] + tj_rank[u]) / 2.0 for u in common}
        # 同率は BT → TJPR → uid の順 (= bt_rank/tj_rank は uid tiebreak 済で
        # それぞれ unique なので bt_rank だけで十分だが、defensive に full chain).
        ens_sorted = sorted(ens_score.items(),
                             key=lambda x: (x[1], bt_rank[x[0]], tj_rank[x[0]], x[0]))
        ens_rank = {u: i + 1 for i, (u, _) in enumerate(ens_sorted)}
        # cascade 内部は raw ens_r (= ens_score 順) を保持. 表示用 jp-counter 化は
        # snapshots ビルド側 (= _build_snapshots_from_v4_cascade) でまとめて行う.
        # こうすると postprocess の ranks.ensemble 算出が ens_score 順を保てる.
        for u in common:
            player_max_lv[u] = lv
            player_ens_at_max_lv[u] = ens_rank[u]
            player_tjpr_at_max_lv[u] = tjpr_scores[u]
        if topn_next is not None:
            # 次 LV の pfilter = jp-counter top-N (= JP top-N + 該当灰色勢).
            # raw rank を渡して jp_topn_filter が拡張済み集合を返す.
            base_rank = tj_rank if PFILTER_MODE == 'tjpr_only' else ens_rank
            if measured_uids is None:
                excl = OVERSEAS_UIDS
            else:
                m = measured_uids[lv + 1] if isinstance(measured_uids, dict) else measured_uids
                excl = OVERSEAS_UIDS | {u for u in base_rank if u not in m}
            prev_pfilter = jp_topn_filter(base_rank, topn_next, excl)
        else:
            prev_pfilter = set(common)
    return player_max_lv, player_ens_at_max_lv, player_tjpr_at_max_lv, level_contribs


def run_shared_cascade(cache, bt_peak180_snap, measured_uids=None):
    """TJPR cascade with ensemble pfilter → player_max_lv + ens_rank @max_lv.

    measured_uids が与えられたら、それに含まれない uid (= 計測中) を topn
    フィルタで海外勢と同様に JP カウント対象外として扱う (= eval 用 opt-in).

    各 LV で:
      - TJPR score at LV (= prev_pfilter で絞った input、LV-specific tfilter)
      - common = TJPR-rated ∩ prev_pfilter
      - BT rank within common, TJPR rank within common
      - ensemble rank = (BT_r + TJPR_r) / 2 → rank
      - 次 LV pfilter = top topn_next by ens rank
    Returns:
      player_max_lv: dict[uid -> int (1-5)]
      player_ens_at_max_lv: dict[uid -> int (rank within max_lv)]
    """
    player_max_lv = {}
    player_ens_at_max_lv = {}
    player_tjpr_at_max_lv = {}
    prev_pfilter = None
    for lv, tfilter, lv_params, topn_next in LV_DEFS:
        tjpr_scores = compute_tjpr_at_lv(cache, lv_params, tfilter, prev_pfilter)
        common = set(tjpr_scores)
        if prev_pfilter is not None:
            common &= prev_pfilter
        if not common:
            continue
        # tie-break = uid (= peak180 / tjpr_scores 同点ブロックが snapshot 間で
        # deterministic. これがないと ens_rank も unstable になる).
        bt_in_c = sorted(common, key=lambda u: (-bt_peak180_snap.get(u, float('-inf')), u))
        bt_rank = {u: i + 1 for i, u in enumerate(bt_in_c)}
        tj_in_c = sorted(common, key=lambda u: (-tjpr_scores[u], u))
        tj_rank = {u: i + 1 for i, u in enumerate(tj_in_c)}
        ens_score = {u: (bt_rank[u] + tj_rank[u]) / 2.0 for u in common}
        # 同率は BT → TJPR → uid の順 (= bt_rank/tj_rank は uid tiebreak 済で
        # それぞれ unique なので bt_rank だけで十分だが、defensive に full chain).
        ens_sorted = sorted(ens_score.items(),
                             key=lambda x: (x[1], bt_rank[x[0]], tj_rank[x[0]], x[0]))
        ens_rank = {u: i + 1 for i, (u, _) in enumerate(ens_sorted)}
        # cascade 内部 ens_r は raw 保持 (= postprocess の ranks.ensemble が ens_score 順
        # を維持する). 表示用 jp-counter 化は _build_snapshots_from_v4_cascade で行う.
        for u in common:
            player_max_lv[u] = lv
            player_ens_at_max_lv[u] = ens_rank[u]
            player_tjpr_at_max_lv[u] = tjpr_scores[u]
        if topn_next is not None:
            base_rank = tj_rank if PFILTER_MODE == 'tjpr_only' else ens_rank
            if measured_uids is None:
                excl = OVERSEAS_UIDS
            else:
                # dict なら Lv 別 (= 次 Lv の対象大会でカウントした集合)、set なら全 Lv 共通.
                m = measured_uids[lv + 1] if isinstance(measured_uids, dict) else measured_uids
                # 海外 + 計測中 (= 別管理の 2 集合をこの場で union するだけ)
                excl = OVERSEAS_UIDS | {u for u in base_rank if u not in m}
            prev_pfilter = jp_topn_filter(base_rank, topn_next, excl)
        else:
            prev_pfilter = set(common)
    return player_max_lv, player_ens_at_max_lv, player_tjpr_at_max_lv


# ───────────────── per-tournament 増分レベル判定 (O(T)) のヘルパ ─────────────────
# learn_bt_lv_shared_g2_pure の SPSP_PER_TOUR_LV=1 経路でのみ使用。SNAPSHOT_DAYS 粒度
# (= run_cascade_at の precompute_banzuke 全prior再計算) を、各 serious 大会の直前に
# 「増分スコア + ソートのみ」で player_lv を更新する形に置換し O(T²)→O(T) にする。
# ロジックは run_shared_cascade / measured_uids_at_ts と一致 (eval/baseline_repro/
# per_tour_levels.py で snapshot 100% 再現を検証済の実装をそのまま移植)。


def _pt_agg(es, topN, pd):
    """compute_tjpr_at_lv の per-player 集計 (上位 topN は 1.0、以降 pd^k 減衰) を増分で。"""
    es = sorted(es, reverse=True)
    tot = 0.0
    for i, e in enumerate(es):
        tot += e * (1.0 if i < topN else pd ** (i - topN + 1))
    return tot


_UID_PERM_CACHE: list = []   # [(配列, その argsort, 昇順の値)] 直近 6 件 (同じ配列 or 先頭が同じ配列に使う)


def _uid_perm(arr):
    """np.argsort(arr, kind='stable') (uid 昇順の並び)。同じ配列オブジェクトに対しては 1 回だけ計算する
    (カスケード・表示順位・アンカーで同じ uid 配列を何度も並べていた)。配列は作ったあと書き換えない前提
    (uids0 / pua は毎回新しく作られ、書き換えられない)。

    uids0 (scores[1] のキー) と pua (peak のキー) は dict のキーの並びで、選手は後ろに足されるだけなので、
    前回の配列がこの配列の先頭とそっくり同じことが多い。そのときは足された分だけ並べて、前回の並びに差し込む
    (全体の並べ直し O(P log P) → O(P)。2026-10-17、北米 20 万人で大会ごとに 4 回)。結果は argsort(stable) と同じ:
    先頭の要素は元の位置が前なので、同じ値なら差し込む要素より前 (searchsorted side='right')、足された分どうしは
    stable に並べる。"""
    for a, perm, _sv in _UID_PERM_CACHE:
        if a is arr:
            return perm
    n = len(arr)
    best = None
    for ent in _UID_PERM_CACHE:
        L = len(ent[0])
        if 0 < L <= n and (best is None or L > len(best[0])) and ent[0].dtype == arr.dtype \
                and np.array_equal(arr[:L], ent[0]):
            best = ent
    if best is None:
        perm = np.argsort(arr, kind='stable')
        sv = arr[perm]
    else:
        a, p, sv0 = best
        L = len(a)
        if L == n:
            perm, sv = p, sv0
        else:
            o = np.argsort(arr[L:], kind='stable') + L
            add = arr[o]
            pos = np.searchsorted(sv0, add, side='right')
            perm = np.insert(p, pos, o)
            sv = np.insert(sv0, pos, add)
    _UID_PERM_CACHE.append((arr, perm, sv))
    del _UID_PERM_CACHE[:-6]
    return perm


_OV_MASK: list = [None, None, None]   # [OVERSEAS_UIDS オブジェクト, 長さ, 真偽の表]


def _ov_table(size):
    """_OV_MASK を OVERSEAS_UIDS の長さ size 以上の表にする (別スレッドからも呼ぶので丸ごと差し替える)。"""
    if _OV_MASK[0] is not OVERSEAS_UIDS or _OV_MASK[1] < size:
        ov = np.fromiter((u for u in OVERSEAS_UIDS if isinstance(u, int) and u >= 0), dtype=np.int64)
        size_ov = max(size, int(ov.max()) + 1 if len(ov) else 0)
        tbl = np.zeros(size_ov, dtype=bool); tbl[ov] = True
        _OV_MASK[:] = [OVERSEAS_UIDS, size_ov, tbl]


class _MeasMask:
    """計測済み集合 (pt_meas_set) の uid を添字にした真偽の表。学習側が集合と一緒に書き換え、_gray_flags に集合の
    代わりに渡す (呼ばれるたびに集合から表を作り直していた: 北米で学習の 5% ほど。2026-10-23)。uid が 0 以上の整数の
    集合だけ (でなければ from_set が None を返し、呼び出し側は集合のまま渡す)。"""
    __slots__ = ('mm',)

    def __init__(self, mm):
        self.mm = mm

    @classmethod
    def from_set(cls, s):
        try:
            a = np.fromiter(s, dtype=np.int64, count=len(s))
        except (TypeError, ValueError, OverflowError):
            return None
        if len(a) and a.min() < 0:
            return None
        mm = np.zeros(max(int(a.max()) + 1 if len(a) else 0, 1024), dtype=bool)
        mm[a] = True
        return cls(mm)

    def add(self, u):
        if u >= len(self.mm):
            mm = np.zeros(max(u + 1, 2 * len(self.mm)), dtype=bool); mm[:len(self.mm)] = self.mm; self.mm = mm
        self.mm[u] = True

    def discard(self, u):
        if 0 <= u < len(self.mm):
            self.mm[u] = False

    def copy(self):
        return _MeasMask(self.mm.copy())

    def to_set(self):
        return set(np.flatnonzero(self.mm).tolist())


def _gray_flags(uids_arr, keys, measured):
    """[(u in OVERSEAS_UIDS) or (u not in measured) for u in keys] の numpy 版 (keys と uids_arr は同じ並び)。

    uid を添字にした真偽の表で引く (選手ごとの Python ループをやめる。2026-09-28)。uid が 0 以上の整数で
    なければ (想定外) 元のループで計算するので、結果は常に同じ。measured は _MeasMask でもよい。"""
    n = len(uids_arr)
    if n == 0:
        return np.zeros(0, dtype=bool)
    if isinstance(measured, _MeasMask):
        if uids_arr.min() < 0:
            measured = measured.to_set()
        else:
            size = int(uids_arr.max()) + 1
            _ov_table(size)
            mmv = measured.mm
            if size <= len(mmv):
                inm = mmv[uids_arr]
            else:
                inm = np.zeros(n, dtype=bool); ok = uids_arr < len(mmv); inm[ok] = mmv[uids_arr[ok]]
            return _OV_MASK[2][uids_arr] | ~inm
    try:
        meas = np.fromiter(measured, dtype=np.int64, count=len(measured))
    except (TypeError, ValueError, OverflowError):
        meas = None
    if meas is None or uids_arr.min() < 0 or (len(meas) and meas.min() < 0):
        if keys is None:
            keys = uids_arr.tolist()
        _ov = OVERSEAS_UIDS
        return np.fromiter(((u in _ov) or (u not in measured) for u in keys), dtype=bool, count=n)
    size = int(uids_arr.max()) + 1
    _ov_table(size)
    mm = np.zeros(size, dtype=bool)
    mm[meas[meas < size]] = True
    return _OV_MASK[2][uids_arr] | ~mm[uids_arr]


def _rank_desc_then_uid(vals, perm_uid):
    """np.lexsort((uids, -vals)) と同じ並び (vals 降順、同値は uid 昇順)。

    perm_uid = np.argsort(uids, kind='stable') (uid 昇順の並び)。先に uid 昇順に並べてから -vals で安定ソートすると、
    同値の中は uid 昇順のまま残る = lexsort と同じ順列になる。lexsort (キー 2 つ = 安定ソート 2 回) より速い
    (選手 5.7 万人で 7 ms → 3.5 ms。2026-09-28)。"""
    return perm_uid[np.argsort(-vals[perm_uid], kind='stable')]


# _rank_desc_then_uid_inc の呼び出し場所ごとの前回分 {stream: (uids, -vals, 並び)}。配列は作ったあと書き換えない前提
_RANK_INC_CACHE: dict = {}
_RANK_INC_MAX_FRAC = 0.25   # 値が変わった選手 (+ 新しい選手) がこの割合を超えたら全体を並べ直す


def _rank_desc_then_uid_inc(stream, uids, vals, perm_uid):
    """_rank_desc_then_uid(vals, perm_uid) と同じ並びを、同じ呼び出し場所 (stream) の前回の並びから作る。

    学習は serious 大会ごとに全選手 (北米 20 万人) を同じキー (値 降順、同値は uid 昇順) で並べ直していた
    (学習時間の 4 割、2026-10-17)。前回から値が変わるのは参加者と peak の窓落ちだけなので、
    前回の並びから「値が変わった選手」を抜き、その分 (+ 新しく足された選手) を並べて二分探索で差し込む。
    キー (-値, uid) は uid が重複しないので全順序 = 並びは一意で、全体を並べ直した結果と同じ。

    使える条件 (満たさなければ全体を並べ直す): uids は dict のキーの並び (重複なし) で、前回の uids がこの uids の
    先頭とそっくり同じ (選手は後ろに足されるだけ)、値に NaN が無い、変わった選手が _RANK_INC_MAX_FRAC 以下。"""
    n = len(uids)
    negv = -vals
    prev = _RANK_INC_CACHE.get(stream)
    order = None
    if prev is not None and n and not np.isnan(negv).any():
        pu, pv, po = prev
        L = len(pu)
        if 0 < L <= n and pu.dtype == uids.dtype and np.array_equal(uids[:L], pu):
            chg = negv[:L] != pv            # 値が変わった (前回の並びの中の位置が変わりうる) 選手
            n_chg = int(np.count_nonzero(chg)) + (n - L)
            if n_chg <= n * _RANK_INC_MAX_FRAC:
                if n_chg == 0:
                    order = po
                else:
                    kept = po[~chg[po]]                                   # 値が同じ選手の前回の並び (相対順は不変)
                    cidx = np.concatenate((np.flatnonzero(chg), np.arange(L, n)))
                    cidx = cidx[np.lexsort((uids[cidx], negv[cidx]))]     # 差し込む選手を (-値, uid) 順に
                    kv = negv[kept]
                    cv = negv[cidx]
                    lo = np.searchsorted(kv, cv, side='left')
                    hi = np.searchsorted(kv, cv, side='right')
                    pos = lo.copy()
                    tie = np.flatnonzero(hi > lo)                          # 同じ値の選手がいる → その中で uid 順
                    if len(tie):
                        ku = uids[kept]
                        cu = uids[cidx]
                        # 同じ (lo, hi) の塊ごとに 1 回だけ二分探索 (cidx は値の順なので塊は連続している)
                        j = 0
                        while j < len(tie):
                            a = tie[j]; k = j + 1
                            while k < len(tie) and lo[tie[k]] == lo[a] and hi[tie[k]] == hi[a]:
                                k += 1
                            grp = tie[j:k]
                            pos[grp] = lo[a] + np.searchsorted(ku[lo[a]:hi[a]], cu[grp], side='left')
                            j = k
                    order = np.insert(kept, pos, cidx)
    if order is None:
        order = perm_uid[np.argsort(negv[perm_uid], kind='stable')]
    _RANK_INC_CACHE[stream] = (uids, negv, order)
    return order


# 学習の書き出しの計算の区間ごとの秒 (診断用、_OutWorker.close が出す。スレッド間で足すので目安)
_SEC: dict = defaultdict(float)
_pcs = time.perf_counter

_EK = np.int64(1) << 32   # 整数キー 2 本を 1 本にするときの桁 (順位・Lv 内順位は 2^32 未満)


def _order_asc_inc(stream, uids, keys):
    """np.argsort(keys) と同じ並びを、同じ呼び出し場所 (stream) の前回の並びから作る (keys は全員違う整数)。

    総合の並び (順位の和) は serious 大会ごとに全選手を並べ直していた (北米 20 万人、学習の書き出しの計算の大半。
    2026-10-23)。1 回の更新で順位が変わる選手は北米で中央値 1 割なので、前回の並びから値が変わった選手を抜き、
    その分 (+ 新しく足された選手) を並べて二分探索で差し込む。キーが全員違うので並びは一意 = argsort と同じ。
    使える条件 (満たさなければ全体を並べ直す): 前回の uids がこの uids の先頭とそっくり同じ (選手は後ろに足されるだけ)、
    変わった選手が _RANK_INC_MAX_FRAC 以下。"""
    n = len(uids)
    prev = _RANK_INC_CACHE.get(stream)
    order = None
    if prev is not None and n:
        pu, pk, po = prev
        L = len(pu)
        if 0 < L <= n and pu.dtype == uids.dtype and np.array_equal(uids[:L], pu):
            chg = keys[:L] != pk
            n_chg = int(np.count_nonzero(chg)) + (n - L)
            if n_chg <= n * _RANK_INC_MAX_FRAC:
                if n_chg == 0:
                    order = po
                else:
                    kept = po[~chg[po]]                       # 値が同じ選手の前回の並び (キーの昇順のまま)
                    cidx = np.concatenate((np.flatnonzero(chg), np.arange(L, n)))
                    cidx = cidx[np.argsort(keys[cidx])]
                    order = np.insert(kept, np.searchsorted(keys[kept], keys[cidx]), cidx)
    if order is None:
        order = np.argsort(keys)
    _RANK_INC_CACHE[stream] = (uids, keys, order)
    return order


def _order_lv_desc_rank(lv, rk):
    """np.argsort((max Lv - lv) * (max rk + 1) + rk) と同じ並び (Lv の降順、同じ Lv の中は rk の昇順)。

    rk = 所属 Lv の中の順位で、同じ Lv の中では全員違う正の整数 (cascade の pens)。並べ替えずに、(Lv, rk) ごとの
    場所の表に置いて詰める (O(選手数 + 各 Lv の順位の最大)。北米 20 万人を毎回並べ直していた。2026-10-23)。
    想定 (rk が正・同じ Lv で重複なし) を外れたら並べ替える。"""
    n = len(lv)
    if n == 0:
        return np.zeros(0, dtype=np.int64)
    if rk.min() < 1 or lv.min() < 0:
        return np.argsort((lv.max() - lv) * (int(rk.max()) + 1) + rk)
    lvs = np.unique(lv)[::-1]                       # Lv の降順
    span = np.zeros(int(lvs[0]) + 1, dtype=np.int64)
    np.maximum.at(span, lv, rk)                     # Lv ごとの rk の最大
    off = np.zeros(len(span), dtype=np.int64)
    acc = 0
    for L in lvs.tolist():
        off[L] = acc; acc += int(span[L]) + 1
    tbl = np.full(acc, -1, dtype=np.int64)
    tbl[off[lv] + rk] = np.arange(n, dtype=np.int64)
    order = tbl[tbl >= 0]
    if len(order) != n:                             # 同じ Lv で rk が重なった (想定外)
        return np.argsort((lv.max() - lv) * (int(rk.max()) + 1) + rk)
    return order


_MISSING = object()


class _ColsSnap:
    """_PtCols のその時点の写し (_pt_cascade_inc が読む n / uids / col だけ)。別スレッドで cascade を引くときに使う
    (学習側が後から set_score で書き換えるので col は写す。uids は後ろに足されるだけなので切り出しでよい)。"""
    __slots__ = ('n', 'uids', 'col', 'pk')

    def __init__(self, cols):
        n = cols.n
        self.n = n
        self.uids = cols.uids[:n]
        self.col = {lv: a[:n].copy() for lv, a in cols.col.items()}
        self.pk = None   # pka_all で渡す


class _Hold:
    """別スレッドで作る値の入れ物。get() はできるまで待つ (スレッドが失敗したら投げる)。"""
    __slots__ = ('v', 'ev', 'w')

    def __init__(self, w=None, v=_MISSING):
        import threading
        self.w = w
        self.ev = threading.Event()
        self.v = None
        if v is not _MISSING:
            self.v = v; self.ev.set()

    def set(self, v):
        self.v = v; self.ev.set()

    def get(self):
        if self.ev.is_set():
            return self.v
        _t = time.perf_counter()
        while not self.ev.wait(1.0):
            if self.w is not None:
                self.w._check()
        if self.w is not None:
            self.w.t_wait += time.perf_counter() - _t
        return self.v


class _OutWorker:
    """学習の結果に効かない書き出し用の計算 (大会直後の cascade・表示順位・大会ごとのアンカー・過去最高順位の追跡) を
    別スレッドで流す。

    北米は serious 大会ごとに全選手 (20 万人) を何度も並べていて、学習の 3〜4 割がこれだった (2026-10-23 計測)。
    どれも学習 (レート・レベル・点) には使わず、pt_at_time_by_path と peak_track に書くだけなので、学習を止めずに流す。
    2 段: 計算 (compute: cascade・表示順位、書き込みなし) は ncompute 本のスレッドで並べて、書き込み (apply: アンカー・
    過去最高) は 1 本のスレッドで積んだ順に (FIFO) 流すので、書き込みの順番は以前と同じ = 結果も同じ。計算どうしで
    前の結果を使うもの (次の大会の直前で使い回す表示順位) は _Hold で待つ。numpy の並べ替え・集計は GIL を外すので
    学習と並んで進む。渡す配列・集合は後から書き換えないもの (呼び出し側で写す)。
    on=False ならその場で実行する (以前と同じ)。pt_at_time_by_path / peak_track を学習側で読むときは先に drain する。"""

    def __init__(self, on: bool, qmax: int = 6, ncompute: int | None = None):
        self.on = on
        self.err = None
        # 秒: 学習側が待った (put で列が一杯 / drain / _Hold.get) / 計算のスレッド (合計) / 書き込みのスレッド
        self.t_wait = self.t_c = self.t_a = 0.0
        if on:
            import queue
            import threading
            from concurrent.futures import ThreadPoolExecutor
            if ncompute is None:
                ncompute = int(os.environ.get('SPSP_LEARN_OUT_COMPUTE', '2'))
            self.ex = ThreadPoolExecutor(max_workers=max(1, ncompute), thread_name_prefix='spsp-learn-out-compute')
            self.sem = threading.BoundedSemaphore(qmax)   # 積んだまま書き込みが済んでいない仕事の数の上限
            self.q2 = queue.Queue()                      # (apply, compute の future) を積んだ順に
            self.th2 = threading.Thread(target=self._run2, name='spsp-learn-out-apply', daemon=True)
            self.th2.start()

    def _timed(self, compute):
        _t = time.perf_counter()
        try:
            return compute()
        finally:
            self.t_c += time.perf_counter() - _t

    def _run2(self):
        while True:
            job = self.q2.get()
            try:
                if job is None:
                    return
                apply, fut = job
                v = fut.result() if fut is not None else None   # 計算の失敗はここで投げる
                if self.err is None and apply is not None:
                    _t = time.perf_counter(); apply(v); self.t_a += time.perf_counter() - _t
            except BaseException as e:   # 学習側の次の put / drain で投げ直す
                if self.err is None:
                    self.err = e
            finally:
                if job is not None:
                    self.sem.release()
                self.q2.task_done()

    def _check(self):
        if self.err is not None:
            raise RuntimeError('学習の書き出しスレッドで失敗') from self.err

    def put(self, apply, compute=None):
        """compute() (計算のスレッド) の結果を apply(結果) (書き込みのスレッド、積んだ順) に渡す。compute 無しなら apply(None)。"""
        if not self.on:
            apply(compute() if compute is not None else None)
            return
        self._check()
        _t = time.perf_counter(); self.sem.acquire(); self.t_wait += time.perf_counter() - _t
        fut = self.ex.submit(self._timed, compute) if compute is not None else None
        self.q2.put((apply, fut))

    def drain(self):
        if self.on:
            _t = time.perf_counter(); self.q2.join(); self.t_wait += time.perf_counter() - _t
        self._check()

    def close(self):
        if self.on:
            _t = time.perf_counter()
            self.q2.join()
            self.q2.put(None); self.th2.join(); self.ex.shutdown(wait=True)
            self.t_wait += time.perf_counter() - _t
            self.on = False
            print(f"  [learn-out] 学習側が待った {self.t_wait:.1f}s / 計算のスレッド (合計) {self.t_c:.1f}s / "
                  f"書き込みのスレッド {self.t_a:.1f}s", flush=True)
            print("  [learn-out] 区間 " + "  ".join(f"{k}={v:.1f}s" for k, v in sorted(_SEC.items(), key=lambda x: -x[1]) if k[0] != '#')
                  + "  回数 " + " ".join(f"{k[1:]}={int(v)}" for k, v in _SEC.items() if k[0] == '#'),
                  flush=True)
        self._check()


class _PtCols:
    """学習中の pt_scores ({Lv: {uid: スコア}}) と peak ({uid: 値}) を、scores[1] のキーの並びの配列でも持つ。

    _pt_cascade_inc は serious 大会ごとに全選手 (北米 20 万人) の値を dict から 1 人ずつ取り出して配列にしていて、
    それが cascade の時間の 7 割だった (2026-10-18)。値を書くとき (大会ごとに数百人) に配列も書けば、取り出しは
    配列の切り出しで済む。値は dict と同じ (Lv>1 の未スコアは 0.0、peak が無ければ -inf = 以前の get の既定値)。
    学習中の書き込みは必ず set_score / set_peak を通す (dict を直接書くと配列とずれる)。保存はしない (dict から作り直す)。
    uids[:n] は後ろに足すだけで書き換えないので、切り出した配列を前回分のキャッシュに持ってよい。
    peak は表示順位 (直対評価) で peak 自身のキーの並びでも使うので、その並びの配列 (pk_uids / pk_vals) も持つ
    (peak のキーは足されるだけ = dict の並びの末尾に足す)。"""
    __slots__ = ('scores', 'peak', 'idx', 'n', 'cap', 'uids', 'col', 'pk', 'ver',
                 'pk_idx', 'pk_n', 'pk_cap', 'pk_uids', 'pk_vals')

    def __init__(self, scores, peak):
        self.scores = scores; self.peak = peak
        self.idx = {}; self.n = 0; self.cap = 0; self.ver = 0
        self.uids = np.zeros(0, dtype=np.int64)
        self.col = {lv: np.zeros(0, dtype=np.float64) for lv in scores}
        self.pk = np.zeros(0, dtype=np.float64)
        self._append(list(scores[1]))
        self.pk_idx = {}; self.pk_n = 0; self.pk_cap = 0
        self.pk_uids = np.zeros(0, dtype=np.int64); self.pk_vals = np.zeros(0, dtype=np.float64)
        self._pk_append(list(peak.keys()), list(peak.values()))

    def _pk_append(self, keys, vals):
        n = self.pk_n; need = n + len(keys)
        if need > self.pk_cap:
            cap = max(need, 2 * self.pk_cap, 1024)
            u = np.zeros(cap, dtype=np.int64); u[:n] = self.pk_uids[:n]; self.pk_uids = u
            v = np.zeros(cap, dtype=np.float64); v[:n] = self.pk_vals[:n]; self.pk_vals = v
            self.pk_cap = cap
        for j, k in enumerate(keys):
            self.pk_idx[k] = n + j
        self.pk_uids[n:need] = keys; self.pk_vals[n:need] = vals
        self.pk_n = need

    def peak_arrays(self):
        """(peak のキーの並びの uid, 値) = (np.fromiter(peak.keys()), np.fromiter(peak.values()))。"""
        return self.pk_uids[:self.pk_n], self.pk_vals[:self.pk_n]

    def peak_arrays_with(self, upd):
        """peak の写しに upd ({uid: 値}、反復順に書く) を書いた dict の (uid, 値) の配列。peak 自体は変えない。"""
        m = self.pk_n
        vals = self.pk_vals[:m].copy()
        nu = []; nv = []
        for u, v in upd.items():
            i = self.pk_idx.get(u)
            if i is not None:
                vals[i] = v
            else:
                nu.append(u); nv.append(v)
        if not nu:
            return self.pk_uids[:m], vals
        return (np.concatenate((self.pk_uids[:m], np.asarray(nu, dtype=np.int64))),
                np.concatenate((vals, np.asarray(nv, dtype=np.float64))))

    def _append(self, keys):
        n = self.n; need = n + len(keys)
        if need > self.cap:
            cap = max(need, 2 * self.cap, 1024)
            def ext(a, fill):
                b = np.full(cap, fill, dtype=a.dtype); b[:n] = a[:n]; return b
            self.uids = ext(self.uids, 0)
            self.col = {lv: ext(a, 0.0) for lv, a in self.col.items()}
            self.pk = ext(self.pk, float('-inf'))
            self.cap = cap
        _ninf = float('-inf'); _pg = self.peak.get
        for j, u in enumerate(keys):
            i = n + j
            self.idx[u] = i; self.uids[i] = u
            for lv, a in self.col.items():
                a[i] = self.scores[lv].get(u, 0.0)
            self.pk[i] = _pg(u, _ninf)
        self.n = need

    def set_score(self, lv, u, v):
        self.scores[lv][u] = v
        self.ver += 1
        i = self.idx.get(u)
        if i is not None:
            self.col[lv][i] = v
        elif lv == 1:          # scores[1] に新しく足された = dict の並びの末尾
            self._append([u])

    def set_peak(self, u, v):
        self.peak[u] = v
        i = self.idx.get(u)
        if i is not None:
            self.pk[i] = v
        j = self.pk_idx.get(u)
        if j is not None:
            self.pk_vals[j] = v
        else:
            self._pk_append([u], [v])


class _PmaxView:
    """cascade の所属 Lv ({uid: lv}) を dict に組み立てずに配列のまま引く (player_lv の get だけ使う)。

    学習は serious 大会ごとに player_lv を作り直していて、20 万人の dict を作るのが cascade の 3 分の 1 だった
    (2026-10-18)。引くのは参加者だけなので配列で足りる。保存するときは to_dict() (dict(zip(uids0, pmax)) と同じ並び)。"""
    __slots__ = ('idx', 'n', 'uids', 'lv')

    def __init__(self, idx, n, uids, lv):
        self.idx = idx; self.n = n; self.uids = uids; self.lv = lv

    def get(self, u, default=None):
        i = self.idx.get(u)
        if i is None or i >= self.n:   # cascade の後に足された選手 (このときの cascade には居ない)
            return default
        return int(self.lv[i])

    def __bool__(self):
        return self.n > 0

    def to_dict(self):
        return dict(zip(self.uids.tolist(), self.lv.tolist()))


def _pt_cascade_inc(scores, peak, measured, cols=None, pka_all=None, want_dict=True, stream=''):
    """run_shared_cascade と同一ロジックの numpy 完全ベクトル化版 (TJPR は増分 scores[lv])。

    lexsort のキー順は run_shared_cascade の tuple ソートに一致:
      bt : (-peak, uid) / tjpr : (-score, uid) / ens : (ens, btr, tjr, uid)。
    pf (topn フィルタ) も numpy ベクトル化 (jp_topn_filter と等価)。
    pmax は player_lv 用に dict、pens/ptj/gray は anchor 用に array で返し dict 往復を排除する。
    cols (_PtCols) があれば値は dict ではなくその配列から取る (同じ値)。pka_all = cols の並びの peak の配列
    (省略時は cols.pk = peak と同じ中身)。want_dict=False なら pmax_dict は作らない (None)。
    Returns: (pmax_dict, arrays) ; arrays = (uids0, pmax_arr, pens_arr, ptj_arr, gray0) or None。
    """
    # cols が _ColsSnap (別スレッド用の写し) なら scores は見ない (学習側が書き足していくので長さが合わない)
    frozen = isinstance(cols, _ColsSnap)
    sc1 = None if frozen else scores[1]
    if (cols.n == 0) if frozen else not sc1:
        return {}, None
    if cols is not None:
        n0 = cols.n
        assert frozen or n0 == len(sc1)
        keys0 = None
        uids0 = cols.uids[:n0]
    else:
        # 選手ごとの Python ループは numpy の要素 (np.int64) ではなく元の dict のキー (int) で回す:
        # int(np.int64) の変換がループの半分以上を占めていた (2026-09-28)。値と並びは同じ (キーは sc1 の挿入順)。
        keys0 = list(sc1)
        n0 = len(keys0)
        uids0 = np.fromiter(keys0, dtype=np.int64, count=n0)
    pmax_arr = np.zeros(n0, dtype=np.int64)
    pens_arr = np.zeros(n0, dtype=np.int64)
    ptj_arr = np.zeros(n0, dtype=np.float64)
    _t = _pcs()
    gray0 = _gray_flags(uids0, keys0, measured)   # = (u in OVERSEAS_UIDS) or (u not in measured)
    _SEC['c_gray'] += _pcs() - _t; _t = _pcs()
    if cols is not None:
        pka0 = (cols.pk if pka_all is None else pka_all)[:n0]
    elif isinstance(peak, _PwSnap):
        pka0 = peak.gather(uids0)
    else:
        _pg = peak.get; _ninf = float('-inf')
        pka0 = np.fromiter((_pg(u, _ninf) for u in keys0), dtype=np.float64, count=n0)
    perm_uid = _uid_perm(uids0)   # uid 昇順 (lv1 の並べ替えで使う)
    _SEC['c_perm'] += _pcs() - _t; _t = _pcs()
    cur = np.arange(n0)                       # lv1 = 全 lv1 プレイヤー (uids0 への index)
    for lv, tfilter, lv_params, topn_next in LV_DEFS:
        if len(cur) == 0:
            break
        sc = None if frozen else scores[lv]
        if lv == 1:
            # cur = 全員 (arange) なので sub の並び = sc1 のキーの並び = sc1.values() の並び
            tja = cols.col[1][:n0] if cols is not None else np.fromiter(sc1.values(), dtype=np.float64, count=n0)
            sub = uids0
        else:                                # lv>1: common = pf (未スコアは 0 埋め)
            sub = uids0[cur]
            if cols is not None:
                tja = cols.col[lv][cur]
            else:
                _sg = sc.get
                tja = np.fromiter((_sg(u, 0.0) for u in sub.tolist()), dtype=np.float64, count=len(sub))
        m = len(cur)
        ua = sub; pka = pka0[cur]; seq = np.arange(1, m + 1)
        btr = np.empty(m, dtype=np.int64); tjr = np.empty(m, dtype=np.int64); er = np.empty(m, dtype=np.int64)
        if lv == 1:
            # 全選手 (cur = arange) の段だけ速い並べ替えにする。結果の順列は下の lexsort と同じ:
            #   btr / tjr = (値 降順, uid 昇順) → _rank_desc_then_uid
            #   er = (ens 昇順, btr, tjr, uid) → ens = (btr+tjr)/2 なので (btr+tjr, btr) の整数キー 1 本を
            #        uid 昇順に並べてから安定ソート (同値なら btr・tjr も同じ = uid 順、lexsort と同じ)
            bt_ord1 = _rank_desc_then_uid_inc('casc_bt' + stream, uids0, pka, perm_uid)
            btr[bt_ord1] = seq
            tjr[_rank_desc_then_uid_inc('casc_tj' + stream, uids0, tja, perm_uid)] = seq
            no_nan = not (np.isnan(pka0).any() or np.isnan(tja).any())
            # btr は 1..m の順列なので (btr + tjr, btr) の整数キーは全員違う → 並びは一意 (uid 順に並べてからの
            # 安定ソートと同じ)。前回の並びから変わった分だけ差し込み直す (_order_asc_inc。桁は m によらない _EK:
            # 以前の (m + 1) 桁と同じ並び) (2026-10-17 / 10-23)
            ekey = (btr + tjr) * _EK + btr
            er[_order_asc_inc('casc_ens' + stream, uids0, ekey)] = seq
        else:
            if no_nan and m * 16 >= n0 and not np.isnan(tja).any():
                # 大きい段 (北米は灰色 = 計測中・海外が多く、Lv2 の母集合が数万人になる) は lv1 の並びから抜き出す (O(n0))。
                # (-peak, uid) は全順序なので、全員の並びを cur に制限したもの = cur の中で並べたもの (lexsort と同じ)。
                # tjr も uid 順 (lv1 の uid 順を cur に制限) に並べてから安定ソート = lexsort((ua, -tja)) と同じ (2026-10-17)
                inv = np.full(n0, -1, dtype=np.int64); inv[cur] = np.arange(m)
                sel = inv[bt_ord1]; btr[sel[sel >= 0]] = seq
                pu = inv[perm_uid]; pu = pu[pu >= 0]
                tjr[pu[np.argsort(-tja[pu], kind='stable')]] = seq
            else:
                btr[np.lexsort((ua, -pka))] = seq
                tjr[np.lexsort((ua, -tja))] = seq
            # ens = (btr + tjr) / 2 の昇順、同値は btr → tjr → uid (= lexsort((ua, tjr, btr, ens)))。btr が 1..m の
            # 順列なので (btr + tjr, btr) の整数キーは全員違う → 並びは一意 (lv1 と同じ。2026-10-17)
            ekey = (btr + tjr) * (m + 1) + btr
            er[np.argsort(ekey)] = seq
        if lv == 1:   # cur = 全員 (arange) なので index で書かずに丸ごと写す
            pmax_arr.fill(1); pens_arr[:] = er; ptj_arr[:] = tja
        else:
            pmax_arr[cur] = lv; pens_arr[cur] = er; ptj_arr[cur] = tja
        _SEC['c_lv1' if lv == 1 else 'c_lvN'] += _pcs() - _t; _t = _pcs()
        if topn_next is not None:
            base_arr = tjr if PFILTER_MODE == 'tjpr_only' else er
            # jp_topn_filter の numpy ベクトル化 (結果同一): base 昇順 (= rank, 一意) に並べ
            # 非灰色の排他累積が topn 未満の位置を選択 (灰色は間に挟まれて自動で入る)。
            g = gray0[cur]
            # base_arr (er / tjr) は 1..m の順列 (順位を seq で振ったもの) なので、昇順の並び = 逆順列
            # (argsort(stable) と同じ。並べ替えずに O(m) で作る。2026-10-17)
            order = np.empty(m, dtype=np.int64)
            order[base_arr - 1] = np.arange(m)
            nongray_ord = (~g)[order]
            prefix_excl = np.cumsum(nongray_ord) - nongray_ord
            cur = cur[order[prefix_excl < topn_next]]
            _SEC['c_filter'] += _pcs() - _t; _t = _pcs()
    if not want_dict:
        pmax_dict = None
    elif cols is not None:
        pmax_dict = _PmaxView(cols.idx, n0, uids0, pmax_arr)
    else:
        pmax_dict = dict(zip(uids0.tolist(), pmax_arr.tolist()))
    return pmax_dict, (uids0, pmax_arr, pens_arr, ptj_arr, gray0)


def _pt_entry_for(t, meta, bt_at_t, parent_by_eid, t_meta, ref_date):
    """大会 t の banzuke エントリを、その時点の bt_at_t を種に増分計算 (1 度だけ)。

    class_virtual は親 (= parent_event_id の本戦) を含めて no-win 判定を正しくする
    (precompute_banzuke は event_id ではなく parent_event_id で親を引く)。
    ref_date は decay 基準日 (= 全 _pt_entry_for で固定すれば cascade の RANK は不変:
    decay は全 player・全大会に共通の係数なので順位に影響しない)。本番は PT_DECAY_REF で固定
    (以前は eval_date だった。固定にしたのは途中保存・再開でも同じ値になるようにするため)。
    """
    if meta.get('is_class_virtual'):
        par = parent_by_eid.get(meta.get('parent_event_id'))
        lst = [par, t] if par is not None else [t]
    else:
        lst = [t]
    cache = precompute_banzuke(lst, ref_date, bt_at_t, 0.0, t_meta, TJPR_DEFAULT_RATE,
                               allow_future=True)
    tid = id(t)
    for e in cache:
        if e['t_id'] == tid:
            return e
    return None


# {lv: その Lv に入るための topn (= 前 Lv の topn_next)}。通常 {2:2048, 3:1024, 4:512, 5:256}、
# SPSP_LV6 実験時は 6:topn も入る。


def _pt_jp_from_order(order, gray_sorted):
    """sorted (best-first) order の各位置に jp-counter 順位を割当てた配列を返す。

    jp_counter_rank と同値: 非灰色は累積非灰色数 (自身込)、灰色は直前 jp+1。
    cum = cumsum(非灰色); jp = cum + gray (灰色のみ +1)。order の元位置に散らして返す。
    """
    nongray = ~gray_sorted
    cum = np.cumsum(nongray)
    jp_sorted = cum + gray_sorted.astype(np.int64)
    out = np.empty(len(order), dtype=np.int64)
    out[order] = jp_sorted
    return out


def _pt_display_ranks(arrays, peak, measured, pk_arrays=None, stream=''):
    """カスケード arrays から **全選手** の表示順位 (jp-counter) を配列で出す。

    arrays = (uids0, pmax_arr, pens_arr, ptj_arr, gray0) = _pt_cascade_inc の 2 つ目の返り値。
    build._build_snapshots_from_v4_cascade と同一の jp_counter ロジック。灰色 (海外 / 計測中) の総合順位は
    所属 Lv の枠サイズでクランプ (以前は参加者ごとに python で行っていた処理の vectorized 版)。
    pk_arrays = peak のキーの並びの (uid, 値) の配列 (_PtCols.peak_arrays。あれば peak からは取り出さない)。
    Returns: (uids0, lv_arr, ens_jp, tj_jp, bt_uids, bt_jp)。bt 側は peak 集合 (ens/tjpr とは別の集合) 上。
    """
    uids0, pmax_arr, pens_arr, ptj_arr, gray0 = arrays
    _t = _pcs()
    # --- bt (peak 集合上) ---
    if pk_arrays is not None:
        pua, pva = pk_arrays; pn = len(pua); puids = None
    else:
        puids = list(peak.keys()); pn = len(puids)
    if pn:
        if pk_arrays is None:
            pua = np.fromiter(puids, dtype=np.int64, count=pn)
            if type(peak) is dict:   # 本番: values() は keys() と同じ順 = float(peak[u]) を keys の順に並べたもの
                pva = np.fromiter(peak.values(), dtype=np.float64, count=pn)
            else:                    # _PwSnap (区分線形の減衰実験) は values() を持たない
                pva = np.fromiter((float(peak[u]) for u in puids), dtype=np.float64, count=pn)
        if measured is not None:
            pgray = _gray_flags(pua, puids, measured)   # = (u in OVERSEAS_UIDS) or (u not in measured)
        else:
            pgray = np.fromiter((u in OVERSEAS_UIDS for u in (puids if puids is not None else pua.tolist())),
                                dtype=bool, count=pn)
        _SEC['d_bt_gray'] += _pcs() - _t; _t = _pcs()
        bt_order = _rank_desc_then_uid_inc('disp_bt' + stream, pua, pva, _uid_perm(pua))   # = np.lexsort((pua, -pva))
        _SEC['d_bt_ord'] += _pcs() - _t; _t = _pcs()
        bt_jp = _pt_jp_from_order(bt_order, pgray[bt_order])
        _SEC['d_bt_jp'] += _pcs() - _t; _t = _pcs()
    else:
        pua = np.zeros(0, dtype=np.int64); bt_jp = np.zeros(0, dtype=np.int64)
    # --- ens / tjpr 全国# ---
    perm_uid = _uid_perm(uids0)
    # = np.lexsort((uids0, pens_arr, -pmax_arr)): primary -lv, then ens_raw, then uid。(-lv, ens_raw) を整数キー 1 本にして
    # uid 昇順に並べた上で安定ソート (同値は uid 昇順 = lexsort と同じ)
    # pens = 所属 Lv での ens 順位 (Lv ごとに 1.. の順列) なので (Lv, pens) は全員違う → 並びは一意
    # (uid 順に並べてからの安定ソートと同じ)。並べ替えずに (Lv, pens) の場所の表で作る (_order_lv_desc_rank、2026-10-23)
    _SEC['d_perm'] += _pcs() - _t; _t = _pcs()
    ens_order = _order_lv_desc_rank(pmax_arr, pens_arr)
    _SEC['d_ens_ord'] += _pcs() - _t; _t = _pcs()
    ens_jp = _pt_jp_from_order(ens_order, gray0[ens_order])
    _SEC['d_ens_jp'] += _pcs() - _t; _t = _pcs()
    tj_order = _rank_desc_then_uid_inc('disp_tj' + stream, uids0, ptj_arr, perm_uid)   # = np.lexsort((uids0, -ptj_arr)): primary -score, then uid
    _SEC['d_tj_ord'] += _pcs() - _t; _t = _pcs()
    tj_jp = _pt_jp_from_order(tj_order, gray0[tj_order])
    _SEC['d_tj_jp'] += _pcs() - _t; _t = _pcs()
    # 灰色は所属 Lv 枠でクランプ (lv >= 2 のみ。cap の無い Lv はそのまま)
    cap_by_lv = np.zeros((int(pmax_arr.max()) + 2) if len(pmax_arr) else 1, dtype=np.int64)
    for lv_, cap_ in _PT_LV_CAP.items():
        if cap_ and lv_ < len(cap_by_lv):
            cap_by_lv[lv_] = cap_
    caps = cap_by_lv[pmax_arr]
    m = gray0 & (pmax_arr >= 2) & (caps > 0) & (ens_jp > caps)
    if m.any():
        ens_jp = ens_jp.copy(); ens_jp[m] = caps[m]
    _SEC['d_clamp'] += _pcs() - _t
    return uids0, pmax_arr, ens_jp, tj_jp, pua, bt_jp


def _positions(arr, keys) -> list:
    """arr (uid の配列、重複なし) の中での keys の位置 (無ければ None) のリスト。"""
    out = [None] * len(keys)
    if len(arr) == 0 or not keys:
        return out
    # uid 以外 (None など) は「無い」扱い (以前の dict 引きと同じ)
    ki = [j for j, k in enumerate(keys) if isinstance(k, (int, np.integer)) and not isinstance(k, bool)]
    if not ki:
        return out
    order = _uid_perm(arr)
    s = arr[order]
    q = np.asarray([keys[j] for j in ki], dtype=np.int64)
    i_c = np.minimum(np.searchsorted(s, q), len(s) - 1)
    hit = s[i_c] == q
    for j, pos, h in zip(ki, i_c.tolist(), hit.tolist()):
        if h:
            out[j] = int(order[pos])
    return out


def _pt_at_time_anchor(arrays, peak, measured, participants, ranks=None):
    """カスケード arrays から参加者の表示アンカー [lv, ens, tjpr_r, bt_r] (list) を算出。

    ranks = _pt_display_ranks(...) の戻り (無ければここで計算)。dict 往復を避けて cascade の array を
    そのまま使う (毎 serious 大会 ×2 回呼ばれる)。
    """
    if arrays is None:
        return {}
    if ranks is None:
        ranks = _pt_display_ranks(arrays, peak, measured)
    uids0, pmax_arr, ens_jp, tj_jp, pua, bt_jp = ranks
    # 参加者 (数十〜数百人) の位置を二分探索で引く (以前は全選手の dict を 2 つ作っていた。2026-09-28)。
    # 並び (participants の反復順) と値は同じ。
    plist = list(participants)
    eidx = _positions(uids0, plist)
    pidx = _positions(pua, plist)
    out = {}
    for u, ie, ib in zip(plist, eidx, pidx):
        if ie is None and ib is None:
            continue
        lv = int(pmax_arr[ie]) if ie is not None else 0
        ens_r = int(ens_jp[ie]) if ie is not None else None
        tjr = int(tj_jp[ie]) if ie is not None else None
        btr = int(bt_jp[ib]) if ib is not None else None
        # before アンカー = [lv, ens_r, tjpr_r, bt_r]。after (= 大会の結果確定直後の順位)
        # は呼び出し側が後から [.., ens_a, tjpr_a, bt_a] を append する (list で返す)。
        out[u] = [lv, ens_r, tjr, btr]
    return out

def learn_bt_lv_shared(prior, t_meta_by_id, M, target_ts_list, capture_timeline=False,
                        capture_per_tour=False, settings: Settings | None = None):
    """BT learning + cascade LV updates at each target boundary.

    capture_timeline=True: 追加で bt_timeline (= 各 player の per-tournament 更新履歴) を返す.
    capture_per_tour=True: bt_per_tour[(uid, t_eid)] = {'bt_used': bool, 'bt_d': float (ord delta)}
      を返す。 t_eid は build 側で event_id で引けるよう attr.json の eid を使う想定だが、
      ここでは id(t) を使い、postprocess 側で id(t)→event_id mapping を別途解決する。

    Returns: (bt_at_t, peak180_at_target, player_lv_at_target, cascade_at_target
              [, bt_timeline] [, bt_per_tour])
    """
    if settings is not None:
        apply_settings(settings)
    b = OpenSkillBTRater()
    tour_count = defaultdict(int)
    last_seen_ts: dict[int, int] = {}
    peak180 = defaultdict(Peak180Tracker)
    bt_at_t: dict[int, dict[int, float]] = {}
    player_lv: dict[int, int] = {}
    bt_timeline: dict[int, list] = defaultdict(list) if capture_timeline else None
    # bt_per_tour[(uid, event_id)] = {'bt_used': bool, 'bt_d': float (ord delta)}
    bt_per_tour: dict = {} if capture_per_tour else None
    # bt_per_match[(eid, winner_uid, loser_uid)] = [[d_w, d_l, tracked_w, tracked_l], ...]
    # (試合進行順の per-occurrence リスト: 同一イベントで同方向の対戦が複数ある場合に対応)
    # (note: 同一 event 内の同一 winner/loser ペアは稀 (= DE GF のみ). 後勝ち上書き.)
    bt_per_match: dict = {} if capture_per_tour else None

    target_sorted = sorted(target_ts_list) if target_ts_list else []
    target_idx = 0
    peak180_at_target: dict[int, dict[int, float]] = {}
    player_lv_at_target: dict[int, dict[int, int]] = {}
    cascade_at_target: dict[int, tuple] = {}

    def tournament_updates_player(uid, meta):
        if BT_GATING_MODE == 'mtour':
            return tour_count[uid] >= M
        if BT_GATING_MODE == 'hybrid':
            # warmup (tour_count < M): 全大会で更新 (= 平日込み)
            # warmup 後: TJPR-LV gated (= player の現 LV の tfilter)
            if tour_count[uid] < M:
                return True
            lv = player_lv.get(uid, 1)
            return lv_tournament_passes(lv, meta)
        lv = player_lv.get(uid, 1)
        return lv_tournament_passes(lv, meta)

    for t in prior:
        while target_idx < len(target_sorted) and t.timestamp >= target_sorted[target_idx]:
            ts = target_sorted[target_idx]
            peak_dict = {}
            for uid, tr in peak180.items():
                pv = tr.peak_at(ts)
                if pv == float('-inf'):
                    pv = b._get(uid).ordinal()
                peak_dict[uid] = pv
            peak180_at_target[ts] = peak_dict
            # Cascade with current BT state
            prior_for_cascade = [tt for tt in prior if tt.timestamp < ts]
            if prior_for_cascade:
                snap_date = dt.datetime.fromtimestamp(ts)
                cache = precompute_banzuke(prior_for_cascade, snap_date, bt_at_t, 0.0,
                                            t_meta_by_id, TJPR_DEFAULT_RATE)
                player_max_lv, player_ens_at_max_lv, player_tjpr_at_max_lv = run_shared_cascade(cache, peak_dict)
                cascade_at_target[ts] = (player_max_lv, player_ens_at_max_lv, player_tjpr_at_max_lv)
                # Replace LVs
                player_lv = dict(player_max_lv)
                del cache
                gc.collect()
            else:
                cascade_at_target[ts] = ({}, {}, {})
            player_lv_at_target[ts] = dict(player_lv)
            target_idx += 1

        meta = t_meta_by_id[id(t)]
        will_update_bt = not (meta.get('is_non_serious') or meta.get('is_gf_missing')
                              or meta.get('is_class_virtual') or meta.get('is_small'))
        participants = set()
        for m in t.matches:
            participants.add(m.winner_id); participants.add(m.loser_id)
        if will_update_bt:
            for uid in participants:
                _apply_inactivity_sigma(b, uid, last_seen_ts, t.timestamp)
        standings_uids = {uid for _, uid in t.standings}
        bt_at_t[id(t)] = {uid: b._get(uid).ordinal() for uid in standings_uids | participants}
        if not will_update_bt:
            continue
        is_wk_event = meta['is_weekend'] and not meta.get('is_lower_class', False)
        any_used = {uid: False for uid in participants}
        ordered_matches = sorted(t.matches, key=_bracket_order_key)
        # per-tour 用に各 player の ord を before/after で記録
        before_ord = {uid: b._get(uid).ordinal() for uid in participants} if bt_per_tour is not None else None
        eid = t.event_id if bt_per_tour is not None else None   # = attr.json の event_id (data_loader が読んだ値)
        for uid in participants:
            tr = peak180[uid]
            cutoff = t.timestamp - _common.BT_PEAK_WINDOW_SEC
            popped_peak = False
            while tr.history and tr.history[0][0] < cutoff:
                _, po = tr.history.popleft()
                if po >= tr.current_peak - 1e-12: popped_peak = True
            if popped_peak:
                tr.current_peak = max((o for _, o in tr.history), default=float('-inf'))
        # peak180 の before snapshot (= 大会開始時点の peak180)
        peak180_before = ({uid: (peak180[uid].current_peak if peak180[uid].current_peak != float('-inf') else b._get(uid).ordinal())
                          for uid in participants}
                          if bt_per_tour is not None else None)
        meta_cls = None  # class bracket 試合は下位クラス扱いで gate (g2_pure と同一規則)
        for m in ordered_matches:
            # LV-gating のみで判定 (M-gating は廃止 = LV システムに統合)
            m_meta = meta
            if _BT_CLASS_GATE and getattr(m, 'is_class', False):
                if meta_cls is None:
                    meta_cls = dict(meta); meta_cls['is_lower_class'] = True
                m_meta = meta_cls
            update_w = tournament_updates_player(m.winner_id, m_meta)
            update_l = tournament_updates_player(m.loser_id, m_meta)
            d_w = 0.0; d_l = 0.0
            if update_w or update_l:
                w_r = b._get(m.winner_id); l_r = b._get(m.loser_id)
                before_w = w_r.ordinal(); before_l = l_r.ordinal()
                new_ratings = b.model.rate([[w_r], [l_r]], ranks=[1, 2])
                if update_w:
                    nw = _apply_sigma_floor_keep_ordinal(b.model, new_ratings[0][0], SIGMA_FLOOR)
                    d_w = nw.ordinal() - before_w
                    b.ratings[m.winner_id] = nw; any_used[m.winner_id] = True
                if update_l:
                    nl = _apply_sigma_floor_keep_ordinal(b.model, new_ratings[1][0], SIGMA_FLOOR)
                    d_l = nl.ordinal() - before_l
                    b.ratings[m.loser_id] = nl; any_used[m.loser_id] = True
            if bt_per_match is not None and eid is not None:
                # 表示用 clamp (= production と同様、勝者は ≥ 0、敗者は ≤ 0)
                d_w_disp = max(0.0, d_w)
                d_l_disp = min(0.0, d_l)
                bt_per_match.setdefault((eid, m.winner_id, m.loser_id), []).append([
                    round(d_w_disp, 6), round(d_l_disp, 6),
                    bool(update_w), bool(update_l)
                ])
        for uid in participants:
            if any_used[uid]:
                r_obj = b._get(uid)
                peak180[uid].update(t.timestamp, r_obj.ordinal())
                if bt_timeline is not None:
                    bt_timeline[uid].append((t.timestamp, r_obj.mu, r_obj.sigma, r_obj.ordinal()))
            if bt_per_tour is not None and eid is not None:
                after_ord = b._get(uid).ordinal()
                peak_after = (peak180[uid].current_peak
                              if peak180[uid].current_peak != float('-inf') else after_ord)
                peak_d = peak_after - peak180_before[uid]
                bt_per_tour[(uid, eid)] = {
                    'bt_used': bool(any_used[uid]),
                    'bt_internal_d': round(after_ord - before_ord[uid], 6),
                    'bt_d': round(peak_d, 6),  # peak180 delta
                    'after': round(after_ord, 6),
                    'peak_after': round(peak_after, 6),
                }
            tour_count[uid] += 1
            last_seen_ts[uid] = t.timestamp

    while target_idx < len(target_sorted):
        ts = target_sorted[target_idx]
        peak_dict = {}
        for uid, tr in peak180.items():
            pv = tr.peak_at(ts)
            if pv == float('-inf'):
                pv = b._get(uid).ordinal()
            peak_dict[uid] = pv
        peak180_at_target[ts] = peak_dict
        prior_for_cascade = [tt for tt in prior if tt.timestamp < ts]
        if prior_for_cascade:
            snap_date = dt.datetime.fromtimestamp(ts)
            cache = precompute_banzuke(prior_for_cascade, snap_date, bt_at_t, 0.0,
                                        t_meta_by_id, TJPR_DEFAULT_RATE)
            player_max_lv, player_ens_at_max_lv, player_tjpr_at_max_lv = run_shared_cascade(cache, peak_dict)
            cascade_at_target[ts] = (player_max_lv, player_ens_at_max_lv, player_tjpr_at_max_lv)
            player_lv = dict(player_max_lv)
            del cache
            gc.collect()
        else:
            cascade_at_target[ts] = ({}, {}, {})
        player_lv_at_target[ts] = dict(player_lv)
        target_idx += 1

    extras = []
    if capture_timeline: extras.append(dict(bt_timeline))
    if capture_per_tour:
        extras.append(bt_per_tour)
        extras.append(bt_per_match)
    if extras:
        return (bt_at_t, peak180_at_target, player_lv_at_target, cascade_at_target, *extras)
    return bt_at_t, peak180_at_target, player_lv_at_target, cascade_at_target


# ───────── Glicko2 unified gating version (= V4 production) ─────────
# WARMUP_M=0 + LV gating (= "pure" V4) でも、env var で warmup_M を切り替え可能.
# 内部で OpenSkill ベースの learn_bt_lv_shared を呼んで LV を決定し、その LV で
# Glicko2 BT を学習する 2 段構成. 出力 shape は learn_bt_lv_shared と同じ.
from collections import deque as _deque
_G2_RD_INFL_TARGET = 128.0
_G2_RD_INFL_INFLECTION_DAYS = 540.0
_G2_RD_INFL_SCALE_DAYS = 180.0
_G2_RD_INFL_GRACE_DAYS = 30.0

# ── peak 線形減衰 (eval 用 hook, SPSP_BT_PEAK_DECAY_K 表示Elo点/月, 0=無効=従来) ──
# 直対評価 = 窓内 max(取得時レート - k*経過月数)。全 entry が同率で減衰するため
# argmax は時間不変で、score_i = r_i + k*t_i/月 に変換して従来の窓付き max を取れば
# 順序は厳密に一致する (peak 値は順序でしか消費されない)。増分キャッシュもそのまま有効。
# fallback (窓内 entry 無し → 現在レート) は評価時刻の score に変換して比較可能にする。
# ── class bracket 試合の BT gating (既定 ON) ──
# 親 event が通常大会でも、クラスブラケット (m.is_class) の試合は「下位クラス大会」
# として gate する (= Lv3+ は BT 更新なし、Lv1/2 は従来どおり = TJPR の class virtual
# 扱いと整合)。SPSP_BT_CLASS_GATE=0 で旧挙動 (実験の新旧比較用)。

# ── BT 更新の実験フック 3 種 (eval 用, いずれも既定 未設定 = 本番挙動不変) ──
# SPSP_BT_PAIR_GATE: レベル差による「片側だけ更新」の扱い。
#   'both' = 両者が gate を通る試合のみ両者更新 (片側のみ対象なら両方更新しない)
#   'any'  = どちらかが通れば両者とも更新 (片側更新を撤廃して常に対称)
#   未設定 = 現行 (u_w / u_l 独立判定)
# SPSP_BT_SIZE_K: 大会規模による BT 変動量スケール。
#   'sqrtlog' = 順位評価と同じ sqrt(log2(nent)) 比 / 'log2' = log2(nent) 比 /
#   'pow' = (log2(nent)/log2(REF)) ** SPSP_BT_SIZE_K_POW (より緩やかな一般形)。
#   基準規模 SPSP_BT_SIZE_K_REF (既定 64) で k=1。適用は更新後 rating を
#   before + k*(after-before) にブレンド (rd/vol の更新は従来どおり)。
# SPSP_BT_BO5_K: 3先セット (winner_score>=3) の変動量倍率 (2先=1 基準)。未設定/0 = 無効。
# SPSP_BT_BO1_K: 1先セット (winner_score==1) の変動量倍率 (2先=1 基準)。**本番 0.5 (2026-09-25〜、settings の既定)**。
#   0 も有効な値 (= 1先セットでは rating を動かさない。rd/vol の更新は従来どおり)。
#   1先は 2019〜2020 のスマバト週次ルーザーズ序盤・美らブラ予選・西武撃#4 予選など (exp/bt-bo1-k)。
# SPSP_BT_LV3_WEEKDAY_K: Lv3 プレイヤーの平日大会 (通常は weekend gate で不更新) を
#   この倍率の変動量で BT 更新する (0.5 = 半分)。weekend_only 以外の Lv3 条件
#   (min_nent_gt / exclude_capped) は満たす大会のみ。未設定/0 = 無効。g2_pure 経路のみ。
# SPSP_BT_ONESIDE_K: 片側更新の試合 (相手が gate 非対象) で、動く側の変動量に掛ける倍率。
#   未設定/0 = 無効 (従来どおり全量動く)。0.5 = 半分。
# SPSP_BT_LVGAP_K: レベル差が SPSP_BT_LVGAP_MIN (既定 2) 以上の試合の変動量倍率。
#   SPSP_BT_LVGAP_SCOPE='gain' (既定) = 勝者側 (増加量) のみ / 'both' = 両側。
#   未設定/0 = 無効。レベルは player_lv (per-tour LV 時は毎大会更新) を参照。
# SPSP_BT_GAMECOUNT_LV: レベルが N 以下のプレイヤーが「自分より上のレベル」と当たる試合で、
#   **そのプレイヤー側だけ** をセットカウント (ゲーム勝敗) で更新する。
#   例: Lv1 が Lv3 に 1-2 で負け → Lv1 側は「1 勝 2 敗」を 1 rating period として更新
#   (= update_player に 3 試合分渡す)。相手 (格上) 側は従来どおり 1 セット = 1 敗/1 勝。
#   狙い: 下位帯は試合数が少なく信号が疎なので、格上戦のゲーム内容まで拾って密にする。
#   格上相手に善戦 (1-2) ならレートが上がりうる = セット勝敗だけより解像度が高い。
#   未設定/0 = 無効。レベルは player_lv (per-tour LV 時は毎大会更新) を参照。
# スコアの健全性ガード。start.gg は DQ / 未入力を -1 や None で返すため、
# ここを通らない試合は従来どおり 1 セット 1 更新にフォールバックする
# (= 実験アームでも「スコア不明な試合は現行と同じ」)。
# SPSP_BT_GAMECOUNT_MODE: 'always' (既定) = 対象試合は常にゲームカウントで更新 /
#   'max' = 通常のセット更新とゲームカウント更新の**両方を試し、レート変動が
#   符号込みでよりプラスな方**を採る (= 下位側に有利な方を選ぶ)。
#   ゲームカウントは「格上に善戦した」を拾える反面、0-2 で負けた試合では
#   1 セット負けより大きく下がる。'max' は前者だけを採り後者を採らない。
# SPSP_BT_GAMECOUNT_SCOPE: 'up' (既定) = 相手が自分より上のレベルのときだけ /
#   'all' = 相手が誰でも (同レベル・格下も含む)。いずれも対象は Lv<=N 側のみで、
#   相手側の更新は常に従来どおり 1 セット 1 更新。
_GC_STATS = {'sets': 0, 'gc_sets': 0, 'gc_games': 0, 'no_score': 0,
             'max_set': 0, 'max_gc': 0}


def _g2_state(p):
    """Glicko-2 Player の全状態 (rating/rd/vol)。private 属性を直接読んで丸め損失を避ける。"""
    return (p._Player__rating, p._Player__rd, p.vol)


def _g2_restore(p, st):
    p._Player__rating, p._Player__rd, p.vol = st


def _gc_update(p, opp_r, opp_rd, gw, gl):
    """gw 勝 gl 敗を 1 rating period として更新する。"""
    n = gw + gl
    p.update_player([opp_r] * n, [opp_rd] * n, [1] * gw + [0] * gl)


def _gc_apply(p, opp_r, opp_rd, gw, gl, set_outcome):
    """対象プレイヤー側の更新。MODE に応じてセット更新かゲームカウント更新を適用。

    'max' では両方を試し、レートが符号込みでより大きくなる方の状態を残す
    (rd/vol も採用した側のものを残す)。
    """
    if _BT_GC_MODE != 'max':
        _gc_update(p, opp_r, opp_rd, gw, gl)
        _GC_STATS['gc_games'] += gw + gl
        return
    st0 = _g2_state(p)
    p.update_player([opp_r], [opp_rd], [set_outcome])
    st_set = _g2_state(p)
    _g2_restore(p, st0)
    _gc_update(p, opp_r, opp_rd, gw, gl)
    if st_set[0] > _g2_state(p)[0]:
        _g2_restore(p, st_set)
        _GC_STATS['max_set'] += 1
    else:
        _GC_STATS['max_gc'] += 1
        _GC_STATS['gc_games'] += gw + gl


def _gc_games(m):
    """セットカウント更新に使う (勝者ゲーム数, 敗者ゲーム数)。使えなければ None。

    winner_score/loser_score は start.gg 由来で、DQ は -1、未入力は None。
    3先で 3-2 の 5 が最大なので、それを超える値は異常として捨てる。
    """
    ws = getattr(m, 'winner_score', None)
    ls = getattr(m, 'loser_score', None)
    if ws is None or ls is None:
        return None
    try:
        ws = int(ws); ls = int(ls)
    except (TypeError, ValueError):
        return None
    if ws <= 0 or ls < 0 or ws <= ls or ws + ls > _BT_GC_MAX_GAMES:
        return None
    return ws, ls

# ── glicko2 ライブラリの _f バグ修正トグル (eval 用 hook, 既定 OFF = バグ込み) ──
# 本家 glicko2==2.1.0 の _f (volatility 反復の目的関数) は Glicko-2 仕様の φ²
# (self.__rd**2) の箇所に self.__rating**2 を使っている。本番は一貫してバグ込みで
# 学習しているため既定は従来どおり。SPSP_G2_FFIX=1 で仕様準拠版に差し替えて
# 精度影響を比較する (実験用)。
def _g2_player_cls():
    from glicko2 import Player
    if not _S.g2_ffix:
        return Player

    class _PlayerFFixed(Player):
        def _f(self, x, delta, v, a):
            ex = math.exp(x)
            phi2 = self._Player__rd ** 2
            num1 = ex * (delta ** 2 - phi2 - v - ex)
            denom1 = 2 * ((phi2 + v + ex) ** 2)
            return (num1 / denom1) - ((x - a) / (self._tau ** 2))

    return _PlayerFFixed

_PEAK_DECAY_T0 = 1514764800  # 2018-01-01 (数値スケール縮小用の基準点)
_MONTH_SEC = 30.4375 * 86400.0

# ── peak の減衰形状 (eval 用 hook) ──
# SPSP_BT_PEAK_DECAY_MODE:
#   'lin'    (既定) 減算線形     score(T) = max_t [ r_t - k*(T-t) ]
#   'exp'            乗算指数     score(T) = max_t [ exp(-λ*(T-t)) * r_t ]
#   'expint'         乗算指数(内部値のみ) score(T) = 1500 + max_t [ exp(-λ*(T-t)) * (r_t-1500) ]
# k は Elo点/月、λ は 1/月。
#
# どれも「経過時間ぶんの減価」を **全 entry・全プレイヤーに共通の変換** で表せる形に
# 限っている。peak は順序でしか消費されない (run_shared_cascade / eval の BT rank は
# ソートキーとしてしか使わない) ので、評価時刻 T に依存する共通項を落として
#   lin    : r_t + k*t            (max は T 不変)
#   exp    : log(r_t) + λ*t       (max は T 不変。真値は exp(-λT)*exp(これ))
#   expint : exp(λ*t) * (r_t-1500)(max は T 不変。真値は 1500 + exp(-λT)*これ)
# を保存すれば、従来どおり O(1) の増分 peak キャッシュがそのまま使える。
# ※ exp(-λT) > 0 は全プレイヤー共通の正の係数なので、掛けても順序は変わらない。
#   expint は内部値が負 (レート<1500) でも max(c*x)=c*max(x) (c>0) が成り立つので同じ。
# 区分線形など「経過時間で減衰率が変わる」形はこの分離ができず、別実装が要る。


# ── 区分線形減衰 (経過が古いほど減衰が速い) ──
# SPSP_BT_PEAK_PW="3:3,6:6,:12"
#   = 0-3ヶ月は 3 Elo/月、3-6ヶ月は 6 Elo/月、6ヶ月以降は 12 Elo/月 で減衰。
#   各要素は "区間の終わり(月):その区間の傾き"。最後の要素は終わりを空にする (= ∞)。
# score(T) = max_t [ r_t - g(T-t) ]、g は上の傾きを積分した非減少・区分線形関数。
#
# 上の lin/exp と違い「評価時刻に依存しない値」に潰せない (減衰率が経過時間で変わる
# ため、全 entry 共通の変換にならない)。そのため peak を 1 スカラーでキャッシュする
# 既存経路は使えず、全プレイヤーぶんを毎大会 numpy でまとめて引き直す (_PwStore)。


def _parse_pw(spec):
    """"3:3,6:6,:12" -> (edges, slopes, g_at_edge, seg_start)"""
    edges, slopes = [], []
    for part in spec.split(','):
        end, _, sl = part.partition(':')
        end = end.strip()
        edges.append(float(end) if end else float('inf'))
        slopes.append(float(sl))
    if not edges or edges[-1] != float('inf'):
        raise ValueError(f'SPSP_BT_PEAK_PW: 最後の区間は終わりを空にすること (got {spec!r})')
    starts = [0.0] + edges[:-1]
    g0, acc = [], 0.0
    for i, s in enumerate(starts):
        g0.append(acc)
        if i + 1 < len(starts):
            acc += slopes[i] * (edges[i] - s)
    return starts, slopes, g0




def _pw_g(age):
    """区分線形の累積減衰 g(age)。age は月単位の ndarray / float。

    「各区間の傾き × その区間に入っている経過分」の総和として書くと、
    分岐なしで vectorize できる (clip の和)。
    """
    a = np.asarray(age, dtype=np.float64)
    out = np.zeros(a.shape, dtype=np.float64)
    for s0, s1, sl in zip(_PW_STARTS, _PW_ENDS_A, _PW_SLOPES):
        out += sl * (np.clip(a, s0, s1) - s0)
    return out


def _peak_score(r, ts):
    if _PEAK_PW:
        # pw では減衰を _PwStore が持つので、ここは素のレートをそのまま渡す。
        return r
    if _PEAK_DECAY_MODE == 'exp':
        # log を取るのは順序を保つ単調変換 + オーバーフロー回避のため。
        return math.log(r if r > 1e-6 else 1e-6) + _PEAK_DECAY_LAMBDA * (ts - _PEAK_DECAY_T0) / _MONTH_SEC
    if _PEAK_DECAY_MODE == 'expint':
        return (math.exp(_PEAK_DECAY_LAMBDA * (ts - _PEAK_DECAY_T0) / _MONTH_SEC)
                * (r - _PEAK_DECAY_BASE))
    if _PEAK_DECAY_K == 0.0:
        return r
    return r + _PEAK_DECAY_K * (ts - _PEAK_DECAY_T0) / _MONTH_SEC


class _PwSnap:
    """_PwStore.snapshot の返り値。dict の代わりに (uid->row, scores) で持つ。

    dict を毎大会 4 万件組み直すと無視できない時間になるので、
    _pt_cascade_inc からは gather() で numpy のまま引く。
    """
    __slots__ = ('idx', 'scores', 'extra')

    def __init__(self, idx, scores, extra=None):
        self.idx = idx
        self.scores = scores
        # extra = BT 更新が一度も無い参加者の fallback (= 現レート/ELO)。
        # 非 pw 経路の _pt_peak_cur / take_snap の fallback と同じ役割。
        self.extra = extra if extra is not None else {}

    def get(self, uid, default=None):
        i = self.idx.get(uid)
        if i is None:
            return self.extra.get(uid, default)
        return float(self.scores[i])

    def __contains__(self, uid):
        return uid in self.idx or uid in self.extra

    def __getitem__(self, uid):
        i = self.idx.get(uid)
        return self.extra[uid] if i is None else float(self.scores[i])

    def keys(self):
        return self.idx.keys() | self.extra.keys()

    def gather(self, uids):
        """uids (ndarray) に対応するスコア配列。未登録は extra → -inf。"""
        idx = self.idx
        rows = np.fromiter((idx.get(int(u), -1) for u in uids),
                           dtype=np.int64, count=len(uids))
        ok = rows >= 0
        out = np.full(len(uids), float('-inf'), dtype=np.float64)
        if ok.any():
            out[ok] = self.scores[rows[ok]]
        if self.extra:
            ex = self.extra
            miss = np.flatnonzero(~ok)
            for k in miss.tolist():
                v = ex.get(int(uids[k]))
                if v is not None:
                    out[k] = v
        return out


class _PwStore:
    """区分線形減衰の peak を全プレイヤーぶんまとめて持つ。

    g が非減少なので「より新しくてレートが高い entry」は「より古くて低い entry」を
    恒久的に支配する (どの評価時刻でも勝つ)。支配される側を捨てると、各プレイヤーの
    履歴は「t 昇順 = r 降順」の短い frontier になる (実データでは数件)。
    frontier を (プレイヤー行 × 列) の numpy 行列で持ち、スナップショットは
    max(fr - g(T - ft)) を一括計算する。
    """

    def __init__(self, cols=8, chunk=4096):
        self.idx = {}
        self.n = 0
        self.cols = cols
        self.chunk = chunk
        self.ft = np.zeros((0, cols), dtype=np.float64)
        self.fr = np.full((0, cols), float('-inf'), dtype=np.float64)
        self.fn = np.zeros(0, dtype=np.int64)

    def _row(self, uid):
        i = self.idx.get(uid)
        if i is not None:
            return i
        if self.n == len(self.fn):
            add = self.chunk
            self.ft = np.vstack([self.ft, np.zeros((add, self.cols))])
            self.fr = np.vstack([self.fr, np.full((add, self.cols), float('-inf'))])
            self.fn = np.concatenate([self.fn, np.zeros(add, dtype=np.int64)])
        i = self.n
        self.idx[uid] = i
        self.n += 1
        return i

    def _grow_cols(self):
        add = self.cols
        self.ft = np.hstack([self.ft, np.zeros((len(self.fn), add))])
        self.fr = np.hstack([self.fr, np.full((len(self.fn), add), float('-inf'))])
        self.cols += add

    def update(self, uid, ts, r):
        i = self._row(uid)
        t = (ts - _PEAK_DECAY_T0) / _MONTH_SEC
        k = int(self.fn[i])
        # 支配される (= r 以下の) 末尾を落とす。frontier は r 狭義降順。
        while k > 0 and self.fr[i, k - 1] <= r:
            k -= 1
        if k == self.cols:
            self._grow_cols()
        self.ft[i, k] = t
        self.fr[i, k] = r
        # 落とした分の残骸が max に混ざらないよう -inf で潰す
        if k + 1 < self.cols:
            self.fr[i, k + 1:] = float('-inf')
        self.fn[i] = k + 1

    def peak_at(self, uid, ts):
        i = self.idx.get(uid)
        if i is None:
            return float('-inf')
        k = int(self.fn[i])
        if k == 0:
            return float('-inf')
        t = (ts - _PEAK_DECAY_T0) / _MONTH_SEC
        return float((self.fr[i, :k] - _pw_g(t - self.ft[i, :k])).max())

    def snapshot(self, ts):
        if self.n == 0:
            return _PwSnap({}, np.zeros(0))
        t = (ts - _PEAK_DECAY_T0) / _MONTH_SEC
        # 実際に使われている列までで打ち切る (確保済みの列は大半が空)。
        c = int(self.fn[:self.n].max())
        if c == 0:
            return _PwSnap(self.idx, np.full(self.n, float('-inf')))
        ft = self.ft[:self.n, :c]
        fr = self.fr[:self.n, :c]
        sc = fr - _pw_g(t - ft)     # 未使用列は fr=-inf なので自動的に負けになる
        return _PwSnap(self.idx, sc.max(axis=1))


class _PwHandle:
    """peak180[uid] が返す、_PwStore への薄いハンドル (_G2Peak と同じ呼び口)。"""
    __slots__ = ('store', 'uid')

    def __init__(self, store, uid):
        self.store = store
        self.uid = uid

    def update(self, ts, r):
        self.store.update(self.uid, ts, r)

    def peak_at(self, ts):
        return self.store.peak_at(self.uid, ts)


class _PwPeakMap(dict):
    """defaultdict(_G2Peak) の代わり。uid を知る必要があるので __missing__ で作る。"""

    def __init__(self, store):
        super().__init__()
        self.store = store

    def __missing__(self, uid):
        h = _PwHandle(self.store, uid)
        self[uid] = h
        return h


# 窓がデータ全期間より長い = 「全期間の最高レート」。窓から外れる entry が
# 存在しないので、expiry 追跡も peak_at の線形走査も不要になる
# (SPSP_BT_PEAK_DAYS=36500 等で全期間 peak を試す実験用)。
# ── ここまでで apply_settings が参照するヘルパ (_parse_pw 等) が揃うので、import 時の設定を適用する ──
# import 時は本番既定値 (Settings()) を適用する。環境変数は読まない (実験ハーネスは v3/eval_bt_lv_shared.py の shim が
# Settings.from_env() を適用する。build_v4_full.py も明示的に apply_settings(Settings()) を呼ぶ)。
apply_settings(Settings())


class _G2Peak:
    __slots__ = ('history', 'current_peak')
    def __init__(self):
        self.history = _deque()
        self.current_peak = float('-inf')

    def update(self, ts, r):
        cutoff = ts - _common.BT_PEAK_WINDOW_SEC
        popped = False
        while self.history and self.history[0][0] < cutoff:
            _, old = self.history.popleft()
            if old >= self.current_peak - 1e-12:
                popped = True
        if popped:
            self.current_peak = max((x for _, x in self.history), default=float('-inf'))
        self.history.append((ts, r))
        if r > self.current_peak:
            self.current_peak = r

    def peak_at(self, ts):
        # 何も窓落ちしないなら current_peak がそのまま答え (= history 全体の max)。
        # 全期間 peak では毎回これが成立し、線形走査を避けられる。
        if self.history and self.history[0][0] >= ts - _common.BT_PEAK_WINDOW_SEC:
            return self.current_peak
        cutoff = ts - _common.BT_PEAK_WINDOW_SEC
        return max((r for ts_, r in self.history if ts_ >= cutoff), default=float('-inf'))


def _g2_inactivity_rd_target(days):
    if days <= _G2_RD_INFL_GRACE_DAYS:
        return 0.0
    x = (days - _G2_RD_INFL_INFLECTION_DAYS) / _G2_RD_INFL_SCALE_DAYS
    s = 1.0 / (1.0 + math.exp(-x))
    return _G2_RD_INFL_TARGET * s


def learn_bt_lv_shared_g2(prior, t_meta_by_id, M, target_ts_list,
                           capture_timeline=False, capture_per_tour=False):
    """V4 production: Glicko2 BT + ens-LV (or TJPR-LV) gating + warmup_M=WARMUP_M.

    Step 1: OpenSkill learn_bt_lv_shared で player_lv_at_target を取得
            (= PFILTER_MODE / LV3_WEEKEND / LV4_NENT_GT を env var で制御)
    Step 2: Glicko2 BT 学習 (= player の current LV の tfilter で gate、
            warmup_M < M なら最初の M 大会は permissive)
    出力 shape は learn_bt_lv_shared と同じ.
    """
    _G2Player = _g2_player_cls()
    print(f"  [g2] Step 1: OpenSkill cascade for LV determination ...", flush=True)
    t0 = time.time()
    _, _, player_lv_at_target, cascade_at_target = learn_bt_lv_shared(
        prior, t_meta_by_id, M, target_ts_list,
        capture_timeline=False, capture_per_tour=False)
    print(f"  [g2] Step 1 done ({time.time()-t0:.0f}s)", flush=True)
    print(f"  [g2] Step 2: Glicko2 BT learning (warmup_M={WARMUP_M}) ...", flush=True)
    t0 = time.time()

    ratings = {}
    tour_count = defaultdict(int)
    _pw_store = _PwStore() if _PEAK_PW else None
    peak180 = _PwPeakMap(_pw_store) if _PEAK_PW else defaultdict(_G2Peak)
    last_seen_ts = {}
    target_sorted = sorted(target_ts_list) if target_ts_list else []
    bt_at_t = {}
    peak180_at_target = {}
    bt_timeline = defaultdict(list) if capture_timeline else None
    bt_per_tour = {} if capture_per_tour else None
    bt_per_match = {} if capture_per_tour else None

    def _get(uid):
        if uid not in ratings:
            ratings[uid] = _G2Player(rating=1500.0, rd=350.0, vol=0.06)
        return ratings[uid]

    def _apply_inactivity(uid, current_ts):
        last = last_seen_ts.get(uid)
        if last is None:
            return
        days = max(0.0, (current_ts - last) / 86400.0)
        if days <= _G2_RD_INFL_GRACE_DAYS:
            return
        target = _g2_inactivity_rd_target(days)
        if target <= 0:
            return
        r = ratings.get(uid)
        if r is not None and r.rd < target:
            r.rd = target

    current_player_lv = {}
    target_idx = 0

    def take_snap(ts):
        if _PEAK_PW:
            # peak180 のキー集合は非 pw と同じ (prune ループが entry を作る)。
            # store に行がある = BT 更新済、無い = fallback (現レート)。
            _sn = _pw_store.snapshot(ts)
            _idx, _sc = _sn.idx, _sn.scores
            snap = {}
            for uid in peak180:
                i = _idx.get(uid)
                pv = float(_sc[i]) if i is not None else _peak_score(_get(uid).rating, ts)
                snap[uid] = pv / ELO_PER_UNIT
            return snap
        snap = {}
        for uid, tr in peak180.items():
            pv = tr.peak_at(ts)
            if pv == float('-inf'):
                pv = _peak_score(_get(uid).rating, ts)
            snap[uid] = pv / ELO_PER_UNIT
        return snap

    for t in prior:
        while target_idx < len(target_sorted) and t.timestamp >= target_sorted[target_idx]:
            ts = target_sorted[target_idx]
            peak180_at_target[ts] = take_snap(ts)
            current_player_lv = dict(player_lv_at_target.get(ts, {}))
            target_idx += 1

        meta = t_meta_by_id[id(t)]
        will_update = not (meta.get('is_non_serious') or meta.get('is_gf_missing')
                            or meta.get('is_class_virtual') or meta.get('is_small'))
        participants = set()
        for m in t.matches:
            participants.add(m.winner_id)
            participants.add(m.loser_id)
        standings_uids = {uid for _, uid in t.standings}
        bt_at_t[id(t)] = {uid: _get(uid).rating / ELO_PER_UNIT
                          for uid in standings_uids | participants}

        if not will_update:
            continue

        for uid in participants:
            _get(uid)
            _apply_inactivity(uid, t.timestamp)

        # peak180 prune (= cutoff の古い entry を捨てる)
        for uid in participants:
            tr = peak180[uid]
            cutoff = t.timestamp - _common.BT_PEAK_WINDOW_SEC
            popped = False
            while tr.history and tr.history[0][0] < cutoff:
                _, old = tr.history.popleft()
                if old >= tr.current_peak - 1e-12:
                    popped = True
            if popped:
                tr.current_peak = max((x for _, x in tr.history), default=float('-inf'))

        def updates(uid, m_meta=None):
            if WARMUP_M > 0 and tour_count[uid] < WARMUP_M:
                return True
            lv = current_player_lv.get(uid, 1)
            return lv_tournament_passes(lv, m_meta if m_meta is not None else meta)

        # per-tour 用 before snapshot
        before_ord = ({uid: _get(uid).rating / ELO_PER_UNIT for uid in participants}
                      if bt_per_tour is not None else None)
        peak180_before = ({uid: (peak180[uid].current_peak / ELO_PER_UNIT
                                 if peak180[uid].current_peak != float('-inf')
                                 else _get(uid).rating / ELO_PER_UNIT)
                          for uid in participants}
                          if bt_per_tour is not None else None)
        eid = t.event_id if bt_per_tour is not None else None   # = attr.json の event_id (data_loader が読んだ値)

        any_used = {uid: False for uid in participants}
        meta_cls = None  # class bracket 試合は下位クラス扱いで gate (g2_pure と同一規則)
        for m in sorted(t.matches, key=_bracket_order_key):
            m_meta = meta
            if _BT_CLASS_GATE and getattr(m, 'is_class', False):
                if meta_cls is None:
                    meta_cls = dict(meta); meta_cls['is_lower_class'] = True
                m_meta = meta_cls
            u_w = updates(m.winner_id, m_meta)
            u_l = updates(m.loser_id, m_meta)
            if not (u_w or u_l):
                # 両方 untracked: rate 変動なしだが postprocess に v2 prod 値を残させない
                # ため bt_per_match に zero entry を記録 (= 上書き用)
                if bt_per_match is not None and eid is not None:
                    bt_per_match.setdefault((eid, m.winner_id, m.loser_id), []).append([0.0, 0.0, False, False])
                continue
            w = _get(m.winner_id)
            l = _get(m.loser_id)
            before_w = w.rating / ELO_PER_UNIT
            before_l = l.rating / ELO_PER_UNIT
            wr, wrd, lr, lrd = w.rating, w.rd, l.rating, l.rd
            if u_w:
                w.update_player([lr], [lrd], [1])
                any_used[m.winner_id] = True
            if u_l:
                l.update_player([wr], [wrd], [0])
                any_used[m.loser_id] = True
            if bt_per_match is not None and eid is not None:
                d_w = (w.rating / ELO_PER_UNIT - before_w) if u_w else 0.0
                d_l = (l.rating / ELO_PER_UNIT - before_l) if u_l else 0.0
                d_w_disp = max(0.0, d_w)
                d_l_disp = min(0.0, d_l)
                bt_per_match.setdefault((eid, m.winner_id, m.loser_id), []).append([
                    round(d_w_disp, 6), round(d_l_disp, 6),
                    bool(u_w), bool(u_l)
                ])

        for uid in participants:
            r = _get(uid)
            if any_used[uid]:
                peak180[uid].update(t.timestamp, _peak_score(r.rating, t.timestamp))
                if bt_timeline is not None:
                    bt_timeline[uid].append((t.timestamp, r.rating, r.rd, r.rating / ELO_PER_UNIT))
            if bt_per_tour is not None and eid is not None:
                after_ord = r.rating / ELO_PER_UNIT
                peak_after = (peak180[uid].current_peak / ELO_PER_UNIT
                              if peak180[uid].current_peak != float('-inf') else after_ord)
                peak_d = peak_after - peak180_before[uid]
                bt_per_tour[(uid, eid)] = {
                    'bt_used': bool(any_used[uid]),
                    'bt_internal_d': round(after_ord - before_ord[uid], 6),
                    'bt_d': round(peak_d, 6),
                    'after': round(after_ord, 6),
                    'peak_after': round(peak_after, 6),
                }
            last_seen_ts[uid] = t.timestamp
            tour_count[uid] += 1

    while target_idx < len(target_sorted):
        ts = target_sorted[target_idx]
        peak180_at_target[ts] = take_snap(ts)
        target_idx += 1

    print(f"  [g2] Step 2 done ({time.time()-t0:.0f}s)", flush=True)

    extras = []
    if capture_timeline:
        extras.append(dict(bt_timeline))
    if capture_per_tour:
        extras.append(bt_per_tour)
        extras.append(bt_per_match)
    if extras:
        return (bt_at_t, peak180_at_target, player_lv_at_target, cascade_at_target, *extras)
    return bt_at_t, peak180_at_target, player_lv_at_target, cascade_at_target


# ───────── Glicko2 single-pass version (cascade inline, no OpenSkill bootstrap) ─────────
def g2_learn_advance(state: G2LearnState, prior, t_meta_by_id, M, until_ts: int | None = None,
                     verify_ledger: bool = True) -> int:
    """学習状態 state を、prior (timestamp 昇順) のうち未処理の大会 [state.cursor_ts, until_ts) ぶん進める。

    Single-pass Glicko2 + per-tour cascade。各大会開始時のレベルで BT スナップ → LV-gated Glicko2 更新。
    serious 大会の直前に増分カスケードで player_lv を更新 (per_tour_lv)。
    SNAPSHOT_DAYS 時点のカスケードはここでは計算しない (学習後に g2_snapshots が状態から復元する) ので、
    学習の軌跡は eval_date にもスナップショット日にも依存しない = 途中で保存して再開しても同じ結果になる。

    再開時は state.cursor_ts より前の大会が台帳と一致することを先に確かめる (StaleCheckpoint)。
    verify_ledger=False はその照合を飛ばす: state がこの実行の中で同じデータから作ったものだと分かっているとき
    だけ使う (北米は区切りごとに別プロセスで、毎回 15 万大会の照合に数分かかっていた。照合は一致すれば何も
    変えないので、データが同じなら結果は同じ)。
    bt_at_t と peak180 は OpenSkill 互換 ord スケール (= rating / ELO_PER_UNIT) で保持。
    戻り値: この呼び出しで処理した大会数。
    """
    # 適用中の設定 (_S) が学習状態の設定と違えば止める。ビルド側の Settings と学習器の _S が
    # 別経路で書き換わる事故 (2026-09-07: unpickle 経由の shim import) を黙って通さないため。
    if _S is None or _S.state_repr() != state.settings_repr:
        raise RuntimeError("学習器に適用中の設定が学習状態と違う (apply_settings が別経路で呼ばれた可能性)\n"
                           f"  適用中: {None if _S is None else _S.state_repr()}\n  学習状態: {state.settings_repr}")

    st = state
    if st.cursor_ts and verify_ledger:
        check_ledger(st, prior, t_meta_by_id)
    _G2Player = _g2_player_cls()

    ratings = st.ratings
    tour_count = st.tour_count
    last_seen_ts = st.last_seen_ts
    if st.peak180 is None:
        st.peak180 = defaultdict(_G2Peak)
    if _PEAK_PW:
        if st.pw_store is None:
            st.pw_store = _PwStore()
        _pw_store = st.pw_store
        peak180 = _PwPeakMap(_pw_store)
    else:
        _pw_store = None
        peak180 = st.peak180
    bt_at_t = st.bt_at_t_view(prior)        # id(t) キーの見え方 (precompute_banzuke 用)。本体は st.bt_at_t_by_path
    player_lv = st.player_lv
    bt_timeline = st.bt_timeline
    st.upgrade_logs()   # 旧形式 (dict) のチェックポイントから再開したときは配列の表にする (spsp/columnar.py、値は同じ)
    bt_per_tour = st.bt_per_tour
    bt_per_match = st.bt_per_match
    rating_hist = st.rating_hist
    first_part_ts = st.first_part_ts
    part_order = st.part_order

    def _get(uid):
        if uid not in ratings:
            ratings[uid] = _G2Player(rating=1500.0, rd=350.0, vol=0.06)
        return ratings[uid]

    def _apply_inactivity(uid, current_ts):
        last = last_seen_ts.get(uid)
        if last is None:
            return
        days = max(0.0, (current_ts - last) / 86400.0)
        if days <= _G2_RD_INFL_GRACE_DAYS:
            return
        target = _g2_inactivity_rd_target(days)
        if target <= 0:
            return
        r = ratings.get(uid)
        if r is not None and r.rd < target:
            r.rd = target

    _DBG_UID = int(os.environ.get('SPSP_DEBUG_UID', '0') or 0)   # 調査用: この uid の gating 判定を毎大会 print

    def tournament_updates_player(uid, meta):
        if BT_GATING_MODE == 'mtour':
            return tour_count[uid] >= M
        if BT_GATING_MODE == 'hybrid':
            if tour_count[uid] < M:
                return True
            lv = player_lv.get(uid, 1)
            return lv_tournament_passes(lv, meta)
        lv = player_lv.get(uid, 1)
        return lv_tournament_passes(lv, meta)

    def take_snap(ts):
        if _PEAK_PW:
            # peak180 のキー集合は非 pw と同じ (prune ループが entry を作る)。
            # store に行がある = BT 更新済、無い = fallback (現レート)。
            _sn = _pw_store.snapshot(ts)
            _idx, _sc = _sn.idx, _sn.scores
            snap = {}
            for uid in peak180:
                i = _idx.get(uid)
                pv = float(_sc[i]) if i is not None else _peak_score(_get(uid).rating, ts)
                snap[uid] = pv / ELO_PER_UNIT
            return snap
        snap = {}
        for uid, tr in peak180.items():
            pv = tr.peak_at(ts)
            if pv == float('-inf'):
                pv = _peak_score(_get(uid).rating, ts)
            snap[uid] = pv / ELO_PER_UNIT
        return snap


    # ── per-tournament レベル判定 (Settings.per_tour_lv, 本番 on) ──
    # serious 大会の直前に player_lv を増分 cascade で更新 → BT gating が毎大会粒度に。
    _per_tour_lv = _S.per_tour_lv
    # 大会ページ用: 各 serious 大会直前の厳密アンカー (per-tour 時のみ)。event_path キー (状態に保存)
    _pt_at_time = st.pt_at_time_by_path if (st.capture_at_time and _per_tour_lv) else None
    # skip_per_match: bt_per_match (= 全試合の per-match delta、最大の保持構造) の捕捉をスキップして
    # ピークメモリを削減 (軽量ビルド用)。bt_per_match は空 {} のまま返るので downstream の .items() は安全。
    _pm_on = (bt_per_match is not None) and not _S.skip_per_match
    if _per_tour_lv:
        _pt_parent_by_eid = {}
        for _tt in prior:
            _mm = t_meta_by_id[id(_tt)]
            if not _mm.get('is_class_virtual') and _mm.get('event_id') is not None:
                _pt_parent_by_eid[_mm['event_id']] = _tt  # last-wins (= precompute_banzuke と一致)
        # 下位クラス (仮想大会) を持つ本戦の event_id → 最後に処理するクラスの event_path。下位クラスは本戦の
        # 1〜4 秒後に並ぶので、本戦の「結果確定直後」の順位は最後のクラスを処理してから引く (クラスの点を入れる)
        _pt_last_cv = {}
        for _tt in prior:
            _mm = t_meta_by_id[id(_tt)]
            if _mm.get('is_class_virtual') and _mm.get('parent_event_id') is not None:
                _pt_last_cv[_mm['parent_event_id']] = rel_event_path(_tt.event_path)
        if st.pt_scores_entries is None:
            st.pt_scores_entries = {lv: defaultdict(list) for lv, _, _ in _PT_LV_AGG}
            st.pt_scores = {lv: {} for lv, _, _ in _PT_LV_AGG}
        _pt_scores_entries = st.pt_scores_entries; _pt_scores = st.pt_scores
        _pt_mcounts = st.pt_mcounts; _pt_mwindow = st.pt_mwindow  # (ts, uid) measured 2y 窓
        _pt_meas_set = st.pt_meas_set          # 計測済み uid (増分維持、全選手再構築を回避)
        _pt_lo_win = PROV_WINDOW_DAYS * 86400
        # pt_scores / peak の配列版 (_PtCols)。pw (区分線形の減衰実験) は peak を毎回引き直すので使わない
        _pt_cols = None if _PEAK_PW else _PtCols(_pt_scores, st.pt_peak_cur)
        # 計測済み集合の表 (_gray_flags 用、集合と一緒に書き換える。cols があるときだけ・作れなければ None = 集合のまま)
        _pt_mm = _MeasMask.from_set(_pt_meas_set) if _pt_cols is not None else None
        # 計測済み集合が変わった回数 (前の大会直後の順位をそのまま使えるかの判定用)
        _pt_mver = [0]
        # 直前の serious 大会の「直後」の cascade と、その入力 (scores の版・計測済みの版・peak)。次の大会の
        # 「直前」の入力がこれとまったく同じなら cascade を計算し直さずに使う (日本で約半分。結果は同じ)
        _pt_prev_after = None
        # peak スナップショットの増分維持 (全選手 take_snap を避ける):
        # _pt_peak_cur[uid] = peak_at(uid, 現在の serious ts)。前回 serious 大会の参加者
        # (peak 変化) + 窓から外れた entry を持つ選手 (peak 低下しうる) だけ再計算、他はキャッシュ。
        _pt_peak_cur = st.pt_peak_cur; _pt_peak_exp = st.pt_peak_exp; _pt_last_parts = st.pt_last_parts
        _PT_PEAK_VERIFY = _S.pt_verify
        _TM = _S.pt_timing
        _tm = defaultdict(float)
        _pc = time.perf_counter
        def _pt_measured(ts):
            # measured_uids_at_ts と同一: 直近 2y の集計対象大会出場 >= PROV_2Y_MIN_TOURS。
            # 永続集合を prune で更新 (全選手再構築を回避)。add 側は本体ループで更新。
            while _pt_mwindow and _pt_mwindow[0][0] <= ts - _pt_lo_win:
                _, _u = _pt_mwindow.popleft(); _pt_mcounts[_u] -= 1
                if _pt_mcounts[_u] < PROV_2Y_MIN_TOURS and _u in _pt_meas_set:
                    _pt_meas_set.discard(_u); _pt_mver[0] += 1
                    if _pt_mm is not None: _pt_mm.discard(_u)
            return _pt_meas_set
        def _pt_after_now(ts):
            """今の状態の cascade と表示順位: 直前の serious 大会の直前の peak (_pt_cols) にその大会の参加者
            (_pt_last_parts) の ts 時点の peak を書き、今のスコア・計測済み集合で引く (= 直前の serious 大会の
            結果確定直後の順位。その後に足された下位クラスの点も入る)。cols が無い (pw) ときは None。
            再開した学習でも永続した状態だけで同じ値になる。(配列, 表示順位, 書いた peak) を返す"""
            if _pt_cols is None:
                return None
            _ninf = float('-inf')
            upd = {}
            for _u in _pt_last_parts:
                _pv = peak180[_u].peak_at(ts)
                upd[_u] = (_peak_score(_get(_u).rating, ts) if _pv == _ninf else _pv) / ELO_PER_UNIT
            key = (_pt_cols.ver, _pt_mver[0], upd)
            if _pt_now_cache[0] is not None and _pt_now_cache[0] == key:
                return _pt_now_cache[1]
            pk = _pt_cols.pk[:_pt_cols.n].copy()
            for _u, _v in upd.items():
                _i = _pt_cols.idx.get(_u)
                if _i is not None:
                    pk[_i] = _v
            _mi = _pt_mm if _pt_mm is not None else _pt_meas_set
            _, arrays = _pt_cascade_inc(_pt_scores, None, _mi, cols=_pt_cols, pka_all=pk, want_dict=False)
            ranks = (_pt_display_ranks(arrays, None, _mi, pk_arrays=_pt_cols.peak_arrays_with(upd))
                     if arrays is not None else None)
            _pt_now_cache[0] = key; _pt_now_cache[1] = (arrays, ranks, upd)
            return _pt_now_cache[1]
        _pt_now_cache = [None, None]
        print(f"  [per-tour-lv] ON: {len(prior)} tours, decay_ref={PT_DECAY_REF.date()}, "
              f"resume from {st.cursor_date}", flush=True)

    _n_done = 0
    # 大会ごとのアンカー (pt_at_time) は、その大会を処理し終えたら (= 次の大会の頭で) 配列に詰める
    # (spsp/columnar.AnchorTable。読む側には同じ dict に見える)
    _pt_freeze = None
    # 表示順位・アンカー・過去最高順位の追跡は _OutWorker で学習と並べて流す (本番の cols ありのときだけ。
    # SPSP_LEARN_OUT_THREAD=0 で以前どおりその場で計算)
    _out = _OutWorker(_per_tour_lv and _pt_at_time is not None and _pt_cols is not None
                      and os.environ.get('SPSP_LEARN_OUT_THREAD', '1') != '0')

    def _freeze_anchor(path):
        if path is not None and type(_pt_at_time.get(path)) is dict:
            _pt_at_time[path] = AnchorTable(_pt_at_time[path])

    def _freeze_anchor_v(path, _v):
        _freeze_anchor(path)

    def _peak_update(ts, ranks):
        _t = _pcs()
        try:
            _peak_update0(ts, ranks)
        finally:
            _SEC['p_update'] += _pcs() - _t

    _peak_last = [None, None]   # 前回 update に渡した (ranks, 日)

    def _peak_update0(ts, ranks):
        _day = dt.datetime.fromtimestamp(ts).date().toordinal()
        # 前の大会の直後の順位をそのまま次の大会の直前に使い回したとき (同じ ranks を同じ日に 2 回) は、2 回目は何も変えない
        # (最良以下の順位で最良と達成日を同じ値に書くだけ) ので飛ばす
        if ranks is _peak_last[0] and _day == _peak_last[1]:
            _SEC['#p_skip'] += 1
            st.peak_track.touch(int(ts))
            return
        _peak_last[0] = ranks; _peak_last[1] = _day
        _u0, _lv0, _e0, _t0, _bu, _bj = ranks
        st.peak_track.update(int(ts), _day,
                             _u0, _e0, _lv0, _t0, level_tier_values(_lv0, _e0), _bu, _bj)

    def _out_before_ranks(arrays, peak, meas, pk_in, rk):
        # rk = 前の大会の直後の表示順位 (_Hold、使い回せるとき。別の計算のスレッドが作っていればできるまで待つ)。無ければここで引く
        ranks = rk.get() if rk is not None else None
        if ranks is None and arrays is not None:
            ranks = _pt_display_ranks(arrays, peak, meas, pk_arrays=pk_in, stream='_b')
        return ranks

    def _out_before(path, arrays, parts, ts, ranks):
        _pt_at_time[path] = _pt_at_time_anchor(arrays, None, None, parts, ranks=ranks)
        # 過去最高順位の追跡 (全選手、この時点の表示順位)。集計開始日より前の大会は数えない
        if ranks is not None and ts >= _PEAK_CUTOFF_TS:
            _peak_update(ts, ranks)

    def _out_after_compute(snap, peak_a, meas_a, pk_a, pkarr_a, hold_arr, hold):
        # 大会直後の cascade (学習には使わない: after アンカーと、次の大会の直前で使い回すときだけ)。snap が無ければ
        # 呼び出し側で引いて hold_arr に入れてある
        if snap is not None:
            hold_arr.set(_pt_cascade_inc(None, None, meas_a, cols=snap, pka_all=pk_a, want_dict=False, stream='_a')[1])
        arrays_a = hold_arr.get()
        ranks_a = (_pt_display_ranks(arrays_a, peak_a, meas_a, pk_arrays=pkarr_a, stream='_a')
                   if arrays_a is not None else None)
        hold.set(ranks_a)
        return arrays_a, ranks_a

    def _out_after(path, ts, res):
        arrays_a, ranks_a = res
        if arrays_a is None:
            return
        before_entry = _pt_at_time[path]
        after = _pt_at_time_anchor(arrays_a, None, None, set(before_entry.keys()), ranks=ranks_a)
        # 結果確定直後の順位も過去最高の追跡に入れる (大会直後に達した順位を落とさない)
        if ts >= _PEAK_CUTOFF_TS:
            _peak_update(ts, ranks_a)
        for _u, _e in before_entry.items():
            _r = after.get(_u)
            if _r is not None and len(_e) < 7:
                _e.extend((_r[1], _r[2], _r[3]))  # ens_a, tjpr_a, bt_a

    def _out_nonserious(path, arrays, ranks, parts, _v):
        anc = _pt_at_time_anchor(arrays, None, None, parts, ranks=ranks)
        if anc:
            _pt_at_time[path] = anc

    _sched_before = None   # この大会の before アンカーを _out に積んだ (= pt_at_time に入る) event_path
    for t in prior:
        if _pt_freeze is not None:
            _out.put(partial(_freeze_anchor_v, _pt_freeze)); _pt_freeze = None
        _sched_before = None
        if t.timestamp < st.cursor_ts:
            continue                       # 処理済み (台帳で一致確認済み)
        if until_ts is not None and t.timestamp >= until_ts:
            break
        meta = t_meta_by_id[id(t)]
        st.ledger.append((rel_event_path(t.event_path), int(t.timestamp), tournament_digest(t, meta)))
        _n_done += 1
        _pt_entry = None
        will_update_bt = not (meta.get('is_non_serious') or meta.get('is_gf_missing')
                              or meta.get('is_class_virtual') or meta.get('is_small'))
        # per-tour: serious 大会の直前に、ここまでの増分スコアで player_lv を更新
        # (BT gating に使われる前。run_shared_cascade と同一ロジックの sort-only)。
        if _per_tour_lv and will_update_bt:
            _a=_pc() if _TM else 0
            _pt_now_cache[0] = None          # peak を書き直すので _pt_after_now の使い回しを止める
            _pt_meas = _pt_measured(t.timestamp)
            # peak スナップショット (増分): 前回参加者 + 窓から外れた entry の選手のみ再計算。
            _cut = t.timestamp - _common.BT_PEAK_WINDOW_SEC
            if _PEAK_PW:
                # pw は評価時刻ごとに全員のスコアが変わるので増分キャッシュが使えない。
                # numpy でまとめて引き直す (_PwSnap は dict 互換の read API を持つ)。
                # BT 更新が一度も無い参加者だけは非 pw と同じく「現レート」fallback で、
                # こちらはレートが動かないので従来どおり dirty 更新で足りる。
                for _u in _pt_last_parts:
                    if _u in _pw_store.idx:
                        _pt_peak_cur.pop(_u, None)
                    else:
                        _pt_peak_cur[_u] = _get(_u).rating / ELO_PER_UNIT
                _pt_peak = _pw_store.snapshot(t.timestamp)
                _pt_peak.scores /= ELO_PER_UNIT
                _pt_peak.extra = _pt_peak_cur
            else:
                _dirty = set(_pt_last_parts)
                while _pt_peak_exp and _pt_peak_exp[0][0] < _cut:
                    _dirty.add(_pt_peak_exp.popleft()[1])
                # 前の serious 大会の直後の peak (= その直前の peak + 参加者の値 _pv_[2]) と今回の peak が同じか
                _pv_ = _pt_prev_after; _pt_prev_after = None
                _same_pk = _pv_ is not None
                for _u in _dirty:
                    _pv = peak180[_u].peak_at(t.timestamp)
                    _v = (_peak_score(_get(_u).rating, t.timestamp) if _pv == float('-inf') else _pv) / ELO_PER_UNIT
                    if _same_pk:
                        _ref = _pv_[2].get(_u, _MISSING)
                        if _ref is _MISSING:
                            _ref = _pt_peak_cur.get(_u, _MISSING)
                        _same_pk = _ref is not _MISSING and _v == _ref
                    _pt_cols.set_peak(_u, _v)
                _pt_peak = _pt_peak_cur
            if _PT_PEAK_VERIFY and not _PEAK_PW:
                _full = take_snap(t.timestamp)
                if _full != _pt_peak_cur:
                    _bad = {k for k in set(_full)|set(_pt_peak_cur) if _full.get(k)!=_pt_peak_cur.get(k)}
                    raise AssertionError(f"peak snap mismatch @ {t.timestamp}: {len(_bad)} uids e.g. "
                                         f"{[(k,_full.get(k),_pt_peak_cur.get(k)) for k in list(_bad)[:3]]}")
            if _TM: _tm['snap_meas_before']+=_pc()-_a; _a=_pc()
            _pt_ranks = None
            if _pt_cols is not None and _same_pk and _pv_[0] == _pt_cols.ver and _pv_[1] == _pt_mver[0]:
                # 前の大会の直後から入力が変わっていない (scores・計測済み・peak。peak は今回引き直した選手だけ
                # 比べればよい: 他は前の大会の直後の peak と同じ値) → 同じ cascade・表示順位になる
                _pt_arrays, _pt_ranks = _pv_[3].get(), _pv_[4]   # 大会直後の cascade (別スレッドならできるまで待つ)
                _pt_pmax = _PmaxView(_pt_cols.idx, len(_pt_arrays[0]), _pt_arrays[0], _pt_arrays[1])
                if _TM: _tm['casc_reused'] += 1
            else:
                _pt_pmax, _pt_arrays = _pt_cascade_inc(_pt_scores, _pt_peak, _pt_mm if _pt_mm is not None else _pt_meas,
                                                       cols=_pt_cols)
            if _TM: _tm['casc_before']+=_pc()-_a
            if _pt_pmax:
                player_lv = _pt_pmax
            if _pt_at_time is not None and _pt_pmax:
                _pt_parts = {uid for _, uid in t.standings}
                for _m in t.matches:
                    _pt_parts.add(_m.winner_id); _pt_parts.add(_m.loser_id)
                # before = 大会直前の厳密 cascade (T の結果は未反映)。表示順位・アンカー・過去最高は _out で
                # (_pt_ranks = 前の大会の直後の表示順位の入れ物、使い回せないときは None)
                _a=_pc() if _TM else 0
                _pk_in = _meas_in = None
                if _pt_ranks is None and _pt_arrays is not None:
                    _pk_in = _pt_cols.peak_arrays() if _pt_cols is not None else None
                    _meas_in = _pt_mm if _pt_mm is not None else _pt_meas
                    if _out.on:   # 学習側がこの後書き換えるので写す
                        _pk_in = (_pk_in[0], _pk_in[1].copy())
                        _meas_in = _pt_mm.copy() if _pt_mm is not None else set(_pt_meas)
                _sched_before = rel_event_path(t.event_path)
                _out.put(partial(_out_before, _sched_before, _pt_arrays, _pt_parts, t.timestamp),
                         partial(_out_before_ranks, _pt_arrays, None if _out.on else _pt_peak, _meas_in, _pk_in, _pt_ranks))
                _pt_freeze = _sched_before
                if _TM: _tm['anchor_before']+=_pc()-_a
        participants = set()
        for m in t.matches:
            participants.add(m.winner_id); participants.add(m.loser_id)
        standings_uids = {uid for _, uid in t.standings}
        bt_at_t[id(t)] = {uid: _get(uid).rating / ELO_PER_UNIT
                          for uid in standings_uids | participants}
        st.bt_at_t_by_path[rel_event_path(t.event_path)] = bt_at_t[id(t)]
        # per-tour: 増分 TJPR スコア + measured 窓を更新 (tjpr-eligible = class_virtual 含む)。
        # bt_at_t[id(t)] (= BT更新前の種) を使う precompute_banzuke と整合。continue の前に置く。
        if _per_tour_lv and not (meta.get('is_non_serious') or meta.get('is_gf_missing')
                                 or meta.get('is_small')):
            _a=_pc() if _TM else 0
            _pt_entry = _pt_entry_for(t, meta, bt_at_t, _pt_parent_by_eid,
                                      t_meta_by_id, PT_DECAY_REF)
            if _TM: _tm['tjpr_entry']+=_pc()-_a
            if _pt_entry is not None:
                for (_lv, _tf, _lp, _tn), (_lv2, _topN, _pd) in zip(LV_DEFS, _PT_LV_AGG):
                    _ed = compute_tjpr_at_lv([_pt_entry], _lp, _tf, None)
                    if not _ed:
                        continue
                    _se = _pt_scores_entries[_lv]; _sc = _pt_scores[_lv]
                    for _u, _e in _ed.items():
                        _se[_u].append(_e)
                        if _pt_cols is not None:
                            _pt_cols.set_score(_lv, _u, _pt_agg(_se[_u], _topN, _pd))
                        else:
                            _sc[_u] = _pt_agg(_se[_u], _topN, _pd)
            for _u in {uid for _, uid in t.standings}:
                _pt_mcounts[_u] += 1; _pt_mwindow.append((int(t.timestamp), _u))
                if _pt_mcounts[_u] >= PROV_2Y_MIN_TOURS and _u not in _pt_meas_set:
                    _pt_meas_set.add(_u); _pt_mver[0] += 1
                    if _pt_mm is not None: _pt_mm.add(_u)
        if _per_tour_lv and _pt_at_time is not None and _pt_cols is not None and not will_update_bt:
            _a=_pc() if _TM else 0
            if meta.get('is_class_virtual'):
                # 本戦の最後の下位クラス → 本戦の「結果確定直後」の順位をここで引く (本戦の処理では引かずに待つ)
                _pp = _pt_last_cv.get(meta.get('parent_event_id'))
                _ptt = _pt_parent_by_eid.get(meta.get('parent_event_id'))
                _ppath = rel_event_path(_ptt.event_path) if _ptt is not None else None
                if _pp == rel_event_path(t.event_path):
                    _out.drain()   # 本戦のアンカーを読み書きする (本戦ごとに 1 回なので待ってその場で計算する)
                if _pp == rel_event_path(t.event_path) and _ppath in _pt_at_time:
                    _now = _pt_after_now(t.timestamp)
                    if _now is not None and _now[0] is not None:
                        _arr_n, _rk_n, _upd_n = _now
                        _src = _pt_at_time[_ppath]
                        _after = _pt_at_time_anchor(_arr_n, None, _pt_meas_set, list(_src.keys()), ranks=_rk_n)
                        _d = {}
                        for _u, _e in _src.items():
                            _e = list(_e)[:4]
                            _r = _after.get(_u)
                            if _r is not None:
                                _e.extend((_r[1], _r[2], _r[3]))
                            _d[_u] = _e
                        _pt_at_time[_ppath] = AnchorTable(_d) if type(_src) is not dict else _d
                        # 次の serious 大会の直前に使い回せる (入力の版と一緒に持つ)
                        _pt_prev_after = (_pt_cols.ver, _pt_mver[0], _upd_n, _Hold(None, _arr_n), _Hold(None, _rk_n))
                        if t.timestamp >= _PEAK_CUTOFF_TS:
                            _peak_update(t.timestamp, _rk_n)
            else:
                # 集計対象外の大会 (身内・小規模・GF なし): 点が動かないので前後とも「その時点の最新の集計」の順位
                # ([lv, ens, tjpr, bt] だけ。after = before として読む)
                _now = _pt_after_now(t.timestamp)
                if _now is not None and _now[0] is not None:
                    _parts_n = {uid for _, uid in t.standings}
                    for _m in t.matches:
                        _parts_n.add(_m.winner_id); _parts_n.add(_m.loser_id)
                    # ranks があるので measured は使わない。空なら書かない (freeze は書いていなければ何もしない)
                    _out.put(partial(_out_nonserious, rel_event_path(t.event_path), _now[0], _now[1], _parts_n))
                    _pt_freeze = rel_event_path(t.event_path)
            if _TM: _tm['anchor_nonserious']+=_pc()-_a
        if not will_update_bt:
            continue

        # rolling delta (大会ごとの順位変動、build 用) を増分計算。build 側の順位評価の表と同じ
        # 引数 (shift = GLOBAL_SHIFT_FROZEN、BT 更新前の種 bt_at_t) で 1 大会ぶんの raw 点を出す。
        # 非仮想の serious 大会 = will_update_bt と同じ集合。減衰基準日は raw 点に効かない。
        _roll_key = rel_event_path(t.event_path)
        if _roll_key not in st.rolling.deltas_by_path:
            if _pt_entry is not None:
                # per-tour 用の同じ大会のエントリ (shift 0) から導出: 非仮想大会のエントリは大会単体で決まり、
                # shift は norm (= ord × ELO_PER_UNIT − shift) にしか効かない (bt_at_t は standings 全員を
                # 含むので default_rate は使われない、rounds / players は shift 非依存)。x − 0.0 = x なので
                # norm − shift は直接計算と bit 一致。precompute_banzuke の呼び直し (約 30 秒/全期間) を省く。
                _rc = [dict(_pt_entry, norm=_pt_entry['norm'] - GLOBAL_SHIFT_FROZEN)]
            else:
                _rc = precompute_banzuke([t], PT_DECAY_REF, bt_at_t, GLOBAL_SHIFT_FROZEN, t_meta_by_id,
                                         TJPR_DEFAULT_RATE, allow_future=True)
            _raw_map, _ = _rolling_lv2_raw_per_tour(_rc).get(id(t), ({}, 0))
            st.rolling.step(_roll_key, t, meta['is_weekend'], _raw_map, placements_of(t))

        for uid in participants:
            _get(uid); _apply_inactivity(uid, t.timestamp)

        # peak180 prune (drop entries outside the 180-day window)
        for uid in participants:
            if uid not in first_part_ts:
                first_part_ts[uid] = int(t.timestamp); part_order.append(uid)
            tr = peak180[uid]
            # 全期間窓では窓落ちが起きないので prune 自体は不要。ただし
            # peak180[uid] へのアクセス (= entry 生成) は飛ばしてはいけない:
            # BT 更新が一度も無い参加者は take_snap で「現レート」fallback として
            # 拾われるため、キー集合が変わると BT 順位の母集合が変わってしまう。
            if _PEAK_ALLTIME:
                continue
            cutoff = t.timestamp - _common.BT_PEAK_WINDOW_SEC
            popped = False
            while tr.history and tr.history[0][0] < cutoff:
                _, old = tr.history.popleft()
                if old >= tr.current_peak - 1e-12:
                    popped = True
            if popped:
                tr.current_peak = max((x for _, x in tr.history), default=float('-inf'))

        before_ord = ({uid: _get(uid).rating / ELO_PER_UNIT for uid in participants}
                      if bt_per_tour is not None else None)
        peak180_before = ({uid: (peak180[uid].current_peak / ELO_PER_UNIT
                                 if peak180[uid].current_peak != float('-inf')
                                 else _get(uid).rating / ELO_PER_UNIT)
                          for uid in participants}
                          if bt_per_tour is not None else None)
        eid = t.event_id if bt_per_tour is not None else None   # = attr.json の event_id (data_loader が読んだ値)

        any_used = {uid: False for uid in participants}
        # クラスブラケット (Bクラス等, m.is_class) の試合は「下位クラス大会」として gate する
        # (= 親 event が通常大会でも、Lv3+ (exclude_capped) のプレイヤーは BT 更新しない。
        #  Lv1/2 は従来どおり更新 = TJPR の class virtual 扱いと整合)。
        meta_cls = None
        # SPSP_BT_SIZE_K: 規模スケール k は大会内で一定 (nent 基準)。
        _k_size = 1.0
        if _BT_SIZE_K_MODE:
            _n = max(meta.get('nent') or 2, 2)
            if _BT_SIZE_K_MODE == 'log2':
                _k_size = math.log2(_n) / math.log2(_BT_SIZE_K_REF)
            elif _BT_SIZE_K_MODE == 'pow':
                _k_size = (math.log2(_n) / math.log2(_BT_SIZE_K_REF)) ** _BT_SIZE_K_POW
            else:
                _k_size = sm_sqrtlog(_n) / sm_sqrtlog(_BT_SIZE_K_REF)
        if _DBG_UID and _DBG_UID in participants:
            print(f"  [dbg settings] per_tour_lv={_S.per_tour_lv} lv3_weekend={_S.lv3_weekend} lv2_nent_gt={_S.lv2_nent_gt} lv4_nent_gt={_S.lv4_nent_gt} prov={_S.provisional_pfilter} LV3_TFILTER={LV_TFILTERS.get(3)} id(_S)={id(_S)}", flush=True)
            print(f"  [dbg uid={_DBG_UID}] {meta.get('date')} {str(meta.get('name',''))[:24]!r} nent={meta.get('nent')} "
                  f"weekend={meta.get('is_weekend')} lv={player_lv.get(_DBG_UID, 1)} "
                  f"passes={tournament_updates_player(_DBG_UID, meta)} rating={_get(_DBG_UID).rating:.1f}", flush=True)
            if _per_tour_lv:
                _out.drain()
                _anc = (_pt_at_time.get(rel_event_path(t.event_path), {}) if _pt_at_time is not None else {})
                print(f"      anchor_before={_anc.get(_DBG_UID)} measured={_DBG_UID in _pt_meas} "
                      f"pt_scores={ {lv: round(_pt_scores[lv].get(_DBG_UID, 0.0), 3) for lv in _pt_scores} } "
                      f"peak={_pt_peak.get(_DBG_UID) if not _PEAK_PW else None}", flush=True)
        for m in sorted(t.matches, key=_bracket_order_key):
            m_meta = meta
            if _BT_CLASS_GATE and getattr(m, 'is_class', False):
                if meta_cls is None:
                    meta_cls = dict(meta); meta_cls['is_lower_class'] = True
                m_meta = meta_cls
            u_w = tournament_updates_player(m.winner_id, m_meta)
            u_l = tournament_updates_player(m.loser_id, m_meta)
            wd_w = wd_l = False
            if _BT_LV3WD_K:
                if (not u_w) and player_lv.get(m.winner_id, 1) == 3 \
                        and lv3_passes_ignoring_weekend(m_meta):
                    u_w = True; wd_w = True
                if (not u_l) and player_lv.get(m.loser_id, 1) == 3 \
                        and lv3_passes_ignoring_weekend(m_meta):
                    u_l = True; wd_l = True
            if _BT_PAIR_GATE == 'both':
                u_w = u_l = (u_w and u_l)
            elif _BT_PAIR_GATE == 'any':
                u_w = u_l = (u_w or u_l)
            if not (u_w or u_l):
                if _pm_on and eid is not None:
                    bt_per_match.add(eid, m.winner_id, m.loser_id, 0.0, 0.0, False, False)
                continue
            w = _get(m.winner_id); l = _get(m.loser_id)
            before_w = w.rating / ELO_PER_UNIT
            before_l = l.rating / ELO_PER_UNIT
            wr, wrd, lr, lrd = w.rating, w.rd, l.rating, l.rd
            _kw = _kl = 1.0
            if _BT_K_ON:
                _kw = _kl = _k_size
                if _BT_BO5_K and (getattr(m, 'winner_score', None) or 0) >= 3:
                    _kw = _kl = _kw * _BT_BO5_K
                if _BT_BO1_K is not None and (getattr(m, 'winner_score', None) or 0) == 1:
                    _kw = _kl = _kw * _BT_BO1_K
                if _BT_ONESIDE_K and (u_w != u_l):
                    if u_w:
                        _kw *= _BT_ONESIDE_K
                    else:
                        _kl *= _BT_ONESIDE_K
                if _BT_LVGAP_K and abs(player_lv.get(m.winner_id, 1)
                                       - player_lv.get(m.loser_id, 1)) >= _BT_LVGAP_MIN:
                    _kw *= _BT_LVGAP_K
                    if _BT_LVGAP_SCOPE == 'both':
                        _kl *= _BT_LVGAP_K
                if wd_w:
                    _kw *= _BT_LV3WD_K
                if wd_l:
                    _kl *= _BT_LV3WD_K
            # SPSP_BT_GAMECOUNT_LV: 下位帯が格上と当たる試合は、その下位側**だけ**を
            # ゲーム勝敗 (セットカウント) で更新する。相手側は触らない (= 非対称)。
            _gc_w = _gc_l = None
            if _BT_GC_LV:
                _lv_w = player_lv.get(m.winner_id, 1)
                _lv_l = player_lv.get(m.loser_id, 1)
                _g = _gc_games(m)
                _GC_STATS['sets'] += 1
                if _g is None:
                    _GC_STATS['no_score'] += 1
                else:
                    _all = (_BT_GC_SCOPE == 'all')
                    if u_w and _lv_w <= _BT_GC_LV and (_all or _lv_l > _lv_w):
                        _gc_w = _g
                    if u_l and _lv_l <= _BT_GC_LV and (_all or _lv_w > _lv_l):
                        _gc_l = _g
            if u_w:
                if _gc_w is None:
                    w.update_player([lr], [lrd], [1])
                else:
                    _gw, _gl = _gc_w      # 勝った側なので _gw 勝 _gl 敗
                    _gc_apply(w, lr, lrd, _gw, _gl, 1)
                    _GC_STATS['gc_sets'] += 1
                if _kw != 1.0:
                    w.rating = wr + _kw * (w.rating - wr)
                any_used[m.winner_id] = True
            if u_l:
                if _gc_l is None:
                    l.update_player([wr], [wrd], [0])
                else:
                    _gw, _gl = _gc_l      # 負けた側なので _gl 勝 _gw 敗
                    _gc_apply(l, wr, wrd, _gl, _gw, 0)
                    _GC_STATS['gc_sets'] += 1
                if _kl != 1.0:
                    l.rating = lr + _kl * (l.rating - lr)
                any_used[m.loser_id] = True
            if _pm_on and eid is not None:
                d_w = (w.rating / ELO_PER_UNIT - before_w) if u_w else 0.0
                d_l = (l.rating / ELO_PER_UNIT - before_l) if u_l else 0.0
                bt_per_match.add(eid, m.winner_id, m.loser_id,
                                 round(max(0.0, d_w), 6), round(min(0.0, d_l), 6),
                                 bool(u_w), bool(u_l))

        for uid in participants:
            r = _get(uid)
            if any_used[uid]:
                peak180[uid].update(t.timestamp, _peak_score(r.rating, t.timestamp))
                rating_hist.add(uid, int(t.timestamp), r.rating)
                if _per_tour_lv and not _PEAK_ALLTIME:
                    # 全期間 peak では窓落ちが起きないので追跡不要 (deque が無駄に伸びる)
                    _pt_peak_exp.append((t.timestamp, uid))  # peak entry expiry tracking
                if bt_timeline is not None:
                    bt_timeline.add(uid, t.timestamp, r.rating, r.rd, r.rating / ELO_PER_UNIT)
            if bt_per_tour is not None and eid is not None:
                after_ord = r.rating / ELO_PER_UNIT
                peak_after = (peak180[uid].current_peak / ELO_PER_UNIT
                              if peak180[uid].current_peak != float('-inf') else after_ord)
                peak_d = peak_after - peak180_before[uid]
                bt_per_tour.add(uid, eid,
                                bool(any_used[uid]),
                                round(after_ord - before_ord[uid], 6),
                                round(peak_d, 6),
                                round(after_ord, 6),
                                round(peak_after, 6))
            last_seen_ts[uid] = t.timestamp
            tour_count[uid] += 1
        if _per_tour_lv:
            # この大会の参加者 = 次の serious before-snap で peak 再計算すべき dirty 集合。
            _pt_last_parts = participants

        # per-tour: T の結果確定「直後」(= TJPR 加点 + BT 更新済) の順位を after として捕捉。
        # T の時刻で cascade を引き直す → T 結果は反映済・以降の減衰や他大会は未反映。
        # before と同一の厳密 cascade を T 更新後の scores/peak で再評価するだけ (second cascade)。
        # 下位クラスを持つ本戦 (cols あり) は最後のクラスの処理で引く (上の not will_update_bt の段)
        # (before アンカーは _out に積んだだけでまだ書かれていないことがある → _sched_before で見る)
        if (_per_tour_lv and _pt_at_time is not None
                and (_sched_before == rel_event_path(t.event_path) or rel_event_path(t.event_path) in _pt_at_time)
                and not (_pt_cols is not None and meta.get('event_id') in _pt_last_cv)):
            # after の measured: 同時刻なので before の _pt_meas に「T 出場で新たに計測済みに
            # なった standings 選手」を足すだけ (全選手再構築を避ける)。count 増は standings のみ。
            _new_a = {_u for _u in standings_uids if _pt_mcounts[_u] >= PROV_2Y_MIN_TOURS}
            if _pt_mm is not None and _pt_meas is _pt_meas_set:
                # 集合を写さず表を写す (_pt_meas_a は cascade・表示順位の灰色判定にだけ使う。大きさは _pt_meas_a_len)
                _new_a.difference_update(_pt_meas)
                _pt_meas_a_len = len(_pt_meas) + len(_new_a)
                _pt_meas_a = _pt_mm.copy()
                for _u in _new_a:
                    _pt_meas_a.add(_u)
            else:
                _pt_meas_a = _pt_meas | _new_a
                _pt_meas_a_len = len(_pt_meas_a)
            # after の peak: 非参加者は同時刻で peak 不変なので before snap (_pt_peak) を流用し、
            # BT 更新された参加者だけ引き直す (全選手 take_snap を避けて O(参加者) に)。
            _a=_pc() if _TM else 0
            # 参加者の大会直後の peak。peak 全体 (_pt_peak_a = 大会直前の peak + これ) は cols があれば配列だけで
            # 作る (20 万人の dict の写しを作らない)
            _upd_a = {}
            for _u in participants:
                _pv = peak180[_u].peak_at(t.timestamp)
                _upd_a[_u] = (_peak_score(_get(_u).rating, t.timestamp)
                              if _pv == float('-inf') else _pv) / ELO_PER_UNIT
            if _pt_cols is not None:
                _pt_peak_a = None
                # cols の並びの peak (= _pt_peak_a.get(u, -inf)) と peak の並びの (uid, 値)
                _pk_a = _pt_cols.pk[:_pt_cols.n].copy()
                for _u, _v in _upd_a.items():
                    _i = _pt_cols.idx.get(_u)
                    if _i is not None:
                        _pk_a[_i] = _v
                _pkarr_a = _pt_cols.peak_arrays_with(_upd_a)
            else:
                _pt_peak_a = dict(_pt_peak); _pt_peak_a.update(_upd_a)
                _pk_a = _pkarr_a = None
            if _TM: _tm['peak_meas_after']+=_pc()-_a; _a=_pc()
            # 大会直後の cascade (_hold_arr)・表示順位 (_hold_a)・after アンカー・過去最高は _out で (cols があれば
            # cascade も別スレッド: 入力を写して渡す)。次の serious 大会の直前に使い回せるように入力の版と一緒に持つ
            # (_pt_meas_a は計測済み集合と同じ中身のはず。違えば使い回さない)
            if _out.on:
                _hold_arr = _Hold(_out); _snap = _ColsSnap(_pt_cols)
                _has_a = _pt_cols.n > 0
            else:
                _hold_arr = _Hold(None, _pt_cascade_inc(_pt_scores, _pt_peak_a, _pt_meas_a, cols=_pt_cols,
                                                        pka_all=_pk_a, want_dict=False)[1]); _snap = None
                _has_a = _hold_arr.v is not None
            if _TM: _tm['casc_after']+=_pc()-_a
            if _has_a:
                _a=_pc() if _TM else 0
                _hold_a = _Hold(_out)
                if _pt_cols is not None and _pt_meas_a_len == len(_pt_meas_set):
                    _pt_prev_after = (_pt_cols.ver, _pt_mver[0], _upd_a, _hold_arr, _hold_a)
                _out.put(partial(_out_after, rel_event_path(t.event_path), t.timestamp),
                         partial(_out_after_compute, _snap, _pt_peak_a, _pt_meas_a, _pk_a, _pkarr_a, _hold_arr, _hold_a))
                if _TM: _tm['anchor_after']+=_pc()-_a
    _out.put(partial(_freeze_anchor_v, _pt_freeze))
    _out.close()
    if _per_tour_lv and _S.pt_timing:
        print("  [pt-timing] " + "  ".join(f"{k}={v:.0f}s" for k, v in sorted(_tm.items(), key=lambda x:-x[1])), flush=True)

    if _BT_GC_LV and _GC_STATS['sets']:
        _s = _GC_STATS
        _mx = (f", mode=max 採用 gc {_s['max_gc']} / set {_s['max_set']} "
               f"(gc {100.0 * _s['max_gc'] / max(1, _s['max_gc'] + _s['max_set']):.1f}%)"
               if _BT_GC_MODE == 'max' else '')
        _scope = '全相手' if _BT_GC_SCOPE == 'all' else '格上のみ'
        print(f"  [gamecount] Lv<={_BT_GC_LV} ({_scope}): {_s['gc_sets']} 側 / {_s['sets']} セット "
              f"({100.0 * _s['gc_sets'] / _s['sets']:.2f}%), 展開ゲーム {_s['gc_games']}, "
              f"スコア無し {_s['no_score']} ({100.0 * _s['no_score'] / _s['sets']:.2f}%)"
              + _mx, flush=True)
    # ループ内で束縛し直した変数を状態に戻す (他は in-place 更新済み)
    st.player_lv = player_lv.to_dict() if isinstance(player_lv, _PmaxView) else player_lv
    if _per_tour_lv:
        st.pt_last_parts = _pt_last_parts
    if until_ts is not None:
        st.cursor_ts = max(st.cursor_ts, int(until_ts))
    elif prior:
        st.cursor_ts = max(st.cursor_ts, int(prior[-1].timestamp) + 1)
    print(f"  [learn] {_n_done} tournaments processed → cursor {st.cursor_date} "
          f"({len(st.ledger)} total)", flush=True)
    return _n_done


def g2_snapshots(state: G2LearnState, prior, t_meta_by_id, target_ts_list, cache_dir=None, compact=None):
    """学習後に、各時点 ts (SNAPSHOT_DAYS 等) の (peak dict, player_lv, cascade) を状態から復元する。

    以前はループ内で ts に到達したときに run_cascade_at(ts) で計算していたものと同じ値:
      - peak: その時点までのレート履歴 (rating_hist) の窓内 max、無ければその時点のレート
        (= ループ内の take_snap と同じ)。dict の並びも peak180 のキー生成順 (part_order) で再現。
      - cascade: ts より前の大会 + 各大会開始時点の bt_at_t + ts 時点の measured で run_shared_cascade。
    学習の軌跡に影響しない (per-tour の player_lv は serious 大会ごとに増分カスケードで決まる) ので、
    途中で保存・再開した状態からでも同じ値になる。

    cache_dir: 時点 ts の値は「ts より前の大会の台帳 (内容ハッシュ)」「Settings」「関係するコード」だけで決まるので、
    その 3 つのハッシュをキーに pickle をキャッシュする。同日の再実行 (3 時間ごと) では day 0 以外が当たる。
    翌日は ts 自体がずれる (N 日前) ので当たらない。データが変わった時点から先は自動的に外れる。

    compact: 呼び出し側が時点ごとに小さくして持つとき (spsp/cli/build_full)。compact(ts, peak_dict, cascade) を時点ごとに
    呼び、返した (peak, cascade) を戻り値に入れる (peak が None の時点は peak180_at_target に入れない)。時点ごとの Lv
    (player_lv_at_target) は作らない (空の dict)。61 時点 × 全選手の dict を最後まで持たないため (北米で +1.8 GB だった)。
    """
    if _PEAK_PW:
        raise NotImplementedError('peak_decay_mode=pw ではスナップショットの事後復元に未対応 (実験用)')
    st = state
    _snapc = _SnapCache(cache_dir, st, prior) if cache_dir else None   # 名前は cache だとループ内の precompute_banzuke の cache と衝突する
    bt_at_t = st.bt_at_t_view(prior)
    win = _common.BT_PEAK_WINDOW_SEC
    peak180_at_target: dict[int, dict[int, float]] = {}
    player_lv_at_target: dict[int, dict[int, int]] = {}
    cascade_at_target: dict[int, tuple] = {}
    n_hit = 0
    for ts in sorted(target_ts_list or []):
        ts = int(ts)
        if _snapc is not None:
            hit = _snapc.get(ts)
            if hit is not None:
                if compact is not None:
                    _keep_point(compact, ts, hit[0], hit[2], peak180_at_target, cascade_at_target)
                else:
                    peak180_at_target[ts], player_lv_at_target[ts], cascade_at_target[ts] = hit
                n_hit += 1
                continue
        cutoff = ts - win
        peak_dict = {}
        for uid in st.part_order:
            if st.first_part_ts[uid] >= ts:
                continue                  # その時点では peak180 のキーがまだ無い
            best = float('-inf'); last_r = 1500.0
            for ts_, r in st.rating_hist.get(uid, ()):
                if ts_ >= ts:
                    break
                last_r = r
                if ts_ >= cutoff:
                    sc = _peak_score(r, ts_)
                    if sc > best:
                        best = sc
            pv = best if best != float('-inf') else _peak_score(last_r, ts)
            peak_dict[uid] = pv / ELO_PER_UNIT
        peak180_at_target[ts] = peak_dict
        prior_for_cascade = [tt for tt in prior if tt.timestamp < ts]
        if prior_for_cascade:
            snap_date = dt.datetime.fromtimestamp(ts)
            cache = precompute_banzuke(prior_for_cascade, snap_date, bt_at_t, 0.0,
                                        t_meta_by_id, TJPR_DEFAULT_RATE)
            _prov_mode = _S.provisional_pfilter
            if _prov_mode == '2':
                measured = measured_uids_by_lv_from_cache(cache)
            elif _prov_mode == '1':
                # 表示順位 (jp_counter) と同一の計測済み定義 (実大会 standings ベース)
                measured = measured_uids_at_ts(prior, t_meta_by_id, ts)
            else:
                measured = None
            player_max_lv, player_ens_at_max_lv, player_tjpr_at_max_lv = run_shared_cascade(
                cache, peak_dict, measured_uids=measured)
            cascade_at_target[ts] = (player_max_lv, player_ens_at_max_lv, player_tjpr_at_max_lv)
            player_lv_at_target[ts] = dict(player_max_lv)
            del cache
        else:
            cascade_at_target[ts] = ({}, {}, {})
            player_lv_at_target[ts] = {}
        if _snapc is not None:
            _snapc.put(ts, (peak180_at_target[ts], player_lv_at_target[ts], cascade_at_target[ts]))
        if compact is not None:
            del player_lv_at_target[ts]
            _keep_point(compact, ts, peak180_at_target.pop(ts), cascade_at_target.pop(ts),
                        peak180_at_target, cascade_at_target)
        gc.collect()
    if _snapc is not None:
        print(f"  [snap-cache] {n_hit} hit / {len(target_ts_list or [])} points ({_snapc.dir})", flush=True)
    return peak180_at_target, player_lv_at_target, cascade_at_target


def _keep_point(compact, ts, peak_dict, cascade, peak180_at_target, cascade_at_target):
    p, c = compact(ts, peak_dict, cascade)
    if p is not None:
        peak180_at_target[ts] = p
    cascade_at_target[ts] = c


# 学習・スナップショットの値に関わらない書き出し側のファイル (spsp/ 直下)。鍵に入れないので、ここだけ直したときは
# キャッシュが当たる (以前は spsp/ を全部入れていて、書き出しを直すたびに北米で 20 分の計算が全部やり直しだった)。
# 学習側のファイルがこれらを import しないことは tests/meta/test_snap_cache_scope.py が確かめる。
# 学習も使う判定はここに入れず spsp/common.py に置く (t_passes / level_tier_values など)。
_SNAP_CODE_EXCLUDE = frozenset({
    'build.py', 'output.py', 'overlay.py', 'score.py', 'split_output.py', 'tour_cache.py', 'lazy_views.py',
    'random_seed.py', 'country_ja.py', 'char_index.py', 'char_names_ja.py', 'char_usage.py', 'char_vote.py',
    'fileio.py',
})


def _snap_code_hash() -> str:
    """スナップショットの値に関わるコードのハッシュ (変えたらキャッシュが自動で外れる)。"""
    import hashlib
    h = hashlib.blake2b(digest_size=8)
    # 学習・スナップショットの値に関わりうるコードは全部入れる (外すのは _SNAP_CODE_EXCLUDE だけ。足したファイルは入る)。
    _here = Path(__file__).resolve().parent
    files = [f for f in sorted(_here.glob('*.py')) if f.name not in _SNAP_CODE_EXCLUDE] \
        + sorted((_here / 'raters').glob('*.py'))   # learner.py 自身を含む (spsp/)
    for f in files:
        h.update(f.name.encode()); h.update(f.read_bytes()); h.update(b'\0')
    return h.hexdigest()


class _SnapCache:
    """g2_snapshots のキャッシュ。ファイル名 = <ts>_<ts より前の台帳と海外タグのハッシュ>_<設定>_<コード>.pkl

    海外タグ集合は ts より前の大会に出た選手の分だけをキーに入れる (check_overseas と同じ理由: 時点 ts の値に効くのは
    それ以前の大会に出た選手だけ)。以前は集合全体のダイジェストを設定に入れていたので、新しく収録された海外選手 1 人で
    全時点が外れていた (日本の 3 時間ごとの回が毎回 0 hit、2026-10-23 に判明)。"""

    def __init__(self, cache_dir, state, prior):
        import hashlib
        from spsp.common import OVERSEAS_UIDS
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._settings = hashlib.blake2b(_S.state_repr().encode(), digest_size=8).hexdigest()
        self._code = _snap_code_hash()
        self._ledger = state.ledger          # (rel_path, ts, digest) 処理順 = timestamp 順
        self._pos = 0                        # 台帳の走査位置 (ts 昇順に呼ばれる前提)
        self._h = hashlib.blake2b(digest_size=16)
        self._last_ts = None
        self._ov_all = OVERSEAS_UIDS
        self._tours = sorted(prior, key=lambda t: t.timestamp)
        self._tpos = 0                       # 大会の走査位置
        self._ov_seen: set[int] = set()      # ts より前の大会に出た海外タグの選手

    def _key(self, ts: int) -> Path:
        import hashlib
        assert self._last_ts is None or ts >= self._last_ts, 'ts は昇順で'
        while self._pos < len(self._ledger) and self._ledger[self._pos][1] < ts:
            p, t, d = self._ledger[self._pos]
            self._h.update(f'{p}|{t}|{d}\n'.encode()); self._pos += 1
        ov = self._ov_all
        while self._tpos < len(self._tours) and self._tours[self._tpos].timestamp < ts:
            t = self._tours[self._tpos]; self._tpos += 1
            for _, u in t.standings:
                if u in ov: self._ov_seen.add(u)
            for m in t.matches:
                if m.winner_id in ov: self._ov_seen.add(m.winner_id)
                if m.loser_id in ov: self._ov_seen.add(m.loser_id)
        self._last_ts = ts
        h = self._h.copy()
        h.update(b'overseas|' + ','.join(map(str, sorted(self._ov_seen))).encode())
        return self.dir / f'{ts}_{h.hexdigest()}_{self._settings}_{self._code}.pkl'

    def get(self, ts: int):
        import pickle
        p = self._key(ts)
        if not p.exists():
            return None
        try:
            from spsp.learn_state import load_pickle
            v = load_pickle(p)
        except Exception as e:
            print(f"  [snap-cache] 読めないので作り直す: {p.name}: {e}", flush=True)
            return None
        try:
            os.utime(p)   # 使った点は掃除 (日本 update_and_deploy.sh 2 日・北米 build_na.sh 7 日使われていないものを消す) の対象から外す
        except OSError:
            pass
        return v

    def put(self, ts: int, value) -> None:
        import pickle, os
        p = self._key(ts)
        tmp = p.with_suffix('.pkl.tmp')
        with open(tmp, 'wb') as f:
            pickle.dump(value, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, p)


def learn_bt_lv_shared_g2_pure(prior, t_meta_by_id, M, target_ts_list,
                                capture_timeline=False, capture_per_tour=False,
                                capture_at_time=False, settings: Settings | None = None):
    """Single-pass Glicko2 + cascade (互換ラッパ)。新規状態で prior を全部学習し、target_ts_list の
    各時点のスナップショットを復元して返す。途中保存・再開は g2_learn_advance / g2_snapshots を直接使う。

    bt_at_t と peak180 は OpenSkill 互換 ord スケール (= rating / ELO_PER_UNIT) で保持。
    """
    if settings is not None:
        apply_settings(settings)
    st = G2LearnState(capture_timeline, capture_per_tour, capture_at_time, _S.state_repr())
    g2_learn_advance(st, prior, t_meta_by_id, M, None)
    peak180_at_target, player_lv_at_target, cascade_at_target = g2_snapshots(st, prior, t_meta_by_id, target_ts_list)
    bt_at_t = st.bt_at_t_view(prior)
    extras = []
    if capture_timeline: extras.append(dict(st.bt_timeline))
    if capture_per_tour:
        extras.append(st.bt_per_tour)
        extras.append(st.bt_per_match)
    if capture_at_time:
        extras.append(st.pt_at_time_view(prior) if _S.per_tour_lv else None)  # per-tour off のときは None
    if extras:
        return (bt_at_t, peak180_at_target, player_lv_at_target, cascade_at_target, *extras)
    return bt_at_t, peak180_at_target, player_lv_at_target, cascade_at_target


def main():
    targets_file = sys.argv[1] if len(sys.argv) > 1 else "14_15.txt"
    targets_path = TARGETS_DIR / targets_file
    last_line = [l for l in targets_path.read_text().splitlines()
                 if l.strip() and not l.startswith("#")][-1]
    last_date = last_line.split("\t")[0]
    last_dt = dt.datetime.fromisoformat(last_date) + dt.timedelta(days=1)

    print(f"Targets: {targets_file}", flush=True)
    print("Loading tournaments ...", flush=True)
    t0 = time.time()
    since = dt.datetime(2018, 1, 1)
    all_t = load_all_tournaments(EVENTS_ROOT, REGION,
                                  since_ts=int(since.timestamp()),
                                  until_ts=int(last_dt.timestamp()),
                                  min_entrants=8)
    filter_dq_no_shows(all_t)
    t_meta = build_t_meta(all_t)
    all_t.sort(key=lambda t: t.timestamp)
    print(f"  {len(all_t)} tournaments ({time.time()-t0:.0f}s)", flush=True)

    targets = []
    for line in targets_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        parts = line.split("\t")
        if len(parts) < 2: continue
        targets.append((parts[0], int(parts[1])))
    eid_by_id = {}
    for t in all_t:
        attrf = Path(t.event_path) / "attr.json"
        try: eid = json.loads(attrf.read_bytes()).get("event_id")
        except Exception: continue
        eid_by_id[id(t)] = eid
    target_eids = {e for _, e in targets}
    target_t_by_eid = {}
    for t in all_t:
        eid = eid_by_id.get(id(t))
        if eid in target_eids:
            target_t_by_eid[eid] = t
    target_order = sorted(target_t_by_eid.items(), key=lambda kv: kv[1].timestamp)
    target_snap_ts = [int(tt.timestamp) for _, tt in target_order]
    target_snap_for_display = max(target_snap_ts)
    print(f"Targets matched: {len(target_order)}/{len(targets)}", flush=True)

    # Production global BT (= comparison)
    print("Training production global BT ...", flush=True)
    t0 = time.time()
    prod_bt = learn_bt_gated_tour(all_t, t_meta, M=GATED_M_TOUR, snap_ts_list=target_snap_ts)
    prod_bt_at_t = prod_bt[9]; prod_peak180 = prod_bt[-1]
    print(f"  done ({time.time()-t0:.0f}s)", flush=True)

    # BT-LV shared cascade
    print("Training BT-LV shared cascade ...", flush=True)
    t0 = time.time()
    bt_at_t_s, peak180_s, player_lv_s, cascade_s = learn_bt_lv_shared(
        all_t, t_meta, GATED_M_TOUR, target_snap_ts)
    print(f"  done ({time.time()-t0:.0f}s)", flush=True)

    # Eval per target
    results = {'BT_LV_shared': [],
               'BT_LV_shared_A1_score': [],   # = TJPR rank by score only (LV 跨ぎ可)
               'BT_LV_shared_A2_cascade': [], # = TJPR rank by (-max_lv, -score) (LV 強制)
               'production_E_TJPR_BT': [], 'v1_E_TJPR_BT': []}
    final_snap_data = {}

    for i, (eid, target_t) in enumerate(target_order, 1):
        ap = load_attendees(eid)
        if not ap: continue
        attendees, placed = ap
        snap_date = dt.datetime.fromtimestamp(target_t.timestamp)
        snap_ts = int(target_t.timestamp)
        prior_t = [t for t in all_t if t.timestamp < target_t.timestamp]

        # Production E_TJPR_BT
        prod_bt_snap = prod_peak180.get(snap_ts, {})
        cache_prod = precompute_banzuke(prior_t, snap_date, prod_bt_at_t, 0.0, t_meta, TJPR_DEFAULT_RATE)
        tjpr_prod = build_tjpr_sized(cache_prod, sm_sqrtlog)
        bt_filt = {u: prod_bt_snap[u] for u in attendees if u in prod_bt_snap}
        bt_sorted = sorted(bt_filt.items(), key=lambda x: -x[1])
        bt_rank_att = {u: i + 1 for i, (u, _) in enumerate(bt_sorted)}
        bt_seeds_att = seeds_from_rank(attendees, bt_rank_att)
        tjpr_filt = {u: r for u, r in tjpr_prod.items() if u in attendees and r > 0}
        tjpr_seeds_att = seeds_from_rank(attendees, tjpr_filt)
        e_score_p = {u: (bt_seeds_att[u] + tjpr_seeds_att[u]) / 2.0 for u in attendees}
        ordered_p = sorted(e_score_p.items(), key=lambda x: x[1])
        e_rank_p = {u: i + 1 for i, (u, _) in enumerate(ordered_p)}
        e_seeds_p = seeds_from_rank(attendees, e_rank_p)
        results['production_E_TJPR_BT'].append(metrics_full(attendees, placed, e_seeds_p))

        # v1 E_TJPR_BT (= linear TJPR g.mean()+eps, size_w=1, TJPR-only cascade, OpenSkill BT)
        tjpr_v1 = build_tjpr_general(cache_prod, rv_default, sm_one)
        tjpr_v1_filt = {u: r for u, r in tjpr_v1.items() if u in attendees}
        tjpr_v1_seeds = seeds_from_rank(attendees, tjpr_v1_filt)
        e_score_v1 = {u: (bt_seeds_att[u] + tjpr_v1_seeds[u]) / 2.0 for u in attendees}
        ordered_v1 = sorted(e_score_v1.items(), key=lambda x: x[1])
        e_rank_v1 = {u: i + 1 for i, (u, _) in enumerate(ordered_v1)}
        e_seeds_v1 = seeds_from_rank(attendees, e_rank_v1)
        results['v1_E_TJPR_BT'].append(metrics_full(attendees, placed, e_seeds_v1))

        # BT-LV shared cascade ranking
        max_lv_dict, ens_at_max_lv, tjpr_at_max_lv = cascade_s.get(snap_ts, ({}, {}, {}))
        # Cascade sort attendees (= original BT_LV_shared)
        scored_att = [u for u in attendees if u in max_lv_dict]
        sorted_att = sorted(scored_att, key=lambda u: (-max_lv_dict[u], ens_at_max_lv[u]))
        rank_map = {u: i + 1 for i, u in enumerate(sorted_att)}
        seeds_s = seeds_from_rank(attendees, rank_map)
        results['BT_LV_shared'].append(metrics_full(attendees, placed, seeds_s))

        # 仮説 A 検証: cascade sort 廃止、attendees 内で BT peak180 と TJPR で単純 ens.
        # 共通: BT seed は attendees 内 peak180 降順 rank.
        peak_dict_s = peak180_s.get(snap_ts, {})
        bt_se_in = {u: peak_dict_s[u] for u in attendees if u in peak_dict_s}
        bt_se_sorted = sorted(bt_se_in.items(), key=lambda x: -x[1])
        bt_se_rank = {u: j + 1 for j, (u, _) in enumerate(bt_se_sorted)}
        bt_se_seeds = seeds_from_rank(attendees, bt_se_rank)

        # A1: TJPR rank = score 降順のみ (= LV 跨ぎ、natural ens に任せる)
        tj_a1_in = {u: tjpr_at_max_lv[u] for u in attendees if u in tjpr_at_max_lv}
        tj_a1_sorted = sorted(tj_a1_in.items(), key=lambda x: -x[1])
        tj_a1_rank = {u: j + 1 for j, (u, _) in enumerate(tj_a1_sorted)}
        tj_a1_seeds = seeds_from_rank(attendees, tj_a1_rank)
        e_pair_a1 = {u: ((bt_se_seeds[u] + tj_a1_seeds[u]) / 2.0, tj_a1_seeds[u]) for u in attendees}
        ord_a1 = sorted(e_pair_a1.items(), key=lambda x: (x[1][0], x[1][1]))
        e_rank_a1 = {u: j + 1 for j, (u, _) in enumerate(ord_a1)}
        results['BT_LV_shared_A1_score'].append(
            metrics_full(attendees, placed, seeds_from_rank(attendees, e_rank_a1)))

        # A2: TJPR rank = (-max_lv, -score) cascade rank (= LV 強制、prod_E と同じ形式)
        tj_a2_in = {u: (max_lv_dict[u], tjpr_at_max_lv[u])
                    for u in attendees if u in tjpr_at_max_lv and u in max_lv_dict}
        tj_a2_sorted = sorted(tj_a2_in.items(), key=lambda x: (-x[1][0], -x[1][1]))
        tj_a2_rank = {u: j + 1 for j, (u, _) in enumerate(tj_a2_sorted)}
        tj_a2_seeds = seeds_from_rank(attendees, tj_a2_rank)
        e_pair_a2 = {u: ((bt_se_seeds[u] + tj_a2_seeds[u]) / 2.0, tj_a2_seeds[u]) for u in attendees}
        ord_a2 = sorted(e_pair_a2.items(), key=lambda x: (x[1][0], x[1][1]))
        e_rank_a2 = {u: j + 1 for j, (u, _) in enumerate(ord_a2)}
        results['BT_LV_shared_A2_cascade'].append(
            metrics_full(attendees, placed, seeds_from_rank(attendees, e_rank_a2)))

        if snap_ts == target_snap_for_display:
            all_uids = set(max_lv_dict.keys())
            order_global = sorted(all_uids,
                                   key=lambda u: (-max_lv_dict[u], ens_at_max_lv[u]))
            final_snap_data['BT_LV_shared'] = {
                'order': order_global,
                'max_lv': max_lv_dict,
                'ens_at_max_lv': ens_at_max_lv,
                'player_lv': player_lv_s.get(snap_ts, {}),
            }
            common_p = set(prod_bt_snap) & set(tjpr_prod)
            bt_g = sorted(common_p, key=lambda u: -prod_bt_snap[u])
            bt_gr = {u: i + 1 for i, u in enumerate(bt_g)}
            tj_g = sorted(common_p, key=lambda u: tjpr_prod[u])
            tj_gr = {u: i + 1 for i, u in enumerate(tj_g)}
            ens = {u: (bt_gr[u] + tj_gr[u]) / 2.0 for u in common_p}
            order_p = sorted(common_p, key=lambda u: ens[u])
            final_snap_data['production_E_TJPR_BT'] = {'order': order_p, 'ens': ens}

        del cache_prod
        gc.collect()
        if i % 5 == 0 or i == len(target_order):
            print(f"  [{i}/{len(target_order)}] {snap_date.date()} eid={eid} done", flush=True)

    print("\n=== Comparison (N=%d targets) ===" % len(target_order))
    methods = ['BT_LV_shared', 'BT_LV_shared_A1_score', 'BT_LV_shared_A2_cascade',
               'production_E_TJPR_BT', 'v1_E_TJPR_BT']
    summary = {}
    for m in methods:
        rs = results[m]
        if not rs: summary[m] = {}; continue
        agg = {
            'pairwise': float(np.mean([r['pairwise'] for r in rs])),
            'spr_abs':  float(np.mean([r['spr_abs']  for r in rs])),
        }
        for K in TOP_K:
            pa = [r[f'top{K}_pa']  for r in rs if r[f'top{K}_pa']  is not None]
            sp = [r[f'top{K}_spr'] for r in rs if r[f'top{K}_spr'] is not None]
            agg[f'top{K}_pa']  = float(np.mean(pa)) if pa else float('nan')
            agg[f'top{K}_spr'] = float(np.mean(sp)) if sp else float('nan')
        summary[m] = agg
    print(f"\n{'metric':<14}" + "".join(f"  {h:>22}" for h in methods))
    print("-" * (14 + 24 * len(methods)))
    rows = [('PairAcc ↑', 'pairwise'), ('|SPR| ↓', 'spr_abs')]
    for K in TOP_K: rows.append((f'top{K}_PA ↑', f'top{K}_pa'))
    for K in TOP_K: rows.append((f'top{K}_SPR ↓', f'top{K}_spr'))
    for label, key in rows:
        cells = "".join(f"  {summary[m].get(key, float('nan')):>22.4f}" for m in methods)
        print(f"{label:<14}{cells}")

    def load_names():
        names = {}
        try:
            with open(REPO / "smash_db_user/data/startgg/users.jsonl") as f:
                for line in f:
                    d = json.loads(line); uid = d.get('user_id')
                    tag = d.get('gamer_tag') or ''; prefix = d.get('prefix') or ''
                    if uid is not None:
                        names[uid] = f"{prefix} {tag}".strip() if prefix else tag
        except Exception: pass
        return names
    names = load_names()

    if 'BT_LV_shared' in final_snap_data:
        d = final_snap_data['BT_LV_shared']
        print(f"\n=== BT_LV_shared global top 30 ===")
        print(f"{'r':>3}  {'uid':>9}  {'name':<22}  {'LV':>2}  {'ens_r':>5}")
        for i, u in enumerate(d['order'][:30], 1):
            name = names.get(u, f"uid{u}")[:22]
            print(f" {i:>2}  {u:>9}  {name:<22}  {d['max_lv'][u]:>2}  {d['ens_at_max_lv'][u]:>5d}")
        # LV breakdown
        lv_counts = defaultdict(int)
        for u, lv in d['player_lv'].items(): lv_counts[lv] += 1
        print(f"\nLV breakdown: " + ", ".join(f"LV{k}={lv_counts[k]}" for k in sorted(lv_counts)))
    if 'production_E_TJPR_BT' in final_snap_data:
        d = final_snap_data['production_E_TJPR_BT']
        print(f"\n=== production_E_TJPR_BT global top 30 ===")
        print(f"{'r':>3}  {'uid':>9}  {'name':<22}  {'ens':>6}")
        for i, u in enumerate(d['order'][:30], 1):
            name = names.get(u, f"uid{u}")[:22]
            print(f" {i:>2}  {u:>9}  {name:<22}  {d['ens'][u]:>6.1f}")


if __name__ == "__main__":
    main()

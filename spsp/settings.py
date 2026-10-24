"""ビルド / 学習の設定を 1 つのオブジェクトにまとめる。

以前は build_v4_full.py が import 前に os.environ へ本番構成を書き込み、
eval_bt_lv_shared.py が import 時と呼び出し時に os.environ を読む構造だった
(環境変数 1 つで本番の数字が変わるのに、どこで読まれるかが散らばっていた)。

- Settings()          = 本番構成。本番ビルド (build_v4_full.py) はこれを明示的に渡す。
                        環境変数は読まない。
- Settings.from_env() = 旧来の環境変数方式。精度実験ハーネス (eval_v4_ultrank_pertour.py 等) が
                        環境変数で実験アームを切り替えるための互換用。
                        v3/eval_bt_lv_shared.py (shim) は import 時にこれを 1 回適用する (ハーネス互換)。本体 v4/learner.py は Settings() を適用する。

各フィールドの右のコメントが対応する旧環境変数。既定値 = 本番の値。
旧 BT_ENGINE は書かれていたがどこにも読まれていなかったので廃止。
bt_peak_days / decay_r は v4/common.py のモジュール定数なので、apply_settings が common.configure() で反映する。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


def _int_or_none(v: str | None) -> int | None:
    return int(v) if v is not None else None


def _float_or_none(v: str | None) -> float | None:
    return float(v) if v is not None else None


def _decay_r_from_env(v: str | None) -> float:
    # 旧 v4/common.py の読み方: 数値でなければ黙って既定 0.96
    try:
        return float(v) if v is not None else 0.96
    except (TypeError, ValueError):
        return 0.96


@dataclass(frozen=True)
class Settings:
    # ── レベル判定 (cascade) の対象大会条件 ──
    pfilter_mode: str = 'ens'               # BT_LV_SHARED_PFILTER
    bt_gating_mode: str = 'lv'              # BT_GATING_MODE
    lv2_weekend: bool = False               # BT_LV_SHARED_LV2_WEEKEND=1
    lv2_nent_gt: int | None = 8             # BT_LV_SHARED_LV2_NENT_GT (本番 8)
    lv3_weekend: bool = True                # BT_LV_SHARED_LV3_WEEKEND=1 (本番 on)
    lv3_nent_gt: int | None = None          # BT_LV_SHARED_LV3_NENT_GT
    lv4_nent_gt: int | None = 32            # BT_LV_SHARED_LV4_NENT_GT (本番 32)
    lv3_tjpr_weekday: bool = False          # SPSP_LV3_TJPR_WEEKDAY=1 (Lv3 順位評価に平日大会も加算)
    lv3_tjpr_weekday_w: float | None = None  # SPSP_LV3_TJPR_WEEKDAY_W
    lv6_nent_gt: int | None = None          # SPSP_LV6_NENT_GT (top N を Lv6 帯として分離)
    lv6_topn: int = 32                      # SPSP_LV6_TOPN
    # ── 計測中 (provisional) の扱い ──
    provisional_pfilter: str | None = '1'   # PROVISIONAL_PFILTER: None | '1' | '2' (本番 '1')
    prov_1y_min: int = 999999999            # PROV_1Y_MIN
    prov_2y_min: int = 3                    # PROV_2Y_MIN
    # ── 直対評価 (Glicko2) の更新 ──
    warmup_m: int = 0                       # WARMUP_M
    bt_class_gate: bool = True              # SPSP_BT_CLASS_GATE (0 で off)
    bt_pair_gate: str = ''                  # SPSP_BT_PAIR_GATE: '' | 'both' | 'any'
    bt_size_k_mode: str = ''                # SPSP_BT_SIZE_K: '' | 'sqrtlog' | 'log2' | 'pow'
    bt_size_k_ref: float = 64.0             # SPSP_BT_SIZE_K_REF
    bt_size_k_pow: float = 0.25             # SPSP_BT_SIZE_K_POW
    bt_bo5_k: float = 0.0                   # SPSP_BT_BO5_K (0 = 無効)
    bt_bo1_k: float | None = 0.5            # SPSP_BT_BO1_K: 1先セットの変動量倍率 (本番 0.5, 2026-09-25〜。None/1 = 無効、0 も有効値)
    bt_lv3_weekday_k: float = 0.0           # SPSP_BT_LV3_WEEKDAY_K (0 = 無効)
    bt_oneside_k: float = 0.0               # SPSP_BT_ONESIDE_K (0 = 無効)
    bt_lvgap_k: float = 0.0                 # SPSP_BT_LVGAP_K (0 = 無効)
    bt_lvgap_min: int = 2                   # SPSP_BT_LVGAP_MIN
    bt_lvgap_scope: str = 'gain'            # SPSP_BT_LVGAP_SCOPE: 'gain' | 'both'
    bt_gamecount_lv: int = 0                # SPSP_BT_GAMECOUNT_LV (0 = 無効)
    bt_gamecount_max: int = 9               # SPSP_BT_GAMECOUNT_MAX
    bt_gamecount_mode: str = 'always'       # SPSP_BT_GAMECOUNT_MODE: 'always' | 'max'
    bt_gamecount_scope: str = 'up'          # SPSP_BT_GAMECOUNT_SCOPE: 'up' | 'all'
    g2_ffix: bool = False                   # SPSP_G2_FFIX=1 (既定 off = glicko2 ライブラリのバグ込み)
    # ── 直対評価 peak の窓と順位評価の月減衰 (v4/common.py の定数を configure() で上書き) ──
    bt_peak_days: int = 180                 # SPSP_BT_PEAK_DAYS (0 = peak 無効)
    decay_r: float = 0.96                   # SPSP_DECAY_R (月減衰率)
    # ── 直対評価 peak の減衰 ──
    peak_decay_k: float = 0.0               # SPSP_BT_PEAK_DECAY_K (Elo点/月, 0 = 無効)
    peak_decay_mode: str = 'lin'            # SPSP_BT_PEAK_DECAY_MODE: 'lin' | 'exp' | 'expint' | 'pw'
    peak_decay_lambda: float = 0.0          # SPSP_BT_PEAK_DECAY_LAMBDA
    peak_decay_base: float = 1500.0         # SPSP_BT_PEAK_DECAY_BASE
    peak_pw_spec: str = ''                  # SPSP_BT_PEAK_PW ("3:3,6:6,:12")
    # ── 実行の仕方 ──
    per_tour_lv: bool = True                # SPSP_PER_TOUR_LV=1 (本番 on, 2026-06-20〜)
    skip_per_match: bool = False            # SPSP_SKIP_PER_MATCH=1 (軽量ビルド: per-match 詳細を捨てメモリ節約)
    keep_no_match: bool = False             # SPSP_KEEP_NO_MATCH=1 (試合 0 件の大会を除外しない)
    pt_verify: bool = False                 # SPSP_PT_VERIFY=1
    pt_timing: bool = False                 # SPSP_PT_TIMING=1

    def state_repr(self) -> str:
        """学習状態 (チェックポイント / スナップショットキャッシュ) の照合に使う文字列。
        学習結果に効かない診断フラグ (pt_verify, pt_timing) は除く: 切り替えただけで全再学習にならないように。"""
        from dataclasses import replace
        return repr(replace(self, pt_verify=False, pt_timing=False))

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """環境変数から組み立てる (旧コードの読み方をそのまま再現。未設定時の値は旧モジュール既定)。"""
        g = (os.environ if environ is None else environ).get
        return cls(
            pfilter_mode=g('BT_LV_SHARED_PFILTER', 'ens'),
            bt_gating_mode=g('BT_GATING_MODE', 'lv'),
            lv2_weekend=g('BT_LV_SHARED_LV2_WEEKEND') == '1',
            lv2_nent_gt=_int_or_none(g('BT_LV_SHARED_LV2_NENT_GT')),
            lv3_weekend=g('BT_LV_SHARED_LV3_WEEKEND') == '1',
            lv3_nent_gt=_int_or_none(g('BT_LV_SHARED_LV3_NENT_GT')),
            lv4_nent_gt=_int_or_none(g('BT_LV_SHARED_LV4_NENT_GT')),
            lv3_tjpr_weekday=g('SPSP_LV3_TJPR_WEEKDAY') == '1',
            lv3_tjpr_weekday_w=_float_or_none(g('SPSP_LV3_TJPR_WEEKDAY_W')),
            lv6_nent_gt=_int_or_none(g('SPSP_LV6_NENT_GT')),
            lv6_topn=int(g('SPSP_LV6_TOPN', '32')),
            provisional_pfilter=g('PROVISIONAL_PFILTER'),
            prov_1y_min=int(g('PROV_1Y_MIN', '999999999')),
            prov_2y_min=int(g('PROV_2Y_MIN', '3')),
            warmup_m=int(g('WARMUP_M', '0')),
            bt_class_gate=g('SPSP_BT_CLASS_GATE', '1') != '0',
            bt_pair_gate=g('SPSP_BT_PAIR_GATE', ''),
            bt_size_k_mode=g('SPSP_BT_SIZE_K', ''),
            bt_size_k_ref=float(g('SPSP_BT_SIZE_K_REF', '64')),
            bt_size_k_pow=float(g('SPSP_BT_SIZE_K_POW', '0.25')),
            bt_bo5_k=float(g('SPSP_BT_BO5_K', '0') or 0.0),
            bt_bo1_k=_float_or_none(g('SPSP_BT_BO1_K', '0.5')),   # 本番既定 0.5。無効化は SPSP_BT_BO1_K=1
            bt_lv3_weekday_k=float(g('SPSP_BT_LV3_WEEKDAY_K', '0') or 0.0),
            bt_oneside_k=float(g('SPSP_BT_ONESIDE_K', '0') or 0.0),
            bt_lvgap_k=float(g('SPSP_BT_LVGAP_K', '0') or 0.0),
            bt_lvgap_min=int(g('SPSP_BT_LVGAP_MIN', '2')),
            bt_lvgap_scope=g('SPSP_BT_LVGAP_SCOPE', 'gain'),
            bt_gamecount_lv=int(g('SPSP_BT_GAMECOUNT_LV', '0') or 0),
            bt_gamecount_max=int(g('SPSP_BT_GAMECOUNT_MAX', '9')),
            bt_gamecount_mode=g('SPSP_BT_GAMECOUNT_MODE', 'always'),
            bt_gamecount_scope=g('SPSP_BT_GAMECOUNT_SCOPE', 'up'),
            g2_ffix=g('SPSP_G2_FFIX') == '1',
            bt_peak_days=int(g('SPSP_BT_PEAK_DAYS', '180')),
            decay_r=_decay_r_from_env(g('SPSP_DECAY_R')),
            peak_decay_k=float(g('SPSP_BT_PEAK_DECAY_K', '0')),
            peak_decay_mode=g('SPSP_BT_PEAK_DECAY_MODE', 'lin'),
            peak_decay_lambda=float(g('SPSP_BT_PEAK_DECAY_LAMBDA', '0') or 0),
            peak_decay_base=float(g('SPSP_BT_PEAK_DECAY_BASE', '1500')),
            peak_pw_spec=g('SPSP_BT_PEAK_PW', ''),
            per_tour_lv=g('SPSP_PER_TOUR_LV') == '1',
            skip_per_match=g('SPSP_SKIP_PER_MATCH') == '1',
            keep_no_match=g('SPSP_KEEP_NO_MATCH') == '1',
            pt_verify=g('SPSP_PT_VERIFY') == '1',
            pt_timing=g('SPSP_PT_TIMING') == '1',
        )


# 旧 build_v4_full.py:43-52 が import 前に os.environ へ書いていた本番構成 (互換テスト用に残す)
LEGACY_PRODUCTION_ENV = {
    'BT_LV_SHARED_PFILTER': 'ens', 'BT_LV_SHARED_LV2_NENT_GT': '8', 'BT_LV_SHARED_LV3_WEEKEND': '1',
    'BT_LV_SHARED_LV4_NENT_GT': '32', 'BT_GATING_MODE': 'lv', 'WARMUP_M': '0',
    'PROVISIONAL_PFILTER': '1', 'SPSP_PER_TOUR_LV': '1',
}

"""V4 共通 constants / regex.

build_tjpr_ranking.py (v2 prod) から純粋な constants / 正規表現を切り出したもの.
副作用なし、import 順序に依存しない、シンプルな値定義のみを置く.

(旧) build_tjpr_ranking.py 側は `from v4.common import *` で再 export しており、
既存の `from build_tjpr_ranking import OSK_BETA` 等の経路も維持される.
"""
from __future__ import annotations

import math
import re
import unicodedata
import datetime as dt
import json
from collections import defaultdict
from pathlib import Path



# ── Repo path constants (定義は v4/paths.py。ここは再 export) ──
from spsp.paths import REPO as _REPO_ROOT, EVENTS_ROOT, SITE_DATA as _SITE_DATA


# ── 海外タグ (= site/data/overseas.json, build/build_overseas_json.py 生成物) ──
# 海外タグ = 地域外 country (country.is_overseas_country) のランキング収録者 ∪ overseas_manual.json の country 未登録者
# (旧「海外上位」ホワイトリストを 2026-08 に国登録ベースへ拡張)。
# cascade Lv 判定 / ens_rank / tj_rank / bt_rank 計算で「JP-only counter top-N」を
# 構成する際の海外勢識別に使う. フロントエンドと同じファイルを参照することで
# source-of-truth を 1 箇所に保つ.
OVERSEAS_JSON_PATH = _SITE_DATA / "overseas.json"

# ── 出力の地域非依存な機械可読フィールド (2026-09 追加、既存フィールドは不変で追加のみ) ──
# フロント (別リポジトリ) が英語表示で使う。契約:
#   bracket_type = start.gg の phase bracketType そのまま (DOUBLE_ELIMINATION / SINGLE_ELIMINATION /
#                  ROUND_ROBIN / SWISS / MATCHMAKING / CUSTOM_SCHEDULE / …) or None。global_bracket_label の隣。
#   tags         = True な is_* フラグ名 (is_ 抜き) を FLAG_TAG_ORDER の順で並べた list。最後の is_* の直後。
#   series_id    = series 名の決定的 slug (NFKC → casefold → 英数字以外の連続を '-' → 両端の '-' を除去)。
FLAG_TAG_ORDER = ('weekend', 'weekend_real', 'smapa', 'pre', 'gf_missing', 'awaiting_resume',
                  'restricted', 'lower_class', 'uchi', 'special_rules', 'random_seed', 'dq')


_FLAG_TAG_KEYS = tuple(('is_' + k, k) for k in FLAG_TAG_ORDER)   # flag_tags 用 (呼ぶたびに 'is_' + k を作らない)


def flag_tags(rec: dict) -> list[str]:
    """rec の is_<name> が真のものを FLAG_TAG_ORDER の順で返す (rec に無いフラグは無視)。"""
    g = rec.get
    return [k for ik, k in _FLAG_TAG_KEYS if g(ik)]


def match_bracket_type(m) -> str | None:
    """試合オブジェクト (data_loader.Match / dict) の phase_bracket_type。無ければ None。"""
    if isinstance(m, dict):
        v = m.get('phase_bracket_type')
    else:
        v = getattr(m, 'phase_bracket_type', None)
    return v if v else None


def series_slug(name) -> str | None:
    """シリーズ名 → 決定的な ID (None → None)。NFKC・casefold・Unicode 文字/数字以外の連続→'-'・両端 '-' 除去。"""
    if name is None:
        return None
    s = unicodedata.normalize('NFKC', str(name)).casefold()
    s = re.sub(r'[\W_]+', '-', s)   # \W = Unicode 文字/数字以外 (+ '_' も区切り扱い) の連続を '-' 1 つに
    return s.strip('-')


# 灰色 (海外・計測中) の総合順位を所属 Lv の枠サイズでクランプするときの枠 (build / overlay で共用)
LV_TOPN_CAP = {2: 2048, 3: 1024, 4: 512, 5: 256}
# 過去最高順位 / 最高到達 Lv (all-time) の集計開始日 = EVO Japan 2020 (build / overlay で共用)
PEAK_ALLTIME_CUTOFF_DATE = dt.date(2020, 1, 24)


def _load_overseas_uids() -> frozenset[int]:
    p = OVERSEAS_JSON_PATH
    if not p.exists():
        # 生成物なので通常は存在するはず (無い = 生成前の checkout / 生成失敗)
        print(f"[WARN] {p} not found — overseas set empty "
              f"(run build/build_overseas_json.py)", flush=True)
        return frozenset()
    try:
        with open(p) as f:
            j = json.load(f)
        return frozenset(int(u) for u in (j.get('uids') or []))
    except Exception as e:
        print(f"[WARN] overseas.json read failed: {e}", flush=True)
        return frozenset()

OVERSEAS_UIDS: frozenset[int] = _load_overseas_uids()


# ── 計測中 (provisional) 判定 ──
# 直近 2 年の集計対象大会出場数が PROV_2Y_MIN_TOURS 未満なら「計測中」。
# 計測中は海外タグと同様に jp-counter (= 表示順位 / topn フィルタ) で JP カウントを
# 消費しない (= 灰色順位扱い)。海外リストとは別管理で、判定は時点依存 (= snapshot ごと).
PROV_WINDOW_DAYS = 730
PROV_2Y_MIN_TOURS = 3


def measured_uids_at_ts(all_t, t_meta_by_id, ts: int) -> set:
    """ts 時点で「計測済み」(= 直近 2 年の集計対象大会 ≥ PROV_2Y_MIN_TOURS) の uid 集合.

    集計対象大会 = precompute_banzuke の cache と同条件
    (is_non_serious / is_gf_missing / is_small を除外、開催が ts より前).
    """
    counts: dict[int, int] = defaultdict(int)
    lo = ts - PROV_WINDOW_DAYS * 86400
    for t in all_t:
        if not (lo < t.timestamp < ts):
            continue
        meta = t_meta_by_id.get(id(t)) or {}
        if meta.get('is_non_serious') or meta.get('is_gf_missing') or meta.get('is_small'):
            continue
        seen = set()
        for _, uid in t.standings:
            if uid in seen:
                continue
            seen.add(uid)
            counts[uid] += 1
    return {u for u, n in counts.items() if n >= PROV_2Y_MIN_TOURS}


# ── 同じ値の int を 1 つのオブジェクトにまとめる (値は同じ。メモリだけ減る。2026-10-17) ──
# build の順位表 (スナップショット 61 日 × 全選手 × 総合/順位評価/直対の各順位) は、日ごとに uid (str → int) と順位の
# int を作り直していた。北米 (20 万人) では同じ値の int オブジェクトが数千万個 (1 個 28 バイト) になっていた。
_UID_OF_STR: dict = {}
_INTS: list = []


def uid_of(s):
    """str(uid) → int(uid)。同じ uid は同じオブジェクトを返す (int(s) と値は同じ)。"""
    v = _UID_OF_STR.get(s)
    if v is None:
        v = int(s)
        _UID_OF_STR[s] = v
    return v


def small_int(n: int) -> int:
    """0 以上の n と同じ値の int (同じ n は同じオブジェクト)。順位の値に使う。"""
    if n >= len(_INTS):
        _INTS.extend(range(len(_INTS), max(n + 1, 2 * len(_INTS))))
    return _INTS[n]


def jp_counter_rank(raw_rank_dict: dict, overseas_uids: frozenset[int] = OVERSEAS_UIDS,
                    measured_uids: set | None = None) -> dict:
    """raw rank dict (uid -> rank 1..N) を「表示順位 (JP-only counter)」に変換.

    JP: 通算 jp_counter (1, 2, 3, ...)
    灰色 (= 海外 or 計測中): 相当順位 (= jp_counter_before + 1)
    多重灰色連続でも全員同じ番号 (= 直前 JP+1).
    raw rank 内での同順位はランダムにならないように、uid タイブレークを併用.

    measured_uids が与えられたら、それに含まれない uid (= 計測中) も
    海外と同様にカウント対象外として扱う.
    """
    sorted_uids = sorted(raw_rank_dict.items(), key=lambda x: (x[1], x[0]))
    result = {}
    jp = 0
    for u, _ in sorted_uids:
        gray = u in overseas_uids or (measured_uids is not None and u not in measured_uids)
        if gray:
            result[u] = small_int(jp + 1)
        else:
            jp += 1
            result[u] = small_int(jp)
    return result


def jp_topn_filter(rank_dict: dict, topn: int,
                    overseas_uids: frozenset[int] = OVERSEAS_UIDS,
                    measured_uids: set | None = None) -> set:
    """raw rank dict から「JP top-N + 該当灰色勢 (= 海外 / 計測中)」の UID 集合を返す.

    cascade の prev_pfilter 構成に使う. JP (計測済み) の count が topn に達するまで
    出現順に拾い、その間に挟まる灰色勢も一緒に入れる.
    """
    if topn is None or topn <= 0:
        return set(rank_dict.keys())
    sorted_uids = sorted(rank_dict.items(), key=lambda x: (x[1], x[0]))
    selected = set()
    jp = 0
    for u, _ in sorted_uids:
        if jp >= topn:
            break
        selected.add(u)
        gray = u in overseas_uids or (measured_uids is not None and u not in measured_uids)
        if not gray:
            jp += 1
    return selected


# ── Glicko2 / OpenSkill BT 共通スケール ──
OSK_BETA = 25.0 / 6.0
ELO_PER_UNIT = 400.0 / (math.log(10) * math.sqrt(2) * OSK_BETA)  # 29.48


# ── TJPR デフォルト / スケール ──
TJPR_DEFAULT_RATE = 1500.0
# サイト表示用 TJPR スコア (順位に影響しない線形変換)
# display = score × TJPR_ELO_SCALE  (offset なし、0 スタート)
# [EXPERIMENTAL] info+log2N² で raw_score が増大したので scale を 100 → 0.35 に再調整
# (top1 raw ~8589 → display ~3006)
TJPR_ELO_SCALE = 0.35
TJPR_ELO_OFFSET = 0.0
BT_ELO_OFFSET = 0.0  # Glicko2 raw rating はそのものが Elo スケール (1500 = baseline)


# ── 各大会の出場者中で最もレートの高い uid を opponent pool から除外する (= マイルドな
# 自己強化バイアス除去). True = 本採用 (eval で tjpr 単体 PairAcc +2.9pt, |SPR| -0.19 改善
# を確認, /tmp/eval_xt1_round_variants.md 等参照). --no-exclude-top1 で無効化可能.
EXCLUDE_TOP1 = True


# ── 大会名のパターン (プレ大会 / 特殊ルール / 身内 / 制限 / 下位クラス / スマパ / 実質平日) と暦 (土日祝) の判定は
#    2026-09-08 に smash_database (scripts/Japan/classify.py) へ移した。結果は各イベントの derived.json にあり、
#    spsp は spsp/derived.py で読むだけ。ここに残るのは判定結果をどう扱うかの定数だけ。
# 上野スマコミ: この参加者数未満の回のみ平日扱い (名前の判定 smacomi は derived.json、nent との合成は読む側).
# 閾値の定義元は smash_database (scripts/Japan/classify.py)。値は derive.py が region_rules.json に書く (ビルドは地域のコードを読まない)。
from spsp.derived import region_rules as _region_rules  # noqa: E402
SMACOMI_FORCE_WEEKDAY_MAX_NENT = _region_rules()["constants"]["SMACOMI_FORCE_WEEKDAY_MAX_NENT"]   # 地域は spsp.paths.REGION

# ── LV パラメータ / BT-gated 設定 ──
LV1_PARAMS = (1700, 350, 1, 0.3)
LV2_PARAMS = (1900, 700, 3, 0.3)
LV3_FILTER = {'min_nent_gt': 24, 'exclude_capped': True}  # top 1024 (制限/下位クラス除外)
LV4_FILTER = {'min_nent_gt': 24, 'weekend_only': True, 'exclude_capped': True}    # top 512: 中規模土日 (制限/下位クラス除外)
LV5_FILTER = {'min_nent_gt': 48, 'weekend_only': True, 'exclude_capped': True}    # top 256: メダリスト級 (制限/下位クラス除外)
GATED_M_TOUR = 20
SIGMA_FLOOR = 3.0  # BT-gated σ 下限 (覚醒選手の順位追従を強化、Top 8 τ +0.023 改善)
BT_PEAK_WINDOW_DAYS = 180   # 直対評価スコア = 過去 N 日間の最高 BT ordinal (peak180)。本番 180。
                            # 実験で窓を振るときは v4.settings.Settings.bt_peak_days → configure() で変える
                            # (0 = peak 無効 → take_snap の -inf fallback で現在レートに一致)。
                            # 内部レート (current ordinal) は別途保持してプレイヤーページに表示する.
                            # 読む側は `from v4 import common` して common.BT_PEAK_WINDOW_SEC を参照すること
                            # (名前を import で束縛すると configure() の変更が届かない)。
BT_PEAK_WINDOW_SEC = BT_PEAK_WINDOW_DAYS * 86400

# 順位評価の表 (precompute_banzuke の cache) の正規化シフト (Elo 相当)。build と rolling delta が使う。
# 以前は「その時点の全選手の最大内部レート × ELO_PER_UNIT − 2500」で、トップ選手のレートが動くたびに
# 過去の全大会の獲得点の表示値 (tjpr_pts_at / SPR 一覧の tjpr_raw / レーダー) が揺れていた (順位には効かない:
# カスケードは shift 0)。2026-09-07 に 2026-09-05 ビルドの値 (bt_per_tour の after 6 桁丸めの最大 94.561613) で
# 凍結 (ユーザー決定)。来季に 0 へ統一する予定 (表示値が変わる)。
GLOBAL_SHIFT_FROZEN = 287.7602298328802


# ── Inactivity σ inflation ──
# 不参加期間に応じて σ を増やす (= 復帰時の rating を慎重に扱う).
# Sigmoid 曲線で滑らかに 540 日 (1.5y) 中心に floor=3.0 → target=6.0 へ inflate.
# Sweep 結果 (sweep_inactivity_sigma.py): 全 scope で τ 改善 (top_32 で +0.0033).
INACTIVITY_SIGMA_TARGET = 6.0      # σ の上限 (これより大きくは増やさない)
INACTIVITY_SIGMA_INFLECTION_DAYS = 540.0  # sigmoid の中央 (= σ が target と floor の中間に到達する日数)
INACTIVITY_SIGMA_SCALE_DAYS = 180.0       # sigmoid の幅 (大きいほどなだらかに増加)
INACTIVITY_SIGMA_GRACE_DAYS = 30          # この日数以内は何もしない


# ── 過去ランク snapshot を取る日数 (今日から N 日前) ──
# 2 年分・1 ヶ月刻み (30 日 × 1..24)
# SNAPSHOT_DAYS: チャートで見せる過去スナップショットの日数列.
# 3ヶ月内は週次、3-6ヶ月は隔週、6ヶ月-2年は月次、2年以降は四半期 (全期間 ~8 年カバー).
# 約 59 点 + 評価日 = 60 点.
SNAPSHOT_DAYS = (
    list(range(7, 91, 7))                       # 7, 14, 21, ..., 84
    + list(range(91, 181, 14))                  # 91, 105, ..., 175 (隔週)
    + [180 + 30 * i for i in range(1, 19)]      # 210, 240, ..., 720 (月次)
    + [720 + 90 * i for i in range(1, 25)]      # 810, 900, ..., 2880 (四半期, 6 年分)
)


# ── 履歴スナップショットの固定グリッド (2026-09-13〜) ──
# 以前は SNAPSHOT_DAYS (評価日から N 日前) で点を置いていたので、評価日が 1 日進むと全点が動き、
# players/*.json の history が毎ビルド全員分書き換わっていた。点を暦に固定すると、同じ日の
# スナップショットは入力が同じなら同じ値になり (history/<uid>.json が安定する)、
# スナップショットのキャッシュ (learner._SnapCache、鍵 = 時刻 + 台帳ハッシュ) も日をまたいで効く。
# 密度は SNAPSHOT_DAYS と同じ: 3 か月は週次、6 か月まで隔週、2 年まで月次、それ以降四半期。
#   週次:   月曜
#   隔週:   偶数 ISO 週の月曜 (固定エポック 2018-01-01 (月) から数えて偶数週)
#   月次:   各月 1 日以降の最初の月曜
#   四半期: 1/4/7/10 月 1 日以降の最初の月曜
# 評価日が進むと密な層から点が抜ける (週 1 回)。SNAPSHOT_DAYS は実験ハーネス用に残す。
_GRID_EPOCH = dt.date(2018, 1, 1)   # 月曜。隔週の偶奇の基準
_GRID_WEEKLY_DAYS = 91
_GRID_BIWEEKLY_DAYS = 180
_GRID_MONTHLY_DAYS = 720
_GRID_MAX_DAYS = 2880


def _first_monday_on_or_after(d: "dt.date") -> "dt.date":
    return d + dt.timedelta(days=(7 - d.weekday()) % 7)


def snapshot_grid_dates(eval_date) -> list:
    """評価日 (date / datetime) 以前の固定グリッド日を新しい順に返す (評価日当日は含まない)。"""
    if isinstance(eval_date, dt.datetime):
        eval_date = eval_date.date()
    out = set()
    # 週次 / 隔週: 月曜を遡る
    d = eval_date - dt.timedelta(days=eval_date.weekday() or 7)   # 直近の月曜 (当日が月曜なら 7 日前 = 当日は day 0 で別扱い)
    while (eval_date - d).days <= _GRID_BIWEEKLY_DAYS:
        age = (eval_date - d).days
        if age <= _GRID_WEEKLY_DAYS or ((d - _GRID_EPOCH).days // 7) % 2 == 0:
            out.add(d)
        d -= dt.timedelta(days=7)
    # 月次 / 四半期: 各月 1 日以降の最初の月曜
    y, m = eval_date.year, eval_date.month
    while True:
        first = dt.date(y, m, 1)
        md = _first_monday_on_or_after(first)
        if md < eval_date:
            age = (eval_date - md).days
            if age > _GRID_MAX_DAYS:
                break
            if age > _GRID_BIWEEKLY_DAYS and (age <= _GRID_MONTHLY_DAYS or m in (1, 4, 7, 10)):
                out.add(md)
        m -= 1
        if m == 0:
            y -= 1; m = 12
        if (eval_date - dt.date(y, m, 1)).days > _GRID_MAX_DAYS + 40:
            break
    return sorted(out, reverse=True)


def snapshot_grid_days(eval_date) -> list:
    """snapshot_grid_dates を「評価日から何日前か」(int, 新しい順) で返す。build は内部で日数キーを使う。"""
    if isinstance(eval_date, dt.datetime):
        eval_date = eval_date.date()
    return [(eval_date - d).days for d in snapshot_grid_dates(eval_date)]


# 既知シリーズの正規化マッピング (_KNOWN_SERIES / _EXPANDED_PAT) は 2026-09-09 に
# smash_database の scripts/Japan/naming.py へ移した。


# ── Lookback windows ──
LOOKBACK_DAYS = 365 * 8         # 8 年 (EVO Japan 2020 / ウメブラ SP3+ までカバー、age_decay で古いデータは自然に減衰)
JJPR_LOOKBACK_DAYS = 365 * 2    # JJPR は 2 年で固定 (= 1 シーズン分の集計を想定)
JJPR_TOP_K = 6                  # JJPR の集計対象 (上位 K 大会の合計)

# ============================================================
# 副作用なしユーティリティ (反復2 で build_tjpr_ranking から move)
# ============================================================

# ─── from build_tjpr_ranking (757-771): _TZ and _DAY_OVERRIDES ───
# 開催日の暦日は地域モジュールの TIMEZONE で切る (日本 = Asia/Tokyo。以前は +9 固定)。
from zoneinfo import ZoneInfo as _ZoneInfo  # noqa: E402
_TZ = _ZoneInfo(_region_rules()["constants"]["TIMEZONE"])

# Event-specific DAY boundary overrides.
# phase_group の startAt が start.gg 側で NULL かつ phase_name に DAY hint も無い大会向け.
# event_id → list of (phase_name_substring, day_num). 上から match した順に適用.
_DAY_OVERRIDES: dict[int, list[tuple[str, int]]] = {
    # ウメブラSP#10 (2024-01-06〜08, 3 days). 開催公式情報: TOP384 から DAY 2, TOP16 から DAY 3.
    949135: [
        ('TOP2048', 1),
        ('TOP384', 2),
        ('TOP48', 2),
        ('TOP16', 3),
        ('TOP8', 3),
    ],
}


# ─── from build_tjpr_ranking (774-887): compute_per_player_max_day ───
def _collect_phase_group_info(matches, dq_records):
    """matches + dq_records から phase_group の (startAt, phase_name) を集計."""
    pg_start: dict[int, int] = {}
    pg_name: dict[int, str] = {}
    for m in matches:
        if getattr(m, 'is_class', False):
            continue
        pgid = getattr(m, 'phase_group_id', None)
        if pgid is None:
            continue
        pg_name.setdefault(int(pgid), getattr(m, 'phase_name', None) or '')
        sa = getattr(m, 'phase_group_start_at', None)
        if sa is not None:
            pg_start.setdefault(int(pgid), int(sa))
    for _uid, _pgid, _sa in dq_records:
        if _pgid is None or _sa is None:
            continue
        pg_start.setdefault(int(_pgid), int(_sa))
    return pg_start, pg_name


def _assign_pg_days_by_start_at(pg_start):
    """phase_group.startAt (JST 日付) ベースで pg → day 番号を割当."""
    pg_day: dict[int, int] = {}
    day_dates: list[dt.date] = []
    for pgid, sa in sorted(pg_start.items(), key=lambda kv: kv[1]):
        d = dt.datetime.fromtimestamp(sa, tz=_TZ).date()
        if d not in day_dates:
            day_dates.append(d)
        pg_day[pgid] = day_dates.index(d) + 1
    return pg_day


def _assign_pg_days_by_phase_name(pg_name, pg_day):
    """phase_name の "[DAY N]" / "[FINAL DAY]" / "(Day N)" / "Day N" を fallback として pg_day に追加."""
    name_day_pgs: dict[int, int] = {}  # pgid → day_num (-1 = FINAL DAY sentinel)
    for pgid, pn in pg_name.items():
        if pgid in pg_day:
            continue
        m_d = re.search(r'\[\s*DAY\s*(\d+)\s*\]|[(（]\s*Day\s*(\d+)|\bDay\s*(\d+)\b', pn, re.IGNORECASE)
        if m_d:
            name_day_pgs[pgid] = int(m_d.group(1) or m_d.group(2) or m_d.group(3))
        elif 'FINAL DAY' in pn.upper():
            name_day_pgs[pgid] = -1
    if not name_day_pgs:
        return
    numbered = set(pg_day.values()) | set(d for d in name_day_pgs.values() if d > 0)
    final_resolved = (max(numbered) + 1) if numbered else 3
    for pgid, d in name_day_pgs.items():
        pg_day[pgid] = final_resolved if d == -1 else d


def _apply_event_day_overrides(event_id, pg_name, pg_day):
    """_DAY_OVERRIDES に登録された event_id について phase_name のキーワード一致で pg_day を上書き."""
    if event_id is None or event_id not in _DAY_OVERRIDES:
        return
    for pgid, pn in pg_name.items():
        for keyword, day_num in _DAY_OVERRIDES[event_id]:
            if keyword in pn:
                pg_day[pgid] = day_num
                break


def _fill_unassigned_pgs_by_event_total(t, pg_name, pg_day):
    """attr.json から event 総日数を読み取り、2-day / 3-day+ で未割当 phase を Day 2 に充填."""
    if not pg_day:
        return
    try:
        attr = json.loads((Path(t.event_path) / 'attr.json').read_bytes())
        ts = attr.get('timestamp'); et = attr.get('end_timestamp')
        if ts and et:
            sd = dt.datetime.fromtimestamp(ts, tz=_TZ).date()
            ed = dt.datetime.fromtimestamp(et, tz=_TZ).date()
            event_total_days = (ed - sd).days + 1
        else:
            event_total_days = 0
    except Exception:
        event_total_days = 0
    # 2-day: 未割当 → Day 2. 3-day+: 区別不能なので Day 2 (保守的に最低割当).
    if event_total_days >= 2:
        for pgid in pg_name:
            if pgid not in pg_day:
                pg_day[pgid] = 2


def _build_player_max_day_from_pg(matches, dq_records, pg_day):
    """pg_day と matches/dq_records から player の最大 DAY 番号を集計."""
    player_max_day: dict[int, int] = {}
    for m in matches:
        if getattr(m, 'is_class', False):
            continue
        pgid = getattr(m, 'phase_group_id', None)
        if pgid is None:
            continue
        day = pg_day.get(int(pgid))
        if day is None:
            continue
        for uid in (m.winner_id, m.loser_id):
            if uid is None:
                continue
            if day > player_max_day.get(uid, 0):
                player_max_day[uid] = day
    # DAY X 進出 → 初戦 DQ のプレイヤーも拾う
    for uid, pgid, _sa in dq_records:
        if pgid is None:
            continue
        day = pg_day.get(int(pgid))
        if day is None:
            continue
        if day > player_max_day.get(uid, 0):
            player_max_day[uid] = day
    return player_max_day


def compute_per_player_max_day(t, event_id: int | None = None) -> dict[int, int]:
    """大規模 multi-day 大会の各プレイヤーの最大 DAY 番号を返す (main bracket のみ).
    主: phase_group.startAt (JST 日付) ベース.
    副: phase_name の "[DAY N]" / "[FINAL DAY]" を fallback として利用
        (= startAt が start.gg 側で NULL の phase は phase_name から DAY 抽出).
    DAY X 進出 → 初戦 DQ のプレイヤーも拾う. 下位クラスは除外.
    """
    matches = getattr(t, 'matches', []) or []
    dq_records = getattr(t, 'dq_match_records_main', []) or []
    pg_start, pg_name = _collect_phase_group_info(matches, dq_records)
    pg_day = _assign_pg_days_by_start_at(pg_start)
    _assign_pg_days_by_phase_name(pg_name, pg_day)
    _apply_event_day_overrides(event_id, pg_name, pg_day)
    _fill_unassigned_pgs_by_event_total(t, pg_name, pg_day)
    if not pg_day or max(pg_day.values()) < 2:
        return {}
    return _build_player_max_day_from_pg(matches, dq_records, pg_day)



# ─── from build_tjpr_ranking (958-979): _build_de_w2w_bounds + _DE_W2W_BOUNDS + placement_to_w2w ───
def _build_de_w2w_bounds(max_w2w=30):
    bounds = []
    for n in range(max_w2w + 1):
        if n == 0: p = 1
        elif n == 1: p = 2
        elif n % 2 == 0: p = 2 ** (n // 2) + 1
        else: p = 3 * 2 ** ((n - 3) // 2) + 1
        bounds.append((p, n))
    return bounds

_DE_W2W_BOUNDS = _build_de_w2w_bounds()

def placement_to_w2w(place: int) -> int:
    if place is None or place < 1:
        return 0
    result = 0
    for p, w in _DE_W2W_BOUNDS:
        if p <= place:
            result = w
        else:
            break
    return result


# ─── 実質休日 (= 暦上は平日だが計算上休日として扱う) ───
# お盆 (8/13〜15) と年末年始 (12/26〜1/5) は祝日ではないが休みの人が多く、
# 大規模大会が平日開催されることがある (例: growup_6 = 2026-08-14 金 96人,
# スマバトSP#34 = 2023-01-04 水 157人, 渋谷BeeSmash BIG 2 = 2024-12-27 金 96人)。
# この期間に開催日がかかる nent >= 80 の大会 (プレ大会 / 実質平日シリーズを除く) は
# 休日扱いにする。期間の判定 (calendar.is_force_weekend_period) は derived.json、合成は meta.py 等で行う。
# 閾値の定義元は smash_database (scripts/Japan/classify.py)。
FORCE_WEEKEND_MIN_NENT = _region_rules()["constants"]["FORCE_WEEKEND_MIN_NENT"]   # 地域は spsp.paths.REGION


# ─── from build_tjpr_ranking (1025-1026): td_exp ───
# 月減衰率。実験で変えるときは v4.settings.Settings.decay_r → configure()。
_DECAY_R_DEFAULT = 0.96  # 月4%減衰. 反復46 で 3%/4%/5% sweep 後に採用 (= 4 シーズン eval で 3% と同等以上)
_DECAY_R = _DECAY_R_DEFAULT


def configure(bt_peak_days: int, decay_r: float) -> None:
    """実験用の 2 定数を設定から反映する (v4/learner.apply_settings が呼ぶ)。本番値は既定のまま。"""
    global BT_PEAK_WINDOW_DAYS, BT_PEAK_WINDOW_SEC, _DECAY_R
    BT_PEAK_WINDOW_DAYS = int(bt_peak_days)
    BT_PEAK_WINDOW_SEC = BT_PEAK_WINDOW_DAYS * 86400
    _DECAY_R = float(decay_r)


def current_decay_r() -> float:
    """適用中の月減衰率 (meta.json に出す。表示側が tjpr_w = raw × pos × r^(days/30) を再計算するのに使う)。"""
    return _DECAY_R


def td_exp(days: int, r: float = None) -> float:
    if r is None:
        r = _DECAY_R
    return r ** (days / 30.0)


# ─── from build_tjpr_ranking (1162-1189): _bracket_order_key ───
def _bracket_order_key_parts(label, round_text, top_x):
    """トーナメント内の試合を実際の進行順に近い順序で並べるための sort key (本体).

    Match オブジェクト用の _bracket_order_key と、JSON 化済み dict 用の
    overlay._bracket_order_key_dict がどちらもこれを呼ぶ (以前は 2 重に書かれていた)。

    順序: ① 予選/総当たり/カテゴリ系 (global_bracket_label なし、または '総当たり' 等) →
          ② WB を Top X 大きい順 (= 早期ラウンド先) → ③ LB 同様 → ④ GF (Reset は後).
    同 (side, top_x) 内は入力順 (sorted は stable). 予選も入力順で固定.

    class prefix ("B-" 等) は body 部分で side 判定 (= main と同じ階層内で順序付け).
    is_class フィルタは別途行う想定.
    """
    label = label or ''
    rt = (round_text or '').lower()
    # class prefix を剥がして本体で side 判定 (= B-Winners TOP 64 → Winners 扱い).
    body = label
    if len(body) >= 2 and body[0] in ('B', 'C', 'D', 'E') and body[1] == '-':
        body = body[2:]
    if 'grand final' in rt or body == 'Grand Final':
        side = 3
    elif body.startswith('Winners'):
        side = 1
    elif body.startswith('Losers'):
        side = 2
    else:
        # bracket 情報なし = 予選総当たり / pool / カテゴリ系 / 古いデータ. 優先で処理.
        side = 0
    top_x = top_x if top_x is not None else 0
    gf_reset = 1 if 'reset' in rt else 0
    return (side, -top_x, gf_reset)


def _bracket_order_key(m):
    """Match オブジェクト用 (v4.learn / build の sort key)。"""
    return _bracket_order_key_parts(m.global_bracket_label, m.round_text, m.global_top_x)


# ─── from build_tjpr_ranking (1192-1234): compute_losers_runs ───
def compute_losers_runs(matches):
    """各プレイヤーの「ルーザーズラン」(= WB敗北後の LB 連勝数 + GF 勝利数) を計算.

    定義 (= 一般的な DE 用語):
      - 勝者側で 1 度負ける (= WB の non-GF 試合で loser)
      - その後 LB で連勝
      - LB 敗退 or トーナメント優勝で終了
      - 連勝数 = LB 勝利 + (LBから来た場合の GF 勝利, GF + Reset で最大 2)

    Returns: {uid: run_length}  (= WB 敗北履歴がある player のみ; 0 は含まない)
    class bracket (B/C/D-class) は除外 (= 別ブラケット扱い).
    """
    stats = defaultdict(lambda: {'wb_loss': False, 'lb_wins': 0, 'gf_wins': 0})
    for m in matches:
        if getattr(m, 'is_class', False):
            continue
        # 予選総当たり (= ROUND_ROBIN) は WB/LB 構造が無いので除外.
        # RR pool のラウンド番号は positive だが「WB 敗北」「LB 勝利」とは別概念.
        if getattr(m, 'phase_bracket_type', None) == 'ROUND_ROBIN':
            continue
        r = m.round
        rt = (m.round_text or '').lower()
        is_gf = 'grand final' in rt
        if is_gf:
            if m.winner_id is not None:
                stats[m.winner_id]['gf_wins'] += 1
            # GF loser: not counted as WB loss (= GF はもう LB に落ちない).
            continue
        if r is None:
            continue
        if r < 0:
            if m.winner_id is not None:
                stats[m.winner_id]['lb_wins'] += 1
        elif r > 0:
            if m.loser_id is not None:
                stats[m.loser_id]['wb_loss'] = True
    out = {}
    for uid, st in stats.items():
        if st['wb_loss']:
            run = st['lb_wins'] + st['gf_wins']
            if run > 0:
                out[uid] = run
    return out

# ───────────────── 学習も使う判定 (spsp.build / spsp.score から移した、2026-10-23) ─────────────────
# snap のキャッシュの鍵は書き出し側のファイル (build.py / score.py など) を含めないので、学習・スナップショットの値に
# 関わるものはこちらに置く (spsp/learner._SNAP_CODE_EXCLUDE、tests/meta/test_snap_cache_scope.py)。
import numpy as _np  # noqa: E402


def t_passes(meta: dict, filt: dict | None) -> bool:
    if filt is None:
        return True
    if filt.get('min_nent_gt') is not None and meta['nent'] <= filt['min_nent_gt']:
        return False
    if filt.get('exclude_smapa') and meta['is_smapa']:
        return False
    if filt.get('weekend_only') and not meta['is_weekend']:
        return False
    if filt.get('exclude_capped') and (meta.get('is_restricted') or meta.get('is_lower_class')):
        return False
    return True


# 各レベル「+」(= そのレベルバンドの上位半分) の累積 rank cutoff。
# Lv5 1-256→128 / Lv4 257-512→384 / Lv3 513-1024→768 / Lv2 1025-2048→1536 /
# Lv1 2049-(4096)→3072 (= 2048+1024)。
LV_PLUS_CUTOFF = {5: 128, 4: 384, 3: 768, 2: 1536, 1: 3072}


def level_tier_values(lv, rank):
    lv = _np.asarray(lv, dtype=_np.int64); rank = _np.asarray(rank, dtype=_np.int64)
    v = _np.zeros(len(lv), dtype=_np.int32)
    ok = (lv > 0) & (rank > 0)
    m5 = ok & (lv == 5)
    v[m5] = 140000
    for cut, prio in ((128, 145000), (64, 150000), (32, 155000), (16, 160000), (8, 170000)):
        v[m5 & (rank <= cut)] = prio
    for l, base in ((4, 100000), (3, 80000), (2, 65000)):
        m = ok & (lv == l)
        v[m] = base
        v[m & (rank <= LV_PLUS_CUTOFF[l])] = base + 5000
    m1 = ok & (lv == 1) & (rank <= LV_PLUS_CUTOFF[1])
    v[m1] = 60000
    return v


def _rolling_lv2_raw_per_tour(cache):
    """Compute LV2 raw_pts per (tid, uid) from precomputed banzuke cache.

    Returns:
        dict[tid -> (per_dict, days)] where per_dict = {uid: raw_pts}.
    """
    LV2_BASE, LV2_FLOOR, _, _ = LV2_PARAMS
    eps_lv2 = 0.01
    out = {}
    for t_c in cache:
        clamped = _np.maximum(t_c['norm'], LV2_FLOOR)
        rp = []
        for idx in t_c['rounds']:
            if len(idx):
                r = clamped[idx]
                g = 1.0 - 1.0 / (1.0 + _np.power(10.0, (r - LV2_BASE) / 400.0))
                rp.append(eps_lv2 + float(_np.maximum(0, g).mean()))
            else:
                rp.append(eps_lv2)
        per = {}
        for uid_, rw_, _ in t_c['players']:
            per[uid_] = float(sum(rp[:rw_])) if rw_ > 0 else 0.0
        out[t_c['t_id']] = (per, t_c.get('days', 0))
    return out

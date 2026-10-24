"""配信 JSON の分割: 選手レコードを「安定部分」「揮発部分」「履歴」に分ける (docs/refactor/04_player_json_split.md)。

build (spsp/output.py) が最終形の detail record (overlay 適用後、json_roundtrip 済) をここに通し、
  players/<uid>.json          安定部分 (その選手が出場したときしか変わらない)
  history/<uid>.json          固定グリッド日付のランク履歴
  players_current.json        全選手の揮発部分 (順位・スコア・ピーク・百分位。毎ビルド 1 ファイル)
に書く。表示側は spsp-web の site/js/player_data.js の assemble() で元の形に戻す。両者は鏡なので、
片方を変えたらもう片方も変えること (spsp-web の tests/split/test_assemble.mjs が突き合わせる。fixture は tests/split/make_fixture.py で作って spsp-web に置く)。

安定部分から落とすもの (評価日に依存するか、全員の順位が動くたびに変わるもの):
  - ranks / scores の一部 / metadata の期間カウント / tjpr_lv_breakdown / radar の百分位 / peak_ranks(_1y)
      → 揮発部分へ
  - history → history/<uid>.json (d = 評価日から N 日前 → date = 暦日)
  - tournaments[] / top_tjpr_contribs[] の tjpr_w, tjpr_age → 表示側が tjpr_raw × tjpr_pos × r^(days/30) で復元
  - top_spr[] の tjpr_w / tjpr_raw / tjpr_lv / tjpr_counted → 表示側が tournaments[] の同じ大会から引く
      (以前は base フェーズの値が残っていて tournaments[] と食い違っていた。復元後は tournaments[] と同じ値になる)
  - recent_spr → 削除 (どのページも読んでいない)
"""
from __future__ import annotations

import datetime as dt

# scores のうち揮発するもの。残り (bt_internal_* / bt_all_* / bt_internal_d_last) は出場時しか変わらない
CURRENT_SCORE_KEYS = (
    'tjpr_score', 'tjpr_elo', 'tjpr_level',
    'bt_gated_ordinal', 'bt_gated_elo',
    'ensemble_avg_rank', 'ensemble_avg_score',
    'shared_cascade_lv', 'shared_cascade_ens_r',
)
# metadata のうち揮発するもの (期間カウントは評価日基準の窓)。bt_weekday_included / debut は安定
CURRENT_META_KEYS = (
    'tour_count_3y', 'matches_count_3y', 'tour_count_by_period', 'match_count_by_period', 'provisional',
)
# radar のうち百分位 (母集団で決まる)。*_raw は安定
RADAR_PCT_KEYS = ('tjpr_pt', 'spr_upper', 'spr_stability', 'bt_pt', 'uf_upper', 'uf_stability')
# top-level で丸ごと揮発へ移すもの
CURRENT_WHOLE_KEYS = ('ranks', 'tjpr_lv_breakdown', 'peak_ranks', 'peak_ranks_1y')

_ENTRY_DECAY_KEYS = ('tjpr_age', 'tjpr_w')
_TOP_SPR_DERIVED_KEYS = ('tjpr_w', 'tjpr_raw', 'tjpr_lv', 'tjpr_counted')


def _without(d: dict, keys) -> dict:
    return {k: v for k, v in d.items() if k not in keys}


def _only(d: dict, keys) -> dict:
    return {k: d[k] for k in keys if k in d}


def split_detail(rec: dict, eval_date: dt.datetime) -> tuple[dict, dict, list]:
    """最終形の detail record → (安定部分, 揮発部分, 履歴行)。rec は変更しない。"""
    eval_day = eval_date.date() if isinstance(eval_date, dt.datetime) else eval_date
    stable: dict = {}
    current: dict = {}
    history_rows: list = []
    for k, v in rec.items():
        if k in CURRENT_WHOLE_KEYS:
            current[k] = v
        elif k == 'scores':
            current['scores'] = _only(v or {}, CURRENT_SCORE_KEYS)
            stable['scores'] = _without(v or {}, CURRENT_SCORE_KEYS)
        elif k == 'metadata':
            current['metadata'] = _only(v or {}, CURRENT_META_KEYS)
            stable['metadata'] = _without(v or {}, CURRENT_META_KEYS)
        elif k == 'radar':
            current['radar'] = _only(v or {}, RADAR_PCT_KEYS)
            stable['radar'] = _without(v or {}, RADAR_PCT_KEYS)
        elif k == 'history':
            for h in v or []:
                row = {}
                for hk, hv in h.items():
                    if hk == 'd':
                        row['date'] = (eval_day - dt.timedelta(days=int(hv))).isoformat()
                    else:
                        row[hk] = hv
                history_rows.append(row)
        elif k == 'recent_spr':
            continue
        elif k in ('tournaments', 'top_tjpr_contribs'):
            stable[k] = [_without(e, _ENTRY_DECAY_KEYS) for e in (v or [])]
        elif k == 'top_spr':
            stable[k] = [_without(e, _TOP_SPR_DERIVED_KEYS) for e in (v or [])]
        else:
            stable[k] = v
    return stable, current, history_rows


# 大会ドキュメントの standings[] / highlights.*[] から落とす「参加者の現在状態」。
# 当時の値 (pretour_ranks / at_lv / tjpr_pts_at / rank_delta_* / perf_rank) と tjpr_raw は残す。
TOURNAMENT_ENTRY_CURRENT_KEYS = ('global_ranks', 'tjpr_w', 'tjpr_lv', 'tjpr_counted', 'tjpr_by_lv')


def split_tournament_doc(doc: dict) -> dict:
    """最終形の大会ドキュメント → 現在状態を落としたもの (安定)。doc は変更しない。"""
    out = {}
    for k, v in doc.items():
        if k == 'standings':
            out[k] = [_without(s, TOURNAMENT_ENTRY_CURRENT_KEYS) for s in (v or [])]
        elif k == 'highlights':
            out[k] = {hk: ([_without(e, TOURNAMENT_ENTRY_CURRENT_KEYS) for e in hv] if isinstance(hv, list) else hv)
                      for hk, hv in (v or {}).items()}
        else:
            out[k] = v
    return out


# players_current.json は 1 ファイルに全選手が入るので、キー名を繰り返さない列形式にする
# (入れ子 dict のままだと 22 MB、列形式で 5 MB 程度)。列の並びは CURRENT_COLUMNS。
# 表示側 (spsp-web の site/js/player_data.js の decodeCurrentRow) が同じ並びで戻す。
#   peak_ranks.*.when は "N日前" / "now" → 日数 (0 = now) で持つ
#   optional な列 (shared_cascade_* / tjpr_lv_breakdown.*) は null = キー無し
CURRENT_COLUMNS = (
    'ranks.ensemble', 'ranks.tjpr', 'ranks.bt_gated', 'ranks.bt_flat',
    'scores.tjpr_score', 'scores.tjpr_elo', 'scores.tjpr_level', 'scores.bt_gated_ordinal', 'scores.bt_gated_elo',
    'scores.ensemble_avg_rank', 'scores.ensemble_avg_score', 'scores.shared_cascade_lv', 'scores.shared_cascade_ens_r',
    'metadata.tour_count_3y', 'metadata.matches_count_3y',
    'metadata.tour_count_by_period.all', 'metadata.tour_count_by_period.3y', 'metadata.tour_count_by_period.2y',
    'metadata.tour_count_by_period.1y', 'metadata.tour_count_by_period.6m', 'metadata.tour_count_by_period.3m',
    'metadata.match_count_by_period.all', 'metadata.match_count_by_period.3y', 'metadata.match_count_by_period.2y',
    'metadata.match_count_by_period.1y', 'metadata.match_count_by_period.6m', 'metadata.match_count_by_period.3m',
    'metadata.provisional',
    'tjpr_lv_breakdown.1', 'tjpr_lv_breakdown.2', 'tjpr_lv_breakdown.3', 'tjpr_lv_breakdown.4', 'tjpr_lv_breakdown.5',
    'radar.tjpr_pt', 'radar.spr_upper', 'radar.spr_stability', 'radar.bt_pt', 'radar.uf_upper', 'radar.uf_stability',
    'peak_ranks.ensemble.rank', 'peak_ranks.ensemble.when', 'peak_ranks.tjpr.rank', 'peak_ranks.tjpr.when',
    'peak_ranks.bt_gated.rank', 'peak_ranks.bt_gated.when',
    'peak_ranks_1y.ensemble.rank', 'peak_ranks_1y.ensemble.when', 'peak_ranks_1y.tjpr.rank', 'peak_ranks_1y.tjpr.when',
    'peak_ranks_1y.bt_gated.rank', 'peak_ranks_1y.bt_gated.when',
)
CURRENT_OPTIONAL_COLUMNS = frozenset(
    ('scores.shared_cascade_lv', 'scores.shared_cascade_ens_r')
    + tuple(f'tjpr_lv_breakdown.{i}' for i in range(1, 6))
)
_MISSING = object()


def _get_path(d, path):
    cur = d
    for part in path.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _when_to_days(w):
    if w is None or w is _MISSING:
        return None
    if w == 'now':
        return 0
    if isinstance(w, str) and w.endswith('日前'):
        return int(w[:-2])
    raise ValueError(f'peak_ranks.when の形式が想定外: {w!r}')


def encode_current_row(cur: dict) -> list:
    """揮発部分 (split_detail の戻り) → CURRENT_COLUMNS 順の list。"""
    row = []
    for col in CURRENT_COLUMNS:
        v = _get_path(cur, col)
        if col.endswith('.when'):
            v = _when_to_days(v)
        elif v is _MISSING:
            if col not in CURRENT_OPTIONAL_COLUMNS:
                raise KeyError(f'揮発部分に {col} が無い')
            v = None
        row.append(v)
    return row


def current_file_dict(eval_date: dt.datetime, players: dict, decay_r: float) -> dict:
    """players_current.json の中身。players = {uid(int): 揮発部分 (dict)、または encode_current_row 済みの list}。"""
    return {
        'format': 'spsp-current/1',
        'eval_date': eval_date.date().isoformat(),
        'eval_ts': int(eval_date.replace(hour=23, minute=59, second=59).timestamp()),
        'decay_r': decay_r,
        'columns': list(CURRENT_COLUMNS),
        'players': {str(u): (p if type(p) is list else encode_current_row(p)) for u, p in players.items()},
    }

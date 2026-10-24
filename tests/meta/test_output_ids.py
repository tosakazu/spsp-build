"""ビルド出力の地域非依存な機械可読フィールド (bracket_type / tags / series_id / generated_at の offset) のテスト。

2026-09 追加。既存の日本語文字列・is_* フラグはそのまま残し、追加のみ (既存フィールドの値と順は不変)。
表示側 (別リポジトリ ~/spsp-site) の英語表示はこの契約に対して書かれている:
  bracket_type  global_bracket_label を持つ全 match record。start.gg の phase bracketType そのまま
                (DOUBLE_ELIMINATION / SINGLE_ELIMINATION / ROUND_ROBIN / SWISS / …) or null。global_bracket_label の直後。
  tags          大会 record (data/tournaments.json、players の tournaments[] / top_tjpr_contribs 等、tournaments/<eid>.json)。
                True な is_* の名前 (is_ 抜き) を固定順 weekend, weekend_real, smapa, pre, gf_missing, awaiting_resume,
                restricted, lower_class, uchi, special_rules, dq で並べた list (その record に無いフラグは出ない)。
                最後の is_* の直後。
  series_id     series 名の隣 (players の top_tours[]、tournaments.json、series.json)。
                spsp.common.series_slug: NFKC → casefold → Unicode 文字/数字以外の連続を '-' → 両端の '-' 除去。series が null なら null。
  generated_at  meta.json は地域 TIMEZONE の offset 付き ISO 8601 (series.json / tournaments.json の 'Z' 付き UTC はそのまま)。

実行: OPENBLAS_NUM_THREADS=1 .venv/bin/python3 tests/meta/test_output_ids.py
(リポジトリルートから。unittest 等は使わず plain assert。smash_database の地域モジュールは SPSP_REGION の既定で読む)
"""
import datetime as dt
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from spsp.common import FLAG_TAG_ORDER, flag_tags, match_bracket_type, series_slug  # noqa: E402
from spsp import output  # noqa: E402


def keys_after(d: dict, key: str) -> list:
    ks = list(d.keys())
    return ks[ks.index(key) + 1:]


# ── series_slug: 安定・冪等・None 透過 ──
EXPECTED_SLUG = {
    '篝火': '篝火',
    'マエスマTOP': 'マエスマtop',
    'Weekly Smash Party ～スマパ～': 'weekly-smash-party-スマパ',
    'Big 1 / ビッグワン': 'big-1-ビッグワン',
}
for name, want in EXPECTED_SLUG.items():
    got = series_slug(name)
    assert got == want, f'{name!r}: {got!r} != {want!r}'
    assert series_slug(name) == got, 'stable'
    assert series_slug(got) == got, f'idempotent: {got!r} -> {series_slug(got)!r}'
    assert got and got[0] != '-' and got[-1] != '-'
assert series_slug(None) is None
assert series_slug('ＢＩＧ　１') == 'big-1'          # NFKC (全角 → 半角) + casefold
assert series_slug('a__b  c') == 'a-b-c'          # 区切りの連続は '-' 1 つ

# ── flag_tags: True な is_* だけ・固定順・record に無いフラグは出ない ──
assert FLAG_TAG_ORDER == ('weekend', 'weekend_real', 'smapa', 'pre', 'gf_missing', 'awaiting_resume',
                          'restricted', 'lower_class', 'uchi', 'special_rules', 'random_seed', 'dq')
assert flag_tags({}) == []
assert flag_tags({'is_dq': True, 'is_weekend': True, 'is_uchi': False, 'is_pre': True, 'is_small': True}) \
    == ['weekend', 'pre', 'dq']
assert flag_tags({'is_weekend': None, 'is_special_rules': True, 'is_weekend_real': True}) == ['weekend_real', 'special_rules']
all_on = {'is_' + k: True for k in FLAG_TAG_ORDER}
assert flag_tags(dict(reversed(list(all_on.items())))) == list(FLAG_TAG_ORDER)   # 入力の順に依らない

# ── match_bracket_type ──
assert match_bracket_type(SimpleNamespace(phase_bracket_type='SWISS')) == 'SWISS'
assert match_bracket_type(SimpleNamespace()) is None
assert match_bracket_type({'phase_bracket_type': ''}) is None

# ── 実際のビルダーを通す ──
def _m(**kw):
    base = dict(winner_id=1, loser_id=2, winner_score=2, loser_score=1, round=3, round_text='Winners Round 3',
                phase_name='Bracket', winners_top=64, losers_top=None, bracket_label='Winners TOP 64',
                global_round=3, global_top_x=64, global_bracket_label='Winners TOP 64',
                phase_bracket_type='DOUBLE_ELIMINATION', is_class=False)
    base.update(kw)
    return SimpleNamespace(**base)

m_de = _m()
m_rr = _m(phase_bracket_type='ROUND_ROBIN', global_round=None, global_top_x=None, global_bracket_label='総当たり',
          round_text='Round 1', round=1)
m_unknown = _m(phase_bracket_type=None, global_bracket_label=None)

# tournaments/<eid>.json の matches[]
t = SimpleNamespace(matches=[m_de, m_rr, m_unknown])
mo = output._build_tour_matches_out(t, {})
assert [x['bracket_type'] for x in mo] == ['DOUBLE_ELIMINATION', 'ROUND_ROBIN', None]
for x in mo:
    assert keys_after(x, 'global_bracket_label')[0] == 'bracket_type'

# players/<uid>.json の recent_matches[]
tsum = {'T1': {'event_id': 10, 'name': 'Big 1 #3', 'event': 'Singles', 'date': '2026-09-01',
               'is_weekend': True, 'is_pre': False, 'is_restricted': False, 'is_lower_class': True}}
fmt = lambda u, uid: f'p{uid}'  # noqa: E731
pm = {'1': [(100, 'T1', '2', True, 5.0, False, m_de, True),
            (99, 'T1', '3', False, -2.0, False, m_rr, True),
            (98, 'T1', '4', True, 1.0, False, m_unknown, False),
            (97, 'T1', '5', True, 1.0, False, None, False)]}
rm = output._build_recent_matches('1', player_matches=pm, tournament_summary=tsum, t_seed_info={}, users={}, format_display=fmt)
assert [x.get('bracket_type', 'ABSENT') for x in rm] == ['DOUBLE_ELIMINATION', 'ROUND_ROBIN', None, 'ABSENT']
for x in rm[:3]:
    assert x['global_bracket_label'] in ('Winners TOP 64', '総当たり', None)
    assert keys_after(x, 'global_bracket_label')[0] == 'bracket_type'

# players/<uid>.json の top_bt_gains[]
mw = {'1': [('T1', '2', 5.0, m_de), ('T1', '3', 3.0, m_rr)]}
bg = output._build_top_bt_gains('1', '2024-09-01', match_wins=mw, tournament_summary=tsum, users={}, format_display=fmt)
assert [x['bracket_type'] for x in bg] == ['DOUBLE_ELIMINATION', 'ROUND_ROBIN']
assert keys_after(bg[0], 'global_bracket_label')[0] == 'bracket_type'

# players/<uid>.json の tournaments[] (共通部 + is_dq の直後に tags) と slim (top_tjpr_contribs 等)
tsum_full = {'event_id': 10, 'parent_event_id': None, 'class_letter': None, 'ts': 1, 'date': '2026-09-01',
             'name': 'Big 1 #3', 'event': 'Singles', 'nent': 128,
             'is_weekend': True, 'is_weekend_real': False, 'is_smapa': False, 'is_pre': False,
             'is_gf_missing': False, 'is_awaiting_resume': True, 'is_restricted': False, 'is_lower_class': True}
rec = output._tour_entry_common_fields(tsum_full, 5, None)
rec.update({'wins': 3, 'losses': 2, 'is_dq': True})
rec['tags'] = flag_tags(rec)
assert rec['tags'] == ['weekend', 'awaiting_resume', 'lower_class', 'dq']
assert keys_after(rec, 'is_dq')[0] == 'tags'
assert [k for k in rec if k.startswith('is_')][-1] == 'is_dq'
slim = output._slim_tour_entry(rec)
assert slim['tags'] == ['weekend', 'awaiting_resume', 'lower_class', 'dq']   # slim に無い weekend_real/smapa は出ない
assert keys_after(slim, 'is_dq')[0] == 'tags'
assert [k for k in slim if k.startswith('is_')][-1] == 'is_dq'
rec2 = output._tour_entry_common_fields({**tsum_full, 'is_weekend_real': True, 'is_smapa': True}, 1, None)
rec2.update({'wins': 0, 'losses': 0, 'is_dq': False}); rec2['tags'] = flag_tags(rec2)
assert rec2['tags'] == ['weekend', 'weekend_real', 'smapa', 'awaiting_resume', 'lower_class']

# tournaments/<eid>.json の record
doc = output.build_tournament_doc(10, {'tname': 'Big 1 #3', 'ename': 'Singles', 'is_weekend': True, 'is_smapa': True,
                                       'is_gf_missing': True, 'is_small': True}, [], {}, [])
assert doc['tags'] == ['weekend', 'smapa', 'gf_missing']   # is_small は tags に含めない
assert keys_after(doc, 'is_small')[0] == 'tags'

# players/<uid>.json の top_tours[] (series の隣に series_id)
tt = output._build_top_tours('1', tjpr_contribs={'1': [{'t_id': 'T1', 'weighted': 1.0}]},
                             tournament_summary={'T1': {**tsum_full}}, placements_per_t={'T1': {'1': 5}},
                             tournament_series=lambda n: 'Big 1 / ビッグワン', tournament_series_number=lambda n: 3)
assert tt[0]['series'] == 'Big 1 / ビッグワン' and tt[0]['series_id'] == 'big-1-ビッグワン'
assert keys_after(tt[0], 'series')[0] == 'series_id'
tt = output._build_top_tours('1', tjpr_contribs={'1': [{'t_id': 'T1', 'weighted': 1.0}]},
                             tournament_summary={'T1': {**tsum_full}}, placements_per_t={},
                             tournament_series=lambda n: None, tournament_series_number=lambda n: None)
assert tt[0]['series'] is None and tt[0]['series_id'] is None

# meta.json の generated_at は offset 付き
meta = output.build_meta_dict(eval_date=dt.datetime(2026, 9, 19), t0=time.time(), n_tournaments_total=1, n_players=1,
                              tjpr_elo={}, norm_bt_gated_disp={}, spr_neg_val={}, uf_lost_val={},
                              radar_spr_clip=1.0, radar_uf_clip=1.0, max_tjpr=1.0)
g = dt.datetime.fromisoformat(meta['generated_at'])
assert g.tzinfo is not None and g.utcoffset() is not None, meta['generated_at']
assert meta['generated_at'][-6] in '+-' and meta['generated_at'][-3] == ':', meta['generated_at']
assert list(meta)[:2] == ['eval_date', 'generated_at']

print('ok: bracket_type / tags / series_id / generated_at offset')

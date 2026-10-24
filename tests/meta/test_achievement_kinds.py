"""実績 (achievements) / 動的バッチ (dynamic_badges) の機械可読な識別 (kind / params) のテスト。

spsp/score.py の各ビルダーは日本語 label に加えて kind (文字列) と params (dict) を出す (2026-09 追加、追加のみで
label / cls / priority は不変)。表示側 (別リポジトリ ~/spsp-site) は kind / params から翻訳した label を自前で
組み立てるので、ここで「同じアイコン・文言表から label を組み立て直すと元の label とバイト一致する」ことを、
全分岐 (全ティア・全バケット・クラス接尾辞の有無・×N・急上昇の各変種) について確かめる。

契約 (表示側の辞書はこれに対して書かれている):
  peak_rank  {'top': 1|2|3|10|50|100|500|1000}
  peak_tier  {'lv': 1..5, 'tier': crown|gold|silver|bronze|plus|base}   (Lv1-4 は plus|base)
  climb      {'n': 1000|500|200|100|50}
  uf / spr / losers_run  {'v': 値}
  tours / matches / wins / same_opp  {'n': ティア閾値}
  tour_series {'series', 'bucket', 'cls', 'count'}   tour {'name', 'bucket', 'cls'}
      bucket = win|2nd|3rd|4th|5th|top8|top16|top32|top64, cls = None|'B'..'E'|'casual'
  trend (dynamic_badges) {'days': 30|90|180, 'n': 上昇幅, 'surge': bool}

実行: OPENBLAS_NUM_THREADS=1 .venv/bin/python3 tests/meta/test_achievement_kinds.py
(リポジトリルートから。unittest 等は使わず plain assert。smash_database の scripts/ には依存しない =
 シリーズ名の関数は差し替える)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
import spsp.score as score  # noqa: E402
from spsp.score import (  # noqa: E402
    _peak_rank_achievement, _level_tier_achievement, _climb_achievement,
    _uf_achievement, _spr_achievement, _losers_run_achievement,
    _activity_achievements, _tournament_achievements, build_achievements, build_dynamic_badges,
    tier_achievement_from_value, level_tier_values,
)

# シリーズ名の関数 (smash_database の scripts/Japan/naming.py) は環境依存なので簡単なものに差し替える
score.tournament_award_label = lambda name: name.split('#')[0].strip()
score.tournament_individual_label = lambda name: name.strip()

# ── 表示側の辞書と同じ表 (ja) ──
PEAK_RANK = {1: ('👑', '全国 1 位'), 2: ('🥇', 'Top 2'), 3: ('🥈', 'Top 3'), 10: ('🌟', 'Top 10'),
             50: ('🔥', 'Top 50'), 100: ('💪', 'Top 100'), 500: ('🎯', 'Top 500'), 1000: ('🎯', 'Top 1000')}
LV5_TIER_SFX = {'crown': '👑', 'gold': '🥇', 'silver': '🥈', 'bronze': '🥉', 'plus': '+'}
LV_ICON = {4: '🥈', 3: '🥉', 2: '🎖', 1: '🎖'}
ACT = {'tours': {500: '🏆', 300: '🎖️', 100: '🏅', 50: '🎫', 20: '🎟️'},
       'matches': '⚔', 'wins': '🏅', 'same_opp': '🤝'}
ACT_TEXT = {'tours': '大会出場', 'matches': '試合数', 'wins': '勝利数', 'same_opp': '同一相手と対戦'}
BUCKET = {'win': ('🏆', '優勝'), '2nd': ('🥈', '準優勝'), '3rd': ('🥉', '3位'), '4th': ('🎖', '4位'),
          '5th': ('🎖', '5位'), 'top8': ('🏅', 'Top 8 以上達成'), 'top16': ('🏅', 'Top 16 以上達成'),
          'top32': ('🏅', 'Top 32 以上達成'), 'top64': ('🏅', 'Top 64 以上達成')}
CLASS_TEXT = {None: '', 'B': ' Bクラス', 'C': ' Cクラス', 'D': ' Dクラス', 'E': ' Eクラス', 'casual': ' カジュアル'}
TREND_PERIOD = {30: '直近 1 ヶ月で', 90: '直近 3 ヶ月で', 180: '直近半年で'}


def compose_label(kind, p):
    """kind / params → 日本語 label (表示側の組み立てを純 Python で再現)。"""
    if kind == 'peak_rank':
        icon, txt = PEAK_RANK[p['top']]
        return f'{icon} 最高 {txt}'
    if kind == 'peak_tier':
        lv, tier = p['lv'], p['tier']
        if lv == 5:
            return '🥇 最高 Lv5 到達' if tier == 'base' else f'最高 Lv5{LV5_TIER_SFX[tier]} 到達'
        return f"{LV_ICON[lv]} 最高 Lv{lv}{'+' if tier == 'plus' else ''} 到達"
    if kind == 'climb':
        return f"🚀 1ヶ月で +{p['n']} ランク上昇"
    if kind == 'uf':
        return f"⚡ UF {p['v']} 達成"
    if kind == 'spr':
        return f"✨ SPR {p['v']} 達成"
    if kind == 'losers_run':
        return f"🏃‍♂️ ルーザーズラン {p['v']} 達成"
    if kind in ACT_TEXT:
        icon = ACT[kind][p['n']] if kind == 'tours' else ACT[kind]
        return f"{icon} {ACT_TEXT[kind]} {p['n']}+"
    if kind == 'tour_series':
        icon, txt = BUCKET[p['bucket']]
        sfx = f" ×{p['count']}" if p['count'] > 1 else ''
        return f"{icon} {p['series']}{CLASS_TEXT[p['cls']]} {txt}{sfx}"
    if kind == 'tour':
        icon, txt = BUCKET[p['bucket']]
        return f"{icon} {p['name']}{CLASS_TEXT[p['cls']]} {txt}"
    if kind == 'trend':
        icon = '🚀' if p['surge'] else '📈'
        return f"{icon} {TREND_PERIOD[p['days']]} +{p['n']} {'急上昇' if p['surge'] else '上昇'}"
    raise AssertionError(f'unknown kind {kind!r}')


checked = 0


def check(d, kind=None, params=None, has_priority=True):
    """1 個の dict について: キーの存在と並び、kind/params の期待値、label の再構成一致。"""
    global checked
    assert d is not None
    assert 'kind' in d and 'params' in d, d
    assert isinstance(d['kind'], str) and isinstance(d['params'], dict), d
    keys = [k for k in d if k != '_v']
    expect = ['label', 'cls', 'priority', 'kind', 'params'] if has_priority else ['label', 'cls', 'kind', 'params']
    if d.get('kind') in ('tour', 'tour_series'):
        expect = expect + ['tjpr_pts']   # 大会の実績だけ: もとの大会ごとの [日付, 順位評価の素点] (プレイヤーカードの並び順、2026-09-30)
        assert all(isinstance(x, list) and len(x) == 2 for x in d['tjpr_pts']), d
    assert keys == expect, (keys, d)
    if kind is not None:
        assert d['kind'] == kind, (d, kind)
    if params is not None:
        assert d['params'] == params, (d['params'], params)
    assert compose_label(d['kind'], d['params']) == d['label'], (d, compose_label(d['kind'], d['params']))
    checked += 1


# ── peak_rank: 全ティア ──
for best, top in [(1, 1), (2, 2), (3, 3), (4, 10), (10, 10), (11, 50), (50, 50), (51, 100), (100, 100),
                  (101, 500), (500, 500), (501, 1000), (1000, 1000)]:
    check(_peak_rank_achievement(best), 'peak_rank', {'top': top})
assert _peak_rank_achievement(1001) is None and _peak_rank_achievement(None) is None

# ── peak_tier: Lv5 の 6 段 + Lv1-4 の plus / base ──
for rk, tier in [(1, 'crown'), (8, 'crown'), (9, 'gold'), (16, 'gold'), (17, 'silver'), (32, 'silver'),
                 (33, 'bronze'), (64, 'bronze'), (65, 'plus'), (128, 'plus'), (129, 'base'), (10000, 'base')]:
    check(_level_tier_achievement(5, rk), 'peak_tier', {'lv': 5, 'tier': tier})
for lv, cut in [(4, 384), (3, 768), (2, 1536)]:
    check(_level_tier_achievement(lv, cut), 'peak_tier', {'lv': lv, 'tier': 'plus'})
    check(_level_tier_achievement(lv, cut + 1), 'peak_tier', {'lv': lv, 'tier': 'base'})
check(_level_tier_achievement(1, 3072), 'peak_tier', {'lv': 1, 'tier': 'plus'})
assert _level_tier_achievement(1, 3073) is None      # Lv1 base は実績なし
# 順序値経由 (学習中の追跡値 → 表示 dict) でも kind / params が残り、_v だけ落ちる
for lv, rk in [(5, 1), (5, 9), (5, 17), (5, 33), (5, 65), (5, 129), (4, 1), (4, 100000), (3, 1), (3, 100000),
               (2, 1), (2, 100000), (1, 1)]:
    a = _level_tier_achievement(lv, rk)
    b = tier_achievement_from_value(int(level_tier_values([lv], [rk])[0]))
    assert '_v' not in b and b == {k: x for k, x in a.items() if k != '_v'}, (lv, rk, a, b)
    check(b)

# ── climb: 全ティア (params['n'] は閾値であって生の上昇幅ではない) ──
for climb, n in [(1000, 1000), (1500, 1000), (500, 500), (999, 500), (200, 200), (100, 100), (50, 50), (99, 50)]:
    check(_climb_achievement([{'d': 30, 'ens': 100 + climb}], 100), 'climb', {'n': n})
assert _climb_achievement([{'d': 30, 'ens': 149}], 100) is None

# ── uf / spr / losers_run ──
for v in (5, 6, 8, 10, 13):
    check(_uf_achievement({'uf': v}), 'uf', {'v': v})
    check(_spr_achievement({'spr': v}), 'spr', {'v': v})
    check(_losers_run_achievement({'run': v}), 'losers_run', {'v': v})
assert _uf_achievement({'uf': 4}) is None and _spr_achievement(None) is None and _losers_run_achievement({'run': 4}) is None

# ── 活動量: 各カテゴリの全ティア ──
for n in (500, 300, 100, 50, 20):
    (t,) = _activity_achievements(n, 0, 0, {})
    check(t, 'tours', {'n': n})
for n in (20000, 10000, 5000, 3000, 1000, 500, 200):
    (t,) = _activity_achievements(0, n, 0, {})
    check(t, 'matches', {'n': n})
for n in (10000, 5000, 3000, 1500, 500, 200, 100):
    (t,) = _activity_achievements(0, 0, n, {})
    check(t, 'wins', {'n': n})
for n in (50, 30, 20, 10, 5):
    (t,) = _activity_achievements(0, 0, 0, {1: n, 2: 1})
    check(t, 'same_opp', {'n': n})
assert _activity_achievements(19, 199, 99, {1: 4}) == []
acts = _activity_achievements(500, 20000, 10000, {1: 50})
assert [a['kind'] for a in acts] == ['tours', 'matches', 'wins', 'same_opp']
for a in acts:
    check(a)


# ── 大会別: シリーズ (nent<100) と個別 (nent>=100)、クラス接尾辞、×N、全バケット ──
def ent(name, place, nent, event='Singles', lower=False, restricted=False):
    return {'name': name, 'event': event, 'place': place, 'nent': nent, 'tjpr_w': 1.0, 'is_weekend': True,
            'is_lower_class': lower, 'is_restricted': restricted}


series_entries = [
    ent('Alpha#1', 1, 64), ent('Alpha#2', 1, 48), ent('Alpha#3', 2, 64),      # 優勝 ×2 (最高位だけ採用 → 準優勝は出ない)
    ent('Beta#5', 3, 32),
    ent('Gamma#1', 1, 40, event='Bクラス'), ent('Gamma#2', 1, 40, event='Ｃクラス シングルス'),
    ent('Delta#1', 2, 40, event='カジュアル', lower=True),
    ent('Eps#1', 5, 32), ent('Eps#2', 8, 32),                                   # 5位 (Top 8 は出ない)
    ent('Zeta#1', 7, 32), ent('Zeta#2', 4, 16),                                 # 4位
    ent('Eta#1', 16, 90),        # nent<128 → Top 16 にならず除外
]
out = _tournament_achievements(series_entries)
by_label = {d['label']: d for d in out}
assert set(by_label) == {'🏆 Alpha 優勝 ×2', '🥉 Beta 3位', '🏆 Gamma Bクラス 優勝', '🏆 Gamma Cクラス 優勝',
                         '🥈 Delta カジュアル 準優勝', '🎖 Eps 5位', '🎖 Zeta 4位'}, set(by_label)
check(by_label['🏆 Alpha 優勝 ×2'], 'tour_series', {'series': 'Alpha', 'bucket': 'win', 'cls': None, 'count': 2})
check(by_label['🥉 Beta 3位'], 'tour_series', {'series': 'Beta', 'bucket': '3rd', 'cls': None, 'count': 1})
check(by_label['🏆 Gamma Bクラス 優勝'], 'tour_series', {'series': 'Gamma', 'bucket': 'win', 'cls': 'B', 'count': 1})
check(by_label['🏆 Gamma Cクラス 優勝'], 'tour_series', {'series': 'Gamma', 'bucket': 'win', 'cls': 'C', 'count': 1})
check(by_label['🥈 Delta カジュアル 準優勝'], 'tour_series', {'series': 'Delta', 'bucket': '2nd', 'cls': 'casual', 'count': 1})
check(by_label['🎖 Eps 5位'], 'tour_series', {'series': 'Eps', 'bucket': '5th', 'cls': None, 'count': 1})
check(by_label['🎖 Zeta 4位'], 'tour_series', {'series': 'Zeta', 'bucket': '4th', 'cls': None, 'count': 1})

indiv_entries = [
    ent('Big 1', 1, 200), ent('Big 2', 2, 200), ent('Big 3', 3, 200), ent('Big 4', 4, 200), ent('Big 5', 5, 200),
    ent('Big 6', 7, 100), ent('Big 7', 12, 128), ent('Big 8', 25, 256), ent('Big 9', 60, 512),
    ent('Big 10', 1, 150, event='Dクラス'), ent('Big 11', 1, 150, event='Eクラス', lower=True),
    ent('Big 12', 2, 150, event='カジュアル', lower=True), ent('Big 13', 1, 150, restricted=True),
    ent('Big 14', 60, 256),      # nent<512 → Top 64 にならず除外
]
out = _tournament_achievements(indiv_entries)
assert len(out) == 13, [d['label'] for d in out]
expect = [('Big 1', 'win', None), ('Big 2', '2nd', None), ('Big 3', '3rd', None), ('Big 4', '4th', None),
          ('Big 5', '5th', None), ('Big 6', 'top8', None), ('Big 7', 'top16', None), ('Big 8', 'top32', None),
          ('Big 9', 'top64', None), ('Big 10', 'win', 'D'), ('Big 11', 'win', 'E'), ('Big 12', '2nd', 'casual'),
          ('Big 13', 'win', None)]
for d, (name, bucket, cls) in zip(out, expect):
    check(d, 'tour', {'name': name, 'bucket': bucket, 'cls': cls})

# ── build_achievements 経由: _v が落ちて kind / params は残る。priority 降順 ──
uid = 7
ach = build_achievements(
    uid, 30, 5, [{'d': 30, 'ens': 600, 'tjpr_lv': 4}, {'d': 90, 'ens': 5, 'tjpr_lv': 5}],
    series_entries + indiv_entries,
    {uid: {'uf': 9}}, {uid: {'spr': 7}}, {uid: {'run': 6}},
    {uid: 120}, {uid: 3500}, {uid: 1800}, {uid: {1: 12}},
    peak_alltime_max_days=365, peak={'ensemble': (2, 0), 'tier_best': 170000},
)
assert all('_v' not in d for d in ach)
assert [d['priority'] for d in ach] == sorted((d['priority'] for d in ach), reverse=True)
kinds = [d['kind'] for d in ach]
for k in ('peak_rank', 'peak_tier', 'climb', 'uf', 'spr', 'losers_run', 'tours', 'matches', 'wins', 'same_opp',
          'tour_series', 'tour'):
    assert k in kinds, (k, kinds)
pr = next(d for d in ach if d['kind'] == 'peak_rank'); assert pr['params'] == {'top': 2}, pr       # peak の 2 位
pt = next(d for d in ach if d['kind'] == 'peak_tier'); assert pt['params'] == {'lv': 5, 'tier': 'crown'}, pt
cl = next(d for d in ach if d['kind'] == 'climb'); assert cl['params'] == {'n': 500}, cl               # 600→30 = +570
for d in ach:
    check(d)

# ── dynamic_badges: 全変種 (priority なし) ──
def hist(d30=None, d90=None, d180=None):
    return [{'d': d, 'ens': v} for d, v in ((30, d30), (90, d90), (180, d180)) if v is not None]


cases = [
    (hist(d30=700), 30, 600, True), (hist(d30=350), 30, 250, False),
    (hist(d30=150, d90=800), 90, 700, True), (hist(d30=150, d90=400), 90, 300, False),
    (hist(d90=150, d180=900), 180, 800, False),
]
for h, days, n, surge in cases:
    (b,) = build_dynamic_badges(100, h)
    check(b, 'trend', {'days': days, 'n': n, 'surge': surge}, has_priority=False)
assert build_dynamic_badges(100, hist(d30=250, d90=250, d180=250)) == []
assert build_dynamic_badges(None, hist(d30=900)) == []

# ── 配信 JSON までの経路: json_roundtrip (overlay の入口) → split_detail (players/<uid>.json 側) → orjson で
#    kind / params が落ちないこと (output.py は achievements / dynamic_badges をそのまま渡す)
from spsp.overlay import json_roundtrip  # noqa: E402
from spsp.split_output import split_detail  # noqa: E402
from spsp.output import dump_json_bytes  # noqa: E402
import datetime as dt  # noqa: E402
import json  # noqa: E402
rec = {'user_id': uid, 'ranks': {'ensemble': 30}, 'scores': {}, 'metadata': {}, 'radar': {}, 'history': [],
       'tournaments': [], 'achievements': ach, 'dynamic_badges': build_dynamic_badges(100, hist(d30=700))}
stable, current, _ = split_detail(json_roundtrip(rec), dt.datetime(2026, 9, 15))
written = json.loads(dump_json_bytes(stable))
assert written['achievements'] == ach and written['dynamic_badges'] == rec['dynamic_badges'], 'kind/params が配信 JSON で落ちた'
assert all(list(d) == ['label', 'cls', 'priority', 'kind', 'params'] + (['tjpr_pts'] if d['kind'] in ('tour', 'tour_series') else [])
           for d in written['achievements'])
assert all(list(d) == ['label', 'cls', 'kind', 'params'] for d in written['dynamic_badges'])
assert 'achievements' not in current and 'dynamic_badges' not in current

print(f'ok: achievement kinds/params ({checked} dicts checked, label recomposed from kind/params matches)')

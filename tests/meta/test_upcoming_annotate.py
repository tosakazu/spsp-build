"""予定大会の注釈が本ビルドの判定と一致するか.

実行: OPENBLAS_NUM_THREADS=1 .venv/bin/python3 tests/meta/test_upcoming_annotate.py

annotate_upcoming.py が is_non_serious を SPECIAL_RULES_PATTERN だけで出していたため、
身内大会 (あらいぶ杯等) が予定一覧で「ポイントが入る大会」に見えていた (2026-08 発覚).
本ビルドは NON_SERIOUS_PATTERN (= 特殊ルール ∪ 身内) で判定するので、そこに揃える.
"""
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
sys.path.insert(0, ROOT)
from _region_rules_module import region_classify  # noqa: E402
_clf = region_classify("Japan")            # パターンの定義元は smash_database (scripts/Japan/classify.py)
NON_SERIOUS_PATTERN, SPECIAL_RULES_PATTERN, UCHI_PATTERN = _clf.NON_SERIOUS_PATTERN, _clf.SPECIAL_RULES_PATTERN, _clf.UCHI_PATTERN

# 1) 注釈は本ビルドと同じ pattern を使うこと。2026-09-11 から注釈の判定は地域モジュールの
#    upcoming_flags() (scripts/Japan/classify.py) にあり、scripts/common/annotate_upcoming.py はそれを呼ぶだけ
import datetime as dt  # noqa: E402
_sat = int(dt.datetime(2026, 9, 5, 12).timestamp())
_f = _clf.upcoming_flags('あらいぶ杯オフライン#6', 'シングルス', 30, _sat)
assert _f['is_non_serious'] and _f['is_uchi'] and not _f['is_special_rules'], \
    f'身内大会が NON_SERIOUS (= 特殊ルール ∪ 身内) で判定されていない: {_f}'
assert not _clf.upcoming_flags('篝火#18', 'シングルス', 300, _sat)['is_non_serious']
SRC = open(os.path.join(ROOT, 'smash_db_tournament', 'scripts', 'common', 'annotate_upcoming.py'),
           encoding='utf-8').read()
assert 'upcoming_flags' in SRC, '注釈スクリプトが地域モジュールの upcoming_flags を使っていない'

# 2) 身内大会が NON_SERIOUS に含まれること (パターン側の退行検知)
UCHI_NAMES = [
    'あらいぶ杯オフライン#6',
    'もつカップ',
    '帝国オフ',
    'スマサー合宿2026',
    '篝炎 in 炎メシハウス',
    'RUST Invitational',
]
for n in UCHI_NAMES:
    assert UCHI_PATTERN.search(n), f'身内判定されない: {n!r}'
    assert NON_SERIOUS_PATTERN.search(n), f'NON_SERIOUS に入らない: {n!r}'
    # 特殊ルールでは拾えない = 注釈側が SPECIAL_RULES だけだと漏れる
    if not SPECIAL_RULES_PATTERN.search(n):
        pass    # これが本件の漏れ方

# 3) 通常大会を誤検知しないこと
NORMAL = ['渋谷大乱 第一陣/SHIBUYA DAIRAN -First Strike-', '第１９回グランドスラム',
          'ウメブラSP12', '篝火#18', 'マエスマTOP#13']
for n in NORMAL:
    assert not UCHI_PATTERN.search(n), f'通常大会を身内と誤判定: {n!r}'

# 4) 篝火 と 篝炎 を取り違えないこと (紛らわしいので明示)
assert not UCHI_PATTERN.search('篝火#18'), '篝火 (通常大会) を身内扱いしている'
assert UCHI_PATTERN.search('篝炎 in 炎メシハウス'), '篝炎 (身内) を拾えていない'

# 5) シミュレーション側が身内と特殊ルールを出し分けること (フロントは spsp-web。無い環境 = Actions などでは飛ばす)
WEB = os.environ.get('SPSP_WEB', os.path.expanduser('~/spsp-web'))
if os.path.isdir(os.path.join(WEB, 'src', 'pages')):
    SIM = open(os.path.join(WEB, 'src', 'pages', 'sim.js'), encoding='utf-8').read()
    assert 'ev.is_uchi' in SIM, 'sim が身内を区別していない'
    JA = open(os.path.join(WEB, 'site', 'i18n', 'ja.js'), encoding='utf-8').read()
    assert '身内・招待制の大会は集計されません' in JA
else:
    print(f'  (spsp-web が無いので sim の表示の確認は飛ばす: {WEB})')

print('all upcoming-annotate tests passed')

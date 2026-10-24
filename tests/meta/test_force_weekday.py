"""is_force_weekday_tournament (実質平日ルール) のテスト.

判定の定義元は smash_database 側 (smash_db_tournament/scripts/Japan/classify.py)。
名前部分は derived.json の names.force_weekday / names.smacomi に書き出され、
参加者数との合成 (上野スマコミ < 40 人) は spsp/meta.py が行う。
ここでは定義元の合成関数 is_force_weekday_tournament を tests/meta/_region_rules_module.region_classify (テスト専用の口) 経由で検証する。

実行: OPENBLAS_NUM_THREADS=1 .venv/bin/python3 tests/meta/test_force_weekday.py
(リポジトリルートから。unittest 等は使わず plain assert)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from _region_rules_module import region_classify  # noqa: E402
from spsp.common import SMACOMI_FORCE_WEEKDAY_MAX_NENT  # noqa: E402

clf = region_classify('Japan')
fw = clf.is_force_weekday_tournament

# spsp/meta.py 側の合成規則 (names.force_weekday or (names.smacomi and nent < 40)) と定義元の閾値が一致していること
assert clf.SMACOMI_FORCE_WEEKDAY_MAX_NENT == SMACOMI_FORCE_WEEKDAY_MAX_NENT, \
    f'上野スマコミの閾値が spsp/common.py ({SMACOMI_FORCE_WEEKDAY_MAX_NENT}) と classify.py ({clf.SMACOMI_FORCE_WEEKDAY_MAX_NENT}) で食い違う'


def meta_rule(tname, ename, nent):
    """spsp/meta.py と同じ合成 (derived.json の names を経由する経路の再現)."""
    nm = clf.name_flags(tname, ename)
    return nm['force_weekday'] or (nm['smacomi'] and (nent or 0) < SMACOMI_FORCE_WEEKDAY_MAX_NENT)


def yes(tname, nent=32, ename=''):
    assert fw(tname, ename, nent), f'{tname!r} (nent={nent}): expected force-weekday'
    assert meta_rule(tname, ename, nent), f'{tname!r} (nent={nent}): meta 側の合成でも force-weekday のはず'


def no(tname, nent=32, ename=''):
    assert not fw(tname, ename, nent), f'{tname!r} (nent={nent}): expected NOT force-weekday'
    assert not meta_rule(tname, ename, nent), f'{tname!r} (nent={nent}): meta 側の合成でも NOT force-weekday のはず'


# ── 大菊月: 常に平日扱い ──
yes('大菊月　その34')
yes('大菊月　その8', nent=64)

# ── 渋谷BeeSmash (weekly): BIG / 上野開催回を除き平日扱い ──
yes('渋谷BeeSmash #17')
yes('渋谷BeeSmash 45', nent=60)      # nent 条件なし
yes('渋谷BeeSmash46')                 # スペースなし表記
yes('渋谷Beesmash 50')                # 大文字小文字ゆらぎ
yes('渋谷BeeSmash#16')
yes('ShibuyaBeeSmash #14')
yes('beesmash#15')                    # 渋谷 prefix なし表記
yes('BeeSmash TOURNAMENT #11', nent=65)
no('渋谷BeeSmash BIG')                # BIG は通常の休日大会
no('渋谷BeeSmash BIG 4', nent=161)
no('Ueno BeeSmash #13', nent=64)      # 上野開催回は対象外
no('上野BeeSmash TOURNA/MATE', nent=62)

# ── 上野スマコミ: 40人未満のみ平日扱い ──
yes('27(土)上野スマコミ#489(オープン)', nent=10)
yes('27(土)上野スマコミ#291(無制限)', nent=39)
no('20(木祝)上野スマコミ#156(無制限)', nent=40)   # 境界: 40人ちょうどは休日のまま
no('11(土)上野スマコミ#431(オープン)', nent=64)
yes('7(土)スマコミ#89(無制限)', nent=37)           # 上野 prefix なし表記も同系列

# ── ename 側マッチ ──
yes('なにかの大会', nent=8, ename='渋谷BeeSmash Singles')

# ── 一般の大会は対象外 ──
no('マエスマTOP#48', nent=128)
no('ウメブラSP12', nent=256)
no('スマバト#40', nent=8)             # 小規模でもスマコミ系でなければ対象外

print('test_force_weekday: all assertions passed')

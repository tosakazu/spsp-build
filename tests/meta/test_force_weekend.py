"""実質休日 (お盆/年末年始の大規模平日大会を休日扱い) のテスト.

実行: OPENBLAS_NUM_THREADS=1 .venv/bin/python3 tests/meta/test_force_weekend.py
(リポジトリルートから。unittest 等は使わず plain assert)
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from spsp.common import FORCE_WEEKEND_MIN_NENT  # noqa: E402
from _region_rules_module import region_classify  # noqa: E402
_clf = region_classify("Japan")            # 暦の判定は smash_database 側 (scripts/Japan/classify.py)
is_force_weekend_date = _clf.is_force_weekend_date
is_force_weekend_range = _clf.is_force_weekend_range


def d(s):
    return dt.date.fromisoformat(s)


# ── 日付単体: お盆 = 8/13〜15 ──
assert not is_force_weekend_date(d('2026-08-12'))
assert is_force_weekend_date(d('2026-08-13'))
assert is_force_weekend_date(d('2026-08-14'))   # growup_6 (金曜)
assert is_force_weekend_date(d('2026-08-15'))
assert not is_force_weekend_date(d('2026-08-16'))

# ── 日付単体: 年末年始 = 12/26〜1/5 ──
assert not is_force_weekend_date(d('2026-12-25'))
assert is_force_weekend_date(d('2026-12-26'))
assert is_force_weekend_date(d('2026-12-27'))   # 渋谷BeeSmash BIG 2 (2024) 相当
assert is_force_weekend_date(d('2026-12-31'))
assert is_force_weekend_date(d('2027-01-01'))
assert is_force_weekend_date(d('2027-01-04'))   # スマバトSP#34 (2023) 相当
assert is_force_weekend_date(d('2027-01-05'))
assert not is_force_weekend_date(d('2027-01-06'))

# ── 範囲: 期間のどこかがかかれば True ──
assert is_force_weekend_range(d('2026-08-12'), d('2026-08-13'))
assert not is_force_weekend_range(d('2026-08-16'), d('2026-08-17'))
# end < start は start 単独扱い (is_weekend_range と同じ規約)
assert is_force_weekend_range(d('2026-08-14'), d('2026-08-01'))
# 14 日超の長期イベントは start 単独判定
assert not is_force_weekend_range(d('2026-08-01'), d('2026-09-30'))
assert is_force_weekend_range(d('2026-08-13'), d('2026-09-30'))

# ── 閾値定数 (growup_6 = 96 人が通る値であること) ──
assert FORCE_WEEKEND_MIN_NENT == 80

print('OK: test_force_weekend')

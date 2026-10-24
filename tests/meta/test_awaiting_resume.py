"""再開待ち (awaiting resume) の判定と登録簿ローダのテスト.

実行: OPENBLAS_NUM_THREADS=1 .venv/bin/python3 tests/meta/test_awaiting_resume.py
(リポジトリルートから。unittest 等は使わず plain assert)

背景: 2026-09-06 IMPACT MAJOR#5 が天候で Losers Semi-Final 以降を延期。従来は優勝者が
居ない大会 = is_gf_missing = 集計から丸ごと除外だったが、再開待ちとして登録した大会は
その時点までの結果 (完了した試合 + start.gg の最低保証順位) で暫定集計する。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from spsp.meta import resolve_completion_flags as rc  # noqa: E402
from spsp.tournament_utils import load_awaiting_resume_ids  # noqa: E402

# ── 判定 ──
# 優勝者なし & 未登録 → 従来どおり未完了 (除外)
assert rc(True, True, 1, set()) == (True, False)
# 優勝者なし & 登録あり → 再開待ち (未完了にしない = 集計する)
assert rc(True, True, 1, {1}) == (False, True)
# 優勝者が出た (no_champion=False) → 登録が残っていても通常の完了大会
assert rc(False, False, 1, {1}) == (False, False)
# gf_missing_base が False (SE-only 等) で優勝者なし & 登録あり → 再開待ちフラグは立つが除外もしない
assert rc(False, True, 1, {1}) == (False, True)
# 別の event が登録されていても無関係
assert rc(True, True, 2, {1}) == (True, False)

# ── 登録簿ローダ ──
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, 'awaiting_resume.json')
    assert load_awaiting_resume_ids(p) == set()          # 不在 = 空
    json.dump({"events": [{"event_id": 1672939, "note": "x"}, 42]}, open(p, 'w'))
    assert load_awaiting_resume_ids(p) == {1672939, 42}
    json.dump({"events": [{"event_id": "1672939"}]}, open(p, 'w'))   # 文字列 id は壊れ扱い (黙って無視しない)
    try:
        load_awaiting_resume_ids(p); assert False, 'expected ValueError'
    except ValueError:
        pass
    json.dump({"events": "oops"}, open(p, 'w'))
    try:
        load_awaiting_resume_ids(p); assert False, 'expected ValueError'
    except ValueError:
        pass

# ── 本番の登録簿が読めること ──
ids = load_awaiting_resume_ids()
assert isinstance(ids, set)
print(f'ok (登録 {len(ids)} 件)')

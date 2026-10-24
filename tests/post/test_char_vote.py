"""キャラ投票のビルド側採用規則テスト (spsp/char_vote.py).

実行: OPENBLAS_NUM_THREADS=1 .venv/bin/python3 tests/post/test_char_vote.py
(リポジトリルートから。unittest 等は使わず plain assert)

採用規則は docs/post_feature_design.md §12。
"""
import json
import os
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
sys.path.insert(0, ROOT)
from spsp.char_vote import (  # noqa: E402
    DOUBLE_MAIN_PCT_GAP, apply_char_votes, double_main_candidates,
    latest_by_uid, load_char_names, load_votes,
)

NAMES = {1305: 'ロックマン', 1271: 'ベヨネッタ', 1273: 'クッパ'}


def c(cid, pct, n=10):
    return {'name': NAMES[cid], 'id': cid, 'pct': pct, 'n': n, 'games': n, 'total': 100}


def vote(uid, cid, ts='2026-08-14T10:00:00+09:00', status='pending'):
    return {'ts': ts, 'user_id': uid, 'char_id': cid,
            'char_name': NAMES.get(cid, '?'), 'status': status}


def apply(usage, votes):
    return apply_char_votes(usage, votes, char_names=NAMES)


# ── 1. 使用実績なし → 投票を採用 ──

out, st = apply({}, [vote(1, 1305)])
assert out[1] == [{'name': 'ロックマン', 'id': 1305, 'src': 'vote'}], out[1]
assert st['adopted_empty'] == 1

# 投票由来のエントリは pct キーを持たない。
# (GAS の voteCandidates_ は Number(pct) が有限かで「使用実績あり」を判定する。
#  pct: None を書くと JSON では null になり Number(null) === 0 が有限値で通ってしまう)
assert 'pct' not in out[1][0]
assert 'pct' not in json.loads(json.dumps(out[1][0]))

# char_usage に uid はあるが空リスト、というケースも同じ扱い
out, st = apply({1: []}, [vote(1, 1305)])
assert out[1][0]['id'] == 1305 and st['adopted_empty'] == 1


# ── 2. 明確なメインがいる → 使用データを採用 (投票は無視) ──

usage = {1: [c(1305, 0.70), c(1271, 0.30)]}      # 差 0.40 > GAP
out, st = apply(usage, [vote(1, 1271)])
assert out[1] == usage[1], out[1]
assert st['ignored_clear_main'] == 1 and st['reordered'] == 0

# 1 体しか実績が無い場合も「明確なメイン」
out, st = apply({1: [c(1305, 1.0)]}, [vote(1, 1271)])
assert out[1][0]['id'] == 1305 and st['ignored_clear_main'] == 1


# ── 3. ダブルメイン圏かつ候補内 → 投票を先頭に ──

usage = {1: [c(1305, 0.55), c(1271, 0.45)]}      # 差 0.10 <= GAP
out, st = apply(usage, [vote(1, 1271)])
assert [x['id'] for x in out[1]] == [1271, 1305], out[1]
assert out[1][0]['pct'] == 0.45, '実績側の pct は残す'
assert out[1][0]['src'] == 'vote'
assert st['reordered'] == 1

# 入力は変更しない (= 呼び出し側の char_usage を壊さない)
assert [x['id'] for x in usage[1]] == [1305, 1271]

# 3 体目が圏外なら並びは維持されたまま後ろに残る
usage = {1: [c(1305, 0.50), c(1271, 0.45), c(1273, 0.05)]}
out, _ = apply(usage, [vote(1, 1271)])
assert [x['id'] for x in out[1]] == [1271, 1305, 1273]

# 既に先頭なら何もしない
out, st = apply({1: [c(1305, 0.55), c(1271, 0.45)]}, [vote(1, 1305)])
assert [x['id'] for x in out[1]] == [1305, 1271]
assert st['ignored_same_order'] == 1 and st['reordered'] == 0

# 候補外への投票は無視
usage = {1: [c(1305, 0.50), c(1271, 0.45), c(1273, 0.05)]}
out, st = apply(usage, [vote(1, 1273)])
assert [x['id'] for x in out[1]] == [1305, 1271, 1273]
assert st['ignored_not_candidate'] == 1


# ── 4. 一度採用したあと、実績が積み上がって明確になったら実績が勝つ ──
# (= 前回ビルドで先頭にした並びを入力にしても、投票が固定化しない)

prev = [dict(c(1271, 0.45), src='vote'), c(1305, 0.55)]   # 前回の出力そのもの
usage = {1: prev}
out, st = apply(usage, [vote(1, 1271)])
assert [x['id'] for x in out[1]] == [1271, 1305], '圏内のうちは投票が先頭のまま'

# 実績が離れた (0.75 vs 0.25) ら、先頭が投票由来でも使用データに戻る
usage = {1: [dict(c(1271, 0.25), src='vote'), c(1305, 0.75)]}
out, st = apply(usage, [vote(1, 1271)])
assert [x['id'] for x in out[1]] == [1305, 1271], out[1]
assert st['ignored_clear_main'] == 1

# 候補外を却下する側でも同じ (= 前回の並びが残らない)
usage = {1: [dict(c(1273, 0.05), src='vote'), c(1305, 0.50), c(1271, 0.45)]}
out, st = apply(usage, [vote(1, 1273)])
assert [x['id'] for x in out[1]] == [1305, 1273, 1271], out[1]
assert st['ignored_not_candidate'] == 1


# ── 5. status と最新行 ──

# 再投票は最新行が勝つ
votes = [vote(1, 1305, '2026-08-14T10:00:00+09:00'),
         vote(1, 1271, '2026-08-14T12:00:00+09:00')]
out, _ = apply({}, votes)
assert out[1][0]['id'] == 1271

# debug 行は絶対に採用しない
out, st = apply({}, [vote(1, 1305, status='debug')])
assert 1 not in out and st['adopted_empty'] == 0

# 最新行が rejected なら、その uid は投票なし扱い (前の行に戻らない)
votes = [vote(1, 1305, '2026-08-14T10:00:00+09:00'),
         vote(1, 1271, '2026-08-14T12:00:00+09:00', status='rejected')]
assert latest_by_uid(votes) == {}
out, _ = apply({}, votes)
assert 1 not in out

# approved は採用する
out, _ = apply({}, [vote(1, 1305, status='approved')])
assert out[1][0]['id'] == 1305


# ── 6. 未知の char_id は採用しない ──

out, st = apply({}, [vote(1, 9999)])
assert 1 not in out and st['unknown_char'] == 1


# ── 7. ダブルメイン圏の判定は GAS と同じ規則 ──

assert double_main_candidates([]) == []
assert double_main_candidates([c(1305, 0.6)]) == []          # 1 体では圏にならない
assert len(double_main_candidates([c(1305, 0.6), c(1271, 0.4)])) == 2
# 境界: ちょうど GAP は圏内
gap = DOUBLE_MAIN_PCT_GAP
assert len(double_main_candidates([c(1305, 0.6), c(1271, round(0.6 - gap, 4))])) == 2
assert len(double_main_candidates([c(1305, 0.6), c(1271, round(0.6 - gap - 0.01, 4))])) == 0
# 基準は最大 pct であって先頭ではない (前回ビルドで並びが変わっているため)
assert len(double_main_candidates([c(1271, 0.30), c(1305, 0.70)])) == 0
# pct を持たないエントリ (投票由来) は実績として数えない
assert double_main_candidates([{'name': 'ロックマン', 'id': 1305, 'src': 'vote'}]) == []


# ── 8. ファイル読み込み ──

with tempfile.TemporaryDirectory() as d:
    missing = os.path.join(d, 'nope.json')
    assert load_votes(missing) == ([], '')

    p = os.path.join(d, 'char_votes.json')
    with open(p, 'w') as f:
        json.dump({'fetched_at': '2026-08-14T02:00:00+09:00',
                   'votes': [vote(1, 1305)]}, f)
    got, at = load_votes(p)
    assert len(got) == 1 and at == '2026-08-14T02:00:00+09:00'

    with open(p, 'w') as f:
        json.dump({'votes': 'oops'}, f)
    try:
        load_votes(p)
        raise AssertionError('壊れたファイルで例外にならなかった')
    except ValueError:
        pass


# ── 9. キャラ一覧 (spsp/characters.json) が読めて、投票で入りうる名前が引ける ──

names = load_char_names()
assert len(names) > 50, len(names)
assert names.get(1305) and names.get(1271)

# ── 10. 取得スクリプトの正規化 (build/fetch_char_votes.py) ──

from spsp.cli import fetch_char_votes as fcv  # noqa: E402

got = fcv.normalize({'votes': [
    {'ts': '2026-08-14T12:00:00+09:00', 'userId': '222', 'charId': '1271',
     'charName': 'ベヨネッタ', 'status': 'pending'},
    {'ts': '2026-08-14T10:00:00+09:00', 'userId': '111', 'charId': '1305',
     'charName': 'ロックマン', 'status': 'approved'},
    # 採用しない status は落とす (GAS 側でも落としているが二重で)
    {'ts': '2026-08-14T11:00:00+09:00', 'userId': '333', 'charId': '1305',
     'charName': 'ロックマン', 'status': 'debug'},
    # 数値にできない行は落とす
    {'ts': '2026-08-14T11:30:00+09:00', 'userId': 'x', 'charId': '1305',
     'charName': '', 'status': 'pending'},
]})
assert [r['user_id'] for r in got] == [111, 222], got     # ts 昇順
assert all(isinstance(r['user_id'], int) and isinstance(r['char_id'], int) for r in got)
assert fcv.normalize({}) == []

# rejected は残す (= latest_by_uid が「却下された最新行」を見て投票なしにできる)
kept = fcv.normalize({'votes': [
    {'ts': '2026-08-14T10:00:00+09:00', 'userId': '1', 'charId': '1305',
     'charName': 'ロックマン', 'status': 'rejected'}]})
assert len(kept) == 1 and kept[0]['status'] == 'rejected'

# 既定のエンドポイントは spsp.games の Worker (2026-09-29 に GAS を止めた)
assert fcv.read_endpoint() == 'https://spsp.games/api'

# 差分マージ: 同じ行 (ts, user_id, char_id) は新しく取ったほうが勝つ
prev = [
    {'ts': '2026-08-14T10:00:00+09:00', 'user_id': 1, 'char_id': 1305,
     'char_name': 'ロックマン', 'status': 'pending'},
    {'ts': '2026-08-14T11:00:00+09:00', 'user_id': 2, 'char_id': 1271,
     'char_name': 'ベヨネッタ', 'status': 'pending'},
]
# 同じ行が status 変更で返ってきた + 新しい行が 1 つ
fresh = [
    dict(prev[1], status='rejected'),
    {'ts': '2026-08-14T12:00:00+09:00', 'user_id': 3, 'char_id': 1273,
     'char_name': 'クッパ', 'status': 'pending'},
]
merged = fcv.merge(prev, fresh)
assert len(merged) == 3, merged
assert [m['user_id'] for m in merged] == [1, 2, 3]
assert merged[1]['status'] == 'rejected', '新しく取った status で上書きされていない'

# 差分に同じ行がそのまま含まれていても重複しない (境界の秒を再取得するため起きる)
assert len(fcv.merge(prev, list(prev))) == 2

# 同じ秒に別ユーザーが投票していても両方残る
same_sec = [
    {'ts': '2026-08-14T11:00:00+09:00', 'user_id': 9, 'char_id': 1305,
     'char_name': 'ロックマン', 'status': 'pending'},
]
assert len(fcv.merge(prev, same_sec)) == 3

# 鍵の導出が Worker 側 (spsp-web worker/src/api/export.ts) と一致する (固定ベクタ。spsp-web の worker/test/export.test.mjs も同じ値を検査する)。
assert fcv.derive_key('SECRET-DO-NOT-LEAK') == 'r_GJ1PQZlcDh4cNc85qvUxAaFBoKzoHGgPVE_pPDfhg'
assert fcv.EXPORT_KEY_LABEL == 'spsp:export_votes:v1'
# ラベルが違えば別の鍵 (= 用途が分かれている) / secret が違えば別の鍵
assert fcv.derive_key('SECRET-DO-NOT-LEAK') != fcv.derive_key('other-secret')
assert '=' not in fcv.derive_key('x') and '+' not in fcv.derive_key('x')


print('OK: tests/post/test_char_vote.py')

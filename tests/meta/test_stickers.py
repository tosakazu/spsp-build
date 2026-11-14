"""spsp/stickers.py (大会シールの配布判定) の検査。
実行: .venv/bin/python3 tests/meta/test_stickers.py
"""
import json
import os
import sys
import tempfile
from types import SimpleNamespace as NS

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)
from spsp import stickers as S  # noqa: E402


def mt(w, l, pg=1, cls=False):
    return NS(winner_id=w, loser_id=l, phase_group_id=pg, is_class=cls)


def row(w, l, order, pid, pg):
    return {'winner_id': w, 'loser_id': l, 'phase_order': order, 'phase_id': pid, 'phase_group_id': pg}


def test_lines():
    # 1 つの山: 金 = 1/4 (最低 2 位・最大 8)、銀 = 48 人以下は 1/2 (最大 16)、49 人以上は 24
    assert S.single_lines(8) == (2, 4)
    assert S.single_lines(24) == (6, 12)
    assert S.single_lines(40) == (8, 16)
    assert S.single_lines(48) == (8, 16)
    assert S.single_lines(49) == (8, 24)
    assert S.single_lines(300) == (8, 24)
    # 予選が複数の山: 突破 8 人以上なら金 Top 8、4〜7 人なら Top 4、4 人未満なら 2 位まで
    assert S.pool_lines(24, 17) == (8, 17)
    assert S.pool_lines(6, 5) == (4, 5)
    assert S.pool_lines(3, 3) == (2, 3)
    # 金・銀・虹の判定
    assert [S.tier_of(p, 7, 13) for p in (1, 2, 7, 9, 13, 17)] == ['rainbow', 'gold', 'gold', 'silver', 'silver', 'normal']


def test_events():
    with tempfile.TemporaryDirectory() as d:
        # 予選 2 ブロック (各 4 人、上位 2 人ずつ抜ける) → 本線 4 人。順位 1,2,3,4,5,5,7,7
        rows = [row(1, 5, 1, 10, 100), row(2, 1, 1, 10, 100), row(5, 6, 1, 10, 100),
                row(3, 7, 1, 10, 200), row(4, 3, 1, 10, 200), row(7, 8, 1, 10, 200),
                row(1, 2, 2, 20, 300), row(3, 4, 2, 20, 300), row(1, 3, 2, 20, 300), row(2, 4, 2, 20, 300)]
        json.dump({'data': rows}, open(os.path.join(d, 'matches.json'), 'w'))
        standings = [(1, 1), (2, 3), (3, 2), (4, 4), (5, 5), (5, 6), (7, 7), (7, 8)]
        place = {u: p for p, u in standings}
        assert S.pool_border(d, place) == 4   # 本線で一番悪い順位 = 4 位
        t = NS(standings=standings, event_path=d, event_id=1,
               matches=[mt(r['winner_id'], r['loser_id'], r['phase_group_id']) for r in rows])
        meta = {'nent': 8}
        got = S.event_stickers(t, meta)
        # 突破 4 人 → 金は Top 4 (= 突破した全員)、それ以外は通常
        assert got == {1: 'rainbow', 3: 'gold', 2: 'gold', 4: 'gold', 5: 'normal', 6: 'normal', 7: 'normal', 8: 'normal'}, got
        # 突破 3 人 (4 位の人が本線にいない) → 金は 2 位まで
        place3 = {**place, 4: 5}
        assert S.pool_lines(sum(1 for p in place3.values() if p <= 3), 3) == (2, 3)
        # 予選で負けた人が境目より上 (予選・本線の作りでない) → 1 つの山として扱う
        assert S.pool_border(d, {**place, 5: 3}) is None

        # 古い大会: 試合に phase_order が無い。人数の多いフェーズを予選として同じ境目になる (はしこうさんの問い合わせ 2026-11-14)
        old = [{**{k: v for k, v in r.items() if k != 'phase_order'}, 'phase_num_seeds': 8 if r['phase_id'] == 10 else 4} for r in rows]
        json.dump({'data': old}, open(os.path.join(d, 'matches.json'), 'w'))
        assert S.pool_border(d, place) == 4
        assert S.event_stickers(t, meta) == got
        # start.gg の順番は見ない: 予選が本戦の後ろに並んでいる大会 (DELTA#8・スマ納め など) でも人数の多い方が予選
        rev = [{**r, 'phase_order': 2 if r['phase_id'] == 10 else 1} for r in old]
        json.dump({'data': rev}, open(os.path.join(d, 'matches.json'), 'w'))
        json.dump({'phases': [{'id': 10, 'order': 2}, {'id': 20, 'order': 1}]}, open(os.path.join(d, 'phases.json'), 'w'))
        assert S.pool_border(d, place) == 4
        # 人数が入っていなければ、そのフェーズで試合をした人数で並べる
        bare = [{k: v for k, v in r.items() if k != 'phase_num_seeds'} for r in rev]
        json.dump({'data': bare}, open(os.path.join(d, 'matches.json'), 'w'))
        assert S.pool_border(d, place) == 4

    # 1 つの山 (phase group 1 つ) は matches.json を読まない。試合をしていない人 (DQ だけ) には配らない
    standings = [(i, i) for i in range(1, 10)]
    t = NS(standings=standings, event_path='/nonexistent', event_id=2,
           matches=[mt(i, i + 1) for i in range(1, 8)])   # 9 番は試合なし
    got = S.event_stickers(t, {'nent': 9})
    assert got[1] == 'rainbow' and got[2] == 'gold' and got[3] == 'silver' and got[4] == 'silver' and got[5] == 'normal'
    assert 9 not in got
    # 配らない大会: 8 人未満・特殊ルール・未完了・少人数
    assert S.event_stickers(t, {'nent': 7}) == {}
    for k in ('is_non_serious', 'is_gf_missing', 'is_small'):
        assert S.event_stickers(t, {'nent': 9, k: True}) == {}


def test_class_virtual():
    # 下位クラスの仮想大会: t.matches は空なので、親の大会のクラスの試合で「試合をした人」を決める
    parent = NS(standings=[(i, i) for i in range(1, 21)], event_path='/x', event_id=50,
                matches=[mt(i, i + 1) for i in range(1, 12)] + [mt(i, i + 1, pg=9, cls=True) for i in range(12, 20)])
    virt = NS(standings=[(i - 11, i) for i in range(12, 21)], event_path='/x/class_phases/B_virtual', event_id=-50, matches=[])
    metas = {id(parent): {'nent': 20}, id(virt): {'nent': 9, 'is_class_virtual': True, 'parent_event_id': 50}}
    out = S.compute_stickers([parent, virt], metas)
    assert out[id(virt)][12] == 'rainbow' and len(out[id(virt)]) == 9, out[id(virt)]


if __name__ == '__main__':
    test_lines()
    test_events()
    test_class_virtual()
    print('ok')

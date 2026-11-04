"""DQ で 1 試合もしていない人 (no-show) がいる大会の人数の表示と、不戦勝だけで勝ち進んだ人の SPR (2026-11-04)。

不具合: 人数 (nent) は no-show を除いた数なのに、順位とシードは start.gg の数字 (no-show を数えたまま) だったので
「12 人中 13 位」が出た。また、勝者側 1 回戦が不戦勝 (相手の DQ) で、そのあと 2 敗の人が、不戦勝で進んだぶん
順位が上がって SPR +1 になっていた (勝ちは 0)。

直したこと (ユーザー判断: start.gg の数字をそのまま):
  - 順位・シードは start.gg のまま。表示の人数を start.gg の登録人数 (no-show を含む) にする (meta の nent_reg →
    出力の nent)。レベルなどの判定は今までの人数 (試合をした人数、出力の nent_lv)
  - 実際の勝ち (DQ の不戦勝は t.matches に入らない) が 0 の人の SPR はプラスにしない
ランキングの計算は変わらない。

実行: OPENBLAS_NUM_THREADS=1 .venv/bin/python3 tests/meta/test_spr_no_shows.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from spsp.data_loader import Match, Tournament  # noqa: E402
from spsp.meta import build_placements_seeds  # noqa: E402
from spsp.tournament_utils import filter_dq_no_shows  # noqa: E402
from spsp.common import placement_to_w2w  # noqa: E402


def tournament(tmp, standings, seeds, matches):
    d = tempfile.mkdtemp(dir=tmp)
    with open(os.path.join(d, 'seeds.json'), 'w') as f:
        json.dump({'data': [{'user_id': u, 'seed_num': s} for u, s in seeds.items()]}, f)
    ms = [Match(timestamp=1, winner_id=w, loser_id=l, event_path=d) for w, l in matches]
    return Tournament(timestamp=1, event_path=d, standings=list(standings), matches=ms)


with tempfile.TemporaryDirectory() as tmp:
    # 14 人登録、2 人 (91, 92) は DQ で 1 試合もしていない (start.gg では 9 位)。13 位の 2 人 (13, 14) の順位はそのまま
    places = [1, 2, 3, 4, 5, 5, 7, 7, 9, 9, 9, 9, 13, 13]
    uids = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 91, 92, 13, 14]
    played = [u for u in uids if u not in (91, 92)]
    seeds = {u: i + 1 for i, u in enumerate(uids)}
    matches = [(played[i], played[i + 1]) for i in range(0, 10, 2)] + [(1, 13), (2, 14), (3, 13), (4, 14)]
    t = tournament(tmp, zip(places, uids), seeds, matches)
    filter_dq_no_shows([t])
    assert len(t.standings) == 12, '判定に使う人数 (nent) は試合をした 12 人'
    assert sorted(u for _, u in t.no_shows) == [91, 92], 'no-show を覚えておく (表示の人数 nent_reg = 12 + 2 = 14)'
    assert len(t.standings) + len({u for _, u in t.no_shows}) == 14
    pl, si = build_placements_seeds([t])
    assert (pl[id(t)][13], si[id(t)][13]['seed']) == (13, 13), '順位・シードは start.gg のまま'
    assert max(pl[id(t)].values()) <= 14, '順位は表示の人数 (登録人数) を超えない'

    # 不戦勝で進んだ人: 勝者側 1 回戦は相手の DQ (t.matches に入らない)、そのあと 2 敗。シード下位だが順位が上 → SPR はプラスにしない
    standings = [(1, 1), (2, 2), (3, 3), (4, 4), (5, 50), (5, 6), (7, 7), (7, 8)]
    seeds = {1: 1, 2: 2, 3: 3, 4: 4, 6: 5, 7: 6, 8: 7, 50: 8}
    matches = [(1, 2), (3, 4), (6, 7), (2, 50), (4, 50), (1, 3), (2, 8)]   # 50 は負けだけ (勝ちは DQ の不戦勝だけ)
    t2 = tournament(tmp, standings, seeds, matches)
    filter_dq_no_shows([t2])
    assert not getattr(t2, 'no_shows', None), 'no-show はいない (全員 1 試合はしている)'
    _, si2 = build_placements_seeds([t2])
    assert placement_to_w2w(8) - placement_to_w2w(5) > 0, 'start.gg の順位だけなら SPR はプラス'
    assert si2[id(t2)][50]['spr'] == 0, '勝ちが 0 なら SPR はプラスにしない'
    assert si2[id(t2)][8]['spr'] == placement_to_w2w(7) - placement_to_w2w(7), '勝ちのある人はそのまま'
    # 勝ちが 0 でもマイナスはそのまま (シード上位で負けた)
    t3 = tournament(tmp, [(1, 1), (2, 2), (3, 3), (3, 4)], {1: 4, 2: 3, 3: 2, 4: 1}, [(1, 2), (2, 3), (1, 4)])
    filter_dq_no_shows([t3])
    _, si3 = build_placements_seeds([t3])
    assert si3[id(t3)][4]['spr'] == placement_to_w2w(1) - placement_to_w2w(3) < 0

print('ok')

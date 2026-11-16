"""start.gg のアカウントが無い参加者との試合 (spsp/guests、2026-11-16) のテスト (plain assert)。

- その試合だけの uid は -match_id。id が正の整数でない試合は読まない
- 学習に入れるのは RATED_SINCE 以降の大会だけ (rated / with_rated)。表示用 (of / with_guests) は全部
- 大会のハッシュ (learn_state.tournament_digest) は、学習に入れる試合がある大会だけ変わる
  (それより前の大会は以前と同じ = チェックポイントを作り直さない)
- 名前の表示 (tournament_utils.format_display): 負の uid は取れていれば名前、無ければ空
"""
import os, sys
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from spsp import data_loader as D  # noqa: E402
from spsp import guests as G  # noqa: E402
from spsp.common import _bracket_order_key  # noqa: E402
from spsp.learn_state import tournament_digest  # noqa: E402
from spsp.tournament_utils import format_display  # noqa: E402


def _m(w, l, ts, top=64):
    return D._match_from_row({'winner_id': w, 'loser_id': l, 'round': 1, 'round_text': 'Winners Round 1',
                              'phase_bracket_type': 'DOUBLE_ELIMINATION', 'global_top_x': top,
                              'global_bracket_label': f'Winners TOP {top}'}, ts, Path('/x/events/e'), False)


def _t(ts, guests):
    return D.Tournament(timestamp=ts, event_path='/x/events/e', standings=[(1, 1), (2, 2)],
                        matches=[_m(1, 2, ts, 8)], event_id=7, guest_matches=guests)


def test_guest_uid():
    assert G.guest_uid(110195494) == -110195494
    for bad in (None, 0, -5, '12', 1.0, True):
        assert G.guest_uid(bad) is None
    assert G.is_guest(-3) and not G.is_guest(3) and not G.is_guest(None)


def test_rated_only_since():
    old, new = G.RATED_SINCE_TS - 86400, G.RATED_SINCE_TS + 3600
    t_old = _t(old, [_m(1, -100, old)]); t_new = _t(new, [_m(-101, 2, new)])
    assert G.rated(t_old) == () and list(G.rated(t_new)) == t_new.guest_matches
    assert len(G.of(t_old)) == 1 and len(G.with_guests(t_old)) == 2
    assert [m.loser_id for m in G.with_rated(t_old, _bracket_order_key)] == [2]
    # 進行順に混ざる (TOP 64 の試合が TOP 8 より先)
    assert [(m.winner_id, m.loser_id) for m in G.with_rated(t_new, _bracket_order_key)] == [(-101, 2), (1, 2)]
    # 古い pickle の Tournament (項目が無い) でも動く
    t_bare = _t(new, []); del t_bare.guest_matches
    assert G.of(t_bare) == () and G.rated(t_bare) == () and G.with_guests(t_bare) is t_bare.matches


def test_digest_changes_only_when_rated():
    old, new = G.RATED_SINCE_TS - 86400, G.RATED_SINCE_TS + 3600
    assert tournament_digest(_t(old, []), {}) == tournament_digest(_t(old, [_m(1, -100, old)]), {})
    assert tournament_digest(_t(new, []), {}) != tournament_digest(_t(new, [_m(1, -100, new)]), {})


def test_display_name():
    G.NAMES[-100] = 'ゆうき'; G.NAMES[-101] = None
    try:
        assert format_display(None, -100) == 'ゆうき'
        assert format_display(None, -101) == '' and format_display(None, -999) == ''
        assert format_display(None, 5).startswith('(unknown')
    finally:
        G.NAMES.pop(-100, None); G.NAMES.pop(-101, None)


def test_register_names_from_tournaments():
    # build の段は大会を読み込み直さないので、名前は大会が持っている分から集める
    t = _t(G.RATED_SINCE_TS + 3600, [_m(1, -555, 0)]); t.guest_names = {-555: 'ゆうき'}
    bare = _t(0, []); del bare.guest_names
    try:
        G.register([t, bare])
        assert format_display(None, -555) == 'ゆうき'
    finally:
        G.NAMES.pop(-555, None)


if __name__ == '__main__':
    test_guest_uid()
    test_rated_only_since()
    test_digest_changes_only_when_rated()
    test_display_name()
    test_register_names_from_tournaments()
    print('ok')

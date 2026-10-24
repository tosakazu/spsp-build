"""学習状態のチェックポイント保存・再開 (v4/learn_state.py, eval_bt_lv_shared.g2_learn_advance) の検査。

2021-06-30 までの大会 (数百件、約 30 秒) で:
  1. 一括学習と、2 か所で保存・再開 (pickle 往復) した学習の出力が完全一致 (dict の並びも)
  2. チェックポイント以前の大会を書き換える / 大会を足す / Settings を変える → 再開を拒否 (StaleCheckpoint)
本番規模のフル vs 差分は tests/golden/incremental_vs_full.sh。

実行: OPENBLAS_NUM_THREADS=1 .venv/bin/python3 tests/meta/test_learn_checkpoint.py
(大会データが無い環境では skip)
"""
import copy
import datetime as dt
import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)
sys.argv = ['x']

from spsp.paths import EVENTS_ROOT  # noqa: E402
if not EVENTS_ROOT.is_dir():
    print('skip: 大会データが無い'); sys.exit(0)

import spsp.cli.build_full as B  # noqa: E402
import spsp.learner as E  # noqa: E402
from spsp.settings import Settings as _S0  # noqa: E402
E.apply_settings(_S0.from_env(), announce=True)
from spsp.common import GATED_M_TOUR  # noqa: E402
from spsp.learn_state import G2LearnState, StaleCheckpoint, save_state, load_state, cutoff_ts_for_date  # noqa: E402
from spsp.settings import Settings  # noqa: E402

E.apply_settings(Settings())
EVAL = dt.datetime(2021, 6, 30, 23, 59, 59)
all_t, t_meta = B._load_tournaments_and_meta(EVAL)
assert len(all_t) > 100, len(all_t)
targets = sorted({int(E.PT_DECAY_REF.timestamp())} | {int(dt.datetime(y, m, 1).timestamp()) for y in (2019, 2020, 2021) for m in range(1, 13) if dt.datetime(y, m, 1) < EVAL})
path_of = {id(t): t.event_path for t in all_t}





def norm(bt_at_t, snaps, tl, per_tour, per_match, pt, rolling=None):
    peak_at, lv_at, casc_at = snaps
    return {
        'bt_at_t': sorted((path_of[k].split('/events/', 1)[1], sorted(v.items())) for k, v in bt_at_t.items()),
        'peak_at': {ts: list(d.items()) for ts, d in peak_at.items()},
        'lv_at': {ts: list(d.items()) for ts, d in lv_at.items()},
        'casc_at': {ts: tuple(list(x.items()) for x in tpl) for ts, tpl in casc_at.items()},
        'timeline': list(tl.items()), 'per_tour': list(per_tour.items()), 'per_match': list(per_match.items()),
        'pt_at_time': sorted((path_of[k].split('/events/', 1)[1], sorted(v.items())) for k, v in (pt or {}).items()),
        'rolling': rolling,
    }


def outputs(st):
    return norm(st.bt_at_t_view(all_t), E.g2_snapshots(st, all_t, t_meta, targets), dict(st.bt_timeline),
                st.bt_per_tour, st.bt_per_match, st.pt_at_time_view(all_t), list(st.rolling.deltas_by_path.items()))


with tempfile.TemporaryDirectory() as tmp:
    st = G2LearnState(True, True, True, E._S.state_repr())
    E.g2_learn_advance(st, all_t, t_meta, GATED_M_TOUR, None)
    full = outputs(st)

    st = G2LearnState(True, True, True, E._S.state_repr())
    x1, x2 = cutoff_ts_for_date('2020-03-31'), cutoff_ts_for_date('2021-01-31')
    n1 = E.g2_learn_advance(st, all_t, t_meta, GATED_M_TOUR, x1); save_state(st, f'{tmp}/s1.pkl')
    st = load_state(f'{tmp}/s1.pkl', E._S.state_repr(), (True, True, True))
    n2 = E.g2_learn_advance(st, all_t, t_meta, GATED_M_TOUR, x2); save_state(st, f'{tmp}/s2.pkl')
    st = load_state(f'{tmp}/s2.pkl', E._S.state_repr(), (True, True, True))
    # 海外タグ集合の増減: 処理済み大会に出ていない uid なら再開できる / 出ている uid なら拒否
    import spsp.common as _C
    from spsp.learn_state import check_overseas as _co
    _before = {u for t in all_t if t.timestamp < st.cursor_ts for _, u in t.standings}
    _after_only = {u for t in all_t if t.timestamp >= st.cursor_ts for _, u in t.standings} - _before
    assert _before and _after_only
    _orig = _C.OVERSEAS_UIDS
    _before -= _orig; _after_only -= _orig          # もともと海外タグの uid を足しても集合は変わらない
    assert _before and _after_only
    try:
        _C.OVERSEAS_UIDS = frozenset(_orig | {min(_after_only)})
        _st = load_state(f'{tmp}/s2.pkl', E._S.state_repr(), (True, True, True)); _co(_st, all_t)
        assert frozenset(_st.overseas_uids) == _C.OVERSEAS_UIDS, '再開時に集合が更新されない'
        _C.OVERSEAS_UIDS = frozenset(_orig | {min(_before)})
        _st = load_state(f'{tmp}/s2.pkl', E._S.state_repr(), (True, True, True))
        try:
            _co(_st, all_t); raise AssertionError('処理済み大会に出ている uid の変化で再開を拒否しない')
        except StaleCheckpoint:
            pass
    finally:
        _C.OVERSEAS_UIDS = _orig
    print('OK: 海外タグ集合の変化は処理済み大会に出ている選手のときだけ再開を拒否')
    n3 = E.g2_learn_advance(st, all_t, t_meta, GATED_M_TOUR, None)
    assert n1 > 0 and n2 > 0 and n3 > 0 and n1 + n2 + n3 == len(all_t), (n1, n2, n3, len(all_t))
    inc = outputs(st)
    for k in full:
        assert inc[k] == full[k], f'再開後の {k} が一括学習と違う'
    print(f'OK: 一括学習と保存・再開 ({n1}/{n2}/{n3} 大会) の出力が完全一致 ({len(all_t)} 大会, {len(targets)} スナップショット)')

    # 変更検知
    st = load_state(f'{tmp}/s2.pkl', E._S.state_repr(), (True, True, True))
    t0 = [t for t in all_t if t.timestamp < x1][10]
    orig = t0.standings; t0.standings = list(orig[:-1])
    try:
        E.g2_learn_advance(st, all_t, t_meta, GATED_M_TOUR, None); raise AssertionError('standings の変更を検知しなかった')
    except StaleCheckpoint:
        pass
    t0.standings = orig
    st = load_state(f'{tmp}/s2.pkl', E._S.state_repr(), (True, True, True))
    extra = copy.copy(all_t[5]); extra.event_path = all_t[5].event_path + '_dup'
    t_meta[id(extra)] = t_meta[id(all_t[5])]
    try:
        E.g2_learn_advance(st, sorted(all_t + [extra], key=lambda t: t.timestamp), t_meta, GATED_M_TOUR, None)
        raise AssertionError('大会の追加を検知しなかった')
    except StaleCheckpoint:
        pass
    try:
        load_state(f'{tmp}/s2.pkl', repr(Settings(prov_2y_min=4)), (True, True, True)); raise AssertionError('Settings の違いを検知しなかった')
    except StaleCheckpoint:
        pass
    print('OK: 過去大会の変更 / 追加 / Settings の違いで再開を拒否')

    # データの置き場所が変わっても (絶対パスが違っても) 同じチェックポイントから再開できる
    assert not any(pth.startswith('/') for pth, _, _ in st.ledger), '台帳に絶対パスが入っている'
    # 別の置き場所 (= 実ファイルへの symlink) から同じデータを読んだ体にする
    alt_root = os.path.join(tmp, 'elsewhere', 'data', 'startgg')
    os.makedirs(alt_root)
    os.makedirs(os.path.join(alt_root, 'Japan'))
    os.symlink(str(EVENTS_ROOT), os.path.join(alt_root, 'Japan', 'events'))   # 新配置 data/startgg/<地域>/events
    moved = [copy.copy(t) for t in all_t]
    for t in moved:
        t.event_path = os.path.join(alt_root, 'Japan', 'events', t.event_path.split('/events/', 1)[1])
        t_meta[id(t)] = t_meta[id(all_t[moved.index(t)])]
        path_of[id(t)] = t.event_path
        t.matches = [copy.replace(m, event_path=os.path.join(alt_root, 'Japan', 'events', m.event_path.split('/events/', 1)[1])) if '/events/' in (m.event_path or '') else m for m in t.matches]
    st = load_state(f'{tmp}/s2.pkl', E._S.state_repr(), (True, True, True))
    n = E.g2_learn_advance(st, moved, t_meta, GATED_M_TOUR, None)
    assert n == n3 and len(st.bt_at_t_view(moved)) == len(all_t), (n, n3)
    inc2 = norm(st.bt_at_t_view(moved), E.g2_snapshots(st, moved, t_meta, targets), dict(st.bt_timeline), st.bt_per_tour, st.bt_per_match, st.pt_at_time_view(moved), list(st.rolling.deltas_by_path.items()))
    for k in full:
        assert inc2[k] == full[k], f'置き場所を変えた再開で {k} が違う'
    print('OK: データの絶対パスが変わっても同じチェックポイントから再開でき、出力も一致')

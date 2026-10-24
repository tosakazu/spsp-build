"""学習状態のファイル形式 (spsp/learn_state.py save_state / load_state)。

- save_state は属性ごとに別の pickle で書く (STATE_FILE_FORMAT)。読み直した状態は、状態そのものを 1 回で pickle した
  旧形式を読み直したものと属性ごとに同じ
- 旧形式 (状態そのものの pickle = 2026-10-17 より前のチェックポイント) も load_state で読める

実行: python3 tests/meta/test_state_file_format.py
"""
import pickle
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from spsp.learn_state import (G2LearnState, save_state, load_state, STATE_FILE_FORMAT,  # noqa: E402
                              StaleCheckpoint)


def _state():
    st = G2LearnState(True, True, True, 'settings-x')
    st.cursor_ts = 1234
    st.ledger = [('Japan/2026/a', 1000, 'd1'), ('Japan/2026/b', 1100, 'd2')]
    st.ratings = {7: (1500.0, 300.0), 12: (1400.0, 250.0)}
    st.rating_hist.add(7, 1000, 1500.0)
    st.bt_per_tour.add(7, 1001, True, 0.1, 0.2, 50.0, 51.0)
    st.bt_per_match.add(1001, 7, 12, 0.1, -0.1, True, True)
    st.pt_meas_set = {7, 12, 99}
    return st


def _attrs(st):
    return {k: pickle.dumps(v, protocol=pickle.HIGHEST_PROTOCOL) for k, v in vars(st).items()}


def test_roundtrip_same_as_old_format():
    st = _state()
    with tempfile.TemporaryDirectory() as tmp:
        p_new = Path(tmp) / 'new.pkl'; p_old = Path(tmp) / 'old.pkl'
        save_state(st, p_new)
        with open(p_new, 'rb') as f:
            head = pickle.load(f)
        assert head['format'] == STATE_FILE_FORMAT and head['keys'] == list(vars(st))
        with open(p_old, 'wb') as f:
            pickle.dump(st, f, protocol=pickle.HIGHEST_PROTOCOL)   # 旧形式
        a = load_state(p_new, 'settings-x', (True, True, True))
        b = load_state(p_old, 'settings-x', (True, True, True))
        assert type(a) is G2LearnState and type(b) is G2LearnState
        assert _attrs(a) == _attrs(b)
        assert a.bt_per_match == st.bt_per_match and a.rolling.n == st.rolling.n
        try:
            load_state(p_new, 'settings-y', (True, True, True)); raise AssertionError('Settings の違いを検知しなかった')
        except StaleCheckpoint:
            pass


def test_upgrade_dict_logs():
    """2026-10-18 より前の状態 (bt_per_tour / bt_per_match が dict、順位変動が 8 項目の dict) は読み込み時に変換する。"""
    st = _state()
    want_tour, want_match = list(st.bt_per_tour.items()), list(st.bt_per_match.items())
    st.bt_per_tour = {(7, 1001): {'bt_used': True, 'bt_internal_d': 0.1, 'bt_d': 0.2, 'after': 50.0, 'peak_after': 51.0}}
    st.bt_per_match = {(1001, 7, 12): [[0.1, -0.1, True, True]]}
    st.rolling.deltas_by_path = {'Japan/2026/a': {}, 'Japan/2026/b': {7: {'tjpr_d': 1, 'raw_at': 3.5}, 12: {'raw_at': 0.0}}}
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / 'old.pkl'
        with open(p, 'wb') as f:
            pickle.dump(st, f, protocol=pickle.HIGHEST_PROTOCOL)
        a = load_state(p, 'settings-x', (True, True, True))
    assert list(a.bt_per_tour.items()) == want_tour and list(a.bt_per_match.items()) == want_match
    assert a.rolling.deltas_by_path == {'Japan/2026/a': {}, 'Japan/2026/b': {7: 3.5, 12: 0.0}}


if __name__ == '__main__':
    test_roundtrip_same_as_old_format()
    test_upgrade_dict_logs()
    print('ok')

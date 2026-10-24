"""学習状態 (pickle) の旧モジュール名の読み替えと、設定の一致検査のテスト (plain assert)。

- 旧コードで保存した pickle は `eval_bt_lv_shared._G2Peak` を参照する。読み込み時に v4.learner へ
  読み替え、旧モジュール (実験用 shim) を import しないこと (import すると import 時処理が設定を潰す)。
- 学習器の適用中設定と学習状態の設定が違えば g2_learn_advance は例外で止まること。
"""
import os, sys, pickle, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
import spsp.learner as L  # noqa: E402
from spsp import learn_state as LS  # noqa: E402
from spsp.settings import Settings  # noqa: E402


def test_alias_unpickler_does_not_import_shim():
    sys.modules.pop('eval_bt_lv_shared', None)
    obj = L._G2Peak()
    data = pickle.dumps(obj, protocol=2)
    assert b'cspsp.learner\n_G2Peak\n' in data, data[:60]
    old = data.replace(b'cspsp.learner\n_G2Peak\n', b'ceval_bt_lv_shared\n_G2Peak\n')
    before = L._S.state_repr()
    with tempfile.NamedTemporaryFile('wb', suffix='.pkl', delete=False) as f:
        f.write(old); path = f.name
    try:
        with open(path, 'rb') as f:
            got = LS._AliasUnpickler(f).load()
    finally:
        os.unlink(path)
    assert type(got) is L._G2Peak, type(got)
    # 旧名 (v4.learner) も読み替えられること
    old2 = data.replace(b'cspsp.learner\n_G2Peak\n', b'cv4.learner\n_G2Peak\n')
    import io
    assert type(LS._AliasUnpickler(io.BytesIO(old2)).load()) is L._G2Peak
    assert 'eval_bt_lv_shared' not in sys.modules, "旧モジュール (shim) が import された"
    assert L._S.state_repr() == before, "読み込みで設定が変わった"


def test_learn_advance_rejects_settings_mismatch():
    st = LS.G2LearnState(False, False, False, Settings(per_tour_lv=not L._S.per_tour_lv).state_repr())
    try:
        L.g2_learn_advance(st, [], {}, 20)
    except RuntimeError as e:
        assert '設定' in str(e)
    else:
        raise AssertionError('設定の不一致で止まらなかった')


def test_lock_blocks_reapply():
    L.lock_settings()
    cur = L._S
    L.apply_settings(Settings(per_tour_lv=not cur.per_tour_lv))
    assert L._S is cur, 'lock 中に設定が書き換わった'
    L._LOCKED = False


if __name__ == '__main__':
    test_alias_unpickler_does_not_import_shim()
    test_learn_advance_rejects_settings_mismatch()
    test_lock_blocks_reapply()
    print('ok')

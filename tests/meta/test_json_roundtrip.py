"""v4.overlay.json_roundtrip (orjson) が標準 json の往復 json.loads(json.dumps(x)) と、型・値・dict の順序込みで
一致することを確かめる。golden3 (~/spsp-golden/20260906-legacy-order/build) があれば全出力 (players / tournaments /
jsonl 全行 / meta.json) で、無ければ辺縁ケースだけで検査する。

実行: .venv/bin/python3 tests/meta/test_json_roundtrip.py   (golden あり: 約 1 分)
"""
import glob
import json
import os
import random
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)
from spsp.overlay import json_roundtrip  # noqa: E402


def rt_json(o):
    return json.loads(json.dumps(o, ensure_ascii=False))


def sig(o):
    """型と値と順序を全部含む署名"""
    if isinstance(o, dict):
        return ('d', tuple((type(k).__name__, k, sig(v)) for k, v in o.items()))
    if isinstance(o, list):
        return ('l', tuple(sig(v) for v in o))
    return (type(o).__name__, repr(o))


def same(o):
    a, b = rt_json(o), json_roundtrip(o)
    return sig(a) == sig(b) and json.dumps(a, ensure_ascii=False) == json.dumps(b, ensure_ascii=False)


random.seed(1)
cases = {
    'int キー / 順序': {3: 'a', 1: 'b', 2: {10: [1, 2], 5: 'x'}},
    'tuple / 入れ子': {'t': (1, 2, (3, 4)), 'l': [(1, 'a')]},
    '非ASCII / 制御文字': {'名前': 'スマパ！#17 ～ “引用”   \t \\ /', 'k': '\x01\x1f'},
    '浮動小数': {'f': [0.1 + 0.2, 1e16, 1e-7, 1.5e300, 5e-324, 2 ** 53 + 1.0, 123456789.123456789, 0.000123, 100.0, -1.0, 1.0, -0.0]},
    '乱数 float': {'f': [random.uniform(-1e6, 1e6) for _ in range(5000)] + [random.random() * 10 ** random.randint(-10, 10) for _ in range(5000)]},
    '整数': {'i': [2 ** 53 + 1, 2 ** 62, -(2 ** 62), 9007199254740993, 0, -1]},
    'bool/None': {'b': [True, False, None], True: 1, None: 2, 1.5: 3},
}
for name, o in cases.items():
    assert same(o), f'orjson 往復が json 往復と違う: {name}'
print(f'OK: 辺縁ケース {len(cases)} 件で orjson 往復 == json 往復 (型・値・順序)')

G = os.path.expanduser('~/spsp-golden/20260906-legacy-order/build')
if not os.path.isdir(G):
    print('skip: golden3 が無いので実データの検査は省略'); sys.exit(0)
n = 0
for f in sorted(glob.glob(f'{G}/players/*.json')) + sorted(glob.glob(f'{G}/tournaments/*.json')) + [f'{G}/meta.json']:
    o = json.load(open(f, encoding='utf-8'))
    assert same(o), f'orjson 往復が json 往復と違う: {f}'
    n += 1
for line in open(f'{G}/latest_tjpr_full.jsonl', encoding='utf-8'):
    if line.strip():
        assert same(json.loads(line)), 'orjson 往復が json 往復と違う: jsonl'
        n += 1
print(f'OK: golden3 の全出力 {n} 件で orjson 往復 == json 往復')


# ビルド中のレコードに混ざる numpy.float64 (標準 json は float 派生として通す) が同じ結果になること
import numpy as _np
import json as _json
from spsp.overlay import json_roundtrip as _rt
_cases = [_np.float64(0.1), _np.float64(1e-300), _np.float64(12345.678), _np.float64(-0.0), _np.float64(2.0) ** 60]
_obj = {'a': _cases, 'b': {1: _np.float64(3.5)}, 'c': [(_np.float64(1.5), 2)]}
assert _rt(_obj) == _json.loads(_json.dumps(_obj, ensure_ascii=False)), 'numpy.float64 の往復が標準 json と違う'
assert _json.dumps(_rt(_obj)) == _json.dumps(_json.loads(_json.dumps(_obj, ensure_ascii=False))), 'numpy.float64 の往復 (順序込み) が違う'
try:
    _rt({'x': _np.int64(1)}); raise AssertionError('numpy.int64 を通してしまった (標準 json も拒否する型)')
except TypeError:
    pass
print('OK: numpy.float64 の往復も標準 json と一致、numpy.int64 は拒否')

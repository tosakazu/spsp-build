"""snap のキャッシュの鍵 (spsp/learner._snap_code_hash) から外した書き出し側のファイルを、学習側のファイルが使っていないか。

鍵に入るファイル (spsp/ 直下で _SNAP_CODE_EXCLUDE 以外 + spsp/raters/) が、外したファイルを import していたら失敗する
(関数の中の遅延 import も数える)。使っていると、外したファイルだけ直したときに古いスナップショットの値が使われてしまう。
学習も使う判定は spsp/common.py に置く (2026-10-23 に t_passes / level_tier_values / _rolling_lv2_raw_per_tour を移した)。

実行: OPENBLAS_NUM_THREADS=1 .venv/bin/python3 tests/meta/test_snap_cache_scope.py
(リポジトリルートから。unittest 等は使わず plain assert)
"""
import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from spsp.learner import _SNAP_CODE_EXCLUDE  # noqa: E402

# 書き出しのときだけ通る遅延 import (学習・スナップショットでは呼ばれない。2026-10-23 に learn / snap の実行を追って確認)
ALLOWED = {
    ('columnar.py', 'lazy_views'),   # PerMatchLog / PerTourLog の読み出し (build が使う)
}

spsp = ROOT / 'spsp'
excluded_mods = {f[:-3] for f in _SNAP_CODE_EXCLUDE}
missing = sorted(f for f in _SNAP_CODE_EXCLUDE if not (spsp / f).exists())
assert not missing, f'_SNAP_CODE_EXCLUDE に無いファイルがある (名前を変えた?): {missing}'


def imported_spsp_modules(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith('spsp.'):
                yield node.module.split('.')[1]
            elif node.module == 'spsp':
                for a in node.names:
                    yield a.name
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith('spsp.'):
                    yield a.name.split('.')[1]


keyed = [f for f in sorted(spsp.glob('*.py')) if f.name not in _SNAP_CODE_EXCLUDE] + sorted((spsp / 'raters').glob('*.py'))
bad = []
for f in keyed:
    for m in imported_spsp_modules(ast.parse(f.read_text(), str(f))):
        if m in excluded_mods and (f.name, m) not in ALLOWED:
            bad.append(f'{f.relative_to(ROOT)} → spsp.{m}')
assert not bad, '鍵に入るファイルが鍵から外したファイルを import している (spsp/common.py へ移すか _SNAP_CODE_EXCLUDE から外す):\n  ' + '\n  '.join(bad)
print(f'ok: 鍵に入る {len(keyed)} ファイル、外した {len(_SNAP_CODE_EXCLUDE)} ファイル')

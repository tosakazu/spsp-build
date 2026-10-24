"""deploy/build_event_icons.py (大会 → 大会アイコンの表) の検査。実行: .venv/bin/python3 tests/meta/test_event_icons.py"""
import json
import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'deploy'))
import build_event_icons as B  # noqa: E402


def test_build():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, 't.jsonl')
        with open(p, 'w') as f:
            f.write(json.dumps({'tournament_id': 1, 'events': [{'event_id': 10}, {'event_id': 11}]}) + '\n')
            f.write(json.dumps({'tournament_id': 2, 'events': [{'event_id': 20}]}) + '\n')   # 同じ画像 → 1 つにまとめる
            f.write(json.dumps({'tournament_id': 3, 'events': [{'event_id': 30}]}) + '\n')   # アイコンなし
            f.write('\n')
        index = {'1': {'file': 'h/aaa.png'}, '2': {'file': 'h/aaa.png'}, '9': {'file': 'h/zzz.png'}}
        out = B.build(__import__('pathlib').Path(p), index)
        assert out == {'f': ['aaa.png'], 'e': {'10': 0, '11': 0, '20': 0}}, out


if __name__ == '__main__':
    test_build()
    print('ok')

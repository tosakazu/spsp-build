"""spsp/index_table.py (users.jsonl / tournaments.jsonl が users/<n>.jsonl に分割されていても読む) の検査。
実行: .venv/bin/python3 tests/meta/test_index_table.py"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'deploy'))
from spsp import index_table  # noqa: E402
import build_event_icons as B  # noqa: E402


def u(uid):
    return json.dumps({'user_id': uid, 'country': 'Japan', 'startgg_discriminator': f'd{uid}'}) + '\n'


def test_single_and_split_read_the_same_lines():
    with tempfile.TemporaryDirectory() as d:
        single = Path(d, 'a', 'users.jsonl'); single.parent.mkdir()
        single.write_text(u(3_000_000) + u(1) + u(600_000))
        split = Path(d, 'b', 'users.jsonl'); (split.parent / 'users').mkdir(parents=True)
        (split.parent / 'users' / '0.jsonl').write_text(u(1))
        (split.parent / 'users' / '1.jsonl').write_text(u(600_000))
        (split.parent / 'users' / '6.jsonl').write_text(u(3_000_000))
        (split.parent / 'users' / '10.jsonl').write_text('')   # 数値順 (文字列順なら 10 が 6 より前)
        assert [f.name for f in index_table.files(split)] == ['0.jsonl', '1.jsonl', '6.jsonl', '10.jsonl']
        assert sorted(index_table.iter_lines(single)) == sorted(index_table.iter_lines(split))
        assert index_table.exists(single) and index_table.exists(split)
        assert not index_table.exists(Path(d, 'none', 'users.jsonl'))
        split.write_text(u(1))   # 両方ある = 壊れた状態
        try:
            index_table.files(split)
        except RuntimeError:
            pass
        else:
            raise AssertionError('both layouts must be refused')


def test_event_icons_from_split_tournaments():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d, 'tournaments.jsonl'); (Path(d) / 'tournaments').mkdir()
        (Path(d) / 'tournaments' / '0.jsonl').write_text(json.dumps({'tournament_id': 1, 'events': [{'event_id': 10}]}) + '\n')
        (Path(d) / 'tournaments' / '9.jsonl').write_text(json.dumps({'tournament_id': 900_001, 'events': [{'event_id': 20}]}) + '\n')
        out = B.build(p, {'1': {'file': 'h/a.png'}, '900001': {'file': 'h/b.png'}})
        assert out == {'f': ['a.png', 'b.png'], 'e': {'10': 0, '20': 1}}, out


if __name__ == '__main__':
    test_single_and_split_read_the_same_lines()
    test_event_icons_from_split_tournaments()
    print('ok')

"""spsp/cli/build_partner_smatch.py (スマッチに渡す一覧) の検査。
実行: .venv/bin/python3 tests/meta/test_partner_smatch.py
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)
from spsp.cli import build_partner_smatch as B  # noqa: E402


def test_build():
    rows = [{'user_id': 10, 'ranks': {'ensemble': 2}}, {'user_id': 20, 'ranks': {'ensemble': 1}},
            {'user_id': 30, 'ranks': {'ensemble': None}}, {'user_id': 40, 'ranks': {'ensemble': 3}}]
    discs = {'10': 'aaaaaaaa', '20': 'bbbbbbbb', '30': 'cccccccc', '11': 'dddddddd', '12': 'aaaaaaaa'}   # 40 は discriminator 不明
    merges = {'11': 10, '12': 10}   # 旧アカウント 2 つ (12 は同じ discriminator = 重ねない)
    idx = {'characters': [{'id': 1, 'name': 'マリオ'}, {'id': 2, 'name': 'ルイージ'}],
           'main_by_char': {'1': [10], '2': [20]}, 'sub_by_char': {'2': [10], '1': [10]}}
    out = B.build(rows, discs, merges, idx, '2026-11-15', 1794700000)
    assert {k: v for k, v in out.items() if k != 'players'} == {
        'version': 1, 'generated_at': 1794700000, 'counted_at': '2026-11-15', 'ranked_total': 3,
        'url_template': 'https://spsp.games/jp/p/?d={discriminator}'}, out
    # 順位の順、順位の無い人は最後。discriminator の分からない人 (40) は載せない
    assert out['players'] == [
        {'discriminators': ['bbbbbbbb'], 'rank': 1, 'characters': ['ルイージ']},
        {'discriminators': ['aaaaaaaa', 'dddddddd'], 'rank': 2, 'characters': ['マリオ', 'ルイージ']},   # 主に使うキャラが先、同じキャラは 1 回
        {'discriminators': ['cccccccc'], 'rank': None, 'characters': []},
    ], out['players']


if __name__ == '__main__':
    test_build()
    print('ok')

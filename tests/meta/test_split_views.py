"""spsp/cli/build_split_views.py (ページ用に分けた一覧 data/views/) の検査。

小さな合成データで、分けたファイルを全部読み戻すと元の 2 ファイル (latest_tjpr_full.jsonl / players_current.json) の
中身にちょうど戻ること、表示順位が site/ranking-table.js と同じ数え方になることを確かめる。
実行: .venv/bin/python3 tests/meta/test_split_views.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)
from spsp.cli import build_split_views as bsv  # noqa: E402


def rec(uid, ens, tjpr, bt, prov=False, char=None):
    return {'user_id': uid, 'display': f'p{uid}', 'ranks': {'ensemble': ens, 'tjpr': tjpr, 'bt_gated': bt},
            'scores': {'tjpr_score': 100 - (ens or 99), 'bt_gated_elo': 2000 - (bt or 99), 'shared_cascade_lv': 3},
            'metadata': {'provisional': prov}, 'top_tours': [], 'main_char_id': char}


def main():
    rows = [rec(10, 1, 2, 1, char=5), rec(11, 2, 1, 3), rec(12, 3, 3, 2, prov=True, char=5), rec(13, 4, 4, 4),
            rec(300, None, None, None)]                       # 順位なしは末尾
    rows += [rec(1000 + i, 5 + i, 5 + i, 5 + i) for i in range(2500)]   # ranking/ が 3 ファイルに分かれる
    current = {'format': 1, 'eval_date': '2026-10-01', 'eval_ts': 1, 'decay_r': 0.5, 'columns': ['ranks.ensemble'],
               'players': {str(r['user_id']): [(r['ranks'] or {}).get('ensemble')] for r in rows}}
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, 'data'))
        with open(os.path.join(d, 'latest_tjpr_full.jsonl'), 'w') as f:
            for r in rows:
                f.write(json.dumps(r) + '\n')
        json.dump(current, open(os.path.join(d, 'players_current.json'), 'w'))
        json.dump({'main_by_char': {'5': [12, 10]}, 'sub_by_char': {'1317': [13]}},
                  open(os.path.join(d, 'data', 'character_index.json'), 'w'))
        json.dump({'10': '東京都', '13': '東京都', '11': '大阪府'}, open(os.path.join(d, 'data', 'player_prefectures.json'), 'w'))
        json.dump({'uids': [11]}, open(os.path.join(d, 'data', 'overseas.json'), 'w'))
        json.dump({'units': [{'id': '東京都', 'order': 13}, {'id': '大阪府', 'order': 27}]},
                  open(os.path.join(d, 'data', 'geo.json'), 'w'))
        json.dump({'10': '2026-09-30'}, open(os.path.join(d, 'data', 'last_activity.json'), 'w'))

        os.makedirs(os.path.join(d, 'players'))
        json.dump({'characters': [{'id': 7, 'pct': 0.2}, {'id': 5, 'pct': 0.8}]}, open(os.path.join(d, 'players', '10.json'), 'w'))
        # サブはデイジー (1277) → 統合先のピーチ (1317) で当てる
        json.dump({'characters': [{'id': 5, 'pct': 0.7}, {'id': 1277, 'pct': 0.3}]}, open(os.path.join(d, 'players', '13.json'), 'w'))
        m = bsv.build_views(d)
        v = os.path.join(d, 'data', 'views')
        ld = lambda rel: json.load(open(os.path.join(v, rel)))  # noqa: E731
        assert m['ranking_chunks'] == {'ensemble': 3, 'tjpr': 3, 'bt_gated': 3}, m
        assert ld('manifest.json') == m

        # ranking/: 連結すると全員がちょうど 1 回ずつ、method の順位順 (順位なしは末尾)
        for meth in bsv.METHODS:
            got = [r for k in range(3) for r in ld(f'ranking/{meth}/{k}.json')]
            assert sorted(r['user_id'] for r in got) == sorted(r['user_id'] for r in rows), meth
            rk = [r['ranks'][meth] for r in got if r['ranks'][meth]]
            assert rk == sorted(rk), meth
            assert got[-1]['user_id'] == 300, meth
            for r in got:   # 元の行 + 表示用の 2 キーだけ
                orig = next(o for o in rows if o['user_id'] == r['user_id'])
                assert {k: x for k, x in r.items() if not k.startswith('_')} == orig
        top = {r['user_id']: r for r in ld('ranking/ensemble/0.json')}
        # 灰色 = 11 (海外), 12 (計測中)。ranking-table.js と同じく灰色は「上の非灰色の数 + 1」で、非灰色の数に入らない
        assert top[10]['_display_rank']['ensemble'] == 1
        assert top[11]['_display_rank']['ensemble'] == 2 and top[11]['_is_overseas'] is True
        assert top[12]['_display_rank']['ensemble'] == 2
        assert top[13]['_display_rank']['ensemble'] == 2
        assert top[11]['_display_rank']['tjpr'] == 1 and top[10]['_display_rank']['tjpr'] == 1

        # current/: 全シャードを合わせると players_current.json の players にちょうど戻る、ヘッダも同じ
        merged = {}
        for i in range(bsv.CURRENT_SHARDS):
            s = ld(f'current/{i}.json')
            assert {k: x for k, x in s.items() if k != 'players'} == {k: x for k, x in current.items() if k != 'players'}
            assert all(int(u) % bsv.CURRENT_SHARDS == i for u in s['players'])
            merged.update(s['players'])
        assert merged == current['players']

        # players_index: 全員、総合順位順、列の値
        idx = ld('players_index.json')
        cols = idx['columns']
        assert len(idx['rows']) == len(rows)
        row10 = dict(zip(cols, idx['rows'][0]))
        assert row10 == {'uid': 10, 'name': 'p10', 'ens': 1, 'tjpr': 2, 'bt': 1, 'lv': 3, 'prov': 0, 'ovs': 0,
                         'char': 5, 'pref': 13, 'last': 20726, 'b': 0}, row10
        assert dict(zip(cols, idx['rows'][1]))['ovs'] == 1
        assert idx['rows'][-1][0] == 300   # 順位なしは末尾

        # char / pref / summary
        assert [r['user_id'] for r in ld('char/5.json')] == [12, 10]
        # 使用率: players/<uid>.json に実績がある人だけ characters が付く
        c5 = {r['user_id']: r for r in ld('char/5.json')}
        assert c5[10]['characters'] == [{'id': 5, 'pct': 0.8}] and 'characters' not in c5[12]
        assert ld('char_sub/1317.json') == [{**rows[3], 'characters': [{'id': 1277, 'pct': 0.3}]}]
        assert m['chars'] == 1 and m['chars_sub'] == 1
        assert [r['user_id'] for r in ld('pref/13.json')] == [10, 13]
        units = ld('pref_summary.json')['units']
        # 大阪府は海外勢 (11) だけなので 1 位なし
        assert [(u['id'], u['n'], (u['top'] or {}).get('uid')) for u in units] == [('東京都', 2, 10), ('大阪府', 1, None)], units

        # sim_pop: 灰色 = 海外 or 計測中
        sim = ld('sim_pop.json')
        g = {r[0]: r[sim['columns'].index('gray')] for r in sim['rows']}
        assert (g[10], g[11], g[12], g[13]) == (0, 1, 1, 0)

        assert m['bundle_shards'] == 0 and not os.path.exists(os.path.join(d, '_unbundled_players.txt'))

        # まとめ (SPSP_BUNDLE_SMALL=1): 選手ファイルが小さい人の players/ と history/ をバイト列のまま uid で束ねる
        os.makedirs(os.path.join(d, 'history'))
        big = b'{"characters":[],"pad":"' + b'x' * bsv.BUNDLE_MAX_BYTES + b'"}'
        open(os.path.join(d, 'players', '13.json'), 'wb').write(big)
        open(os.path.join(d, 'players', '11.json'), 'wb').write(b'{"a":1}')
        open(os.path.join(d, 'history', '11.json'), 'wb').write(b'[[1,2]]')
        os.environ['SPSP_BUNDLE_SMALL'] = '1'
        try:
            m = bsv.build_views(d)
        finally:
            del os.environ['SPSP_BUNDLE_SMALL']
        assert m['bundle_shards'] == 16 and m['bundled'] == 2, m      # 10 と 11 (13 は大きい)
        got = {}
        for i in range(16):
            b = ld(f'bundle/{i}.json')
            assert b['shards'] == 16 and all(int(u) % 16 == i for u in b['players'])
            got.update(b['players'])
        assert got == {'10': {'p': {'characters': [{'id': 7, 'pct': 0.2}, {'id': 5, 'pct': 0.8}]}, 'h': None},
                       '11': {'p': {'a': 1}, 'h': [[1, 2]]}}, got
        assert open(os.path.join(d, '_unbundled_players.txt')).read() == '13.json\n'
        idx = ld('players_index.json')
        b_of = {r[0]: r[idx['columns'].index('b')] for r in idx['rows']}
        assert (b_of[10], b_of[11], b_of[13], b_of[12]) == (1, 1, 0, 0)
        assert ld(f'current/{10 % bsv.CURRENT_SHARDS}.json')['bundled'] == [10]
        assert ld(f'current/{13 % bsv.CURRENT_SHARDS}.json')['bundled'] == []
        assert bsv.bundle_shard_count(0) == 16 and bsv.bundle_shard_count(100 * bsv.BUNDLE_TARGET_BYTES) == 128

        # 2 回目は前回分を丸ごと差し替える (古いファイルが残らない)。まとめないときは同期用の一覧も消す
        json.dump({'main_by_char': {}}, open(os.path.join(d, 'data', 'character_index.json'), 'w'))
        bsv.build_views(d)
        assert not os.path.exists(os.path.join(v, 'char', '5.json'))
        assert not os.path.exists(v + '.tmp') and not os.path.exists(v + '.old')
        assert not os.path.exists(os.path.join(v, 'bundle')) and not os.path.exists(os.path.join(d, '_unbundled_players.txt'))
    print('ok')


if __name__ == '__main__':
    main()

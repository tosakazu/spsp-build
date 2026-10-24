"""制限判定の同時開催巻き込まれ解除 (build_tournament_metadata) のテスト.

実行: OPENBLAS_NUM_THREADS=1 .venv/bin/python3 tests/meta/test_restricted_cohost.py
(リポジトリルートから。unittest 等は使わず plain assert)

ケース: 第2.5回スマフェス(通常トナメ＆R1600制限トナメの同時開催) のように
大会名に制限キーワードを含み、制限イベント (ename マッチ) と無制限イベントを
同一大会ページで同時開催する場合、無制限イベント側は is_restricted=False になること。
制限が大会名のみの単独イベント大会 (九龍LIMIT BREAK / 鬼灯火 等) は従来どおり True。

名前パターンの判定は smash_database 側 (scripts/Japan/classify.py) が derived.json に書き出し、
spsp/meta.py はそれを読む。ここでは一時ディレクトリに classify.name_flags で作った derived.json を
置き、実際の読み取り経路 (spsp.derived.load) を通して検証する。
"""
import os
import sys
import json
import tempfile
import datetime as dt
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from spsp.meta import build_tournament_metadata  # noqa: E402
from _region_rules_module import region_classify  # noqa: E402

clf = region_classify('Japan')


class FakeT:
    def __init__(self, event_path, ts):
        self.event_path = event_path
        self.event_id = None   # data_loader.Tournament と同じ属性 (attr.json 無し = None)
        self.timestamp = ts
        self.end_timestamp = ts
        self.matches = []
        self.standings = []


NAMES = {
    # 同時開催: 大会名に "R1600制限"、制限は ename 側で表現
    '/x/2026/07/20/sumafes2.5/normal': (
        '第2.5回スマフェス(通常トナメ＆R1600制限トナメの同時開催)', '通常トーナメント(Non Limit)', 32),
    '/x/2026/07/20/sumafes2.5/limit': (
        '第2.5回スマフェス(通常トナメ＆R1600制限トナメの同時開催)', 'レート1600制限トーナメント(Limit1600)', 19),
    # 制限イベントの class virtual (ename に制限文字列を含む) も制限のまま
    '/x/2026/07/20/sumafes2.5/limit/class_phases/B_virtual': (
        '第2.5回スマフェス(通常トナメ＆R1600制限トナメの同時開催)', 'レート1600制限トーナメント(Limit1600) / Bクラス', 8),
    # 単独イベント大会: 制限は大会名のみ → 従来どおり制限
    '/x/2026/07/19/kowloon_lb3/main': ('九龍LIMIT BREAK#3', 'LIMIT BREAK', 91),
    '/x/2023/06/30/hoozuki25/main': ('鬼灯火#25', 'スマブラSP 1on1', 31),
    # 大会名制限 + 制限マッチしないサブイベント (団体戦) だけの大会 → 全部制限のまま
    '/x/2024/02/12/rising2/singles': ('西武撃Rising #2', 'シングルストーナメント/Singles', 288),
    '/x/2024/02/12/rising2/5on5': ('西武撃Rising #2', '【サブイベント】5on5団体戦', 0),
    # 完全無制限の同時開催大会 (対照)
    '/x/2026/07/18/kowloon18/main': ('九龍-KOWLOON-#18', 'KOWLOON_SP', 388),
}

ts = int(dt.datetime(2026, 7, 20).timestamp())
d0 = dt.datetime.fromtimestamp(ts).date()

# 一時ディレクトリの下に event dir を作り、derived.json (names は classify.name_flags の実物) を置く
root = Path(tempfile.mkdtemp(prefix='spsp_test_cohost_'))
paths = {}
for rel, (tname, ename, nent) in NAMES.items():
    ev = root / rel.lstrip('/')
    ev.mkdir(parents=True)
    (ev / 'derived.json').write_text(json.dumps({
        'classifier_version': clf.CLASSIFIER_VERSION,
        'tournament_name': tname, 'event_name': ename, 'num_entrants': nent,
        'results': {'n_standings': nent, 'min_placement': 1, 'has_de_phase': True, 'has_gf_recorded': None},
        'is_1on1': True, 'is_offline': True,
        'is_class_virtual': False, 'class_letter': None, 'parent_event_id': None,
        'names': clf.name_flags(tname, ename),
        'calendar': {'date': d0.isoformat(), 'end_date': d0.isoformat(),
                     'is_weekend_real': clf.is_weekend_range(d0, d0),
                     'is_force_weekend_period': clf.is_force_weekend_range(d0, d0)},
    }, ensure_ascii=False))
    (ev / 'attr.json').write_text(json.dumps({'url': ''}))
    paths[str(ev)] = NAMES[rel]
NAMES = paths

tours = [FakeT(p, ts) for p in NAMES]
meta = build_tournament_metadata(tours, tournament_display_name=lambda p: NAMES[p],
                                 min_entrants_for_ranking=8)
by_path = {t.event_path: meta[id(t)] for t in tours}

r = lambda p: by_path[str(root / p.lstrip('/'))]['is_restricted']

# スマフェス: 通常側だけ解除、制限側 (+virtual) は維持
assert r('/x/2026/07/20/sumafes2.5/normal') is False, '通常トナメが制限扱いのまま'
assert r('/x/2026/07/20/sumafes2.5/limit') is True
assert r('/x/2026/07/20/sumafes2.5/limit/class_phases/B_virtual') is True

# 単独イベント制限大会は従来どおり
assert r('/x/2026/07/19/kowloon_lb3/main') is True
assert r('/x/2023/06/30/hoozuki25/main') is True

# 大会名のみ制限 + ename 制限イベント無し → 全イベント制限のまま
assert r('/x/2024/02/12/rising2/singles') is True
assert r('/x/2024/02/12/rising2/5on5') is True

# 無関係の大会は無制限のまま
assert r('/x/2026/07/18/kowloon18/main') is False

print('test_restricted_cohost: all assertions passed')

#!/usr/bin/env python3
"""compare_live.py <配信中の置き場> <今回のビルド> <変わったファイルの一覧> — 変わったファイルを全部、値ごとに比べて違いを種類別に数える。

sync_r2.sh の差分モード (deploy=diff) が、R2 から落とした配信中のファイルと今回のビルドの出力を比べるのに使う
(読むのも出すのも公開している配信データだけ)。r2_json_diff.py が例を 12 件出すのに対し、こちらは全件を数える。

違いの種類:
  小数の最下位桁   どちらも数で、相対差 1e-9 以下 (計算の順序による丸めの違い)
  作成時刻など     generated_at / fetched_at / computation_time_s
  表示名           キーが display / name / tag などの文字列
  その他           上のどれでもないもの。キーの道筋 (選手 ID・要素番号はまとめる) ごとに件数と例を全部出す
順位の確認: キーに rank を含む値の違い (整数でも小数でも) は「その他」とは別に「順位」として数え、全部出す。
最後の行に「順位の違い N 件 / その他 M 件」を出す。
"""
from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

TIME_KEYS = {'generated_at', 'fetched_at', 'computation_time_s', 'built_at', 'updated_at'}
NAME_KEYS = {'display', 'name', 'tag', 'gamer_tag', 'prefix', 'display_name', 'winner_display', 'loser_display'}


def load(p: Path):
    txt = p.read_text(encoding='utf-8')
    try:
        return json.loads(txt)
    except json.JSONDecodeError:   # .jsonl
        return [json.loads(line) for line in txt.splitlines() if line.strip()]


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def tiny(a, b):
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def norm(path):
    """選手 ID などの数字のキーは {id}、要素番号は [] にまとめる (末尾の要素が値なら番号を残す = 列の位置に意味がある)。"""
    out = []
    for kind, v in path:
        if kind == 'k':
            out.append('.{id}' if re.fullmatch(r'-?\d+', str(v)) else f'.{v}')
        else:
            out.append(f'[{v}]' if kind == 'i_leaf' else '[]')
    return ''.join(out)


class Tally:
    def __init__(self):
        self.kinds = Counter()
        self.other = defaultdict(lambda: [0, []])
        self.rank = defaultdict(lambda: [0, []])

    def add(self, fcat, path, a, b, fname):
        last = next((str(v) for kind, v in reversed(path) if kind == 'k'), '')
        key = f'{fcat} {norm(path)}'
        ex = f'{fname}: {str(a)[:50]} -> {str(b)[:50]}'
        if 'rank' in last.lower() or 'rank' in norm(path).lower():
            if not (is_num(a) and is_num(b) and tiny(a, b)):
                r = self.rank[key]; r[0] += 1
                if len(r[1]) < 5:
                    r[1].append(ex)
                return
        if is_num(a) and is_num(b) and tiny(a, b):
            self.kinds['小数の最下位桁'] += 1
        elif last in TIME_KEYS:
            self.kinds['作成時刻など'] += 1
        elif last in NAME_KEYS and isinstance(a, str) and isinstance(b, str):
            self.kinds['表示名'] += 1
        else:
            o = self.other[key]; o[0] += 1
            if len(o[1]) < 3:
                o[1].append(ex)


def walk(a, b, path, t, fcat, fname, cols=None):
    """cols: 列形式 (players_current.json・data/views/current/*.json の columns) の列名。行の i 番目を列名で呼ぶ"""
    if isinstance(a, dict) and isinstance(b, dict):
        c = a.get('columns')
        if isinstance(c, list) and c and all(isinstance(x, str) for x in c):
            if a.get('columns') != b.get('columns'):
                t.add(fcat, path + [('k', 'columns')], '列が違う', '列が違う', fname)
            cols = c
        for k in set(a) | set(b):
            p = path + [('k', k)]
            if k not in a or k not in b:
                t.add(fcat, p, a.get(k, '(なし)'), b.get(k, '(なし)'), fname)
            else:
                walk(a[k], b[k], p, t, fcat, fname, cols)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            t.add(fcat, path + [('i', 'len')], f'要素数 {len(a)}', f'要素数 {len(b)}', fname)
        as_row = cols is not None and len(a) == len(cols) and not any(isinstance(x, (dict, list)) for x in a)
        for i, (x, y) in enumerate(zip(a, b)):
            if as_row:
                step = ('k', cols[i])
            else:
                step = ('i_leaf' if not isinstance(x, (dict, list)) else 'i', i)
            walk(x, y, path + [step], t, fcat, fname, cols)
    elif a != b or (type(a) is not type(b) and not (is_num(a) and is_num(b))):
        t.add(fcat, path, a, b, fname)


def main():
    old_dir, new_dir, listing = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    files = [l.strip() for l in listing.read_text().splitlines() if l.strip()]
    t = Tally()
    n_files = Counter()
    for f in files:
        fcat = f.split('/')[0] if '/' in f else f
        if fcat == 'data' and f.count('/') >= 2:
            fcat = '/'.join(f.split('/')[:3])   # data/views/current など
        n_files[fcat] += 1
        try:
            walk(load(old_dir / f), load(new_dir / f), [], t, fcat, f)
        except FileNotFoundError as e:
            t.add(fcat, [('k', '(ファイル)')], '?', f'読めない {e.filename}', f)
    print(f'  比べたファイル {len(files)} 件: ' + ', '.join(f'{k} {v}' for k, v in sorted(n_files.items())))
    for k in ('小数の最下位桁', '作成時刻など', '表示名'):
        print(f'  {k:<10} {t.kinds[k]:>10} 件')
    n_rank = sum(v[0] for v in t.rank.values())
    n_other = sum(v[0] for v in t.other.values())
    print(f'  順位         {n_rank:>10} 件')
    for key, (n, exs) in sorted(t.rank.items()):
        print(f'    {n:>8}  {key}')
        for e in exs:
            print(f'              例 {e}')
    print(f'  その他       {n_other:>10} 件')
    for key, (n, exs) in sorted(t.other.items(), key=lambda kv: -kv[1][0]):
        print(f'    {n:>8}  {key}')
        for e in exs:
            print(f'              例 {e}')
    print(f'  => 順位の違い {n_rank} 件 / その他 {n_other} 件')


if __name__ == '__main__':
    main()

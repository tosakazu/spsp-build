#!/usr/bin/env python3
"""tests/golden/compare_trees.py — 2 つの出力ツリーを「内容一致」で比べる (golden 比較・フル=差分比較の共通部)。

  python3 tests/golden/compare_trees.py <expect_dir> <got_dir> [--label 'OK のときの文言']

判定:
  - *.json / *.jsonl: バイト一致ならそのまま合格。違えば両方を orjson で読み、標準 json で書き直した文字列で比べる
    (正規化で消えるのは空白と浮動小数の表記だけ。キー順・int/float の区別・不正 JSON はそのまま検出される)。
    書き出しを orjson (区切りにスペース無し) にした 2026-09-07 以降、本番 2026-09-05 の golden とはバイトが違うので
    こうしている。ブラウザは JSON.parse した値しか見ないので、この方式で無視する差はサイトから観測できない。
  - 生成時刻を持つ 3 ファイルは、その項目を除いて比べる。
  - それ以外 (HTML / JS / CSV など): バイト一致。
  - 片方にしか無いファイルは不合格 (*.learn.done / *.build.done のスタンプは無視)。
終了コード 0 = 合格。
"""
import json
import os
import sys

import orjson

TS_ONLY = {'meta.json': {'generated_at', 'computation_time_s'},
           'data/tournaments.json': {'generated_at'}, 'data/series.json': {'generated_at'}}
IGNORE_SUFFIX = ('.learn.done', '.build.done')


def canon(b: bytes) -> str:
    return json.dumps(orjson.loads(b), ensure_ascii=False)


def json_equal(rel: str, a: bytes, b: bytes) -> bool:
    if a == b:
        return True
    if rel in TS_ONLY:
        x = orjson.loads(a); y = orjson.loads(b)
        for k in TS_ONLY[rel]:
            x.pop(k, None); y.pop(k, None)
        return canon(orjson.dumps(x)) == canon(orjson.dumps(y))
    if rel.endswith('.jsonl'):
        la = a.split(b'\n'); lb = b.split(b'\n')
        return len(la) == len(lb) and all(x == y or canon(x) == canon(y) for x, y in zip(la, lb) if x or y)
    return canon(a) == canon(b)


def walk(root: str) -> set[str]:
    out = set()
    for d, _, files in os.walk(root, followlinks=True):
        for f in files:
            if f.endswith(IGNORE_SUFFIX):
                continue
            out.add(os.path.relpath(os.path.join(d, f), root))
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    label = 'OK: 出力は期待と一致'
    if '--label' in sys.argv:
        label = sys.argv[sys.argv.index('--label') + 1]
        args = [a for a in args if a != label]
    exp, got = args[:2]
    fe, fg = walk(exp), walk(got)
    bad = [f'期待にだけある: {r}' for r in sorted(fe - fg)] + [f'出力にだけある: {r}' for r in sorted(fg - fe)]
    n_json = n_canon = n_other = 0
    for rel in sorted(fe & fg):
        a = open(os.path.join(exp, rel), 'rb').read(); b = open(os.path.join(got, rel), 'rb').read()
        if rel.endswith('.json') or rel.endswith('.jsonl'):
            n_json += 1
            if a != b:
                n_canon += 1
                try:
                    ok = json_equal(rel, a, b)
                except Exception as e:  # 不正 JSON など
                    ok = False; bad.append(f'{rel}: 読めない ({e})'); continue
                if not ok:
                    bad.append(f'{rel}: 内容が違う')
        else:
            n_other += 1
            if a != b:
                bad.append(f'{rel}: バイトが違う')
    if bad:
        print(f'NG: 期待と違うもの {len(bad)} 件')
        for s in bad[:30]:
            print('  ' + s)
        return 1
    print(f'{label} (JSON {n_json} 件: バイト一致 {n_json - n_canon}・内容一致 {n_canon} / その他 {n_other} 件バイト一致)')
    return 0


if __name__ == '__main__':
    sys.exit(main())

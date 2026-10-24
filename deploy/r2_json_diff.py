#!/usr/bin/env python3
"""r2_json_diff.py <配信中の JSON> <今回の JSON> — 2 つの JSON で値が違う場所 (キーの道筋) を最大 12 件出す。
sync_r2.sh の差分モード (SPSP_R2_DIFF_ONLY=1) が、変わるファイルの例について「何が変わるのか」を示すのに使う。"""
import json
import sys


def walk(a, b, path, out):
    if len(out) >= 12:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a:
                out.append(f"{path}.{k}: (なし) -> {str(b[k])[:60]}")
            elif k not in b:
                out.append(f"{path}.{k}: {str(a[k])[:60]} -> (なし)")
            else:
                walk(a[k], b[k], f"{path}.{k}", out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: 要素数 {len(a)} -> {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            walk(x, y, f"{path}[{i}]", out)
    elif a != b:
        out.append(f"{path}: {str(a)[:60]} -> {str(b)[:60]}")


def load(p):
    with open(p, encoding="utf-8") as f:
        txt = f.read()
    try:
        return json.loads(txt)
    except json.JSONDecodeError:   # .jsonl
        return [json.loads(line) for line in txt.splitlines() if line.strip()]


out = []
walk(load(sys.argv[1]), load(sys.argv[2]), "", out)
print("\n".join(out) if out else "(値は同じ。書式だけ違う)")

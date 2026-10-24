# -*- coding: utf-8 -*-
"""ランキング収録者の uid -> start.gg discriminator を生成 (優先枠作成ページ用).

出力: { "<user_id>": "<startgg_discriminator>", ... }  (判明した選手のみ。統合した旧アカウント (manual/user_merges.json) の旧 uid → 旧 discriminator も入る)
入力: <build-dir>/latest_tjpr_full.jsonl (収録者の母集合) +
      smash_db_tournament / smash_db_user 両側の users.jsonl
      (tournament 側が nightly 更新で新しいので優先、欠けは user 側で補完)。

使い方: python build/build_discriminators_json.py [--build-dir DIR] [--out PATH]
  --out 省略時は <build-dir>/data/discriminators.json (--build-dir 既定 site/ = 本番に載る) に書くので、
  プレビュー段階では --build-dir か --out で別の場所を指定すること。
"""
import argparse
import json
import os
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # リポジトリ直下 (spsp パッケージ)
from spsp.paths import SITE_DIR, USERS_JSONL as USERS_T, USERS_JSONL_USERDB as USERS_U  # noqa: E402
from spsp.data_loader import _resolve_merge_chains  # noqa: E402
from spsp.derived import load_user_merges  # noqa: E402


def load_discs(path):
    m = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            uid, disc = d.get('user_id'), d.get('startgg_discriminator')
            if uid is not None and disc:
                m[int(uid)] = disc
    return m


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--build-dir', default=str(SITE_DIR), help='latest_tjpr_full.jsonl を読み data/discriminators.json を書く場所 (既定 site/)')
    ap.add_argument('--out', default=None, help='出力先 (既定 <build-dir>/data/discriminators.json)')
    args = ap.parse_args(argv)
    if args.out is None:
        args.out = os.path.join(args.build_dir, 'data', 'discriminators.json')

    ranked = []
    with open(os.path.join(args.build_dir, 'latest_tjpr_full.jsonl'), encoding='utf-8') as f:
        for line in f:
            try:
                ranked.append(int(json.loads(line)['user_id']))
            except Exception:
                continue

    # 旧ユーザーデータベース (smash_db_user) は日本だけが持つ補完用。無い地域は大会側の users.jsonl だけで作る。
    if os.path.exists(USERS_U):
        discs = load_discs(USERS_U)
    else:
        print(f"[build_discriminators_json] 補完 DB {USERS_U} は無い (この地域は大会側の users.jsonl のみ)", flush=True)
        discs = {}
    discs.update(load_discs(USERS_T))  # tournament 側 (新しい) を優先

    out = {str(u): discs[u] for u in ranked if u in discs}
    # 統合した旧アカウント (manual/user_merges.json) の discriminator も載せる。旧アカウントの p/?d= を選手ページが旧 uid に引き、
    # data/user_merges.json で新しい方へ飛ばす。新しい方が収録者のときだけ (旧 uid の行は収録者ではないので、ほかのページの uid 引きには出てこない)
    n_old = 0
    for old, new in sorted(_resolve_merge_chains(load_user_merges()).items()):
        d = discs.get(old)
        if str(new) in out and d and str(old) not in out and d != out[str(new)]:
            out[str(old)] = d
            n_old += 1
    tmp = args.out + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, args.out)
    print(f"wrote {args.out}: {len(out) - n_old}/{len(ranked)} 選手 + 統合した旧アカウント {n_old}")


if __name__ == '__main__':
    main()

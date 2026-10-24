# -*- coding: utf-8 -*-
"""海外タグ (site/data/overseas.json) を生成する.

誰が海外勢かは smash_database が決める (2026-09-28〜): derive.py が data/startgg/<地域>/overseas_status.json に
  by_country = 国登録が地域外の人、manual = 手動リストのうち国登録が無い人
を書く (どちらも home_uids と「住んでそう」勢 = 直近 2 年でオフライン 1on1 大会 12 回以上・8 ヶ月以上 を除いた後)。
このスクリプトは、そのうちランキングに載っている人 (by_country ∩ 前回ランキング) と manual を海外タグにするだけ。
それまでは前回ビルドの選手ページから「住んでそう」を判定していた (前回の出力を読み直す必要があった)。

タグの効果:
  - ランキング各ページで灰色行 + 「海外」バッジ (地域カウントを消費しない相当順位表示)
  - 使い手ランキング / 全一表示から除外 (spsp-web の c/ ページ, build_player_subranks.py)
  - エンジン側 (spsp/common.py OVERSEAS_UIDS) の jp_counter_rank / jp_topn_filter でも同じ集合を使う

実行タイミング: update_and_deploy.sh の V4 ビルド ([3/5]) より前 (spsp/common.py が import 時に読むため)。
ランキング収録者の母集合は前回ビルドの site/latest_tjpr_full.jsonl (当回に初収録される選手のタグは次回から)。

使い方: python spsp/cli/build_overseas_json.py
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # リポジトリ直下 (spsp パッケージ)
from spsp.paths import SITE_DIR, SITE_DATA, REGION_DB  # noqa: E402

STATUS = str(REGION_DB / 'overseas_status.json')     # smash_database derive.py の生成物
RANKED = str(SITE_DIR / 'latest_tjpr_full.jsonl')   # 前回ビルド分 (ビルド前に走るので site/ を読む)
OUT = str(SITE_DATA / 'overseas.json')


def main():
    try:
        with open(STATUS, encoding='utf-8') as f:
            st = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: {STATUS} が無い — smash_database の derive.py を先に走らせる (前回生成分を継続使用)", file=sys.stderr)
        return 1

    ranked = set()
    with open(RANKED, encoding='utf-8') as f:
        for line in f:
            try:
                ranked.add(int(json.loads(line)['user_id']))
            except Exception:
                continue
    if not ranked:
        # 空の母集合で生成すると海外タグが manual 残党だけになり本番表示が壊れるので中止
        print(f"ERROR: {RANKED} が空/読めない — 生成中止 (前回生成分を継続使用)", file=sys.stderr)
        return 1

    by_country = ranked & {int(u) for u in st['by_country']}
    manual = {int(u) for u in st['manual']}
    uids = sorted(by_country | manual)
    rule = st.get('rule') or {}
    out = {
        'uids': uids,
        '_comment': (
            f"海外タグ UIDs ({len(uids)} 名, build_overseas_json.py 生成物・手編集しない)。"
            f"地域外 country のランキング収録者 {len(by_country)} 名 + 手動リストの country 未登録 {len(manual)} 名。"
            f"判定は smash_database の overseas_status.json (home_uids {len(st.get('home') or [])} 名と "
            f"住んでそう勢 {len(st.get('resident') or [])} 名 "
            f"[{rule.get('window_days')} 日で {rule.get('min_tournaments')} 大会以上・{rule.get('min_months')} ヶ月以上] を除いた後)。"
        ),
    }
    # 本番 nightly が旧ファイルへハードリンクしていることがあるため、in-place write は
    # せず tmp + os.replace で新 inode に差し替える (= 配信中のファイルを書き換えない)。
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, OUT)
    print(f"wrote {OUT}: {len(uids)} uids (地域外 country の収録者: {len(by_country)}, 手動: {len(manual)}; "
          f"判定元 {STATUS})")
    return 0


if __name__ == '__main__':
    sys.exit(main())

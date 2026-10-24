"""キャラ投票をメインキャラ判定に反映する.

投票は「使用データで決めきれない部分」だけを埋める. 大会でのキャラ使用が積み上がって
判定が明確になったら、常に使用データが勝つ (docs/post_feature_design.md §12).

採用規則 (uid ごと):

    使用データの状態                                  採用
    ------------------------------------------------- ------------------------
    characters が空 (= 使用実績なし)                   投票を採用
    ダブルメイン圏 (トップとの pct 差が GAP 以内の      投票を先頭に持ってくる
      キャラが 2 体以上) かつ 投票がその候補内          (= メインの選定だけ決める)
    圏を超えて明確なメインが出た / 投票が候補外        使用データを採用 (投票は無視)

反映先はすべて char_usage の **並び** に集約されている:
  - プレイヤーページの使用キャラ行      characters[0] を主として赤字
  - ランキング行のメインキャラ ID      overlay._build_main_char が [0] (絵文字はフロントが ID から引く)
  - 使い手ランキング                    v4/char_index.build_character_index が [0]
したがって char_usage を作った直後にここを通せば、3 つとも自動で追随する.

投票由来のエントリは **pct を持たない** (キーごと無い). これは GAS
(spsp-web の worker/src/api/vote.ts voteCandidates。以前は gas/vote.gs) が「pct のあるエントリ = 使用実績」で再投票の可否を
判定しているため. pct: null にすると Number(null) === 0 が有限値として通ってしまい、
使用率 0% の実績があるものとして扱われる.
"""
from __future__ import annotations

import json
from pathlib import Path

# ダブルメイン圏の閾値. spsp-web の worker/src/config.ts と site/js/post_config.js の
# DOUBLE_MAIN_PCT_GAP と同じ値にすること
# (tests/post/page.test.cjs が 3 つの一致を検査する).
DOUBLE_MAIN_PCT_GAP = 0.20

# 採用しうる status. 'debug' (= vote.html?debug=1) は絶対に採用しない.
# 'rejected' は「人間が却下した」印なので、最新行がこれなら投票なし扱いにする
# (= 1 つ前の投票に戻したりはしない).
ADOPTABLE_STATUS = frozenset({'pending', 'approved'})

from spsp.paths import CHARACTERS_JSON as _CHARACTERS_JSON


def load_votes(path) -> tuple[list[dict], str]:
    """build/data/char_votes.json を読み (votes, fetched_at) を返す.

    ファイルが無ければ ([], '') (= まだ一度も取得していない). 壊れていたら例外にする
    (= 投票が黙って消えるより落ちたほうがよい).
    fetched_at は取得が失敗して古いファイルのまま走ったときに気づくためのもの
    (呼び出し側がログに出す).
    """
    p = Path(path)
    if not p.exists():
        return [], ''
    data = json.loads(p.read_text(encoding='utf-8'))
    votes = data.get('votes')
    if not isinstance(votes, list):
        raise ValueError(f'{p}: votes が配列ではありません')
    return votes, str(data.get('fetched_at') or '')


def load_char_names(path=_CHARACTERS_JSON) -> dict[int, str]:
    """spsp/characters.json から {char_id: 表示名}. 投票の char_id 検証にも使う."""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    return {int(k): v for k, v in (data.get('characters') or {}).items() if v}


def latest_by_uid(votes: list[dict]) -> dict[int, dict]:
    """uid ごとの最新行. 最新行が採用不可 status なら、その uid は投票なし扱い.

    再投票は上書き (設計書 §12) なので最新行だけを見る.
    """
    latest: dict[int, dict] = {}
    for v in votes:
        try:
            uid = int(v['user_id'])
        except (KeyError, TypeError, ValueError):
            continue
        cur = latest.get(uid)
        if cur is None or str(v.get('ts') or '') >= str(cur.get('ts') or ''):
            latest[uid] = v
    return {uid: v for uid, v in latest.items()
            if str(v.get('status') or '') in ADOPTABLE_STATUS}


def double_main_candidates(chars: list[dict], gap: float = DOUBLE_MAIN_PCT_GAP) -> list[dict]:
    """ダブルメイン圏の候補 (2 体未満なら []). spsp-web の worker/src/api/vote.ts voteCandidates と同じ規則.

    基準は **最大 pct** であって先頭ではない (ここが並びを変えるため、
    2 回目以降のビルドでは characters[0] が最大 pct とは限らない).
    """
    measured = [c for c in chars
                if isinstance(c.get('pct'), (int, float)) and c.get('id') is not None]
    if not measured:
        return []
    top = max(c['pct'] for c in measured)
    cands = [c for c in measured if top - c['pct'] <= gap]
    return cands if len(cands) >= 2 else []


def _measured_first(chars: list[dict]) -> list[dict]:
    """使用率が最大のキャラを先頭にする (それ以外の並びは維持).

    compute_char_usage の出力は元から pct 降順なので通常は何も変わらない.
    投票を却下する側の分岐で呼ぶことで、「使用データを採用」が入力の並びに
    依存せず必ず成り立つようにしている (= 前回ビルドの並びを食わせても固定化しない).
    """
    measured = [c for c in chars if isinstance(c.get('pct'), (int, float))]
    if not measured:
        return list(chars)
    top = max(measured, key=lambda c: c['pct'])
    if chars[0] is top:
        return list(chars)
    return [top] + [c for c in chars if c is not top]


def apply_char_votes(char_usage: dict, votes: list[dict],
                     char_names: dict[int, str] | None = None,
                     gap: float = DOUBLE_MAIN_PCT_GAP) -> tuple[dict, dict]:
    """char_usage に投票を反映した新しい dict と内訳を返す (入力は変更しない).

    Args:
        char_usage: {uid: [{name,id,pct,...}, ...]} (compute_char_usage の出力)
        votes:      build/data/char_votes.json の votes 配列
        char_names: {char_id: 表示名}. None なら spsp/characters.json から読む.
        gap:        ダブルメイン圏の閾値

    Returns:
        (new_char_usage, stats)
        stats = {'adopted_empty', 'reordered', 'ignored_clear_main',
                 'ignored_not_candidate', 'ignored_same_order', 'unknown_char'}
    """
    if char_names is None:
        char_names = load_char_names()
    out = dict(char_usage)
    stats = {'adopted_empty': 0, 'reordered': 0, 'ignored_clear_main': 0,
             'ignored_not_candidate': 0, 'ignored_same_order': 0, 'unknown_char': 0}

    for uid, vote in latest_by_uid(votes).items():
        try:
            cid = int(vote['char_id'])
        except (KeyError, TypeError, ValueError):
            stats['unknown_char'] += 1
            continue
        name = char_names.get(cid)
        if not name:
            # spsp/characters.json に無い id (= 集計対象外か、キャラ一覧の更新漏れ).
            # GAS 側でも弾いているので通常は起きない。黙って採用はしない。
            print(f'  [char_vote] 未知の char_id={cid} (uid={uid}) — 無視', flush=True)
            stats['unknown_char'] += 1
            continue

        chars = list(out.get(uid) or [])
        if not chars:
            # 使用実績なし → 投票をそのまま採用. pct は付けない (docstring 参照).
            out[uid] = [{'name': name, 'id': cid, 'src': 'vote'}]
            stats['adopted_empty'] += 1
            continue

        cands = double_main_candidates(chars, gap)
        if not cands:
            # 圏を超えて明確なメインがいる → 使用データが勝つ
            out[uid] = _measured_first(chars)
            stats['ignored_clear_main'] += 1
            continue
        if not any(c['id'] == cid for c in cands):
            out[uid] = _measured_first(chars)
            stats['ignored_not_candidate'] += 1
            continue
        if chars[0].get('id') == cid:
            stats['ignored_same_order'] += 1
            continue
        # 候補内 → 投票されたキャラを先頭に. 残りの並びは使用順のまま.
        voted = next(c for c in chars if c.get('id') == cid)
        rest = [c for c in chars if c is not voted]
        moved = dict(voted)
        moved['src'] = 'vote'      # 「並びが投票由来」の印 (pct はそのまま残す)
        out[uid] = [moved] + rest
        stats['reordered'] += 1

    return out, stats

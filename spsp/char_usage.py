"""使用キャラ集計.

各プレイヤーの直近 1 年間 (= そのプレイヤーが最後に「キャラ報告のある」eligible
event に出場した日を起点に 365 日遡る窓) でのキャラ使用率を計算する.

「最新の eligible 出場」ではなく「最新の キャラ情報あり 出場」を起点にする理由:
  matches.json の details (= キャラ報告) は event ごとに有/無があり、最新出場が
  無報告 event だと窓の起点が報告データから離れ、サンプル不足になる. 報告ありの
  最新出場を起点にすれば、報告データ密度に窓を寄せられる.

集計対象の二段階:
  1. 「BT 集計対象」 (= ランキング計算で実際に使われた matches を含む event,
     `bt_per_tour[(uid, eid)].bt_used == True`). 第一優先. ガチ大会の
     キャラ報告のみを使うことで、招待制 / 平日 / 小規模イベントでの偶発的な
     別人マッピングや遊びキャラ報告を除外する.
  2. 「全体」 (= is_non_serious=False AND is_lower_class=False の全 event).
     第一段で 0 件だった uid のみ、フォールバックでここから集計する.

eligible event の前提条件 (両段で共通):
  - is_non_serious == False  (= 特殊ルール / 身内除外)
  - is_lower_class == False  (= Bクラス等の下位 bracket 除外)

データは matches.json の details[].selections[] から取得:
  selection = {user_id, character_id, character_name}
  matches でキャラ情報が欠落している event は単に scan skip する.

出力: {uid: [{name, id, pct, n}, ...]} pct 降順, 使用 top-3 (しきい値なし).
"""
from __future__ import annotations
import json
from collections import defaultdict
from pathlib import Path

from spsp.char_names_ja import to_ja


# 主キャラと判定するための最小条件.
# カウント単位は「セット」(= 1 対戦): 1 セット内で同キャラ何 game 出しても +1、
# 複数キャラならそれぞれ +1 (_iter_event_details 参照).
MIN_GAMES_TOTAL = 1          # 窓内に 1 セットでも報告あれば判定対象 (= 閾値実質なし)
MIN_PCT = 0.0                # 使用率しきい値なし (= 使用 top-3 をそのまま表示)
TOP_K = 3                    # 上位 N キャラまで表示
WINDOW_DAYS = 365


def _iter_event_details(event_path: str):
    """matches.json から (winner_id, loser_id, [selections]) を yield.

    details が空 (= キャラ情報なし) なら skip.
    """
    p = Path(event_path) / 'matches.json'
    if not p.exists():
        return
    try:
        with open(p) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        from spsp.skiplog import skipped
        skipped('キャラ使用データが読めない', f'{p}: {e}')
        return
    matches = data if isinstance(data, list) else data.get('data', [])
    if not isinstance(matches, list):
        return
    for m in matches:
        if not isinstance(m, dict):
            continue
        details = m.get('details')
        if not details:
            continue
        # 1 セット (= BO3/BO5 等の 1 対戦) 内では同一キャラを何 game 出しても +1.
        # 複数キャラを出した場合はそれぞれのキャラに +1 (= per-set distinct カウント).
        # game 単位で数えると BO5 フルセットの試合がキャラ 1 つに最大 +5 入り、
        # セット数の多寡や試合の長さで使用率が歪むため.
        # 同セット内の game 数もタイブレーク用に保持: cid -> [cname, games]
        per_uid_chars: dict[int, dict[int, list]] = {}
        for game in details:
            sels = (game or {}).get('selections') or []
            for sel in sels:
                if not isinstance(sel, dict):
                    continue
                uid = sel.get('user_id')
                cid = sel.get('character_id')
                cname = sel.get('character_name')
                if uid is None or cid is None or not cname:
                    continue
                ent = per_uid_chars.setdefault(int(uid), {}).setdefault(int(cid), [str(cname), 0])
                ent[1] += 1
        for uid, chars in per_uid_chars.items():
            for cid, (cname, games) in chars.items():
                yield uid, cid, cname, games


def _aggregate_window(recs: list) -> list:
    """recs = [(ts, cid, cname, games), ...] を最新 ts 起点 1 年窓で集計し
    [{name, id, pct, n, games, total}, ...] (top TOP_K) を返す.
    並びは n (= セット数) 降順、同数なら games (= 実 game 数) 降順でタイブレーク."""
    if not recs:
        return []
    max_ts = max(r[0] for r in recs)
    floor_ts = max_ts - WINDOW_DAYS * 86400
    in_window = [(cid, cname, games) for (ts, cid, cname, games) in recs if ts > floor_ts]
    total = len(in_window)
    if total < MIN_GAMES_TOTAL:
        return []
    counts: dict[int, list] = {}  # cid -> [name, n_sets, n_games]
    for cid, cname, games in in_window:
        if cid in counts:
            counts[cid][1] += 1
            counts[cid][2] += games
        else:
            counts[cid] = [cname, 1, games]
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1][1], -kv[1][2]))
    chars = []
    for cid, (cname, n, games) in ranked:
        pct = n / total
        if pct < MIN_PCT:
            break
        chars.append({
            'name': to_ja(cname),
            'id': cid,
            'pct': round(pct, 4),
            'n': n,
            'games': games,
            'total': total,
        })
        if len(chars) >= TOP_K:
            break
    return chars


def compute_char_usage(all_tournaments, t_meta_by_id, bt_per_tour=None):
    """eligible event を walk し、per uid のキャラ分布を計算.

    Args:
        all_tournaments: Tournament objects (= ordered list, eligibility は meta で判定)
        t_meta_by_id: {id(t): meta dict}
        bt_per_tour: 学習結果 {(uid, eid): {'bt_used': bool, ...}}.
                     None なら primary フィルタ無効 (= 全 eligible event を一段で使う).

    Returns:
        {uid: [{'name', 'id', 'pct', 'n', 'total'}, ...]}
    """
    eligible_events = []
    for t in all_tournaments:
        meta = t_meta_by_id.get(id(t))
        if not meta:
            continue
        if meta.get('is_non_serious') or meta.get('is_lower_class') or meta.get('is_small'):
            continue
        eligible_events.append((int(t.timestamp), t.event_path, meta.get('event_id')))
    eligible_events.sort()

    # bt_used 集合 (= 第一優先 = 集計対象): {eid: set of bt_used uids}.
    primary_uids_by_eid: dict[int, set] = defaultdict(set)
    if bt_per_tour:
        for key, info in bt_per_tour.items():
            uid, eid = key
            # info = 学習側の dict、または配列の表 (spsp/columnar.PerTourLog) の tuple
            if (info[0] if type(info) is tuple else info.get('bt_used')):
                primary_uids_by_eid[eid].add(int(uid))

    # Phase 1: 全 eligible event をスキャンし、primary / all 両方を蓄積.
    by_uid_all: dict[int, list[tuple[int, int, str, int]]] = defaultdict(list)
    by_uid_primary: dict[int, list[tuple[int, int, str, int]]] = defaultdict(list)
    for ts, ep, eid in eligible_events:
        primary_uids = primary_uids_by_eid.get(eid, set())
        for uid, cid, cname, games in _iter_event_details(ep):
            by_uid_all[uid].append((ts, cid, cname, games))
            if uid in primary_uids:
                by_uid_primary[uid].append((ts, cid, cname, games))

    # Phase 2: per uid で集計. primary 優先、0 件のときだけ all にフォールバック.
    out: dict[int, list[dict]] = {}
    for uid in by_uid_all:
        recs = by_uid_primary.get(uid) or by_uid_all.get(uid) or []
        chars = _aggregate_window(recs)
        if chars:
            out[uid] = chars
    return out

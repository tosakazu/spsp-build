"""大会シール (プレイヤーカードの裏に貼る大会アイコンのシール) の配布判定 (2026-10-20〜)。

1 つの大会で、条件を満たした色は下位のものも全部もらえる (優勝なら 虹・金・銀・通常 の 4 枚)。
出力は一番上の色だけ (rainbow / gold / silver / normal)。下の色はフロントが足す。

配る大会: 参加 8 人以上で Lv1 の集計対象 (特殊ルール・身内 = is_non_serious、未完了 = is_gf_missing、少人数 = is_small は外す。
オンラインはそもそも読み込まれない)。平日大会も入る。大会アイコンが無い大会はフロントが出さない。
配る人: その大会で 1 試合でもした人 (DQ だけの人には配らない)。

色の条件:
  予選が複数の山 (フェーズ 1 の phase group が 2 つ以上で、優勝者が後のフェーズにも出ている):
    虹 = 優勝
    金 = Top 8。予選突破が 8 人未満なら Top 4、4 人未満なら 2 位まで
    銀 = 予選 (フェーズ 1) 突破。境目 = 優勝者と同じ後のフェーズで試合をした人の一番悪い順位
         (= 敗者側からフェーズ 1 を抜けてすぐ負けた人)。予選が 2 段でもフェーズ 1 を抜ければ銀
  1 つの山 (上以外。予選と本線の順位が食い違う大会、9 割以上が本戦に進む大会もこちら):
    虹 = 優勝
    金 = 上位 1/4 以内 (最低 2 位まで・最大 Top 8)
    銀 = 48 人以下は上位 1/2 以内 (最大 Top 16)、49 人以上は Top 24
  通常 = 参加
"""
from __future__ import annotations

import json
from pathlib import Path

from spsp.skiplog import skipped

RAINBOW, GOLD, SILVER, NORMAL = 'rainbow', 'gold', 'silver', 'normal'
MIN_ENTRANTS = 8   # これ未満の大会には配らない (is_small の境目とは別に決める)
ALL_ADVANCE = 0.9  # 予選を抜けた人がこの割合以上なら「全員が本戦に進む大会」= 1 つの山として扱う (2026-11-14)


def single_lines(nent: int) -> tuple[float, float]:
    """1 つの山の大会の (金の境目, 銀の境目)。順位がこれ以下ならその色。"""
    gold = max(2, min(8, nent / 4))
    silver = min(16, nent / 2) if nent <= 48 else 24
    return gold, silver


def pool_lines(n_qualified: int, border: int) -> tuple[int, int]:
    """予選が複数の山の大会の (金の境目, 銀の境目)。n_qualified = 予選突破の人数。"""
    gold = 8 if n_qualified >= 8 else 4 if n_qualified >= 4 else 2
    return min(gold, border), border


def phase_orders(rows: list[dict]) -> dict:
    """{phase_id: フェーズの順番}。人数 (phase_num_seeds) の多いフェーズが先。

    start.gg の順番 (試合の phase_order・phases.json の order) は使わない。主催者が画面で並べた順で、
    予選が本戦の後ろに置かれている大会がある (DELTA#7・#8、スマ納め、ITSUKUSHIMA、錦祭 など。
    予選が最後だと「全員が予選突破」や「予選なし」になる)。しかも試合の phase_order は 2026 年の途中から
    取得していて、それより前に取った大会 (全体の約 8 割) には無い (2026-11-14: これを見落としていて、
    古い大会の予選が全部「1 つの山」扱いになり、予選を抜けても銀が出なかった)。
    全員が出るフェーズ = 予選なので人数で並べる。同数 (全員が本戦に進む大会) は山の多い方が先、それも同じなら ID 順。
    """
    seeds: dict = {}
    groups: dict = {}
    users: dict = {}
    for m in rows:
        pid = m.get('phase_id')
        if pid is None:
            continue
        if m.get('phase_num_seeds'):
            seeds[pid] = m['phase_num_seeds']
        groups.setdefault(pid, set()).add(m.get('phase_group_id'))
        users.setdefault(pid, set()).update((m['winner_id'], m['loser_id']))
    size = {pid: seeds.get(pid) or len(users[pid]) for pid in groups}
    ranked = sorted(groups, key=lambda pid: (-size[pid], -len(groups[pid]), pid))
    return {pid: i + 1 for i, pid in enumerate(ranked)}


def pool_border(event_path: str, place: dict[int, int]) -> int | None:
    """予選が複数の山なら予選突破の境目の順位、そうでなければ None。

    フェーズの順番・ID は Match に持たないので matches.json を読み直す (呼ぶのは phase group が 2 つ以上の大会だけ)。
    順番は phase_orders (人数の多いフェーズが先)。
    """
    try:
        with open(Path(event_path) / 'matches.json', 'rb') as f:
            d = json.loads(f.read())
    except Exception as e:
        skipped('シール: matches.json が読めない', f'{event_path}: {e}')
        return None
    rows = [m for m in (d if isinstance(d, list) else d.get('data') or [])
            if m.get('winner_id') is not None and m.get('loser_id') is not None]
    order_of = phase_orders(rows)
    rows = [{**m, 'phase_order': order_of[m.get('phase_id')]} for m in rows if order_of.get(m.get('phase_id')) is not None]
    orders = sorted({m['phase_order'] for m in rows})
    if len(orders) < 2:
        return None
    first = [m for m in rows if m['phase_order'] == orders[0]]
    if len({m.get('phase_group_id') for m in first}) < 2:
        return None   # フェーズ 1 が 1 つの山 (→ TOP8 などの別フェーズがあっても 1 つの山の大会)
    champ = next((u for u, p in place.items() if p == 1), None)
    later = {m.get('phase_id') for m in rows
             if m['phase_order'] > orders[0] and champ in (int(m['winner_id']), int(m['loser_id']))}
    if not later:
        return None   # 優勝者がフェーズ 1 だけ (予選リーグだけの大会など)
    qualified = set()
    for m in rows:
        if m.get('phase_id') in later:
            qualified.update((int(m['winner_id']), int(m['loser_id'])))
    border = max((place[u] for u in qualified if u in place), default=None)
    if border is None:
        return None
    out = {int(u) for m in first for u in (m['winner_id'], m['loser_id'])} - qualified
    if any(place.get(u, border + 1) <= border for u in out):
        return None   # 予選で負けた人が境目より上 = 予選・本線の作りではない → 1 つの山として扱う
    if sum(1 for p in place.values() if p <= border) >= ALL_ADVANCE * len(place):
        return None   # ほぼ全員が本戦に進む (総当たり予選で本戦のシードを決めるだけ、など) → 予選突破とは数えない
    return border


def tier_of(place: int, gold: float, silver: float) -> str:
    if place == 1:
        return RAINBOW
    if place <= gold:
        return GOLD
    if place <= silver:
        return SILVER
    return NORMAL


def event_stickers(t, meta: dict, class_matches=None) -> dict[int, str]:
    """1 大会分の {uid: 一番上の色}。配らない大会は {}。

    class_matches = 下位クラスの仮想大会のとき、親の大会のクラスの試合 (仮想大会の t.matches は空)。
    """
    if meta.get('is_non_serious') or meta.get('is_gf_missing') or meta.get('is_small'):
        return {}
    if (meta.get('nent') or 0) < MIN_ENTRANTS:
        return {}
    place: dict[int, int] = {}
    for p, u in t.standings:
        if p is not None and p >= 1 and u not in place:
            place[u] = p
    played = set()
    for m in (t.matches if class_matches is None else class_matches):   # DQ の試合は data_loader が外している
        played.add(m.winner_id)
        played.add(m.loser_id)
    nent = meta['nent']
    border = None
    if len({m.phase_group_id for m in t.matches}) >= 2 and not meta.get('is_class_virtual'):
        border = pool_border(t.event_path, place)
    if border is not None:
        n_q = sum(1 for p in place.values() if p <= border)
        gold, silver = pool_lines(n_q, border)
    else:
        gold, silver = single_lines(nent)
    return {u: tier_of(p, gold, silver) for u, p in place.items() if u in played}


def compute_stickers(all_tournaments, t_meta_by_id: dict) -> dict:
    """{id(t): {uid: 一番上の色}} (配る大会だけ)。"""
    by_eid = {t.event_id: t for t in all_tournaments if t.event_id is not None}
    out = {}
    for t in all_tournaments:
        meta = t_meta_by_id.get(id(t))
        if meta is None:
            continue
        cm = None
        if meta.get('is_class_virtual'):
            parent = by_eid.get(meta.get('parent_event_id'))
            cm = [m for m in parent.matches if m.is_class] if parent is not None else []
        s = event_stickers(t, meta, cm)
        if s:
            out[id(t)] = s
    return out

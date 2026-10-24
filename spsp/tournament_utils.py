"""大会データまわりの小さな共通関数 (本番ビルドが使うもの)。

以前は eval/ (精度実験用ディレクトリ) の eval_tjpr_size_correction.py, build_latest_ranking.py,
jjpr_v3.py, smash_banzuke.py から import していた。実験用コードに本番が依存していると
実験側を触るたびに本番が壊れうるので、本番が使う関数だけをここに移した (2026-09)。
関数本体は移動元と同一。eval/ 側の複製は ⑧ (重複統合) で片付ける。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from spsp.paths import REPO, EVENTS_ROOT, USERS_JSONL  # noqa: F401  (再 export: tjpr_cascade 等が使う)
from spsp.skiplog import skipped


# ── 順位 → tier / JJPR 得点 (旧 eval/jjpr_v3.py) ──
def placement_to_tier(place: int) -> int:
    """Convert DE bracket placement to tier.

    1→0, 2→1, 3→2, 4→3, 5-6→4, 7-8→5, 9-12→6, 13-16→7,
    17-24→8, 25-32→9, 33-48→10, 49-64→11, 65-96→12, ...
    """
    if place <= 0:
        return 0
    if place <= 4:
        return place - 1
    # For place >= 5: find the bracket group
    # Groups: [5,6], [7,8], [9,12], [13,16], [17,24], [25,32], [33,48], ...
    # Tier 4: 5-6   (size 2)
    # Tier 5: 7-8   (size 2)
    # Tier 6: 9-12  (size 4)
    # Tier 7: 13-16 (size 4)
    # Tier 8: 17-24 (size 8)
    # Tier 9: 25-32 (size 8)
    # Pattern: pairs of tiers share the same power-of-2 size
    # Tier 4+2k, 4+2k+1 have size 2^(k+1) each
    # Cumulative: after tier 3, position 5
    # After tier 4: 7, tier 5: 9, tier 6: 13, tier 7: 17, tier 8: 25, tier 9: 33...
    pos = 5
    tier = 4
    size = 2
    while True:
        if place < pos + size:
            return tier
        pos += size
        tier += 1
        if tier % 2 == 0:
            size *= 2


def jjpr_point(num_entrants: int, place: int, days_elapsed: float) -> float:
    if num_entrants < 2 or place < 1:
        return 0.0
    tier = placement_to_tier(place)
    base = num_entrants * (math.log2(num_entrants) ** 2) * 10
    placement_factor = (1.0 / math.sqrt(2)) ** tier
    time_decay = math.exp(1.0 - days_elapsed / 365.0)
    return base * placement_factor * time_decay


# ── users.jsonl (旧 eval/jjpr_v3.py) ──
def load_users() -> dict[int, dict]:
    idx: dict[int, dict] = {}
    if not USERS_JSONL.exists():
        return idx
    with USERS_JSONL.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception as e:
                skipped('users.jsonl の行が読めない', f'{e}')
                continue
            uid = d.get("user_id")
            if uid is not None:
                idx[int(uid)] = d
    return idx


def format_display(user_record: dict | None, uid: int) -> str:
    if not user_record:
        return f"(unknown user_id={uid})"
    prefix = user_record.get("prefix") or ""
    tag = user_record.get("gamer_tag") or "?"
    if prefix:
        return f"{prefix} | {tag}"
    return tag


# ── ランキング対象しきい値 (旧 build/build_tournaments_index.py と build_series_json.py の複製) ──
def load_min_entrants_threshold(meta_path) -> int:
    """ランキング対象しきい値. meta.json 優先, fallback 8."""
    try:
        with open(meta_path, "rb") as f:
            m = json.loads(f.read())
        v = m.get("min_entrants_for_ranking")
        if isinstance(v, int) and v > 0:
            return v
        print(f"  WARN: {meta_path} に min_entrants_for_ranking が無い/不正 ({v!r}) — 既定 8 を使う", flush=True)
    except Exception as e:
        print(f"  WARN: {meta_path} が読めない ({e}) — 既定 8 を使う", flush=True)
    return 8


# ── DQ / 不参加の除外 (旧 eval/baseline_repro/eval_tjpr_size_correction.py) ──
def filter_dq_no_shows(all_t):
    for t in all_t:
        if not t.matches:
            continue
        played = set()
        for m in t.matches:
            played.add(m.winner_id); played.add(m.loser_id)
        if not played:
            continue
        filtered = [(p, u) for (p, u) in t.standings if u in played]
        if filtered and len(filtered) < len(t.standings):
            t.standings = filtered


# ── attr.json から表示名 (旧 eval/build_latest_ranking.py) ──
def tournament_display_name(event_path: str) -> tuple[str, str, int]:
    """Return (tournament_name, event_name, num_entrants)."""
    p = Path(event_path)
    try:
        with open(p / "attr.json", "rb") as f:
            d = json.loads(f.read())
        return (
            d.get("tournament_name", p.parent.name),
            d.get("event_name", p.name),
            int(d.get("num_entrants") or 0),
        )
    except Exception as e:
        skipped('attr.json が読めない (ディレクトリ名で代用)', f'{p}: {e}')
        return (p.parent.name, p.name, 0)


# ── 仮想 DE 敗者側の各ラウンド参加者 (旧 eval/smash_banzuke.py) ──
def build_de_losers_round_participants(n: int) -> list[list[int]]:
    """Compute which seed indices participate in each LR round.

    Virtual DE losers bracket:
    - Winners bracket: log2(bracket_size) rounds
    - WR r produces bracket_size/2^(r+1) dropdowns (weakest half loses)
    - Losers bracket alternates:
        - Internal round: LR survivors face each other (count halves)
        - Dropdown round: LR survivors + WR dropdowns (same count, no halving)

    Participants are seed indices 0..n-1 (weakest=0, strongest=n-1).
    We don't simulate who wins; we just track who is present at each round.

    Returns: list of participant seed lists per LR round.
    """
    bracket_size = 1
    while bracket_size < n:
        bracket_size *= 2
    winners_rounds = int(math.log2(bracket_size))

    # Simulate winners bracket to get dropdowns per round
    # Simple model: pair adjacent seeds, weaker (lower) loses
    winners_active = list(range(min(n, bracket_size)))
    dropdowns_by_wr: dict[int, list[int]] = {}

    for wr in range(winners_rounds):
        winners_active.sort()  # weakest first
        # Standard DE chalk: pair (weakest, strongest), (2nd-weakest, 2nd-strongest), ...
        # → weakest half loses, strongest half survives.
        half = len(winners_active) // 2
        losers = winners_active[:half]
        survivors = winners_active[half:]
        dropdowns_by_wr[wr] = losers
        winners_active = survivors

    # Build LR rounds + 1 GF round.
    # Target total rounds = placement_to_tier(bracket_size) = 2*winners_rounds - 1,
    # which matches JJPR v4's W2W (wins-to-win-from-bottom) for a full DE bracket.
    # The last round represents Grand Final (counted once, not twice).
    _ptt = placement_to_tier  # 同じモジュール内の定義 (以前は eval/jjpr_v3 から import していた)
    target_rounds = max(0, _ptt(bracket_size))
    lr_target = max(0, target_rounds - 1)  # GF appended at the end

    lr_rounds: list[list[int]] = []
    lr_active = sorted(dropdowns_by_wr.get(0, []))
    wr_idx = 1

    while len(lr_rounds) < lr_target:
        # Internal round
        if len(lr_active) >= 2:
            lr_rounds.append(list(lr_active))
            lr_active.sort()
            half = len(lr_active) // 2
            lr_active = lr_active[half:]
        else:
            lr_rounds.append(list(lr_active))

        if len(lr_rounds) >= lr_target:
            break

        # Dropdown round: add WR dropdowns from next WR round.
        if wr_idx < winners_rounds and wr_idx in dropdowns_by_wr:
            dropdowns = dropdowns_by_wr[wr_idx]
            combined = sorted(lr_active + dropdowns)
            lr_rounds.append(list(combined))
            half = len(combined) // 2
            lr_active = combined[half:]
            wr_idx += 1
        else:
            lr_rounds.append(list(lr_active))
            if len(lr_active) >= 2:
                half = len(lr_active) // 2
                lr_active = lr_active[half:]

    # Grand Final round: top-2 strongest seeds (WR winner + LR winner).
    if target_rounds > 0 and bracket_size >= 2:
        top2 = sorted(range(max(0, min(n, bracket_size) - 2), min(n, bracket_size)))
        lr_rounds.append(top2)

    return lr_rounds


# ── 再開待ち (awaiting resume) 登録簿 ──
# build/data/awaiting_resume.json: 途中で延期・中断した大会の event_id 一覧。
# 登録された event は優勝者未確定でも is_gf_missing にせず、その時点までの結果で集計する
# (v4.meta.resolve_completion_flags)。ファイルが無ければ空集合、壊れていれば例外 (黙って無視しない)。
def awaiting_resume_path():
    from spsp.paths import MANUAL_DIR
    return MANUAL_DIR / "awaiting_resume.json"   # 手動管理の登録簿 (smash_database data-Japan、2026-09-08〜)


def load_awaiting_resume_ids(path=None) -> set[int]:
    import json as _json
    path = Path(path) if path is not None else awaiting_resume_path()
    if not path.exists():
        return set()
    with open(path, "rb") as f:
        d = _json.loads(f.read())
    events = d.get("events") if isinstance(d, dict) else d
    if not isinstance(events, list):
        raise ValueError(f"{path}: 'events' が配列でない")
    out: set[int] = set()
    for e in events:
        eid = e.get("event_id") if isinstance(e, dict) else e
        if not isinstance(eid, int):
            raise ValueError(f"{path}: event_id が整数でない: {e!r}")
        out.add(eid)
    return out

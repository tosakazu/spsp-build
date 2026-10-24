"""
Load Japanese Singles 1on1 tournaments from the smash_database, time-sorted.

Unit of observation: Tournament (one event_dir). Each tournament has:
  - timestamp (the startAt from attr.json, shared by all matches in it)
  - standings: [(placement, user_id), ...] sorted by placement (ties allowed)
  - matches: list[Match] (possibly empty)
  - event_path (for logging)

Filters:
- region == spsp.paths.REGION (既定 Japan、SPSP_REGION で切替)
- derived.json の is_1on1 / is_offline (判定は smash_database 側 scripts/Japan/classify.py と derive.py)
- num_entrants > 0
- match has non-null winner_id/loser_id
- match.state == 3 (completed)
- not DQ and not cancel
"""
from __future__ import annotations

import json

import orjson
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterator


from spsp.paths import LEGACY_ORDER_PATH as _LEGACY_ORDER_PATH, rel_event_path, REGION  # noqa: E402
from spsp import derived  # noqa: E402
_legacy_order_cache = None


def _load_legacy_order() -> dict:
    """同一 timestamp の大会の凍結済み並び {events/ からの相対パス: 順位}。無ければ空 (= 新規則のみ)。"""
    global _legacy_order_cache
    if _legacy_order_cache is None:
        if _LEGACY_ORDER_PATH.exists():
            with open(_LEGACY_ORDER_PATH, encoding='utf-8') as f:
                _legacy_order_cache = json.load(f).get('order', {})
        else:
            print(f"  WARN: {_LEGACY_ORDER_PATH} が無い — 同一 timestamp の大会は新規則だけで並ぶ (2026-09-06 以前の順位が変わる)", flush=True)
            _legacy_order_cache = {}
    return _legacy_order_cache


def _skipped(tag: str, detail: str) -> None:
    """読めなかった入力を数える (v4/skiplog)。data_loader は v3/ より先に import されることがあるので遅延 import。"""
    from spsp.skiplog import skipped
    skipped(tag, detail)


# slots: 試合 1 件 256 → 200 バイト (北米で 580 万件、約 0.3 GB)。Match は中間ファイル (snap → build、同じ実行の中) にしか
# pickle しないので、形が変わっても古いファイルを読むことは無い
@dataclass(frozen=True, slots=True)
class Match:
    timestamp: int
    winner_id: int
    loser_id: int
    event_path: str
    # phase 内クラス bracket (Bクラス・Cクラス等) の match かどうか.
    # True の場合は BT-gated 学習でレアプレイヤー限定の扱いとなる.
    is_class: bool = False
    # Bracket position info (matches.json v2 由来). 古いデータでは None.
    round: int | None = None           # start.gg round 番号 (WB は正、LB は負, GF=0)
    round_text: str | None = None      # "Winners Round 2", "Losers Final" 等
    phase_name: str | None = None
    phase_bracket_type: str | None = None  # DOUBLE_ELIMINATION / SINGLE_ELIMINATION / ROUND_ROBIN / CUSTOM_SCHEDULE
    phase_group_id: int | None = None         # phase_group の start.gg ID (= DAY 判定用)
    phase_group_start_at: int | None = None   # phase_group 開始予定時刻 (Unix sec, JST)
    wave_id: int | None = None
    wave_start_at: int | None = None
    winners_top: int | None = None     # phase-internal WB 敗北 → 落ちる TOP X
    losers_top: int | None = None      # phase-internal LB 敗北 → 確定する TOP X
    bracket_label: str | None = None   # phase-internal "Winners TOP X" / "Losers TOP X"
    global_round: int | None = None    # main bracket 累積 WB round 番号
    global_top_x: int | None = None    # bracket_capacity / 2^(global_round-1) または LB placement bucket
    global_bracket_label: str | None = None   # global "Winners TOP X" / "Losers TOP X"
    winner_score: int | None = None    # 試合スコア (勝者側のセット獲得数)
    loser_score: int | None = None     # 試合スコア (敗者側のセット獲得数)


@dataclass
class Tournament:
    timestamp: int
    event_path: str
    # (placement, user_id). placement may repeat for ties.
    standings: list[tuple[int, int]]
    matches: list[Match]
    end_timestamp: int | None = None
    # attr.json の event_id。同 timestamp の大会の並びを決定的にするための第 2 キー
    # (無いと os.walk の readdir 順 = ファイルシステム依存になり、逐次学習の結果が
    #  ディレクトリのコピー先で変わる。2026-09-05 に 731 グループ中 411 が入れ替わるのを確認)。
    event_id: int | None = None
    # 「途中 DQ」プレイヤー一覧 (matches.json で dq=True の match の loser_id).
    # matches リスト自体からは DQ match を除外しているため別途保持.
    # main bracket (phase 内クラス bracket 以外) と class bracket で分離して保持し、
    # 親 event entry vs class virtual entry で DQ flag を別々に判定できるようにする.
    dq_player_ids: set[int] = field(default_factory=set)            # 全体 (union) — backward compat
    dq_player_ids_main: set[int] = field(default_factory=set)       # main bracket での DQ
    dq_player_ids_class: set[int] = field(default_factory=set)      # class bracket での DQ
    # DAY進出判定用: dq=True の match を別途保持 (= main bracket の DAY X 進出後 DQ を拾うため).
    # (loser_user_id, phase_group_id, phase_group_start_at) のタプル. is_class は除外.
    dq_match_records_main: list[tuple[int, int | None, int | None]] = field(default_factory=list)


def _load_json(path: Path) -> dict | None:
    # orjson で読む (標準 json の数倍速い。大会 5,000〜25,000 件を毎回読むので効く)。orjson が受け付けない
    # もの (NaN / Infinity のリテラル、64 bit を超える整数など) は標準 json で読み直すので、結果は従来と同じ。
    try:
        with open(path, "rb") as f:
            raw = f.read()
        try:
            return orjson.loads(raw)
        except orjson.JSONDecodeError:
            return json.loads(raw.decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        _skipped('JSON が読めない (大会を飛ばす)', f'{path}: {e}')
        return None


def _unwrap(blob):
    if isinstance(blob, dict) and "data" in blob:
        return blob["data"]
    return blob


class _Share:
    """読み込み中に、同じ値の str / int を 1 つのオブジェクトにまとめる (値・型はそのまま、メモリだけ減る)。

    JSON から読んだ値は試合ごとに別のオブジェクトになる (round_text・phase_name・選手 ID・phase_group_id など、
    同じ値が何万回も出る)。北米 (試合 数百万件) では試合 1 件あたり約 400 バイト、全部で数 GB がこの重複だった
    (2026-10-17)。型ごとに別の表にする (True と 1、1 と 1.0 は == で同じなので、1 つの表だと型が入れ替わる)。"""
    __slots__ = ('s', 'i')

    def __init__(self):
        self.s: dict = {}
        self.i: dict = {}

    def __call__(self, v):
        c = v.__class__
        if c is str:
            return self.s.setdefault(v, v)
        if c is int:
            return self.i.setdefault(v, v)
        return v


def _match_from_row(m: dict, ts: int, event_dir: Path, is_class: bool, sh=None) -> Match:
    """matches.json の 1 行 (完了・DQ でない試合) → Match。sh = _Share (同じ値のオブジェクトをまとめる。無くても値は同じ)。"""
    if sh is None:
        sh = _Share()
    g = m.get
    rr = g("phase_bracket_type") == 'ROUND_ROBIN'
    return Match(
        timestamp=ts,
        winner_id=sh(int(m["winner_id"])),
        loser_id=sh(int(m["loser_id"])),
        event_path=str(event_dir),
        is_class=is_class,
        round=sh(g("round")),
        round_text=sh(g("round_text")),
        phase_name=sh(g("phase_name")),
        phase_bracket_type=sh(g("phase_bracket_type")),
        phase_group_id=sh(g("phase_group_id")),
        phase_group_start_at=sh(g("phase_group_start_at")),
        wave_id=sh(g("wave_id")),
        wave_start_at=sh(g("wave_start_at")),
        winners_top=sh(g("winners_top")),
        losers_top=sh(g("losers_top")),
        bracket_label=sh(g("bracket_label")),
        # ROUND_ROBIN は global_round / global_top_x が無いが、global_bracket_label には
        # カテゴリ名 ("総当たり" 等) を保持する (= 表示用).
        global_round=(None if rr else sh(g("global_round"))),
        global_top_x=(None if rr else sh(g("global_top_x"))),
        global_bracket_label=sh(g("global_bracket_label")),
        winner_score=sh(g("winner_score")),
        loser_score=sh(g("loser_score")),
    )


def iter_tournaments(
    events_root: Path,
    region: str = REGION,
    since_ts: int | None = None,
    until_ts: int | None = None,
    min_entrants: int = 2,
) -> Iterator[Tournament]:
    # events_root は地域の events ディレクトリ (data/startgg/<地域>/events)。region は Tournament の属性用に残す
    region_root = events_root
    if not region_root.is_dir():
        return

    seen_event_ids: set[int] = set()
    sh = _Share()   # 同じ値の str / int をまとめる (読み込み全体で 1 つ)

    for dirpath, dirnames, filenames in os.walk(region_root):
        # readdir 順はファイルシステム依存。名前順に固定して走査自体を決定的にする
        # (最終的な大会順は (timestamp, event_id) で決めるが、重複 event_id の先勝ち等も
        #  走査順に依存するため、ここでも固定しておく)。
        dirnames.sort()
        if "attr.json" not in filenames:
            continue
        if "standings.json" not in filenames:
            continue
        event_dir = Path(dirpath)
        attr = _load_json(event_dir / "attr.json")
        if attr is None:
            continue
        cur = derived.load(event_dir)          # smash_database の判定 (1on1 か / オフラインか / クラス bracket の phase_group)
        if not cur["is_1on1"]:
            continue
        # オンライン大会は集計しない。start.gg の isOnline (attr.offline → derived.is_offline) で決める。
        # 名前に「オンライン」が無い大会 (Smash the Virus 等) はこれまで名前判定をすり抜けていた (2026-09-12〜)。
        if cur["is_offline"] is False:
            continue
        event_id = attr.get("event_id")
        if event_id is not None:
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
        # attr の num_entrants ではフィルタしない: 大会当日 fetch だと空 standings と
        # 共に 0 のまま残ることがあり (Badawi#5 2026-07 で発覚)、standings を復旧しても
        # ここで大会ごと落ちてしまう。実数は後段の len(standings) >= min_entrants で判定。
        ts = attr.get("timestamp")
        if ts is None:
            continue
        if since_ts is not None and ts < since_ts:
            continue
        if until_ts is not None and ts >= until_ts:
            continue

        standings_blob = _load_json(event_dir / "standings.json")
        standings_raw = _unwrap(standings_blob) if standings_blob is not None else None
        if not isinstance(standings_raw, list):
            continue
        standings: list[tuple[int, int]] = []
        for s in standings_raw:
            if not isinstance(s, dict):
                continue
            place = s.get("placement")
            uid = s.get("user_id")
            if place is None or uid is None:
                continue
            standings.append((sh(int(place)), sh(int(uid))))
        if len(standings) < min_entrants:
            continue
        standings.sort(key=lambda x: x[0])
        # NOTE: phase 内クラス bracket は start.gg 側で「単一 DE bracket 相当の placement」
        # に変換されて standings に反映されている (e.g., 篝火#15 の placement 分布は標準 DE pattern と一致).
        # → 追加の clipping は不要 (試みたが A-class TOP 1536 losers まで切り捨ててしまうため revert).

        matches: list[Match] = []
        dq_player_ids: set[int] = set()
        dq_player_ids_main: set[int] = set()
        dq_player_ids_class: set[int] = set()
        dq_match_records_main: list[tuple[int, int | None, int | None]] = []
        # phase 内クラス bracket (Bクラス等) の phase_group_id を事前に把握しておき、
        # 各 match をクラス bracket 由来か否かで分類する.
        class_pg_ids = set(cur["class_bracket"]["phase_group_ids"])
        # 下位クラスの試合で本戦の matches.json に無いもの (Challonge で開いた下位クラス、2026-10〜):
        # smash_database が class_phases/<クラス>_virtual/class_matches.json に start.gg と同じ行の形で置く
        # ({"data": [...], "replaces_phase_group_ids": [...]}、古い形は行の list)。
        # start.gg の下位クラスの試合 (本戦の matches.json にあり phase_group で is_class になる) と同じく、
        # 本戦の試合として is_class=True で学習する。仮想大会側の matches.json は空のまま (二重に数えない)。
        # 同じクラスを start.gg でも作っていた場合、smash_database は進行した方だけを採り、Challonge を採ったときは
        # その start.gg のクラスの phase_group を replaces_phase_group_ids に挙げる → 本戦の matches.json のそれらの
        # 試合 (と DQ 記録) は読まない。start.gg の下位クラスにはこのファイルが無いので、既存の大会の読み込みは変わらない。
        class_rows: list[Match] = []
        replaced_pg_ids: set[int] = set()
        if "class_phases" in dirnames:
            for cm_path in sorted((event_dir / "class_phases").glob("*_virtual/class_matches.json")):
                cm_blob = _load_json(cm_path)
                if isinstance(cm_blob, dict):
                    replaced_pg_ids.update(int(x) for x in cm_blob.get("replaces_phase_group_ids") or [])
                cm_raw = _unwrap(cm_blob)
                if not isinstance(cm_raw, list):
                    continue
                for m in cm_raw:
                    if not isinstance(m, dict) or m.get("state") != 3 or m.get("cancel"):
                        continue
                    wid, lid = m.get("winner_id"), m.get("loser_id")
                    if wid is None or lid is None or wid == lid:
                        continue
                    if m.get("dq"):
                        # 下位クラスの途中 DQ: start.gg の下位クラスの DQ (本戦の matches.json、下の分岐) と同じく、
                        # 試合は学習せず「下位クラスで DQ した」ことだけを記録する
                        dq_player_ids.add(int(lid))
                        dq_player_ids_class.add(int(lid))
                        continue
                    class_rows.append(_match_from_row(m, ts, event_dir, True, sh))
        if "matches.json" in filenames:
            matches_blob = _load_json(event_dir / "matches.json")
            matches_raw = _unwrap(matches_blob) if matches_blob is not None else None
            if isinstance(matches_raw, list):
                for m in matches_raw:
                    if not isinstance(m, dict):
                        continue
                    if m.get("state") != 3:
                        continue
                    if m.get("cancel"):
                        continue
                    wid = m.get("winner_id")
                    lid = m.get("loser_id")
                    if wid is None or lid is None:
                        continue
                    if wid == lid:
                        continue
                    # phase_group_id でクラス bracket 判定 (DQ match / 通常 match 共通)
                    pg_id = m.get("phase_group_id")
                    if replaced_pg_ids and pg_id is not None and str(pg_id).isdigit() and int(pg_id) in replaced_pg_ids:
                        continue   # Challonge の下位クラスに置き換えられた start.gg の下位クラスの試合 (上の説明)
                    is_class_match = False
                    if pg_id is not None and class_pg_ids:
                        try:
                            is_class_match = int(pg_id) in class_pg_ids
                        except (ValueError, TypeError):
                            is_class_match = False
                    if m.get("dq"):
                        # 途中 DQ: 試合自体はスコア計算には含めない (BT 学習等から除外) が、
                        # 「この大会から DQ した」事実は記録. main / class で分離.
                        lid_int = int(lid)
                        dq_player_ids.add(lid_int)
                        if is_class_match:
                            dq_player_ids_class.add(lid_int)
                        else:
                            dq_player_ids_main.add(lid_int)
                            # DAY 進出判定用: phase_group_id + startAt を別途記録
                            _pgid = m.get('phase_group_id')
                            _sa = m.get('phase_group_start_at')
                            if _pgid is not None:
                                dq_match_records_main.append((lid_int, _pgid, _sa))
                        continue
                    matches.append(_match_from_row(m, ts, event_dir, is_class_match, sh))

        matches.extend(class_rows)   # Challonge の下位クラスの試合 (上で読んだもの。本戦の試合の後ろに足す)

        end_ts = attr.get("end_timestamp")
        yield Tournament(
            timestamp=ts,
            event_id=int(event_id) if event_id is not None else None,
            end_timestamp=int(end_ts) if end_ts is not None else None,
            event_path=str(event_dir),
            standings=standings,
            matches=matches,
            dq_player_ids=dq_player_ids,
            dq_player_ids_main=dq_player_ids_main,
            dq_player_ids_class=dq_player_ids_class,
            dq_match_records_main=dq_match_records_main,
        )


def _resolve_merge_chains(merges: dict[int, int]) -> dict[int, int]:
    """{A: B, B: C} を {A: C, B: C} に展開. cycle は ValueError.

    複数 old uid を同じ new uid に向ける形 (= {A: D, B: D, C: D}) はそのまま動く.
    """
    resolved: dict[int, int] = {}
    for old in merges:
        seen = {old}
        cur = old
        while cur in merges:
            cur = merges[cur]
            if cur in seen:
                raise ValueError(
                    f"USER_MERGES cycle: {seen | {cur}}"
                )
            seen.add(cur)
        resolved[old] = cur
    return resolved


def apply_user_merges(
    tournaments: list[Tournament], merges: dict[int, int] | None = None
) -> None:
    """In-place: 旧 uid → 新 uid に rewrite. standings / matches / dq sets 全部.

    None なら smash_database の manual/user_merges.json を使用. {} なら no-op.
    複数 old → 同 new (= {A: D, B: D, C: D}) も連鎖 (= {A: B, B: C}) も OK.
    """
    if merges is None:
        try:
            merges = derived.load_user_merges()   # smash_database の manual/user_merges.json
        except ImportError:
            merges = {}
    if not merges:
        return
    merges = _resolve_merge_chains(merges)
    n_st = n_m = n_dq = 0
    for t in tournaments:
        # standings: rewrite + dedupe in-place (= 同大会で旧+新が両方出てた場合は
        # 先に登場した方を残す. 通常は片方しか出場しないので副作用なし).
        new_st: list[tuple[int, int]] = []
        seen: set[int] = set()
        for place, uid in t.standings:
            new_uid = merges.get(uid, uid)
            if new_uid != uid:
                n_st += 1
            if new_uid in seen:
                continue
            new_st.append((place, new_uid))
            seen.add(new_uid)
        t.standings = new_st
        # matches: frozen dataclass なので replace で再構築. self-match (= 同大会で
        # 旧+新が対戦してた場合) は drop.
        new_matches: list[Match] = []
        for m in t.matches:
            new_w = merges.get(m.winner_id, m.winner_id)
            new_l = merges.get(m.loser_id, m.loser_id)
            if new_w == new_l:
                continue
            if new_w == m.winner_id and new_l == m.loser_id:
                new_matches.append(m)
            else:
                new_matches.append(replace(m, winner_id=new_w, loser_id=new_l))
                n_m += 1
        t.matches = new_matches
        # dq sets: rewrite
        for attr in ("dq_player_ids", "dq_player_ids_main", "dq_player_ids_class"):
            old_set = getattr(t, attr)
            if not old_set:
                continue
            new_set = {merges.get(u, u) for u in old_set}
            if new_set != old_set:
                n_dq += len(old_set - new_set)
            setattr(t, attr, new_set)
        # dq_match_records_main: tuple 要素を rewrite
        t.dq_match_records_main = [
            (merges.get(uid, uid), pg, pgs)
            for (uid, pg, pgs) in t.dq_match_records_main
        ]
    if n_st or n_m or n_dq:
        print(
            f"[user_merges] applied {len(merges)} mapping(s): "
            f"{n_st} standings, {n_m} matches, {n_dq} dq entries rewritten",
            flush=True,
        )


def load_all_tournaments(
    events_root: Path,
    region: str = REGION,
    since_ts: int | None = None,
    until_ts: int | None = None,
    min_entrants: int = 2,
) -> list[Tournament]:
    tournaments = list(
        iter_tournaments(events_root, region, since_ts, until_ts, min_entrants)
    )
    # 同 timestamp の大会は event_id で並べる。逐次学習は大会順に更新するので、
    # ここが安定ソートだけだと readdir 順で結果が変わる (順位 ±1 のずれが 2 万人規模で出る)。
    # 同一 timestamp の大会の並び (学習は大会を逐次処理するので、並びが順位に影響する):
    #   1. 2026-09-06 時点で存在した大会は、当時の本番の並び (build/data/legacy_tournament_order.json に凍結)。
    #      以前はディレクトリの読み出し順 (ファイルシステム依存) だったので、それをそのまま残して順位を変えない。
    #   2. 表に無い大会 (以後の新規) は決定的な規則: 本戦 (親イベント) → 下位クラスの仮想大会 (class_phases/*_virtual)
    #      → event_id → path。仮想大会は event_id が負なので単純な event_id 順だと本戦より先に来てしまう。
    legacy = _load_legacy_order()
    def _order_key(t):
        rel = rel_event_path(t.event_path)   # 凍結表のキー "<地域>/<年>/..." (旧配置でも新配置でも同じ)
        is_virtual = '/class_phases/' in t.event_path
        return (t.timestamp, legacy.get(rel, 1 << 30), is_virtual,
                t.event_id if t.event_id is not None else -1, t.event_path)
    tournaments.sort(key=_order_key)
    apply_user_merges(tournaments)
    return tournaments


# Backwards-compat helpers: older callers wanted a flat match list.
def iter_matches(
    events_root: Path,
    region: str = REGION,
    since_ts: int | None = None,
    until_ts: int | None = None,
) -> Iterator[Match]:
    for t in iter_tournaments(events_root, region, since_ts, until_ts):
        for m in t.matches:
            yield m


def load_all_matches(
    events_root: Path,
    region: str = REGION,
    since_ts: int | None = None,
    until_ts: int | None = None,
) -> list[Match]:
    matches = list(iter_matches(events_root, region, since_ts, until_ts))
    matches.sort(key=lambda m: m.timestamp)
    return matches


if __name__ == "__main__":
    import argparse
    import datetime as dt

    ap = argparse.ArgumentParser()
    ap.add_argument("--events-root", default="../smash_db_tournament/data/startgg/Japan/events")
    ap.add_argument("--region", default=REGION)
    ap.add_argument("--since", default=None, help="YYYY-MM-DD")
    ap.add_argument("--until", default=None, help="YYYY-MM-DD")
    args = ap.parse_args()

    since_ts = int(dt.datetime.fromisoformat(args.since).timestamp()) if args.since else None
    until_ts = int(dt.datetime.fromisoformat(args.until).timestamp()) if args.until else None

    tournaments = load_all_tournaments(Path(args.events_root), args.region, since_ts, until_ts)
    total_matches = sum(len(t.matches) for t in tournaments)
    total_standings = sum(len(t.standings) for t in tournaments)
    print(f"Loaded {len(tournaments):,} tournaments")
    print(f"  Total matches:   {total_matches:,}")
    print(f"  Total standings: {total_standings:,}")
    players = set()
    for t in tournaments:
        for _, uid in t.standings:
            players.add(uid)
    print(f"  Unique players:  {len(players):,}")
    if tournaments:
        print(f"  Date range: {dt.datetime.fromtimestamp(tournaments[0].timestamp)} -> {dt.datetime.fromtimestamp(tournaments[-1].timestamp)}")

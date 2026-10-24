"""V4 tournament メタ解析モジュール.

build_tjpr_ranking.py から tournament name のシリーズ抽出 / シリーズ番号 /
個人賞ラベル / クラス判定 / event_id / seeds 取得 等の関数群を移動.

大会名から導くラベル (シリーズ名 / 開催回 / 実績ラベル / 中止・テスト検出) は 2026-09-09 に
smash_database (scripts/Japan/naming.py) へ移した。ここでは読み込み口だけを再 export する。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# v4.common から re-export 経由で参照する内部 helper.
import functools

from spsp.skiplog import skipped



# ── 大会名から決まるラベルは smash_database (scripts/<地域>/naming.py) が定義元 ──
# 各イベントの判定結果は derived.json の "naming" にもある。ここは名前だけを持つ場面 (実績バッジの集約など) 用。
# derive.py が地域の全大会名について評価して region_rules.json に書いたものを引く (ビルドは地域のコードを読まない)。
def _labels(name: str) -> dict:
    from spsp.derived import tournament_name_labels
    return tournament_name_labels(name)


def tournament_series(name: str) -> str:
    return _labels(name)["series"]


def tournament_series_number(name: str):
    return _labels(name)["series_number"]


def tournament_award_label(name: str) -> str:
    return _labels(name)["award_label"]


def tournament_individual_label(name: str) -> str:
    return _labels(name)["individual_label"]







def tournament_event_id(event_path: str) -> int | None:
    """attr.json から event_id を返す。失敗時は None。"""
    try:
        with open(Path(event_path) / "attr.json", "rb") as f:
            d = json.loads(f.read())
        return int(d.get("event_id")) if d.get("event_id") is not None else None
    except Exception as e:
        skipped('attr.json から event_id が読めない', f'{event_path}: {e}')
        return None


def tournament_is_class_virtual(event_path: str) -> bool:
    """クラス bracket の仮想大会か (smash_database の derived.json。判定はディレクトリの形)."""
    from spsp import derived
    return bool(derived.load(event_path)["is_class_virtual"])


def tournament_class_letter(event_path: str) -> str | None:
    """仮想大会の class letter (B/C/D/E 等). 通常 event は None."""
    from spsp import derived
    return derived.load(event_path)["class_letter"] or None


def tournament_parent_event_id(event_path: str) -> int | None:
    """virtual sub-tournament の親 event_id を返す.

    virtual の event_path は <parent>/class_phases/<letter>_virtual/.
    親の attr.json から event_id を読み取る. 親が見つからなければ None.
    """
    p = Path(event_path)
    # virtual sub-tournament の親 dir 階層: class_phases/<letter>_virtual → ../../
    if p.parent.name == "class_phases" or "_virtual" in p.name:
        parent = p.parent.parent if p.parent.name == "class_phases" else p.parent
        try:
            with open(parent / "attr.json", "rb") as f:
                d = json.loads(f.read())
            return d.get("event_id")
        except Exception as e:
            skipped('親 attr.json から event_id が読めない', f'{parent}: {e}')
            return None
    return None


def load_event_seeds(event_path: str) -> dict[int, int]:
    """seeds.json から {user_id: seed_num} を返す。実際に start.gg で使われたシード。"""
    sp = Path(event_path) / "seeds.json"
    if not sp.exists():
        # 古い大会 (2019 年頃まで) には無いのが普通なので、読めない件とは分けて数える
        skipped('seeds.json が無い (古い大会は普通)', str(event_path))
        return {}
    try:
        with open(sp, "rb") as f:
            d = json.loads(f.read())
        items = d if isinstance(d, list) else (d.get("data") or [])
        out = {}
        for it in items:
            uid = it.get("user_id"); sn = it.get("seed_num")
            if uid is None or sn is None: continue
            out[int(uid)] = int(sn)
        return out
    except Exception as e:
        skipped('seeds.json が読めない', f'{event_path}: {e}')
        return {}


def resolve_completion_flags(gf_missing_base: bool, no_champion: bool, event_id, awaiting_ids) -> tuple[bool, bool]:
    """未完了 (is_gf_missing) と再開待ち (is_awaiting_resume) の決定。

    再開待ち = 登録簿に載っていて、かつまだ優勝者が居ない。再開待ちなら未完了扱いにしない
    (= 途中までの結果で集計)。優勝者が出れば登録が残っていても両方 False (通常の完了大会)。
    """
    awaiting = bool(no_champion) and (event_id in awaiting_ids)
    return (bool(gf_missing_base) and not awaiting), awaiting


def build_tournament_metadata(all_tournaments, *, tournament_display_name,
                                min_entrants_for_ranking=8):
    """大会群から t_meta_by_id (= id(t) -> meta dict) を一括構築.

    各 meta dict は event_id / nent / 日付 / weekend / 各種 pattern flag
    / startgg_url / class virtual 関連 / GF 欠損フラグ / is_small を含む.

    引数:
      all_tournaments: List[Tournament]
      tournament_display_name: callable(event_path) -> (tname, ename, nent_attr).
        eval/build_latest_ranking.py の関数を caller 側から注入する
        (= v4 内では eval/ に依存しないため hook 経由で渡す).
      min_entrants_for_ranking: int. これ未満の nent の大会は is_small=True に.
        ランキング計算 (= BT/TJPR/SPR/UF/radar/peak) からスキップされるが、
        大会一覧 / ローカルランキング / per-tournament JSON には残す.

    元 build_tjpr_ranking.main() の inline メタ構築ループを関数化 (反復16).
    """
    # 遅延 import: build_tjpr_ranking が import 時に v4.meta を読むため
    # v4.common との top-level 相互参照を避ける.
    from spsp.common import FORCE_WEEKEND_MIN_NENT, SMACOMI_FORCE_WEEKDAY_MAX_NENT
    from spsp import derived
    import datetime as dt

    def _tournament_dir(event_path: str) -> str:
        # 通常 event: <date>/<tournament>/<event> → parent = tournament dir.
        # class virtual: <event>/class_phases/<x>_virtual → event dir に上がってから parent.
        p = Path(event_path)
        if p.parent.name == "class_phases":
            p = p.parent.parent
        elif p.name.endswith("_virtual"):
            p = p.parent
        return str(p.parent)

    # ── 制限判定の同時開催 pre-pass ──
    # is_restricted は大会名 (tname) にもマッチするため、「制限イベントと無制限イベントを
    # 同一大会ページで同時開催し、大会名に制限キーワードを含む」場合
    # (例: 第2.5回スマフェス(通常トナメ＆R1600制限トナメの同時開催)) に
    # 無制限イベント側まで制限扱いになる。同一大会 dir 内に ename 自体が制限マッチする
    # イベントが存在する場合、制限はそのイベントに付いているとみなし、ename 非マッチの
    # イベントには tname 由来の制限を適用しない。制限を tname だけで表現する
    # 単独イベント大会 (雛囃子 / 九龍LIMIT BREAK / 鬼灯火 等) は従来どおり制限扱い。
    _disp_by_path: dict = {}
    _ename_restricted_tdirs: set = set()
    for t in all_tournaments:
        _dsp = tournament_display_name(t.event_path)
        _disp_by_path[t.event_path] = _dsp
        if derived.names(t.event_path)['restricted_ename']:
            _ename_restricted_tdirs.add(_tournament_dir(t.event_path))

    t_meta_by_id: dict = {}
    from spsp.tournament_utils import load_awaiting_resume_ids as _load_awaiting   # 循環 import 回避で関数内
    _awaiting_ids = _load_awaiting()
    for t in all_tournaments:
        tname, ename, nent_attr = _disp_by_path[t.event_path]
        # attr.json から url (= start.gg tournament path) と has_gf_recorded を読む
        _cur = derived.load(t.event_path)
        _nm = _cur['names']
        try:
            with open(Path(t.event_path) / "attr.json", "rb") as _af:
                _attr_d = json.loads(_af.read())
            _startgg_url_path = _attr_d.get("url") or ""
        except Exception as e:
            skipped('attr.json から url が読めない', f'{t.event_path}: {e}')
            _startgg_url_path = ""
        _has_gf_recorded = _cur['results']['has_gf_recorded']  # True / False / None (= 未マーク)
        # is_gf_missing: DE bracket を含む event で GF が記録されてない場合のみ True
        # (= start.gg 側でデータ欠損). 以下のケースは除外:
        #   - class_virtual: 元々 GF を持たない sub-bracket 設計
        #   - SE-only event: "Final" は あるが "Grand Final" ラベル無いだけで正常
        #   - RR-only / CUSTOM_SCHEDULE: そもそも GF の概念無い
        _is_class_virtual_pre = tournament_is_class_virtual(t.event_path)
        _has_de_phase = _cur['results']['has_de_phase']
        # has_gf_recorded は過去の一括バックフィルでしか付かない (新規 DL は None) ので、
        # standings に優勝者 (place=1) が居ないことからも「結果未確定」を直接導出する.
        _no_champion = False
        try:
            _pl = [p for p, _u in t.standings if p is not None]
            _no_champion = bool(_pl) and min(_pl) != 1
        except Exception as e:
            skipped('standings から優勝者判定ができない', f'{t.event_path}: {e}')
            _no_champion = False
        _gf_missing_base = (
            ((_has_gf_recorded is False) or _no_champion)
            and ((nent_attr or 0) >= 8 or (len(t.standings) or 0) >= 8)
            and not _is_class_virtual_pre
            and _has_de_phase  # DE phase が存在する場合のみ GF 必須
        )
        # 再開待ち (延期・中断で後日再開予定、build/data/awaiting_resume.json に登録):
        # 優勝者未確定でも除外せず、その時点までの結果 (完了した試合 + start.gg が付ける
        # 最低保証順位) で集計する。優勝者が出たら自動で通常扱い。
        _is_gf_missing, _is_awaiting = resolve_completion_flags(
            _gf_missing_base, _no_champion, t.event_id, _awaiting_ids)
        # nent: フィルター後の standings 数を使う (DQ 無参加を除外したサイズ)
        nent = len(t.standings) or nent_attr or 0
        d = dt.datetime.fromtimestamp(t.timestamp).date()
        end_ts = t.end_timestamp if t.end_timestamp is not None else t.timestamp
        end_d = dt.datetime.fromtimestamp(end_ts).date()
        # 開催期間中 (start〜end) のいずれかが土日祝なら weekend 扱い
        is_wk_real = _cur['calendar']['is_weekend_real']
        is_pre = _nm['pre']
        # 実質平日ルール (= 大菊月 / 渋谷BeeSmash(BIG除く) / 40人未満の上野スマコミ):
        # 休日に開催されても平日扱い. 名前部分は derived.json、参加者数との合成はここ.
        is_force_weekday = _nm['force_weekday'] or (_nm['smacomi'] and nent < SMACOMI_FORCE_WEEKDAY_MAX_NENT)
        is_non_serious = _nm['non_serious']
        is_restricted = _nm['restricted_tname'] or _nm['restricted_ename']
        # 同時開催の巻き込まれ解除 (pre-pass 参照): tname 由来のみの制限で、
        # 同一大会内に ename 制限イベントが別にあるなら本イベントは無制限。
        if (is_restricted and not _nm['restricted_ename']
                and _tournament_dir(t.event_path) in _ename_restricted_tdirs):
            is_restricted = False
        is_lower_class = _nm['lower_class']
        # Class virtual sub-tournament (e.g., 篝火#15 Bクラス virtual): TJPR の placement scoring 用.
        # BT 学習 / DQ filter は親 event 側で完結するため、ここでは scoring のみ.
        is_class_virtual = tournament_is_class_virtual(t.event_path)
        parent_eid = tournament_parent_event_id(t.event_path) if is_class_virtual else None
        class_letter = tournament_class_letter(t.event_path) if is_class_virtual else None
        if is_class_virtual:
            is_lower_class = True  # virtual は必ず下位クラス扱い (Lv4/Lv5 除外)
        # 実質休日: お盆/年末年始の nent>=80 大会は平日開催でも休日扱い
        is_force_weekend = (not is_wk_real and nent >= FORCE_WEEKEND_MIN_NENT
                            and _cur['calendar']['is_force_weekend_period'])
        # プレ大会 / FORCE_WEEKDAY 対象シリーズは計算上は平日扱い
        # (BT-gated gating / Banzuke weekend-only filter で平日相当)
        is_wk = (is_wk_real or is_force_weekend) and not is_pre and not is_force_weekday
        eid = t.event_id   # = attr.json の event_id (data_loader が読んだ値。読み直さない)
        t_meta_by_id[id(t)] = {
            'event_id': eid,
            'nent': nent,
            'tname': tname or '',
            'ename': ename or '',
            'ts': int(t.timestamp),  # 同日内の順序 (本戦 → 下位クラス) を保持する sort 用
            'date': d.isoformat(),
            'end_date': end_d.isoformat(),
            'is_smapa': _nm['smapa'],
            'is_weekend': is_wk,
            'is_weekend_real': is_wk_real,  # 表示用: 実暦の土日祝
            'is_pre': is_pre,
            'is_non_serious': is_non_serious,  # おま5/タイマン等: スコア計算から除外
            'is_restricted': is_restricted,    # 制限大会 (Lv4/5 除外)
            'is_lower_class': is_lower_class,  # 下位クラス (Lv4/5 除外)
            'is_class_virtual': is_class_virtual,  # クラス bracket virtual sub-tournament
            'parent_event_id': parent_eid,         # virtual のみ: 親 event id (matches lookup 用)
            'class_letter': class_letter,          # virtual のみ: B/C/D/E
            'startgg_url': f"https://www.start.gg{_startgg_url_path}" if _startgg_url_path else None,
            'is_gf_missing': _is_gf_missing,       # nent>=8 で GF 試合が start.gg 側で欠損 → 集計除外
            'has_gf_recorded': _has_gf_recorded,   # 生フラグ (= attr.json 由来, 未マークは None)
            'is_small': nent < min_entrants_for_ranking,  # 参加者少: ランキング集計対象外 (一覧には残す)
        }
        # 再開待ちのときだけキーを足す (meta は学習台帳のハッシュに入るので、他の大会の
        # ダイジェストを変えない = 既存チェックポイントを無効化しない)。
        if _is_awaiting:
            t_meta_by_id[id(t)]['is_awaiting_resume'] = True
        elif t.event_id in _awaiting_ids and not _no_champion:
            # 登録は残っているが優勝者確定済み = 通常扱いに戻っている。登録簿から消してよい
            # (build/awaiting_resume.py --prune)。挙動には影響しない。
            print(f"[awaiting] event {t.event_id} ({tname} / {ename}) は優勝者確定済み — "
                  f"manual/awaiting_resume.json から消してよい (spsp/cli/awaiting_resume.py --prune)", flush=True)
    return t_meta_by_id


def build_multiday_max_day(all_tournaments, t_meta_by_id):
    """大規模 multi-day 大会の per-player max DAY 計算 (DAY X 進出 実績用).

    条件: nent ≥ 256 AND 開催日程 ≥ 2 days. phase_group.startAt を JST 日付ベースで day 割当.

    返り値: (per_tournament_max_day, per_tournament_total_days)
      - per_tournament_max_day: {id(t) -> {uid -> max_day_int}}
      - per_tournament_total_days: {id(t) -> total_days_int}

    元 build_tjpr_ranking.main() の inline ループを関数化 (反復16).
    """
    from spsp.common import compute_per_player_max_day

    per_tournament_max_day: dict = {}
    per_tournament_total_days: dict = {}
    for t in all_tournaments:
        meta_ = t_meta_by_id.get(id(t))
        if meta_ is None:
            continue
        nent_ = meta_.get('nent') or 0
        if nent_ < 256:
            continue
        if meta_.get('is_class_virtual'):
            continue
        max_day_map = compute_per_player_max_day(t, event_id=meta_.get('event_id'))
        if not max_day_map:
            continue
        total_days = max(max_day_map.values())
        if total_days < 2:
            continue
        per_tournament_max_day[id(t)] = max_day_map
        per_tournament_total_days[id(t)] = total_days
    return per_tournament_max_day, per_tournament_total_days


def build_eid_to_parent_tid(t_meta_by_id):
    """event_id → parent tid (= is_class_virtual=False の id(t)) のマッピング.

    class_virtual entry の wins/losses 計算で parent event の class match を引くのに使う.
    元 build_tjpr_ranking.main() の inline 構築ループを関数化 (反復16).
    """
    eid_to_parent_tid: dict = {}
    for tid_pt, meta_pt in t_meta_by_id.items():
        if meta_pt.get('is_class_virtual'):
            continue
        ev = meta_pt.get('event_id')
        if ev is None:
            continue
        eid_to_parent_tid[ev] = tid_pt
    return eid_to_parent_tid


class SeedInfo(tuple):
    """t_seed_info の 1 人分 (大会 × 参加者): (seed, place, spr)。以前は 3 項目の dict (北米で 約 300 万件、1 件 184 バイト)。
    読む側は si['seed'] / si.get('spr') のように名前で引くだけなので、tuple に名前で引く [] と .get を足したもの。
    書き換えない (ランダムシード大会の spr を消すときは with_spr_none() で作り直す)。"""
    __slots__ = ()
    _POS = {'seed': 0, 'place': 1, 'spr': 2}

    def __getitem__(self, key):
        if type(key) is str:
            return tuple.__getitem__(self, self._POS[key])
        return tuple.__getitem__(self, key)

    def get(self, key, default=None):
        i = self._POS.get(key)
        return default if i is None else tuple.__getitem__(self, i)

    def with_spr_none(self) -> 'SeedInfo':
        return SeedInfo((tuple.__getitem__(self, 0), tuple.__getitem__(self, 1), None))


def build_placements_seeds(all_tournaments):
    """大会群から placements_per_t と t_seed_info を一括構築.

    返り値:
      placements_per_t: {id(t) -> {uid -> place}}  standings から重複排除して構築
      t_seed_info:      {id(t) -> {uid -> {'seed', 'place', 'spr'}}}
                        seeds.json と placements を突合して SPR (placement_to_w2w
                        ベース) を計算.

    元 build_tjpr_ranking.main() の inline 構築ブロックを関数化.
    """
    # 遅延 import (build_tjpr_ranking が import 時に v4.meta も読むため循環回避)
    from spsp.common import placement_to_w2w

    placements_per_t: dict = {}
    for t in all_tournaments:
        seen = set()
        place_map: dict = {}
        for p, u in t.standings:
            if u in seen: continue
            seen.add(u); place_map[u] = p
        placements_per_t[id(t)] = place_map

    t_seed_info: dict = {}
    for t in all_tournaments:
        tid = id(t)
        placements = placements_per_t.get(tid, {})
        if not placements: continue
        seeds_map = load_event_seeds(t.event_path)
        per_player: dict = {}
        for u, place in placements.items():
            if place is None or place < 1: continue
            seed = seeds_map.get(u)
            if seed is None: continue
            spr = placement_to_w2w(seed) - placement_to_w2w(place)
            per_player[u] = SeedInfo((seed, place, spr))
        t_seed_info[tid] = per_player

    return placements_per_t, t_seed_info

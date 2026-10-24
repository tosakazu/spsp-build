"""derived.json — smash_database の derive.py が各イベントディレクトリに書く「データとしての判定」を読む。

2026-09-08 から、1on1 かどうか・大会名のパターン (特殊ルール / 身内 / 制限 / 下位クラス / プレ / スマパ / 実質平日)・
暦 (土日祝・お盆年末年始)・クラス bracket の phase_group 集合は smash_database 側 (scripts/Japan/classify.py) が判定し、
spsp は読むだけ。無ければ止める (推測やフォールバックはしない): データ側で
`python3 scripts/common/derive.py --region Japan --all` を走らせる。

読んだ事実をどう合成するか (同時開催の制限解除、参加者数の閾値、平日扱いの合成) は各 build モジュールに残る。
"""
from __future__ import annotations

import functools
import json
from pathlib import Path

from spsp.paths import TOURNAMENT_DB, MANUAL_DIR, USERS_DERIVED, REGION, REGION_DB

FILE = "derived.json"


@functools.lru_cache(maxsize=None)
def load(event_path) -> dict:
    p = Path(event_path) / FILE
    try:
        return json.loads(p.read_bytes())
    except FileNotFoundError:
        raise SystemExit(f"ERROR: {p} が無い — smash_database で `python3 scripts/common/derive.py --region Japan --all` を実行してから build する")


def names(event_path) -> dict:
    return load(event_path)["names"]


def calendar(event_path) -> dict:
    cal = load(event_path).get("calendar")
    if cal is None:
        raise SystemExit(f"ERROR: {Path(event_path) / FILE} に calendar が無い (timestamp 無しのイベント)")
    return cal


RULES_FILE = "region_rules.json"


@functools.lru_cache(maxsize=None)
def region_rules() -> dict:
    """smash_database の derive.py が地域の規則を評価して書いた data/startgg/<地域>/region_rules.json。
    定数 (TIMEZONE、人数の閾値)、地域の国コード、国名の日本語表記、大会名ごとのラベル (シリーズ・開催回・実績ラベル・
    中止 / テスト)、シリーズごとのコミュニティと名前の印。
    2026-10-08 まではここで smash_database の scripts/<地域>/*.py を import していたが、データのブランチ (北米は外部の
    協力者が push できる) のコードがデプロイ用の鍵のあるビルドの中で動くので、ビルドはデータ (この JSON) だけを読む
    ようにした (セキュリティレビュー H1)。無ければ止める (データ側で derive.py を走らせる)。"""
    p = REGION_DB / RULES_FILE
    try:
        return json.loads(p.read_bytes())
    except FileNotFoundError:
        raise SystemExit(f"ERROR: {p} が無い — smash_database で `python3 scripts/common/derive.py --region {REGION}` を実行してから build する")


def tournament_name_labels(name: str) -> dict:
    """大会名 → {series, series_number, award_label, individual_label, cancelled, test_page} (region_rules.json)。
    表に無い名前は止める (推測しない)。"""
    try:
        return region_rules()["tournament_names"][name]
    except KeyError:
        raise SystemExit(f"ERROR: 大会名 {name!r} が {REGION_DB / RULES_FILE} に無い — smash_database の derive.py を走らせ直す")


def series_rules(series: str) -> dict:
    """シリーズ名 (derived.json の naming.series_event) → {community, non_serious, uchi, special_rules, restricted}。"""
    try:
        return region_rules()["series"][series]
    except KeyError:
        raise SystemExit(f"ERROR: シリーズ {series!r} が {REGION_DB / RULES_FILE} に無い — smash_database の derive.py を走らせ直す")


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_bytes())
    except FileNotFoundError:
        raise SystemExit(f"ERROR: {p} が無い — smash_database の data-{REGION} (data/startgg/{REGION}/manual/) に手動管理の表を置く")


@functools.lru_cache(maxsize=None)
def load_user_merges() -> dict:
    """manual/user_merges.json → {old_uid: new_uid} (連鎖の解決は data_loader._resolve_merge_chains)。"""
    d = _read_json(MANUAL_DIR / "user_merges.json")
    out = {}
    for e in d.get("merges") or []:
        out[int(e["old"])] = int(e["new"])
    return out


def load_overseas_manual() -> dict:
    """manual/overseas_manual.json (uids: 国未登録の海外勢、home_uids: 地域勢扱い。旧 jp_uids)。"""
    return _read_json(MANUAL_DIR / "overseas_manual.json")


def iter_users_derived():
    """users_derived.jsonl の行 (user_id, geo, geo_reason)。geo は geo.json の unit id (日本: 都道府県名)。無ければ止める。"""
    try:
        f = open(USERS_DERIVED, encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"ERROR: {USERS_DERIVED} が無い — smash_database で `python3 scripts/common/derive.py --region Japan --all` を実行する")
    with f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)

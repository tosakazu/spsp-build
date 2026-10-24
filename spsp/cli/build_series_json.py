#!/usr/bin/env python3
"""大会シリーズ別の参加者集合 JSON を生成する.

ローカルランキング機能 (= シリーズ別ランキング) 用. attr.json + standings.json から
シリーズ単位の参加者集合と参加履歴を集約.

dedup ルール:
  - シリーズインスタンス: (date YYYY-MM-DD, tournament_name) 単位で 1 大会扱い
    → 同一大会内の Singles/Doubles/Squad Strike 等のサブイベントは 1 にまとまる.
  - 参加者: 上の (date, tournament_name) 単位で uid を unique 化, 1 大会 = 1 参加カウント.

出力: <build-dir>/data/series.json (--build-dir 既定 = site/)
入力: <build-dir>/{meta.json, latest_tjpr_full.jsonl} と events 配下の attr.json / standings.json
"""
from __future__ import annotations
import sys, json, datetime, re
from pathlib import Path
from collections import defaultdict

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # リポジトリ直下 (spsp パッケージ)
from spsp.paths import SITE_DIR, EVENTS_ROOT, REGION  # noqa: E402
from spsp.tournament_utils import load_min_entrants_threshold as _load_min_entrants_threshold  # noqa: E402
from spsp.skiplog import skipped, report_skips  # noqa: E402
# シリーズ名・コミュニティ・名前の印・中止/テストの判定は smash_database (scripts/<地域>/) が定義元。derive.py が評価して
# 各イベントの derived.json (naming.series_event) と region_rules.json に書いたものを読む (ビルドは地域のコードを読まない)。
from spsp import derived  # noqa: E402
from spsp.fileio import write_json_atomic  # noqa: E402
from spsp.common import series_slug  # noqa: E402
REGION_COUNTRY_CODES = frozenset(derived.region_rules()["country_codes"])




def _load_named_uids(jsonl_path: Path) -> set[int]:
    """latest_tjpr_full.jsonl から「名前が取れるプレイヤー」の uid 集合を返す.
    master ranking JSONL に存在する uid のみ "名前あり" として扱う.
    """
    named: set[int] = set()
    if not jsonl_path.exists():
        return named
    with open(jsonl_path, "rb") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception as e:
                skipped('latest_tjpr_full.jsonl の行が読めない', f'{jsonl_path}: {e}')
                continue
            uid = rec.get("user_id")
            if uid is None:
                continue
            try:
                named.add(int(uid))
            except (TypeError, ValueError):
                pass
    return named


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--build-dir', default=str(SITE_DIR),
                    help='meta.json / latest_tjpr_full.jsonl を読み data/series.json を書く場所 (既定 site/)')
    args = ap.parse_args(argv)
    build_dir = Path(args.build_dir)
    out_dir = build_dir / "data"
    out_path = out_dir / "series.json"
    named_uids = _load_named_uids(build_dir / "latest_tjpr_full.jsonl")
    print(f"loaded named uids: {len(named_uids)}")
    min_ent_thresh = _load_min_entrants_threshold(build_dir / "meta.json")
    print(f"min_entrants_for_ranking threshold = {min_ent_thresh}")
    # series → {(date, tname): {entrants, uids_set, ts}}
    series_inst: dict[str, dict[tuple[str, str], dict]] = defaultdict(dict)
    series_flags: dict[str, dict] = {}   # コミュニティ名 → 名前の印 (region_rules.json。印はコミュニティ名に当てたもの)
    scanned = 0
    used = 0

    for attr_path in EVENTS_ROOT.rglob("attr.json"):
        scanned += 1
        try:
            with open(attr_path, "rb") as f:
                d = json.loads(f.read())
        except Exception as e:
            skipped('attr.json が読めない', f'{attr_path}: {e}')
            continue
        if (d.get("region") or "").replace(" ", "_") != REGION:   # attr は "North America"、REGION は "North_America"
            continue
        # 地域内 (country_code が region_rules の country_codes。日本は JP だけ) 限定. 海外開催で region 誤ラベル除外.
        # 2026-10-17: "JP" 直書きで北米のシリーズが 0 件だった (build_tournaments_index と同じ集合を使う)
        place = d.get("place") or {}
        cc = place.get("country_code")
        if cc and cc not in REGION_COUNTRY_CODES:
            continue
        ts = d.get("timestamp")
        if ts is None:
            continue
        tname = d.get("tournament_name") or ""
        if not tname:
            continue
        # 中止/延期/cancel 大会は除外 (= 実際には開催されていない).
        if derived.tournament_name_labels(tname)["cancelled"]:
            continue
        # テスト/検証ページは除外.
        if derived.tournament_name_labels(tname)["test_page"]:
            continue
        ts = float(ts)
        dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
        date_str = dt.strftime("%Y-%m-%d")

        # 参加者集合: standings.json から取得
        standings_path = attr_path.parent / "standings.json"
        try:
            with open(standings_path, "rb") as f:
                stand_data = json.loads(f.read())
        except Exception as e:
            skipped('standings.json が読めない', f'{standings_path}: {e}')
            continue
        # standings.json は 2 形式存在: {"data": [...]} もしくは list 直接.
        rows = stand_data.get("data", []) if isinstance(stand_data, dict) else stand_data
        if not isinstance(rows, list):
            rows = []
        uids = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            uid = row.get("user_id")
            if uid is None:
                continue
            try:
                uids.add(int(uid))
            except (TypeError, ValueError):
                continue
        if not uids:
            continue

        # サブイベント名 (event_name) も series 判定に使う:
        # スマパ系統で event_name が「カジュアル」を含む場合は スマパカジュアル に振り分ける.
        # (tournament_name は同じ "Weekly Smash Party #N" でも sub-event 単位で別 series 扱いにする).
        # (この分け方は smash_database の naming.event_series が決め、derived.json の naming.series_event に入る)
        strict = derived.load(attr_path.parent)["naming"]["series_event"]
        _sr = derived.series_rules(strict)
        series = _sr["community"]
        series_flags[series] = _sr
        # instance key に series 名 (= sub-event 区別) を入れて、同一 tournament_name 内でも
        # スマパ vs スマパカジュアル を別 instance として保持する.
        key = (date_str, tname, series)
        inst = series_inst[series].get(key)
        if inst is None:
            inst = {
                "ts": ts,
                "entrants_max": int(d.get("num_entrants") or 0),
                "uids": set(uids),
            }
            series_inst[series][key] = inst
        else:
            # 同一大会の別サブイベント: uid union, entrants は max を取る
            inst["uids"] |= uids
            ne = int(d.get("num_entrants") or 0)
            if ne > inst["entrants_max"]:
                inst["entrants_max"] = ne
        used += 1

    # シリーズ毎に集約
    out_series = []
    skipped_low = 0
    for series, instances in series_inst.items():
        insts = list(instances.values())
        # 有効プレイヤー数が ≤1 の大会しかないシリーズは除外 (= 開催されたことが疑わしい).
        # 名前が取れない uid (= master JSONL に未登録) は数えない.
        # named_uids が空のとき (= JSONL 未生成) はフィルタしない.
        if named_uids:
            max_uids = max(
                (len(i["uids"] & named_uids) for i in insts),
                default=0,
            )
        else:
            max_uids = max((len(i["uids"]) for i in insts), default=0)
        if max_uids <= 1:
            skipped_low += 1
            continue
        # tour-instance level meta
        ts_list = [i["ts"] for i in insts]
        ts_list.sort()
        entrants_list = [i["entrants_max"] for i in insts if i["entrants_max"] > 0]
        first_ts = ts_list[0]
        last_ts = ts_list[-1]

        # participants 集計 (uid → {n, first_ts, last_ts})
        part: dict[int, dict] = {}
        for inst in insts:
            for uid in inst["uids"]:
                p = part.get(uid)
                if p is None:
                    part[uid] = {"n": 1, "first_ts": inst["ts"], "last_ts": inst["ts"]}
                else:
                    p["n"] += 1
                    if inst["ts"] < p["first_ts"]:
                        p["first_ts"] = inst["ts"]
                    if inst["ts"] > p["last_ts"]:
                        p["last_ts"] = inst["ts"]
        # シリーズ全体のフラグ判定: シリーズ名そのものが NON_SERIOUS / RESTRICTED にマッチするかで
        # 判断 (= tournament_name のサブタイトル副イベント "doubles" 等で誤フラグしないため).
        any_non_serious = series_flags[series]["non_serious"]
        any_uchi = series_flags[series]["uchi"]
        any_special_rules = series_flags[series]["special_rules"]
        any_restricted = series_flags[series]["restricted"]

        participants = []
        for uid in sorted(part.keys()):
            p = part[uid]
            participants.append({
                "uid": uid,
                "n": p["n"],
                "first": datetime.datetime.fromtimestamp(
                    p["first_ts"], tz=datetime.timezone.utc).strftime("%Y-%m-%d"),
                "last": datetime.datetime.fromtimestamp(
                    p["last_ts"], tz=datetime.timezone.utc).strftime("%Y-%m-%d"),
            })

        n_small = sum(1 for i in insts if i["entrants_max"] < min_ent_thresh)
        is_all_small = n_small == len(insts) and len(insts) > 0
        out_series.append({
            "name": series,
            "series_id": series_slug(series),
            "first_date": datetime.datetime.fromtimestamp(
                first_ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d"),
            "last_date": datetime.datetime.fromtimestamp(
                last_ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d"),
            "tour_count": len(insts),
            "avg_entrants": round(sum(entrants_list) / len(entrants_list), 1) if entrants_list else 0.0,
            "max_entrants": max(entrants_list) if entrants_list else 0,
            "participant_count": len(participants),
            "non_serious": any_non_serious,
            "is_uchi": any_uchi,
            "is_special_rules": any_special_rules,
            "restricted": any_restricted,
            "n_small": n_small,
            "is_all_small": is_all_small,
            "participants": participants,
        })

    # 新しい順 (last_date desc)
    out_series.sort(key=lambda s: (s["last_date"], s["name"]), reverse=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "series": out_series,
    }
    if scanned == 0:   # 入力ディレクトリが無い/空 (mount 外れ・パス違い) を「成功して空 JSON」にしない
        raise SystemExit(f"ERROR: {EVENTS_ROOT} に attr.json が 1 件も無い — 出力しない")
    write_json_atomic(out_path, payload, ensure_ascii=False, separators=(",", ":"))

    report_skips()
    print(f"scanned attr.json: {scanned}")
    print(f"used (with standings + non-empty uids): {used}")
    print(f"unique series: {len(out_series)}  (skipped low-quality: {skipped_low})")
    print(f"total tour instances (deduped): {sum(s['tour_count'] for s in out_series)}")
    print(f"wrote: {out_path}")
    sz = out_path.stat().st_size
    print(f"size: {sz/1024/1024:.2f} MB")


if __name__ == "__main__":
    main()

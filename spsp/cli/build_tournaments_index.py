#!/usr/bin/env python3
"""大会一覧ページ用のインデックス JSON を生成.

attr.json + standings.json を rglob して、各大会の最小メタ情報を 1 ファイルにまとめる.
出力: <build-dir>/data/tournaments.json (--build-dir 既定 = site/)
入力: <build-dir>/{meta.json, latest_tjpr_full.jsonl, tournaments/} と events 配下の attr.json
"""
from __future__ import annotations
import sys, json, datetime, re

import orjson
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # リポジトリ直下 (spsp パッケージ)
from spsp.paths import SITE_DIR, EVENTS_ROOT  # noqa: E402
from spsp.tournament_utils import load_min_entrants_threshold as _load_min_entrants_threshold  # noqa: E402
from spsp.skiplog import skipped, report_skips  # noqa: E402
from spsp.common import FORCE_WEEKEND_MIN_NENT, SMACOMI_FORCE_WEEKDAY_MAX_NENT, flag_tags, series_slug  # noqa: E402
from spsp import derived  # noqa: E402
from spsp.meta import tournament_series  # type: ignore  # noqa: E402
from spsp.fileio import write_json_atomic  # noqa: E402
from spsp.paths import REGION  # noqa: E402
# 地域の国コード・中止/テストの判定は smash_database (scripts/<地域>/) が定義元。derive.py が region_rules.json に書いたものを読む
REGION_COUNTRY_CODES = frozenset(derived.region_rules()["country_codes"])


def _read_standings(attr_path: Path) -> list[dict]:
    """standings.json を読んで [{place, uid}] のリストを返す (place昇順)."""
    sp = attr_path.parent / "standings.json"
    try:
        with open(sp, "rb") as f:
            sd = json.loads(f.read())
    except Exception as e:
        skipped('standings.json が読めない', f'{sp}: {e}')
        return []
    rows = sd.get("data", []) if isinstance(sd, dict) else sd
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict): continue
        uid = r.get("user_id")
        if uid is None: continue
        try:
            uid_i = int(uid)
        except (TypeError, ValueError):
            continue
        place = r.get("placement")
        try:
            place_i = int(place) if place is not None else None
        except (TypeError, ValueError):
            place_i = None
        out.append({"uid": uid_i, "place": place_i})
    out.sort(key=lambda x: (x["place"] if x["place"] is not None else 10**9))
    return out


def _load_master(jsonl_path: Path) -> dict:
    """latest_tjpr_full.jsonl から uid → {display, rank_ensemble} を作る."""
    out = {}
    if not jsonl_path.exists():
        return out
    with open(jsonl_path, "rb") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                rec = json.loads(line)
            except Exception as e:
                skipped('latest_tjpr_full.jsonl の行が読めない', f'{jsonl_path}: {e}')
                continue
            uid = rec.get("user_id")
            if uid is None: continue
            try:
                uid_i = int(uid)
            except Exception:
                skipped('latest_tjpr_full.jsonl の user_id が整数でない', f'{uid!r}')
                continue
            out[uid_i] = {
                "display": rec.get("display") or "",
                "rank": (rec.get("ranks") or {}).get("ensemble"),
            }
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--build-dir', default=str(SITE_DIR),
                    help='meta.json / latest_tjpr_full.jsonl / tournaments/ を読み data/tournaments.json を書く場所 (既定 site/)')
    args = ap.parse_args(argv)
    build_dir = Path(args.build_dir)
    tournaments_dir = build_dir / "tournaments"   # 個別 detail JSON (has_detail 判定用。build dir の当夜分を見る)
    out_dir = build_dir / "data"
    out_path = out_dir / "tournaments.json"
    # 大会詳細 JSON が存在する event_id 集合を読む (= クリック可能判定用).
    detail_ids: set[int] = set()
    gf_missing_ids: set[int] = set()
    awaiting_ids: set[int] = set()   # 再開待ち (途中結果で暫定集計中)
    random_seed_ids: set[int] = set()   # ランダムシード (build の spsp/random_seed.py が判定)
    if tournaments_dir.exists():
        for p in tournaments_dir.glob("*.json"):
            try:
                eid_p = int(p.stem)
            except ValueError:
                continue
            detail_ids.add(eid_p)
            # 未完了大会 (= 決勝未報告 → is_gf_missing) の判定は DE phase / 優勝者有無 /
            # nent など複合条件で、build 側の per-tournament JSON に正確な値が既にある。
            # ここで再現するとズレるので、その JSON を読んで拾う。
            # (以前は '"is_gf_missing": true' の文字列検索だったが、書き出しを orjson にして区切りの
            #  スペースが無くなり全件 False になった (2026-09-07、golden の内容比較で検出)。書式に依存しないよう parse する。
            #  orjson なら 4,228 ファイル 400 MB で約 1 秒。)
            try:
                _doc = orjson.loads(p.read_bytes())
                if _doc.get("is_gf_missing") is True:
                    gf_missing_ids.add(eid_p)
                if _doc.get("is_awaiting_resume") is True:
                    awaiting_ids.add(eid_p)
                if _doc.get("is_random_seed") is True:
                    random_seed_ids.add(eid_p)
            except Exception as e:
                skipped('大会 detail JSON が読めない (is_gf_missing 判定)', f'{p}: {e}')
                pass
    # ランキング対象しきい値 (これ未満は is_small=True. ランキング計算には寄与しないが一覧には載せる).
    min_ent_thresh = _load_min_entrants_threshold(build_dir / "meta.json")
    print(f"min_entrants_for_ranking threshold = {min_ent_thresh}")
    # master JSONL: uid → display, ensemble rank.
    master = _load_master(build_dir / "latest_tjpr_full.jsonl")
    print(f"loaded master: {len(master)} players")
    # 1 イベント = 1 エントリ (2026-07-22〜)。以前は (date, tname) で max-nent に集約して
    # いたため、制限/無制限の同時開催 (第2.5回スマフェス) やクラスブラケットが検索に
    # 出なかった。dedup は event_id のみ (同一 eid の重複 dir は max nent 採用)。
    def _tournament_dir(event_path: Path) -> str:
        # v4.meta.build_tournament_metadata と同一規則 (同時開催の制限巻き込まれ解除用)
        p = event_path
        if p.parent.name == "class_phases":
            p = p.parent.parent
        elif p.name.endswith("_virtual"):
            p = p.parent
        return str(p.parent)

    raw_items: list[dict] = []
    ename_restricted_tdirs: set[str] = set()
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
        place = d.get("place") or {}
        cc = place.get("country_code")
        if cc and cc not in REGION_COUNTRY_CODES:   # 地域外の会場 (定義は scripts/<地域>/country.py)
            continue
        ts = d.get("timestamp")
        if ts is None:
            continue
        tname = d.get("tournament_name") or ""
        if not tname:
            continue
        if derived.tournament_name_labels(tname)["cancelled"]:
            continue
        if derived.tournament_name_labels(tname)["test_page"]:
            continue
        event_id = d.get("event_id")
        if event_id is None:
            continue
        # 仮想クラスブラケット (event_id < 0) も独立エントリとして含める
        # (= 下位クラスも検索対象。詳細ページは無いので has_detail=False のまま非リンク表示)。
        try:
            eid = int(event_id)
        except Exception:
            skipped('event_id が整数でない', f'{attr_path}: {event_id!r}')
            continue
        is_class_virtual = eid < 0
        nent = int(d.get("num_entrants") or 0)
        if nent <= 0:
            continue
        standings = _read_standings(attr_path)
        if not standings:
            continue
        dt_utc = datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc)
        # JST (UTC+9) で日付を出す
        dt_jst = dt_utc.astimezone(datetime.timezone(datetime.timedelta(hours=9)))
        date_str = dt_jst.strftime("%Y-%m-%d")
        ename = d.get("event_name") or ""
        series = tournament_series(tname)
        _nm = derived.names(attr_path.parent)          # 名前の判定は smash_database 側 (derived.json)
        is_pre = _nm['pre']
        is_force_weekday = _nm['force_weekday'] or (_nm['smacomi'] and nent < SMACOMI_FORCE_WEEKDAY_MAX_NENT)
        ename_restricted = _nm['restricted_ename']
        is_restricted = _nm['restricted_tname'] or ename_restricted
        if ename_restricted:
            ename_restricted_tdirs.add(_tournament_dir(attr_path.parent))
        is_lower_class = _nm['lower_class'] or is_class_virtual
        is_uchi = _nm['uchi']
        is_special_rules = _nm['special_rules']
        _cal = derived.calendar(attr_path.parent)
        is_weekend_real = _cal['is_weekend_real']
        is_force_weekend = (not is_weekend_real and nent >= FORCE_WEEKEND_MIN_NENT
                            and _cal['is_force_weekend_period'])
        # 本体ビルド (v4/meta.py build_tournament_meta) と同じ規約:
        # is_weekend = 計算上の扱い (プレ大会 / FORCE_WEEKDAY シリーズは休日開催でも平日扱い,
        #   お盆/年末年始の nent>=80 大会 = 実質休日は平日開催でも休日扱い),
        # is_weekend_real = 実暦の土日祝. 両者が食い違う大会をフロントで「実質平日/実質休日」表示する.
        is_weekend = (is_weekend_real or is_force_weekend) and not is_pre and not is_force_weekday
        # 開催地: city / venue_name (空文字なら省略)
        city = place.get("city") or ""
        venue_name = place.get("venue_name") or ""
        # top 3 finishers (place 1-3) with display + global rank
        top_players = []
        for s in standings:
            if s["place"] is None or s["place"] > 3: continue
            info = master.get(s["uid"]) or {}
            top_players.append({
                "uid": s["uid"],
                "display": info.get("display") or "",
                "place": s["place"],
                "rank": info.get("rank"),
            })
            if len(top_players) >= 3: break
        raw_items.append({
            "entry": {
                "event_id": eid,
                "tournament_name": tname,
                "event_name": ename,
                "date": date_str,
                "nent": nent,
                "series": series,
                "series_id": series_slug(series),
                "city": city,
                "venue": venue_name,
                "is_weekend": bool(is_weekend),
                "is_weekend_real": bool(is_weekend_real),
                "is_pre": bool(is_pre),
                "is_restricted": bool(is_restricted),
                "is_lower_class": bool(is_lower_class),
                "is_uchi": bool(is_uchi),
                "is_special_rules": bool(is_special_rules),
                "is_small": nent < min_ent_thresh,
                "is_gf_missing": eid in gf_missing_ids,
                "is_awaiting_resume": eid in awaiting_ids,
                "is_random_seed": eid in random_seed_ids,
                "tags": None,   # 下で is_restricted の巻き込まれ解除後に確定
                "has_detail": eid in detail_ids,
                "top_players": top_players,
            },
            "ename_restricted": ename_restricted,
            "tdir": _tournament_dir(attr_path.parent),
        })
        used += 1

    # 同時開催の制限巻き込まれ解除 (v4.meta.build_tournament_metadata と同一規則):
    # 同一大会 dir 内に ename 制限イベントがあるなら、ename 非マッチのイベントは無制限。
    by_eid: dict[int, dict] = {}
    for it in raw_items:
        e = it["entry"]
        if (e["is_restricted"] and not it["ename_restricted"]
                and it["tdir"] in ename_restricted_tdirs):
            e["is_restricted"] = False
        e["tags"] = flag_tags(e)   # 地域非依存の機械可読タグ (True な is_* の名前、固定順)
        prev = by_eid.get(e["event_id"])
        if prev is None or e["nent"] > prev["nent"]:
            by_eid[e["event_id"]] = e

    out = list(by_eid.values())
    # 新しい順 (date desc)、同一大会内は nent 降順 (main event が上)
    out.sort(key=lambda e: (e["date"], e["tournament_name"], e["nent"], e["event_id"]),
             reverse=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tournaments": out,
    }
    if scanned == 0:   # 入力ディレクトリが無い/空 (mount 外れ・パス違い) を「成功して空 JSON」にしない
        raise SystemExit(f"ERROR: {EVENTS_ROOT} に attr.json が 1 件も無い — 出力しない")
    write_json_atomic(out_path, payload, ensure_ascii=False, separators=(",", ":"))

    report_skips()
    print(f"scanned attr.json: {scanned}")
    print(f"used: {used}")
    print(f"wrote: {out_path}")
    sz = out_path.stat().st_size
    print(f"size: {sz/1024/1024:.2f} MB")


if __name__ == "__main__":
    main()

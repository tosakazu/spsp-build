#!/usr/bin/env python3
"""再開待ち大会の登録簿 (smash_database data-Japan の data/startgg/Japan/manual/awaiting_resume.json) の表示・掃除。

  python3 build/awaiting_resume.py --list           # 登録一覧と、各 event が今も再開待ちか (優勝者未確定か)
  python3 build/awaiting_resume.py --prune          # 優勝者が確定した event を登録簿から消す (履歴は git にある)
  python3 build/awaiting_resume.py --add 1672939 "IMPACT MAJOR#5 天候延期"   # 追加

優勝者の有無は大会データ (smash_db_tournament/data/startgg/Japan/events/**/<event>/standings.json) から見る。
登録簿は追跡対象なので、--prune / --add のあとは git commit する。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # リポジトリ直下 (spsp パッケージ)
from spsp.paths import EVENTS_ROOT  # noqa: E402
from spsp.tournament_utils import awaiting_resume_path  # noqa: E402


def _event_dir_index() -> dict[int, Path]:
    idx: dict[int, Path] = {}
    for attr in EVENTS_ROOT.glob("*/*/*/*/*/attr.json"):   # events/<年>/<月>/<日>/<大会>/<イベント>
        try:
            eid = json.loads(attr.read_bytes()).get("event_id")
        except Exception:
            continue
        if isinstance(eid, int):
            idx[eid] = attr.parent
    return idx


def has_champion(event_dir: Path | None) -> bool | None:
    """True/False。データが無ければ None。"""
    if event_dir is None:
        return None
    try:
        rows = json.loads((event_dir / "standings.json").read_bytes()).get("data") or []
    except Exception:
        return None
    places = [r.get("placement") for r in rows if isinstance(r.get("placement"), int)]
    return bool(places) and min(places) == 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--add", nargs=2, metavar=("EVENT_ID", "NOTE"))
    args = ap.parse_args()
    path = awaiting_resume_path()
    data = json.loads(path.read_bytes()) if path.exists() else {"events": []}
    events = data.setdefault("events", [])

    if args.add:
        eid = int(args.add[0])
        if any(e.get("event_id") == eid for e in events):
            print(f"既に登録済み: {eid}")
        else:
            events.append({"event_id": eid, "added": dt.date.today().isoformat(), "note": args.add[1]})
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            print(f"追加: {eid} → {path} (git commit を忘れずに)")
        return 0

    idx = _event_dir_index()
    rows = []
    for e in events:
        eid = e.get("event_id")
        ch = has_champion(idx.get(eid))
        state = "再開待ち (優勝者なし)" if ch is False else ("完了 (優勝者確定 → 消してよい)" if ch else "データなし")
        rows.append((eid, e.get("name") or "", e.get("added", ""), state, ch))
        print(f"  {eid}  {state:26s}  {e.get('name') or ''}  ({e.get('added', '')}) {e.get('note', '')}")
    if not rows:
        print("  (登録なし)")
    if args.prune:
        keep = [e for e, r in zip(events, rows) if r[4] is not True]
        n = len(events) - len(keep)
        data["events"] = keep
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        print(f"--prune: {n} 件を削除 → {path} (git commit を忘れずに)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

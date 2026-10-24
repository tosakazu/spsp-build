"""下位クラスの試合 (class_matches.json) を本戦の試合として読むことのテスト (spsp/data_loader.py).

実行: OPENBLAS_NUM_THREADS=1 .venv/bin/python3 tests/meta/test_class_matches_loader.py
(リポジトリルートから。unittest 等は使わず plain assert)

背景 (2026-10-01): SPSP のページから Challonge で開いた下位クラスは、smash_database が
class_phases/<クラス>_virtual/ に start.gg の下位クラスと同じ形で置き、試合は class_matches.json に書く
(start.gg の下位クラスの試合は本戦の matches.json にあるが、Challonge の試合は無いため)。
ビルドはそれを本戦の試合 (is_class=True) として読み、start.gg の下位クラスとまったく同じに学習する。
仮想大会側の matches.json は空のまま (二重に数えない)。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from spsp.data_loader import iter_tournaments  # noqa: E402


def row(mid, w, l, **kw):
    r = {"match_id": mid, "winner_id": w, "loser_id": l, "winner_score": 2, "loser_score": 1, "state": 3,
         "dq": False, "cancel": False, "phase_group_id": None, "round": 1, "round_text": None,
         "global_bracket_label": None, "global_top_x": None, "phase_bracket_type": "DOUBLE_ELIMINATION"}
    r.update(kw)
    return r


def write(d: Path, name, obj):
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def derived(is_virtual=False, class_pg=()):
    return {"is_1on1": True, "is_offline": True, "is_class_virtual": is_virtual,
            "class_bracket": {"phase_group_ids": list(class_pg)}}


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "events"
    # 本戦 A: start.gg の下位クラス (試合は本戦の matches.json、phase_group 900 が下位クラス) — 今までどおり
    a = root / "2026/08/01/A/Singles"
    write(a, "attr.json", {"event_id": 100, "timestamp": 1000})
    write(a, "standings.json", [{"placement": i, "user_id": u} for i, u in enumerate([1, 2, 3, 4], 1)])
    write(a, "matches.json", {"data": [row(1, 1, 2, phase_group_id=800), row(2, 3, 4, phase_group_id=900)]})
    write(a, "derived.json", derived(class_pg=[900]))
    av = a / "class_phases/B_virtual"
    write(av, "attr.json", {"event_id": -1001, "timestamp": 1001})
    write(av, "standings.json", [{"placement": 1, "user_id": 3}, {"placement": 2, "user_id": 4}])
    write(av, "matches.json", [])
    write(av, "derived.json", derived(is_virtual=True))
    # 本戦 B: Challonge の下位クラス (試合は B_virtual/class_matches.json)
    b = root / "2026/08/02/B/Singles"
    write(b, "attr.json", {"event_id": 200, "timestamp": 2000})
    write(b, "standings.json", [{"placement": i, "user_id": u} for i, u in enumerate([11, 12, 13, 14], 1)])
    write(b, "matches.json", {"data": [row(10, 11, 12)]})
    write(b, "derived.json", derived())
    bv = b / "class_phases/B_virtual"
    write(bv, "attr.json", {"event_id": -2001, "timestamp": 2001})
    write(bv, "standings.json", [{"placement": 1, "user_id": 13}, {"placement": 2, "user_id": 14}])
    write(bv, "matches.json", [])
    write(bv, "derived.json", derived(is_virtual=True))
    write(bv, "class_matches.json", {"replaces_phase_group_ids": [], "data": [
        row(20, 13, 14, global_bracket_label="B-Winners TOP 2", global_top_x=2, source="challonge"),
        row(21, 14, 13, dq=True),                   # DQ は学習せず、負けた側 (13) を DQ として記録 (本戦の matches.json と同じ)
        row(22, 13, None),                          # 相手が start.gg の選手でない行は読まない
        row(23, 13, 14, state=2),                   # 未完了は読まない
    ]})
    # 本戦 C: start.gg にも同じ B クラス (phase_group 950) があったが、進行した Challonge を採った
    # → replaces_phase_group_ids の試合 (と DQ 記録) は読まず、Challonge の試合だけを学習する (二重に数えない)
    c = root / "2026/08/03/C/Singles"
    write(c, "attr.json", {"event_id": 300, "timestamp": 3000})
    write(c, "standings.json", [{"placement": i, "user_id": u} for i, u in enumerate([21, 22, 23, 24], 1)])
    write(c, "matches.json", {"data": [row(30, 21, 22, phase_group_id=850), row(31, 23, 24, phase_group_id=950),
                                       row(32, 24, 23, phase_group_id=950, dq=True)]})
    write(c, "derived.json", derived(class_pg=[950]))
    cv = c / "class_phases/B_virtual"
    write(cv, "attr.json", {"event_id": -3001, "timestamp": 3001})
    write(cv, "standings.json", [{"placement": 1, "user_id": 24}, {"placement": 2, "user_id": 23}])
    write(cv, "matches.json", [])
    write(cv, "derived.json", derived(is_virtual=True))
    write(cv, "class_matches.json", {"replaces_phase_group_ids": [950], "data": [row(40, 24, 23)]})
    # 古い形 (行の list のまま) も読める
    d = root / "2026/08/04/D/Singles"
    write(d, "attr.json", {"event_id": 400, "timestamp": 4000})
    write(d, "standings.json", [{"placement": 1, "user_id": 31}, {"placement": 2, "user_id": 32}])
    write(d, "matches.json", {"data": []})
    write(d, "derived.json", derived())
    write(d / "class_phases/B_virtual", "class_matches.json", [row(50, 31, 32)])

    ts = {t.event_id: t for t in iter_tournaments(root)}
    # start.gg の下位クラス: 今までどおり (本戦に 2 試合、うち 1 つが is_class。仮想大会は試合なし)
    assert [(m.winner_id, m.is_class) for m in ts[100].matches] == [(1, False), (3, True)], ts[100].matches
    assert ts[-1001].matches == []
    # Challonge の下位クラス: 本戦の試合に is_class で 1 つ足される。仮想大会は試合なし (二重に数えない)
    got = [(m.winner_id, m.loser_id, m.is_class, m.global_bracket_label) for m in ts[200].matches]
    assert got == [(11, 12, False, None), (13, 14, True, "B-Winners TOP 2")], got
    assert ts[200].matches[1].event_path == str(b)      # 本戦の試合として扱う
    assert ts[-2001].matches == []
    # 下位クラスの DQ (row 21: 14 が 13 に DQ 負け) は学習せず、start.gg の下位クラスの DQ と同じく記録する
    assert ts[200].dq_player_ids_class == {13}, ts[200].dq_player_ids_class
    assert 13 in ts[200].dq_player_ids and ts[200].dq_player_ids_main == set()
    assert [s[1] for s in ts[-2001].standings] == [13, 14]
    got = [(m.winner_id, m.loser_id, m.is_class) for m in ts[300].matches]
    assert got == [(21, 22, False), (24, 23, True)], got
    assert ts[300].dq_player_ids == set(), ts[300].dq_player_ids     # 置き換えた start.gg クラスの DQ も読まない
    assert [(m.winner_id, m.is_class) for m in ts[400].matches] == [(31, True)]

print("OK: tests/meta/test_class_matches_loader.py")

"""「読めなかったので飛ばした」を数えて最後にまとめて出す。

以前は except → continue / return None で黙って飛ばしていたので、データの一部が壊れていても
ログに何も出ず、順位表が静かに変わる余地があった。出力は変えず、件数と例だけを出す。

使い方:
    from spsp.skiplog import skipped, report_skips
    except Exception as e:
        skipped('attr.json が読めない', f'{path}: {e}')
        continue
    ...
    report_skips()   # 実行の最後に 1 回 (何も無ければ何も出さない)
"""
from __future__ import annotations

from collections import Counter

_COUNTS: Counter = Counter()
_FIRST: dict[str, str] = {}


def skipped(tag: str, detail: str | None = None) -> None:
    _COUNTS[tag] += 1
    if detail is not None and tag not in _FIRST:
        _FIRST[tag] = str(detail)[:200]


def counts() -> dict[str, int]:
    return dict(_COUNTS)


def reset() -> None:
    _COUNTS.clear()
    _FIRST.clear()


def report_skips(prefix: str = "  [skipped]") -> None:
    """件数のある項目だけを 1 行ずつ出す (最初の例つき)。"""
    for tag, n in sorted(_COUNTS.items()):
        ex = f"  例: {_FIRST[tag]}" if tag in _FIRST else ""
        print(f"{prefix} {tag}: {n} 件{ex}", flush=True)

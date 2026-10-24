"""テスト・実験専用: smash_database の地域の規則モジュール (scripts/<地域>/<name>.py) を直接読む。

本番のビルドは地域のコードを読まず、derive.py が書いた region_rules.json だけを読む (spsp/derived.py、セキュリティ
レビュー 2026-10-08 H1)。ここは規則そのものをテストしたい場面 (日本の規則の検証) のためだけに残す口。
"""
from __future__ import annotations

import functools
import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from spsp.paths import TOURNAMENT_DB  # noqa: E402


@functools.lru_cache(maxsize=None)
def region_module(name: str, region: str = "Japan"):
    root = TOURNAMENT_DB.parent.parent          # smash_db_tournament/
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return importlib.import_module(f"scripts.{region.replace(' ', '_')}.{name}")


def region_classify(region: str = "Japan"):
    return region_module("classify", region)

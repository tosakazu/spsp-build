# -*- coding: utf-8 -*-
"""国データ (プレイヤーページの居住地表示用).

country は smash_db_tournament 側の users.jsonl にのみ入っている
(smash_db_user 側の users.jsonl には country フィールドが無い) ため、
表示名ソースとは別にここで uid -> country を読む。

国名の英日表 (COUNTRY_JA) と海外判定は 2026-09-09 に smash_database
(scripts/Japan/country.py) へ移した。ここは users.jsonl を読む口と、その表を借りる薄い層。
"""
from __future__ import annotations

import json
from pathlib import Path

from spsp.paths import USERS_JSONL as _USERS_JSONL

def country_ja(name: str | None) -> str | None:
    """英語国名 → 日本語国名 (表の定義元は smash_database の scripts/Japan/country.py。derive.py が users.jsonl の全ての
    国名について評価して region_rules.json に書いたものを引く。ビルドは地域のコードを読まない)。"""
    if not name:
        return None
    from spsp.derived import region_rules
    try:
        return region_rules()["countries"][name]["ja"]
    except KeyError:
        raise SystemExit(f"ERROR: 国名 {name!r} が region_rules.json に無い — smash_database の derive.py を走らせ直す")


_country_map: dict[int, str] | None = None


def load_country_map() -> dict[int, str]:
    """uid -> country (英語) を lazy load してキャッシュ."""
    global _country_map
    if _country_map is None:
        m: dict[int, str] = {}
        try:
            with open(_USERS_JSONL, encoding='utf-8') as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    uid = d.get('user_id')
                    country = d.get('country')
                    if uid is not None and country:
                        m[int(uid)] = country
        except Exception as e:
            print(f"[WARN] country users.jsonl read failed: {e}", flush=True)
        _country_map = m
    return _country_map

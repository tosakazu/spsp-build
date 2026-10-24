"""リポジトリ内のパス定義 (唯一の置き場所)。

以前は v4/common.py, v4/tournament_utils.py (旧 eval/), v4/country_ja.py, build/build_*.py の
それぞれが REPO / site / data/startgg のパスを自前で組み立てていた。ここに集約し、他は import する。
副作用なし (ファイルを読まない) なので、どこからでも安全に import できる。
"""
from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]   # spsp/paths.py → リポジトリ直下

# ビルドする地域 (= smash_database の data/startgg/<地域>/ と scripts/<地域>/)。既定は Japan。
# 北米版は SPSP_REGION=North_America。地域ごとに違う判定・表は全部 smash_database 側 (2026-09-15〜)。
REGION = os.environ.get("SPSP_REGION", "Japan").replace(" ", "_")

# 配信ディレクトリ (spsp.games はここを直接配信、gh-pages は BUILD_DIR から rsync)
SITE_DIR = REPO / "site"
SITE_DATA = SITE_DIR / "data"                # フロントが読む補助 JSON と生成物 (gitignore)
CHARACTERS_JSON = REPO / "spsp" / "characters.json"   # キャラ一覧 (id → 表示名)。絵文字はフロント (spsp-web) が持つ (2026-09-29)
BUILD_INPUT_DIR = REPO / "build" / "data"    # ビルド入力 (char_votes.json, 市区町村 CSV, 凍結順序表 等)
LEGACY_ORDER_PATH = BUILD_INPUT_DIR / "legacy_tournament_order.json"   # 同一 timestamp の大会順の凍結表 (data_loader)
CHAR_VOTES_PATH = BUILD_INPUT_DIR / "char_votes.json"                   # キャラ投票の取り込み結果 (fetch_char_votes → build_full)
TARGETS_DIR = REPO / "experiments" / "harness" / "targets"   # 実験ハーネスのシーズン区切り (learner.main が読む)

# 大会データベース (入れ子リポジトリ smash_db_tournament の生成物)
TOURNAMENT_DB = REPO / "smash_db_tournament" / "data" / "startgg"
REGION_DB = TOURNAMENT_DB / REGION           # 地域ごとに 1 ディレクトリ: index (done.csv, users.jsonl, ...) と events/ が同居 (2026-09-08〜)
EVENTS_ROOT = REGION_DB / "events"           # <地域>/events/Y/M/D/<大会>/<イベント>/{attr,standings,matches,seeds}.json
GEO_JSON = REGION_DB / "geo.json"            # 地理単位のカタログ (derive.py が scripts/<地域>/geo.py から書く)。ビルドは site/data/geo.json に写す


def region_events_root(region: str):
    """他地域のイベント root (data/startgg/<地域>/events)。ビルドは今は Japan だけ読む"""
    return TOURNAMENT_DB / region.replace(" ", "_") / "events"
MANUAL_DIR = REGION_DB / "manual"   # 手動管理の表 (user_merges.json / overseas_manual.json / awaiting_resume.json)。smash_database 管理 (2026-09-08〜)
USERS_DERIVED = REGION_DB / "users_derived.jsonl"   # derive.py の生成物 (居住地 → 地理単位 geo。日本は都道府県)
USERS_JSONL = REGION_DB / "users.jsonl"  # country / city / discriminator を持つ (nightly 更新)。2026-09-07 から地域配下 (data/startgg/<地域>/)
# 旧ユーザーデータベース (smash_db_user)。discriminator の補完にだけ使う
USERS_JSONL_USERDB = REPO / "smash_db_user" / "data" / "startgg" / "users.jsonl"

# BUILD_DIR (= build_v4_full.py の出力先) の中の生成物。[4/5] の補助 JSON もここに書き、最後に site/ へ反映する
BUILD_META = "meta.json"
BUILD_MASTER_JSONL = "latest_tjpr_full.jsonl"
BUILD_PLAYERS = "players"
BUILD_TOURNAMENTS = "tournaments"
BUILD_DATA = "data"

# [4/5] が BUILD_DIR/data に生成し、[4.5/5] で site/data に反映する補助 JSON
AUX_DATA_FILES = (
    "tournaments.json", "series.json", "geo.json", "upcoming.json", "player_prefectures.json", "tournament_prefectures.json",
    "player_subranks.json", "discriminators.json", "last_activity.json", "user_merges.json",
    "character_index.json",  # build_v4_full の overlay が生成
)


def rel_event_path(p: str) -> str:
    """大会ディレクトリをデータルート (.../events/) からの相対パスに。状態・台帳のキーはこれで持つ
    (絶対パスだとデータの置き場所を変えただけでチェックポイントが使えなくなる)。"""
    p = str(p)
    if '/events/' not in p:
        return p
    head, rest = p.split('/events/', 1)
    region = head.rsplit('/', 1)[-1]
    if region in ('', 'startgg'):
        return rest                    # 旧配置 .../startgg/events/<地域>/<年>/... (凍結入力・fixture はこの形)
    return f"{region}/{rest}"          # 新配置 .../startgg/<地域>/events/<年>/... → キーは旧配置と同じ "<地域>/<年>/..."

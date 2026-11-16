"""start.gg のアカウントが無い参加者 (主催者が名前だけで足した枠) との試合 (2026-11-16)。

matches.json では、その参加者の側の winner_id / loser_id が null で入っている。以前は試合ごと読み飛ばしていたので、
相手の選手のページに試合が出ず、勝敗数も直対評価も動かなかった。

ここでは、その参加者を「その試合だけの選手」として読む (uid = -match_id、負の数)。大会の matches には入れず、
Tournament.guest_matches に分けて持つ (参加者の集合・人数・順位の母集団に負の uid を混ぜないため)。読むのは
  - 直対評価の学習 (spsp/learner.g2_learn_advance): アカウントのある側だけを、初めて出た選手 (初期値) が相手として更新する。
    無い側のレートはどこにも残さない (試合ごとに初期値)。RATED_SINCE 以降の大会だけ
  - 勝敗数 (spsp/score._TourOutcomes)・選手のページの対戦一覧 (spsp/build._v4_adapt_bt_outputs)・SPR の「実際の勝ち」(spsp/meta)
だけ。相手の名前は matches.json の winner_name / loser_name (smash_database が
アカウントの無い側だけ入れる。古いデータには無い) で、無ければ None (フロントが「アカウント未登録の参加者」と出す)。

両方ともアカウントが無い試合と DQ・中止の試合は今までどおり読まない。Challonge の下位クラス (class_matches.json) も対象外。
SPSP_GUEST_MATCHES=0 で以前どおり (読み飛ばす)。
"""
from __future__ import annotations

import datetime as _dt
import os

ENABLED = os.environ.get('SPSP_GUEST_MATCHES', '1') != '0'

# 直対評価に数え始める日 (JST、この日以降に始まった大会)。それより前の大会の試合は、表示 (対戦一覧・勝敗数) には出すが
# 学習には入れない (レート変動 0・「直対に数えない試合」)。過去の大会まで数えると全員の順位がシーズン途中で動くため
# (凍結データで試して上位 64 位のうち 14 人、全体の 9 割が動いた。2026-11-16)。
# SPSP_GUEST_RATED_SINCE=YYYY-MM-DD で変えられる (検証用。本番は既定のまま)
RATED_SINCE = os.environ.get('SPSP_GUEST_RATED_SINCE', '2026-11-15')
RATED_SINCE_TS = int(_dt.datetime.fromisoformat(RATED_SINCE).replace(
    tzinfo=_dt.timezone(_dt.timedelta(hours=9))).timestamp())

# uid (負) → 名前 (無ければ None)。data_loader が大会を読むたびに足す
NAMES: dict[int, str | None] = {}


def is_guest(uid) -> bool:
    return isinstance(uid, int) and uid < 0


def guest_uid(match_id) -> int | None:
    """試合の id から、その試合だけの uid。id が正の整数でなければ None (その試合は読まない)。"""
    if isinstance(match_id, bool) or not isinstance(match_id, int) or match_id <= 0:
        return None
    return -match_id


def name(uid) -> str | None:
    return NAMES.get(uid)


def of(t):
    """大会のアカウント無しの相手との試合 (無ければ空)。古い pickle の Tournament には項目が無い。"""
    return getattr(t, 'guest_matches', None) or ()


def rated(t):
    """直対評価に数える分 (RATED_SINCE 以降の大会だけ)。"""
    g = getattr(t, 'guest_matches', None)
    return g if g and t.timestamp >= RATED_SINCE_TS else ()


def with_rated(t, key):
    """学習用: 大会の試合 + 直対評価に数えるアカウント無しの相手との試合を、進行順 (key) に並べる。"""
    g = rated(t)
    return sorted(list(t.matches) + list(g), key=key) if g else sorted(t.matches, key=key)


def with_guests(t, key=None):
    """大会の試合 + アカウント無しの相手との試合。key (進行順のキー) があればその順に並べる。無い大会は t.matches そのもの。"""
    g = of(t)
    if not g:
        return sorted(t.matches, key=key) if key is not None else t.matches
    ms = list(t.matches) + list(g)
    return sorted(ms, key=key) if key is not None else ms

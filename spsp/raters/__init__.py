"""本番ビルド (v4/learn.py) が使う rater だけを置く。

以前は eval/raters/ (精度実験用の rater 群) から OpenSkillBTRater を import していた。
base.py / openskill_rater.py は eval/raters/ の同名ファイルと同一内容 (2026-09 時点)。
eval/ 側の複製は ⑧ (重複統合) で片付ける。
"""
from .base import Rater
from .openskill_rater import OpenSkillBTRater, OpenSkillPLRater

__all__ = ["Rater", "OpenSkillBTRater", "OpenSkillPLRater"]

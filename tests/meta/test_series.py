"""tournament_series のシリーズ統合テスト.

実行: OPENBLAS_NUM_THREADS=1 .venv/bin/python3 tests/meta/test_series.py
(リポジトリルートから。unittest 等は使わず plain assert)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 日本の命名規則 (smash_database scripts/Japan/naming.py) そのものを架空の名前で確かめる。本番のビルドは規則を読まず
# region_rules.json の表を引く (spsp/meta.py) ので、規則はテスト専用の口から直接呼ぶ。
from _region_rules_module import region_module  # noqa: E402
ts = region_module("naming").tournament_series


def eq(name, expected):
    got = ts(name)
    assert got == expected, f'{name!r}: {got!r} != {expected!r}'


# ── 2026-07 シリーズ分裂修正 (実データで確認した8系統) ──
# こくブラ SeasonN (〜wrap 内のナンバリング + Season N + 全角空白)
eq('～こくブラ Season29～', 'こくブラ')
eq('〜こくブラ　Season34〜', 'こくブラ')
eq('〜こくブラ Season39.75〜', 'こくブラ')
eq('平日こくブラ#10', 'こくブラ')
eq('こくブラ Season20 Aトナメ', 'こくブラ')   # クラス別トナメ suffix も親シリーズに統合
# 第N話
eq('スマえもん第21話', 'スマえもん')
eq('スマえもん第22話', 'スマえもん')
# 大文字小文字ゆらぎ (casefold 正規化)
eq('impact#23', 'Impact')
eq('Impact#19', 'Impact')
eq('彩trial#2', '彩Trial')
eq('彩Trial#5', '彩Trial')
# FlyHigh の「!」抜き表記
eq('FlyHigh♯23 96人トーナメント 埼玉県熊谷市スマブラ0328', 'FlyHigh!')
eq('FlyHigh!!#27 96人トーナメント 埼玉県熊谷市スマブラ大会', 'FlyHigh!')
# 連続空白
eq('Tokyo  Nights #42', 'Tokyo Nights')
# 年度+季節 suffix
eq('ELEVATE 2025冬', 'ELEVATE')
eq('ELEVATE 2025夏', 'ELEVATE')
eq('ELEVATE 2026冬', 'ELEVATE')
# N人規模 + 末尾【...】ラベル
eq('【1/24(土)開催！】船スマ32人規模トーナメント【船橋スマブラ対戦会】', '船スマ')
eq('【3/28(土)開催！】船スマ48人規模トーナメント【船橋スマブラ対戦会】', '船スマ')

# ── 既存挙動のガード (regression diff で不変を確認済みの代表例) ──
eq('ウメブラSP12', 'ウメブラ')      # 既知シリーズマッチ (SP suffix も統合)
eq('カリスマSP17', 'カリスマ')
eq('第一回/幻月～GENGETSU～', '幻月')
eq('マエスマTOP#13.5', 'マエスマTOP')
eq('スマわんこ12杯目', 'スマわんこ')
eq('るゆぶらっ！其の弐', 'るゆぶらっ！')
eq('下剋上#171', '下克上')
eq('こくブラ in Oita ～暁前夜祭～', 'こくブラin Oita')       # 地名 variant は別シリーズのまま
eq('こくブラEncore#1', 'こくブラEncore')                    # サブブランドも別のまま
# マエスマU20 の U20 は Under 20 (シリーズ番号ではない)
assert ts('マエスマU20').endswith('U20'), ts('マエスマU20')

# ── 2026-08: 第N陣 (渋谷大乱) ──
# 「第一陣」を残すと第二陣が別シリーズになり、開催回数が積み上がらない。
eq('渋谷大乱 第一陣/SHIBUYA DAIRAN -First Strike-', '渋谷大乱')
eq('渋谷大乱 第二陣/SHIBUYA DAIRAN -Second Strike-', '渋谷大乱')
eq('渋谷大乱 第三陣', '渋谷大乱')
eq('渋谷大乱 第10陣', '渋谷大乱')
eq('渋谷大乱', '渋谷大乱')                     # 番号なしでも同じシリーズ

# 回数の抽出 (第N陣 / 第N幕 も 第N回 と同じ扱い)
tsn = region_module("naming").tournament_series_number
def num_eq(name, expected):
    got = tsn(name)
    assert got == expected, f'{name!r}: {got!r} != {expected!r}'

num_eq('渋谷大乱 第一陣/SHIBUYA DAIRAN -First Strike-', 1)
num_eq('渋谷大乱 第三陣', 3)
num_eq('渋谷大乱 第10陣', 10)
num_eq('渋谷大乱', None)                        # 番号が無ければ None
num_eq('桜梅桃李 第肆幕/OH-BAI-TOH-RI 4', 4)     # 既存の 幕 も従来どおり

print('all series tests passed')

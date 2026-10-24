"""fileio — 配信されるファイルの原子的な書き出し。

書きかけのファイルが配信・rsync されないよう、同じディレクトリの一時ファイルに書いてから os.replace で置く。
JSON の書式 (separators / indent / sort_keys / 末尾改行) は呼び出し側がそのまま指定する = 出力バイトは変えない。
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def write_text_atomic(path, text: str, encoding: str = 'utf-8') -> None:
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'w', encoding=encoding) as f:
        f.write(text)
    os.replace(tmp, path)


def write_bytes_atomic(path, data: bytes) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)


def write_json_atomic(path, obj, *, trailing_newline: bool = False, **dump_kwargs) -> None:
    """json.dump と同じ引数 (ensure_ascii / separators / indent / sort_keys ...) で原子的に書く。"""
    text = json.dumps(obj, **dump_kwargs)
    if trailing_newline:
        text += '\n'
    write_text_atomic(path, text)

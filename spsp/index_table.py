"""smash_database の索引ファイル (users.jsonl / tournaments.jsonl) を読む口。

GitHub は 100 MiB を超えるファイルを受け付けないので、smash_database では地域ごとに
users.jsonl → users/<user_id // 500000>.jsonl、tournaments.jsonl → tournaments/<tournament_id // 100000>.jsonl
に分けられることがある (smash_database issue #106、docs/data_model.md の「Shards」)。
行の中身は 1 ファイルのときと同じ。ディレクトリがあればその <n>.jsonl を n の小さい順に全部、無ければ 1 ファイルを読む。
両方あるのは壊れた状態なので止める。ファイルをまたぐ行の順は 1 ファイルのときと違うので、id で引くこと。
"""
from __future__ import annotations

from pathlib import Path


def _shard_dir(path: Path) -> Path:
    return path.with_name(path.name.removesuffix('.jsonl'))


def files(path) -> list[Path]:
    """表の行が入っているファイル (読む順)。表が無ければ []。"""
    p = Path(path)
    d = _shard_dir(p)
    if d != p and d.is_dir():
        if p.exists():
            raise RuntimeError(f'{p} と {d}/ が両方ある (smash_database の scripts/common/manual/shard_jsonl.py でどちらかにする)')
        return sorted((f for f in d.iterdir() if f.suffix == '.jsonl' and f.stem.isdigit()), key=lambda f: int(f.stem))
    return [p] if p.exists() else []


def exists(path) -> bool:
    return bool(files(path))


def iter_lines(path):
    """表全体の行 (改行つき)。1 ファイルを for line in f で読むのと同じ。"""
    for f in files(path):
        with open(f, encoding='utf-8') as fh:
            yield from fh

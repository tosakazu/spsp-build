#!/usr/bin/env python3
"""deploy/r2_manifest.py — R2 に置いた JSON の一覧 (パス → MD5) で、前回の同期から変わったファイルだけを送る (2026-10-21)。

deploy/sync_r2.sh が使う。rclone sync --checksum は毎回 R2 の一覧を取って全ファイルを比べるので、何も変わらない回でも
日本で 90 秒かかっていた (players / history / まとめ … 各 5〜30 秒、送ったのは 0 バイト)。ここでは前回送ったものの一覧
(マニフェスト) を R2 の <地域>/_sync_manifest.json.gz に置いておき、手元のファイルの MD5 と比べて
送るもの・消すものの一覧を作る (R2 は見ない)。

    python3 deploy/r2_manifest.py plan <配信ディレクトリ> <前回のマニフェスト (無ければ "-")> <作業ディレクトリ>

作業ディレクトリに書くもの:
  <組>.upload / <組>.delete   組の根 (下の GROUPS) からの相対パス、1 行 1 つ (rclone の --files-from)
  manifest.json.gz            新しいマニフェスト (同期が全部終わってから R2 に置く)
  summary.txt                 組ごとの数
終了コード: 0 = 計画できた / 3 = 前回のマニフェストが使えない (全件の sync で同期し直すこと) /
           4 = 1 組で消すファイルが SPSP_R2_MAX_DELETE (既定 1000) を超える (rclone sync の --max-delete と同じ止め方)

組 (送る順・Cache-Control は sync_r2.sh と同じ):
  bundle      data/views/bundle/ の *.json (一覧より先に送る)
  players     players/ の *.json。まとめた回 (_unbundled_players.txt がある) はそこに載っている選手だけ (載っていない
  history     ファイルは同期の対象外で、R2 からも消さない。rclone sync --files-from と同じ)
  tournaments tournaments/ の *.json
  data        data/ の *.json (views/bundle/ は bundle の組)
直下の players_current.json / meta.json / latest_*.jsonl は毎回送る (sync_r2.sh。マニフェストには入れない)。

マニフェストは「送ったもの」の記録で、R2 の中身そのものではない。ずれないために sync_r2.sh は
  - R2 の <地域>/ に書くのは sync_r2.sh だけ (手で rclone しない)
  - 送り始める前に R2 のマニフェストを消し、全部送り終えてから置く (途中で落ちた回の次は全件 sync)
  - 念のため SPSP_R2_FULL_SYNC_DAYS (既定 7) 日に 1 回、従来どおり R2 と全件を比べる sync をしてマニフェストを作り直す
使うのは既定で日本 (jp) だけ (SPSP_R2_MANIFEST、2026-10-22)。
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import os
import sys

FORMAT = 'spsp-r2-manifest/1'
GROUPS = ('bundle', 'players', 'tournaments', 'history', 'data')
ROOTS = {'bundle': 'data/views/bundle', 'players': 'players', 'tournaments': 'tournaments',
         'history': 'history', 'data': 'data'}
EXIT_NO_MANIFEST = 3
EXIT_TOO_MANY_DELETES = 4


def md5_of(path: str) -> str:
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _json_files(root: str, skip_dir: str | None = None) -> list:
    """root の下の *.json (root からの相対パス、並びは決まった順)。skip_dir (root からの相対) の下は除く。"""
    out = []
    if not os.path.isdir(root):
        return out
    for d, dirs, files in os.walk(root):
        rel_d = os.path.relpath(d, root)
        if skip_dir is not None and (rel_d == skip_dir or rel_d.startswith(skip_dir + os.sep)):
            dirs[:] = []
            continue
        dirs.sort()
        for f in sorted(files):
            if f.endswith('.json'):
                out.append(f if rel_d == '.' else os.path.join(rel_d, f))
    return out


def group_files(src: str) -> tuple[dict, dict]:
    """({組: [組の根からの相対パス]}, {組: 消してよい組か})。sync_r2.sh の rclone sync と同じファイルを選ぶ。"""
    unbundled = os.path.join(src, '_unbundled_players.txt')
    listed = None
    if os.path.isfile(unbundled):
        with open(unbundled) as f:
            listed = [ln.strip() for ln in f if ln.strip()]
    files, may_delete = {}, {}
    for g in GROUPS:
        root = os.path.join(src, ROOTS[g])
        if g in ('players', 'history') and listed is not None:
            # まとめた回: 一覧に載っている選手だけ (無いファイルは rclone も飛ばす)。一覧に無いものは R2 から消さない
            files[g] = [p for p in listed if os.path.isfile(os.path.join(root, p))]
            may_delete[g] = False
        else:
            files[g] = _json_files(root, skip_dir=os.path.join('views', 'bundle') if g == 'data' else None)
            may_delete[g] = True
    return files, may_delete


def load_manifest(path: str):
    if path == '-' or not os.path.isfile(path):
        return None
    try:
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            m = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict) or m.get('format') != FORMAT or not isinstance(m.get('files'), dict):
        return None
    return m


def plan(src: str, old_path: str, work: str, *, full: bool = False, max_delete: int = 1000, now=None) -> int:
    """full=True: 前回のマニフェストを使わずに新しいマニフェストだけ作る (全件の sync の後)。"""
    os.makedirs(work, exist_ok=True)
    now = now or dt.datetime.now(dt.timezone.utc)
    old = None if full else load_manifest(old_path)
    if not full and old is None:
        print(f'前回のマニフェストが無い・読めない ({old_path}) — 全件の sync が要る', file=sys.stderr)
        return EXIT_NO_MANIFEST
    old_files = (old or {}).get('files', {})
    files, may_delete = group_files(src)
    new_files: dict = {}
    summary = []
    too_many = []
    for g in GROUPS:
        prefix = ROOTS[g] + '/'
        up, n_same = [], 0
        for rel in files[g]:
            key = prefix + rel
            h = md5_of(os.path.join(src, ROOTS[g], rel))
            new_files[key] = h
            if old_files.get(key) == h:
                n_same += 1
            else:
                up.append(rel)
        dele = []
        if may_delete[g] and not full:
            here = set(files[g])
            # 前回送ったもののうち、今回の組に無いもの。data の組は views/bundle/ を除く (bundle の組が見る)
            for key in old_files:
                if not key.startswith(prefix):
                    continue
                rel = key[len(prefix):]
                if g == 'data' and rel.startswith('views/bundle/'):
                    continue
                if rel.endswith('.json') and rel not in here:
                    dele.append(rel)
            dele.sort()
        elif not may_delete[g]:
            # 一覧に載っていない (まとめた) 選手のファイルは R2 に残っている。マニフェストにも残す
            # (後で一覧に戻って中身が同じなら送り直さない)
            for key, h in old_files.items():
                if key.startswith(prefix) and key not in new_files:
                    new_files[key] = h
        if len(dele) > max_delete >= 0:
            too_many.append(f'{g}: {len(dele)} 件')
        for ext, lst in (('upload', up), ('delete', dele)):
            with open(os.path.join(work, f'{g}.{ext}'), 'w') as f:
                f.writelines(p + '\n' for p in lst)
        summary.append(f'{g:<12} 同じ {n_same:6d} / 送る {len(up):6d} / 消す {len(dele):6d}')
    m = {'format': FORMAT, 'written_at': now.isoformat(timespec='seconds'),
         'full_sync_at': now.isoformat(timespec='seconds') if full else (old or {}).get('full_sync_at'),
         'files': dict(sorted(new_files.items()))}
    tmp = os.path.join(work, 'manifest.json.gz.tmp')
    with gzip.open(tmp, 'wt', encoding='utf-8', compresslevel=6) as f:
        json.dump(m, f, separators=(',', ':'))
    os.replace(tmp, os.path.join(work, 'manifest.json.gz'))
    with open(os.path.join(work, 'summary.txt'), 'w') as f:
        f.write('\n'.join(summary) + '\n')
    print('\n'.join(summary))
    if too_many:
        print(f'消すファイルが多すぎる (上限 {max_delete}): {", ".join(too_many)} — SPSP_R2_MAX_DELETE=-1 で手で流すこと',
              file=sys.stderr)
        return EXIT_TOO_MANY_DELETES
    return 0


def full_sync_due(old_path: str, days: float, now=None) -> bool:
    """前回の全件 sync から days 日以上たったか (マニフェストが無い・日付が無いときも True)。"""
    m = load_manifest(old_path)
    t = (m or {}).get('full_sync_at')
    if not t:
        return True
    try:
        at = dt.datetime.fromisoformat(t)
    except ValueError:
        return True
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - at).total_seconds() >= days * 86400


def main(argv) -> int:
    if len(argv) >= 2 and argv[1] == 'due':          # due <前回のマニフェスト> <日数>: 全件 sync の時期なら 0
        return 0 if full_sync_due(argv[2], float(argv[3])) else 1
    if len(argv) == 5 and argv[1] in ('plan', 'full'):
        return plan(argv[2], argv[3], argv[4], full=(argv[1] == 'full'),
                    max_delete=int(os.environ.get('SPSP_R2_MAX_DELETE', '1000')))
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
"""大会アイコンを R2 に置く (2026-09-30〜)。

smash_database の tournament_images.jsonl (大会 ID → start.gg のアイコン URL) を読み、まだ取っていない大会の
アイコンを start.gg から元解像度のままダウンロードして R2 に置く。

R2 の置き方 (中身で重複をまとめる。シリーズ大会は同じ画像を大会ごとに上げ直すので、中身が違うのは半分以下):
  r2:spsp-data/images/tournament/h/<MD5>.<拡張子>   画像。同じ中身は 1 つだけ。Cache-Control は immutable
  r2:spsp-data/images/tournament/index.json         対応表 {"<大会 ID>": {"file": "h/<MD5>.<拡張子>", "src": "<start.gg のファイル名>"}}
公開 URL は https://data.spsp.games/images/tournament/<file>。フロントは index.json で大会 ID から引く。

- 取り直すのは、対応表に無い大会と、start.gg のファイル名 (src) が変わった大会 (= 主催者が差し替えた) だけ。
  アイコンが消えた大会は対応表から外す。対応表は画像を送り終えてから書く (指す先が無い行を作らない)。
- 使われなくなった画像は消さない。
- 画像は smash_database には入れない (URL だけ)。画像なしでデータを使う人は何もしなくてよい。
- ダウンロードに失敗したもの (404 など) は数えて表示し、次回また試す。失敗があっても終了コードは 0。
  rclone の失敗は 1 (対応表は書かない)。

使い方:
  .venv/bin/python deploy/sync_tournament_images.py [--index smash_db_tournament/data/startgg/Japan/tournament_images.jsonl]
      [--limit N] [--workers 4] [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = ROOT / 'smash_db_tournament/data/startgg/Japan/tournament_images.jsonl'
DEFAULT_DEST = f"r2:{os.environ.get('R2_BUCKET', 'spsp-data')}/images/tournament"
MANIFEST = 'index.json'
BATCH = 200   # この件数ごとに R2 へ送り、対応表を書く (途中で止まっても送った分は残る)


def rclone_bin() -> str:
    r = os.environ.get('RCLONE') or str(Path.home() / 'bin/rclone')
    return r if os.access(r, os.X_OK) else (shutil.which('rclone') or r)


def rclone(args: list[str], **kw) -> subprocess.CompletedProcess:
    # rclone は Go 製。ConoHa のスレッド上限に当たらないよう OS スレッドを 1 本に
    return subprocess.run([rclone_bin(), *args], env={**os.environ, 'GOMAXPROCS': os.environ.get('GOMAXPROCS', '1')}, **kw)


def src_name(url: str) -> str:
    """start.gg のファイル名 (?ehk=… は除く)。差し替えられると変わる。"""
    return os.path.basename(urlsplit(url).path)


def load_index(path: Path) -> dict[int, str | None]:
    out = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            if line.strip():
                rec = json.loads(line)
                out[int(rec['tournament_id'])] = rec.get('url')
    return out


def load_manifest(dest: str) -> dict[str, dict]:
    p = rclone(['cat', f'{dest}/{MANIFEST}'], capture_output=True, text=True)
    if p.returncode != 0:
        if 'not found' in p.stderr.lower():
            return {}
        raise SystemExit(f"ERROR: {dest}/{MANIFEST} を読めない: {p.stderr.strip()[:300]}")
    return json.loads(p.stdout) if p.stdout.strip() else {}


def list_hashed(dest: str) -> set[str]:
    p = rclone(['lsf', '--files-only', f'{dest}/h'], capture_output=True, text=True)
    if p.returncode != 0:
        if 'not found' in p.stderr.lower():
            return set()
        raise SystemExit(f"ERROR: rclone lsf {dest}/h に失敗: {p.stderr.strip()[:300]}")
    return {f"h/{l.strip()}" for l in p.stdout.splitlines() if l.strip()}


def download(session: requests.Session, url: str) -> tuple[bytes | None, str | None]:
    """(中身, None) か (None, 失敗の理由)。"""
    reason = '?'
    for attempt in range(3):
        try:
            r = session.get(url, timeout=60)
            if r.status_code == 200 and r.content:
                return r.content, None
            if r.status_code in (403, 404, 410):
                return None, f"HTTP {r.status_code}"
            reason = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            reason = type(e).__name__
        time.sleep(2 * (attempt + 1))
    return None, reason


def write_manifest(manifest: dict, dest: str, tmp: Path) -> None:
    f = tmp / MANIFEST
    f.write_text(json.dumps(dict(sorted(manifest.items(), key=lambda kv: int(kv[0]))), ensure_ascii=False,
                            separators=(',', ':')), encoding='utf-8')
    p = rclone(['copyto', str(f), f'{dest}/{MANIFEST}', '--s3-no-check-bucket',
                '--header-upload', 'Cache-Control: public, max-age=300'])
    if p.returncode != 0:
        raise SystemExit(f"ERROR: 対応表を書けない (rc={p.returncode})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--index', type=Path, default=DEFAULT_INDEX)
    ap.add_argument('--dest', default=DEFAULT_DEST)
    ap.add_argument('--limit', type=int, default=None, help='今回取る最大件数')
    ap.add_argument('--workers', type=int, default=int(os.environ.get('SPSP_IMG_WORKERS', '4')))
    ap.add_argument('--dry-run', action='store_true', help='取る件数を数えるだけ')
    args = ap.parse_args()

    if not args.index.exists():
        print(f"[images] {args.index} が無い — 何もしない (smash_database が大会アイコンを記録する前の版)")
        return 0
    index = load_index(args.index)
    manifest = load_manifest(args.dest)
    # アイコンが消えた大会 (url が null) は対応表から外す
    removed = [t for t, url in index.items() if url is None and str(t) in manifest]
    for t in removed:
        del manifest[str(t)]
    todo = [(t, url) for t, url in sorted(index.items())
            if url and manifest.get(str(t), {}).get('src') != src_name(url)]
    print(f"[images] アイコンあり {sum(1 for u in index.values() if u)} 大会 / 対応表 {len(manifest)} / "
          f"取る {len(todo)} / 外す {len(removed)}", flush=True)
    if args.limit is not None:
        todo = todo[:args.limit]
    if args.dry_run:
        return 0
    if not todo:
        if removed:
            with tempfile.TemporaryDirectory(prefix='spsp_img_') as tmp:
                write_manifest(manifest, args.dest, Path(tmp))
        return 0

    have = list_hashed(args.dest)
    uploaded, reused, failed = 0, 0, []
    session = requests.Session()
    session.headers['User-Agent'] = 'spsp-ranking (https://spsp.games)'
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        with tempfile.TemporaryDirectory(prefix='spsp_img_') as tmp:
            stage = Path(tmp) / 'h'
            stage.mkdir()
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                results = list(ex.map(lambda it: download(session, it[1]), batch))
            new_rows, staged = {}, 0
            for (tid, url), (body, reason) in zip(batch, results):
                if body is None:
                    failed.append((tid, url, reason))
                    continue
                ext = os.path.splitext(src_name(url))[1].lower() or '.img'
                file = f"h/{hashlib.md5(body).hexdigest()}{ext}"
                if file in have or (stage / file[2:]).exists():
                    reused += 1
                else:
                    (stage / file[2:]).write_bytes(body)
                    staged += 1
                new_rows[str(tid)] = {'file': file, 'src': src_name(url)}
            if staged:
                p = rclone(['copy', str(stage), f'{args.dest}/h', '--s3-no-check-bucket',
                            '--transfers', str(max(args.workers, 4)),
                            '--header-upload', 'Cache-Control: public, max-age=31536000, immutable'])
                if p.returncode != 0:
                    print(f"ERROR: rclone copy に失敗 (rc={p.returncode})", file=sys.stderr)
                    return 1
                have |= {f"h/{f.name}" for f in stage.iterdir()}
                uploaded += staged
            manifest.update(new_rows)
            write_manifest(manifest, args.dest, Path(tmp))
        print(f"[images] {min(i + BATCH, len(todo))}/{len(todo)} (新しい画像 {uploaded}、既にある画像 {reused}、"
              f"失敗 {len(failed)})", flush=True)

    print(f"[images] 完了: 新しい画像 {uploaded} 枚、既にある画像を使った {reused} 大会、失敗 {len(failed)} 件")
    for tid, url, r in failed[:20]:
        print(f"  失敗 {tid} {r} {url}")
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""ページ用に小さく分けた一覧ファイル (BUILD_DIR/data/views/) を書き出す (2026-10-17〜)。

latest_tjpr_full.jsonl (全選手の行) と players_current.json (全選手の揮発部分) は、選手が増えると
(北米 20 万人で圧縮後 20 MB / 14 MB) どのページでも丸ごと読むには重すぎる。ページが本当に要る分だけを読めるよう、
用途ごとのファイルに分ける。元の 2 ファイルはダウンロード用・互換のためにそのまま残す。

  data/views/manifest.json                   形式・件数・分割数 (ページはまずこれを読む)
  data/views/players_index.json              全選手の軽い一覧 (名前検索・決まった選手の順位引き・優先枠・都道府県の集計)
                                             列形式 {columns, rows}。行は総合順位順 (順位なしは末尾、jsonl 行順)。
                                             pref = geo.json の単位の order、last = 最終参加日 (1970-01-01 からの日数)
  data/views/current/<uid % N>.json          players_current.json を uid で N 分割 (1 人分を読むページ用。中身の形は同じ)。
                                             まとめた回は bundled = このシャードでまとめた uid の一覧 (選手ページはこれで読む先を決める)
  data/views/ranking/<method>/<k>.json       全国ランキング表を method の順位順に CHUNK 人ずつ (latest_tjpr_full.jsonl の行そのもの
                                             + 表示順位 _display_rank / _is_overseas。順位なしは末尾)
  data/views/char/<char_id>.json             使い手ランキングの行 (character_index の main_by_char の順)。
                                             行に characters = [そのキャラの使用実績] を足す (players/<uid>.json から。使用率の列用)
  data/views/char_sub/<char_id>.json         そのキャラをサブで使う人の行 (character_index の sub_by_char の順、形は char/ と同じ)。
                                             使い手ランキングの「サブ使用も表示」で char/ に混ぜる
  data/views/pref/<geo order>.json           都道府県 (地域の単位) ごとのランキングの行 (jsonl 行順)
  data/views/pref_summary.json               単位ごとの人数と 1 位 (海外勢を除く)
  data/views/sim_pop.json                    シミュレーション用の全選手の数値 (列形式)
  data/views/bundle/<uid % B>.json           SPSP_BUNDLE_SMALL=1 のときだけ: 選手ファイル (players/<uid>.json) が BUNDLE_MAX_BYTES
                                             未満の選手の players/ と history/ を uid で B 個にまとめたもの
                                             {format, players: {uid: {p: <players/uid.json>, h: <history/uid.json>}}}。
                                             まとめた選手の players/<uid>.json と history/<uid>.json は消す (R2 の書き込み回数と
                                             ファイル数を減らす。参加の少ない選手が大半で、history/ は週 1 回全員分が変わる)。
                                             players_index の列 b = 1 がまとめた選手。B は manifest.bundle_shards

入力: latest_tjpr_full.jsonl, players_current.json, data/{character_index,player_prefectures,overseas,geo,last_activity}.json
(どれも同じ BUILD_DIR にあるビルドの出力。aux の build_player_prefectures の後に走る)。
使い方: python spsp/cli/build_split_views.py [--build-dir DIR]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # リポジトリ直下 (spsp パッケージ)
from spsp.paths import SITE_DIR  # noqa: E402
from spsp.fileio import write_json_atomic  # noqa: E402
from spsp.char_index import canonical_char_id  # noqa: E402

FORMAT = 1
CURRENT_SHARDS = 256          # current/ の分割数 (北米 20 万人で 1 個 約 800 人)
CHUNK = 1000                  # ranking/ の 1 ファイルの人数
METHODS = ('ensemble', 'tjpr', 'bt_gated')
INDEX_COLUMNS = ('uid', 'name', 'ens', 'tjpr', 'bt', 'lv', 'prov', 'ovs', 'char', 'pref', 'last', 'b')
BUNDLE_MAX_BYTES = 30_000     # これ未満の選手ファイルをまとめる (日本で選手の 79%・バイトの 18%)
BUNDLE_TARGET_BYTES = 256_000 # まとめファイル 1 個の目安 (分割数 B = 2 のべき乗で、これに近くなるように)
SIM_COLUMNS = ('uid', 'tjpr_score', 'bt_gated_elo', 'bt_internal_elo', 'lv', 'gray', 'ens', 'tjpr', 'bt')
_EPOCH = dt.date(1970, 1, 1)
_DUMP = dict(ensure_ascii=False, separators=(',', ':'))


def _load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _load_jsonl(path):
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def _days(date_str):
    """'YYYY-MM-DD' → 1970-01-01 からの日数 (無ければ None)。"""
    try:
        return (dt.date.fromisoformat(date_str[:10]) - _EPOCH).days
    except (TypeError, ValueError):
        return None


def _lv(rec):
    s = rec.get('scores') or {}
    v = s.get('shared_cascade_lv')
    return v if v is not None else s.get('tjpr_level')


def _main_char_use(player_path, char_id):
    """players/<uid>.json の characters から char_id の使用実績 ({id, name, pct, n, games, total}) を。無ければ None。"""
    try:
        with open(player_path, encoding='utf-8') as f:
            chars = json.load(f).get('characters') or []
    except (OSError, ValueError):
        return None
    return next((c for c in chars if c.get('id') == char_id), None)


def _sub_char_use(player_path, char_id):
    """サブ用: 統合キャラ (ピーチ/デイジー など) も canonical id で当てる。2 番目以降の使用実績から"""
    try:
        with open(player_path, encoding='utf-8') as f:
            chars = json.load(f).get('characters') or []
    except (OSError, ValueError):
        return None
    return next((c for c in chars[1:] if c.get('id') is not None
                 and canonical_char_id(int(c['id'])) == char_id), None)


def _pick_bundled(site):
    """まとめる選手 {uid: 選手ファイル + 履歴ファイルのバイト数}。選手ファイルが BUNDLE_MAX_BYTES 未満の人。"""
    pdir, hdir = os.path.join(site, 'players'), os.path.join(site, 'history')
    out = {}
    for name in os.listdir(pdir) if os.path.isdir(pdir) else ():
        if not name.endswith('.json') or not name[:-5].isdigit():
            continue
        size = os.path.getsize(os.path.join(pdir, name))
        if size < BUNDLE_MAX_BYTES:
            h = os.path.join(hdir, name)
            out[int(name[:-5])] = size + (os.path.getsize(h) if os.path.exists(h) else 0)
    return out


def bundle_shard_count(total_bytes):
    """まとめファイルの数 B: 2 のべき乗で、1 個が BUNDLE_TARGET_BYTES 前後 (最低 16)。人数がゆっくり増えても
    B は倍々でしか変わらないので、普段は同じ uid が同じファイルに入り続ける (= 変わらない選手の分は書き直さない)。"""
    b = 16
    while total_bytes / b > BUNDLE_TARGET_BYTES * 1.5:
        b *= 2
    return b


def _write_bundles(site, tmp_dir, bundled):
    """bundle/<uid % B>.json を書き、まとめなかった選手の一覧 (R2 同期用) を site/_unbundled_players.txt に書く。
    選手ファイル・履歴ファイルの中身はバイト列のまま埋め込む (読み直して書き直さない = 中身は個別ファイルと完全に同じ)。"""
    pdir, hdir = os.path.join(site, 'players'), os.path.join(site, 'history')
    n = bundle_shard_count(sum(bundled.values()))
    groups = [[] for _ in range(n)]
    for uid in sorted(bundled):
        groups[uid % n].append(uid)
    os.makedirs(os.path.join(tmp_dir, 'bundle'), exist_ok=True)

    def raw(path):
        try:
            with open(path, 'rb') as f:
                return f.read()
        except OSError:
            return b'null'

    for i, uids in enumerate(groups):
        parts = [b'"%d":{"p":%s,"h":%s}' % (u, raw(os.path.join(pdir, f'{u}.json')), raw(os.path.join(hdir, f'{u}.json')))
                 for u in uids]
        with open(os.path.join(tmp_dir, 'bundle', f'{i}.json'), 'wb') as f:
            f.write(b'{"format":%d,"shards":%d,"players":{' % (FORMAT, n) + b','.join(parts) + b'}}')
    keep = sorted(int(x[:-5]) for x in os.listdir(pdir) if x.endswith('.json') and x[:-5].isdigit()
                  and int(x[:-5]) not in bundled)
    with open(os.path.join(site, '_unbundled_players.txt.tmp'), 'w') as f:
        f.writelines(f'{u}.json\n' for u in keep)
    os.replace(os.path.join(site, '_unbundled_players.txt.tmp'), os.path.join(site, '_unbundled_players.txt'))
    return n


def display_ranks(rows, overseas, methods=METHODS):
    """site/ranking-table.js computeDisplayRanks と同じ: 灰色 (海外 or 計測中) を数えない表示順位。
    {id(rec): {method: rank}} を返す (灰色が 1 人もいなければ空 = 表示順位は順位そのもの)。"""
    gray = {id(r): (r.get('user_id') in overseas) or bool((r.get('metadata') or {}).get('provisional')) for r in rows}
    out = {}
    if not any(gray.values()):
        return out
    for m in methods:
        ranked = [r for r in rows if (r.get('ranks') or {}).get(m) and r['ranks'][m] > 0]
        ranked.sort(key=lambda r: (r['ranks'][m], 0 if gray[id(r)] else 1))   # 同順位は灰色が先 (ranking-table.js と同じ)
        jp = 0
        for r in ranked:
            if gray[id(r)]:
                out.setdefault(id(r), {})[m] = jp + 1
            else:
                jp += 1
                out.setdefault(id(r), {})[m] = jp
    return out


def _order_by(rows, method):
    """method の順位順 (順位なしは末尾、元の行順のまま)。"""
    BIG = 10 ** 12
    return sorted(rows, key=lambda r: (r.get('ranks') or {}).get(method) or BIG)


def build_views(site: str) -> dict:
    data = os.path.join(site, 'data')
    out_dir = os.path.join(data, 'views')
    tmp_dir = out_dir + '.tmp'
    shutil.rmtree(tmp_dir, ignore_errors=True)
    os.makedirs(tmp_dir)

    rows = _load_jsonl(os.path.join(site, 'latest_tjpr_full.jsonl'))
    current = _load_json(os.path.join(site, 'players_current.json'), None)
    char_idx = _load_json(os.path.join(data, 'character_index.json'), {})
    pref_map = _load_json(os.path.join(data, 'player_prefectures.json'), {})
    overseas = set(_load_json(os.path.join(data, 'overseas.json'), {}).get('uids') or [])
    geo = _load_json(os.path.join(data, 'geo.json'), {})
    last_act = _load_json(os.path.join(data, 'last_activity.json'), {})
    unit_order = {u['id']: u['order'] for u in geo.get('units') or [] if 'id' in u and 'order' in u}

    def w(rel, obj):
        p = os.path.join(tmp_dir, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(obj, f, **_DUMP)

    by_ens = _order_by(rows, 'ensemble')
    disp = display_ranks(rows, overseas)
    bundled = _pick_bundled(site) if os.environ.get('SPSP_BUNDLE_SMALL') == '1' else {}

    # ── 軽い一覧 ──
    idx_rows = []
    for r in by_ens:
        uid = r.get('user_id')
        rk = r.get('ranks') or {}
        idx_rows.append([uid, r.get('display'), rk.get('ensemble'), rk.get('tjpr'), rk.get('bt_gated'), _lv(r),
                         1 if (r.get('metadata') or {}).get('provisional') else 0, 1 if uid in overseas else 0,
                         r.get('main_char_id'), unit_order.get(pref_map.get(str(uid))), _days(last_act.get(str(uid))),
                         1 if uid in bundled else 0])
    w('players_index.json', {'format': FORMAT, 'columns': list(INDEX_COLUMNS), 'rows': idx_rows})

    # ── 揮発部分を uid で分割 ──
    n_current = 0
    if current and isinstance(current.get('players'), dict):
        head = {k: v for k, v in current.items() if k != 'players'}
        shards = [dict() for _ in range(CURRENT_SHARDS)]
        for uid, row in current['players'].items():
            shards[int(uid) % CURRENT_SHARDS][uid] = row
        for i, sub in enumerate(shards):
            extra = {'bundled': sorted(u for u in bundled if u % CURRENT_SHARDS == i)} if bundled else {}
            w(f'current/{i}.json', {**head, **extra, 'players': sub})
        n_current = len(current['players'])

    # ── 全国ランキング表 (method ごと) ──
    def table_row(r):
        x = dict(r)
        d = disp.get(id(r))
        if d:
            x['_display_rank'] = d
        if r.get('user_id') in overseas:
            x['_is_overseas'] = True
        return x

    chunks = {}
    for m in METHODS:
        ordered = by_ens if m == 'ensemble' else _order_by(rows, m)
        n = 0
        for k in range(0, len(ordered), CHUNK):
            w(f'ranking/{m}/{k // CHUNK}.json', [table_row(r) for r in ordered[k:k + CHUNK]])
            n += 1
        chunks[m] = n

    # ── 使い手・都道府県の行 ──
    by_uid = {r.get('user_id'): r for r in rows}
    def char_rows(key, folder, use_of):
        n = 0
        for char_id, uids in (char_idx.get(key) or {}).items():
            out = []
            for u in uids:
                if u not in by_uid:
                    continue
                # 使い手ランキングの「使用率」列 (c/ranking は characters[0].pct を読む)。jsonl の行には無いので選手ファイルから
                x = by_uid[u]
                use = use_of(os.path.join(site, 'players', f'{u}.json'), int(char_id))
                out.append({**x, 'characters': [use]} if use else x)
            if out:
                w(f'{folder}/{char_id}.json', out)
                n += 1
        return n
    n_char = char_rows('main_by_char', 'char', _main_char_use)
    n_char_sub = char_rows('sub_by_char', 'char_sub', _sub_char_use)
    by_pref = {}
    for r in rows:                                   # jsonl 行順 (pref/ranking の安定ソートの並びを保つ)
        p = pref_map.get(str(r.get('user_id')))
        if p in unit_order:
            by_pref.setdefault(p, []).append(r)
    summary = []
    for p, sub in by_pref.items():
        w(f'pref/{unit_order[p]}.json', sub)
        top = None
        for r in _order_by(sub, 'ensemble'):
            if r.get('user_id') not in overseas and (r.get('ranks') or {}).get('ensemble'):
                top = {'uid': r['user_id'], 'name': r.get('display'), 'ens': r['ranks']['ensemble']}
                break
        summary.append({'id': p, 'order': unit_order[p], 'n': len(sub), 'top': top})
    summary.sort(key=lambda s: s['order'])
    w('pref_summary.json', {'format': FORMAT, 'units': summary})

    # ── シミュレーション用 ──
    sim_rows = []
    for r in by_ens:
        s = r.get('scores') or {}
        rk = r.get('ranks') or {}
        uid = r.get('user_id')
        sim_rows.append([uid, s.get('tjpr_score'), s.get('bt_gated_elo'), s.get('bt_internal_elo'), _lv(r),
                         1 if (uid in overseas or (r.get('metadata') or {}).get('provisional')) else 0,
                         rk.get('ensemble'), rk.get('tjpr'), rk.get('bt_gated')])
    w('sim_pop.json', {'format': FORMAT, 'columns': list(SIM_COLUMNS), 'rows': sim_rows})

    # ── 参加の少ない選手の players/ と history/ をまとめる (SPSP_BUNDLE_SMALL=1 のときだけ) ──
    n_shards = _write_bundles(site, tmp_dir, bundled) if bundled else 0

    manifest = {'format': FORMAT, 'eval_date': (current or {}).get('eval_date'), 'n_players': len(rows),
                'current_shards': CURRENT_SHARDS if n_current else 0, 'chunk': CHUNK, 'ranking_chunks': chunks,
                'chars': n_char, 'chars_sub': n_char_sub, 'prefs': len(by_pref), 'bundle_shards': n_shards, 'bundled': len(bundled)}
    w('manifest.json', manifest)

    if not bundled:
        try:
            os.remove(os.path.join(site, '_unbundled_players.txt'))   # 前回まとめていた名残があれば消す (= 全員を個別に同期)
        except OSError:
            pass

    # まとめて差し替える (途中で止まっても前回分が残る)
    old = out_dir + '.old'
    shutil.rmtree(old, ignore_errors=True)
    if os.path.isdir(out_dir):
        os.rename(out_dir, old)
    os.rename(tmp_dir, out_dir)
    shutil.rmtree(old, ignore_errors=True)
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--build-dir', default=str(SITE_DIR), help='入出力の基点 (既定 site/)')
    args = ap.parse_args(argv)
    m = build_views(args.build_dir)
    print(f"[build_split_views] {m['n_players']} players: ranking chunks {m['ranking_chunks']}, "
          f"current shards {m['current_shards']}, chars {m['chars']}, prefs {m['prefs']}, "
          f"bundled {m['bundled']} players in {m['bundle_shards']} files")


if __name__ == '__main__':
    sys.exit(main())

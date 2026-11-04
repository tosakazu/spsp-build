"""deploy/sync_tournament_images.py: 取れなかった大会を覚えて試さない・変わらないファイルは書かない。
R2 (rclone) は辞書に置き換え、start.gg は download を差し替える。実行: python3 tests/meta/test_icon_sync.py"""
import datetime as dt
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'deploy'))
import sync_tournament_images as S  # noqa: E402


class FakeR2:
    def __init__(self):
        self.files: dict[str, str] = {}
        self.writes: list[str] = []
        self.lists = 0

    def __call__(self, args, **kw):
        cmd = args[0]
        out, err, rc = '', '', 0
        if cmd == 'cat':
            name = args[1].rsplit('/', 1)[1]
            if name in self.files:
                out = self.files[name]
            else:
                err, rc = 'object not found', 1
        elif cmd == 'copyto':
            name = args[2].rsplit('/', 1)[1]
            self.files[name] = Path(args[1]).read_text(encoding='utf-8')
            self.writes.append(name)
        elif cmd == 'copy':
            for f in Path(args[1]).iterdir():
                self.files[f't/{f.name}'] = 'img'
                self.writes.append(f't/{f.name}')
        elif cmd == 'lsf':
            self.lists += 1
            sub = args[2].rsplit('/', 1)[1]
            out = '\n'.join(n[len(sub) + 1:] for n in self.files if n.startswith(sub + '/'))
        return mock.Mock(returncode=rc, stdout=out, stderr=err)


def url(t, name):
    return f'https://images.start.gg/images/tournament/{t}/{name}.png'


def run(r2, d, answers, now=None):
    """answers: URL → 'ok' か失敗の理由。"""
    def fetch_one(session, u, thumbs_only):
        a = answers[u]
        return (b'body-' + u.encode(), b'thumb', None) if a == 'ok' else (None, None, a)
    argv = ['x', '--index', str(Path(d, 'tournament_images.jsonl')), '--dest', 'r2:b/na', '--thumbs-only']
    with mock.patch.object(S, 'rclone', r2), mock.patch.object(S, 'fetch_one', fetch_one), \
            mock.patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()) as out:
        if now:
            real = S.dt.datetime
            class FixedDT(real):
                @classmethod
                def now(cls, tz=None):
                    return now
            with mock.patch.object(S.dt, 'datetime', FixedDT):
                assert S.main() == 0
        else:
            assert S.main() == 0
    return out.getvalue()


def write_index(d, rows):
    Path(d, 'tournament_images.jsonl').write_text(''.join(
        json.dumps({'tournament_id': t, 'url': u, 'width': 1, 'height': 1}) + '\n' for t, u in rows))
    Path(d, 'tournaments.jsonl').write_text(''.join(
        json.dumps({'tournament_id': t, 'events': [{'event_id': t * 10}]}) + '\n' for t, _ in rows))


def test_permanent_failures_are_remembered_and_unchanged_files_not_rewritten():
    with tempfile.TemporaryDirectory() as d:
        r2 = FakeR2()
        a, b, c = url(1, 'a'), url(2, 'b'), url(3, 'c')
        write_index(d, [(1, a), (2, b), (3, c)])
        out = run(r2, d, {a: 'ok', b: 'HTTP 403', c: 'HTTP 503'})
        assert set(json.loads(r2.files['index.json'])) == {'1'}
        memo = json.loads(r2.files['failed.json'])
        assert list(memo) == ['2'] and memo['2']['src'] == 'b.png' and memo['2']['reason'] == 'HTTP 403', memo
        assert json.loads(r2.files['events.json'])['e'] == {'10': 0}

        # 次の回: 403 の大会は試さない、503 の大会はまた試す。何も変わらないので何も書かない
        r2.writes.clear(); r2.lists = 0
        out = run(r2, d, {a: 'ok', c: 'HTTP 503'})
        assert '取る 1 /' in out and '今回は試さない 1' in out, out
        assert r2.writes == [], r2.writes

        # 503 も解消すると、そこだけ足して index.json・events.json を書く
        r2.writes.clear()
        run(r2, d, {c: 'ok'})
        assert set(json.loads(r2.files['index.json'])) == {'1', '3'}
        assert sorted(w for w in r2.writes if not w.startswith('t/')) == ['events.json', 'index.json'], r2.writes

        # 全部済んだ回: 取るもの無し → R2 には読むだけ (書かない・t/ の一覧も取らない)
        r2.writes.clear(); r2.lists = 0
        run(r2, d, {})
        assert r2.writes == [] and r2.lists == 0, (r2.writes, r2.lists)

        # 主催者が差し替えた (URL が変わった) → すぐ試す。取れたら記録から消える
        b2 = url(2, 'b2')
        write_index(d, [(1, a), (2, b2), (3, c)])
        run(r2, d, {b2: 'ok'})
        assert set(json.loads(r2.files['index.json'])) == {'1', '2', '3'}
        assert json.loads(r2.files['failed.json']) == {}


def test_retry_after_days():
    with tempfile.TemporaryDirectory() as d:
        r2 = FakeR2()
        b = url(2, 'b')
        write_index(d, [(2, b)])
        t0 = dt.datetime(2026, 11, 4, tzinfo=dt.timezone.utc)
        run(r2, d, {b: 'HTTP 404'}, now=t0)
        out = run(r2, d, {}, now=t0 + dt.timedelta(days=S.RETRY_DAYS - 1))
        assert '取る 0 /' in out, out
        out = run(r2, d, {b: 'HTTP 404'}, now=t0 + dt.timedelta(days=S.RETRY_DAYS))
        assert '取る 1 /' in out, out
        assert json.loads(r2.files['failed.json'])['2']['at'].startswith('2026-11-11')


if __name__ == '__main__':
    test_permanent_failures_are_remembered_and_unchanged_files_not_rewritten()
    test_retry_after_days()
    print('ok')

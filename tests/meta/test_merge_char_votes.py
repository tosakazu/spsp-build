"""merge_char_votes: 先に渡した取得元の status を優先し、和集合を ts 順で返す."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from spsp.cli.merge_char_votes import merge_sources  # noqa: E402

gas = {'votes': [{'ts': '2026-09-01T00:00:00', 'user_id': 1, 'char_id': 10, 'status': 'approved'},
                 {'ts': '2026-09-02T00:00:00', 'user_id': 2, 'char_id': 11, 'status': 'pending'}]}
cf = {'votes': [{'ts': '2026-09-01T00:00:00', 'user_id': 1, 'char_id': 10, 'status': 'pending'},
                {'ts': '2026-09-03T00:00:00', 'user_id': 3, 'char_id': 12, 'status': 'pending'}]}
m = merge_sources([gas, cf])
assert [v['user_id'] for v in m] == [1, 2, 3], m
assert m[0]['status'] == 'approved', 'GAS (先) の status が優先されるべき'
assert merge_sources([cf, gas])[0]['status'] == 'pending'
assert merge_sources([cf]) == sorted(cf['votes'], key=lambda r: (r['ts'], r['user_id']))
print('ok test_merge_char_votes')

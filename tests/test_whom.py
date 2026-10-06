import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'whom.py'


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        cls.repo = Path(cls.tmp.name) / 'repo'
        cls.repo.mkdir()
        cls.git('init', '-q')
        cls.git('config', 'user.name', 'Fixture')
        cls.git('config', 'user.email', 'fixture@example.com')
        for i in range(30):
            cls.commit('Alice', 'alice@example.com', '2024-03-%02dT%02d:00:00+09:00' % (1 + i % 20, 9 + i % 10), 12)
        for i in range(25):
            cls.commit('Bob', 'bob@example.com', '2024-03-%02dT%02d:00:00+01:00' % (1 + i % 20, 8 + i % 12), 1)
        cls.commit('dependabot[bot]', 'bot@example.com', '2024-03-22T12:00:00+00:00', 100)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def git(cls, *args, env=None):
        return subprocess.run(['git', *args], cwd=str(cls.repo), env=env, check=True, capture_output=True, text=True).stdout

    @classmethod
    def commit(cls, name, email, date, lines, message='work'):
        with (cls.repo / 'sample.txt').open('a') as f:
            f.write(('change\n') * lines)
        cls.git('add', '.')
        env = dict(os.environ, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        cls.git('commit', '-q', '-m', message, env=env)

    def run_cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(CLI), '--now', '2024-04-01T17:00:00Z', *args], cwd=str(cwd or self.repo), capture_output=True, text=True)

    def test_rank_and_recommendation(self):
        result = self.run_cli('sample.txt')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('1. Alice', result.stdout)
        self.assertIn('(1 bot commits ignored)', result.stdout)
        self.assertIn('probably asleep', result.stdout)
        self.assertIn('Ask Bob', result.stdout)
        self.assertIn('55 commits by 2 people', result.stdout)

    def test_json_shape_and_hours(self):
        result = self.run_cli('--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        people = json.loads(result.stdout)
        self.assertEqual([p['name'] for p in people], ['Alice', 'Bob'])
        self.assertEqual(set(people[0]), {'name', 'email', 'share', 'commits', 'lines', 'last_commit', 'offset', 'local_time', 'sleep_window', 'active_hours', 'status'})
        self.assertEqual(people[0]['commits'], 30)
        self.assertEqual(people[0]['lines'], 360)
        self.assertEqual(people[0]['sleep_window'], [19, 9])
        self.assertIn('asleep', people[0]['status'])
        self.assertIn('awake', people[1]['status'])
        self.assertEqual(people[0]['offset'], 'UTC+09:00')
        self.assertAlmostEqual(sum(p['share'] for p in people), 100)

    def test_outside_repo(self):
        result = self.run_cli(cwd=Path(self.tmp.name))
        self.assertEqual(result.returncode, 2)
        self.assertIn('not inside a git repo', result.stderr)

    def test_absolute_path_from_outside_repo(self):
        # The file's own repo is used, not the cwd — whom works like `git log <path>` from anywhere.
        result = self.run_cli(str(self.repo / 'sample.txt'), cwd=Path(self.tmp.name))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('1. Alice', result.stdout)
        self.assertIn('55 commits by 2 people', result.stdout)

    def test_relative_path_from_subdirectory(self):
        sub = self.repo / 'sub'
        sub.mkdir(exist_ok=True)
        result = self.run_cli('../sample.txt', cwd=sub)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('55 commits by 2 people', result.stdout)

    def test_missing_history(self):
        result = self.run_cli('nonexistent')
        self.assertEqual(result.returncode, 1)
        self.assertIn('no history', result.stderr)

    def test_who_and_no_sleep(self):
        result = self.run_cli('--who', 'ALIC')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('00', result.stdout)
        self.assertIn('23', result.stdout)
        self.assertIn('Histogram', result.stdout)
        self.assertNotIn('2. Bob', result.stdout)
        result = self.run_cli('--no-sleep', '--json')
        self.assertTrue(all(p['sleep_window'] is None for p in json.loads(result.stdout)))
        self.assertNotIn('asleep', result.stdout)


spec = importlib.util.spec_from_file_location('whom', CLI)
assert spec is not None and spec.loader is not None
whom = importlib.util.module_from_spec(spec)
spec.loader.exec_module(whom)


class UnitTests(unittest.TestCase):
    def test_since_and_naive_dates(self):
        now = whom.instant('2024-04-01T00:00:00Z')
        self.assertEqual((now - whom.since_date('2mo', now)).days, 60)
        self.assertEqual((now - whom.since_date('1w', now)).days, 7)
        self.assertEqual(whom.since_date('2024-01-01', now).tzinfo, timezone.utc)
        with self.assertRaises(ValueError):
            whom.since_date('banana', now)

    def test_parser_multiline_and_binary(self):
        raw = ('a' * 40 + '\x1fAlice\x1fa@example.com\x1f2024-03-01T09:00:00+09:00\x1f'
               'subject\n\nCo-authored-by: Bob <b@example.com>\n\x1e\n'
               '12\t3\tfile.txt\n-\t-\timage.png\n\n' +
               'b' * 40 + '\x1fBob\x1fb@example.com\x1f2024-03-02T09:00:00+01:00\x1fnext\n\x1e\n1\t0\tother\n')
        records = list(whom.parse_log(raw))
        self.assertEqual(len(records), 2)
        self.assertEqual([r['lines'] for r in records], [15, 1])
        self.assertIn('Co-authored-by:', records[0]['body'])
        self.assertEqual(records[0]['date'].hour, 9)

    def test_offset_tie_and_recent_twenty(self):
        events = [(whom.instant('2024-03-%02dT12:00:00%s' % (i + 1, '+09:00' if i % 2 else '-05:00')), 1) for i in range(20)]
        events += [(whom.instant('2023-01-01T12:00:00-05:00'), 1)] * 30
        person = whom.profile({'events': events}, whom.instant('2024-04-01T17:00:00Z'))
        self.assertEqual(person['offset'], 'UTC+09:00')
        self.assertEqual(person['varied'], [540, -300])
        self.assertIn('asleep', person['status'])

    def test_insufficient_and_all_hours(self):
        events = [(whom.instant('2024-03-01T12:00:00Z'), 1)]
        self.assertIn('not enough', whom.profile({'events': events}, events[0][0])['status'])
        events = [(datetime(2024, 3, 1, h, tzinfo=timezone.utc), 1) for h in range(24)]
        person = whom.profile({'events': events}, events[0][0])
        self.assertEqual(person['status'], 'commits at all hours')
        self.assertIsNone(person['sleep_window'])


class ExtraIntegrationTests(unittest.TestCase):
    def test_coauthor_mailmap_binary_and_subdirectory(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temp:
            repo = Path(temp)
            def git(*args, env=None):
                return subprocess.run(['git', *args], cwd=repo, env=env, check=True, capture_output=True, text=True)
            git('init', '-q')
            git('config', 'user.name', 'Alias')
            git('config', 'user.email', 'alias@example.com')
            (repo / 'src').mkdir()
            (repo / 'src' / 'file').write_text('one\ntwo\n')
            (repo / 'blob').write_bytes(b'\x00\x01\x02')
            git('add', '.')
            env = dict(os.environ, GIT_AUTHOR_DATE='2024-03-01T12:00:00+09:00', GIT_COMMITTER_DATE='2024-03-01T12:00:00+09:00')
            git('commit', '-qm', 'work\n\nCo-authored-by: Helper <helper@example.com>\nCo-authored-by: Helper <helper@example.com>', env=env)
            (repo / '.mailmap').write_text('Alice <alice@example.com> Alias <alias@example.com>\nBob <bob@example.com> Helper <helper@example.com>\n')
            result = subprocess.run([sys.executable, str(CLI), '--now', '2024-04-01T00:00:00Z', '--json'], cwd=repo / 'src', capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual([p['name'] for p in data], ['Alice', 'Bob'])
            self.assertEqual([p['commits'] for p in data], [1, .5])
            self.assertEqual([p['lines'] for p in data], [2, 1])
            self.assertAlmostEqual(data[0]['share'], 200 / 3)


if __name__ == '__main__':
    unittest.main()

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'whom.py'
TMPDIR = os.environ.get('TMPDIR') or None

# Keep tests independent of a developer's global/system Git configuration.
GIT_ENV = dict(
    os.environ,
    GIT_CONFIG_GLOBAL=os.devnull,
    GIT_CONFIG_SYSTEM=os.devnull,
    GIT_CONFIG_NOSYSTEM='1',
    GIT_TERMINAL_PROMPT='0',
)

spec = importlib.util.spec_from_file_location('whom', CLI)
assert spec is not None and spec.loader is not None
whom = importlib.util.module_from_spec(spec)
spec.loader.exec_module(whom)


def run_git(cwd, *args, env=None, check=True):
    return subprocess.run(
        ['git', *args], cwd=str(cwd), env=dict(GIT_ENV, **(env or {})),
        check=check, capture_output=True, text=True,
    )


def init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    run_git(path, 'init', '-q')
    run_git(path, 'config', 'user.name', 'Fixture')
    run_git(path, 'config', 'user.email', 'fixture@example.com')


def commit(repo, name, email, date, lines=1, message='work', filename='sample.txt'):
    path = repo / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        handle.write('change\n' * lines)
    run_git(repo, 'add', '.')
    env = dict(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    run_git(repo, 'commit', '-q', '-m', message, env=env)


def run_cli(*args, cwd=None, now='2024-04-01T17:00:00Z'):
    return subprocess.run(
        [sys.executable, str(CLI), '--now', now, *args],
        cwd=str(cwd) if cwd is not None else None, env=GIT_ENV, capture_output=True, text=True,
    )


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=TMPDIR)
        cls.repo = Path(cls.tmp.name) / 'repo'
        init_repo(cls.repo)
        for i in range(30):
            commit(cls.repo, 'Alice', 'alice@example.com',
                   '2024-03-%02dT%02d:00:00+09:00' % (1 + i % 20, 9 + i % 10), 12)
        for i in range(25):
            commit(cls.repo, 'Bob', 'bob@example.com',
                   '2024-03-%02dT%02d:00:00+01:00' % (1 + i % 20, 8 + i % 12), 1)
        commit(cls.repo, 'dependabot[bot]', 'bot@example.com', '2024-03-22T12:00:00+00:00', 100)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_rank_and_recommendation(self):
        result = run_cli('sample.txt', cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('1. Alice', result.stdout)
        self.assertIn('(1 bot commit ignored)', result.stdout)
        self.assertIn('probably asleep', result.stdout)
        self.assertIn('Ask Bob', result.stdout)
        self.assertIn('55 commits by 2 people', result.stdout)

    def test_json_shape_and_hours(self):
        result = run_cli('--json', cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        people = json.loads(result.stdout)
        self.assertEqual([p['name'] for p in people], ['Alice', 'Bob'])
        self.assertEqual(set(people[0]), {
            'name', 'email', 'share', 'commits', 'lines', 'last_commit', 'offset', 'local_time',
            'sleep_window', 'active_hours', 'status', 'state', 'sample_size',
        })
        self.assertEqual(people[0]['commits'], 30)
        self.assertEqual(people[0]['lines'], 360)
        self.assertEqual(people[0]['sample_size'], 30)
        self.assertEqual(people[0]['sleep_window'], [19, 9])
        self.assertEqual(people[0]['state'], 'asleep')
        self.assertIn('asleep', people[0]['status'])
        self.assertEqual(people[1]['state'], 'awake')
        self.assertIn('awake', people[1]['status'])
        self.assertEqual(people[0]['offset'], 'UTC+09:00')
        self.assertAlmostEqual(sum(p['share'] for p in people), 100)

    def test_outside_repo(self):
        result = run_cli(cwd=Path(self.tmp.name))
        self.assertEqual(result.returncode, 2)
        self.assertIn('not inside a git repo', result.stderr)

    def test_absolute_path_from_outside_repo(self):
        # The file's own repo is used, not the cwd — whom works like `git log <path>` from anywhere.
        result = run_cli(str(self.repo / 'sample.txt'), cwd=Path(self.tmp.name))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('1. Alice', result.stdout)
        self.assertIn('55 commits by 2 people', result.stdout)

    def test_relative_path_from_subdirectory(self):
        sub = self.repo / 'sub'
        sub.mkdir(exist_ok=True)
        result = run_cli('../sample.txt', cwd=sub)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('55 commits by 2 people', result.stdout)

    def test_omitted_path_from_subdirectory_means_whole_repo(self):
        sub = self.repo / 'sub'
        sub.mkdir(exist_ok=True)
        result = run_cli(cwd=sub)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('55 commits by 2 people', result.stdout)

    def test_missing_history(self):
        result = run_cli('nonexistent', cwd=self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn('no history', result.stderr)

    def test_nested_missing_path(self):
        result = run_cli('does/not/exist.py', cwd=self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn('no history', result.stderr)

    def test_who_and_no_sleep(self):
        result = run_cli('--who', 'ALIC', cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('00', result.stdout)
        self.assertIn('23', result.stdout)
        self.assertIn('Histogram', result.stdout)
        self.assertNotIn('2. Bob', result.stdout)
        result = run_cli('--no-sleep', '--json', cwd=self.repo)
        people = json.loads(result.stdout)
        self.assertTrue(all(p['sleep_window'] is None for p in people))
        self.assertTrue(all(p['active_hours'] == [] for p in people))
        self.assertTrue(all(p['state'] == 'disabled' for p in people))
        self.assertNotIn('asleep', result.stdout)

    def test_who_matches_email(self):
        result = run_cli('--who', 'bob@example.com', '--json', cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([p['name'] for p in json.loads(result.stdout)], ['Bob'])

    def test_who_no_match(self):
        result = run_cli('--who', 'nobody', cwd=self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn('no matching person', result.stderr)

    def test_top_limits_json(self):
        self.assertEqual(len(json.loads(run_cli('--json', '--top', '1', cwd=self.repo).stdout)), 1)
        self.assertEqual(len(json.loads(run_cli('--json', '--top', '2', cwd=self.repo).stdout)), 2)

    def test_invalid_options(self):
        self.assertEqual(run_cli('--top', '0', cwd=self.repo).returncode, 2)
        self.assertEqual(run_cli('--since', 'banana', cwd=self.repo).returncode, 2)

    def test_help_and_version(self):
        help_result = run_cli('--help', cwd=self.repo)
        self.assertEqual(help_result.returncode, 0)
        for flag in ('--since', '--top', '--json', '--who', '--no-sleep', '--now', '--version'):
            self.assertIn(flag, help_result.stdout)
        version_result = run_cli('--version', cwd=self.repo)
        self.assertEqual(version_result.returncode, 0)
        self.assertIn(whom.__version__, version_result.stdout)


class MailmapAndCoauthorTests(unittest.TestCase):
    def test_coauthor_mailmap_from_inside_and_outside_repo(self):
        with tempfile.TemporaryDirectory(dir=TMPDIR) as temp:
            repo = Path(temp) / 'repo'
            init_repo(repo)
            (repo / 'src').mkdir()
            (repo / 'src' / 'file').write_text('one\ntwo\n')
            (repo / 'blob').write_bytes(b'\x00\x01\x02')
            run_git(repo, 'add', '.')
            env = dict(GIT_AUTHOR_NAME='Alias', GIT_AUTHOR_EMAIL='alias@example.com',
                       GIT_AUTHOR_DATE='2024-03-01T12:00:00+09:00',
                       GIT_COMMITTER_DATE='2024-03-01T12:00:00+09:00')
            run_git(repo, 'commit', '-qm',
                    'work\n\nCo-authored-by: Helper <helper@example.com>\nCo-authored-by: Helper <helper@example.com>',
                    env=env)
            (repo / '.mailmap').write_text(
                'Alice <alice@example.com> Alias <alias@example.com>\n'
                'Bob <bob@example.com> Helper <helper@example.com>\n')

            for label, cwd, path in (
                ('inside', repo / 'src', None),
                ('outside', Path(temp), str(repo / 'src')),
            ):
                args = ('--json',) if path is None else ('--json', path)
                result = run_cli(*args, cwd=cwd)
                self.assertEqual(result.returncode, 0, '%s: %s' % (label, result.stderr))
                data = json.loads(result.stdout)
                self.assertEqual([p['name'] for p in data], ['Alice', 'Bob'], label)
                self.assertEqual([p['commits'] for p in data], [1, .5], label)
                self.assertEqual([p['lines'] for p in data], [2, 1], label)
                self.assertAlmostEqual(data[0]['share'], 200 / 3, msg=label)

    def test_bot_coauthor_is_ignored(self):
        with tempfile.TemporaryDirectory(dir=TMPDIR) as temp:
            repo = Path(temp) / 'repo'
            init_repo(repo)
            commit(repo, 'Author', 'author@example.com', '2024-03-01T12:00:00+00:00', 2,
                   'work\n\nCo-authored-by: Robot <bot@example.com>\nCo-authored-by: Human <human@example.com>')
            result = run_cli('--json', cwd=repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            names = {p['name'] for p in json.loads(result.stdout)}
            self.assertEqual(names, {'Author', 'Human'})


class HistoryEdgeTests(unittest.TestCase):
    def test_empty_repo(self):
        with tempfile.TemporaryDirectory(dir=TMPDIR) as temp:
            repo = Path(temp) / 'repo'
            init_repo(repo)
            result = run_cli(cwd=repo)
            self.assertEqual(result.returncode, 1)
            self.assertIn('no history', result.stderr)

    def test_all_bot_history(self):
        with tempfile.TemporaryDirectory(dir=TMPDIR) as temp:
            repo = Path(temp) / 'repo'
            init_repo(repo)
            commit(repo, 'dependabot[bot]', 'bot@example.com', '2024-03-01T12:00:00+00:00', 3)
            text = run_cli(cwd=repo)
            self.assertEqual(text.returncode, 0, text.stderr)
            self.assertIn('0 commits by 0 people', text.stdout)
            self.assertIn('1 bot commit ignored', text.stdout)
            data = run_cli('--json', cwd=repo)
            self.assertEqual(data.returncode, 0, data.stderr)
            self.assertEqual(json.loads(data.stdout), [])

    def test_no_history_in_range(self):
        with tempfile.TemporaryDirectory(dir=TMPDIR) as temp:
            repo = Path(temp) / 'repo'
            init_repo(repo)
            commit(repo, 'Alice', 'alice@example.com', '2024-03-01T12:00:00+00:00')
            result = run_cli('--since', '1d', cwd=repo)
            self.assertEqual(result.returncode, 1)
            self.assertIn('no history in the selected time range', result.stderr)

    def test_since_filters_monotonic_history(self):
        with tempfile.TemporaryDirectory(dir=TMPDIR) as temp:
            repo = Path(temp) / 'repo'
            init_repo(repo)
            for day in range(1, 6):
                commit(repo, 'Alice', 'alice@example.com',
                       '2024-03-%02dT12:00:00+00:00' % day)
            result = run_cli('--since', '2024-03-04', '--json', cwd=repo)
            self.assertEqual(result.returncode, 0, result.stderr)
            people = json.loads(result.stdout)
            self.assertEqual([p['name'] for p in people], ['Alice'])
            self.assertEqual(people[0]['commits'], 2)


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

    def test_parser_streaming_chunks(self):
        raw = ('a' * 40 + '\x1fAlice\x1fa@example.com\x1f2024-03-01T09:00:00+09:00\x1f'
               'subject\n\nCo-authored-by: Bob <b@example.com>\n\x1e\n'
               '12\t3\tfile.txt\n-\t-\timage.png\n\n' +
               'b' * 40 + '\x1fBob\x1fb@example.com\x1f2024-03-02T09:00:00+01:00\x1fnext\n\x1e\n1\t0\tother\n')
        records = list(whom.parse_log(whom.read_chunks(io.StringIO(raw))))
        self.assertEqual(len(records), 2)
        self.assertEqual([r['lines'] for r in records], [15, 1])
        self.assertEqual([r['name'] for r in records], ['Alice', 'Bob'])

    def test_offset_tie_and_recent_twenty(self):
        events = [(whom.instant('2024-03-%02dT12:00:00%s' % (i + 1, '+09:00' if i % 2 else '-05:00')), 1)
                  for i in range(20)]
        events += [(whom.instant('2023-01-01T12:00:00-05:00'), 1)] * 30
        person = whom.profile({'events': events}, whom.instant('2024-04-01T17:00:00Z'))
        self.assertEqual(person['offset'], 'UTC+09:00')
        self.assertEqual(person['varied'], [540, -300])
        self.assertIn('asleep', person['status'])

    def test_offset_ignores_coauthor_events(self):
        authored = [(whom.instant('2024-01-01T12:00:00-05:00'), 1.0)]
        inherited = [(whom.instant('2024-03-%02dT12:00:00+09:00' % (i + 1)), .5) for i in range(20)]
        person = whom.profile({'events': authored + inherited}, whom.instant('2024-04-01T00:00:00Z'))
        self.assertEqual(person['offset'], 'UTC-05:00')
        self.assertEqual(person['varied'], [-300])
        self.assertEqual(person['last_commit'], '2024-01-01T12:00:00-05:00')
        self.assertEqual(person['sample_size'], 21)

    def test_offset_falls_back_for_coauthor_only(self):
        events = [(whom.instant('2024-03-%02dT12:00:00+09:00' % (i + 1)), .5) for i in range(20)]
        person = whom.profile({'events': events}, whom.instant('2024-04-01T00:00:00Z'))
        self.assertEqual(person['offset'], 'UTC+09:00')
        self.assertEqual(person['varied'], [540])

    def test_insufficient_and_all_hours(self):
        events = [(whom.instant('2024-03-01T12:00:00Z'), 1)]
        person = whom.profile({'events': events}, events[0][0])
        self.assertIn('not enough', person['status'])
        self.assertEqual(person['state'], 'insufficient')
        self.assertEqual(person['active_hours'], [])
        events = [(datetime(2024, 3, 1, h, tzinfo=timezone.utc), 1) for h in range(24)]
        person = whom.profile({'events': events}, events[0][0])
        self.assertEqual(person['status'], 'commits at all hours')
        self.assertEqual(person['state'], 'all_hours')
        self.assertIsNone(person['sleep_window'])

    def test_active_hours_wrap_and_gaps(self):
        events = [(datetime(2024, 3, 1 + i % 5, h, tzinfo=timezone.utc), 1)
                  for h in (0, 1, 22, 23) for i in range(5)]
        person = whom.profile({'events': events}, datetime(2024, 4, 1, 23, 30, tzinfo=timezone.utc))
        self.assertEqual(person['state'], 'awake')
        self.assertIn('22:00-02:00', person['status'])

        events = [(datetime(2024, 3, 1 + i % 2, h, tzinfo=timezone.utc), 1)
                  for h in list(range(0, 5)) + list(range(9, 20)) for i in range(2)]
        person = whom.profile({'events': events}, datetime(2024, 4, 1, 12, tzinfo=timezone.utc))
        self.assertIn('00:00-05:00, 09:00-20:00', person['status'])

    def test_sleep_window_tie_prefers_lowest_start(self):
        events = [(datetime(2024, 3, 1 + day, h, tzinfo=timezone.utc), 1)
                  for day in range(2) for h in range(24) if h not in (5, 6, 7, 8, 20, 21, 22, 23)]
        person = whom.profile({'events': events}, datetime(2024, 4, 1, 12, tzinfo=timezone.utc))
        self.assertEqual(person['sleep_window'], [5, 9])

    def test_coauthors_only_uses_final_paragraph(self):
        body = ('subject\n\nExample:\nCo-authored-by: Fake <fake@example.com>\n\n'
                'Co-authored-by: Real <real@example.com>\n')
        self.assertEqual(whom.coauthors(body), [('Real', 'real@example.com')])

    def test_hour_ranges_and_labels(self):
        self.assertEqual(whom.hour_ranges([0, 1, 22, 23]), [(22, 26)])
        self.assertEqual(whom.active_label([0, 1, 22, 23]), '22:00-02:00')
        self.assertEqual(whom.hour_ranges(list(range(24))), [(0, 24)])
        self.assertEqual(whom.active_label(list(range(24))), 'all hours')

    def test_human_number_and_plural(self):
        self.assertEqual(whom.human_number(1234567), '1,234,567')
        self.assertEqual(whom.human_number(1.5), '1.5')
        self.assertEqual(whom.human_number(1.0), '1')
        self.assertEqual(whom.plural(1, 'commit'), 'commit')
        self.assertEqual(whom.plural(2, 'commit'), 'commits')
        self.assertEqual(whom.plural(2, 'person', 'people'), 'people')

    def test_clean_control_characters(self):
        self.assertEqual(whom.clean('a\x1b[31mb\nc'), 'a?[31mb?c')

    def test_tiny_share_prints_less_than_one_percent(self):
        person = dict(name='A', email='a@example.com', share=.4, commits=1, lines=1,
                      last_commit='2024-03-01T00:00:00Z', local_time='2024-04-01T00:00:00Z',
                      offset='UTC+00:00', status='testing', varied=[], sample_size=1,
                      histogram=[0] * 24)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            whom.print_person(person, 1, whom.instant('2024-04-01T00:00:00Z'))
        self.assertIn('<1%', output.getvalue())

    def test_offset_label_negative(self):
        self.assertEqual(whom.offset_label(-330), '-05:30')
        self.assertEqual(whom.offset_label(330, 'UTC'), 'UTC+05:30')

    def test_ago_months_and_years(self):
        now = whom.instant('2024-04-01T00:00:00Z')
        self.assertEqual(whom.ago('2024-02-01T00:00:00Z', now), '2 months ago')
        self.assertEqual(whom.ago('2023-04-01T00:00:00Z', now), '1 year ago')


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""Find likely contributors and cautiously guess their local hours."""
import argparse
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

__version__ = '0.2.0'

UTC = timezone.utc
BOT = re.compile(r'\b(bot|dependabot|renovate|github-actions|noreply)\b|\[bot\]', re.I)
COAUTHOR = re.compile(r'^Co-authored-by:\s*(.+?)\s*<([^<>]+)>\s*$', re.I | re.M)
HEADER = re.compile(r'(?:^|\n)([0-9a-f]{40,64})\x1f')
PARAGRAPH = re.compile(r'\r?\n[ \t]*\r?\n')
CONTROL = re.compile(r'[\x00-\x1f\x7f-\x9f]')


def clean(value):
    """Make untrusted single-line text safe for terminal output."""
    return CONTROL.sub('?', str(value))


def plural(value, singular, plural_form=None):
    return singular if value == 1 else (plural_form or singular + 's')


def human_number(value):
    if value == int(value):
        return '{:,}'.format(int(value))
    return '{:,.1f}'.format(value)


def instant(value):
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('expected an ISO8601 date or timestamp: ' + value)
    return date.replace(tzinfo=UTC) if date.tzinfo is None else date


def since_date(value, now):
    match = re.fullmatch(r'(\d+)(s|d|w|mo|y)', value)
    if match:
        seconds = {'s': 1, 'd': 86400, 'w': 604800, 'mo': 30 * 86400, 'y': 365 * 86400}
        return now - timedelta(seconds=int(match[1]) * seconds[match[2]])
    return instant(value)


def git(*args, cwd=None):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace')


def git_log(root, since, paths):
    command = [
        'git', 'log', '--since=' + since.isoformat(), '--use-mailmap', '--no-merges',
        '--no-color', '--no-show-signature', '--no-ext-diff', '--no-textconv',
        '--format=%H%x1f%aN%x1f%aE%x1f%ad%x1f%B%x1e', '--date=iso-strict', '--numstat', '--', *paths,
    ]
    return subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding='utf-8', errors='replace')


def locate(paths):
    """Return (repo_root, paths relative to it); the first real path anchors the repo, else cwd."""
    anchor = None
    for path in paths:
        if not path.startswith(':'):  # leave git pathspec magic such as ':/' alone
            absolute = os.path.abspath(path)
            anchor = absolute if os.path.isdir(absolute) else os.path.dirname(absolute)
            while anchor and not os.path.isdir(anchor):
                parent = os.path.dirname(anchor)
                if parent == anchor:
                    break
                anchor = parent
            break
    check = git('rev-parse', '--show-toplevel', cwd=anchor)
    if check.returncode:
        return None, paths
    root = os.path.realpath(check.stdout.strip())
    rel = [p if p.startswith(':') else os.path.relpath(os.path.realpath(os.path.abspath(p)), root) for p in paths]
    return root, rel


def numstat_lines(text):
    lines = 0
    for line in text.splitlines():
        fields = line.split('\t', 2)
        if len(fields) == 3:
            lines += sum(int(n) for n in fields[:2] if n.isdigit())
    return lines


def parse_log(source):
    """Yield commit records from full git output or an iterable of separator-delimited chunks."""
    chunks = source.split('\x1e') if isinstance(source, str) else source
    pending = None
    for chunk in chunks:
        match = HEADER.search(chunk)
        stats = chunk[:match.start()] if match else chunk
        if pending is not None:
            pending['lines'] = numstat_lines(stats)
            yield pending
            pending = None
        if match:
            fields = chunk[match.start():].lstrip('\n').split('\x1f', 4)
            if len(fields) == 5:
                pending = dict(hash=fields[0], name=fields[1], email=fields[2], date=instant(fields[3]),
                               body=fields[4], lines=0)
    if pending is not None:
        yield pending


def read_chunks(stream, size=65536):
    """Split a git log stream on the record separator without buffering the whole output."""
    buffer = ''
    while True:
        data = stream.read(size)
        if not data:
            break
        buffer += data
        parts = buffer.split('\x1e')
        buffer = parts.pop()
        for part in parts:
            yield part
    if buffer:
        yield buffer


def coauthors(body):
    """Return coauthor trailers from the final trailer paragraph only."""
    block = PARAGRAPH.split(body.strip())[-1]
    return COAUTHOR.findall(block)


def offset_label(minutes, prefix=''):
    return '{}{}{:02d}:{:02d}'.format(prefix, '+' if minutes >= 0 else '-', abs(minutes) // 60, abs(minutes) % 60)


def hour_ranges(hours):
    """Merge active hours into ranges, joining a run that wraps past midnight."""
    if not hours:
        return []
    if len(hours) == 24:
        return [(0, 24)]
    ranges = []
    start = previous = hours[0]
    for hour in hours[1:]:
        if hour == previous + 1:
            previous = hour
        else:
            ranges.append((start, previous + 1))
            start = previous = hour
    ranges.append((start, previous + 1))
    if len(ranges) > 1 and ranges[0][0] == 0 and ranges[-1][1] == 24:
        first = ranges.pop(0)
        last = ranges.pop()
        ranges.append((last[0], first[1] + 24))
    return sorted(ranges)


def active_label(hours):
    ranges = hour_ranges(hours)
    if not ranges:
        return 'unknown'
    if ranges == [(0, 24)]:
        return 'all hours'
    return ', '.join('{:02d}:00-{:02d}:00'.format(start % 24, end % 24) for start, end in ranges)


def sleep_window(histogram):
    best = None
    length = 0
    for start in range(24):
        if histogram[start] or not histogram[(start - 1) % 24]:
            continue
        run = 0
        while run < 24 and not histogram[(start + run) % 24]:
            run += 1
        if run >= 4 and run > length:
            best, length = [start, (start + run) % 24], run
    return best


def inside(hour, window):
    start, end = window
    return (hour - start) % 24 < (end - start) % 24


def profile(person, now, no_sleep=False):
    events = sorted(person['events'], key=lambda event: event[0], reverse=True)
    # Coauthor events inherit the primary author's timestamp, so they are only a
    # fallback for people who have never authored a commit themselves.
    authored = [date for date, credit in events if credit == 1.0]
    source = authored or [date for date, _ in events]
    offsets = [int(date.utcoffset().total_seconds() // 60) for date in source]
    recent = offsets[:20]
    counts = Counter(recent)
    offset = max(recent, key=lambda item: counts[item])
    local = now.astimezone(timezone(timedelta(minutes=offset))).replace(microsecond=0)
    histogram = [0] * 24
    for date, _ in events:
        histogram[date.hour] += 1
    window = None
    active = []
    if no_sleep:
        state = 'disabled'
        status = 'hours inference disabled'
    elif len(events) < 20:
        state = 'insufficient'
        status = 'not enough history to guess their hours'
    else:
        active = [hour for hour, count in enumerate(histogram) if count / len(events) >= .02]
        window = sleep_window(histogram)
        if window is None:
            state = 'all_hours'
            status = 'commits at all hours'
        elif inside(local.hour, window):
            state = 'asleep'
            status = 'probably asleep (never committed between {:02d}:00 and {:02d}:00)'.format(*window)
        elif local.hour in active:
            state = 'awake'
            status = 'probably awake (usually active {})'.format(active_label(active))
        else:
            state = 'outside'
            status = 'outside their usual hours'
    person.update(offset=offset_label(offset, 'UTC'), local_time=local.isoformat(), sleep_window=window,
                  active_hours=active, status=status, state=state, sample_size=len(events),
                  histogram=histogram, varied=list(dict.fromkeys(offsets)), last_commit=source[0].isoformat())
    return person


def analyze(records, now, no_sleep, root):
    people = {}
    bots = 0
    commits = 0
    record_count = 0
    mailmap = {}
    for record in records:
        record_count += 1
        if BOT.search(record['name'] + ' ' + record['email']):
            bots += 1
            continue
        commits += 1
        credits = [(record['name'], record['email'], 1.0)]
        seen = {record['email'].lower()}
        for name, email in coauthors(record['body']):
            if email.lower() in seen:
                continue
            key = (name, email)
            if key not in mailmap:
                mapped = git('check-mailmap', '--', '{} <{}>'.format(name, email), cwd=root)
                canonical = (name, email)
                match = re.fullmatch(r'(.*?)\s*<([^<>]+)>', mapped.stdout.strip()) if mapped.returncode == 0 else None
                if match:
                    canonical = match.groups()
                mailmap[key] = canonical
            name, email = mailmap[key]
            if email.lower() not in seen and not BOT.search(name + ' ' + email):
                credits.append((name, email, .5))
                seen.add(email.lower())
        date = record['date']
        age = max(0, (now - date).total_seconds() / 86400)
        weight = .5 ** (age / 180)
        for name, email, credit in credits:
            person = people.setdefault(email.lower(), dict(name=name, email=email, commits=0, lines=0,
                                                          score=0, events=[]))
            person['commits'] += credit
            person['lines'] += record['lines'] * credit
            person['score'] += credit * weight * (.6 + .4 * math.log1p(record['lines']))
            person['events'].append((date, credit))
    ordered = sorted(people.values(), key=lambda p: (-p['score'], p['name'], p['email']))
    total = sum(p['score'] for p in ordered)
    for person in ordered:
        person['share'] = person['score'] / total * 100 if total else 0
        profile(person, now, no_sleep)
    return ordered, commits, bots, record_count


def ago(date, now):
    days = max(0, int((now - instant(date)).total_seconds() // 86400))
    if not days:
        return 'today'
    if days < 14:
        return '{} {} ago'.format(days, plural(days, 'day'))
    if days < 60:
        weeks = days // 7
        return '{} {} ago'.format(weeks, plural(weeks, 'week'))
    if days < 365:
        months = days // 30
        return '{} {} ago'.format(months, plural(months, 'month'))
    years = days // 365
    return '{} {} ago'.format(years, plural(years, 'year'))


def print_person(person, index, now, full=False):
    rounded_share = int(round(person['share']))
    share = '{}%'.format(rounded_share) if rounded_share >= 1 else '<1%'
    print('{}. {} <{}>  {}  · {} {} · last touched {}'.format(
        index, clean(person['name']), clean(person['email']), share, human_number(person['commits']),
        plural(person['commits'], 'commit'), ago(person['last_commit'], now)))
    local = instant(person['local_time'])
    print('   their time: {} ({}) · {}'.format(local.strftime('%a %H:%M'), person['offset'], person['status']))
    if len(person['varied']) > 1:
        print('   (offset has varied: {})'.format(', '.join(offset_label(o) for o in person['varied'])))
    if full:
        print('   {} credited {} changed · {} credited {} · last commit {}'.format(
            human_number(person['lines']), plural(person['lines'], 'line'),
            human_number(person['sample_size']), plural(person['sample_size'], 'event'), person['last_commit']))
        ramp = ' ▁▂▃▄▅▆▇█'
        maximum = max(person['histogram']) or 1
        spark = ''.join(ramp[max(1, round(n / maximum * 8))] if n else ' ' for n in person['histogram'])
        print('   Histogram (author-local commit hours, 00–23): |{}|'.format(spark))
        print('   Counts: ' + ' '.join('{:02d}={}'.format(h, n) for h, n in enumerate(person['histogram'])))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Examples:\n'
               '  whom src/billing.py\n'
               '  whom src/ tests/ --since 6mo\n'
               '  whom --who alice --json\n'
               '\n'
               'Sleep/awake inference is an uncertain convenience, not presence detection.\n'
               'Use --no-sleep to disable it.',
    )
    parser.add_argument('paths', nargs='*', metavar='PATH',
                        help='files or directories to inspect (default: the whole repository)')
    parser.add_argument('--since', default='2y', help='Ns/Nd/Nw/Nmo/Ny or ISO date (default: 2y)')
    parser.add_argument('--top', type=int, default=3, help='number of people to show (default: 3)')
    parser.add_argument('--json', action='store_true', help='emit a JSON array instead of text')
    parser.add_argument('--who', metavar='NAME', help='show details for the best name or email match')
    parser.add_argument('--no-sleep', action='store_true', help='disable sleep/awake inference')
    parser.add_argument('--now', metavar='ISO8601', help='override the current instant')
    parser.add_argument('--version', action='version', version='%(prog)s ' + __version__)
    args = parser.parse_args(argv)
    if args.top < 1:
        parser.error('--top must be at least 1')
    try:
        now = instant(args.now).astimezone(UTC) if args.now else datetime.now(UTC)
        since = since_date(args.since, now)
    except (ValueError, OverflowError) as exc:
        parser.error(str(exc))
    try:
        root, paths = locate(args.paths)
        if root is None:
            print('whom: not inside a git repo', file=sys.stderr)
            return 2
        # An omitted path means the whole repository, even from a subdirectory.
        paths = paths or [':/']
        process = git_log(root, since, paths)
        try:
            people, count, bots, records = analyze(
                parse_log(read_chunks(process.stdout)), now, args.no_sleep, root)
            stderr = process.stderr.read()
            returncode = process.wait()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        if returncode:
            empty = git('rev-parse', '--verify', 'HEAD', cwd=root).returncode != 0
            detail = stderr.strip() or 'git log exited with status {}'.format(returncode)
            print('whom: path has no history' if empty else 'whom: git log failed: ' + detail, file=sys.stderr)
            return 1
        if not records:
            print('whom: path has no history in the selected time range', file=sys.stderr)
            return 1
    except OSError as exc:
        print('whom: unable to run git: ' + str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print('whom: invalid git history data: ' + str(exc), file=sys.stderr)
        return 1
    selected = people[:args.top]
    if args.who:
        needle = args.who.casefold()
        matches = [p for p in people if needle in p['name'].casefold() or needle in p['email'].casefold()]
        if not matches:
            print('whom: no matching person: ' + clean(args.who), file=sys.stderr)
            return 1
        selected = matches[:1]
    if args.json:
        keys = ('name', 'email', 'share', 'commits', 'lines', 'last_commit', 'offset', 'local_time',
                'sleep_window', 'active_hours', 'status', 'state', 'sample_size')
        print(json.dumps([{key: p[key] for key in keys} for p in selected], ensure_ascii=False, indent=2))
        return 0
    period = ('in the last ' + args.since) if re.fullmatch(r'\d+(s|d|w|mo|y)', args.since) else ('since ' + args.since)
    bot_note = '{} {} ignored'.format(human_number(bots), plural(bots, 'bot commit'))
    print('{} — {} {} by {} {} {} ({})\n'.format(
        ', '.join(clean(p) for p in args.paths) or '.', human_number(count), plural(count, 'commit'),
        human_number(len(people)), plural(len(people), 'person', 'people'), period, bot_note))
    for index, person in enumerate(selected, 1):
        print_person(person, index, now, bool(args.who))
    if people and not args.who and people[0]['state'] in ('asleep', 'outside'):
        awake = next((p for p in people[1:3] if p['state'] == 'awake'), None)
        if awake:
            top = people[0]
            if top['state'] == 'asleep' and top['sleep_window']:
                local = instant(top['local_time'])
                hours = ((top['sleep_window'][1] - local.hour) % 24) - local.minute / 60
                reason = 'is probably asleep for another ~{}h'.format(math.ceil(hours))
            else:
                reason = 'is outside their usual hours'
            print('\nAsk {} — {} {}.'.format(clean(awake['name']), clean(top['name']), reason))
    return 0


def cli(argv=None):
    try:
        return main(argv)
    except BrokenPipeError:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            pass
        return 0


if __name__ == '__main__':
    sys.exit(cli())

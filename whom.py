#!/usr/bin/env python3
"""Find knowledgeable contributors and cautiously guess their local hours."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
import os
import re
import subprocess
import sys

UTC = timezone.utc
BOT = re.compile(r'\b(bot|dependabot|renovate|github-actions|noreply)\b|\[bot\]', re.I)
COAUTHOR = re.compile(r'^Co-authored-by:\s*(.+?)\s*<([^<>]+)>\s*$', re.I | re.M)
HEADER = re.compile(r'(?:^|\n)([0-9a-f]{40,64})\x1f')


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


def locate(paths):
    """Return (repo_root, paths relative to it); the first real path anchors the repo, else cwd."""
    anchor = None
    for path in paths:
        if not path.startswith(':'):  # leave git pathspec magic such as ':/' alone
            absolute = os.path.abspath(path)
            anchor = absolute if os.path.isdir(absolute) else os.path.dirname(absolute)
            break
    check = git('rev-parse', '--show-toplevel', cwd=anchor)
    if check.returncode:
        return None, paths
    root = os.path.realpath(check.stdout.strip())
    rel = [p if p.startswith(':') else os.path.relpath(os.path.realpath(os.path.abspath(p)), root) for p in paths]
    return root, rel


def parse_log(text):
    """The record separator ends %B; numstat follows it, before the next header."""
    pending = None
    for chunk in text.split('\x1e'):
        match = HEADER.search(chunk)
        stats = chunk[:match.start()] if match else chunk
        if pending is not None:
            lines = 0
            for line in stats.splitlines():
                fields = line.split('\t', 2)
                if len(fields) == 3:
                    lines += sum(int(n) for n in fields[:2] if n.isdigit())
            pending['lines'] = lines
            yield pending
            pending = None
        if match:
            fields = chunk[match.start():].lstrip('\n').split('\x1f', 4)
            if len(fields) == 5:
                pending = dict(hash=fields[0], name=fields[1], email=fields[2], date=instant(fields[3]), body=fields[4])


def offset_label(minutes, prefix=''):
    return '{}{}{:02d}:{:02d}'.format(prefix, '+' if minutes >= 0 else '-', abs(minutes) // 60, abs(minutes) % 60)


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
    offsets = [int(date.utcoffset().total_seconds() // 60) for date, _ in events]
    recent = offsets[:20]
    counts = Counter(recent)
    offset = max(recent, key=lambda item: counts[item])
    local = now.astimezone(timezone(timedelta(minutes=offset)))
    histogram = [0] * 24
    for date, _ in events:
        histogram[date.hour] += 1
    active = [hour for hour, count in enumerate(histogram) if count / len(events) >= .02]
    window = None
    if no_sleep:
        status = 'hours inference disabled'
        active = []
    elif len(events) < 20:
        status = 'not enough history to guess their hours'
    else:
        window = sleep_window(histogram)
        if window is None:
            status = 'commits at all hours'
        elif inside(local.hour, window):
            status = 'probably asleep (never committed between {:02d}:00 and {:02d}:00)'.format(*window)
        elif local.hour in active:
            # The range denotes the enclosing span; the histogram can have gaps.
            status = 'probably awake (usually active {:02d}:00-{:02d}:00)'.format(active[0], (active[-1] + 1) % 24)
        else:
            status = 'outside their usual hours'
    person.update(offset=offset_label(offset, 'UTC'), local_time=local.isoformat(), sleep_window=window,
                  active_hours=active, status=status, histogram=histogram,
                  varied=list(dict.fromkeys(offsets)), last_commit=events[0][0].isoformat())
    return person


def analyze(records, now, no_sleep):
    people = {}
    bots = 0
    commits = 0
    for record in records:
        if BOT.search(record['name'] + ' ' + record['email']):
            bots += 1
            continue
        commits += 1
        credits = [(record['name'], record['email'], 1.0)]
        seen = {record['email'].lower()}
        for name, email in COAUTHOR.findall(record['body']):
            mapped = git('check-mailmap', '{} <{}>'.format(name, email))
            match = re.fullmatch(r'(.*?)\s*<([^<>]+)>', mapped.stdout.strip()) if mapped.returncode == 0 else None
            if match:
                name, email = match.groups()
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
    return ordered, commits, bots


def ago(date, now):
    days = max(0, int((now - instant(date)).total_seconds() // 86400))
    if not days:
        return 'today'
    if days < 14:
        return '{} day{} ago'.format(days, '' if days == 1 else 's')
    if days < 60:
        return '{} weeks ago'.format(days // 7)
    return '{} days ago'.format(days)


def print_person(person, index, now, full=False):
    print('{}. {} <{}>  {:.0f}%  · {:g} commits · last touched {}'.format(
        index, person['name'], person['email'], person['share'], person['commits'], ago(person['last_commit'], now)))
    local = instant(person['local_time'])
    print('   their time: {} ({}) · {}'.format(local.strftime('%a %H:%M'), person['offset'], person['status']))
    if len(person['varied']) > 1:
        print('   (offset has varied: {})'.format(', '.join(offset_label(o) for o in person['varied'])))
    if full:
        print('   {:g} credited lines changed · last commit {}'.format(person['lines'], person['last_commit']))
        ramp = ' ▁▂▃▄▅▆▇█'
        maximum = max(person['histogram']) or 1
        spark = ''.join(ramp[max(1, round(n / maximum * 8))] if n else ' ' for n in person['histogram'])
        print('   Histogram (author-local commit hours, 00–23): |{}|'.format(spark))
        print('   Counts: ' + ' '.join('{:02d}={}'.format(h, n) for h, n in enumerate(person['histogram'])))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', nargs='*', metavar='PATH')
    parser.add_argument('--since', default='2y', help='Ns/Nd/Nw/Nmo/Ny or ISO date (default: 2y)')
    parser.add_argument('--top', type=int, default=3)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--who', metavar='NAME')
    parser.add_argument('--no-sleep', action='store_true')
    parser.add_argument('--now', metavar='ISO8601')
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
        result = git('log', '--since=' + since.isoformat(), '--use-mailmap', '--no-merges',
                     '--format=%H%x1f%aN%x1f%aE%x1f%ad%x1f%B%x1e', '--date=iso-strict', '--numstat', '--', *paths, cwd=root)
        if result.returncode:
            empty = git('rev-parse', '--verify', 'HEAD', cwd=root).returncode != 0
            print('whom: path has no history' if empty else 'whom: git log failed: ' + result.stderr.strip(), file=sys.stderr)
            return 1
        records = list(parse_log(result.stdout))
        if not records:
            print('whom: path has no history in the selected time range', file=sys.stderr)
            return 1
        people, count, bots = analyze(records, now, args.no_sleep)
    except OSError as exc:
        print('whom: unable to run git: ' + str(exc), file=sys.stderr)
        return 2
    selected = people[:args.top]
    if args.who:
        matches = [p for p in people if args.who.casefold() in p['name'].casefold()]
        if not matches:
            print('whom: no matching person: ' + args.who, file=sys.stderr)
            return 1
        selected = matches[:1]
    if args.json:
        keys = ('name', 'email', 'share', 'commits', 'lines', 'last_commit', 'offset', 'local_time',
                'sleep_window', 'active_hours', 'status')
        print(json.dumps([{key: p[key] for key in keys} for p in selected], ensure_ascii=False, indent=2))
        return 0
    period = ('in the last ' + args.since) if re.fullmatch(r'\d+(s|d|w|mo|y)', args.since) else ('since ' + args.since)
    print('{} — {} commits by {} people {} ({} bot commits ignored)\n'.format(
        ', '.join(args.paths) or '.', count, len(people), period, bots))
    for index, person in enumerate(selected, 1):
        print_person(person, index, now, bool(args.who))
    if people and not args.who and (people[0]['status'].startswith('probably asleep') or people[0]['status'] == 'outside their usual hours'):
        awake = next((p for p in people[1:3] if p['status'].startswith('probably awake')), None)
        if awake:
            top = people[0]
            if top['sleep_window']:
                local = instant(top['local_time'])
                hours = ((top['sleep_window'][1] - local.hour) % 24) - local.minute / 60
                reason = 'is probably asleep for another ~{}h'.format(math.ceil(hours)) if top['status'].startswith('probably asleep') else 'is outside their usual hours'
            else:
                reason = 'is outside their usual hours'
            print('\nAsk {} — {} {}.'.format(awake['name'], top['name'], reason))
    return 0


if __name__ == '__main__':
    sys.exit(main())

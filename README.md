# whom

Who should I ask about this file, and are they awake right now?

`whom` ranks contributors from Git history, shows their inferred current local time, and cautiously guesses whether they are awake. Python 3.9+ and Git are required. Runtime dependencies: standard library only. It runs locally, reads history, changes nothing in the repository, and sends no data anywhere.

## Install and run

```sh
pipx install .
whom src/billing.py
# Or, without installation:
python3 whom.py src/billing.py
```

Point it at any file or directory, from anywhere: the repository is found from the first path given (falling back to the current directory when no path is given), so `whom ~/src/foo/bar.py` works from your home directory (plain `git log` would refuse).

```text
whom [PATH ...] [--since 2y] [--top 3] [--json] [--who NAME]
     [--no-sleep] [--now ISO8601]
```

```sh
whom src/ tests/ --since 6mo
whom --who alice
whom src/ --json --top 10
whom --no-sleep
whom --since 2024-01-01 --now 2024-04-01T17:00:00Z
```

Omitted paths select the whole repository, even when called from a subdirectory. Explicit paths are resolved from the current directory and may point into a different repository than the one you are standing in (all paths in one call must belong to the same repository); directories aggregate their files, and overlapping paths do not double-count commits. Git pathspec syntax is supported. History is HEAD-reachable, non-merge history; renamed paths are not followed automatically.

`--since` accepts an ISO date/timestamp or an integer followed by `s`, `d`, `w`, `mo`, or `y`. Months mean 30 days and years 365 days. Naive timestamps mean UTC. `--now` overrides the current instant and the relative cutoff anchor; it is not a historical upper-bound filter. Git's `--since` filters by committer time, whereas scoring uses author time. Future author timestamps are treated as age zero.

Exit codes: 0 for success (including all-bot history), 1 for no selected history/no matching person or a Git log error, 2 for invalid arguments, missing Git, or running outside a repository.

## Ranking

For each commit, `w = 0.5 ** (age_days / 180)`; its score is
`0.6 * w + 0.4 * w * log1p(added_lines + deleted_lines)`.
Each person's share is their score divided by everyone's total score, as a percentage. Binary numstat entries contribute zero lines. Recent work and meaningful changes rank higher, but lines changed are not expertise.

Primary authors receive full credit. Unique `Co-authored-by: Name <email>` entries receive half credit for commits, lines and score. Identity is grouped by case-insensitive email, after Git mailmap normalization; the primary author is not counted again as their own coauthor. The header counts distinct non-bot commits, not fractional person credits. Human coauthors of bot-authored commits are excluded with those commits. Bot-like names/emails matching `\b(bot|dependabot|renovate|github-actions|noreply)\b|\[bot\]` are ignored, case-insensitively. The text header reports ignored primary-author bot commits.

## How the sleep/awake inference works

**This infers people's sleep from commit timestamps, and that is a little creepy.** It is an uncertain convenience, not presence detection. Don't use it to monitor staff, assess productivity, or decide someone owes you a reply. Use `--no-sleep` to disable hours inference (local offset/time is still shown).

* Each author timestamp carries a UTC offset. The most common offset in their 20 newest commits is used for their current local time; ties favor the newest occurrence. Varying historical offsets are listed. Offsets are never mapped to IANA zone names, and daylight-saving transitions are not predicted.
* Commit hours are bucketed into 24 bins using **each timestamp's own local offset**. Coauthor events inherit the primary author's timestamp/offset because trailers contain neither: this can be very misleading across time zones. Each credited event counts once in the histogram and minimum-history threshold, even when its ranking credit is half.
* Fewer than 20 events: `not enough history to guess their hours`.
* The longest circular run of zero-commit hours, at least four hours long, is the guessed sleep window. The end is exclusive. Equal-length runs favor the lowest starting hour. No such gap produces `commits at all hours` (this means no long zero-commit gap, not literally activity every hour).
* Hours containing at least 2% of events are active. If local time is in the sleep window: `probably asleep`. Otherwise, an active hour means `probably awake`; anything else means `outside their usual hours`. The displayed active range encloses active buckets and can contain gaps; `--who` shows the exact histogram.
* If the leader is asleep/outside their hours and someone else among the top three is probably awake, an `Ask ...` line suggests that person. Sleep time remaining is rounded up to the guessed window end, not a reliable wake-up prediction.

## Output

Default: the top three profiles with score shares, credited commits, recency, local time, and cautious status. `--top N` changes the number shown; shares always use all contributors as their denominator. `--who NAME` selects the highest-ranked case-insensitive name substring match and adds credited lines, last timestamp, and a 24-hour text sparkline plus exact counts. It selects only one person even if multiple names match.

`--json` emits just a list (no prose) of the selected profiles. Keys: `name`, `email`, `share` (0–100 percentage), `commits`, `lines`, `last_commit` (ISO8601), `offset` (`UTC±HH:MM`), `local_time` (offset-aware ISO8601), `sleep_window` (`[start_hour, end_hour]` or null), `active_hours`, `status`. Commit/line credits can be fractional. `--no-sleep` makes the window null, active hours empty and status `hours inference disabled`. All-bot history produces `[]`. Bot counts and varied-offset notes are text-only to preserve this JSON shape.

## Failure modes

* CI bots and human-looking automation can slip through; the deliberately broad `noreply` exclusion can also hide real GitHub users.
* Squash merges concentrate credit and timing on the squash author; omitted merge commits can hide useful work. Copied code, bulk formatting, generated files and vendored files can skew ranking.
* People who moved time zones or traveled, shared accounts, DST changes, rewritten dates and asynchronous patches undermine local time and sleep guesses. A timestamp is not a presence signal.
* Sparse/solo repos offer no useful alternative person. No sleep gap does not establish round-the-clock availability. Work schedules are not necessarily sleep schedules.
* `.mailmap` helps merge aliases, but missing/incorrect mappings split or misattribute people. Coauthor timezone inference is especially weak.
* Only the selected path/time range is considered, not an author's whole work pattern. Git's normal date traversal can omit old branches under the cutoff. Extremely unusual commit messages containing the chosen ASCII field/record separators are unsupported.

## Tests

```sh
python3 -m unittest discover -s tests
```

Tests create isolated repositories with controlled author/committer timestamps, distinct UTC offsets and a bot. No network or existing Git history is needed.

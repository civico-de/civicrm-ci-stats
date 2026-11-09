# civicrm-ci-stats

Hourly statistics on how long the civicrm-core pull request tests take on
[test.civicrm.org](https://test.civicrm.org/duderino/), with detection of lasting
changes. The result is a static page in `docs/`.

## What it answers

- Did the test suites get faster or slower, when, and on which hosts?
- Which merged pull requests fall into the time window of a change?
- How do hosts differ, and how busy were they?

Test time reflects the test harness as well as the code. Speeding up test bootstrap
makes the suites faster without making CiviCRM faster, so a detected change is a lead
to investigate. The e2e suites run without a headless boot and are the closest proxy
for code speed. Measuring CiviCRM's own speed needs a benchmark on fixed hardware.

## How it works

| File | Purpose |
|---|---|
| `collect.py` | Reads finished batches from Duderino and merged PRs from GitHub, appends to `data/` |
| `detect.py` | Finds lasting steps across suites and hosts |
| `report.py` | Renders the pages in `docs/`: an overview of the whole history, and one page per month with daily values |
| `.github/workflows/collect.yml` | Runs all of it hourly and commits new results |

Everything uses the Python standard library. There is no build step.

## Data

| File | Content |
|---|---|
| `data/jobs/YYYY-MM.csv` | One row per job, one file per month: batch, PR, build type, PHP profile, suite, host, image, timestamps, `duration`, wall time, test counts |
| `data/merges.csv` | Merged civicrm-core PRs against master: number, time, title |
| `data/runs.csv` | One row per collector run with its counters |
| `data/cursor.txt` | Last batch id that is fully handled going forward |

`duration` is the time Duderino reports for the job and is empty for failed jobs.
`node` is empty on older jobs: they are charted as "host unknown" and left out of detection,
which compares hosts. `wall` is `completed` minus `started` and includes setup. Individual test names and
timings are not collected, because the Jenkins reports need a login.

Batch types come from the all-jobs page and `data/jobs/`. A type that appears in
neither is skipped, which only understates the load chart.

A job can be retried after it was collected. The stored row then shows the earlier
attempt, which has no measurable effect on medians.

## Detection

A change must meet all of these:

1. Only successful PR runs against `master` with `ORNERY=disable` count, grouped by
   suite, build type, PHP profile and host.
2. The 8 runs before and the 8 runs after a point barely overlap (Cliff's delta of at
   least 0.85), the median moved by at least 5 %, and each side covers at least 3
   different PRs, so one slow PR cannot cause a report.
3. It shows on at least 2 hosts, and either in at least 6 series or for one suite on
   at least 3 hosts.

The constants are at the top of `detect.py`. On simulated data with realistic spread
this gave 2 false reports in 150 healthy months. It found a 32 % step in 60 of 60 runs,
a 15 % step in 56, and a 10 % step in 24. Smaller changes stay below the noise.

Two labels help interpretation:

- **Likely host load:** the Karma suite, which runs no PHP, moved in the same direction
  on at least 2 hosts.
- **Test count changed:** the number of tests in the affected suites changed by more
  than 5 %, which includes moving tests between the parts of a split suite.

Merged PRs in the window are ranked by how many affected runs match the old level before
the merge and the new level after it; a PR's own run counts as after. All PRs within 3
points of the best fit are listed. The start of the window ignores series with no run for
more than 48 hours around the step, unless all of them have such a gap. They are candidates, not established causes.

## Running

```sh
python3 -m unittest discover -s tests -t .
python3 collect.py --start-id 20893600 --dry-run   # counters only, writes nothing
GITHUB_TOKEN=... python3 collect.py --start-id 20893600
python3 report.py
```

`--start-id` is required until `data/cursor.txt` exists. After that, each run continues
behind the cursor and reads only batches it does not have, up to `--max-batches`. A run
of the workflow that fails or never starts loses nothing, because the next one continues
from the same place. A run without new batches writes nothing and creates no commit. A
batch with no activity for 3 days is saved as far as it got and counted as `abandoned`.

## Going further back

`--back` collects older batches, walking down from the oldest one in `data/`. Run it again
to continue from where the last walk ended. It stops at `--max-batches`, or after
`--max-gap` missing ids in a row, which is how the start of the available history shows.
To continue below a larger gap, pass `--start-id` with an id below it. A page that fails
ends the walk, keeps what was collected so far and records the reason in `data/runs.csv`.

```sh
gh workflow run collect -f back=true -f max_batches=500 -f workers=4
```

Going back is always started by hand, and the hourly schedule only moves forward. GitHub
keeps one waiting run per workflow, so check that a walk has started; a waiting walk can
be replaced by the next hourly run.

By default the collector makes one request at a time and pauses for a second after each.
`--workers N` fetches the job pages of a batch in parallel, for one-off backfills. If a
Duderino page no longer has the expected structure, it exits non-zero and writes nothing.

## Setup on GitHub

1. Enable Pages for branch `main`, folder `/docs`.
2. For an empty `data/` directory, run the `collect` workflow by hand with `start_id` set
   to the first batch to collect. Otherwise the hourly schedule continues on its own.

## Security

See [SECURITY.md](SECURITY.md).

## License

AGPL-3.0-or-later, see [LICENSE.txt](LICENSE.txt).

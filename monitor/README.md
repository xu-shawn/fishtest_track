# How the status report is built

[`.github/workflows/status-report.yml`](../.github/workflows/status-report.yml) runs at 17 minutes past every hour (GitHub may delay or skip a scheduled run when it is busy):

1. `python -m monitor collect` reads fishtest's public JSON API: `/api/active_runs` for the running tests, and `/api/finished_runs` for tests finished since the last run. On the first run it backfills 35 days of finished tests. It also reads `/tests/machines`, the only place that shows which user runs each worker. That page sits behind fishtest's proof-of-work check, which [`machines.py`](machines.py) solves the way a browser would: it finds a nonce whose SHA-256 has the requested number of leading zero bits (26 at present, a few seconds on a GitHub runner). If that page cannot be read, the rest of the snapshot is still saved. All requests are gzip-compressed and identify themselves with a user agent that links to this repository.
2. `python -m monitor charts` draws the SVG charts, in light and dark versions, using only the Python standard library.
3. The charts are pushed to the `charts` branch as a single commit that replaces the previous one, so images never pile up in the history.
4. `python -m monitor readme` rewrites the top-level README, linking to the charts at that exact commit so GitHub's image cache never shows stale charts.
5. `data/` and `README.md` are committed to `main`.

## Data

| File | Contents |
|:--|:--|
| [`data/checks.csv`](../data/checks.csv) | One row per hourly run: tests running and waiting, authors, workers, cores (total and by test type), nodes per second, games per minute, pending hours. If the API could not be reached, that row has only the time and the reason. |
| [`data/active_runs.csv`](../data/active_runs.csv) | Every unfinished test at the latest run, with its cores, workers, games, Elo and LLR. Its git history is an hourly record of each test. |
| [`data/finished_runs.csv`](../data/finished_runs.csv) | One row per finished test: outcome, bounds, games, Elo and estimated core-hours. |
| [`data/workers.csv`](../data/workers.csv) | Every active worker at the latest run: the user who runs it, its cores, and the test it is playing. |
| [`data/llr_distribution.csv`](../data/llr_distribution.csv) | One row per hourly run: how many SPRT tests were running, their median LLR, and how many fall in each of 16 equal LLR bins from the rejection bound to the acceptance bound (each test scaled by its own bounds onto the standard ±2.94). The chart smooths each bin over 5 hours. |
| [`data/hourly_breakdown.jsonl`](../data/hourly_breakdown.jsonl) | One JSON line per hourly run with cores per running test, cores and running tests per author, and cores and workers per contributor. Only the last 15 days are kept; it feeds the 14-day band charts. |
| [`data/daily_breakdown.csv`](../data/daily_breakdown.csv) | Per day and per author or contributor: core-hours, plus test-hours (authors) or worker-hours (contributors). Summed from the hourly snapshots, each counting for the time since the previous one, at most an hour. Kept for good; it feeds the long-range charts and the contributors table. |

Test types are checked in this order:

| Type | Rule | Typical TC |
|:--|:--|:--|
| Nodestime | either engine sets the `nodestime` option to a non-zero value (those tests count nodes rather than time) | 100+1 nodestime=60 |
| SPSA | parameter tuning run | 60+0.6 |
| Progtest | progression test: `master` against the last release, at least 60,000 fixed games (no SPRT or SPSA) at an LTC base time | 60+0.6, 1 or 8 threads |
| VVLTC | more than one thread, base time of 40 s or more | 60+0.6, 8 threads |
| SMP | more than one thread, shorter base time | 5+0.05 or 20+0.2, 8 threads |
| VLTC | one thread, base time of 180 s or more | 180+1.8 |
| LTC | one thread, base time of 40 s or more (fishtest's own boundary) | 60+0.6 |
| STC | everything else | 10+0.1 |

Pending hours, Elo and the game-length estimate behind core-hours are ports of fishtest's code and match the numbers on its website.

Tests are also split into net tests, whose new side plays with different NNUE networks than the base (`new_nets` differs from `base_nets`), and search tests: every other test, i.e. code changes.

Finished tests are also split by their SPRT bounds: gainer bounds have a lower bound of 0 or more (such as [0, 2] and [0.5, 2.5]); simplification bounds have a negative lower bound (such as [-1.75, 0.25]).

The weekly charts cover a year of finished tests. The hourly job only fetches newly finished tests, so that year was loaded once with `python -m monitor backfill --days 365`, which pages through `/api/finished_runs` one page per second. Adding `--refresh` downloads the whole window again, which is how a new column (such as `net`) gets filled in for older tests.

## Running locally

Requires Python 3.11 or newer and nothing else.

```sh
python -m monitor collect                      # take one snapshot and update data/
python -m monitor charts --out build/charts    # draw the SVGs
python -m monitor readme --charts-url build/charts
python -m unittest                             # run the tests
```

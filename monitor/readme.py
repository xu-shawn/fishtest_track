"""Build README.md, which holds nothing but the latest status report."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from html import escape
from urllib.parse import quote

from .charts import compact, has_long_history, matches_kind, short
from .fishtest import TEST_TYPES
from .store import Check, History, parse_iso

FISHTEST = "https://tests.stockfishchess.org"
NBSP = "\u00a0"  # keeps short values like "LTC 60+0.6" on one line in tables
TITLE = "# Fishtest status"

def _whole(v: float) -> str:
    return f"{v:,.0f}"


TABLE_ROWS = [
    ("Tests running", "tests_active", _whole),
    ("Tests waiting for workers", "tests_pending", _whole),
    ("Authors with running tests", "authors_active", _whole),
    ("Cores", "cores", _whole),
    ("Workers", "workers", _whole),
    ("Contributors with workers", "contributors", _whole),
    ("Net tests running", "tests_net", _whole),
    ("Cores on net tests", "cores_net", _whole),
    ("Nodes per second", "mnps", lambda v: short(v * 1e6)),
    ("Games per minute", "games_per_min", _whole),
    ("Pending hours", "pending_hours", lambda v: f"{v:,.0f} h"),
]
WINDOWS = [("24 h avg", 24), ("7 d avg", 24 * 7), ("30 d avg", 24 * 30)]


def md(text: str) -> str:
    """Escape Markdown punctuation so names render literally (also inside tables)."""
    return "".join("\\" + ch if ch in "\\`*_[]<>|#~" else ch for ch in text)


def _when(t: datetime) -> str:
    return f"{t:%b} {t.day}, {t:%H:%M}"


def _mean(checks: list[Check], attr: str) -> float | None:
    values = [getattr(c, attr) for c in checks if getattr(c, attr) is not None]
    return sum(values) / len(values) if values else None


def _picture(name: str, base_url: str, alt: str) -> str:
    return (
        "<picture>\n"
        f'  <source media="(prefers-color-scheme: dark)" srcset="{base_url}/{name}-dark.svg">\n'
        f'  <img alt="{escape(alt)}" src="{base_url}/{name}-light.svg" width="100%">\n'
        "</picture>"
    )


def _key_numbers(checks: list[Check], snap: Check, now: datetime) -> list[str]:
    windows = [(name, [c for c in checks if c.time > now - timedelta(hours=hours)])
               for name, hours in WINDOWS]
    lines = [
        "| | Now | " + " | ".join(name for name, _ in windows) + " |",
        "|:--|--:|" + "--:|" * len(windows),
    ]
    for label, attr, fmt in TABLE_ROWS:
        value = getattr(snap, attr)
        cells = [fmt(value) if value is not None else "–"]
        for _, subset in windows:
            avg = _mean(subset, attr)
            cells.append(fmt(avg) if avg is not None else "–")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return lines


def _finished_summary(finished: dict[str, dict[str, str]], now: datetime) -> str:
    week = [r for r in finished.values() if parse_iso(r["finished"]) > now - timedelta(days=7)]
    if not week:
        return "No tests finished in the last 7 days."
    count = defaultdict(int)
    for r in week:
        count[r["outcome"]] += 1
    passed = Counter(r["type"] for r in week if r["outcome"] == "passed")
    by_type = ", ".join(f"{passed[t]} {t if t.isupper() else t.lower()}" for t in TEST_TYPES if passed[t])
    sprt = count["passed"] + count["rejected"]
    games = sum(int(r["games"]) for r in week)
    text = (
        f"Last 7 days: **{len(week):,} tests** finished, {compact(games)} games. "
        f"**{count['passed']} passed**{f' ({by_type})' if by_type else ''}, {count['rejected']} rejected, "
        f"{count['stopped']} stopped, {count['completed']} completed (SPSA or fixed games)"
    )
    if count["failed"]:
        text += f", {count['failed']} failed"
    if sprt:
        text += f". SPRT pass rate {100 * count['passed'] / sprt:.1f}%"
        groups = []
        for pair in (("gainer", "simplification"), ("net", "search")):
            rates = []
            for kind in pair:
                tests = [r for r in week if matches_kind(r, kind) and r["outcome"] in ("passed", "rejected")]
                if tests:
                    rate = 100 * sum(r["outcome"] == "passed" for r in tests) / len(tests)
                    label = f"{kind}s" if kind in ("gainer", "simplification") else f"{kind} tests"
                    rates.append(f"{rate:.1f}% for {label} ({len(tests)})")
            if rates:
                groups.append(", ".join(rates))
        if groups:
            text += ": " + "; ".join(groups)
    return text + "."


def _test_link(r: dict[str, str], max_len: int = 24) -> str:
    branch = r["branch"].replace("`", "") or r["id"]
    if len(branch) > max_len:
        branch = branch[: max_len - 1] + "…"
    return f"[`{branch}`]({FISHTEST}/tests/view/{r['id']})"


def _author_link(name: str) -> str:
    return f"[{md(name)}]({FISHTEST}/tests/user/{quote(name)})"


def _time_control(r: dict[str, str]) -> str:
    threads = f"{NBSP}×{r['threads']}" if r["threads"] not in ("", "1") else ""
    nodestime = f"{NBSP}nodestime" if r["type"] == "Nodestime" else ""
    return f"{r['tc']}{threads}{nodestime}"


def _recent_passes(finished: dict[str, dict[str, str]], limit: int = 12) -> list[str]:
    passed = sorted((r for r in finished.values() if r["outcome"] == "passed"),
                    key=lambda r: r["finished"], reverse=True)[:limit]
    if not passed:
        return ["No passed tests on record yet."]
    lines = [
        "| Finished | Author | Test | TC | Bounds | Elo |",
        "|:--|:--|:--|:--|:--|--:|",
    ]
    for r in passed:
        bounds = f"[{r['elo0']},{r['elo1']}]" if r.get("elo0") else ""
        elo = f"{float(r['elo']):+.2f}{NBSP}±{float(r['elo95']):.2f}" if r["elo"] else ""
        finished = parse_iso(r["finished"])
        lines.append(
            f"| {finished:%b}{NBSP}{finished.day} | {_author_link(r['username'])} | "
            f"{_test_link(r)} | {_time_control(r)} | {bounds} | {elo} |"
        )
    return lines


def _top_authors(finished: dict[str, dict[str, str]], active: list[dict[str, str]],
                 now: datetime, limit: int = 10) -> list[str]:
    month = [r for r in finished.values() if parse_iso(r["finished"]) > now - timedelta(days=30)]
    if not month:
        return ["No finished tests in the last 30 days."]
    stats: dict[str, dict] = defaultdict(lambda: {"tests": 0, "passed": 0, "hours": 0.0})
    for r in month:
        s = stats[r["username"]]
        s["tests"] += 1
        s["passed"] += r["outcome"] == "passed"
        s["hours"] += float(r["core_hours"] or 0)
    running = defaultdict(int)
    for r in active:
        if int(r["cores"] or 0) > 0:
            running[r["username"]] += 1
    total_hours = sum(s["hours"] for s in stats.values()) or 1

    ranked = sorted(stats.items(), key=lambda kv: (-kv[1]["hours"], kv[0].lower()))[:limit]
    lines = [
        "| Author | Finished | Passed | Running now | Core-hours | Share |",
        "|:--|--:|--:|--:|--:|--:|",
    ]
    for name, s in ranked:
        lines.append(
            f"| {_author_link(name)} | {s['tests']} | {s['passed']} | {running.get(name, 0)} | "
            f"{s['hours']:,.0f} | {100 * s['hours'] / total_hours:.1f}% |"
        )
    return lines


def _top_contributors(daily: list[dict[str, str]], workers: list[dict[str, str]],
                      now: datetime, limit: int = 10) -> list[str]:
    since = (now - timedelta(days=29)).date().isoformat()
    daily = [r for r in daily if r["kind"] == "contributor"]
    hours: dict[str, float] = defaultdict(float)
    for r in daily:
        if r["date"] >= since:
            hours[r["name"]] += float(r["core_hours"])
    if not hours:
        return ["No contributor history yet."]
    cores_now: dict[str, int] = defaultdict(int)
    workers_now: dict[str, int] = defaultdict(int)
    for w in workers:
        cores_now[w["username"]] += int(w["cores"])
        workers_now[w["username"]] += 1
    total = sum(hours.values()) or 1
    lines = [
        "| Contributor | Core-hours | Share | Cores now | Workers now |",
        "|:--|--:|--:|--:|--:|",
    ]
    for name, h in sorted(hours.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:limit]:
        lines.append(f"| {_author_link(name)} | {h:,.0f} | {100 * h / total:.1f}% | "
                     f"{cores_now.get(name, 0):,} | {workers_now.get(name, 0)} |")
    return lines


def _section(lines: list[str], title: str, name: str, url: str, alt: str) -> None:
    lines += ["", f"#### {title}", "", _picture(name, url, alt)]


def build_readme(history: History, now: datetime, charts_url: str) -> str:
    """Trends first, then the current snapshot, then reference tables."""
    h, url = history, charts_url
    checks = h.checks
    good = [c for c in checks if c.has_data]
    if not good:
        return f"{TITLE}\n\nNo data yet.\n"
    snap, last = good[-1], checks[-1]

    lines = [TITLE, "", f"<sub>Updated {snap.time:%a %d %b %Y, %H:%M} UTC</sub>", ""]
    if not last.has_data:
        lines += [
            "> [!WARNING]",
            f"> The fishtest API did not answer at {_when(last.time)} UTC ({last.reason}). "
            f"The numbers below are from {_when(snap.time)} UTC.",
            "",
        ]

    # ------------------------------------------------------------ trends
    lines += ["## Trends", "", "### Last 14 days"]
    _section(lines, "Cores by test type", "types", url, "Stacked area of cores used by each test "
             "type (STC, LTC, VLTC, VVLTC, SMP, progtest, nodestime, SPSA) over the last 14 days.")
    if h.hourly:
        _section(lines, "Tests by type", "type-counts", url, "Stacked area of running tests by test "
                 "type over the last 14 days.")
    if h.hourly:
        _section(lines, "Net tests and search tests", "net-search-trend", url, "Stacked bands of "
                 "running search tests and net tests over the last 14 days.")
        _section(lines, "Gainer and simplification bounds", "bounds-trend", url, "Stacked bands of "
                 "running tests with gainer bounds, simplification bounds and no SPRT bounds over "
                 "the last 14 days.")
    if h.hourly:
        _section(lines, "Cores by test", "tests-trend", url, "Stacked bands of cores per test over "
                 "the last 14 days, one band per test, colored by test type.")
        _section(lines, "Cores by author", "authors-trend", url, "Stacked bands of cores per author "
                 "over the last 14 days, the top 8 authors labelled.")
        _section(lines, "Tests per author", "author-tests-trend", url, "Stacked bands of running "
                 "tests per author over the last 14 days, the top 8 authors labelled.")
        if any(row.get("contributors") for row in h.hourly):
            _section(lines, "Cores contributed by user", "contributors-trend", url, "Stacked bands of "
                     "cores contributed per user over the last 14 days, the top 8 labelled.")
    _section(lines, "Tests, authors, contributors and throughput", "activity", url, "Line charts of "
             "tests running and waiting, authors, workers, contributors, nodes per second, games per "
             "minute and pending hours over the last 14 days.")
    _section(lines, "Tests finished per hour", "throughput", url, "Faint columns of tests finished "
             "in each hour over the last 14 days, with a line for the trailing 24-hour average.")
    if h.llr:
        _section(lines, "LLR distribution of running tests", "llr", url, "Heatmap of where the LLRs "
                 "of running SPRT tests sit between -2.94 (reject) and +2.94 (pass), one column per "
                 "hour, with the median as a line.")

    lines += ["", "### Last 30 days"]
    _section(lines, "Finished tests per day", "finished", url, "Stacked columns of tests finished "
             "per day, split into gainers passed, simplifications passed, rejected and other.")
    lines += ["", _finished_summary(h.finished, now)]

    start = good[0].time
    lines += ["", "### Last year"]
    _section(lines, "Finished tests per week", "weekly-finished", url, "Stacked columns of tests "
             "finished per week over the last year, split into gainers passed, simplifications "
             "passed, rejected and other.")
    _section(lines, "Runtime of finished tests", "runtime-trend", url, "Heatmap of how long the "
             "tests finished each week ran, from submission to finish on a log scale from 15 "
             "minutes to 6 weeks, with the weekly median as a line.")
    _section(lines, "Average cores per week by test type", "weekly-types", url, "Stacked columns "
             "of the average cores each test type used per week, measured from the hourly snapshots.")
    _section(lines, "Gainer bounds", "weekly-gainers", url, "Stacked columns of tests with gainer "
             "bounds (such as [0, 2] and [0.5, 2.5]) finished per week.")
    _section(lines, "Simplification bounds", "weekly-simplifications", url, "Stacked columns of tests "
             "with simplification bounds (such as [-1.75, 0.25]) finished per week.")
    _section(lines, "Net tests", "weekly-net", url, "Stacked columns of tests of new networks "
             "finished per week.")
    _section(lines, "Search tests", "weekly-search", url, "Stacked columns of tests of code changes "
             "(not new networks) finished per week.")

    if has_long_history(checks):
        lines += ["", f"### Since {start.day} {start:%b %Y}, daily averages"]
        _section(lines, "Cores by test type", "long-types", url, "Stacked area of the daily "
                 "average cores used by each test type.")
        _section(lines, "Cores by author", "long-authors", url, "Stacked bands of the daily "
                 "average cores per author, the top 8 labelled.")
        _section(lines, "Tests per author", "long-author-tests", url, "Stacked bands of the daily "
                 "average running tests per author, the top 8 labelled.")
        _section(lines, "Cores contributed by user", "long-contributors", url, "Stacked bands of "
                 "the daily average cores contributed per user, the top 8 labelled.")
        _section(lines, "Tests, authors, contributors and throughput", "long-activity", url,
                 "Line charts of the daily averages of tests, authors, workers, contributors, "
                 "nodes per second, games per minute and pending hours.")

    # ------------------------------------------------------------ right now
    waiting = (f"{snap.tests_pending} {'test is' if snap.tests_pending == 1 else 'tests are'} "
               "waiting for workers")
    machines = f"{snap.workers:,} workers"
    if snap.contributors is not None:
        machines += f" from {snap.contributors} contributors"
    lines += [
        "", "## Right now", "",
        f"**{snap.tests_active} tests** from **{snap.authors_active} authors** are running on "
        f"**{snap.cores:,} cores** ({machines}). {waiting}, and about "
        f"{snap.pending_hours:,.0f} hours of work is queued.",
        "",
    ]
    lines += _key_numbers(checks, snap, now)
    lines += [
        "", "### Cores by test", "",
        _picture("tests", url, "Horizontal bars of cores per running test, largest first, "
                 "colored by test type."),
        "", "### Runtime of running tests", "",
        _picture("runtime", url, "Columns of running tests by how long ago they were submitted, "
                 "from under an hour to over two weeks, stacked by test type."),
        "", "### Cores by author", "",
        _picture("authors", url, "Horizontal bars of cores per author, largest first, "
                 "split by test type."),
    ]
    if h.workers:
        lines += ["", "### Cores contributed by user", ""]
        latest = next((c for c in reversed(checks) if c.contributors is not None), None)
        if latest is not None and latest is not snap:
            lines += [f"<sub>The machines page could not be read at the latest check; these are "
                      f"from {_when(latest.time)} UTC.</sub>", ""]
        lines.append(_picture("contributors", url, "Horizontal bars of cores contributed per "
                              "user, largest first, split by the type of test their workers play."))

    # ------------------------------------------------------------ tables
    lines += ["", "## Last 30 days in detail", "", "### Recently passed", ""]
    lines += _recent_passes(h.finished)
    lines += ["", "### Most active authors", ""]
    lines += _top_authors(h.finished, h.active, now)
    if any(r["kind"] == "contributor" for r in h.daily):
        lines += ["", "### Top contributors", ""]
        lines += _top_contributors(h.daily, h.workers, now)
    return "\n".join(lines) + "\n"

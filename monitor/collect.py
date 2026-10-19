"""Take one hourly snapshot of fishtest and add it to the history in data/."""

from __future__ import annotations

import os
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .fishtest import (
    ACTIVE_FIELDS,
    FINISHED_FIELDS,
    FetchError,
    active_record,
    fetch_json_with_retries,
    finished_record,
    iso,
    llr_distribution,
    parse_time,
    summarize,
)
from .machines import Worker, fetch_workers
from .store import (
    ACTIVE_FILE,
    DAILY_FIELDS,
    DAILY_FILE,
    FINISHED_FILE,
    HOURLY_FILE,
    LLR_FIELDS,
    LLR_FILE,
    WORKER_FIELDS,
    WORKERS_FILE,
    Check,
    append_check,
    append_row,
    parse_iso,
    read_checks,
    read_finished,
    read_jsonl,
    read_rows,
    write_jsonl,
    write_rows,
)

HOURLY_RETENTION = timedelta(days=15)  # the hourly breakdown only feeds the 14-day charts

# On the first run, fetch enough finished tests to fill the 30-day charts.
BACKFILL_DAYS = 35
MAX_PAGES_FIRST_RUN = 20
MAX_PAGES = 5  # 50 tests per page; fishtest finishes roughly 20 a day


def run_check(now: datetime) -> tuple[Check, dict | None]:
    """The check row, plus the raw unfinished runs when the API answered."""
    try:
        reply = fetch_json_with_retries("/api/active_runs")
        if not isinstance(reply.data, dict):
            raise FetchError("unexpected response shape")
        if isinstance(reply.data.get("error"), str):
            raise FetchError(f"API error: {reply.data['error']}")
    except FetchError as e:
        return Check(time=now, status="down", reason=str(e)), None

    snapshot = summarize(reply.data)
    check = Check(time=now, status="up", latency_ms=round(reply.latency_s * 1000), **asdict(snapshot))
    return check, reply.data


def write_active(data_dir: Path, runs: dict) -> None:
    rows = [active_record(r) for r in runs.values() if not r.get("finished")]
    rows.sort(key=lambda r: (-int(r["cores"]), r["username"].lower(), r["branch"]))
    write_rows(data_dir, ACTIVE_FILE, ACTIVE_FIELDS, rows)


def snapshot_hours(data_dir: Path, now: datetime) -> float:
    """How many hours this snapshot stands for: the time since the previous
    snapshot with data, capped at one hour, so an extra manual run in the same
    hour does not count twice."""
    previous = [c for c in read_checks(data_dir) if c.has_data]
    if not previous:
        return 1.0
    return max(0.0, min(1.0, (now - previous[-1].time).total_seconds() / 3600))


def write_workers(data_dir: Path, workers: list[Worker]) -> None:
    rows = [{"username": w.username, "cores": str(w.cores), "run": w.run} for w in workers]
    rows.sort(key=lambda r: (r["username"].lower(), -int(r["cores"]), r["run"]))
    write_rows(data_dir, WORKERS_FILE, WORKER_FIELDS, rows)


def breakdown(runs: dict, workers: list[Worker] | None) -> dict[str, dict]:
    """Cores per running test, cores and tests per author, cores and workers per contributor."""
    tests: dict[str, int] = {}
    authors: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for run in runs.values():
        if run.get("finished") or run.get("workers", 0) <= 0:
            continue
        tests[str(run["_id"])] = run.get("cores", 0)
        author = authors[run["args"].get("username", "")]
        author[0] += run.get("cores", 0)
        author[1] += 1
    parts = {"tests": tests, "authors": dict(authors)}
    if workers is not None:
        contributors: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for w in workers:
            contributors[w.username][0] += w.cores
            contributors[w.username][1] += 1
        parts["contributors"] = dict(contributors)
    return parts


def update_breakdown(data_dir: Path, parts: dict[str, dict], now: datetime, hours: float) -> None:
    """Add this snapshot to the hourly breakdown and to the daily totals."""
    hourly = [row for row in read_jsonl(data_dir, HOURLY_FILE)
              if parse_iso(row["time"]) > now - HOURLY_RETENTION]
    hourly.append({"time": iso(now), **parts})
    write_jsonl(data_dir, HOURLY_FILE, hourly)

    daily = {(r["date"], r["kind"], r["name"]): [float(r["core_hours"]), float(r["count_hours"] or 0)]
             for r in read_rows(data_dir, DAILY_FILE)}
    today = now.date().isoformat()
    for kind, key in (("author", "authors"), ("contributor", "contributors")):
        for name, (cores, count) in parts.get(key, {}).items():
            totals = daily.setdefault((today, kind, name), [0.0, 0.0])
            totals[0] += cores * hours
            totals[1] += count * hours
    rows = [{"date": d, "kind": k, "name": n, "core_hours": f"{c:.1f}", "count_hours": f"{t:.1f}"}
            for (d, k, n), (c, t) in sorted(daily.items(), key=lambda kv: (kv[0][0], kv[0][1],
                                                                           kv[0][2].lower()))]
    write_rows(data_dir, DAILY_FILE, DAILY_FIELDS, rows)


def update_finished(data_dir: Path, now: datetime) -> int:
    """Fetch recently finished tests until we reach ones we already have."""
    known = read_finished(data_dir)
    if known:
        newest_known = max(parse_iso(r["finished"]) for r in known.values())
        max_pages = MAX_PAGES
    else:
        newest_known = now - timedelta(days=BACKFILL_DAYS)
        max_pages = MAX_PAGES_FIRST_RUN

    added = 0
    for page in range(1, max_pages + 1):
        if page > 1:
            time.sleep(1)  # be gentle during the backfill
        reply = fetch_json_with_retries(f"/api/finished_runs?page={page}", delays=(10,))
        runs = list(reply.data.values()) if isinstance(reply.data, dict) else []
        runs = [r for r in runs if isinstance(r, dict) and not r.get("deleted")]
        if not runs:
            break
        for run in runs:
            record = finished_record(run)
            added += record["id"] not in known
            known[record["id"]] = record
        if min(parse_time(r["last_updated"]) for r in runs) <= newest_known:
            break

    # Oldest first, so each hour's new tests land at the end of the file.
    rows = sorted(known.values(), key=lambda r: (r["finished"], r["id"]))
    write_rows(data_dir, FINISHED_FILE, FINISHED_FIELDS, rows)
    return added


def backfill_finished(data_dir: Path, days: int, max_pages: int = 400) -> int:
    """Extend data/finished_runs.csv back to `days` ago (a one-off, slow on purpose)."""
    known = read_finished(data_dir)
    target = datetime.now(UTC) - timedelta(days=days)
    if known and min(parse_iso(r["finished"]) for r in known.values()) <= target:
        return 0
    # Skip pages we already have, keeping one page of overlap for runs finished since.
    page = max(1, len(known) // 50 - 1)
    added = 0
    for _ in range(max_pages):
        reply = fetch_json_with_retries(f"/api/finished_runs?page={page}", delays=(10, 30))
        runs = list(reply.data.values()) if isinstance(reply.data, dict) else []
        runs = [r for r in runs if isinstance(r, dict) and not r.get("deleted")]
        if not runs:
            break
        for run in runs:
            record = finished_record(run)
            added += record["id"] not in known
            known[record["id"]] = record
        oldest = min(parse_time(r["last_updated"]) for r in runs)
        if page % 20 == 0:
            print(f"page {page}: back to {oldest:%Y-%m-%d}, {added} added", flush=True)
        if oldest <= target:
            break
        page += 1
        time.sleep(1)

    rows = sorted(known.values(), key=lambda r: (r["finished"], r["id"]))
    write_rows(data_dir, FINISHED_FILE, FINISHED_FIELDS, rows)
    return added


def collect(data_dir: Path) -> Check:
    now = datetime.now(UTC).replace(microsecond=0)
    check, runs = run_check(now)
    workers = None
    if runs is not None:
        try:
            workers = fetch_workers()
            check.contributors = len({w.username for w in workers})
        except FetchError as e:
            # Contributor data is a bonus; keep the rest of the snapshot.
            print(f"warning: could not read the machines page: {e}")
    hours = snapshot_hours(data_dir, now)
    append_check(data_dir, check)

    if runs is None:
        print(f"fishtest API unreachable: {check.reason}")
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::warning title=fishtest API unreachable::{check.reason}")
        return check

    write_active(data_dir, runs)
    update_breakdown(data_dir, breakdown(runs, workers), now, hours)
    tests, median, counts = llr_distribution(runs)
    append_row(data_dir, LLR_FILE, LLR_FIELDS, {
        "time": iso(now), "tests": str(tests),
        "median": f"{median:.3f}" if median is not None else "",
        "counts": " ".join(map(str, counts)),
    })
    if workers is not None:
        write_workers(data_dir, workers)
        print(f"{check.contributors} contributors run {len(workers)} workers "
              f"({sum(w.cores for w in workers)} cores)")
    print(
        f"{check.tests_active} tests running ({check.tests_pending} waiting) from "
        f"{check.authors_active} authors on {check.cores} cores / {check.workers} workers"
    )
    try:
        added = update_finished(data_dir, now)
        print(f"finished tests: {added} new")
    except FetchError as e:
        # The snapshot itself is already saved; try again next hour.
        print(f"warning: could not update finished tests: {e}")
    return check

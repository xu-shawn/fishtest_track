"""Reading and writing the CSV history kept in data/."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CHECKS_FILE = "checks.csv"  # one row per hourly check
ACTIVE_FILE = "active_runs.csv"  # unfinished tests at the latest successful check
FINISHED_FILE = "finished_runs.csv"  # one row per finished test
WORKERS_FILE = "workers.csv"  # active workers at the latest run: owner, cores, test
HOURLY_FILE = "hourly_breakdown.jsonl"  # per-test/author/contributor numbers, last 15 days
DAILY_FILE = "daily_breakdown.csv"  # core-hours and test-hours per author/contributor per day
LLR_FILE = "llr_distribution.csv"  # running SPRT tests binned by LLR, one row per run
WORKER_FIELDS = ["username", "cores", "run", "arch", "compiler", "mnps"]
LLR_FIELDS = ["time", "tests", "median", "counts"]  # counts: space-separated, low LLR first
DAILY_FIELDS = ["date", "kind", "name", "core_hours", "count_hours", "mnps_hours"]
# kind: author, contributor, arch or compiler. count_hours: test-hours for authors (running
# tests x hours), worker-hours for everything else. mnps_hours: MNps x hours, for the kinds
# read from the machines page (empty before it was recorded). Rows of kind "hours" record,
# under core_hours, how many hours each kind (named in `name`) was observed that day; the
# one named "mnps" counts the hours MNps was recorded.


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


@dataclass
class Check:
    """One hourly check. Everything but time/status/reason is None if the API was down."""

    time: datetime
    status: str  # up | down
    reason: str = ""
    tests_active: int | None = None
    tests_pending: int | None = None
    authors_active: int | None = None
    workers: int | None = None
    contributors: int | None = None  # users with active workers; None if the page was unavailable
    cores: int | None = None
    cores_stc: int | None = None
    cores_ltc: int | None = None
    cores_vltc: int | None = None
    cores_vvltc: int | None = None
    cores_smp: int | None = None
    cores_progtest: int | None = None
    cores_nodestime: int | None = None
    cores_spsa: int | None = None
    tests_net: int | None = None  # running tests of a new network
    cores_net: int | None = None
    mnps: float | None = None
    games_per_min: float | None = None
    pending_hours: float | None = None
    latency_ms: int | None = None

    @property
    def has_data(self) -> bool:
        return self.status != "down"


CHECK_FIELDS = [f.name for f in fields(Check)]
_FLOAT_FIELDS = {"mnps", "games_per_min", "pending_hours"}
_TEXT_FIELDS = {"status", "reason"}


def _to_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _from_row(row: dict[str, str]) -> Check:
    values: dict[str, Any] = {}
    for name in CHECK_FIELDS:
        raw = row.get(name) or ""
        if name == "time":
            values[name] = parse_iso(raw)
        elif name in _TEXT_FIELDS:
            values[name] = raw
        elif name in _FLOAT_FIELDS:
            values[name] = float(raw) if raw else None
        else:
            values[name] = int(raw) if raw else None
    return Check(**values)


def read_checks(data_dir: Path) -> list[Check]:
    path = data_dir / CHECKS_FILE
    if not path.exists():
        return []
    with path.open(newline="") as f:
        checks = [_from_row(row) for row in csv.DictReader(f)]
    return sorted(checks, key=lambda c: c.time)


def append_row(data_dir: Path, name: str, fieldnames: list[str], row: dict[str, str]) -> None:
    """Append one row to a history file, creating it or updating its columns as needed."""
    path = data_dir / name
    data_dir.mkdir(parents=True, exist_ok=True)
    if path.exists():
        with path.open(newline="") as f:
            header = next(csv.reader(f), [])
        if header != fieldnames:
            # The columns changed since the file was started: rewrite it with the
            # current ones, keeping old values by name and leaving new columns empty.
            write_rows(data_dir, name, fieldnames, read_rows(data_dir, name))
    else:
        write_rows(data_dir, name, fieldnames, [])
    with path.open("a", newline="") as f:
        csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n").writerow(row)


def append_check(data_dir: Path, check: Check) -> None:
    append_row(data_dir, CHECKS_FILE, CHECK_FIELDS,
               {name: _to_cell(getattr(check, name)) for name in CHECK_FIELDS})


def read_rows(data_dir: Path, name: str) -> list[dict[str, str]]:
    path = data_dir / name
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def write_rows(data_dir: Path, name: str, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / name).open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_jsonl(data_dir: Path, name: str) -> list[dict]:
    path = data_dir / name
    if not path.exists():
        return []
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(data_dir: Path, name: str, rows: list[dict]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / name).open("w") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")


def read_finished(data_dir: Path) -> dict[str, dict[str, str]]:
    return {row["id"]: row for row in read_rows(data_dir, FINISHED_FILE)}


@dataclass
class History:
    """Everything in data/ that the charts and the README are drawn from."""

    checks: list[Check] = field(default_factory=list)
    active: list[dict[str, str]] = field(default_factory=list)
    finished: dict[str, dict[str, str]] = field(default_factory=dict)
    workers: list[dict[str, str]] = field(default_factory=list)
    llr: list[dict[str, str]] = field(default_factory=list)
    hourly: list[dict] = field(default_factory=list)
    daily: list[dict[str, str]] = field(default_factory=list)


def load(data_dir: Path) -> History:
    return History(
        checks=read_checks(data_dir),
        active=read_rows(data_dir, ACTIVE_FILE),
        finished=read_finished(data_dir),
        workers=read_rows(data_dir, WORKERS_FILE),
        llr=read_rows(data_dir, LLR_FILE),
        hourly=read_jsonl(data_dir, HOURLY_FILE),
        daily=read_rows(data_dir, DAILY_FILE),
    )

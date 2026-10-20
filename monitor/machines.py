"""Who runs fishtest's workers: the /tests/machines page.

The JSON API does not say which user runs each worker, so this reads the
machines page instead. That page sits behind fishtest's proof-of-work check:
the server sends a challenge, and a client proves it spent some CPU by finding
a nonce for which SHA-256("<challenge>:<nonce>") starts with `bits` zero bits
(26 at the moment, about 67 million hashes on average). We do that work once
per hourly run, spread over all CPU cores, then fetch the page with the cookie
the check hands out, using the same identifying user agent as the API calls.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from multiprocessing import Pool

from .fishtest import FetchError, fetch

PATH = "/tests/machines"
COOKIE = "__Host-fishtest-ac"
PAGE_SIZE = 500  # fishtest's MACHINES_PAGE_SIZE
MAX_PAGES = 4
SOLVE_TIME_LIMIT = 300  # seconds
_CHUNK = 1 << 20  # nonces per work unit, about half a second of hashing

_CHALLENGE = re.compile(
    r'var challenge="([0-9a-f]+)",bits=(\d+);\s*var slot="(\d+)",cap=(\d+);'
)
_RUN_ID = re.compile(r"/tests/view/([0-9a-f]{24})")


@dataclass
class Worker:
    username: str
    cores: int
    run: str  # id of the test the worker is playing games for
    key: str = ""  # short worker id shown on the page, only used to drop duplicates
    arch: str = ""  # Stockfish build the worker uses, e.g. x86-64-avx512
    compiler: str = ""  # e.g. g++ 16.2.0
    mnps: float = 0.0  # the whole worker's speed: cores x the page's MNps per core


# ---------------------------------------------------------------- proof of work

def _meets(digest: bytes, bits: int) -> bool:
    return int.from_bytes(digest[:8], "big") >> (64 - bits) == 0


def _scan(job: tuple[str, int, int, int]) -> int | None:
    challenge, bits, start, count = job
    base = hashlib.sha256(challenge.encode())
    for nonce in range(start, start + count):
        h = base.copy()
        h.update(b":%d" % nonce)
        if _meets(h.digest(), bits):
            return nonce
    return None


def solve(challenge: str, bits: int, cap: int, time_limit: float = SOLVE_TIME_LIMIT) -> int:
    """Find a nonce below `cap` whose hash has `bits` leading zero bits."""
    if not 0 < bits <= 64:
        raise FetchError(f"unsupported proof-of-work difficulty ({bits} bits)")
    deadline = time.monotonic() + time_limit
    jobs = ((challenge, bits, start, min(_CHUNK, cap - start)) for start in range(0, cap, _CHUNK))
    with Pool(os.cpu_count() or 1) as pool:
        for nonce in pool.imap_unordered(_scan, jobs):
            if nonce is not None:
                return nonce
            if time.monotonic() > deadline:
                raise FetchError(f"proof of work not solved within {time_limit:.0f} s")
    raise FetchError("proof of work has no solution")


def _page(path: str, cookie: str | None) -> str:
    body, _, _ = fetch(path, timeout=60, cookie=cookie)
    return body.decode("utf-8", "replace")


def _pass_check(page: str) -> str | None:
    """If `page` is the proof-of-work check, solve it and return the cookie."""
    m = _CHALLENGE.search(page)
    if not m:
        return None
    challenge, bits, slot, cap = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
    started = time.monotonic()
    nonce = solve(challenge, bits, cap)
    print(f"proof of work: {bits} bits solved in {time.monotonic() - started:.1f} s")
    return f"{COOKIE}={slot}.{nonce}"


# ---------------------------------------------------------------- parsing

class _MachineRows(HTMLParser):
    """Collects the cells of each row in the machines table body."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[dict[str, str]]] = []
        self._row: list[dict[str, str]] | None = None
        self._cell: dict[str, str] | None = None
        self._in_body = False

    def handle_starttag(self, tag, attrs):
        if tag == "tbody":
            self._in_body = True
        elif tag == "tr" and self._in_body:
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell = {"text": "", "href": ""}
        elif tag == "a" and self._cell is not None:
            self._cell["href"] = dict(attrs).get("href") or ""

    def handle_endtag(self, tag):
        if tag == "td" and self._cell is not None and self._row is not None:
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag == "tbody":
            self._in_body = False

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"] += data


def parse_workers(page: str) -> list[Worker]:
    """Workers from the machines table: username, cores and current test.

    Columns follow fishtest's machines_fragment template: username, cores
    (after a country flag), worker, MNps (per core), memory, system, arch,
    compiler, python, worker version, run/task, last active.
    """
    parser = _MachineRows()
    parser.feed(page)
    workers = []
    for row in parser.rows:
        if len(row) < 11:
            continue  # e.g. the "No machines running" row
        cores = row[1]["text"].strip()
        run = _RUN_ID.search(row[10]["href"])
        if not cores.isdigit():
            raise FetchError(f"unexpected machines table layout (cores cell {cores!r})")
        try:
            per_core = float(row[3]["text"].strip())
        except ValueError:
            per_core = 0.0  # no speed reported yet
        workers.append(Worker(row[0]["text"].strip(), int(cores), run.group(1) if run else "",
                              row[2]["text"].strip(), row[6]["text"].strip(),
                              " ".join(row[7]["text"].split()), int(cores) * per_core))
    if not workers and "No machines running" not in page:
        raise FetchError("machines table not found")
    return workers


def fetch_workers() -> list[Worker]:
    """Every active worker, passing the proof-of-work check if it is shown."""
    page = _page(PATH, None)
    cookie = _pass_check(page)
    if cookie:
        page = _page(PATH, cookie)
        if _CHALLENGE.search(page):
            raise FetchError("proof-of-work check not accepted")
    workers = parse_workers(page)
    page_number = 1
    while len(workers) == PAGE_SIZE * page_number and page_number < MAX_PAGES:
        page_number += 1
        workers += parse_workers(_page(f"{PATH}?page={page_number}", cookie))
    # fishtest clamps out-of-range pages to the last one, so drop repeats.
    unique = {(w.key or id(w)): w for w in workers}
    return list(unique.values())

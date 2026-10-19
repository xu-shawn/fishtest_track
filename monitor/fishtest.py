"""Fishtest API client and the numbers derived from its answers.

Pending hours and Elo mirror fishtest's own code (server/fishtest/util.py and
stats/stat_util.py in official-stockfish/fishtest), so they match what the
fishtest website shows.
"""

from __future__ import annotations

import gzip
import http.client
import json
import math
import os
import time
import urllib.error
import urllib.request
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from statistics import NormalDist

BASE_URL = "https://tests.stockfishchess.org"
REPO = os.environ.get("GITHUB_REPOSITORY", "xu-shawn/fishtest_track")
USER_AGENT = f"fishtest_track/1.0 (hourly status report; +https://github.com/{REPO})"

TEST_TYPES = ("STC", "LTC", "VLTC", "VVLTC", "SMP", "Nodestime", "SPSA")
# Column in data/checks.csv holding the cores used by each test type.
TYPE_COLUMNS = {t: f"cores_{t.lower()}" for t in TEST_TYPES}
LTC_MIN_BASE_TIME = 40  # seconds; fishtest's own STC/LTC boundary (rundb.ltc_lower_bound)
VLTC_MIN_BASE_TIME = 180  # seconds; VLTC is 180+1.8
LLR_BINS = 16
STANDARD_LLR_BOUND = math.log(0.95 / 0.05)  # 2.944, the bound for alpha = beta = 0.05


class FetchError(Exception):
    """The API did not return usable JSON."""


@dataclass
class Response:
    data: object
    latency_s: float
    server_time: datetime


def fetch(path: str, timeout: float = 30, cookie: str | None = None) -> tuple[bytes, dict, float]:
    """GET a fishtest URL; returns the (decompressed) body, headers and latency."""
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    if cookie:
        headers["Cookie"] = cookie
    start = time.monotonic()
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE_URL + path, headers=headers),
                                    timeout=timeout) as reply:
            body = reply.read()
            reply_headers = reply.headers
    except urllib.error.HTTPError as e:
        raise FetchError(f"HTTP {e.code}") from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            raise FetchError(f"no response within {timeout:.0f} s") from e
        raise FetchError(f"connection failed: {e.reason}") from e
    except TimeoutError as e:
        raise FetchError(f"no response within {timeout:.0f} s") from e
    except (OSError, http.client.HTTPException) as e:
        raise FetchError(f"connection failed: {type(e).__name__}") from e
    latency = time.monotonic() - start
    if reply_headers.get("Content-Encoding", "").lower() == "gzip":
        body = gzip.decompress(body)
    return body, reply_headers, latency


def fetch_json(path: str, timeout: float = 30) -> Response:
    body, headers, latency = fetch(path, timeout)
    try:
        data = json.loads(body)
    except ValueError as e:
        if b"Checking your browser" in body:
            raise FetchError("blocked by the site's bot check") from e
        raise FetchError("response was not JSON") from e

    try:
        server_time = parsedate_to_datetime(headers["Date"])
    except (KeyError, TypeError, ValueError):
        server_time = datetime.now(UTC)
    return Response(data, latency, server_time)


def fetch_json_with_retries(path: str, delays: tuple[float, ...] = (15, 45)) -> Response:
    """Retry so that a single network blip does not cost an hour of data."""
    error = FetchError("not attempted")
    for delay in (0, *delays):
        time.sleep(delay)
        try:
            return fetch_json(path)
        except FetchError as e:
            error = e
    raise FetchError(f"{error} ({len(delays) + 1} attempts)")


def parse_time(value: str) -> datetime:
    """Fishtest times look like '2026-10-19 04:15:28.320000+00:00'."""
    t = datetime.fromisoformat(value)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- fishtest formulas

def estimate_game_duration(tc: str) -> float:
    """Seconds one game takes at time control `tc` (port of fishtest's util)."""
    scale = 2 * 0.92  # both players, games end a bit before using all time
    game_moves = 68

    chunks = tc.split("+")
    increment = float(chunks[1]) if len(chunks) == 2 else 0.0

    chunks = chunks[0].split("/")
    num_moves = int(chunks[0]) if len(chunks) == 2 else 0

    clock = chunks[-1].split(":")
    if len(clock) == 2:
        base = float(clock[0]) * 60 + float(clock[1])
    else:
        base = float(clock[0])
    if num_moves > 0:
        base *= game_moves / num_moves
    return (base + increment * game_moves) * scale


def _beta_1_15_cdf(x: float) -> float:
    # Closed form of scipy.stats.beta(1, 15).cdf, which fishtest uses.
    return 1 - (1 - x) ** 15


def remaining_hours(run: dict) -> float:
    """Core-hours still needed to finish `run` (port of fishtest's util)."""
    args, results = run["args"], run["results"]
    if "sprt" in args:
        average_total_games = 95000
        played_pairs = sum(results["pentanomial"])
        played_games = played_pairs * 2

        sprt = args["sprt"]
        llr, alpha, beta = sprt["llr"], sprt["alpha"], sprt["beta"]
        o0 = o1 = 0
        if "overshoot" in sprt:
            o = sprt["overshoot"]
            o0 = -o["sq0"] / o["m0"] / 2 if o["m0"] != 0 else 0
            o1 = o["sq1"] / o["m1"] / 2 if o["m1"] != 0 else 0
        if llr > 0.0:
            llr_bound = math.log((1 - beta) / alpha) - o1
        else:
            llr_bound = math.log(beta / (1 - alpha)) + o0

        llr, llr_bound = max(0.1, abs(llr)), abs(llr_bound)
        if llr >= llr_bound:
            return 0.0

        book_positions = 2632036
        t = _beta_1_15_cdf(min(played_pairs / book_positions, 1.0))
        expected_games_llr = int(played_games * llr_bound / llr)
        expected_games = min(
            args["num_games"],
            int(expected_games_llr * t + average_total_games * (1 - t)),
        )
        remaining_games = max(0, expected_games - played_games)
    else:
        played_games = results["wins"] + results["losses"] + results["draws"]
        remaining_games = max(0, args["num_games"] - played_games)
    game_secs = estimate_game_duration(args["tc"])
    return game_secs * remaining_games * int(args.get("threads", 1)) / 3600


def get_elo(pentanomial: list[int]) -> tuple[float, float] | None:
    """Elo and its 95% half-width from pentanomial counts (port of stat_util)."""
    if sum(pentanomial) == 0:
        return None
    results = [r if r else 1e-3 for r in pentanomial]  # fishtest's regularization
    count = len(results)
    games = sum(results) * (count - 1) / 2.0
    mu = sum(r * (i / 2.0) for i, r in enumerate(results)) / games
    mu_ = (count - 1) / 2.0 * mu
    var = sum(r * (i / 2.0 - mu_) ** 2.0 for i, r in enumerate(results)) / games
    stdev = math.sqrt(var)

    def elo(x: float) -> float:
        x = min(max(x, 1e-3), 1 - 1e-3)
        return -400 * math.log10(1 / x - 1)

    z = NormalDist().inv_cdf(0.975)
    mu_min = mu - z * stdev / math.sqrt(games)
    mu_max = mu + z * stdev / math.sqrt(games)
    return elo(mu), (elo(mu_max) - elo(mu_min)) / 2.0


def uses_nodestime(args: dict) -> bool:
    """True if either engine sets the nodestime option (0, the default, means off)."""
    for options in (args.get("new_options") or "", args.get("base_options") or ""):
        for token in options.split():
            name, _, value = token.partition("=")
            if name.lower() == "nodestime" and value.strip() not in ("", "0"):
                return True
    return False


def base_time(tc: str) -> float:
    """Seconds on the clock at the start: '60+0.6' -> 60, '1:30+1' -> 90, '40/10' -> 10."""
    clock = tc.split("+")[0].split("/")[-1].split(":")
    return float(clock[0]) * 60 + float(clock[1]) if len(clock) == 2 else float(clock[0])


def classify_test(args: dict, tc_base: float | None = None) -> str:
    """One of TEST_TYPES, checked in this order:

    Nodestime  either engine sets nodestime; such tests count nodes instead of
               time, so their time control says little about game length
    SPSA       parameter tuning
    VVLTC      more than one thread at an LTC base time (60+0.6 with 8 threads)
    SMP        more than one thread at a shorter base time (5+0.05, 20+0.2)
    VLTC       one thread, base time of 180 s or more (180+1.8)
    LTC        one thread, base time of 40 s or more (60+0.6)
    STC        everything else (10+0.1)
    """
    if uses_nodestime(args):
        return "Nodestime"
    if "spsa" in args:
        return "SPSA"
    if tc_base is None:
        tc_base = base_time(args["tc"])
    if int(args.get("threads", 1)) > 1:
        return "VVLTC" if tc_base >= LTC_MIN_BASE_TIME else "SMP"
    if tc_base >= VLTC_MIN_BASE_TIME:
        return "VLTC"
    return "LTC" if tc_base >= LTC_MIN_BASE_TIME else "STC"


def bounds_kind(elo0: str | float | None, elo1: str | float | None) -> str:
    """'simplification' or 'gainer', by fishtest's own rule for coloring a passed
    test blue rather than green: the SPRT bounds add up to less than zero
    ([-1.75, 0.25] is a simplification; [0, 2] and [0.5, 2.5] are gainers).
    '' when the test has no SPRT bounds."""
    if elo0 in (None, "") or elo1 in (None, ""):
        return ""
    return "simplification" if float(elo0) + float(elo1) < 0 else "gainer"


def is_net_test(args: dict) -> bool:
    """A test of a new network: the new side plays with different NNUE nets than the base.
    Every other test changes code (search, evaluation, time management, ...)."""
    return sorted(args.get("new_nets") or []) != sorted(args.get("base_nets") or [])


def games_played(run: dict) -> int:
    r = run["results"]
    return r["wins"] + r["losses"] + r["draws"]


def core_hours(run: dict) -> float:
    """Compute spent on a run so far, using fishtest's game-length estimate."""
    args = run["args"]
    return estimate_game_duration(args["tc"]) * games_played(run) * int(args.get("threads", 1)) / 3600


# ---------------------------------------------------------------- summaries

@dataclass
class Snapshot:
    tests_active: int
    tests_pending: int
    authors_active: int
    workers: int
    cores: int
    cores_stc: int
    cores_ltc: int
    cores_vltc: int
    cores_vvltc: int
    cores_smp: int
    cores_nodestime: int
    cores_spsa: int
    tests_net: int
    cores_net: int
    mnps: float
    games_per_min: float
    pending_hours: float


def summarize(runs: dict) -> Snapshot:
    """Aggregate /api/active_runs the way the fishtest homepage does."""
    unfinished = [r for r in runs.values() if not r.get("finished")]
    active = [r for r in unfinished if r.get("workers", 0) > 0]

    cores = sum(r.get("cores", 0) for r in active)
    pending_hours = 0.0
    if cores > 0:
        for run in unfinished:
            try:
                pending_hours += remaining_hours(run) / cores
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue  # one odd run should not break the whole check

    by_type = Counter()
    for r in active:
        by_type[classify_test(r["args"], r.get("tc_base"))] += r.get("cores", 0)
    net = [r for r in active if is_net_test(r["args"])]

    return Snapshot(
        tests_active=len(active),
        tests_pending=len(unfinished) - len(active),
        authors_active=len({r["args"].get("username") for r in active}),
        workers=sum(r.get("workers", 0) for r in active),
        cores=cores,
        **{TYPE_COLUMNS[t]: by_type[t] for t in TEST_TYPES},
        tests_net=len(net),
        cores_net=sum(r.get("cores", 0) for r in net),
        mnps=round(sum(r.get("nps", 0.0) for r in active) / 1e6, 1),
        games_per_min=round(sum(r.get("games_per_minute", 0.0) for r in active), 1),
        pending_hours=round(pending_hours, 1),
    )


def llr_distribution(runs: dict) -> tuple[int, float | None, list[int]]:
    """How far running SPRT tests have moved toward passing or failing.

    Returns the number of tests, their median LLR and counts in LLR_BINS equal
    bins from the rejection bound (bin 0) to the acceptance bound (last bin).
    Each LLR is scaled by its test's own bounds onto the standard +/-2.94, so
    tests with unusual alpha/beta line up with the rest.
    """
    counts = [0] * LLR_BINS
    scaled = []
    for run in runs.values():
        sprt = run["args"].get("sprt")
        if run.get("finished") or not sprt or run.get("workers", 0) <= 0:
            continue
        llr, lower, upper = sprt.get("llr"), sprt.get("lower_bound"), sprt.get("upper_bound")
        if llr is None or not lower or not upper:
            continue
        x = llr / upper if llr >= 0 else llr / abs(lower)  # -1 .. 1
        x = max(-1.0, min(1.0, x))
        counts[min(LLR_BINS - 1, int((x + 1) / 2 * LLR_BINS))] += 1
        scaled.append(x * STANDARD_LLR_BOUND)
    median = statistics.median(scaled) if scaled else None
    return len(scaled), median, counts


def _common_fields(run: dict) -> dict[str, str]:
    args = run["args"]
    sprt = args.get("sprt", {})
    elo = get_elo(run["results"]["pentanomial"]) if "pentanomial" in run["results"] else None
    return {
        "id": str(run["_id"]),
        "username": args.get("username", ""),
        "branch": args.get("new_tag", ""),
        "type": classify_test(args, run.get("tc_base")),
        "net": "1" if is_net_test(args) else "0",
        "tc": args.get("tc", ""),
        "threads": str(args.get("threads", 1)),
        "elo0": f"{sprt['elo0']:g}" if "elo0" in sprt else "",
        "elo1": f"{sprt['elo1']:g}" if "elo1" in sprt else "",
        "games": str(games_played(run)),
        "elo": f"{elo[0]:.2f}" if elo else "",
        "elo95": f"{elo[1]:.2f}" if elo else "",
    }


ACTIVE_FIELDS = ["id", "username", "branch", "type", "net", "tc", "threads", "elo0", "elo1", "games",
                 "elo", "elo95", "cores", "workers", "llr", "started"]
FINISHED_FIELDS = ["id", "finished", "started", "username", "branch", "type", "net", "tc", "threads",
                   "elo0", "elo1", "outcome", "games", "elo", "elo95", "core_hours"]


def active_record(run: dict) -> dict[str, str]:
    """One row of data/active_runs.csv (the latest snapshot)."""
    sprt = run["args"].get("sprt", {})
    return {
        **_common_fields(run),
        "cores": str(run.get("cores", 0) if run.get("workers", 0) > 0 else 0),
        "workers": str(run.get("workers", 0)),
        "llr": f"{sprt['llr']:.2f}" if "llr" in sprt else "",
        "started": iso(parse_time(run["start_time"])),
    }


def finished_record(run: dict) -> dict[str, str]:
    """One row of data/finished_runs.csv."""
    args = run["args"]
    if run.get("failed"):
        outcome = "failed"
    elif "sprt" in args:
        state = args["sprt"].get("state", "")
        outcome = {"accepted": "passed", "rejected": "rejected"}.get(state, "stopped")
    elif games_played(run) >= args.get("num_games", 0) > 0:
        outcome = "completed"
    else:
        outcome = "stopped"

    return {
        **_common_fields(run),
        "finished": iso(parse_time(run["last_updated"])),
        "started": iso(parse_time(run["start_time"])),
        "outcome": outcome,
        "core_hours": f"{core_hours(run):.1f}",
    }

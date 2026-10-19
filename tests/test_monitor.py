import gzip
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from monitor import charts, collect, fishtest, machines, readme, store
from monitor.store import Check

NOW = datetime(2026, 10, 19, 4, 17, tzinfo=UTC)


def make_run(**overrides):
    run = {
        "_id": "abc",
        "args": {
            "tc": "10+0.1", "threads": 1, "num_games": 1000, "username": "someone",
            "sprt": {"llr": 0.5, "alpha": 0.05, "beta": 0.05, "state": "", "elo0": 0.0,
                     "elo1": 2.0},
        },
        "results": {"wins": 100, "losses": 90, "draws": 210, "pentanomial": [5, 40, 100, 50, 5]},
        "workers": 2, "cores": 16, "tc_base": 10.0, "nps": 5e6, "games_per_minute": 30.0,
        "start_time": "2026-10-18 01:00:00+00:00",
        "last_updated": "2026-10-19 04:16:50.5+00:00",
        "finished": False, "failed": False,
    }
    run.update(overrides)
    return run


def make_check(hours_ago, status="up", **values):
    defaults = dict(tests_active=50, tests_pending=2, authors_active=20, workers=170,
                    contributors=55, cores=1600,
                    cores_stc=900, cores_ltc=300, cores_vltc=50, cores_vvltc=150, cores_smp=60,
                    cores_nodestime=100, cores_spsa=40, tests_net=0, cores_net=0,
                    mnps=900.0,
                    games_per_min=1200.0, pending_hours=50.0, latency_ms=500)
    if status == "down":
        return Check(time=NOW - timedelta(hours=hours_ago), status="down", reason="HTTP 502")
    defaults.update(values)
    return Check(time=NOW - timedelta(hours=hours_ago), status=status, **defaults)


class FishtestFormulas(unittest.TestCase):
    def test_game_duration(self):
        self.assertAlmostEqual(fishtest.estimate_game_duration("10+0.1"), (10 + 6.8) * 1.84)
        self.assertAlmostEqual(fishtest.estimate_game_duration("60+0.6"), (60 + 40.8) * 1.84)
        self.assertAlmostEqual(fishtest.estimate_game_duration("1:00+0.6"), (60 + 40.8) * 1.84)
        self.assertAlmostEqual(fishtest.estimate_game_duration("40/10"), 10 * 68 / 40 * 1.84)

    def test_remaining_hours_fixed_games(self):
        run = make_run()
        del run["args"]["sprt"]
        expected = 600 * fishtest.estimate_game_duration("10+0.1") / 3600
        self.assertAlmostEqual(fishtest.remaining_hours(run), expected)

    def test_remaining_hours_sprt_done(self):
        run = make_run()
        run["args"]["sprt"]["llr"] = 3.0
        self.assertEqual(fishtest.remaining_hours(run), 0)

    def test_remaining_hours_sprt_in_progress(self):
        self.assertGreater(fishtest.remaining_hours(make_run()), 0)


class Summaries(unittest.TestCase):
    def test_summarize(self):
        ltc = make_run(workers=1, cores=8, tc_base=60.0)
        ltc["args"] = {**ltc["args"], "tc": "60+0.6", "username": "other"}
        runs = {
            "a": make_run(),
            "b": ltc,
            "c": make_run(workers=0, cores=0, nps=0, games_per_minute=0),
        }
        snap = fishtest.summarize(runs)
        self.assertEqual((snap.tests_active, snap.tests_pending, snap.authors_active), (2, 1, 2))
        self.assertEqual((snap.workers, snap.cores), (3, 24))
        self.assertEqual([getattr(snap, fishtest.TYPE_COLUMNS[t]) for t in fishtest.TEST_TYPES],
                         [16, 8, 0, 0, 0, 0, 0, 0])
        self.assertEqual(snap.mnps, 10.0)
        self.assertEqual(snap.games_per_min, 60.0)
        self.assertGreater(snap.pending_hours, 0)

    def test_summarize_idle(self):
        snap = fishtest.summarize({})
        self.assertEqual((snap.workers, snap.cores, snap.pending_hours), (0, 0, 0))

    def test_classify(self):
        self.assertEqual(fishtest.classify_test({"tc": "10+0.1", "threads": 1}), "STC")
        self.assertEqual(fishtest.classify_test({"tc": "60+0.6", "threads": 1}), "LTC")
        self.assertEqual(fishtest.classify_test({"tc": "40+0.4"}), "LTC")  # fishtest's boundary
        self.assertEqual(fishtest.classify_test({"tc": "5+0.05", "threads": 8}), "SMP")
        self.assertEqual(fishtest.classify_test({"tc": "60+0.6", "spsa": {}}), "SPSA")
        self.assertEqual(fishtest.classify_test({"tc": "1:00+0.6"}, tc_base=60.0), "LTC")
        self.assertEqual(fishtest.classify_test({"tc": "1:00+0.6"}), "LTC")  # minutes:seconds
        self.assertEqual(fishtest.classify_test({"tc": "180+1.8"}), "VLTC")
        self.assertEqual(fishtest.classify_test({"tc": "100+1"}), "LTC")
        self.assertEqual(fishtest.classify_test({"tc": "60+0.6", "threads": 8}), "VVLTC")
        self.assertEqual(fishtest.classify_test({"tc": "60+0.6", "threads": 6}), "VVLTC")
        self.assertEqual(fishtest.classify_test({"tc": "20+0.2", "threads": 8}), "SMP")
        prog = {"tc": "60+0.6", "threads": 8, "num_games": 60000, "new_tag": "master",
                "base_tag": "edb0d9db6731067ec50ce619ff372b463bc4dd5d",
                "info": 'SMP Progression test of "Update NNUE architecture" of Oct 17'}
        # A fork's own master over 60,000 games, but not a progression test.
        self.assertEqual(fishtest.classify_test({**prog, "threads": 1, "info": "Yeet psqt weights"}),
                         "LTC")
        self.assertEqual(fishtest.classify_test(prog), "Progtest")
        self.assertEqual(fishtest.classify_test({**prog, "threads": 1}), "Progtest")
        self.assertEqual(fishtest.classify_test({**prog, "new_tag": "my-branch", "base_tag": "master"}),
                         "VVLTC")  # a branch against master is not a progression test
        self.assertEqual(fishtest.classify_test({**prog, "num_games": 20000}), "VVLTC")
        self.assertEqual(fishtest.classify_test({**prog, "sprt": {}}), "VVLTC")
        self.assertEqual(fishtest.classify_test({**prog, "tc": "10+0.1", "threads": 1}), "STC")

    def test_nodestime(self):
        ltc = {"tc": "100+1", "threads": 1}
        self.assertEqual(fishtest.classify_test({**ltc, "new_options": "Hash=16 nodestime=60",
                                                 "base_options": "Hash=16 nodestime=60"}), "Nodestime")
        # Either side counts, the option name is case-insensitive, and it beats SMP.
        self.assertEqual(fishtest.classify_test({**ltc, "base_options": "NodesTime=40"}), "Nodestime")
        self.assertEqual(fishtest.classify_test({"tc": "5+0.05", "threads": 8,
                                                 "new_options": "nodestime=10"}), "Nodestime")
        # nodestime=0 is Stockfish's default and means the option is off.
        self.assertEqual(fishtest.classify_test({**ltc, "new_options": "Hash=16 nodestime=0"}), "LTC")
        self.assertEqual(fishtest.classify_test({**ltc, "new_options": None}), "LTC")

    def test_llr_distribution(self):
        def sprt_run(llr, workers=1, lower=-2.944, upper=2.944):
            run = make_run(workers=workers)
            run["args"]["sprt"].update(llr=llr, lower_bound=lower, upper_bound=upper)
            return run

        runs = {
            "reject": sprt_run(-2.9), "pass": sprt_run(2.9), "zero": sprt_run(0.0),
            "beyond": sprt_run(3.5),  # overshoot is clamped into the top bin
            "waiting": sprt_run(1.0, workers=0),  # not running, so not counted
            "odd_bounds": sprt_run(-1.0, lower=-2.0, upper=2.0),  # halfway to its bound
        }
        tests, median, counts = fishtest.llr_distribution(runs)
        self.assertEqual(tests, 5)
        self.assertEqual(sum(counts), 5)
        self.assertEqual((counts[0], counts[8], counts[15]), (1, 1, 2))
        self.assertEqual(counts[4], 1)  # -0.5 of the way scales to -1.47
        self.assertAlmostEqual(median, 0.0)
        self.assertEqual(fishtest.llr_distribution({}), (0, None, [0] * 16))

    def test_net_tests(self):
        self.assertTrue(fishtest.is_net_test({"base_nets": ["nn-a.nnue"], "new_nets": ["nn-b.nnue"]}))
        self.assertFalse(fishtest.is_net_test({"base_nets": ["nn-a.nnue", "nn-s.nnue"],
                                               "new_nets": ["nn-s.nnue", "nn-a.nnue"]}))
        self.assertFalse(fishtest.is_net_test({}))
        net = make_run(workers=3, cores=30)
        net["args"] = {**net["args"], "base_nets": ["nn-a.nnue"], "new_nets": ["nn-b.nnue"]}
        snap = fishtest.summarize({"search": make_run(), "net": net})
        self.assertEqual((snap.tests_net, snap.cores_net), (1, 30))
        self.assertEqual(fishtest.active_record(net)["net"], "1")
        self.assertEqual(fishtest.finished_record(make_run(finished=True))["net"], "0")

    def test_bounds_kind(self):
        self.assertEqual(fishtest.bounds_kind("0", "2"), "gainer")
        self.assertEqual(fishtest.bounds_kind("0.5", "2.5"), "gainer")
        self.assertEqual(fishtest.bounds_kind("-1.75", "0.25"), "simplification")
        self.assertEqual(fishtest.bounds_kind("-0.5", "1.5"), "gainer")  # sums to more than 0
        self.assertEqual(fishtest.bounds_kind("", ""), "")

    def test_elo_matches_fishtest(self):
        # Reference values from fishtest's stat_util.get_elo (with scipy).
        elo, elo95 = fishtest.get_elo([1203, 17660, 39290, 18157, 1188])
        self.assertAlmostEqual(elo, 1.046820, places=5)
        self.assertAlmostEqual(elo95, 0.935902, places=5)
        self.assertIsNone(fishtest.get_elo([0, 0, 0, 0, 0]))

    def test_finished_outcomes(self):
        def outcome(**changes):
            run = make_run(finished=True)
            args = changes.pop("args", None)
            if args is not None:
                run["args"] = {**run["args"], **args}
            run.update(changes)
            return fishtest.finished_record(run)["outcome"]

        sprt = make_run()["args"]["sprt"]
        self.assertEqual(outcome(args={"sprt": {**sprt, "state": "accepted"}}), "passed")
        self.assertEqual(outcome(args={"sprt": {**sprt, "state": "rejected"}}), "rejected")
        self.assertEqual(outcome(), "stopped")
        self.assertEqual(outcome(failed=True), "failed")
        fixed = make_run(finished=True)
        del fixed["args"]["sprt"]
        fixed["args"]["num_games"] = 400
        self.assertEqual(fishtest.finished_record(fixed)["outcome"], "completed")
        record = fishtest.finished_record(fixed)
        self.assertEqual(record["finished"], "2026-10-19T04:16:50Z")
        self.assertEqual(record["games"], "400")


class FakeReply:
    def __init__(self, body, headers):
        self.body, self.headers = body, headers

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Fetching(unittest.TestCase):
    def fetch(self, reply=None, error=None):
        with mock.patch("urllib.request.urlopen", side_effect=error, return_value=reply):
            return fishtest.fetch_json("/api/active_runs")

    def test_gzip_json(self):
        reply = FakeReply(gzip.compress(b'{"a": 1}'),
                          {"Content-Encoding": "gzip", "Date": "Mon, 19 Oct 2026 04:17:00 GMT"})
        result = self.fetch(reply)
        self.assertEqual(result.data, {"a": 1})
        self.assertEqual(result.server_time, NOW)

    def test_bot_check_page(self):
        reply = FakeReply(b"<html><title>Checking your browser</title></html>", {})
        with self.assertRaisesRegex(fishtest.FetchError, "bot check"):
            self.fetch(reply)

    def test_http_error(self):
        error = urllib.error.HTTPError("u", 502, "Bad Gateway", {}, None)
        with self.assertRaisesRegex(fishtest.FetchError, "HTTP 502"):
            self.fetch(error=error)

    def test_timeout(self):
        with self.assertRaisesRegex(fishtest.FetchError, "no response"):
            self.fetch(error=TimeoutError())

    def test_retries_then_reports_attempts(self):
        with mock.patch("time.sleep"), mock.patch.object(
            fishtest, "fetch_json", side_effect=fishtest.FetchError("HTTP 503")
        ) as fetch:
            with self.assertRaisesRegex(fishtest.FetchError, r"HTTP 503 \(3 attempts\)"):
                fishtest.fetch_json_with_retries("/x")
        self.assertEqual(fetch.call_count, 3)


class Collecting(unittest.TestCase):
    def test_down_check(self):
        with mock.patch.object(collect, "fetch_json_with_retries",
                               side_effect=fishtest.FetchError("HTTP 502 (3 attempts)")):
            check, runs = collect.run_check(NOW)
        self.assertIsNone(runs)
        self.assertEqual((check.status, check.reason), ("down", "HTTP 502 (3 attempts)"))
        self.assertIsNone(check.cores)

    def test_finished_runs_stop_at_known(self):
        def page(n):
            # 50 runs per page, one hour apart, newest first.
            base = (n - 1) * 50
            return fishtest.Response({
                f"r{base + i}": make_run(_id=f"r{base + i}", finished=True,
                                         last_updated=str(NOW - timedelta(hours=base + i)))
                for i in range(50)
            }, 0.1, NOW)

        with tempfile.TemporaryDirectory() as tmp, mock.patch("time.sleep"):
            data = Path(tmp)
            with mock.patch.object(collect, "fetch_json_with_retries",
                                   side_effect=lambda path, **_: page(int(path.split("=")[1]))) as f:
                added = collect.update_finished(data, NOW)
            # First run backfills 35 days = 840 hourly runs, i.e. 17 pages.
            self.assertEqual(f.call_count, 17)
            self.assertEqual(added, 850)
            with mock.patch.object(collect, "fetch_json_with_retries",
                                   side_effect=lambda path, **_: page(int(path.split("=")[1]))) as f:
                added = collect.update_finished(data, NOW)
            self.assertEqual((f.call_count, added), (1, 0))  # page 1 reaches known runs


MACHINES_PAGE = """<table><thead><tr><th>Username</th></tr></thead><tbody>
<tr><td>alice</td><td><div class="flag flag-us"></div> 16 </td><td><a href="/workers/x">7305a1c</a></td>
<td>1.49</td><td>32195</td><td>Linux</td><td>x86-64-avx2</td><td>g++ 14</td><td>3.12</td><td>331</td>
<td><a href="/tests/view/6ad54e36733e627f7058c7b2?show_task=158" title="t">test1732/158</a></td>
<td data-sort-value="-1">seconds ago</td></tr>
<tr><td>bob</td><td>8</td><td><a href="/workers/y">bdbcd8e</a></td><td>1.1</td><td>16000</td>
<td>Windows 11</td><td>x86-64-bmi2</td><td>g++ 15</td><td>3.12</td><td>331</td>
<td><a href="/tests/view/6ad43985733e627f7058c6c8?show_task=85">depth/85</a></td>
<td>seconds ago</td></tr>
</tbody></table>"""
CHALLENGE_PAGE = """<title>Checking your browser</title><script>(function(){
  var challenge="020ced91191941d571ae4ee19d69ba28b357710988e0ea729ebd1edf04f20923",bits=8;
  var slot="82980",cap=4294967296;"""


class Machines(unittest.TestCase):
    def test_parse(self):
        workers = machines.parse_workers(MACHINES_PAGE)
        self.assertEqual([(w.username, w.cores, w.run) for w in workers],
                         [("alice", 16, "6ad54e36733e627f7058c7b2"),
                          ("bob", 8, "6ad43985733e627f7058c6c8")])
        empty = '<table><tbody><tr id="no-machines"><td colspan=20>No machines running</td></tr></tbody></table>'
        self.assertEqual(machines.parse_workers(empty), [])
        with self.assertRaises(fishtest.FetchError):
            machines.parse_workers("<html>something else</html>")

    def test_solve(self):
        challenge = "ab" * 32
        nonce = machines.solve(challenge, 12, 1 << 32)
        digest = __import__("hashlib").sha256(f"{challenge}:{nonce}".encode()).digest()
        self.assertTrue(machines._meets(digest, 12))
        self.assertEqual(int.from_bytes(digest[:2], "big") >> 4, 0)

    def test_fetch_passes_check(self):
        pages = [CHALLENGE_PAGE, MACHINES_PAGE]
        cookies = []

        def fake_page(path, cookie):
            cookies.append(cookie)
            return pages.pop(0)

        with mock.patch.object(machines, "_page", side_effect=fake_page):
            workers = machines.fetch_workers()
        self.assertEqual(len(workers), 2)
        self.assertIsNone(cookies[0])
        self.assertRegex(cookies[1], r"^__Host-fishtest-ac=82980\.\d+$")

    def test_rejected_cookie(self):
        with mock.patch.object(machines, "_page", return_value=CHALLENGE_PAGE):
            with self.assertRaisesRegex(fishtest.FetchError, "not accepted"):
                machines.fetch_workers()

    def test_breakdown_history(self):
        runs = {
            "a": make_run(_id="a", cores=16),
            "b": make_run(_id="b", cores=8),
            "w": make_run(_id="w", workers=0, cores=0),  # waiting: not counted
        }
        runs["b"]["args"] = {**runs["b"]["args"], "username": "other"}
        workers = [machines.Worker("alice", 16, "a"), machines.Worker("alice", 4, "b")]
        parts = collect.breakdown(runs, workers)
        self.assertEqual(parts, {"tests": {"a": 16, "b": 8},
                                 "authors": {"someone": [16, 1], "other": [8, 1]},
                                 "contributors": {"alice": [20, 2]}})
        self.assertNotIn("contributors", collect.breakdown(runs, None))

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            self.assertEqual(collect.snapshot_hours(data, NOW), 1.0)
            collect.update_breakdown(data, parts, NOW, 1.0)
            store.append_check(data, make_check(0))
            # A second run 15 minutes later only adds a quarter hour.
            later = NOW + timedelta(minutes=15)
            hours = collect.snapshot_hours(data, later)
            self.assertAlmostEqual(hours, 0.25)
            collect.update_breakdown(data, parts, later, hours)
            # Hourly rows older than the retention window are dropped.
            collect.update_breakdown(data, parts, later + timedelta(days=16), 1.0)
            hourly = store.read_jsonl(data, store.HOURLY_FILE)
            daily = store.read_rows(data, store.DAILY_FILE)
        self.assertEqual(len(hourly), 1)
        self.assertEqual(hourly[0]["authors"], {"someone": [16, 1], "other": [8, 1]})
        today = [r for r in daily if r["date"] == "2026-10-19"]
        self.assertEqual(today, [
            {"date": "2026-10-19", "kind": "author", "name": "other", "core_hours": "10.0",
             "count_hours": "1.2"},
            {"date": "2026-10-19", "kind": "author", "name": "someone", "core_hours": "20.0",
             "count_hours": "1.2"},
            {"date": "2026-10-19", "kind": "contributor", "name": "alice", "core_hours": "25.0",
             "count_hours": "2.5"},
        ])

    def test_collect_survives_machines_failure(self):
        runs = {"a": make_run()}
        reply = fishtest.Response(runs, 0.5, NOW)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(collect, "fetch_json_with_retries", return_value=reply), \
                mock.patch.object(collect, "fetch_workers",
                                  side_effect=fishtest.FetchError("proof-of-work check not accepted")), \
                mock.patch.object(collect, "update_finished", return_value=0):
            data = Path(tmp)
            check = collect.collect(data)
            self.assertEqual(check.status, "up")
            self.assertIsNone(check.contributors)
            self.assertEqual(len(store.read_rows(data, store.ACTIVE_FILE)), 1)
            self.assertFalse((data / store.WORKERS_FILE).exists())


class Storage(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            store.append_check(data, make_check(1))
            store.append_check(data, make_check(0, "down"))
            checks = store.read_checks(data)
        self.assertEqual(checks[0], make_check(1))
        self.assertEqual(checks[1], make_check(0, "down"))


class Migration(unittest.TestCase):
    def test_new_columns_rewrite_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / store.CHECKS_FILE).write_text(
                "time,status,reason,cores\n2026-10-19T03:00:00Z,up,,1500\n")
            store.append_check(data, make_check(0))
            checks = store.read_checks(data)
            header = (data / store.CHECKS_FILE).read_text().splitlines()[0]
        self.assertEqual(header, ",".join(store.CHECK_FIELDS))
        self.assertEqual([c.cores for c in checks], [1500, 1600])
        self.assertIsNone(checks[0].contributors)


class Charts(unittest.TestCase):
    def test_scales(self):
        self.assertEqual(charts.nice_ticks(1645, integer=True), [0, 500, 1000, 1500, 2000])
        self.assertEqual(charts.nice_ticks(2, integer=True), [0, 1, 2])
        self.assertEqual(charts.nice_ticks(2100, integer=True), [0, 500, 1000, 1500, 2000, 2500])
        self.assertEqual(charts.nice_ticks(57, integer=True), [0, 20, 40, 60])
        self.assertEqual(charts.nice_ticks(0), [0, 0.2, 0.4, 0.6, 0.8, 1])
        self.assertEqual(charts.nice_ticks(0, integer=True), [0, 1])
        self.assertEqual(charts.short(826.6e6), "827M")
        self.assertEqual(charts.short(1.234e9), "1.23G")
        ticks = charts.time_ticks(NOW - timedelta(days=14), NOW, 7)
        self.assertEqual(ticks[1][0] - ticks[0][0], timedelta(days=2))

    def test_contributors_chart_other_segment(self):
        theme = charts.THEMES["light"]
        active = [fishtest.active_record(make_run(_id="t1"))]
        workers = [{"username": "u1", "cores": "8", "run": "t1"},
                   {"username": "u1", "cores": "4", "run": "finished-meanwhile"}]
        svg = charts.contributors_chart(workers, active, theme)
        ET.fromstring(svg)
        self.assertIn(">Other<", svg)
        self.assertIn("u1 — 12 cores on 2 workers, playing 8 STC, 4 other", svg)

    def test_png(self):
        png = charts._png([[(255, 0, 0, 255), (0, 0, 0, 0)], [(0, 0, 255, 255), (1, 2, 3, 4)]])
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        width, height = __import__("struct").unpack(">II", png[16:24])
        self.assertEqual((width, height), (2, 2))
        idat = png.index(b"IDAT")
        length = int.from_bytes(png[idat - 4:idat], "big")
        raw = __import__("zlib").decompress(png[idat + 4:idat + 4 + length])
        self.assertEqual(raw, bytes([0, 255, 0, 0, 255, 0, 0, 0, 0, 0, 0, 0, 255, 255, 1, 2, 3, 4]))

    def test_llr_chart(self):
        rows = [{"time": f"2026-10-{d:02d}T{h:02d}:17:00Z", "tests": "4", "median": "0.3",
                 "counts": "1 0 0 0 0 0 0 1 1 0 0 0 0 0 0 1"}
                for d in (17, 18, 19) for h in range(0, 24, 3) if (d, h) <= (19, 3)]
        svg = charts.llr_chart(rows, NOW, charts.THEMES["dark"])
        ET.fromstring(svg)
        self.assertIn("data:image/png;base64,", svg)
        self.assertIn("4 SPRT tests running · median LLR +0.30", svg)
        self.assertIn("Oct 19, 03:00 UTC — 4 SPRT tests, median LLR +0.30", svg)
        ET.fromstring(charts.llr_chart([], NOW, charts.THEMES["light"]))

    def test_long_history(self):
        hourly = [make_check(h) for h in range(24 * 20, -1, -1)]
        self.assertTrue(charts.has_long_history(hourly))
        self.assertFalse(charts.has_long_history(hourly[-24 * 10:]))
        days = charts.daily_means(hourly)
        self.assertEqual(len(days), 21)
        self.assertEqual(days[-1].time, datetime(2026, 10, 19, 12, tzinfo=UTC))
        self.assertEqual((days[-1].cores, days[-1].mnps), (1600, 900.0))
        history = store.History(hourly, [], {}, [], [], [], [
            {"date": "2026-10-18", "kind": "author", "name": "a", "core_hours": "2400.0",
             "count_hours": "48.0"}])
        files = charts.render_all(history, NOW)
        for name in ("long-types", "long-activity", "long-authors", "long-author-tests",
                     "long-contributors"):
            ET.fromstring(files[f"{name}-dark.svg"])
        # Oct 18: 2,400 core-hours over 24 covered hours = 100 cores; nothing today.
        self.assertIn("a: 0 cores now, peak 100", files["long-authors-light.svg"])

    def test_neighbor_shades(self):
        # a, b, c are one type, x another. b ends before c starts, so at some point
        # a sits directly under c: all three must differ, while x needs no contrast.
        order = ["a", "b", "c", "x"]
        kind = {"a": 0, "b": 0, "c": 0, "x": 1}
        shade = charts.neighbor_shades(order, kind, [{"a", "b", "x"}, {"a", "c", "x"}, {"b", "c"}])
        self.assertEqual(len({shade["a"], shade["b"], shade["c"]}), 3)
        self.assertEqual(shade["x"], 0)

    def test_shade(self):
        self.assertEqual(charts.shade("#2a78d6", 0), "#2a78d6")  # round trip
        lighter, darker = charts.shade("#eda100", 0.05), charts.shade("#eda100", -0.05)
        self.assertGreater(int(lighter[1:3], 16), int(darker[1:3], 16))
        self.assertNotEqual(lighter, darker)

    def test_label_spread(self):
        # Close anchors are pushed apart; the stack shifts up if it hits the bottom.
        self.assertEqual(charts._spread([10, 12, 50], 0, 100, 13), [10, 23, 50])
        self.assertEqual(charts._spread([90, 95, 99], 0, 100, 13), [74, 87, 100])
        self.assertEqual(charts._spread([], 0, 100, 13), [])

    def test_band_charts(self):
        theme = charts.THEMES["light"]
        hourly = [{"time": f"2026-10-19T0{h}:17:00Z", "tests": {"t1": 100, "t2": 20 + h},
                   "authors": {"big": [100, 1], "small": [20 + h, 1]}} for h in range(4)]
        info = {"t1": {"username": "big", "branch": "master", "type": "VVLTC",
                       "started": "2026-10-18T00:00:00Z"},
                "t2": {"username": "small", "branch": "tweak", "type": "STC",
                       "started": "2026-10-19T00:00:00Z"}}
        svg = charts.tests_trend_chart(hourly, info, NOW, theme)
        ET.fromstring(svg)
        self.assertIn(">big · master<", svg)  # labelled inside its band
        self.assertIn("small · tweak (STC): peak 23 cores", svg)
        # Grouped by test type (STC below VVLTC, as in the type chart), not by start time.
        self.assertLess(svg.index("small · tweak (STC)"), svg.index("big · master (VVLTC)"))
        series = charts._hourly_series(hourly, "authors", 0)
        times = [charts.parse_iso(r["time"]) for r in hourly]
        svg = charts.entity_trend_chart(series, times, NOW, theme, days=14, max_gap=charts.MAX_GAP,
                                        what="authors", unit="cores", label="x", top=1)
        ET.fromstring(svg)
        self.assertIn("big: 100 cores now, peak 100", svg)
        self.assertIn("1 other authors: 23 cores now, peak 23", svg)

    def test_weekly_charts(self):
        theme = charts.THEMES["dark"]
        finished = {}
        for i in range(140):  # 417 days, more than the 52-week window
            t = NOW - timedelta(days=i * 3)
            finished[str(i)] = {"id": str(i), "finished": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "outcome": "passed" if i % 5 == 0 else "rejected",
                                "type": "LTC" if i % 3 == 0 else "STC", "core_hours": "1680.0",
                                "elo0": "0", "elo1": "2"}
        finished["s"] = {"id": "s", "finished": "2026-10-12T10:00:00Z", "outcome": "passed",
                         "elo0": "-1.75", "elo1": "0.25", "type": "STC", "core_hours": "1"}
        svg = charts.weekly_finished_chart(finished, NOW, theme)
        ET.fromstring(svg)
        self.assertIn("tests in 52 weeks", svg)
        self.assertNotIn("SPRT pass rate", svg)  # no per-week rate panel any more
        self.assertIn("Simplifications passed 1", svg)
        self.assertIn('fill="#66ccff"', svg)
        svg = charts.weekly_finished_chart(finished, NOW, theme, kind="gainer")
        self.assertNotIn("Simplifications passed", svg)  # legend lists only what occurs
        finished["n"] = {**finished["s"], "id": "n", "net": "1", "elo0": "0", "elo1": "2"}
        svg = charts.weekly_finished_chart(finished, NOW, theme, kind="net")
        self.assertIn("Net tests · 1 tests in 52 weeks", svg)
        self.assertEqual(len(charts._week_list(finished, NOW, 52)), 52)

    def test_weekly_types_chart(self):
        theme = charts.THEMES["light"]
        # Three weeks of hourly checks; LTC doubles in the last week.
        checks = [make_check(h, cores_ltc=600 if h < 24 * 5 else 300) for h in range(24 * 17, -1, -1)]
        svg = charts.weekly_types_chart(checks, NOW, theme)
        ET.fromstring(svg)
        self.assertEqual(svg.count("hourly snapshots —"), 4)  # Oct 2 (a Friday) to Oct 19: 4 weeks
        self.assertIn("Week of Oct 19 (so far): 1,900 cores on average from 5 hourly snapshots", svg)
        self.assertIn("1,900 cores on average this week", svg)
        ET.fromstring(charts.weekly_types_chart([], NOW, theme))

    def test_type_counts_chart(self):
        hourly = [{"time": f"2026-10-19T0{h}:17:00Z", "tests": {"a": 10, "b": 20, "c": 200}}
                  for h in range(4)]
        info = {"a": {"type": "STC"}, "b": {"type": "STC"}, "c": {"type": "Progtest"}}
        svg = charts.breakdown_types_chart(hourly, info, [], NOW, charts.THEMES["light"], count=True)
        ET.fromstring(svg)
        self.assertIn("3 tests running", svg)
        self.assertIn(">STC 2<", svg)
        self.assertIn(">Progtest 1<", svg)
        # Cores use the same lookup, so a reclassified test moves in the whole history.
        svg = charts.breakdown_types_chart(hourly, info, [], NOW, charts.THEMES["light"])
        self.assertIn("230 cores in total", svg)
        self.assertIn(">Progtest 200<", svg)

    def test_net_search_chart(self):
        checks = [make_check(h, tests_net=8 + h % 3) for h in range(30, -1, -1)]
        checks[0] = make_check(31, tests_net=None)  # older checks lack the column: skipped
        svg = charts.net_search_chart(checks, NOW, charts.THEMES["light"])
        ET.fromstring(svg)
        self.assertIn("8 of 50 running tests are net tests", svg)
        self.assertIn("Net tests: 8 running now, peak 10", svg)
        self.assertIn(">Search tests<", svg)

    def test_throughput_chart(self):
        finished = {str(i): {"id": str(i), "games": "1000", "outcome": "rejected",
                             "finished": (NOW - timedelta(hours=1, minutes=10 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")}
                    for i in range(12)}  # 12 tests over the 2 hours before the current one
        finished["empty"] = {**finished["0"], "id": "empty", "games": "0"}  # stopped at once
        svg = charts.throughput_chart(finished, NOW, charts.THEMES["light"])
        ET.fromstring(svg)
        self.assertIn("0.50 per hour now · 12 in 14 days", svg)  # 12 tests / 24 hours

    def test_runtime_trend_chart(self):
        finished = {}
        for i, hours in enumerate([2, 4, 24, 48, 300]):
            end = NOW - timedelta(days=8)
            finished[str(i)] = {"id": str(i), "games": "1000", "outcome": "rejected",
                                "finished": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "started": (end - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        # History starting on a Monday, so the first week counts as complete.
        finished["old"] = {"id": "old", "games": "1000", "finished": "2026-09-28T00:00:00Z",
                           "started": "2026-09-27T14:00:00Z"}
        svg = charts.runtime_trend_chart(finished, NOW, charts.THEMES["dark"])
        ET.fromstring(svg)
        self.assertIn("data:image/png;base64,", svg)
        self.assertIn("Week of Oct 5: 5 tests, median 24 h, 90% within 2.0 days", svg)
        self.assertIn("6 tests in 4 weeks · median 17 h lately", svg)  # 2, 4, 10, 24, 48, 300 h

    def test_runtime_chart(self):
        def running(hours_ago, kind, test_id):
            return {"id": test_id, "cores": "8", "type": kind,
                    "started": (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        active = [running(0.5, "STC", "a"), running(2, "STC", "b"), running(30, "LTC", "c"),
                  running(400, "VVLTC", "d"), {**running(5, "STC", "w"), "cores": "0"}]
        svg = charts.runtime_chart(active, NOW, charts.THEMES["light"])
        ET.fromstring(svg)
        self.assertIn("4 running tests · median 16 h since submitted", svg)
        self.assertIn("Running for 2w+: 1 test — 1 VVLTC", svg)
        self.assertIn("Running for 1–2d: 1 test — 1 LTC", svg)

    def test_header_wraps_when_crowded(self):
        theme = charts.THEMES["light"]
        _, one_line = charts._header(theme, charts._type_items(theme, [0, 1]), "1,644 cores in total")
        crowded = charts._type_items(theme, range(8), [1740, 252, 30, 400, 236, 0, 16, 0])
        parts, two_lines = charts._header(theme, crowded, "54 tests running · median 16 cores")
        self.assertEqual(two_lines - one_line, 24)
        self.assertIn("SPSA 0", "".join(parts))

    def test_render_edge_cases(self):
        histories = {
            "empty": [],
            "one check": [make_check(0)],
            "only down": [make_check(0, "down")],
            "mixed": [make_check(h, "down" if h in (5, 6) else "up") for h in range(400, -1, -1)],
        }
        finished = {"x": {"id": "x", "finished": "2026-10-18T10:00:00Z", "outcome": "passed",
                          "games": "1000"}}
        active = [fishtest.active_record(make_run(_id=f"t{i}", cores=8 * i)) for i in range(1, 30)]
        active.append(fishtest.active_record(make_run(_id="w", workers=0, cores=0)))
        for name, checks in histories.items():
            for snapshot in (active, []):
                workers = [{"username": "u1", "cores": "8", "run": "t1"},
                           {"username": "u2", "cores": "4", "run": "gone"}] if snapshot else []
                llr = [{"time": "2026-10-19T03:00:00Z", "tests": "3", "median": "-0.5",
                        "counts": "1 0 0 0 0 0 0 1 0 0 0 0 0 0 0 1"}] if snapshot else []
                hourly = [{"time": "2026-10-19T03:00:00Z", "tests": {"t1": 8},
                           "authors": {"someone": [8, 1]}, "contributors": {"u1": [8, 1]}}
                          ] if snapshot else []
                history = store.History(checks, snapshot, finished, workers, llr, hourly, [])
                for file, svg in charts.render_all(history, NOW).items():
                    with self.subTest(history=name, file=file, tests=len(snapshot)):
                        root = ET.fromstring(svg)
                        self.assertTrue(root.tag.endswith("svg"))
                        self.assertNotIn("nan", svg.lower())


class Readme(unittest.TestCase):
    def finished(self):
        rows = {}
        for i, outcome in enumerate(["passed", "rejected", "stopped", "passed"]):
            run = make_run(_id=f"f{i}", finished=True,
                           last_updated=str(NOW - timedelta(days=i + 1)))
            run["args"]["sprt"]["state"] = {"passed": "accepted"}.get(outcome, outcome)
            if outcome == "stopped":
                run["args"]["sprt"]["state"] = ""
            run["args"]["username"] = "a_b*c" if i == 3 else "someone"
            if i == 3:
                run["args"]["tc"], run["tc_base"] = "100+1", 100.0
                run["args"]["new_options"] = "Hash=16 nodestime=60"
            rows[f"f{i}"] = fishtest.finished_record(run)
        return rows

    def test_full_report(self):
        checks = [make_check(h) for h in range(48, -1, -1)]
        active = [fishtest.active_record(make_run())]
        workers = [{"username": "donor", "cores": "64", "run": "abc"}]
        daily = [{"date": "2026-10-18", "kind": "contributor", "name": "donor",
                  "core_hours": "1536.0", "count_hours": "24.0"},
                 {"date": "2026-10-18", "kind": "author", "name": "someone",
                  "core_hours": "384.0", "count_hours": "24.0"}]
        llr = [{"time": "2026-10-19T04:00:00Z", "tests": "1", "median": "0.5",
                "counts": "0 0 0 0 0 0 0 0 1 0 0 0 0 0 0 0"}]
        hourly = [{"time": "2026-10-19T04:00:00Z", "tests": {"abc": 16},
                   "authors": {"someone": [16, 1]}, "contributors": {"donor": [64, 1]}}]
        history = store.History(checks, active, self.finished(), workers, llr, hourly, daily)
        text = readme.build_readme(history, NOW, "https://example.com/c")
        self.assertTrue(text.startswith("# Fishtest status"))
        self.assertIn("**50 tests** from **20 authors**", text)
        self.assertIn("https://example.com/c/tests-dark.svg", text)
        self.assertIn("**2 passed** (1 STC, 1 nodestime)", text)
        self.assertIn("| 100+1\u00a0nodestime |", text)
        self.assertIn("SPRT pass rate 66.7%: 66.7% for gainers (3); 66.7% for search tests (3)", text)
        for chart in ("weekly-net", "weekly-search", "throughput", "runtime", "runtime-trend",
                      "net-search-trend", "type-counts"):
            self.assertIn(f"/{chart}-light.svg", text)
        self.assertIn("| Net tests running | 0 |", text)
        for chart in ("weekly-gainers", "weekly-simplifications"):
            self.assertIn(f"/{chart}-dark.svg", text)
        self.assertIn(r"[a\_b\*c](https://tests.stockfishchess.org/tests/user/a_b%2Ac)", text)
        self.assertIn("/tests/view/f0", text)
        self.assertNotIn("WARNING", text)
        headings = [line for line in text.splitlines() if line.startswith("## ")]
        self.assertEqual(headings, ["## Trends", "## Right now", "## Last 30 days in detail"])
        for chart in ("tests-trend", "authors-trend", "author-tests-trend", "contributors-trend",
                      "weekly-finished"):
            self.assertIn(f"/{chart}-light.svg", text)
        self.assertLess(text.index("weekly-simplifications-light.svg"), text.index("## Right now"))
        self.assertIn("/weekly-types-light.svg", text)
        self.assertNotIn("long-types", text)  # only 2 days of hourly history
        # No static explanations: the only small print is the update time.
        self.assertEqual([line for line in text.splitlines() if "<sub>" in line],
                         ["<sub>Updated Mon 19 Oct 2026, 04:17 UTC</sub>"])
        self.assertIn("| Nodes per second | 900M |", text)
        self.assertLess(text.index("types-light.svg"), text.index("tests-light.svg"))
        self.assertLess(text.index("llr-light.svg"), text.index("## Right now"))
        self.assertIn("(170 workers from 55 contributors)", text)
        self.assertIn("https://example.com/c/contributors-light.svg", text)
        self.assertIn("| [donor](https://tests.stockfishchess.org/tests/user/donor) | 1,536 | 100.0% | 64 | 1 |",
                      text)

    def test_api_down_warning(self):
        checks = [make_check(2), make_check(1), make_check(0, "down")]
        text = readme.build_readme(store.History(checks), NOW, "x")
        self.assertIn("[!WARNING]", text)
        self.assertIn("HTTP 502", text)

    def test_no_data(self):
        self.assertIn("No data yet", readme.build_readme(store.History(), NOW, "x"))


if __name__ == "__main__":
    unittest.main()

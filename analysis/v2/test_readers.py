"""What makes a process good (C5): the .done file, the run whose data count, void runs and section 1's settings.

Every case is one synthetic process in a temporary folder, written as the runner leaves it
(synth.write_process), so each check is shown to fail on its own.
"""

import copy
import os
import shutil
import tempfile
import unittest

import grid
import plan
import readers
import synth

LINUX_S = plan.s_bt_entries("linux")
WINDOWS_S = plan.s_bt_entries("windows")


def first_of(entries, family, fragment):
    return [e for e in entries if e["family"] == family and fragment in e["id"]][0]


FIXED = first_of(LINUX_S, "S", "S-epoll-none-W1-B1ms-p0")
DELAYING = first_of(LINUX_S, "S", "S-epoll-absent_wake-W1-B1ms-p0")
WRONG_WORKER = first_of(LINUX_S, "S", "S-epoll-wrong_worker-W2-B1ms-p0")
BATCH = first_of(LINUX_S, "Bt", "Bt-epoll-undrained_edge-W1-B1ms-p0")
WIN_FIXED = first_of(WINDOWS_S, "S", "S-iocp-none-W1-B1ms-p0")
SWEEP = plan.sweep_entries("windows")[0]
H4 = plan.h4_entries("linux", {c: 14 for c in grid.h4_cells("linux")})[0]


def lines_for(entry, system="linux", **kw):
    delays = kw.pop("delays", None)
    if entry["tool"] == "abpair":
        return synth.abpair_output(entry, kw.pop("ratios", [1.0, 1.0]), **kw)
    posts = int(readers.arg_value(entry["args"], "--posts"))
    if system == "windows":
        kw.setdefault("coarsest_timer_us", synth.WINDOWS_TIMER_US)
    return synth.wakeprobe_output(entry, delays or [1000] * posts, system=system, **kw)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p1v2-readers-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def resolve(self, entry, system="linux", **kw):
        synth.write_process(self.tmp, entry, **kw)
        return readers.Resolved(entry, self.tmp, system)

    def done_file(self, entry, text, system="linux"):
        with open(os.path.join(self.tmp, entry["id"] + ".done"), "wb") as f:
            f.write(text)
        return readers.Resolved(entry, self.tmp, system)


class ParseDone(unittest.TestCase):
    def test_what_the_runner_writes(self):
        self.assertEqual(readers.parse_done("outcome: ok\nattempt 1 exit: 0\n"), ("ok", (0,)))
        self.assertEqual(readers.parse_done("outcome: ok after rerun\nattempt 1 exit: 6\nattempt 2 exit: 0\n"),
                         ("ok after rerun", (6, 0)))
        self.assertEqual(readers.parse_done("outcome: incomplete\nattempt 1 exit: -11\nattempt 2 exit: 3221225477\n"),
                         ("incomplete", (-11, 3221225477)))

    def test_a_timeout_is_a_failed_run(self):
        self.assertEqual(readers.parse_done("outcome: ok after rerun\nattempt 1 exit: timeout\nattempt 2 exit: 0\n"),
                         ("ok after rerun", ("timeout", 0)))
        self.assertEqual(readers.parse_done("outcome: incomplete\nattempt 1 exit: timeout\nattempt 2 exit: timeout\n"),
                         ("incomplete", ("timeout", "timeout")))
        # ok means attempt 1 exited 0: a timeout there cannot be ok.
        with self.assertRaises(ValueError):
            readers.parse_done("outcome: ok\nattempt 1 exit: timeout\n")

    def test_everything_else_is_refused(self):
        bad = {
            "empty": "",
            "no final line end": "outcome: ok\nattempt 1 exit: 0",
            # Without the final line end the last digit would be dropped and the rest would still parse.
            "no final line end, two digits": "outcome: incomplete\nattempt 1 exit: 6\nattempt 2 exit: 61",
            "CRLF": "outcome: ok\r\nattempt 1 exit: 0\r\n",
            "blank line": "outcome: ok\n\nattempt 1 exit: 0\n",
            "no outcome": "attempt 1 exit: 0\n",
            "unknown outcome": "outcome: fine\nattempt 1 exit: 0\n",
            "outcome in capitals": "outcome: OK\nattempt 1 exit: 0\n",
            "no attempt": "outcome: ok\n",
            "attempts in the wrong order": "outcome: ok after rerun\nattempt 2 exit: 0\nattempt 1 exit: 6\n",
            "a third attempt": "outcome: incomplete\nattempt 1 exit: 6\nattempt 2 exit: 6\nattempt 3 exit: 6\n",
            "an unknown key": "outcome: ok\nattempt 1 exit: 0\nseconds: 3\n",
            "a repeated attempt": "outcome: incomplete\nattempt 1 exit: 6\nattempt 1 exit: 6\n",
            "a rerun after success": "outcome: ok\nattempt 1 exit: 0\nattempt 2 exit: 0\n",
            "no rerun after failure": "outcome: incomplete\nattempt 1 exit: 6\n",
            "outcome ok after a failure": "outcome: ok\nattempt 1 exit: 6\nattempt 2 exit: 0\n",
            "outcome ok after rerun, rerun failed": "outcome: ok after rerun\nattempt 1 exit: 6\nattempt 2 exit: 6\n",
            "incomplete, rerun passed": "outcome: incomplete\nattempt 1 exit: 6\nattempt 2 exit: 0\n",
            "a leading zero": "outcome: ok\nattempt 1 exit: 00\n",
            "a plus sign": "outcome: ok after rerun\nattempt 1 exit: +6\nattempt 2 exit: 0\n",
            "minus zero": "outcome: ok\nattempt 1 exit: -0\n",
            "timeout in capitals": "outcome: ok after rerun\nattempt 1 exit: Timeout\nattempt 2 exit: 0\n",
            "a float": "outcome: ok\nattempt 1 exit: 0.0\n",
            "trailing space": "outcome: ok \nattempt 1 exit: 0\n",
            "no space after the colon": "outcome: ok\nattempt 1 exit:0\n",
        }
        for name, text in bad.items():
            with self.assertRaises(ValueError, msg=name):
                readers.parse_done(text)

    def test_used_attempt(self):
        self.assertEqual(readers.used_attempt((0,)), 1)
        self.assertEqual(readers.used_attempt((6, 0)), 2)
        self.assertEqual(readers.used_attempt(("timeout", 0)), 2)
        self.assertIsNone(readers.used_attempt((6, 6)))
        self.assertIsNone(readers.used_attempt(("timeout", "timeout")))


class DoneFile(Case):
    def test_a_whole_process_is_good(self):
        r = self.resolve(FIXED, first=lines_for(FIXED))
        self.assertTrue(r.ok)
        self.assertEqual((r.used, r.problem, r.first_problem, r.rerun_problem, r.outcome, r.exits),
                         ("first", None, None, None, "ok", (0,)))

    def test_no_done_file_is_not_good_and_its_files_are_void(self):
        r = self.resolve(FIXED, first=lines_for(FIXED, lose_at=4), done=False)
        self.assertFalse(r.ok)
        self.assertEqual(r.problem, "no .done file")
        self.assertIsNone(r.first)
        self.assertEqual([(v.name, v.reason) for v in r.void], [("S-epoll-none-W1-B1ms-p0.jsonl", "no valid .done file")])
        self.assertEqual(r.lost_units(), {"first": 0, "rerun": 0, "void": 1})  # a lost unit is still counted

    def test_a_malformed_done_file_is_not_good(self):
        for text in (b"outcome: ok\nattempt 1 exit: 0", b"outcome: ok\r\nattempt 1 exit: 0\r\n", b"", b"\xff\xfe",
                     b"outcome: incomplete\nattempt 1 exit: 6\n"):
            r = self.done_file(FIXED, text)  # the first run's file is absent too; the problem is the .done file
            self.assertFalse(r.ok)
            self.assertTrue(r.problem.startswith("malformed .done file"), r.problem)

    def test_a_run_that_exited_zero_is_used(self):
        r = self.resolve(FIXED, first=lines_for(FIXED), rerun=lines_for(FIXED, delays=[2000] * 630), exits=[0])
        self.assertEqual((r.used, r.ok), ("first", True))
        self.assertEqual(r.run.path, os.path.join(self.tmp, FIXED["id"] + ".jsonl"))

    def test_a_failed_first_run_is_replaced_by_a_rerun_that_exited_zero(self):
        r = self.resolve(FIXED, first=lines_for(FIXED, lose_at=100), rerun=lines_for(FIXED))
        self.assertEqual((r.used, r.ok, r.exits), ("rerun", True, (6, 0)))
        self.assertEqual(r.first_problem, "exit 6: lost unit")  # the first run is reported with the rerun
        self.assertIsNone(r.rerun_problem)
        self.assertEqual(r.lost_units(), {"first": 1, "rerun": 0, "void": 0})
        self.assertEqual(r.run.path, os.path.join(self.tmp, FIXED["id"] + "-rerun.jsonl"))

    def test_a_timeout_is_failed_and_the_rerun_counts(self):
        r = self.resolve(FIXED, first=None, rerun=lines_for(FIXED), exits=["timeout", 0])
        self.assertEqual((r.used, r.ok, r.first_problem), ("rerun", True, "timeout"))
        # A timeout that left output: reported with it.
        r = self.resolve(FIXED, first=lines_for(FIXED, lose_at=3), rerun=lines_for(FIXED), exits=["timeout", 0])
        self.assertEqual(r.first_problem, "timeout: lost unit")

    def test_two_failed_runs_are_incomplete_whatever_their_output(self):
        r = self.resolve(FIXED, first=lines_for(FIXED, lose_at=100), rerun=lines_for(FIXED, lose_at=7))
        self.assertFalse(r.ok)
        self.assertIsNone(r.used)
        self.assertEqual(r.problem, "no run exited 0 (outcome: incomplete)")
        self.assertEqual(r.lost_units(), {"first": 1, "rerun": 1, "void": 0})
        r = self.resolve(FIXED, first=None, rerun=None, exits=["timeout", "timeout"])
        self.assertFalse(r.ok)

    def test_a_done_file_that_says_incomplete_overrules_a_whole_rerun(self):
        # The rerun crashed after it wrote its summary: its output is whole, its exit code is not 0.
        r = self.resolve(FIXED, first=lines_for(FIXED, lose_at=100), rerun=lines_for(FIXED), exits=[6, -11])
        self.assertFalse(r.ok)
        self.assertEqual(r.rerun_problem, "exit -11")
        self.assertIsNone(r.run)

    def test_a_run_that_exited_zero_with_a_bad_output_is_not_rescued(self):
        bad = lines_for(FIXED, summary={"error": "boom"})
        r = self.resolve(FIXED, first=bad, rerun=lines_for(FIXED), exits=[0])
        self.assertFalse(r.ok)
        self.assertEqual((r.used, r.problem), ("first", "error: boom"))

    def test_the_exit_code_does_not_make_a_missing_file_good(self):
        r = self.resolve(FIXED, first=None, exits=[0])
        self.assertEqual((r.ok, r.problem), (False, "no output"))

    def test_the_report(self):
        r = self.resolve(FIXED, first=lines_for(FIXED, lose_at=100), rerun=lines_for(FIXED),
                         void=[(1, lines_for(FIXED, lose_at=2), False)])
        rep = r.report()
        self.assertEqual((rep["id"], rep["used"], rep["ok"], rep["problem"], rep["outcome"], rep["exits"]),
                         (FIXED["id"], "rerun", True, None, "ok after rerun", [6, 0]))
        self.assertEqual(rep["lost_units"], {"first": 1, "rerun": 0, "void": 1})
        self.assertEqual(rep["void_runs"], [{"file": FIXED["id"] + ".interrupted-1.jsonl", "reason": "interrupted",
                                             "lost_units": 1}])
        self.assertTrue(rep["rerun"])


class VoidRuns(Case):
    def test_void_runs_are_listed_per_process_and_never_used(self):
        pid = FIXED["id"]
        r = self.resolve(FIXED, first=lines_for(FIXED), void=[
            (1, lines_for(FIXED, lose_at=5), False), (2, lines_for(FIXED, delays=[9] * 630), False),
            (1, lines_for(FIXED, lose_at=9), True)])
        self.assertTrue(r.ok)
        self.assertEqual(sorted(v.name for v in r.void),
                         [pid + "-rerun.interrupted-1.jsonl", pid + ".interrupted-1.jsonl", pid + ".interrupted-2.jsonl"])
        self.assertEqual(r.lost_units(), {"first": 0, "rerun": 0, "void": 2})
        self.assertEqual(r.run.path, os.path.join(self.tmp, pid + ".jsonl"))
        self.assertEqual(sorted(name for name, n in r.lost_unit_runs()),
                         [pid + "-rerun.interrupted-1.jsonl", pid + ".interrupted-1.jsonl"])

    def test_a_void_run_is_not_a_failed_run(self):
        # A void run is never an attempt: the process below ran once, and exited 0.
        r = self.resolve(FIXED, first=lines_for(FIXED), void=[(1, lines_for(FIXED, lose_at=5), False)])
        self.assertEqual((r.exits, r.used, r.first_problem), ((0,), "first", None))

    def test_only_this_process_s_void_runs_belong_to_it(self):
        other = dict(FIXED, id="S-epoll-none-W1-B1ms-p00")  # an id that starts with this one's
        names = [FIXED["id"] + ".interrupted-1.jsonl", other["id"] + ".interrupted-1.jsonl",
                 FIXED["id"] + ".interrupted-x.jsonl", FIXED["id"] + ".interrupted-1.out", "x" + FIXED["id"] + ".interrupted-1.jsonl",
                 FIXED["id"] + "-rerun.interrupted-3.jsonl"]
        for name in names:
            synth.write_lines(os.path.join(self.tmp, name), lines_for(FIXED, lose_at=3))
        synth.write_process(self.tmp, FIXED, first=lines_for(FIXED))
        r = readers.Resolved(FIXED, self.tmp, "linux")
        self.assertEqual(sorted(v.name for v in r.void),
                         [FIXED["id"] + "-rerun.interrupted-3.jsonl", FIXED["id"] + ".interrupted-1.jsonl"])

    def test_a_void_run_cut_off_while_it_wrote_still_counts_what_it_lost(self):
        lines = lines_for(FIXED, lose_at=5)
        path = os.path.join(self.tmp, FIXED["id"] + ".interrupted-1.jsonl")
        synth.write_lines(path, lines[:-1])  # no summary line
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"design": "synth')  # a line cut in half
        synth.write_process(self.tmp, FIXED, first=lines_for(FIXED))
        r = readers.Resolved(FIXED, self.tmp, "linux")
        self.assertEqual(r.lost_units()["void"], 1)

    def test_an_abpair_window_is_no_lost_unit(self):
        r = self.resolve(H4, first=lines_for(H4), void=[(1, lines_for(H4), False)])
        self.assertTrue(r.ok)
        self.assertEqual(r.lost_units(), {"first": 0, "rerun": 0, "void": 0})


class WakeprobeSettings(Case):
    """Section 1: the Release build of a clean tree, Linux timer slack 1 ns, Windows' default resolution."""

    def problem(self, entry=FIXED, system="linux", **summary):
        out = self.resolve(entry, system, first=lines_for(entry, system, summary=summary))
        return out.problem

    def test_the_planned_run_is_good(self):
        for entry, system in ((FIXED, "linux"), (DELAYING, "linux"), (WRONG_WORKER, "linux"), (BATCH, "linux"),
                              (WIN_FIXED, "windows"), (SWEEP, "windows")):
            self.assertIsNone(self.problem(entry, system), entry["id"])

    def test_the_build(self):
        self.assertIn("build_type", self.problem(build_type="Debug"))
        self.assertIn("build_type", self.problem(build_type="RelWithDebInfo"))
        self.assertIn("build_type", self.problem(build_type=None))
        self.assertIn("bench_dirty", self.problem(bench_dirty=True))
        self.assertIn("bench_dirty", self.problem(bench_dirty=None))
        self.assertIn("bench_dirty", self.problem(bench_dirty=0))  # not the JSON false

    def test_the_label_the_design_and_the_seed(self):
        self.assertIn("label", self.problem(label="S-epoll-none-W1-B1ms-p1"))
        self.assertIn("design", self.problem(design="adhoc"))
        self.assertIn("gap_seed", self.problem(gap_seed=2026102701))
        self.assertIn("gap_seed", self.problem(gap_seed=20260926))  # the 2026-09 seed
        self.assertIn("gap_seed", self.problem(gap_seed=None))
        self.assertIn("gap_frac_range", self.problem(gap_frac_range=[0.1, 2.0]))
        self.assertIn("gap_unit", self.problem(gap_unit="timer"))
        # The sweep has no seed and no range; a summary that has one is not the sweep.
        self.assertIn("gap_seed", self.problem(SWEEP, "windows", gap_seed=2026102700))
        self.assertIn("phase_frac", self.problem(SWEEP, "windows", phase_frac=0.15))

    def test_what_the_plan_asked_for(self):
        for field, wrong in (("backend", "io_uring"), ("variant", "plain_wake"), ("workers", 2), ("wait_us", 10000),
                             ("protocol", "batch"), ("posts", 631), ("warmup", 4), ("calibrate", 11),
                             ("worker_cpus", [12]), ("poster_cpu", 3)):
            self.assertIn(field, self.problem(**{field: wrong}), field)
        self.assertIn("calibrate", self.problem(DELAYING, calibrate=0))

    def test_where_the_threads_ended(self):
        self.assertIn("worker_cpus_seen", self.problem(worker_cpus_seen=[15]))
        self.assertIn("worker_cpus_seen", self.problem(worker_cpus_seen=None))
        self.assertIn("poster_cpu_seen", self.problem(poster_cpu_seen=3))
        self.assertIn("poster_cpu_seen", self.problem(poster_cpu_seen=None))
        two = self.problem(WRONG_WORKER, worker_cpus_seen=[14, 12])  # the right CPUs, in the wrong order
        self.assertIn("worker_cpus_seen", two)

    def test_linux_timer_slack(self):
        self.assertIn("timerslack_set_rc", self.problem(timerslack_set_rc=1))
        self.assertIn("timerslack_set_rc", self.problem(timerslack_set_rc=-1))
        self.assertIn("timerslack_set_rc", self.problem(timerslack_set_rc=None))
        self.assertIn("timerslack_set_rc", self.problem(timerslack_set_rc=False))
        self.assertIn("timerslack_ns_worker", self.problem(timerslack_ns_worker=50000))  # the default slack
        self.assertIn("timerslack_ns_worker", self.problem(timerslack_ns_worker=None))
        self.assertIn("timerslack_ns_worker", self.problem(timerslack_ns_worker=True))

    def test_windows_default_timer_resolution(self):
        win = synth.WINDOWS_TIMER_US * 10  # in 100 ns
        self.assertIsNone(self.problem(WIN_FIXED, "windows"))
        for field, wrong, where in (("timer_resolution_100ns_start", 10000, "start"),
                                    ("timer_resolution_100ns_end", 10000, "end"),
                                    ("timer_resolution_100ns_start", -1, "start"),
                                    ("timer_resolution_100ns_end", -1, "end"),
                                    ("timer_resolution_100ns_start", None, "start"),
                                    ("timer_resolution_100ns_gap", [win, win, 5000, win, win], "gap"),
                                    ("timer_resolution_100ns_gap", [win, -1, win, win, win], "gap"),
                                    ("timer_resolution_100ns_gap", [win, win, win, win, 5000], "gap")):
            problem = self.problem(WIN_FIXED, "windows", **{field: wrong})
            self.assertIn("timer resolution at the %s" % where, problem or "", (field, wrong))
        self.assertIn("timer_resolution_100ns_gap", self.problem(WIN_FIXED, "windows", timer_resolution_100ns_gap=None))
        self.assertIn("timer_period_ms_requested", self.problem(WIN_FIXED, "windows", timer_period_ms_requested=1))
        self.assertIn("timer_period_ms_requested", self.problem(WIN_FIXED, "windows", timer_period_ms_requested=None))

    def test_the_default_resolution_is_the_coarsest_the_system_reports(self):
        # The period every reading must show is coarsest_timer_us; without it nothing can be checked.
        self.assertIn("coarsest_timer_us", self.problem(WIN_FIXED, "windows", coarsest_timer_us=None))
        self.assertIn("coarsest_timer_us", self.problem(WIN_FIXED, "windows", coarsest_timer_us=0))
        # A system whose coarsest period is 10 ms is at its default when every reading says 10 ms.
        ten = 100000
        self.assertIsNone(self.problem(WIN_FIXED, "windows", coarsest_timer_us=10000, timer_resolution_100ns_start=ten,
                                       timer_resolution_100ns_end=ten, timer_resolution_100ns_gap=[ten] * 5))
        # wakeprobe takes the period in us as ceil(coarsest / 10): 156251 (100 ns) is 15626 us.
        ok = self.problem(WIN_FIXED, "windows", coarsest_timer_us=15626, timer_resolution_100ns_start=156251,
                          timer_resolution_100ns_end=156251, timer_resolution_100ns_gap=[156251] * 5)
        self.assertIsNone(ok)

    def test_the_sweep_needs_no_gap_reading(self):
        self.assertIsNone(self.problem(SWEEP, "windows", timer_resolution_100ns_gap=[]))
        self.assertIn("timer resolution at the gap",
                      self.problem(SWEEP, "windows", timer_resolution_100ns_gap=[5000]))

    def test_a_linux_summary_in_a_windows_plan_and_the_other_way_round(self):
        out = self.resolve(WIN_FIXED, "windows", first=lines_for(WIN_FIXED, "linux"))
        self.assertFalse(out.ok)
        out = self.resolve(FIXED, "linux", first=lines_for(FIXED, "windows"))
        self.assertFalse(out.ok)


class WakeprobeUnits(Case):
    """The trial and unit counts are as planned."""

    def mutate(self, entry, system, edit):
        lines = lines_for(entry, system)
        edit(lines)
        return self.resolve(entry, system, first=lines).problem

    def test_missing_units(self):
        def drop(kind, n=1):
            def edit(lines):
                for _ in range(n):
                    for i in range(len(lines) - 1, -1, -1):
                        if lines[i].get("kind") == kind:
                            del lines[i]
                            break
            return edit

        self.assertIn("units by kind", self.mutate(FIXED, "linux", drop("probe")))
        self.assertIn("units by kind", self.mutate(FIXED, "linux", drop("warmup")))
        self.assertIn("units by kind", self.mutate(DELAYING, "linux", drop("calib")))
        problem = self.mutate(FIXED, "linux", drop("measured"))
        self.assertIn("units by kind", problem)
        self.assertIn("units by kind", self.mutate(BATCH, "linux", drop("measured", 21)))

    def test_extra_units(self):
        def extra(kind):
            def edit(lines):
                lines.insert(0, dict(lines[0], kind=kind))
            return edit

        for kind in ("probe", "warmup", "measured", "calib"):
            self.assertIn("units by kind", self.mutate(FIXED, "linux", extra(kind)), kind)

    def test_calibration_posts_belong_to_the_delaying_variants(self):
        self.assertIsNone(self.mutate(DELAYING, "linux", lambda lines: None))
        self.assertEqual(sum(1 for x in lines_for(DELAYING) if x.get("kind") == "calib"), 11)
        self.assertEqual(sum(1 for x in lines_for(FIXED) if x.get("kind") == "calib"), 0)
        # wrong worker delays single posts from W = 2 on only; the plan calibrates it at W = 2 and above.
        self.assertEqual(sum(1 for x in lines_for(WRONG_WORKER) if x.get("kind") == "calib"), 11)

    def test_wakeprobe_calibrates_only_the_variants_that_delay(self):
        # A line that asks for calibration posts of a variant that does not delay gets none (wakeprobe's own rule).
        args = list(FIXED["args"])
        args[args.index("--calibrate") + 1] = "11"
        asked = dict(FIXED, args=args)
        lines = lines_for(asked)
        self.assertEqual(sum(1 for x in lines if x.get("kind") == "calib"), 0)
        self.assertEqual(lines[-1]["calibrate"], 11)
        self.assertIsNone(self.resolve(asked, first=lines).problem)

    def test_an_undelivered_measured_unit(self):
        def undeliver(lines):
            lines[-2]["delay_ns"] = None
        self.assertIn("measured units delivered", self.mutate(FIXED, "linux", undeliver))

    def test_a_lost_unit(self):
        problem = self.resolve(FIXED, first=lines_for(FIXED, lose_at=3), rerun=lines_for(FIXED)).first_problem
        self.assertEqual(problem, "exit 6: lost unit")
        # The summary's own count of lost units, whatever the records say.
        self.assertEqual(self.resolve(FIXED, first=lines_for(FIXED, summary={"lost": 1}), exits=[0]).problem, "lost unit")

    def test_an_error_line(self):
        out = self.resolve(FIXED, first=lines_for(FIXED, error="the workers did not start within 1 s"), exits=[0])
        self.assertEqual(out.problem, "error: the workers did not start within 1 s")

    def test_no_summary(self):
        out = self.resolve(FIXED, first=lines_for(FIXED)[:-1], exits=[0])
        self.assertEqual(out.problem, "no summary line")


class AbpairSettings(Case):
    def problem(self, entry=H4, **summary):
        return self.resolve(entry, first=lines_for(entry, summary=summary)).problem

    def test_the_planned_run_is_good(self):
        self.assertIsNone(self.problem())

    def test_the_build_and_the_mode(self):
        self.assertIn("build_type", self.problem(build_type="Debug"))
        self.assertIn("bench_dirty", self.problem(bench_dirty=True))
        self.assertIn("bench_dirty", self.problem(bench_dirty=None))
        self.assertIn("mode", self.problem(mode="count"))
        self.assertIn("check", self.problem(check=True))

    def test_what_the_plan_asked_for(self):
        for field, wrong in (("backend", "io_uring"), ("arm_a", "plain_wake"), ("arm_b", "none"), ("workers", 2),
                             ("producers", 2), ("inflight", 32), ("pairs", 3), ("order", "shuffle"),
                             ("window_ms", 1000), ("work_ns", 1000), ("worker_cpus", [12]), ("producer_cpus", [4]),
                             ("windows", 3)):
            self.assertIn(field, self.problem(**{field: wrong}), field)

    def test_the_work_level_is_the_cells(self):
        e = [x for x in plan.h4_entries("linux", {c: 14 for c in grid.h4_cells("linux")}) if "N100us" in x["id"]][0]
        self.assertEqual(readers.arg_value(e["args"], "--work-ns"), "100000")
        self.assertIsNone(self.problem(e))
        self.assertIn("work_ns", self.problem(e, work_ns=10000))

    def test_where_the_threads_ended(self):
        # C4: the CPUs the pinned threads were seen on at the end, in the order of the planned CPUs.
        self.assertIn("worker_cpus_seen", self.problem(worker_cpus_seen=[15]))
        self.assertIn("worker_cpus_seen", self.problem(worker_cpus_seen=None))
        self.assertIn("producer_cpus_seen", self.problem(producer_cpus_seen=[3]))
        self.assertIn("producer_cpus_seen", self.problem(producer_cpus_seen=None))
        self.assertIn("producer_cpus_seen", self.problem(producer_cpus_seen=[-1]))

    def test_the_windows(self):
        lines = lines_for(H4)
        out = self.resolve(H4, first=lines[:-2] + lines[-1:], exits=[0])
        self.assertTrue(out.problem.startswith("3 of 4 windows"), out.problem)
        for edit, text in ((lambda w: w.update(tasks=0), "positive tasks"), (lambda w: w.update(elapsed_ns=0), "positive elapsed_ns"),
                           (lambda w: w.update(tasks=None), "positive tasks"), (lambda w: w.update(side="B"), "side or arm"),
                           (lambda w: w.update(arm="plain_wake"), "side or arm"), (lambda w: w.update(ran="plain_wake"), "side or arm"),
                           (lambda w: w.update(position=1), "order")):
            broken = copy.deepcopy(lines)
            edit(broken[0])
            out = self.resolve(H4, first=broken, exits=[0])
            self.assertIn(text, out.problem or "", text)

    def test_the_order_is_abba(self):
        lines = lines_for(H4)
        swapped = copy.deepcopy(lines)
        for w in swapped[:4]:
            w["position"] = 1 - w["position"]  # pair 0 run B then A under positions that say A then B
        self.assertIn("order", self.resolve(H4, first=swapped, exits=[0]).problem)
        twice = copy.deepcopy(lines)
        twice[1].update(pair=0, position=0, side="A", arm=twice[0]["arm"], ran=twice[0]["ran"])
        self.assertIsNotNone(self.resolve(H4, first=twice, exits=[0]).problem)


class PlanFile(unittest.TestCase):
    def write(self, tmp, entries, head=None):
        path = os.path.join(tmp, "plan.jsonl")
        plan.write_plan(path, head or plan.header("linux", "test"), entries)
        return path

    def test_a_line_holds_exactly_the_contract_s_keys(self):
        with tempfile.TemporaryDirectory(prefix="p1v2-planfile-") as tmp:
            good = copy.deepcopy(FIXED)
            readers.load_plan(self.write(tmp, [good]))
            for edit in (lambda e: e.pop("limit_s"), lambda e: e.update(extra=1), lambda e: e.update(limit_s=0),
                         lambda e: e.update(limit_s=-5), lambda e: e.update(limit_s=7.5), lambda e: e.update(limit_s="9"),
                         lambda e: e.update(limit_s=True), lambda e: e.update(tool="other"),
                         lambda e: e.update(args="x"), lambda e: e.pop("id")):
                bad = copy.deepcopy(FIXED)
                edit(bad)
                with self.assertRaises((ValueError, KeyError)):
                    readers.load_plan(self.write(tmp, [bad]))

    def test_duplicate_ids_and_the_header(self):
        with tempfile.TemporaryDirectory(prefix="p1v2-planfile-") as tmp:
            with self.assertRaises(ValueError):
                readers.load_plan(self.write(tmp, [FIXED, FIXED]))
            with self.assertRaises(ValueError):
                readers.load_plan(self.write(tmp, [FIXED], head={"plan": 2, "system": "linux"}))


if __name__ == "__main__":
    unittest.main()

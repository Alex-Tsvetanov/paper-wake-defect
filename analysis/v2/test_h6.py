"""H6's scorer on synthetic run_h6.py folders: the rules of good processes, H6a's tests and Holm, H6b, the
diagnostics, the compiled-state reading and the gate."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction

import h6
import run_h6

COMMIT, DIGEST, JDK = "c" * 40, "d" * 64, "openjdk version \"1.8.0_504\" (Temurin) build 1.8.0_504-b01"
POSTS = run_h6.TRIALS * run_h6.POSTS
ARGS = list(run_h6.SIZES) + ["--loop-cpu", "14", "--poster-cpu", "2", "--rest-cpu", "0"]
COMPILED = ("   1657  427 % !   3       io.netty.channel.epoll.EpollEventLoop::run @ 0 (235 bytes)\n"
            "   1941  432 % !   4       io.netty.channel.epoll.EpollEventLoop::run @ 0 (235 bytes)\n")


def delays_for(entry, misses=(), hits=()):
    """315 delays: CR7 forced about 1 s, everything else about 20 us; trial indexes in misses get 20 us on
    CR7 forced, (trial, index) pairs in hits get 999 ms."""
    slow = entry["arm"] == "cr7" and entry["protocol"] == "forced"
    out = []
    for t in range(run_h6.TRIALS):
        for i in range(run_h6.POSTS):
            d = 1000400000 if slow and t not in misses else 20000 + i
            if (t, i) in hits:
                d = 999000000
            out.append((t, i, d))
    return out


def lines_for(entry, misses=(), hits=(), summary=None):
    lines = []
    for t, i, d in delays_for(entry, misses, hits):
        line = {"kind": "post", "trial": t, "index": i, "delay_ns": d}
        if entry["protocol"] == "forced":
            line.update(x_delay_ns=30000, wakenup_at_hook=1, latch_ok=True, hook_held_ns=40000,
                        y_after_hook_ns=d - 50000)
        else:
            line["realized_gap_ns"] = 300
        lines.append(line)
    lines += [{"kind": "trial", "trial": t, "gc_count": 0, "gc_time_ms": 0} for t in range(run_h6.TRIALS)]
    s = {"summary": True, "good": True, "problems": [], "check": False, "protocol": entry["protocol"],
         "expected_version": run_h6.ARMS[entry["arm"]], "process": entry["process"], "lost_wakes": 0,
         "hook_injections": run_h6.WARMUP_POSTS + POSTS if entry["protocol"] == "forced" else None,
         "hook_armed_with_tasks": 0 if entry["protocol"] == "forced" else None, "max_delay_ns": 1000400000,
         "warmup_natural_posts": 1000, "warmup_natural_over_half_b": 0, "first_measured_uptime_ms": 12000,
         "timerslack_ns": {"main": "50000", "loop": "50000"},
         "args": ["--netty-version", run_h6.ARMS[entry["arm"]], "--protocol", entry["protocol"]] + ARGS}
    s.update(summary or {})
    return lines + [s]


def write_lines(path, lines):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for l in lines:
            f.write(json.dumps(l) + "\n")


class Run:
    """A synthetic run folder and records folder; per-process changes through the arguments of build()."""

    def __init__(self, case):
        self.root = tempfile.mkdtemp(prefix="p1v2-h6-")
        case.addCleanup(shutil.rmtree, self.root, True)
        self.dir = os.path.join(self.root, "run")
        self.records = os.path.join(self.root, "records")
        os.makedirs(self.dir)
        os.makedirs(self.records)

    def build(self, special=None, record=None, gate=True, fields=None, ended=True, missing_done=()):
        special = special or {}
        order = run_h6.plan()
        with open(os.path.join(self.dir, "plan.txt"), "w", encoding="utf-8") as f:
            f.write("# H6 plan\n" + "".join(json.dumps(e, sort_keys=True) + "\n" for e in order))
        rec = {"green": True, "lab_job": "test-job", "host": run_h6.LAB_HOST, "desktop_check": False,
               "commit": COMMIT, "inputs_hash": DIGEST, "jdk": JDK}
        rec.update(record or {})
        name = "h6-functional-ccccccccc-L-20261029t200000.json"
        with open(os.path.join(self.records, name), "w", encoding="utf-8") as f:
            json.dump(rec, f)
        sha = run_h6.sha256_file(os.path.join(self.records, name))
        run_txt = {"h6 gate": "passed", "h6 host": run_h6.LAB_HOST, "h6 commit": COMMIT, "h6 inputs hash": DIGEST,
                   "h6 functional record": "%s %s" % (name, sha), "jdk": JDK, "cpus": "0,2,14"}
        if not gate:
            del run_txt["h6 gate"]
        run_txt.update(fields or {})
        if ended:
            run_txt["ended utc"] = "20261030t000000"
        with open(os.path.join(self.dir, "run.txt"), "w", encoding="utf-8") as f:
            f.write("".join("%s: %s\n" % kv for kv in run_txt.items()))
        for e in order:
            spec = special.get(e["id"], {})
            attempts = spec.get("attempts", [("", 0)])
            for suffix, rc in attempts:
                stem = os.path.join(self.dir, e["id"] + suffix)
                if rc == 0:
                    write_lines(stem + ".jsonl", lines_for(e, spec.get("misses", ()), spec.get("hits", ()),
                                                           spec.get("summary")))
                with open(stem + ".out", "w", encoding="utf-8") as f:
                    f.write(spec.get("out", COMPILED))
            if e["id"] in missing_done:
                continue
            with open(os.path.join(self.dir, e["id"] + ".done"), "w", encoding="ascii") as f:
                f.write("".join("%s %d\n" % (("first", "rerun")[k], rc) for k, (_, rc) in enumerate(attempts)))
        return self

    def score(self, draft=False):
        return h6.score(self.dir, self.records, "test", draft)


class H6a(unittest.TestCase):
    def test_both_forced_cells_reject(self):
        r = Run(self).build().score()
        t = r["H6a"]["tests"]
        self.assertTrue(t["cr7-forced"]["rejected"] and t["final-forced"]["rejected"])
        self.assertEqual(r["cells"]["cr7-forced"]["t_med"]["flags"], 150)
        self.assertEqual(r["cells"]["final-forced"]["t_med"]["flags"], 0)
        self.assertEqual(r["incomplete_cells"], [])
        self.assertTrue(r["H6a"]["holds"])

    def test_two_misses_pass_only_at_holms_second_step(self):
        r = Run(self).build({"cr7-forced-p3": {"misses": (0, 1)}}).score()
        t = r["H6a"]["tests"]["cr7-forced"]
        self.assertEqual(r["cells"]["cr7-forced"]["t_med"]["flags"], 148)
        self.assertEqual((t["rank"], t["threshold"]), (2, Fraction(25, 1000)))
        self.assertTrue(t["rejected"])
        self.assertAlmostEqual(float(t["p"]), 1.82e-2, places=3)

    def test_three_misses_do_not_reject(self):
        r = Run(self).build({"cr7-forced-p3": {"misses": (0, 1, 2)}}).score()
        self.assertFalse(r["H6a"]["tests"]["cr7-forced"]["rejected"])
        self.assertFalse(r["H6a"]["holds"])

    def test_a_process_that_fails_twice_leaves_its_cell_incomplete(self):
        r = Run(self).build({"final-forced-p0": {"attempts": [("", 1), ("-rerun", 1)]}}).score()
        cell = r["cells"]["final-forced"]
        self.assertEqual((cell["complete"], cell["processes_good"]), (False, 9))
        self.assertIsNone(r["H6a"]["tests"]["final-forced"]["p"])
        self.assertFalse(r["H6a"]["tests"]["final-forced"]["rejected"])
        self.assertEqual(r["incomplete_cells"], ["final-forced"])

    def test_a_clean_rerun_replaces_a_failed_first_run(self):
        r = Run(self).build({"cr7-natural-p2": {"attempts": [("", 1), ("-rerun", 0)]}}).score()
        view = [p for p in r["cells"]["cr7-natural"]["processes"] if p["id"] == "cr7-natural-p2"][0]
        self.assertEqual((view["good"], view["used"], view["exits"]), (True, "rerun", {"first": 1, "rerun": 0}))
        self.assertTrue(r["cells"]["cr7-natural"]["complete"])


class GoodProcesses(unittest.TestCase):
    def view(self, pid, spec):
        r = Run(self).build({pid: spec}).score()
        arm, protocol, _ = pid.split("-")
        return [p for p in r["cells"]["%s-%s" % (arm, protocol)]["processes"] if p["id"] == pid][0]

    def test_a_check_mode_output_is_not_good(self):
        self.assertIn("summary check", self.view("final-natural-p1", {"summary": {"check": True}})["problem"])

    def test_another_netty_version_is_not_good(self):
        p = self.view("cr7-forced-p1", {"summary": {"expected_version": "4.1.0.Final"}})
        self.assertIn("summary expected_version", p["problem"])

    def test_a_probe_that_says_not_good_is_not_good(self):
        p = self.view("cr7-forced-p1", {"summary": {"good": False, "problems": ["1 Netty log records"]}})
        self.assertIn("not good", p["problem"])

    def test_a_wrong_hook_count_is_not_good(self):
        self.assertIn("hook injected", self.view("final-forced-p4", {"summary": {"hook_injections": 300}})["problem"])

    def test_other_sizes_or_cpus_are_not_good(self):
        args = [a if a != "1100" else "50" for a in ARGS]
        p = self.view("cr7-forced-p5", {"summary": {"args": args}})
        self.assertIn("--slot-ms", p["problem"])
        args = [a if a != "14" else "4" for a in ARGS]
        self.assertIn("--loop-cpu", self.view("cr7-natural-p5", {"summary": {"args": args}})["problem"])


class H6b(unittest.TestCase):
    def test_natural_hits_and_the_predictions(self):
        hits = {(2, 5), (7, 0)}
        r = Run(self).build({"cr7-natural-p4": {"hits": hits}}).score()
        b = r["H6b"]
        self.assertEqual(b["cr7"]["posts_over_half_b"], 2)
        self.assertEqual(b["predictions"], {"cr7_some_post_over_half_b": True, "final_no_post_over_half_b": True,
                                            "cr7_t_count_flags_some": True, "final_t_count_flags_none": True})
        self.assertFalse(b["cr7"]["t_med_blind_prediction_fails"])
        over = r["cells"]["cr7-natural"]["over_half_b"]
        self.assertEqual(sorted((o["trial"], o["index"]) for o in over), sorted(hits))
        self.assertEqual(r["cells"]["cr7-natural"]["t_count"]["flags"], 2)

    def test_zero_hits_give_the_one_sided_bound(self):
        r = Run(self).build().score()
        self.assertEqual(r["cells"]["final-natural"]["posts"], 3150)
        self.assertAlmostEqual(float(r["cells"]["final-natural"]["posts_over_half_b_upper_95"]), 9.5e-4, places=5)

    def test_forced_diagnostics(self):
        d = Run(self).build().score()["cells"]["cr7-forced"]["diagnostics"]
        self.assertEqual(d["wakenup_at_hook"], {"1": 3150})
        self.assertEqual(d["latch_ok"], {"True": 3150})
        self.assertEqual(d["x_delay_ns"]["max"], 30000)

    def test_realized_gaps_are_reported(self):
        cell = Run(self).build().score()["cells"]["final-natural"]
        self.assertEqual((cell["realized_gap_ns"]["n"], cell["realized_gap_ns"]["max"]), (3150, 300))
        self.assertNotIn("realized_gap_ns", Run(self).build().score()["cells"]["final-forced"])


class Compiled(unittest.TestCase):
    EVENTS = ("   1657  427 % !   3       io.netty.channel.epoll.EpollEventLoop::run @ 0 (235 bytes)\n"
              "   1700  428       3       io.netty.util.concurrent.SingleThreadEventExecutor::runAllTasks (60 bytes)\n"
              "   2000  427 % !   3       io.netty.channel.epoll.EpollEventLoop::run @ 0 (235 bytes)   made not entrant\n"
              "   2100  440 % !   4       io.netty.channel.epoll.EpollEventLoop::run @ 0 (235 bytes)\n")

    def events(self):
        root = tempfile.mkdtemp(prefix="p1v2-h6c-")
        self.addCleanup(shutil.rmtree, root, True)
        path = os.path.join(root, "x.out")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.EVENTS)
        return h6.compile_events(path)

    def test_the_parser(self):
        self.assertEqual(self.events(), [(1657, 427, 3, True, False), (2000, 427, 3, True, True),
                                         (2100, 440, 4, True, False)])

    def test_compiled_code_in_use(self):
        e = self.events()
        self.assertEqual([h6.compiled_at(e, ms) for ms in (1000, 1800, 2050, 2200)], [False, True, False, True])
        self.assertIsNone(h6.compiled_at(e, None))

    def test_a_skipped_compile_is_no_code(self):
        root = tempfile.mkdtemp(prefix="p1v2-h6c-")
        self.addCleanup(shutil.rmtree, root, True)
        path = os.path.join(root, "x.out")
        with open(path, "w", encoding="utf-8") as f:
            f.write("   1657  427 % !   3       io.netty.channel.epoll.EpollEventLoop::run @ 0 (235 bytes)\n"
                    "   1700  427 % !   3       io.netty.channel.epoll.EpollEventLoop::run @ 0 (235 bytes)"
                    "   COMPILE SKIPPED: out of nodes (retry at different tier)\n")
        self.assertFalse(h6.compiled_at(h6.compile_events(path), 1800))

    def test_only_osr_code_counts_and_ids_retire_it(self):
        # The desktop's check-final-forced.out: OSR 432 made not entrant and not redone; non-OSR 429 stays.
        e = [(1657, 427, 3, True, False), (1667, 429, 3, False, False), (1941, 432, 4, True, False),
             (1950, 427, 3, True, True), (2267, 432, 4, True, True)]
        self.assertEqual([h6.compiled_at(e, ms) for ms in (1950, 2300, 4000)], [True, False, False])

    def test_a_process_without_compiled_run_is_reported_and_kept(self):
        r = Run(self).build({"cr7-natural-p0": {"out": ""}}).score()
        view = [p for p in r["cells"]["cr7-natural"]["processes"] if p["id"] == "cr7-natural-p0"][0]
        self.assertEqual((view["good"], view["run_compiled_at_first_post"]), (True, False))


class Gate(unittest.TestCase):
    def test_no_gate_line_is_refused(self):
        with self.assertRaises(h6.Refusal):
            Run(self).build(gate=False).score()

    def test_a_red_record_is_refused(self):
        with self.assertRaises(h6.Refusal) as e:
            Run(self).build(record={"green": False}).score()
        self.assertIn("may not gate this run", str(e.exception))

    def test_a_record_of_another_commit_is_refused(self):
        with self.assertRaises(h6.Refusal) as e:
            Run(self).build(record={"commit": "e" * 40}).score()
        self.assertIn("may not gate this run", str(e.exception))

    def test_a_record_without_a_lab_job_is_refused(self):
        with self.assertRaises(h6.Refusal):
            Run(self).build(record={"lab_job": None}).score()

    def test_a_desktop_or_off_laptop_record_is_refused(self):
        for change in ({"desktop_check": True}, {"host": "94c04692be81"}, {"jdk": "another JDK"}):
            with self.assertRaises(h6.Refusal):
                Run(self).build(record=change).score()

    def test_a_run_off_the_laptop_or_on_other_cpus_is_refused(self):
        with self.assertRaises(h6.Refusal) as e:
            Run(self).build(fields={"h6 host": "desktop"}).score()
        self.assertIn("host", str(e.exception))
        with self.assertRaises(h6.Refusal) as e:
            Run(self).build(fields={"cpus": "0,2,4"}).score()
        self.assertIn("CPUs", str(e.exception))

    def test_an_unfinished_run_is_refused(self):
        with self.assertRaises(h6.Refusal) as e:
            Run(self).build(ended=False).score()
        self.assertIn("did not end", str(e.exception))
        with self.assertRaises(h6.Refusal) as e:
            Run(self).build(missing_done=("cr7-forced-p9",)).score()
        self.assertIn("did not end", str(e.exception))

    def test_a_rerun_still_owed_is_refused(self):
        with self.assertRaises(h6.Refusal) as e:
            Run(self).build({"final-natural-p6": {"attempts": [("", 1)]}}).score()
        self.assertIn("1 planned processes did not end", str(e.exception))

    def test_resumes_and_void_files_are_reported(self):
        run = Run(self).build()
        with open(os.path.join(run.dir, "run.txt"), "a", encoding="utf-8") as f:
            f.write("resumed utc: 20261030t010000\n")
        open(os.path.join(run.dir, "cr7-forced-p2.jsonl.void-20261030t010000"), "w").close()
        r = run.score()
        self.assertEqual(r["resumes"], {"resumed_utc": ["20261030t010000"],
                                        "void_files": ["cr7-forced-p2.jsonl.void-20261030t010000"]})

    def test_draft_turns_refusals_into_warnings(self):
        r = Run(self).build(gate=False).score(draft=True)
        self.assertEqual(r["warnings"], ["run.txt has no 'h6 gate: passed' line"])

    def test_another_plan_is_refused(self):
        run = Run(self).build()
        path = os.path.join(run.dir, "plan.txt")
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        lines[1], lines[2] = lines[2], lines[1]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        with self.assertRaises(h6.Refusal):
            run.score()

    def test_main_writes_the_record_in_draft(self):
        run = Run(self).build()
        out = os.path.join(run.root, "h6.json")
        with redirect_stdout(io.StringIO()) as printed, redirect_stderr(io.StringIO()):
            self.assertEqual(h6.main(["--run", run.dir, "--records", run.records, "--draft", "--out", out]), 0)
        self.assertIn("H6a: 2 of 2 F6 cells rejected", printed.getvalue())
        with open(out, encoding="utf-8") as f:
            self.assertTrue(json.load(f)["generated_by"].startswith("analysis/v2/h6.py "))


if __name__ == "__main__":
    unittest.main()

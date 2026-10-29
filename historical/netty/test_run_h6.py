"""run_h6.py without Java: the plan, the gate's pieces, the timed run's reruns and resume, and the functional
record, with the commands replaced by fakes."""

import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import run_h6

sys.path.insert(0, os.path.join(run_h6.REPO, "analysis", "v2"))
sys.path.insert(1, os.path.join(run_h6.REPO, "bench"))
import gate_lib  # noqa: E402
import rng  # noqa: E402

JDK = "openjdk version \"1.8.0_504\" / OpenJDK Runtime Environment (Temurin)(build 1.8.0_504-b01)"
GOOD = {"green": True, "lab_job": "j", "host": "alex-laptop", "desktop_check": False, "commit": "c" * 40,
        "inputs_hash": "d" * 64, "jdk": JDK}


class Plan(unittest.TestCase):
    def test_forty_processes_ten_per_cell_in_the_seeded_order(self):
        order = run_h6.plan()
        self.assertEqual(len(order), 40)
        self.assertEqual(len({e["id"] for e in order}), 40)
        for arm in run_h6.ARMS:
            for protocol in run_h6.PROTOCOLS:
                self.assertEqual(sorted(e["process"] for e in order if (e["arm"], e["protocol"]) == (arm, protocol)),
                                 list(range(10)))
        unshuffled = [{"id": "%s-%s-p%d" % (a, p, i), "arm": a, "protocol": p, "process": i}
                      for a in run_h6.ARMS for p in run_h6.PROTOCOLS for i in range(10)]
        self.assertEqual(order, rng.shuffled(unshuffled, 20261026))
        self.assertNotEqual(order, unshuffled)
        self.assertEqual(order, run_h6.plan())


class Coverage(unittest.TestCase):
    def test_the_committed_gap_file_matches_the_inputs(self):
        cov = run_h6.load_coverage()
        self.assertEqual(run_h6.input_problems(cov), [])
        self.assertEqual(set(cov["third_party"]), {"netty-all-4.1.0.CR7.jar", "netty-all-4.1.0.Final.jar",
                                                    "OpenJDK8U-jdk_x64_linux_hotspot_8u504b01.tar.gz"})

    def test_a_changed_input_is_named(self):
        cov = run_h6.load_coverage()
        cov["inputs"]["historical/netty/H6Probe.java"] = "0" * 64
        self.assertEqual(len(run_h6.input_problems(cov)), 1)

    def test_the_inputs_hash_depends_on_paths_and_hashes(self):
        a = run_h6.inputs_hash({"x": "1" * 64, "y": "2" * 64})
        self.assertEqual(a, run_h6.inputs_hash({"y": "2" * 64, "x": "1" * 64}))
        self.assertNotEqual(a, run_h6.inputs_hash({"x": "1" * 64, "y": "3" * 64}))

    def test_the_lab_host_is_the_gates(self):
        self.assertEqual((run_h6.LAB_HOST,), gate_lib.LAB_HOSTS["L"])


class Records(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="p1v2-h6r-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, name, rec):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            json.dump(rec, f)

    def test_only_a_record_that_may_gate_matches(self):
        for name, change in (("a", {"green": False}), ("b", {"lab_job": None}), ("c", {"commit": "e" * 40}),
                             ("d", {"inputs_hash": "f" * 64}), ("e", {"host": "94c04692be81"}),
                             ("f", {"desktop_check": True}), ("g", {"jdk": "another"})):
            self.write("h6-functional-%s.json" % name, dict(GOOD, **change))
        self.assertIsNone(run_h6.matching_record(self.dir, "c" * 40, "d" * 64, JDK))
        self.write("h6-functional-z.json", dict(GOOD, host="ALEX-LAPTOP"))
        self.write("other.json", GOOD)
        name, sha = run_h6.matching_record(self.dir, "c" * 40, "d" * 64, JDK)
        self.assertEqual(name, "h6-functional-z.json")
        self.assertEqual(sha, run_h6.sha256_file(os.path.join(self.dir, name)))

    def test_no_folder_matches_nothing(self):
        self.assertIsNone(run_h6.matching_record(None, "c", "d", JDK))


class Pieces(unittest.TestCase):
    def test_null_times(self):
        self.assertTrue(run_h6.null_times([{"kind": "post", "delay_ns": None},
                                           {"summary": True, "max_delay_ns": None, "warmup_natural_posts": None,
                                            "timerslack_ns": {"main": "50000"}}]))
        self.assertFalse(run_h6.null_times([{"kind": "post", "delay_ns": 5}]))
        self.assertFalse(run_h6.null_times([{"kind": "trial", "gc_time_ms": 0}]))
        self.assertFalse(run_h6.null_times([{"summary": True, "warmup_natural_posts": 149908}]))

    def test_cpus(self):
        self.assertEqual(run_h6.parse_cpus("0,2,14"), (0, 2, 14))
        for bad in ("0,2", "0,2,2"):
            with self.assertRaises(argparse.ArgumentTypeError):
                run_h6.parse_cpus(bad)

    def test_the_probe_command(self):
        cmd = run_h6.probe_command("/jdk", "/netty", {"cr7": "/c7"}, "cr7", (0, 2, 14), "/nat", ["--x"])
        self.assertEqual(cmd[:4], ["taskset", "-c", "0,2,14", os.path.join("/jdk", "bin", "java")])
        for flag in run_h6.JVM_FLAGS + ("-Dio.netty.native.workdir=/nat", "--loop-cpu", "--x"):
            self.assertIn(flag, cmd)
        self.assertIn("4.1.0.CR7", cmd)

    def test_the_timeout_has_room(self):
        self.assertGreater(run_h6.timeout_s(), 2 * (run_h6.WARMUP_S + 320 * 1.1))


class Inputs(unittest.TestCase):
    """checked_inputs: the host, then the inputs, the jars and the JDK."""

    def checked(self, host, jdk, anywhere=False):
        with mock.patch.object(run_h6.platform, "node", return_value=host), \
                mock.patch.object(run_h6, "load_coverage", return_value={"inputs": {}, "third_party": {}}), \
                mock.patch.object(run_h6, "clean_commit", return_value="c" * 40), \
                mock.patch.object(run_h6, "input_problems", return_value=[]), \
                mock.patch.object(run_h6, "jar_problems", return_value=[]), \
                mock.patch.object(run_h6, "jdk_version", return_value=jdk):
            return run_h6.checked_inputs("/netty", "/jdk", anywhere)

    def test_off_the_laptop_is_refused(self):
        with self.assertRaises(run_h6.Refusal) as e:
            self.checked("94c04692be81", JDK)
        self.assertIn("only on the laptop", str(e.exception))
        self.assertEqual(self.checked("94c04692be81", JDK, anywhere=True)[1], "c" * 40)

    def test_another_jdk_is_refused(self):
        with self.assertRaises(run_h6.Refusal) as e:
            self.checked("alex-laptop", "openjdk version \"1.8.0_492\"")
        self.assertIn("1.8.0_504-b01", str(e.exception))
        self.assertEqual(self.checked("alex-laptop", JDK)[3], JDK)


class Fakes(unittest.TestCase):
    """The timed run and the functional mode with the gate, the build and the probe replaced."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="p1v2-h6t-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name, value in (("build", ({"cr7": "c", "final": "f"}, {"cr7": {"H6Probe.class": "1"}})),
                            ("jdk_version", JDK)):
            p = mock.patch.object(run_h6, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        self.args = argparse.Namespace(jdk="/jdk", netty_dir="/netty", out_dir=self.dir, records=None,
                                       cpus=(0, 2, 14), dry_run=False, resume=False, functional=False,
                                       desktop_check=False)
        self.gate = [("h6 gate", "passed"), ("h6 host", "alex-laptop"), ("h6 commit", "c" * 40), ("jdk", JDK)]

    def fake_run(self, exits):
        """A probe stand-in: writes a summary line and returns the next exit of exits[id] (default 0)."""
        calls = []

        def run(cmd, stem, timeout):
            name = os.path.basename(stem)
            base = name[:-len("-rerun")] if name.endswith("-rerun") else name
            rc = exits.get(base, [0, 0])[1 if name.endswith("-rerun") else 0]
            calls.append(name)
            with open(stem + ".jsonl", "w", encoding="utf-8") as f:
                f.write(json.dumps({"summary": True, "good": rc == 0, "max_delay_ns": None}) + "\n")
            return rc
        return run, calls

    def timed(self, run, gate=None):
        with mock.patch.dict(os.environ, {"LAB_JOB": "j"}), \
                mock.patch.object(run_h6, "gate", return_value=gate or self.gate), \
                redirect_stdout(io.StringIO()):
            return run_h6.timed(self.args, run=run)

    def test_a_timed_run_needs_lab_job(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LAB_JOB", None)
            with self.assertRaises(run_h6.Refusal):
                run_h6.timed(self.args)

    def test_a_timed_run_refuses_other_cpus(self):
        self.args.cpus = (0, 2, 4)
        with mock.patch.dict(os.environ, {"LAB_JOB": "j"}):
            with self.assertRaises(run_h6.Refusal) as e:
                run_h6.timed(self.args)
        self.assertIn("CPUs", str(e.exception))

    def test_reruns_and_done_files(self):
        run, calls = self.fake_run({"cr7-forced-p1": [1, 0], "final-natural-p3": [1, 1]})
        self.assertEqual(self.timed(run), 1)
        self.assertEqual(len(calls), 42)
        with open(os.path.join(self.dir, "cr7-forced-p1.done")) as f:
            self.assertEqual(f.read(), "first 1\nrerun 0\n")
        with open(os.path.join(self.dir, "final-natural-p3.done")) as f:
            self.assertEqual(f.read(), "first 1\nrerun 1\n")
        fields = run_h6.read_fields(os.path.join(self.dir, "run.txt"))
        self.assertEqual((fields["h6 gate"], fields["h6 host"], fields["incomplete processes"]), ("passed", "alex-laptop", "1"))
        self.assertIn("ended utc", fields)
        with open(os.path.join(self.dir, "plan.txt")) as f:
            self.assertEqual([json.loads(l) for l in f.read().splitlines()[1:]], run_h6.plan())

    def test_a_folder_with_outputs_is_refused_without_resume(self):
        open(os.path.join(self.dir, "x.jsonl"), "w").close()
        with self.assertRaises(run_h6.Refusal):
            self.timed(self.fake_run({})[0])

    def interrupted(self):
        """A run whose first 5 processes ended and whose 6th was killed with its output half written."""
        run, _ = self.fake_run({})
        self.timed(run)
        order = run_h6.plan()
        for e in order[5:]:
            os.remove(os.path.join(self.dir, e["id"] + ".done"))
            for suffix in (".jsonl",):
                p = os.path.join(self.dir, e["id"] + suffix)
                if os.path.exists(p) and e is not order[5]:
                    os.remove(p)
        return order

    def test_resume_keeps_ended_processes_and_sets_aside_the_killed_one(self):
        order = self.interrupted()
        self.args.resume = True
        run, calls = self.fake_run({})
        self.assertEqual(self.timed(run), 0)
        self.assertEqual(calls, [e["id"] for e in order[5:]])
        self.assertTrue([n for n in os.listdir(self.dir) if n.startswith(order[5]["id"] + ".jsonl.void-")])
        fields = run_h6.read_fields(os.path.join(self.dir, "run.txt"))
        self.assertIn("resumed utc", fields)

    def test_resume_refuses_another_gate(self):
        self.interrupted()
        self.args.resume = True
        other = [kv if kv[0] != "h6 commit" else ("h6 commit", "e" * 40) for kv in self.gate]
        with self.assertRaises(run_h6.Refusal) as e:
            self.timed(self.fake_run({})[0], gate=other)
        self.assertIn("cannot resume", str(e.exception))

    def functional(self, exits, desktop=False):
        run, calls = self.fake_run(exits)
        self.args.desktop_check = desktop
        with mock.patch.dict(os.environ, {"LAB_JOB": "j"}), \
                mock.patch.object(run_h6, "checked_inputs", return_value=({}, "c" * 40, "d" * 64, JDK)), \
                redirect_stdout(io.StringIO()):
            rc = run_h6.functional(self.args, run=run)
        name = [n for n in os.listdir(self.dir) if n.startswith("h6-functional-")][0]
        with open(os.path.join(self.dir, name)) as f:
            return rc, json.load(f), calls

    def test_the_functional_record_is_green_when_every_step_passed(self):
        rc, rec, calls = self.functional({})
        self.assertEqual((rc, rec["green"], rec["lab_job"], rec["commit"], rec["jdk"], rec["desktop_check"]),
                         (0, True, "j", "c" * 40, JDK, False))
        self.assertEqual(calls, [s[0] for s in run_h6.FUNCTIONAL_STEPS])
        self.assertEqual([s[1] for s in run_h6.FUNCTIONAL_STEPS if "check" in s[0]], ["final", "final"])

    def test_a_desktop_check_record_never_gates(self):
        rc, rec, _ = self.functional({}, desktop=True)
        self.assertEqual((rc, rec["desktop_check"], rec["lab_job"]), (0, True, None))
        self.assertFalse(run_h6.record_gates(rec, rec["commit"], rec["inputs_hash"], rec["jdk"]))

    def test_a_failed_step_makes_it_red(self):
        rc, rec, _ = self.functional({"cr7-epoll-only": [1, 1]})
        self.assertEqual((rc, rec["green"]), (1, False))

    def test_main_refuses_with_exit_2(self):
        with mock.patch.object(run_h6, "checked_inputs", side_effect=run_h6.Refusal("x")), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(run_h6.main(["--functional", "--jdk", "/j", "--netty-dir", "/n", "--out-dir", self.dir]), 2)


if __name__ == "__main__":
    unittest.main()

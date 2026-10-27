#!/usr/bin/env python3
"""Tests of bench/run_campaign.py. Python 3, standard library only (unittest).

Nothing here is timed. The tools are stubs: a small Python script that records what it was given
and exits with the code the plan's args ask for ("--stub-exit FIRST,RERUN"). "--stub-pause S" makes
a stub take S seconds. "--stub-marker FILE" makes the first stub that creates FILE sleep for
"--stub-sleep" seconds, so that a test can kill it; a later stub with the same FILE does not sleep.
"--stub-only-attempt N" limits that to attempt N. "--stub-spawn FILE" makes a stub start a child that
appends its pid to FILE and outlives it. "--stub-runner-pid N" makes a stub record the runner's CPU
affinity. The runner starts a stub through run_campaign.launch_prefix, which the tests replace,
because a script cannot be named wakeprobe.exe on Windows. The tests that start the runner as a
program use the real launch_prefix, with shell-script stubs on POSIX and with refusals and dry runs
everywhere, or a small driver script that patches launch_prefix (the kill tests, on both systems).
A kill is also simulated by editing the output directory of a finished stub run; on POSIX one test
sends SIGTERM. LAB_JOB is set only for runs on a temporary build directory with stubs.

Run: python3 bench/test_run_campaign.py
"""

import contextlib
import ctypes
import hashlib
import io
import itertools
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_campaign as rc  # noqa: E402

RUNNER = os.path.join(HERE, "run_campaign.py")
SAMPLE_PLAN = os.path.join(HERE, "testdata", "plan-sample.jsonl")
OTHER_SYSTEM = {"linux": "windows", "windows": "linux"}
LOG_LINE = re.compile(r"^(\S+) (\S+) attempt=(\d) exit=(-?\d+|timeout) seconds=(\d+\.\d{3})$")
# The real forms of generated_by (contract C2), written out by hand: plan.py is not used here.
SHA1 = "0123456789abcdef" * 2 + "01234567"
PILOT_BY = "analysis/v2/plan.py " + SHA1
CAMPAIGN_BY = PILOT_BY + " revision-log-v2.md sha256:" + "ab" * 32
STARTUP_CPUS = rc.current_affinity()  # before any test pins this process

STUB = r'''
import json, os, subprocess, sys, time
tool, argv = sys.argv[1], sys.argv[2:]

def value(flag, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default

def affinity(pid):
    if hasattr(os, "sched_getaffinity"):
        return sorted(os.sched_getaffinity(pid))
    import ctypes
    k32 = ctypes.WinDLL("kernel32")
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.GetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                                           ctypes.POINTER(ctypes.c_size_t)]
    handle = k32.OpenProcess(0x1000, 0, pid or os.getpid())
    mask, system = ctypes.c_size_t(), ctypes.c_size_t()
    assert k32.GetProcessAffinityMask(handle, ctypes.byref(mask), ctypes.byref(system))
    return [i for i in range(64) if mask.value >> i & 1]

def read_or_none(path):
    try:
        with open(path) as handle:
            return handle.read()
    except OSError:
        return None

out = value("--out")
rerun = out.endswith("-rerun.jsonl")
first, second = (int(x) for x in value("--stub-exit", "0,0").split(","))
attempt = 2 if rerun else 1
stem = os.path.basename(out)[:-len(".jsonl")]
if rerun:
    stem = stem[:-len("-rerun")]
folder = os.path.dirname(out)
record = {"tool": os.path.basename(tool), "argv": argv, "attempt": attempt, "pid": os.getpid(),
          "run_txt": os.path.exists(os.path.join(folder, "run.txt")),
          "lock": os.path.exists(os.path.join(folder, "campaign.lock")),
          "attempt1_file": read_or_none(os.path.join(folder, stem + ".attempt1")),
          "group_leader": os.getpgrp() == os.getpid() if hasattr(os, "getpgrp") else None,
          "affinity": affinity(0)}
if value("--stub-runner-pid"):
    record["runner_affinity"] = affinity(int(value("--stub-runner-pid")))
with open(out, "w") as handle:
    handle.write(json.dumps(record) + "\n")
print("stdout of attempt", attempt)
print("stderr of attempt", attempt, file=sys.stderr)
if value("--stub-spawn"):
    child = subprocess.Popen([sys.executable, "-c", "import os, sys, time\n"
                              "open(sys.argv[1], 'a').write(str(os.getpid()) + '\\n')\ntime.sleep(120)",
                              value("--stub-spawn")])
    while True:
        text = read_or_none(value("--stub-spawn")) or ""
        if str(child.pid) in text.split():
            break
        time.sleep(0.02)
if value("--stub-marker") and int(value("--stub-only-attempt", attempt)) == attempt:
    first_time = not os.path.exists(value("--stub-marker"))
    open(value("--stub-marker"), "w").close()
    if first_time:
        time.sleep(float(value("--stub-sleep", "60")))
if value("--stub-pause"):
    time.sleep(float(value("--stub-pause")))
sys.exit(second if rerun else first)
'''

# The kill tests run the runner as a program with launch_prefix patched to start the stub.
DRIVER = r'''
import sys
sys.dont_write_bytecode = True
here, stub = sys.argv[1], sys.argv[2]
sys.path.insert(0, here)
import run_campaign as rc
rc.launch_prefix = lambda path: [sys.executable, stub, path]
sys.exit(rc.main(sys.argv[3:]))
'''


def make_process(pid, tool="wakeprobe", codes="0,0", extra=(), limit_s=600, family="S"):
    args = ["--backend", "epoll", "--worker-cpus", "12,14", "--poster-cpu", "2", "--stub-exit", codes, *extra]
    return {"id": pid, "tool": tool, "args": args, "cell": "cell-" + pid, "family": family, "limit_s": limit_s}


def make_header(system=None, by=CAMPAIGN_BY):
    return {"plan": 1, "system": system or rc.host_system(), "seed": 20261026, "generated_by": by}


def process_is_running(pid):
    """True while the process exists. A zombie (killed, not yet reaped by its new parent) is gone."""
    if sys.platform == "win32":
        k32 = ctypes.WinDLL("kernel32")
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = k32.OpenProcess(0x00100000, 0, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: not signalled yet
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def jsonl(*objects):
    return "".join(json.dumps(o) + "\n" for o in objects).encode("ascii")


class CampaignCase(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory(prefix="campaign test ")  # a space in every path
        self.addCleanup(holder.cleanup)
        self.root = os.path.realpath(holder.name)
        self.stub = os.path.join(self.root, "stub.py")
        with open(self.stub, "w", encoding="ascii") as handle:
            handle.write(STUB)
        self.build = os.path.join(self.root, "build dir")
        self.out = os.path.join(self.root, "out dir")
        self.plan_path = os.path.join(self.root, "plan.jsonl")
        self.make_build(self.build)
        # The runner pins this process while a test runs it in-process; put the CPUs back whatever
        # happens. Where CPU 0 is not ours (a cpuset), the runner takes the lowest CPU we have.
        if STARTUP_CPUS is not None:
            self.addCleanup(rc.set_affinity, STARTUP_CPUS)
            if rc.RUNNER_CPU not in STARTUP_CPUS:
                patcher = mock.patch.object(rc, "RUNNER_CPU", min(STARTUP_CPUS))
                patcher.start()
                self.addCleanup(patcher.stop)

    # -- fixtures ---------------------------------------------------------------------------

    def make_build(self, build, build_type="Release", extra_cache="", binaries=True):
        os.makedirs(os.path.join(build, "bench"))
        if build_type is not None:
            with open(os.path.join(build, "CMakeCache.txt"), "w", encoding="ascii") as handle:
                handle.write(f"CMAKE_BUILD_TYPE:STRING={build_type}\n{extra_cache}")
        if binaries:
            for tool in rc.TOOLS:
                with open(rc.tool_path(build, tool), "wb") as handle:
                    handle.write(b"not a program: " + tool.encode("ascii"))

    def write_plan(self, *processes, system=None, raw=None, by=None):
        if by is None:
            by = PILOT_BY if all(p["family"] == "pilot" for p in processes) else CAMPAIGN_BY
        data = raw if raw is not None else jsonl(make_header(system, by), *processes)
        with open(self.plan_path, "wb") as handle:
            handle.write(data)
        return data

    def stub_prefix(self, path):
        return [sys.executable, self.stub, path]

    def invoke(self, *extra, lab_job="1", dry=False, stubs=True, build=None, out=None):
        argv = ["--plan", self.plan_path, "--build-dir", build or self.build, "--out-dir", out or self.out, *extra]
        if dry:
            argv.append("--dry-run")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ))
            os.environ.pop("LAB_JOB", None)
            if lab_job is not None:
                os.environ["LAB_JOB"] = lab_job
            if stubs:
                stack.enter_context(mock.patch.object(rc, "launch_prefix", self.stub_prefix))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            code = rc.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def path(self, name):
        return os.path.join(self.out, name)

    def read(self, name):
        with open(self.path(name), encoding="utf-8") as handle:
            return handle.read()

    def write(self, name, text):
        with open(self.path(name), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def record(self, name):
        return json.loads(self.read(name))

    def log(self):
        rows = []
        for line in self.read(rc.LOG_FILE).splitlines():
            match = LOG_LINE.match(line)
            rows.append(match.groups() if match else tuple(line.split(" ")))
        return rows

    def log_events(self, since=""):
        """The log lines added after the text `since`, without their times."""
        text = self.read(rc.LOG_FILE)
        self.assertTrue(text.startswith(since), "campaign.log must only grow")
        return [line.split(" ", 1)[1] for line in text[len(since):].splitlines()]

    def run_txt(self):
        return dict(line.split(": ", 1) for line in self.read(rc.RUN_FILE).splitlines())

    def snapshot(self):
        """Every file under the output directory: its path relative to it, mapped to its bytes."""
        found = {}
        for root, _dirs, files in os.walk(self.out):
            for name in files:
                with open(os.path.join(root, name), "rb") as handle:
                    found[os.path.relpath(os.path.join(root, name), self.out)] = handle.read()
        return found

    def assert_refused(self, code, stderr, needle):
        self.assertEqual(code, 2, stderr)
        self.assertIn(needle, stderr)
        self.assertFalse(os.path.exists(self.out), "a refusal must not create the output directory")

    def wait_until(self, condition, what, seconds=30):
        deadline = time.monotonic() + seconds
        while not condition():
            self.assertLess(time.monotonic(), deadline, what)
            time.sleep(0.05)

    def sleeper(self):
        """A process that does nothing for a while, for the lock and pid files to name."""
        proc = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(300)"])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        self.wait_until(lambda: rc.process_identity(proc.pid) is not None, "the sleeper did not start")
        return proc.pid

    def dead_pid(self):
        proc = subprocess.Popen([sys.executable, "-I", "-B", "-c", "pass"])
        proc.wait()
        return proc.pid

    def write_holder_file(self, name, pid, image=None, created=None):
        """A lock or pid file naming `pid`: its real identity unless `image` or `created` are given."""
        now_image, now_created = rc.process_identity(pid) or ("", "")
        self.write(name, f"{pid}\n{now_image if image is None else image}\n"
                         f"{now_created if created is None else created}\n")


class DryRunTests(CampaignCase):
    def test_prints_every_command_and_runs_nothing(self):
        self.write_plan(make_process("a"), make_process("b", tool="abpair"), make_process("c", limit_s=90))
        code, stdout, stderr = self.invoke(dry=True, lab_job=None)
        self.assertEqual(code, 0, stderr)
        commands = [line for line in stdout.splitlines() if re.match(r"\d+: ", line)]
        self.assertEqual(len(commands), 3)
        for line, pid in zip(commands, "abc"):
            self.assertIn(pid + ".jsonl", line)
            self.assertIn("--out", line)
            self.assertIn("2> ", line)
        self.assertTrue(commands[0].endswith("(limit 600 s)"), commands[0])
        self.assertTrue(commands[2].endswith("(limit 90 s)"), commands[2])
        self.assertIn("dry run: 3 processes", stdout)
        self.assertFalse(os.path.exists(self.out), "a dry run writes nothing")

    def test_expect_processes(self):
        self.write_plan(make_process("a"), make_process("b"))
        self.assertEqual(self.invoke("--expect-processes", "2", dry=True)[0], 0)
        code, _, stderr = self.invoke("--expect-processes", "3", dry=True)
        self.assertEqual(code, 2)
        self.assertIn("expected 3", stderr)

    def test_other_system_and_bad_build_only_warn(self):
        self.write_plan(make_process("a"), system=OTHER_SYSTEM[rc.host_system()])
        debug = os.path.join(self.root, "debug build")
        self.make_build(debug, build_type="Debug", binaries=False)
        code, _, stderr = self.invoke(dry=True, build=debug)
        self.assertEqual(code, 0, stderr)
        for needle in ("the plan is for", "not Release", "no wakeprobe", "no abpair"):
            self.assertIn(needle, stderr)

    def test_existing_records_still_refuse(self):
        self.write_plan(make_process("a"))
        os.makedirs(self.out)
        with open(self.path("old.jsonl"), "w") as handle:
            handle.write("{}\n")
        code, _, stderr = self.invoke(dry=True)
        self.assertEqual(code, 2)
        self.assertIn("already holds records", stderr)

    def test_sample_plan(self):
        with open(SAMPLE_PLAN, "rb") as handle:
            expected = len(handle.read().splitlines()) - 1
        self.assertEqual(expected, 12)
        self.plan_path = SAMPLE_PLAN
        code, stdout, stderr = self.invoke("--expect-processes", str(expected), dry=True, stubs=False)
        self.assertEqual(code, 0, stderr)
        commands = [line for line in stdout.splitlines() if re.match(r"\d+: ", line)]
        self.assertEqual(len(commands), expected)
        self.assertTrue(any("abpair" in line for line in commands))
        self.assertTrue(all("--worker-cpus" in line and "--out" in line and "(limit " in line for line in commands))
        self.assertNotIn("generated_by", stderr, "the sample's header has the real form")

    def test_quote(self):
        words = ["a b", "c"]
        self.assertEqual(rc.quote(words), '"a b" c' if sys.platform == "win32" else "'a b' c")

    def test_the_dry_run_pins_nothing(self):
        self.write_plan(make_process("a"))
        with mock.patch.object(rc, "RUNNER_CPU", 10_000):  # a real run could not pin to this
            code, _, stderr = self.invoke(dry=True, lab_job=None)
        self.assertEqual(code, 0, stderr)


class ProvenanceTests(CampaignCase):
    """Contract C2: outside a dry run, generated_by has the real form of the plan's kind."""

    CAMPAIGN_CASES = {
        "a draft": "analysis/v2/plan.py draft",
        "a stand-in": "analysis/v2/plan.py " + SHA1 + " stand-in",
        "a pilot form on a campaign plan": PILOT_BY,
        "short commit": "analysis/v2/plan.py " + SHA1[:39] + " revision-log-v2.md sha256:" + "ab" * 32,
        "upper case hex": CAMPAIGN_BY.replace("abcdef", "ABCDEF", 1),
        "short digest": CAMPAIGN_BY[:-1],
        "a trailing newline": CAMPAIGN_BY + "\n",
        "a trailing space": CAMPAIGN_BY + " ",
        "another script": CAMPAIGN_BY.replace("plan.py", "plan2.py"),
        "another log": CAMPAIGN_BY.replace("revision-log-v2.md", "revision-log.md"),
        "no sha256 prefix": CAMPAIGN_BY.replace("sha256:", ""),
        "empty": "",
        "hand-written": "test_run_campaign",
    }

    def test_real_forms_run(self):
        self.write_plan(make_process("a"), make_process("b", family="sweep"))
        code, _, stderr = self.invoke()
        self.assertEqual(code, 0, stderr)
        fields = self.run_txt()
        self.assertEqual(fields["plan generated_by"], CAMPAIGN_BY)
        self.assertEqual(fields["plan kind"], "campaign")
        self.out = os.path.join(self.root, "pilot out")
        self.write_plan(make_process("p", family="pilot"), make_process("q", family="pilot"))
        code, _, stderr = self.invoke()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.run_txt()["plan generated_by"], PILOT_BY)
        self.assertEqual(self.run_txt()["plan kind"], "pilot")

    def test_a_campaign_plan_with_a_wrong_header_is_refused(self):
        for label, text in self.CAMPAIGN_CASES.items():
            with self.subTest(label):
                self.write_plan(make_process("a"), make_process("b", family="pilot"), by=text)
                code, _, stderr = self.invoke()
                self.assert_refused(code, stderr, "generated_by")

    def test_a_pilot_plan_with_a_wrong_header_is_refused(self):
        cases = {"a campaign form on a pilot plan": CAMPAIGN_BY, "a draft": "analysis/v2/plan.py draft",
                 "a trailing newline": PILOT_BY + "\n", "long commit": PILOT_BY + "0", "empty": ""}
        for label, text in cases.items():
            with self.subTest(label):
                self.write_plan(make_process("p", family="pilot"), by=text)
                code, _, stderr = self.invoke()
                self.assert_refused(code, stderr, "generated_by")

    def test_one_line_of_another_family_makes_the_plan_a_campaign_plan(self):
        self.write_plan(make_process("p", family="pilot"), make_process("q", family="H4"), by=PILOT_BY)
        code, _, stderr = self.invoke()
        self.assert_refused(code, stderr, "campaign plan")

    def test_a_dry_run_only_warns(self):
        for label, text in {**self.CAMPAIGN_CASES, "a draft pilot": PILOT_BY + " draft"}.items():
            with self.subTest(label):
                self.write_plan(make_process("a"), by=text)
                code, stdout, stderr = self.invoke(dry=True, lab_job=None)
                self.assertEqual(code, 0, stderr)
                self.assertIn("warning", stderr)
                self.assertIn("generated_by", stderr)
                self.assertIn("dry run: 1 processes", stdout)
                self.assertFalse(os.path.exists(self.out))

    def test_a_dry_run_of_a_real_header_does_not_warn(self):
        self.write_plan(make_process("a"))
        self.assertNotIn("generated_by", self.invoke(dry=True, lab_job=None)[2])

    def test_a_resume_checks_it_too(self):
        self.write_plan(make_process("a"))
        self.assertEqual(self.invoke()[0], 0)
        self.write_plan(make_process("a"), by="analysis/v2/plan.py draft")
        before = self.snapshot()
        code, _, stderr = self.invoke("--resume")
        self.assertEqual(code, 2)
        self.assertIn("generated_by", stderr)
        self.assertEqual(self.snapshot(), before)


class RefusalTests(CampaignCase):
    def test_lab_job_unset_or_empty(self):
        self.write_plan(make_process("a"))
        for value in (None, ""):
            with self.subTest(lab_job=value):
                code, _, stderr = self.invoke(lab_job=value)
                self.assert_refused(code, stderr, "LAB_JOB")

    def test_output_dir_holds_records_or_planned_files(self):
        self.write_plan(make_process("a"))
        os.makedirs(os.path.join(self.out, "sub"))
        for name, needle in (("sub/old.JSONL", "already holds records"), ("a.err", "would write"),
                             ("a.done", "would write"), ("A.DONE.TMP", "would write"),
                             ("a.attempt1", "would write"), ("A.attempt1.tmp", "would write"),
                             ("a.pid", "would write"), (rc.LOCK_FILE, "would write"),
                             (rc.RUN_FILE, "would write"), (rc.LOG_FILE, "would write")):
            with self.subTest(name=name):
                target = os.path.join(self.out, *name.split("/"))
                with open(target, "w") as handle:
                    handle.write("x")
                code, _, stderr = self.invoke()
                self.assertEqual(code, 2)
                self.assertIn(needle, stderr)
                os.remove(target)
        self.assertEqual(os.listdir(os.path.join(self.out)), ["sub"], "a refusal writes nothing")

    def test_system_mismatch(self):
        self.write_plan(make_process("a"), system=OTHER_SYSTEM[rc.host_system()])
        code, _, stderr = self.invoke()
        self.assert_refused(code, stderr, "the plan is for")

    def test_plans(self):
        good = make_process("a")
        cases = {
            "empty file": (b"", "is empty"),
            "not utf-8": (b"\xff\xfe\x00", "not UTF-8"),
            "header not json": (b"{plan\n", "not valid JSON"),
            "header only": (jsonl(make_header()), "no process"),
            "plan version": (jsonl({**make_header(), "plan": 2}, good), '"plan" must be 1'),
            "system": (jsonl({**make_header(), "system": "macos"}, good), '"system" must be'),
            "seed missing": (jsonl({k: v for k, v in make_header().items() if k != "seed"}, good), "missing key"),
            "header key": (jsonl({**make_header(), "extra": 1}, good), "unknown key"),
            "unknown tool": (jsonl(make_header(), {**good, "tool": "wakeprob"}), "unknown tool"),
            "process key": (jsonl(make_header(), {**good, "extra": 1}), "unknown key"),
            "limit_s missing": (jsonl(make_header(), {k: v for k, v in good.items() if k != "limit_s"}),
                                "missing key(s) limit_s"),
            "family": (jsonl(make_header(), {**good, "family": "X"}), '"family" must be'),
            "empty cell": (jsonl(make_header(), {**good, "cell": ""}), '"cell"'),
            "duplicate id": (jsonl(make_header(), good, make_process("b"), good), "duplicate id"),
            "duplicate id, case": (jsonl(make_header(), good, make_process("A")), "duplicate id"),
            "args not a list": (jsonl(make_header(), {**good, "args": "--backend epoll"}), '"args" must be a list'),
            "args not strings": (jsonl(make_header(), {**good, "args": ["--workers", 1]}), '"args" must be a list'),
            "args hold --out": (jsonl(make_header(), {**good, "args": ["--out", "x.jsonl"]}), "must not hold --out"),
            "args hold NUL": (jsonl(make_header(), {**good, "args": ["a\0b"]}), "NUL character"),
            "args hold --out=": (jsonl(make_header(), {**good, "args": ["--out=x.jsonl"]}), "must not hold --out"),
        }
        for value in (0, -1, 1.5, 1.0, "60", None, True, [], rc.MAX_LIMIT_S + 1):
            cases[f"limit_s {value!r}"] = (jsonl(make_header(), {**good, "limit_s": value}), '"limit_s" must be')
        for pid in ("NUL", "con.x", "a/b", "a b", "../x", "-x", "x.", "a-rerun", "A-RERUN", "", 5,
                    "a.interrupted-1", "a.Interrupted-2.b"):
            cases[f"id {pid!r}"] = (jsonl(make_header(), {**good, "id": pid}), "id ")
        for label, (data, needle) in cases.items():
            with self.subTest(label):
                self.write_plan(raw=data)
                code, _, stderr = self.invoke()
                self.assert_refused(code, stderr, needle)

    def test_the_limits_that_are_allowed(self):
        self.write_plan(make_process("a", limit_s=1), make_process("b", limit_s=rc.MAX_LIMIT_S))
        self.assertEqual(self.invoke(dry=True, lab_job=None)[0], 0)

    def test_missing_plan_file(self):
        code, _, stderr = self.invoke()
        self.assert_refused(code, stderr, "cannot read the plan")

    def test_builds(self):
        self.write_plan(make_process("a"))
        builds = {
            "Debug": dict(build_type="Debug"),
            "RelWithDebInfo": dict(build_type="RelWithDebInfo"),
            "no cache": dict(build_type=None),
            "multi-config": dict(extra_cache="CMAKE_CONFIGURATION_TYPES:STRING=Debug;Release\n"),
            "no binaries": dict(binaries=False),
        }
        for label, options in builds.items():
            with self.subTest(label):
                build = os.path.join(self.root, "build " + label)
                self.make_build(build, **options)
                code, _, stderr = self.invoke(build=build)
                self.assert_refused(code, stderr, "")
                self.assertTrue(any(w in stderr for w in ("Release", "multi-config", "no wakeprobe")), stderr)
        code, _, stderr = self.invoke(build=os.path.join(self.root, "nowhere"))
        self.assert_refused(code, stderr, "no build directory")

    def test_expect_processes_mismatch(self):
        self.write_plan(make_process("a"))
        code, _, stderr = self.invoke("--expect-processes", "2")
        self.assert_refused(code, stderr, "expected 2")

    def test_bad_command_lines(self):
        for argv in ([], ["--plan", self.plan_path], ["--bogus"], ["--plan", "p", "--build-dir", "b",
                     "--out-dir", "o", "--dry"]):  # an abbreviation of --dry-run is not accepted
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(rc.main(argv), 2)


class RunTests(CampaignCase):
    def test_all_pass(self):
        self.write_plan(make_process("a"), make_process("b", tool="abpair"), make_process("c"))
        code, stdout, stderr = self.invoke()
        self.assertEqual(code, 0, stderr)
        self.assertIn("incomplete processes: 0", stdout)
        rows = self.log()
        self.assertEqual([(r[1], r[2], r[3]) for r in rows], [("a", "1", "0"), ("b", "1", "0"), ("c", "1", "0")])
        for pid, tool in (("a", "wakeprobe"), ("b", "abpair"), ("c", "wakeprobe")):
            record = self.record(pid + ".jsonl")
            self.assertEqual(record["tool"], tool + rc.EXE_SUFFIX)
            self.assertEqual(record["argv"], make_process(pid)["args"] + ["--out", self.path(pid + ".jsonl")])
            self.assertTrue(record["run_txt"], "run.txt must exist before the first process starts")
            self.assertTrue(record["lock"], "campaign.lock is held while a process runs")
            self.assertFalse(os.path.exists(self.path(pid + "-rerun.jsonl")))
            self.assertIn("stdout of attempt 1", self.read(pid + ".out"))
            self.assertIn("stderr of attempt 1", self.read(pid + ".err"))
            self.assertEqual(self.read(pid + ".attempt1"), "attempt 1 exit: 0\n")
        self.assertEqual(sorted(os.listdir(self.out)), sorted(
            [rc.RUN_FILE, rc.LOG_FILE] + [f"{p}{e}" for p in "abc" for e in (".jsonl", ".out", ".err", ".done", ".attempt1")]),
            "the lock and the pid files are gone when the run ends")

    @unittest.skipIf(sys.platform == "win32", "a process group is a POSIX idea")
    def test_a_tool_leads_its_own_process_group(self):
        self.write_plan(make_process("a"))
        self.assertEqual(self.invoke()[0], 0)
        self.assertIs(self.record("a.jsonl")["group_leader"], True)

    def test_run_txt(self):
        data = self.write_plan(make_process("a"))
        self.assertEqual(self.invoke()[0], 0)
        fields = self.run_txt()
        self.assertEqual(fields["plan sha256"], hashlib.sha256(data).hexdigest())
        for tool in rc.TOOLS:
            expected = hashlib.sha256(b"not a program: " + tool.encode("ascii")).hexdigest()
            self.assertEqual(fields[tool + " sha256"], expected)
        self.assertEqual(fields["build type"], "Release")
        self.assertEqual(fields["host"], platform.node())
        self.assertEqual(fields["plan system"], rc.host_system())
        self.assertEqual(fields["processes"], "1")
        self.assertEqual(fields["lab job"], "1")
        self.assertEqual(fields["plan generated_by"], CAMPAIGN_BY)
        self.assertEqual(fields["runner cpu"], str(rc.RUNNER_CPU))
        self.assertEqual(fields["tool cpus at start"].split(), [str(c) for c in sorted(STARTUP_CPUS)])
        self.assertRegex(fields["started"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")

    def test_fail_then_pass_reruns_once_and_keeps_first_run(self):
        self.write_plan(make_process("a", codes="1,0"), make_process("b"))
        code, stdout, stderr = self.invoke()
        self.assertEqual(code, 0, stderr)
        self.assertIn("incomplete processes: 0", stdout)
        self.assertEqual([(r[1], r[2], r[3]) for r in self.log()],
                         [("a", "1", "1"), ("a", "2", "0"), ("b", "1", "0")])
        self.assertEqual(self.record("a.jsonl")["attempt"], 1)
        self.assertEqual(self.record("a-rerun.jsonl")["attempt"], 2)
        self.assertEqual(self.record("a-rerun.jsonl")["argv"][:-2], self.record("a.jsonl")["argv"][:-2])
        self.assertIn("stdout of attempt 1", self.read("a.out"))
        self.assertIn("stderr of attempt 1", self.read("a.err"))
        self.assertIn("stdout of attempt 2", self.read("a-rerun.out"))
        self.assertIn("stderr of attempt 2", self.read("a-rerun.err"))
        self.assertFalse(os.path.exists(self.path("b-rerun.jsonl")))
        self.assertEqual(self.read("a.attempt1"), "attempt 1 exit: 1\n")
        self.assertEqual(self.record("a-rerun.jsonl")["attempt1_file"], "attempt 1 exit: 1\n",
                         "the marker is written before the rerun starts")

    def test_fail_twice_is_incomplete_and_the_run_goes_on(self):
        self.write_plan(make_process("a", codes="7,3"), make_process("b"), make_process("c", codes="5,5"),
                        make_process("d"))
        code, stdout, stderr = self.invoke()
        self.assertEqual(code, 1, stderr)
        self.assertIn("incomplete processes: 2", stdout)
        rows = [(r[1], r[2], r[3] if len(r) > 3 else None) for r in self.log()]
        self.assertEqual(rows, [("a", "1", "7"), ("a", "2", "3"), ("a", "INCOMPLETE", None), ("b", "1", "0"),
                                ("c", "1", "5"), ("c", "2", "5"), ("c", "INCOMPLETE", None), ("d", "1", "0")])
        self.assertIn("a INCOMPLETE", stdout)
        for name in ("a.jsonl", "a-rerun.jsonl", "a.err", "a-rerun.err", "d.jsonl"):
            self.assertTrue(os.path.exists(self.path(name)), name)

    def test_a_tool_that_cannot_start_is_incomplete(self):
        self.write_plan(make_process("a"))
        with mock.patch.object(rc, "launch_prefix", lambda path: [os.path.join(self.root, "no such tool")]):
            code, stdout, _ = self.invoke(stubs=False)
        self.assertEqual(code, 1)
        self.assertEqual([(r[1], r[2], r[3]) for r in self.log() if len(r) > 3],
                         [("a", "1", "127"), ("a", "2", "127")])
        self.assertIn("cannot start", self.read("a.err"))
        self.assertIn("incomplete processes: 1", stdout)
        self.assertFalse(os.path.exists(self.path(rc.LOCK_FILE)))

    def test_a_second_run_in_the_same_directory_is_refused(self):
        self.write_plan(make_process("a"))
        self.assertEqual(self.invoke()[0], 0)
        code, _, stderr = self.invoke()
        self.assertEqual(code, 2)
        self.assertIn("already holds records", stderr)


class TimeoutTests(CampaignCase):
    """A tool that does not end is killed at its limit_s, and that run has failed (section 8)."""

    def test_a_timeout_is_a_failed_run_and_the_rerun_passes(self):
        slow = os.path.join(self.root, "slow")
        self.write_plan(make_process("a", limit_s=2, extra=("--stub-marker", slow, "--stub-sleep", "60")),
                        make_process("b"))
        code, stdout, stderr = self.invoke()
        self.assertEqual(code, 0, stderr)
        rows = self.log()
        self.assertEqual([(r[1], r[2], r[3]) for r in rows], [("a", "1", "timeout"), ("a", "2", "0"), ("b", "1", "0")])
        self.assertTrue(2.0 <= float(rows[0][4]) < 30, rows[0])
        self.assertEqual(self.read("a.done"), "outcome: ok after rerun\nattempt 1 exit: timeout\nattempt 2 exit: 0\n")
        self.assertEqual(self.read("a.attempt1"), "attempt 1 exit: timeout\n")
        self.assertFalse(process_is_running(self.record("a.jsonl")["pid"]), "the tool must be dead")
        self.assertEqual(self.record("a-rerun.jsonl")["attempt"], 2)
        self.assertIn("incomplete processes: 0", stdout)
        self.assertFalse(os.path.exists(self.path("a.pid")))

    def test_two_timeouts_make_the_process_incomplete_and_kill_the_tool_and_what_it_started(self):
        spawned = os.path.join(self.root, "children")
        self.write_plan(make_process("a", limit_s=2, extra=("--stub-pause", "60", "--stub-spawn", spawned)),
                        make_process("b"))
        code, stdout, stderr = self.invoke()
        self.assertEqual(code, 1, stderr)
        self.assertIn("incomplete processes: 1", stdout)
        rows = [(r[1], r[2], r[3] if len(r) > 3 else None) for r in self.log()]
        self.assertEqual(rows, [("a", "1", "timeout"), ("a", "2", "timeout"), ("a", "INCOMPLETE", None),
                                ("b", "1", "0")])
        self.assertEqual(self.read("a.done"),
                         "outcome: incomplete\nattempt 1 exit: timeout\nattempt 2 exit: timeout\n")
        with open(spawned) as handle:
            children = [int(line) for line in handle.read().split()]
        self.assertEqual(len(children), 2, "one child for each run")
        pids = children + [self.record("a.jsonl")["pid"], self.record("a-rerun.jsonl")["pid"]]
        self.wait_until(lambda: not any(process_is_running(p) for p in pids), "a tool or its child is still running")
        self.assertEqual(self.read("b.done"), "outcome: ok\nattempt 1 exit: 0\n")

    def test_what_a_tool_started_is_killed_when_the_tool_ends_normally_too(self):
        spawned = os.path.join(self.root, "children")
        self.write_plan(make_process("a", extra=("--stub-spawn", spawned)))
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.read("a.done"), "outcome: ok\nattempt 1 exit: 0\n")
        with open(spawned) as handle:
            children = [int(line) for line in handle.read().split()]
        self.assertEqual(len(children), 1)
        self.wait_until(lambda: not process_is_running(children[0]), "the tool's child outlived the run")

    def test_the_largest_limit_is_not_cut_short_by_the_wait_of_the_system(self):
        """Windows waits take 32 bits of milliseconds; a larger number wraps (see MAX_LIMIT_S)."""
        popen = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(1.2)"])
        self.addCleanup(lambda: (popen.kill(), popen.wait()))
        started = time.monotonic()
        self.assertEqual(rc.RunningTool(popen).wait(rc.MAX_LIMIT_S), 0)
        self.assertGreaterEqual(time.monotonic() - started, 1.0, "the wait ran its course")

    def test_a_tool_that_ends_in_time_is_not_a_timeout(self):
        self.write_plan(make_process("a", limit_s=60, extra=("--stub-pause", "0.2")))
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.read("a.done"), "outcome: ok\nattempt 1 exit: 0\n")

    def test_the_timeout_text_is_read_back(self):
        os.makedirs(self.out)
        record = rc.Done("incomplete", (rc.TIMEOUT, 3))
        rc.write_done(self.out, "p", record)
        self.assertEqual(self.read("p.done"), "outcome: incomplete\nattempt 1 exit: timeout\nattempt 2 exit: 3\n")
        self.assertEqual(rc.parse_done(self.read("p.done")), record)
        self.assertEqual(rc.outcome_of((rc.TIMEOUT, 0)), "ok after rerun")
        self.assertEqual(rc.outcome_of((rc.TIMEOUT, rc.TIMEOUT)), "incomplete")


class DoneFileTests(CampaignCase):
    def test_done_files_hold_the_outcome_and_the_exit_codes_only(self):
        self.write_plan(make_process("a"), make_process("b", codes="1,0"), make_process("c", codes="7,3"))
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.read("a.done"), "outcome: ok\nattempt 1 exit: 0\n")
        self.assertEqual(self.read("b.done"), "outcome: ok after rerun\nattempt 1 exit: 1\nattempt 2 exit: 0\n")
        self.assertEqual(self.read("c.done"), "outcome: incomplete\nattempt 1 exit: 7\nattempt 2 exit: 3\n")
        self.assertEqual([n for n in os.listdir(self.out) if n.endswith(".tmp")], [])

    def test_done_and_the_marker_are_moved_into_place_from_temporary_files(self):
        calls = []
        real_replace = os.replace

        def spy(source, target):
            calls.append((os.path.basename(source), os.path.basename(target), os.path.exists(source),
                          os.path.exists(target)))
            real_replace(source, target)

        self.write_plan(make_process("a"))
        with mock.patch.object(os, "replace", spy):
            self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(calls, [("a.attempt1.tmp", "a.attempt1", True, False),
                                 ("a.done.tmp", "a.done", True, False)])

    def test_the_temporary_files_are_never_written_over(self):
        os.makedirs(self.out)
        for name, write in (("a.done.tmp", lambda: rc.write_done(self.out, "a", rc.Done("ok", (0,)))),
                            ("a.attempt1.tmp", lambda: rc.write_marker(self.out, "a", 0))):
            with self.subTest(name=name):
                with open(self.path(name), "w") as handle:
                    handle.write("old")
                with self.assertRaises(FileExistsError):
                    write()
                self.assertEqual(self.read(name), "old")
                self.assertFalse(os.path.exists(self.path(name[:-4])))

    def test_what_the_runner_writes_is_read_back(self):
        os.makedirs(self.out)
        for record in (rc.Done("ok", (0,)), rc.Done("ok after rerun", (1, 0)), rc.Done("incomplete", (-9, 127)),
                       rc.Done("ok after rerun", ("timeout", 0))):
            with self.subTest(record=record):
                rc.write_done(self.out, "p", record)
                self.assertEqual(rc.parse_done(self.read("p.done")), record)
                os.remove(self.path("p.done"))
        for code in (0, 1, -9, "timeout"):
            with self.subTest(code=code):
                rc.write_marker(self.out, "p", code)
                self.assertEqual(rc.parse_marker(self.read("p.attempt1")), code)
                os.remove(self.path("p.attempt1"))

    def test_other_text_is_not_a_done_file(self):
        bad = ["", "garbage", "outcome: ok\n", "outcome: fine\nattempt 1 exit: 0\n",
               "outcome: ok\nattempt 1 exit: 1\n", "outcome: ok after rerun\nattempt 1 exit: 1\n",
               "outcome: incomplete\nattempt 1 exit: 1\nattempt 2 exit: 0\n",
               "outcome: ok\nattempt 1 exit: 0\nattempt 2 exit: 0\n", "outcome: ok\nattempt 1 exit: zero\n",
               "outcome: ok\nattempt 1 exit: 0\nseconds: 1.5\n", "outcome: ok\noutcome: ok\nattempt 1 exit: 0\n",
               "outcome: ok\nattempt 2 exit: 0\n", "outcome: ok\nattempt 1 exit: timeout\n",
               "outcome: ok after rerun\nattempt 1 exit: timeout\nattempt 2 exit: Timeout\n"]
        for text in bad:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    rc.parse_done(text)

    def test_other_text_is_not_an_attempt1_file(self):
        for text in ("", "attempt 1 exit: 0", "attempt 1 exit: zero\n", "attempt 2 exit: 0\n", "attempt 1 exit: 0\n\n",
                     "attempt 1 exit: 0\nattempt 2 exit: 0\n", "attempt 1 exit: 1.5\n", "attempt 1 exit: \n"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    rc.parse_marker(text)


class ResumeCase(CampaignCase):
    PARTIAL = b'{"partial": '

    def finished_run(self, *processes):
        """Write the plan and run it to its end with stubs."""
        self.write_plan(*processes)
        code, _, stderr = self.invoke()
        self.assertIn(code, (0, 1), stderr)

    def kill_during(self, pid, never_started=(), stage="first"):
        """Make the directory look as if the runner was killed while it ran `pid`.

        first   during the first run: no .done and no .attempt1 file, the first record cut short.
        rerun   during the rerun (the process must have had one): no .done file, the rerun's record
                cut short; the first run's files and its .attempt1 file are as they were.
        marker  after the first run ended and its .attempt1 file was written, before the .done file.
        The files of the processes in `never_started` are removed: they never began."""
        os.remove(self.path(pid + rc.DONE_SUFFIX))
        if stage == "first":
            with contextlib.suppress(FileNotFoundError):
                os.remove(self.path(pid + rc.MARKER_SUFFIX))
        if stage in ("first", "rerun"):
            with open(self.path(pid + (".jsonl" if stage == "first" else "-rerun.jsonl")), "wb") as handle:
                handle.write(self.PARTIAL)
        for other in never_started:
            for stem, ext in rc.id_files(other):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(self.path(stem + ext))
        gone = {pid, *never_started}
        kept = []
        for line in self.read(rc.LOG_FILE).splitlines():
            fields = line.split(" ")
            if " RENAMED " in line or fields[1] not in gone:
                kept.append(line)
            elif stage in ("rerun", "marker") and fields[1] == pid and "attempt=1" in line:
                kept.append(line)
        with open(self.path(rc.LOG_FILE), "w", encoding="ascii", newline="\n") as handle:
            handle.write("".join(line + "\n" for line in kept))

    def tail(self, name, before):
        """What was appended to a file since `before` (its earlier text)."""
        text = self.read(name)
        self.assertTrue(text.startswith(before), name + " must only grow")
        return text[len(before):]


class ResumeTests(ResumeCase):
    def five(self):
        self.finished_run(make_process("a"), make_process("b", codes="1,0"), make_process("c", codes="7,3"),
                          make_process("d"), make_process("e"))

    def test_resume_after_a_kill(self):
        self.five()
        self.kill_during("d", never_started=["e"])
        with open(self.path("d.done.tmp"), "w") as handle:  # a leftover temporary file goes with the void run
            handle.write("outcome: ok\n")
        before, run_txt, log = self.snapshot(), self.read(rc.RUN_FILE), self.read(rc.LOG_FILE)
        code, stdout, stderr = self.invoke("--resume")
        self.assertEqual(code, 1, stderr)  # c is INCOMPLETE; the count comes from its .done file
        self.assertIn("incomplete processes: 1", stdout)
        self.assertIn("resuming 5 processes", stdout)
        self.assertIn("process 4/5: d", stdout)
        self.assertNotIn("process 1/5", stdout)
        after = self.snapshot()
        for pid in "abc":  # not run again: not one byte changed
            for stem, ext in rc.id_files(pid):
                name = stem + ext
                self.assertEqual(after.get(name), before.get(name), name)
        for ext in (".jsonl", ".out", ".err", ".done.tmp"):  # the interrupted process is set aside whole
            self.assertEqual(after["d.interrupted-1" + ext], before["d" + ext], ext)
        self.assertEqual(before["d.jsonl"], self.PARTIAL)
        self.assertEqual(self.record("d.jsonl")["attempt"], 1)
        self.assertEqual(self.record("e.jsonl")["attempt"], 1)
        self.assertEqual(self.read("d.done"), "outcome: ok\nattempt 1 exit: 0\n")
        self.assertEqual(self.read("e.done"), "outcome: ok\nattempt 1 exit: 0\n")
        self.assertEqual(self.read("d.attempt1"), "attempt 1 exit: 0\n")
        self.assertFalse(os.path.exists(self.path("d.done.tmp")))
        self.assertFalse(os.path.exists(self.path(rc.LOCK_FILE)), "the lock is removed when the run ends")
        # run.txt only grew: the earlier text is the same, one resume line follows
        first_block = self.read(rc.RUN_FILE).split("resumed: ")[0]
        self.assertEqual(first_block, run_txt)
        digests = [self.run_txt()[tool + " sha256"] for tool in rc.TOOLS]
        pattern = (r"resumed: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d; lab job 1; "
                   + "; ".join(f"{tool} sha256 ({digest})" for tool, digest in zip(rc.TOOLS, digests)) + r"\n")
        self.assertRegex(self.tail(rc.RUN_FILE, run_txt), "^" + pattern + "$")
        # campaign.log only grew: the resume, the set-aside files, then the two processes that ran
        added = self.log_events(log)
        self.assertEqual(added[:2], ["- RESUME skipped=3 renamed=1 marked=0 to-run=2",
                                     "d RENAMED interrupted-1 files=4"])
        self.assertEqual(len(added), 4, added)
        self.assertTrue(added[2].startswith("d attempt=1 exit=0 seconds="), added)
        self.assertTrue(added[3].startswith("e attempt=1 exit=0 seconds="), added)

    def test_a_rename_that_fails_leaves_run_txt_and_the_log_as_they_were(self):
        """The Windows finding: a rename failed (PermissionError 32) after run.txt and the log were appended."""
        self.five()
        self.kill_during("d")
        dead = self.dead_pid()
        self.write_holder_file(rc.LOCK_FILE, dead, image="python", created="7")  # the dead runner's lock
        run_txt, log = self.read(rc.RUN_FILE), self.read(rc.LOG_FILE)
        with mock.patch.object(os, "rename", side_effect=PermissionError(32, "in use by another process")):
            code, _, stderr = self.invoke("--resume")
        self.assertEqual(code, 3, stderr)
        self.assertEqual(self.read(rc.RUN_FILE), run_txt)
        self.assertEqual(self.read(rc.LOG_FILE), log)
        self.assertFalse(os.path.exists(self.path(rc.LOCK_FILE)), "the run's own lock is released")
        self.assertEqual(self.invoke("--resume")[0], 1)  # nothing is lost: the next resume goes through
        self.assertEqual(self.read("d.done"), "outcome: ok\nattempt 1 exit: 0\n")

    def test_a_finished_campaign_resumes_to_nothing_and_a_spent_budget_does_not_stop_it(self):
        self.five()
        before, log = self.snapshot(), self.read(rc.LOG_FILE)
        clock = itertools.chain([0.0], itertools.repeat(99.0))  # the 1 s budget is gone at the first look
        with mock.patch.object(rc, "time", mock.Mock(monotonic=lambda: next(clock))):
            code, stdout, stderr = self.invoke("--resume", "--stop-after-s", "1")
        self.assertEqual(code, 1, stderr)
        self.assertIn("incomplete processes: 1", stdout)
        self.assertNotRegex(stdout, r"process \d+/")
        after = self.snapshot()
        self.assertEqual({k: v for k, v in after.items() if k not in (rc.RUN_FILE, rc.LOG_FILE)},
                         {k: v for k, v in before.items() if k not in (rc.RUN_FILE, rc.LOG_FILE)})
        self.assertEqual(self.log_events(log), ["- RESUME skipped=5 renamed=0 marked=0 to-run=0"])

    def test_the_final_count_includes_outcomes_read_from_done_files(self):
        self.finished_run(make_process("a", codes="7,7"), make_process("b", codes="5,5"), make_process("c"))
        os.remove(self.path("c.done"))
        before = self.snapshot()
        code, stdout, _ = self.invoke("--resume")
        self.assertEqual(code, 1)
        self.assertIn("incomplete processes: 2", stdout)
        self.assertEqual(self.read("c.done"), "outcome: ok\nattempt 1 exit: 0\n")
        self.assertEqual(self.snapshot()["c.jsonl"], before["c.jsonl"], "c is not run again")

    def test_interrupted_files_get_increasing_k_and_are_never_overwritten(self):
        self.finished_run(make_process("a"), make_process("b", codes="1,0"))
        old = b"the record of an earlier interruption\n"
        with open(self.path("b.interrupted-1.jsonl"), "wb") as handle:  # no .err beside it
            handle.write(old)
        with open(self.path("b-rerun.interrupted-3.err"), "wb") as handle:  # the highest k is on a rerun file
            handle.write(old)
        for k in (4, 5, 6):
            self.kill_during("b", stage="rerun")  # killed in its rerun: only the rerun is void
            before = self.snapshot()
            self.assertEqual(self.invoke("--resume")[0], 0)
            after = self.snapshot()
            for name, data in before.items():
                if ".interrupted-" in name:
                    self.assertEqual(after[name], data, name + " must not change")
            for ext in (".jsonl", ".out", ".err"):
                self.assertEqual(after[f"b-rerun.interrupted-{k}{ext}"], before["b-rerun" + ext], (k, ext))
                self.assertEqual(after["b" + ext], before["b" + ext], "the first run stays")
                self.assertNotIn(f"b.interrupted-{k}{ext}", after)
            self.assertEqual(self.read("b.done"), "outcome: ok after rerun\nattempt 1 exit: 1\nattempt 2 exit: 0\n")
        self.kill_during("b")  # killed in its first run: both runs are void, and the next k is 7
        before = self.snapshot()
        self.assertEqual(self.invoke("--resume")[0], 0)
        after = self.snapshot()
        for stem in ("b", "b-rerun"):
            for ext in (".jsonl", ".out", ".err"):
                self.assertEqual(after[f"{stem}.interrupted-7{ext}"], before[stem + ext], (stem, ext))
        self.assertEqual(after["b.interrupted-1.jsonl"], old)
        self.assertNotIn("b.interrupted-1.err", after)
        self.assertEqual(sorted(n for n in after if n.startswith("a.")),
                         ["a.attempt1", "a.done", "a.err", "a.jsonl", "a.out"])
        renamed = [line.split(" ", 2)[1:] for line in self.read(rc.LOG_FILE).splitlines() if " RENAMED " in line]
        self.assertEqual(renamed, [["b", "RENAMED interrupted-4 files=3"], ["b", "RENAMED interrupted-5 files=3"],
                                   ["b", "RENAMED interrupted-6 files=3"], ["b", "RENAMED interrupted-7 files=6"]])

    def test_a_partly_renamed_process_gets_a_new_k_for_what_is_left(self):
        self.finished_run(make_process("a"))
        self.kill_during("a")
        os.rename(self.path("a.jsonl"), self.path("a.interrupted-1.jsonl"))  # a resume that died mid-rename
        self.assertEqual(self.invoke("--resume")[0], 0)
        self.assertEqual(self.read("a.interrupted-1.jsonl"), self.PARTIAL.decode("ascii"))
        for ext in (".out", ".err"):
            self.assertTrue(os.path.exists(self.path("a.interrupted-2" + ext)), ext)
        self.assertFalse(os.path.exists(self.path("a.interrupted-2.jsonl")))

    def test_files_are_matched_by_exact_name_not_by_prefix(self):
        self.finished_run(make_process("a"), make_process("a.b"), make_process("a.done"), make_process("a-x"))
        self.kill_during("a")
        before = self.snapshot()
        self.assertEqual(self.invoke("--resume")[0], 0)
        after = self.snapshot()
        moved = sorted(n for n in after if ".interrupted-" in n)
        self.assertEqual(moved, ["a.interrupted-1.err", "a.interrupted-1.jsonl", "a.interrupted-1.out"])
        for name in before:
            if name in (rc.RUN_FILE, rc.LOG_FILE):
                continue
            if not name.startswith("a.") or name.startswith(("a.b", "a.done")):
                self.assertEqual(after[name], before[name], name)
        self.kill_during("a.done")
        self.assertEqual(self.invoke("--resume")[0], 0)
        moved = sorted(n for n in self.snapshot() if ".interrupted-" in n)
        self.assertEqual(moved, ["a.done.interrupted-1.err", "a.done.interrupted-1.jsonl",
                                 "a.done.interrupted-1.out", "a.interrupted-1.err", "a.interrupted-1.jsonl",
                                 "a.interrupted-1.out"])

    def test_the_dry_run_of_a_resume_says_what_a_resume_does_and_writes_nothing(self):
        self.five()
        self.kill_during("d", never_started=["e"])
        with open(self.path("d.interrupted-1.jsonl"), "wb") as handle:
            handle.write(b"x")
        before = self.snapshot()
        code, stdout, stderr = self.invoke("--resume", dry=True, lab_job=None)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.snapshot(), before, "a dry run writes nothing")
        lines = stdout.splitlines()
        self.assertIn("1: skip a (done: ok)", lines)
        self.assertIn("2: skip b (done: ok after rerun)", lines)
        self.assertIn("3: skip c (done: incomplete)", lines)
        self.assertIn("4: rename d: d.jsonl to d.interrupted-2.jsonl, d.out to d.interrupted-2.out, "
                      "d.err to d.interrupted-2.err", lines)
        runs = [line for line in lines if re.match(r"\d+: run ", line)]
        self.assertEqual([line.split(":")[0] for line in runs], ["4", "5"])
        self.assertIn("d.jsonl", runs[0])
        self.assertIn("e.jsonl", runs[1])
        self.assertIn("(limit 600 s)", runs[0])
        self.assertIn("dry run: 5 processes, 3 to skip, 1 to rename, 0 to finish from a marker, 2 to run; "
                      "incomplete among the skipped: 1", lines)
        code, stdout, _ = self.invoke("--resume")  # the real resume ends with the same count
        self.assertEqual(code, 1)
        self.assertIn("incomplete processes: 1", stdout)
        self.assertEqual(self.read("d.interrupted-1.jsonl"), "x")
        self.assertEqual(self.read("d.interrupted-2.jsonl"), self.PARTIAL.decode("ascii"))

    def test_the_dry_run_of_a_resume_names_what_it_finishes_and_reruns(self):
        self.finished_run(make_process("a"), make_process("b", codes="1,0"), make_process("c", codes="1,0"))
        self.kill_during("a", stage="marker")
        self.kill_during("b", stage="rerun")
        before = self.snapshot()
        code, stdout, stderr = self.invoke("--resume", dry=True, lab_job=None)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.snapshot(), before)
        lines = stdout.splitlines()
        self.assertIn("1: finish a: a.attempt1 says attempt 1 exited 0; write a.done (ok)", lines)
        self.assertIn("2: rename b: b-rerun.jsonl to b-rerun.interrupted-1.jsonl, "
                      "b-rerun.out to b-rerun.interrupted-1.out, b-rerun.err to b-rerun.interrupted-1.err", lines)
        rerun = [line for line in lines if line.startswith("2: rerun ")]
        self.assertEqual(len(rerun), 1, lines)
        self.assertIn("b-rerun.jsonl", rerun[0])
        self.assertTrue(rerun[0].endswith("(b.attempt1 says attempt 1 exited 1)"), rerun[0])
        self.assertIn("3: skip c (done: ok after rerun)", lines)
        self.assertIn("dry run: 3 processes, 1 to skip, 1 to rename, 1 to finish from a marker, 1 to run; "
                      "incomplete among the skipped: 0", lines)

    def test_the_dry_run_of_a_resume_warns_about_other_binaries_and_refuses_another_plan(self):
        self.five()
        with open(rc.tool_path(self.build, "wakeprobe"), "wb") as handle:
            handle.write(b"another build")
        code, _, stderr = self.invoke("--resume", dry=True, lab_job=None)
        self.assertEqual(code, 0, stderr)
        self.assertIn("not the binaries this run started with", stderr)
        self.write_plan(make_process("a"))
        code, _, stderr = self.invoke("--resume", dry=True, lab_job=None)
        self.assertEqual(code, 2)
        self.assertIn("sha256", stderr)


class AttemptStateTests(ResumeCase):
    """Contract C3: a run the runner was killed in is void and never uses up an attempt."""

    def test_a_kill_during_the_rerun_continues_at_attempt_2(self):
        self.finished_run(make_process("a", codes="1,0"), make_process("b"))
        self.kill_during("a", stage="rerun")
        before, log = self.snapshot(), self.read(rc.LOG_FILE)
        code, _, stderr = self.invoke("--resume")
        self.assertEqual(code, 0, stderr)
        after = self.snapshot()
        for ext in (".jsonl", ".out", ".err"):
            self.assertEqual(after["a" + ext], before["a" + ext], "the first run is not run again")
            self.assertEqual(after["a-rerun.interrupted-1" + ext], before["a-rerun" + ext], "the void rerun is set aside")
        self.assertEqual(after["a.attempt1"], b"attempt 1 exit: 1\n")
        self.assertEqual(self.record("a-rerun.jsonl")["attempt"], 2)
        self.assertEqual(self.record("a-rerun.jsonl")["attempt1_file"], "attempt 1 exit: 1\n")
        self.assertEqual(self.read("a.done"), "outcome: ok after rerun\nattempt 1 exit: 1\nattempt 2 exit: 0\n")
        self.assertEqual([n for n in after if n.startswith("a.interrupted")], [], "attempt 1 was not void")
        events = self.log_events(log)
        self.assertEqual(events[:3], ["- RESUME skipped=1 renamed=1 marked=0 to-run=1", "a RENAMED interrupted-1 files=3",
                                      "a MARKER attempt1-exit=1"])
        self.assertEqual(len(events), 4, events)
        self.assertTrue(events[3].startswith("a attempt=2 exit=0 seconds="), events)

    def test_a_kill_during_a_rerun_that_fails_again_is_incomplete(self):
        self.finished_run(make_process("a", codes="7,3"))
        self.kill_during("a", stage="rerun")
        code, stdout, _ = self.invoke("--resume")
        self.assertEqual(code, 1)
        self.assertIn("incomplete processes: 1", stdout)
        self.assertEqual(self.read("a.done"), "outcome: incomplete\nattempt 1 exit: 7\nattempt 2 exit: 3\n")
        self.assertIn("a INCOMPLETE", stdout)

    def test_a_kill_after_the_marker_with_exit_0_writes_done_ok_and_runs_nothing(self):
        self.finished_run(make_process("a"), make_process("b"))
        self.kill_during("a", stage="marker")
        with open(self.path("a.done.tmp"), "w") as handle:  # killed while writing .done, too
            handle.write("outcome: ok\n")
        before, log = self.snapshot(), self.read(rc.LOG_FILE)
        code, stdout, stderr = self.invoke("--resume")
        self.assertEqual(code, 0, stderr)
        after = self.snapshot()
        for ext in (".jsonl", ".out", ".err", ".attempt1"):
            self.assertEqual(after["a" + ext], before["a" + ext], ext)
        self.assertEqual(after["a.interrupted-1.done.tmp"], before["a.done.tmp"])
        self.assertEqual(self.read("a.done"), "outcome: ok\nattempt 1 exit: 0\n")
        self.assertEqual(self.log_events(log), ["- RESUME skipped=1 renamed=1 marked=1 to-run=0",
                                                "a RENAMED interrupted-1 files=1", "a MARKER attempt1-exit=0"])
        self.assertNotIn("process ", stdout, "no process starts")

    def test_a_kill_before_the_marker_reruns_attempt_1_and_sets_the_void_run_aside(self):
        self.finished_run(make_process("a", codes="1,0"))
        self.kill_during("a", stage="first")
        with open(self.path("a.attempt1.tmp"), "w") as handle:  # killed while writing the marker
            handle.write("attempt 1 exit: 1\n")
        before, log = self.snapshot(), self.read(rc.LOG_FILE)
        code, _, stderr = self.invoke("--resume")
        self.assertEqual(code, 0, stderr)
        after = self.snapshot()
        for stem in ("a", "a-rerun"):
            for ext in (".jsonl", ".out", ".err"):
                self.assertEqual(after[f"{stem}.interrupted-1{ext}"], before[stem + ext], (stem, ext))
        self.assertEqual(after["a.interrupted-1.attempt1.tmp"], before["a.attempt1.tmp"])
        self.assertEqual(self.record("a.jsonl")["attempt"], 1)
        self.assertEqual(self.record("a-rerun.jsonl")["attempt"], 2)
        self.assertEqual(self.read("a.attempt1"), "attempt 1 exit: 1\n")
        self.assertEqual(self.read("a.done"), "outcome: ok after rerun\nattempt 1 exit: 1\nattempt 2 exit: 0\n")
        events = self.log_events(log)
        self.assertEqual(events[:2], ["- RESUME skipped=0 renamed=1 marked=0 to-run=1", "a RENAMED interrupted-1 files=7"])
        self.assertTrue(events[2].startswith("a attempt=1 exit=1 seconds="), events)
        self.assertTrue(events[3].startswith("a attempt=2 exit=0 seconds="), events)

    def test_a_failed_first_run_without_rerun_files_runs_only_attempt_2(self):
        self.finished_run(make_process("a", codes="1,0"))
        self.kill_during("a", stage="marker")  # killed right after the marker, before the rerun made a file
        for ext in (".jsonl", ".out", ".err"):
            os.remove(self.path("a-rerun" + ext))
        before = self.snapshot()
        code, _, stderr = self.invoke("--resume")
        self.assertEqual(code, 0, stderr)
        after = self.snapshot()
        self.assertEqual([n for n in after if ".interrupted-" in n], [], "nothing to set aside")
        for ext in (".jsonl", ".out", ".err", ".attempt1"):
            self.assertEqual(after["a" + ext], before["a" + ext], ext)
        self.assertEqual(self.read("a.done"), "outcome: ok after rerun\nattempt 1 exit: 1\nattempt 2 exit: 0\n")

    def test_a_timeout_in_the_marker_is_a_failed_first_run(self):
        self.finished_run(make_process("a", codes="0,0"))
        self.kill_during("a", stage="marker")
        self.write("a.attempt1", "attempt 1 exit: timeout\n")
        code, _, stderr = self.invoke("--resume")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(self.read("a.done"), "outcome: ok after rerun\nattempt 1 exit: timeout\nattempt 2 exit: 0\n")
        self.assertEqual(self.record("a-rerun.jsonl")["attempt"], 2)

    def test_a_stop_budget_does_not_hold_back_a_finish_from_a_marker(self):
        self.finished_run(make_process("a"), make_process("b"), make_process("c"))
        self.kill_during("a", stage="marker")
        self.kill_during("c", stage="marker")
        clock = itertools.chain([0.0], itertools.repeat(99.0))
        with mock.patch.object(rc, "time", mock.Mock(monotonic=lambda: next(clock))):
            code, _, stderr = self.invoke("--resume", "--stop-after-s", "1")
        self.assertEqual(code, 0, stderr)  # nothing is left to start, so nothing is "stopped"
        self.assertEqual(self.read("a.done"), "outcome: ok\nattempt 1 exit: 0\n")
        self.assertEqual(self.read("c.done"), "outcome: ok\nattempt 1 exit: 0\n")


class ResumeRefusalTests(ResumeCase):
    def setUp(self):
        super().setUp()
        self.finished_run(make_process("a"), make_process("b"), make_process("c"))
        self.kill_during("c")  # a resume that did not check first would set c's files aside

    def assert_resume_refused(self, needle, **options):
        before = self.snapshot()
        code, stdout, stderr = self.invoke("--resume", **options)
        self.assertEqual(code, 2, stdout + stderr)
        self.assertIn(needle, stderr)
        self.assertEqual(self.snapshot(), before, "a refusal must change nothing")

    def test_another_plan(self):
        self.write_plan(make_process("a"), make_process("b"), make_process("c"), make_process("d"))
        self.assert_resume_refused("the plan has sha256")

    def test_another_binary(self):
        for tool in rc.TOOLS:
            with self.subTest(tool=tool):
                path = rc.tool_path(self.build, tool)
                with open(path, "rb") as handle:
                    original = handle.read()
                with open(path, "wb") as handle:
                    handle.write(original + b" rebuilt")
                self.assert_resume_refused(f"{tool} at ")
                with open(path, "wb") as handle:
                    handle.write(original)
        self.assertEqual(self.invoke("--resume")[0], 0)  # the binaries as they were: it resumes

    def test_a_build_that_is_not_release_now(self):
        with open(os.path.join(self.build, "CMakeCache.txt"), "w", encoding="ascii") as handle:
            handle.write("CMAKE_BUILD_TYPE:STRING=Debug\n")
        self.assert_resume_refused("not Release")

    def test_a_run_that_was_not_a_release_run(self):
        text = self.read(rc.RUN_FILE).replace("build type: Release", "build type: Debug")
        with open(self.path(rc.RUN_FILE), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        self.assert_resume_refused("'Debug' build")

    def test_a_run_txt_without_what_a_resume_needs(self):
        for key in rc.RESUME_KEYS:
            with self.subTest(key=key):
                original = self.read(rc.RUN_FILE)
                kept = "".join(line + "\n" for line in original.splitlines() if not line.startswith(key + ": "))
                with open(self.path(rc.RUN_FILE), "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(kept)
                self.assert_resume_refused(f"has no '{key}' line")
                with open(self.path(rc.RUN_FILE), "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(original)

    def test_no_run_txt(self):
        os.remove(self.path(rc.RUN_FILE))
        self.assert_resume_refused("cannot read")

    def test_done_files_that_are_not_the_runners(self):
        cases = {"a.done": ("outcome: fine\nattempt 1 exit: 0\n", "is not a .done file"),
                 "b.done": ("outcome: ok\nattempt 1 exit: 1\n", "does not match"),
                 "stray.done": ("outcome: ok\nattempt 1 exit: 0\n", "no process of this plan")}
        self.restoring_each(cases)

    def test_attempt1_files_that_are_not_the_runners(self):
        cases = {"a.attempt1": ("garbage\n", "is not a .attempt1 file"),
                 "c.attempt1": ("attempt 1 exit: zero\n", "is not a .attempt1 file"),
                 "stray.attempt1": ("attempt 1 exit: 0\n", "no process of this plan"),
                 "b.attempt1": ("attempt 1 exit: 5\n", "says attempt 1 exited 5")}  # b.done says 0
        self.restoring_each(cases)

    def restoring_each(self, cases):
        for name, (text, needle) in cases.items():
            with self.subTest(name=name):
                original = self.snapshot().get(name)
                with open(self.path(name), "w") as handle:
                    handle.write(text)
                self.assert_resume_refused(needle)
                if original is None:
                    os.remove(self.path(name))
                else:
                    with open(self.path(name), "wb") as handle:
                        handle.write(original)

    def test_no_lab_job(self):
        self.assert_resume_refused("LAB_JOB", lab_job=None)

    def test_a_build_that_is_missing(self):
        self.assert_resume_refused("no build directory", build=os.path.join(self.root, "nowhere"))

    def test_no_output_directory_or_an_empty_one(self):
        elsewhere = os.path.join(self.root, "elsewhere")
        code, _, stderr = self.invoke("--resume", out=elsewhere)
        self.assertEqual(code, 2)
        self.assertIn("no output directory", stderr)
        self.assertFalse(os.path.exists(elsewhere))
        os.makedirs(elsewhere)
        code, _, stderr = self.invoke("--resume", out=elsewhere)
        self.assertEqual(code, 2)
        self.assertIn("cannot read", stderr)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_a_runner_that_cannot_pin_itself(self):
        with mock.patch.object(rc, "RUNNER_CPU", 10_000):
            self.assert_resume_refused("cannot pin the runner")

    def test_a_tool_that_is_still_running(self):
        self.write_holder_file("c.pid", self.sleeper())
        self.assert_resume_refused("is still running")

    def test_a_runner_that_is_still_running(self):
        self.write_holder_file(rc.LOCK_FILE, self.sleeper())
        self.assert_resume_refused("is still running")

    def test_a_live_pid_that_is_not_that_program_does_not_block(self):
        """A reused pid: the image name or the start time is not the recorded one."""
        pid = self.sleeper()
        for label, options in {"another image": dict(image="no-such-program.exe"),
                               "another start time": dict(created="1")}.items():
            with self.subTest(label):
                self.write_holder_file(rc.LOCK_FILE, pid, **options)
                self.write_holder_file("c.pid", pid, **options)
                code, stdout, stderr = self.invoke("--resume")
                self.assertEqual(code, 0, stdout + stderr)
                self.assertIn(" STALE-LOCK removed pid=", self.read(rc.LOG_FILE))
                self.assertEqual(self.read("c.done"), "outcome: ok\nattempt 1 exit: 0\n")
                self.assertFalse(os.path.exists(self.path("c.pid")), "a pid file whose tool is gone is removed")
                self.assertFalse(os.path.exists(self.path(rc.LOCK_FILE)))
                self.kill_during("c")  # for the next case

    def test_files_that_are_not_the_runners(self):
        for name in (rc.LOCK_FILE, "c.pid"):
            for text in ("", "garbage", "12\nimage\n", "0\nimage\nstart\n", "x\nimage\nstart\n",
                         "12\nimage\nstart\nmore\n", "12\r\nimage\r\nstart\r\n"):
                with self.subTest(name=name, text=text):
                    self.write(name, text)
                    self.assert_resume_refused("is not a file this runner wrote")
                    os.remove(self.path(name))

    def test_a_dead_runner_and_tool_leave_a_stale_lock_and_pid_file_that_a_resume_clears(self):
        dead = self.dead_pid()
        self.write_holder_file(rc.LOCK_FILE, dead, image="python", created="7")
        self.write_holder_file("c.pid", dead, image="wakeprobe", created="7")
        self.write_holder_file("a.pid", self.sleeper())  # a is done: its pid file is not looked at
        code, stdout, stderr = self.invoke("--resume")
        self.assertEqual(code, 0, stdout + stderr)
        events = self.log_events()
        self.assertIn(f"- STALE-LOCK removed pid={dead}", events)
        self.assertLess(events.index(f"- STALE-LOCK removed pid={dead}"),
                        next(i for i, e in enumerate(events) if e.startswith("- RESUME ")))
        self.assertFalse(os.path.exists(self.path("c.pid")))
        self.assertTrue(os.path.exists(self.path("a.pid")))
        self.assertFalse(os.path.exists(self.path(rc.LOCK_FILE)))

    def test_a_dry_run_only_warns_about_live_processes(self):
        self.write_holder_file(rc.LOCK_FILE, self.sleeper())
        self.write_holder_file("c.pid", self.sleeper())
        before = self.snapshot()
        code, stdout, stderr = self.invoke("--resume", dry=True, lab_job=None)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr.count("is still running"), 2)
        self.assertIn("dry run: 3 processes", stdout)
        self.assertEqual(self.snapshot(), before)


class LockTests(CampaignCase):
    def test_the_lock_is_held_during_the_run_and_removed_after_it(self):
        self.write_plan(make_process("a"))
        self.assertEqual(self.invoke()[0], 0)
        self.assertTrue(self.record("a.jsonl")["lock"])
        self.assertFalse(os.path.exists(self.path(rc.LOCK_FILE)))

    def test_the_lock_names_the_runner(self):
        os.makedirs(self.out)
        with rc.campaign_lock(self.out, None):
            holder = rc.read_holder(self.path(rc.LOCK_FILE))
            self.assertEqual(holder.pid, os.getpid())
            self.assertEqual((holder.image, holder.created), rc.process_identity(os.getpid()))
            self.assertTrue(holder.image and holder.created)
            with self.assertRaises(rc.Refusal), rc.campaign_lock(self.out, None):  # exclusive create
                pass
            self.assertTrue(os.path.exists(self.path(rc.LOCK_FILE)), "the failed attempt left the lock alone")
        self.assertFalse(os.path.exists(self.path(rc.LOCK_FILE)))

    def test_the_lock_is_removed_after_an_interrupt_and_after_an_internal_error(self):
        self.write_plan(make_process("a"), make_process("b"))
        for exc, code in ((KeyboardInterrupt, 130), (MemoryError, 3)):
            with self.subTest(exc=exc.__name__):
                out = os.path.join(self.root, "out " + exc.__name__)
                with mock.patch.object(rc, "execute", side_effect=exc):
                    got, _, _ = self.invoke(out=out)
                self.assertEqual(got, code)
                # ABORTED is logged for an interrupt only; the lock and the pid files are gone either way
                expected = [rc.LOG_FILE, rc.RUN_FILE] if exc is KeyboardInterrupt else [rc.RUN_FILE]
                self.assertEqual(sorted(os.listdir(out)), expected)

    def test_the_runner_gets_its_cpus_back_whatever_happens(self):
        self.write_plan(make_process("a"))
        for exc in (KeyboardInterrupt, MemoryError):
            with mock.patch.object(rc, "execute", side_effect=exc):
                self.invoke(out=os.path.join(self.root, "out " + exc.__name__))
            self.assertEqual(rc.current_affinity(), STARTUP_CPUS)

    def test_the_identity_of_a_process(self):
        image, created = rc.process_identity(os.getpid())
        self.assertTrue(image and created)
        self.assertIn("python", image.lower())
        self.assertIsNone(rc.process_identity(self.dead_pid()))
        sleeper = self.sleeper()
        self.assertEqual(rc.process_identity(sleeper)[1] != created, True)
        self.assertTrue(rc.same_program(("a", "1"), ("A", "1")))
        self.assertFalse(rc.same_program(("a", "1"), ("b", "1")))
        self.assertFalse(rc.same_program(("a", "1"), ("a", "2")))
        self.assertTrue(rc.same_program(("", ""), ("b", "2")), "nothing recorded: a live pid counts")
        self.assertTrue(rc.same_program(("a", "1"), ("", "")), "nothing to compare: a live pid counts")


class PlacementTests(CampaignCase):
    """Section 8: core 0 for the runner; a tool starts with the CPUs the runner had before."""

    def test_the_runner_pins_itself_and_a_tool_gets_the_original_cpus(self):
        original = sorted(STARTUP_CPUS)
        self.write_plan(make_process("a", extra=("--stub-runner-pid", str(os.getpid()))),
                        make_process("b", extra=("--stub-runner-pid", str(os.getpid()))))
        code, _, stderr = self.invoke()
        self.assertEqual(code, 0, stderr)
        for pid in "ab":
            record = self.record(pid + ".jsonl")
            self.assertEqual(record["runner_affinity"], [rc.RUNNER_CPU], "the runner is on its CPU")
            self.assertEqual(record["affinity"], original, "the tool has the CPUs the runner had before")
        self.assertEqual(sorted(rc.current_affinity()), original, "the runner is given its CPUs back")

    def test_a_runner_that_cannot_pin_itself_refuses_before_it_writes_anything(self):
        self.write_plan(make_process("a"))
        for cpu in (10_000, -1):
            with self.subTest(cpu=cpu), mock.patch.object(rc, "RUNNER_CPU", cpu):
                code, _, stderr = self.invoke()
                self.assert_refused(code, stderr, "cannot pin the runner")

    def test_a_cpu_that_is_not_the_runners_refuses(self):
        self.write_plan(make_process("a"))
        with mock.patch.object(rc, "current_affinity", return_value=frozenset({1000})):
            code, _, stderr = self.invoke()
        self.assert_refused(code, stderr, "not in the runner's CPUs")

    def test_the_placement_object(self):
        placement = rc.Placement()
        self.assertEqual(placement.original, STARTUP_CPUS)
        placement.restore()  # not pinned: nothing happens
        placement.pin()
        self.assertEqual(rc.current_affinity(), frozenset({rc.RUNNER_CPU}))
        with placement.for_child():
            if sys.platform == "win32":
                self.assertEqual(rc.current_affinity(), STARTUP_CPUS, "widened for the start")
        self.assertEqual(rc.current_affinity(), frozenset({rc.RUNNER_CPU}), "narrowed again")
        placement.restore()
        self.assertEqual(rc.current_affinity(), STARTUP_CPUS)


@unittest.skipUnless(sys.platform == "win32", "Windows API structures")
class WindowsApiTests(unittest.TestCase):
    def test_the_job_structure_has_the_size_windows_expects(self):
        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION: 144 bytes on x64, 112 on x86.
        self.assertEqual(ctypes.sizeof(rc._JobExtendedLimits), 144 if sys.maxsize > 2**32 else 112)
        self.assertEqual(ctypes.sizeof(rc._JobBasicLimits), 64 if sys.maxsize > 2**32 else 48)

    def test_a_job_kills_what_is_in_it_when_it_closes(self):
        proc = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(120)"],
                                creationflags=rc.CREATE_SUSPENDED)
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        job = rc.ToolJob()
        job.adopt(proc.pid)
        time.sleep(0.2)
        self.assertTrue(process_is_running(proc.pid))
        job.close()
        self.assertEqual(proc.wait(timeout=20) is not None, True)
        self.assertFalse(process_is_running(proc.pid))


class StopTests(CampaignCase):
    def test_a_stop_finishes_the_process_and_its_rerun_then_resume_continues(self):
        pause = ("--stub-pause", "1.2")  # longer than the 1 s budget
        self.write_plan(make_process("a", codes="1,0", extra=pause), make_process("b", codes="7,7", extra=pause),
                        make_process("c"))
        code, stdout, stderr = self.invoke("--stop-after-s", "1")
        self.assertEqual(code, 4, stderr)
        self.assertIn("continue with --resume", stdout)
        self.assertIn("stopped: 2 of 3 processes not started", stdout)
        self.assertNotIn("incomplete processes:", stdout)
        rows = self.log()
        self.assertEqual([(r[1], r[2], r[3]) for r in rows[:2]], [("a", "1", "1"), ("a", "2", "0")],
                         "the budget ran out in a's first run; its rerun still ran")
        self.assertEqual(rows[-1][1:], ("-", "STOP", "stop-after-s=1", "remaining=2"))
        self.assertEqual(self.read("a.done"), "outcome: ok after rerun\nattempt 1 exit: 1\nattempt 2 exit: 0\n")
        self.assertFalse(os.path.exists(self.path("b.out")), "no new process starts after the budget")
        self.assertFalse(os.path.exists(self.path(rc.LOCK_FILE)), "a stop releases the lock")
        # a second stop, with an INCOMPLETE process: exit 4 wins over exit 1
        code, stdout, stderr = self.invoke("--resume", "--stop-after-s", "1")
        self.assertEqual(code, 4, stderr)
        self.assertIn("stopped: 1 of 3 processes not started; incomplete so far: 1", stdout)
        self.assertEqual(self.log()[-1][1:], ("-", "STOP", "stop-after-s=1", "remaining=1"))
        before = self.snapshot()
        code, stdout, stderr = self.invoke("--resume")
        self.assertEqual(code, 1, stderr)
        self.assertIn("incomplete processes: 1", stdout)
        self.assertIn("resuming 3 processes", stdout)
        after = self.snapshot()
        for name in before:
            if name not in (rc.RUN_FILE, rc.LOG_FILE):
                self.assertEqual(after[name], before[name], name)
        self.assertEqual(self.read("c.done"), "outcome: ok\nattempt 1 exit: 0\n")
        self.assertEqual(self.read(rc.RUN_FILE).count("resumed: "), 2)

    def test_a_budget_that_is_not_reached_changes_nothing(self):
        self.write_plan(make_process("a"), make_process("b"))
        code, stdout, stderr = self.invoke("--stop-after-s", "3600")
        self.assertEqual(code, 0, stderr)
        self.assertIn("incomplete processes: 0", stdout)
        self.assertNotIn("STOP", self.read(rc.LOG_FILE))

    def test_a_budget_that_is_gone_before_the_first_process_starts_nothing(self):
        self.write_plan(make_process("a"), make_process("b"))
        ticks = iter([0.0, 5.0, 5.0, 5.0])  # run() begins at 0; the first check sees 5 s
        with mock.patch.object(rc, "time", mock.Mock(monotonic=lambda: next(ticks))):
            code, stdout, _ = self.invoke("--stop-after-s", "1")
        self.assertEqual(code, 4)
        self.assertIn("stopped: 2 of 2 processes not started", stdout)
        self.assertFalse(os.path.exists(self.path("a.out")))

    def test_the_budget_is_a_whole_number_of_at_least_one(self):
        self.write_plan(make_process("a"))
        for value in ("0", "-3", "x", "1.5", ""):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                code = rc.main(["--plan", self.plan_path, "--build-dir", self.build, "--out-dir", self.out,
                                "--stop-after-s", value, "--dry-run"])
                self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(self.out))


class KillTests(CampaignCase):
    """The runner as a program, killed while a stub tool runs (finding: an orphaned tool). The driver
    script patches launch_prefix, so the same tests run on Windows (TerminateProcess) and on POSIX
    (SIGKILL)."""

    def setUp(self):
        super().setUp()
        self.driver = os.path.join(self.root, "driver.py")
        with open(self.driver, "w", encoding="ascii") as handle:
            handle.write(DRIVER)

    def start_runner(self, *extra):
        env = dict(os.environ, LAB_JOB="1")
        argv = [sys.executable, "-I", "-B", self.driver, HERE, self.stub, "--plan", self.plan_path,
                "--build-dir", self.build, "--out-dir", self.out, *extra]
        log = open(os.path.join(self.root, "runner.log"), "wb")
        self.addCleanup(log.close)
        runner = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: (runner.poll() is None and runner.kill(), runner.wait()))
        return runner

    def kill_the_runner_and_wait_for_its_tool_to_die(self, runner, tool_pid):
        runner.kill()  # SIGKILL, or TerminateProcess: nothing in the runner gets to run
        runner.wait(timeout=30)
        self.wait_until(lambda: not process_is_running(tool_pid), "the tool outlived its runner")

    def test_a_killed_runner_leaves_no_tool_and_the_resume_works(self):
        marker = os.path.join(self.root, "started")
        self.write_plan(make_process("a", extra=("--stub-marker", marker, "--stub-sleep", "120")), make_process("b"))
        runner = self.start_runner()
        self.wait_until(lambda: os.path.exists(marker) and os.path.exists(self.path("a.pid")), "no tool started")
        tool_pid = self.record("a.jsonl")["pid"]
        holder = rc.read_holder(self.path("a.pid"))
        self.assertEqual(holder.pid, tool_pid, "a.pid names the tool")
        self.assertTrue(holder.image and holder.created)
        self.assertTrue(process_is_running(tool_pid))
        self.kill_the_runner_and_wait_for_its_tool_to_die(runner, tool_pid)
        self.assertTrue(os.path.exists(self.path(rc.LOCK_FILE)), "a killed runner leaves its lock")
        self.assertFalse(os.path.exists(self.path("a.done")))
        before = self.snapshot()
        code, stdout, stderr = self.invoke("--resume")
        self.assertEqual(code, 0, stdout + stderr)
        after = self.snapshot()
        for ext in (".jsonl", ".out", ".err"):
            self.assertEqual(after["a.interrupted-1" + ext], before["a" + ext], ext)
        self.assertEqual(self.read("a.done"), "outcome: ok\nattempt 1 exit: 0\n")
        self.assertEqual(self.read("b.done"), "outcome: ok\nattempt 1 exit: 0\n")
        self.assertNotEqual(self.record("a.jsonl")["pid"], tool_pid)
        self.assertIn(f"- STALE-LOCK removed pid={runner.pid}", self.log_events())
        for name in (rc.LOCK_FILE, "a.pid", "b.pid"):
            self.assertFalse(os.path.exists(self.path(name)), name)

    def test_a_kill_during_a_rerun_continues_at_attempt_2(self):
        marker = os.path.join(self.root, "started")
        self.write_plan(make_process("a", codes="1,0", extra=("--stub-marker", marker, "--stub-sleep", "120",
                                                              "--stub-only-attempt", "2")))
        runner = self.start_runner()
        self.wait_until(lambda: os.path.exists(marker) and os.path.exists(self.path("a.pid")), "no rerun started")
        self.assertEqual(self.read("a.attempt1"), "attempt 1 exit: 1\n")
        first_run = {ext: self.snapshot()["a" + ext] for ext in (".jsonl", ".out", ".err")}
        rerun_pid = self.record("a-rerun.jsonl")["pid"]
        self.kill_the_runner_and_wait_for_its_tool_to_die(runner, rerun_pid)
        self.assertFalse(os.path.exists(self.path("a.done")))
        code, stdout, stderr = self.invoke("--resume")
        self.assertEqual(code, 0, stdout + stderr)
        after = self.snapshot()
        for ext, data in first_run.items():
            self.assertEqual(after["a" + ext], data, "attempt 1 is not run again")
        self.assertEqual(self.read("a.done"), "outcome: ok after rerun\nattempt 1 exit: 1\nattempt 2 exit: 0\n")
        self.assertEqual(self.record("a-rerun.interrupted-1.jsonl")["pid"], rerun_pid)
        self.assertNotEqual(self.record("a-rerun.jsonl")["pid"], rerun_pid)
        attempts = [row[2] for row in self.log() if row[1] == "a" and row[2] in ("1", "2")]
        self.assertEqual(attempts, ["1", "2"], "one attempt 1 and one attempt 2: the kill used up nothing")

    def test_a_runner_that_is_killed_while_a_tool_lives_is_not_resumed_beside_it(self):
        """With the guard gone (a tool that is still there), a resume refuses and changes nothing."""
        marker = os.path.join(self.root, "started")
        self.write_plan(make_process("a", extra=("--stub-marker", marker, "--stub-sleep", "120")))
        runner = self.start_runner()
        self.wait_until(lambda: os.path.exists(marker) and os.path.exists(self.path("a.pid")), "no tool started")
        tool_pid = self.record("a.jsonl")["pid"]
        self.kill_the_runner_and_wait_for_its_tool_to_die(runner, tool_pid)
        # Put a live process under the dead tool's pid file, as a tool that escaped the guard would be.
        sleeper = self.sleeper()
        self.write_holder_file("a.pid", sleeper)
        before = self.snapshot()
        code, _, stderr = self.invoke("--resume")
        self.assertEqual(code, 2, stderr)
        self.assertIn("is still running", stderr)
        self.assertEqual(self.snapshot(), before)


class ProgramTests(CampaignCase):
    """The runner started as a program, as the lab runner starts it."""

    def run_program(self, *args, lab_job=None):
        env = {k: v for k, v in os.environ.items() if k != "LAB_JOB"}
        if lab_job:
            env["LAB_JOB"] = lab_job
        return subprocess.run([sys.executable, "-B", RUNNER, *args], capture_output=True, text=True, env=env,
                              timeout=120, check=False)

    def common(self, *extra):
        return ["--plan", self.plan_path, "--build-dir", self.build, "--out-dir", self.out, *extra]

    def install_shell_stubs(self):
        for tool in rc.TOOLS:
            path = rc.tool_path(self.build, tool)
            with open(path, "w", encoding="ascii") as handle:
                handle.write(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(self.stub)} \"$0\" \"$@\"\n")
            os.chmod(path, 0o755)

    def test_exit_codes_of_refusals(self):
        self.write_plan(make_process("a"))
        done = self.run_program(*self.common())  # no LAB_JOB
        self.assertEqual(done.returncode, 2)
        self.assertIn("LAB_JOB", done.stderr)
        self.assertEqual(self.run_program().returncode, 2)  # no arguments
        self.write_plan(make_process("a"), system=OTHER_SYSTEM[rc.host_system()])
        done = self.run_program(*self.common(), lab_job="1")  # a plan for the other system
        self.assertEqual(done.returncode, 2)
        self.assertIn("the plan is for", done.stderr)
        self.assertFalse(os.path.exists(self.out))
        self.write_plan(make_process("a"), by="analysis/v2/plan.py draft")
        done = self.run_program(*self.common(), lab_job="1")  # a draft plan
        self.assertEqual(done.returncode, 2)
        self.assertIn("generated_by", done.stderr)
        self.assertFalse(os.path.exists(self.out))
        os.makedirs(self.out)
        with open(self.path("old.jsonl"), "w") as handle:
            handle.write("{}" + os.linesep)
        done = self.run_program(*self.common("--dry-run"))  # records in the output directory
        self.assertEqual(done.returncode, 2)
        self.assertIn("already holds records", done.stderr)

    def test_dry_run_of_the_sample_plan(self):
        with open(SAMPLE_PLAN, "rb") as handle:
            expected = len(handle.read().splitlines()) - 1
        done = self.run_program("--plan", SAMPLE_PLAN, "--build-dir", self.build, "--out-dir", self.out,
                                "--dry-run", "--expect-processes", str(expected))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len([l for l in done.stdout.splitlines() if re.match(r"\d+: ", l)]), expected)
        self.assertFalse(os.path.exists(self.out))

    @unittest.skipIf(sys.platform == "win32", "needs shell-script stubs")
    def test_end_to_end(self):
        self.install_shell_stubs()
        self.write_plan(make_process("a", codes="1,0"), make_process("b", codes="7,7"), make_process("c"))
        done = self.run_program(*self.common(), lab_job="job-1")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("incomplete processes: 1", done.stdout)
        self.assertEqual(self.record("a-rerun.jsonl")["tool"], "wakeprobe")
        self.assertEqual(self.record("c.jsonl")["argv"], make_process("c")["args"] + ["--out", self.path("c.jsonl")])
        self.assertEqual(self.run_txt()["lab job"], "job-1")
        self.assertTrue(self.record("a.jsonl")["lock"])
        self.assertFalse(os.path.exists(self.path(rc.LOCK_FILE)))

    def terminate_when_started(self, marker):
        """Start the runner on the plan and send it SIGTERM once a stub has created the marker."""
        env = {k: v for k, v in os.environ.items()}
        env["LAB_JOB"] = "1"
        runner = subprocess.Popen([sys.executable, "-B", RUNNER, *self.common()], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 30
            while not os.path.exists(marker):
                self.assertLess(time.monotonic(), deadline, "the stub did not start")
                time.sleep(0.05)
            runner.terminate()
            self.assertEqual(runner.wait(timeout=30), 130)
        finally:
            if runner.poll() is None:
                runner.kill()

    @unittest.skipIf(sys.platform == "win32", "SIGTERM is a POSIX signal")
    def test_resume_after_sigterm(self):
        self.install_shell_stubs()
        marker = os.path.join(self.root, "started")
        self.write_plan(make_process("a"), make_process("b", extra=("--stub-marker", marker, "--stub-sleep", "60")),
                        make_process("c"))
        self.terminate_when_started(marker)
        self.assertTrue(os.path.exists(self.path("a.done")))
        self.assertFalse(os.path.exists(self.path("b.done")), "an interrupted process gets no .done file")
        self.assertFalse(os.path.exists(self.path("c.jsonl")))
        self.assertFalse(os.path.exists(self.path(rc.LOCK_FILE)), "SIGTERM is an interrupt: the lock is released")
        self.assertFalse(os.path.exists(self.path("b.pid")))
        before = self.snapshot()
        done = self.run_program(*self.common("--resume"), lab_job="job-2")
        self.assertEqual(done.returncode, 0, done.stderr)
        after = self.snapshot()
        for name in ("a.jsonl", "a.out", "a.err", "a.done"):
            self.assertEqual(after[name], before[name], name)
        for ext in (".jsonl", ".out", ".err"):
            self.assertEqual(after["b.interrupted-1" + ext], before["b" + ext], ext)
        for pid in "bc":
            self.assertEqual(self.read(pid + ".done"), "outcome: ok\nattempt 1 exit: 0\n")
        self.assertRegex(self.read(rc.RUN_FILE).splitlines()[-1], r"^resumed: \S+; lab job job-2; wakeprobe sha256 ")
        self.assertEqual(self.run_txt()["lab job"], "1", "the first block of run.txt is not changed")

    @unittest.skipIf(sys.platform == "win32", "SIGTERM is a POSIX signal")
    def test_sigterm_stops_the_runner_and_kills_the_tool(self):
        self.install_shell_stubs()
        marker = os.path.join(self.root, "started")
        self.write_plan(make_process("a", extra=("--stub-marker", marker, "--stub-sleep", "60")), make_process("b"))
        self.terminate_when_started(marker)
        pid = self.record("a.jsonl")["pid"]
        deadline = time.monotonic() + 10
        while process_is_running(pid):
            self.assertLess(time.monotonic(), deadline, "the tool is still running")
            time.sleep(0.05)
        self.assertEqual(self.read(rc.LOG_FILE).splitlines()[-1].split(" ")[1:], ["a", "ABORTED"])
        self.assertFalse(os.path.exists(self.path("b.jsonl")))
        self.assertFalse(os.path.exists(self.path("a.done")), "an interrupted process gets no .done file")
        self.assertFalse(os.path.exists(self.path("a.attempt1")), "an interrupted run is void: no marker")


if __name__ == "__main__":
    unittest.main(verbosity=2)

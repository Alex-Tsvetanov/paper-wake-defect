"""Reading plans and the campaign's outputs, and what makes a process good (hypotheses-v2.md,
sections 1 and 8; the runner's contracts C3 and C5).

Interface with the campaign runner (bench/run_campaign.py). A process with plan id ID leaves
these files in the system's output folder:
  ID.jsonl                      the first run's output (attempt 1)
  ID-rerun.jsonl                the rerun's output (attempt 2), if the runner made one
  ID.done                       the outcome and the exit code of each attempt (parse_done)
  ID.interrupted-K.jsonl        a void run: the runner was killed in it. Its files were set aside
  ID-rerun.interrupted-K.jsonl  by --resume. A void run is never a failed run.
The .attempt1 marker is the runner's own; it is not read here.

A process is good only if its .done file exists and is the runner's, the run whose data are used
exited 0 (attempt 1 if it did, else attempt 2 if it did), and that run's output passes every
check of wakeprobe_problem or abpair_problem. Void runs are listed per process; their lost units
are counted and reported, and their data are never used. A failed first run is reported with the
rerun (section 8: "both runs are reported").
"""

import json
import os
import re

import grid

FIRST_SUFFIX = ".jsonl"
RERUN_SUFFIX = "-rerun.jsonl"  # the runner's name for a rerun's output
DONE_SUFFIX = ".done"

PLAN_KEYS = ("id", "tool", "args", "cell", "family", "limit_s")  # C1: no other key
TOOLS = ("wakeprobe", "abpair")

_PROCESS_INDEX = re.compile(r"-[pk](\d+)$")
_VOID_FILE = re.compile(r"^(?P<stem>.+)\.interrupted-(?P<k>[0-9]+)\.jsonl$")
_EXIT = re.compile(r"0|-?[1-9][0-9]*|timeout")
TIMEOUT = "timeout"
OUTCOMES = ("ok", "ok after rerun", "incomplete")

RELEASE = "Release"  # section 1: every build is the Release build
ABPAIR_PROGRAM = "abpair"  # what abpair's summary calls itself
ABPAIR_MODE = "timed"  # H4 runs timed windows (--window-ms)

# Every summary and record field read below, so that a test can check that the tools emit it
# under exactly that name (bench/wakeprobe.cpp, bench/abpair.cpp).
WAKEPROBE_SUMMARY_FIELDS = (
    "summary", "design", "label", "backend", "wait_us", "gap_frac_range", "gap_seed", "gap_unit", "phase_frac",
    "protocol", "posts", "warmup", "calibrate", "lost", "b_eff_ns", "timerslack_set_rc", "timerslack_ns_worker",
    "timer_period_ms_requested", "timer_resolution_100ns_start", "timer_resolution_100ns_gap",
    "timer_resolution_100ns_end", "variant", "workers", "worker_cpus", "worker_cpus_seen", "poster_cpu",
    "poster_cpu_seen", "coarsest_timer_us", "build_type", "bench_commit", "bench_dirty", "error")
WAKEPROBE_RECORD_FIELDS = ("index", "kind", "delay_ns", "post_ns", "exec_ns", "timed_out", "gate_timed_out",
                           "residual_ns")
WAKEPROBE_KINDS = ("probe", "calib", "warmup", "measured")
ABPAIR_SUMMARY_FIELDS = (
    "summary", "program", "check", "backend", "arm_a", "arm_b", "workers", "producers", "inflight", "pairs", "order",
    "mode", "window_ms", "work_ns", "worker_cpus", "producer_cpus", "worker_cpus_seen", "producer_cpus_seen",
    "windows", "build_type", "bench_commit", "bench_dirty", "error")
ABPAIR_WINDOW_FIELDS = ("pair", "position", "side", "arm", "ran", "tasks", "elapsed_ns")


def load_plan(path):
    """Returns (header, entries). Raises ValueError on a malformed plan."""
    with open(path, "r", encoding="utf-8") as f:
        lines = [line for line in f.read().splitlines() if line.strip()]
    if not lines:
        raise ValueError("%s: empty plan" % path)
    header = json.loads(lines[0])
    if header.get("plan") != 1 or "system" not in header:
        raise ValueError("%s: line 1 is not a plan header" % path)
    entries = [json.loads(line) for line in lines[1:]]
    for e in entries:
        if not isinstance(e, dict) or set(e) != set(PLAN_KEYS):
            raise ValueError("%s: a process line must hold exactly %s, not %s" % (
                path, list(PLAN_KEYS), sorted(e) if isinstance(e, dict) else e))
        if e["tool"] not in TOOLS or not isinstance(e["args"], list):
            raise ValueError("%s: %s has an unknown tool or arguments that are not a list" % (path, e["id"]))
        if type(e["limit_s"]) is not int or e["limit_s"] <= 0:
            raise ValueError("%s: %s: limit_s is %r, not a positive integer" % (path, e["id"], e["limit_s"]))
    ids = [e["id"] for e in entries]
    if len(set(ids)) != len(ids):
        raise ValueError("%s: duplicate ids" % path)
    return header, entries


def process_index(entry_id):
    """The process number at the end of an id (-p03 or -k07)."""
    m = _PROCESS_INDEX.search(entry_id)
    if not m:
        raise ValueError("no process number in id %r" % entry_id)
    return int(m.group(1))


def arg_value(args, flag, default=None):
    """The value after a flag in an argument list."""
    for i, a in enumerate(args[:-1]):
        if a == flag:
            return args[i + 1]
    return default


class Run:
    """One output file: its record lines, its summary line, and lines that did not parse."""

    def __init__(self, path):
        self.path = path
        self.records = []
        self.summary = None
        self.bad_lines = 0
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    self.bad_lines += 1  # a process killed while it wrote
                    continue
                if not isinstance(obj, dict):
                    self.bad_lines += 1
                elif obj.get("summary") is True:
                    self.summary = obj
                else:
                    self.records.append(obj)

    def lost_units(self):
        """Units never delivered (exec_ns null), of any kind. A line without a kind (an abpair window) is no unit."""
        return sum(1 for r in self.records if "kind" in r and r.get("exec_ns") is None)

    def lost_in_all(self):
        """The most lost units either count gives: the records' and the summary's own."""
        said = self.summary.get("lost") if self.summary else 0
        return max(self.lost_units(), said if type(said) is int else 0)

    def measured(self):
        """Measured units in run order."""
        return sorted((r for r in self.records if r.get("kind") == "measured"), key=lambda r: r.get("index", 0))


# ---------------------------------------------------------------------------------------------
# The output folder and the .done file
# ---------------------------------------------------------------------------------------------


def parse_done(text):
    """Read a .done file strictly: (outcome, exits). Raises ValueError, with the reason, if the runner did not write it.

    outcome: ok | ok after rerun | incomplete
    attempt 1 exit: <int> | timeout
    attempt 2 exit: <int> | timeout      (only when attempt 1 failed)
    Each line ends in LF; there is nothing else. A timeout is a failed run.
    """
    if not text.endswith("\n") or "\r" in text:
        raise ValueError("lines must end in LF and the file in a line end")
    lines = text[:-1].split("\n")
    outcome = lines[0][len("outcome: "):] if lines[0].startswith("outcome: ") else None
    if outcome not in OUTCOMES:
        raise ValueError("line 1 is not 'outcome: ok | ok after rerun | incomplete'")
    exits = []
    for n, line in enumerate(lines[1:], 1):
        prefix = "attempt %d exit: " % n
        if n > 2 or not line.startswith(prefix) or not _EXIT.fullmatch(line[len(prefix):]):
            raise ValueError("line %d is not '%s<int>|timeout'" % (n + 1, prefix))
        value = line[len(prefix):]
        exits.append(value if value == TIMEOUT else int(value))
    if not exits:
        raise ValueError("no 'attempt 1 exit' line")
    if len(exits) != (1 if exits[0] == 0 else 2):
        raise ValueError("attempt 2 must follow a failed attempt 1, and only that")
    want = "ok" if exits[0] == 0 else ("ok after rerun" if exits[1] == 0 else "incomplete")
    if outcome != want:
        raise ValueError("the outcome %r does not match the exits" % outcome)
    return outcome, tuple(exits)


def used_attempt(exits):
    """C5: attempt 1 if it exited 0, else attempt 2 if it exited 0, else None."""
    if exits[0] == 0:
        return 1
    return 2 if len(exits) == 2 and exits[1] == 0 else None


class DataDir:
    """The files of one system's output folder, listed once."""

    def __init__(self, path):
        self.path = path
        try:
            names = os.listdir(path)
        except OSError:
            names = []
        self.names = frozenset(names)
        self._void = {}
        for name in sorted(names):
            m = _VOID_FILE.match(name)
            if m:
                self._void.setdefault(m.group("stem"), []).append(name)

    def load(self, name):
        return Run(os.path.join(self.path, name)) if name in self.names else None

    def void_names(self, entry_id):
        """The void runs of a process: <id>.interrupted-<k>.jsonl and <id>-rerun.interrupted-<k>.jsonl."""
        return self._void.get(entry_id, []) + self._void.get(entry_id + "-rerun", [])

    def read_done(self, name):
        """(outcome, exits) of a .done file, or (None, reason)."""
        if name not in self.names:
            return None, "no .done file"
        try:
            with open(os.path.join(self.path, name), "rb") as f:
                text = f.read().decode("ascii")
            return parse_done(text), None
        except (OSError, UnicodeDecodeError, ValueError) as e:
            return None, "malformed .done file: %s" % e


# ---------------------------------------------------------------------------------------------
# What makes a process good
# ---------------------------------------------------------------------------------------------


def _same(got, want):
    """Equal, and not a JSON boolean standing in for a number."""
    return got == want and (type(got) is not bool or type(want) is bool)


def _cpu_list(text):
    return None if text is None else [int(x) for x in text.split(",")]


def _mismatch(s, want):
    """The first summary field that differs from the plan's value, as text; else None."""
    for field, value in want.items():
        if not _same(s.get(field), value):
            return "summary %s is %r, the plan asked for %r" % (field, s.get(field), value)
    return None


def _build_problem(s):
    """Section 1: the Release build, from a clean tree."""
    if s.get("build_type") != RELEASE:
        return "build_type is %r, not %s" % (s.get("build_type"), RELEASE)
    if s.get("bench_dirty") is not False:
        return "bench_dirty is %r, not false" % (s.get("bench_dirty"),)
    return None


def _timer_problem(s):
    """Section 1 on Windows: the default timer resolution, read at the start, in the gaps and at the end.

    The default is the coarsest the system supports (coarsest_timer_us, which wakeprobe takes as
    ceil(NtQueryTimerResolution's coarsest / 10)): a finer reading means some process asked for
    more, and the run is not at the default.
    """
    coarsest = s.get("coarsest_timer_us")
    if type(coarsest) is not int or coarsest <= 0:
        return "coarsest_timer_us is %r: the default timer resolution cannot be read" % (coarsest,)
    if not _same(s.get("timer_period_ms_requested"), 0):
        return "timer_period_ms_requested is %r, not 0" % (s.get("timer_period_ms_requested"),)
    readings = [("start", s.get("timer_resolution_100ns_start"))]
    gaps = s.get("timer_resolution_100ns_gap")
    if not isinstance(gaps, list):
        return "timer_resolution_100ns_gap is %r, not a list" % (gaps,)
    readings += [("gap", g) for g in gaps] + [("end", s.get("timer_resolution_100ns_end"))]
    for where, v in readings:
        if type(v) is not int or (v + 9) // 10 != coarsest:
            return "timer resolution at the %s is %r (100 ns), not the default of %d us" % (where, v, coarsest)
    return None


def _slack_problem(s):
    """Section 1 on Linux: timer slack 1 ns, set without error and seen on the worker."""
    if not _same(s.get("timerslack_set_rc"), 0):
        return "timerslack_set_rc is %r, not 0" % (s.get("timerslack_set_rc"),)
    if not _same(s.get("timerslack_ns_worker"), 1):
        return "timerslack_ns_worker is %r, not 1" % (s.get("timerslack_ns_worker"),)
    return None


def wakeprobe_problem(run, entry, system):
    """None when the wakeprobe run is complete and is the run the plan asked for; else why not."""
    if run is None:
        return "no output"
    s = run.summary
    if s is None:
        return "no summary line"
    if s.get("error"):
        return "error: %s" % s["error"]
    args = entry["args"]
    posts = int(arg_value(args, "--posts", "51"))
    warmup = int(arg_value(args, "--warmup", "5"))
    variant = arg_value(args, "--variant", "none")
    workers = int(arg_value(args, "--workers", "1"))
    gap_range = arg_value(args, "--gap-frac-range")
    seed = arg_value(args, "--gap-seed")
    phase = arg_value(args, "--phase-frac")
    poster = arg_value(args, "--poster-cpu")
    worker_cpus = _cpu_list(arg_value(args, "--worker-cpus"))
    want = {
        "design": arg_value(args, "--design", "adhoc"), "label": entry["id"],
        "backend": arg_value(args, "--backend"), "variant": variant, "workers": workers,
        "wait_us": int(arg_value(args, "--wait-us")), "protocol": arg_value(args, "--protocol", "single"),
        "posts": posts, "warmup": warmup, "calibrate": int(arg_value(args, "--calibrate", "11")),
        "gap_seed": None if seed is None else int(seed),
        "gap_frac_range": None if gap_range is None else [float(x) for x in gap_range.split(":")],
        "gap_unit": None if gap_range is None else arg_value(args, "--gap-unit", "bound"),
        "phase_frac": None if phase is None else float(phase),
        "worker_cpus": worker_cpus, "poster_cpu": None if poster is None else int(poster),
    }
    # Section 8: a pinned thread seen on another CPU fails the process. wakeprobe says so itself and
    # exits non-zero; its summary's readings are checked here again.
    if worker_cpus is not None:
        want["worker_cpus_seen"] = worker_cpus
    if poster is not None:
        want["poster_cpu_seen"] = int(poster)
    problem = _mismatch(s, want) or _build_problem(s) or (_slack_problem(s) if system == "linux" else _timer_problem(s))
    if problem:
        return problem
    # The units: one probe, the calibration posts only where the variant delays single posts
    # (wakeprobe's own rule), the warm-up and the measured units, every one delivered.
    if run.lost_in_all():
        return "lost unit"
    calibrated = want["calibrate"] if grid.delays_single_posts(variant, workers) else 0
    kinds = {k: sum(1 for r in run.records if r.get("kind") == k) for k in WAKEPROBE_KINDS}
    planned = {"probe": 1, "calib": calibrated, "warmup": warmup, "measured": posts}
    if kinds != planned:
        return "units by kind are %s, the plan asked for %s" % (kinds, planned)
    delivered = [r for r in run.measured() if r.get("delay_ns") is not None]
    if len(delivered) != posts:
        return "%d of %d measured units delivered" % (len(delivered), posts)
    return None


def abpair_problem(run, entry, system):
    """None when the abpair run is complete and is the run the plan asked for; else why not."""
    if run is None:
        return "no output"
    s = run.summary
    if s is None:
        return "no summary line"
    if s.get("error"):
        return "error: %s" % s["error"]
    args = entry["args"]
    arms = {"A": arg_value(args, "--arm-a"), "B": arg_value(args, "--arm-b")}
    pairs = int(arg_value(args, "--pairs"))
    worker_cpus = _cpu_list(arg_value(args, "--worker-cpus"))
    producer_cpus = _cpu_list(arg_value(args, "--producer-cpus"))
    want = {
        "program": ABPAIR_PROGRAM, "check": False, "backend": arg_value(args, "--backend"),
        "arm_a": arms["A"], "arm_b": arms["B"], "workers": int(arg_value(args, "--workers")),
        "producers": int(arg_value(args, "--producers")), "inflight": int(arg_value(args, "--inflight")),
        "pairs": pairs, "order": arg_value(args, "--order"), "mode": ABPAIR_MODE,
        "window_ms": int(arg_value(args, "--window-ms")), "work_ns": int(arg_value(args, "--work-ns")),
        "worker_cpus": worker_cpus, "producer_cpus": producer_cpus,
        # C4: where each pinned thread ended, in the order of the planned CPUs.
        "worker_cpus_seen": worker_cpus, "producer_cpus_seen": producer_cpus,
        "windows": 2 * pairs,
    }
    problem = _mismatch(s, want) or _build_problem(s)
    if problem:
        return problem
    windows = run.records
    if len(windows) != 2 * pairs:
        return "%d of %d windows" % (len(windows), 2 * pairs)
    seen = set()
    for w in windows:
        side = w.get("side")
        if side not in arms or w.get("arm") != arms[side] or w.get("ran", arms[side]) != arms[side]:
            return "a window's side or arm differs from the plan"
        for field in ("elapsed_ns", "tasks"):
            if type(w.get(field)) is not int or w[field] <= 0:
                return "a window without a positive %s" % field
        # abba: pair k runs A then B when k is even, B then A when it is odd.
        pair, position = w.get("pair"), w.get("position")
        if type(pair) is not int or position not in (0, 1) or (side == "B") != ((position == 1) != (pair % 2 == 1)):
            return "a window is not in the planned order"
        seen.add((pair, position))
    if len(seen) != 2 * pairs:
        return "a pair lacks a window"
    return None


def throughput(window):
    """Tasks per second of one abpair window, from its exact tasks and elapsed_ns."""
    return window["tasks"] * 1e9 / window["elapsed_ns"]


def pair_ratios(run):
    """Per pair, in pair order: side A's throughput over side B's, as abpair names the sides."""
    by_pair = {}
    for w in run.records:
        by_pair.setdefault(w["pair"], {})[w["side"]] = throughput(w)
    return [by_pair[k]["A"] / by_pair[k]["B"] for k in sorted(by_pair)]


# ---------------------------------------------------------------------------------------------
# The rerun rule
# ---------------------------------------------------------------------------------------------


class VoidRun:
    """A run whose data never count: one the runner was killed in, or one with no valid .done file."""

    def __init__(self, name, run, reason):
        self.name = name
        self.run = run
        self.reason = reason

    def lost_units(self):
        return self.run.lost_in_all() if self.run else 0


class Resolved:
    """A plan entry's runs. `run` is the one whose data count, if the process is good (C5)."""

    def __init__(self, entry, data_dir, system):
        d = data_dir if isinstance(data_dir, DataDir) else DataDir(data_dir)
        self.entry = entry
        self.id = entry["id"]
        self.system = system
        check = wakeprobe_problem if entry["tool"] == "wakeprobe" else abpair_problem
        first_name, rerun_name = self.id + FIRST_SUFFIX, self.id + RERUN_SUFFIX
        first, rerun = d.load(first_name), d.load(rerun_name)
        done, done_problem = d.read_done(self.id + DONE_SUFFIX)
        self.void = [VoidRun(n, d.load(n), "interrupted") for n in d.void_names(self.id)]
        self.outcome, self.exits = done if done else (None, ())
        # Without a valid .done file the process never ended: its files are not the data of a run.
        if done is None:
            self.void += [VoidRun(n, r, "no valid .done file") for n, r in ((first_name, first), (rerun_name, rerun))
                          if r is not None]
            first = rerun = None
        self.first, self.rerun = first, rerun
        self.done_problem = done_problem
        runs = {1: first, 2: rerun}
        self.attempt = used_attempt(self.exits) if done else None
        self._attempt_problem = {n: self._problem_of(n, runs[n], check) for n in (1, 2)}
        self.first_problem = self._attempt_problem[1] if done else done_problem
        self.rerun_problem = self._attempt_problem[2]
        if done is None:
            self.problem = done_problem
        elif self.attempt is None:
            self.problem = "no run exited 0 (outcome: %s)" % self.outcome
        else:
            self.problem = self._attempt_problem[self.attempt]
        self.used = {1: "first", 2: "rerun"}.get(self.attempt)
        self.run = runs[self.attempt] if self.attempt and self.problem is None else None

    def _problem_of(self, n, run, check):
        """Why attempt n's data do not count; None when the attempt is whole, or when there was none."""
        if n > len(self.exits):
            return None
        code = self.exits[n - 1]
        try:
            if code == 0:
                return check(run, self.entry, self.system)  # a missing file reads "no output"
            # A failed run is reported with what is wrong with its output, if it left any.
            failure = "timeout" if code == TIMEOUT else "exit %d" % code
            output = check(run, self.entry, self.system) if run is not None else None
        except (TypeError, ValueError) as e:  # a plan line that does not hold the arguments its tool needs
            return "the plan line cannot be read: %s" % e
        return failure if output is None else "%s: %s" % (failure, output)

    @property
    def ok(self):
        return self.run is not None

    def runs(self):
        """Every run file of the process that exists, void ones too."""
        return [r for r in (self.first, self.rerun) if r is not None] + [v.run for v in self.void if v.run]

    def lost_units(self):
        """Lost units by run, each reported (section 8): the first run's, the rerun's, and the void runs'."""
        return {"first": self.first.lost_in_all() if self.first else 0,
                "rerun": self.rerun.lost_in_all() if self.rerun else 0,
                "void": sum(v.lost_units() for v in self.void)}

    def lost_unit_runs(self):
        """(run name, lost units) for every run of the process that lost a unit."""
        found = [("first", self.first), ("rerun", self.rerun)]
        out = [(name, r.lost_in_all()) for name, r in found if r is not None and r.lost_in_all()]
        return out + [(v.name, v.lost_units()) for v in self.void if v.lost_units()]

    def report(self):
        return {"id": self.id, "used": self.used, "ok": self.ok, "problem": self.problem,
                "outcome": self.outcome, "exits": list(self.exits),
                "first_problem": self.first_problem, "rerun": self.rerun is not None,
                "rerun_problem": self.rerun_problem, "lost_units": self.lost_units(),
                "void_runs": [{"file": v.name, "reason": v.reason, "lost_units": v.lost_units()} for v in self.void]}

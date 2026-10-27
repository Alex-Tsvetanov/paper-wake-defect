#!/usr/bin/env python3
"""The campaign runner of P1 v2 (hypotheses-v2.md, section 11, item 5).

Usage: run_campaign.py --plan FILE --build-dir DIR --out-dir DIR --records DIR --other-records DIR [--dry-run]
                       [--expect-processes N] [--resume] [--stop-after-s S] [--inputs-hash FILE]
       run_campaign.py --check-gate --build-dir DIR --records DIR --other-records DIR [--plan FILE]
                       [--inputs-hash FILE]

It runs the processes of one plan, one after the other, in the plan's order. It never builds,
never times anything itself and never overwrites a record. A process is one wakeprobe or abpair
run. Python 3, standard library only (ctypes included). The lab runner (lab/bin/labrun.py) starts
it on the laptop and sets LAB_JOB. A campaign is hours long and may span more than one laptop
visit (hypotheses-v2.md, section 9), so a run can stop and continue (--stop-after-s, --resume)
without losing the hours already run.

The plan is a JSON Lines file. Line 1 is the header:
    {"plan": 1, "system": "linux" or "windows", "seed": N, "generated_by": "..."}
Every other line is one process, in run order:
    {"id": "...", "tool": "wakeprobe" or "abpair", "args": [...], "cell": "...", "family": "...",
     "limit_s": N}
"family" is one of S, Bt, sweep, H4, pilot. "args" is the whole argument list except --out; the
pins are inside it. "limit_s" is a whole number of at least 1, the most seconds the runner lets
the tool run (see "A bound on every tool"). The runner appends --out <out-dir>/<id>.jsonl, or
<id>-rerun.jsonl for the rerun. An id is filename-safe: letters, digits, '.', '_' and '-', starting
with a letter or a digit. Ids are unique when case is ignored, none ends in "-rerun", and none is a
Windows device name. No id holds ".interrupted-", which names the files --resume sets aside. The
runner refuses a plan with a key it does not know or without one it needs, so the plan generator
and the runner cannot drift apart without a dry run showing it.

Provenance. Outside --dry-run the header's "generated_by" must have the form of a real plan of its
kind, as analysis/v2/plan.py writes it. A pilot plan (every line has family "pilot") has
    analysis/v2/plan.py <40 hex digits>
and a campaign plan (any other family) has
    analysis/v2/plan.py <40 hex digits> revision-log-v2.md sha256:<64 hex digits>
A stand-in or draft plan, or any other text, is refused. Under --dry-run it only warns, so that
a stand-in or draft plan can be checked on the desktop. run.txt records the text and the kind.

The records gate (hypotheses-v2.md, section 8, "Gate"; the Papers repo's rule D5). The runner takes no gate file
and trusts none: it runs the gate itself, in its own process (bench/gate_lib.py, run_gate), from --records DIR (the
sanitizer records of this system; on W the folder laptop-w alone) and --other-records DIR (the other system's). It runs
at the start of a timed run, again at every --resume, and alone as --check-gate, which exits 0 or 2 and runs nothing
(bench/run_h5.sh and run_h5.ps1 call it before any cell). A dry run needs no records; given them, it only warns.
The gate computes everything from the actual --build-dir and the folders, and refuses (exit 2, before anything is
written) unless all of this holds:
    the code paths (bench/code_paths.txt) and bench/coverage.json have no change (git is LAB_GIT, else git), and the
    build was configured from this checkout as Release with no sanitizer (WAKELOOP_SANITIZER empty or a CMake false
    constant), no toolchain file, no interprocedural optimization and the compile and link flags a fresh configure
    gives, on W neither CL nor _CL_ is set, and neither binary holds the symbols of a sanitizer runtime;
    the build's own ninja (CMAKE_MAKE_PROGRAM of its cache, never one from PATH) has nothing to build, and each
    binary's time agrees with its .ninja_log entry (a binary put in place after the build is seen);
    the build's inputs, hashed now, with its configuration (CMake files included), and the compiler: the one CMake
    identified at configure time, which compiled the binaries. A timed run is refused when today's banner of the compiler
    differs from it ("the compiler changed since configure; configure and build again"); on L the binaries' .comment
    section names that clang version too. A --resume skips the banner and wants the identity run.txt recorded
    ("gate compiler"). There is no option to name a compiler;
    for every sanitizer the system needs (L: ASan+UBSan, TSan, MSan; W: ASan; fixed in gate_lib.py, and no file can
    waive one) one green record made on the laptop matches all four targets, the compiler and the configuration, is of
    the current record format, was made with today's generator files (analysis/v2/plan.py and grid.py,
    bench/campaign_smoke.py, run_h5.sh, run_h5.ps1) and names its logs archive, which is on this machine with the
    sha256 the record names. A record that matches the inputs and does not fit is skipped with a note; a red record
    stops the gate only when it records a finding (a sanitizer report, a failed or timed-out test or smoke run), never
    when it is red for the infrastructure (a failed log pack, a dirty tree, a host refusal, a job problem);
    the other system has its records for the same code and the same generator files;
    every plan line has a shape that each required sanitizer's record ran in its campaign smoke (a plan line whose
    shape key is in no such record is refused).
run.txt records the result: the compiler identity, the code commit and fingerprint, coverage.json's sha256, the
generator files' sha256, and the name and sha256 of every record used (analysis/v2 score.py and pilot.py read these lines
and check the records against their folders); a resume line repeats it. The hashes of wakeprobe and abpair in run.txt
are those the gate made. Before each process, and before a rerun, the runner hashes them again; if either differs from
the one the gate hashed (a resume: from the start), it logs BINARY-CHANGED, starts nothing more and exits 3.
--check-gate also prints a line "binary <tool> sha256 <hex>" for each, which run_h5 keeps and checks before every cell.

Files written to the output directory:
    run.txt        written first, before any process starts: the plan's sha256, both binaries'
                   sha256, the host, the start time, the build type, the runner's CPU and more.
                   Each resume appends one "resumed:" line (see below). Nothing in it is changed.
    campaign.log   one line per run, see below
    campaign.lock  held while a runner is alive in the directory (see "Orphans and the lock")
    <id>.out, <id>.err, <id>.jsonl                the first run of a process
    <id>-rerun.out, <id>-rerun.err, <id>-rerun.jsonl   its rerun, if it needed one
    <id>.attempt1  the first run's exit code, written the moment that run ends (see "Rules")
    <id>.attempt1.tmp  the same file while it is being written
    <id>.done      the process's outcome, written when its whole handling has ended
    <id>.done.tmp  the .done file while it is being written; it is moved over <id>.done at once
    <id>.pid       the tool's pid and identity, while the tool runs; removed when it has ended
    <id>.interrupted-<k>.<ext>, <id>-rerun.interrupted-<k>.<ext>
                   the files of a run a kill interrupted, set aside by --resume
A first run's files stay when a rerun happens. The .jsonl files are written by the tools. The
runner writes no .jsonl file.

campaign.log has these lines, fields separated by one space, in the order they happened:
    <time> <id> attempt=<1 or 2> exit=<exit code or "timeout"> seconds=<wall seconds, 3 decimals>
    <time> <id> INCOMPLETE     the rerun failed too (hypotheses-v2.md, section 8)
    <time> <id> ABORTED        the runner was interrupted (Ctrl-C or SIGTERM) during this process
    <time> <id> RENAMED interrupted-<k> files=<n>     --resume set the files of this process aside
    <time> <id> MARKER attempt1-exit=<code>   --resume went on from this process's .attempt1 file
    <time> - STALE-LOCK removed pid=<n>       --resume removed a lock whose runner was gone
    <time> - RESUME skipped=<n> renamed=<n> marked=<n> to-run=<n>     a resume began
    <time> - STOP stop-after-s=<S> remaining=<n>     --stop-after-s ran out; n processes not started
<time> is the local time at the end of the run, ISO 8601 with its UTC offset, to the second. A "-"
stands where an id would be; no id is "-". The same lines go to standard output.

Rules (hypotheses-v2.md, section 8):
    A process whose run fails is run again once, from its start, with the same arguments, into
    <id>-rerun.jsonl. A run fails when the tool exits with a non-zero code or when the runner
    kills it at its limit_s (exit "timeout"). If the rerun fails too, the process is INCOMPLETE and
    the run goes on. At the end the runner prints "incomplete processes: N".
    A run the runner was killed in is void. It never counts as a failed run and never uses up an
    attempt. To make that true the first run's exit code goes to <id>.attempt1 (one line,
    "attempt 1 exit: <code>"; code is a whole number or "timeout") before any rerun starts, with
    a temporary file, a flush to disk and os.replace. The marker is written for a first run that
    passed too.

A bound on every tool. Section 8 counts a window that does not finish as a failed process, so a
hung tool must not stall a campaign. The runner waits for a tool at most its plan line's limit_s
seconds. At the limit it kills the tool's whole process group (POSIX) or job (Windows), waits
until the tool is gone, logs exit=timeout and treats the run as failed (the rerun rule applies).
The limits are chosen by the plan generator; they are not estimates. The runner never times
anything with them. The lab runner is no help here: lab/bin/labrun.py never kills a job. When its
timeout_s has passed it stops polling and refuses, and the run may still be live on the laptop.

Placement (hypotheses-v2.md, section 8: core 0 for the system and the runner). In a real run the
runner pins itself to CPU 0 and refuses (exit 2, before anything is written) if it cannot. It
starts every tool with the CPUs it had before it pinned itself, so the tool's own per-thread pins
work. Linux: the tool's start resets the affinity mask. Windows: a child inherits the process
mask and a thread cannot pin outside it, so the runner widens its mask to the original one for the
moment of the start and narrows it back at once. The runner is single-threaded, so that is safe.
wakeprobe and abpair read no inherited mask: they call pthread_setaffinity_np or
SetThreadAffinityMask for each thread they pin, and compare the CPU they end on with their pins.
A dry run pins nothing. The runner puts its mask back when it ends.

Orphans and the lock. A runner that is killed (SIGKILL, TerminateProcess, a power cut of the
terminal) must not leave a tool running: a second timed process beside it would share its pinned
CPUs. Each tool therefore dies with the runner:
    Linux    the tool starts in its own session; its start calls prctl(PR_SET_PDEATHSIG, SIGKILL)
             (ctypes), so the kernel kills it when the runner dies. The runner kills the whole
             group on every exit path: after the tool ends, at its limit, on Ctrl-C and on SIGTERM.
             A change of uid clears the parent-death signal, so a tool that drops privileges after
             it starts (wakeprobe with --pm-qos-us under sudo) is not covered by it. No plan of
             this campaign uses that flag, and the pid file below still stops a resume beside it.
    Windows  the tool starts suspended, is put into a Job Object with
             JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (ctypes, kernel32) and is then resumed. The
             runner holds the job handle while the tool runs, so the system closes it when the
             runner dies and kills the job. A job is made for each run and closed after it.
Before a tool is waited on, the runner writes <id>.pid: the pid, the image name and the start time
of the process, three lines. campaign.lock is made with an exclusive create when the run begins,
holds the same three lines for the runner, and is removed when the run ends, whatever the exit.
The pid files are runner state, not records: they exist only while a tool runs. (lab/bin/labrun.py
treats a *.pid file without a *.done file in a job's own directory as a busy laptop, so keep
--out-dir in a subdirectory of the job directory.)
A lock that is left (the runner was killed) is stale when its pid is gone or is another program:
the image name or the start time differs, so a reused pid does not block. --resume removes a
stale lock and logs STALE-LOCK. A resume is refused (exit 2) if the lock's process, or the process
of a pid file of a process that has no .done file, is still running and is that program. A pid file
that is left with its process gone is removed.
Every refusal and every check that can fail (the pin, the lock's and the tools' liveness, the
names the renames would make) happens before anything in the directory is renamed or appended
to. The only changes before the renames are the removal of a stale lock and the new lock, which
is written first. campaign.log and run.txt are appended to after the renames.

The .done file holds the outcome and the exit codes of the runs, never a measured value:
    outcome: ok | ok after rerun | incomplete
    attempt 1 exit: <exit code or timeout>
    attempt 2 exit: <exit code or timeout>        (only when there was a rerun)
It is written after the process's first run and, if needed, its rerun: first to <id>.done.tmp,
forced to disk, then moved over with os.replace. A process the runner was interrupted in (Ctrl-C,
SIGTERM, a kill) gets no .done file.

--resume continues a run in an existing output directory. Without it, an output directory that
holds records or files this run would write is refused. It is refused (exit 2) unless the
directory's run.txt records the same plan sha256 and the same binary sha256s as now, and the
build is Release. A refusal changes nothing in the directory. Then, per id in the plan's order:
    - an id with a .done file is skipped, and its outcome counts in the final total;
    - an id with an .attempt1 file but no .done file whose exit code is 0: the first run was
      complete; its .done file is written (ok) and nothing runs;
    - an id with an .attempt1 file whose exit code is a failure: the first run's files stay, any
      rerun files are set aside, and only attempt 2 runs (its first run is read from the
      marker, so it is not run again);
    - an id with no .attempt1 file but with files: the first run was void. All its files, the
      rerun's too, are set aside, and the process runs from attempt 1;
    - every other id runs, the plan's order unchanged.
Setting aside means renaming to <name>.interrupted-<k><ext>, for example a.interrupted-2.jsonl
and a-rerun.interrupted-2.err. A leftover .done.tmp or .attempt1.tmp goes with them. All files
set aside for one id in one resume share one k, which is one more than the highest k already used
for that id. Nothing is overwritten; the .attempt1 file itself is never set aside.
The files are set aside first; only then does each resume append one line to run.txt, "resumed:
<time>; lab job <LAB_JOB>; wakeprobe sha256 <hex>; abpair sha256 <hex>", and log RESUME and one
RENAMED line per process with files set aside. A resume needs LAB_JOB like any timed run.

--stop-after-s S (a whole number, at least 1) starts no new process once S seconds have passed
since this invocation began. The running process finishes, its rerun included. If processes
remain, the runner prints how to continue (--resume), logs STOP and exits 4. Otherwise it ends as
without the option. "incomplete processes: N" is printed only when no process remains.

Exit codes:
    0    every process passed, on its first run or on its rerun
    1    N > 0 processes are INCOMPLETE
    2    refused, before anything ran (the message says why). Refusals: no LAB_JOB in a real
         run; a bad plan (an unknown key, a missing limit_s, a limit_s that is not a whole number
         of at least 1 and at most 4294967, a generated_by that is not of the real form for the
         plan's kind); a plan for the other system (real run); a build that is missing, not
         Release or multi-config (real run); no --records and --other-records, or a records gate that
         refuses this build or a plan line (real run, --check-gate); an output directory that already holds records or
         any file this run would write; --expect-processes that does not match; a bad command
         line (a --stop-after-s below 1 too); a runner that cannot pin itself to CPU 0 (real
         run); with --resume: no output directory, no run.txt or one without the fields needed,
         another plan sha256, other binaries (real run), a .done, .attempt1, lock or pid file
         that is malformed or names no process of the plan, a live lock or tool (real run)
    3    the runner itself failed (for example a full disk, or a tool that cannot be put under
         the runner's guard or cannot be killed), or a measured binary is not the one the run started
         with (BINARY-CHANGED in campaign.log); the message is on standard error
    4    stopped by --stop-after-s with processes left; continue with --resume
    130  interrupted

A dry run (--dry-run) prints every command line with its limit and runs nothing. It writes nothing,
not even the output directory. It needs no LAB_JOB and no records. A plan for the other system, a
generated_by of the wrong form, a missing binary, other binaries than the run's own, a build that is
not Release, a live lock or tool and a records gate that refuses are warnings in a dry run,
so that the desktop can check a plan made for the laptop's other system. A bad plan, an output
directory that holds records and, with --resume, another plan sha256 still refuse. --dry-run
--resume prints, in the plan's order, which ids it would skip, rename (with their k), finish from a
marker, rerun and run, and the incomplete outcomes among the skipped ids. --stop-after-s has no
effect in a dry run.
"""

import argparse
import contextlib
import ctypes
import datetime
import errno
import hashlib
import json
import os
import platform
import re
import shlex
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

# The siblings of this file, whether it is started as a script (python -B bench/run_campaign.py) or isolated (python -I).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campaign_smoke  # noqa: E402  (shape_key)
import gate_lib  # noqa: E402  (run_gate)

EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_REFUSED = 2
EXIT_INTERNAL = 3
EXIT_STOPPED = 4
EXIT_INTERRUPTED = 130
EXIT_LAUNCH_FAILED = 127  # logged when a tool cannot be started at all

PLAN_VERSION = 1
TOOLS = ("wakeprobe", "abpair")
FAMILIES = ("S", "Bt", "sweep", "H4", "pilot")
SYSTEMS = ("linux", "windows")
HEADER_KEYS = frozenset({"plan", "system", "seed", "generated_by"})
PROCESS_KEYS = frozenset({"id", "tool", "args", "cell", "family", "limit_s"})
# The most seconds a limit_s may be. A Windows wait takes its time as a 32-bit number of
# milliseconds, 0xFFFFFFFF being INFINITE, and Python 3.14 on Windows hands one that is too large
# on modulo 2**32 without an error: Popen.wait(timeout=4294968) on a running process returned
# TimeoutExpired after 0.7 s (probe, 2026-10-27), which would kill a tool early. This is the
# largest limit whose milliseconds fit; the same cap holds on every system, so a plan is portable.
MAX_LIMIT_S = (2**32 - 2) // 1000
PILOT_FAMILY = "pilot"
# Real plans only (analysis/v2/plan.py, contract C2). Matched with fullmatch.
GENERATED_BY = {
    "pilot": re.compile(r"analysis/v2/plan\.py [0-9a-f]{40}"),
    "campaign": re.compile(r"analysis/v2/plan\.py [0-9a-f]{40} revision-log-v2\.md sha256:[0-9a-f]{64}"),
}
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
RESERVED_STEMS = frozenset({"CON", "PRN", "AUX", "NUL"}
                           | {f"COM{i}" for i in range(1, 10)}
                           | {f"LPT{i}" for i in range(1, 10)})
RERUN_SUFFIX = "-rerun"
DONE_SUFFIX = ".done"
TEMP_SUFFIX = ".done.tmp"
MARKER_SUFFIX = ".attempt1"
MARKER_TEMP_SUFFIX = ".attempt1.tmp"
PID_SUFFIX = ".pid"
INTERRUPTED_MARK = ".interrupted-"
RUN_FILE = "run.txt"
LOG_FILE = "campaign.log"
LOCK_FILE = "campaign.lock"
TIMEOUT = "timeout"  # the "exit code" of a run the runner killed at its limit_s
OUTCOMES = ("ok", "ok after rerun", "incomplete")
EXIT_TEXT = re.compile(r"-?[0-9]+|" + TIMEOUT)
# What a resume needs from run.txt; the first block of run.txt holds them (resume lines come later).
RESUME_KEYS = ("plan sha256", "build type", *(f"{tool} sha256" for tool in TOOLS))
CACHE_BUILD_TYPE = re.compile(r"CMAKE_BUILD_TYPE:[A-Za-z]*=(.*)")
EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
RUNNER_CPU = 0  # section 8: core 0 for the system and the runner
KILL_WAIT_S = 30  # how long a killed tool may take to be gone before the runner fails
LINUX = sys.platform.startswith("linux")
WINDOWS = sys.platform == "win32"
PR_SET_PDEATHSIG = 1
GATE_HOST = {"linux": "L", "windows": "W"}  # the records gate's names for the plan systems (see "The records gate")


class Refusal(Exception):
    """A reason to stop before anything runs. main() turns it into exit code 2."""


@dataclass(frozen=True)
class Process:
    id: str
    tool: str
    args: tuple
    cell: str
    family: str
    limit_s: int


@dataclass(frozen=True)
class Done:
    """What a .done file says: the outcome and the exit codes of the process's one or two runs.
    A code is a whole number, or TIMEOUT for a run the runner killed at its limit."""
    outcome: str
    codes: tuple


@dataclass(frozen=True)
class Holder:
    """What a lock or pid file says: a process by pid, image name and start time ("" if unknown)."""
    pid: int
    image: str
    created: str
    path: str
    text: str


@dataclass(frozen=True)
class Pending:
    """What --resume does with one process that has no .done file.

    action: "run" (from attempt 1), "rerun" (attempt 2 only; `first` is attempt 1's code from the
    marker) or "finish" (the marker says attempt 1 passed: write .done, run nothing).
    aside: the (old, new) names of the files it sets aside, all as interrupted-<k> (k is None if none)."""
    action: str
    first: object
    aside: tuple
    k: object


FRESH = Pending("run", None, (), None)


@dataclass(frozen=True)
class ResumeState:
    """What --resume found in the output directory, and the digests of the binaries it rechecked."""
    done: dict         # id -> Done, for every process with a .done file
    pending: dict      # id -> Pending, for every other process
    digests: dict      # tool -> sha256 now
    stale_lock: object  # the Holder of a lock whose runner is gone, or None
    stale_pids: tuple   # names of pid files whose tool is gone
    compiler: object = None  # the compiler identity run.txt recorded at the start ("gate compiler"), or None


@dataclass(frozen=True)
class Plan:
    path: str
    sha256: str
    system: str
    seed: int
    generated_by: str
    processes: tuple


# ---------------------------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------------------------

def is_whole_number(value):
    return isinstance(value, int) and not isinstance(value, bool)


def parse_json_object(text, lineno):
    try:
        value = json.loads(text)
    except ValueError as err:
        raise Refusal(f"plan line {lineno}: not valid JSON ({err})") from None
    if not isinstance(value, dict):
        raise Refusal(f"plan line {lineno}: expected a JSON object")
    return value


def check_keys(obj, required, lineno):
    missing = sorted(required - obj.keys())
    extra = sorted(obj.keys() - required)
    if missing:
        raise Refusal(f"plan line {lineno}: missing key(s) {', '.join(missing)}")
    if extra:
        raise Refusal(f"plan line {lineno}: unknown key(s) {', '.join(extra)}")


def parse_header(obj, lineno):
    check_keys(obj, HEADER_KEYS, lineno)
    if not is_whole_number(obj["plan"]) or obj["plan"] != PLAN_VERSION:
        raise Refusal(f"plan line {lineno}: \"plan\" must be {PLAN_VERSION}, got {obj['plan']!r}")
    if obj["system"] not in SYSTEMS:
        raise Refusal(f"plan line {lineno}: \"system\" must be one of {', '.join(SYSTEMS)}, got {obj['system']!r}")
    if not is_whole_number(obj["seed"]):
        raise Refusal(f"plan line {lineno}: \"seed\" must be a whole number")
    if not isinstance(obj["generated_by"], str):
        raise Refusal(f"plan line {lineno}: \"generated_by\" must be a string")
    return obj["system"], obj["seed"], obj["generated_by"]


def check_id(pid, lineno):
    where = f"plan line {lineno}"
    if not isinstance(pid, str) or not ID_PATTERN.fullmatch(pid) or pid.endswith("."):
        raise Refusal(f"{where}: id {pid!r} is not filename-safe (letters, digits, '.', '_' and '-', "
                      "starting with a letter or a digit, not ending in '.')")
    if pid.split(".")[0].upper() in RESERVED_STEMS:
        raise Refusal(f"{where}: id {pid!r} is a Windows device name")
    if pid.lower().endswith(RERUN_SUFFIX):
        raise Refusal(f"{where}: id {pid!r} ends in '{RERUN_SUFFIX}', which names a rerun's files")
    if INTERRUPTED_MARK in pid.lower():
        raise Refusal(f"{where}: id {pid!r} holds '{INTERRUPTED_MARK}', which names the files of an "
                      "interrupted process")


def check_args(args, lineno):
    where = f"plan line {lineno}"
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise Refusal(f"{where}: \"args\" must be a list of strings")
    if any(a == "--out" or a.startswith("--out=") for a in args):
        raise Refusal(f"{where}: \"args\" must not hold --out; the runner appends it")
    if any("\0" in a for a in args):
        raise Refusal(f"{where}: \"args\" must not hold a NUL character")
    return tuple(args)


def check_limit(limit_s, lineno):
    if not is_whole_number(limit_s) or not 1 <= limit_s <= MAX_LIMIT_S:
        raise Refusal(f"plan line {lineno}: \"limit_s\" must be a whole number of seconds from 1 to "
                      f"{MAX_LIMIT_S}, got {limit_s!r}")
    return limit_s


def parse_process(obj, lineno):
    check_keys(obj, PROCESS_KEYS, lineno)
    check_id(obj["id"], lineno)
    if obj["tool"] not in TOOLS:
        raise Refusal(f"plan line {lineno}: unknown tool {obj['tool']!r} (expected {' or '.join(TOOLS)})")
    if not isinstance(obj["cell"], str) or not obj["cell"]:
        raise Refusal(f"plan line {lineno}: \"cell\" must be a non-empty string")
    if obj["family"] not in FAMILIES:
        raise Refusal(f"plan line {lineno}: \"family\" must be one of {', '.join(FAMILIES)}, got {obj['family']!r}")
    return Process(obj["id"], obj["tool"], check_args(obj["args"], lineno), obj["cell"], obj["family"],
                   check_limit(obj["limit_s"], lineno))


def load_plan(path):
    """Read and check the whole plan. The sha256 is of the bytes that were parsed."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as err:
        raise Refusal(f"cannot read the plan {path}: {err}") from None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as err:
        raise Refusal(f"the plan {path} is not UTF-8 ({err})") from None
    lines = [(n, s) for n, s in enumerate(text.split("\n"), 1) if s.strip()]
    if not lines:
        raise Refusal(f"the plan {path} is empty")
    system, seed, generated_by = parse_header(parse_json_object(lines[0][1], lines[0][0]), lines[0][0])
    seen = set()
    processes = []
    for lineno, line in lines[1:]:
        proc = parse_process(parse_json_object(line, lineno), lineno)
        if proc.id.lower() in seen:
            raise Refusal(f"plan line {lineno}: duplicate id {proc.id!r} (ids are compared without case)")
        seen.add(proc.id.lower())
        processes.append(proc)
    if not processes:
        raise Refusal(f"the plan {path} holds a header and no process")
    return Plan(path, hashlib.sha256(raw).hexdigest(), system, seed, generated_by, tuple(processes))


def plan_kind(plan):
    """"pilot" if every line has family pilot, else "campaign" (contract C2)."""
    return "pilot" if all(proc.family == PILOT_FAMILY for proc in plan.processes) else "campaign"


def check_provenance(plan, dry):
    """Refuse a plan whose generated_by is not of the real form for its kind; a dry run only warns."""
    kind = plan_kind(plan)
    if GENERATED_BY[kind].fullmatch(plan.generated_by):
        return
    message = (f"the plan is a {kind} plan, but its generated_by {plan.generated_by!r} is not of the real form "
               f"({GENERATED_BY[kind].pattern}); stand-in and draft plans run only under --dry-run")
    if not dry:
        raise Refusal(message)
    warn(message)


# ---------------------------------------------------------------------------------------------
# The records gate
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class GateOutcome:
    """What a passed gate leaves: the lines for run.txt and the resume line, and the sha256 of the binaries it hashed."""
    lines: tuple
    binaries: dict
    compiler: str = ""


def evaluate_gate(system, build_dir, records, other_records, inputs_hash, resume_compiler=None):
    """The records gate, in this process (gate_lib.run_gate). Returns its result; Refusal if it refuses. Tests replace this
    function. There is no way to hand it a result. resume_compiler: at --resume, the compiler identity that run.txt
    recorded at the start (see gate_lib.run_gate)."""
    try:
        return gate_lib.run_gate(build=Path(build_dir), host=GATE_HOST[system], records=Path(records),
                                 other_records=Path(other_records), tool=Path(inputs_hash) if inputs_hash else None,
                                 resume_compiler=resume_compiler)
    except gate_lib.GateError as err:
        raise Refusal(f"the records gate refuses {build_dir}: {err}") from None


def plan_shape_problems(plan, smoke_keys):
    """The plan lines whose code shape (campaign_smoke.shape_key) is in no record's smoke of every required sanitizer."""
    missing = {}
    for proc in plan.processes:
        key = campaign_smoke.shape_key({"tool": proc.tool, "args": list(proc.args), "family": proc.family})
        if key not in smoke_keys:
            missing.setdefault(key, []).append(proc.id)
    if not missing:
        return []
    count = sum(len(ids) for ids in missing.values())
    key, ids = next(iter(missing.items()))
    return [f"{count} plan lines run {len(missing)} code shapes that no green record of every required sanitizer ran in "
            f"its campaign smoke, for example {ids[0]}: {list(key)}"]


def result_problems(result, plan, build_dir):
    """Why a gate result does not let this run start (empty when it does)."""
    problems = []
    if result.get("passed") is not True or result.get("dry_run") or result.get("citable") is not True:
        problems.append("the result is not a passed, citable gate (a dry run or a failure)")
    if result.get("partial"):
        problems.append("a declared gap waived a record (partial is not empty)")
    binaries = result.get("binaries") or {}
    for tool in TOOLS:
        path = tool_path(build_dir, tool)
        if binaries.get(tool) is None:
            problems.append(f"the gate hashed no {tool}")
        elif os.path.isfile(path) and sha256_file(path) != binaries[tool]:  # a missing file is check_build's to report
            problems.append(f"{tool} at {path} is not the binary the gate hashed")
    if plan is not None:
        keys = result.get("smoke_keys")
        problems += plan_shape_problems(plan, frozenset() if keys is None else keys)
    return problems


def gate_lines(result):
    """The lines run.txt keeps about a passed gate: names and sha256 of every record, and what the gate was made of."""
    def names(entries):
        return ", ".join(f"{e['name']} {e['sha256']}" for e in entries)
    own = result["wakeloop"]["coverage"]
    other = result["other_host"]["records"]
    return (("gate", "passed"), ("gate compiler", result["wakeloop"]["compiler"]), ("gate code commit", result["code_commit"]),
            ("gate code fingerprint", result["code_fingerprint"]), ("gate coverage.json sha256", result["coverage_json_sha256"]),
            ("gate generators", "; ".join(f"{k} {v}" for k, v in sorted(result["generators"].items()))),
            ("gate records", "; ".join(f"{san} {names(own[san])}" for san in sorted(own))),
            ("gate other records", "; ".join(f"{san} {names(other[san])}" for san in sorted(other))),
            ("gate smoke shape keys", str(len(result["smoke_keys"]))))


def gate_outcome(args, system, plan, build_dir, resume_compiler=None):
    """Runs the gate for a build and, with a PLAN, checks every line of it. Returns a GateOutcome; Refusal if the gate or
    a plan line is refused or the records folders are not given. resume_compiler: see evaluate_gate."""
    if args.records is None or args.other_records is None:
        raise Refusal("refusing a timed run: no --records and --other-records (the folders of this system's and the other "
                      "system's sanitizer records; hypotheses-v2.md section 8); use --dry-run to check the plan")
    result = evaluate_gate(system, build_dir, args.records, args.other_records, args.inputs_hash, resume_compiler)
    problems = result_problems(result, plan, build_dir)
    if problems:
        raise Refusal("the records gate does not let this run start: " + "; ".join(problems))
    return GateOutcome(gate_lines(result), dict(result["binaries"]), result["wakeloop"]["compiler"])


def check_gate(args, plan, build_dir, dry, resume_compiler=None):
    """The gate of a run. Returns a GateOutcome, or None when a dry run has no records or the gate refuses it (it warns).
    A timed run the gate refuses is refused."""
    if dry and (args.records is None or args.other_records is None):
        return None
    try:
        return gate_outcome(args, plan.system, plan, build_dir, resume_compiler)
    except Refusal as err:
        if not dry:
            raise
        warn(str(err))
        return None


def check_gate_mode(args):
    """--check-gate: the gate alone, for run_h5.sh and run_h5.ps1. Exit 0 or, as a Refusal, 2."""
    if args.dry_run or args.resume:
        raise Refusal("--check-gate runs the gate and nothing else; it takes no --dry-run or --resume")
    plan = load_plan(args.plan) if args.plan else None
    system = plan.system if plan else host_system()
    if system is None:
        raise Refusal(f"this host ({sys.platform}) is neither the laptop's Arch nor its Windows")
    check_host_matches(system)
    outcome = gate_outcome(args, system, plan, os.path.abspath(args.build_dir))
    print("records gate passed: " + "; ".join(f"{k} {v}" for k, v in outcome.lines))
    for tool in TOOLS:  # one line each, for run_h5, which hashes the binary again before every cell
        print(f"binary {tool} sha256 {outcome.binaries[tool]}")
    return EXIT_OK


class BinaryChanged(Exception):
    """A measured binary is not the one the run started with."""

    def __init__(self, tool, expected, found):
        super().__init__(f"{tool} is not the binary this run started with (sha256 {found}, was {expected})")
        self.tool, self.expected, self.found = tool, expected, found


class BinaryGuard:
    """Hashes wakeprobe and abpair again before each process and each rerun. A change is logged (BINARY-CHANGED) and
    raised; the runner exits 3 and starts nothing more."""

    def __init__(self, build_dir, digests, log_path):
        self.build_dir, self.digests, self.log_path = build_dir, dict(digests), log_path

    def check(self):
        for tool in TOOLS:
            try:
                found = sha256_file(tool_path(self.build_dir, tool))
            except OSError:
                found = "missing"
            if found != self.digests[tool]:
                log_event(self.log_path, f"{now()} - BINARY-CHANGED tool={tool} expected={self.digests[tool]} found={found}")
                raise BinaryChanged(tool, self.digests[tool], found)


# ---------------------------------------------------------------------------------------------
# The host, the build and the output directory
# ---------------------------------------------------------------------------------------------

def host_system():
    """The plan system names this host runs, or None for any other host."""
    return {"linux": "linux", "win32": "windows"}.get(sys.platform)


def tool_path(build_dir, tool):
    return os.path.join(build_dir, "bench", tool + EXE_SUFFIX)


def launch_prefix(path):
    """The words that start a tool. Tests replace this to run stubs; a real run starts the binary."""
    return [path]


def read_cache(build_dir):
    """The build type in CMakeCache.txt ('unknown' if none) and whether the generator is multi-config."""
    build_type = "unknown"
    multi_config = False
    try:
        with open(os.path.join(build_dir, "CMakeCache.txt"), encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = CACHE_BUILD_TYPE.match(line)
                if match:
                    build_type = match.group(1).strip() or "unknown"
                elif line.startswith("CMAKE_CONFIGURATION_TYPES:"):
                    multi_config = True
    except OSError:
        pass
    return build_type, multi_config


def check_build(build_dir):
    """Return (build type, problems). A real run needs no problems; a dry run only warns."""
    if not os.path.isdir(build_dir):
        return "unknown", [f"no build directory at {build_dir}"]
    problems = []
    for tool in TOOLS:
        path = tool_path(build_dir, tool)
        if not os.path.isfile(path):
            problems.append(f"no {tool} at {path}; build it first (this script never builds)")
    build_type, multi_config = read_cache(build_dir)
    if multi_config:
        problems.append(f"{build_dir} uses a multi-config generator; only a single-config Release build is timed")
    if build_type != "Release":
        problems.append(f"{build_dir} is built as '{build_type}', not Release; nothing measured")
    return build_type, problems


def run_paths(out_dir, pid, attempt):
    """The .jsonl, .out and .err paths of one run of a process (attempt 1 or 2)."""
    base = os.path.join(out_dir, pid if attempt == 1 else pid + RERUN_SUFFIX)
    return base + ".jsonl", base + ".out", base + ".err"


def run_files(pid, attempts=(1, 2)):
    """The (stem, extension) pairs of the files of the runs of a process: .jsonl, .out and .err."""
    return [(pid + (RERUN_SUFFIX if attempt == 2 else ""), ext)
            for attempt in attempts for ext in (".jsonl", ".out", ".err")]


def id_files(pid):
    """Every (stem, extension) pair the runner may write for a process: its runs' files, the .done
    file and its temporary name, the .attempt1 file and its temporary name, and the pid file. Files
    are matched by these names, never by prefix."""
    return run_files(pid) + [(pid, ext) for ext in (DONE_SUFFIX, TEMP_SUFFIX, MARKER_SUFFIX,
                                                    MARKER_TEMP_SUFFIX, PID_SUFFIX)]


def check_output_dir(out_dir, plan):
    """Refuse when the directory holds records, or any file this run would write."""
    if os.path.exists(out_dir) and not os.path.isdir(out_dir):
        raise Refusal(f"the output path {out_dir} exists and is not a directory")
    if not os.path.isdir(out_dir):
        return
    for root, _dirs, files in os.walk(out_dir):
        for name in files:
            if name.lower().endswith(".jsonl"):
                raise Refusal(f"the output directory {out_dir} already holds records "
                              f"(for example {os.path.join(root, name)}); use an empty one")
    planned = {RUN_FILE, LOG_FILE, LOCK_FILE}
    for proc in plan.processes:
        planned.update(stem + ext for stem, ext in id_files(proc.id))
    planned = {name.lower() for name in planned}
    clash = sorted(name for name in os.listdir(out_dir) if name.lower() in planned)
    if clash:
        raise Refusal(f"the output directory {out_dir} already holds files this run would write "
                      f"(for example {clash[0]}); use an empty one")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------------------------
# Windows: processes, affinity and Job Objects (ctypes, kernel32 and ntdll)
# ---------------------------------------------------------------------------------------------

if WINDOWS:
    from ctypes import wintypes

    _HANDLE = ctypes.c_void_p
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _nt = ctypes.WinDLL("ntdll")

    def _api(func, restype, *argtypes):
        """Set the types of a Windows call, so that 64-bit handles are not cut to 32 bits."""
        func.restype, func.argtypes = restype, list(argtypes)
        return func

    _GetCurrentProcess = _api(_k32.GetCurrentProcess, _HANDLE)
    _OpenProcess = _api(_k32.OpenProcess, _HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _CloseHandle = _api(_k32.CloseHandle, wintypes.BOOL, _HANDLE)
    _WaitForSingleObject = _api(_k32.WaitForSingleObject, wintypes.DWORD, _HANDLE, wintypes.DWORD)
    _GetProcessAffinityMask = _api(_k32.GetProcessAffinityMask, wintypes.BOOL, _HANDLE,
                                   ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t))
    _SetProcessAffinityMask = _api(_k32.SetProcessAffinityMask, wintypes.BOOL, _HANDLE, ctypes.c_size_t)
    _GetProcessTimes = _api(_k32.GetProcessTimes, wintypes.BOOL, _HANDLE, ctypes.POINTER(wintypes.FILETIME),
                            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                            ctypes.POINTER(wintypes.FILETIME))
    _QueryFullProcessImageNameW = _api(_k32.QueryFullProcessImageNameW, wintypes.BOOL, _HANDLE, wintypes.DWORD,
                                       wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    _CreateJobObjectW = _api(_k32.CreateJobObjectW, _HANDLE, ctypes.c_void_p, wintypes.LPCWSTR)
    _SetInformationJobObject = _api(_k32.SetInformationJobObject, wintypes.BOOL, _HANDLE, ctypes.c_int,
                                    ctypes.c_void_p, wintypes.DWORD)
    _AssignProcessToJobObject = _api(_k32.AssignProcessToJobObject, wintypes.BOOL, _HANDLE, _HANDLE)
    _TerminateJobObject = _api(_k32.TerminateJobObject, wintypes.BOOL, _HANDLE, wintypes.UINT)
    _NtResumeProcess = _api(_nt.NtResumeProcess, ctypes.c_long, _HANDLE)

    PROCESS_TERMINATE = 0x0001
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_SUSPEND_RESUME = 0x0800
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x102
    CREATE_SUSPENDED = 0x00000004
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9  # the JOBOBJECTINFOCLASS value

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _JobBasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _JobExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _JobBasicLimits), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    def _win_error(what):
        return OSError(f"{what}: {ctypes.WinError(ctypes.get_last_error())}")

    def _win_affinity():
        mask, system = ctypes.c_size_t(), ctypes.c_size_t()
        if not _GetProcessAffinityMask(_GetCurrentProcess(), ctypes.byref(mask), ctypes.byref(system)):
            raise _win_error("GetProcessAffinityMask")
        return frozenset(cpu for cpu in range(8 * ctypes.sizeof(ctypes.c_size_t)) if mask.value >> cpu & 1)

    def _win_set_affinity(cpus):
        if not cpus or max(cpus) >= 8 * ctypes.sizeof(ctypes.c_size_t):
            raise OSError(errno.EINVAL, f"CPUs {sorted(cpus)} do not fit in one processor group")
        if not _SetProcessAffinityMask(_GetCurrentProcess(), sum(1 << cpu for cpu in cpus)):
            raise _win_error("SetProcessAffinityMask")

    def _win_identity(pid):
        handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
        if not handle:  # no such process, or one that is not ours to ask about
            return None
        try:
            if _WaitForSingleObject(handle, 0) != WAIT_TIMEOUT:  # signalled: the process has ended
                return None
            image = ""
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if _QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                image = os.path.basename(buffer.value)
            created = ""
            times = [wintypes.FILETIME() for _ in range(4)]
            if _GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                created = str(times[0].dwHighDateTime << 32 | times[0].dwLowDateTime)
            return image, created
        finally:
            _CloseHandle(handle)

    class ToolJob:
        """A Job Object that kills everything in it when its last handle closes. The runner holds the
        only handle (it is not inheritable), so the system closes it if the runner dies."""

        def __init__(self):
            self.handle = _CreateJobObjectW(None, None)
            if not self.handle:
                raise _win_error("CreateJobObjectW")
            info = _JobExtendedLimits()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _SetInformationJobObject(self.handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                            ctypes.byref(info), ctypes.sizeof(info)):
                err = _win_error("SetInformationJobObject")
                self.close()
                raise err

        def adopt(self, pid):
            """Put the suspended process `pid` into the job, then let it run. It runs nothing before."""
            access = PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_SUSPEND_RESUME
            process = _OpenProcess(access, False, pid)
            if not process:
                raise _win_error("OpenProcess")
            try:
                if not _AssignProcessToJobObject(self.handle, process):
                    raise _win_error("AssignProcessToJobObject")
                status = _NtResumeProcess(process)
                if status != 0:
                    raise OSError(f"NtResumeProcess failed with status {status & 0xFFFFFFFF:#x}")
            finally:
                _CloseHandle(process)

        def terminate(self):
            if self.handle:
                _TerminateJobObject(self.handle, 1)

        def close(self):
            if self.handle:
                _TerminateJobObject(self.handle, 1)
                _CloseHandle(self.handle)
                self.handle = None


# ---------------------------------------------------------------------------------------------
# Processes: identity, placement and the guard that keeps a tool from outliving the runner
# ---------------------------------------------------------------------------------------------

def process_identity(pid):
    """(image name, start time) of the running process `pid`, or None if there is none. A zombie is
    not running. Either part is "" where this platform cannot say. The start time is what tells a
    reused pid from the process that had it before."""
    if WINDOWS:
        return _win_identity(pid)
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as handle:
            text = handle.read()
    except OSError:
        if LINUX:
            return None
        try:  # no /proc: only whether the pid exists
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass
        return "", ""
    head, _, tail = text.rpartition(")")  # the name may hold spaces and parentheses; "pid (name) S ..."
    fields = tail.split()
    if len(fields) < 20 or fields[0] in ("Z", "X"):
        return None
    return head.partition("(")[2], fields[19]  # fields[19] is field 22 of proc(5): start time in ticks


def same_program(recorded, live):
    """True if a live process (image, start) can be the one recorded. Where nothing was recorded, any
    live process counts, so a refusal is the safe side."""
    return all(not was or not now_ or was.lower() == now_.lower() for was, now_ in zip(recorded, live))


def holder_text(pid):
    image, created = process_identity(pid) or ("", "")
    return f"{pid}\n{image}\n{created}\n"


def read_holder(path):
    """Read a lock or pid file. Raise ValueError, with the reason, if the runner did not write it."""
    with open(path, encoding="utf-8", newline="") as handle:  # no newline translation: exactly what was written
        text = handle.read()
    lines = text.split("\n")
    if len(lines) != 4 or lines[3] or not re.fullmatch(r"[1-9][0-9]*", lines[0]):
        raise ValueError("expected three lines: pid, image name, start time")
    return Holder(int(lines[0]), lines[1], lines[2], path, text)


def holder_running(holder):
    live = process_identity(holder.pid)
    return live is not None and same_program((holder.image, holder.created), live)


def write_holder(path, pid):
    """Write a pid file: the identity of `pid` right now. It must not exist yet."""
    with open(path, "x", encoding="utf-8", newline="\n") as handle:
        handle.write(holder_text(pid))
        handle.flush()
        os.fsync(handle.fileno())


def current_affinity():
    """The CPUs this runner may use, as a frozenset, or None where that cannot be read."""
    if WINDOWS:
        return _win_affinity()
    if hasattr(os, "sched_getaffinity"):
        return frozenset(os.sched_getaffinity(0))
    return None


def set_affinity(cpus):
    if WINDOWS:
        _win_set_affinity(cpus)
    else:
        os.sched_setaffinity(0, set(cpus))


class Placement:
    """Where the runner and its tools run: the runner on RUNNER_CPU, every tool on the CPUs the runner
    had before it pinned itself. The mask is read when the object is made."""

    def __init__(self):
        self.original = current_affinity()
        self.cpu = None  # the CPU the runner is pinned to, or None

    def pin(self):
        """Pin the runner to RUNNER_CPU. Raise Refusal if that cannot be done."""
        cpu = RUNNER_CPU
        if self.original is None or not (WINDOWS or hasattr(os, "sched_setaffinity")):
            raise Refusal(f"cannot pin the runner to CPU {cpu}: this platform has no way to set an affinity")
        if cpu not in self.original:
            raise Refusal(f"cannot pin the runner to CPU {cpu}: it is not in the runner's CPUs "
                          f"{sorted(self.original)}")
        try:
            set_affinity({cpu})
        except (OSError, ValueError) as err:
            raise Refusal(f"cannot pin the runner to CPU {cpu}: {err}") from None
        self.cpu = cpu

    def restore(self):
        """Give the runner back the CPUs it had. Nothing is done if it was never pinned."""
        if self.cpu is not None:
            set_affinity(self.original)
            self.cpu = None

    @contextlib.contextmanager
    def for_child(self):
        """Windows: a child inherits the process mask, so widen it to the original for the start and
        narrow it back at once. Elsewhere the tool's start resets the mask itself (child_setup)."""
        if not (WINDOWS and self.cpu is not None):
            yield
            return
        set_affinity(self.original)
        try:
            yield
        finally:
            set_affinity({self.cpu})

    def child_setup(self):
        """The function the forked child calls before it starts the tool (Linux), or None. It asks
        the kernel to kill the tool when the runner dies, then gives it the original CPUs. The C
        library is loaded here, in the parent, so the child only calls what is already loaded."""
        if not LINUX:
            return None
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.restype = ctypes.c_int
        prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        runner, original, kill = os.getpid(), self.original, int(signal.SIGKILL)

        def setup():
            if prctl(PR_SET_PDEATHSIG, kill, 0, 0, 0) != 0:
                raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
            if os.getppid() != runner:  # the runner died before the kernel could watch it
                os._exit(EXIT_LAUNCH_FAILED)
            if original is not None and hasattr(os, "sched_setaffinity"):
                os.sched_setaffinity(0, original)
        return setup


class RunningTool:
    """A started tool and the means to kill all of it: its process group (POSIX) or its job (Windows)."""

    def __init__(self, popen, job=None):
        self.popen = popen
        self.job = job

    @property
    def pid(self):
        return self.popen.pid

    def kill(self):
        """Kill the tool and everything it started."""
        if self.job is not None:
            self.job.terminate()
            return
        try:
            os.killpg(self.popen.pid, signal.SIGKILL)  # the tool leads its own group (start_new_session)
        except ProcessLookupError:
            pass  # nothing is left in the group

    def reap(self):
        try:
            self.popen.wait(timeout=KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"the tool (pid {self.popen.pid}) is still there {KILL_WAIT_S} s after it was "
                               "killed; nothing else may run beside it") from None

    def wait(self, limit_s):
        """Wait for the tool at most `limit_s` seconds. Return its exit code, or TIMEOUT after killing it."""
        try:
            return self.popen.wait(timeout=limit_s)
        except subprocess.TimeoutExpired:
            self.kill()
            self.reap()
            return TIMEOUT

    def close(self):
        """Leave nothing of the tool behind, on every exit path: kill what is left, wait, free the job."""
        try:
            self.kill()
            self.reap()
        finally:
            if self.job is not None:
                self.job.close()


def start_tool(cmd, out_file, err_file, placement):
    """Start a tool under the runner's guard. OSError means the tool could not be started at all; any
    other failure to guard it is a RuntimeError, because a tool must never run unguarded."""
    common = dict(stdin=subprocess.DEVNULL, stdout=out_file, stderr=err_file)
    if not WINDOWS:
        try:
            popen = subprocess.Popen(cmd, start_new_session=True, preexec_fn=placement.child_setup(), **common)
        except subprocess.SubprocessError as err:  # the guard failed in the child
            raise RuntimeError(f"cannot start {cmd[0]} under the runner's guard: {err}") from err
        return RunningTool(popen)
    job = ToolJob()
    try:
        flags = CREATE_SUSPENDED | subprocess.CREATE_NEW_PROCESS_GROUP
        with placement.for_child():
            popen = subprocess.Popen(cmd, creationflags=flags, **common)
        try:
            job.adopt(popen.pid)
        except BaseException as err:  # the tool has run nothing yet; it must not be left suspended
            popen.kill()
            popen.wait()
            if isinstance(err, OSError):
                raise RuntimeError(f"cannot put {cmd[0]} into its job: {err}") from err
            raise
    except BaseException:
        job.close()
        raise
    return RunningTool(popen, job)


# ---------------------------------------------------------------------------------------------
# Commands, the log and run.txt
# ---------------------------------------------------------------------------------------------

def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def build_command(proc, build_dir, out_jsonl):
    return [*launch_prefix(tool_path(build_dir, proc.tool)), *proc.args, "--out", out_jsonl]


def quote(words):
    return subprocess.list2cmdline(words) if sys.platform == "win32" else shlex.join(words)


def command_line(proc, build_dir, out_dir, attempt):
    """A run's command as the dry run prints it: the command, where its output goes, its limit."""
    out_jsonl, out_path, err_path = run_paths(out_dir, proc.id, attempt)
    cmd = build_command(proc, build_dir, out_jsonl)
    return f"{quote(cmd)} > {quote([out_path])} 2> {quote([err_path])} (limit {proc.limit_s} s)"


def warn(message):
    print(f"run_campaign.py: warning: {message}", file=sys.stderr)


def log_event(log_path, text):
    """Append one line to campaign.log and echo it. The file is closed after every line."""
    with open(log_path, "a", encoding="ascii", newline="\n") as handle:
        handle.write(text + "\n")
    print(text, flush=True)


def write_run_txt(path, fields):
    """Write run.txt and force it to disk. It must not exist yet."""
    text = "".join(f"{key}: {value}\n" for key, value in fields)
    with open(path, "x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def run_fields(plan, build_dir, out_dir, build_type, started, placement, gate=None, digests=None):
    fields = [
        ("runner", "run_campaign.py"),
        ("started", started),
        ("host", platform.node()),
        ("host platform", f"{sys.platform}, {platform.platform()}"),
        ("python", f"{platform.python_version()}, {sys.executable}"),
        ("lab job", os.environ.get("LAB_JOB", "")),
        ("plan", os.path.abspath(plan.path)),
        ("plan sha256", plan.sha256),
        ("plan system", plan.system),
        ("plan seed", plan.seed),
        ("plan kind", plan_kind(plan)),
        ("plan generated_by", plan.generated_by),
        ("processes", len(plan.processes)),
        ("build dir", build_dir),
        ("build type", build_type),
        *(gate or ()),
        ("out dir", out_dir),
        ("runner cpu", placement.cpu),
        ("tool cpus at start", " ".join(str(cpu) for cpu in sorted(placement.original))),
    ]
    for tool in TOOLS:
        path = tool_path(build_dir, tool)
        fields.append((tool, path))
        fields.append((f"{tool} sha256", digests[tool] if digests else sha256_file(path)))  # the gate's, when there is one
    return fields


# ---------------------------------------------------------------------------------------------
# The .done and .attempt1 files and resuming
# ---------------------------------------------------------------------------------------------

def outcome_of(codes):
    """The outcome of a process from the exit codes of its runs: one, or two when it was rerun.
    A code is a whole number or TIMEOUT; only 0 passes."""
    if codes[0] == 0:
        return "ok"
    return "ok after rerun" if len(codes) == 2 and codes[1] == 0 else "incomplete"


def write_atomic(final, temp, text):
    """Write `final` atomically: a temporary file, forced to disk, then os.replace. The temporary file
    is opened with "x", so a leftover one is never written over."""
    with open(temp, "x", encoding="ascii", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, final)


def write_done(out_dir, pid, done):
    lines = [f"outcome: {done.outcome}"]
    lines += [f"attempt {n} exit: {code}" for n, code in enumerate(done.codes, 1)]
    write_atomic(os.path.join(out_dir, pid + DONE_SUFFIX), os.path.join(out_dir, pid + TEMP_SUFFIX),
                 "".join(line + "\n" for line in lines))


def write_marker(out_dir, pid, code):
    """Record how the first run ended, before any rerun starts (contract C3)."""
    write_atomic(os.path.join(out_dir, pid + MARKER_SUFFIX), os.path.join(out_dir, pid + MARKER_TEMP_SUFFIX),
                 f"attempt 1 exit: {code}\n")


def parse_exit(text):
    if not EXIT_TEXT.fullmatch(text):
        raise ValueError(f"{text!r} is not an exit code or {TIMEOUT}")
    return text if text == TIMEOUT else int(text)


def parse_done(text):
    """Read a .done file. Raise ValueError, with the reason, if the runner did not write it."""
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(": ")
        if not sep or key in fields:
            raise ValueError(f"the line {line!r} is not a new 'key: value' line")
        fields[key] = value
    outcome = fields.pop("outcome", None)
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    codes = []
    for attempt in (1, 2):
        key = f"attempt {attempt} exit"
        if key in fields:
            codes.append(parse_exit(fields.pop(key)))
        elif attempt == 1:
            raise ValueError(f"no '{key}' line")
    if fields:
        raise ValueError(f"unknown key {sorted(fields)[0]!r}")
    if len(codes) != (1 if codes[0] == 0 else 2) or outcome_of(codes) != outcome:
        raise ValueError("the outcome does not match the exit codes")
    return Done(outcome, tuple(codes))


def parse_marker(text):
    """Read an .attempt1 file: its one line. Raise ValueError if the runner did not write it."""
    match = re.fullmatch(r"attempt 1 exit: (\S+)\n", text)
    if not match:
        raise ValueError("expected the one line 'attempt 1 exit: <code>'")
    return parse_exit(match.group(1))


def read_run_txt(out_dir):
    """The fields of run.txt, the first line of each key. Resume lines come after the first block."""
    path = os.path.join(out_dir, RUN_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as err:
        raise Refusal(f"cannot resume: cannot read {path}: {err}") from None
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(": ")
        if sep:
            fields.setdefault(key, value)
    return fields


def read_state_files(out_dir, plan, names, suffix, parse, what):
    """Map id -> parsed value for every file with this suffix in the directory. Refuse on one that is
    not the runner's, or that belongs to no process of the plan."""
    by_lower = {proc.id.lower(): proc.id for proc in plan.processes}
    found = {}
    for name in sorted(names):
        if not name.lower().endswith(suffix):
            continue
        path = os.path.join(out_dir, name)
        pid = by_lower.get(name[:-len(suffix)].lower())
        if pid is None:
            raise Refusal(f"cannot resume: {path} is the {what} of no process of this plan")
        try:
            with open(path, encoding="ascii") as handle:
                found[pid] = parse(handle.read())
        except (OSError, ValueError) as err:
            raise Refusal(f"cannot resume: {path} is not a {what} this runner wrote ({err})") from None
    return found


def check_holders(plan, out_dir, names, done, dry):
    """Look at the lock and the pid files without changing anything. Refuse if a runner or a tool of the
    directory is still running (a dry run only warns). Return (the stale lock or None, the names of
    the pid files that are left with their tool gone)."""
    present = {name.lower(): name for name in names}
    stale_lock = None
    stale_pids = []
    candidates = []
    if LOCK_FILE in present:
        candidates.append((present[LOCK_FILE], "runner"))
    for proc in plan.processes:
        name = present.get((proc.id + PID_SUFFIX).lower())
        if name is not None and proc.id not in done:
            candidates.append((name, f"tool of process {proc.id}"))
    for name, who in candidates:
        path = os.path.join(out_dir, name)
        try:
            holder = read_holder(path)
        except (OSError, ValueError) as err:
            raise Refusal(f"cannot resume: {path} is not a file this runner wrote ({err})") from None
        if holder_running(holder):
            message = (f"{path} names the {who}, pid {holder.pid} ({holder.image or 'image unknown'}), which is "
                       "still running; nothing is resumed beside it. If that pid is not this campaign's, "
                       f"remove {name} by hand")
            if not dry:
                raise Refusal("cannot resume: " + message)
            warn(message)
        elif who == "runner":
            stale_lock = holder
        else:
            stale_pids.append(name)
    return stale_lock, tuple(stale_pids)


def next_interrupted_k(names, pid):
    """One more than the highest k in the interrupted files of a process. It looks at both stems and
    every extension, so the files of one interruption share one k and no earlier file is touched."""
    pattern = re.compile(re.escape(pid) + "(?:" + re.escape(RERUN_SUFFIX) + ")?"
                         + re.escape(INTERRUPTED_MARK) + r"(\d+)\.", re.IGNORECASE)
    used = [int(match.group(1)) for match in map(pattern.match, names) if match]
    return max(used, default=0) + 1


def aside_names(pid, found, k):
    """The (old, new) names that set the files `found` of a process aside as interrupted-<k>."""
    split = {(stem + ext).lower(): (stem, ext) for stem, ext in id_files(pid)}
    pairs = []
    for name in found:
        stem, ext = split[name.lower()]
        pairs.append((name, f"{stem}{INTERRUPTED_MARK}{k}{ext}"))
    return pairs


def plan_resume(plan, out_dir, names, done, markers):
    """What a resume does with every process that has no .done file (contract C3). It reads only."""
    present = {name.lower(): name for name in names}
    pending = {}
    for proc in plan.processes:
        if proc.id in done:
            continue
        temps = [(proc.id, TEMP_SUFFIX), (proc.id, MARKER_TEMP_SUFFIX)]
        if proc.id in markers:
            first = markers[proc.id]
            action = "finish" if first == 0 else "rerun"
            candidates = run_files(proc.id, attempts=(2,)) + temps  # the first run's data stay
        else:
            first, action = None, "run"
            candidates = run_files(proc.id) + temps  # the first run was void, and so is anything after it
        wanted = [stem + ext for stem, ext in candidates]
        found = [present[name.lower()] for name in wanted if name.lower() in present]
        aside, k = (), None
        if found:
            k = next_interrupted_k(names, proc.id)
            aside = tuple(aside_names(proc.id, found, k))
            taken = [new for _old, new in aside if new.lower() in present]
            if taken:
                raise Refusal(f"cannot resume: {taken[0]} exists; {proc.id}'s files are not renamed over it")
        pending[proc.id] = Pending(action, first, aside, k)
    return pending


def check_resume(plan, build_dir, out_dir, dry):
    """Check that the directory holds a run of this plan with these binaries, and say what is left.
    It reads and changes nothing. In a dry run, other binaries and live processes only warn."""
    if not os.path.isdir(out_dir):
        raise Refusal(f"cannot resume: there is no output directory at {out_dir}")
    recorded = read_run_txt(out_dir)
    missing = [key for key in RESUME_KEYS if key not in recorded]
    if missing:
        raise Refusal(f"cannot resume: {os.path.join(out_dir, RUN_FILE)} has no '{missing[0]}' line")
    if recorded["plan sha256"] != plan.sha256:
        raise Refusal(f"cannot resume: the plan has sha256 {plan.sha256}, but {RUN_FILE} in {out_dir} "
                      f"records {recorded['plan sha256']}")
    if recorded["build type"] != "Release":
        raise Refusal(f"cannot resume: {RUN_FILE} in {out_dir} records a '{recorded['build type']}' build, "
                      "not Release")
    digests = {}
    for tool in TOOLS:
        path = tool_path(build_dir, tool)
        if not os.path.isfile(path):  # a dry run has warned already; a real run has refused
            continue
        digests[tool] = sha256_file(path)
        if digests[tool] != recorded[f"{tool} sha256"]:
            message = (f"{tool} at {path} has sha256 {digests[tool]}, but {RUN_FILE} records "
                       f"{recorded[tool + ' sha256']}; these are not the binaries this run started with")
            if not dry:
                raise Refusal("cannot resume: " + message)
            warn(message)
    names = os.listdir(out_dir)
    done = read_state_files(out_dir, plan, names, DONE_SUFFIX, parse_done, ".done file")
    markers = read_state_files(out_dir, plan, names, MARKER_SUFFIX, parse_marker, ".attempt1 file")
    for pid, record in done.items():
        if pid in markers and markers[pid] != record.codes[0]:
            raise Refusal(f"cannot resume: {pid}.attempt1 says attempt 1 exited {markers[pid]}, but "
                          f"{pid}.done says {record.codes[0]}")
    stale_lock, stale_pids = check_holders(plan, out_dir, names, done, dry)
    markers = {pid: code for pid, code in markers.items() if pid not in done}
    pending = plan_resume(plan, out_dir, names, done, markers)
    compiler = recorded.get("gate compiler")
    if not compiler and not dry:
        raise Refusal(f"cannot resume: {os.path.join(out_dir, RUN_FILE)} has no 'gate compiler' line, so the run was not "
                      "started by a runner with the records gate")
    return ResumeState(done, pending, digests, stale_lock, stale_pids, compiler)


@contextlib.contextmanager
def campaign_lock(out_dir, stale):
    """Hold campaign.lock for the run: made with an exclusive create, removed when the run ends. A stale
    lock (the Holder read by check_resume) is removed first; begin_resume logs that, after its renames."""
    path = os.path.join(out_dir, LOCK_FILE)
    if stale is not None:
        with open(path, encoding="utf-8", newline="") as handle:
            if handle.read() != stale.text:
                raise Refusal(f"{path} changed while this run was starting; another runner may be starting")
        os.remove(path)
    text = holder_text(os.getpid())
    try:
        with open(path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise Refusal(f"{path} appeared while this run was starting; another runner started at the same "
                      "moment") from None
    try:
        yield
    finally:
        with contextlib.suppress(OSError):  # a lock that is gone, or no longer ours, is left alone
            with open(path, encoding="utf-8", newline="") as handle:
                ours = handle.read() == text
            if ours:
                os.remove(path)


def begin_resume(plan, out_dir, state, gate=None):
    """Begin a resume. The renames come first, because they are what another process can make fail (a
    file that is open elsewhere): the "resumed:" line in run.txt and the lines in the log are written
    only once the interrupted files are set aside and the pid files of tools that are gone removed."""
    for pending in state.pending.values():
        for old, new in pending.aside:
            target = os.path.join(out_dir, new)
            if os.path.lexists(target):  # cannot happen (checked, and k is new); os.rename may overwrite on POSIX
                raise FileExistsError(f"{target} exists; {old} is not renamed over it")
            os.rename(os.path.join(out_dir, old), target)
    for name in state.stale_pids:
        os.remove(os.path.join(out_dir, name))
    started = now()
    fields = [f"resumed: {started}", f"lab job {os.environ.get('LAB_JOB', '')}"]
    fields += [f"{tool} sha256 {state.digests[tool]}" for tool in TOOLS]
    if gate is not None:
        fields += [f"{key} {value}" for key, value in gate.lines]
    with open(os.path.join(out_dir, RUN_FILE), "a", encoding="utf-8", newline="\n") as handle:
        handle.write("; ".join(fields) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    log_path = os.path.join(out_dir, LOG_FILE)
    if state.stale_lock is not None:
        log_event(log_path, f"{started} - STALE-LOCK removed pid={state.stale_lock.pid}")
    todo = state.pending.values()
    log_event(log_path, f"{started} - RESUME skipped={len(state.done)} renamed={sum(1 for t in todo if t.aside)} "
                        f"marked={sum(1 for t in todo if t.action == 'finish')} "
                        f"to-run={sum(1 for t in todo if t.action != 'finish')}")
    for pid, pending in state.pending.items():
        if pending.aside:
            log_event(log_path, f"{now()} {pid} RENAMED interrupted-{pending.k} files={len(pending.aside)}")


# ---------------------------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------------------------

def execute(proc, build_dir, out_dir, attempt, placement):
    """Run a process once. Return (exit code or TIMEOUT, wall seconds). The tool's stdout and stderr go
    to files. The tool is killed at the plan line's limit_s, and on every other way out of here."""
    out_jsonl, out_path, err_path = run_paths(out_dir, proc.id, attempt)
    cmd = build_command(proc, build_dir, out_jsonl)
    pid_path = os.path.join(out_dir, proc.id + PID_SUFFIX)
    with open(out_path, "xb") as out_file, open(err_path, "xb") as err_file:
        start = time.monotonic()
        try:
            tool = start_tool(cmd, out_file, err_file, placement)
        except OSError as err:
            err_file.write(f"run_campaign.py: cannot start {cmd[0]}: {err}\n".encode("utf-8", "replace"))
            return EXIT_LAUNCH_FAILED, time.monotonic() - start
        try:
            write_holder(pid_path, tool.pid)
            code = tool.wait(proc.limit_s)
        finally:
            tool.close()  # if the tool cannot be killed this raises, and its pid file stays
            with contextlib.suppress(FileNotFoundError):
                os.remove(pid_path)
        return code, time.monotonic() - start


def run_process(proc, build_dir, out_dir, log_path, placement, first=None, guard=None):
    """Run a process, and once more if it fails. Return its Done: the outcome and the exit codes.
    `first` is the exit code of attempt 1 read from its .attempt1 file by a resume: that attempt is
    not run again. `guard` (a BinaryGuard) is checked before every attempt."""
    codes = [] if first is None else [first]
    for attempt in (1, 2):
        if attempt <= len(codes):
            continue
        if guard is not None:
            guard.check()
        code, seconds = execute(proc, build_dir, out_dir, attempt, placement)
        codes.append(code)
        if attempt == 1:
            write_marker(out_dir, proc.id, code)  # before the log line, before any rerun
        log_event(log_path, f"{now()} {proc.id} attempt={attempt} exit={code} seconds={seconds:.3f}")
        if code == 0:
            break
    else:
        log_event(log_path, f"{now()} {proc.id} INCOMPLETE")
    return Done(outcome_of(codes), tuple(codes))


def raise_interrupt(_signum, _frame):
    raise KeyboardInterrupt


@contextlib.contextmanager
def sigterm_as_interrupt():
    """On POSIX, let SIGTERM stop the runner like Ctrl-C: the signal handler raises KeyboardInterrupt in
    the main thread, the running tool's group is killed on the way out (execute), ABORTED is logged
    and the exit code is 130. This cannot help where Python code does not get to run: SIGKILL, and on
    Windows every termination (TerminateProcess, Stop-Process, taskkill), which never raises a signal.
    The tool still dies then, through PR_SET_PDEATHSIG (Linux) or the job object (Windows)."""
    try:
        previous = signal.signal(signal.SIGTERM, raise_interrupt)
    except (ValueError, OSError):  # not the main thread
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def run_all(plan, build_dir, out_dir, done, pending, stop_at, placement, guard=None):
    """Run every process that has no .done file, in order, and write its .done file.

    `done` maps an id to the Done read from its .done file (empty in a first run). `pending` maps an id
    to what a resume does with it; an id not in it runs from attempt 1. `stop_at` is a time.monotonic()
    value or None: no process starts at or after it, and the running one, its rerun included, always
    finishes. `guard` is a BinaryGuard or None. Return (INCOMPLETE processes in all, including the ones in
    `done`; processes not started because of `stop_at`)."""
    log_path = os.path.join(out_dir, LOG_FILE)
    incomplete = sum(1 for record in done.values() if record.outcome == "incomplete")
    current = None
    try:
        # A process whose .attempt1 file says it passed only lacks its .done file: write it, run nothing.
        for proc in plan.processes:
            todo = pending.get(proc.id, FRESH)
            if proc.id not in done and todo.action == "finish":
                log_event(log_path, f"{now()} {proc.id} MARKER attempt1-exit={todo.first}")
                write_done(out_dir, proc.id, Done("ok", (todo.first,)))
        for index, proc in enumerate(plan.processes, 1):
            todo = pending.get(proc.id, FRESH)
            if proc.id in done or todo.action == "finish":
                continue
            if stop_at is not None and time.monotonic() >= stop_at:
                remaining = sum(1 for p in plan.processes[index - 1:]
                                if p.id not in done and pending.get(p.id, FRESH).action != "finish")
                return incomplete, remaining
            if todo.first is not None:
                log_event(log_path, f"{now()} {proc.id} MARKER attempt1-exit={todo.first}")
            current = proc
            print(f"process {index}/{len(plan.processes)}: {proc.id} ({proc.tool}, family {proc.family})",
                  flush=True)
            record = run_process(proc, build_dir, out_dir, log_path, placement, todo.first, guard)
            write_done(out_dir, proc.id, record)
            current = None
            if record.outcome == "incomplete":
                incomplete += 1
    except KeyboardInterrupt:
        if current is not None:
            log_event(log_path, f"{now()} {current.id} ABORTED")
        raise
    return incomplete, 0


# ---------------------------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------------------------

def positive_whole_number(text):
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a whole number of at least 1, got {text!r}")
    return value


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="run_campaign.py", allow_abbrev=False,
                                     description="Run the processes of a P1 v2 campaign plan.")
    parser.add_argument("--plan", help="the JSON Lines plan (required but for --check-gate)")
    parser.add_argument("--build-dir", required=True, help="an existing v2 Release build directory")
    parser.add_argument("--out-dir", help="where the records and logs go (required but for --check-gate)")
    parser.add_argument("--records", help="the folder of this system's sanitizer records (W: laptop-w); a timed run needs it")
    parser.add_argument("--other-records", help="the folder of the other system's sanitizer records; a timed run needs it")
    parser.add_argument("--inputs-hash", help="the Papers repo's lab/bin/inputs_hash.py (default: found beside the checkout)")
    parser.add_argument("--check-gate", action="store_true",
                        help="run the records gate alone and exit 0 or 2 (run_h5.sh and run_h5.ps1 do, before any cell)")
    parser.add_argument("--dry-run", action="store_true", help="print every command line; run nothing")
    parser.add_argument("--expect-processes", type=int, metavar="N",
                        help="refuse (exit 2) unless the plan has exactly N processes")
    parser.add_argument("--resume", action="store_true",
                        help="continue the run in an existing --out-dir: skip the processes with a .done file")
    parser.add_argument("--stop-after-s", type=positive_whole_number, metavar="S",
                        help="start no new process once S seconds have passed; exit 4 if any remain")
    args = parser.parse_args(argv)
    if not args.check_gate and (args.plan is None or args.out_dir is None):
        parser.error("the following arguments are required: --plan, --out-dir")
    return args


def dry_run(plan, build_dir, out_dir):
    total = len(plan.processes)
    print(f"plan: {total} processes, system {plan.system}, seed {plan.seed}, sha256 {plan.sha256}")
    for index, proc in enumerate(plan.processes, 1):
        print(f"{index}: {command_line(proc, build_dir, out_dir, 1)}")
    print(f"dry run: {total} processes")
    return EXIT_OK


def dry_run_resume(plan, build_dir, out_dir, state):
    """Say, in the plan's order, which processes a resume would skip, rename, finish and run."""
    total = len(plan.processes)
    print(f"plan: {total} processes, system {plan.system}, seed {plan.seed}, sha256 {plan.sha256}")
    for index, proc in enumerate(plan.processes, 1):
        if proc.id in state.done:
            print(f"{index}: skip {proc.id} (done: {state.done[proc.id].outcome})")
            continue
        todo = state.pending[proc.id]
        if todo.aside:
            print(f"{index}: rename {proc.id}: " + ", ".join(f"{old} to {new}" for old, new in todo.aside))
        if todo.action == "finish":
            print(f"{index}: finish {proc.id}: {proc.id}{MARKER_SUFFIX} says attempt 1 exited 0; "
                  f"write {proc.id}{DONE_SUFFIX} (ok)")
        elif todo.action == "rerun":
            print(f"{index}: rerun {command_line(proc, build_dir, out_dir, 2)} "
                  f"({proc.id}{MARKER_SUFFIX} says attempt 1 exited {todo.first})")
        else:
            print(f"{index}: run {command_line(proc, build_dir, out_dir, 1)}")
    todo = state.pending.values()
    incomplete = sum(1 for record in state.done.values() if record.outcome == "incomplete")
    print(f"dry run: {total} processes, {len(state.done)} to skip, {sum(1 for t in todo if t.aside)} to rename, "
          f"{sum(1 for t in todo if t.action == 'finish')} to finish from a marker, "
          f"{sum(1 for t in todo if t.action != 'finish')} to run; incomplete among the skipped: {incomplete}")
    return EXIT_OK


def check_host(plan, dry):
    host = host_system()
    if host == plan.system:
        return
    message = f"the plan is for {plan.system}, this host is {host or sys.platform}"
    if not dry:
        raise Refusal(message)
    warn(message)


def check_host_matches(system):
    """Refusal unless this host runs SYSTEM (a plan's, or the host's own for --check-gate with a plan)."""
    host = host_system()
    if host != system:
        raise Refusal(f"the plan is for {system}, this host is {host or sys.platform}")


def real_run(plan, build_dir, out_dir, build_type, state, begun, stop_after_s, gate=None):
    """A first run (state is None) or a resume. `begun` is the time.monotonic() of the invocation.

    Every check has been made by now. The runner pins itself first (a refusal, with nothing written),
    then takes the lock, which is the first thing written, then starts the run."""
    placement = Placement()
    placement.pin()
    try:
        if state is None:
            os.makedirs(out_dir, exist_ok=True)
        with campaign_lock(out_dir, None if state is None else state.stale_lock), sigterm_as_interrupt():
            if state is None:
                started = now()
                write_run_txt(os.path.join(out_dir, RUN_FILE),
                              run_fields(plan, build_dir, out_dir, build_type, started, placement,
                                         None if gate is None else gate.lines, None if gate is None else gate.binaries))
                print(f"running {len(plan.processes)} processes, system {plan.system}, plan sha256 {plan.sha256}")
                done, pending = {}, {}
            else:
                begin_resume(plan, out_dir, state, gate)
                done, pending = state.done, state.pending
                print(f"resuming {len(plan.processes)} processes, system {plan.system}, plan sha256 "
                      f"{plan.sha256}: {len(done)} done, {len(plan.processes) - len(done)} to run")
            stop_at = None if stop_after_s is None else begun + stop_after_s
            # The guard holds the hashes the gate made of the binaries, not new ones, so that a binary put in place between
            # the gate and the first process is seen (dry runs have no gate and hash the files).
            if state is not None:
                digests = state.digests
            elif gate is not None:
                digests = dict(gate.binaries)
            else:
                digests = {tool: sha256_file(tool_path(build_dir, tool)) for tool in TOOLS}
            guard = BinaryGuard(build_dir, digests, os.path.join(out_dir, LOG_FILE))
            incomplete, remaining = run_all(plan, build_dir, out_dir, done, pending, stop_at, placement, guard)
            print(f"results in {out_dir}")
            if remaining:
                log_event(os.path.join(out_dir, LOG_FILE),
                          f"{now()} - STOP stop-after-s={stop_after_s} remaining={remaining}")
                print(f"stopped: {remaining} of {len(plan.processes)} processes not started; "
                      f"incomplete so far: {incomplete}")
                print("continue with --resume (the same --plan, --build-dir and --out-dir)")
                return EXIT_STOPPED
            print(f"incomplete processes: {incomplete}")
            return EXIT_INCOMPLETE if incomplete else EXIT_OK
    finally:
        placement.restore()


def run(argv):
    begun = time.monotonic()  # --stop-after-s counts from here
    args = parse_args(argv)
    if args.check_gate:
        return check_gate_mode(args)
    dry = args.dry_run
    # The lab gate: a timed run is started by the lab runner, which sets LAB_JOB on the laptop.
    if not dry and not os.environ.get("LAB_JOB"):
        raise Refusal("refusing a timed run: LAB_JOB is not set (the lab runner sets it on the laptop); "
                      "use --dry-run to check the plan")
    plan = load_plan(args.plan)
    if args.expect_processes is not None and args.expect_processes != len(plan.processes):
        raise Refusal(f"the plan has {len(plan.processes)} processes, expected {args.expect_processes}")
    check_host(plan, dry)
    check_provenance(plan, dry)
    build_dir = os.path.abspath(args.build_dir)
    out_dir = os.path.abspath(args.out_dir)
    build_type, problems = check_build(build_dir)
    if problems and not dry:
        raise Refusal("; ".join(problems))
    for problem in problems:
        warn(problem)
    state = None
    if args.resume:
        state = check_resume(plan, build_dir, out_dir, dry)
    else:
        check_output_dir(out_dir, plan)
    # The last refusal: nothing has been written yet. A resume skips the banner check of the compiler and wants the identity
    # that run.txt recorded at the start (its binaries are bound to the start by hash).
    gate = check_gate(args, plan, build_dir, dry, None if state is None else state.compiler)
    if dry:
        return dry_run(plan, build_dir, out_dir) if state is None else dry_run_resume(plan, build_dir, out_dir, state)
    return real_run(plan, build_dir, out_dir, build_type, state, begun, args.stop_after_s, gate)


def main(argv=None):
    try:
        return run(argv)
    except SystemExit as err:  # argparse: 2 for a bad command line, 0 for --help
        return err.code if isinstance(err.code, int) else EXIT_OK
    except Refusal as err:
        print(f"run_campaign.py: {err}", file=sys.stderr)
        return EXIT_REFUSED
    except BinaryChanged as err:
        print(f"run_campaign.py: {err}; nothing more was started (see {LOG_FILE})", file=sys.stderr)
        return EXIT_INTERNAL
    except KeyboardInterrupt:
        print("run_campaign.py: interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception:  # the exit code must not be mistaken for "1 process is incomplete"
        traceback.print_exc()
        print("run_campaign.py: internal error, see above", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    # A path with a character the console cannot show must not stop a campaign half way.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    sys.exit(main())

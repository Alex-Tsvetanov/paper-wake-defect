#!/usr/bin/env python3
"""The campaign smoke: one reduced real run of every distinct code shape of the campaign, pilot and H5 runs.

    campaign_smoke.py --build-dir DIR --out FILE [--system linux|windows] [--scratch DIR] [--list]

Why (hypotheses-v2.md, sections 8 and 11 item 7; the Papers repo's rule D5). The CTest suite of a
sanitizer record runs wakeprobe and abpair only as --check, which times nothing. So it runs none of the
measured code of the campaign: calibration, the --out writers, abpair's timed windows, the phase sweep, the
blocking bound, and most of the variant instantiations (every variant is a separate Loop<Backend, D, Hooks>
through `if constexpr`; wakeprobe's checks cover five variants, abpair's the default arms). The 2026-09 records
ran every design of run_matrix at reduced size, which was this step's precedent.

It runs on the laptop only, inside the record job, after CTest and with the record's sanitizer build and
environment (the driver sets ASAN_OPTIONS and the others). The desktop never runs it (no timing there): the
driver's dry run passes --list, which builds and checks the shapes and starts no tool. A real run needs LAB_JOB
(the lab runner sets it) and the laptop's host name for the system.

The shapes are taken from the generators, not listed by hand:
- analysis/v2/plan.py's canonical entries for the system (S, Bt, the phase sweep, H4 and the A/A pilot), with a
  stand-in R;
- H5's cells, from the platform's own runner: the lines that `run_h5.sh --dry-run` (Linux) or
  `run_h5.ps1 -DryRun` (Windows) prints, one wakeprobe command per cell, with the redirections and --out cut off.
They are collapsed to the first entry (by id) of each distinct
    wakeprobe  (backend, variant, workers, protocol, family, calibrates, bound class, gap mode, timer slack, timer period)
    abpair     (backend, arm A, arm B, workers, producers, family, task work or none)
The bound class is blocking (--wait-us 0, the blocking path) or timed; the gap mode is random, fixed or phase; the
slack and the period are the values the line gives (--timerslack-ns, --timer-period-ms) or none. The bound B, the
process number, the gap seed, the sweep's phase target and N's size are values, not shapes. Task work or none is a
shape: abpair reads the clock to spin only when it is asked for.

Each entry is reduced and keeps everything else, the pinning arguments included:
    wakeprobe  --posts at most SMOKE_POSTS and --warmup at most max(SMOKE_WARMUP, workers) (a no_retrigger run with
               several workers needs a warm-up unit per worker); --calibrate, --phase-frac and every other flag as the
               line has them
    abpair     --pairs at most 1 and --window-ms at most SMOKE_WINDOW_MS
Each run has a time limit, the plan generator's own C1 formula on the reduced counts (a chosen bound, not an
estimate).

Every reduced line is first given to the tool itself: `TOOL --validate-args ARGS --out FILE` applies every
check a real run applies before it measures, exits 0 printing `valid` or exits 2 with the real message, and
creates no loop, thread, pin or clock reading and no --out file. Nothing runs unless every line is valid, and a
tool that creates the --out file under --validate-args breaks its contract and fails the smoke. There is no
copy of the tools' rules here to drift from them.

What it keeps. Only the exit code of each run, whether the limit killed it, and the count of sanitizer reports
found in its standard output and error (bench/wakeloop_record.py, is_report). It never reads the tools'
measurements: --out goes to a scratch folder and is deleted after each run, standard output is scanned and
dropped, and no duration is taken or written. Standard error is kept in short form (the first lines and the
report blocks) only for a run that failed.

Output: --out FILE (smoke.json) and one line per run on standard output ("ok" or "FAIL", the id, the exit code,
the report count). Exit 0 when every run exited 0 with no report, 1 when any did not or any line was invalid, 2
when it refused (a missing tool, an unknown system, a missing LAB_JOB, a shape list that cannot be made). The record
writer recomputes the shape list and turns the record red unless the file holds exactly those runs, all green.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import gate_lib
from wakeloop_record import blocks, is_report

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
ANALYSIS = REPO / "analysis" / "v2"
SMOKE_VERSION = 2
SMOKE_POSTS = 5
SMOKE_WARMUP = 2
SMOKE_PAIRS = 1
SMOKE_WINDOW_MS = 200
TOOLS = ("wakeprobe", "abpair")
SYSTEMS = ("linux", "windows")
HOST_TAG = {"linux": "L", "windows": "W"}
EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
STDERR_LINES = 8  # lines of standard error kept for a run that failed
TIMEOUT = "timeout"
VALIDATE_TIMEOUT_S = 60
H5_TIMEOUT_S = 300
H5_FAMILY = "H5"
DRY_LINE = re.compile(r"^\d+: ")


class SmokeError(Exception):
    """A shape or an argument list that cannot be made or run; the message says why."""


@dataclass(frozen=True)
class Shape:
    id: str
    tool: str
    key: tuple
    args: tuple      # the reduced argument list, without --out
    limit_s: int
    plan_id: str     # the plan or H5 entry the shape was taken from


# ---------------------------------------------------------------------------------------------
# The generators
# ---------------------------------------------------------------------------------------------

@lru_cache(maxsize=None)
def load_plan():
    """analysis/v2/plan.py as a module (it puts its own folder on the path for grid, h4log and rng)."""
    path = ANALYSIS / "plan.py"
    if not path.is_file():
        raise SmokeError(f"no plan generator at {path}")
    spec = importlib.util.spec_from_file_location("p1v2_plan", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def flag(args, name: str, default=None):
    """The value after a flag in an argument list."""
    args = list(args)
    return args[args.index(name) + 1] if name in args else default


def split_dry_line(line: str, system: str) -> list[str]:
    """The program and arguments of one line of run_h5's dry run: its number and, on Windows, `cmd.exe /c` cut
    off, and everything from the first `>` on (the redirections)."""
    body = DRY_LINE.sub("", line, count=1)
    if system == "windows":
        tokens = [m.group(1) if m.group(1) is not None else m.group(2) for m in re.finditer(r'"([^"]*)"|(\S+)', body)]
        tokens = tokens[2:] if tokens[:2] == ["cmd.exe", "/c"] else tokens
    else:
        tokens = shlex.split(body)
    return tokens[:tokens.index(">")] if ">" in tokens else tokens


@lru_cache(maxsize=None)
def h5_entries(system: str) -> tuple[dict, ...]:
    """H5's wakeprobe command lines for the system, as plan-like entries sorted by id, taken from the platform's
    own runner in its dry run. SmokeError if the runner is not here or fails (a Windows list needs PowerShell, a
    Linux list bash)."""
    with tempfile.TemporaryDirectory(prefix="h5 dry run ") as tmp:
        build = Path(tmp) / "build"
        (build / "bench").mkdir(parents=True)
        exe = build / "bench" / ("wakeprobe.exe" if system == "windows" else "wakeprobe")
        exe.write_bytes(b"#!/bin/sh\n")  # a shebang: Git for Windows' bash tests -x by the first bytes of the file
        exe.chmod(0o755)
        out = Path(tmp) / "out"
        if system == "windows":
            shell = shutil.which("powershell") or shutil.which("pwsh")
            if not shell:
                raise SmokeError("H5's Windows cells come from run_h5.ps1 in its dry run, and there is no PowerShell here")
            cmd = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(HERE / "run_h5.ps1"), "-BuildDir",
                   str(build), "-OutDir", str(out), "-DryRun"]
        else:
            shell = shutil.which("bash")  # the full path: on a Windows host the bare name may find the WSL launcher first
            if not shell:
                raise SmokeError("H5's Linux cells come from run_h5.sh in its dry run, and there is no bash here")
            # POSIX spelling of the paths: bash on a Windows host (Git for Windows) reads backslashes as escapes.
            cmd = [shell, (HERE / "run_h5.sh").as_posix(), "--build-dir", build.as_posix(), "--out-dir", out.as_posix(),
                   "--dry-run"]
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=H5_TIMEOUT_S, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as err:
            raise SmokeError(f"run_h5's dry run did not run: {err}") from None
    if done.returncode != 0:
        raise SmokeError(f"run_h5's dry run exited {done.returncode}: {(done.stderr or done.stdout).strip()[-300:]}")
    entries = []
    for line in done.stdout.splitlines():
        if not DRY_LINE.match(line):
            continue
        tokens = split_dry_line(line, system)
        args = list(tokens[1:])
        if "--out" not in args:
            raise SmokeError(f"a line of run_h5's dry run names no --out: {line}")
        i = args.index("--out")
        stem = re.sub(r"\.jsonl$", "", re.split(r"[\\/]", args[i + 1])[-1])
        entries.append({"id": stem, "tool": "wakeprobe", "args": args[:i] + args[i + 2:], "cell": stem, "family": H5_FAMILY})
    if not entries:
        raise SmokeError("run_h5's dry run printed no cell")
    return tuple(sorted(entries, key=lambda e: e["id"]))


def canonical_entries(system: str) -> list[dict]:
    """The generators' entries for one system, before any shuffle, with a stand-in R: S and Bt, the phase sweep,
    H4, the pilot, and H5. R only sets how many H4 processes there are, which the collapse drops."""
    plan = load_plan()
    r_by_cell = plan.r_for_system(system, lambda key: plan.grid.H4_R_MIN)
    return (plan.s_bt_entries(system) + plan.sweep_entries(system) + plan.h4_entries(system, r_by_cell)
            + plan.pilot_entries(system) + list(h5_entries(system)))


def shape_key(entry: dict) -> tuple:
    """The distinct code shape of a plan or H5 entry (see the module text)."""
    a = entry["args"]
    if entry["tool"] == "wakeprobe":
        wait = flag(a, "--wait-us")
        gap = ("phase" if "--phase-frac" in a else "random" if "--gap-frac-range" in a
               else "fixed" if "--gap-us" in a else "none")
        return ("wakeprobe", flag(a, "--backend"), flag(a, "--variant"), int(flag(a, "--workers", "1")),
                flag(a, "--protocol", "single"), entry["family"], int(flag(a, "--calibrate", "11")) > 0,
                "blocking" if wait is not None and wait.lstrip("-").isdigit() and int(wait) == 0 else "timed",
                gap, flag(a, "--timerslack-ns"), flag(a, "--timer-period-ms"))
    return ("abpair", flag(a, "--backend"), flag(a, "--arm-a"), flag(a, "--arm-b"), int(flag(a, "--workers", "1")),
            int(flag(a, "--producers", "1")), entry["family"], int(flag(a, "--work-ns", "0")) > 0)


def key_family(key) -> str:
    return key[5] if key[0] == "wakeprobe" else key[6]


def with_flag(args, name: str, value: str) -> list[str]:
    """A copy of ARGS with the value of NAME replaced. SmokeError if the plan no longer has the flag."""
    out = list(args)
    if name not in out:
        raise SmokeError(f"the plan's arguments have no {name}: {' '.join(out)}")
    out[out.index(name) + 1] = value
    return out


def count_of(args, name: str) -> int:
    """The whole number after a flag the plan must have. SmokeError if the plan no longer has it."""
    value = flag(args, name)
    if value is None or not value.isdigit():
        raise SmokeError(f"the plan's arguments have no whole number after {name}: {' '.join(args)}")
    return int(value)


def reduce_args(tool: str, args) -> list[str]:
    """The reduced argument list of a plan entry. Counts only ever go down."""
    if tool == "wakeprobe":
        workers = int(flag(args, "--workers", "1"))
        out = with_flag(args, "--posts", str(min(count_of(args, "--posts"), SMOKE_POSTS)))
        return with_flag(out, "--warmup", str(min(count_of(args, "--warmup"), max(SMOKE_WARMUP, workers))))
    out = with_flag(args, "--pairs", str(min(count_of(args, "--pairs"), SMOKE_PAIRS)))
    return with_flag(out, "--window-ms", str(min(count_of(args, "--window-ms"), SMOKE_WINDOW_MS)))


@lru_cache(maxsize=None)
def shapes(system: str) -> tuple[Shape, ...]:
    """The runs of the smoke for one system, in the generators' canonical order of first appearance."""
    if system not in SYSTEMS:
        raise SmokeError(f"unknown system {system!r}")
    plan = load_plan()
    seen: dict[tuple, Shape] = {}
    for entry in canonical_entries(system):
        key = shape_key(entry)
        if key in seen:
            continue
        args = reduce_args(entry["tool"], entry["args"])
        seen[key] = Shape(id="smoke-%03d-%s" % (len(seen) + 1, entry["id"]), tool=entry["tool"], key=key,
                          args=tuple(args), limit_s=plan.LIMIT_S[entry["tool"]](args), plan_id=entry["id"])
    return tuple(seen.values())


# ---------------------------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------------------------

def launch_prefix(path: str) -> list[str]:
    """The words that start a tool. The tests replace this to run stubs; a real run starts the binary."""
    return [path]


def text_of(data) -> str:
    if data is None:
        return ""
    return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data


def validate_shape(shape: Shape, tool_path: str, scratch: Path) -> dict | None:
    """Gives the reduced line to the tool's own --validate-args. None if the tool says `valid` and exits 0, else
    {"id", "exit", "message"}. A tool that leaves the --out file behind has broken the contract of the flag."""
    out = scratch / (shape.id + ".validate.jsonl")
    cmd = [*launch_prefix(tool_path), "--validate-args", *shape.args, "--out", str(out)]
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=VALIDATE_TIMEOUT_S, stdin=subprocess.DEVNULL)
        code, stdout, stderr = done.returncode, text_of(done.stdout), text_of(done.stderr)
    except subprocess.TimeoutExpired as err:
        code, stdout, stderr = TIMEOUT, text_of(err.stdout), text_of(err.stderr)
    except OSError as err:
        code, stdout, stderr = 127, "", f"cannot start {tool_path}: {err}"
    created = out.exists()
    out.unlink(missing_ok=True)
    if code == 0 and "valid" in stdout.split() and not created:
        return None
    message = " ".join((stderr or stdout).split())[:300]
    if created:
        message = "--validate-args created the --out file; " + message
    return {"id": shape.id, "exit": code, "message": message or "the tool did not say `valid`"}


def run_shape(shape: Shape, tool_path: str, scratch: Path) -> dict:
    """Runs one shape once. Returns what is kept: the exit code (TIMEOUT if the limit killed it), the report
    count, and, for a failed run, a short form of standard error. The output file is deleted, standard output dropped."""
    out = scratch / (shape.id + ".jsonl")
    cmd = [*launch_prefix(tool_path), *shape.args, "--out", str(out)]
    timed_out, stdout, stderr = False, "", ""
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=shape.limit_s, stdin=subprocess.DEVNULL)
        code, stdout, stderr = done.returncode, text_of(done.stdout), text_of(done.stderr)
    except subprocess.TimeoutExpired as err:  # run() has killed the tool and waited for it
        code, timed_out, stdout, stderr = TIMEOUT, True, text_of(err.stdout), text_of(err.stderr)
    except OSError as err:
        code, stderr = 127, f"cannot start {tool_path}: {err}"
    finally:
        for leftover in scratch.glob(shape.id + ".*"):
            leftover.unlink(missing_ok=True)
    reports = sum(1 for text in (stdout, stderr) for line in text.splitlines() if is_report(line))
    result = {"id": shape.id, "exit": code, "timed_out": timed_out, "reports": reports}
    if code != 0 or reports > 0:
        result["report_blocks"] = [b for text in (stdout, stderr) for b in blocks(text)][:3]
        result["stderr_head"] = [line[:300] for line in stderr.splitlines()[:STDERR_LINES]]
    return result


def tool_path(build_dir: Path, tool: str) -> Path:
    return build_dir / "bench" / (tool + EXE_SUFFIX)


def host_name() -> str:
    """The host name as the drivers and the lab give it: COMPUTERNAME on Windows (platform.node() may differ from it
    in case), the node name elsewhere."""
    if sys.platform == "win32":
        return os.environ.get("COMPUTERNAME") or platform.node()
    return platform.node()


def host_problems(system: str) -> list[str]:
    """Why this process is not the laptop's system for SYSTEM (empty when it is): the platform and the host name,
    which is compared without regard to case."""
    problems = []
    if {"linux": "linux", "win32": "windows"}.get(sys.platform) != system:
        problems.append(f"this host is {sys.platform}, not {system}")
    elif not gate_lib.is_lab_host(HOST_TAG[system], host_name()):
        problems.append(f"the host {host_name()!r} is not the laptop's {system} "
                        f"({', '.join(gate_lib.LAB_HOSTS[HOST_TAG[system]])}); the smoke is never run on the desktop")
    return problems


def summary(system: str, mode: str, planned, runs: list[dict], validate_failures: list[dict]) -> dict:
    return {"smoke": SMOKE_VERSION, "system": system, "mode": mode, "stand_in_r": load_plan().grid.H4_R_MIN,
            "reduced": {"posts": SMOKE_POSTS, "warmup": SMOKE_WARMUP, "pairs": SMOKE_PAIRS, "window_ms": SMOKE_WINDOW_MS},
            "planned": len(planned), "ran": len(runs),
            "failed": sum(1 for r in runs if r["exit"] != 0 or r["reports"] > 0) + len(validate_failures),
            "reports": sum(r["reports"] for r in runs),
            "validate_failures": validate_failures,
            "limit_s_total": sum(s.limit_s for s in planned),
            "by_tool": {t: sum(1 for s in planned if s.tool == t) for t in TOOLS},
            "shapes": [{"id": s.id, "tool": s.tool, "plan_id": s.plan_id, "key": list(s.key), "args": list(s.args),
                        "limit_s": s.limit_s} for s in planned],
            "runs": runs}


def default_system() -> str | None:
    return {"linux": "linux", "win32": "windows"}.get(sys.platform)


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")


def refuse(message: str) -> int:
    print(f"campaign_smoke: refused: {message}", file=sys.stderr)
    return 2


def run_all(planned, build_dir: Path, scratch: Path, system: str, out: Path) -> int:
    """Validates every line, then runs every shape. Returns the exit code; smoke.json is written either way."""
    invalid = [f for s in planned if (f := validate_shape(s, str(tool_path(build_dir, s.tool)), scratch))]
    for f in invalid:
        print(f"INVALID {f['id']} exit={f['exit']}: {f['message']}", flush=True)
    if invalid:
        write(out, summary(system, "run", planned, [], invalid))
        print(f"smoke: {len(invalid)} of {len(planned)} lines were refused by the tools' --validate-args; nothing ran")
        return 1
    runs = []
    for s in planned:
        result = run_shape(s, str(tool_path(build_dir, s.tool)), scratch)
        runs.append(result)
        print(f"{'FAIL' if result['exit'] != 0 or result['reports'] else 'ok'} {s.id} exit={result['exit']} "
              f"reports={result['reports']}", flush=True)
        for block in result.get("report_blocks", []):
            print(block, flush=True)
    data = summary(system, "run", planned, runs, [])
    write(out, data)
    print(f"smoke: planned {data['planned']}, ran {data['ran']}, failed {data['failed']}, reports {data['reports']}")
    return 0 if data["failed"] == 0 and data["ran"] == data["planned"] else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-dir", type=Path, help="the sanitizer build of the record (not needed with --list)")
    ap.add_argument("--out", type=Path, required=True, help="where smoke.json goes")
    ap.add_argument("--system", choices=SYSTEMS, default=default_system())
    ap.add_argument("--scratch", type=Path, help="where the tools' --out files go while they run (default beside --out)")
    ap.add_argument("--list", action="store_true", help="build and check the shapes and run nothing (the desktop's dry run)")
    a = ap.parse_args(argv)
    try:
        planned = shapes(a.system)
    except SmokeError as err:
        return refuse(str(err))
    if a.list:
        for s in planned:
            print(f"list {s.id} limit={s.limit_s}: {s.tool} {' '.join(s.args)}")
        write(a.out, summary(a.system, "list", planned, [], []))
        print(f"smoke: {len(planned)} shapes listed for {a.system}, nothing run")
        return 0
    if not os.environ.get("LAB_JOB"):
        return refuse("a real run needs LAB_JOB (the lab runner sets it on the laptop); use --list to check the shapes")
    problems = host_problems(a.system)
    if problems:
        return refuse("; ".join(problems))
    if a.build_dir is None:
        return refuse("--build-dir is required unless --list")
    missing = [str(tool_path(a.build_dir, t)) for t in TOOLS if not tool_path(a.build_dir, t).is_file()]
    if missing:
        return refuse(f"no {', '.join(missing)}")
    scratch = a.scratch or a.out.parent / "smoke-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        return run_all(planned, a.build_dir, scratch, a.system, a.out)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

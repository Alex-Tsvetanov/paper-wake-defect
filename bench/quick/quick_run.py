"""Runs one P1 quick plan on the laptop (quick-plan.md). Release builds only; one campaign of at most 600 s.

    quick_run.py --plan NAME --quick SHA256 --commit SHA --build-dir DIR --out-dir DIR [--dry-run]

Before anything is timed it refuses (exit 2) unless: the plan file bench/quick/plans/NAME.jsonl has the
sha256 given (CRLF read as LF), HEAD is --commit, the guarded code paths equal the campaign's code
(quick_common.GUARDED_PATHS at MEASURED_COMMIT), the plan's system is this one, the build's
compile_commands.json shows wakeprobe.cpp built at -O3 (GCC, Clang) or /O2 (MSVC) and no other level,
and, for a plan with the power condition, the power settings can be read and written.

It then runs the rows in the plan's order. A condition block starts its condition (load: one pinned
spinning process per load CPU; power: the host's own power settings), runs its rows, and stops the
condition in a finally. Before each round after the first it stops if the time used plus the mean round
so far would pass 600 s (the realized rounds are recorded). A wakeprobe process that fails is recorded with
its exit code and not rerun. A condition that does not start (it sets back what it changed) skips only its
block, which is recorded; a condition that cannot be set back ends the campaign. Output in --out-dir: units/<id>.jsonl (wakeprobe's records), units/<id>.out
(its stdout and stderr), job.json and validate.txt. Exit 0 when every process exited 0, 3 when one did not
(its data is kept), 2 for a refusal. --dry-run checks the plan and prints the commands, and runs nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quick_common as qc  # noqa: E402
import quick_power  # noqa: E402

RAMP_S = 2.0  # after a condition starts, before its first row
HOG_READY_S = 30.0


class Refusal(Exception):
    pass


def this_os() -> str:
    return "windows" if platform.system() == "Windows" else "linux"


def read_plan(name: str, sha: str, plans_dir: Path = qc.PLANS_DIR) -> tuple[dict, list[dict]]:
    path = plans_dir / f"{name}.jsonl"
    if path.parent.resolve() != plans_dir.resolve() or not path.is_file():
        raise Refusal(f"no plan {name} in {plans_dir}")
    got = qc.file_sha256(path)
    if got != sha:
        raise Refusal(f"plan {name} has sha256 {got}, not {sha}")
    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    rows = [json.loads(x) for x in lines[1:] if x.strip()]
    if header.get("plan") != name or not rows:
        raise Refusal(f"plan {name}: header or rows missing")
    return header, rows


def git(*args: str, cwd: Path = qc.REPO) -> subprocess.CompletedProcess:
    exe = os.environ.get("LAB_GIT", "git")
    return subprocess.run([exe, "-C", str(cwd), *args], capture_output=True, text=True, timeout=60)


def check_code(commit: str) -> None:
    head = git("rev-parse", "HEAD")
    if head.returncode != 0 or head.stdout.strip() != commit:
        raise Refusal(f"HEAD is {head.stdout.strip() or head.stderr.strip()}, not {commit}")
    diff = git("diff", "--quiet", qc.MEASURED_COMMIT, "HEAD", "--", *qc.GUARDED_PATHS)
    if diff.returncode != 0:
        raise Refusal(f"the guarded code paths differ from {qc.MEASURED_COMMIT} (git diff exit {diff.returncode})")


OTHER_LEVELS = {"linux": ("-O0", "-O1", "-O2", "-Os", "-Oz", "-Og", "-Ofast"), "windows": ("/Od", "/O1", "-Od", "-O1")}
WANT_LEVEL = {"linux": "-O3", "windows": "/O2"}


def check_release(build_dir: Path, os_name: str) -> dict:
    """Rule 1: wakeprobe.cpp at -O3 (GCC, Clang) or /O2 (MSVC), and no other optimization level."""
    cc = build_dir / "compile_commands.json"
    if not cc.is_file():
        raise Refusal(f"no {cc}: configure with -DCMAKE_EXPORT_COMPILE_COMMANDS=ON")
    entries = [e for e in json.loads(cc.read_text(encoding="utf-8"))
               if Path(e.get("file", "")).name == "wakeprobe.cpp"]
    if not entries:
        raise Refusal("compile_commands.json has no wakeprobe.cpp")
    cmd = entries[0].get("command") or " ".join(entries[0].get("arguments", []))
    words = cmd.split()
    if WANT_LEVEL[os_name] not in words and WANT_LEVEL[os_name].replace("/", "-") not in words:
        raise Refusal(f"wakeprobe.cpp is not built with {WANT_LEVEL[os_name]}: {cmd[:300]}")
    other = [w for w in words if w in OTHER_LEVELS[os_name]]
    if other:
        raise Refusal(f"wakeprobe.cpp carries another optimization level {other}")
    exe = build_dir / "bench" / ("wakeprobe.exe" if os_name == "windows" else "wakeprobe")
    if not exe.is_file():
        raise Refusal(f"no {exe}")
    return {"wakeprobe": str(exe), "level": WANT_LEVEL[os_name], "command": cmd}


class Load:
    """One quick_hog.py process per load CPU, started and checked, then stopped in stop()."""

    def __init__(self, cpus: list[int], python: str = sys.executable):
        self.cpus, self.python, self.procs = cpus, python, []

    def start(self) -> dict:
        hog = str(Path(__file__).resolve().parent / "quick_hog.py")
        for c in self.cpus:
            self.procs.append(subprocess.Popen([self.python, "-I", "-B", hog, "--cpu", str(c)],
                                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
        ready = []
        deadline = time.monotonic() + HOG_READY_S
        for c, p in zip(self.cpus, self.procs):
            line = p.stdout.readline().strip() if time.monotonic() < deadline else ""
            if line != f"ready {c}":
                self.stop()
                raise RuntimeError(f"hog on CPU {c} did not start: {line!r}")
            ready.append(c)
        return {"hog_cpus": ready, "hog_pids": [p.pid for p in self.procs]}

    def stop(self) -> dict:
        codes = []
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=10)
            codes.append(p.returncode)
        self.procs = []
        return {"hog_exit": codes}


class StartError(Exception):
    """A condition that did not start; it set back what it had changed, so only its block is skipped."""


class Condition:
    def __init__(self, name: str, header: dict, power=None):
        self.name, self.header, self.power = name, header, power
        self.state: dict = {}

    def __enter__(self) -> dict:
        rec: dict = {"condition": self.name, "start": time.time()}
        try:
            if self.name == "load":
                self.load = Load(self.header["load_cpus"])
                rec.update(self.load.start())
            elif self.name == "power":
                self.state = self.power.apply()
                rec["power"] = self.state
        except (RuntimeError, OSError) as e:  # PowerError is a RuntimeError; apply() and start() set back first
            raise StartError(f"{type(e).__name__}: {e}") from e
        if self.name != "quiet":
            time.sleep(RAMP_S)
        self.rec = rec
        return rec

    def __exit__(self, *exc) -> None:
        if self.name == "load":
            self.rec.update(self.load.stop())
        elif self.name == "power" and self.state:
            self.rec["power_restore"] = self.power.restore(self.state)
        self.rec["end"] = time.time()


def run_row(exe: str, row: dict, out_dir: Path) -> dict:
    units = out_dir / "units"
    units.mkdir(parents=True, exist_ok=True)
    rec_path = units / f"{row['id']}.jsonl"
    cmd = [exe, *row["args"], "--out", str(rec_path)]
    t0 = time.monotonic()
    limit = 10 * row["est_s"] + 60
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=limit)
        rc, text = p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        rc, text = -9, f"timeout after {limit:.0f} s: {e}"
    (units / f"{row['id']}.out").write_text(text, encoding="utf-8")
    return {"id": row["id"], "rc": rc, "wall_s": round(time.monotonic() - t0, 3)}


def run_plan(header: dict, rows: list[dict], exe: str, out_dir: Path, power, budget_s: float = qc.BUDGET_S,
             runner=run_row, clock=time.monotonic) -> dict:
    job: dict = {"plan": header["plan"], "rounds_planned": header["rounds"], "rounds_run": 0, "cut": None,
                 "blocks": [], "units": []}
    t_start = clock()
    try:
        _run_rounds(header, rows, exe, out_dir, power, budget_s, runner, clock, job, t_start)
    except Exception as e:  # kept with what ran so far; every condition was stopped by its finally
        job["error"] = f"{type(e).__name__}: {e}"
    job["wall_s"] = round(clock() - t_start, 3)
    return job


def _run_rounds(header, rows, exe, out_dir, power, budget_s, runner, clock, job, t_start) -> None:
    round_walls: list[float] = []
    for rnd in range(header["rounds"]):
        elapsed = clock() - t_start
        if round_walls and elapsed + sum(round_walls) / len(round_walls) > budget_s:
            job["cut"] = f"cut rounds {rnd} of {header['rounds']}: {elapsed:.1f} s used"
            break
        r0 = clock()
        rrows = [r for r in rows if r["round"] == rnd]
        for block in sorted({r["block"] for r in rrows}):
            brows = [r for r in rrows if r["block"] == block]
            cond = Condition(brows[0]["condition"], header, power)
            try:
                with cond:
                    for row in brows:
                        job["units"].append(dict(runner(exe, row, out_dir), round=rnd, condition=cond.name))
            except StartError as e:
                # Only this block is skipped. A failed restore is not a StartError: it ends the campaign, since
                # later blocks would run on the wrong settings.
                job["blocks"].append({"condition": cond.name, "round": rnd, "block": block, "start_error": str(e),
                                      "skipped": [r["id"] for r in brows]})
                continue
            job["blocks"].append(dict(cond.rec, round=rnd, block=block))
        round_walls.append(clock() - r0)
        job["rounds_run"] = rnd + 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one P1 quick plan (quick-plan.md).")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--quick", required=True, help="the plan file's sha256")
    ap.add_argument("--commit", required=True)
    ap.add_argument("--build-dir", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        header, rows = read_plan(a.plan, a.quick)
        if header["os"] != this_os():
            raise Refusal(f"plan {a.plan} is for {header['os']}, this is {this_os()}")
        if a.dry_run:
            for r in rows:
                print(" ".join(["wakeprobe", *r["args"]]))
            print(f"dry run: {len(rows)} processes, {header['rounds']} rounds, nothing run")
            return 0
        check_code(a.commit)
        build = check_release(a.build_dir, header["os"])
        power = None
        pre = None
        if "power" in header["conditions"]:
            power = quick_power.for_os(header["os"])
            pre = power.precheck()
    except (Refusal, quick_power.PowerError, OSError, ValueError) as e:
        print(f"quick_run: refused: {e}", file=sys.stderr)
        return 2
    a.out_dir.mkdir(parents=True, exist_ok=True)
    job = {"header": header, "quick_sha256": a.quick, "commit": a.commit, "build": build, "power_precheck": pre,
           "lab_quiet": os.environ.get("LAB_QUIET"), "started": time.time()}
    job.update(run_plan(header, rows, build["wakeprobe"], a.out_dir, power))
    rc = 3 if job.get("error") else 0
    job["ended"] = time.time()
    failed = [u for u in job.get("units", []) if u["rc"] != 0]
    skipped = [b for b in job.get("blocks", []) if b.get("start_error")]
    if failed or skipped:
        rc = 3
    (a.out_dir / "job.json").write_text(json.dumps(job, indent=1), encoding="utf-8")
    lines = [f"plan {a.plan} rounds {job.get('rounds_run', 0)} of {header['rounds']}",
             f"processes {len(job.get('units', []))} of {len(rows)}, failed {len(failed)}"]
    if job.get("cut"):
        lines.append(job["cut"])
    if job.get("error"):
        lines.append(f"error {job['error']}")
    lines += [f"failed {u['id']} rc {u['rc']}" for u in failed]
    lines += [f"skipped block {b['condition']} round {b['round']}: {b['start_error']}" for b in skipped]
    (a.out_dir / "validate.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())

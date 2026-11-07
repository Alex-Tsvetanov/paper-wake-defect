"""Writes the laptop requests of the P1 quick plans (quick-plan.md), one file per job.

    python bench/quick/quick_requests.py --commit SHA --out-dir DIR [--queue-dir DIR]

Each request is one labrun job entry plus the request-only keys of lab/pipeline/README.md (stage_after,
consumer, expected_basis). With --queue-dir it also writes the matching queue drafts, without the
request-only keys, named with a placeholder gap number that the orchestrator replaces. It times nothing.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quick_common as qc  # noqa: E402

REPO = {"name": "wake-defect", "path": "p1-wake-defect/wake-defect"}
WHAT = {"R1": "false positives of the correct loops quiet, under load and on the default power plan",
        "R2": "the effective bound calibrated on the correct loop, against the absent-wake value",
        "R3": "Windows single posts with gaps in units of the timer period"}
PLAIN = {"R1": "the fixed loop and the plain wake, quiet, under processor load and on the default power plan",
         "R2": "the wake period measured on the fixed loop with nothing posted, beside the planted variant's",
         "R3": "single posts on Windows with gaps spread over the whole timer period"}


def build_cmd(os_name: str) -> str:
    if os_name == "linux":
        return ("cmake -S \"$LAB_SRC\" -B \"$LAB_JOB/build-rel\" -G Ninja -DCMAKE_CXX_COMPILER=clang++ "
                "-DCMAKE_BUILD_TYPE=Release -DCMAKE_EXPORT_COMPILE_COMMANDS=ON && "
                "cmake --build \"$LAB_JOB/build-rel\" --target wakeprobe")
    return ("call \"%LAB_VCVARS%\" >nul && \"%LAB_CMAKE%\" -S \"%LAB_SRC%\" -B \"%LAB_JOB%\\build-rel\" -G Ninja "
            "-DCMAKE_CXX_COMPILER=cl -DCMAKE_BUILD_TYPE=Release -DCMAKE_EXPORT_COMPILE_COMMANDS=ON "
            "-DCMAKE_MAKE_PROGRAM=\"%LAB_NINJA%\" && \"%LAB_CMAKE%\" --build \"%LAB_JOB%\\build-rel\" --target wakeprobe")


def run_cmd(os_name: str, plan: str, sha: str, commit: str) -> str:
    if os_name == "linux":
        return (f"\"$LAB_PYTHON\" -I -B \"$LAB_SRC/bench/quick/quick_run.py\" --plan {plan} --quick {sha} "
                f"--commit {commit} --build-dir \"$LAB_JOB/build-rel\" --out-dir \"$LAB_OUT\"")
    return ("set \"PATH=C:\\Program Files\\Git\\cmd;%PATH%\"&& set \"LAB_GIT=C:\\Program Files\\Git\\cmd\\git.exe\"&& "
            f"\"%LAB_PYTHON%\" -I -B \"%LAB_SRC%\\bench\\quick\\quick_run.py\" --plan {plan} --quick {sha} "
            f"--commit {commit} --build-dir \"%LAB_JOB%\\build-rel\" --out-dir \"%LAB_OUT%\"")


def job(name: str, commit: str) -> dict:
    spec = qc.SPECS[name]
    sha = qc.file_sha256(qc.PLANS_DIR / f"{name}.jsonl")
    trials = qc.trials_for(spec)
    total = spec.rounds * qc.round_estimate_s(spec, trials)
    jid = "p1q-" + name.removeprefix("quick-").lower()
    note = (f"quick-plan.md, decision d010 option B, run {spec.run}. A timed campaign of at most 600 s with its plan's "
            "sha256 pinned (--quick); the runner cuts rounds to stay inside 600 s. The build phase is untimed; exit 2 "
            "is a refusal before anything is timed and is never rerun with labrun. Branch quick-benchmarks of "
            "wake-defect is local and unpushed at the time of this request: it must be on the remote (Alex's "
            "standing yes, rule 14) before the orchestrator queues it.")
    if spec.run == "R1":
        note += (" The run starts its own processor load (pinned spinning processes, stopped at each block's end) "
                 "after the lab's quiet check, and for its power blocks puts back the host's own power settings "
                 "from the lab's records and restores the lab's after each block.")
    return {
        "id": jid, "paper": "P1", "os": spec.os, "repo": dict(REPO), "commit": commit,
        "build": build_cmd(spec.os), "run": run_cmd(spec.os, name, sha, commit),
        "outputs": ["out", "record.json"],
        "expected_s": int(math.ceil(total)), "poll_s": 60, "timeout_s": 2400, "build_timeout_s": 1800,
        "quiet": True, "boost": False,
        "run_label": f"P1 quick benchmark {name} at {commit[:12]}",
        "note": note,
        "plain": f"Run P1's quick benchmark: {PLAIN[spec.run]}",
    }


def request(name: str, commit: str) -> dict:
    spec = qc.SPECS[name]
    j = job(name, commit)
    j.update({
        "stage_after": "analyse",
        "consumer": f"P1 results-analyst on the Release quick archive (run {spec.run}, decision d010, quick-plan.md "
                    "on branch quick-benchmarks)",
        "expected_basis": ("derived: seconds per trial from the measured 100-trial S cells (hypotheses-v2.md "
                           "section 9) and the Bt probe of 5e0fc7e (STATE.md), plus per-process and per-block "
                           "allowances with no measured basis; R3's timer-unit gaps are arithmetic only; "
                           f"{spec.rounds} rounds x {qc.round_estimate_s(spec, qc.trials_for(spec)):.1f} s"),
    })
    return {"about": f"P1 quick benchmark {j['id']} (decision d010 option B, run {spec.run}: {WHAT[spec.run]}). "
                     "Laptop request written by the P1 quick session, 2026-11-07; one OS at a time, queue order "
                     "by the orchestrator.", "jobs": [j]}


def queue(name: str, commit: str) -> dict:
    spec = qc.SPECS[name]
    j = job(name, commit)
    return {"about": f"P1 quick benchmark (decision d010, option B), 2026-11-07: one campaign of at most 10 minutes, "
                     f"{j['id']} (run {spec.run}).", "jobs": [j]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--commit", required=True)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--queue-dir", type=Path)
    a = ap.parse_args(argv)
    if len(a.commit) != 40:
        print("--commit must be a full 40-hex commit", file=sys.stderr)
        return 2
    a.out_dir.mkdir(parents=True, exist_ok=True)
    order = {"linux": "a", "windows": "w"}
    for i, name in enumerate(qc.SPECS, start=1):
        spec = qc.SPECS[name]
        jid = job(name, a.commit)["id"]
        (a.out_dir / f"p1-{jid}.json").write_text(json.dumps(request(name, a.commit), indent=1) + "\n",
                                                  encoding="utf-8")
        if a.queue_dir:
            a.queue_dir.mkdir(parents=True, exist_ok=True)
            n = sum(1 for m in list(qc.SPECS)[:i] if qc.SPECS[m].os == spec.os)
            path = a.queue_dir / f"gapNN-{order[spec.os]}-q{n:02d}-{jid}.json"
            path.write_text(json.dumps(queue(name, a.commit), indent=1) + "\n", encoding="utf-8")
        print(f"{jid}: {spec.os} expected_s {job(name, a.commit)['expected_s']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

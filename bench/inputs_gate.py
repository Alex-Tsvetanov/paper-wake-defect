#!/usr/bin/env python3
"""What the fixed and defect builds compiled, and the gate against the sanitizer records.

    inputs_gate.py --inputs-hash PATH --build fixed=DIR --build defect=DIR --out inputs.json
                   [--expect gate.json]

Runs the Papers mono-repo's lab/bin/inputs_hash.py in project mode over the targets wakeloop and
wakeprobe, with the options that select compiled code (WAKELOOP_DEFECT, WAKELOOP_BACKENDS), and
writes inputs.json. Then checks:

  - coverage: each build's wakeloop inputs include every loop source this platform compiles and
    the public header. Without this, a layout that left the loop outside the first-party root
    would hash no loop file, and the hash would gate any code;
  - arms: the two builds differ in WAKELOOP_DEFECT and in nothing else of the configuration;
  - with --expect (a gate.json from check_records.py): each build matches its own arm's
    inputs_hash and configuration in the records (inputs_hash.py --expect, exit 3 otherwise).

Exits 0 when all checks pass, 1 on a failed check, 3 when the builds do not match the records.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

TARGETS = ("wakeloop", "wakeprobe")
CONFIG_KEYS = ("WAKELOOP_DEFECT", "WAKELOOP_BACKENDS")
FOUND_KEYS = ("WAKELOOP_MSAN_LIBCXX",)
# The loop sources each platform compiles into the wakeloop target, as inputs_hash.py names them.
LOOP_FILES = {
    "epoll;io_uring": ["project/loop/src/common.cpp", "project/loop/src/epoll.cpp", "project/loop/src/uring.cpp",
                       "project/loop/src/common.hpp", "project/loop/src/linux_fd.hpp",
                       "project/loop/include/wakeloop/loop.hpp", "project/loop/include/wakeloop/task_stack.hpp"],
    "iocp": ["project/loop/src/common.cpp", "project/loop/src/iocp.cpp", "project/loop/src/common.hpp",
             "project/loop/include/wakeloop/loop.hpp", "project/loop/include/wakeloop/task_stack.hpp"],
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inputs-hash", type=Path, required=True)
    ap.add_argument("--build", action="append", required=True, help="ARM=DIR for fixed and defect")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--expect", type=Path)
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        listing = Path(tmp) / "inputs.tsv"
        cmd = [sys.executable, str(args.inputs_hash)]
        for spec in args.build:
            cmd += ["--build", spec]
        for t in TARGETS:
            cmd += ["--target", t]
        for k in CONFIG_KEYS:
            cmd += ["--config-key", k]
        for k in FOUND_KEYS:
            cmd += ["--found-key", k]
        cmd += ["--out", str(args.out), "--list", str(listing)]
        done = subprocess.run(cmd, check=False)
        if done.returncode != 0:
            print("inputs_gate: inputs_hash.py failed", file=sys.stderr)
            return 1
        lines = [line.split("\t") for line in listing.read_text(encoding="utf-8").splitlines() if line]

    data = json.loads(args.out.read_text(encoding="utf-8"))
    problems = []
    for arm, info in data["builds"].items():
        backends = info["config"]["WAKELOOP_BACKENDS"]
        want = LOOP_FILES.get(backends)
        if want is None:
            problems.append(f"{arm}: unknown backend set {backends}")
            continue
        have = {path.lower() for build, target, path, _ in lines if build == arm and target == "wakeloop"}
        missing = [f for f in want if f.lower() not in have]
        if missing:
            problems.append(f"{arm}: the wakeloop inputs lack {', '.join(missing)}")
    configs = {arm: info["config"] for arm, info in data["builds"].items()}
    if set(configs) == {"fixed", "defect"}:
        fixed, defect = configs["fixed"], configs["defect"]
        if fixed.get("WAKELOOP_DEFECT", "").upper() not in ("OFF", "0", "FALSE"):
            problems.append(f"fixed: WAKELOOP_DEFECT is {fixed.get('WAKELOOP_DEFECT')}")
        if defect.get("WAKELOOP_DEFECT", "").upper() not in ("ON", "1", "TRUE"):
            problems.append(f"defect: WAKELOOP_DEFECT is {defect.get('WAKELOOP_DEFECT')}")
        others = [k for k in CONFIG_KEYS if k != "WAKELOOP_DEFECT" and fixed.get(k) != defect.get(k)]
        if others:
            problems.append(f"the arms differ in {', '.join(others)}")
    for p in problems:
        print(f"inputs_gate: {p}", file=sys.stderr)
    if problems:
        return 1
    print(f"inputs_gate: coverage and arms ok ({', '.join(sorted(configs))})")

    if args.expect:
        cmd = [sys.executable, str(args.inputs_hash)]
        for spec in args.build:
            cmd += ["--build", spec]
        for t in TARGETS:
            cmd += ["--target", t]
        for k in CONFIG_KEYS:
            cmd += ["--config-key", k]
        for k in FOUND_KEYS:
            cmd += ["--found-key", k]
        with tempfile.TemporaryDirectory() as tmp:
            cmd += ["--expect", str(args.expect), "--out", str(Path(tmp) / "again.json")]
            done = subprocess.run(cmd, check=False)
        if done.returncode != 0:
            print("inputs_gate: the builds do not match the sanitizer records", file=sys.stderr)
            return 3
        print("inputs_gate: both builds match the sanitizer records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

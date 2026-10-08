#!/usr/bin/env python3
"""Write a sanitizer record for the wakeloop build from a run_matrix output directory.

Called by sanitize_wakeprobe.sh and sanitize_wakeprobe.ps1 after a run_matrix pass under the
sanitizer with the loop's tests (BUILD_TESTS=1, RUN_CTEST=1) and every design at reduced size.

The record has one section per arm (fixed, defect). An arm is green when:
  - every test in its JUnit file ran and passed, and there are as many as its build registers;
  - the wake detectors reported what the arm declares: no DETECTED line in the fixed arm, and a
    DETECTED line from every detector of every backend in the defect arm (the seeded defect is
    found by each of them);
  - every planned probe cell of the arm exited 0 and wrote a summary without an error.
The record is green when both arms are, run_matrix exited 0, and the full output of every test
and every probe cell (ctest-<arm>.log, summaries.log) holds no sanitizer report. The scan covers
passing tests too: a detector passes in the defect arm on its DETECTED line whatever its exit
status, so a report inside it would otherwise go unseen. Reports are copied into the record.

Each arm carries what its build compiled (inputs.json from inputs_gate.py: inputs_hash per
target, config, third_party) and the compiler CMake identified (build-<arm>.log).
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# The first line of every sanitizer report and every SUMMARY line; the same text as the
# Papers repo's lab/bin/sanitize.sh, checked by its lab/bin/test_report_pattern.sh.
REPORT = re.compile(r"ERROR: (Address|Memory|Leak|Thread)Sanitizer|WARNING: (Memory|Thread)Sanitizer|"
                    r"SUMMARY: [A-Za-z]+Sanitizer|runtime error:")
COMPILER_ID = re.compile(r"^-- The CXX compiler identification is (.+?)\s*$", re.MULTILINE)
DETECTORS = ("detect_blocking_single", "detect_blocking_burst", "detect_blocking_producers",
             "detect_self_post_blocking", "detect_latency21")
SEMANTIC = ("post_runs_once", "post_before_run", "fifo_one_producer", "many_producers", "self_post",
            "stop_before_run", "stop_wakes_blocked", "stop_from_task", "idle_blocking_no_spin",
            "idle_bounded_passes", "defect_is_delay_not_loss")
ARMS = ("fixed", "defect")


def arm_of(file_name: str) -> str:
    return "defect" if "-defect" in file_name else "fixed"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def compiler_of(out: Path, arm: str) -> str:
    match = COMPILER_ID.search(read(out / f"build-{arm}.log"))
    return match.group(1) if match else ""


def junit(path: Path) -> dict:
    """Test names and failures from CTest's JUnit output."""
    if not path.exists():
        return {"error": f"no {path.name}"}
    root = ET.parse(path).getroot()
    cases = root.iter("testcase")
    names, failed = [], []
    for c in cases:
        name = c.get("name", "")
        names.append(name)
        if c.find("failure") is not None or c.find("error") is not None or c.get("status") not in (None, "run"):
            failed.append(name)
    return {"names": names, "failed": failed}


def tests_section(out: Path, arm: str, backends: list[str]) -> dict:
    j = junit(out / f"ctest-{arm}.xml")
    log = read(out / f"ctest-{arm}.log")
    expected_names = [f"{b}.{t}" for b in backends for t in SEMANTIC + DETECTORS] + ["defect_sites"]
    seen = sorted(set(re.findall(r"DETECTED: ([\w.]+):", log)))
    declared = sorted(f"{b}.{t}" for b in backends for t in DETECTORS) if arm == "defect" else []
    section = {"expected_count": len(expected_names), "detections_declared": declared, "detections_seen": seen}
    if "error" in j:
        section.update({"error": j["error"], "green": False})
        return section
    missing = sorted(set(expected_names) - set(j["names"]))
    section.update({"count": len(j["names"]), "passed": len(j["names"]) - len(j["failed"]), "failed": j["failed"],
                    "missing": missing})
    section["green"] = not j["failed"] and not missing and seen == declared
    return section


def probe_section(out: Path, arm: str) -> dict:
    planned = defaultdict(int)
    for line in read(out / "plan.txt").splitlines():
        if line and not line.startswith("#"):
            design, cell_arm = line.split()[:2]
            if cell_arm == arm:
                planned[design] += 1
    failed = defaultdict(list)
    for line in read(out / "failures.txt").splitlines():
        where = line.split(": ", 1)[1]
        design, name = where.split("/", 1)
        if arm_of(name) == arm:
            failed[design].append(line)
    ok = defaultdict(int)
    for f in out.glob("*/*.jsonl"):
        if arm_of(f.name) != arm:
            continue
        lines = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
        if lines and lines[-1].get("summary") and lines[-1].get("error") is None:
            ok[f.parent.name] += 1
    designs = [{"design": d, "planned": planned[d], "ok": ok[d], "failures": failed[d]} for d in sorted(planned)]
    return {"designs": designs, "green": all(d["ok"] == d["planned"] and not d["failures"] for d in designs)}


def report_blocks(text: str) -> list[str]:
    """The report blocks: each from its first matching line to the next blank line."""
    blocks, current = [], []
    for line in text.splitlines():
        if REPORT.search(line) or (current and line.strip()):
            current.append(line)
        elif current:
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))
    return blocks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", type=Path, required=True, help="run_matrix output directory")
    ap.add_argument("--record", type=Path, required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--commit", required=True, help="code commit")
    ap.add_argument("--repo-head", required=True)
    ap.add_argument("--sanitizer", required=True)
    ap.add_argument("--cmake-args", default="")
    ap.add_argument("--options", default="", help="sanitizer runtime options, as NAME=value;...")
    ap.add_argument("--ldflags", default="", help="LDFLAGS the builds were configured with")
    ap.add_argument("--runtime-path", default="", help="directory put first on PATH for the runs")
    ap.add_argument("--matrix-exit", type=int, required=True)
    ap.add_argument("--seconds", type=int, required=True)
    ap.add_argument("--logs-archive", default="", help="tarball of the output directory")
    ap.add_argument("--logs-sha256", default="")
    ap.add_argument("--host", default=socket.gethostname())
    args = ap.parse_args()

    out = args.out_dir
    inputs = json.loads(read(out / "inputs.json") or "{}")
    scanned = ["ctest-fixed.log", "ctest-defect.log", "summaries.log"]
    text = "\n".join(read(out / name) for name in scanned)
    findings = [line for line in text.splitlines() if REPORT.search(line)]

    arms = {}
    for arm in ARMS:
        build = (inputs.get("builds") or {}).get(arm, {})
        backends = (build.get("config") or {}).get("WAKELOOP_BACKENDS", "").split(";")
        backends = [b for b in backends if b]
        tests = tests_section(out, arm, backends)
        probe = probe_section(out, arm)
        section = {"compiler": compiler_of(out, arm),
                   "config": build.get("config"),
                   "inputs_hash": {t: v["inputs_hash"] for t, v in (build.get("targets") or {}).items()},
                   "inputs_files": {t: v["files"] for t, v in (build.get("targets") or {}).items()},
                   "third_party": build.get("third_party"),
                   "tests": tests, "probe": probe}
        section["green"] = bool(section["compiler"] and section["config"] and section["inputs_hash"]
                                and tests["green"] and probe["green"])
        arms[arm] = section

    compilers = list(dict.fromkeys(a["compiler"] for a in arms.values() if a["compiler"]))
    green = args.matrix_exit == 0 and not findings and all(a["green"] for a in arms.values())
    record = {"artifact": "wakeloop", "repo": args.repo, "commit": args.commit, "repo_head": args.repo_head,
              "date": datetime.now().astimezone().isoformat(timespec="seconds"), "host": args.host,
              "sanitizer": args.sanitizer, "compiler": " | ".join(compilers), "green": green,
              "extra_cmake_args": args.cmake_args, "sanitizer_options": args.options, "ldflags": args.ldflags,
              "runtime_path_prepend": args.runtime_path, "matrix_exit": args.matrix_exit, "seconds": args.seconds,
              "inputs_scope": inputs.get("scope", ""), "arms": arms,
              "scanned_logs": scanned, "sanitizer_reports": len(findings), "report_blocks": report_blocks(text),
              "logs_dir": str(out), "logs_archive": args.logs_archive, "logs_sha256": args.logs_sha256}
    args.record.parent.mkdir(parents=True, exist_ok=True)
    args.record.write_text(json.dumps(record) + "\n", encoding="utf-8")
    print(json.dumps({k: record[k] for k in ("commit", "sanitizer", "compiler", "green", "sanitizer_reports")}))
    for arm, a in arms.items():
        t = a["tests"]
        print(f"  {arm}: {'green' if a['green'] else 'red'}; tests {t.get('passed', '?')} of {t.get('count', '?')} "
              f"passed (expected {t['expected_count']}), detections {len(t['detections_seen'])} of "
              f"{len(t['detections_declared'])} declared; probe "
              + ", ".join(f"{d['design']} {d['ok']}/{d['planned']}" for d in a["probe"]["designs"]))
    return 0 if green else 1


if __name__ == "__main__":
    raise SystemExit(main())

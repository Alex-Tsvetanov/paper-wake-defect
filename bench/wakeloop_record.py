#!/usr/bin/env python3
# Derived from paper-io-core bench/iocore_record.py (itself from paper-one-port bench/oneport_record.py
# at commit 1f27ea8), same author (Apache-2.0).
# Changed: wakeloop's names and sections; CTest's JUnit file decides which tests ran and passed; the
# record checks that every backend of the build has its wake matrix, wakeprobe --check and abpair
# --check tests (the 2026-09 bench/sanitizer_record.py counted its tests the same way); the campaign
# smoke; the code fingerprint and the generator files; no link orders.
"""Write the sanitizer record of a wakeloop build from a work directory.

    wakeloop_record.py --work DIR --record FILE --repo URL --commit SHA --repo-head SHA --fingerprint-start HEX
                       --sanitizer asan|tsan|msan --cmake-args ARGS --options OPTS --host UNAME --host-tag L|W
                       --build-exit N --inputs-exit N --ctest-exit N --smoke-exit N --seconds N
                       --logs-archive F --logs-sha256 H [--ctest-jobs N] [--dry-run]
(F and H may be "-": no archive)

A record has three steps: the build of the whole CMake project under the sanitizer, the whole CTest
suite, and the campaign smoke (hypotheses-v2.md, section 11 item 7; the Papers repo's rule D5). The suite
holds the wake matrix of every backend, wakeprobe --check and abpair --check on every backend, the pinning
and failure checks, and the Python tests. Nothing in it is timed, so it runs none of the measured code. The
smoke (bench/campaign_smoke.py) runs one reduced real invocation of every distinct code shape of the campaign,
pilot and H5 runs under the same sanitizer build. Both are only ever run by a record job on the laptop;
nothing here reads a measurement.

The work directory holds build.log (configure and build), ctest.log (ctest -V: the whole output of
every test, so a report inside a test that passes on a regular expression is seen), ctest.xml (the
same run as JUnit), tests.txt (ctest -N before the run: the registered tests), build.inputs.json
(bench/build_inputs.py) and smoke.json and smoke.log (bench/campaign_smoke.py: exit codes and report
counts only). The record names the repo, the commit, the host, the compiler (CMake's identification, which
compiled the binaries, and today's banner beside it), the record format, the sanitizer and its runtime options (every *SAN_OPTIONS the run saw), the CMake arguments, what
each target compiled (inputs_hash, inputs_files, the configuration with its CMake files' hash, third_party),
the code fingerprint, the sha256 of each generator file, and the logs archive with its sha256.

Green means: the build succeeded; build_inputs.py exited 0 and its file holds the host, the compiler and the
file hashes; CTest exited 0; the JUnit file lists exactly the registered tests, each run and passed; every
backend of the build has its required tests (required_tests below) among them; the smoke exited 0 and ran
exactly the shapes that bench/campaign_smoke.py computes for the system now, each with exit 0 and no report;
the commit and the code fingerprint at the end are those at the start, and the tree is clean at the end; the
logs archive and its sha256 are named; and no log or standard error file of the work directory holds a
sanitizer report. Reports are copied into the record. A dry run (the desktop's test of the drivers) runs the
smoke with --list, which runs no tool: only a dry run may hold a smoke of that mode, and it may have a dirty tree.

Format and red reasons. Every record carries "format" (build_inputs.RECORD_FORMAT: the gate wants the current one
and skips an older record with a note), "red_reasons" (for each reason a record is red, its class and a detail) and
"blocking". A reason is of one of two kinds. A finding is a real result of the sanitizer run: a sanitizer report
(class "report"), a failed or timed-out CTest test ("test"), a failed or timed-out smoke run ("smoke-run"). The rest
are infrastructure: a logs pack that failed ("logs-pack"), a tree that was not clean ("dirty-tree"), the host or the
job refusing ("host"), anything else that went wrong in the job ("job"). "blocking" is true when there is a finding.
The gate (bench/gate_lib.py) stops on a red record of its inputs only when it is blocking, so a disk that was full or a
tree that was dirty does not close the inputs for good.

The record is created with exclusive creation: an existing file is never replaced, red or not.
--dry-run marks the record ("dry_run": true): a test of the records driver, never citable. The gate
refuses a dry-run record found among the records (bench/gate_lib.py), and the drivers never write one
into lab/sanitizer-records.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import build_inputs
from gate_lib import SANITIZERS_BY_HOST, SHA256

# The first line of every sanitizer report and every SUMMARY line; the same text as the Papers
# repo's lab/bin/sanitize.sh, checked by its lab/bin/test_report_pattern.sh.
REPORT = re.compile(r"ERROR: (Address|Memory|Leak|Thread)Sanitizer|WARNING: (Memory|Thread)Sanitizer|"
                    r"SUMMARY: [A-Za-z]+Sanitizer|runtime error:")
# What the shared pattern misses and this paper's runs can print: a runtime's own check failure, and a fatal
# error of ThreadSanitizer (for example a memory mapping it cannot live with). A Lab request asks for both in the
# shared pattern; until then they are scanned for here, on top of it.
EXTRA_REPORT = re.compile(r"(?:Sanitizer|sanitizer)[A-Za-z_]*: CHECK failed|CHECK failed: |FATAL: ThreadSanitizer")
CTEST_LINE = re.compile(r"Test\s+#(\d+): (\S+)\s*$")
BUILD = build_inputs.BUILD
# The tests every backend must have in the record, by name. They are the campaign's own programs and
# the loop under them: a configure without the measurement programs, or a backend left out of the
# build, makes the record red.
WAKEPROBE_PROTOCOLS = ("single", "batch")
WAKEPROBE_VARIANTS = ("none", "absent_wake", "undrained_edge", "lazy_wake_race", "lazy_wake_wide")
MATRIX_VARIANTS = ("none", "plain_wake", "absent_wake", "unobserved_primitive", "lazy_wake_race", "lazy_wake_wide",
                   "wrong_worker", "undrained_edge")
NO_RETRIGGER_BACKENDS = ("epoll", "io_uring")  # the backends with a wake registration to withhold


def is_report(line: str) -> bool:
    """True for a line that is a sanitizer report: the shared pattern or this paper's extra one."""
    return bool(REPORT.search(line) or EXTRA_REPORT.search(line))


def required_tests(backend: str) -> list[str]:
    """Names (exact) or prefixes (ending in '.') of the tests one backend must have in the record."""
    names = [f"abpair_check.{backend}", f"abpair_check_work.{backend}", f"abpair_check.{backend}.pinned",
             f"abpair_check.{backend}.pin_refused", f"wakeprobe_check.{backend}.pinned"]
    names += [f"wakeprobe_check.{backend}.{p}.{v}" for p in WAKEPROBE_PROTOCOLS for v in WAKEPROBE_VARIANTS]
    variants = MATRIX_VARIANTS + (("no_retrigger",) if backend in NO_RETRIGGER_BACKENDS else ())
    return names + [f"{backend}.{v}." for v in variants]


FINDING_CLASSES = ("report", "test", "smoke-run")
INFRASTRUCTURE_CLASSES = ("logs-pack", "dirty-tree", "host", "job")


def junit(path: Path) -> dict:
    """Test names, failed and not-run tests from CTest's JUnit output. "failed" are the tests that ran and failed or
    timed out (a failure or error element); "bad" are those and the tests that did not run."""
    if not path.exists():
        return {"error": f"no {path.name}"}
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        return {"error": f"{path.name}: {e}"}
    names, bad, failed = [], [], []
    for c in root.iter("testcase"):
        name = c.get("name", "")
        names.append(name)
        ran_and_failed = c.find("failure") is not None or c.find("error") is not None or c.get("status") in ("fail", "failed")
        if ran_and_failed or c.find("skipped") is not None or c.get("status") not in (None, "run"):
            bad.append(name)
        if ran_and_failed:
            failed.append(name)
    return {"names": names, "bad": bad, "failed": failed}


def registered(path: Path) -> list[str]:
    """The test names of `ctest -N` output."""
    return [m.group(2) for m in (CTEST_LINE.search(line) for line in read(path).splitlines()) if m]


def missing_required(names: list[str], backends: list[str]) -> dict[str, list[str]]:
    """Per backend, the required tests (exact names, or prefixes ending in '.') that no test of NAMES matches."""
    have = set(names)
    out = {}
    for b in backends:
        lacking = [r for r in required_tests(b)
                   if not (r in have or (r.endswith(".") and any(n.startswith(r) for n in names)))]
        if lacking:
            out[b] = lacking
    return out


def backend_counts(names: list[str], bad: list[str], backends: list[str]) -> dict[str, dict[str, int]]:
    """Per backend: how many matrix, wakeprobe_check and abpair_check tests the run held, and how many of them failed."""
    out = {}
    failed = set(bad)
    for b in backends:
        classes = {"matrix": lambda n, b=b: n.startswith(f"{b}."),
                   "wakeprobe_check": lambda n, b=b: n.startswith(f"wakeprobe_check.{b}."),
                   "abpair_check": lambda n, b=b: n.startswith((f"abpair_check.{b}", f"abpair_check_work.{b}"))}
        out[b] = {}
        for cls, match in classes.items():
            mine = [n for n in names if match(n)]
            out[b][cls] = len(mine)
            out[b][cls + "_bad"] = sum(1 for n in mine if n in failed)
    return out


def reason(cls: str, detail: str) -> dict:
    return {"class": cls, "detail": detail}


def smoke_section(work: Path, host_tag: str, smoke_exit: int, dry_run: bool) -> tuple[dict, dict, list[dict]]:
    """(the smoke preset entry, the record's "smoke" facts, the red reasons). The entry is green only when smoke.json
    holds exactly the runs bench/campaign_smoke.py computes for the system now, all with exit 0 and no report, and no
    line that a tool's --validate-args refused. The facts are this writer's own count and the expected shapes' keys.
    A run that failed, reported or timed out is a finding ("smoke-run"); the smoke refusing the host (exit 2) is "host";
    everything else is "job"."""
    import campaign_smoke  # here and not above: it takes is_report and blocks from this module

    system = {"L": "linux", "W": "windows"}[host_tag]
    facts: dict = {"mode": None, "planned": 0, "ran": 0, "failed": -1, "reports": -1, "limit_s_total": 0, "by_tool": {},
                   "shape_keys": []}
    problems, reasons = [], []
    try:
        data = json.loads(read(work / "smoke.json"))
    except ValueError:
        data = None
        problems.append("no readable smoke.json")
        reasons.append(reason("host" if smoke_exit == 2 else "job", "no readable smoke.json"))
    try:
        expected = campaign_smoke.shapes(system)
    except campaign_smoke.SmokeError as e:
        expected = ()
        problems.append(f"the shapes cannot be computed: {e}")
    if data is not None:
        mode = data.get("mode")
        facts["mode"] = mode
        if data.get("smoke") != campaign_smoke.SMOKE_VERSION or data.get("system") != system:
            problems.append(f"smoke.json is version {data.get('smoke')!r} for {data.get('system')!r}, not "
                            f"{campaign_smoke.SMOKE_VERSION} for {system}")
            reasons.append(reason("job", problems[-1]))
        if [(s.get("id"), s.get("args")) for s in data.get("shapes", [])] != [(s.id, list(s.args)) for s in expected]:
            problems.append(f"smoke.json holds other shapes than the {len(expected)} of {system} now")
            reasons.append(reason("job", problems[-1]))
        if mode == "list" and not dry_run:
            problems.append("the smoke only listed its shapes and ran none: only a dry run may")
            reasons.append(reason("job", problems[-1]))
        elif mode not in ("run", "list"):
            problems.append(f"the smoke mode is {mode!r}")
            reasons.append(reason("job", problems[-1]))
        runs = data.get("runs", [])
        bad = [r.get("id") for r in runs if r.get("exit") != 0 or r.get("reports") != 0 or r.get("timed_out")]
        invalid = data.get("validate_failures") or []
        # The facts are the writer's own count of what the file holds, not what the file says about itself.
        facts.update(ran=len(runs), failed=len(bad) + len(invalid), reports=sum(r.get("reports") or 0 for r in runs),
                     planned=len(expected), limit_s_total=sum(s.limit_s for s in expected),
                     by_tool={t: sum(1 for s in expected if s.tool == t) for t in campaign_smoke.TOOLS},
                     shape_keys=[list(s.key) for s in expected])
        if invalid:
            problems.append(f"{len(invalid)} lines were refused by the tools' --validate-args (e.g. {invalid[0].get('id')})")
            reasons.append(reason("job", problems[-1]))
        if mode == "run":
            if [r.get("id") for r in runs] != [s.id for s in expected]:
                problems.append(f"the smoke ran {len(runs)} of the {len(expected)} shapes, or not these")
                reasons.append(reason("job", problems[-1]))
            if bad:
                problems.append(f"{len(bad)} smoke runs failed or reported (e.g. {bad[0]})")
                reasons.append(reason("smoke-run", problems[-1]))
        elif runs:
            problems.append("a listing holds runs")
            reasons.append(reason("job", problems[-1]))
    if smoke_exit != 0:
        problems.append(f"smoke exit {smoke_exit}")
        if not any(r["class"] in FINDING_CLASSES or r["class"] == "host" for r in reasons):
            reasons.append(reason("host" if smoke_exit == 2 else "job", problems[-1]))
    entry = {"preset": "smoke", "status": "green" if not problems else "red",
             "detail": f"{facts['ran']} of {facts['planned']} shapes run, {facts['failed']} failed, {facts['reports']} reports"
                       + ("; " + "; ".join(problems) if problems else "")}
    return entry, facts, reasons


def blocks(text: str) -> list[str]:
    lines = text.splitlines()
    return ["\n".join(lines[i:i + 30]) for i, line in enumerate(lines) if is_report(line)]


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def cxx_compiler(work: Path) -> str | None:
    """CMAKE_CXX_COMPILER of the build in WORK/build, from its CMakeCache.txt, or None: the record names the
    Visual Studio instance (or the clang) that made it."""
    m = re.search(r"^CMAKE_CXX_COMPILER:[A-Z]+=(.*)$", read(work / "build" / "CMakeCache.txt"), re.MULTILINE)
    return m.group(1).strip() if m else None


INPUTS_FIELDS = ("host", "compiler", "file_hashes", "config")


def inputs_section(work: Path, inputs_exit: int) -> tuple[dict, list[str]]:
    """(the record's input fields, the problems with build.inputs.json). An inputs file that build_inputs.py did not
    finish is no file (it is written whole), so a missing or short one is red."""
    problems = []
    if inputs_exit != 0:
        problems.append(f"build_inputs.py exited {inputs_exit}")
    path = work / "build.inputs.json"
    if not path.exists():
        return ({"inputs_error": "no build.inputs.json (the build failed, or build_inputs.py refused it)", "compiler": ""},
                problems + ["no inputs file"])
    try:
        b = json.loads(read(path))["builds"][BUILD]
    except (ValueError, KeyError) as e:
        return {"inputs_error": f"build.inputs.json is unreadable: {e}", "compiler": ""}, problems + ["unreadable inputs file"]
    absent = [k for k in INPUTS_FIELDS if not b.get(k)]
    if absent:
        problems.append(f"the inputs file lacks {', '.join(absent)}")
    inputs = {"inputs_hash": {t: v["inputs_hash"] for t, v in b["targets"].items()},
              "inputs_files": {t: v["files"] for t, v in b["targets"].items()},
              "config": b.get("config"), "third_party": b.get("third_party"),
              "compiler": b.get("compiler", ""), "compiler_cmake": b.get("compiler_cmake"),
              "compiler_banner": b.get("compiler_banner"), "inputs_host": b.get("host")}
    return inputs, problems


def ctest_section(work: Path, ctest_exit: int, config) -> tuple[dict, dict, list[dict]]:
    """(the ctest preset entry, the test facts of the record, the red reasons). The entry is green only when every
    check holds. A test that ran and failed or timed out is a finding ("test"); a test that did not run, a run that
    does not hold the registered tests, a missing JUnit file and a CTest that exited non-zero with every test passing
    are "job"."""
    j = junit(work / "ctest.xml")
    want = registered(work / "tests.txt")
    backends = [b for b in ((config or {}).get("WAKELOOP_BACKENDS") or "").split(";") if b]
    facts: dict = {"tests_registered": len(want), "tests_total": 0, "tests_failed": -1, "missing_required": {},
                   "by_backend": {}}
    problems, reasons = [], []

    def add(cls: str, text: str) -> None:
        problems.append(text)
        reasons.append(reason(cls, text))

    if "error" in j:
        add("job", j["error"])
    else:
        names, bad, failed = j["names"], j["bad"], j["failed"]
        facts.update(tests_total=len(names), tests_failed=len(bad), failed=bad[:20])
        if not names:
            add("job", "CTest ran no test")
        if not want:
            add("job", "no list of registered tests (tests.txt)")
        elif sorted(names) != sorted(want):
            add("job", f"the run holds {len(names)} tests, ctest -N registered {len(want)}, or their names differ")
        if failed:
            add("test", f"{len(failed)} tests failed or timed out (e.g. {failed[0]})")
        if len(bad) > len(failed):
            add("job", f"{len(bad) - len(failed)} tests did not run")
        if not backends:
            add("job", "the configuration names no backend")
        facts["missing_required"] = missing_required(names, backends)
        facts["by_backend"] = backend_counts(names, bad, backends)
        if facts["missing_required"]:
            add("job", "required tests missing: " + "; ".join(f"{b}: {', '.join(v[:3])}"
                                                              for b, v in facts["missing_required"].items()))
    if ctest_exit != 0:
        problems.append(f"ctest exit {ctest_exit}")
        if not any(r["class"] == "test" for r in reasons):
            reasons.append(reason("job", problems[-1]))
    entry = {"preset": "ctest", "status": "green" if not problems else "red",
             "detail": f"exit {ctest_exit}, {facts['tests_total']} tests, {facts['tests_failed']} failed"
                       + ("; " + "; ".join(problems) if problems else "")}
    return entry, facts, reasons


def code_section(commit: str, fingerprint_start: str, dry_run: bool) -> tuple[dict, dict, list[dict]]:
    """(the code preset entry, the facts, the red reasons). The commit, the code fingerprint and the generator files at
    the end of the job must be those at its start ("job" if not), and, but for a dry run, the whole tree must be clean
    ("dirty-tree" if not)."""
    problems, facts = [], {"code_fingerprint": fingerprint_start, "code_commit": "", "generators": {},
                           "worktree_dirty_paths": -1}
    reasons = []
    try:
        head = build_inputs.git(build_inputs.REPO, "rev-parse", "HEAD").stdout.strip()
        end = build_inputs.code_fingerprint()
        facts.update(code_commit=build_inputs.code_commit(), generators=build_inputs.generator_hashes())
        dirty = build_inputs.repo_dirty()
        facts["worktree_dirty_paths"] = len(dirty)
        if head != commit:
            problems.append(f"HEAD is {head[:12]} at the end and was {commit[:12]} at the start")
            reasons.append(reason("job", problems[-1]))
        if end != fingerprint_start:
            problems.append("the code fingerprint at the end is not the one at the start")
            reasons.append(reason("job", problems[-1]))
        if dirty and not dry_run:
            problems.append(f"the tree has {len(dirty)} changed or untracked paths (e.g. {dirty[0]})")
            reasons.append(reason("dirty-tree", problems[-1]))
    except RuntimeError as e:
        problems.append(str(e))
        reasons.append(reason("job", problems[-1]))
    return {"preset": "code", "status": "green" if not problems else "red",
            "detail": "; ".join(problems) or "commit, fingerprint and generators as at the start"}, facts, reasons


def write_exclusive(path: Path, text: str) -> None:
    """Creates PATH with the text; never replaces a file that is there (FileExistsError)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for k in ("work", "record", "repo", "commit", "repo-head", "fingerprint-start", "sanitizer", "cmake-args", "options",
              "host", "logs-archive", "logs-sha256"):
        ap.add_argument(f"--{k}", required=True)
    ap.add_argument("--host-tag", required=True, choices=sorted(SANITIZERS_BY_HOST), help="the host part of the record name")
    for k in ("build-exit", "inputs-exit", "ctest-exit", "smoke-exit", "seconds"):
        ap.add_argument(f"--{k}", type=int, required=True)
    ap.add_argument("--ctest-jobs", type=int, default=1, help="ctest -j N (1: serial)")
    ap.add_argument("--dry-run", action="store_true", help="a test of the driver: the record is marked and never citable")
    a = ap.parse_args(argv)
    # "-" says that there is no archive (Windows PowerShell 5.1 drops an empty argument to a native command).
    archive, archive_sha = ("" if v == "-" else v for v in (a.logs_archive, a.logs_sha256))
    work = Path(a.work)
    inputs, inputs_problems = inputs_section(work, a.inputs_exit)
    presets = [{"preset": "build", "status": "green" if a.build_exit == 0 else "red", "detail": f"exit {a.build_exit}"}]
    reasons = [reason("job", f"the build exited {a.build_exit}")] if a.build_exit != 0 else []
    entry, facts, found = ctest_section(work, a.ctest_exit, inputs.get("config"))
    presets.append(entry)
    reasons += found
    smoke_entry, smoke_facts, found = smoke_section(work, a.host_tag, a.smoke_exit, a.dry_run)
    presets.append(smoke_entry)
    reasons += found
    code_entry, code_facts, found = code_section(a.commit, a.fingerprint_start, a.dry_run)
    presets.append(code_entry)
    reasons += found
    presets.append({"preset": "inputs", "status": "green" if not inputs_problems else "red",
                    "detail": "; ".join(inputs_problems) or "the inputs file is whole"})
    reasons += [reason("job", p) for p in inputs_problems]
    archive_ok = bool(archive) and bool(SHA256.match(archive_sha))
    presets.append({"preset": "logs", "status": "green" if archive_ok else "red",
                    "detail": "the logs archive and its sha256 are named" if archive_ok else "no logs archive or sha256"})
    if not archive_ok:
        reasons.append(reason("logs-pack", "no logs archive or sha256: the logs could not be packed"))
    reports = []
    for f in [work / "build.log", work / "ctest.log", work / "smoke.log", *sorted(work.rglob("*.err"))]:
        reports += [f"{f.relative_to(work)}:\n{b}" for b in blocks(read(f))]
    if reports:
        reasons.append(reason("report", f"{len(reports)} sanitizer reports in the logs"))
    green = all(p["status"] == "green" for p in presets) and not reports and "inputs_hash" in inputs
    if not green and not reasons:
        reasons.append(reason("job", "the record is red for a reason no check names"))  # a safety net: never a silent red
    rec = {"format": build_inputs.RECORD_FORMAT, "repo": a.repo, "commit": a.commit, "repo_head": a.repo_head, **code_facts,
           "date": datetime.now().astimezone().isoformat(timespec="seconds"),
           "host": a.host, "host_tag": a.host_tag, "green": green,
           "red_reasons": [] if green else reasons,
           "blocking": (not green) and any(r["class"] in FINDING_CLASSES for r in reasons),
           "cxx_compiler": cxx_compiler(work), **inputs, "presets": presets, "tool": BUILD, "sanitizer": a.sanitizer,
           "cmake_args": a.cmake_args, "sanitizer_options": a.options, "build_exit": a.build_exit,
           "inputs_exit": a.inputs_exit, "ctest_exit": a.ctest_exit, "ctest_jobs": a.ctest_jobs, **facts,
           "smoke_exit": a.smoke_exit, "smoke": smoke_facts, "seconds": a.seconds,
           "sanitizer_reports": len(reports), "report_blocks": reports[:20],
           "logs_archive": archive, "logs_archive_sha256": archive_sha, "dry_run": a.dry_run,
           "note": "wakeloop's CMake project built under the sanitizer, then its whole CTest suite, run serially, then "
                   "the campaign smoke: one reduced real run of every code shape of the campaign, pilot and H5 runs "
                   "(hypotheses-v2.md, section 11 item 7). No measurement is read or kept. Declared gaps: bench/coverage.json."
                   + (" A dry run of the records driver: never citable, refused by the gate." if a.dry_run else "")}
    try:
        write_exclusive(Path(a.record), json.dumps(rec, indent=1) + "\n")
    except FileExistsError:
        print(f"wakeloop_record: {a.record} exists and is never replaced", file=sys.stderr)
        return 2
    print(json.dumps({"record": a.record, "green": green, "sanitizer": a.sanitizer, "reports": len(reports),
                      "dry_run": a.dry_run}))
    for p in presets:
        if p["status"] != "green":
            print(f"  {p['preset']}: {p['status']} ({p['detail']})")
    return 0 if green else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""The testbed facts the paper states, gathered from the run records into one JSON file.

  python paper_setup.py --kernel RUN.kernel.json --build-log-linux LOG --build-log-windows LOG
                        --records RECORD.json [...] --hypotheses hypotheses-v2.md --out setup.json

  --kernel             the Linux campaign job's run.kernel.json (the kernel the runs had)
  --build-log-linux    the Linux campaign job's build.log (CMake's compiler line, the bench line)
  --build-log-windows  the same for Windows
  --records            the D5 sanitizer records of the campaign's inputs (lab/sanitizer-records)
  --hypotheses         the frozen hypotheses-v2.md: the laptop, BIOS and Windows build of its
                       testbed paragraph (section 1), which no run record repeats

Refused, with nothing written: a build log without its compiler or bench line; the two systems built
from different commits or from a dirty tree; a record that is not green, reports a failed test or has
no complete smoke run; the testbed paragraph's kernel or compilers differing from the runs'.

Every input's sha256 (of its bytes with CRLF read as LF) is kept in the output under "sources", by
role for the four single inputs and by file name for the records.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402

COMPILER_LINE = re.compile(r"The CXX compiler identification is (.+?)\s*$", re.M)
BENCH_LINE = re.compile(r"bench: repo ([0-9a-f]{40}) dirty=(\d+), code ([0-9a-f]{40})")
TESTBED = re.compile(
    r"\*\*Testbed\.\*\*\s+The laptop \((?P<model>[^,]+),\s+BIOS\s+(?P<bios>[^,]+),\s+(?P<cpu>AMD Ryzen 7 5800H),\s+"
    r"(?P<cores>\d+)\s+cores\s+and\s+(?P<logical>\d+)\s+logical\s+CPUs.*?kernel\s+(?P<kernel>[^,\s]+),\s+"
    r"clang\s+(?P<clang>[\d.]+)\)\s+and\s+Windows\s+11\s+Pro\s+\(build\s+(?P<build>[\d.]+),\s+MSVC\s+cl\s+"
    r"(?P<msvc>[\d.]+)\),\s+as\s+read\s+on\s+(?P<read>\d{4}-\d{2}-\d{2})", re.S)


class Refusal(Exception):
    pass


def sha256(path):
    """Of the file's bytes with CRLF read as LF, so a checkout's line endings do not change it."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def build_facts(text, system):
    c = COMPILER_LINE.search(text)
    b = BENCH_LINE.search(text)
    if not c or not b:
        raise Refusal("%s build log: no compiler or bench line" % system)
    if b.group(2) != "0":
        raise Refusal("%s build log: the tree was dirty" % system)
    return {"compiler": c.group(1), "bench_commit": b.group(1), "code_commit": b.group(3)}


def testbed(text):
    m = TESTBED.search(text)
    if not m:
        raise Refusal("hypotheses file: no testbed paragraph of the expected form")
    d = m.groupdict()
    for k in ("cores", "logical"):
        d[k] = int(d[k])
    return d


def record_facts(path):
    with open(path, encoding="utf-8") as f:
        r = json.load(f)
    if not r.get("green") or r.get("tests_failed") or r.get("sanitizer_reports"):
        raise Refusal("%s: not a green record with every test passed" % path)
    smoke = r.get("smoke") or {}
    if type(smoke.get("ran")) is not int or smoke.get("ran") <= 0 or smoke.get("failed") != 0:
        raise Refusal("%s: no smoke run, or a failed one" % path)
    if type(r.get("tests_total")) is not int or r["tests_total"] <= 0:
        raise Refusal("%s: no test count" % path)
    return {"file": os.path.basename(path), "sha256": sha256(path), "system": {"L": "linux", "W": "windows"}[r["host_tag"]],
            "sanitizer": r["sanitizer"], "compiler": r["compiler"], "commit": r["commit"],
            "code_commit": r["code_commit"], "tests_total": r["tests_total"], "tests_failed": r["tests_failed"],
            "smoke_ran": smoke.get("ran"), "smoke_failed": smoke.get("failed"),
            "sanitizer_reports": r["sanitizer_reports"]}


def gather(kernel_path, linux_log, windows_log, record_paths, hypotheses_path):
    with open(kernel_path, encoding="utf-8") as f:
        kernel = json.load(f)
    linux = build_facts(read_text(linux_log), "linux")
    windows = build_facts(read_text(windows_log), "windows")
    if (linux["bench_commit"], linux["code_commit"]) != (windows["bench_commit"], windows["code_commit"]):
        raise Refusal("the two systems were built from different commits")
    bed = testbed(read_text(hypotheses_path))
    if bed["kernel"] != kernel["kernel"]:
        raise Refusal("testbed kernel %s, runs had %s" % (bed["kernel"], kernel["kernel"]))
    if bed["clang"] not in linux["compiler"].split() or not windows["compiler"].split()[-1].startswith(bed["msvc"]):
        raise Refusal("the testbed's compilers (clang %s, MSVC %s) are not the builds' (%s, %s)"
                      % (bed["clang"], bed["msvc"], linux["compiler"], windows["compiler"]))
    records = sorted((record_facts(p) for p in record_paths), key=lambda r: (r["system"], r["sanitizer"]))
    if not records:
        raise Refusal("no records")
    if {r["code_commit"] for r in records} != {linux["code_commit"]}:
        raise Refusal("the records do not name the measured code commit")
    linux.update(kernel=kernel["kernel"], kernel_read_at=kernel.get("at"))
    windows.update(build=bed["build"], build_read_on=bed["read"])
    sources = {role: sha256(p) for role, p in (("kernel", kernel_path), ("build_log_linux", linux_log),
                                                ("build_log_windows", windows_log),
                                                ("hypotheses", hypotheses_path))}
    return {"laptop": {"model": bed["model"], "bios": bed["bios"], "cpu": bed["cpu"], "cores": bed["cores"],
                       "logical_cpus": bed["logical"], "read_on": bed["read"]},
            "linux": linux, "windows": windows, "records": records,
            "sources": dict(sources, **{r["file"]: r["sha256"] for r in records})}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kernel", required=True)
    ap.add_argument("--build-log-linux", required=True)
    ap.add_argument("--build-log-windows", required=True)
    ap.add_argument("--records", nargs="+", required=True)
    ap.add_argument("--hypotheses", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    try:
        facts = gather(args.kernel, args.build_log_linux, args.build_log_windows, args.records, args.hypotheses)
    except Refusal as e:
        print("refused: %s" % e, file=sys.stderr)
        return 2
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(facts, f, indent=1, sort_keys=True)
        f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

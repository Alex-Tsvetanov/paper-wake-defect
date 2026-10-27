"""What a real run shows about where it came from (contract C2; hypotheses-v2.md, sections 1 and 8).

score.py and pilot.py share these checks. Each returns the reason it fails, or None. Outside
--draft a failed check refuses the run; with --draft it is a warning.
"""

import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import grid  # noqa: E402
import plan  # noqa: E402

COMMIT_NAME = re.compile(r"[0-9a-f]{40}")  # what bench_commit holds when the build knew its commit

# The lines the campaign runner (bench/run_campaign.py) writes into run.txt after the gate of sanitizer records passed
# (bench/gate_lib.py): a run without them was not gated. A "gate records" line is "<san> <name> <sha256>[, <name>
# <sha256>]" per sanitizer, joined by "; ". The sanitizers each system needs are those of bench/gate_lib.py,
# SANITIZERS_BY_HOST (bench/test_records.py compares the two).
RUN_FILE = "run.txt"
GATE_KEYS = ("gate", "gate compiler", "gate code commit", "gate code fingerprint", "gate coverage.json sha256",
             "gate generators", "gate records", "gate other records", "gate smoke shape keys")
GATE_SANITIZERS = {"linux": ("asan", "tsan", "msan"), "windows": ("asan",)}
OTHER_SYSTEM = {"linux": "windows", "windows": "linux"}
SYSTEM_TAG = {"linux": "L", "windows": "W"}
RECORD_FILE = re.compile(r"(wakeloop2-[0-9a-f]+-([LW])-([a-z]+)-\S*\.json) ([0-9a-f]{64})")


def header_problem(header, system, kind):
    """None when the plan header is of the real form of its kind ('pilot' or 'campaign') and names `system`.

    A stand-in or draft plan (generated_by ending in stand-in-R or draft) is not of either form.
    """
    form = plan.PILOT_HEADER if kind == "pilot" else plan.CAMPAIGN_HEADER
    by = header.get("generated_by")
    if header.get("system") != system:
        return "the plan names the system %r, not %r" % (header.get("system"), system)
    if header.get("plan") != plan.PLAN_VERSION or header.get("seed") != grid.SEED:
        return "the plan's version or seed is not the frozen one"
    if not isinstance(by, str) or not form.fullmatch(by):
        return "generated_by %r is not of the real form of a %s plan (%s)" % (by, kind, form.pattern)
    return None


def log_digest(header):
    """The revision log's sha256 a campaign plan's header records, or None."""
    m = plan.CAMPAIGN_HEADER.fullmatch(header.get("generated_by") or "")
    return m.group(1) if m else None


def departures(expected, entries):
    """How the plan's process lines differ from the ones plan.py itself makes.

    expected: id -> entry. entries: the plan's lines. Returns {"differ": ids, "extra": ids,
    "missing": ids}, each sorted. A line differs when any of its fields does, limit_s included.
    """
    seen = {e["id"]: e for e in entries}
    return {"differ": sorted(i for i, e in seen.items() if i in expected and expected[i] != e),
            "extra": sorted(i for i in seen if i not in expected),
            "missing": sorted(i for i in expected if i not in seen)}


def order_text(system, ids, want):
    """None when the plan runs its processes in the order plan.py shuffles them, else a sentence.

    Section 8 fixes one order, shuffled with seed 20261026. A plan whose lines are not the generator's
    (other ids, missing or extra ones) is reported for that, not for its order.
    """
    if ids == want or sorted(ids) != sorted(want):
        return None
    return "%s: the processes are not in the order shuffled with seed %d" % (system, grid.SEED)


def departure_texts(system, found):
    """The departures as sentences; empty when there are none."""
    out = []
    differ = found["differ"] + found["extra"]
    if differ:
        out.append("%s: %d plan entries differ from the frozen grid, e.g. %s" % (system, len(differ), differ[:3]))
    if found["missing"]:
        out.append("%s: %d process lines of the frozen grid are not in the plan, e.g. %s" % (
            system, len(found["missing"]), found["missing"][:3]))
    if found.get("order"):
        out.append(found["order"])
    return out


def commit_problem(commits):
    """None when every process came from one build commit that is a full commit name.

    commits: bench_commit -> number of processes. Section 1: every build, on both systems, is the
    Release build of one commit.
    """
    if not commits:
        return None
    if len(commits) > 1:
        return "the processes were built from %d commits, not one: %s" % (
            len(commits), ", ".join("%s (%d)" % (c, n) for c, n in sorted(commits.items())))
    only = next(iter(commits))
    if not isinstance(only, str) or not COMMIT_NAME.fullmatch(only):
        return "bench_commit is %r, not a commit name" % (only,)
    return None


def read_run_txt(data_dir):
    """The fields of run.txt in DATA_DIR, the first line of each key ('key: value'); None if there is no such file.
    Resume lines come after the first block and are not read."""
    path = os.path.join(data_dir, RUN_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(": ")
        if sep:
            fields.setdefault(key, value)
    return fields


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def named_records(value):
    """{sanitizer: [(name, sha256, host tag, sanitizer in the name)]} from a 'gate records' value."""
    out = {}
    for part in value.split("; "):
        san, _, rest = part.partition(" ")
        if san:
            out[san] = [(m.group(1), m.group(4), m.group(2), m.group(3)) for m in RECORD_FILE.finditer(rest)]
    return out


def gate_problems(system, data_dir, records_dirs):
    """The reasons the run in DATA_DIR was not gated, as sentences (empty when it was): run.txt lacks a gate line or
    says the gate did not pass; a sanitizer the system needs has no record named (own system's for 'gate records',
    the other's for 'gate other records'); a named record is in none of RECORDS_DIRS, or has another sha256 than
    run.txt records, or is red or a dry run; or no records folder was given.

    The runner writes these lines only after its gate passed, so a missing line means a run that was not gated
    (the gate is the runner's, and no run is timed without it); the folder check is the second look: the records are
    still the ones the gate read."""
    if not records_dirs:
        found = ["%s: no --records folder was given, so the records named in run.txt were not checked" % system]
    else:
        found = []
    fields = read_run_txt(data_dir)
    if fields is None:
        return found + ["%s: %s has no %s, so the records gate of the run cannot be checked" % (system, data_dir, RUN_FILE)]
    absent = [k for k in GATE_KEYS if k not in fields]
    if absent:
        return found + ["%s: run.txt has no %s line%s: the run was not gated by the records gate" % (
            system, ", ".join("'%s'" % k for k in absent[:3]), "s" if len(absent) > 1 else "")]
    if fields["gate"] != "passed":
        found.append("%s: run.txt says 'gate: %s', not passed" % (system, fields["gate"]))
    for key, which in (("gate records", system), ("gate other records", OTHER_SYSTEM[system])):
        named = named_records(fields[key])
        for san in GATE_SANITIZERS[which]:
            entries = named.get(san) or []
            if not entries:
                found.append("%s: run.txt names no %s record for %s in '%s'" % (system, san, which, key))
            for name, sha, tag, name_san in entries:
                if tag != SYSTEM_TAG[which] or name_san != san:
                    found.append("%s: the record %s is named as a %s record of %s" % (system, name, san, which))
                    continue
                where = [os.path.join(d, name) for d in records_dirs if os.path.isfile(os.path.join(d, name))]
                if not where:
                    if records_dirs:
                        found.append("%s: the record %s that run.txt names is in none of the --records folders" % (system, name))
                    continue
                actual = sha256_of(where[0])
                if actual != sha:
                    found.append("%s: the record %s has sha256 %s, and run.txt records %s" % (system, name, actual, sha))
                    continue
                try:
                    with open(where[0], encoding="utf-8") as f:
                        rec = json.load(f)
                except (OSError, ValueError):
                    found.append("%s: the record %s cannot be read" % (system, name))
                    continue
                if rec.get("green") is not True or rec.get("dry_run"):
                    found.append("%s: the record %s is not a green record of a real job" % (system, name))
    return found


def bench_commits(resolved):
    """bench_commit -> number of good processes, over Resolved objects. Only the runs whose data count."""
    out = {}
    for r in resolved:
        if r.ok:
            commit = r.run.summary.get("bench_commit")
            key = commit if isinstance(commit, str) else repr(commit)
            out[key] = out.get(key, 0) + 1
    return out

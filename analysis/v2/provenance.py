"""What a real run shows about where it came from (contract C2; hypotheses-v2.md, sections 1 and 8).

score.py and pilot.py share these checks. Each returns the reason it fails, or None. Outside
--draft a failed check refuses the run; with --draft it is a warning.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import grid  # noqa: E402
import plan  # noqa: E402

COMMIT_NAME = re.compile(r"[0-9a-f]{40}")  # what bench_commit holds when the build knew its commit


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


def bench_commits(resolved):
    """bench_commit -> number of good processes, over Resolved objects. Only the runs whose data count."""
    out = {}
    for r in resolved:
        if r.ok:
            commit = r.run.summary.get("bench_commit")
            key = commit if isinstance(commit, str) else repr(commit)
            out[key] = out.get(key, 0) + 1
    return out

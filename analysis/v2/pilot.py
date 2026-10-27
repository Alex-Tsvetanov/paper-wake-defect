"""The A/A pilot of H4 (hypotheses-v2.md, section 7).

Per cell (backend, system and N), s is the standard deviation of the 8 per-pair log ratios of
the fixed loop against itself (side A's throughput over side B's, as abpair names the sides).
The power rule then gives the cell's R: the smallest even R in 14 to 24 whose power at the
strictest Holm step is at least 0.8; else R = 24 and the cell is underpowered.

  python pilot.py --linux PLAN DIR [--windows PLAN DIR] --out FILE [--markdown FILE] [--draft]

--out is the pilot's full record (JSON). --markdown writes the block that a person appends to
revision-log-v2.md, where plan.py reads each cell's R from (h4log.py); it holds all 20 H4 cells,
so it needs both --linux and --windows.

The record carries the commit it was made at, read from git, with the provenance rule of a
campaign plan (plan.py): refused unless the repository's working tree is clean. Outside --draft
it is also refused when a plan's header is not of the real form of a pilot plan (C2), when a plan's
lines are not the ones plan.py makes, or when the processes were not all built from one commit
(section 1). --draft is for dry runs from an uncommitted tree and ends generated_by in "draft";
those three become warnings, kept in the record.

The block's s is written at full precision, so that h4log.py can check each row against the rule.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import statistics  # noqa: E402

import grid  # noqa: E402
import h4log  # noqa: E402
import plan  # noqa: E402
import provenance  # noqa: E402
import readers  # noqa: E402
import stats  # noqa: E402

H4_R_VERSION = 1


def pilot_cell(system, backend, work_us, resolved):
    """s, R and power of one pilot cell from its processes (after the rerun rule)."""
    log_ratios = []
    for r in resolved:
        if r.ok:
            log_ratios.extend(math.log(x) for x in readers.pair_ratios(r.run))
    out = {"system": system, "backend": backend, "work_us": work_us, "pairs": len(log_ratios),
           "processes": [r.report() for r in resolved], "log_ratios": log_ratios}
    if len(log_ratios) < 2:
        # The frozen rule needs an SD. Without one, take the cap and declare the cell underpowered.
        out.update(s=None, R=grid.H4_R_MAX, power=None, underpowered=True,
                   note="fewer than 2 pairs: no s; R set to the cap, underpowered")
        return out
    s = statistics.stdev(log_ratios)  # the sample SD (n - 1)
    r, power, underpowered = stats.pilot_choose_r(s)
    out.update(s=s, R=r, power=power, underpowered=underpowered,
               note=None if len(log_ratios) == grid.PILOT_PAIRS else
               "%d pairs, not %d" % (len(log_ratios), grid.PILOT_PAIRS))
    return out


def pilot_system(plan_path, data_dir):
    """(cells, problems): one system's pilot cells keyed by H4 key, and what its provenance lacks."""
    header, entries = readers.load_plan(plan_path)
    system = header["system"]
    folder = readers.DataDir(data_dir)
    by_cell = {}
    for e in entries:
        if e["family"] != "pilot":
            raise ValueError("%s: %s is not a pilot process" % (plan_path, e["id"]))
        by_cell.setdefault(e["cell"], []).append(readers.Resolved(e, folder, system))
    cells = {}
    for backend, work_us in grid.h4_cells(system):
        resolved = sorted(by_cell.get(grid.pilot_cell_name(backend, work_us), []),
                          key=lambda r: readers.process_index(r.id))
        cells[grid.h4_key(system, backend, work_us)] = pilot_cell(system, backend, work_us, resolved)
    problems = []
    found = provenance.header_problem(header, system, "pilot")
    if found:
        problems.append("%s: %s" % (system, found))
    wanted = plan.pilot_plan_entries(system)
    found = provenance.departures({e["id"]: e for e in wanted}, entries)
    found["order"] = provenance.order_text(system, [e["id"] for e in entries], [e["id"] for e in wanted])
    problems += provenance.departure_texts(system, found)
    return cells, problems, provenance.bench_commits(r for resolved in by_cell.values() for r in resolved)


def main(argv=None, repo=plan.REPO_ROOT):
    """repo: the repository whose clean tree a real record needs. Tests pass a temporary one."""
    ap = argparse.ArgumentParser(description="The A/A pilot: each H4 cell's s, R and power.")
    ap.add_argument("--linux", nargs=2, metavar=("PLAN", "DIR"))
    ap.add_argument("--windows", nargs=2, metavar=("PLAN", "DIR"))
    ap.add_argument("--draft", action="store_true",
                    help="a dry run from an uncommitted tree: no clean-tree check, generated_by ends in draft")
    ap.add_argument("--out", required=True, help="the pilot's full record (JSON)")
    ap.add_argument("--markdown", help="the block for revision-log-v2.md (needs --linux and --windows)")
    a = ap.parse_args(argv)
    if not a.linux and not a.windows:
        ap.error("give --linux and/or --windows")
    if a.markdown and not (a.linux and a.windows):
        ap.error("--markdown needs --linux and --windows: the block holds all %d H4 cells" % len(h4log.expected_cells()))
    try:
        by = plan.provenance(repo, a.draft, tool="pilot.py")
    except plan.Refusal as e:
        raise SystemExit("pilot.py: refused: %s" % e)
    cells, problems, commits = {}, [], {}
    for pair in (a.linux, a.windows):
        if pair:
            found, found_problems, found_commits = pilot_system(pair[0], pair[1])
            cells.update(found)
            problems += found_problems
            for commit, n in found_commits.items():
                commits[commit] = commits.get(commit, 0) + n
    found = provenance.commit_problem(commits)
    if found:
        problems.append(found)
    if problems and not a.draft:
        raise SystemExit("pilot.py: refused: %s" % "; ".join(problems))
    doc = {"h4_r": H4_R_VERSION, "rule": "hypotheses-v2.md section 7", "draft": a.draft,
           "generated_by": by, "warnings": problems, "bench_commits": commits, "cells": cells}
    block = h4log.render_block(cells) if a.markdown else None  # before any file is written
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(stats.jsonable(doc), f, indent=1, sort_keys=True)
        f.write("\n")
    if block is not None:
        with open(a.markdown, "w", encoding="utf-8", newline="\n") as f:
            f.write(block)
    for problem in problems:
        print("warning: " + problem, file=sys.stderr)
    for key, cell in sorted(doc["cells"].items()):
        print("%-28s pairs %2d  s %-10s R %2d  power %s%s" % (
            key, cell["pairs"], "%.5f" % cell["s"] if cell["s"] is not None else "-", cell["R"],
            "%.4f" % cell["power"] if cell["power"] is not None else "-",
            "  UNDERPOWERED" if cell["underpowered"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The B_eff rule of hypotheses-v2.md, section 2.

For the thresholds of every S and Bt cell of a backend, system and B: the median of the
calibrated B_eff (summary field b_eff_ns) over the 10 processes of the absent-wake, W = 1, S
cell of that backend, system and B. Fallback, if fewer than 8 of the 10 processes calibrate or
their values span more than 10% of their median: B on Linux, and on Windows the larger of B
and the coarsest timer resolution (summary field coarsest_timer_us). Which source applied is
recorded.

The coarsest timer resolution is a property of the system, so it is read from any wakeprobe
summary of that system, good process or not. When no summary reports one, the Windows fallback
cannot be taken and the rule refuses (BeffError); it never falls back to B.

Usage: python beff.py --plan PLAN --data DIR   (prints the rule's result per backend and B)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse  # noqa: E402
import json  # noqa: E402
from fractions import Fraction  # noqa: E402

import grid  # noqa: E402
import stats  # noqa: E402

SOURCE_MEASURED = "measured: median of the calibrated b_eff_ns"
SOURCE_BOUND = "fallback: B"
SOURCE_TIMER = "fallback: coarsest timer resolution"


class BeffError(ValueError):
    """The rule cannot give a B_eff: the Windows fallback has no timer resolution to use."""


def coarsest_timer_us(summaries):
    """The coarsest timer resolution (us) any of these summaries reports, or None."""
    timers = [s["coarsest_timer_us"] for s in summaries
              if s is not None and isinstance(s.get("coarsest_timer_us"), (int, float))
              and not isinstance(s["coarsest_timer_us"], bool) and s["coarsest_timer_us"] > 0]
    return max(timers) if timers else None


def beff_rule(system, bound_us, summaries, system_summaries=None, processes=grid.PROCESSES_PER_CELL):
    """B_eff in ns for one backend, system and B.

    summaries: the summary lines of the absent-wake, W = 1, S cell's processes whose data count
    (a missing or failed process is simply absent, and counts as not calibrated). The count is
    against the cell's 10 processes, so fewer than 10 summaries can still pass with 8.
    system_summaries: every wakeprobe summary of the system, good or not, which the Windows
    fallback reads the coarsest timer resolution from (default: summaries).
    """
    values = sorted(s["b_eff_ns"] for s in summaries
                    if s is not None and isinstance(s.get("b_eff_ns"), (int, float)) and s["b_eff_ns"] > 0)
    out = {"system": system, "bound_us": bound_us, "processes": processes, "summaries": len(summaries),
           "calibrated": len(values), "median_ns": None, "span_ns": None, "span_over_median": None,
           "reason": None}
    if values:
        med = stats.median(values)
        span = values[-1] - values[0]
        out.update(median_ns=med, span_ns=span, span_over_median=span / med)
    if len(values) < grid.BEFF_MIN_CALIBRATED:
        out["reason"] = "%d of %d processes calibrated, fewer than %d" % (
            len(values), processes, grid.BEFF_MIN_CALIBRATED)
    elif Fraction(out["span_ns"]) > grid.BEFF_MAX_SPAN * Fraction(out["median_ns"]):
        out["reason"] = "the values span more than 10% of their median"
    if out["reason"] is None:
        out.update(b_eff_ns=out["median_ns"], source=SOURCE_MEASURED)
        return out
    bound_ns = bound_us * 1000
    out.update(b_eff_ns=bound_ns, source=SOURCE_BOUND)
    if system == "windows":
        coarsest_us = coarsest_timer_us(summaries if system_summaries is None else system_summaries)
        if coarsest_us is None:
            raise BeffError("the B_eff fallback on Windows needs the coarsest timer resolution, and no wakeprobe "
                            "summary reports coarsest_timer_us (%s)" % out["reason"])
        coarsest_ns = coarsest_us * 1000
        out["coarsest_timer_ns"] = coarsest_ns
        if coarsest_ns > bound_ns:
            out.update(b_eff_ns=coarsest_ns, source=SOURCE_TIMER)
    return out


def beff_table(system, resolved_by_cell, system_summaries=None):
    """B_eff per (backend, bound_us). resolved_by_cell maps a cell name to its Resolved list.

    system_summaries: every wakeprobe summary of the system, good or not (default: those of
    every Resolved given, first run, rerun and void runs alike).
    """
    if system_summaries is None:
        system_summaries = [run.summary for resolved in resolved_by_cell.values() for r in resolved
                            for run in r.runs() if run.summary]
    table = {}
    for backend in grid.BACKENDS[system]:
        for bound_us in grid.BOUNDS_US:
            cell = grid.beff_source_cell(system, backend, bound_us)
            summaries = [r.run.summary for r in resolved_by_cell.get(cell.name, []) if r.ok]
            entry = beff_rule(system, bound_us, summaries, system_summaries)
            entry["from_cell"] = cell.name
            table[(backend, bound_us)] = entry
    return table


def main(argv=None):
    import readers

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plan", required=True)
    ap.add_argument("--data", required=True)
    a = ap.parse_args(argv)
    header, entries = readers.load_plan(a.plan)
    system = header["system"]
    folder = readers.DataDir(a.data)
    by_cell = {}
    for e in entries:
        if e["tool"] == "wakeprobe":
            by_cell.setdefault(e["cell"], []).append(readers.Resolved(e, folder, system))
    try:
        table = beff_table(system, by_cell)
    except BeffError as e:
        raise SystemExit("beff.py: refused: %s" % e)
    for (backend, bound_us), entry in sorted(table.items()):
        print(json.dumps(stats.jsonable(dict(entry, backend=backend)), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

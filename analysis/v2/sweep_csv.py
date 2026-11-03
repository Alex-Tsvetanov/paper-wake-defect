"""The posts of H3's phase sweep as one CSV, for the paper's figure of the delay of a missed wake.

  python sweep_csv.py --linux PLAN DIR --windows PLAN DIR --score SCORE.json --out SWEEP.csv

PLAN and DIR are a system's campaign plan and output folder, as score.py reads them. Only the good
processes of each sweep go in, as score.py's score_sweep takes them: each measured post with its
phase, its delay, its residual from wakeprobe (the delay minus the predicted delay, B_eff minus the
phase mod B_eff) and its own process's calibrated B_eff. Nothing is fitted or filtered here.

The output must show what was scored. Before it is written, the median of |residual| / B_eff over
each sweep's rows is computed as score_sweep computes it and compared with H3's value in SCORE.json
(score.py's output) for the same system, backend and bound; any difference, a missing sweep or a
sweep that is not complete is refused, and nothing is written.

Columns: system, backend, bound_us, step (the process, 0 to 29), phase_target (the step's target as
a fraction of B_eff), index (the post's index in its process), phase_ns, delay_ns, residual_ns,
b_eff_ns. Rows in the order of the plan's processes and of each process's posts.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse  # noqa: E402
import csv  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402

import grid  # noqa: E402
import readers  # noqa: E402
import score  # noqa: E402
import stats  # noqa: E402

COLUMNS = ("system", "backend", "bound_us", "step", "phase_target", "index", "phase_ns", "delay_ns",
           "residual_ns", "b_eff_ns")


class Refusal(Exception):
    pass


def sweep_rows(system, backend, bound_us, resolved):
    """The CSV rows of one sweep and the median score_sweep would compute from them."""
    rows = []
    ratios = []
    for r in resolved:
        if not r.ok:
            continue
        b = r.run.summary.get("b_eff_ns")
        step = readers.process_index(r.id)
        target = r.run.summary.get("phase_frac")
        for u in r.run.measured():
            if u.get("residual_ns") is None or not b:
                raise Refusal("%s: post %s has no residual or its process no B_eff" % (r.id, u.get("index")))
            ratios.append(abs(u["residual_ns"]) / b)
            rows.append({"system": system, "backend": backend, "bound_us": bound_us, "step": step,
                         "phase_target": target, "index": u["index"], "phase_ns": u.get("phase_ns"),
                         "delay_ns": u["delay_ns"], "residual_ns": u["residual_ns"], "b_eff_ns": b})
    return rows, (stats.median(ratios) if ratios else None)


def scored_h3(score_json):
    """H3's median per (system, backend, bound) from score.py's output; only complete sweeps."""
    out = {}
    for r in score_json["H3"]["results"]:
        if not r["complete"]:
            raise Refusal("H3 sweep %s %s %s is not complete" % (r["system"], r["backend"], r["bound_us"]))
        out[(r["system"], r["backend"], r["bound_us"])] = r["median_abs_residual_over_b_eff"]
    return out


def collect(systems, score_json):
    """All rows, every sweep checked against the scored value. systems: [(plan, dir)]."""
    scored = scored_h3(score_json)
    seen = set()
    rows = []
    for plan_path, data_dir in systems:
        header, groups, _ = score.resolve_plan(plan_path, data_dir)
        system = header["system"]
        for backend, bound_us in grid.sweep_cells(system):
            resolved = groups.get(("sweep", grid.sweep_name(backend, bound_us)), [])
            got, med = sweep_rows(system, backend, bound_us, resolved)
            key = (system, backend, bound_us)
            if key not in scored:
                raise Refusal("no scored H3 value for %s %s %s" % key)
            if med != scored[key]:
                raise Refusal("%s %s %s: median %r here, %r scored" % (key + (med, scored[key])))
            seen.add(key)
            rows += got
    if seen != set(scored):
        raise Refusal("sweeps scored but not read: %s" % sorted(set(scored) - seen))
    return rows


def to_csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    w.writeheader()
    for row in rows:
        w.writerow(row)
    return buf.getvalue()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--linux", nargs=2, metavar=("PLAN", "DIR"))
    ap.add_argument("--windows", nargs=2, metavar=("PLAN", "DIR"))
    ap.add_argument("--score", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    systems = [s for s in (args.linux, args.windows) if s]
    with open(args.score, encoding="utf-8") as f:
        score_json = json.load(f)
    try:
        rows = collect(systems, score_json)
    except Refusal as e:
        print("refused: %s" % e, file=sys.stderr)
        return 2
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        f.write(to_csv(rows))
    print("%d posts from %d sweeps, each median equal to score.json's H3 value"
          % (len(rows), len({(r["system"], r["backend"], r["bound_us"]) for r in rows})))
    return 0


if __name__ == "__main__":
    sys.exit(main())

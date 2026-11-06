"""Facts the paper states that only the raw run outputs hold, each checked against the scored results.

  python raw_facts.py --linux PLAN DIR --windows PLAN DIR --score score.json
                      --h5-linux DIR --h5-windows DIR --h5 h5.json --records DIR [--records DIR ...]
                      --out RAW-FACTS.json

PLAN and DIR are a system's campaign plan and output folder, as score.py reads them. --h5-linux and
--h5-windows are the H5 output folders, as h5.py reads them, and --records the sanitizer-record
folders h5.py checks its gate against. Nothing is scored, fitted or filtered here: the code that
reads and scores is score.py's and h5.py's, imported unchanged. Three facts are taken:

  processes        for every cell of the power and the false-positive family, each good process's
                   count of trials flagged by the test and its count of trials, as score_cell counts
                   them (score_process against the cell's own B_eff from score.json). The sums must
                   equal the cell's flags and trials in score.json, and the process count its
                   processes_good.
  undrained_rounds for every batch-round cell of undrained edge, the median delay of its rounds (the
                   good processes' measured units pooled, stats.median) and the cell's B_eff; the
                   round count must equal the cell's units in score.json.
  prior_bound      for every bound-sweep run of the prior study's design (h5.py's read path and
                   2026-09's bound_sweep_tables), the median delay of the correct arm in
                   microseconds at each bound, 0 being the blocking wait. Each group's smallest and
                   largest median and their ratio must equal h5.json's.

Any difference is refused and nothing is written.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.dirname(HERE))  # analysis/, for 2026-09's summarize.py

import argparse  # noqa: E402
import json  # noqa: E402

import grid  # noqa: E402
import h5 as h5_mod  # noqa: E402
import score  # noqa: E402
import stats  # noqa: E402
import summarize as s2609  # noqa: E402

FAMILIES = ("F1", "F2")
UNDRAINED = "undrained_edge"


class Refusal(Exception):
    pass


def need(cond, message):
    if not cond:
        raise Refusal(message)


def cell_processes(key, cell, resolved):
    """Per good process: (trials flagged by the test, trials), as score_cell scores them."""
    good = [r for r in resolved if r.ok]
    coarsest = [r.run.summary.get("coarsest_timer_us") for r in good]
    coarsest_ns = max((c for c in coarsest if isinstance(c, (int, float))), default=0) * 1000
    procs = [score.score_process(r.run, cell["b_eff_ns"], cell["bound_us"] * 1000, coarsest_ns) for r in good]
    flags = [p["t_med"] for p in procs]
    trials = [p["trials"] for p in procs]
    need(len(procs) == cell["processes_good"], "%s: %d good processes, %d scored" % (key, len(procs),
                                                                                   cell["processes_good"]))
    need(sum(flags) == cell["t_med"]["flags"] and sum(trials) == cell["t_med"]["trials"],
         "%s: %d of %d trials flagged here, %d of %d scored" % (key, sum(flags), sum(trials),
                                                             cell["t_med"]["flags"], cell["t_med"]["trials"]))
    return {"flags": flags, "trials": trials, "complete": cell["complete"]}


def undrained_rounds(key, cell, resolved):
    delays = [u["delay_ns"] for r in resolved if r.ok for u in r.run.measured()]
    need(len(delays) == cell["units"], "%s: %d rounds here, %d scored" % (key, len(delays), cell["units"]))
    return {"rounds": len(delays), "median_delay_ns": stats.median(sorted(delays)), "b_eff_ns": cell["b_eff_ns"],
            "complete": cell["complete"]}


def campaign(systems, score_json):
    """systems: [(plan, dir)]. The processes and undrained_rounds blocks."""
    processes, rounds = {}, {}
    for plan_path, data_dir in systems:
        header, groups, _ = score.resolve_plan(plan_path, data_dir)
        system = header["system"]
        for key, cell in sorted(score_json["cells"].items()):
            if not key.startswith(system + ":"):
                continue
            resolved = groups.get((cell["protocol"], cell["cell"]))
            need(resolved, "%s: no planned processes" % key)
            if cell["family"] in FAMILIES:
                processes[key] = dict(cell_processes(key, cell, resolved), family=cell["family"])
            if cell["protocol"] == "Bt" and cell["variant"] == UNDRAINED:
                rounds[key] = undrained_rounds(key, cell, resolved)
    for fam in FAMILIES:
        want = {k for k, c in score_json["cells"].items() if c["family"] == fam}
        got = {k for k, v in processes.items() if v["family"] == fam}
        need(want == got, "%s: cells scored but not read: %s" % (fam, sorted(want - got)[:5]))
    return processes, rounds


def prior_bound(h5_dirs, records, h5_json):
    """The correct arm's median delay per bound of each bound-sweep group, checked against h5.json."""
    rows = []
    for system, folder in h5_dirs:
        got = h5_mod.read_system(system, folder, list(records), False)
        loaded = [c["loaded"] for c in got["cells"] if c["complete"] and c["design"] == "bound-sweep"]
        need(len(loaded) == sum(1 for c in got["cells"] if c["design"] == "bound-sweep"),
             "%s: an incomplete bound-sweep run" % system)
        table, _ = s2609.bound_sweep_tables(loaded)
        for r in table:
            rows.append({"system": system, "backend": r["backend"], "variant": r["variant"], "arm": r["arm"],
                         "wait_us": r["wait_us"], "median_us": r["median_us"]})
    groups = {(g["system"], g["backend"], g["variant"]): g for g in h5_json["bound_independence"]["groups"]}
    seen = set()
    for key, g in groups.items():
        mine = [r for r in rows if (r["system"], r["backend"], r["variant"]) == key]
        need(mine and all(r["arm"] == "fixed" for r in mine), "%s: no rows, or a row of another arm" % (key,))
        medians = [r["median_us"] for r in mine]
        need(" ".join(str(r["wait_us"]) for r in mine) == g["bounds_us"], "%s: bounds differ" % (key,))
        need(min(medians) == g["min_median_us"] and max(medians) == g["max_median_us"]
             and max(medians) / min(medians) == g["h2_ratio"], "%s: medians differ from h5.json" % (key,))
        seen.add(key)
    need(seen == {(r["system"], r["backend"], r["variant"]) for r in rows}, "bound-sweep rows outside h5.json")
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--linux", nargs=2, metavar=("PLAN", "DIR"), required=True)
    ap.add_argument("--windows", nargs=2, metavar=("PLAN", "DIR"), required=True)
    ap.add_argument("--score", required=True)
    ap.add_argument("--h5-linux", required=True)
    ap.add_argument("--h5-windows", required=True)
    ap.add_argument("--h5", required=True)
    ap.add_argument("--records", action="append", default=[], required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    with open(a.score, encoding="utf-8") as f:
        score_json = json.load(f)
    with open(a.h5, encoding="utf-8") as f:
        h5_json = json.load(f)
    try:
        need(not score_json.get("draft") and not h5_json.get("draft"), "a draft input")
        processes, rounds = campaign([tuple(a.linux), tuple(a.windows)], score_json)
        bound = prior_bound([("linux", a.h5_linux), ("windows", a.h5_windows)], a.records, h5_json)
    except (Refusal, h5_mod.Refusal) as e:
        print("refused: %s" % e, file=sys.stderr)
        return 2
    out = {"generated_by": "analysis/v2/raw_facts.py", "draft": False,
           "processes_per_cell": grid.PROCESSES_PER_CELL, "processes": processes,
           "undrained_rounds": rounds, "prior_bound": bound}
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(stats.jsonable(out), f, indent=1, sort_keys=True)
        f.write("\n")
    print("%d family cells, %d undrained-edge round cells, %d bound-sweep runs; every sum equal to the scored one"
          % (len(processes), len(rounds), len(bound)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

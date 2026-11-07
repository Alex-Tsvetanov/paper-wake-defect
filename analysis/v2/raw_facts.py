"""Facts the paper states that only the raw run outputs hold, each checked against the scored results.

  python raw_facts.py --linux PLAN DIR --windows PLAN DIR --score score.json
                      --h5-linux DIR --h5-windows DIR --h5 h5.json --records DIR [--records DIR ...]
                      --out RAW-FACTS.json

PLAN and DIR are a system's campaign plan and output folder, as score.py reads them. --h5-linux and
--h5-windows are the H5 output folders, as h5.py reads them, and --records the sanitizer-record
folders h5.py checks its gate against. Nothing is scored, fitted or filtered here: the code that
reads and scores is score.py's and h5.py's, imported unchanged. The facts taken:

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

Added 2026-11-07 (round 4 of the writer's requests):

  h4_throughput    for every H4 row of score.json (fixed loop over absent wake, plain wake over absent
                   wake), the median tasks per second of each arm's abpair windows (readers.throughput)
                   over the good processes score_h4 used; the pair ratios recomputed from those windows
                   must equal the row's ratios, in order.
  prior_design     each system's H5 plan.txt (h5.py's read_plan), which must be the runners' design
                   (h5.py's design_problem); the file names are dropped.

Added 2026-11-07 (round 5 of the writer's requests):

  trial_medians    for every cell of single posts and batch rounds of score.json, per good process:
                   its trials, the median over its trials of each trial's median delay (21
                   measured units, as score_process cuts them, stats.median), and the trials
                   flagged with the cell's
                   B_eff and with the requested bound B as the threshold base. The B_eff counts must
                   equal the cell's T_med flags and trials in score.json, and the process count its
                   processes_good. Per cell also the median of all its trials' medians, pooled. The
                   B count is descriptive only: it is no test of the design.
  netty_posts      for the buggy version's natural cell of H6, per good process (h6.py's read_plan,
                   read_process and trials_of, unchanged): its posts and its posts above half the
                   bound. The sums must equal h6.json's posts and posts_over_half_b, and each
                   process's count its entries of h6.json's over_half_b.

Added 2026-11-07 (round 6 of the writer's requests):

  divisor_flags    for every cell of single posts and batch rounds, per good process: its trials and,
                   for each divisor d of RECOUNT_DIVISORS, its trials whose median delay exceeds
                   B_eff / d (as d x median > B_eff). The count at the test's own divisor must equal
                   the cell's T_med flags and trials in score.json. Descriptive only, outside the
                   families: the design names one divisor.
  calibrated_periods for every single-post cell of a variant that calibrates (grid.delays_single_posts),
                   its good processes' calibrated periods (summary field b_eff_ns, from wakeprobe's
                   calibration posts) with the cell's B_eff; for a B_eff source cell whose B_eff was
                   measured, their median must equal it. Every other cell must report no period.

Any difference is refused and nothing is written.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.dirname(HERE))  # analysis/, for 2026-09's summarize.py
sys.path.insert(2, os.path.join(HERE, "..", "..", "historical", "netty"))  # run_h6.py, which h6.py imports

import argparse  # noqa: E402
import json  # noqa: E402

import grid  # noqa: E402
import h5 as h5_mod  # noqa: E402
import readers  # noqa: E402
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


def h4_throughput(systems, score_json):
    """Per H4 row of score.json (fixed loop and plain wake, each over absent wake): every abpair window's
    tasks per second of each arm (readers.throughput), from the good processes score_h4 used. The
    pair ratios recomputed from these windows must equal the row's ratios, in order."""
    rows = {}
    scored = {(r["key"], r["arm_a"]): r for part in ("fixed_over_absent", "plain_over_absent")
              for r in score_json["H4"][part]}
    for plan_path, data_dir in systems:
        header, groups, _ = score.resolve_plan(plan_path, data_dir)
        for (family, name), resolved in sorted(groups.items()):
            if family != "H4":
                continue
            good = [r for r in resolved if r.ok]
            need(good, "%s: no good process" % name)
            rows_here = [r for r in scored.values() if r["system"] == header["system"]
                         and grid.h4_cell_name(r["backend"], r["work_us"], r["arm_a"]) == name]
            need(len(rows_here) == 1, "%s: %d scored H4 rows" % (name, len(rows_here)))
            row = rows_here[0]
            ratios = [x for r in good for x in readers.pair_ratios(r.run)]
            need(ratios == row["ratios"], "%s: pair ratios differ from score.json" % name)
            sides = {"A": [], "B": []}
            for r in good:
                for w in r.run.records:
                    want = row["arm_a"] if w["side"] == "A" else row["arm_b"]
                    need(w["arm"] == want and w["ran"] == want, "%s: a window of arm %s on side %s" % (name, w["arm"], w["side"]))
                    sides[w["side"]].append(readers.throughput(w))
            need(len(sides["A"]) == len(sides["B"]) == row["pairs"], "%s: windows and pairs differ" % name)
            rows["%s-%s" % (row["key"], row["arm_a"])] = {
                "key": row["key"], "system": row["system"], "backend": row["backend"], "work_us": row["work_us"],
                "arm_a": row["arm_a"], "arm_b": row["arm_b"], "windows": row["pairs"],
                "median_a_tasks_per_s": stats.median(sorted(sides["A"])),
                "median_b_tasks_per_s": stats.median(sorted(sides["B"]))}
    need(set(rows) == {"%s-%s" % k for k in scored}, "H4 rows scored but not read")
    return rows


def prior_design(h5_dirs):
    """The prior study's designs as run: each system's plan.txt (h5.py's read_plan), which must be the
    runners' design (h5.py's design_problem against DESIGN_CELLS and DESIGN_SIZES)."""
    out = {}
    for system, folder in h5_dirs:
        plan = h5_mod.read_plan(folder)
        problem = h5_mod.design_problem(system, plan["cells"], h5_mod.DESIGN_CELLS)
        need(problem is None, "%s: %s" % (system, problem))
        files = [p["file"] for p in plan["cells"]]
        need(len(files) == len(set(files)), "%s: two plan lines write one file" % system)
        out[system] = [{k: v for k, v in p.items() if k != "file"} for p in plan["cells"]]
    return out


PROTOCOLS = ("S", "Bt")


def process_trial_medians(run, b_eff, bound_ns):
    """One good process: each trial's median delay, and the trials flagged against B_eff and against B."""
    units = run.measured()
    per = grid.UNITS_PER_TRIAL
    medians = [stats.median([u["delay_ns"] for u in units[t * per:(t + 1) * per]]) for t in range(len(units) // per)]
    return {"trials": len(medians), "medians": medians, "median_trial_median_ns": stats.median(sorted(medians)),
            "flags_b_eff": sum(1 for x in medians if grid.T_MED_DIVISOR * x > b_eff),
            "flags_requested": sum(1 for x in medians if grid.T_MED_DIVISOR * x > bound_ns)}


def trial_medians(systems, score_json):
    """Per cell of single posts and batch rounds: each good process's trial medians and flag counts."""
    out = {}
    for plan_path, data_dir in systems:
        header, groups, _ = score.resolve_plan(plan_path, data_dir)
        system = header["system"]
        for key, cell in sorted(score_json["cells"].items()):
            if not key.startswith(system + ":") or cell["protocol"] not in PROTOCOLS:
                continue
            resolved = groups.get((cell["protocol"], cell["cell"]))
            need(resolved, "%s: no planned processes" % key)
            procs = [process_trial_medians(r.run, cell["b_eff_ns"], cell["bound_us"] * 1000)
                     for r in resolved if r.ok]
            need(len(procs) == cell["processes_good"], "%s: %d good processes, %d read" % (key, len(procs),
                                                                                         cell["processes_good"]))
            flags, trials = sum(p["flags_b_eff"] for p in procs), sum(p["trials"] for p in procs)
            need(flags == cell["t_med"]["flags"] and trials == cell["t_med"]["trials"],
                 "%s: %d of %d trials flagged here, %d of %d scored" % (key, flags, trials, cell["t_med"]["flags"],
                                                                     cell["t_med"]["trials"]))
            pooled = sorted(x for p in procs for x in p.pop("medians"))
            out[key] = {"protocol": cell["protocol"], "family": cell["family"], "complete": cell["complete"],
                        "b_eff_ns": cell["b_eff_ns"], "bound_ns": cell["bound_us"] * 1000,
                        "median_trial_median_ns": stats.median(pooled) if pooled else None, "processes": procs}
    want = {k for k, c in score_json["cells"].items() if c["protocol"] in PROTOCOLS}
    need(set(out) == want, "cells scored but not read: %s" % sorted(want - set(out))[:5])
    return out


RECOUNT_DIVISORS = (2, 4, grid.T_MED_DIVISOR, 10)  # the test's own divisor is the check


def recount_and_periods(systems, score_json):
    """The divisor_flags and calibrated_periods blocks, read in one pass over the campaign outputs."""
    flags, periods = {}, {}
    for plan_path, data_dir in systems:
        header, groups, _ = score.resolve_plan(plan_path, data_dir)
        system = header["system"]
        for key, cell in sorted(score_json["cells"].items()):
            if not key.startswith(system + ":") or cell["protocol"] not in PROTOCOLS:
                continue
            resolved = groups.get((cell["protocol"], cell["cell"]))
            need(resolved, "%s: no planned processes" % key)
            good = [r for r in resolved if r.ok]
            need(len(good) == cell["processes_good"], "%s: %d good processes, %d read" % (key, len(good),
                                                                                        cell["processes_good"]))
            flags[key] = divisor_cell(key, cell, good)
            got = [r.run.summary.get("b_eff_ns") for r in good]
            delaying = cell["protocol"] == "S" and grid.delays_single_posts(cell["variant"], cell["workers"])
            need(delaying or all(g is None for g in got), "%s: a period where no calibration runs" % key)
            if delaying:
                periods[key] = period_cell(key, cell, got)
    want = {k for k, c in score_json["cells"].items() if c["protocol"] in PROTOCOLS}
    need(set(flags) == want, "cells scored but not recounted: %s" % sorted(want - set(flags))[:5])
    return flags, periods


def divisor_cell(key, cell, good):
    """Per good process, its trials and its trials whose median delay exceeds B_eff / d, for each d."""
    per = grid.UNITS_PER_TRIAL
    procs = []
    for r in good:
        units = r.run.measured()
        medians = [stats.median([u["delay_ns"] for u in units[t * per:(t + 1) * per]])
                   for t in range(len(units) // per)]
        procs.append({"trials": len(medians),
                      "flags": {str(d): sum(1 for x in medians if d * x > cell["b_eff_ns"]) for d in RECOUNT_DIVISORS}})
    own = str(grid.T_MED_DIVISOR)
    need(sum(p["flags"][own] for p in procs) == cell["t_med"]["flags"]
         and sum(p["trials"] for p in procs) == cell["t_med"]["trials"],
         "%s: the recount at the test's own divisor differs from score.json" % key)
    return {"protocol": cell["protocol"], "family": cell["family"], "complete": cell["complete"],
            "processes": procs}


def period_cell(key, cell, got):
    """The calibrated period (summary field b_eff_ns) of each good process of a delaying single-post cell."""
    source = grid.beff_source_cell(cell["system"], cell["backend"], cell["bound_us"]).name == cell["cell"]
    need(source == (cell["b_eff_from"] == cell["cell"]), "%s: its B_eff source differs from the rule's" % key)
    values = sorted(g for g in got if isinstance(g, (int, float)) and g > 0)
    if source and cell["b_eff_source"].startswith("measured"):
        need(stats.median(values) == cell["b_eff_ns"], "%s: the source cell's median is not its B_eff" % key)
    return {"variant": cell["variant"], "workers": cell["workers"], "system": cell["system"],
            "bound_ns": cell["bound_us"] * 1000, "b_eff_ns": cell["b_eff_ns"], "b_eff_source_cell": source,
            "complete": cell["complete"], "processes": len(got), "periods_ns": values}


NETTY_CELL = ("cr7", "natural")


def netty_posts(h6_dir, h6_json):
    """The buggy version's natural cell: per good process, its posts and its posts above half the bound."""
    import h6 as h6_mod  # its run_h6 lives under historical/netty
    entries = [e for e in h6_mod.read_plan(h6_dir) if (e["arm"], e["protocol"]) == NETTY_CELL]
    cell = h6_json["cells"]["%s-%s" % NETTY_CELL]
    scored = {}
    for x in cell["over_half_b"]:
        scored[x["process"]] = scored.get(x["process"], 0) + 1
    rows = []
    for e in entries:
        p = h6_mod.read_process(h6_dir, e)
        if not p["good"]:
            continue
        delays = [d for t in h6_mod.trials_of(p) for d in t]
        over = sum(1 for d in delays if d > h6_mod.T_COUNT_NS)
        need(over == scored.get(e["process"], 0), "H6 process %s: %d posts over B/2 here, %d scored"
             % (e["process"], over, scored.get(e["process"], 0)))
        rows.append({"process": e["process"], "posts": len(delays), "over_half_b": over})
    need(len(rows) == cell["processes_good"], "H6: %d good processes, %d scored" % (len(rows), cell["processes_good"]))
    need(sum(r["posts"] for r in rows) == cell["posts"], "H6: posts differ from h6.json")
    need(sum(r["over_half_b"] for r in rows) == cell["posts_over_half_b"], "H6: posts over B/2 differ from h6.json")
    return sorted(rows, key=lambda r: r["process"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--linux", nargs=2, metavar=("PLAN", "DIR"), required=True)
    ap.add_argument("--windows", nargs=2, metavar=("PLAN", "DIR"), required=True)
    ap.add_argument("--score", required=True)
    ap.add_argument("--h5-linux", required=True)
    ap.add_argument("--h5-windows", required=True)
    ap.add_argument("--h5", required=True)
    ap.add_argument("--records", action="append", default=[], required=True)
    ap.add_argument("--h6-run", required=True, help="the H6 archive's out/h6 folder, as h6.py reads it")
    ap.add_argument("--h6", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    with open(a.score, encoding="utf-8") as f:
        score_json = json.load(f)
    with open(a.h5, encoding="utf-8") as f:
        h5_json = json.load(f)
    with open(a.h6, encoding="utf-8") as f:
        h6_json = json.load(f)
    try:
        need(not score_json.get("draft") and not h5_json.get("draft") and not h6_json.get("draft"), "a draft input")
        processes, rounds = campaign([tuple(a.linux), tuple(a.windows)], score_json)
        bound = prior_bound([("linux", a.h5_linux), ("windows", a.h5_windows)], a.records, h5_json)
        throughput = h4_throughput([tuple(a.linux), tuple(a.windows)], score_json)
        designs = prior_design([("linux", a.h5_linux), ("windows", a.h5_windows)])
        medians = trial_medians([tuple(a.linux), tuple(a.windows)], score_json)
        netty = netty_posts(a.h6_run, h6_json)
        recount, periods = recount_and_periods([tuple(a.linux), tuple(a.windows)], score_json)
    except (Refusal, h5_mod.Refusal) as e:
        print("refused: %s" % e, file=sys.stderr)
        return 2
    out = {"generated_by": "analysis/v2/raw_facts.py", "draft": False,
           "processes_per_cell": grid.PROCESSES_PER_CELL, "processes": processes,
           "undrained_rounds": rounds, "prior_bound": bound,
           "h4_throughput": throughput, "prior_design": designs,
           "trial_medians": medians, "netty_posts": netty,
           "divisor_flags": recount, "calibrated_periods": periods}
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(stats.jsonable(out), f, indent=1, sort_keys=True)
        f.write("\n")
    print("%d family cells, %d undrained-edge round cells, %d bound-sweep runs; every sum equal to the scored one"
          % (len(processes), len(rounds), len(bound)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

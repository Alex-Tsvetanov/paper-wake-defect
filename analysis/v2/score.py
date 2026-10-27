"""Scoring of the P1 v2 campaign (hypotheses-v2.md, sections 1 to 5, 7 and 8).

  python score.py [--linux PLAN DIR] [--windows PLAN DIR] [--revision-log FILE | --no-revision-log]
                  [--draft] --out RESULTS.json

PLAN is a system's plan (plan.py) and DIR the folder of its outputs, as the campaign runner
leaves them (readers.py): ID.jsonl, ID-rerun.jsonl, ID.done and the void runs. A process is good
only by readers.py's rules. A rerun's data replace the first run's (section 8) when the first run
failed, and both runs are reported.

The cells the A/A pilot declared underpowered come from the h4-r block of the revision log
(default: revision-log-v2.md at the repository root; h4log.py), the same record plan.py reads R
from. The output records the log's file name and sha256 as H4.h4_r_source. A log without a usable
block is refused.

Provenance. The analysis commit is read from git (no override) and the working tree must be
clean. The scoring is refused when:
  - a plan's header is not of the real form of a campaign plan (C2);
  - the log's sha256 in a plan's header differs from the sha256 of the log read;
  - a plan's lines differ from the ones plan.py makes, or an H4 comparison does not have R / 2
    processes for its cell in the log;
  - the good processes were not all built from one commit (section 1: every build, on both
    systems, is the Release build of one commit).
--draft is for dry runs and tests: those four become warnings, --no-revision-log is allowed, no
clean tree is needed, and the output is marked draft.

F1, F2, F4 and F4c each hold cells of both systems, so Holm runs over both. Every cell of the
frozen grid counts in its family's m; a cell with no data (a system not given, a process lost
twice, an H4 cell under 14 pairs) is incomplete and not rejected. Given one system only, the
other system's cells are all incomplete and the output says so.

Output: one JSON file. No TeX.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse  # noqa: E402
import json  # noqa: E402

import beff as beff_mod  # noqa: E402
import grid  # noqa: E402
import h4log  # noqa: E402
import plan as plan_mod  # noqa: E402
import provenance  # noqa: E402
import readers  # noqa: E402
import stats  # noqa: E402

REFUSAL_SHOWN = 6  # problems named in a refusal; the rest are counted

# ---------------------------------------------------------------------------------------------
# Reading one system
# ---------------------------------------------------------------------------------------------


def resolve_plan(plan_path, data_dir):
    """The plan's processes, grouped by (family, cell), each with its rerun rule applied.

    Returns (header, groups, ids), ids being the plan's process ids in the order of the file.
    """
    header, entries = readers.load_plan(plan_path)
    system = header["system"]
    if system not in grid.SYSTEMS:
        raise ValueError("%s: unknown system %r" % (plan_path, system))
    folder = readers.DataDir(data_dir)
    groups = {}
    for e in entries:
        groups.setdefault((e["family"], e["cell"]), []).append(readers.Resolved(e, folder, system))
    for resolved in groups.values():
        resolved.sort(key=lambda r: readers.process_index(r.id))
    return header, groups, [e["id"] for e in entries]


# ---------------------------------------------------------------------------------------------
# S and Bt cells
# ---------------------------------------------------------------------------------------------


def score_process(run, b_eff, bound_ns, coarsest_ns):
    """One process: trials of 21 measured units in order, T_med and T_count from delay_ns.

    timed_out is section 8's count: measured units delivered after 5 x max(B, B_eff, the
    coarsest timer resolution) and before the hard limit. "After" is strict. A unit not delivered
    by the hard limit is lost and its process is not good (readers.wakeprobe_problem), so every
    unit seen here was delivered before it. B_eff is section 2's value for this process's
    backend, system and B (b_eff, from beff.py: the median calibrated B_eff of the absent-wake,
    W = 1, S cell, or its fallback), the same value as the T_med and T_count thresholds. It is
    not this process's own calibration, which sets wakeprobe's own timeout flag; that flag is
    kept only as a cross-check (timed_out_wakeprobe, reported under `diagnostics`).
    """
    units = run.measured()
    per_trial = grid.UNITS_PER_TRIAL
    t_med = t_count = 0
    for t in range(len(units) // per_trial):
        delays = [u["delay_ns"] for u in units[t * per_trial:(t + 1) * per_trial]]
        if grid.T_MED_DIVISOR * stats.median(delays) > b_eff:
            t_med += 1
        if any(grid.T_COUNT_DIVISOR * d > b_eff for d in delays):
            t_count += 1
    delays = [u["delay_ns"] for u in units]
    limit = grid.TIMEOUT_FACTOR * max(bound_ns, b_eff, coarsest_ns or 0)
    by_index = {r.get("index"): r for r in run.records}
    gaps = []
    for u in units:
        prev = by_index.get(u.get("index", 0) - 1)
        if prev is not None and prev.get("exec_ns") is not None:
            gaps.append(u["post_ns"] - prev["exec_ns"])
    return {"trials": len(units) // per_trial, "t_med": t_med, "t_count": t_count, "units": len(units),
            "units_over_half": sum(1 for d in delays if grid.T_COUNT_DIVISOR * d > b_eff),
            "timed_out": sum(1 for d in delays if d > limit),
            "timed_out_wakeprobe": sum(1 for u in units if u.get("timed_out") or u.get("gate_timed_out")),
            "gaps": gaps}


def rate_block(counts, sizes, clustered=True, dispersion=True):
    """A rate with its exact 95% interval, its clustered interval and the overdispersion check."""
    k = sum(counts)
    n = sum(sizes)
    out = {"flags": k, "trials": n, "rate": k / n if n else None,
           "exact_95": stats.clopper_pearson(k, n) if n else None}
    if clustered:
        out["clustered_95"] = stats.rate_interval(counts, sizes) if n else None
    if clustered and dispersion:
        od = stats.overdispersion(counts, sizes) if n else None
        out["overdispersion"] = od
        if od is not None and not od["defined"]:
            out["note"] = "all or none flag: the dispersion index is undefined, the cell is not overdispersed, " \
                          "and its clustered interval is a single point; only the exact interval is stated"
    return out


def score_cell(cell, resolved, beff):
    """One S or Bt cell against its backend's B_eff."""
    b_eff = beff["b_eff_ns"]
    bound_ns = cell.bound_us * 1000
    good = [r for r in resolved if r.ok]
    complete = len(resolved) == grid.PROCESSES_PER_CELL and len(good) == grid.PROCESSES_PER_CELL
    coarsest = [r.run.summary.get("coarsest_timer_us") for r in good]
    coarsest_ns = max((c for c in coarsest if isinstance(c, (int, float))), default=0) * 1000
    procs = [score_process(r.run, b_eff, bound_ns, coarsest_ns) for r in good]
    sizes = [p["trials"] for p in procs]
    t_med = [p["t_med"] for p in procs]
    t_count = [p["t_count"] for p in procs]
    lost = [r.lost_units() for r in resolved]
    gaps = sorted(g for p in procs for g in p["gaps"])
    out = {
        "cell": cell.name, "system": cell.system, "protocol": cell.protocol, "backend": cell.backend,
        "variant": cell.variant, "workers": cell.workers, "bound_us": cell.bound_us,
        "family": cell.family, "replication": cell.replication, "blind_spot": cell.blind_spot,
        "complete": complete, "processes_planned": len(resolved), "processes_good": len(good),
        "b_eff_ns": b_eff, "b_eff_source": beff["source"], "b_eff_from": beff["from_cell"],
        "t_med": rate_block(t_med, sizes),
        # Section 5: every rate gets a clustered interval. The overdispersion check of T_count is
        # reported on the F2 cells, where T_count's false-positive rate is read (section 4).
        "t_count": rate_block(t_count, sizes, dispersion=cell.family == "F2"),
        "units": sum(p["units"] for p in procs), "units_over_half_b_eff": sum(p["units_over_half"] for p in procs),
        # Section 8: the one count of timed-out units (measured units only), by its formula.
        "timed_out": sum(p["timed_out"] for p in procs),
        # A cross-check, not a result: wakeprobe's own flag counts against its process's own
        # calibration, so it can differ from `timed_out` in either direction.
        "diagnostics": {"timed_out_wakeprobe": sum(p["timed_out_wakeprobe"] for p in procs)},
        "lost_units_first": sum(x["first"] for x in lost), "lost_units_rerun": sum(x["rerun"] for x in lost),
        "lost_units_void": sum(x["void"] for x in lost),
        "realized_gap_over_b": None if not gaps else {
            "min": gaps[0] / bound_ns, "median": stats.median(gaps) / bound_ns, "max": gaps[-1] / bound_ns},
        "processes": [r.report() for r in resolved],
    }
    if cell.variant in (grid.FIXED, grid.PLAIN) and sum(sum(x.values()) for x in lost):
        # Section 8: a lost unit in a fixed or plain cell is itself a lost wake, whichever run lost
        # it: a first run that was then rerun, the rerun, or a void run.
        out["lost_wake"] = True
        out["lost_wake_runs"] = [{"process": r.id, "run": name, "lost_units": n}
                                 for r in resolved for name, n in r.lost_unit_runs()]
    flags, trials = out["t_med"]["flags"], out["t_med"]["trials"]
    if cell.family == "F1":
        out["p"] = stats.h1_pvalue(flags, trials) if complete else None
    elif cell.family == "F2":
        out["p"] = stats.h2_pvalue(flags, trials) if complete else None
    if cell.blind_spot and trials:
        lower = stats.cp_lower(flags, trials, grid.BLIND_CONFIDENCE)
        out["blind_lower_95"] = lower
        out["blind_prediction_fails"] = lower > grid.BLIND_RATE
    if cell.lazy_wake and out["units"]:
        # Section 4: the rate of delays above B_eff / 2 per post (per round under Bt), with its
        # exact one-sided 95% upper bound, its exact interval and its clustered interval.
        out["per_unit_over_half"] = rate_block([p["units_over_half"] for p in procs], [p["units"] for p in procs],
                                               dispersion=False)
        out["per_unit_over_half_upper_95"] = stats.cp_upper(out["units_over_half_b_eff"], out["units"],
                                                            grid.BLIND_CONFIDENCE)
    return out


# ---------------------------------------------------------------------------------------------
# H3, the phase sweep
# ---------------------------------------------------------------------------------------------


def score_sweep(system, backend, bound_us, resolved):
    """|residual| / the post's own process's B_eff, median over the sweep's 630 posts."""
    ratios = []
    missing = 0
    good = [r for r in resolved if r.ok]
    for r in good:
        b = r.run.summary.get("b_eff_ns")
        for u in r.run.measured():
            if u.get("residual_ns") is None or not b:
                missing += 1
            else:
                ratios.append(abs(u["residual_ns"]) / b)
    complete = len(resolved) == grid.SWEEP_STEPS and len(good) == grid.SWEEP_STEPS
    med = stats.median(ratios) if ratios else None
    if not complete:
        verdict = "incomplete"
    else:
        verdict = "holds" if med is not None and med < grid.H3_CRITERION else "fails"
    return {"system": system, "backend": backend, "bound_us": bound_us, "complete": complete,
            "processes_planned": len(resolved), "processes_good": len(good), "posts": len(ratios),
            "posts_without_residual": missing, "median_abs_residual_over_b_eff": med,
            "criterion": "< %s" % grid.H3_CRITERION, "verdict": verdict,
            "lost_units": sum(sum(r.lost_units().values()) for r in resolved),
            "processes": [r.report() for r in resolved]}


# ---------------------------------------------------------------------------------------------
# H4
# ---------------------------------------------------------------------------------------------


def _mean_of_pairs(groups):
    return sum(sum(g) for g in groups) / sum(len(g) for g in groups)


def score_h4(system, backend, work_us, arm_a, resolved):
    """Per-pair ratios arm A over absent wake, the sign tests and the clustered interval."""
    groups = [readers.pair_ratios(r.run) for r in resolved if r.ok]
    groups = [g for g in groups if g]
    ratios = [x for g in groups for x in g]
    tests = stats.h4_sign_tests(ratios)
    complete = len(ratios) >= grid.H4_R_MIN
    out = {"system": system, "backend": backend, "work_us": work_us, "arm_a": arm_a, "arm_b": grid.H4_ARM_B,
           "key": grid.h4_key(system, backend, work_us), "pairs_planned": len(resolved) * grid.H4_PAIRS_PER_PROCESS,
           "pairs": len(ratios), "complete": complete, "ratios": ratios,
           "median_ratio": stats.median(ratios) if ratios else None,
           "mean_ratio": sum(ratios) / len(ratios) if ratios else None,
           "clustered_95_mean_ratio": stats.cluster_bootstrap(groups, _mean_of_pairs) if groups else None,
           "sign_tests": tests, "p": tests["p"] if complete else None,
           "p_cost": tests["p_cost"] if complete else None,
           "processes": [r.report() for r in resolved]}
    med = out["median_ratio"]
    if med is not None:
        out["median_position"] = ("below %s" % grid.H4_MARGIN_LOW if med < grid.H4_MARGIN_LOW else
                                  "above %s" % grid.H4_MARGIN_HIGH if med > grid.H4_MARGIN_HIGH else "within")
    return out


# ---------------------------------------------------------------------------------------------
# One system
# ---------------------------------------------------------------------------------------------


def plan_departures(system, groups, h4_r=None, order=None):
    """How the plan's lines differ from the frozen grid (provenance.departures), H4 included.

    S, Bt and sweep lines are compared with plan.py's own, field by field (limit_s too). H4 lines
    are compared with the lines of a plan with R = 24 (a smaller R keeps a prefix of each
    comparison's lines), and a comparison's process count must be R / 2 for its cell in the
    revision log (h4_r), or 7 to 12 where there is no log. order, the plan's ids as the file lists
    them, must be the order plan.py shuffles the same lines into (section 8). Returns {"differ",
    "extra", "missing", "h4_counts", "order"}; order is a sentence or None.
    """
    entries = [r.entry for resolved in groups.values() for r in resolved]
    h4_entries = [e for e in entries if e["family"] == "H4"]
    expected = {e["id"]: e for e in plan_mod.s_bt_entries(system) + plan_mod.sweep_entries(system)}
    found = provenance.departures(expected, [e for e in entries if e["family"] != "H4"])
    cap = {cell: grid.H4_R_MAX for cell in grid.h4_cells(system)}
    cap_found = provenance.departures({e["id"]: e for e in plan_mod.h4_entries(system, cap)}, h4_entries)
    found["differ"] = sorted(found["differ"] + cap_found["differ"])
    found["extra"] = sorted(found["extra"] + cap_found["extra"])
    found["h4_counts"] = []
    for backend, work_us in grid.h4_cells(system):
        for arm_a in grid.H4_COMPARISONS:
            name = grid.h4_cell_name(backend, work_us, arm_a)
            planned = sum(1 for e in h4_entries if e["cell"] == name)
            if h4_r is not None:
                wanted = [h4_r[grid.h4_key(system, backend, work_us)]["R"] // grid.H4_PAIRS_PER_PROCESS]
            else:
                wanted = list(range(grid.H4_R_MIN // grid.H4_PAIRS_PER_PROCESS,
                                    grid.H4_R_MAX // grid.H4_PAIRS_PER_PROCESS + 1))
            if planned not in wanted:
                found["h4_counts"].append({"cell": name, "planned": planned, "wanted": wanted})
    # The order is shuffled over the lines the plan holds, whatever R the log asks for (the count
    # is checked above): read each cell's R off the plan itself.
    found["order"] = None
    r_by_cell = {}
    for backend, work_us in grid.h4_cells(system):
        planned = sum(1 for e in h4_entries if e["cell"] == grid.h4_cell_name(backend, work_us, grid.H4_ARM_A_FIX))
        r_by_cell[(backend, work_us)] = planned * grid.H4_PAIRS_PER_PROCESS
    valid = range(grid.H4_R_MIN, grid.H4_R_MAX + 1, grid.H4_PAIRS_PER_PROCESS)
    if order is not None and all(r in valid for r in r_by_cell.values()):
        want = [e["id"] for e in plan_mod.campaign_entries(system, r_by_cell)]
        found["order"] = provenance.order_text(system, order, want)
    return found


def departure_problems(system, found):
    """The departures as sentences, the H4 process counts included."""
    out = provenance.departure_texts(system, found)
    for c in found["h4_counts"]:
        want = ("R / 2 = %d for this cell in the revision log" % c["wanted"][0] if len(c["wanted"]) == 1
                else "%d to %d" % (c["wanted"][0], c["wanted"][-1]))
        out.append("%s: H4 comparison %s has %d processes in the plan, not %s" % (system, c["cell"], c["planned"], want))
    return out


def refusal(problems):
    """The Refusal for a list of provenance problems, naming the first few."""
    shown = problems[:REFUSAL_SHOWN]
    more = "; and %d more" % (len(problems) - len(shown)) if len(problems) > len(shown) else ""
    return plan_mod.Refusal("; ".join(shown) + more)


def score_system(plan_path, data_dir, h4_r=None, strict=False):
    """One system's cells, sweeps and H4 comparisons. strict: refuse, before scoring anything, a plan whose
    header or lines are not the real ones (score() asks for it outside draft)."""
    header, groups, order = resolve_plan(plan_path, data_dir)
    system = header["system"]
    problems = []  # what the plan and the builds lack as provenance; refused outside --draft
    found = provenance.header_problem(header, system, "campaign")
    if found:
        problems.append("%s: %s" % (system, found))
    departures = plan_departures(system, groups, h4_r, order)
    problems += departure_problems(system, departures)
    if strict and problems:
        raise refusal(problems)
    commits = provenance.bench_commits(r for resolved in groups.values() for r in resolved)
    by_name = {}
    for (family, name), resolved in groups.items():
        by_name.setdefault(family, {})[name] = resolved

    s_bt = dict(by_name.get("S", {}), **by_name.get("Bt", {}))
    expected = {c.name for c in grid.all_cells(system)}
    unknown = sorted(set(s_bt) - expected)
    if unknown:
        raise ValueError("%s: cells not in the frozen grid: %s" % (plan_path, unknown[:5]))

    # The coarsest timer resolution comes from any wakeprobe summary of the system, good or not.
    summaries = [run.summary for resolved in groups.values() for r in resolved if r.entry["tool"] == "wakeprobe"
                 for run in r.runs() if run.summary]
    beff = beff_mod.beff_table(system, by_name.get("S", {}), summaries)
    cells = {}
    for cell in grid.all_cells(system):
        cells[cell.name] = score_cell(cell, s_bt.get(cell.name, []), beff[(cell.backend, cell.bound_us)])

    sweeps = []
    for backend, bound_us in grid.sweep_cells(system):
        sweeps.append(score_sweep(system, backend, bound_us,
                                  by_name.get("sweep", {}).get(grid.sweep_name(backend, bound_us), [])))

    h4 = {}
    for backend, work_us in grid.h4_cells(system):
        for arm_a in grid.H4_COMPARISONS:
            name = grid.h4_cell_name(backend, work_us, arm_a)
            h4[(backend, work_us, arm_a)] = score_h4(system, backend, work_us, arm_a,
                                                     by_name.get("H4", {}).get(name, []))
    return {"system": system, "plan_header": header, "warnings": [],
            "provenance": {"problems": problems, "departures": departures, "bench_commits": commits,
                           "plan_log_sha256": provenance.log_digest(header)},
            "b_eff": [dict(v, backend=k[0]) for k, v in sorted(beff.items())],
            "cells": cells, "sweeps": sweeps, "h4": h4}


# ---------------------------------------------------------------------------------------------
# Both systems: Holm, verdicts and reports
# ---------------------------------------------------------------------------------------------


def _family_result(name, hypothesis, tests):
    rejected = sorted(k for k, t in tests.items() if t["rejected"])
    incomplete = sorted(k for k, t in tests.items() if t["p"] is None)
    failing = sorted(k for k, t in tests.items() if t["p"] is not None and not t["rejected"])
    return {"family": name, "hypothesis": hypothesis, "m": len(tests), "alpha": grid.ALPHA,
            "holds_on_every_cell": len(rejected) == len(tests), "rejected": len(rejected),
            "failing_cells": failing, "incomplete_cells": incomplete, "tests": tests}


def _underpowered(h4_r):
    """The H4 keys the pilot declared underpowered; h4_r is the log block's rows (h4log), or None."""
    return {k for k, row in (h4_r or {}).items() if row["underpowered"]}


def provenance_problems(systems, h4_r_source):
    """Everything the plans and the builds lack as provenance, as sentences (see the module docstring)."""
    problems = [p for res in systems.values() for p in res["provenance"]["problems"]]
    for system, res in sorted(systems.items()):
        digest = res["provenance"]["plan_log_sha256"]
        if h4_r_source is None:
            problems.append("%s: no revision log was read, so the log hash in the plan header cannot be checked" % system)
        elif digest is not None and digest != h4_r_source["sha256"]:
            problems.append("%s: the plan header records a log with sha256 %s, and the log read has %s" % (
                system, digest, h4_r_source["sha256"]))
    commits = {}
    for res in systems.values():
        for commit, n in res["provenance"]["bench_commits"].items():
            commits[commit] = commits.get(commit, 0) + n
    found = provenance.commit_problem(commits)
    if found:
        problems.append(found)
    return problems


def score(inputs, h4_r=None, generated_by="analysis/v2/score.py", h4_r_source=None, draft=False):
    """inputs: (plan, data_dir) per system; h4_r: the revision log's h4-r rows (h4log.read_log), or
    None; h4_r_source: the {file, sha256} of that log, recorded in the output.

    Outside draft, a provenance problem raises plan.Refusal; with draft each one is a warning.
    """
    systems = {}
    for plan_path, data_dir in inputs:
        result = score_system(plan_path, data_dir, h4_r, strict=not draft)
        if result["system"] in systems:
            raise ValueError("system %s given twice" % result["system"])
        systems[result["system"]] = result
    problems = provenance_problems(systems, h4_r_source)
    if problems and not draft:
        raise refusal(problems)
    warnings = [w for s in systems.values() for w in s["warnings"]] + problems
    for system in grid.SYSTEMS:
        if system not in systems:
            warnings.append("NOT SCORED: no %s data; its cells count as incomplete in F1, F2, F4 and F4c" % system)
    if h4_r is None:
        warnings.append("no revision log (--no-revision-log): the underpowered H4 cells are not known")

    cells = {}
    for system, res in systems.items():
        for name, c in res["cells"].items():
            cells["%s:%s" % (system, name)] = c

    out = {"analysis": "P1 v2, hypotheses-v2.md frozen 2026-10-27", "generated_by": generated_by, "draft": draft,
           "systems_scored": sorted(systems), "warnings": warnings,
           "provenance": {s: r["provenance"] for s, r in systems.items()},
           "b_eff": {s: r["b_eff"] for s, r in systems.items()}, "cells": cells}

    # H1 and H2: Holm over F1 and F2, both systems.
    for hyp, family in (("H1", "F1"), ("H2", "F2")):
        keys = grid.family_cells(family)
        pvalues = {k: cells[k]["p"] if k in cells else None for k in keys}
        tests = stats.holm(pvalues)
        for k, t in tests.items():
            if k in cells:
                cells[k]["holm"] = t
        out[hyp] = _family_result(family, hyp, tests)

    # H3: ten criteria, no test.
    sweeps = [s for r in systems.values() for s in r["sweeps"]]
    out["H3"] = {"criteria": len(grid.sweep_cells("linux")) + len(grid.sweep_cells("windows")),
                 "scored": len(sweeps), "holding": sum(1 for s in sweeps if s["verdict"] == "holds"),
                 "results": sweeps}

    # H4: F4 and F4c over the 15 confirmatory cells (the fixed loop over absent wake).
    h4_cells = {}
    for system, res in systems.items():
        for (backend, work_us, arm_a), c in res["h4"].items():
            h4_cells[(c["key"], arm_a)] = c
    under = _underpowered(h4_r)
    f4_keys = grid.f4_cells()
    fix = grid.H4_ARM_A_FIX
    f4 = stats.holm({k: h4_cells[(k, fix)]["p"] if (k, fix) in h4_cells else None for k in f4_keys})
    f4c = stats.holm({k: h4_cells[(k, fix)]["p_cost"] if (k, fix) in h4_cells else None for k in f4_keys})
    readings = {}
    for k in f4_keys:
        c = h4_cells.get((k, fix))
        if f4[k]["rejected"]:
            reading = "no cost: equivalence within [0.98, 1.02] shown"
        elif c is None or not c["complete"]:
            reading = "incomplete"
        elif k in under:
            reading = "inconclusive: the cell was declared underpowered before the campaign"
        else:
            reading = "equivalence not shown"
        readings[k] = {"reading": reading, "cost_claimed": f4c[k]["rejected"], "underpowered": k in under}
        if c is not None:
            c["holm_f4"] = f4[k]
            c["holm_f4c"] = f4c[k]
    out["H4"] = {
        "F4": _family_result("F4", "H4", f4),
        "F4c": _family_result("F4c", "H4 cost claim", f4c),
        "readings": readings,
        "underpowered_cells": sorted(under),
        "holds_on_every_cell": all(f4[k]["rejected"] for k in f4_keys),
        "h4_r_source": h4_r_source,
        # N = 0: a disclosed repeat outside F4, with the seen direction as its prediction.
        "n0_disclosed_repeat": [dict(h4_cells[(grid.h4_key(s, b, 0), fix)],
                                     prediction=grid.H4_N0_PREDICTION[(s, b)])
                                for s in grid.SYSTEMS for b in grid.BACKENDS[s]
                                if (grid.h4_key(s, b, 0), fix) in h4_cells],
        "fixed_over_absent": [c for (k, a), c in sorted(h4_cells.items()) if a == fix],
        # Descriptive: plain wake against absent wake at the same R.
        "plain_over_absent": [c for (k, a), c in sorted(h4_cells.items()) if a == grid.H4_ARM_A_PLAIN],
    }

    # Section 4: blind spots, T_count on F2, lazy-wake rates per unit.
    blind = [c for c in cells.values() if c["blind_spot"]]
    out["blind_spots"] = {
        "rule": "a prediction fails when the exact one-sided 95% lower bound of the flag rate exceeds 0.05",
        "cells": [{"cell": c["cell"], "system": c["system"], "protocol": c["protocol"], "complete": c["complete"],
                   "t_med": c["t_med"], "lower_95": c.get("blind_lower_95"),
                   "prediction_fails": c.get("blind_prediction_fails")} for c in blind],
        "failed_predictions": sorted("%s:%s" % (c["system"], c["cell"]) for c in blind
                                     if c.get("blind_prediction_fails")),
    }
    out["t_count_f2"] = [{"cell": k, "t_count": c["t_count"]} for k, c in sorted(cells.items())
                         if c["family"] == "F2"]
    out["lazy_wake_per_unit"] = [{"cell": k, "units": c["units"], "over_half_b_eff": c["units_over_half_b_eff"],
                                  "upper_95": c.get("per_unit_over_half_upper_95"),
                                  "rate": c.get("per_unit_over_half")}
                                 for k, c in sorted(cells.items()) if c["variant"] in grid.LAZY_WAKE]

    # Section 1: the replication cells, also reported on their own.
    out["replications"] = {}
    for label in ("R", "Rd"):
        keys = [c.key for s in grid.SYSTEMS for c in grid.all_cells(s) if c.replication == label]
        out["replications"][label] = [{
            "cell": k, "family": cells[k]["family"] if k in cells else None,
            "t_med": cells[k]["t_med"] if k in cells else None,
            "p": cells[k].get("p") if k in cells else None,
            "rejected_in_family": cells[k]["holm"]["rejected"] if k in cells and "holm" in cells[k] else False,
        } for k in keys]

    out["lost_wakes"] = sorted(k for k, c in cells.items() if c.get("lost_wake"))
    out["incomplete_cells"] = sorted(k for k, c in cells.items() if not c["complete"])
    return out


def main(argv=None, repo=None):
    """repo: the repository whose commit and revision log a real scoring uses (default: the one holding this script)."""
    repo = repo or plan_mod.REPO_ROOT
    ap = argparse.ArgumentParser(description="Score the P1 v2 campaign.")
    ap.add_argument("--linux", nargs=2, metavar=("PLAN", "DIR"))
    ap.add_argument("--windows", nargs=2, metavar=("PLAN", "DIR"))
    source = ap.add_mutually_exclusive_group()
    source.add_argument("--revision-log", help="the log with the h4-r block, for the underpowered H4 cells "
                        "(default: %s at the repository root)" % plan_mod.REVISION_LOG)
    source.add_argument("--no-revision-log", action="store_true",
                        help="score without it (with --draft only): the underpowered H4 cells are then not known")
    ap.add_argument("--draft", action="store_true",
                    help="a dry run: provenance problems become warnings, no clean tree is needed, and the output "
                    "ends generated_by in draft")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    inputs = [tuple(p) for p in (a.linux, a.windows) if p]
    if not inputs:
        ap.error("give --linux and/or --windows")
    if a.no_revision_log and not a.draft:
        raise SystemExit("score.py: refused: --no-revision-log is for --draft: a real scoring checks the log hash in "
                         "the plan header against the log")
    try:
        by = plan_mod.provenance(repo, a.draft, tool="score.py")
    except plan_mod.Refusal as e:
        raise SystemExit("score.py: refused: %s" % e)
    h4_r = h4_r_source = None
    if not a.no_revision_log:
        path = a.revision_log or os.path.join(repo, plan_mod.REVISION_LOG)
        try:
            h4_r, digest = h4log.read_log(path)
        except h4log.LogError as e:
            raise SystemExit("score.py: refused: %s (give --no-revision-log with --draft to score without the "
                             "underpowered cells)" % e)
        h4_r_source = {"file": os.path.basename(path), "sha256": digest}
    try:
        result = score(inputs, h4_r, by, h4_r_source, a.draft)
    except (plan_mod.Refusal, beff_mod.BeffError) as e:
        raise SystemExit("score.py: refused: %s" % e)
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(stats.jsonable(result), f, indent=1, sort_keys=True)
        f.write("\n")
    for w in result["warnings"]:
        print("warning: " + w, file=sys.stderr)
    print("H1: %d of %d F1 cells rejected; H2: %d of %d; H3: %d of %d criteria hold; H4: %d of %d F4 cells" % (
        result["H1"]["rejected"], result["H1"]["m"], result["H2"]["rejected"], result["H2"]["m"],
        result["H3"]["holding"], result["H3"]["criteria"], result["H4"]["F4"]["rejected"], result["H4"]["F4"]["m"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

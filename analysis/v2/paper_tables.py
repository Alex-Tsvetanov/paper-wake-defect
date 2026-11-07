"""The paper's table bodies, generated from the scored results and the functional tests' verdict table.

  python paper_tables.py --score score.json --h5 h5.json --h6 h6.json --extra score-extra-w.json
                         --raw raw-facts.json --verdicts tests/CMakeLists.txt --sweep h3-sweep.csv
                         --out-dir DIR

Each table is one file DIR/<name>.tex holding only the rows of a tabular (the manuscript writes the
tabular, its header and its caption), preceded by comment lines that name its columns. Wording is
paper_words'; numbers are formatted as in paper_macros.py. Nothing is measured or scored here.

  detection-single   the test's flag rate under single posts: one row per variant and worker count,
                     one column per system, backend and bound; "inc." for an incomplete cell, empty
                     where the variant does not exist on that backend
  detection-batch    the same under batch rounds (one worker)
  functional         the verdict of each variant on each functional detector, from the expected-
                     verdict table that CTest enforces (tests/CMakeLists.txt): caught, passes, or
                     either (a racy cell whose verdict is not asserted)
  delay-model        the phase sweep: B, the cells' B_eff, the median of the sweep processes' own B_eff
                     (by which each residual is divided; from sweep_csv.py's CSV) and the median
                     |residual| / B_eff per backend
  fixcost            the throughput cost of the fix: pairs, the median pair ratio of the fixed loop
                     and of the plain wake over absent wake, the clustered interval, the reading
  prior-delay        the prior study's delay-model check measured again, per configuration
  prior-bound        the prior study's bound-independence check measured again, per group
  netty              the historical case: the four cells
  incomplete         the incomplete cells and what the disclosed extra run gave
  detection-single-exact, detection-batch-exact
                     the rows and columns of detection-single and detection-batch, each entry the
                     two-sided exact 95% interval of the flag rate ("inc." for an incomplete cell)
  detection-single-clustered, detection-batch-clustered
                     the same with the clustered 95% interval
  process-counts     per power and false-positive cell: its good processes, those whose trials all
                     flag and those with no trial flagged (raw_facts.py's per-process counts)
  throughput         added 2026-11-07: per system, backend and N, the median tasks per second of each
                     arm's 10 s windows, the fixed loop and the absent wake of its comparison, then
                     the plain wake and the absent wake of its own comparison (raw_facts.py's
                     h4_throughput), rounded to whole tasks; the rows of fixcost, in its order
  detection-single-requested, detection-batch-requested
                     added 2026-11-07 (round 5): the rows and columns of detection-single and
                     detection-batch, each entry the share of trials the test flags when its
                     threshold base is the requested bound B instead of B_eff (raw_facts.py's
                     trial_medians); descriptive, no test; "inc." for an incomplete cell
  trial-medians      added 2026-11-07 (round 5): per power and false-positive cell, the median of all
                     its trials' median delays over the cell's B_eff, then each good process's median
                     of its trials' median delays over B_eff, smallest first (raw_facts.py's
                     trial_medians); an incomplete cell has fewer process values and says so
  seen-cells         added 2026-11-07 (round 7): the cells seen before the freeze (score.json's
                     replications), how each was seen, its family, its flagged trials and trials,
                     and whether its family's frozen test rejected it
  process-flags      added 2026-11-07 (round 7, the process-level appendix of decision d012): per
                     power, false-positive and predicted blind-spot cell, each good process's flagged
                     trials, smallest first (raw_facts.py's trial_medians); descriptive, no test
  blind-trial-medians
                     added 2026-11-07 (round 7): as trial-medians, for the predicted blind-spot cells
  throughput-processes
                     added 2026-11-07 (round 7): per row of fixcost and comparison, each good
                     process's ratio, the median of its pair ratios (raw_facts.py's
                     h4_process_ratios), smallest first; descriptive, no test
  fixcost-median     added 2026-11-07 (round 9, request 34): the rows of fixcost, each comparison's
                     median pair ratio with the clustered 95% interval of the median ratio (the
                     frozen bootstrap, paper_macros.median_intervals, which checks that the same
                     call with the mean gives score.json's interval); descriptive, no test
  divisor-recount    added 2026-11-07 (round 9, request 37): per divisor d of B_eff (2, 4, the
                     frozen 5, 10), the test's flags over the complete power, false-positive and
                     predicted blind-spot cells (raw_facts.py's divisor_flags): cells, flags, the
                     lowest or highest cell rate, the power cells with every trial flagged and the
                     false-positive cells with none; the frozen row must equal score.json;
                     descriptive, no test is run again
  idle-bound         added 2026-11-07 (round 9, request 38): per family and system, the complete
                     cells' trials and their T_med and T_count flags with the frozen B_eff and with
                     the fixed loop's idle value of the added bound run (raw_facts.py's
                     idle_bound_flags), and the cells whose counts differ; descriptive, no test
  detection-single-clustered-varied, detection-batch-clustered-varied
                     added 2026-11-07 (round 10, request 41, decision d013 option F): the cells of
                     detection-single-clustered and detection-batch-clustered that are not uniform
                     (their processes do not all flag every trial or all flag none) or are
                     incomplete, one row each with its group and interval; the uniform cells are
                     counted by group in paper_macros' ClusteredSingle and ClusteredBatch macros
  process-flags-varied
                     added 2026-11-07 (round 10, request 41): the rows of process-flags for the same
                     cells; the uniform cells are counted by group in the ProcessFlags macros
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "historical", "netty"))

import argparse  # noqa: E402
import csv  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402

import grid  # noqa: E402
import paper_macros as pm  # noqa: E402
import paper_words as pw  # noqa: E402
import run_h6  # noqa: E402
import stats  # noqa: E402

VERDICT_WORDS = {"D": "caught", "P": "passes", "A": "either"}
# The CTest detectors in the order of WAKE_DETECTORS: the blocking detectors (the infinite-bound
# baseline) first, then the bounded ones.
DETECTORS = ("detect_blocking_single", "detect_blocking_burst", "detect_blocking_producers",
             "detect_self_post_blocking", "detect_blocking_sequence", "detect_batch_blocking",
             "detect_lazy_forced", "detect_latency21", "detect_latency_batch", "detect_latency_forced")
DETECTOR_WORDS = ("one post", "a burst of posts", "several producers", "a task posts a task", "posts in sequence",
                  "posts behind a gate", "forced park interleaving", "the test: 21 posts",
                  "the test: batch rounds", "the test: forced interleavings")
H5_SETTING_WORDS = pm.H5_SETTING_WORDS
VERDICT_OF_CRITERION = pm.VERDICT_OF_CRITERION


class Refusal(Exception):
    pass


def body(columns, rows):
    head = "% Generated by analysis/v2/paper_tables.py. Do not edit.\n% Columns: " + "; ".join(columns) + ".\n"
    for r in rows:
        if len(r) != len(columns):
            raise Refusal("a row of %d cells for %d columns" % (len(r), len(columns)))
    return head + "".join(" & ".join(str(c) for c in r) + " \\\\\n" for r in rows)


def column_keys():
    return [(system, backend, bound) for system, backend in pw.COLUMNS for bound in grid.BOUNDS_US]


def column_words():
    return ["%s %s, B = %d ms" % (pw.SYSTEM_WORDS[s], pw.BACKEND_WORDS[b], bound // 1000)
            for s, b, bound in column_keys()]


def detection(score, protocol):
    by = {}
    for key, c in score["cells"].items():
        p = pw.cell_parts(key)
        if p["protocol"] == protocol:
            by[(p["variant"], p["workers"], p["system"], p["backend"], p["bound_us"])] = c
    rows = []
    for variant in pw.VARIANT_ORDER:
        for workers in sorted({k[1] for k in by if k[0] == variant}):
            cells = []
            for system, backend, bound in column_keys():
                c = by.get((variant, workers, system, backend, bound))
                if c is None:
                    cells.append("")
                elif not c["complete"]:
                    cells.append("inc.")
                else:
                    cells.append(pw.rate_text(c["t_med"]["rate"]))
            label = "%s, %d worker%s" % (pw.VARIANT_WORDS[variant], workers, "" if workers == 1 else "s")
            rows.append([label] + cells)
    return body(["variant and workers"] + column_words(), rows)


def parse_verdicts(cmake_text):
    out = {}
    for variant, rest in re.findall(r"set\(WAKE_EXPECT_(\w+)\s+([DPA\s]+)\)", cmake_text):
        out[variant] = rest.split()
    detectors = re.search(r"set\(WAKE_DETECTORS\s+([^)]+)\)", cmake_text)
    if not detectors or tuple(detectors.group(1).split()) != DETECTORS:
        raise Refusal("tests/CMakeLists.txt: WAKE_DETECTORS is not the list this table knows")
    if set(out) != set(pw.VARIANT_ORDER) or any(len(v) != len(DETECTORS) for v in out.values()):
        raise Refusal("tests/CMakeLists.txt: the verdict table does not cover every variant and detector")
    return out


def functional(cmake_text):
    verdicts = parse_verdicts(cmake_text)
    rows = [[pw.VARIANT_WORDS[v]] + [VERDICT_WORDS[x] for x in verdicts[v]] for v in pw.VARIANT_ORDER]
    return body(["variant"] + list(DETECTOR_WORDS), rows)


def sweep_beff(sweep_rows):
    """(system, backend, bound_us) -> the median over the sweep's processes of each one's own B_eff (ns)."""
    per_step = {}
    for row in sweep_rows:
        key = (row["system"], row["backend"], int(row["bound_us"]))
        b = int(row["b_eff_ns"])
        if per_step.setdefault(key, {}).setdefault(int(row["step"]), b) != b:
            raise Refusal("sweep %s: one process with two B_eff values" % (key,))
    return {k: stats.median(sorted(v.values())) for k, v in per_step.items()}


def delay_model(score, sweep_rows):
    beff = {(system, r["backend"], r["bound_us"]): r["b_eff_ns"]
            for system in grid.SYSTEMS for r in score["b_eff"][system]}
    own = sweep_beff(sweep_rows)
    rows = []
    for r in sorted(score["H3"]["results"],
                    key=lambda r: (pw.COLUMNS.index((r["system"], r["backend"])), r["bound_us"])):
        key = (r["system"], r["backend"], r["bound_us"])
        if key not in own:
            raise Refusal("no sweep rows for %s" % (key,))
        rows.append([pw.SYSTEM_WORDS[r["system"]], pw.BACKEND_WORDS[r["backend"]], r["bound_us"] // 1000,
                     pw.ms(beff[key]), pw.ms(own[key]), pw.fixed(r["median_abs_residual_over_b_eff"], 4), r["verdict"]])
    return body(["system", "backend", "B (ms)", "B_eff of the cells (ms)",
                 "B_eff of the sweep (ms, median of its processes' own)",
                 "median |residual| / B_eff (each post over its process's own)", "criterion"], rows)


def fix_reading(h4, key, work):
    if work in grid.H4_CONFIRMATORY_WORK_US:
        return "equivalence shown" if h4["F4"]["tests"][key]["rejected"] else "equivalence not shown"
    pred = next(x for x in h4["n0_disclosed_repeat"] if x["key"] == key)
    if pred["prediction"] == "no prediction":
        return "outside the family; no prediction"
    held = pred["median_position"] in pred["prediction"]
    return "outside the family; prediction %s" % ("held" if held else "did not hold")


def fixcost(score):
    h4 = score["H4"]
    plain = {r["key"]: r for r in h4["plain_over_absent"]}
    order = {k: i for i, k in enumerate(pw.COLUMNS)}
    rows = []
    for r in sorted(h4["fixed_over_absent"],
                    key=lambda r: (order[pw.h4_parts(r["key"])[:2]], pw.h4_parts(r["key"])[2])):
        system, backend, work = pw.h4_parts(r["key"])
        lo, hi = r["clustered_95_mean_ratio"]
        rows.append([pw.SYSTEM_WORDS[system], pw.BACKEND_WORDS[backend], work, r["pairs"],
                     pw.fixed(r["median_ratio"], 4), pw.interval(lo, hi, 4), fix_reading(h4, r["key"], work),
                     pw.fixed(plain[r["key"]]["median_ratio"], 4)])
    return body(["system", "backend", "N (us)", "pairs", "median ratio, fixed loop over absent wake",
                 "clustered 95% interval of the mean ratio", "reading",
                 "median ratio, plain wake over absent wake"], rows)


def prior_delay(h5):
    worst = {}
    for c in h5["delay_model"]["cells"]:
        key = (c["system"], c["variant"], c["wait_us"])
        worst[key] = max(worst.get(key, 0), c["median_abs_residual_over_beff"])
    rows = []
    for c in h5["delay_model"]["configurations"]:
        key = (c["system"], c["variant"], c["wait_us"])
        rows.append([pw.SYSTEM_WORDS[c["system"]], H5_SETTING_WORDS[c["variant"]], c["wait_us"] // 1000,
                     c["cells"], pw.fixed(worst[key], 4), VERDICT_OF_CRITERION[c["verdict"]]])
    return body(["system", "timer setting", "B (ms)", "cells", "largest median |residual| / B_eff", "criterion"],
                rows)


def prior_bound(h5):
    rows = []
    for g in h5["bound_independence"]["groups"]:
        rows.append([pw.SYSTEM_WORDS[g["system"]], pw.BACKEND_WORDS[g["backend"]], H5_SETTING_WORDS[g["variant"]],
                     pw.fixed(g["min_median_us"], 1), pw.fixed(g["max_median_us"], 1), pw.fixed(g["h2_ratio"], 2),
                     VERDICT_OF_CRITERION[g["verdict"]]])
    return body(["system", "backend", "timer setting", "smallest median delay (us)", "largest median delay (us)",
                 "ratio", "criterion"], rows)


def netty(h6):
    rows = []
    for arm in run_h6.ARMS:
        for protocol in run_h6.PROTOCOLS:
            c = h6["cells"]["%s-%s" % (arm, protocol)]
            ns = c["max_delay_ns"]
            largest = "%s~s" % pw.fixed(ns / 1e9, 4) if ns >= 1e8 else "%s~$\\mu$s" % pw.us(ns)
            rows.append([run_h6.ARMS[arm], protocol, "%d of %d" % (c["t_med"]["flags"], c["t_med"]["trials"]),
                         "%d of %d" % (c["t_count"]["flags"], c["t_count"]["trials"]),
                         "%s of %s" % (pw.thousands(c["posts_over_half_b"]), pw.thousands(c["posts"])), largest])
    return body(["Netty", "protocol", "trials flagged by the test", "trials flagged by the tail statistic",
                 "posts delayed by more than B / 2", "largest delay"], rows)


def incomplete(score, extra):
    rows = []
    for key in sorted(score["incomplete_cells"]):
        c, e = score["cells"][key], extra["cells"][key]
        rows.append([pw.cell_words(key), c["processes_good"],
                     "%s of %s" % (pw.thousands(c["t_med"]["flags"]), pw.thousands(c["t_med"]["trials"])),
                     "complete" if e["complete"] else "incomplete", e["processes_good"],
                     "%s of %s" % (pw.thousands(e["t_med"]["flags"]), pw.thousands(e["t_med"]["trials"]))])
    return body(["cell", "good processes", "trials flagged", "extra run", "good processes in the extra run",
                 "trials flagged in the extra run"], rows)


def detection_intervals(score, protocol, kind):
    """The same rows and columns as detection(), each entry the cell's exact or clustered 95% interval
    of the test's flag rate, four places."""
    by = {}
    for key, c in score["cells"].items():
        p = pw.cell_parts(key)
        if p["protocol"] == protocol:
            by[(p["variant"], p["workers"], p["system"], p["backend"], p["bound_us"])] = c
    rows = []
    for variant in pw.VARIANT_ORDER:
        for workers in sorted({k[1] for k in by if k[0] == variant}):
            cells = []
            for system, backend, bound in column_keys():
                c = by.get((variant, workers, system, backend, bound))
                if c is None:
                    cells.append("")
                elif not c["complete"]:
                    cells.append("inc.")
                else:
                    lo, hi = c["t_med"][kind]
                    cells.append("%s to %s" % (pw.rate_text(lo, 4), pw.rate_text(hi, 4)))
            label = "%s, %d worker%s" % (pw.VARIANT_WORDS[variant], workers, "" if workers == 1 else "s")
            rows.append([label] + cells)
    return body(["variant and workers"] + column_words(), rows)


def process_counts(score, raw):
    """Per power and false-positive cell: good processes, those whose trials all flag, those with none
    flagged (raw_facts.py)."""
    rows = []
    for fam, word in (("H1", "power"), ("H2", "false positive")):
        for key in sorted(score[fam]["tests"]):
            p = raw["processes"][key]
            if sum(p["flags"]) != score["cells"][key]["t_med"]["flags"]:
                raise Refusal("%s: raw facts and score.json differ" % key)
            allf = sum(1 for f, t in zip(p["flags"], p["trials"]) if t and f == t)
            none = sum(1 for f in p["flags"] if f == 0)
            rows.append([word, pw.cell_words(key), len(p["flags"]), allf, none])
    return body(["family", "cell", "good processes", "processes whose trials all flag",
                 "processes with no trial flagged"], rows)


def throughput(score, raw):
    """The rows of fixcost, each with the median tasks per second of the arms of both comparisons."""
    h4 = score["H4"]
    order = {k: i for i, k in enumerate(pw.COLUMNS)}
    rows = []
    for r in sorted(h4["fixed_over_absent"],
                    key=lambda r: (order[pw.h4_parts(r["key"])[:2]], pw.h4_parts(r["key"])[2])):
        system, backend, work = pw.h4_parts(r["key"])
        fix, plain = raw["h4_throughput"][r["key"] + "-none"], raw["h4_throughput"][r["key"] + "-plain_wake"]
        if fix["windows"] != r["pairs"] or plain["windows"] != fix["windows"] or fix["arm_b"] != "absent_wake" or plain["arm_b"] != "absent_wake":
            raise Refusal("%s: throughput rows and score.json differ" % r["key"])
        rows.append([pw.SYSTEM_WORDS[system], pw.BACKEND_WORDS[backend], work, fix["windows"],
                     pw.thousands(round(fix["median_a_tasks_per_s"])), pw.thousands(round(fix["median_b_tasks_per_s"])),
                     pw.thousands(round(plain["median_a_tasks_per_s"])),
                     pw.thousands(round(plain["median_b_tasks_per_s"]))])
    return body(["system", "backend", "N (us)", "windows per arm", "median tasks/s, fixed loop",
                 "median tasks/s, absent wake (beside the fixed loop)", "median tasks/s, plain wake",
                 "median tasks/s, absent wake (beside the plain wake)"], rows)


def detection_requested(score, raw, protocol):
    """The rows and columns of detection(), each entry the share of trials flagged with B as the base."""
    by = {}
    for key, c in score["cells"].items():
        p = pw.cell_parts(key)
        if p["protocol"] == protocol:
            t = raw["trial_medians"][key]
            if sum(x["flags_b_eff"] for x in t["processes"]) != c["t_med"]["flags"]:
                raise Refusal("%s: raw facts and score.json differ" % key)
            by[(p["variant"], p["workers"], p["system"], p["backend"], p["bound_us"])] = (c, t)
    rows = []
    for variant in pw.VARIANT_ORDER:
        for workers in sorted({k[1] for k in by if k[0] == variant}):
            cells = []
            for system, backend, bound in column_keys():
                ct = by.get((variant, workers, system, backend, bound))
                if ct is None:
                    cells.append("")
                elif not ct[0]["complete"]:
                    cells.append("inc.")
                else:
                    procs = ct[1]["processes"]
                    cells.append(pw.rate_text(sum(x["flags_requested"] for x in procs) / sum(x["trials"] for x in procs)))
            label = "%s, %d worker%s" % (pw.VARIANT_WORDS[variant], workers, "" if workers == 1 else "s")
            rows.append([label] + cells)
    return body(["variant and workers"] + column_words(), rows)


def trial_medians(score, raw):
    """Per power and false-positive cell: the pooled median of its trial medians over B_eff, then each good
    process's median of its trial medians over B_eff, smallest first, four places."""
    rows = []
    n = grid.PROCESSES_PER_CELL
    for fam, word in (("H1", "power"), ("H2", "false positive")):
        for key in sorted(score[fam]["tests"]):
            t, c = raw["trial_medians"][key], score["cells"][key]
            if len(t["processes"]) != c["processes_good"] or len(t["processes"]) > n:
                raise Refusal("%s: raw facts and score.json differ" % key)
            b = t["b_eff_ns"]
            values = sorted(p["median_trial_median_ns"] / b for p in t["processes"])
            rows.append([word, pw.cell_words(key), "yes" if c["complete"] else "no",
                         pw.fixed(t["median_trial_median_ns"] / b, 4)] + [pw.fixed(v, 4) for v in values]
                        + [""] * (n - len(values)))
    return body(["family", "cell", "complete", "cell median of trial medians / B_eff"]
                + ["process value %d / B_eff (smallest first)" % (i + 1) for i in range(n)], rows)


SEEN_WORDS = {"R": "results seen in the earlier study", "Rd": "seen through run durations only"}
FAMILY_WORDS = {"F1": "power", "F2": "false positive"}


def seen_cells(score):
    """Round 7, request 4: the cells seen before the freeze, how each was seen, its family, and its own
    flags and trials as the frozen test counted them, with whether the whole family's test rejected it."""
    rows = []
    for label in ("R", "Rd"):
        listed = sorted(score["replications"][label], key=lambda x: (x["family"], x["cell"]))
        for x in listed:
            c = score["cells"][x["cell"]]
            fam = "H1" if c["family"] == "F1" else "H2"
            if c["replication"] != label or x["family"] != c["family"] or c["family"] not in FAMILY_WORDS:
                raise Refusal("%s: replication and cell differ" % x["cell"])
            if x["rejected_in_family"] != score[fam]["tests"][x["cell"]]["rejected"]:
                raise Refusal("%s: rejection differs from the family" % x["cell"])
            rows.append([SEEN_WORDS[label], FAMILY_WORDS[c["family"]], pw.cell_words(x["cell"]),
                         pw.thousands(c["t_med"]["flags"]), pw.thousands(c["t_med"]["trials"]),
                         "yes" if x["rejected_in_family"] else "no"])
    return body(["how it was seen", "family", "cell", "flagged trials", "trials", "rejected in its family"], rows)


def appendix_cells(score):
    """(group word, cell key) of every power, false-positive and predicted blind-spot cell, in that order."""
    out = [("power", k) for k in sorted(score["H1"]["tests"])]
    out += [("false positive", k) for k in sorted(score["H2"]["tests"])]
    blind = sorted(k for k, c in score["cells"].items() if c["blind_spot"])
    if len(blind) != len(score["blind_spots"]["cells"]):
        raise Refusal("blind-spot cells differ")
    return out + [("blind spot", k) for k in blind]


def process_flags(score, raw, keep=None):
    """Process-level appendix: per power, false-positive and blind-spot cell, each good process's
    flagged trials, smallest first (raw_facts.py's trial_medians, the sum checked against score.json).
    keep, when given, is the set of cell keys to print (process-flags-varied)."""
    rows = []
    n = grid.PROCESSES_PER_CELL
    for word, key in appendix_cells(score):
        if keep is not None and key not in keep:
            continue
        t, c = raw["trial_medians"][key], score["cells"][key]
        procs = t["processes"]
        if len(procs) != c["processes_good"] or len(procs) > n:
            raise Refusal("%s: raw facts and score.json differ" % key)
        if sum(p["flags_b_eff"] for p in procs) != c["t_med"]["flags"]:
            raise Refusal("%s: flags differ from score.json" % key)
        trials = {p["trials"] for p in procs}
        if len(trials) != 1:
            raise Refusal("%s: processes with different trial counts" % key)
        flags = sorted(p["flags_b_eff"] for p in procs)
        rows.append([word, pw.cell_words(key), "yes" if c["complete"] else "no", trials.pop(), len(procs)]
                    + flags + [""] * (n - len(flags)))
    return body(["group", "cell", "complete", "trials of each process", "good processes"]
                + ["flagged trials of process %d (smallest first)" % (i + 1) for i in range(n)], rows)


def varied_cells(score, raw):
    """Round 10, request 41: the appendix cells that are not uniform (paper_macros.uniform_states: the
    processes do not all flag every trial or all flag none) or are incomplete."""
    return {k for k, (_w, state) in pm.uniform_states(score, raw).items() if state in ("varied", "incomplete")}


def clustered_varied(score, raw, protocol):
    """Round 10, request 41: the cells of detection-<protocol>-clustered that are not uniform or are
    incomplete, one row each: group, cell, clustered 95% interval ("inc." for an incomplete cell)."""
    keep = varied_cells(score, raw)
    rows = []
    for word, key in appendix_cells(score):
        c = score["cells"][key]
        if key not in keep or c["protocol"] != protocol:
            continue
        if c["complete"]:
            lo, hi = c["t_med"]["clustered_95"]
            interval = "%s to %s" % (pw.rate_text(lo, 4), pw.rate_text(hi, 4))
        else:
            interval = "inc."
        rows.append([word, pw.cell_words(key), interval])
    return body(["group", "cell", "clustered 95% interval of the flag rate"], rows)


def blind_trial_medians(score, raw):
    """Process-level appendix: as trial-medians, for the predicted blind-spot cells."""
    rows = []
    n = grid.PROCESSES_PER_CELL
    for word, key in appendix_cells(score):
        if word != "blind spot":
            continue
        t, c = raw["trial_medians"][key], score["cells"][key]
        if len(t["processes"]) != c["processes_good"] or len(t["processes"]) > n:
            raise Refusal("%s: raw facts and score.json differ" % key)
        b = t["b_eff_ns"]
        values = sorted(p["median_trial_median_ns"] / b for p in t["processes"])
        rows.append([word, pw.cell_words(key), "yes" if c["complete"] else "no",
                     pw.fixed(t["median_trial_median_ns"] / b, 4)] + [pw.fixed(v, 4) for v in values]
                    + [""] * (n - len(values)))
    return body(["group", "cell", "complete", "cell median of trial medians / B_eff"]
                + ["process value %d / B_eff (smallest first)" % (i + 1) for i in range(n)], rows)


H4_COMPARISON_WORDS = {grid.H4_ARM_A_FIX: "fixed loop over absent wake", grid.H4_ARM_A_PLAIN: "plain wake over absent wake"}


def throughput_processes(score, raw):
    """Process-level appendix: per system, backend, N and comparison, each good process's ratio (the median
    of its pair ratios, raw_facts.py's h4_process_ratios, checked against score.json's pair ratios),
    smallest first; the rows of fixcost, in its order, the fixed loop first."""
    h4 = score["H4"]
    order = {k: i for i, k in enumerate(pw.COLUMNS)}
    n = grid.H4_R_MAX // grid.H4_PAIRS_PER_PROCESS
    rows = []
    for part, arm in (("fixed_over_absent", grid.H4_ARM_A_FIX), ("plain_over_absent", grid.H4_ARM_A_PLAIN)):
        for r in sorted(h4[part], key=lambda r: (order[pw.h4_parts(r["key"])[:2]], pw.h4_parts(r["key"])[2])):
            system, backend, work = pw.h4_parts(r["key"])
            got = raw["h4_process_ratios"]["%s-%s" % (r["key"], arm)]
            if got["arm_a"] != arm or r["arm_a"] != arm or got["arm_b"] != grid.H4_ARM_B:
                raise Refusal("%s: arms differ" % r["key"])
            if [x for g in got["processes"] for x in g] != r["ratios"]:
                raise Refusal("%s: pair ratios differ from score.json" % r["key"])
            values = sorted(stats.median(sorted(g)) for g in got["processes"])
            if len(values) > n:
                raise Refusal("%s: more processes than the cap" % r["key"])
            rows.append([pw.SYSTEM_WORDS[system], pw.BACKEND_WORDS[backend], work, H4_COMPARISON_WORDS[arm],
                         len(values)] + [pw.fixed(v, 4) for v in values] + [""] * (n - len(values)))
    return body(["system", "backend", "N (us)", "comparison", "good processes"]
                + ["ratio of process %d, the median of its pairs (smallest first)" % (i + 1) for i in range(n)], rows)


def fixcost_median(score, raw):
    """Round 9, request 34: the rows of fixcost with the clustered interval of each median ratio."""
    h4 = score["H4"]
    ci = pm.median_intervals(score, raw)
    plain = {r["key"]: r for r in h4["plain_over_absent"]}
    order = {k: i for i, k in enumerate(pw.COLUMNS)}
    rows = []
    for r in sorted(h4["fixed_over_absent"],
                    key=lambda r: (order[pw.h4_parts(r["key"])[:2]], pw.h4_parts(r["key"])[2])):
        system, backend, work = pw.h4_parts(r["key"])
        p = plain[r["key"]]
        rows.append([pw.SYSTEM_WORDS[system], pw.BACKEND_WORDS[backend], work, r["pairs"],
                     pw.fixed(r["median_ratio"], 4), pw.interval(*ci[(r["key"], grid.H4_ARM_A_FIX)], 4),
                     pw.fixed(p["median_ratio"], 4), pw.interval(*ci[(r["key"], grid.H4_ARM_A_PLAIN)], 4)])
    return body(["system", "backend", "N (us)", "pairs", "median ratio, fixed loop over absent wake",
                 "clustered 95% interval of that median ratio", "median ratio, plain wake over absent wake",
                 "clustered 95% interval of that median ratio"], rows)


RECOUNT_GROUPS = (("power", "F1"), ("false positive", "F2"), ("predicted blind spot", None))


def divisor_recount(score, raw):
    """Round 9, request 37: the test's flags at B_eff / d for d = 2, 4, 5 (frozen) and 10, by family."""
    df = raw["divisor_flags"]
    rows = []
    for d in sorted(int(x) for x in df[next(iter(df))]["processes"][0]["flags"]):
        row = [d, "frozen" if d == grid.T_MED_DIVISOR else "recount"]
        for _, fam in RECOUNT_GROUPS:
            keys = sorted(k for k, c in df.items() if c["complete"] and c["family"] == fam)
            fl = {k: sum(p["flags"][str(d)] for p in df[k]["processes"]) for k in keys}
            tr = {k: sum(p["trials"] for p in df[k]["processes"]) for k in keys}
            if d == grid.T_MED_DIVISOR:
                for k in keys:
                    if (fl[k], tr[k]) != (score["cells"][k]["t_med"]["flags"], score["cells"][k]["t_med"]["trials"]):
                        raise Refusal("%s: the frozen divisor's count differs from score.json" % k)
            rates = [fl[k] / tr[k] for k in keys]
            row += [len(keys), pw.thousands(sum(fl.values())), pw.thousands(sum(tr.values()))]
            if fam == "F1":
                row += [pw.rate_text(min(rates)), sum(1 for k in keys if fl[k] == tr[k])]
            else:
                row += [pw.rate_text(max(rates)), sum(1 for k in keys if fl[k] == 0)]
        rows.append(row)
    cols = ["divisor d (threshold B_eff / d)", "frozen or recount"]
    for words, fam in RECOUNT_GROUPS:
        cols += ["%s: complete cells" % words, "%s: flagged trials" % words, "%s: trials" % words]
        cols += (["power: lowest cell rate", "power: cells with every trial flagged"] if fam == "F1" else
                 ["%s: highest cell rate" % words, "%s: cells with no trial flagged" % words])
    return body(cols, rows)


def idle_bound(score, raw):
    """Round 9, request 38: the complete cells' flags with the frozen B_eff and with the idle value."""
    ib = raw["idle_bound_flags"]
    rows = []
    for words, fam in RECOUNT_GROUPS:
        for system in grid.SYSTEMS:
            keys = sorted(k for k, c in ib.items() if c["complete"] and c["family"] == fam and c["system"] == system)
            if not keys:
                raise Refusal("no complete %s cell on %s" % (words, system))
            tot = {f: sum(p[f] for k in keys for p in ib[k]["processes"])
                   for f in ("trials", "t_med", "t_count", "t_med_b_eff", "t_count_b_eff")}
            for k in keys:
                for f in ("t_med", "t_count"):
                    if sum(p[f + "_b_eff"] for p in ib[k]["processes"]) != score["cells"][k][f]["flags"]:
                        raise Refusal("%s: %s with B_eff differs from score.json" % (k, f))
            changed = sum(1 for k in keys if any(sum(p[f] for p in ib[k]["processes"])
                                                 != sum(p[f + "_b_eff"] for p in ib[k]["processes"])
                                                 for f in ("t_med", "t_count")))
            rows.append([words, pw.SYSTEM_WORDS[system], len(keys), pw.thousands(tot["trials"]),
                         pw.thousands(tot["t_med_b_eff"]), pw.thousands(tot["t_med"]),
                         pw.thousands(tot["t_count_b_eff"]), pw.thousands(tot["t_count"]), changed])
    return body(["family", "system", "complete cells", "trials", "the test, frozen B_eff", "the test, idle value",
                 "the tail statistic, frozen B_eff", "the tail statistic, idle value",
                 "cells whose count differs"], rows)


def build(inputs, cmake_text, sweep_rows):
    for name, data in inputs.items():
        if data.get("draft"):
            raise Refusal("%s is a draft output" % name)
    s = inputs["score"]
    return {"detection-single": detection(s, "S"), "detection-batch": detection(s, "Bt"),
            "functional": functional(cmake_text), "delay-model": delay_model(s, sweep_rows), "fixcost": fixcost(s),
            "prior-delay": prior_delay(inputs["h5"]), "prior-bound": prior_bound(inputs["h5"]),
            "netty": netty(inputs["h6"]), "incomplete": incomplete(s, inputs["extra"]),
            "detection-single-exact": detection_intervals(s, "S", "exact_95"),
            "detection-batch-exact": detection_intervals(s, "Bt", "exact_95"),
            "detection-single-clustered": detection_intervals(s, "S", "clustered_95"),
            "detection-batch-clustered": detection_intervals(s, "Bt", "clustered_95"),
            "process-counts": process_counts(s, inputs["raw"]), "throughput": throughput(s, inputs["raw"]),
            "detection-single-requested": detection_requested(s, inputs["raw"], "S"),
            "detection-batch-requested": detection_requested(s, inputs["raw"], "Bt"),
            "trial-medians": trial_medians(s, inputs["raw"]),
            "seen-cells": seen_cells(s), "process-flags": process_flags(s, inputs["raw"]),
            "blind-trial-medians": blind_trial_medians(s, inputs["raw"]),
            "throughput-processes": throughput_processes(s, inputs["raw"]),
            "fixcost-median": fixcost_median(s, inputs["raw"]),
            "divisor-recount": divisor_recount(s, inputs["raw"]),
            "idle-bound": idle_bound(s, inputs["raw"]),
            "detection-single-clustered-varied": clustered_varied(s, inputs["raw"], "S"),
            "detection-batch-clustered-varied": clustered_varied(s, inputs["raw"], "Bt"),
            "process-flags-varied": process_flags(s, inputs["raw"], varied_cells(s, inputs["raw"]))}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for name in ("score", "h5", "h6", "extra", "raw", "verdicts", "sweep"):
        ap.add_argument("--" + name, required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args(argv)
    inputs = {}
    for name in ("score", "h5", "h6", "extra", "raw"):
        with open(getattr(args, name), encoding="utf-8") as f:
            inputs[name] = json.load(f)
    with open(args.verdicts, encoding="utf-8") as f:
        cmake_text = f.read()
    with open(args.sweep, encoding="utf-8", newline="") as f:
        sweep_rows = list(csv.DictReader(f))
    try:
        tables = build(inputs, cmake_text, sweep_rows)
    except (Refusal, pm.Refusal, KeyError, ValueError, StopIteration) as e:
        print("refused: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 2
    os.makedirs(args.out_dir, exist_ok=True)
    for name, text in tables.items():
        with open(os.path.join(args.out_dir, name + ".tex"), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    print("%d tables" % len(tables))
    return 0


if __name__ == "__main__":
    sys.exit(main())

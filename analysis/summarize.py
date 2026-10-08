#!/usr/bin/env python3
"""Summarise wakeprobe runs into CSV tables and macros.tex.

Reads every *.jsonl under the given directories: one wakeprobe run per file, each with a
"design" (matrix, gap-sweep, bound-sweep, bound-sweep-qos, random-gap) and a variant (Linux
timer slack or Windows timer period). Statistics are recomputed from the per-post rows with the nearest-rank
rule wakeprobe uses and cross-checked against its summary line. The arm comes from the
run's own record (defect_active), not from its label.

B_eff is the defect arm's measured wake period (calibration posts in wakeprobe).
Each measured post carries phase, predicted = B_eff - (phase mod B_eff) and residual.

Outputs in --out-dir:
  matrix.csv            design (c): one row per (backend, variant, B, arm)
  gap_sweep.csv         design (a): one row per step
  gap_sweep_summary.csv design (a): pooled per (backend, variant, B)
  gap_sweep_posts.csv   design (a): one row per measured post, for the sawtooth plot
  bound_sweep.csv       designs (b) and bound-sweep-qos: one row per (design, backend, variant, B)
  bound_sweep_summary.csv the same: max/min median ratio across B (H2, and H5 for the QoS
                        design), Kruskal-Wallis
  modes.csv             cells whose calibration is bimodal: the two wake-period modes
  random_gap.csv        design (r), exploratory: one row per (backend, B, arm), trials of 21
                        posts at random gaps, flagged against B_eff / 5 and B / 5
  macros.tex            per-cell values, then derived macros: design constants and host
                        facts read back from the runs and their env.txt, pin.json and
                        quiet.json; aggregates (H1 over configurations, H3 factors and
                        counts); B_eff against B; the gap sweep as runs of the test and its
                        resampling at random phases; the random-gap results

The gap-sweep columns h3_* treat each step as one run of the test at a fixed gap. The
resampling draws RESAMPLE_TRIALS sets of 21 posts per cell, each without replacement, with random.Random(RESAMPLE_SEED)
(the seed and the Python version are written into macros.tex).

Bimodality guard: a defect cell whose calibration intervals have p90/p10 > 1.5 has no
single wake period. It is marked `bimodal`, gets no B_eff or H1 (residual) macro, and its
two modes (2-means on log intervals) go to modes.csv. That is a finding, not an error.

Hypothesis checks follow hypotheses.md: H1 median |residual| < 5% of B_eff; H2 the
largest-to-smallest ratio of fixed-arm medians across B within 1.25; H3 the median of
the first 21 measured posts against B_eff/5, B_eff from the defect run of the same cell.
macros.tex carries H3's numbers too: WakeDetect<Backend><Arm><Bound><Variant> (the median
of the first 21 posts) and WakeDetectThreshold<Backend><Bound><Variant> (B_eff/5).

Only the standard library is required. With scipy, matrix.csv gets a two-sided
Mann-Whitney U p-value (defect against fixed) and bound_sweep_summary.csv a
Kruskal-Wallis p-value across bounds.

The publication tables (paper numbers come only from results/rework/macros.tex):
  summarize.py --out-dir results/rework results/raw/2026-09-29-L-publication-0af1ac4 \\
      results/raw/2026-09-30-W-publication results/raw/2026-09-29-L-random-gap-0af1ac4
Smoke data goes to its own directory under results/smoke, never to results/ or results/rework.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import platform
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

try:
    from scipy.stats import kruskal, mannwhitneyu  # optional
except ImportError:  # pragma: no cover - depends on the host
    kruskal = mannwhitneyu = None

REPO = Path(__file__).resolve().parent.parent
BACKEND_WORD = {"io_uring": "Uring", "epoll": "Epoll", "iocp": "Iocp", "kqueue": "Kqueue"}
ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
        "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen",
        "Eighteen", "Nineteen"]
TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]
H1_LIMIT = 0.05
BIMODAL_RATIO = 1.5
H2_LIMIT = 1.25
H3_POSTS = 21
H3_DIVISOR = 5  # the test flags a median above B_eff / H3_DIVISOR (or B / H3_DIVISOR)
H5_LIMIT = 1.25
# Resampling of the gap sweep: RESAMPLE_TRIALS draws of H3_POSTS posts without replacement
# from each cell's pooled posts, with random.Random(RESAMPLE_SEED); the cells are visited in
# sorted order, so the draws are fixed by the seed and the Python version (recorded).
RESAMPLE_TRIALS = 100_000
RESAMPLE_SEED = 20260926
# Descriptive statistics of the one H1 failure (IOCP, 1 ms timer, B = 1 ms): the share of
# achieved phases within PHASE_GRID_TOL_US of a multiple of PHASE_GRID_US, and of delays
# within DELAY_BAND x B of B.
PHASE_GRID_US = 500
PHASE_GRID_TOL_US = 60
DELAY_BAND = 0.1
# The first version of hypotheses.md (commit 4abc958, line 7) also bounded the correct loop:
# "With the fix, the median delay is independent of B and below 100 microseconds." The
# revision before the publication runs dropped that bound; the paper reports what it would
# have given.
DROPPED_FIXED_BOUND_US = 100
# The publication runs were built at a commit of this repository's earlier, archived history,
# which the public history does not contain. PROVENANCE.md shows that the first-party files the
# builds compiled are byte-identical at the root commit of the public history (same git object
# ids for the paths of bench/code_paths.txt, same inputs hashes). The paper names the public
# commit; the measured commit is kept where provenance needs it. A measured commit that is
# not listed here stops a non-smoke run.
PUBLIC_COMMIT = {"0af1ac47ec708595b91e5c15300556f2714314c1": "47f14f09efac14e2a14f39195a0c891b6c6f2e27"}
# paper/fig-sawtooth.tex, through analysis/fig_sawtooth.py, draws the gap sweep at the smallest
# B for these cells, keeping every second measured post of each step ([1::2] by index).
FIG_CELLS = [("epoll", "slack=1ns"), ("io_uring", "slack=1ns"), ("iocp", "timer=default")]
DESIGNS = {"matrix", "gap-sweep", "bound-sweep", "bound-sweep-qos", "random-gap"}


# ---------------------------------------------------------------------------- naming

def words(n: int) -> str:
    """CamelCase English words for 1 <= n < 1e6, so macro names carry no digits."""
    if not 0 < n < 1_000_000:
        raise ValueError(f"no macro name for {n}")
    if n >= 1000:
        return words(n // 1000) + "Thousand" + (words(n % 1000) if n % 1000 else "")
    if n >= 100:
        return ONES[n // 100] + "Hundred" + (words(n % 100) if n % 100 else "")
    if n >= 20:
        return TENS[n // 10] + ONES[n % 10]
    return ONES[n]


def bound_word(wait_us: int) -> str:
    if wait_us <= 0:
        return "Blocking" if wait_us == 0 else "Default"
    return words(wait_us // 1000) + "Ms" if wait_us % 1000 == 0 else words(wait_us) + "Us"


def variant_of(summary: dict) -> tuple[str, str]:
    """(label for CSV, word for macro names). 1 ns slack is the unsuffixed Linux case."""
    timer = summary.get("timer_period_ms_requested")
    if timer is not None:
        return ("timer=default", "DefaultTimer") if timer == 0 else (
            f"timer={timer}ms", "Timer" + words(timer) + "Ms")
    slack = summary.get("timerslack_ns_requested")
    qos = summary.get("pm_qos_us_requested")
    if qos is not None:
        base, word = variant_of({**summary, "pm_qos_us_requested": None})
        return f"{base},qos={qos}us", word + "Qos" + ("Zero" if qos == 0 else words(qos)) + "Us"
    if slack is not None:
        if slack == 0:
            return "slack=default", "DefaultSlack"
        return (f"slack={slack}ns", "" if slack == 1 else "Slack" + words(slack) + "Ns")
    return "", ""


def display_path(path: Path) -> str:
    path = path.resolve()
    return path.relative_to(REPO).as_posix() if path.is_relative_to(REPO) else str(path)


# ---------------------------------------------------------------------------- statistics

def nearest_rank(sorted_values: list, p: int):
    """Same rule as wakeprobe: rank = ceil(p * n / 100), clamped to [1, n]."""
    n = len(sorted_values)
    rank = min(max((p * n + 99) // 100, 1), n)
    return sorted_values[rank - 1]


def median_ci(sorted_values: list, level: float = 0.95):
    """Distribution-free CI for the median from binomial order statistics, or None."""
    n = len(sorted_values)
    alpha = (1.0 - level) / 2.0
    # Largest rank l with P(Bin(n, 1/2) <= l - 1) <= alpha; interval [x_(l), x_(n - l + 1)].
    cdf, l = 0.0, 0
    for k in range(n + 1):
        cdf += math.comb(n, k) / 2.0**n
        if cdf > alpha:
            l = k
            break
    if l < 1:
        return None
    return sorted_values[l - 1], sorted_values[n - l]


def two_modes(values: list[int]) -> dict:
    """1-D 2-means on log values; returns each cluster's median and size."""
    xs = sorted(math.log(v) for v in values)
    lo, hi = xs[0], xs[-1]
    for _ in range(100):
        a = [x for x in xs if abs(x - lo) <= abs(x - hi)]
        b = [x for x in xs if abs(x - lo) > abs(x - hi)]
        new_lo = sum(a) / len(a)
        new_hi = sum(b) / len(b) if b else hi
        if (new_lo, new_hi) == (lo, hi):
            break
        lo, hi = new_lo, new_hi
    a = sorted(v for v in values if abs(math.log(v) - lo) <= abs(math.log(v) - hi))
    b = sorted(v for v in values if abs(math.log(v) - lo) > abs(math.log(v) - hi))
    return {"mode_lo_us": nearest_rank(a, 50) / 1e3 if a else "", "n_lo": len(a),
            "mode_hi_us": nearest_rank(b, 50) / 1e3 if b else "", "n_hi": len(b)}


def calibration_intervals(posts: list[dict], bound_us: int) -> list[int]:
    """Kept calibration intervals, with wakeprobe's rule (drop below half the median or B/2)."""
    out = [posts[i]["exec_ns"] - posts[i - 1]["exec_ns"] for i in range(1, len(posts))
           if posts[i]["kind"] == "calib" and posts[i]["exec_ns"] is not None
           and posts[i - 1]["exec_ns"] is not None]
    if not out:
        return []
    first = nearest_rank(sorted(out), 50)
    floor = bound_us * 500 if bound_us > 0 else 0
    return sorted(d for d in out if 2 * d >= first and d >= floor)


def is_bimodal(intervals: list[int]) -> bool:
    return bool(intervals) and nearest_rank(intervals, 90) > BIMODAL_RATIO * nearest_rank(intervals, 10)


# ---------------------------------------------------------------------------- loading

def load_run(path: Path) -> dict:
    posts, summary = [], None
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            obj = json.loads(line)
            if obj.get("summary"):
                if summary is not None:
                    raise SystemExit(f"{path}:{line_no}: second summary line")
                summary = obj
            else:
                posts.append(obj)
    if summary is None:
        raise SystemExit(f"{path}: no summary line (run did not finish?)")
    if "design" not in summary:
        raise SystemExit(f"{path}: no 'design' field; this is a round-1 record, not supported here")
    if summary.get("error"):
        raise SystemExit(f"{path}: the run reported an error: {summary['error']}")
    arm = "defect" if summary["defect_active"] else "fixed"
    if summary.get("label") in ("fixed", "defect") and summary["label"] != arm:
        raise SystemExit(f"{path}: label '{summary['label']}' but the build says {arm}")

    measured = [p for p in posts if p["kind"] == "measured"]
    delays = sorted(p["delay_ns"] for p in measured if p["delay_ns"] is not None)
    if not delays:
        raise SystemExit(f"{path}: no delivered measured posts")
    if summary["median_ns"] != nearest_rank(delays, 50):
        raise SystemExit(f"{path}: recomputed median {nearest_rank(delays, 50)} != summary {summary['median_ns']}")
    b_eff = summary.get("b_eff_ns")
    for p in measured:
        if b_eff and p["phase_ns"] is not None and p["predicted_ns"] != b_eff - p["phase_ns"] % b_eff:
            raise SystemExit(f"{path}: post {p['index']} predicted_ns does not follow from phase and B_eff")
    label, word = variant_of(summary)
    intervals = calibration_intervals(posts, int(summary["wait_us"]))
    return {"path": path, "summary": summary, "measured": measured, "delays": delays, "arm": arm,
            "design": summary["design"], "backend": summary["backend"], "variant": label,
            "variant_word": word, "wait_us": int(summary["wait_us"]),
            "gap_us": int(summary["gap_us"]) if summary.get("gap_us") is not None else "",
            "phase_frac": summary.get("phase_frac") if summary.get("phase_frac") is not None else "",
            "gap_frac_range": summary.get("gap_frac_range"), "gap_seed": summary.get("gap_seed"),
            "b_eff": b_eff, "intervals": intervals, "bimodal": is_bimodal(intervals)}


def residual_ratios(run: dict) -> list[float]:
    b = run["b_eff"]
    return sorted(abs(p["residual_ns"]) / b for p in run["measured"] if b and p["residual_ns"] is not None)


def delay_columns(run: dict) -> dict:
    d = run["delays"]
    row = {"n": len(d), "median_us": nearest_rank(d, 50) / 1e3, "p10_us": nearest_rank(d, 10) / 1e3,
           "p90_us": nearest_rank(d, 90) / 1e3, "max_us": d[-1] / 1e3,
           "median_ci95_lo_us": "", "median_ci95_hi_us": ""}
    ci = median_ci(d)
    if ci:
        row["median_ci95_lo_us"], row["median_ci95_hi_us"] = ci[0] / 1e3, ci[1] / 1e3
    return row


def residual_columns(run: dict) -> dict:
    s, b = run["summary"], run["b_eff"]
    ratios = residual_ratios(run)
    return {
        "b_eff_us": b / 1e3 if b else "",
        "calib_p10_us": nearest_rank(run["intervals"], 10) / 1e3 if run["intervals"] else "",
        "calib_p90_us": nearest_rank(run["intervals"], 90) / 1e3 if run["intervals"] else "",
        "bimodal": run["bimodal"],
        "calib_dropped": s.get("calib_dropped", ""),
        "median_abs_residual_us": (s["median_abs_residual_ns"] / 1e3) if s.get("median_abs_residual_ns") is not None else "",
        "median_abs_residual_over_beff": nearest_rank(ratios, 50) if ratios else "",
        "median_residual_over_beff": s.get("median_residual_over_beff") if s.get("median_residual_over_beff") is not None else "",
        "residual_jumps": s.get("residual_jumps", ""),
    }


def provenance(run: dict) -> dict:
    s = run["summary"]
    return {"timerslack_ns_worker": s.get("timerslack_ns_worker"),
            "timer_begin_result": s.get("timer_begin_result"),
            "code_commit": s["bench_code_commit"], "bench_commit": s["bench_commit"],
            "bench_dirty": s["bench_dirty"], "defect_mode": s.get("defect_mode"), "wait_method": s.get("wait_method"),
            "compiler": s["compiler"], "host": s["host"], "source": display_path(run["path"])}


# ---------------------------------------------------------------------------- designs

def matrix_rows(runs: list[dict]) -> list[dict]:
    defect_beff = {(r["backend"], r["variant"], r["wait_us"]): r["b_eff"]
                   for r in runs if r["arm"] == "defect" and r["b_eff"] and not r["bimodal"]}
    rows = []
    for r in runs:
        s = r["summary"]
        bound, gap = r["wait_us"], r["gap_us"]
        first = sorted(p["delay_ns"] for p in r["measured"][:H3_POSTS] if p["delay_ns"] is not None)
        b_ref = defect_beff.get((r["backend"], r["variant"], bound))
        med21 = nearest_rank(first, 50) if first else None
        # H3's threshold B_eff / 5, with B_eff from the defect run of the same cell, and the
        # threshold B / 5 that needs no defect build.
        thr = b_ref / H3_DIVISOR if b_ref else None
        thr_b = bound * 1e3 / H3_DIVISOR if bound > 0 else None
        row = {"backend": r["backend"], "variant": r["variant"], "wait_us": bound, "gap_us": gap, "arm": r["arm"],
               **delay_columns(r), "timeouts": s["timeouts"], "lost": s["lost"],
               "predicted_nominal_us": bound - (gap % bound) if r["arm"] == "defect" and bound > 0 else "",
               **residual_columns(r), "mwu_p_vs_fixed": "",
               "h1_pass": "", "h3_median21_us": med21 / 1e3 if med21 is not None else "",
               "h3_threshold_us": thr / 1e3 if thr else "", "h3_flagged": "",
               "h3_factor": med21 / thr if med21 is not None and thr else "",
               "h3_threshold_b_us": thr_b / 1e3 if thr_b else "",
               "h3_flagged_b": med21 > thr_b if med21 is not None and thr_b else "",
               "h3_factor_b": med21 / thr_b if med21 is not None and thr_b else "",
               **provenance(r), "_delays": r["delays"], "_word": r["variant_word"]}
        ratio = row["median_abs_residual_over_beff"]
        if r["arm"] == "defect" and r["bimodal"]:
            row["h1_pass"] = "bimodal"
        elif r["arm"] == "defect" and ratio != "":
            row["h1_pass"] = ratio < H1_LIMIT
        if med21 is not None and thr:
            row["h3_flagged"] = med21 > thr
        rows.append(row)
    if mannwhitneyu is not None:
        fixed = {(r["backend"], r["variant"], r["wait_us"]): r for r in rows if r["arm"] == "fixed"}
        for r in rows:
            f = fixed.get((r["backend"], r["variant"], r["wait_us"]))
            if r["arm"] == "defect" and f is not None:
                r["mwu_p_vs_fixed"] = float(mannwhitneyu(r["_delays"], f["_delays"], alternative="two-sided").pvalue)
    rows.sort(key=lambda r: (r["backend"], r["variant"], r["wait_us"], r["arm"] != "fixed"))
    return rows


def gap_sweep_tables(runs: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    steps, posts = [], []
    pooled = defaultdict(lambda: {"ratios": [], "beffs": [], "intervals": [], "steps": 0, "bimodal_steps": 0,
                                  "jumps": 0, "word": "", "missed": 0, "missed_b": 0, "missed_late": 0,
                                  "delays": [], "phases": [], "min_step": None})
    for r in runs:
        b = r["b_eff"]
        phases = sorted(p["phase_ns"] for p in r["measured"] if p["phase_ns"] is not None)
        preds = sorted(p["predicted_ns"] for p in r["measured"] if p["predicted_ns"] is not None)
        # Each step is one run of the 21-post test at a fixed gap: its median against
        # B_eff / 5 (this step's own calibration) and against B / 5.
        first = sorted(p["delay_ns"] for p in r["measured"][:H3_POSTS] if p["delay_ns"] is not None)
        med21 = nearest_rank(first, 50)
        thr = b / H3_DIVISOR if b else None
        thr_b = r["wait_us"] * 1e3 / H3_DIVISOR
        med_phase = nearest_rank(phases, 50) if phases else None
        steps.append({"backend": r["backend"], "variant": r["variant"], "wait_us": r["wait_us"],
                      "gap_us": r["gap_us"], "phase_frac": r["phase_frac"], "n": len(r["delays"]),
                      "median_phase_us": med_phase / 1e3 if med_phase is not None else "",
                      "phase_in_period": (med_phase / b) % 1.0 if med_phase is not None and b else "",
                      "median_delay_us": nearest_rank(r["delays"], 50) / 1e3,
                      "median_predicted_us": nearest_rank(preds, 50) / 1e3 if preds else "",
                      "h3_median21_us": med21 / 1e3, "h3_threshold_us": thr / 1e3 if thr else "",
                      "h3_flagged": med21 > thr if thr else "", "h3_threshold_b_us": thr_b / 1e3,
                      "h3_flagged_b": med21 > thr_b,
                      **residual_columns(r), **provenance(r)})
        for p in r["measured"]:
            if p["delay_ns"] is None:
                continue
            posts.append({"backend": r["backend"], "variant": r["variant"], "wait_us": r["wait_us"],
                          "gap_us": r["gap_us"], "phase_frac": r["phase_frac"], "index": p["index"],
                          "phase_us": p["phase_ns"] / 1e3 if p["phase_ns"] is not None else "",
                          "delay_us": p["delay_ns"] / 1e3,
                          "predicted_us": p["predicted_ns"] / 1e3 if p["predicted_ns"] is not None else "",
                          "b_eff_us": b / 1e3 if b else ""})
        cell = pooled[(r["backend"], r["variant"], r["wait_us"])]
        cell["ratios"].extend(residual_ratios(r))
        cell["steps"] += 1
        cell["bimodal_steps"] += int(r["bimodal"])
        cell["intervals"].extend(r["intervals"])
        cell["jumps"] += r["summary"].get("residual_jumps", 0) or 0
        cell["word"] = r["variant_word"]
        if b:
            cell["beffs"].append(b)
        step = steps[-1]
        cell["missed"] += int(step["h3_flagged"] is False)
        cell["missed_b"] += int(step["h3_flagged_b"] is False)
        cell["missed_late"] += int(step["h3_flagged"] is False and step["phase_in_period"] != ""
                                   and step["phase_in_period"] >= 1 - 1 / H3_DIVISOR)
        cell["delays"].extend(p["delay_ns"] for p in r["measured"] if p["delay_ns"] is not None)
        cell["phases"].extend(p["phase_ns"] for p in r["measured"] if p["phase_ns"] is not None)
        if cell["min_step"] is None or step["h3_median21_us"] < cell["min_step"]["h3_median21_us"]:
            cell["min_step"] = step
    summary = []
    for (backend, variant, bound), c in sorted(pooled.items()):
        ratios, beffs, intervals = sorted(c["ratios"]), sorted(c["beffs"]), sorted(c["intervals"])
        med = nearest_rank(ratios, 50) if ratios else ""
        bimodal = is_bimodal(intervals)
        grid, tol = PHASE_GRID_US * 1000, PHASE_GRID_TOL_US * 1000
        band = DELAY_BAND * bound * 1000
        summary.append({"backend": backend, "variant": variant, "wait_us": bound, "steps": c["steps"],
                        "bimodal_steps": c["bimodal_steps"], "bimodal": bimodal, "posts": len(ratios),
                        "b_eff_median_us": nearest_rank(beffs, 50) / 1e3 if beffs else "",
                        "b_eff_min_us": beffs[0] / 1e3 if beffs else "",
                        "b_eff_max_us": beffs[-1] / 1e3 if beffs else "",
                        "median_abs_residual_over_beff": med,
                        "p90_abs_residual_over_beff": nearest_rank(ratios, 90) if ratios else "",
                        "jump_fraction": c["jumps"] / len(ratios) if ratios else "",
                        "h1_pass": "bimodal" if bimodal else ((med < H1_LIMIT) if med != "" else ""),
                        "h3_missed_steps": c["missed"], "h3_missed_late_steps": c["missed_late"],
                        "h3_missed_steps_b": c["missed_b"],
                        "h3_min_step_median_us": c["min_step"]["h3_median21_us"],
                        "h3_min_step_gap_us": c["min_step"]["gap_us"],
                        "h3_min_step_phase_frac": c["min_step"]["phase_frac"],
                        "resample_miss_rate": "", "resample_miss_rate_b": "",
                        "phase_grid_posts": sum(min(p % grid, grid - p % grid) <= tol for p in c["phases"]),
                        "delay_band_posts": sum(abs(d - bound * 1000) <= band for d in c["delays"]),
                        "_word": c["word"], "_intervals": intervals, "_delays": c["delays"]})
    order = lambda r: (r["backend"], r["variant"], r["wait_us"], str(r["gap_us"]).zfill(9), str(r["phase_frac"]))
    steps.sort(key=order)
    posts.sort(key=lambda r: (*order(r), r["index"]))
    return steps, summary, posts


def bound_sweep_tables(runs: list[dict]) -> tuple[list[dict], list[dict]]:
    rows = [{"design": r["design"], "backend": r["backend"], "variant": r["variant"], "wait_us": r["wait_us"],
             "gap_us": r["gap_us"],
             "arm": r["arm"], **delay_columns(r), "timeouts": r["summary"]["timeouts"],
             "lost": r["summary"]["lost"], **provenance(r), "_delays": r["delays"], "_word": r["variant_word"]}
            for r in runs]
    rows.sort(key=lambda r: (r["design"], r["backend"], r["variant"], r["wait_us"]))
    groups = defaultdict(list)
    for r in rows:
        groups[(r["design"], r["backend"], r["variant"])].append(r)
    summary = []
    for (design, backend, variant), g in sorted(groups.items()):
        medians = [r["median_us"] for r in g]
        ratio = max(medians) / min(medians) if min(medians) > 0 else ""
        p = ""
        if kruskal is not None and len(g) > 1:
            p = float(kruskal(*[r["_delays"] for r in g]).pvalue)
        summary.append({"design": design, "backend": backend, "variant": variant,
                        "bounds_us": " ".join(str(r["wait_us"]) for r in g),
                        "min_median_us": min(medians), "max_median_us": max(medians), "h2_ratio": ratio,
                        "h2_pass": (ratio <= H2_LIMIT) if ratio != "" else "", "kruskal_p": p,
                        "_word": g[0]["_word"]})
    return rows, summary


def resample_misses(sweep: list[dict]) -> dict:
    """The 21-post test at random phases, from the gap sweep: RESAMPLE_TRIALS draws of H3_POSTS
    posts without replacement from each cell's pooled posts. A draw misses when its median
    is not above B_eff / 5 (the cell's pooled B_eff) or B / 5. Fills the resample columns of
    `sweep` and returns the settings, which go into the macros file."""
    rng = random.Random(RESAMPLE_SEED)
    for c in sweep:  # sorted by (backend, variant, B): the draw order is fixed
        delays = c["_delays"]
        thr = c["b_eff_median_us"] * 1e3 / H3_DIVISOR if c["b_eff_median_us"] != "" else None
        thr_b = c["wait_us"] * 1e3 / H3_DIVISOR
        miss = miss_b = 0
        for _ in range(RESAMPLE_TRIALS):
            med = sorted(rng.sample(delays, H3_POSTS))[(50 * H3_POSTS + 99) // 100 - 1]
            miss += int(thr is not None and med <= thr)
            miss_b += int(med <= thr_b)
        c["resample_miss_rate"] = miss / RESAMPLE_TRIALS if thr is not None else ""
        c["resample_miss_rate_b"] = miss_b / RESAMPLE_TRIALS
    return {"seed": RESAMPLE_SEED, "trials": RESAMPLE_TRIALS, "python": platform.python_version()}


def binomial_tail(n: int, k: int, p: float) -> float:
    """P(X >= k) for X ~ Bin(n, p)."""
    return sum(math.comb(n, j) * p**j * (1 - p) ** (n - j) for j in range(k, n + 1))


def binomial_upper(k: int, n: int, level: float = 0.95) -> float:
    """One-sided Clopper-Pearson upper bound on p after k events in n trials."""
    def cdf(p: float) -> float:  # P(X <= k)
        return sum(math.exp(math.lgamma(n + 1) - math.lgamma(j + 1) - math.lgamma(n - j + 1)
                            + (j * math.log(p) if j else 0.0) + (n - j) * math.log1p(-p)) for j in range(k + 1))
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if cdf(mid) > 1 - level:
            lo = mid
        else:
            hi = mid
    return hi


def random_gap_tables(runs: list[dict]) -> list[dict]:
    """Design (r), exploratory: consecutive blocks of H3_POSTS measured posts, each a trial of the
    test at random gaps. A trial is flagged when its median is above B_eff / 5 (B_eff from the
    defect run of the same cell) or above B / 5."""
    defect_beff = {(r["backend"], r["variant"], r["wait_us"]): r["b_eff"]
                   for r in runs if r["arm"] == "defect" and r["b_eff"] and not r["bimodal"]}
    rows = []
    for r in runs:
        b_ref = defect_beff.get((r["backend"], r["variant"], r["wait_us"]))
        thr = b_ref / H3_DIVISOR if b_ref else None
        thr_b = r["wait_us"] * 1e3 / H3_DIVISOR
        delays = [p["delay_ns"] for p in r["measured"]]
        if any(d is None for d in delays):
            raise SystemExit(f"{r['path']}: a random-gap post was lost")
        trials = len(delays) // H3_POSTS
        medians = sorted(nearest_rank(sorted(delays[t * H3_POSTS:(t + 1) * H3_POSTS]), 50) for t in range(trials))
        flagged = sum(m > thr for m in medians) if thr else ""
        flagged_b = sum(m > thr_b for m in medians)
        ratios = residual_ratios(r)
        rows.append({"backend": r["backend"], "variant": r["variant"], "wait_us": r["wait_us"], "arm": r["arm"],
                     "gap_frac_lo": r["gap_frac_range"][0], "gap_frac_hi": r["gap_frac_range"][1],
                     "gap_seed": r["gap_seed"], "posts": len(delays), "trials": trials, "posts_per_trial": H3_POSTS,
                     "b_eff_ref_us": b_ref / 1e3 if b_ref else "", "threshold_us": thr / 1e3 if thr else "",
                     "flagged": flagged, "threshold_b_us": thr_b / 1e3, "flagged_b": flagged_b,
                     "trial_median_min_us": medians[0] / 1e3, "trial_median_p50_us": nearest_rank(medians, 50) / 1e3,
                     "trial_median_max_us": medians[-1] / 1e3,
                     "median_abs_residual_over_beff": nearest_rank(ratios, 50) if ratios else "",
                     **provenance(r), "_word": r["variant_word"]})
    rows.sort(key=lambda x: (x["backend"], x["variant"], x["wait_us"], x["arm"] != "fixed"))
    return rows


# ---------------------------------------------------------------------------- derived macros

def host_meta(inputs: list[Path]) -> dict:
    """Host facts from the run directories: env.txt (key: value lines), pin.json, quiet.json."""
    meta = {"linux": {}, "windows": {}, "cpuidle_states": []}
    for d in inputs:
        if not d.is_dir():
            continue
        env = {}
        env_path = d / "env.txt"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                key, sep, value = line.partition(": ")
                if not sep:
                    continue
                if key == "cpuidle_state":
                    name, *fields = value.split()
                    kv = dict(f.split("=", 1) for f in fields)
                    meta["cpuidle_states"].append({"name": name, **{k: int(v) for k, v in kv.items()}})
                else:
                    env[key] = value.strip()
        if (d / "pin.json").exists():
            pin = json.loads((d / "pin.json").read_text(encoding="utf-8"))
            m = meta["linux"]
            # Short forms for the prose: "AMD Ryzen 7 5800H with Radeon Graphics" -> "Ryzen 7
            # 5800H", "7.2.3-arch1-2" -> "7.2.3".
            m.setdefault("cpu", re.sub(r"^AMD\s+|\s+with\s.*$", "", pin["cpu"]))
            m.setdefault("kernel", re.match(r"[\d.]+", pin["kernel"]).group(0))
            for k in ("cpuidle_driver", "cpuidle_governor"):
                if k in env:
                    m[k] = env[k]
        if (d / "quiet.json").exists():
            q = json.loads((d / "quiet.json").read_text(encoding="utf-8-sig"))
            m = meta["windows"]
            m["quiet_threshold"], m["quiet_mean"] = q["threshold"], q["idle_percent_mean"]
            # "AMD Ryzen 5 3600 6-Core Processor" -> "Ryzen 5 3600";
            # "Microsoft Windows 11 Pro N 10.0.26200" -> "Windows 11".
            m["cpu"] = re.sub(r"^AMD\s+|\s+\d+-Core Processor$", "", env.get("cpu", ""))
            os_match = re.search(r"Windows \d+", env.get("os", ""))
            m["os"] = os_match.group(0) if os_match else env.get("os", "")
    return meta


def tex_text(s: str) -> str:
    return s.replace("\\", "\\textbackslash{}").replace("_", "\\_").replace("&", "\\&").replace("%", "\\%")


def only(values, what: str):
    """The one value all runs share, or stop: a design constant must be constant."""
    distinct = sorted(set(values), key=str)
    if len(distinct) != 1:
        raise SystemExit(f"{what} is not constant across runs: {distinct}")
    return distinct[0]


def public_commit(measured: str, smoke: bool) -> str:
    """The public-history commit with the same compiled files as the measured commit."""
    if measured in PUBLIC_COMMIT:
        return PUBLIC_COMMIT[measured]
    if smoke:
        return measured
    raise SystemExit(f"code commit {measured} is not in PUBLIC_COMMIT (see PROVENANCE.md)")


def derived_macros(runs: list[dict], matrix: list[dict], steps: list[dict], sweep: list[dict],
                   bound: list[dict], random_rows: list[dict], modes: list[dict], meta: dict,
                   resample: dict, smoke: bool = False) -> list[tuple[str, object, int | None]]:
    """Macros for the paper's prose beyond the per-cell values: design constants, host facts,
    aggregates over cells, B_eff against B, the resampling and random-gap results. A digit
    count of None marks a text macro."""
    out: list[tuple[str, object, int | None]] = []
    add = lambda name, value, digits=1: out.append((name, value, digits))
    linux = lambda r: r["backend"] in ("epoll", "io_uring")
    by = lambda design: [r for r in runs if r["design"] == design]

    # Design constants, read back from the runs.
    add("WakeTestPosts", H3_POSTS, 0)
    add("WakeTestDivisor", H3_DIVISOR, 0)
    add("WakeHOneLimitPct", round(100 * H1_LIMIT), 0)
    add("WakeHTwoLimit", H2_LIMIT, 2)
    add("WakeHFiveLimit", H5_LIMIT, 2)
    add("WakeCalibPosts", only([r["summary"]["calibrate"] for r in runs if r["arm"] == "defect"], "calibrate"), 0)
    add("WakeMatrixPosts", only([len(r["delays"]) for r in by("matrix")], "matrix posts"), 0)
    add("WakeBoundPosts", only([len(r["delays"]) for r in by("bound-sweep") + by("bound-sweep-qos")], "bound posts"), 0)
    add("WakeWarmupPosts", only([r["summary"]["warmup"] for r in by("matrix") + by("bound-sweep")], "warmup"), 0)
    add("WakeMatrixGapFrac", only([r["gap_us"] / r["wait_us"] for r in by("matrix")], "matrix gap / B"), 1)
    add("WakeBoundGapMs", only([r["gap_us"] / 1000 for r in by("bound-sweep") + by("bound-sweep-qos")], "bound gap"), 0)
    add("WakeSweepSteps", only([c["steps"] for c in sweep], "sweep steps"), 0)
    add("WakeSweepStepPosts", only([s["n"] for s in steps], "sweep posts per step"), 0)
    add("WakeSweepCellPosts", only([c["posts"] for c in sweep], "sweep posts per cell"), 0)
    lin_fracs = [s["gap_us"] / s["wait_us"] for s in steps if linux(s)]
    add("WakeSweepGapLoFrac", min(lin_fracs), 1)
    add("WakeSweepGapHiFrac", max(lin_fracs), 1)
    win_fracs = [float(s["phase_frac"]) for s in steps if not linux(s)]
    add("WakeSweepPhaseLoFrac", min(win_fracs), 2)
    add("WakeSweepPhaseHiFrac", max(win_fracs), 2)
    bounds = sorted({r["wait_us"] for r in runs if r["wait_us"] > 0})
    if len(bounds) != 3:
        raise SystemExit(f"expected three positive bounds, got {bounds}")
    for word, b in zip(("Small", "Mid", "Large"), bounds):
        add(f"WakeB{word}Ms", b // 1000, 0)
    slacks = sorted({r["summary"]["timerslack_ns_worker"] for r in runs if linux(r)})
    add("WakeSlackNs", slacks[0], 0)
    add("WakeSlackDefaultUs", slacks[-1] / 1000, 0)
    add("WakeQosUs", only([r["summary"]["pm_qos_us_requested"] for r in by("bound-sweep-qos")], "PM QoS"), 0)
    add("WakeTimerRequestedMs", only([r["summary"]["timer_period_ms_requested"] for r in runs
                                      if not linux(r) and r["summary"]["timer_period_ms_requested"]], "timer"), 0)
    win = [r for r in runs if not linux(r)]
    res = only([r["summary"]["timer_resolution_100ns_start"] for r in win], "Windows timer resolution")
    add("WakeWinTimerResolutionMs", res / 1e4, 1)
    add("WakeRunsWindows", len(win), 0)
    add("WakeRunsLinux", sum(1 for r in runs if linux(r) and r["design"] != "random-gap"), 0)
    add("WakeRunsRandom", len(by("random-gap")), 0)
    w = meta["windows"]
    if w:
        add("WakeQuietThresholdPct", w["quiet_threshold"], 0)
        add("WakeQuietMeanPct", w["quiet_mean"], 1)
        add("WakeHostWindowsCpu", tex_text(w["cpu"]), None)
        add("WakeHostWindowsOs", tex_text(w["os"]), None)
    lm = meta["linux"]
    if lm:
        add("WakeHostLinuxCpu", tex_text(lm["cpu"]), None)
        add("WakeHostLinuxKernel", tex_text(lm["kernel"]), None)
        if "cpuidle_driver" in lm:
            add("WakeCpuidleDriver", tex_text(lm["cpuidle_driver"]), None)
            add("WakeCpuidleGovernor", tex_text(lm["cpuidle_governor"]), None)
    if meta["cpuidle_states"]:
        deepest = max(meta["cpuidle_states"], key=lambda s: s["latency_us"])
        add("WakeCpuidleDeepest", tex_text(deepest["name"]), None)
        add("WakeCpuidleDeepestLatencyUs", deepest["latency_us"], 0)
    for os_word, pred in (("Linux", linux), ("Windows", lambda r: not linux(r))):
        sel = [r for r in runs if pred(r) and r["design"] != "random-gap"]
        add(f"WakeCompiler{os_word}", tex_text(" ".join(only([r["summary"]["compiler"] for r in sel], "compiler")
                                                        .split()[:2])), None)
        measured = only([r["summary"]["bench_code_commit"] for r in sel], "code commit")
        add(f"WakeCodeCommit{os_word}", public_commit(measured, smoke)[:7], None)
        add(f"WakeMeasuredCommit{os_word}", measured[:7], None)
    # The bound the first version put on the correct loop, against the largest correct median
    # of the publication designs (matrix and bound sweeps, with and without the QoS request).
    add("WakeHDroppedBoundUs", DROPPED_FIXED_BOUND_US, 0)
    add("WakeFixedMedianMaxUs", max([r["median_us"] for r in matrix if r["arm"] == "fixed"]
                                    + [r["median_us"] for r in bound]), 1)
    # Posts drawn per panel of the figure, and the posts of the cell.
    shown = []
    for backend, variant in FIG_CELLS:
        per_step = defaultdict(list)
        for r in runs:
            if (r["design"], r["backend"], r["variant"], r["wait_us"]) == ("gap-sweep", backend, variant, bounds[0]):
                per_step[(r["gap_us"], r["phase_frac"])] = [p for p in r["measured"] if p["phase_ns"] is not None]
        shown.append(sum(len(sorted(ps, key=lambda p: p["index"])[1::2]) for ps in per_step.values()))
    add("WakeFigPostsShown", only(shown, "posts per figure panel"), 0)

    # H1: the model's median |residual| over B_eff, Linux cells and Windows configurations.
    cells = [(r["backend"], r["variant"], r["wait_us"], r["median_abs_residual_over_beff"])
             for r in matrix if r["arm"] == "defect" and not r["bimodal"] and r["median_abs_residual_over_beff"] != ""]
    cells += [(c["backend"], c["variant"], c["wait_us"], c["median_abs_residual_over_beff"])
              for c in sweep if not c["bimodal"] and c["median_abs_residual_over_beff"] != ""]
    add("WakeHOneLinuxMaxPct", 100 * max(v for b, _, _, v in cells if b in ("epoll", "io_uring")), 2)
    configs = defaultdict(list)
    for b, variant, bound_us, v in cells:
        if b not in ("epoll", "io_uring"):
            configs[(variant, bound_us)].append(v)
    passing = [max(v) for v in configs.values() if max(v) < H1_LIMIT]
    add("WakeHOneWindowsConfigs", len(configs), 0)
    add("WakeHOneWindowsPassConfigs", len(passing), 0)
    add("WakeHOneWindowsPassMaxPct", 100 * max(passing), 2)

    # H3 on the matrix: counts, and the median over its threshold.
    for arm in ("defect", "fixed"):
        rows = [r for r in matrix if r["arm"] == arm and r["h3_flagged"] != ""]
        a = arm.capitalize()
        add(f"WakeDetect{a}Configs", len(rows), 0)
        add(f"WakeDetect{a}Flagged", sum(bool(r["h3_flagged"]) for r in rows), 0)
        add(f"WakeDetectB{a}Flagged", sum(bool(r["h3_flagged_b"]) for r in rows), 0)
        pick = min if arm == "defect" else max
        if arm == "defect":
            add("WakeDetectFactorDefectMin", min(r["h3_factor"] for r in rows), 2)
            add("WakeDetectFactorDefectMax", max(r["h3_factor"] for r in rows), 2)
            add("WakeDetectFactorBDefectMin", min(r["h3_factor_b"] for r in rows), 2)
            add("WakeDetectFactorBDefectMax", max(r["h3_factor_b"] for r in rows), 1)
            add("WakeDetectConfigsLinux", sum(1 for r in rows if linux(r)), 0)
            add("WakeDetectConfigsWindows", sum(1 for r in rows if not linux(r)), 0)
        else:
            add("WakeDetectFactorFixedMax", pick(r["h3_factor"] for r in rows), 2)
            add("WakeDetectFactorBFixedMax", pick(r["h3_factor_b"] for r in rows), 2)

    # The gap sweep as runs of the test at fixed gaps, and its resampling at random phases.
    add("WakeSweepRunsTotal", len(steps), 0)
    add("WakeSweepMissedTotal", sum(c["h3_missed_steps"] for c in sweep), 0)
    add("WakeSweepMissedLateTotal", sum(c["h3_missed_late_steps"] for c in sweep), 0)
    add("WakeSweepMissedBTotal", sum(c["h3_missed_steps_b"] for c in sweep), 0)
    for c in sweep:
        be, bw, vw = BACKEND_WORD[c["backend"]], bound_word(c["wait_us"]), c["_word"]
        add(f"WakeSweep{be}Missed{bw}{vw}", c["h3_missed_steps"], 0)
        add(f"WakeSweep{be}MinMedian{bw}{vw}", c["h3_min_step_median_us"], 1)
        if c["h3_min_step_gap_us"] != "":
            add(f"WakeSweep{be}MinMedianGapMs{bw}{vw}", c["h3_min_step_gap_us"] / 1000, 1)
    add("WakeResampleTrials", resample["trials"], 0)
    add("WakeResampleMissMaxPct", 100 * max(c["resample_miss_rate"] for c in sweep if c["resample_miss_rate"] != ""), 2)
    add("WakeResampleMissBMaxPct", 100 * max(c["resample_miss_rate_b"] for c in sweep), 2)
    k = (50 * H3_POSTS + 99) // 100
    add("WakeBinomialMissPct", 100 * binomial_tail(H3_POSTS, k, 1 / H3_DIVISOR), 3)

    # Descriptive statistics of each H1 failure of the gap sweep.
    add("WakeGridUs", PHASE_GRID_US, 0)
    add("WakeGridTolUs", PHASE_GRID_TOL_US, 0)
    add("WakeGridUniformPct", 100 * 2 * PHASE_GRID_TOL_US / PHASE_GRID_US, 0)
    for c in sweep:
        if c["h1_pass"] is False:
            be, bw, vw = BACKEND_WORD[c["backend"]], bound_word(c["wait_us"]), c["_word"]
            add(f"WakeGridPosts{be}{bw}{vw}", c["phase_grid_posts"], 0)
            add(f"WakeBandPosts{be}{bw}{vw}", c["delay_band_posts"], 0)
            add(f"WakeBandLoUs{be}{bw}{vw}", (1 - DELAY_BAND) * c["wait_us"], 0)
            add(f"WakeBandHiUs{be}{bw}{vw}", (1 + DELAY_BAND) * c["wait_us"], 0)

    # B_eff against B: the excess, in us and percent of B.
    for rows, prefix, key in ((matrix, "Wake", "b_eff_us"), (sweep, "WakeSweep", "b_eff_median_us")):
        for r in rows:
            if r.get("arm", "defect") != "defect" or r["bimodal"] or r[key] == "":
                continue
            be, bw, vw = BACKEND_WORD[r["backend"]], bound_word(r["wait_us"]), r["_word"]
            excess = r[key] - r["wait_us"]
            add(f"{prefix}{be}BEffExcess{bw}{vw}", excess, 1)
            add(f"{prefix}{be}BEffExcessPct{bw}{vw}", 100 * excess / r["wait_us"], 1)
    for r in matrix:  # the thread's default timer slack against 1 ns, same backend and bound
        if r["arm"] == "defect" and r["variant"] == "slack=default" and r["b_eff_us"] != "":
            base = [m for m in matrix if m["arm"] == "defect" and m["backend"] == r["backend"]
                    and m["wait_us"] == r["wait_us"] and m["variant"] == "slack=1ns"]
            if base and base[0]["b_eff_us"] != "":
                add(f"Wake{BACKEND_WORD[r['backend']]}SlackEffect{bound_word(r['wait_us'])}",
                    r["b_eff_us"] - base[0]["b_eff_us"], 1)
    # Windows, default timer: the period at the smallest bound is one tick; a larger bound
    # against a whole number of such ticks.
    tick_rows = [c for c in sweep if not linux(c) and c["variant"] == "timer=default" and c["wait_us"] == bounds[0]]
    if tick_rows:
        tick = tick_rows[0]["b_eff_median_us"]
        add("WakeWinDefaultTickMs", tick / 1000, 1)
        for r in matrix:
            if r["arm"] == "defect" and not linux(r) and r["variant"] == "timer=default" and r["wait_us"] > tick:
                ticks = math.ceil(r["wait_us"] / tick)
                add(f"WakeIocpTicks{bound_word(r['wait_us'])}DefaultTimer", ticks, 0)
                add(f"WakeIocpBEffOverTicks{bound_word(r['wait_us'])}DefaultTimer", r["b_eff_us"] - ticks * tick, 1)
        # The largest delay of a defective loop at the bounds below one tick (gap sweep and
        # matrix), in ms: the cost of the defect at those bounds, set by the tick, not by B.
        below_tick = [r for r in runs if not linux(r) and r["arm"] == "defect" and r["variant"] == "timer=default"
                      and r["design"] in ("gap-sweep", "matrix") and r["wait_us"] < tick]
        if below_tick:
            add("WakeIocpDefectMaxMsDefaultTimer", max(max(r["delays"]) for r in below_tick) / 1e6, 1)

    # Wake-period modes of bimodal runs (one per cell).
    seen = set()
    for m in modes:
        if m["scope"] != "run":
            continue
        cell = (m["backend"], m["variant"], m["wait_us"])
        if cell in seen:
            raise SystemExit(f"two bimodal runs in {cell}: name their modes by step")
        seen.add(cell)
        word = next(r["variant_word"] for r in runs if (r["backend"], r["variant"], r["wait_us"]) == cell)
        be, bw = BACKEND_WORD[m["backend"]], bound_word(m["wait_us"])
        add(f"WakeMode{be}Lo{bw}{word}", m["mode_lo_us"], 1)
        add(f"WakeMode{be}Hi{bw}{word}", m["mode_hi_us"], 1)

    # Random gaps (exploratory, added after review).
    if random_rows:
        add("WakeRandomTrials", only([r["trials"] for r in random_rows], "random-gap trials"), 0)
        add("WakeRandomGapLo", only([r["gap_frac_lo"] for r in random_rows], "gap range"), 1)
        add("WakeRandomGapHi", only([r["gap_frac_hi"] for r in random_rows], "gap range"), 1)
        for r in random_rows:
            be, bw, vw, a = BACKEND_WORD[r["backend"]], bound_word(r["wait_us"]), r["_word"], r["arm"].capitalize()
            add(f"WakeRandom{be}{a}Flagged{bw}{vw}", r["flagged"], 0)
            add(f"WakeRandom{be}{a}FlaggedB{bw}{vw}", r["flagged_b"], 0)
        defect = [r for r in random_rows if r["arm"] == "defect"]
        fixed = [r for r in random_rows if r["arm"] == "fixed"]
        n_d, n_f = sum(r["trials"] for r in defect), sum(r["trials"] for r in fixed)
        miss = sum(r["trials"] - r["flagged"] for r in defect)
        miss_b = sum(r["trials"] - r["flagged_b"] for r in defect)
        fp, fp_b = sum(r["flagged"] for r in fixed), sum(r["flagged_b"] for r in fixed)
        add("WakeRandomDefectTrials", n_d, 0)
        add("WakeRandomDefectMissed", miss, 0)
        add("WakeRandomDefectMissedB", miss_b, 0)
        add("WakeRandomFixedTrials", n_f, 0)
        add("WakeRandomFixedFlagged", fp, 0)
        add("WakeRandomFixedFlaggedB", fp_b, 0)
        if n_d:
            add("WakeRandomDefectMissPct", 100 * miss / n_d, 2)
            add("WakeRandomDefectMissBPct", 100 * miss_b / n_d, 2)
            add("WakeRandomDefectMissUpperPct", 100 * binomial_upper(miss, n_d), 2)
            add("WakeRandomDefectMissUpperBPct", 100 * binomial_upper(miss_b, n_d), 2)
            add("WakeRandomAbsResidualPctMax", 100 * max(r["median_abs_residual_over_beff"] for r in defect), 2)
        if n_f:
            add("WakeRandomFixedFlaggedUpperPct", 100 * binomial_upper(max(fp, fp_b), n_f), 2)
    return out


# ---------------------------------------------------------------------------- output

def fmt(key: str, value):
    if not isinstance(value, float):
        return value
    if key.endswith("_p") or key.startswith("mwu_p") or key.startswith("kruskal"):
        return f"{value:.3g}"
    if "over_beff" in key or "fraction" in key or key.endswith("ratio"):
        return f"{value:.4f}"
    return f"{value:.3f}"


def write_csv(rows: list[dict], path: Path) -> None:
    if not rows:
        return
    fields = [k for k in rows[0] if not k.startswith("_")]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: fmt(k, v) for k, v in r.items() if not k.startswith("_")})


def write_macros(path: Path, sources: list[Path], smoke: bool, matrix: list[dict],
                 sweep: list[dict], bound: list[dict], bound_summary: list[dict],
                 extra: list[tuple[str, object, int | None]], resample: dict) -> None:
    lines = ["% Generated by analysis/summarize.py. Do not edit.",
             "% Delays in microseconds (nearest-rank medians); residual shares in percent of B_eff.",
             "% Sources: " + ", ".join(sorted({display_path(s.parent) for s in sources}))]
    runs = matrix + bound
    commits = sorted({r["code_commit"] + ("-dirty" if r["bench_dirty"] else "") for r in runs})
    lines.append("% Code commit measured: " + ", ".join(commits))
    lines.append("% Public commit with the same compiled files (PROVENANCE.md): "
                 + ", ".join(sorted({public_commit(r["code_commit"], smoke) for r in runs})))
    lines.append(f"% Resampling of the gap sweep: random.Random({resample['seed']}), {resample['trials']} draws "
                 f"per cell, Python {resample['python']}.")
    if smoke:
        lines.append("% SMOKE RUN: NOT CITABLE.")
    seen = {}

    def add(name: str, value, digits: int | None = 1) -> None:
        if name in seen:
            raise SystemExit(f"duplicate macro {name}")
        if re.search(r"\d", name):
            raise SystemExit(f"macro name with a digit: {name}")
        seen[name] = value
        text = value if digits is None else f"{round(value, digits) + 0.0:.{digits}f}"  # no -0.0
        lines.append(f"\\newcommand{{\\{name}}}{{{text}}}")

    for r in matrix:
        be, bw, vw = BACKEND_WORD[r["backend"]], bound_word(r["wait_us"]), r["_word"]
        add(f"Wake{be}{r['arm'].capitalize()}{bw}{vw}", r["median_us"])
        if r["arm"] == "defect" and r["b_eff_us"] != "" and not r["bimodal"]:
            add(f"Wake{be}BEff{bw}{vw}", r["b_eff_us"])
            if r["median_abs_residual_over_beff"] != "":
                add(f"Wake{be}AbsResidualPct{bw}{vw}", 100 * r["median_abs_residual_over_beff"], 2)
        # H3, the detection test: median of the first 21 measured posts, per arm, and the
        # threshold B_eff/5 it is compared with (one per cell, from the defect run).
        if r["h3_median21_us"] != "":
            add(f"WakeDetect{be}{r['arm'].capitalize()}{bw}{vw}", r["h3_median21_us"])
        if r["arm"] == "defect" and r["h3_threshold_us"] != "":
            add(f"WakeDetectThreshold{be}{bw}{vw}", r["h3_threshold_us"])
    for r in sweep:
        be, bw, vw = BACKEND_WORD[r["backend"]], bound_word(r["wait_us"]), r["_word"]
        if r["bimodal"]:
            continue
        if r["b_eff_median_us"] != "":
            add(f"WakeSweep{be}BEff{bw}{vw}", r["b_eff_median_us"])
        if r["median_abs_residual_over_beff"] != "":
            add(f"WakeSweep{be}AbsResidualPct{bw}{vw}", 100 * r["median_abs_residual_over_beff"], 2)
    prefix = {"bound-sweep": "WakeBound", "bound-sweep-qos": "WakeBoundQos"}
    for r in bound:
        add(f"{prefix[r['design']]}{BACKEND_WORD[r['backend']]}Fixed{bound_word(r['wait_us'])}{r['_word']}",
            r["median_us"])
    for r in bound_summary:
        if r["h2_ratio"] != "":
            add(f"{prefix[r['design']]}{BACKEND_WORD[r['backend']]}RatioMax{r['_word']}", r["h2_ratio"], 3)
    lines.append("% Derived: design constants, host facts, aggregates, resampling and random gaps.")
    for name, value, digits in extra:
        add(name, value, digits)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mode_rows(by_design: dict, sweep: list[dict]) -> list[dict]:
    """One row per bimodal cell: matrix runs, gap-sweep steps and pooled gap-sweep cells."""
    rows = []
    for design in ("matrix", "gap-sweep"):
        for r in by_design[design]:
            if r["bimodal"]:
                rows.append({"design": design, "scope": "run", "backend": r["backend"], "variant": r["variant"],
                             "wait_us": r["wait_us"], "gap_us": r["gap_us"], "phase_frac": r["phase_frac"],
                             "intervals": len(r["intervals"]), **two_modes(r["intervals"]),
                             "source": display_path(r["path"])})
    for c in sweep:
        if c["bimodal"]:
            rows.append({"design": "gap-sweep", "scope": "pooled", "backend": c["backend"], "variant": c["variant"],
                         "wait_us": c["wait_us"], "gap_us": "", "phase_frac": "", "intervals": len(c["_intervals"]),
                         **two_modes(c["_intervals"]), "source": ""})
    rows.sort(key=lambda r: (r["design"], r["scope"], r["backend"], r["variant"], r["wait_us"],
                             str(r["gap_us"]).zfill(9), str(r["phase_frac"])))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="*", type=Path, default=[REPO / "results" / "raw"])
    ap.add_argument("--out-dir", type=Path, default=REPO / "results" / "rework")
    args = ap.parse_args()

    files = sorted(f for d in args.inputs for f in (d.rglob("*.jsonl") if d.is_dir() else [d]))
    if not files:
        raise SystemExit("no .jsonl files under " + ", ".join(map(str, args.inputs)))
    smoke = any("smoke" in part for f in files for part in f.resolve().parts)
    if smoke and args.out_dir.resolve() in ((REPO / "results").resolve(), (REPO / "results" / "rework").resolve()):
        raise SystemExit("smoke data must not be written to results/ or results/rework; pass --out-dir under results/smoke")

    by_design, cells = defaultdict(list), {}
    for f in files:
        run = load_run(f)
        key = (run["design"], run["backend"], run["variant"], run["wait_us"], run["gap_us"], run["phase_frac"],
               run["arm"])
        if key in cells:
            raise SystemExit(f"cell {key} appears in both {cells[key]} and {f}; pass one run per host")
        cells[key] = f
        by_design[run["design"]].append(run)
    unknown = set(by_design) - DESIGNS
    if unknown:
        raise SystemExit(f"unknown designs {sorted(unknown)}")

    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    matrix = matrix_rows(by_design["matrix"])
    steps, sweep, posts = gap_sweep_tables(by_design["gap-sweep"])
    resample = resample_misses(sweep)
    bound, bound_summary = bound_sweep_tables(by_design["bound-sweep"] + by_design["bound-sweep-qos"])
    random_rows = random_gap_tables(by_design["random-gap"])
    modes = mode_rows(by_design, sweep)
    all_runs = [r for d in by_design.values() for r in d]
    extra = derived_macros(all_runs, matrix, steps, sweep, bound, random_rows, modes,
                           host_meta(args.inputs), resample, smoke)
    write_csv(matrix, out / "matrix.csv")
    write_csv(steps, out / "gap_sweep.csv")
    write_csv(sweep, out / "gap_sweep_summary.csv")
    write_csv(posts, out / "gap_sweep_posts.csv")
    write_csv(bound, out / "bound_sweep.csv")
    write_csv(bound_summary, out / "bound_sweep_summary.csv")
    write_csv(modes, out / "modes.csv")
    write_csv(random_rows, out / "random_gap.csv")
    write_macros(out / "macros.tex", files, smoke, matrix, sweep, bound, bound_summary, extra, resample)
    print(f"{len(files)} runs: matrix {len(matrix)} cells, gap sweep {len(steps)} steps, "
          f"bound sweep {len(bound)} cells, random gap {len(random_rows)} cells, {len(modes)} bimodal "
          f"-> {display_path(out)}"
          + ("" if mannwhitneyu else " (scipy not installed: no Mann-Whitney or Kruskal-Wallis columns)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Synthetic wakeprobe and abpair outputs with known answers, for the tests only.

Nothing here is measured data. The lines carry the fields that score.py and pilot.py read,
with the names and meanings of bench/wakeprobe.cpp and bench/abpair.cpp (test_plan.py checks
every name against the tools' sources). Every value that comes from the plan is read from the
plan entry; every value that comes from the build or the host is a fixture.
"""

import json
import os

import grid
import h4log
import stats
from readers import ABPAIR_MODE, ABPAIR_PROGRAM, RELEASE, arg_value

# Delays relative to B_eff: a flagged trial has every delay at 0.6 B_eff (above B_eff / 5 and
# above B_eff / 2); a clean one has every delay at 0.01 B_eff.
HIGH = 0.6
LOW = 0.01

BENCH_COMMIT = "0123456789abcdef0123456789abcdef01234567"  # a fixture, not a commit of anything
OTHER_COMMIT = "fedcba9876543210fedcba9876543210fedcba98"
WINDOWS_TIMER_US = 15625  # the default timer period of Windows, in us (a fixture for the summary)


def write_lines(path, objs):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for obj in objs:
            f.write(json.dumps(obj) + "\n")


def process_delays(b_eff_ns, t_med_trials, t_count_only=0, trials=grid.TRIALS_PER_PROCESS):
    """630 delays: the first t_med_trials trials flag T_med (and T_count), the next
    t_count_only trials flag T_count only (one delay of 21 high), the rest flag neither."""
    hi = int(HIGH * b_eff_ns)
    lo = max(1, int(LOW * b_eff_ns))
    out = []
    for t in range(trials):
        if t < t_med_trials:
            out += [hi] * grid.UNITS_PER_TRIAL
        elif t < t_med_trials + t_count_only:
            out += [lo] * (grid.UNITS_PER_TRIAL - 1) + [hi]
        else:
            out += [lo] * grid.UNITS_PER_TRIAL
    return out


def _cpus(text):
    return None if text is None else [int(x) for x in text.split(",")]


def wakeprobe_output(entry, delays, b_eff_ns=None, coarsest_timer_us=None, lose_at=None, residuals=None,
                     error=None, timed_out_at=(), system="linux", summary=None):
    """The lines of one wakeprobe run of a plan entry.

    delays: the measured units' delay_ns, in order. lose_at: a measured position whose unit is
    never delivered; the run ends there, as wakeprobe does. residuals: residual_ns per
    measured unit (the sweep). timed_out_at: measured positions flagged timed_out. system: which
    system's settings the summary shows (timer slack on Linux, the timer resolution on Windows;
    coarsest_timer_us is the Windows default period). summary: fields to override in the summary.
    """
    args = entry["args"]
    backend = arg_value(args, "--backend")
    variant = arg_value(args, "--variant")
    workers = int(arg_value(args, "--workers", "1"))
    wait_us = int(arg_value(args, "--wait-us"))
    protocol = arg_value(args, "--protocol", "single")
    posts = int(arg_value(args, "--posts"))
    warmup = int(arg_value(args, "--warmup"))
    calibrate = int(arg_value(args, "--calibrate", "0"))
    delaying = grid.delays_single_posts(variant, workers)
    batch = protocol == "batch"
    seed = arg_value(args, "--gap-seed")
    gap_range = arg_value(args, "--gap-frac-range")
    phase = arg_value(args, "--phase-frac")
    worker_cpus = _cpus(arg_value(args, "--worker-cpus"))
    poster = arg_value(args, "--poster-cpu")

    lines = []
    clock = [1000000]
    index = [0]

    def unit(kind, worker, delay, residual=None, timed_out=False):
        post = clock[0]
        rec = {"design": arg_value(args, "--design"), "label": entry["id"], "backend": backend, "wait_us": wait_us,
               "gap_us": None, "phase_frac": None if phase is None else float(phase), "index": index[0],
               "worker": worker, "kind": kind, "warmup": kind != "measured", "post_ns": post,
               "timed_out": timed_out, "gap_drawn_ns": None}
        start = post
        if batch and kind in ("warmup", "measured"):
            rec.update(unit="round", tasks=192, ran=192, release_ns=post + 1000, gate_timed_out=False)
            start = post + 1000
        rec["delay_ns"] = delay
        rec["exec_ns"] = None if delay is None else start + delay
        rec["predicted_ns"] = None
        rec["residual_ns"] = residual
        rec["phase_ns"] = None
        lines.append(rec)
        index[0] += 1
        if delay is not None:
            clock[0] = start + delay + 2 * wait_us * 1000  # the next post, a gap of 2 B later
        return delay is not None

    lost = 0
    count = 0
    ended = False
    if not unit("probe", 0, 1000):
        ended = True
    if not ended and delaying:
        for _ in range(calibrate):
            unit("calib", 0, b_eff_ns or wait_us * 1000)
    for i in range(warmup):
        if ended:
            break
        unit("warmup", i % workers, 1000)
    for k in range(posts):
        if ended:
            break
        if lose_at is not None and k == lose_at:
            unit("measured", (warmup + k) % workers, None)
            lost += 1
            ended = True
            break
        res = residuals[k] if residuals is not None else None
        unit("measured", (warmup + k) % workers, delays[k], res, k in timed_out_at)
        count += 1
    windows = system == "windows"
    default_100ns = None if coarsest_timer_us is None else coarsest_timer_us * 10
    summary_line = {
        "summary": True, "design": arg_value(args, "--design"), "label": entry["id"], "backend": backend,
        "backend_requested": backend, "wait_us": wait_us, "gap_us": None,
        "phase_frac": None if phase is None else float(phase),
        "gap_frac_range": None if gap_range is None else [float(x) for x in gap_range.split(":")],
        "gap_seed": None if seed is None else int(seed),
        "gap_unit": None if gap_range is None else arg_value(args, "--gap-unit", "bound"),
        "protocol": protocol, "posts": posts, "warmup": warmup, "calibrate": calibrate,
        "count": count, "lost": lost, "b_eff_ns": b_eff_ns if delaying else None,
        "timerslack_ns_requested": None if windows else 1, "timerslack_set_rc": None if windows else 0,
        "timerslack_ns_main": None if windows else 1, "timerslack_ns_worker": None if windows else 1,
        "timer_period_ms_requested": 0 if windows else None,
        "timer_resolution_100ns_start": default_100ns if windows else -1,
        "timer_resolution_100ns_gap": [default_100ns] * warmup if windows and warmup and gap_range else [],
        "timer_resolution_100ns_end": default_100ns if windows else -1,
        "variant": variant, "workers": workers, "worker_cpus": worker_cpus, "worker_cpus_seen": worker_cpus,
        "poster_cpu": None if poster is None else int(poster),
        "poster_cpu_seen": None if poster is None else int(poster),
        "coarsest_timer_us": coarsest_timer_us, "build_type": RELEASE, "bench_commit": BENCH_COMMIT,
        "bench_dirty": False, "error": error,
    }
    summary_line.update(summary or {})
    lines.append(summary_line)
    return lines


def abpair_output(entry, ratios, error=None, drop_windows=0, summary=None):
    """The lines of one abpair run: pair k has side A at ratios[k] times side B's throughput.

    Pairs run AB, BA, AB, ... (abba). drop_windows removes that many windows from the end, as
    a run that stopped early would. summary: fields to override in the summary.
    """
    args = entry["args"]
    pairs = int(arg_value(args, "--pairs"))
    arm = {"A": arg_value(args, "--arm-a"), "B": arg_value(args, "--arm-b")}
    base = 10000000  # tasks in a 10 s window at 1e6 tasks per second
    elapsed = 10 * 1000 * 1000 * 1000
    windows = []
    for k in range(pairs):
        for position, side in enumerate(("A", "B") if k % 2 == 0 else ("B", "A")):
            tasks = int(round(ratios[k] * base)) if side == "A" else base
            windows.append({"pair": k, "position": position, "side": side, "arm": arm[side], "ran": arm[side],
                            "tasks": tasks, "passes": [tasks], "elapsed_ns": elapsed,
                            "tasks_per_s": round(tasks * 1e9 / elapsed, 3)})
    if drop_windows:
        windows = windows[:-drop_windows]
    worker_cpus = _cpus(arg_value(args, "--worker-cpus"))
    producer_cpus = _cpus(arg_value(args, "--producer-cpus"))
    summary_line = {
        "summary": True, "program": ABPAIR_PROGRAM, "check": False, "backend": arg_value(args, "--backend"),
        "arm_a": arm["A"], "arm_b": arm["B"], "workers": int(arg_value(args, "--workers")),
        "producers": int(arg_value(args, "--producers")), "inflight": int(arg_value(args, "--inflight")),
        "pairs": pairs, "order": arg_value(args, "--order"), "mode": ABPAIR_MODE,
        "window_ms": int(arg_value(args, "--window-ms")), "work_ns": int(arg_value(args, "--work-ns")),
        "worker_cpus": worker_cpus, "producer_cpus": producer_cpus,
        "worker_cpus_seen": worker_cpus, "producer_cpus_seen": producer_cpus,
        "windows": len(windows), "build_type": RELEASE, "bench_commit": BENCH_COMMIT, "bench_dirty": False,
        "error": error,
    }
    summary_line.update(summary or {})
    return windows + [summary_line]


# ---------------------------------------------------------------------------------------------
# The runner's files (contract C3)
# ---------------------------------------------------------------------------------------------


def lines_exit(lines):
    """The exit code the tool would give: 0, 4 for an error, 6 for a lost unit."""
    summary = [x for x in lines if x.get("summary")]
    if summary and summary[0].get("error"):
        return 4
    if any("kind" in x and x.get("exec_ns") is None for x in lines):
        return 6
    return 0


def done_text(exits):
    """A .done file as the runner writes it. exits: one entry per attempt, an int or "timeout"."""
    if exits[0] == 0:
        outcome = "ok"
    else:
        outcome = "ok after rerun" if len(exits) == 2 and exits[1] == 0 else "incomplete"
    return "outcome: %s\n" % outcome + "".join("attempt %d exit: %s\n" % (n, e) for n, e in enumerate(exits, 1))


def write_process(folder, entry, first=None, rerun=None, exits=None, done=True, void=()):
    """The files the runner leaves for one process: ID.jsonl, ID-rerun.jsonl, ID.done and void runs.

    first, rerun: the lines of each run (or None for no file). exits: the attempts' exit codes
    (default: lines_exit of each run; a rerun means a second attempt). done=False leaves no .done
    file. void: (k, lines, rerun) per void run, written as ID[-rerun].interrupted-K.jsonl.
    """
    pid = entry["id"]
    if exits is None:
        exits = [lines_exit(first)] + ([lines_exit(rerun)] if rerun is not None else [])
    if first is not None:
        write_lines(os.path.join(folder, pid + ".jsonl"), first)
    if rerun is not None:
        write_lines(os.path.join(folder, pid + "-rerun.jsonl"), rerun)
    for k, lines, is_rerun in void:
        write_lines(os.path.join(folder, "%s%s.interrupted-%d.jsonl" % (pid, "-rerun" if is_rerun else "", k)), lines)
    if done:
        with open(os.path.join(folder, pid + ".done"), "w", encoding="ascii", newline="\n") as f:
            f.write(done_text(exits))


# ---------------------------------------------------------------------------------------------
# The revision log's block
# ---------------------------------------------------------------------------------------------

# An s whose pilot rule gives each R that the rule can give (stats.pilot_choose_r): R = 16 and
# R = 22 are never chosen, because power is not monotone in R. A fixture, not a pilot result.
S_FOR_R = {14: 0.010, 18: 0.0125, 20: 0.015, 24: 0.0155}
UNDERPOWERED_S = 0.05  # R = 24 and underpowered


def pilot_cell(system, backend, work_us, r=14, underpowered=False, pairs=8):
    """The fields pilot.pilot_cell gives, computed by the pilot's own rule from a fixture s.

    underpowered=True gives R = 24 and the flag. pairs under 2 gives no s: R = 24, underpowered.
    """
    cell = {"system": system, "backend": backend, "work_us": work_us, "pairs": pairs}
    if pairs < 2:
        cell.update(s=None, R=grid.H4_R_MAX, power=None, underpowered=True)
        return cell
    s = UNDERPOWERED_S if underpowered else S_FOR_R[r]
    chosen, power, under = stats.pilot_choose_r(s)
    assert (chosen, under) == (grid.H4_R_MAX if underpowered else r, underpowered), (s, chosen, under)
    cell.update(s=s, R=chosen, power=power, underpowered=under)
    return cell


def pilot_cells(r=14, overrides=None):
    """The `cells` of a pilot record for the 20 H4 cells: key -> the fields pilot.pilot_cell gives.

    Every cell gets the R of `r` (fixtures, not pilot results); overrides maps a key to the
    keyword arguments of pilot_cell to change (r, underpowered, pairs).
    """
    overrides = overrides or {}
    out = {}
    for key, (system, backend, work_us) in h4log.expected_cells().items():
        out[key] = pilot_cell(system, backend, work_us, **dict({"r": r}, **overrides.get(key, {})))
    return out

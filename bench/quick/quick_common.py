"""P1 quick benchmarks (quick-plan.md, decision d010 option B): the shared definitions.

The five plans, their cells and arms, the time arithmetic and the seeds live here, so that the plan
generator, the runner, the request writer and the tests read one copy. Nothing here times anything.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
PLANS_DIR = HERE / "plans"

BUDGET_S = 600  # CLAUDE.md rule 5: a campaign takes 5 to 10 minutes
MARGIN = 1.10  # starts, stops and the condition switches the estimate does not see
POSTS_PER_TRIAL = 21  # hypotheses-v2.md section 2: one trial is 21 posts (or rounds)
WARMUP = 5  # as the frozen S and Bt processes
CALIB_POSTS = 11  # as the frozen calibration
WORKER_CPU = 14  # lab/laptop/README.md placement, W = 1
POSTER_CPU = 2
LOAD_CPUS = [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15]  # every CPU outside core 0, the poster and the worker
GAP_RANGE = "0.1:3.0"
BOUNDS_US = (1000, 10000)
MEASURED_COMMIT = "796b71fd8fd88ab57654bd27eafe6c4ea0d408d4"  # the v2 campaign's code (STATE.md)
# Paths that must equal the campaign's code: the loop and everything it builds with. wakeprobe.cpp and
# bench/CMakeLists.txt change on this branch (--calibrate-idle and its tests); quick-plan.md says how.
GUARDED_PATHS = ["loop", "tests", "CMakeLists.txt", "bench/abpair.cpp", "bench/bench_common.hpp",
                 "bench/probe_platform.hpp", "bench/code_paths.txt"]
# Seeds of the frozen designs, never reused (hypotheses-v2.md section 8; the 2026-09 study).
FROZEN_SEEDS = {20260926, 20261026, 20261027} | {2026102700 + i for i in range(10)}

BACKENDS = {"linux": ("epoll", "io_uring", "condvar"), "windows": ("iocp", "condvar")}

# Seconds per trial of 21 units, from measured bases only (quick-plan.md, "Arithmetic"):
# protocol S, 100-trial cells (hypotheses-v2.md section 9): Arch fixed 3.3 / 33.7 s and delaying
# 4.7 / 44.3 s at B = 1 / 10 ms, Windows 5 / 34 s and 34.5 / 51 s; protocol Bt, the fixed loop's
# 211-round probe (STATE.md, 5e0fc7e): Arch 0.4 / 3.4 s, Windows 1 / 4 s.
TRIAL_S = {
    ("linux", "single", "fixed", 1000): 3.3 / 100, ("linux", "single", "fixed", 10000): 33.7 / 100,
    ("linux", "single", "delaying", 1000): 4.7 / 100, ("linux", "single", "delaying", 10000): 44.3 / 100,
    ("linux", "batch", "fixed", 1000): 0.4 * 21 / 211, ("linux", "batch", "fixed", 10000): 3.4 * 21 / 211,
    ("windows", "single", "fixed", 1000): 5 / 100, ("windows", "single", "fixed", 10000): 34 / 100,
    ("windows", "single", "delaying", 1000): 34.5 / 100, ("windows", "single", "delaying", 10000): 51 / 100,
    ("windows", "batch", "fixed", 1000): 1 * 21 / 211, ("windows", "batch", "fixed", 10000): 4 * 21 / 211,
}
WIN_PERIOD_S = 0.0156  # the default Windows timer period (hypotheses-v2.md section 12)
WIN_GAP_OVERRUN_S = 0.0008  # Windows gap sleeps overran by about 0.8 ms per post (hypotheses-v2.md section 9)
PROCESS_S = 0.13  # 50 ms settle + a process start; the A/A pilot's abpair processes ran at most 0.081 s over their windows
BLOCK_S = 5.0  # a condition switch: hog start and stop with its 2 s ramp, or a plan switch with its 2 s settle; no measured basis


@dataclass(frozen=True)
class PlanSpec:
    name: str
    os: str  # linux or windows
    run: str  # R1, R2 or R3
    conditions: tuple[str, ...]
    rounds: int


SPECS = {
    "quick-L-r1": PlanSpec("quick-L-r1", "linux", "R1", ("quiet", "load", "power"), 3),
    "quick-W-r1": PlanSpec("quick-W-r1", "windows", "R1", ("quiet", "load", "power"), 3),
    "quick-L-r2": PlanSpec("quick-L-r2", "linux", "R2", ("quiet",), 10),
    "quick-W-r2": PlanSpec("quick-W-r2", "windows", "R2", ("quiet",), 10),
    "quick-W-r3": PlanSpec("quick-W-r3", "windows", "R3", ("quiet",), 6),
}


def label_seed(text: str) -> int:
    """The first 8 hex digits of sha256(text), as P5's quick plans seed their orders."""
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


def gap_seed(plan: str, rnd: int) -> int:
    """One gap sequence per round, shared by every cell of the round (as the frozen processes share theirs)."""
    seed = label_seed(f"P1 quick gaps {plan} round {rnd}")
    if seed in FROZEN_SEEDS:
        raise ValueError(f"gap seed {seed} of {plan} round {rnd} is a frozen seed")
    return seed


def b_eff_s(os_name: str, bound_us: int) -> float:
    """B_eff for the arithmetic only: B on Linux, the larger of B and the timer period on Windows."""
    b = bound_us / 1e6
    return max(b, WIN_PERIOD_S) if os_name == "windows" else b


def trial_s(os_name: str, protocol: str, delaying: bool, bound_us: int, gap_unit: str) -> float:
    if gap_unit == "timer":
        # R3: no measured basis; arithmetic with the default period as the gap unit (quick-plan.md).
        unit = max(bound_us / 1e6, WIN_PERIOD_S)
        post = 1.55 * unit + WIN_GAP_OVERRUN_S + (b_eff_s(os_name, bound_us) / 2 if delaying else 0.0)
        return POSTS_PER_TRIAL * post
    return TRIAL_S[(os_name, protocol, "delaying" if delaying else "fixed", bound_us)]


def unit_estimate_s(os_name: str, protocol: str, delaying: bool, bound_us: int, trials: int, gap_unit: str,
                    calibrate: int, calibrate_idle: int, posts: int | None = None) -> float:
    per_trial = trial_s(os_name, protocol, delaying, bound_us, gap_unit)
    per_post = per_trial / POSTS_PER_TRIAL
    n_posts = posts if posts is not None else trials * POSTS_PER_TRIAL
    be = b_eff_s(os_name, bound_us)
    est = PROCESS_S + per_post * (1 + WARMUP + n_posts)
    est += calibrate * (0.1 * bound_us / 1e6 + be)
    est += (calibrate_idle + 1) * be
    return est


def wakeprobe_args(backend: str, variant: str, protocol: str, bound_us: int, posts: int, warmup: int,
                   gap: list[str], calibrate: int, calibrate_idle: int, design: str, label: str) -> list[str]:
    args = ["--backend", backend, "--variant", variant, "--protocol", protocol, "--workers", "1",
            "--worker-cpus", str(WORKER_CPU), "--poster-cpu", str(POSTER_CPU), "--wait-us", str(bound_us),
            "--posts", str(posts), "--warmup", str(warmup), "--calibrate", str(calibrate)]
    if calibrate_idle:
        args += ["--calibrate-idle", str(calibrate_idle)]
    return args + gap + ["--design", design, "--label", label]


def _unit(spec: PlanSpec, condition: str, backend: str, variant: str, protocol: str, bound_us: int, trials: int,
          seed: int, *, arm: str | None = None, gap_unit: str = "bound", calibrate: int = 0, calibrate_idle: int = 0,
          calib_only: bool = False) -> dict:
    arm = arm or variant
    proto = "S" if protocol == "single" else "Bt"
    kind = "calib" if calib_only else proto
    cell = f"{spec.run}-{backend}-{arm}-{kind}-B{bound_us // 1000}ms-{condition}"
    if calib_only:
        posts, warmup, gap = 1, 0, ["--gap-us", "0"]
    else:
        posts, warmup = trials * POSTS_PER_TRIAL, WARMUP
        gap = ["--gap-frac-range", GAP_RANGE, "--gap-unit", gap_unit, "--gap-seed", str(seed)]
    delaying = variant == "absent_wake"
    est = unit_estimate_s(spec.os, protocol, delaying, bound_us, trials, gap_unit, calibrate, calibrate_idle,
                          posts=posts if calib_only else None)
    return {"cell": cell, "arm": arm, "condition": condition, "backend": backend, "variant": variant,
            "protocol": protocol, "wait_us": bound_us, "posts": posts, "est_s": round(est, 4),
            "args_head": (backend, variant, protocol, bound_us, posts, warmup, gap, calibrate, calibrate_idle)}


def round_units(spec: PlanSpec, trials: int, seed: int) -> list[dict]:
    """Every unit of one round of a plan, in a fixed (unshuffled) order."""
    units: list[dict] = []
    backends = BACKENDS[spec.os]
    for condition in spec.conditions:
        for backend in backends:
            for bound in BOUNDS_US:
                if spec.run == "R1":
                    for variant in ("none", "plain_wake"):
                        for protocol in ("single", "batch"):
                            units.append(_unit(spec, condition, backend, variant, protocol, bound, trials, seed))
                    # The bound's source for T_med under this condition: calibration posts of absent wake.
                    units.append(_unit(spec, condition, backend, "absent_wake", "single", bound, trials, seed,
                                       calibrate=CALIB_POSTS, calib_only=True))
                    if condition == "quiet" and backend == backends[0]:
                        # The A/A pair (rule 4): the quiet fixed loop twice, same binary, same arguments.
                        units.append(_unit(spec, condition, backend, "none", "single", bound, trials, seed,
                                           arm="none-aa"))
                elif spec.run == "R2":
                    units.append(_unit(spec, condition, backend, "none", "single", bound, trials, seed,
                                       calibrate_idle=CALIB_POSTS))
                    units.append(_unit(spec, condition, backend, "absent_wake", "single", bound, trials, seed,
                                       calibrate=CALIB_POSTS, calibrate_idle=CALIB_POSTS))
                elif spec.run == "R3":
                    units.append(_unit(spec, condition, backend, "absent_wake", "single", bound, trials, seed,
                                       gap_unit="timer", calibrate=CALIB_POSTS, calibrate_idle=CALIB_POSTS))
                    for variant in ("none", "plain_wake"):
                        units.append(_unit(spec, condition, backend, variant, "single", bound, trials, seed,
                                           gap_unit="timer", calibrate_idle=CALIB_POSTS))
    return units


def round_estimate_s(spec: PlanSpec, trials: int) -> float:
    return sum(u["est_s"] for u in round_units(spec, trials, 1)) + BLOCK_S * len(spec.conditions)


def fits(spec: PlanSpec, trials: int) -> bool:
    return spec.rounds * round_estimate_s(spec, trials) * MARGIN <= BUDGET_S


def trials_for(spec: PlanSpec) -> int:
    """The most trials per process with which the plan's rounds fit the budget with the margin."""
    t = 0
    while fits(spec, t + 1):
        t += 1
    if t < 1:
        raise ValueError(f"{spec.name}: not even one trial per process fits {BUDGET_S} s")
    return t


def expand(spec: PlanSpec) -> tuple[dict, list[dict]]:
    """The plan header and its rows in run order: per round the conditions in a new random order, and
    inside each condition block the units in a new random order."""
    trials = trials_for(spec)
    plan_seed = label_seed(f"P1 quick order {spec.name}")
    rng = random.Random(plan_seed)
    rows: list[dict] = []
    for rnd in range(spec.rounds):
        seed = gap_seed(spec.name, rnd)
        units = round_units(spec, trials, seed)
        conditions = list(spec.conditions)
        rng.shuffle(conditions)
        for block, condition in enumerate(conditions):
            block_units = [u for u in units if u["condition"] == condition]
            rng.shuffle(block_units)
            for u in block_units:
                uid = f"{u['cell']}-r{rnd}"
                backend, variant, protocol, bound, posts, warmup, gap, cal, idle = u["args_head"]
                args = wakeprobe_args(backend, variant, protocol, bound, posts, warmup, gap, cal, idle,
                                      f"p1quick-{spec.run}", uid)
                row = {k: v for k, v in u.items() if k != "args_head"}
                row.update({"id": uid, "round": rnd, "block": block, "gap_seed": seed if "--gap-seed" in gap else None,
                            "args": args})
                rows.append(row)
    header = {"plan": spec.name, "os": spec.os, "run": spec.run, "conditions": list(spec.conditions),
              "rounds": spec.rounds, "trials_per_process": trials, "posts_per_trial": POSTS_PER_TRIAL,
              "order_seed": plan_seed, "budget_s": BUDGET_S, "margin": MARGIN,
              "round_estimate_s": round(round_estimate_s(spec, trials), 3),
              "load_cpus": LOAD_CPUS if "load" in spec.conditions else None,
              "measured_commit": MEASURED_COMMIT, "generator": "bench/quick/quick_plans.py"}
    return header, rows


def plan_text(spec: PlanSpec) -> str:
    import json
    header, rows = expand(spec)
    lines = [json.dumps(header, sort_keys=True)] + [json.dumps(r, sort_keys=True) for r in rows]
    return "\n".join(lines) + "\n"


def file_sha256(path: Path) -> str:
    """sha256 of the file with CRLF read as LF, so a Windows checkout pins the same bytes."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()

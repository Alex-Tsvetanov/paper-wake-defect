#!/usr/bin/env python3
"""Reduce results/rework/gap_sweep_posts.csv to the points drawn in paper/fig-sawtooth.tex.

One series per backend for the defect arm at B = 1 ms: epoll and io_uring on L (timer slack
1 ns, the unsuffixed Linux case) and IOCP on W at the default timer period. For each series:

  posts  the measured posts at odd positions of each gap-sweep step, in index order (10 of
         the 21 posts per step, so 300 of 630 per series); no post is altered.
  model  the sawtooth d = B_eff - (phase mod B_eff), one line per tooth from (k B_eff, B_eff)
         to ((k + 1) B_eff, 0), the last one cut at the largest measured phase of the kept
         posts, teeth separated by a nan row so the plot does not draw the jump. For a cell with one wake period, B_eff is the
         pooled median of the per-step B_eff (b_eff_median_us in gap_sweep_summary.csv), kind 1.
         For a bimodal cell (no single wake period), there is one model line per mode of the
         pooled calibration intervals (modes.csv, scope pooled): the mode with more intervals
         is kind 1, the other kind 2.

Output columns: series (1 epoll, 2 io_uring, 3 iocp), kind (0 post, 1 model, 2 model of the
minority mode), backend, variant, wait_us, phase_us, delay_us, b_eff_us. pgfplots selects a
series and kind with restrict expr to domain on 10 * series + kind.

  fig_sawtooth.py [--results DIR]
"""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERIES = [(1, "epoll", "slack=1ns"), (2, "io_uring", "slack=1ns"), (3, "iocp", "timer=default")]
BOUND_US = "1000"
FIELDS = ["series", "kind", "backend", "variant", "wait_us", "phase_us", "delay_us", "b_eff_us"]


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def model_periods(summary: list[dict], modes: list[dict], backend: str, variant: str) -> list[tuple[int, str]]:
    """(kind, B_eff in us) for each model line of the cell: one, or one per mode if bimodal."""
    key = (backend, variant, BOUND_US)
    rows = [r for r in summary if (r["backend"], r["variant"], r["wait_us"]) == key]
    if len(rows) != 1:
        raise SystemExit(f"no pooled gap-sweep row for {backend} {variant} B={BOUND_US} us")
    if rows[0]["bimodal"] != "True":
        if not rows[0]["b_eff_median_us"]:
            raise SystemExit(f"no pooled B_eff for {backend} {variant} B={BOUND_US} us")
        return [(1, rows[0]["b_eff_median_us"])]
    pooled = [m for m in modes if (m["design"], m["scope"]) == ("gap-sweep", "pooled")
              and (m["backend"], m["variant"], m["wait_us"]) == key]
    if len(pooled) != 1:
        raise SystemExit(f"{backend} {variant} B={BOUND_US} us is bimodal but modes.csv has no pooled row")
    m = pooled[0]
    by_size = sorted([(int(m["n_lo"]), m["mode_lo_us"]), (int(m["n_hi"]), m["mode_hi_us"])], reverse=True)
    return [(1, by_size[0][1]), (2, by_size[1][1])]


def post_rows(posts: list[dict], series: int, backend: str, variant: str, b_eff: str) -> list[dict]:
    # A step is one run: L sets it by gap_us, W by phase_frac (a fraction of B_eff).
    steps = defaultdict(list)
    for r in posts:
        if (r["backend"], r["variant"], r["wait_us"]) == (backend, variant, BOUND_US) and r["phase_us"]:
            steps[(r["gap_us"], r["phase_frac"])].append(r)
    if not steps:
        raise SystemExit(f"no gap-sweep posts for {backend} {variant} B={BOUND_US} us")
    out = []
    for step in sorted(steps, key=lambda s: tuple(float(v) if v else -1.0 for v in s)):
        kept = sorted(steps[step], key=lambda r: int(r["index"]))[1::2]
        out += [{"series": series, "kind": 0, "backend": backend, "variant": variant, "wait_us": BOUND_US,
                 "phase_us": r["phase_us"], "delay_us": r["delay_us"], "b_eff_us": b_eff} for r in kept]
    return out


def model_rows(series: int, kind: int, backend: str, variant: str, b_eff: str, max_phase: float) -> list[dict]:
    b = float(b_eff)
    base = {"series": series, "kind": kind, "backend": backend, "variant": variant, "wait_us": BOUND_US,
            "b_eff_us": b_eff}
    out = []
    for k in range(math.ceil(max_phase / b)):
        end = min((k + 1) * b, max_phase)  # the last tooth stops where the data stop
        out.append({**base, "phase_us": f"{k * b:.3f}", "delay_us": f"{b:.3f}"})
        out.append({**base, "phase_us": f"{end:.3f}", "delay_us": f"{(k + 1) * b - end:.3f}"})
        out.append({**base, "phase_us": "nan", "delay_us": "nan"})
    return out[:-1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", type=Path, default=REPO / "results" / "rework")
    args = ap.parse_args()

    posts = read_csv(args.results / "gap_sweep_posts.csv")
    summary = read_csv(args.results / "gap_sweep_summary.csv")
    modes = read_csv(args.results / "modes.csv")
    rows = []
    for series, backend, variant in SERIES:
        periods = model_periods(summary, modes, backend, variant)
        kept = post_rows(posts, series, backend, variant, periods[0][1])
        max_phase = max(float(r["phase_us"]) for r in kept)
        rows += kept
        for kind, b_eff in periods:
            rows += model_rows(series, kind, backend, variant, b_eff, max_phase)
        print(f"{backend} {variant}: {len(kept)} posts, model B_eff "
              + ", ".join(f"{b} us (kind {k})" for k, b in periods))
    out = args.results / "fig_sawtooth.csv"
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    shown = out.resolve().relative_to(REPO).as_posix() if out.resolve().is_relative_to(REPO) else out
    print(f"{len(rows)} rows -> {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

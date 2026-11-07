# Quick plan for P1's run R4

Frozen at the commit that adds this file on branch `quick-benchmarks` (2026-11-07), before any R4 data exists. Later
changes go into a new file, never into this one (CLAUDE.md rule 7). `quick-plan.md` (runs R1 to R3) stays as it is.

This file adds one descriptive Windows run. It edits no frozen file: `hypotheses.md`, `hypotheses-v2.md`,
`hypotheses-v2-h6.md`, `design/`, `plans/`, `analysis/`, `results/`, `bench/run_campaign.py`, the D5 records and
`quick-plan.md` stay as they are. The frozen hypotheses H1 to H6, their families, tests and Holm steps are not reopened.
Nothing here enters a frozen family; every result is reported as median and spread (rule 4).

Authority: the three-model vote `decisions/d013-p1-round9.md` (Papers repository), question 3 (item 32 of
`p1-wake-defect/writing/fixes-r9.md`), option H, which stands under CLAUDE.md rule 13 because Claude, Grok and Gemini all
ranked it first. Rule 5 sets the length: the campaign takes at most 10 minutes. Rule 1: Release only, /O2 with MSVC.
Rule 3: the laptop only, one campaign and one OS at a time.

## What R4 answers

Gemini r9 item 6, last part: do unobserved primitive, wrong worker, and 2 or 4 workers still reach a high detection rate
on Windows at B = 1 ms when the gaps are in units of the timer period? R3 covered absent wake, the fixed loop and plain
wake at 1 worker on IOCP and the condition variable, at B = 1 and 10 ms. R4 takes the 12 cells that option H fixed.

## Cells (fixed by d013 option H)

Windows, B = 1 ms, protocol S (single posts), `--gap-unit timer`, on IOCP and the condition variable:

| Variant | Workers | Calibration posts | Idle calibration |
|---|---|---|---|
| unobserved primitive | 1 | 11 | 11 |
| unobserved primitive | 4 | 11 | 11 |
| wrong worker | 2 | 11 | 11 |
| wrong worker | 4 | 11 | 11 |
| absent wake | 4 | 11 | 11 |
| fixed loop (`none`) | 4 | 0 | 11 |

6 rows per backend, 12 cells. A variant delays single posts as wakeprobe's `delays_single_posts` says: absent wake and
unobserved primitive at any worker count, wrong worker from 2 workers. Those cells take decision 15's 11 calibration posts
(the campaign's default for a delaying S cell); the fixed loop takes none. Every cell also takes the idle calibration of
R2 and R3 (`--calibrate-idle 11`), the second B_eff line below.

Not run, a stated limit (option H): B = 10 ms, the other worker counts of each variant, plain wake, and every other
variant.

## Design

As R3 (`quick-plan.md`) except where this list says otherwise. One wakeprobe process per row, poster on CPU 2, 5 warm-up
posts, trials of 21 posts, `--gap-frac-range 0.1:3.0 --gap-unit timer` with one gap seed per round shared by every cell of
the round. Workers sit on the campaign's CPUs (`plans/plan-windows.jsonl` at 796b71f): W = 1 on 14, W = 2 on 12 and 14,
W = 4 on 8, 10, 12 and 14. With W > 1 the probe and the calibration posts go to worker 0 and the warm-up and measured posts
go round robin over the workers (wakeprobe, unchanged). Seeds are new: the first 8 hex digits of sha256 of
`P1 quick gaps quick-W-r4 round <r>` and `P1 quick order quick-W-r4`, checked against the frozen seeds.

| Plan | System | Run | Conditions | Rounds | Trials per process | Processes per round |
|---|---|---|---|---|---|---|
| quick-W-r4 | Windows | R4 | quiet | 10 | 5 | 12 |

Rounds: 10, so that each cell has 10 processes and decision 15's B_eff rule (8 of 10, 10%) applies as written, and the
interquartile range is defined. R3 had 6 rounds, which left its count threshold unspecified (an open point of R3). With
the plan's arithmetic, 10 rounds give 5 trials per process (105 measured posts) and 50 trials per cell; 6 rounds would give
9 trials per process and 54 per cell. The trial count per cell is about the same, and 10 processes give a spread over more
independent processes.

Order: every round runs every cell once, in a new random order per round from the plan's seed. The plan is fully expanded
in `bench/quick/plans/quick-W-r4.jsonl`; its sha256 pins rows, order and arguments, and the runner never reshuffles.

## What is computed (descriptive, rule 4)

The analysis is a later step on the Release archive. Its rules are fixed here, before any data.

- **Trial flags.** A trial is 21 consecutive measured posts of one process. T_med flags it when the median of its 21
  delays exceeds B_eff / 5; T_count when one delay exceeds B_eff / 2 (`hypotheses-v2.md` section 2). Per process: the share
  of its trials that each statistic flags and the median delay of its measured posts. Per cell: the median over its
  processes and the spread as minimum and maximum, and the interquartile range (at least 5 processes). No test, no Holm
  step, no family. For the delaying cells the T_med share is the detection rate the question asks about; for the fixed loop
  it is the false-positive rate.
- **B_eff, first line.** Decision 15 takes B_eff from the absent-wake, W = 1, S cell of the backend, system and bound. R4
  has no such cell. Its substitute is the unobserved-primitive, W = 1 cell of the same backend: R4's only W = 1 delaying
  cell, where nothing wakes the worker either, so its calibration posts measure the same period of one worker's timed
  returns. B_eff is the median of that cell's `b_eff_ns`. Fallback: if fewer than 8 of its 10 processes calibrate, or their
  values span more than 10% of their median, the larger of B and `coarsest_timer_us`. If the runner cuts rounds, the count
  threshold is the smallest whole number at or above 0.8 times the processes that ran. The source is reported per backend.
- **B_eff, second line.** The median of the fixed-loop, W = 4 cell's `b_eff_idle_ns` on the same backend, as R3's second
  line. Both lines flag every cell.
- **Beside them.** Each delaying cell's own median `b_eff_ns` and median `b_eff_idle_ns`, so a worker-count effect on the
  period shows. Each process's `gap_unit_us` beside its calibrated B_eff; a process whose two values differ by more than
  10% is listed (another process may have changed the timer resolution).
- **Phase coverage.** For each delaying cell, the share of its measured posts whose phase mod B_eff falls in each tenth of
  B_eff (R3's reduction), with the share whose phase exceeds one period beside it. With W > 1 the phase is wakeprobe's,
  measured against the worker the post went to (for wrong worker, also the latest misdirected wake of that worker).
- **Lost wakes.** A unit of the fixed loop not delivered within wakeprobe's hard limit is reported as a lost wake.
- R3's results are not pooled with R4's. Where a cell matches an R3 cell in variant, worker count, backend and bound
  (none does), it would be listed beside, not merged.

## Code (branch `quick-benchmarks`)

No C++ change: wakeprobe already takes `--workers`, `--worker-cpus`, `--gap-unit timer` and `--calibrate-idle` for every
variant, and the loop stays the campaign's (the runner's guard against 796b71f is unchanged).

- `bench/quick/quick_common.py`: plan `quick-W-r4` (run R4), its 12 cells (`R4_CELLS`), the campaign's worker placement per
  worker count, cell ids with the worker count (`R4-<backend>-<variant>-W<w>-S-B1ms-quiet`), and the delaying rule as
  wakeprobe's (`delays_single_posts`), which sets the calibration posts and the time estimate. The plans of R1 to R3 are
  byte for byte as before (their sha256 values in `quick-plan.md` and STATE.md still match).
- `bench/quick/quick_requests.py`: R4's request text and a `--plan` switch that writes one plan's files only. The
  requests of R1 to R3 come out as committed.
- `bench/quick/test_quick.py`: R4's cells, flags and placement, and the delaying rule against wakeprobe's.
- `bench/CMakeLists.txt`: `--check` cases on every backend for unobserved primitive at W = 4 and wrong worker at W = 2 and
  W = 4 with calibration posts, and the fixed loop at W = 4, each with the idle calibration and the timer gap unit at
  B = 1 ms; `--validate-args` of R4's W = 4 argument sets with the laptop's placement. Nothing prints a time.

The runner is `quick_run.py` as for R3, unchanged: plan sha256, HEAD, guarded paths, system and /O2 checked before
anything is timed (exit 2 otherwise), the cut before a round that would pass 600 s, exit 3 when a process failed.

## Deviations from the frozen design (the paper states each)

1. Processes hold 5 trials instead of 30; cells have 10 processes, as the campaign.
2. B = 1 ms and Windows only; the 12 cells of option H, not the full worker grid.
3. The quick runner replaces `run_campaign.py`: no D5 sanitizer-record gate (rule 2) and no rerun of a failed process.
4. New seeds; the order is shuffled per round.
5. `--gap-unit timer`, as R3: the larger of B and the coarsest timer resolution.
6. The B_eff source is the unobserved-primitive, W = 1 cell instead of the absent-wake, W = 1 cell (above).
7. Reporting is median and spread, no test (rule 4); the frozen families are untouched.

## Arithmetic

R3's basis, unchanged (`quick-plan.md`, "Arithmetic"): timer-unit gaps have no measured basis, so a post costs
1.55 x 15.6 ms + 0.8 ms, plus 7.8 ms (B_eff / 2) for a delaying variant; per process 0.13 s, the probe and 5 warm-up posts
at the per-post rate, 11 calibration posts of 0.1 B + B_eff for a delaying cell, and 12 periods of 15.6 ms for the idle
calibration; 5 s for the block. Trials per process are the most for which rounds x round seconds x 1.10 <= 600
(`quick_common.trials_for`, asserted by `test_quick.py`). The worker count does not enter: one poster sends one post at a
time whatever the number of workers.

| Plan | Delaying process s | Fixed-loop process s | Round s | Rounds | Total s | With 1.10 |
|---|---|---|---|---|---|---|
| quick-W-r4 | 4.13 | 3.09 | 52.5 | 10 | 524.6 | 577.1 |

Derived, not measured. The 12 cells fit 10 minutes by this arithmetic. For comparison only, the same arithmetic gave R3
527.7 s and R3 ran in 480 s (STATE.md); the 480 s estimate for R4 in d013 is a linear scaling of R3 and is not used here.
If the real rounds run longer, the runner's cut keeps the campaign inside 600 s and records the rounds that ran.

## Alternatives considered

- **B_eff source.** Chosen: the unobserved-primitive, W = 1 cell (W = 1 as decision 15). Others: the absent-wake, W = 4
  cell (decision 15's variant, but a different worker count); R3's absent-wake, W = 1 value at 1 ms (another campaign);
  each cell's own calibration (no common threshold per backend). The first is the only option that keeps decision 15's
  W = 1 inside this campaign; the absent-wake, W = 4 value is reported beside it as that cell's own `b_eff_ns`.
- **Rounds.** Chosen: 10 (decision 15's 8 of 10 as written). Other: 6, as R3 (more trials per process, fewer processes).

## Open points

- The analysis script is not written for R4: `quick_analyse.py` sends every run other than R1 and R2 to its R3 branch,
  which does not know R4's cells or its B_eff source. An R4 branch is added after the run, to the rules above.
- The real round times are unknown until the run; the cut handles an overrun.

# Revision log, P1 v2

The frozen files of P1 v2 are never edited. Every later change to what they say, and every value
their frozen rules produce, is an entry here, appended with its date and the reason, and never
rewritten. An entry that changes a rule names whether any confirmatory data existed when it was
made.

## Frozen files (2026-10-27)

Frozen on 2026-10-27 with Alex's yes (decisions 11 to 21 in P1's STATE.md), before any
confirmatory window. The sha256 is of each file as stored in git (LF line endings); check it with
`git show <commit>:<file> | sha256sum`.

| File | sha256 |
|---|---|
| `hypotheses-v2.md` | b82d845d24188041fa0a52ea71363ce7476a38dff83cd50f9fbeeff0e84f8a88 |
| `design/v2-defects.md` | 5602b20c8438b5599dc8f7d9d35bf46f79193d1b2008dac4489f254fb3535419 |

## Entries

Expected entries, from the frozen rules: each H4 cell's R from the A/A pilot
(`hypotheses-v2.md` section 7), the commit of the generated run plan (section 8),
ANALYSIS_COMMIT, and any change of the testbed (section 1).

### 2026-10-27: ANALYSIS_COMMIT ad39c9c4165860dec3edbe8b93a6157cd4031e7f

The analysis code (`analysis/v2/`), the plan generator and the campaign runner
(`bench/run_campaign.py`) are fixed at this commit. No pilot or confirmatory data existed. The
code follows the frozen text; where that text leaves a choice open, the code fixes it as below.

**Plans and the runner**
- Each H4 cell's R is read from one h4-r block appended to this log (section 8). `pilot.py`
  writes the block. `plan.py` and `score.py` refuse a missing, second or malformed block, and one
  whose R differs from section 7's rule applied to its s. That rule can only give R of 14, 18, 20
  or 24, so 16 and 22 are refused.
- A real plan needs a clean tree. Its header records the commit and this log's sha256 (CRLF read
  as LF). Stand-in and draft plans run only as dry runs.
- Each plan line carries `limit_s`, a chosen bound and not an estimate. Past it the runner kills
  the tool and counts a failed run. wakeprobe: units x (k x 10 s + 1 s) + 120 s, where units are
  the probe, calibration, warm-up and measured units, and k is 1 under S and the sweep, 2 under Bt
  (the 10 s waits one unit can make). abpair: windows x (window + 10 s) + 120 s.
- abpair fails a window whose start-up, drain or worker stop exceeds 10 s: section 8's "a window
  that does not finish". It also fails a pinned thread that ends on another CPU.
- A rerun follows its failed run at once, in the same place in the order. Any non-zero exit or a
  timeout triggers it.
- A run the runner is killed in is void. It is set aside and reported, its data are never used,
  and it does not use up an attempt. A campaign may stop on a time budget and resume in plan order.
- The runner runs on CPU 0. Each tool starts with the full CPU mask and pins its own threads.
- The canonical order (S and Bt, then the sweep, then H4) is shuffled by Fisher-Yates over
  SplitMix64 with seed 20261026. The pilot plan uses the same seed and rule.

**Good processes**
- A process is good only if the runner's `.done` exists, the run whose data are used exited 0,
  and its summary matches the plan and section 1: a Release build, not dirty, one bench commit
  across both systems; on Linux, timer slack set to 1 ns without error; on Windows, no timer
  period requested and every timer-resolution reading (start, gaps, end) equal to the coarsest
  resolution. A finer reading means some process asked for more, so the run is not at the default.
- Lost units of failed first runs and of void runs are reported with the lost wakes of fixed and
  plain cells. The larger of the records' count and the summary's count is used.

**B_eff and thresholds**
- The median of an even count is the mean of the two middle values. A process calibrates when it
  is good and its `b_eff_ns` is above 0. A span of exactly 10% passes.
- Windows fallback: the largest `coarsest_timer_us` over every wakeprobe summary of the system.
  If none reports one, scoring stops.
- Timed-out units: measured units only, with a delay strictly above 5 x max(B, the cell's B_eff,
  the cell's coarsest timer). The Bt gate's own wait is not counted. The sweep and H4 have none.

**Tests and intervals**
- A trial is a block of 21 consecutive measured units in index order.
- Holm rejects at p at or below its threshold.
- Bootstrap: nearest-rank 2.5% and 97.5% points, SplitMix64 seeded 20261026 afresh for each cell.
- Overdispersion: the index is chi-square over (k - 1) with the pooled p. Its null is simulated
  10,000 times (the frozen text gives no count) as Binomial(n_i, pooled p), with the nearest-rank
  99th percentile; an undefined simulated index counts as 0. T_count gets a clustered interval on
  every cell and its overdispersion check on F2 cells only.
- Clopper-Pearson limits are found by bisection.
- H3: the median is over the posts that have a residual. A criterion is incomplete unless all 30
  sweep processes are good.
- H4: throughput is a window's tasks over its `elapsed_ns`. A cell with at least 14 pairs is
  tested on the pairs it has (section 8). s is the sample standard deviation. A pilot cell with
  fewer than 2 pairs gets R = 24 and is underpowered. At N = 0 the median's side is reported, not
  tested.

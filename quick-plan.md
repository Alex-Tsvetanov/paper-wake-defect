# Quick plan for P1's runs R1 to R3

Frozen at the commit that adds this file on branch `quick-benchmarks` (2026-11-07). Later changes go into a new file, never
into this one (CLAUDE.md rule 7).

This file adds three descriptive runs. It edits no frozen file: `hypotheses.md`, `hypotheses-v2.md`, `hypotheses-v2-h6.md`,
`design/`, `plans/`, `analysis/`, `results/`, `bench/run_campaign.py` and the D5 records stay as they are. The frozen
hypotheses H1 to H6, their families, tests and Holm steps are not reopened. Nothing here enters a frozen family; every result
of these runs is reported beside the confirmatory results, as median and spread (rule 4).

Authority: the three-model vote `decisions/d010-p1-round1-scope.md` (Papers repository), option B, which stands under
CLAUDE.md rule 13 because Claude, Grok and Gemini all ranked it first. The runs are the needs-run lines R1 to R3 of
`p1-wake-defect/writing/fixes-r2.md`, repeated in `fixes-r7.md` and `fixes-r8.md`. Rule 5 sets the length: each campaign takes
at most 10 minutes. Rule 1: Release builds only, -O3 with Clang on Linux and /O2 with MSVC on Windows. Rule 3: the laptop
only, one campaign and one OS at a time.

## What each run answers

- **R1, false positives under conditions the campaign did not have** (Grok Maj6 and Question 5 of round 1, Gemini 6, Grok
  Maj3 and Maj4 of later rounds). The fixed loop (`none`) and the 2026-09 loop (`plain_wake`), protocol S (single posts) and
  protocol Bt (batch rounds), B = 1 and 10 ms, every backend of the system, under three conditions in the same campaign:
  `quiet` (as the campaign ran), `load` (processor load) and `power` (the host's own power settings instead of the lab's).
- **R2, the effective bound without a planted defect** (Gemini 1 of every round, Grok Maj1, fix 1). B_eff measured on the
  fixed loop, with no absent-wake variant, compared with the value from the absent-wake variant's calibration posts on the
  same backend, system and bound. Both systems are planned; Linux may run first, as the fix list asks.
- **R3, Windows single posts with gaps over the whole period** (Gemini 4 and 5, Grok Maj4, fix 2). On Windows both bounds
  give about one timer period, and the campaign's gaps (0.1 to 3.0 B) reach only the first part of it at B = 1 ms. R3 draws
  the gaps in units of the timer period instead, for the absent-wake variant and both correct loops, at both bounds.

## Not run, and why

- **One virtual machine (part of R1).** No hypervisor is recorded on the laptop: `lab/laptop/versions.md`, `setup-log.md`
  and the Windows startup inventories name no WSL, Hyper-V, QEMU, KVM, VirtualBox or VMware. Setting one up is an install and
  a system change, not a design choice of this plan. It stays a stated limit until a VM exists. Options for later: WSL2 on
  Windows (a Hyper-V utility VM) running the Linux build, or QEMU with KVM on Arch.
- Everything d010 left as a limit (items 28, 30, 33 and 15 of `fixes-r1.md`).

## Design

Common to every row: one wakeprobe process, W = 1, worker on CPU 14, poster on CPU 2 (the campaign's placement), 5 warm-up
units, trials of 21 posts or rounds, `--gap-frac-range 0.1:3.0` with one gap seed per round shared by every cell of that
round (as the frozen processes share theirs). Seeds are new: the first 8 hex digits of sha256 of a label
(`P1 quick gaps <plan> round <r>`, `P1 quick order <plan>`), checked against the frozen seeds (20260926, 20261026, 20261027,
2026102700 to 2026102709).

Order: every round runs every cell once. A round shuffles its condition blocks, and each block shuffles its rows, with a
new draw per round from the plan's seed. The plans are fully expanded in `bench/quick/plans/*.jsonl`, so the sha256 of a
plan pins its rows, order and arguments; the runner never reshuffles.

| Plan | System | Run | Conditions | Rounds | Trials per process | Processes per round |
|---|---|---|---|---|---|---|
| quick-L-r1 | Linux | R1 | quiet, load, power | 3 | 10 | 92 |
| quick-W-r1 | Windows | R1 | quiet, load, power | 3 | 13 | 62 |
| quick-L-r2 | Linux | R2 | quiet | 10 | 17 | 12 |
| quick-W-r2 | Windows | R2 | quiet | 10 | 18 | 8 |
| quick-W-r3 | Windows | R3 | quiet | 6 | 11 | 12 |

Rows per round:

- **R1**, per condition, per backend (Linux epoll, io_uring, condvar; Windows IOCP, condvar) and bound: `none` and
  `plain_wake`, each under S and Bt; one calibration row of `absent_wake` (`--calibrate 11 --posts 1 --warmup 0 --gap-us 0`)
  that gives that condition's B_eff; and in the quiet block on the first backend, the A/A pair: `none` under S again, arm
  `none-aa`, the same binary and arguments in a second process.
- **R2**, per backend and bound: `none` with `--calibrate 0 --calibrate-idle 11`, and `absent_wake` with
  `--calibrate 11 --calibrate-idle 11`, both under S.
- **R3**, per backend and bound: `absent_wake` (`--calibrate 11 --calibrate-idle 11`), `none` and `plain_wake`
  (`--calibrate-idle 11`), under S with `--gap-unit timer`.

### The conditions of R1

- `quiet`: nothing added, as the campaign.
- `load`: one spinning process pinned to each CPU outside core 0, the poster's CPU and the worker's CPU: CPUs 3 to 13 and 15
  (12 processes, `bench/quick/quick_hog.py`). The siblings of the poster (3) and the worker (15) are loaded, so they share
  their cores; nothing runs on CPUs 2 and 14 themselves. The runner starts the hogs after the lab's job-start quiet check,
  waits until each has pinned itself, waits 2 s, runs the block, and stops them in a `finally`. Each hog also ends by itself
  after 900 s.
- `power`: the host's own power settings, read from the lab's records of the job, for the block's length, and the lab's
  settings back after it (`bench/quick/quick_power.py`). Arch: per CPU the governor and boost that `labpower.sh` recorded
  before it changed them (`LAB_POWER_RECORD`, "before") and the `scaling_min_freq` that `clockfloor.sh` recorded before it
  raised the floor (`IOCORE_CLOCK_RECORD`, "before"), written with `sudo -n tee`, as `labpower.sh` writes them; on
  2026-10-22 the host's own values were governor powersave and boost on (`setup-log.md`). Windows: the plan that `labjob.py`
  saved in `C:/lab/power-restore.json` before it made the lab plan active (Alex's Balanced plan, 381b4222, in the README),
  made active with `powercfg /setactive`. Every value is read back; a mismatch stops the block and sets every value back. The
  lab's own teardown sets the host back again at the job's end, so a runner that dies mid-block changes nothing that lasts.
  Before anything is timed the runner checks that the records parse and that a write works (it writes cpu0's governor with its
  own value), and refuses with exit 2 otherwise.

## What is computed (descriptive, rule 4)

The analysis is a later step on the Release archives (results-analyst). Its rules are fixed here, before any data.

A trial is 21 consecutive measured posts (rounds under Bt) of one process. T_med flags a trial when the median of its 21
delays exceeds B_eff / 5; T_count flags it when one delay exceeds B_eff / 2 (both as in `hypotheses-v2.md` section 2). Per
process: the share of its trials that T_med flags, the same for T_count, and the median delay of its measured posts. Per
cell: the median over its processes (one per round) and the spread as the minimum and maximum, and the interquartile range
where there are at least 5 processes. No test, no Holm step, no family.

- **R1.** B_eff per condition, backend and bound: the median of the `b_eff_ns` of that condition's three calibration rows.
  Fallback, as in decision 15: if fewer than 2 of the 3 calibrate or their values span more than 10% of their median, B on
  Linux and on Windows the larger of B and `coarsest_timer_us`; the source is reported per cell. A second line flags the
  same trials with the campaign's B_eff (`results/v2/score.json`). Per round, the difference load minus quiet and power
  minus quiet of each cell's flag share and median delay, median and spread over the rounds. The A/A run: per round the
  difference `none-aa` minus `none` in the same two values, median and spread, reported once per system and never used to
  tune anything. A unit lost by a correct loop (not delivered by wakeprobe's 10 s hard limit) is reported as a lost wake.
- **R2.** Per backend, system and bound: B_eff from the fixed loop, the median over the 10 processes of the `none` cell's
  `b_eff_idle_ns`; B_eff from the planted variant, the median over the 10 processes of the `absent_wake` cell's `b_eff_ns`
  (decision 15's source and fallback); their ratio; the within-process ratio `b_eff_idle_ns / b_eff_ns` of the absent-wake
  processes, median and spread over the 10; and the campaign's value beside them. T_med and T_count shares of both cells with
  each of the two thresholds.
- **R3.** Per backend, bound and variant: the T_med and T_count shares and the median delay, with B_eff from the absent-wake
  cell's 6 `b_eff_ns` (decision 15's rule) and, as a second line, from the `none` cell's `b_eff_idle_ns`. The phase coverage:
  the share of measured posts of the absent-wake cell whose phase falls in each tenth of B_eff. Each process's `gap_unit_us`
  beside its calibrated B_eff; a process whose two values differ by more than 10% is listed (another process may have
  changed the timer resolution). The sensitivity line of request 8 (the power family without the Windows cells at the
  smaller bound) uses the campaign's data, not R3's, and is not part of this plan.

## Code (branch `quick-benchmarks`, from `v2` 6651f4e)

- `bench/wakeprobe.cpp`: one new flag, `--calibrate-idle N` (default 0, off). After the probe, the poster watches worker 0's
  pass counter (`Loop::passes`, which the loop already had) and takes the times of N + 1 consecutive increments. With nothing
  queued every pass is a timed return, so the N intervals are periods of the worker's timed returns: decision 15's
  definition of B_eff, measured with no post and no planted defect. The intervals go through the same filter and percentiles
  as the calibration posts (the calibration code is split into `summarize_intervals`, its behaviour unchanged). The summary
  gains `calibrate_idle`, `b_eff_idle_ns`, `idle_intervals`, `idle_dropped`, `idle_p10_ns` and `idle_p90_ns`, only when the flag
  is given. Nothing else uses the value: the timeout, phase and prediction still use the calibration posts' B_eff. Without
  the flag every output line is as before, field for field.
- The loop is untouched. The runner refuses unless `git diff` of `loop`, `tests`, `CMakeLists.txt`, `bench/abpair.cpp`,
  `bench/bench_common.hpp`, `bench/probe_platform.hpp` and `bench/code_paths.txt` against the campaign's code commit
  796b71f is empty. Since `bench/wakeprobe.cpp` is a code path, `bench_code_commit` in these summaries differs from the
  campaign's.
- `bench/CMakeLists.txt`: ctest cases for the new flag (`--check` with the idle calibration on every backend, on the fixed
  loop, beside the calibration posts and with the timer gap unit; `--validate-args` of the R2 and R3 argument sets; three
  refusals), and `quick_unittest`, which runs `bench/quick/test_quick.py` with `--validate-args` on every row of the system's
  plans. Nothing in them prints a time.
- `bench/quick/`: `quick_common.py` (plans, arithmetic, seeds), `quick_plans.py` (writes or checks the plans),
  `quick_run.py` (the runner), `quick_hog.py` (the load), `quick_power.py` (the power condition), `quick_requests.py` (the
  laptop requests), `test_quick.py`, and `plans/`.

The runner (`quick_run.py --plan NAME --quick SHA256 --commit SHA --build-dir DIR --out-dir DIR`) refuses with exit 2,
before anything is timed, unless the plan's sha256 (CRLF read as LF) matches, HEAD is the job's commit, the guarded paths
match, the plan's system is this one, `compile_commands.json` shows `wakeprobe.cpp` at -O3 (Linux) or /O2 (Windows) and no
other level, and the power check passes where the plan has a power block. Then it runs the rows in order. Before each round
after the first it stops when the time used plus the mean round so far would pass 600 s, and records the realized rounds. A
failed process is recorded with its exit code and not rerun. It writes `units/<id>.jsonl` (wakeprobe's records),
`units/<id>.out`, `job.json` (conditions, readbacks, exit codes, wall seconds) and `validate.txt`. Exit 0 when every process
exited 0, 3 when one did not (its data is kept).

## Deviations from the frozen design (the paper states each)

1. Processes hold 10 to 18 trials instead of 30, and R1 and R3 cells have 3 and 6 processes instead of 10 (R2 keeps 10).
2. W = 1 only.
3. The quick runner replaces `run_campaign.py`: no D5 sanitizer-record gate (rule 2 replaces it for new campaigns) and no
   rerun of a failed process.
4. New seeds; the order is shuffled per round with condition blocks.
5. wakeprobe has `--calibrate-idle`; the loop is the campaign's.
6. R1's calibration rows are absent-wake processes with one measured post and no warm-up; their single delay is not used.
7. R3's gap unit is `--gap-unit timer`, the larger of B and the coarsest timer resolution, wakeprobe's existing option
   that spans the same share of the wake period at both bounds. It equals B_eff when the default timer period holds, which
   R3 checks per process (above). A unit taken from each process's own calibration would need new code.
8. Load and power are conditions the campaign did not have; the VM is not run.
9. Reporting is median and spread, no test (rule 4); the frozen families are untouched.

## Arithmetic

Seconds per trial, from measured bases only: protocol S from the laptop's 100-trial cells (`hypotheses-v2.md` section 9:
Linux fixed 3.3 and 33.7 s, delaying 4.7 and 44.3 s at B = 1 and 10 ms; Windows 5 and 34 s, delaying 34.5 and 51 s);
protocol Bt from the fixed loop's 211-round probe at 5e0fc7e (Linux 0.4 and 3.4 s, Windows 1 and 4 s). R3's timer-unit gaps
have no measured basis: 21 x (1.55 x 15.6 ms + 0.8 ms) per trial, plus 7.8 ms per post for absent wake (15.6 ms is the
default period, 0.8 ms the measured Windows gap overrun, both from `hypotheses-v2.md`). Per process add 0.13 s (50 ms settle
and a process start; the A/A pilot's abpair processes ran at most 0.081 s over their windows), the probe and warm-ups at the
per-post rate, 11 calibration posts of 0.1 B + B_eff, and 12 periods for the idle calibration (B_eff = B on Linux, the larger
of B and 15.6 ms on Windows). Per condition block add 5 s for the switch, with no measured basis. Trials per process are the
most for which rounds x round seconds x 1.10 <= 600 (`quick_common.trials_for`, asserted by `test_quick.py`).

| Plan | Round s | Rounds | Total s | With 1.10 |
|---|---|---|---|---|
| quick-L-r1 | 172.5 | 3 | 517.4 | 569.1 |
| quick-W-r1 | 174.6 | 3 | 523.7 | 576.1 |
| quick-L-r2 | 52.3 | 10 | 523.5 | 575.8 |
| quick-W-r2 | 53.8 | 10 | 537.8 | 591.6 |
| quick-W-r3 | 88.0 | 6 | 527.7 | 580.5 |

Laptop time per system, builds not counted: Linux about 1,041 s (two campaigns), Windows about 1,589 s (three campaigns).
Under load a round can take longer than the estimate; the runner's cut keeps each campaign inside 600 s and records it.

## Alternatives considered

- **Load.** Chosen: pinned spinners on every CPU but core 0, the poster's and the worker's. Others: spinners on CPUs 2 and
  14 as well (the poster and worker preempted directly, the harshest case); unpinned spinners, one per logical CPU (the
  scheduler places them, so runs differ); memory or I/O load (needs a tool the laptop does not have, such as stress-ng). The
  chosen load is the simplest that loads the machine without taking the measured threads' own CPUs.
- **Power.** Chosen: the host's own settings from the lab's records, set and restored by the runner. Others: a labrun
  switch for the power plan (a change under `lab/`, outside this branch); Windows only (leaves Linux's governor unanswered).
- **R2 mechanism.** Chosen: the idle pass counter, no loop change. Others: a post that skips its wake (a new loop API on the
  measured loop's post path); a hook in each backend's timed return (a loop change); reading it from the fixed loop's delays
  (impossible, they are near zero). The pass counter is one counter of wait returns, which any loop can keep without
  planting a defect.
- **R3 unit.** Chosen: `--gap-unit timer`. Other: a new unit equal to each process's calibrated B_eff (more code; it would
  follow a changed timer resolution, which R3 instead detects and lists).

## Open points

- The real round times, under load in particular, are unknown until the first run; the cut handles an overrun.
- The analysis script is not written; this file fixes what it computes.
- The scripts were run on the desktop only in their dry and fake modes; the hog, the power switch and the timed rows need
  the laptop.

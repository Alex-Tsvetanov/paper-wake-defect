# Hypotheses v2 (DRAFT 4, not frozen)

Status: **draft 4**, written 2026-10-27 on the desktop after three freeze checks (of drafts 1, 2
and 3). Not frozen. Alex accepted draft 3 on 2026-10-27; the changes since are listed in section
10 for his confirmation, after which the file is frozen, before the first confirmatory window.
`hypotheses.md` (the 2026-09 study) is frozen and is not edited; this file is new. Design:
`design/v2-defects.md`. Values marked **(proposed)** are those Alex accepted.

## 1. Setup

One or more workers (W) park in their backend's wait with bound B; one poster posts tasks; the
measured quantity is a task's delay from post to run. Backends: epoll and io_uring on Linux,
IOCP on Windows, a condition variable on both; Linux and Windows on the same laptop (AMD Ryzen 7
5800H). Variants (design note, section 4): the fixed loop with the park handshake; plain wake
(the 2026-09 loop, a reference arm, not a defect); absent wake; unobserved primitive; lazy-wake
race (natural window); lazy-wake race, widened (section 6); wrong worker (W >= 2); no re-trigger
(epoll, io_uring); undrained edge. Every build is the Release build of one commit, which holds
every variant (selected at run time).

Settings for H1 to H4: Linux keeps timer slack 1 ns; Windows keeps the default timer
resolution. H5 alone also runs the 2026-09 settings (section 3).

**The 2026-09 study, read before this file was written.** Its outcomes (`paper/main.tex`, table
of hypotheses, and its exploratory runs):
- The delay model held on Linux and on Windows with the default timer, and failed on Windows
  with the 1 ms timer at B = 1 ms.
- The bound-independence check failed on Linux and held on Windows.
- The 21-post test flagged every defective and no correct configuration at a gap of 1.7 B, but
  in the fixed-gap sweep it missed in 41 of 240 runs, 37 of them with phases late in the period.
- The exploratory PM QoS check failed: under a CPU latency request the correct loop's delay on
  Linux still depended on the bound.
- An exploratory random-gap run on this laptop's Linux (epoll and io_uring, B = 1 and 10 ms, W =
  1, 21-post trials, gaps from [0.1 B, 3.0 B], threshold B_eff / 5, 1,000 trials per cell)
  missed 4 of 4,000 defective trials and flagged 0 of 4,000 correct ones. Resampling each gap
  sweep into 100,000 random draws of 21 posts gave a largest miss rate of 0.08%.
- Its Linux data came from this laptop and its Windows data from another machine, built there
  with clang-cl.

**Testbed.** The laptop (ASUS ROG Strix G513QM, BIOS G513QM.331, AMD Ryzen 7 5800H, 8 cores and
16 logical CPUs, core k holding CPUs 2k and 2k + 1) dual-boots Arch Linux (kernel 7.2.6-arch2-1,
clang 22.1.8) and Windows 11 Pro (build 26200.6584, MSVC cl 19.51.36248), as read on 2026-10-22
(`lab/laptop/versions.md`). Every run is a job of the lab runner (`lab/bin/labrun.py`): it needs
AC power, passes the job-start quiet check, samples power, clock and temperature on CPUs 0 and 1,
and archives every output with its sha256. On Windows, Defender's real-time protection and
Windows Update are off (read back on 2026-10-27). Any change of these before the campaign goes
into the revision log.

How they informed v2: H1 to H4 run Windows only at the default resolution, where the delay model
held, and the failing condition (the 1 ms timer) moves to H5; B = 100 ms, the blocking bound and
Linux's default timer slack move to H5 as well, so the hours go to backends, workers and
variants; the random-gap design becomes protocol S, because a fixed gap of 1.7 B holds every
post at one phase; the thresholds use B_eff, as in 2026-09.

**Cells whose outcome was seen: disclosed replications (Alex, decision 11).** Two kinds, both
kept in their families and also reported on their own, with what was seen as their stated
expectation, on new code (the handshake) and new gap seeds (section 8):
- **R, results seen.** The 2026-09 random-gap run is protocol S at W = 1 on Linux; its defect
  arms are v2's absent wake on epoll and unobserved primitive on io_uring, its correct arm v2's
  plain wake. That is 8 cells at B = 1 and 10 ms: 4 in F1 and 4 in F2.
- **Rd, seen through durations only (proposed, to confirm).** Request 1 (2026-10-23 and 24) ran
  protocol S at W = 1 with plain wake and absent wake on every backend at B = 1 and 10 ms, and
  its durations were read: absent wake took longer in every cell, and plain wake took about its
  gaps. Request 7 ran the fixed loop under protocol Bt and its durations were read. That marks 8
  more F1 cells (absent wake on io_uring, condvar on Linux, IOCP and condvar on Windows), 6 more
  F2 cells (plain wake under S on those backends) and the 10 fixed-loop Bt cells of F2.

Together 12 of F1's 102 cells and 20 of F2's 80 are replications. H3's criteria were also seen
in part (section 3).

## 2. Protocols, B_eff and test statistics

- **Single posts (S).** One trial is 21 posts. Before each post the poster sleeps a gap drawn
  uniformly from [0.1 B, 3.0 B] (seeded, section 8), after it sees the previous delivery; posts
  go round robin over the workers. wakeprobe `--protocol single --gap-frac-range 0.1:3.0` with
  the default `--gap-unit bound`.
- **Batch (Bt), W = 1.** One trial is 21 rounds. A round posts a gate task that holds the
  worker, then 3 x 64 = 192 tasks to that worker, then releases the gate; the round's delay is
  the latest run time of the 192 tasks minus the release time. Before each round the poster
  sleeps a gap drawn as in S, counted from the previous round's last run. wakeprobe
  `--protocol batch --gap-frac-range 0.1:3.0`.
- **Phase sweep (H3 only).** Absent wake, W = 1. Each of 30 processes posts at one phase target,
  0.05, 0.15, ..., 2.95 B_eff after each delivery: 21 measured posts, 3 warm-up posts, B_eff
  from the process's own 11 calibration posts. wakeprobe `--phase-frac`. This is the 2026-09
  Windows gap sweep; the 2026-09 Linux sweep used fixed gaps instead (B_eff = B there), so on
  Linux the design changes from fixed gaps to phase targets.
- **Phase.** The time from the worker's previous delivery to the post (wakeprobe `phase_ns`).
  Under absent wake at W = 1 every delivery happens at a timed return, so the phase is the time
  since the worker's last timed return.
- **B_eff (rule, decision 15).** The observed period of a worker's timed returns. For the
  thresholds of every S and Bt cell of a backend, system and B: the median of the calibrated
  B_eff (summary field `b_eff_ns`, from 11 calibration posts per process) over the 10 processes
  of the absent-wake, W = 1, S cell of that backend, system and B. Fallback, if fewer than 8 of
  the 10 processes calibrate or their values span more than 10% of their median: B on Linux, and
  on Windows the larger of B and the coarsest timer resolution (summary field
  `coarsest_timer_us`). Which source applied is reported per cell. H3 uses each process's own
  B_eff, as in 2026-09.
- **T_med**, the paper's test: a trial flags when the median of its 21 delays exceeds B_eff / 5.
- **T_count**, a tail statistic (exploratory): a trial flags when at least one of its 21 delays
  exceeds B_eff / 2. The analysis computes both from the per-unit delays; wakeprobe's summary
  field `count_over_half_b_nominal` uses the nominal B and is not used.
- **n = 300 trials per cell (proposed)**, run as 10 processes of 30 trials (section 8). Basis
  (D9, exact one-sided binomial tests, alpha 0.025, Holm): at the strictest Holm step (alpha/m =
  2.45e-4 in F1, 3.13e-4 in F2) a cell passes with up to 3 misses or false positives (p =
  1.64e-4) and fails with 4 (p = 6.9e-4); at the last step up to 7 pass. With n = 200 one miss
  gives p = 4.0e-4, so only zero would pass. Power at the strictest step: a cell whose true
  per-trial power is 0.995 passes with probability 0.93; 0.99 gives 0.65; 0.98 gives 0.15.

## 3. Hypotheses

**H1 (power).** For every cell of absent wake, unobserved primitive, wrong worker and no
re-trigger under protocol S, at B in {1, 10} ms and W in {1, 2, 4} (wrong worker 2 and 4), and
for every cell of undrained edge under protocol Bt (W = 1), T_med flags more than 95% of trials.
Test: exact one-sided binomial test of power <= 0.95 per cell; Holm over family F1. H1 holds if
every F1 cell rejects; otherwise the failing cells are named and H1 is reported as holding on
the remaining cells only. An incomplete cell (section 8) stays in m and counts as not
rejected. Twelve F1 cells are replications (section 1).

**H2 (false-positive rate).** For every cell of the fixed loop and of plain wake, under protocol
S (W in {1, 2, 4}) and protocol Bt (W = 1), T_med flags fewer than 5% of trials. Test: exact
one-sided binomial test of a false-positive rate >= 0.05 per cell; Holm over family F2. The
same rules as H1 for failing and incomplete cells. Twenty F2 cells are replications (section 1).

**H3 (the delay of a missed wake).** For absent wake at W = 1 under the phase sweep, at the
settings of section 1 (Windows at its default timer resolution), each post's delay equals
B_eff minus (its phase mod B_eff). Criterion, per backend, system and B in {1, 10} ms: over the
sweep's 630 measured posts, the median of |residual| / B_eff, each post divided by its own
process's B_eff, is below 0.05 (the 2026-09 H1 criterion, unchanged). Ten pre-specified
criteria, no test and no p-value. Seen before: on epoll and IOCP the criterion held in 2026-09
for the same variant (IOCP on another machine); on io_uring 2026-09 swept unobserved primitive,
and absent wake was seen only through request 1's durations, as on the condition variable.
The 1 ms Windows timer, where the 2026-09 criterion failed, is in H5, not here.

**H4 (the throughput cost of the fix).** With a busy worker (poll mode, W = 1, P = 1, K = 64),
for each backend and system and each per-task work N in {1, 10, 100} us, the per-pair
throughput ratio, fixed loop over absent wake, lies within [0.98, 1.02]. Decision rule in
section 7; family F4 (m = 15). H4 holds if every F4 cell rejects; otherwise the failing cells
and the cells declared underpowered by the pilot are named, and H4 is reported as holding on
the remaining cells only. A cost is claimed only through family F4c (section 7). The margins are the 2026-09 H4 criterion; "the fix" now means the
wake with the park handshake (D2). **N = 0 us is not confirmatory:** its ratios were seen before
the freeze, and they contradict H4 on three backends (section 12). It is run and reported
outside F4 as a disclosed repeat, with the seen direction as its prediction: below 0.98 on
epoll, io_uring and IOCP; within the margins on the condition variable on Linux; no prediction
for the condition variable on Windows (too noisy to read in the probe).

**H5 (same hardware, two systems; exploratory).** On this laptop, the 2026-09 delay-model fit
and bound-independence check are measured again with their frozen criteria and the 2026-09
designs (gap sweep, bound sweep, matrix) and all their factors: B in {1, 10, 100} ms and
blocking; on Windows every design at the timer period {default, 1 ms}; on Linux the timer
slack {1 ns, default} where 2026-09 varied it (the matrix at B = 1 ms). H5 is exempt from
section 1's settings. The 2026-09 runners (`bench/run_matrix.*`) build v1's compile-time defect
and do not run on v2; `bench/run_h5.sh` and `run_h5.ps1` run the same designs with the v2
binary: 140 cells on Linux (2026-09's 148 less its 8 PM QoS cells) and 136 on Windows. Backends: those of 2026-09 (epoll, io_uring, IOCP). Arms: the 2026-09
fixed loop is `plain_wake`; the 2026-09 defect is `absent_wake` on epoll and IOCP (2026-09
omitted the wake) and `unobserved_primitive` on io_uring (the 2026-09 misdirect: an eventfd
written, never watched). Differences from 2026-09: one binary holds both arms; v2's defect
variants sit on the park handshake, which changes nothing for a post that lands while the
worker is parked (every post of these designs); the worker and poster threads are pinned
(CPU 14 and CPU 2, section 8), where in 2026-09 they were placed by the system; and the Windows
build uses MSVC, where 2026-09 used clang-cl. Each run uses its own calibrated B_eff, as in
2026-09. Only the two criteria are scored; the matrix design's 21-post test and the 2026-09 PM
QoS experiment are not. Reading, three outcomes: (a) the 2026-09 split recurs on this one
machine (the delay model fails on Windows with the 1 ms timer at B = 1 ms; bound independence
fails on Linux and holds on Windows): it is attributed to the operating system and its timer
configuration, not to the hardware; (b) neither part of the split recurs: the split is
reported as not reproduced on one machine, with its cause left open among the machine, the
operating system build and the code; (c) any other pattern: reported as found, with no
attribution.

**H6 (the historical case): deferred.** The Netty case (`historical/candidates-2026-10.md`) is
not part of this freeze. Its design (bound, n, trial, the forced and the natural protocol) goes
into a dated addendum, frozen before any scoring run and before any reproduction that shows
CR7's behaviour.

## 4. Predicted blind spots (stated now; reported with intervals, not tested)

"Blind" means T_med flags at most 5% of trials, the H2 bound. Each rate is reported with its
exact 95% interval and its clustered interval. A prediction **fails** for a cell when the exact
one-sided 95% lower bound of its flag rate exceeds 0.05; failed predictions are reported as
such.
- T_med under protocol S is blind to the lazy-wake race (natural and widened) and to undrained
  edge: their single posts are delayed rarely or never.
- T_med under protocol Bt is blind to absent wake, unobserved primitive, no re-trigger and the
  lazy-wake race (natural and widened): the 192 tasks are posted behind a running gate and need
  no wake, and the gate itself, whose delay is not counted, is posted to a parked worker,
  outside the race window.
- No claim that T_med or T_count detects the lazy-wake race is drawn from the campaign. T_count
  is reported on every F2 cell (its false-positive rate) with both intervals. For each
  lazy-wake cell the rate of delays above B_eff / 2 per post (per round under Bt) is reported
  with an exact one-sided 95% upper bound (zero in 6,300 give 4.75e-4).

## 5. Families and Holm

| Family | Claims | m |
|---|---|---|
| F1 | H1: S delaying cells, Arch 30 per bound x 2 = 60, Windows 16 x 2 = 32; Bt undrained edge, Arch 6, Windows 4 | 102 |
| F2 | H2: S fixed and plain, Arch 36, Windows 24; Bt fixed and plain, Arch 12, Windows 8 | 80 |
| F4 | H4: 5 backend-system pairs x 3 work levels (1, 10, 100 us) | 15 |
| F4c | H4's cost claim, the same 15 cells (section 7; proposed, to confirm) | 15 |

alpha 0.025 one-sided per family **(proposed)**; in F4, per side of each test. An incomplete
cell stays in its family's m and counts as not rejected. Outside the
families: H3's ten criteria (no p-value), H4 at N = 0, plain wake against absent wake under H4's
grid (descriptive), the blind spots, T_count, and H5. The replication cells (section 1) are
reported both within their family and on their own.

Every rate also gets a clustered interval: a percentile bootstrap over the cell's 10 processes,
10,000 resamples, seed 20261026, next to the exact Clopper-Pearson interval. Overdispersion: for
each cell the variance of the per-process flag rate is compared with its binomial value; a cell
whose dispersion index exceeds the 99th percentile of the index under the binomial (simulated,
same seed) is flagged as overdispersed, and its clustered interval is then stated next to its
exact p-value as a limitation. When every trial or no trial of a cell flags, the dispersion
index is undefined and the cell is reported as not overdispersed; its clustered interval is a
single point and only the exact interval is stated.

## 6. The widened lazy-wake plant **(proposed)**

The natural window lies between two instructions. The widened plant puts one real operation in
it: between its look at the stack and its announcement the worker makes one call of its
backend's own wait with a zero timeout, as loops do that check for I/O between their queue check
and their park (the Netty case checks its queue, then selects). The call per backend: epoll_wait
with timeout 0; io_uring_enter with GETEVENTS and min_complete 0; GetQueuedCompletionStatus
with timeout 0; `std::condition_variable::wait_for` with zero and no predicate. The window is as
wide as that call is on that system; it is not claimed to enter the kernel on the condition
variable, whose library may return without a system call. It is fixed here without any
detection data, and it replaces "a stated delay" of Alex's decision 2 (section 10).

## 7. H4: decision rule, R and the A/A pilot

- **Decision (proposed).** Per cell, two one-sided exact sign tests on the R per-pair ratios:
  p_low tests "the median ratio is at most 0.98" with the number of pairs above 0.98, and p_high
  tests "the median ratio is at least 1.02" with the number below 1.02, each against
  Binomial(R, 1/2). The cell's p-value is max(p_low, p_high), and Holm over F4 at alpha 0.025
  runs on these. A cell shows **no cost** when it rejects. This is the exact, distribution-free
  form of the 2026-09 criterion (a 95% interval inside [0.98, 1.02]): it asks whether the exact
  interval of the median pair ratio, at the cell's Holm level, lies inside the margins. The
  estimand is the median pair ratio, where 2026-09 named no estimator (a departure, section 10).
- **Reading a non-rejection.** In a cell with enough power (below), a non-rejection reads
  "equivalence not shown", not "a cost". **Cost claim (family F4c; proposed, to confirm).** A
  cost is claimed for a cell only when the exact one-sided sign test of "the median ratio is at
  least 0.98" (the number of pairs below 0.98 against Binomial(R, 1/2)) rejects under Holm over
  the 15 cells at alpha 0.025. The 95% interval (percentile bootstrap of the mean per-pair
  ratio, clustered by process) is descriptive and decides nothing.
- **R >= 14 (proposed).** At m = 15 the strictest threshold is 0.025 / 15 = 1.67e-3. R = 14 is
  the smallest R at which one pair outside a margin still passes: P(X <= 1 | 14) = 15/16384 =
  9.2e-4. R = 13 gives 14/8192 = 1.71e-3, which fails.
- **Pilot rule (proposed), frozen before the pilot.** The A/A pilot runs the fixed loop against
  itself in every H4 cell (5 backend-system pairs x 4 work levels, N = 0 included): 8 pairs of
  10 s windows in 4
  processes of 2 pairs. Per cell, s is the standard deviation of the 8 per-pair log ratios.
  Power(R) is the probability that the decision rule rejects at the strictest Holm step when
  the true ratio is 1 and each pair's log ratio is normal with standard deviation s (computed
  exactly from the trinomial of pairs below, inside and above the margins). The cell's R is the
  smallest **even** R in 14 to 24 with power at least 0.8 (even, because a process holds 2
  pairs). If none reaches 0.8, R = 24 and the cell is declared underpowered before the
  campaign: its tests are still run and reported, and a non-rejection is read as inconclusive.
  For orientation (arithmetic, no data): s = 0.010 gives R = 14 (power 0.92); s = 0.0125 gives
  R = 18 (0.86). The pilot compares no arms; each cell's R goes into the revision log before
  the first H4 window.
- **Pairs.** Windows of 10 s, ABBA within a process, 2 pairs per process, R/2 processes per
  comparison, in the system's shuffled order (section 8). The descriptive comparison of plain
  wake against absent wake runs at the same R. Pairs in one process may be correlated; the
  clustered interval shows it.

## 8. Procedure and validity rules

- **Processes.** Each S and Bt cell runs as 10 wakeprobe processes of 30 trials (630 posts or
  rounds each), each with its own settle, probe post, calibration (delaying variants under S;
  Bt calibrates nothing) and 5 warm-up units. Process i (0 to 9) of every cell draws its gaps with seed 2026102700 + i, so
  the cells share their gap sequences; no seed of the 2026-09 study (20260926) is reused.
- **Order.** All processes of one system's confirmatory campaign (S, Bt, the sweep, H4) run in
  one order, shuffled with seed 20261026. The plan generator is part of ANALYSIS_COMMIT; it reads
  each H4 cell's R from the revision log, so the plan itself is generated after the pilot and
  committed before the first window. H5, exploratory, runs as its own block after the campaign,
  in its runner's order (`bench/run_h5.*`, shuffled with the same seed), with the same rules for
  lost units.
- **Placement** (`lab/laptop/README.md`): core 0 for the system and the runner; the workers on
  CPU 14 (W = 1), CPUs 12 and 14 (W = 2), or CPUs 8, 10, 12 and 14 (W = 4), their siblings
  idle; the poster on CPU 2; H4 and the pilot as W = 1. Workers and poster are threads of one
  process, so wakeprobe and abpair pin each thread themselves (0e6c2c1); a refused pin, or a
  pinned thread seen on another CPU at the end, fails the process.
- **Lost units.** A unit not delivered by wakeprobe's hard limit (10 s) ends its process. That
  process is run again once, from its start with the same seed; the rerun's data replace it, and
  both runs are reported. If the rerun loses a unit too, the cell is reported as incomplete. A
  lost unit in a fixed or plain cell is itself reported as a lost wake. wakeprobe stops with a
  non-zero exit; the campaign runner (section 11) does the rerun.
- **Failed abpair processes.** An abpair process that fails (a thread error, a refused pin, a
  window that does not finish) is run again once with the same order and seed; both runs are
  kept. If the rerun fails too, its pairs are missing, and a cell left with fewer than 14 pairs
  is reported as incomplete.
- **Timed-out units** (delivered after 5 x max(B, B_eff, the coarsest timer resolution) but
  before the hard limit) keep their measured delay; their count is reported per cell.
- **Gaps** are reported as realized (from post and delivery times), not as drawn.
- **Gate.** No timed run before the D5 sanitizer records of the campaign's inputs exist for
  both systems (section 11).

## 9. Grid and laptop hours

Measured bases: the laptop's 100-trial S cells (STATE.md: Arch fixed 3.3 / 33.7 s and delaying
4.7 / 44.3 s at B = 1 / 10 ms; Windows 5 / 34 s and 34.5 / 51 s), times 3 for n = 300; abpair
processes took their windows plus at most 1 s; the 2026-09 publication designs on this laptop's
Linux took 1 min 44 s (`results/README.md`); request 7 ran protocol Bt with the fixed loop, 211
rounds per process, in 0.4 / 3.4 s on Arch and 1 / 4 s on Windows (1 s resolution) at B = 1 /
10 ms, process start included (2026-10-26 and 27). Windows gap sleeps overran by about 0.8 ms per post
(the fixed cells above against their drawn gaps). Everything else is arithmetic with B_eff = B
on Linux and 15.625 ms on Windows.

| Item | Arch | Windows | Basis |
|---|---|---|---|
| S: 150 cells (Arch), 92 (Windows), n = 300 | 9,729 s, 2.70 h | 8,451 s, 2.35 h | measured cells; the widened plant counted as delaying (upper bound) |
| S and Bt split into 10 processes | 245 s | 227 s | 9 extra processes per cell, each 50 ms settle + one B_eff probe + 5 warm-up units of 1.55 B (+ B_eff if delaying) + for delaying S cells 11 calibration posts of 0.1 B + B_eff; process starts not counted, no measured basis |
| Bt: 46 cells (Arch), 28 (Windows), n = 300 | at most 4,134 s, 1.15 h | at most 5,289 s, 1.47 h | 6,300 rounds at request 7's measured fixed rate (Windows at the top of its 1 s resolution: 2 / 5 s per 211 rounds), + 2 B_eff per round for delaying cells |
| H3 phase sweep | 68 s | 119 s | 30 steps x (24 posts of 2 B_eff + 11 calibration posts of 1.1 B_eff + 50 ms) |
| H4, R = 14 (cap 24), 2 comparisons x 4 levels | 6,888 s, 1.91 h (11,808 s, 3.28 h) | 4,592 s, 1.28 h (7,872 s, 2.19 h) | 2R windows of 10 s + R/2 processes of at most 1 s |
| A/A pilot, 4 levels x 8 pairs | 1,968 s | 1,312 s | 16 windows of 10 s + 4 processes of at most 1 s |
| H5: the 2026-09 designs | at most 104 s | about 131 s, plus 136 process starts | Arch measured (2026-09); Windows arithmetic |
| D5 sanitizer records | no measured basis | no measured basis | |
| **Total, R = 14** | **23,136 s, 6.43 h** | **20,120 s, 5.59 h** | |
| Total, R = 24 in every H4 cell | 28,056 s, 7.79 h | 23,400 s, 6.50 h | |

Together 12.02 h at R = 14 (14.29 h at the cap): 0.61 calendar days (0.73) at the queue plan's
19.68 measuring hours a day, plus builds, ctest, OS switches and the D5 records.

S cells per bound: epoll and io_uring 26 each (8 variants x 3 W + wrong worker x 2), condvar 23
(no re-trigger does not apply); Windows, IOCP and condvar 23 each. Delaying, for hours: absent
wake, unobserved primitive, wrong worker, no re-trigger and the widened plant (Arch 39 per
bound, Windows 22); the rest 36 and 24. Bt cells at W = 1 (no wrong worker): Arch per bound
8 + 8 + 7, Windows 7 + 7; delaying 11 and 6 per bound. A delaying round costs at most two B_eff
more than a fixed one: absent wake, unobserved primitive and no re-trigger leave the gate
waiting for one timed return; undrained edge drains the 192 tasks in three passes of 64, two of
them after a timed return.

## 10. Freeze package (for Alex)

Proposed values: n = 300 per cell as 10 processes of 30 trials; alpha 0.025 one-sided per
family with Holm; power threshold 0.95; false-positive bound 0.05; the B_eff rule of section 2;
the seen cells and H3 criteria as disclosed replications (section 1); H4 confirmatory over N in
{1, 10, 100} us with N = 0 disclosed; H4's decision rule and R >= 14 with the pilot rule of
section 7, cap 24; H3 on the phase sweep at the default settings; the widened plant of section
6; protocol Bt at W = 1; the procedure of section 8; H5 exploratory; H6 deferred to an addendum.

**Alex's answers, 2026-10-27:** every proposed value above and every departure below, as
drafted in draft 3 (e0252b9), accepted (STATE.md, decisions 11 to 18).

**Changes since his yes, for his confirmation:** H5's threads are pinned, its slack factor is
where 2026-09 varied it, it runs as its own block after the campaign, and its Windows compiler
differs from 2026-09's; the Bt hours rest on request 7's measured rate (totals 6.43 h on Arch
and 5.59 h on Windows at R = 14); the Rd replication label (section 1); family F4c for H4's cost
claim and H4's overall verdict rule; the testbed paragraph; the rules for incomplete cells and
failed abpair processes; the campaign runner as a prerequisite.

Departures from Alex's recorded decisions and from the plan, accepted on 2026-10-27:
- Decision 2 named a plant widened by "a stated delay"; section 6 uses one zero-timeout call of
  the backend's own wait instead (no chosen number of microseconds).
- Decision 10 kept the 2026-09 criterion (a 95% interval inside [0.98, 1.02]) and sized H4 at 8
  pairs per cell (1.07 h on Arch, 0.71 h on Windows). Section 7 states that criterion as exact
  sign tests on the median pair ratio, and D9 with one pair of tolerance needs R >= 14, which
  the pilot may raise to 24: 1.91 to 3.28 h on Arch and 1.28 to 2.19 h on Windows. N = 0 leaves
  the confirmatory family.
- Decision 3 (W in {1, 2, 4}) holds for protocol S; protocol Bt runs at W = 1 only.
- Plan section 2.0 item 4 takes margins from the pilot. Here the margins are the 2026-09
  criterion, fixed in advance as the claim itself; the pilot sets only R.
- Decision 6 (Netty, forced and natural, both reported) stands, in the deferred addendum.
- Plan section 2.0 item 7 asks for a probe-based budget. H5 on Windows, the process-split row
  and the delaying cells' extra time rest on arithmetic; the delaying Bt cells are not probed on
  purpose (section 12).

## 11. Prerequisites, in order, before the first confirmatory window

1. Done: the widened plant and protocol Bt (0cb37ee, 5e0fc7e); the review's fixes (281f90d);
   the four-core placement in `lab/laptop/README.md`.
2. Done: the Bt timing probe, fixed loop only (request 7, 2026-10-26 and 27).
3. Done: per-thread CPU pinning in wakeprobe and abpair; `coarsest_timer_us` in wakeprobe's
   summary; the v2 runner of the 2026-09 designs for H5 (`bench/run_h5.*`); desktop functional
   tests and a fresh review (0e6c2c1).
4. Done: freeze checks of drafts 1, 2 and 3. Then Alex confirms the changes of section 10 and
   the file is frozen.
5. The campaign runner: it runs the plan of S, Bt, sweep and H4 processes with each process's
   pins, reruns a failed process once (section 8) and keeps every output; desktop dry runs and
   a review.
6. Analysis code fixed and committed (ANALYSIS_COMMIT), with its own tests on synthetic data:
   the B_eff rule, both test statistics, Holm, the clustered intervals and the overdispersion
   check, the pilot power rule and the plan generator.
7. v2 sanitizer-record and gate scripts; D5 records of the campaign's inputs on the laptop for
   both systems; `bench/coverage.json` with its declared gaps (io_uring under MSan).
8. The A/A pilot, under the frozen rule; each cell's R into the revision log; the run plan
   generated and committed.
9. The first confirmatory window.

## 12. Disclosures (what was seen before the freeze)

- The 2026-09 study's results, including its exploratory random-gap run and resample (section
  1), and the seed of that run (20260926), which draft 2 reused and draft 3 replaces.
- 2026-10-23 and 24, laptop, at d6e157f (before the park handshake): a timing probe (wakeprobe,
  the then fixed loop, today's plain wake, and absent wake, 100-trial cells). Its latency values
  were not analysed, but its durations were read: absent wake took longer than the fixed loop in
  every probed cell, and Windows' absent-wake cells at B = 1 ms waited whole timer ticks.
- The same night, at d6e157f: the first H4 engineering probe, the then fixed loop (today's plain
  wake) against absent wake, empty tasks. Its ratios were read and decided D2 (the park
  handshake).
- 2026-10-25, at 56697c1 (after the handshake): the second H4 engineering probe, the fixed loop
  and plain wake against absent wake, empty tasks. Its ratios showed the N = 0 level on every
  backend before H4's workload was fixed by Alex (decision 10). The dispersion of both probes
  over 8 pairs and their passes per task were also seen before R and the pilot rule were set.
- Desktop development runs, which are not data and give no rates: the functional tests D1 to
  D10 (including D5, D9 and D10, which check delays against a threshold); a 1,000,000-round
  stress of the handshake in which the lazy-wake race, run as a positive control, lost one wake
  on IOCP and the fixed loop lost none; a TSan run of the test matrix at 4dfe58f (498 of 498, no
  report). All came before the blind spots and section 13 were written.
- The Bt timing probe (request 7) runs the fixed loop only, so it shows no defect's delay; its
  durations are read.
- Code reviews (of 0f94026; of the handshake, with its stress run, fixed in 56697c1; of 0cb37ee
  and 5e0fc7e, fixed in 281f90d; of the pinning and the H5 runner, fixed in 0e6c2c1) and freeze
  checks of drafts 1, 2 and 3 (2026-10-26 and 27) shaped this draft; none saw a latency measured
  in v2.

## 13. Threats to validity

- The detectors D8 and D10 use a test hook that the measured binary does not have; the other
  detectors run the measured loop. The evidence for the frozen verdict table is the laptop's
  ctest log at the campaign commit, archived with its sha256 by the lab runner.
- Open LOW from review: the blocking settle step cannot prove that a worker passed its
  re-check, so a worker descheduled for more than 50 ms could turn a strict D verdict into P
  (not reproduced in 30 runs under a CPU cap with a hog).
- Trials within one process are not independent; the clustered intervals and the
  overdispersion check of section 5 show how far that matters.
- Under protocol S at B = 1 ms on Windows, gaps of 0.1 to 3.0 ms cover the first fifth of the
  15.6 ms default period, so those cells hold for that protocol only; H3's sweep covers the
  whole period. Both Windows bounds are kept because B is what a programmer sets: at the
  default resolution they give one period through two different requests, and they count as
  separate cells in F1 and F2, a cost in power only.
- ThreadSanitizer is predicted to report no race on any variant (a lost wake is not a data
  race). This is checked by the D5 TSan record, not by a statistical test; a desktop
  development run agreed (section 12).

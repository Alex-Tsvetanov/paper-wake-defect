# Hypotheses v2 (DRAFT 2, not frozen)

Status: **draft 2**, written 2026-10-26 on the desktop after a freeze check of draft 1. Not
frozen. It becomes frozen only after Alex approves it (the freeze package in section 10) and
before the first confirmatory window. `hypotheses.md` (the 2026-09 study) is frozen and is not
edited; this file is new. Design: `design/v2-defects.md`. Values marked **(proposed)** are
Alex's to fix at the freeze.

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
of hypotheses): the delay model held on Linux and on Windows with the default timer, and failed
on Windows with the 1 ms timer at B = 1 ms; the bound-independence check failed on Linux and
held on Windows; the 21-post test flagged every defective and no correct configuration at a
gap of 1.7 B. Its Linux data came from this laptop and its Windows data from another machine.
They informed v2 as follows: H1 to H4 run Windows only at the default resolution, where the
delay model held, and the failing condition (the 1 ms timer) moves to H5; B = 100 ms, the
blocking bound and Linux's default timer slack move to H5 as well, so the hours go to backends,
workers and variants; the 2026-09 random-gap design (gaps from [0.1 B, 3.0 B]) becomes protocol
S, because a fixed gap of 1.7 B holds every post at one phase; the thresholds use B_eff, as in
2026-09.

## 2. Protocols, B_eff and test statistics

- **Single posts (S).** One trial is 21 posts. Before each post the poster sleeps a gap drawn
  uniformly from [0.1 B, 3.0 B] (seeded, section 8), after it sees the previous delivery; posts go
  round robin over the workers. wakeprobe `--protocol single --gap-frac-range 0.1:3.0`.
- **Batch (Bt), W = 1.** One trial is 21 rounds. A round posts a gate task that holds the
  worker, then 3 x 64 = 192 tasks to that worker, then releases the gate; the round's delay is
  the latest run time of the 192 tasks minus the release time. Before each round the poster
  sleeps a gap drawn as in S, counted from the previous round's last run. wakeprobe
  `--protocol batch --gap-frac-range 0.1:3.0`.
- **Phase sweep (H3 only), the 2026-09 gap-sweep design.** Absent wake, W = 1. Each of 30
  processes posts at one phase target, 0.05, 0.15, ..., 2.95 B_eff after each delivery: 21
  measured posts, 3 warm-up posts, B_eff from the process's own 11 calibration posts. wakeprobe
  `--phase-frac`.
- **Phase.** The time from the worker's previous delivery to the post (wakeprobe `phase_ns`).
  Under absent wake at W = 1 every delivery happens at a timed return, so the phase is the time
  since the worker's last timed return.
- **B_eff (proposed rule).** The observed period of a worker's timed returns. For the
  thresholds of every S and Bt cell of a backend, system and B: the median of the calibrated
  B_eff (`calib.b_eff_ns`, 11 calibration posts per process) over the 10 processes of the
  absent-wake, W = 1, S cell of that backend, system and B. Fallback, if fewer than 8 of the 10
  processes calibrate or their values span more than 10% of their median: B on Linux, and on
  Windows the larger of B and the coarsest timer resolution the process read. Which source
  applied is reported per cell. H3 uses each process's own B_eff, as in 2026-09.
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
the remaining cells only.

**H2 (false-positive rate).** For every cell of the fixed loop and of plain wake, under protocol
S (W in {1, 2, 4}) and protocol Bt (W = 1), T_med flags fewer than 5% of trials. Test: exact
one-sided binomial test of a false-positive rate >= 0.05 per cell; Holm over family F2. The
same rule as H1 for failing cells.

**H3 (the delay of a missed wake).** For absent wake at W = 1 under the phase sweep, at the
settings of section 1 (Windows at its default timer resolution), each post's delay equals
B_eff minus (its phase mod B_eff). Criterion, per backend, system and B in {1, 10} ms: the
median absolute residual over the sweep's 630 measured posts is below 5% of B_eff (the 2026-09
H1 criterion, unchanged). Ten criteria, no p-value. The 1 ms Windows timer, where the 2026-09
criterion failed, is in H5, not here.

**H4 (the throughput cost of the fix).** With a busy worker (poll mode, W = 1, P = 1, K = 64),
for each backend and system and each per-task work N in {1, 10, 100} us, the per-pair
throughput ratio, fixed loop over absent wake, lies within [0.98, 1.02]. Decision rule (section
7): two one-sided exact sign tests per cell, Holm over family F4 (m = 15). The margins are the
2026-09 H4 criterion; "the fix" now means the wake with the park handshake (D2). **N = 0 us is
not confirmatory:** its ratios were seen before the freeze (section 12). It is run and reported
outside F4 as a disclosed repeat, with the seen direction as its prediction: below 0.98 on
epoll, io_uring and IOCP; within the margins on condvar on Linux; no prediction for condvar on
Windows (too noisy to read in the probe).

**H5 (same hardware, two systems; exploratory).** On this laptop, the 2026-09 delay-model fit
and bound-independence check are measured again with their frozen criteria and the 2026-09
designs (`bench/run_matrix.*`: gap sweep, bound sweep, matrix) and all their factors: B in
{1, 10, 100} ms and blocking; on Windows the timer period {default, 1 ms}; on Linux the timer
slack {1 ns, default}. H5 is exempt from section 1's settings. Backends: those of 2026-09
(epoll, io_uring, IOCP). Arms: the 2026-09 fixed loop is `plain_wake`; the 2026-09 defect is
`absent_wake` on epoll and IOCP (2026-09 omitted the wake) and `unobserved_primitive` on io_uring
(the 2026-09 misdirect: an eventfd written, never watched). Differences from 2026-09: one binary
holds both arms, and v2's defect variants sit on the park handshake, which changes nothing for
a post that lands while the worker is parked (every post of these designs) and is disclosed.
The 2026-09 PM QoS experiment is not repeated. Reading: if the 2026-09 split (the delay model
fails on Windows with the 1 ms timer at B = 1 ms; bound independence fails on Linux and holds
on Windows) recurs on this one machine, it is attributed to the operating system and its timer
configuration, not to the hardware; if it does not recur, the 2026-09 split is reported as
machine-dependent; anything else is reported as found.

**H6 (the historical case): deferred.** The Netty case (`historical/candidates-2026-10.md`) is
not part of this freeze. Its design (bound, n, trial, the forced and the natural protocol) goes
into a dated addendum, frozen before any scoring run and before any reproduction that shows
CR7's behaviour.

## 4. Predicted blind spots (stated now; reported with intervals, not tested)

"Blind" means T_med flags at most 5% of trials, the H2 bound; each rate is reported with its
exact 95% interval and its clustered interval.
- T_med under protocol S is blind to the lazy-wake race (natural and widened) and to undrained
  edge: their single posts are delayed rarely or never.
- T_med under protocol Bt is blind to absent wake, unobserved primitive, no re-trigger and the
  lazy-wake race (natural and widened): the 192 tasks are posted behind a running gate and need
  no wake, and the gate itself, whose delay is not counted, is posted to a parked worker,
  outside the race window.
- No claim that T_med or T_count detects the lazy-wake race is drawn from the campaign. T_count
  is reported on every F2 cell (its false-positive rate) with both intervals. For each
  lazy-wake cell the per-post rate of delays above B_eff / 2 is reported with an exact one-sided
  95% upper bound (zero such posts in 6,300 give 4.75e-4 per post).

## 5. Families and Holm

| Family | Claims | m |
|---|---|---|
| F1 | H1: S delaying cells, Arch 30 per bound x 2 = 60, Windows 16 x 2 = 32; Bt undrained edge, Arch 6, Windows 4 | 102 |
| F2 | H2: S fixed and plain, Arch 36, Windows 24; Bt fixed and plain, Arch 12, Windows 8 | 80 |
| F4 | H4: 5 backend-system pairs x 3 work levels (1, 10, 100 us) | 15 |

alpha 0.025 one-sided per family **(proposed)**; in F4, per side of the TOST. Outside the
families: H3's ten criteria (no p-value), H4 at N = 0, plain wake against absent wake under H4's
grid (descriptive), the blind spots, T_count, and H5.

Every rate also gets a clustered interval: a percentile bootstrap over the cell's 10 processes,
10,000 resamples, seed 20261026, next to the exact Clopper-Pearson interval. Overdispersion: for
each cell the variance of the per-process flag rate is compared with its binomial value; a cell
whose dispersion index exceeds the 99th percentile of the index under the binomial (simulated,
same seed) is flagged as overdispersed, and its clustered interval is then stated next to its
exact p-value as a limitation.

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

- **Decision.** Per cell, two one-sided exact sign tests: the number of pairs whose ratio
  exceeds 0.98, and the number below 1.02, each against Binomial(R, 1/2), at alpha 0.025 per
  side with Holm over F4. The cell shows no cost when both reject. The 95% interval (percentile
  bootstrap of the mean per-pair ratio, clustered by process) is descriptive and decides nothing.
- **R >= 14 (proposed).** At m = 15 the strictest threshold is 0.025 / 15 = 1.67e-3. R = 14 is
  the smallest R at which one pair outside a margin still passes: P(X <= 1 | 14) = 15/16384 =
  9.2e-4. R = 13 gives 14/8192 = 1.71e-3, which fails.
- **Pilot rule (proposed), frozen before the pilot.** The A/A pilot runs the fixed loop against
  itself in every H4 cell (5 pairs x 4 work levels, N = 0 included): 8 pairs of 10 s windows in 4
  processes of 2 pairs. Per cell, s is the standard deviation of the 8 per-pair log ratios.
  Power(R) is the probability that the TOST passes at the strictest Holm step when the true
  ratio is 1 and each pair's log ratio is normal with standard deviation s (computed exactly
  from the trinomial of pairs below, inside and above the margins). The cell's R is the smallest
  R in 14 to 24 with power at least 0.8. If none reaches 0.8, R = 24 and the cell is declared
  underpowered before the campaign: its TOST is still run and reported, and a failure is read
  as inconclusive, never as a cost. For orientation (arithmetic, no data): s = 0.010 gives R =
  14 (power 0.92), s = 0.0125 gives R = 18 (0.86), s = 0.015 gives R = 20 (0.80). The pilot
  compares no arms; each cell's R goes into the revision log before the first H4 window.
- **Pairs.** ABBA within a process, 2 pairs per process, ceil(R/2) processes per comparison, in
  the system's shuffled order (section 8). Pairs in one process may be correlated; the clustered
  interval shows it.

## 8. Procedure and validity rules

- **Processes.** Each S and Bt cell runs as 10 wakeprobe processes of 30 trials (630 posts or
  rounds each), each with its own settle, probe post, calibration (delaying variants) and 5
  warm-up units. Process i (0 to 9) of every cell draws its gaps with seed 20260926 + i, so the
  cells share their gap sequences.
- **Order.** All processes of one system's campaign (S, Bt, the sweep, H4, H5) run in one order,
  shuffled with seed 20261026; the plan is generated from this file by the analysis code and
  committed with it (ANALYSIS_COMMIT).
- **Placement.** As in `lab/laptop/README.md`: core 0 for the system and the runner, workers on
  server cores with their siblings idle, the poster on a generator CPU. W = 4 needs four
  server cores, which the lab placement does not yet cover; that placement is a lab request and
  is fixed before the freeze.
- **Lost units.** A unit not delivered by wakeprobe's hard limit (10 s) ends its process. That
  process is run again once, from its start with the same seed; the rerun's data replace it, and
  both runs are reported. If the rerun loses a unit too, the cell is reported as incomplete. A
  lost unit in a fixed or plain cell is itself reported as a lost wake.
- **Timed-out units** (delivered after 5 x max(B, B_eff, the coarsest timer resolution) but
  before the hard limit) keep their measured delay; their count is reported per cell.
- **Gaps** are reported as realized (from post and delivery times), not as drawn.
- **Gate.** No timed run before the D5 sanitizer records of the campaign's inputs exist for
  both systems (section 11).

## 9. Grid and laptop hours

Bases: the laptop's measured 100-trial S cells (STATE.md: Arch fixed 3.3 / 33.7 s and
delaying 4.7 / 44.3 s at B = 1 / 10 ms; Windows 5 / 34 s and 34.5 / 51 s), times 3 for n = 300;
abpair processes took their windows plus at most 1 s (measured); the 2026-09 Arch runs on this
laptop took 30 min 23 s. Everything else is arithmetic, named as such.

| Item | Arch | Windows | Basis |
|---|---|---|---|
| S: 150 cells (Arch), 92 (Windows), n = 300 | 9,729 s, 2.70 h | 8,451 s, 2.35 h | measured cells; the widened plant counted as delaying (upper bound) |
| S and Bt split into 10 processes | 206 s | 273 s | arithmetic (settle, calibration, warm-up); process starts not counted, no measured basis |
| Bt: 46 cells (Arch), 28 (Windows), n = 300 | at most 3,995 s, 1.11 h | at most 4,007 s, 1.11 h | arithmetic upper bound; the fixed rate is replaced by request 7's when it lands |
| H3 phase sweep | 68 s | 119 s | arithmetic |
| H4, R = 14 (cap 24), 2 comparisons x 4 levels | 6,888 s, 1.91 h (11,808 s, 3.28 h) | 4,592 s, 1.28 h (7,872 s, 2.19 h) | measured abpair overhead |
| A/A pilot, 4 levels x 8 pairs | 1,968 s | 1,312 s | measured abpair overhead |
| H5: the 2026-09 designs | about 1,823 s | about 131 s, plus 136 process starts | 2026-09 Arch runs; Windows arithmetic |
| D5 sanitizer records | no measured basis | no measured basis | |
| **Total, R = 14** | **24,677 s, 6.85 h** | **18,885 s, 5.25 h** | |
| Total, R = 24 in every H4 cell | 29,597 s, 8.22 h | 22,165 s, 6.16 h | |

Together 12.10 h at R = 14 (14.38 h at the cap): 0.61 calendar days (0.73) at the queue plan's
19.68 measuring hours a day, plus builds, ctest, OS switches and the D5 records.

S cells per bound: epoll and io_uring 26 each (8 variants x 3 W + wrong worker x 2), condvar 23
(no re-trigger does not apply); Windows, IOCP and condvar 23 each. Delaying, for hours: absent wake,
unobserved primitive, wrong worker, no re-trigger and the widened plant (Arch 39 per bound,
Windows 22); the rest 36 and 24. Bt cells at W = 1 (no wrong worker): Arch per bound 8 + 8 + 7,
Windows 7 + 7. Bt delaying cells: at most two B_eff more per round than the fixed rate (one for
the gate, one for an undrained batch).

## 10. Freeze package (for Alex)

Proposed values: n = 300 per cell as 10 processes of 30 trials; alpha 0.025 one-sided per
family with Holm; power threshold 0.95; false-positive bound 0.05; the B_eff rule of section 2;
H4 confirmatory over N in {1, 10, 100} us with N = 0 disclosed; the sign-test TOST as H4's
decision rule; R >= 14 and the pilot rule of section 7 with the cap of 24; H3 on the phase
sweep at the default settings; the widened plant of section 6; protocol Bt at W = 1; the
procedure of section 8; H5 exploratory; H6 deferred to an addendum.

Departures from Alex's recorded decisions, each needing his yes:
- Decision 2 named a plant widened by "a stated delay"; section 6 uses one zero-timeout call of
  the backend's own wait instead (no chosen number of microseconds).
- Decision 10 sized H4 at 8 pairs per cell (1.07 h on Arch, 0.71 h on Windows). D9 with one
  pair of tolerance needs R >= 14, and the pilot rule may raise it to 24: 1.91 to 3.28 h on Arch
  and 1.28 to 2.19 h on Windows. N = 0 leaves the confirmatory family.
- Decision 6 (Netty, forced and natural, both reported) stands, in the deferred addendum.

## 11. Prerequisites, in order, before the first confirmatory window

1. Done: the widened plant and protocol Bt (0cb37ee, 5e0fc7e); the review's fixes (281f90d).
2. The Bt timing probe, fixed loop only (request 7), so Bt's hours rest on a measured rate.
3. The W = 4 placement on the laptop (lab request).
4. A fresh freeze check of this draft; then Alex freezes it.
5. Analysis code fixed and committed (ANALYSIS_COMMIT), with its own tests on synthetic data:
   the B_eff rule, both test statistics, Holm, the clustered intervals, the overdispersion
   check, the pilot power rule and the run plan.
6. v2 sanitizer-record and gate scripts; D5 records of the campaign's inputs on the laptop for
   both systems.
7. The A/A pilot, under the frozen rule; each cell's R into the revision log.
8. The first confirmatory window.

## 12. Disclosures (what was seen before the freeze)

- The 2026-09 study's results (section 1).
- 2026-10-23 and 24, laptop, at d6e157f: a timing probe (wakeprobe, fixed loop and absent wake,
  100-trial cells). Its latency values were not analysed, but its durations were read, and they
  encode the mean extra delay per post of absent wake (STATE.md notes that Windows' absent-wake
  cells at B = 1 ms waited whole timer ticks).
- The same night, and again on 2026-10-25 at 56697c1: two H4 engineering probes (fixed and plain
  against absent wake, empty tasks). Their ratios were read: the first decided D2 (the park
  handshake); the second showed the N = 0 level on every backend before H4's workload was fixed
  by Alex (decision 10). Their dispersion over 8 pairs and their passes per task were also seen
  before R and the pilot rule were set.
- Desktop development runs, which are not data and give no rates: the functional tests D1 to
  D10 (including D5, D9 and D10, which check delays against a threshold), and a 1,000,000-round
  stress of the handshake in which the lazy-wake race, run as a positive control, lost one wake
  on IOCP and the fixed loop lost none. Both came before the blind spots were written.
- The Bt timing probe (request 7) runs the fixed loop only, so it shows no defect's delay; its
  durations are read.
- A code review of 0cb37ee and 5e0fc7e and a freeze check of draft 1 (2026-10-26) shaped this
  draft; neither saw any measured latency.

## 13. Threats to validity

- The detectors D1 to D10 are white-box tests: they use a test hook that the measured binary
  does not have. The evidence for the frozen verdict table is the laptop's ctest log at the
  campaign commit, archived with its sha256 by the lab runner.
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
  race). This is checked by the D5 TSan record, not by a statistical test.

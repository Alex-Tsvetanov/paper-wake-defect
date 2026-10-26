# Hypotheses v2 (DRAFT, not frozen)

Status: **draft**, written 2026-10-26 on the desktop. Not frozen. It becomes frozen only after
Alex approves it (the freeze package in section 9) and before the first confirmatory window.
`hypotheses.md` (the 2026-09 study) is frozen and is not edited; this file is new. Design:
`design/v2-defects.md`. Values marked **(proposed)** are Alex's to fix at the freeze.

## 1. Setup

One or more workers (W) park in their backend's wait with bound B; a poster posts tasks; the
measured quantity is a task's delay from post to run. Backends: epoll and io_uring on Linux,
IOCP on Windows, a condition variable on both; Linux and Windows on the same laptop (AMD Ryzen 7
5800H). Variants (design note, section 4): the fixed loop with the park handshake; plain wake
(the 2026-09 loop, a reference arm, not a defect); absent wake; unobserved primitive; lazy-wake
race (natural window); lazy-wake race, widened (section 6); wrong worker (W >= 2); no re-trigger
(epoll, io_uring); undrained edge. Every build is the Release build of one commit, which holds
every variant (selected at run time).

B_eff is the observed period of a worker's timed returns: B on Linux, and B rounded up to the
timer resolution in effect on Windows. Linux keeps timer slack 1 ns; Windows keeps the default
timer resolution.

## 2. Protocols and test statistics

- **Single posts (S).** One trial is 21 posts, each after a gap drawn uniformly from
  [0.1 B, 3.0 B] (seeded), posts spread round robin over the workers (the 2026-09 random-gap
  design). wakeprobe, `--posts 21 x n`.
- **Batch (Bt).** One trial is 21 rounds; a round posts a task that holds its worker, then
  3 x 64 tasks to that worker, then releases it; the round's delay is that of its slowest task,
  from the release. (To be built in wakeprobe; the functional test D9 is its model.)
- **T_med**, the paper's test: a trial flags when the median of its 21 delays exceeds B_eff / 5.
- **T_count**, a tail statistic (exploratory): a trial flags when at least one of its 21 delays
  exceeds B_eff / 2.
- n = **300 trials per cell (proposed)**. Basis (D9, exact one-sided binomial tests, alpha 0.025,
  Holm over the families below): with n = 300 a cell can pass with up to 3 misses (F1) or 3
  false positives (F2); with n = 200 it could pass only with none.

## 3. Hypotheses

**H1 (power).** For every cell of absent wake, unobserved primitive, wrong worker and no
re-trigger under protocol S, at B in {1, 10} ms and W in {1, 2, 4} (wrong worker 2 and 4), and
for every cell of undrained edge under protocol Bt (W = 1), T_med flags more than 95% of trials.
Test: exact one-sided binomial test of power <= 0.95 per cell; Holm over family F1.

**H2 (false-positive rate).** For every cell of the fixed loop and of plain wake, under protocol
S (W in {1, 2, 4}) and protocol Bt (W = 1), T_med flags fewer than 5% of trials. Test: exact
one-sided binomial test of a false-positive rate >= 0.05 per cell; Holm over family F2.

**H3 (the delay of a missed wake).** For absent wake at W = 1 under protocol S, each post's delay
equals B_eff minus (its phase mod B_eff), where the phase is the time since the worker's last
return: the median absolute residual is below 5% of B_eff at B = 1 and 10 ms on every backend.
(The 2026-09 H1 criterion, unchanged.)

**H4 (the throughput cost of the fix).** With a busy worker (poll mode, W = 1, P = 1, K = 64),
for each backend and each per-task work N in {0, 1, 10, 100} us, the 95% interval of the
per-pair throughput ratio, fixed loop over absent wake, lies within [0.98, 1.02]. Test: two
one-sided exact sign tests (TOST) per cell; Holm over family F4; a clustered bootstrap by pair
as the interval. (The 2026-09 H4 criterion, unchanged; "the fix" now means the wake with the
park handshake, D2.)

**H5 (same hardware, two systems).** The 2026-09 H1 (sawtooth) and H2 (fixed delay independent
of B) are measured again with their frozen criteria and designs on both systems of the laptop,
with the plain-wake loop (the 2026-09 fixed loop) and its omitted wake (absent wake on single
posts). In 2026-09 H1 held on Linux and failed on Windows with the 1 ms timer, and H2 the
reverse; on one machine a split that recurs is attributed to the operating system. Exploratory
attribution; the criteria are the frozen ones.

**H6 (a historical case).** Netty NioEventLoop 4.1.0.CR7 (buggy) against 4.1.0.Final (fix
f44f3e7): forced through the public SelectStrategy hook (disclosed as forcing), T_med flags
CR7 and passes Final; unforced, the power and false-positive rate are reported. Exploratory.

## 4. Predicted blind spots (stated now; reported with intervals, not tested)

- T_med under protocol S is blind to the lazy-wake race (natural and widened) and to undrained
  edge: their single posts are delayed rarely or never.
- T_med under protocol Bt is blind to absent wake, unobserved primitive, wrong worker and no
  re-trigger: posts made behind a running task need no wake (the re-check finds them).
- The widened plant may be caught by T_count under protocol S; no level of power is predicted.
- ThreadSanitizer reports no race on any variant (a lost wake is not a data race).

## 5. Families and Holm

| Family | Claims | m |
|---|---|---|
| F1 | H1: Arch 30 delaying cells per bound x 2 = 60; Windows 16 x 2 = 32; Bt undrained 6 + 4 | 102 |
| F2 | H2: S fixed and plain, Arch 36, Windows 24; Bt fixed and plain, Arch 12, Windows 8 | 80 |
| F3 | H3: absent wake, W = 1, 5 backend-system pairs x 2 bounds (a criterion, not a p-value) | 10 |
| F4 | H4: 5 backend-system pairs x 4 work levels | 20 |

alpha 0.025 one-sided per family **(proposed)**. Plain wake against absent wake under H4's grid
is a descriptive reference outside F4.

## 6. The widened lazy-wake plant **(proposed)**

The natural window lies between two instructions. The widened plant puts one real operation in
it: between its look at the stack and its announcement the worker makes one non-blocking poll of
its own wait object, as loops do that check for I/O between their queue check and their park
(the Netty case checks its queue, then selects). Its width is one system call, not a chosen
number of microseconds, and it is fixed here without any detection data.

## 7. R for H4 and the A/A pilot

- D9: TOST at alpha 0.025 per side, Holm over m = 20: the threshold is 0.00125, so R >= 10
  pairs (2^-10 = 0.00098). **R = 12 (proposed)**, for margin.
- The A/A pilot (fixed against fixed, 8 pairs of 10 s windows per backend and work level)
  measures the dispersion of the per-pair ratio. Rule, frozen before the pilot: R is the larger
  of 12 and the smallest R whose bootstrap 95% interval half-width, from the pilot's per-pair
  ratios, is at most 0.01. The pilot compares no arms.
- Detection false-positive rates come from the campaign's own fixed and plain cells (F2), not
  from the pilot.

## 8. Grid and laptop hours

Basis: the laptop's measured 100-trial cells (STATE.md: Arch fixed 3.3 / 33.7 s and delaying
4.7 / 44.3 s at B = 1 / 10 ms; Windows 5 / 34 s and 34.5 / 51 s), times 3 for n = 300; abpair
runs of 16 windows took 160 s plus at most 1 s.

| Item | Arch | Windows |
|---|---|---|
| S: 150 cells (Arch), 92 (Windows), n = 300 | 9,405 s, 2.61 h | 7,614 s, 2.12 h |
| Bt: 46 cells (Arch), 28 (Windows), W = 1, n = 300 | no measured basis | no measured basis |
| H4, per backend 2 comparisons x 4 levels x 24 windows of 10 s | 5,760 s, 1.60 h | 3,840 s, 1.07 h |
| A/A pilot, per backend 4 levels x 16 windows of 10 s | 1,920 s, 0.53 h | 1,280 s, 0.36 h |
| H5: the 2026-09 designs | about 0.5 h (2026-09 Arch runs: 30 min 23 s) | no measured basis |
| Netty (H6) | no measured basis | no measured basis |

S cells: Arch per bound, epoll and io_uring 26 each (8 variants x 3 W + wrong worker x 2),
condvar 23; Windows per bound, IOCP and condvar 23 each. Bt cells at W = 1 (no wrong worker):
Arch per bound 8 + 8 + 7, Windows 7 + 7. Before the freeze a timing probe (durations only,
compares nothing) gives Bt's rate.

## 9. Freeze package (for Alex)

Proposed values: n = 300; alpha 0.025 one-sided per family with Holm; power threshold 0.95;
false-positive bound 0.05; R = 12 and the pilot rule of section 7; the widened plant of section
6; protocol Bt at W = 1; H5 and H6 as exploratory.

## 10. Prerequisites, in order, before the first confirmatory window

1. Build the widened plant and protocol Bt; desktop functional tests; a fresh review.
2. Timing probe for Bt (durations only, compares nothing), so its hours are known at the freeze.
3. This file frozen by Alex, with the hours above and the pilot rule of section 7.
4. The A/A pilot, under the frozen rule (H4's R).
5. Analysis code fixed and committed (ANALYSIS_COMMIT), with its own tests on synthetic data.
6. v2 sanitizer-record and gate scripts; D5 records on the laptop for both systems.
7. Then the first confirmatory window.

## 11. Disclosures

- 2026-10-23 and 24, laptop, at d6e157f: a timing probe (wakeprobe, none and absent wake,
  100-trial cells). Its latency values were not analysed; only its durations were read.
- The same night: the first H4 engineering probe (fixed against absent wake, empty tasks). It
  was read before the freeze and decided D2 (the park handshake).
- 2026-10-25, at 56697c1: the second H4 engineering probe (fixed and plain against absent wake,
  empty tasks). It was read, and H4's workload (section 3, H4) was fixed afterwards, by Alex,
  on a basis independent of its ratios. The 0 us level was thereby seen on every backend before
  the freeze.
- Desktop development runs (functional tests, development sanitizers) are not data.

# P1 v2, H6: the historical case (addendum to hypotheses-v2.md)

**Status: draft 1, 2026-10-29. Not frozen.** `hypotheses-v2.md` section 3 deferred H6 to this
addendum: "Its design (bound, n, trial, the forced and the natural protocol) goes into a dated
addendum, frozen before any scoring run and before any reproduction that shows CR7's
behaviour." No run of Netty 4.1.0.CR7 has been made, on any machine. Everything said below
about CR7 is read from its source.

## 1. The case

Netty's event loop, 4.1.0.CR7 (defect) against 4.1.0.Final (fix), on the laptop's Linux, with
the Epoll transport (`EpollEventLoopGroup`; Alex, decision 36 of 2026-10-29, which narrows
decision 6 from NioEventLoop to the Epoll loop). The fix is commit 877767d ("EpollEventLoop
ensures that all submitted tasks are executed immediately"), in Netty PR 5258, whose title is
"Some EpollEventLoop tasks might be pended for a long time". It is a lazy-wake race with a
bounded delay, the shape of `design/v2-defects.md`'s lazy-wake row.

The defect, read at tag netty-4.1.0.CR7 (`EpollEventLoop.java`):
- A poster runs `execute(task)`: `addTask(task)`, then `wakeup`: if `wakenUp` changes from 0 to
  1 it writes the loop's eventfd. If `wakenUp` is already 1, it does nothing.
- The loop runs `calculateStrategy(selectNowSupplier, hasTasks())`. With no task it takes
  SELECT and calls `epollWait(getAndSet(wakenUp, 0) == 1)`, which blocks in `epoll_wait` until
  the eventfd is written or the deadline passes. The deadline is the next scheduled task, or
  `SCHEDULE_PURGE_INTERVAL` = 1 s when there is none.
- After a wake, the loop writes the eventfd again when `wakenUp` is 1 (the re-arm), and then
  `processReady` reads the eventfd, which drains both writes. So after every wake by a poster,
  `wakenUp` stays 1 with no wake pending, until the loop's next SELECT resets it.
- A post that lands between `hasTasks()` and that reset finds `wakenUp` at 1 and writes
  nothing. The loop resets the flag and blocks in `epoll_wait` with the post queued, for up to
  1 s. 4.1.0.Final checks the queue again inside `epollWait`, before it blocks.

Artifacts, unmodified: `io.netty:netty-all` 4.1.0.CR7 and 4.1.0.Final from Maven Central
(sha1 and md5 as published; sha256 bce6ce00... and 840f27fb..., `lab` request 1). Both jars
hold the Epoll classes and `META-INF/native/libnetty-transport-native-epoll.so`. JDK: Temurin
8u504-b01, Linux x64 (sha256 as published).

**A derived note on NIO, not run (2026-10-29).** `historical/candidates-2026-10.md` sketches a
forced NIO reproduction. Read again at the CR7 tag, it would not delay: its CONSUME step calls
`selectNow`, and CR7's `NioEventLoop.selectNow()` writes the wakeup again whenever `wakenUp` is
true, and a JDK `Selector` keeps a wakeup pending until its next selection. So on NIO the
stale flag never comes without a pending wakeup, except on rare paths (a raw `selectNow` after
256 cancelled keys, or a selector rebuild). This matches the fix PR's note that NIO tests
showed no problem. The note is reported as derived; the candidates file is left as it is.

## 2. Harness and settings

- One Java program, `historical/netty/` (first-party, compiled by the pinned JDK's `javac`),
  runs both arms; the arm is only the jar on the class path. One JVM process per run.
- One event loop: `new EpollEventLoopGroup(1, threadFactory, strategyFactory)` (public in both
  versions). The forced protocol passes its own `SelectStrategyFactory`; the natural protocol
  passes `DefaultSelectStrategyFactory.INSTANCE`.
- Delay of a post: `System.nanoTime()` in the task when it runs, minus `System.nanoTime()` in
  the posting thread just before `execute`.
- Bound and threshold: no task is ever scheduled, so the loop's bound is B = 1 s. B_eff = B (the
  fallback rule of section 2, which on Linux is B). T_med flags a trial when the median of its
  21 delays exceeds B / 5 = 200 ms; T_count when one of them exceeds B / 2 = 500 ms.
- JVM: `-XX:+UseSerialGC -Xms128m -Xmx128m`. The whole JVM is pinned with `taskset -c 2,14`
  (the poster's and the worker's CPUs of section 8); which thread runs on which of the two is
  the kernel's. Timer slack is the JVM's default. Both are disclosed.
- Each process does 5 warm-up posts of its protocol first, not measured.

## 3. Protocols

**Forced (disclosed as forcing, decision 6).** One forced post:
1. The loop is idle in `epoll_wait`. The main thread posts a task X, which wakes it normally.
   X runs on the loop and arms the hook.
2. On the loop's next `calculateStrategy` call with no task, the hook (on the loop thread) has a
   helper thread post the measured task Y and waits until the helper's `execute(Y)` has
   returned. Then it disarms itself and returns SELECT, as the default strategy would.
3. On CR7, Y was posted while `wakenUp` was still 1 from X's wake, so Y wrote nothing; the loop
   blocks in `epoll_wait` for up to 1 s (derived). On Final, `epollWait` finds Y in its new check
   and does not block (derived).
4. Pacing: each forced post owns a slot of 1.1 s from X's post, on both arms, so the loop is
   idle in `epoll_wait` again when the next X comes.

The hook widens the natural window to certainty: it holds the loop between its look at the
queue and the flag reset while Y is posted. It uses only the public `SelectStrategy` API.

**Natural (no hook).** The default strategy. One poster posts a task, waits until it has run
(it spins on a volatile flag the task sets), then waits a gap drawn uniformly from [0, 20 us]
(seeded, busy-waited) and posts the next one. The window of section 1 opens after every wake, so
whether a post lands in it depends only on timing. No hit rate is known beforehand.

**Trials and processes.** A trial is 21 posts. Each cell is 10 processes of 15 trials (150
trials), with process i using seed 2026102900 + i. Cells: CR7 forced, Final forced, CR7
natural, Final natural. The 40 processes run in one order, shuffled with seed 20261026.

## 4. Hypotheses

**H6a (forced, confirmatory).** On CR7, T_med flags more than 95% of forced trials. On Final,
T_med flags fewer than 5% of forced trials. Tests: exact one-sided binomial tests, as H1 and
H2; Holm over family F6 (m = 2, alpha 0.025). With n = 150 a cell rejects at the first Holm step
(0.0125) with at most 1 miss or false positive (p = 4.05e-3; 2 give 1.82e-2), and at the second
step (0.025) with at most 2.

If CR7's forced cell does not reject, the derivation of section 1 is wrong for this system or
JDK. That is reported as found, with the cell's delays, and the design is not changed after the
fact.

**H6b (natural, exploratory).** No test. Predictions, stated now: on CR7 some natural posts are
delayed by more than B / 2; on Final none is. Reported per cell: the number of posts delayed by
more than B / 2 with its exact 95% interval, and the T_med and T_count flag rates with exact
and clustered intervals (section 7's bootstrap). A Final post delayed by more than B / 2 is
reported as found.

## 5. Validity rules

- A process is good when it exits 0, every post ran, the forced hook injected exactly once per
  forced post, and the JVM reports the pinned CPU set and the expected Netty version
  (`Version.identify()` against the jar's file name). A failed process is run once more with
  the same seed; both runs are reported; a cell without 10 good processes is incomplete and
  counts as not rejected (as section 8).
- Every run records the JVM's GC count and time per trial and the Epoll availability check.
- The runs are Release-equivalent by construction (no debug agent, no assertions flag).

## 6. Order of work and the desktop rule

1. This addendum, a fresh Opus review, then Alex's freeze.
2. After the freeze: the harness, its runner and the scorer, committed with tests on synthetic
   output, and reviewed. On the desktop the harness runs only in a check mode that prints no
   time, and only against 4.1.0.Final, in the shared Linux image. Against CR7 it is only
   compiled; no CR7 post runs anywhere before the laptop run (its wall-clock time alone would
   show the delay).
3. The D5 gate for a Java harness: open (section 7).
4. The laptop run, then scoring.

## 7. Open before any laptop run

- **D5.** Sanitizers do not cover Java. A declared gap would go into `bench/coverage.json`,
  whose sha256 every D5 record of the campaign carries, so it is not edited here. The lab
  decides with Alex: a separate H6 coverage declaration, new records, or another gate.
- **The native library.** Whether the 2016 `libnetty-transport-native-epoll.so` loads on the
  laptop's current Arch is not known. A check-mode run against Final in the shared Linux image,
  then on the laptop, shows it before any timed run.

## 8. Laptop hours (no measured basis)

- Forced: 21 posts x 1.1 s = 23.1 s per trial; 15 trials and 5 warm-up posts per process, about
  352 s; 10 processes per cell; two cells: about 1.96 h.
- Natural: on Final, about 21 x (a few us) per trial. On CR7, each hit costs up to 1 s, so a
  trial costs at most 21 s and a cell at most 150 x 21 s = 52.5 min.
- JVM start and the shuffle's overhead: not measured.
- Total: about 2.9 h at most on one Linux visit.

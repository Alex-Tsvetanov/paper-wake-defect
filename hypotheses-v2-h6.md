# P1 v2, H6: the historical case (addendum to hypotheses-v2.md)

**Status: draft 2, 2026-10-29. Not frozen.** `hypotheses-v2.md` section 3 deferred H6 to this
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

The defect, read at tag netty-4.1.0.CR7 (`EpollEventLoop.java` and the native epoll C file):
- A poster runs `execute(task)`: `addTask(task)`, then `wakeup`: if `wakenUp` changes from 0 to
  1 it writes the loop's eventfd. If `wakenUp` is already 1, it does nothing.
- The loop runs `calculateStrategy(selectNowSupplier, hasTasks())`. With no task it takes
  SELECT and calls `epollWait(getAndSet(wakenUp, 0) == 1)`, which blocks in `epoll_wait` until
  the eventfd is written or the deadline passes. No task is scheduled in 4.1.0 by default, so the
  deadline is `SCHEDULE_PURGE_INTERVAL` = 1 s. An EINTR makes the native code wait again with the
  full timeout.
- After a wake, the loop writes the eventfd again when `wakenUp` is 1 (the re-arm), and then
  `processReady` reads the eventfd. The eventfd is non-blocking, not in semaphore mode, and
  registered level-triggered, so that one read drains both writes. So after every wake by a
  poster, `wakenUp` stays 1 with no wake pending, until the loop's next SELECT resets it.
- A post that lands between `hasTasks()` and that reset finds `wakenUp` at 1 and writes
  nothing. The loop resets the flag and blocks in `epoll_wait` with the post queued, for up to
  1 s. 4.1.0.Final checks the queue again inside `epollWait`, before it blocks.

Only the fix differs on the measured path. Between the two tags, `EpollEventLoop` differs by the
fix alone, and `SingleThreadEventExecutor`, `SelectStrategy`, `DefaultSelectStrategy`,
`AbstractScheduledEventExecutor`, the `MpscLinkedQueue` classes and `EpollEventArray` are
identical. `SingleThreadEventLoop` (registration), `Native.java` (library loading) and the
native C file (sendmmsg) differ off the measured path. (Read at both tags, 2026-10-29.)

Artifacts, unmodified, from Maven Central (lab request 1; sha1 and md5 as published):
- `netty-all-4.1.0.CR7.jar`, sha256 bce6ce004eb63cdb455f0da747c50aa75a2f80f4682b9537c531cbe4cd564ffa;
- `netty-all-4.1.0.Final.jar`, sha256 840f27fb93ffdf372f6f554bad726759891d538c41b4eaac60ee9e86c29b48bd.
Both hold the Epoll classes and `META-INF/native/libnetty-transport-native-epoll.so`, which
needs only `libc.so.6` and marks its stack non-executable (read with `readelf`, 2026-10-29). JDK:
Temurin 8u504-b01, Linux x64, `OpenJDK8U-jdk_x64_linux_hotspot_8u504b01.tar.gz`, sha256
9c70e102f527ac674ac2fe9c7d47b9a04e2d19842ba5ab8e9b33f368bbadfaea.

**A derived note on NIO, not run (2026-10-29).** `historical/candidates-2026-10.md` sketches a
forced NIO reproduction. Read again at the CR7 tag, it would not delay: its CONSUME step calls
`selectNow`, and CR7's `NioEventLoop.selectNow()` writes the wakeup again whenever `wakenUp` is
true, and a JDK `Selector` keeps a wakeup pending until its next selection. So on NIO the
stale flag never comes without a pending wakeup, except on rare paths (a raw `selectNow` after
256 cancelled keys, or a selector rebuild). The fix PR's author found no problem in NIO tests
but still called it possible, and Netty merged the NIO fix too (f44f3e7). The note is reported
as derived; the candidates file is left as it is.

## 2. Harness and settings

- One Java program under `historical/netty/` (first-party, compiled by the pinned JDK's
  `javac`) runs both arms; the arm is only the jar on the class path. One JVM process per run.
- One event loop: `new EpollEventLoopGroup(1, threadFactory, strategyFactory)` (public in both
  versions). The forced protocol passes its own `SelectStrategyFactory`; the natural protocol
  passes `DefaultSelectStrategyFactory.INSTANCE`. The forcing uses only this public API. One
  diagnostic (section 5) reads the private field `wakenUp` by reflection; it only reads.
- Delay of a post: `System.nanoTime()` in the task when it runs, minus `System.nanoTime()` in
  the posting thread just before `execute`. A post not run within 10 s fails its process (the
  hard limit of section 8).
- Bound and thresholds: B = 1 s, the loop's own bound when no task is scheduled; no task is
  ever scheduled. B_eff = B, a rule of this addendum (nothing here calibrates, so the fallback
  of section 2 does not apply as such). Basis: the predicted delays are about B on CR7's forced
  posts and microseconds on Final's, far from both thresholds. T_med flags a trial when the
  median of its 21 delays exceeds B / 5 = 200 ms; T_count when one of them exceeds B / 2 = 500 ms.
- JVM: `-XX:+UseSerialGC -Xms128m -Xmx128m -XX:+PrintCompilation`. It starts under
  `taskset -c 0` (core 0 holds the system and the runner, section 8), so its JIT and GC threads
  stay there. Each measured thread then pins itself with `taskset -p -c <cpu> <tid>`, the tid
  read from `/proc/thread-self`: the loop thread to CPU 14, every posting thread to CPU 2. A
  refused pin, or a thread seen at the end on another CPU (`Cpus_allowed_list` in
  `/proc/self/task/<tid>/status`), fails the process, as section 8.
- Timer slack stays at the default (50 us), where section 1 sets 1 ns: the JVM cannot set it
  without native code, and 50 us is small against both thresholds. Disclosed.
- Warm-up, unmeasured, in every process: 10 s of back-to-back natural posts (so the loop's code
  is compiled as on a busy server), then 5 posts of the process's own protocol.

## 3. Protocols

**Forced (disclosed as forcing, decision 6).** One forced post owns a slot of 1.1 s:
1. The loop is idle in `epoll_wait`. The main thread posts a task X, which wakes it normally.
   X runs on the loop and arms the hook.
2. On the loop's next `calculateStrategy` call with `hasTasks` false, the hook (on the loop
   thread) records the time, signals a helper thread, and blocks on a latch. The helper posts
   the measured task Y and counts the latch down after its `execute(Y)` has returned. The hook
   then records the time again, disarms itself and returns SELECT, as the default strategy
   would. The latch orders Y's enqueue before the loop's next look at its queue.
3. On CR7, Y was posted while `wakenUp` was still 1 from X's wake, so Y wrote nothing; the loop
   blocks in `epoll_wait` for up to 1 s (derived). On Final, `epollWait` finds Y in its new check
   and does not block (derived).
4. The next X is posted at the slot's end, and only once Y has run. The main thread and the
   helper block (park), they do not spin. CR7 runs Y about 1 s after the hook returns and then
   parks again, so with 1.1 s slots the loop is idle in `epoll_wait` when the next X comes.

The hook widens the natural window to certainty: it holds the loop between its look at the
queue and the flag reset while Y is posted.

**Natural (no hook).** The default strategy. One poster posts a task, spins on a volatile flag
the task sets until it has run, then posts the next one at once (gap 0, back-to-back). The
window of section 1 opens after every wake, and only a post that comes right after the
previous task can land in it, so the gap is 0. No hit rate is known beforehand. Realized gaps
(from the task's run time to the next `execute`) are reported.

**Trials and processes.** A trial is 21 posts. Each cell is 10 processes of 15 trials (150
trials), with process i using seed 2026102900 + i (the natural protocol draws nothing; the seed
names the process). Cells: CR7 forced, Final forced, CR7 natural, Final natural. The 40
processes run in one order, shuffled with seed 20261026.

## 4. Hypotheses

**H6a (forced, confirmatory).** On CR7, T_med flags more than 95% of forced trials. On Final,
T_med flags fewer than 5% of forced trials. Tests: exact one-sided binomial tests, as H1 and
H2; Holm over family F6 (m = 2, alpha 0.025). With n = 150 a cell rejects at the first Holm step
(0.0125) with at most 1 miss or false positive (p = 4.05e-3; 2 give 1.82e-2); if the other cell
rejected first, the second step (0.025) allows at most 2 (3 give 5.48e-2). Basis of n: the
forced posts are predicted to be all-or-none on each arm. A cell whose true per-trial miss rate
is 1% rejects with probability 0.56 at the first step and 0.81 at the second.

This is a forced window. It does not contradict `hypotheses-v2.md` section 4, which predicts
T_med blind to the natural lazy-wake race under protocol S.

Readings fixed now:
- If CR7's forced cell does not reject, section 5's per-post diagnostics decide between two
  causes: the state was not reached (the harness), or it was reached and the loop still woke
  (the derivation of section 1). Either is reported as found, with the cell's delays.
- If Final's forced cell does not reject, Final also delays forced posts. It is reported as
  found, with the same diagnostics.
- In neither case is the design changed after the fact.

**H6b (natural, exploratory).** No test. Predictions, stated now:
- T_med: blind on both arms (flags at most 5% of trials), as section 4 of `hypotheses-v2.md`
  predicts for the natural race.
- Posts delayed by more than B / 2: some on CR7, none on Final.
- T_count: flags some CR7 trials, no Final trial.
Reported per cell: the number of posts delayed by more than B / 2 with its exact 95% interval
(with zero hits over 3,150 posts the one-sided upper bound is 9.5e-4 per post), these posts by
trial index within their process, and the T_med and T_count flag rates with exact and
clustered intervals (the bootstrap of `hypotheses-v2.md` section 5). A Final post delayed by
more than B / 2 is reported as found.

## 5. Validity rules and diagnostics

- **A good process** exits 0, runs every post within the hard limit, has the forced hook inject
  exactly once per forced post, keeps every pin, logs no line at WARN or above from Netty
  (`java.util.logging` on stderr), raises no exception in the hook or in any task, and reports
  the expected Netty version (`Version.identify()` against the jar's file name). A failed process
  is run once more with the same seed; the rerun's data replace it and both are reported; a
  cell without 10 good processes is incomplete and counts as not rejected (section 8). The WARN
  rule is there because `run()` sleeps 1 s after any exception it catches, in both versions,
  which would mimic the defect.
- **Per forced post:** X's own delay; whether `hasTasks` was false at the hook; `wakenUp` read
  by reflection at the hook's entry (predicted 1 on both arms); the hook's return time; Y's
  delay. On CR7 each delay is predicted to end about 1 s after the hook returns.
- **Per process:** GC count and time per trial, the compiled methods (`PrintCompilation`), the
  largest delay (an EINTR can make a delay exceed B), and the Epoll availability check.
- The runs are Release-equivalent by construction: no debug agent and no assertions flag.

## 6. Order of work and the desktop rule

1. This addendum, a fresh Opus review, then Alex's freeze.
2. After the freeze: the harness, its runner, its gate (decision 37: `bench/coverage-h6.json`)
   and the scorer, committed with tests on synthetic output, and reviewed. On the desktop the
   harness runs only in a check mode that prints no time, and only against 4.1.0.Final, in the
   shared Linux image. Against CR7 it is only compiled; no CR7 post runs anywhere before the
   laptop run (its wall-clock time alone would show the delay).
3. The laptop: first the functional tests in check mode at the measured commit (decision 37),
   then the run, then scoring.

## 7. The native library

`libnetty-transport-native-epoll.so` from each jar must load on the laptop's Arch. A check-mode
run against Final, in the shared Linux image (glibc 2.44) and then on the laptop, shows it before
any timed run. If it does not load on the laptop, H6 is not run, and it is reported as not
runnable on this system: the jars and the library are not modified.

## 8. Laptop hours (no measured basis)

- Forced: 21 posts x 1.1 s = 23.1 s per trial; 15 trials, 5 warm-up posts and 10 s of natural
  warm-up per process, about 362 s; 10 processes per cell; two cells: about 2.0 h.
- Natural: on Final, about 21 x (a few us) per trial. On CR7 each hit costs about 1 s, so a trial
  costs at most about 21 s and a cell at most about 150 x 21 s = 52.5 min; plus 10 s of warm-up
  per process.
- JVM start and the shuffle's overhead are not measured, so the total of about 3 h is not a
  bound.

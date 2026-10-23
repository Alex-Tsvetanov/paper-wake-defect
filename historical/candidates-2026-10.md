# Bounded-wait missed wake: historical candidates (2026-10)

Searched 2026-10-23. Method: web search and web fetch only. Nothing was cloned, built or run. Every reproduction sketch below is "not executed".

Re-check (2026-10-23, second reader, candidate 1 only): read again at the source. At tag
netty-4.1.0.CR7, `run()` calls `calculateStrategy(selectNowSupplier, hasTasks())` and then
`select(wakenUp.getAndSet(false))`; `wakeup(boolean)` calls `selector.wakeup()` only when
`wakenUp.compareAndSet(false, true)` succeeds; `select()` blocks in `selector.select(timeoutMillis)`
with the deadline from `delayNanos`, which returns `SCHEDULE_PURGE_INTERVAL`
(`TimeUnit.SECONDS.toNanos(1)`) when no task is scheduled; the break condition after the select
includes `hasTasks()`. The fix commit f44f3e7 (Hyangtack Lee, 2016-05-16) adds a check to
`NioEventLoop.select` that runs `selectNow()` and leaves the loop when tasks are queued, and
touches no other file. Confirmed. Candidates 2 to 4 were not re-checked.
Mapping to the matrix: candidate 1 is a lazy-wake race (design/v2-defects.md, section 4).

Reading notes:
- All pages were read through a fetch tool that summarises the page. Code lines and quotes below are as that tool rendered them. Re-read at the URL before citing in a manuscript.
- The tool does not return line numbers. Where a line number appears it is approximate. Function and field names are the reliable locators.
- The ASF Bugzilla, ASF svn viewer and Boost Trac refused or failed (login wall, 401, connection refused). Claims that rest on them are marked "search snippet only".
- Release containment was checked with the read-only GitHub compare endpoint (api.github.com) and Maven Central directory listings.
- "Bounded delay" means: the queued work runs when the wait times out, nothing is lost.

## Ranking at a glance

| Rank | Candidate | Bound | Sub-class | Reproduce with a small program |
|---|---|---|---|---|
| 1 | Netty NioEventLoop (and EpollEventLoop), 4.1.0.CR6/CR7 and 4.0.36, fixed by PR 5258 | 1 s default, or time to next scheduled task | racy "already awake" flag (wakenUp), plus check-order regression | Yes, deterministic with the public SelectStrategy hook, unmodified jars from Maven Central |
| 2 | Tomcat NioEndpoint.Poller wakeupCounter, Bugzilla 51794, fixed 7.0.22 / 6.0.35 | selectorTimeout, 1000 ms default | racy counter lets poster skip the wake | Only as a stress test or a model of the loop, no hook |
| 3 | Asio epoll_reactor::update_timeout without timerfd, fixed in Asio 1.4.8 / Boost 1.46 | at most 5 minutes (or the earlier timer) | wake issued through the wrong primitive, edge-triggered source never drained (mechanism is a hypothesis) | Needs a shim to reach the no-timerfd path, result unverified |
| 4 | Qpid Proton reactor wakeup after reconnect, PROTON-1104, fixed 0.12.0 | 3141 ms (value not verified verbatim) | wake goes to a closed descriptor | Possible, fit to "bounded" is uncertain |

Not eligible, noted only: Tokio 8696 (open, no merged fix), Asio 320 (kernel bug). See the last sections.

---

## 1. Netty: NioEventLoop and EpollEventLoop, stale wakenUp after the SelectStrategy refactor

### Links
- Fix PR (both event loops): https://github.com/netty/netty/pull/5258 (title "Some EpollEventLoop tasks might be pended for a long time", author hyangtack, opened 2016-05-13, closed 2016-05-17, milestone 4.0.37.Final).
- NIO fix commit: https://github.com/netty/netty/commit/f44f3e7926f1676315ae86d0f18bdd9b95681d9f ("NioEventLoop ensures that all submitted tasks are executed immediately."). Authored 2016-05-16T03:01:19Z, committed 2016-05-17T05:43:46Z (GitHub compare API).
- Epoll fix commit: https://github.com/netty/netty/commit/877767da00f7ab93d401b4df7351040eb0c36a8b ("EpollEventLoop ensures that all submitted tasks are executed immediately."). It is the parent of the NIO fix commit and does not touch NioEventLoop.
- 4.0 backports: short hashes 013abb5 and ee9d8c9, as listed on the PR page. Not opened.
- Commit that introduced the window: https://github.com/netty/netty/commit/5d76daf33b645248a1cde150cbefbd3c24824f78 ("Allow to customize NIO (channel) select strategies.", authored by daschl, committed by Scottmitch, 2016-03-30 on the file history page).
- Later issue that cites the fix commit, still open and not a confirmed instance: https://github.com/netty/netty/issues/7719 (see "Leads checked").

### Buggy and fixed versions
- Buggy source for NioEventLoop: commit 877767da00f7ab93d401b4df7351040eb0c36a8b (NIO file lacks the new check; read via raw URL at that commit).
- Fixed source: commit f44f3e7926f1676315ae86d0f18bdd9b95681d9f.
- Buggy releases (Maven Central timestamps, https://repo1.maven.org/maven2/io/netty/netty-all/): 4.1.0.CR6 (2016-04-02), 4.1.0.CR7 (2016-04-10), 4.0.36.Final (2016-04-04).
  - CR7: run() read and shown buggy, and compare API says the fix commit is 71 commits after CR7.
  - CR6: SelectStrategy.java exists there (read). Its NioEventLoop was not read. It is older than CR7, so it is assumed buggy.
  - 4.0.36.Final: run() uses SelectStrategy and select() has no new check (read).
  - 4.1.0.CR5 (2016-03-29) probably predates 5d76daf (2016-03-30). Not checked.
- Fixed releases: 4.1.0.Final (2016-05-25; compare API: the fix commit is 31 commits behind this tag) and 4.0.37.Final (2016-06-07; select() has the check, read).
- Before the refactor (4.0.35.Final, 2016-03-21) run() cleared the flag first and then checked the queue. That version is a natural "good" control.

### The bug in plain words
Commit 5d76daf moved `wakenUp.getAndSet(false)` so that it runs after the task-queue check instead of before it. A thread that submits a task in the gap sees `wakenUp == true` from an earlier wake, so its compare-and-set fails and it never calls `Selector.wakeup()`. The loop then clears the flag and blocks in `selector.select(timeoutMillis)` with the task already queued. The fix re-checks the queue after the flag is cleared and does `selectNow()` instead of blocking.

Quote (commit message, read): "the task might be pended until select operation was timed out"

### Interleaving, derived from the CR7 source (not executed)
Preconditions:
- (a) `wakenUp` is still true from an earlier foreign wake, and the selector's own pending wakeup has already been consumed. Only the SELECT branch of run() resets `wakenUp`. A `selectNow()` clears earlier `wakeup()` calls (JDK 8 Selector Javadoc, read: "clears the effect of any previous invocations of the wakeup method").
- (b) A poster adds its task after the loop evaluated `hasTasks()` and before the loop executes `wakenUp.getAndSet(false)`.

Sequence on CR7:
1. Loop thread: `selectStrategy.calculateStrategy(selectNowSupplier, hasTasks())`. The argument `hasTasks()` is evaluated first and is false.
2. Poster thread: `execute(Y)` does `addTask(Y)`, then `wakeup(false)`: `if (!inEventLoop && wakenUp.compareAndSet(false, true)) selector.wakeup();`. The CAS fails because of (a). No wakeup.
3. Loop thread: strategy returns SELECT, then `select(wakenUp.getAndSet(false))`.
4. `select(...)` computes the deadline and calls `selector.select(timeoutMillis)`. Nothing between the flag reset and that call checks the queue.
5. After the timeout, the break condition in select() contains `hasTasks()`, so Y runs. Delay is at most the timeout.

### What bounds the park (read in source at tag netty-4.1.0.CR7)
- `NioEventLoop.select(boolean oldWakenUp)`: `selectDeadLineNanos = currentTimeNanos + delayNanos(currentTimeNanos)`, then `timeoutMillis = (selectDeadLineNanos - currentTimeNanos + 500000L) / 1000000L`, then `selector.select(timeoutMillis)`.
- `SingleThreadEventExecutor.delayNanos(long)`: returns `SCHEDULE_PURGE_INTERVAL` when there is no scheduled task, otherwise `scheduledTask.delayNanos(currentTimeNanos)`.
- `private static final long SCHEDULE_PURGE_INTERVAL = TimeUnit.SECONDS.toNanos(1);` So the default bound is 1 second. With a scheduled task pending the bound is the time to that task.
- Epoll variant: same structure; `EpollEventLoop.run()` calls `epollWait(WAKEN_UP_UPDATER.getAndSet(this, 0) == 1)` after the strategy, deadline from `delayNanos`, `Native.epollWait(epollFd.intValue(), events, timeoutMillis)`. Fix commit message: "until epoll_wait was timed out".
- URLs read:
  - https://raw.githubusercontent.com/netty/netty/netty-4.1.0.CR7/transport/src/main/java/io/netty/channel/nio/NioEventLoop.java
  - https://raw.githubusercontent.com/netty/netty/netty-4.1.0.CR7/common/src/main/java/io/netty/util/concurrent/SingleThreadEventExecutor.java
  - https://raw.githubusercontent.com/netty/netty/netty-4.1.0.CR7/transport-native-epoll/src/main/java/io/netty/channel/epoll/EpollEventLoop.java
- Line numbers: not verified.

### Hang or bounded delay
Bounded. The queued task is never lost. When `select(timeoutMillis)` returns, the break condition (`selectedKeys != 0 || oldWakenUp || wakenUp.get() || hasTasks() || hasScheduledTasks()`) is true because `hasTasks()` is true, and the next `runAllTasks()` drains it. The next poster after that wakes the loop normally.

### Sub-class
Racy "already awake" flag lets the poster skip the wake (wakenUp-style). It was introduced as a regression by moving the queue check before the flag publication.

### Toolchain and runtime
- JDK 8 recommended (not verified on newer JDKs; Netty 4.1.0 predates the module system).
- Artifacts, unmodified: `io.netty:netty-all:4.1.0.CR7` (buggy) and `io.netty:netty-all:4.1.0.Final` (fixed). Both directories exist on Maven Central: https://repo1.maven.org/maven2/io/netty/netty-all/4.1.0.CR7/ and https://repo1.maven.org/maven2/io/netty/netty-all/4.1.0.Final/ (CR7 jar 3,299,685 bytes, 2016-04-10).
- No special hardware or kernel feature for NIO. Runs where a JDK 8 runs, Linux or Windows (the JDK Selector implementation differs per OS; Windows behavior not verified).
- Epoll variant: Linux only, needs the native-epoll classifier jar. Not needed for the first experiment.

### Reproduction sketch (not executed)
Public API only. `SelectStrategy` and `SelectStrategyFactory` are public in CR6/CR7. `NioEventLoopGroup` has `(int, ThreadFactory, SelectorProvider, SelectStrategyFactory)` in both CR7 (constructor 6) and 4.0.36 (constructor 5), and `(int, Executor, SelectorProvider, SelectStrategyFactory)` in CR7.

Idea: the strategy callback runs on the loop thread between `hasTasks()` and `getAndSet(false)`, so it can widen window (b) without changing Netty. This is a window-widener and must be disclosed as such in the paper.

```
group  = new NioEventLoopGroup(1, threadFactory, SelectorProvider.provider(), () -> hookStrategy)
phase  = NORMAL | CONSUME | INJECT
hookStrategy.calculateStrategy(selectNow, hasTasks):
   NORMAL : return hasTasks ? selectNow.get() : SELECT          // same as DefaultSelectStrategy
   CONSUME: return selectNow.get()                              // clears the pending wakeup, wakenUp stays true
   INJECT : loop.schedule(noop, T ms)        // optional bound sweep, called on the loop thread
            helper.execute(Y).await()        // helper thread = a thread other than the loop thread
            return SELECT
main:
 1. start loop, run noop X0, sleep 300 ms so the loop idles inside select() with wakenUp == false
 2. phase = CONSUME; main thread runs noop X1
    (CAS false->true, selector.wakeup(); loop wakes; run() re-wakes the selector because wakenUp is true; X1 runs)
 3. loop iteration A (hasTasks false): CONSUME consumes the re-wake via selectNow; wakenUp is still true; phase = INJECT
 4. loop iteration B (hasTasks false): INJECT submits Y from the helper, its CAS fails, strategy returns SELECT,
    loop clears wakenUp, blocks in select(timeoutMillis)
 5. Y records (run time - submit time)
```
Measure: submit-to-run latency of Y.
Expected on CR7 (derived, not run): about 1000 ms with no scheduled task. About T ms when a scheduled task is T ms away (bound sweep: T = 100, 300, 2000 gives about 100, 300, 2000). Expected on 4.1.0.Final: under a few ms, because the new in-select check does `selectNow()` and breaks. The same harness on 4.0.35.Final (no SelectStrategy) is not directly runnable since the constructor differs; use plain stress there.

Natural (no hook) variant: several poster threads submit tiny tasks in bursts with random pauses; the loop thread alternates between busy passes (hasTasks true, `selectNow()` path, which keeps `wakenUp` true) and idle transitions. Record execute-to-run latency and look for outliers near 1000 ms on CR7 and none on Final. Probability per idle transition is small. From the PR page (summarised), hyangtack "found no problem in their nio test environment" and argued the NIO case was possible. Time to hit: not verified.

Fallback if the NIO reproduction does not behave: the Epoll variant (commit 877767d), Linux only.

### Verification status
| Claim | Status |
|---|---|
| NIO fix diff and message | read at https://github.com/netty/netty/commit/f44f3e7926f1676315ae86d0f18bdd9b95681d9f (page and .patch) |
| Epoll fix diff and message | read at https://github.com/netty/netty/commit/877767d |
| 5d76daf moved getAndSet after hasTasks | read at https://github.com/netty/netty/commit/5d76daf (page summary; commit date from file history page) |
| CR7 run() order and select() has no queue check | read in source at the CR7 raw URL above |
| 1 s default bound | read in source at the CR7 SingleThreadEventExecutor raw URL |
| Fixed code in 4.1.0.Final and 4.0.37.Final | read at raw URLs for those tags |
| 4.0.36 has SelectStrategy and no check | read at https://raw.githubusercontent.com/netty/netty/netty-4.0.36.Final/transport/src/main/java/io/netty/channel/nio/NioEventLoop.java |
| Release containment | read from compare API output and Maven Central listing |
| Interleaving and deterministic sketch | derived from source, not executed |
| Natural stress rate | not verified |
| JDK newer than 8, Windows behavior | not verified |
| Byte-exact quotes | not verified verbatim (tool rendering) |

---

## 2. Tomcat: NioEndpoint.Poller wakeupCounter (Bugzilla 51794)

### Links
- Bug: https://bz.apache.org/bugzilla/show_bug.cgi?id=51794 . Title from search snippet only: "Race condition in NioEndpoint$Poller causes socket to not be read until selectorTimeout". Reporter's patch idea (single `getAndSet(-1)`) and "fixed in 6.0.34 onwards" are also snippet only. Direct fetch hit a login wall.
- Trunk fix commit (git mirror of svn trunk r1170647): https://github.com/apache/tomcat/commit/58429a8c7fbf911c8323e76508968822d01782d5 . Parent: 4ac8d30c72db892d1f8e1fa7173afb6f8b21560a . Commit date on the mirror: 2011-09-14 (commit search listing; the parent has the same date per the compare API). Message: "Fix race condition in selector potentially causing a delay in select operations".
- Changelogs (read): https://tomcat.apache.org/tomcat-7.0-doc/changelog.html (entry "51794 ... Fix race condition in NioEndpoint. (fhanik)" under 7.0.22, released 2011-10-01; 7.0.21 released 2011-09-01) and https://tomcat.apache.org/tomcat-6.0-doc/changelog.html (entry "51794 ... Fix race condition in NioEndpoint selector" under 6.0.34, which is marked not released; 6.0.35 released 2011-12-05; 6.0.33 released 2011-08-18).

### Buggy and fixed versions
- Buggy code read at tag 7.0.21: https://raw.githubusercontent.com/apache/tomcat/7.0.21/java/org/apache/tomcat/util/net/NioEndpoint.java and at trunk parent 4ac8d30c72db892d1f8e1fa7173afb6f8b21560a.
- Fixed code read at tag 7.0.22: https://raw.githubusercontent.com/apache/tomcat/7.0.22/java/org/apache/tomcat/util/net/NioEndpoint.java and at trunk commit 58429a8c.
- 6.x: first released fix is 6.0.35. 6.0.33 presumed buggy (code not read).
- First affected version: not verified.

### The bug in plain words
The poller thread checks `wakeupCounter.get() > 0` and only then sets the counter to -1 and blocks in `selector.select(selectorTimeout)`. A worker that enqueues an event and increments the counter between the check and the set gets the value 1 back (not 0), so it skips `selector.wakeup()`. The poller then overwrites the counter with -1 and blocks with the event queued. The fix makes check and set one atomic step: `if (wakeupCounter.getAndSet(-1) > 0)`.

Quote (commit message, read): "Fix race condition in selector potentially causing a delay in select operations"

### Code at the buggy state (read, tag 7.0.21 and parent commit)
- `Poller.run()`: `events()` drains the queue; `if (wakeupCounter.get()>0) { keyCount = selector.selectNow(); } else { keyCount = selector.keys().size(); wakeupCounter.set(-1); keyCount = selector.select(selectorTimeout); }` then `wakeupCounter.set(0);`
- `Poller.addEvent(Runnable)`: `events.offer(event)`, then `if ( wakeupCounter.incrementAndGet() == 0 ) selector.wakeup();`
- The `selector.keys().size()` call sits inside the race window and widens it.

### What bounds the park
`NioEndpoint`: `protected long selectorTimeout = 1000;` (read at 7.0.21 and at the fix commit). `Poller.run()` passes it to `selector.select(selectorTimeout)`. Bound: 1000 ms, unless a connector config sets another value.

### Hang or bounded delay
Bounded: the blocked `select(selectorTimeout)` returns after 1 s and the loop comes back around to `events()`. The Bugzilla title (snippet) says the socket is not read "until selectorTimeout".

### Sub-class
Racy counter/flag lets the poster skip the wake.

### Toolchain and runtime
- JDK 6, 7 or 8 era code (Tomcat 7.0.21 targets Java 6; not verified which JDKs run it today).
- Tomcat 7.0.21 and 7.0.22 binaries or sources from the Tomcat archive (not checked).
- No special hardware.

### Reproduction sketch (not executed)
There is no pluggable hook, so the window cannot be widened without changing code.
- Product-level: run the 7.0.21 NIO connector (`Http11NioProtocol`), keep-alive clients over many connections, and a handler that finishes quickly. Record per-request latency. Outliers near `selectorTimeout` indicate the race. Repeat with `selectorTimeout` set to 250 and 2000 as a bound sweep. Expect no outliers on 7.0.22. Hit rate: not verified.
- Model-level (not the project): copy the 6-line select section and `addEvent` quoted above into a 40-line harness with one poller and N posters, then count delays above the timeout. This is a model of the code, not the product, and should be labelled so.

### Verification status
| Claim | Status |
|---|---|
| Fix diff (check-then-set to getAndSet(-1)) | read at https://github.com/apache/tomcat/commit/58429a8c7fbf911c8323e76508968822d01782d5 |
| Buggy and fixed code at 7.0.21 and 7.0.22 | read at the raw URLs above |
| Default selectorTimeout 1000 | read in source at 7.0.21 |
| Fix versions 7.0.22 and 6.0.34/6.0.35 | read in changelogs |
| Bugzilla title, reporter's analysis | search snippet only |
| Reproduction | not executed |

---

## 3. Asio: epoll_reactor::update_timeout without timerfd (Asio 1.4.8 / Boost 1.46)

### Links
- Fix commit: https://github.com/chriskohlhoff/asio/commit/2f0c34246a0721f6630eb61c40e66d2829f7a367 ("Use correct interrupt method when timerfd is not available."). Parent (buggy): https://github.com/chriskohlhoff/asio/commit/0554900ad44aec2b93f604f5de8a0e24d9087dc0 . Date: 2011-02-08 (commit search listing). One file, +1 -1, in `asio/include/asio/detail/impl/epoll_reactor.ipp`.
- Release note (read via fetch tool): https://www.boost.org/doc/libs/1_46_1/doc/html/boost_asio/history.html , section "Asio 1.4.8 / Boost 1.46", a bullet about older Linux kernels where epoll is used without timerfd support, preventing timely delivery of `deadline_timer` handlers after the program has run for some time, ticket #5045.
- Ticket #5045 itself (Boost Trac): fetch failed (connection refused). The link between commit 2f0c342 and #5045 is my inference from the matching area and date. Not confirmed.

### The bug in plain words
In `epoll_reactor::update_timeout()`, when `timer_fd_ == -1` (no timerfd), the code ends with `interrupter_.interrupt();`. Everywhere else the reactor wakes the loop with `epoll_reactor::interrupt()`, which does `epoll_ctl(EPOLL_CTL_MOD, ...)` on the edge-triggered interrupter descriptor. The reactor never drains that descriptor (comment in `run()`: "No need to reset the interrupter ..."), and it relies on the MOD to produce a new edge. The fix replaces the direct write with `interrupt()`.

### What bounds the park (read at the buggy parent, via summary)
- `epoll_reactor::run(bool block, ...)`: with `timer_fd_ == -1`, `timeout = block ? get_timeout() : 0;` then `epoll_wait(epoll_fd_, events, 128, timeout)`.
- `get_timeout()` without timerfd: `timer_queues_.wait_duration_msec(5 * 60 * 1000)`; comment "By default we will wait no longer than 5 minutes." The bound is the earliest timer already in the queue, capped at 5 minutes.
- `schedule_timer()` calls `update_timeout()` only when the new timer is the earliest.
- Line numbers: not verified.

### Hang or bounded delay
Bounded by the `epoll_wait` timeout computed before the new timer was added (at most 5 minutes). The queued timer is serviced when `epoll_wait` returns.

### Sub-class
Wake issued through the wrong primitive, and an edge-triggered source that is never drained so a plain write may not produce a new edge. Which of these actually loses the wake is a hypothesis: the descriptor may be a pipe, and `pipe_select_interrupter::interrupt()` writes one byte to a non-blocking pipe and discards the result (`(void)result;`, read). A pipe that is never read fills up, and then writes fail silently. That would fit "after the program has run for some time" in the release note. Not verified. With an eventfd interrupter I would expect the plain write to still wake the loop on common kernels. Not verified.

### Toolchain and runtime
- C++ compiler, header-only Asio at commit 0554900ad44aec2b93f604f5de8a0e24d9087dc0 (namespace `asio`, `io_service`).
- The buggy line is only reachable when `timer_fd_ == -1`. `config.hpp` at that commit defines `ASIO_HAS_TIMERFD` only for glibc 2.8 or newer and has no `ASIO_DISABLE_TIMERFD` macro (read). So on glibc the line is unreachable unless a shim makes `timerfd_create` fail (for example LD_PRELOAD), or the build uses a libc without `__GLIBC__` such as musl. Use `-DASIO_DISABLE_EVENTFD` to select the pipe interrupter.
- Linux only. Result on a current Arch kernel: not verified. Pipe wake behavior changed across kernels (see Asio issue 320).

### Reproduction sketch (not executed)
1. Thread R: `io_service.run()` with a `deadline_timer` T0 pending for 60 s (so `epoll_wait` blocks with about 60 000 ms).
2. Main thread, after 1 s: new `deadline_timer` T1, `expires_from_now(100 ms)`, `async_wait(handler)`. This calls `schedule_timer` -> `update_timeout` -> `interrupter_.interrupt()`.
3. Measure when `handler` runs. Buggy expectation if the wake is lost: about 60 s. Fixed expectation: about 100 ms. Bound sweep: T0 = 5 s, 20 s, 60 s.
4. If step 3 shows no delay on the first try, test the saturation hypothesis: with the pipe interrupter, schedule a long series of strictly earlier timers (tens of thousands, more than the pipe capacity) from the main thread, then measure the last one.

### Verification status
| Claim | Status |
|---|---|
| Diff of 2f0c342 | read at the commit URL |
| epoll_reactor behavior at parent (ET interrupter, no reset, 5 minute cap, timerfd fallthrough) | read in source at https://raw.githubusercontent.com/chriskohlhoff/asio/0554900ad44aec2b93f604f5de8a0e24d9087dc0/asio/include/asio/detail/impl/epoll_reactor.ipp (via summary) |
| Pipe/eventfd interrupt() ignore write result | read in source at the sibling files at that commit |
| Release note text | read on the Boost 1.46.1 history page (via summary) |
| Commit 2f0c342 fixes ticket 5045 | inferred |
| Failure mechanism (pipe saturation or edge loss) | hypothesis, not verified |
| Reproduction on a modern kernel | not verified |

---

## 4. Qpid Proton reactor: wakeup after reconnect (PROTON-1104)

### Links
- JIRA: https://issues.apache.org/jira/browse/PROTON-1104 . Read: title "reactor hangs on reconnect", type Bug, Blocker, affects proton-0.10 and 0.11.1, fix version 0.12.0, created 2016-01-24, resolved 2016-01-26, attachment testcase.c (not read). Reported error: "pn_reactor_wakeup: : Bad file descriptor".
- Fix commit: https://github.com/apache/qpid-proton/commit/8c6c7c53198f4152472572b8fccc5ab453d9ca86 ("PROTON-1104: Delay releasing timer and IPC pipes until pn_reactor_stop()"). Parent (buggy): https://github.com/apache/qpid-proton/commit/72b5f95323cd316011b0aa6dce27a51ebffd85e9 . One file, `proton-c/src/reactor/reactor.c`, +12 -29.
- Red Hat Bugzilla: https://bugzilla.redhat.com/show_bug.cgi?id=1302033 (read): reactor "fails to respond or responds slowly when a periodic timer is in use", and the wakeup is processed only when the reactor timer expires.

### The bug in plain words
The reactor's wakeup pipe is watched through its timer selectable. When the reactor ran out of work, that selectable was terminated and its finalizer closed both pipe ends (`pni_timer_finalize`). If the application then continued with a new connection, `pn_reactor_wakeup()` wrote to a closed descriptor and failed with EBADF, so the reactor thread never got the wake. The fix moves the teardown to `pn_reactor_stop()`. The mechanism is inferred from the diff and the error text. The JIRA comments were not visible.

### What bounds the park
- `pni_handle_quiesced()` in `proton-c/src/handlers/iohandler.c` calls `pn_selector_select(selector, pn_reactor_get_timeout(reactor));` (read at the parent commit, via summary).
- `pn_reactor_run()` in `reactor.c` sets the reactor timeout to 3141 (ms) (read via summary, value not verified verbatim). A periodic timer selectable would bound the wait further.
- File and function names are the locators. Line numbers: not verified.

### Hang or bounded delay
Uncertain. The JIRA title says "hangs". The Bugzilla text says slow when a periodic timer is in use. If the select timeout of 3141 ms always applies, the delay is bounded; I did not confirm that the select loop re-runs in the reconnect scenario. Treat the fit as unproven until testcase.c is read.

### Sub-class
The wake signals an object the wait does not watch (closed descriptor).

### Toolchain and runtime
C compiler and CMake; proton-c 0.11.1 or the parent commit. Python bindings not needed. No special hardware. Windows build path not checked.

### Reproduction sketch (not executed)
Use the attached testcase.c from PROTON-1104 as the starting point (not read). Reactor thread runs `pn_reactor_run`; a peer closes the first connection; the application opens a new connection on the same reactor; another thread calls `pn_reactor_wakeup()` repeatedly and records return codes and the time until the reactor handler runs. Compare parent and fix commits.

### Verification status
| Claim | Status |
|---|---|
| JIRA fields, error text | read at the JIRA URL |
| Fix diff | read at the commit URL |
| pn_selector_select with reactor timeout | read at the parent commit (iohandler.c) |
| 3141 ms default | read via summary, not verified verbatim |
| Mechanism (closed wakeup pipe after teardown) | inferred from diff |
| Bounded rather than hang | not verified |

---

## Noted, not eligible

### Tokio 8696: time::advance() does not wake a parked driver (open)
- Issue: https://github.com/tokio-rs/tokio/issues/8696 , opened 2026-10-22, labels C-bug, M-time. Open PR with a fix: https://github.com/tokio-rs/tokio/pull/8699 (open, waiting on review). No merged fix and no fixed commit yet.
- From issue text only: with the `test-util` feature, a paused current-thread runtime, and a `spawn_blocking` task that inhibits auto-advance, the driver parks for the timer's remaining time in real time. `advance()` and `resume()` change the clock but do not unpark the driver. Reported real elapsed time 8.19 s for a 10 s sleep, on every run. Bound is the timer wheel's next expiration, so it is bounded.
- Reported on tokio v1.54.1 and master, Linux. Reproducer is a short Rust program (paused runtime, spawn_blocking that calls `advance(10s)`, main awaits `sleep(10s)` and prints real elapsed time).
- Not eligible now: not historical, only the test-util paused clock, no merged fix. Worth re-checking once PR 8699 lands. If it merges, the commit before the merge is a clean Tokio-based bounded-delay case ("wake never issued" sub-class). Everything here is "from issue text only".

### Asio issue 320: epoll_reactor misses wakeups on Linux 5.3 and later
- https://github.com/boostorg/asio/issues/320 . Reporter blames a kernel bug in the edge-triggered eventfd path after `EPOLL_CTL_MOD`. Closed without a linked fix. One commenter reports a program that hangs in `epoll_wait()`. It reads as a kernel regression, reported as hangs, and cannot be reproduced at a project commit. Not eligible.

---

## Leads checked and rejected

| Lead | Outcome |
|---|---|
| Netty f44f3e7 | Qualifies. See candidate 1. Its fix commit is also cited by Netty issue 7719. |
| Netty "nextWakeupNanos" era, PR 9586 https://github.com/netty/netty/pull/9586 | Eventfd shutdown/wakeup race tracked through epoll edges. Fixes issue 9362 (shutdown). Not a steady-state bounded delay. Follow-up PR 9614 notes that a redundant `epollWaitNow()` in `closeAll()` could miss a wakeup during shutdown, with minor effect "because of a one-second safeguard timeout" (page summary). Near miss, shutdown only. |
| Netty issue 7719 https://github.com/netty/netty/issues/7719 | Open since 2018-02-14. Reporter saw writes delayed about the idle timeout on 4.1.17.Final. Maintainer says writes from outside the loop are not propagated until a flush wakes the selector, "just how it works". Not a confirmed missed wake. |
| Netty issue 10796 | Loop stuck in `epollWait` with timeout -1. Root cause not identified, maintainers dispute the race. Hang, not bounded. |
| Netty issue 8986 | Question about the 1 s epoll timeout. No missed wake. |
| Netty issue 14368 (fixed by PR 14425) | EINTR retry of `epoll_wait` restarts the full timeout, so scheduled tasks are late or never run under frequent signals. Timer deadline bug, not a lost wake. |
| libuv `uv_async_send` pending flag | joyent/libuv issue 1453 (https://github.com/joyent/libuv/issues/1453) is a Windows coalescing case, closed with a documentation change. libuv issue 2949 is scheduling latency on a pinned single core (sched_yield), not a missed wake. No qualifying libuv bug found. |
| Tokio PR 1788 / issue 1768 | Permanent hang (lost wakeup in the scheduler). Excluded as the same class as issue 6996. |
| Tokio issue 525 | Reported as hangs and deadlock. Excluded. |
| Tokio issue 6536, PR 4383 | Seen in search results only. Not read. |
| Go netpoller and timers | Searched issues and commits (wakeNetPoller, netpollBreak, pollUntil). Found no bounded-delay missed-wake fix. A golang-nuts thread mentions a NetBSD kernel bug that can miss netpollBreak (search snippet only, thread not opened). Issues 43997 and 44313 are scheduler spinning and GC worker issues. Not exhaustive. |
| glib GMainLoop | GNOME issue 1179 is an API gap for waking a context from another thread, not a timed-poll delay. A Debian patch reverting glib wakeup commits and a later wakeup rework were seen as search snippets only, not read. Nothing established. |
| Qt event dispatcher | QTBUG-85981 (posted events stuck with nested loops on Windows) redirected and was not read. Nothing established. |
| mio Waker | Search listed issue 2026 (waker events not readable on NetBSD, OpenBSD, DragonFly). Title only. Not read. |
| folly EventBase, Seastar, .NET ThreadPool/IOCP, ForkJoinPool/ThreadPoolExecutor, Python asyncio/uvloop | Searched, no matching bounded-delay missed-wake fix found. ForkJoinPool results (JDK-8078490, JDK-8330017) are about tasks never running, not bounded. A CPython/Codex report where a sandbox blocks the self-pipe write is environmental. Not exhaustive. |
| HAProxy commit 13afcb7 ("task: fix possibly missed event in inter-thread wakeups") | Missed wake between threads. The page did not state whether a poll timeout bounds it. Not pursued. |

## Recommendation

Pick candidate 1, the Netty `NioEventLoop` regression (buggy source at commit 877767da00f7ab93d401b4df7351040eb0c36a8b or release 4.1.0.CR7, fixed at f44f3e7926f1676315ae86d0f18bdd9b95681d9f or release 4.1.0.Final).

Why:
- It is a bounded delay by construction: the park is `selector.select(timeoutMillis)` with a 1 s default read in source, and the queued task runs when it returns.
- The cause is a one-line ordering change introduced on a known date (commit 5d76daf), and the fix is a small, readable re-check. A pre-regression release (4.0.35.Final) exists as a control.
- The race window sits between two statements of `run()`, and the public `SelectStrategy` hook runs exactly there on the loop thread. That makes the interleaving deterministic with unmodified jars from Maven Central, and the same harness on 4.1.0.Final should show the fix. The bound can be swept by scheduling a dummy task at T ms.
- Needs only a JDK 8 and the two jars; no kernel feature or special hardware.

Risks to carry: the interleaving is derived from source and not yet run; the hook widens the window and must be disclosed; the natural, hook-free rate is unknown and the PR author reported no failure in an NIO test environment.

Second choice: Tomcat 51794. Same shape and verified at 7.0.21 versus 7.0.22, but no hook, so only a stress test or a model of the loop.

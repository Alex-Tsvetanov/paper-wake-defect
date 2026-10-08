# Hypotheses (frozen before the publication runs)

Setup: one worker parked in the backend's wait with bound B; a second thread posts a task;
the measured quantity is the delay from post to execution. The phase of a post is the time
since the worker last parked. B_eff is the observed period of the worker's timed wakeups:
equal to B on Linux, and B rounded up to the timer resolution in effect on Windows.

H1. With the wake defect present, each post's delay equals B_eff minus (phase mod B_eff).
    Accepted if the median absolute residual is below 5% of B_eff at B = 1, 10 and 100 ms.
H2. With the fix, the delay does not depend on B. At a fixed gap of 2 ms, the ratio of the
    median delays for any two bounds in {1, 10, 100 ms, blocking} lies within [0.8, 1.25].
H3. A test that posts 21 times and compares the median delay with B_eff/5 flags every
    defective backend and passes every fixed backend, at B = 1 ms and B = 100 ms.
H4. The fix costs no measurable throughput when the worker is busy, measured as a paired
    A/B on the lab laptop (95% CI of the throughput ratio within [0.98, 1.02]).

H5 (exploratory, mechanism for H2). On L, holding a PM QoS CPU latency request of 0 us
    (/dev/cpu_dma_latency) during the run removes the dependence of the fixed-arm delay
    on B: the max/min ratio of the medians over {1, 10, 100 ms, blocking} falls to 1.25 or
    below. Not tested on W (no per-process equivalent without changing system settings).

Backends: epoll and io_uring on Linux (laptop L), IOCP on Windows (desktop W).
Recorded factors: Linux timer slack; Windows timer resolution obtained by the process.

## Revision log

- 2026-09-25: first version.
- 2026-09-26, after the smoke run and before any publication run: the prediction now uses
  each post's measured phase and B_eff instead of the nominal gap, because the gap sleep
  overshoots and Windows rounds waits up to its timer tick. Independence from B is tested
  at a fixed gap (H2), because in the first design the gap and B changed together. H3's
  threshold uses B_eff. The throughput criterion in H4 is now explicit.
- 2026-09-26, after the second smoke run and before any publication run: the smoke data
  contradicts H2 on every backend. H2 is kept unchanged and will be tested as written.
  H5 is added as an exploratory test of one candidate mechanism (core idle depth).
- 2026-09-26: H4 not run for this paper; deferred.
- 2026-09-26, after the publication runs and a review of the draft: an exploratory design,
  not a hypothesis, measures H3's test with each gap drawn at random from [0.1B, 3.0B]
  (bench/run_matrix.sh, design random-gap). H1 to H5 above are unchanged.
- 2026-09-29, before any record or run on the new implementation: the measured
  implementation is now this repository's minimal event loop (`loop/`), with one backend per
  wait interface (epoll, io_uring, IOCP) and a compile-time switch that seeds the wake
  defect. The earlier implementation is withdrawn, and none of its data is used. In H1 and
  H3, "the wake defect" is the loop built with the switch on: epoll omits the wake, io_uring
  writes an eventfd that its ring does not watch, and IOCP omits the completion packet. In
  H2 and H4, "the fix" is the loop with its wake compiled in. H1 to H5, their thresholds, the
  designs and the random-gap design are unchanged; H4 stays deferred.

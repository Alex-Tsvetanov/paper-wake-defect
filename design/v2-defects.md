# P1 v2: six defects, a condition-variable backend, worker counts and H4

Status: **draft, not frozen**. Written 2026-10-23 on the desktop (development only). It becomes
frozen only after Alex approves it together with `hypotheses-v2.md` and the laptop hours.
`design/minimal-loop.md` (the 2026-09 design) stays frozen and is not edited; this note extends
it. Section numbers "v1 2.3" and so on refer to that note.

## 1. What stays from v1, and the park handshake (Alex, 2026-10-24)

- The backends' system calls (v1 2.4 to 2.6): epoll with an edge-triggered wake eventfd and
  `epoll_pwait2`; io_uring on raw system calls with a multishot poll on the wake eventfd,
  single issuer and deferred task run; IOCP with a wake packet; a stop path of its own in each.
- The v1 wake rule (v1 2.3): a post wakes its worker when its push found the stack empty. In v2
  that rule is the variant **plain wake**, kept as H4's reference arm. With one worker it issues
  the same system calls in the same order as v1's fixed arm (io_uring re-arms its wake poll
  after the stop check, as v1 did). One behaviour differs in every variant: the core checks the
  stop flag after a backend's start as well as before it, so on epoll and IOCP a stop that lands
  between the two checks skips the first drain, which v1 would have run.
- **The fixed loop adds the park handshake** (D2: the H4 engineering probe on the laptop showed
  that the plain wake costs throughput under a busy worker on epoll and IOCP). Each worker has
  a flag, sleeping. Before every wait the worker stores sleeping = true (seq_cst), re-checks its
  stack (seq_cst load), waits only if the stack is empty, and clears the flag after. A post
  pushes (seq_cst compare-and-swap) and, if the push found the stack empty, wakes the worker
  only if it loads sleeping = true (seq_cst). Of the worker's store and load and the poster's
  push and load, all seq_cst, at least one side sees the other: either the re-check finds the
  post or the post finds the sleeper. A post made while the worker runs therefore costs one
  load and no system call. The handshake runs on every pass, also when the wait returns at once
  (poll mode): a loop does not know beforehand that its wait will not block.
- Two v2 rows equal the two v1 defects in their sites: **absent wake** on epoll and IOCP is v1's
  "omit", and **unobserved primitive** on io_uring is v1's "misdirect" (eventfd written, wake
  poll never armed). They now sit on top of the handshake.

## 2. Structure

- `Loop<Backend, D, Hooks>` (`loop/include/wakeloop/loop.hpp`) holds the core: W workers, each
  with its own task stack and its own backend object (wake object and wait). The backend is a
  class template on the defect too, defined in its own source file and explicitly instantiated.
- Every defect is a compile-time deviation at named sites (`if constexpr`). No defect code
  path runs in the fixed instantiation, so an A/B between variants compares exactly the sites.
  The fixed instantiation still carries a few defect-only members that it never uses: the
  spare descriptor or port handle (left empty), the condition variable backend's spare mutex,
  condition variable and predicate, and the undrained-edge leftover pointer. Both arms of an
  A/B carry the same members.
- One binary instantiates every variant on every backend of the platform. `variants.hpp`
  dispatches by backend name and variant name at run time. That removes the build-layout noise
  of separate builds (memory: iteration grids need same-arm builds).
- `Hooks` is empty in every measured instantiation (`NoHooks`, `[[no_unique_address]]`). The
  tests use one hook, called between the two steps of the worker's park, to force the
  lazy-wake race (section 4).
- Wait modes: bounded (B > 0), blocking (B = 0, the infinite-bound baseline), and poll (the
  wait returns at once; H4's busy worker, section 6).

## 3. Workers and threads

- W workers, each the single consumer of its own stack. A post names its worker (`post_to`);
  `post` is worker 0. The episode argument of v1 2.3 holds per worker unchanged, because each
  stack still has one consumer and each wake object one waiter.
- W = 1 of plain wake is the v1 loop. Wrong worker exists only for W >= 2 (at W = 1 the
  "other" worker is the same worker).
- Posters P are separate threads of the harness. Proposal for the campaign: W in {1, 2, 4} with
  one poster for the latency test, posts spread round robin over the workers. Alex decides.

## 4. The six defects

| Defect | Where | Site | epoll | io_uring | IOCP | condvar |
|---|---|---|---|---|---|---|
| absent wake | core, `post_to` | the wake call is omitted | v1 omit | new | v1 omit | new |
| unobserved primitive | backend, `wake` | the post signals an object the wait does not watch | a second eventfd outside the epoll set | v1 misdirect: eventfd written, poll never armed | a packet to a second port | a second condition variable and predicate |
| lazy-wake race | core, park | the worker looks at its stack before it announces that it parks, not after | yes | yes | yes | yes |
| wrong worker | core, `post_to` | the push goes to worker i, the wake to worker (i + 1) mod W | W >= 2 | W >= 2 | W >= 2 | W >= 2 |
| no re-trigger | backend, registration | the wake registration is one-shot and never re-armed | `EPOLLONESHOT` | single-shot poll, re-arm skipped | N/A | N/A |
| undrained edge | core, drain | the worker runs at most K = 64 tasks per pass, keeps the rest and parks with the full bound | yes | yes | yes | yes |

- **N/A cells.** IOCP wakes with a posted packet and the condition variable with a notify.
  Neither has a registration to re-arm, so no re-trigger has no natural site there. No plant
  is contrived (D2).
- **Lazy-wake race.** The handshake done in the wrong order: the worker looks at its stack, finds
  it empty, and only then announces that it parks. A post in between pushes onto the empty stack,
  finds no sleeper, skips its wake, and the worker then waits with the post queued. This is the
  shape of the Netty case (`historical/candidates-2026-10.md`: the queue check before the flag
  reset). The natural window is a few instructions wide. The tests force it with the hook (the
  worker stops between its look and its announcement while the test posts). The measured
  instantiation has no hook. The campaign measures the natural window and a plant widened by a
  stated delay (section 9); the widened plant is not built yet.
- **Undrained edge.** Edge-triggered sources must be drained until empty; reading part of the
  data and then parking leaves no new edge for the rest. Here the "edge" is the wake rule's
  empty to non-empty transition, and the leftover runs only at the next return of the wait.
- The condition-variable backend wakes by setting a predicate under the mutex and notifying.
  Its wait is `wait_for` with the predicate, so spurious returns re-wait for the rest of B.
  A notify without the predicate under the mutex is a lazy-wake race of its own; it is a
  candidate seventh row, with the frame-ownership race, if Alex wants backend-specific races.

`tests/defect_sites.cmake` (X1 v2) counts the `if constexpr` lines that name each defect in
each source and fails on any count other than this table: `loop.hpp` 2 for plain wake (post and
park) and 1 each for absent wake, lazy-wake race, wrong worker and undrained edge; `epoll.cpp`
2 unobserved (create, signal) and
1 no re-trigger; `uring.cpp` 1 unobserved and 2 no re-trigger (single-shot arm, skipped re-arm);
`iocp.cpp` 2 unobserved; `condvar.cpp` 1 unobserved. No other loop source names a defect.

## 5. The wake matrix (functional tests)

Detectors with a blocking wait (the infinite-bound baseline; a missed wake is a hang, caught
by a 1 s deadline): D1 one post; D2 64 posts back to back; D3 4 threads x 10,000 posts; D4 a
task posts a task; D6 10 posts in sequence; D7 192 posts made while a gate task holds the
worker; D8 the forced park interleaving (a task arms the hook; the worker stops in its next
park between the two steps of the handshake; the test posts; the hook lets the worker go on).

Detectors with B = 200 ms (the paper's test: a median delay above B / 5 is a missed wake; the
detectors also print the count of posts slower than B / 2): D5 the v1 test, 21 posts; D9 seven
rounds of 192 posts behind a gate, the slowest task of each round; D10 five forced park
interleavings.

Expected verdicts, derived from the handshake before any run (D detected, P passes, A racy and
not asserted; CTest fails on any other outcome). Wrong worker is run at W = 2; every other row
at W = 1.

| Variant | D1 | D2 | D3 | D4 | D6 | D7 | D8 | D5 | D9 | D10 |
|---|---|---|---|---|---|---|---|---|---|---|
| fixed | P | P | P | P | P | P | P | P | P | P |
| plain wake (2026-09 loop) | P | P | P | P | P | P | P | P | P | P |
| absent wake | D | D | D | D | D | D | D | D | P | P |
| unobserved primitive | D | D | D | D | D | D | D | D | P | P |
| lazy-wake race | P | A | A | P | P | P | D | P | P | D |
| wrong worker (W = 2) | D | D | D | D | D | D | D | D | P | P |
| no re-trigger | P | A | A | P | D | P | P | D | P | P |
| undrained edge | P | P | A | P | P | D | P | P | D | P |

Why the handshake changes the table. A worker re-checks its stack before it parks, so a post
made while the worker runs needs no wake. The defects that lose wakes therefore pass D9 and D10,
whose posts land behind a running task, and no re-trigger passes D4 and D7 for the same reason.
Before any detector posts, each worker runs one starter task posted before `run()` and the
workers settle, so a slow thread start cannot let the first drain pick up the detector's post.
Starters find no sleeper and send no wake, so no re-trigger's one-shot is still unused when a
detector starts: D1 passes on its first wake and D6 catches the second. Every applicable cell
is still caught by at least one bounded detector: D5 for absent wake, unobserved primitive,
wrong worker and no re-trigger; D10 for the lazy-wake race; D9 for undrained edge. The semantics
tests of v1 (S1 to S11) and S12 (each worker runs its own posts) pass in every variant: with a
bound, every defect is a delay and never a loss.

## 6. H4: the throughput cost of the fix

- v1's H4 (deferred) reads "the fix costs no measurable throughput when the worker is busy".
  In v1 "the fix" is the wake compiled in. In v2 (D2, Alex 2026-10-24) "the fix" is the wake
  with the park handshake: arm A is the fixed loop, arm B is absent wake (Alex, 2026-10-23).
- "Busy" means the worker's wait returns at once because other completions are ready. The
  harness models that with the poll wait mode in both arms; posted work rides on those returns
  and absent wake loses nothing. In poll mode every variant calls its wait on every pass, as a
  busy loop collects its other completions every pass, and every variant but plain wake runs
  the handshake on every pass. So arm A and arm B differ only in the wake decision: arm A loads
  the flag after a push onto an empty stack and wakes when the flag is set (a post that lands
  while the worker is in its poll); arm B does neither. Absent wake is still "the fixed loop
  without its wake", a one-site change as Alex chose; since D2 that loop includes the handshake.
  The reference comparison is descriptive, not a hypothesis: plain wake against absent wake
  differs by plain wake's wake on every push onto an empty stack and by absent wake's
  handshake (one seq_cst store and load per pass). With a bounded or blocking wait the
  absent-wake arm would stall, which measures the defect, not the fix.
- The engineering probes of 2026-10-23 and 24 and of 2026-10-25 (laptop, not citable) were read
  before the freeze, the first to decide D2; `hypotheses-v2.md` discloses both. H4 keeps the
  2026-09 criterion (the 95% interval of the throughput ratio within [0.98, 1.02]), fixed before
  any data. Its workload (task work, producers, tasks in flight) was not fixed in 2026-09, so it
  must be fixed before any further throughput comparison, on a basis independent of the probes.
- **H4's workload (Alex, 2026-10-25):** the decade grid of per-task CPU work, N in {0, 1, 10,
  100} us (`abpair --work-ns`), with W = 1, P = 1, K = 64, 8 pairs of 10 s windows per cell, and
  the whole curve reported, not one chosen point. The grid is set before any further throughput
  data and does not depend on the probes' ratios; the decision to vary task work came after them,
  and the paper says so. Under empty tasks a batch runs in about the time of one poll call, so the
  worker's flag is set for much of every pass and most posts pay a wake; task work shrinks that
  share.
- `bench/abpair.cpp`: P producers keep K tasks each in flight on W workers; each window runs
  one arm; pairs are ordered ABBA (or by a seeded shuffle); one process holds both arms. The
  output is one record per window with the arm, the tasks completed and each worker's passes.
  The loop does not count wakes issued: a counter would add code to the fixed loop's post path.
  `--check` leaves the pass counts out too: in poll mode they grow with elapsed time.
- Unobserved primitive on IOCP is a poor abpair arm: its wake packets go to a port that no
  one dequeues, so in a long timed window that port's queue grows without bound. Throughput is computed only on the laptop. On the desktop the program runs only
  `--check`, which uses count-based windows and prints counts and the pair order, never a time.
- Engineering note (D2). The probe showed the plain wake costs throughput under a busy worker
  on epoll and IOCP, so the fixed loop took the standard remedy, the park handshake of
  section 1, with Alex's yes (2026-10-24). That remedy is exactly where lazy-wake races live,
  which is why the lazy-wake row now plants its error in the handshake.

## 7. wakeprobe

wakeprobe selects the backend, the variant and W at run time. The single-post latency protocol
is blind to the lazy-wake race (unforced) and to undrained edge by construction; the campaign
needs the batch and forced protocols of D9 and D10 in wakeprobe as well. That is open work.

## 8. Comparison with ThreadSanitizer

A lost wake is not a data race: every shared access in the loop is atomic or under a lock.
The prediction is that TSan reports nothing on any variant. A development TSan run of the
matrix on the desktop checks that the code is race-free; a citable TSan record is made on the
laptop.

## 9. Decisions (Alex, 2026-10-23)

The design is still a draft; these answers shape it before the freeze.

1. The six definitions and the N/A cells above: accepted as drafted.
2. Lazy-wake race in the campaign: both the natural window and a plant widened by a stated
   delay, each reported. The widened plant is not built yet.
3. Worker counts W in {1, 2, 4} with one poster.
4. H4: the fixed loop against absent wake in poll mode, as in section 6.
5. No seventh row for now.
6. The historical case is Netty NioEventLoop 4.1.0.CR7 against 4.1.0.Final
   (`historical/candidates-2026-10.md`), run both forced through the public `SelectStrategy`
   hook, disclosed as forcing, and natural; both reported.
7. (2026-10-24) D2: the fixed loop takes the park handshake; the 2026-09 loop stays as the
   variant plain wake, H4's reference arm; the lazy-wake race moves into the handshake.
8. (2026-10-25) H4's workload is the decade grid of per-task work in section 6.

# P1 v2: six defects, a condition-variable backend, worker counts and H4

Status: **draft, not frozen**. Written 2026-10-23 on the desktop (development only). It becomes
frozen only after Alex approves it together with `hypotheses-v2.md` and the laptop hours.
`design/minimal-loop.md` (the 2026-09 design) stays frozen and is not edited; this note extends
it. Section numbers "v1 2.3" and so on refer to that note.

## 1. What stays from v1

- The wake rule (v1 2.3): a post wakes its worker only when its push found the stack empty.
  The task stack, its memory orders and the episode argument are unchanged.
- The backends' system calls (v1 2.4 to 2.6): epoll with an edge-triggered wake eventfd and
  `epoll_pwait2`; io_uring on raw system calls with a multishot poll on the wake eventfd,
  single issuer and deferred task run; IOCP with a wake packet; a stop path of its own in each.
- The fixed loop with one worker issues the same system calls in the same order as v1's fixed
  arm (io_uring re-arms its wake poll after the stop check, as v1 did). One behaviour differs:
  the core checks the stop flag after a backend's start as well as before it, so on epoll and
  IOCP a stop that lands between the two checks skips the first drain, which v1 would have run. Two v2 rows equal the two v1 defects: **absent wake** on epoll and IOCP is v1's
  "omit", and **unobserved primitive** on io_uring is v1's "misdirect" (eventfd written, wake
  poll never armed).

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
  tests use one hook, called just before a post's push, to force the lazy-wake race (section 4).
- Wait modes: bounded (B > 0), blocking (B = 0, the infinite-bound baseline), and poll (the
  wait returns at once; H4's busy worker, section 6).

## 3. Workers and threads

- W workers, each the single consumer of its own stack. A post names its worker (`post_to`);
  `post` is worker 0. The episode argument of v1 2.3 holds per worker unchanged, because each
  stack still has one consumer and each wake object one waiter.
- W = 1 is the v1 loop. Wrong worker exists only for W >= 2 (at W = 1 the "other" worker is
  the same worker).
- Posters P are separate threads of the harness. Proposal for the campaign: W in {1, 2, 4} with
  one poster for the latency test, posts spread round robin over the workers. Alex decides.

## 4. The six defects

| Defect | Where | Site | epoll | io_uring | IOCP | condvar |
|---|---|---|---|---|---|---|
| absent wake | core, `post_to` | the wake call is omitted | v1 omit | new | v1 omit | new |
| unobserved primitive | backend, `wake` | the post signals an object the wait does not watch | a second eventfd outside the epoll set | v1 misdirect: eventfd written, poll never armed | a packet to a second port | a second condition variable and predicate |
| lazy-wake race | core, `post_to` | the wake decision comes from a load of the stack made before the push, not from the push itself | yes | yes | yes | yes |
| wrong worker | core, `post_to` | the push goes to worker i, the wake to worker (i + 1) mod W | W >= 2 | W >= 2 | W >= 2 | W >= 2 |
| no re-trigger | backend, registration | the wake registration is one-shot and never re-armed | `EPOLLONESHOT` | single-shot poll, re-arm skipped | N/A | N/A |
| undrained edge | core, drain | the worker runs at most K = 64 tasks per pass, keeps the rest and parks with the full bound | yes | yes | yes | yes |

- **N/A cells.** IOCP wakes with a posted packet and the condition variable with a notify.
  Neither has a registration to re-arm, so no re-trigger has no natural site there. No plant
  is contrived (D2).
- **Lazy-wake race.** The real pattern is "look whether the queue is empty, push, wake if it
  was". The look is stale when the worker drains the stack between the look and the push; the
  push then lands on an empty stack and no one wakes the worker. The natural window is a few
  instructions wide. The tests force it with the hook (the poster stops between its look and
  its push while the test lets the worker drain). The measured instantiation has no hook.
  Whether the campaign measures the natural window only, or also a plant widened by a stated
  delay, is a decision for Alex (section 9).
- **Undrained edge.** Edge-triggered sources must be drained until empty; reading part of the
  data and then parking leaves no new edge for the rest. Here the "edge" is the wake rule's
  empty to non-empty transition, and the leftover runs only at the next return of the wait.
- The condition-variable backend wakes by setting a predicate under the mutex and notifying.
  Its wait is `wait_for` with the predicate, so spurious returns re-wait for the rest of B.
  A notify without the predicate under the mutex is a lazy-wake race of its own; it is a
  candidate seventh row, with the frame-ownership race, if Alex wants backend-specific races.

`tests/defect_sites.cmake` (X1 v2) counts the `if constexpr` lines that name each defect in
each source and fails on any count other than this table: `loop.hpp` 1 each for absent wake,
lazy-wake race, wrong worker and undrained edge; `epoll.cpp` 2 unobserved (create, signal) and
1 no re-trigger; `uring.cpp` 1 unobserved and 2 no re-trigger (single-shot arm, skipped re-arm);
`iocp.cpp` 2 unobserved; `condvar.cpp` 1 unobserved. No other loop source names a defect.

## 5. The wake matrix (functional tests)

Detectors with a blocking wait (the infinite-bound baseline; a missed wake is a hang, caught
by a 1 s deadline): D1 one post; D2 64 posts back to back; D3 4 threads x 10,000 posts; D4 a
task posts a task; D6 10 posts in sequence; D7 192 posts made while a gate task holds the
worker; D8 the forced lazy-wake interleaving.

Detectors with B = 200 ms (the paper's test: a median delay above B / 5 is a missed wake; the
detectors also print the count of posts slower than B / 2): D5 the v1 test, 21 posts; D9 seven
rounds of 192 posts behind a gate, the slowest task of each round; D10 five forced lazy-wake
interleavings.

Expected verdicts (D detected, P passes, A racy and not asserted; CTest fails on any other
outcome). Wrong worker is run at W = 2; every other row at W = 1.

| Variant | D1 | D2 | D3 | D4 | D6 | D7 | D8 | D5 | D9 | D10 |
|---|---|---|---|---|---|---|---|---|---|---|
| fixed | P | P | P | P | P | P | P | P | P | P |
| absent wake | D | D | D | D | D | D | D | D | D | D |
| unobserved primitive | D | D | D | D | D | D | D | D | D | D |
| lazy-wake race | P | A | A | P | P | P | D | P | P | D |
| wrong worker (W = 2) | D | D | D | D | D | D | D | D | D | D |
| no re-trigger | D | D | D | D | D | D | D | D | D | D |
| undrained edge | P | P | A | P | P | D | P | P | D | P |

Before any detector posts, each worker runs one starter task posted before `run()` and the
workers settle, so a slow thread start cannot let the first drain pick up the detector's post.
The starters' own wake uses up no re-trigger's one-shot registration, which is why that row is
D throughout; a post that is the first wake after setup would pass D1 (the campaign's protocol
decides which case it measures). Every applicable cell is caught by at least one bounded
detector (D5, D9 or D10). The semantics
tests of v1 (S1 to S11) and S12 (each worker runs its own posts) pass in every variant: with a
bound, every defect is a delay and never a loss.

## 6. H4: the throughput cost of the fix

- v1's H4 (deferred) reads "the fix costs no measurable throughput when the worker is busy".
  In v1 "the fix" is the wake compiled in. v2 keeps that meaning: arm A is the fixed loop, arm
  B is absent wake, so the only difference is the wake system call on a post that finds the
  stack empty.
- "Busy" means the worker's wait returns at once because other completions are ready. The
  harness models that with the poll wait mode in both arms; posted work rides on those returns
  and absent wake loses nothing. With a bounded or blocking wait the absent-wake arm would
  stall, which measures the defect, not the fix.
- `bench/abpair.cpp`: P producers keep K tasks each in flight on W workers; each window runs
  one arm; pairs are ordered ABBA (or by a seeded shuffle); one process holds both arms. The
  output is one record per window with the arm, the tasks completed and each worker's passes.
  The loop does not count wakes issued: a counter would add code to the fixed loop's post path.
  `--check` leaves the pass counts out too: in poll mode they grow with elapsed time.
- Unobserved primitive on IOCP is a poor abpair arm: its wake packets go to a port that no
  one dequeues, so in a long timed window that port's queue grows without bound. Throughput is computed only on the laptop. On the desktop the program runs only
  `--check`, which uses count-based windows and prints counts and the pair order, never a time.
- Engineering note (D2). If the laptop probe shows the wake costs throughput under a busy
  worker, the standard remedy is to skip the wake while the worker is known to be running (a
  "sleeping" flag with a full fence on both sides). That remedy is exactly where lazy-wake
  races live, so it would enter the fixed loop only with its own revision entry and the matrix
  re-run. No such change is made now.

## 7. wakeprobe

wakeprobe selects the backend, the variant and W at run time. The single-post latency protocol
is blind to the lazy-wake race (unforced) and to undrained edge by construction; the campaign
needs the batch and forced protocols of D9 and D10 in wakeprobe as well. That is open work.

## 8. Comparison with ThreadSanitizer

A lost wake is not a data race: every shared access in the loop is atomic or under a lock.
The prediction is that TSan reports nothing on any variant. A development TSan run of the
matrix on the desktop checks that the code is race-free; a citable TSan record is made on the
laptop.

## 9. Open decisions for Alex

1. The six definitions and the N/A cells above.
2. Lazy-wake race in the campaign: the natural window only (honest power, likely near zero for
   single posts), or also a widened plant with a stated delay in the window.
3. Worker counts W in {1, 2, 4} and one poster, or another grid.
4. H4's pairing: fixed against absent wake in poll mode, as above.
5. Seventh rows: the condition-variable notify race; the frame-ownership race (no portable
   plant found yet).

# P1 minimal event loop: design

Status: design for review, phase 1. Nothing here is implemented yet. Written 2026-09-29.
Once Alex approves it, this note is frozen. Later changes go into the revision log at the end.

The earlier publication runs of P1 measured a library that is now withdrawn from all papers.
Under the component model, this repository must contain a minimal event loop that has exactly
the features P1 studies, optimized as far as possible, and P1 must be measured on that loop.
This note fixes the design before any code is written. It never names the withdrawn library.
Where it must refer to that library, it says "the withdrawn library". It describes the
library's identifiers (option names, macro names, target names, header paths) in words.

Line numbers refer to the files at paper-repo commit `2233155` unless another commit is named.

## 1. What wakeprobe exercises today

### 1.1 Calls into the withdrawn library

| # | Use | Where (`bench/wakeprobe.cpp` unless named) | Serves |
|---|---|---|---|
| U1 | The library's context header and an alias for its network namespace | 23, 76 | all |
| U2 | The context factory with a thread count of 1, a backend and the wait bound B in microseconds (0 means block). A failure exits with 3 | 773; 770-779 | H1, H2, H3, H5, random gap |
| U3 | Backend by name, through the library's backend enum and its name parser. On Windows, `iocp` maps to the platform default | 280-295 | all (arm label) |
| U4 | The backend name the context reports, compared with the request. A mismatch exits with 3 | 780-786 | all |
| U5 | `run()` on a runner thread; `stop()` then join on scope exit; an exception from `run()` is kept and reported | 316-351, 809, 822-823 | all |
| U6 | `post()` of a type-erased callable from the main thread. The callable reads `steady_clock` first, then runs an optional hook, then sets a shared promise. The main thread waits on the future with the run's timeout, then with a 10 s hard limit | 353-383 (post at 370) | all |
| U7 | Defect detection: CMake reads, from the library target, the per-backend no-wake macros it was compiled with, and injects the list as a string. The probe searches it for the backend's macro | 297-305, 65-67; `bench/CMakeLists.txt` 41-62, 108 | H1, H3, random gap |
| U8 | Refusals: a defect build with a blocking wait never delivers (exit 2); phase targets need a defect build and calibration posts (exit 2) | 787-797 | H1 on W (phase targets) |
| U9 | Provenance of the library checkout (commit, dirty flag) in every summary line | 50-55, 719-720; `bench/CMakeLists.txt` 66-95, 103-104 | provenance only |
| U10 | Timer slack is set before any thread starts, because the library's worker thread was created by the runner thread and inherits its slack | 760-766 | H1 (B_eff on Linux), matrix cells at the default slack |

The probe's first post also reads the worker's timer slack from inside the task (467-470).
The loop must therefore run tasks on the thread that waits.

### 1.2 Behaviour inside the withdrawn library that the earlier runs relied on

This was read from the library's sources at the measured revision. It is described here, not
copied, and none of it is reused.

- Threads: `run()` started one worker thread and joined it. `stop()` set a flag and signalled.
- epoll: tasks went into a mutex-guarded queue. The wake was a write to an edge-triggered
  eventfd in the epoll set, and the worker read it back to zero. The worker ran one task per
  pass and signalled itself again if more remained. It waited with `epoll_wait` (B rounded up
  to whole milliseconds) or with `epoll_pwait2` for other bounds. Stop used a second,
  level-triggered eventfd. Defect form: the post's eventfd write was compiled out (omitted wake).
- io_uring: a mutex-guarded queue per ring. The wake was a write to an eventfd that a
  single-shot `POLL_ADD` watched, re-armed after each wake. The worker waited through liburing
  with a timeout passed as an extended argument, or without one for a blocking wait, and ran a
  bounded batch of tasks per pass. Defect form: the poll was never armed, but the eventfd was
  still written (misdirected wake).
- IOCP: a mutex-guarded queue plus `PostQueuedCompletionStatus` with a callback key, one task
  per packet. The worker waited in `GetQueuedCompletionStatus` with B rounded up to
  milliseconds, or `INFINITE`. Defect form: no packet was posted, and a queue drain on
  `WAIT_TIMEOUT` was added (synthetic).
- One CMake option compiled the no-wake macro of every backend in the build.

### 1.3 Probe features that stay in wakeprobe unchanged

The loop must not absorb any of these. B_eff and the phase are properties of the host's
timers. A portable loop uses each OS's standard wait and leaves host timer policy alone. If
the loop changed that policy, it would change the quantity H1 measures.

| Feature | Where | Serves |
|---|---|---|
| Linux timer slack: set on the main thread, read on the worker | `probe_platform.hpp` 36-63; `wakeprobe.cpp` 760-766, 467-470 | H1, matrix slack cells |
| PM QoS CPU latency request, then drop from sudo back to the user (with dumpability restored for LeakSanitizer) | `probe_platform.hpp` 69-143; `wakeprobe.cpp` 740-758 | H5 |
| Windows timer resolution query | `probe_platform.hpp` 149-172; `wakeprobe.cpp` 768, 825 | H1 and H2 on W |
| `timeBeginPeriod` with the power-throttling opt-out | `probe_platform.hpp` 175-214; `wakeprobe.cpp` 767 | H1 and H2 on W |
| Resolution sampled mid-gap, on warmup gaps only | `wakeprobe.cpp` 524-530 | H1 on W |
| Gap sleep through a high-resolution waitable timer on Windows | `probe_platform.hpp` 223-286 | every W design |
| Calibration posts at B/10 after each delivery; B_eff as the median interval | `wakeprobe.cpp` 473-484, 385-415 | H1, H3 threshold, W phase targets |
| Phase, prediction and residual per post | `wakeprobe.cpp` 417-430 | H1 |
| Fixed gap, phase target, or random gap from a seeded, platform-independent draw | `wakeprobe.cpp` 437-456, 488-535 | gap sweeps, random gap |
| Per-post JSON and the summary line | `wakeprobe.cpp` 586-727 | analysis |

### 1.4 Build, run and analysis files that refer to the withdrawn library

| File | Lines | What changes |
|---|---|---|
| `bench/wakeprobe.cpp` | 1, 23, 50-55, 76, 280-305, 321, 348, 355, 460, 719-720, 761, 770-786 | its header comment, include, namespace, context type, factory, backend names, defect macros and provenance. Ported to the API in 2.2 |
| `bench/CMakeLists.txt` | 4-10, 19-39, 41-62, 66-95, 97-98, 102-109, 116-141 | checkout path, option defaults, defect macros read from its target, its provenance, linking, sanitizer flags passed through its cache options. Replaced by the root project (2.8) |
| `bench/run_matrix.sh` | 8-9, 23, 40-42, 65, 112-113, 119-129, 150-151 | checkout, two builds through its defect option, inputs gate on its target |
| `bench/run_matrix.ps1` | 5-7, 20, 25-26, 54, 57, 83-84, 95, 103, 129-130 | the same on W |
| `bench/run_publication.sh` | 8-13, 22, 30, 37, 39, 47, 55 | the same, plus its record gate |
| `bench/run_publication.ps1` | 6-12, 25, 33, 36, 41, 43, 71, 78 | the same on W (the quiet check at 54-69 stays) |
| `bench/sanitize_wakeprobe.sh` | 6, 11, 13, 21-22, 25-35, 44-47, 63-64 | its sanitizer options; logs under `/tmp`, which D5 forbids |
| `bench/sanitize_wakeprobe.ps1` | 3, 5, 10, 19, 28-30, 38, 40, 77, 88-89 | the same on W; logs under `%TEMP%` (29) |
| `bench/check_records.py` | 1-26, 42-61, 64-88, 100 | requires the library's own records |
| `bench/sanitizer_record.py` | 15-18, 52-79, 89-91, 145-153 | library tree and commit fields. The `REPORT` literal at 34-35 stays text for text |
| `analysis/summarize.py` | 95-99, 296-298, 650-654, 837-838 | two library constants, library commit fields and macros. The arm still comes from `defect_active` (8, 235) |

## 2. The minimal loop

### 2.1 Scope

In scope:

- one worker thread;
- posting a task from any thread;
- a wait bounded by B microseconds, or blocking when B = 0;
- one wake mechanism per backend;
- stop from any thread;
- a compile-time defect seed;
- a pass counter.

Out of scope: I/O, sockets, timers, more than one worker, kqueue, and a fast path for a busy
worker. The busy-worker path matters only for H4, which stays deferred.

### 2.2 API

```cpp
namespace wakeloop {

// Owned by the caller. The loop links it while it is queued.
struct Task {
    Task* next = nullptr;
    void (*run)(Task&) noexcept = nullptr;
};

enum class Defect { none, omit, misdirect };

// One class per backend, all with the same members: EpollLoop and UringLoop on Linux,
// IocpLoop on Windows. No virtual functions; the probe picks the class once at startup.
class EpollLoop {
public:
    explicit EpollLoop(std::int64_t wait_us);  // 0 blocks; < 0 throws std::invalid_argument;
                                               // a failed setup call throws std::system_error
    ~EpollLoop();                              // never while run() is active
    void run();                                // the calling thread is the worker until stop()
    void stop() noexcept;                      // any thread, any time, idempotent
    void post(Task& task) noexcept;            // any thread; never blocks, never allocates
    static constexpr const char* name = "epoll";
    static constexpr Defect defect = /* none, or this backend's seeded form */;
    std::uint64_t passes() const noexcept;     // number of wait returns so far
    const char* wait_method() const noexcept;  // the wait call in use, recorded by the probe
};

}  // namespace wakeloop
```

Contract:

- A task runs once, on the worker, unless the loop stops first. It must stay alive until it
  has run, or until the loop is destroyed. Tasks still queued at stop are neither run nor
  touched.
- Tasks posted by one thread run in the order they were posted.
- `run()` runs the tasks queued before it started, then waits.
- `stop()` wakes a blocked worker in both arms. It never goes through the defect site.
- A wake system call that fails is a broken invariant. The loop aborts with a message, so a
  lost wake can never be silent. The one expected failure, a saturated eventfd counter, is
  handled (2.4).
- `passes()` and `wait_method()` exist for the tests and for the probe's summary. The probe
  records the wait method as a factor.

What wakeprobe needs from this API (feature to hypothesis):

| Feature | Replaces | Serves |
|---|---|---|
| Constructor with B, blocking at B = 0 | U2 | H1, H2 (B sweep with blocking), H3 (B = 1 and 100 ms), H5 |
| `post()` from the main thread, task run on the worker | U6 | every hypothesis and the random-gap design |
| `run()` and `stop()` on the runner thread | U5 | all |
| `name` checked against `--backend` | U3, U4 | all (no mislabelled arm) |
| `defect`, recorded as `defect_mode` and `defect_active` | U7 | H1, H3, random gap (the analysis takes the arm from the record) |
| The worker is the runner thread, created after the slack is set | U10 | H1 on Linux, slack cells |
| `wait_method()` and `passes()` in the summary | new | provenance; a check that the timed wakes match B_eff |

wakeprobe keeps each post's `Task` node in storage that outlives the loop. A post that is
lost (the 10 s hard limit) then stays valid while it is still queued. wakeprobe keeps its
refusals (U8). It stops accepting a negative `--wait-us`. The old meaning,
"the library's default bound", has no counterpart, and no design ever used it.

### 2.3 Common core: the task stack and the wake rule

- **Queue.** A Treiber stack of intrusive `Task` nodes. `post()` pushes with a
  compare-and-swap on the head, with release order. The worker takes the whole stack with
  one exchange to null, with acquire order, reverses it into FIFO order and runs the tasks. It
  reads each task's `next` before running it, because a task may destroy or re-post itself.
- **Wake rule.** `post()` wakes the worker only when its push found the stack empty.
- **Why this rule is correct.** Call the time from a push onto an empty stack until the
  exchange that empties it an episode. The push that opens an episode issues a wake after the
  push. The wake object (eventfd event, completion packet) stays pending until a wait returns.
  Every wait return is followed by an exchange. So each episode ends at the latest at the
  first wait return after its wake. A push onto a non-empty stack joins an episode that
  already has its wake. A wake that arrives after its episode ended causes one extra pass and
  nothing else.
- **Why this design is the fastest correct one.** On the measured path (one post to an idle,
  parked worker) the poster does one uncontended compare-and-swap and one wake system call.
  The worker does one exchange after the wait returns and takes no lock. The alternatives
  cost more:
  - A mutex and a deque put a lock on both sides. The woken worker's first act would be to
    take the lock the poster just used.
  - A linked MPSC queue has a window in which a producer has swapped the tail but not yet
    linked its node. The consumer must then spin or wrongly see the queue as empty. Taking
    the whole stack has no such window, and emptiness is one load.
  - A "worker is parked" flag checked by the poster would save wakes only for a busy worker
    (H4, deferred). It adds a store-load handshake whose failure mode is exactly the defect
    class this paper studies.
- **Happens-before.** Task data reaches the worker through the release compare-and-swap and
  the acquire exchange, not through the wake object. ThreadSanitizer's view of the loop
  therefore does not depend on how it models kernel objects.

### 2.4 epoll backend

- **Setup (constructor).**
  - `epoll_create1(EPOLL_CLOEXEC)`.
  - A wake eventfd (`EFD_NONBLOCK | EFD_CLOEXEC`), registered `EPOLLIN | EPOLLET`.
  - A stop eventfd, registered `EPOLLIN`, level-triggered.
- **Wait.** `epoll_pwait2` with the exact `timespec` for B > 0, and a null timeout for B = 0.
  If the call fails with `ENOSYS`, the loop uses `epoll_wait` from then on, with B rounded up
  to whole milliseconds (-1 for B = 0), and `wait_method()` says so. `EINTR` counts as a pass.
- **Wake.** The poster writes 8 bytes to the wake eventfd. The worker never reads it. With
  `EPOLLET`, every write is a new wakeup, so each write ends one wait whatever the counter
  holds. If the counter is saturated, the write fails with `EAGAIN`; the poster then reads
  the counter to zero and writes again.
- **Stop.** `stop()` sets an atomic flag, then writes the stop eventfd. Nobody reads that
  eventfd. It stays readable, level-triggered, so every later wait returns at once. The worker
  checks the flag after every wait return.
- **Defect (omit).** The wake call in `post()` is compiled out. Nothing else differs.
- **Why this is the fastest correct choice.** One system call per wake, on the poster's side.
  After the wait returns, the worker does one exchange: no read, no lock. An eventfd needs
  one descriptor, and unlike a pipe it cannot fill. `epoll_pwait2` takes the bound to the
  microsecond.
- **Pinned by tests, not assumed.**
  - Each write wakes without a read: tests S9, D2 and D3.
  - `epoll_pwait2` is present on L and in WSL: the first test run records `wait_method()`.
  - Fallback if the first point fails: read the eventfd once after running the batch. That
    read is outside the measured delay.

### 2.5 io_uring backend

**Raw system calls, no liburing.** The loop uses `io_uring_setup`, `io_uring_enter` and
`io_uring_register` directly, with `<linux/io_uring.h>`. Reasons:

- the ring code this loop needs is small;
- the liburing versions differ between L and WSL (WSL has 2.5), and this removes that
  difference;
- under MemorySanitizer, no uninstrumented library sits between our code and the kernel;
- each wait is exactly one `io_uring_enter`.

**Setup (constructor).**

- `io_uring_setup` with a small SQ, since only a few SQEs are ever submitted.
- Flags: `IORING_SETUP_SINGLE_ISSUER | IORING_SETUP_DEFER_TASKRUN | IORING_SETUP_R_DISABLED`.
- Required features: `IORING_FEAT_EXT_ARG`, `IORING_FEAT_SINGLE_MMAP` and
  `IORING_FEAT_NODROP`. If one is missing, the constructor throws. Refused, not substituted.
- Map the rings. Create two eventfds, wake and stop.

**`run()` start, on the worker thread, in this order.**

1. Return at once if stop was requested.
2. Enable the ring with `IORING_REGISTER_ENABLE_RINGS`, so the worker becomes the single
   issuer.
3. Submit a multishot `POLL_ADD` on the wake eventfd, tagged WAKE, and a `POLL_ADD` on the
   stop eventfd, tagged STOP.
4. Check the stop flag.
5. Drain the stack once.
6. Enter the wait loop.

Arming comes before draining. A post whose push came before the drain is run by the drain. A
post after the drain writes the eventfd after the poll was armed, and the poll sees that write.
Stop works the same way: its flag store comes before its write, and the flag check comes after
the arming.

**Wait.** `io_uring_enter` with `to_submit` = any pending re-arm SQE, `min_complete = 1`,
`IORING_ENTER_GETEVENTS | IORING_ENTER_EXT_ARG`, and a timeout argument pointing at B (null
for B = 0). `ETIME` is a timed return. `EINTR` counts as a pass.

**Reap.**

- Load the CQ tail with acquire order and process the entries up to it.
- A WAKE entry without `IORING_CQE_F_MORE` means the kernel ended the multishot poll. Queue a
  re-arm, then drain the stack before the next wait, as at start.
- A STOP entry ends `run()`.
- Store the head with release order.

**Wake.** The poster writes the wake eventfd; nobody reads it. io_uring polls are
edge-triggered unless `IORING_POLL_ADD_LEVEL` is set. (That flag is defined in the uapi header
of WSL's Ubuntu 24.04.) So each write produces one completion, and the multishot poll needs no
re-arm. The poster never touches the ring, which is what makes `SINGLE_ISSUER` legal. With
`DEFER_TASKRUN`, the poll's completion work runs inside the worker's own wait call and cannot
interrupt the worker anywhere else.

**Defect (misdirect).** The arming of the wake poll is compiled out. `post()` still writes the
eventfd. This is the form the abstract calls "signals an object the wait does not watch". Stop
has its own eventfd and poll, so a blocking loop in the defect arm still stops. The same
tests pin this for all three backends (S7).

**MemorySanitizer.** The kernel's writes are invisible to MSan.

- Value-initialize every structure a raw system call fills: the setup parameters, the
  `io_uring_getevents_arg`, the `__kernel_timespec`, the offsets used for mapping, and each
  eventfd buffer.
- Before reading a CQE, call `__msan_unpoison` on it. This is compiled only when
  `__has_feature(memory_sanitizer)` holds.
- The rings are mapped through libc's `mmap`. MSan's interceptor marks that memory
  initialized when it is mapped, and each CQE is unpoisoned anyway.
- The same rule applies to the epoll backend's event array: value-initialize it, and unpoison
  the returned entries if the MSan record shows that `epoll_pwait2` is not intercepted.

**To be pinned by tests.**

| Assumption | Tests | Fallback |
|---|---|---|
| Each eventfd write gives one completion without a read (edge-triggered multishot poll) | S9, D2, D3 | read after the batch |
| The enabling thread becomes the single issuer | every test (the first submission fails otherwise) | create the ring in `run()`, and in the constructor only check that the kernel allows one |
| `DEFER_TASKRUN` completes the poll inside a timed wait | D5, S8 | drop `DEFER_TASKRUN`, and record the flags obtained |

**Alternatives considered.**

- **`IORING_OP_MSG_RING`.** Every posting thread would need its own ring. When the target
  ring runs `DEFER_TASKRUN`, the remote completion is expected to go through task work in the
  target's context, the same path as a poll completion. That is not yet confirmed in the
  kernel source for L's version. It also cannot express the misdirected form. Not chosen; an
  optional check is in 2.9.
- **Single-shot `POLL_ADD`, re-armed on every wake.** This needs one SQE per wake, and a read
  of the eventfd before each re-arm. Otherwise the re-armed poll finds the eventfd readable
  and completes at once. The multishot poll needs neither.
- **`IORING_OP_READ` on the eventfd.** It completes once and must then be re-armed on every
  wake.
- **`io_uring_register_eventfd`.** It signals completions outward, the wrong direction.
- **The futex wait operation.** It is single-shot, so it needs a re-arm per wake, and it
  needs a newer kernel than the other options.

### 2.6 IOCP backend

- **Setup.** `CreateIoCompletionPort(INVALID_HANDLE_VALUE, nullptr, 0, 1)`.
- **Wait.** `GetQueuedCompletionStatus` with B rounded up to milliseconds, or `INFINITE` for
  B = 0. The documented call takes milliseconds, and the process's timer resolution then sets
  the period. That is exactly the property H1 on W measures, so the loop keeps the standard
  call. A timed return is `FALSE`, with a null `OVERLAPPED` and `WAIT_TIMEOUT`.
- **Wake.** `PostQueuedCompletionStatus(port, 0, WAKE key, nullptr)`.
- **Stop.** Set the flag, then post a packet with the STOP key.
- **Defect (omit).** The `PostQueuedCompletionStatus` in `post()` is compiled out. In both
  arms the worker drains the stack after every wait return, so the defect needs no
  drain-on-timeout code of its own. This differs from the earlier synthetic form, which added
  that code only in the defect arm.
- **Why this is the fastest correct choice.** It is one documented system call. The wake
  rule leaves at most one stale packet per episode. `GetQueuedCompletionStatusEx` helps only
  when several packets are pending, which this design avoids.
- **Alternatives considered.**
  - A packet that carries the task pointer, with no user-space queue: one system call per
    post always, and no way to seed the defect without adding a queue.
  - A user APC with an alertable wait: not the completion-port idiom this paper studies.

### 2.7 The defect switch and the arms

- One CMake option, `WAKELOOP_DEFECT`, off by default. When on, it defines one macro for the
  loop target only. One source tree builds both arms, as before.
- Each backend source has exactly one preprocessor conditional on that macro:
  - epoll: the wake in `post()`;
  - IOCP: the wake in `post()`;
  - io_uring: the arming of the wake poll.
- Test X1 counts these conditionals and fails on any other count. It pins the claim in
  `paper/main.tex` (150-151) that the switch "changes nothing else".
- Each class exposes its form as `defect`. wakeprobe records `defect_mode` and
  `defect_active` from it, which replaces U7. The analysis keeps reading `defect_active`.

| Arm | Build | epoll | io_uring | IOCP |
|---|---|---|---|---|
| fixed | `WAKELOOP_DEFECT=OFF` | eventfd write | eventfd write, multishot poll armed | completion packet |
| defect | `WAKELOOP_DEFECT=ON` | wake omitted | eventfd written, poll never armed (misdirected) | packet omitted |

The run-time switches of each design stay as in the earlier scripts:

| Design | Host | Arm | wakeprobe switches | Serves |
|---|---|---|---|---|
| gap-sweep | L | defect | `--wait-us` 1000, 10000; `--gap-us` B x k/10 for k = 1..30; `--posts 21 --warmup 3 --calibrate 11 --timerslack-ns 1` | H1, H3 (resampling) |
| gap-sweep | W | defect | `--wait-us` 1000, 10000; `--phase-frac` 0.05 to 2.95 in steps of 0.1; `--posts 21 --warmup 3 --calibrate 11`; `--timer-period-ms` 0, 1 | H1, H3 (resampling) |
| bound-sweep | L, W | fixed | `--wait-us` 0, 1000, 10000, 100000; `--gap-us 2000 --posts 51 --warmup 5`; L `--timerslack-ns 1`; W `--timer-period-ms` 0, 1 | H2 |
| bound-sweep-qos | L | fixed | as bound-sweep, plus `--pm-qos-us 0` under sudo | H5 |
| matrix | L | both | `--wait-us` 1000, 100000; `--gap-us` 1.7B; `--posts 51 --warmup 5`; `--timerslack-ns 1`, plus 0 (the kernel default) at B = 1000 | H1 (one phase), H3 |
| matrix | W | both | as on L, with `--timer-period-ms` 0, 1 instead of slack | H1 (one phase), H3 |
| random-gap | L | both | `--wait-us` 1000, 10000; `--gap-frac-range 0.1:3.0 --gap-seed 20260926`; `--posts 21000` (1000 trials of 21) `--warmup 5 --timerslack-ns 1` | exploratory |

H4 has no arm; it stays deferred.

### 2.8 Layout and build

```
CMakeLists.txt             root project: the loop, its tests and wakeprobe
loop/include/wakeloop/     public header
loop/src/                  task_stack.hpp, epoll.cpp, uring.cpp, iocp.cpp
tests/                     harness.hpp, wake_tests.cpp, defect_sites.cmake, CMakeLists.txt
bench/                     wakeprobe.cpp, probe_platform.hpp, CMakeLists.txt, scripts
```

- Targets: `wakeloop` (static library), `wakeloop_tests`, `wakeprobe`.
- The standard stays C++23. Warnings: `-Wall -Wextra`, or `/W4` on MSVC.
- Cache options:
  - `WAKELOOP_DEFECT` (BOOL);
  - `WAKELOOP_SANITIZER` (`address+undefined`, `thread`, `memory`; `address` on Windows);
  - `WAKELOOP_MSAN_LIBCXX` (PATH).
- Sanitizer flags apply to every target of the build. The MSan build uses `-stdlib=libc++`
  from `WAKELOOP_MSAN_LIBCXX`, and configure fails if that prefix is missing.
- The project root is the repository root. That is what keeps the loop's files first-party
  for the inputs gate (4.4).

### 2.9 Optional check of the io_uring wake (engineering, not citable)

The claim that the multishot poll is the fastest correct io_uring wake rests on the reasoning
in 2.5. It is not measured. If Alex wants it measured, and L is free by 2 October:

- build a throwaway `MSG_RING` variant of the fixed arm;
- run the bound-sweep cells at reduced size on L, alternating the two builds;
- keep the poll unless `MSG_RING` gives lower medians in every pair.

Either outcome goes into the revision log before any sanitizer record. The throwaway code is
not committed to the measured tree.

## 3. Tests, written first

### 3.1 Harness

- There is one executable, `wakeloop_tests <backend> <test>`, and one process per test, so a
  hung loop cannot affect another test. CTest registers every pair of test and backend that
  the host compiles, named `<backend>.<test>`.
- No third-party test framework. The inputs gate would otherwise have to pin its version, and
  the MSan build would have to instrument it against libc++. Plain code also avoids the
  exception unwinding through `std::function` in which the known clang-cl
  stack-use-after-return false positive appeared (4.6).
- Outcomes:
  - pass: exit 0;
  - failure: print `FAIL: <file>:<line>: <reason>`, exit 1;
  - a detector that sees a missing wake: print `DETECTED: <test>: <evidence>`, exit 2.
- CTest properties:
  - `TIMEOUT 60` on every test, as a last resort. Every test has its own tighter deadline.
  - In defect builds, the detectors get `PASS_REGULAR_EXPRESSION "DETECTED: "`. A detector
    then passes only if it actually detected. A detector that fails to detect the seeded
    defect makes the defect build red.
- Sanitizer runtimes run with `halt_on_error=1`, so a report also fails an ordinary test. The
  record writer scans the full output of every test, passing or not (4.3).
- Waits use deadlines on `steady_clock`. Sleeps appear only as the documented gaps.

### 3.2 Semantics tests

These pass in both arms. They use B = 5 ms, so the defect arm still delivers.

| Test | Pins |
|---|---|
| S1 `post_runs_once` | a posted task runs exactly once, on the thread inside `run()` |
| S2 `post_before_run` | a task posted before `run()` runs in the first pass |
| S3 `fifo_one_producer` | 1000 tasks from one thread run in post order |
| S4 `many_producers` | 4 threads post 10000 tasks each; each runs once; each producer's order is kept (the ThreadSanitizer workload) |
| S5 `self_post` | a task posts another task, which runs |
| S6 `stop_before_run` | `run()` returns at once after an earlier `stop()` |
| S7 `stop_wakes_blocked` | with B = 0 (blocking), `stop()` from another thread ends `run()` within 1 s. Proves in the defect arm that the switch leaves stop alone |
| S8 `stop_from_task` | a task calls `stop()`; `run()` returns after that pass |
| S9 `idle_blocking_no_spin` | B = 0. After 10 delivered posts, `passes()` grows by at most 1 over 200 ms idle. Catches a wake object that stays ready (a level-triggered poll that is never read) |
| S10 `idle_bounded_passes` | B = 10 ms, 300 ms idle. `passes()` is at least 1 (the bound expires) and at most `ceil(300 ms / B) + 2` (no spinning). No lower bound tighter than 1 is asserted, because Windows rounds the wait up to its timer tick |
| S11 `defect_is_delay_not_loss` | B = 20 ms, 5 posts 5 ms apart. Each is delivered within 3 x max(B, one timer tick). The paper's premise: the defect is a delay, not a loss |

### 3.3 Wake detectors

These pass in the fixed arm. In the defect arm each must print `DETECTED`.

| Test | Method | Defect caught |
|---|---|---|
| D1 `detect_blocking_single` | B = 0, one post, delivered within 1 s | any missing or misdirected wake |
| D2 `detect_blocking_burst` | B = 0, 64 posts back to back, all delivered within 1 s | the above, and a loop that runs one task per wake when several posts share one wake |
| D3 `detect_blocking_producers` | B = 0, 4 threads post 10000 tasks each, all delivered within 10 s | the above, and a race that loses a wake between push and park |
| D4 `detect_self_post_blocking` | B = 0, a task posts another, which must run within 1 s | a wake that works only from other threads |
| D5 `detect_latency21` | the paper's test: B = 200 ms, 21 posts, each 0.3B after the previous delivery. Detected when the median delay exceeds B/5. Stops once 11 posts exceed the threshold | the defect under a bounded wait, which D1 to D4 cannot see |

D5 is the paper's latency test at unit scale. Its gap puts the phase at 0.3B, far from the
blind spot in the last fifth of the period. In a correct arm, even under a sanitizer, the
median must stay below B/5.

### 3.4 Structural test

X1 `defect_sites` (a CMake script): each backend source contains exactly one conditional on
the defect macro, and no other file contains any.

### 3.5 Where each test runs

| Backend | Development | Records |
|---|---|---|
| epoll, io_uring | WSL Ubuntu 24.04, clang 18.1.3, with ASan+UBSan, TSan, and MSan against `~/opt/libcxx-msan-18` | L, clang 22.1.8 |
| IOCP | W native, clang-cl 22.1.0 and MSVC, with ASan | W |

WSL builds are for development only and never produce records. WSL's kernel is
6.6.114.1-microsoft-standard-WSL2, with io_uring enabled (`kernel.io_uring_disabled` is 0).
L ran 7.2.3 in the earlier runs. Every io_uring assumption in 2.5 is re-tested on L the first
day L is free.

### 3.6 Order of work

1. Write the harness, S1 to S11, D1 to D5 and X1 against the header only. They fail to link
   (red).
2. Write the task stack. Make S1 to S6 and S8 pass on each backend, one backend at a time:
   epoll, then io_uring, then IOCP.
3. Add the wakes and the stop paths. S7 and S9 to S11, then D1 to D5, go green in the fixed
   arm.
4. Build the defect arm. D1 to D5 must print `DETECTED`, and everything else must stay green.
5. Run all of this under every sanitizer in development, then on L and W.

## 4. Sanitizer plan

### 4.1 Records

| Host | Compiler | Sanitizer | Runtime options | Gates |
|---|---|---|---|---|
| L | clang 22.1.8 | ASan+UBSan | `ASAN_OPTIONS=detect_leaks=1:detect_stack_use_after_return=1:strict_string_checks=1:symbolize=1`, `UBSAN_OPTIONS=print_stacktrace=1:halt_on_error=1` | yes |
| L | clang 22.1.8 | TSan | `TSAN_OPTIONS=halt_on_error=1:second_deadlock_stack=1` | yes |
| L | clang 22.1.8 | MSan, `-fsanitize-memory-track-origins=2`, instrumented libc++ | `MSAN_OPTIONS=halt_on_error=1:print_stats=1:fast_unwind_on_fatal=1` | yes |
| W | clang-cl 22.1.0 | ASan | `ASAN_OPTIONS=detect_stack_use_after_return=1:strict_string_checks=1:symbolize=1` | yes: the measured W builds use clang-cl, as the earlier W runs did |
| W | MSVC | ASan | same | no: extra coverage. The gate matches compilers, so it cannot cover a clang-cl build |

Each record covers both arms. For each arm it builds the loop, the tests and wakeprobe under
the sanitizer, then runs:

- the full CTest suite, with the detectors' expected detections declared in the defect arm;
- every design of the run matrix at the reduced sizes used before: 11 posts, 5 gap-sweep
  steps, 2 warmup posts, one random-gap trial. The L records include the PM QoS cells under
  sudo.

The MSan prefix on L is the one the earlier L records used, `$HOME/opt/libcxx-msan-gcc`.
Before the MSan record, the script checks that the prefix has headers and libraries. Record
files are named `wakeloop-<code commit, 9 characters>-<host>-<sanitizer>.json` in the
mono-repo's `lab/sanitizer-records/`.

### 4.2 Logs

- On L, every log goes to `~/lab/records-logs/<record>/`: configure, build, the full CTest
  output and its JUnit file, the probe summaries, `plan.txt`, `inputs.json`.
- On W, the same set goes to `%USERPROFILE%\lab\records-logs\<record>\`.
- Build trees may live elsewhere. Nothing is kept under `/tmp` or `%TEMP%`.
- The directory is packed into a tar.gz. Its sha256 goes into the record, and the tarball is
  archived beside the raw runs.

### 4.3 Report pattern

The record writer stays at `bench/sanitizer_record.py`, with its `REPORT` literal text for
text. `lab/bin/test_report_pattern.sh` already checks that file, so no second copy of the
pattern is added anywhere.

The writer runs CTest with the output of every test (`ctest -V`, plus `--output-junit`) and
scans all of it, not only the output of failing tests. Two reasons:

- `lab/bin/sanitize.sh` notes that CTest's log holds a test's output only when the test fails;
- in the defect arm, a detector passes on its `DETECTED` line whatever its exit code, so a
  sanitizer report inside it would otherwise be missed.

A record is green only if:

- every test outcome is as declared;
- every probe cell is green;
- the scanned output contains no line matching the pattern.

### 4.4 Gate

- Compiled inputs are hashed per target for `wakeloop` and `wakeprobe`. The code-selecting
  configuration is `WAKELOOP_DEFECT` plus the list of compiled backends.
- The fixed build must match a fixed-arm section and the defect build a defect-arm section,
  in every required record for the host.
- The gate also checks that the `wakeloop` input list contains every file under `loop/src`
  and `loop/include`. If the layout ever left them outside the first-party roots, the hash
  would become constant and would gate any code. This check prevents that.
- `bench/check_records.py` is rewritten:
  - L needs green ASan, TSan and MSan records; W needs a green clang-cl ASan record;
  - all required records must agree, per arm, on compiler, inputs and configuration;
  - no library record is involved.
- `run_matrix` checks both builds against the gate before any cell runs. That now includes W,
  whose earlier gate was applied only after the run.
- `run_publication` refuses to start if `loop/`, `tests/`, the `bench/` sources or the root
  `CMakeLists.txt` have local changes.

### 4.5 MemorySanitizer and the kernel

See 2.5. Every buffer a raw system call fills is value-initialized, and each CQE is unpoisoned
before it is read. The MSan record runs every test on both Linux backends and the reduced
matrix, so a missed kernel write shows up there as a report. No report is waived.

### 4.6 Known risk: clang-cl stack-use-after-return

`lab/evidence/2026-09-26-clangcl-asan-uar-false-positive/` documents a clang-cl 22.1.0
report during exception unwinding through `std::function`. The new tests contain neither. If
the report comes back anyway:

1. Reduce the failing test until it is plain code, and re-run.
2. If it still recurs, stop and ask Alex before any record is made with the option off.

### 4.7 Changes outside this repository (mono-repo, each its own commit, after Alex's go)

1. `lab/bin/inputs_hash.py`:
   - code-selecting options given on the command line or declared by the project, instead of
     the fixed list of the withdrawn library's options;
   - a neutral label for the project root;
   - a per-build `--expect`, so each arm is checked against its own record section.

   The current behaviour stays the default, so P2's callers do not change. Before the change
   is committed, the last P2 records' inputs are recomputed and must be identical.
2. `lab/bin/test_report_pattern.sh`: no change, as long as `bench/sanitizer_record.py` keeps
   its literal.
3. `lab/journal.jsonl`: one entry per record set.

## 5. Run plan and schedule

### 5.1 Matrices

The matrices are the same as in the earlier publication runs, with the same designs, sizes and
seeds.

| Host | Invocation | Designs | Cells |
|---|---|---|---|
| L | `run_publication.sh`, `DESIGNS=gap-sweep,bound-sweep,bound-sweep-qos,matrix` | 120 + 8 + 8 + 12 | 148 |
| L | `run_publication.sh`, `DESIGNS=random-gap` | 8 | 8 |
| W | `run_publication.ps1` | 2 timer settings x (60 + 4 + 4) | 136 |

- The cell order is shuffled with `SEED=20260926`: on L with Python's `random.Random`, on W
  with a `System.Random` Fisher-Yates shuffle. The cell lists are unchanged, so each run has
  the same order as its earlier counterpart.
- The random-gap draws use `GAP_SEED=20260926`. The analysis resamples the gap sweep with
  `random.Random(20260926)`, 100000 draws per configuration.
- R: each cell runs once, as before (`paper/main.tex` 342-343).
- D9 does not apply to H1, H2, H3 or H5, because none of them is a paired test. It would apply
  to H4 if H4 were ever run.
- W again has no random-gap run. Adding one would be a design change, and this note does not
  propose it.

### 5.2 Durations of the earlier runs

`results/README.md` gives none, so these come from the run logs. The L runs are in the
archived tarballs in `D:\Archive\p1-raw\`; the W run is in `results/raw/`.

| Run | From | To | Duration |
|---|---|---|---|
| L publication (148 cells) | `env.txt` 10:00:46Z | last cell end 10:03:08Z | 2 min 22 s (cells from 10:01:33Z) |
| L random gap (8 cells) | `env.txt` 10:48:25Z | last cell end 11:17:44Z | 29 min 19 s |
| W publication (136 cells) | `env.txt` 01:53:41Z | last cell end 01:57:00Z | 3 min 19 s (cells from 01:54:44Z; the quiet check runs before) |

By the records' `seconds` field, the probe sanitizer records at the last two earlier code
commits took 78 to 100 s each on L. At the last one, W took 113 s with clang-cl ASan and 143 s
with MSVC ASan. The new records add the test suite.
Its longest tests are bounded by their deadlines (3.2, 3.3).

The runs are short. The schedule risk is getting code and gates ready, not machine time.

### 5.3 Schedule (2026)

| Dates | Work | Where |
|---|---|---|
| Tue 29 Sep | this design; wait for Alex's reply | |
| Wed 30 Sep to Fri 2 Oct | tests first, then epoll and io_uring; IOCP in parallel | WSL; W |
| Thu 1 Oct (L free) | first build and test run with clang 22.1.8 on kernel 7.2.3; optional check of 2.9 | L |
| Sat 3 to Tue 6 Oct | wakeprobe port, root CMake project, run and record scripts, `check_records.py`, the lab's `inputs_hash.py` change, the analysis fields | W, WSL |
| Wed 7 to Thu 8 Oct | smoke runs through the whole pipeline, including the analysis, on L and W (not citable) | L, W |
| Fri 9 Oct | code freeze; the `hypotheses.md` revision-log entry committed (6.3) | |
| Fri 9 to Sun 11 Oct | records: L ASan, TSan and MSan; W clang-cl ASan and MSVC ASan | L, W |
| **Mon 12 Oct** | **target: code and records green** | |
| Tue 13 to Thu 15 Oct | publication runs: the two L invocations, W in a quiet window | L, W |
| Fri 16 to Mon 19 Oct | buffer for re-runs. **Target: runs done by 19 Oct** | |
| Tue 20 to Fri 23 Oct | analysis, macros, paper rewrite, independent fact-check, Word copy. **Target 23 Oct** | |
| Sat 24 to Sun 25 Oct | buffer | |
| Mon 26 Oct | Alex decides on registration | |
| by Mon 2 Nov | Alex uploads | |

### 5.4 Risks

1. **Reply latency.** Implementation starts on Alex's reply. Each day of delay moves the
   12 October date. The gap between 12 and 19 October absorbs about three days.
2. **Toolchain drift on L.** The gate matches the compiler identification string (clang
   22.1.8 in the earlier runs). An Arch upgrade between the first record and the last run
   makes the run refuse to start. A kernel upgrade also changes a recorded factor (kernel
   7.2.3 in the earlier runs). Freeze L's packages from 9 October until the runs are done.
   The same applies to LLVM and Visual Studio on W; clang-cl 22.1.0 was confirmed on W today.
3. **L's time is shared with P2.** P2's timed runs end around 30 September, and P2 has more
   L work planned. By the earlier logs, P1's two L invocations took 31 min 41 s together
   (5.2). P1 needs a lab-locked window of that order between 13 and 19 October, plus the
   records around 9 to 11 October.
4. **Kernel semantics.** The edge-triggered multishot poll, issuer enabling and
   `DEFER_TASKRUN` behaviour are verified in WSL first (kernel 6.6), then on L (7.2.3). The
   fallbacks in 2.5 cost a day at most, if they are taken before 9 October.
5. **MSan false positives from kernel writes.** These may need more value-initialization or
   unpoisoning. Days 9 to 11 October leave room; no report is waived.
6. **The shared lab script.** `inputs_hash.py` is also used by P2. The change must keep P2's
   hashes bit-identical (4.7).
7. **The paper's motivation.** The earlier paper showed the defect class through defects found
   in a real library. That evidence goes (6.1). Without replacement, the motivation rests on
   the mechanism and the seeded defects alone. Replacing it needs public, verifiable reports
   of lost wakes in other projects, found and checked by 20 October, or a narrower claim.
   This is the largest risk to the paper's substance.
8. **New outcomes.** The hypotheses may come out differently on the new loop. The Linux H2 and
   H5 picture in particular could change the story. The results sections are rewritten from
   the new macros, and every qualitative sentence is re-checked, not only the numbers.

## 6. Paper impact

### 6.1 `paper/main.tex`

| Lines | Now | Change |
|---|---|---|
| 38-40 | defects found in a C++ server library | goes. New sentence: a minimal loop over the three interfaces, with a compile-time seeded defect per backend |
| 64 | "including the one studied here" | stays true for the new loop |
| 71-76 | evaluation on the named library and its defect history | goes. We evaluate on a minimal loop written for this study, with the defect seeded in each backend |
| 118-120 | timers routed through the post path "as in the library under test" | keep only the conditional statement, without reference to a library |
| 141-144 | fault seeding | stays |
| 148-154 | the library, its four backends, the "historical defect" switches, the synthetic IOCP drain | rewritten as "Loop and arms": one worker, the task stack and wake rule, the wake per backend, the three seeded forms (2.7), stop outside the switch, B as a run-time parameter |
| 174-185 | revisions; the fix commit of the third defect | the commit sentence goes. Add the implementation change from the revision log (6.3) |
| 186-213 | hypothesis table | outcomes from the new macros. H4 stays "Not run; no claim" |
| 215-228 | machines; builds "without TLS, HTTP/2 and simdjson"; the library's commits on each OS | the build options go. Both systems measure the loop at one code commit of this repository, through a new macro |
| 230-239 | validity: library tests, W gate applied after the run, library detectors, the clang-cl note | the loop's tests; gates before every run on both hosts; the five detectors per backend (3.3); the clang-cl note only if 4.6 recurs |
| 243-327 | results | numbers regenerate. Re-check every qualitative claim: the 10 ms excess peak, the H1 failure on W with the 1 ms timer, the H2 and H5 contrasts, "io_uring difference unexplained" |
| 329-337 | "Defects found": the library's history, the third defect, the shutdown hang | goes entirely. Anonymizing it would still be text about the library |
| 339-348 | threats: "The library is ours", "the IOCP fault is synthetic" | the loop is ours and minimal (no I/O, timers or second worker); every seeded fault is hand-written; "each cell ran once" stays |

Macros that go: the two library constants (default epoll bound, callbacks per pass) and the
two library commit macros. Macros that come: the code commit, and the wait method per backend.

### 6.2 Other files that name the withdrawn library

- `README.md` (3, 9, 14): rewritten.
- `results/README.md`: rewritten for the new runs.
- `results/*.csv`: regenerated. The library commit column becomes the code commit.
- `results/macros.tex` and `paper/Tsvetanov-wake-TELECOM2026.docx`: regenerated.
- `results/smoke/**` (its `env.txt` and JSONL fields): removed from the tree. The archive
  keeps them.
- `bench/*` and `analysis/summarize.py`: per 1.4.

The repository's history keeps all of these. Whether the history must be rewritten before any
public release is Alex's decision (7).

### 6.3 Proposed revision-log entry for `hypotheses.md`

To be committed before the first record. Not committed in phase 1.

> - 2026-09-29, before any record or run on the new implementation: the measured
>   implementation is now this repository's minimal event loop (`loop/`), with one backend per
>   wait interface (epoll, io_uring, IOCP) and a compile-time switch that seeds the wake
>   defect. The earlier implementation is withdrawn, and none of its data is used. In H1 and
>   H3, "the wake defect" is the loop built with the switch on: epoll omits the wake, io_uring
>   writes an eventfd that its ring does not watch, and IOCP omits the completion packet. In
>   H2 and H4, "the fix" is the loop with its wake compiled in. H1 to H5, their thresholds, the
>   designs and the random-gap design are unchanged; H4 stays deferred.

### 6.4 Frozen designs that cannot be reproduced as written

None. Every design and all five hypotheses run unchanged on the new loop (2.7, 5.1). Two
points still need the revision-log entry in 6.3 before any run:

- the subject of measurement changes;
- "the fix" no longer names a historical change. It names the correct arm.

## 7. Questions for Alex

1. io_uring defect form: misdirected (recommended; it keeps the "unwatched object" form of the
   class) or omitted (the same form on all three backends)?
2. Run the optional `MSG_RING` check (2.9)?
3. The MSVC ASan record on W as extra coverage, beside the gating clang-cl record: yes?
4. Before any public release, must this private repository get a fresh history, as in D3?
   Its current history names the withdrawn library.
5. Code availability in the paper: cite this repository, or "available on request" while it
   is private?

## Revision log

- 2026-09-29: first version, for review.
- 2026-09-29, approved with these answers: the io_uring defect form is misdirected; the
  optional `MSG_RING` check (2.9) is not run; the MSVC ASan record on W is extra coverage,
  and the clang-cl ASan record gates the measured W builds; the repository keeps its history;
  the paper cites the public paper repository for code availability, through a macro, and it
  is published only on Alex's explicit yes.
- 2026-09-29, during implementation, before any record. What the code does differently from
  the text above, and why:
  1. The build defines `WAKELOOP_DEFECT` as 0 or 1 for the loop and everything that links it
     (2.7 said "for the loop target only"). The header reads its value once
     (`kDefectSeeded`) to give each class its `defect` constant. Each backend source still
     holds exactly one conditional on it, and X1 checks that.
  2. `task_stack.hpp` is in `loop/include/wakeloop/`, not `loop/src/` (2.8), because the
     classes hold the stack by value. `loop/src/linux_fd.hpp` holds the eventfd helpers the
     two Linux backends share.
  3. The io_uring ring indices use the compiler's `__atomic` builtins, not `std::atomic_ref`.
     The instrumented libc++ 18 used for MSan in WSL has no `atomic_ref`.
  4. The test harness returns an outcome instead of throwing an exception (3.1). With
     exceptions, clang-cl 22.1.0 ASan reported stack-use-after-return in all five detectors
     of the defect build, during unwinding, in the test's own frame (`std::thread::joinable`
     in the runner's destructor). That is the documented false positive (4.6). Step 1 of 4.6
     was applied, and the report is gone with the option on. MSVC ASan never reported it.
  5. S9 posts only in the fixed arm, because a defect build cannot deliver with a blocking
     wait; its idle check runs in both arms. D3 detects when delivery stalls for 1 s after
     the producers finish, with no overall deadline, so a slow but progressing loop under a
     sanitizer is never taken for a missing wake. S3 and S4 fail only when delivery stalls
     for 5 s. Every test has a CTest timeout of 60 s as the last resort.
  6. `run()` may be called once per loop object; a second call throws `std::logic_error`.
  7. Where the MSan runtime is installed without its headers (WSL's clang 18), `common.hpp`
     declares the documented `__msan_unpoison` itself.
  8. The records: one per host and sanitizer, covering both arms, named
     `wakeloop-<code commit>-<host>-<sanitizer>.json`. `bench/inputs_gate.py` runs the lab's
     `inputs_hash.py` in its new opt-in project mode and checks the loop-source coverage of
     4.4. The code paths for the code commit are listed once, in `bench/code_paths.txt`.
  9. wakeprobe needs an explicit `--backend` (`epoll` or `io_uring` on Linux, `iocp` on
     Windows). Its summary drops the old provenance fields and adds `defect_mode`,
     `wait_method` and `loop_passes`.
  10. An independent code review found no critical or high issue and two medium ones, both
     fixed before any record: the io_uring submission retries when a signal interrupts it
     (the wait already did), and the stall-based waits of item 5 replace fixed deadlines
     that a correct loop could exceed under MemorySanitizer.

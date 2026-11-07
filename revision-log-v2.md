# Revision log, P1 v2

The frozen files of P1 v2 are never edited. Every later change to what they say, and every value
their frozen rules produce, is an entry here, appended with its date and the reason, and never
rewritten. An entry that changes a rule names whether any confirmatory data existed when it was
made.

## Frozen files (2026-10-27)

Frozen on 2026-10-27 with Alex's yes (decisions 11 to 21 in P1's STATE.md), before any
confirmatory window. The sha256 is of each file as stored in git (LF line endings); check it with
`git show <commit>:<file> | sha256sum`.

| File | sha256 |
|---|---|
| `hypotheses-v2.md` | b82d845d24188041fa0a52ea71363ce7476a38dff83cd50f9fbeeff0e84f8a88 |
| `design/v2-defects.md` | 5602b20c8438b5599dc8f7d9d35bf46f79193d1b2008dac4489f254fb3535419 |

## Entries

Expected entries, from the frozen rules: each H4 cell's R from the A/A pilot
(`hypotheses-v2.md` section 7), the commit of the generated run plan (section 8),
ANALYSIS_COMMIT, and any change of the testbed (section 1).

### 2026-10-27: ANALYSIS_COMMIT ad39c9c4165860dec3edbe8b93a6157cd4031e7f

The analysis code (`analysis/v2/`), the plan generator and the campaign runner
(`bench/run_campaign.py`) are fixed at this commit. No pilot or confirmatory data existed. The
code follows the frozen text; where that text leaves a choice open, the code fixes it as below.

**Plans and the runner**
- Each H4 cell's R is read from one h4-r block appended to this log (section 8). `pilot.py`
  writes the block. `plan.py` and `score.py` refuse a missing, second or malformed block, and one
  whose R differs from section 7's rule applied to its s. That rule can only give R of 14, 18, 20
  or 24, so 16 and 22 are refused.
- A real plan needs a clean tree. Its header records the commit and this log's sha256 (CRLF read
  as LF). Stand-in and draft plans run only as dry runs.
- Each plan line carries `limit_s`, a chosen bound and not an estimate. Past it the runner kills
  the tool and counts a failed run. wakeprobe: units x (k x 10 s + 1 s) + 120 s, where units are
  the probe, calibration, warm-up and measured units, and k is 1 under S and the sweep, 2 under Bt
  (the 10 s waits one unit can make). abpair: windows x (window + 10 s) + 120 s.
- abpair fails a window whose start-up, drain or worker stop exceeds 10 s: section 8's "a window
  that does not finish". It also fails a pinned thread that ends on another CPU.
- A rerun follows its failed run at once, in the same place in the order. Any non-zero exit or a
  timeout triggers it.
- A run the runner is killed in is void. It is set aside and reported, its data are never used,
  and it does not use up an attempt. A campaign may stop on a time budget and resume in plan order.
- The runner runs on CPU 0. Each tool starts with the full CPU mask and pins its own threads.
- The canonical order (S and Bt, then the sweep, then H4) is shuffled by Fisher-Yates over
  SplitMix64 with seed 20261026. The pilot plan uses the same seed and rule.

**Good processes**
- A process is good only if the runner's `.done` exists, the run whose data are used exited 0,
  and its summary matches the plan and section 1: a Release build, not dirty, one bench commit
  across both systems; on Linux, timer slack set to 1 ns without error; on Windows, no timer
  period requested and every timer-resolution reading (start, gaps, end) equal to the coarsest
  resolution. A finer reading means some process asked for more, so the run is not at the default.
- Lost units of failed first runs and of void runs are reported with the lost wakes of fixed and
  plain cells. The larger of the records' count and the summary's count is used.

**B_eff and thresholds**
- The median of an even count is the mean of the two middle values. A process calibrates when it
  is good and its `b_eff_ns` is above 0. A span of exactly 10% passes.
- Windows fallback: the largest `coarsest_timer_us` over every wakeprobe summary of the system.
  If none reports one, scoring stops.
- Timed-out units: measured units only, with a delay strictly above 5 x max(B, the cell's B_eff,
  the cell's coarsest timer). The Bt gate's own wait is not counted. The sweep and H4 have none.

**Tests and intervals**
- A trial is a block of 21 consecutive measured units in index order.
- Holm rejects at p at or below its threshold.
- Bootstrap: nearest-rank 2.5% and 97.5% points, SplitMix64 seeded 20261026 afresh for each cell.
- Overdispersion: the index is chi-square over (k - 1) with the pooled p. Its null is simulated
  10,000 times (the frozen text gives no count) as Binomial(n_i, pooled p), with the nearest-rank
  99th percentile; an undefined simulated index counts as 0. T_count gets a clustered interval on
  every cell and its overdispersion check on F2 cells only.
- Clopper-Pearson limits are found by bisection.
- H3: the median is over the posts that have a residual. A criterion is incomplete unless all 30
  sweep processes are good.
- H4: throughput is a window's tasks over its `elapsed_ns`. A cell with at least 14 pairs is
  tested on the pairs it has (section 8). s is the sample standard deviation. A pilot cell with
  fewer than 2 pairs gets R = 24 and is underpowered. At N = 0 the median's side is reported, not
  tested.

### 2026-10-27: ANALYSIS_COMMIT cf4c3d85e3cf05bcc6ea6505e6d62cc6b2d6ab77, and the records gate

No pilot or confirmatory data existed. ANALYSIS_COMMIT moves from ad39c9c to cf4c3d8 (Alex's
decision 24 in P1's STATE.md). ad39c9c is superseded, and every rule of the entry above stands at
cf4c3d8. Since ad39c9c, `analysis/v2/` and `bench/run_campaign.py` have gained refusals and the
lines the runner writes, nothing else: no statistic, threshold, test, interval, plan rule, order,
limit or rerun rule changed. The changes are listed below. None of the new files is measured code,
and none is in `bench/code_paths.txt`.

The measured code moves too. `bench/wakeprobe.cpp`, `bench/abpair.cpp` and `bench/CMakeLists.txt`
are code paths, and d9e0ef77a32f59d7afc57ec32ac1a59ad375adea changed them twice. First,
`--validate-args` in both programs: it applies every check of a measuring run and measures nothing
(no loop, thread, pin, clock reading or file), with CTest cases `validate_args.*`. Second,
`--validate-args` of wakeprobe refuses `--wait-us` above the loop's limit of one day
(86400000000), as the loop does when a run creates it, through the same `plan_refusal` that a run
asks first; two CTest cases and one that compares the number with the loop's source were added.
The campaign's builds and the D5 records of this entry are of code paths equal to d9e0ef7's. The
gate matches records by inputs hash, whatever the commit.

The gate runs in the runner, not beside it.

- `bench/run_campaign.py` was fixed at ad39c9c (the entry above). It changes only by refusals and by
  the lines it writes. It takes `--records DIR` and `--other-records DIR`. A timed run runs the gate
  (`bench/gate_lib.py`) in its own process at its start and again at every `--resume`, and
  `--check-gate` runs the gate alone and exits 0 or 2 (it also prints `binary <tool> sha256 <hex>`
  lines). It takes no gate file and no override: the gate hashes the inputs of the actual
  `--build-dir` itself (into a temporary file), reads the records again, and refuses a timed run
  (exit 2, before anything is written) unless every check below holds. A dry run needs no records and
  only warns. `run.txt` gains nine lines (gate, gate compiler, gate code commit, gate code
  fingerprint, gate coverage.json sha256, gate generators, gate records, gate other records, gate
  smoke shape keys), and the hashes of wakeprobe and abpair in it are the gate's. Before each process
  the runner hashes both again; a change from the hashes the gate made stops the run with exit 3 and
  a BINARY-CHANGED line in the log. Plans, order, limits, reruns, resume and the other exit codes
  are as at ad39c9c. `bench/test_run_campaign.py` has 132 tests (100 at ad39c9c).
- The tools. ninja is the build's own (`CMAKE_MAKE_PROGRAM` of its cache), never one from PATH, and
  its folder is put first on PATH for the Lab's `inputs_hash.py`, which calls a bare `ninja`. git is
  `LAB_GIT` when that is set, else `git`; when it cannot be started the refusal names it.
- The build must be a plain Release build of this checkout: `bench/coverage.json` and the code paths
  unchanged, no `WAKELOOP_SANITIZER` (empty or a CMake false constant such as OFF), no toolchain
  file, no interprocedural optimization, `CMAKE_CXX_FLAGS`, `CMAKE_CXX_FLAGS_RELEASE`,
  `CMAKE_EXE_LINKER_FLAGS` and `CMAKE_EXE_LINKER_FLAGS_RELEASE` equal to what a fresh configure gives
  (constants in `gate_lib.py`; `bench/test_records.py` configures a fresh tree on each system and
  compares), on W neither `CL` nor `_CL_` set, binaries that hold no sanitizer-runtime symbol, `ninja
  -n` with nothing to do, and each binary's time in agreement with its `.ninja_log` entry (a binary
  put in place after the build is seen).
- The compiler is the one CMake identified at configure time, which compiled the binaries (cut to
  three version parts: `Clang 22.1.8`, `MSVC 19.51.36246`). A timed run is refused when today's
  banner of the compiler differs from it ("the compiler changed since configure; configure and build
  again"); on L the binaries' `.comment` section must name that clang version too. A resume skips the
  banner, because its binaries are bound to the start by hash, and wants the identity that `run.txt`
  recorded at the start. There is no way to name a compiler.
- Required records are fixed in the gate's code: on L ASan+UBSan, TSan and MSan, on W ASan. A
  declared gap never waives one; `bench/coverage.json` only annotates and its sha256 is written into
  `run.txt`. One record must match all four targets (wakeloop, wakeloop_tests, wakeprobe, abpair),
  the compiler and the configuration, which includes the hash of every tracked `CMakeLists.txt` and
  `*.cmake`. It must be made on the laptop (host names are compared without case), not be a dry run,
  be green, be of the current record format, have been made from the same generator files as today,
  and name its logs archive, which on the system that made it must exist with the sha256 the record
  names.
- A record that matches the inputs and does not fit (other generator files, an older format, a
  missing archive) is skipped with a note, never fatal. A red record of the same inputs stops the
  gate only when it records a finding: a sanitizer report, a failed or timed-out CTest test, or a
  failed or timed-out smoke run. A record that is red for an infrastructure reason (a failed log
  pack, a dirty tree, a host refusal, a job-level problem) never blocks; the record says which, in
  `red_reasons` and `blocking`. Record names carry a UTC time stamp, and the driver checks the
  record's path before it builds, so a rerun at the same commit is never blocked by the name.
- Section 8's "both systems" is checked by the code fingerprint, because the two systems compile
  different backends and so hash differently. The other system needs a green record of each of its
  sanitizers for the same code, by the same rules.
- `analysis/v2/score.py` and `pilot.py` take `--records DIR` (once per folder) and refuse a run
  whose `run.txt` lacks the gate lines, or names a record that is in none of the folders, has another
  sha256, or is not a green record of a real job; with `--draft` each is a warning.
- New files, none of them measured code: `bench/build_inputs.py`, `gate_lib.py`,
  `check_records_v2.py` (a reporting wrapper), `wakeloop_record.py`, `campaign_smoke.py`,
  `sanitize_wakeloop.sh`, `sanitize_wakeloop.ps1`, `keep_record_logs.sh`, `coverage.json`,
  `test_records.py`. The 2026-09 `check_records.py`, `sanitizer_record.py`, `inputs_gate.py` and
  `sanitize_wakeprobe.*` stay as they are.

The records, and the campaign smoke.

- Records are named `wakeloop2-<commit>-<L|W>-<san>-<time stamp>`. A record holds its format, one
  inputs hash per target, the code-selecting options, the compiler and the banner, a fingerprint of the
  committed code paths, the sha256 of the generator files, the smoke's shape keys, every
  `*SAN_OPTIONS` it ran with, the reasons it is red if it is, and the logs archive with its sha256.
  The writer creates the file exclusively and the code fingerprint at the end of the job must be the
  one at its start.
- A record is the build under the sanitizer, the whole CTest suite run serially, and the campaign
  smoke. CTest runs wakeprobe and abpair only as `--check`, which times nothing, so it executes
  none of the measured code. The smoke (`bench/campaign_smoke.py`) runs on the laptop only, inside
  the record job, right after CTest and under the same sanitizer build and environment. It takes
  the shapes from the generators: the canonical entries of `analysis/v2/plan.py` (S, Bt, the phase
  sweep, H4 and the A/A pilot, with a stand-in R of 14) and the cells of H5, from the lines that
  the platform's own `run_h5` prints in its dry run. A shape is the tool, backend, variant or arms,
  workers, protocol and family, with whether calibration is present, whether the bound is blocking
  or timed, whether the gap is random, fixed or a phase target, the timer slack and the timer
  period, and task work. That is 129 shapes on Linux (111 wakeprobe, 18 abpair, 10 of them H5) and
  82 on Windows (70 and 12, 8 of them H5). Each is run once with its own arguments, pinning
  included, but `--posts` at most 5, `--warmup` at most max(2, workers), `--pairs` 1 and
  `--window-ms` 200; calibration and the phase target are kept. Every reduced line is first given
  to the tool's own `--validate-args`, so no copy of the tools' rules exists to drift. Each run has
  the plan generator's C1 limit on the reduced counts. The record is red if any run exits non-zero,
  is killed at its limit, or shows a sanitizer report, or if the runs are not exactly the shapes
  the generators give now. The record carries the shape keys, and the runner refuses a plan line
  whose shape key is not in the record of every sanitizer its system needs. No measurement is read
  or kept: the tools' `--out` files go to a scratch folder and are deleted, standard output is
  dropped, no duration is taken, and only exit codes and report counts are written. The desktop
  runs the smoke with `--list` only, which starts no tool. The full-size runs (630 posts, 10 s
  windows, the other phase targets, the second bound) are not executed under sanitizers
  (`bench/coverage.json`).
- H5 is gated too: `run_h5.sh` and `run_h5.ps1` call `run_campaign.py --check-gate` before any cell
  and run nothing if it exits 2. They keep the gate's output as `gate.txt` in the output folder and
  hash wakeprobe again before every run; a change stops them with exit 3.
- Each job configures a fresh build directory: a reused one keeps `bench_commit` and `bench_dirty`
  from its configure, which are then stale.

### 2026-10-28: each H4 cell's R, from the A/A pilot

The A/A pilot of section 7 ran on the laptop on 2026-10-28 at commit c651e31 (code commit
d9e0ef7), before any confirmatory window. No confirmatory data existed. The rule is unchanged;
this entry records the values it gives.

**The runs**
- `plan.py pilot` made the plans on the laptop from a clean clone at c651e31. Each had the
  sha256 computed on the desktop before the runs: `plans/pilot-linux.jsonl`
  22853d44fd8543e3cde08df75bdd423425b24c2431c897921b341a55fc8be89b (48 processes) and
  `plans/pilot-windows.jsonl` 94a3162f10544dc49a49a57a98fa7465b46f6e35832600aca34ed08f079df1c1
  (32 processes). Both are committed with this entry, byte for byte as run.
- Windows ran from 11:07 to 11:29 and Linux from 11:34 to 12:06 (UTC+2). Each runner passed
  its records gate first, with the four green D5 records at c651e31 (Linux ASan, TSan and MSan;
  Windows MSVC ASan). All 80 processes exited 0 at their first run. None was rerun, void or
  incomplete.
- The laptop job archives are kept in the lab's archive: `p1-pilot-l.tar.gz`, sha256
  932ae04079f1547eecba97f9c3dad32a88b90cfbab33060d2e42227eeeb44a19, and `p1-pilot-w.tar.gz`,
  sha256 681ce7bf2fc43e69f0dfc966a14155fb4e0f2a2062b1f2862ccc16998a003bd4.
- `pilot.py` ran at c651e31 on a clean tree, with `--records` for the Papers repository's
  `lab/sanitizer-records` and its `laptop-w`. It passed every check with no warning. Its record
  is `results/v2/pilot-h4.json` (sha256
  501730480a13531f8eac87f33318f1c795a073e160c213e821373e53abf46800). The block below is its
  `--markdown` output, unchanged.

**What the rule gives**
- Every F4 cell (N = 1, 10 and 100 us, 15 cells) has R = 14, the floor, with power 1.0000 at
  the strictest Holm step; s is 0.000023 to 0.0018. No confirmatory cell is underpowered.
- N = 0 is outside F4, a disclosed repeat (section 3). It has R = 14 on IOCP and on the condition
  variable of both systems. On Linux, epoll (s = 0.0386, power 0.0025) and io_uring (s = 0.2014,
  power 0.0000) reach power 0.8 at no R up to 24. So both have R = 24 and are declared
  underpowered: their tests are run and reported, and a non-rejection reads as inconclusive.

**Read by hand, after pilot.py had given R.** The P1 session printed the per-pair ratios of the
six N = 0 cells and of Linux io_uring at N = 1 us, and the per-window throughput and passes of
the Linux processes of epoll, io_uring and the condition variable at N = 0 and of io_uring at
N = 1 us. All are the fixed loop against itself and compare no arms. Seen: on io_uring at N = 0
the throughput varied from window to window inside one process, from 2.3 to 5.0 million tasks/s,
while the passes per window stayed near 1.47 million. It changed nothing.

<!-- h4-r:begin -->
| cell | system | backend | N (us) | pairs | s | power | R | underpowered |
|---|---|---|---|---|---|---|---|---|
| linux-epoll-N0us | linux | epoll | 0 | 8 | 0.03856057261611845 | 0.0025 | 24 | yes |
| linux-epoll-N1us | linux | epoll | 1 | 8 | 0.0017809977714103686 | 1.0000 | 14 | no |
| linux-epoll-N10us | linux | epoll | 10 | 8 | 0.00006168065338970847 | 1.0000 | 14 | no |
| linux-epoll-N100us | linux | epoll | 100 | 8 | 0.000023188733622744 | 1.0000 | 14 | no |
| linux-io_uring-N0us | linux | io_uring | 0 | 8 | 0.20142119579063086 | 0.0000 | 24 | yes |
| linux-io_uring-N1us | linux | io_uring | 1 | 8 | 0.001682548948616116 | 1.0000 | 14 | no |
| linux-io_uring-N10us | linux | io_uring | 10 | 8 | 0.00007938826071637536 | 1.0000 | 14 | no |
| linux-io_uring-N100us | linux | io_uring | 100 | 8 | 0.0000354274628304525 | 1.0000 | 14 | no |
| linux-condvar-N0us | linux | condvar | 0 | 8 | 0.004334545286409711 | 1.0000 | 14 | no |
| linux-condvar-N1us | linux | condvar | 1 | 8 | 0.0015069575730544385 | 1.0000 | 14 | no |
| linux-condvar-N10us | linux | condvar | 10 | 8 | 0.00014799204947455006 | 1.0000 | 14 | no |
| linux-condvar-N100us | linux | condvar | 100 | 8 | 0.000022910805771093328 | 1.0000 | 14 | no |
| windows-iocp-N0us | windows | iocp | 0 | 8 | 0.0012348727278008228 | 1.0000 | 14 | no |
| windows-iocp-N1us | windows | iocp | 1 | 8 | 0.00021070991749506658 | 1.0000 | 14 | no |
| windows-iocp-N10us | windows | iocp | 10 | 8 | 0.00018927888205106457 | 1.0000 | 14 | no |
| windows-iocp-N100us | windows | iocp | 100 | 8 | 0.00003399050512482959 | 1.0000 | 14 | no |
| windows-condvar-N0us | windows | condvar | 0 | 8 | 0.00475742970184998 | 1.0000 | 14 | no |
| windows-condvar-N1us | windows | condvar | 1 | 8 | 0.0001340583415029833 | 1.0000 | 14 | no |
| windows-condvar-N10us | windows | condvar | 10 | 8 | 0.00010678104029710107 | 1.0000 | 14 | no |
| windows-condvar-N100us | windows | condvar | 100 | 8 | 0.000025560639952983952 | 1.0000 | 14 | no |
<!-- h4-r:end -->

### 2026-10-28: the run plans

`plan.py campaign` made the confirmatory plans at commit 4ec2551 on a clean tree. It read each
H4 cell's R from the block above; the log's sha256 at that commit is
7353b1a2bf9a0387bb08efec32048cdbc8d02248c51280cec544e4db73477be2. No confirmatory data existed.
The plans are committed with this entry, byte for byte as made.

- `plans/plan-linux.jsonl`, sha256
  78102dfaf50ded2e201823794aaa2ef1121806ea69013a4c27daa23a82b78dca: 2,328 processes in 226
  cells (S 1,500, Bt 460, the sweep 180, H4 188).
- `plans/plan-windows.jsonl`, sha256
  3a475a5e3de78b1fbc208289139b1f66a8b20a93ec9cef67f1456c67635a28f9: 1,432 processes in 140
  cells (S 920, Bt 280, the sweep 120, H4 112).
- H4 has R / 2 processes per comparison: 7 in every cell but Linux epoll and io_uring at N = 0,
  which have 12.
- Desktop check, nothing timed: both plans pass the runner's provenance check, and every plan
  line's shape is in the campaign smoke of every record its system needs (the four D5 records
  at c651e31): 113 distinct shapes on Linux, 70 on Windows.
- Each plan header records the log's sha256 at 4ec2551, and score.py refuses a log with another
  sha256. This entry and any later one change it. So the scoring reads the log as it was at
  4ec2551: `git show 4ec2551:revision-log-v2.md > DIR/revision-log-v2.md`, then
  `score.py --revision-log DIR/revision-log-v2.md`. Its h4-r block is the one above.
- `.gitattributes` keeps `*.jsonl` at LF in every checkout, so a checked-out plan has the
  sha256 named here.

### 2026-10-28: four Windows cells incomplete, and a disclosed extra run (Alex, decision 28)

The Windows campaign ran on the laptop on 2026-10-28 at 796b71f, from 12:40 to 16:57 (UTC+2).
Of its 1,432 processes, 1,428 are good. The other four read a timer resolution of 10 ms against
the default of 15.625 ms, at their start, a gap or their end. Some process on the laptop held
that timer from 13:47:37 to 13:47:49. By the rule under "Good processes" a finer reading makes
a process not good. All four exited 0, so the runner did not rerun them (section 8 reruns only
failed runs). Their cells have 9 good processes of 10, so each is incomplete:

- `S-condvar-wrong_worker-W4-B1ms` (p5), `S-iocp-lazy_wake_wide-W4-B1ms` (p7),
  `Bt-condvar-lazy_wake_wide-W1-B1ms` (p8), `Bt-condvar-unobserved_primitive-W1-B10ms` (p5).

The rules are unchanged. In the confirmatory scoring the four cells are incomplete: they stay in
their families' m and are not rejected. When this was decided, no result of the campaign had been
read. Only the processes' goodness, their timer readings and the laptop's 10 s samples had been
read. At 13:47:50 those samples show System at 20% CPU, and NVDisplay.Container.exe and
sppsvc.exe running. Which process asked for the timer is not known.

**The extra run.** The four cells run again whole on Windows, at 796b71f, once the lab has found
and stopped the timer's source.
- The plan is the 40 lines of `plans/plan-windows.jsonl` whose cell is one of the four,
  unchanged, in the plan's order, under the plan's header. Its sha256 is
  662b5c34f64c4b41e6e0c28c142565f7b90ffb4b6c9a9fa22c01d1dd84fd708d. The job makes it from the
  committed plan and checks this sha256 before any run.
- It runs under the campaign runner and its records gate, with the same seeds and the same rules
  for good processes and reruns.
- Its cells are reported beside the confirmatory results, as an extra run with this cause. They
  never enter F1, F2 or any Holm step, and they decide nothing.

### 2026-10-29: the confirmatory scoring

Both systems' campaigns ran at 796b71f (chain A16) and exited 0. Archives, sha256 matched:
- Linux: `gap37-a-q1-p1-campaign-fba5024d/p1-campaign-l.tar.gz`, sha256
  239784122130b3bf6f855849faad1a55f3eeb139d3b8d3665037a828aa1e331a. Run 2026-10-28 18:09 to
  2026-10-29 00:14 (UTC+2). All 2,328 processes are good at their first run: no rerun, no void
  run, no lost unit.
- Windows: `gap37-w-q1-p1-campaign-e3b32dd9/p1-campaign-w.tar.gz`, sha256
  9048d6ab407b635edc28020f3e25243cd866ad3577404b2ddfa772f64ecceedc. 1,428 of 1,432 good (the
  entry above).

The scoring:

    git show 4ec2551:revision-log-v2.md > DIR/revision-log-v2.md
    python analysis/v2/score.py --linux plans/plan-linux.jsonl L --windows plans/plan-windows.jsonl W
        --revision-log DIR/revision-log-v2.md --records <Papers>/lab/sanitizer-records
        --records <Papers>/lab/sanitizer-records/laptop-w --out results/v2/score.json

- L and W are each archive's `out/campaign`. The log copy has the sha256 of the plan headers,
  7353b1a2bf9a0387bb08efec32048cdbc8d02248c51280cec544e4db73477be2.
- score.py ran at 4d51e63 on a clean tree, so the output names 4d51e63. The code is
  ANALYSIS_COMMIT's: no file under `analysis/`, `bench/`, `loop/` or `tests/` and not
  `CMakeLists.txt` changed between cf4c3d8 and 4d51e63.
- No refusal, no warning, no departure from either plan, one bench commit (796b71f) for every
  good process, and both systems' gate lines checked against the four D5 records.
- `results/v2/score.json`, sha256
  ecb624940338508ee7ced13b1243ecd137eaed273c17d85e7b6a8aaae9b51242, is committed with this
  entry byte for byte as made. Its summary line: "H1: 101 of 102 F1 cells rejected; H2: 80 of
  80; H3: 10 of 10 criteria hold; H4: 15 of 15 F4 cells".
- Of the four incomplete Windows cells, one is in a family: `S-condvar-wrong_worker-W4-B1ms`,
  in F1's m and not rejected. It is the one F1 cell not rejected. The other three are in no
  family: the two widened lazy-wake cells are reported per unit, and
  `Bt-condvar-unobserved_primitive-W1-B10ms` is a predicted blind spot.

**How the extra run is scored**, fixed now, before its data exist (decision 28 left it open):
1. Its own output folder is checked first, as the campaign's: the runner's gate lines, and
   every process good by the same rules.
2. A copy of the Windows campaign folder is made, and in it every file of the extra plan's 40
   processes (`<id>.*` and `<id>-rerun.*`) is replaced by the extra run's files of the same
   names. Every other file stays, `run.txt` included.
3. score.py runs as above with that copy as the Windows folder. So the four cells take B_eff
   from the campaign's absent-wake cells, as in the confirmatory scoring.
4. Only the four cells are read from that output: `cells`, their entries in `blind_spots`
   and `lazy_wake_per_unit`, and the F1 cell's entry in H1's tests, whose exact p is not
   compared with any Holm threshold. That output's family counts and Holm steps are not
   results, and they are not reported.

Desktop check of steps 2 and 3, nothing new read: with the campaign's own files of the 40
processes standing in for the extra run's, the output is byte for byte `results/v2/score.json`.

### 2026-10-29: the extra run goes ahead with the timer's source unknown (Alex, decision 32)

The entry of 2026-10-28 runs the extra run once the lab has found and stopped the timer's
source. The lab looked twice on the laptop's Windows, with `powercfg /energy`: for 60 s at
00:26, 34 s after boot, and for 1 h from 00:28, 2 min after boot. Both reports list no timer
request, and both show the default resolution, 156250 in 100 ns units. So the source is not
known. It may appear only under load.

Alex chose to run the extra run now (decision 32). Its rules are unchanged: the run line exits 7
before any run if the timer is finer than the default, a process that reads a finer timer is not
good, and a cell without 10 good processes stays incomplete and is reported so. This entry is
written before the extra run's data exist.

What the campaign's own readings show (every Windows process reads the resolution at its start,
at gaps and at its end, and the runner starts one process at a time; none of them asks for a
timer):
- The process that started at 13:47:37 read the default at its start and at all five gaps, and
  10 ms at its end (13:47:46). The process that started at 13:47:49 read 10 ms at its start and
  gaps, and the default at its end (13:48:05). The processes just before and after read the
  default throughout.
- So the request began after 13:47:37 and before 13:47:46, and it ended after 13:47:49 and
  before 13:48:05. The entry of 2026-10-28 put it "from 13:47:37 to 13:47:49", the start times
  of the first and the last process that read it; these are the bounds.
- It spanned four separate wakeprobe processes, and a timer request ends when its process exits,
  so no wakeprobe process held it.

### 2026-10-29: H5's scorer, before any H5 value is read (Alex, decision 30)

Both H5 runs are done at 796b71f (chain A16), each gated by the four D5 records:
- Linux: `gap37-a-q2-p1-h5-a143a1dd/p1-h5-l.tar.gz`, sha256
  ad8ddab9d7ffc1c7a2c3e6963f4b2aeaf96fa49e799e47eff326c1e6c1b0b6d1. 140 of 140 cells exited 0.
- Windows: `gap37-w-q2-p1-h5-cb30a602/p1-h5-w.tar.gz`, sha256
  ff8971fe63317de23b475b4d56d023d05ca0150e311259b1c7e0e86c20552bd5. 136 of 136 cells exited 0.

Section 3 scores H5 with the 2026-09 criteria, and the 2026-09 code that computes them is
`analysis/summarize.py`. It cannot read v2's runs as they are. It takes a run's arm from the
build (`defect_active`), and in v2's one binary H5's fixed arm, `plain_wake`, is a defect
variant. So every fixed-arm run is refused, the whole bound sweep among them. This was found by
loading the runs and printing only the loader's refusals and the record keys; no value was
printed.

`analysis/v2/h5.py` (this commit, with `test_h5.py`) scores H5 with summarize.py's own loader,
tables and limits. Its module text is the rule; in short:
- The arm comes from H5's mapping (`plain_wake` fixed; `absent_wake` on epoll and IOCP,
  `unobserved_primitive` on io_uring defect). Every run must match its plan.txt line field by
  field and the mapping.
- plan.txt must hold the runners' design (120, 8 and 12 cells on Linux, 120, 8 and 8 on
  Windows; 51 posts and 5 warm-up, 21 and 3 in the gap sweep), and every run one worker pinned
  to CPU 14 with the poster on CPU 2.
- Runs follow section 8's rules for lost units, as readers.py states them for the campaign. A
  run fails when it left no output, has no summary, reports an error, lost a unit, delivered
  fewer measured units than planned, or is not a Release build from a clean tree. A rerun
  replaces the first run, and both are reported. A cell whose used run failed is incomplete,
  and a lost unit in a fixed cell is reported as a lost wake.
- The records gate in each folder's gate.txt is checked as score.py checks run.txt, against the
  records folders, and the wakeprobe sha256 in plan.txt must be the one the gate hashed.
- Delay-model fit per configuration (system, timer setting, B): it fails when a cell fails; it
  passes when no cell is incomplete, at least one has a verdict and every verdict passes;
  otherwise it has none. A bimodal cell has no verdict, as in 2026-09, and neither has a
  gap-sweep cell with an incomplete step. Bound independence per (system, backend, timer
  setting); a group with an incomplete run fails if the rest fails and has no verdict otherwise.

The reading's rule, fixed here before any H5 value is read:
- part 1 recurs when the failing delay-model configurations are exactly Windows, 1 ms timer,
  B = 1 ms;
- part 2 recurs when every Linux group fails and every Windows group passes;
- (a) both parts recur, (b) neither recurs, (c) exactly one recurs. This is section 3's wording
  read literally: "neither part of the split recurs" covers failures in new places too, as long
  as neither 2026-09 pattern is seen again.
- A part is undetermined when configurations or groups without a verdict could still decide it
  either way, and then so is the outcome.

Also reported, with no verdict: Windows runs whose timer readings differ from what they asked
for, and Linux runs that asked for 1 ns of slack and did not get it.

Desktop checks: 41 tests on synthetic folders in both runners' plan formats, among them the
reading against an oracle that tries every completion of the verdicts that are missing, and the
whole `analysis/v2` suite (346 tests) on Windows and in the Linux image. Two Opus reviews. The
first found two HIGH issues (a run that lost a unit was scored as complete; an unscored key
configuration read as not recurring) and five MEDIUM ones. The second found no CRITICAL or HIGH
issue and four MEDIUM ones (a run that left no output; plan.txt not checked against the
runners' design; pinning not checked; an empty group list read as not recurring) and two LOW.
All are fixed here. On the real folders only the structure was checked: every plan line, run
and gate passes, all 140 and 136 cells are complete, and nothing was scored.

This code is written after the H5 data arrived, which is disclosed here. ANALYSIS_COMMIT for H1
to H4 stays cf4c3d8: nothing it ran changes, and their scoring is done (f3bd9ec).

### 2026-10-29: H5 scored

H5 was scored at 72aa42a, on a clean tree, after that commit was pushed (Alex, decision 33):

    python analysis/v2/h5.py --linux L --windows W --records <Papers>/lab/sanitizer-records
        --records <Papers>/lab/sanitizer-records/laptop-w --out results/v2/h5.json

- L and W are the `out/h5` folders of the two archives named in the entry above.
- No refusal, no warning. All 140 and 136 cells are complete, at their first run, from one
  bench commit (796b71f). There is no lost wake, no timer note and no slack note.
- `results/v2/h5.json`, sha256
  022b41bf7f7168a22315f471d3bf586669d27ffd0320a7c1cbbdb88e5debf9dd, is committed with this
  entry byte for byte as made. Its summary line: "H5: 1 delay-model configurations failing, 3
  bound-independence groups failing, 0 incomplete cells; reading c".
- Part 1 recurs: the only failing delay-model configuration is Windows, 1 ms timer, B = 1 ms
  (its gap-sweep cell).
- Part 2 does not recur. Both Linux groups fail, as in 2026-09, but of the two Windows groups
  the 1 ms timer fails and only the default timer passes.
- By the rule fixed before scoring, the reading is (c): reported as found, with no attribution.

### 2026-10-29: the extra run, checked and scored

p1-extra-w ran on the laptop's Windows on 2026-10-29 (chain A18) at 796b71f, from 18:43:33 to
18:48:20 (UTC+2), more than 15 min after boot, and exited 0. Archive
`gap39-w-q1-p1-extra-ddc81f9b/p1-extra-w.tar.gz`, sha256
8f7e541e9671951a92482ae913b4e5abcf1b017d140e04a7ea4d8c1c2adab798, matched.

- The plan made in the job has the logged sha256 (662b5c34...). Before the runner started, the
  timer guard read the default resolution. The gate lines pass against the four D5 records; one
  bench commit (796b71f); no rerun, no void run, no lost unit.
- 38 of 40 processes are good. Two read a 10 ms timer again:
  `S-condvar-wrong_worker-W4-B1ms-p7` (started 18:47:37; the default at its start and gaps,
  10 ms at its end, 18:47:45) and `Bt-condvar-unobserved_primitive-W1-B10ms-p9` (10 ms at its
  start and gaps, the default at its end, 18:48:00). So the request began after 18:47:37 and
  ended before 18:48:00.
- The laptop's sample at 18:47:42 shows taskhostw.exe at 28% CPU and System at 23%, with
  MoUsoCoreWorker.exe, WmiPrvSE.exe, SecurityHealthService.exe and NVDisplay.Container.exe.
  Both incidents began at hh:47:37 (13:47:37 on 2026-10-28, 18:47:37 on 2026-10-29). The source
  is still not known.
- Scored by the entry of 2026-10-28 (the overlay), with score.py at 9d0d942 on a clean tree.
  Its code is cf4c3d8's: since then only `analysis/v2/h5.py` and `test_h5.py` were added, and
  score.py imports neither. The output `results/v2/score-extra-w.json`, sha256
  b4263a58681efcdf136e045e1097d77e9d3141e75181e608f6c63c35af7536d1, is committed with this
  entry byte for byte as made. Only the four cells are read from it:
  - `S-iocp-lazy_wake_wide-W4-B1ms`: complete. T_med flags 0 of 300 trials (exact 95% interval
    0 to 0.0122), so its blind-spot prediction holds. Per unit, 17 of 6,300 units exceed half of
    B_eff.
  - `Bt-condvar-lazy_wake_wide-W1-B1ms`: complete. T_med flags 0 of 300; the prediction holds.
    Per unit, 0 of 6,300.
  - `S-condvar-wrong_worker-W4-B1ms`: incomplete again (9 good processes, which flag 270 of 270
    trials).
  - `Bt-condvar-unobserved_primitive-W1-B10ms`: incomplete again (9 good processes, which flag
    0 of 270).
  As the entry of 2026-10-28 says, these cells decide nothing.
- A diagnostic the lab asked for (on Windows, a job that starts after idle ran 3 to 10% slow in
  its first 30 s in P2): the six processes that started in this run's first 30 s have the same
  durations as their cells' later processes, and their median delays lie within or at the top
  of the later ones' range. Nothing is concluded from it.

### 2026-10-29: no further extra run (Alex, decision 35)

After the extra run, two Windows cells are still incomplete: `S-condvar-wrong_worker-W4-B1ms`
and `Bt-condvar-unobserved_primitive-W1-B10ms`. Alex chose to stop here. Both cells are
reported as incomplete in the confirmatory scoring and in the extra run, with the timer readings
that made them so. No further run of them is made. The confirmatory results do not depend on
them: the first is one of F1's 102 cells, counted in its m and not rejected, and the second is
in no family.

### 2026-10-29: what the lab found about the 10 ms timer (read-only)

The lab ran a read-only check on the laptop's Windows at 20:41 on 2026-10-29: scheduled tasks
whose last run falls in either window, enabled tasks with a trigger at minute 47, and the
System, TaskScheduler and Application logs around 13:47 on 2026-10-28 and 18:47 on 2026-10-29.
It found one candidate per day, a different one each day, and proved neither:
- 2026-10-29: the scheduled task
  `\Microsoft\Windows\DeviceDirectoryClient\RegisterDeviceLocationRightsChange` last ran at
  18:47:33, 4 s before the window began. It has no next run, so it is started by an event.
- 2026-10-28: the Application log has Microsoft-Windows-Security-SPP event 16394 ("Offline
  downlevel migration succeeded") at 13:47:41 and event 16384 at 13:48:12, which reschedules the
  Software Protection service. This matches sppsvc.exe in the laptop's sample at 13:47:50.
- No enabled task has a trigger at minute 47, and the TaskScheduler log has no event in either
  window (it is probably off).
So both incidents fall at the time of Windows' own background services, not of anything P1
runs. The host was not changed. This is a disclosure; nothing is concluded from it.

### 2026-10-29: the D5 gate for H6 (Alex, decision 37)

H6's harness is Java, and no sanitizer applies to JVM bytecode. Alex chose a separate gap file
(his answer to the lab, relayed verbatim: "Separate gap file (Recommended)"; the option read: "A
new bench/coverage-h6.json declares the Java harness and Netty's 2016 native epoll library a
sanitizer gap and lists each input's hash. Green functional tests on Linux at the measured
commit stand in for a record. The existing coverage file and gate lines stay unchanged."):
- `bench/coverage-h6.json` declares the gap and names every first-party H6 input by path with
  its hash, and the two Netty jars by their sha256. Green functional tests of the harness on
  Linux, run in check mode at the measured commit, stand in for a record.
- `bench/coverage.json`, `bench/code_paths.txt` and every D5 record stay as they are. The H6
  code lives under `historical/netty/`, outside the records' code paths, so the campaign's
  records and gate lines remain valid.
- Any first-party C or C++ code in H6 (JNI or a native helper) would need a normal record.

### 2026-10-29: H6 frozen (Alex, decisions 36 to 39)

- Decision 36: H6 is Netty's Epoll event loop on the laptop's Linux, CR7 against Final, forced
  and natural. It narrows decision 6, which named NioEventLoop: read at the CR7 tag, the NIO
  loop keeps its wakeup pending, so the defect barely arises there (derived, not run; the note
  is in `hypotheses-v2-h6.md` section 1).
- Decision 37: the D5 gate for H6 (entry above).
- Decision 38: `hypotheses-v2-h6.md` is frozen from draft 3 (d70b9c2). Drafts 1 (c61e1d4) and
  2 (79513c6) came before. Two Opus checks: the first found one HIGH issue (`run()` sleeps 1 s
  after any exception it catches, in both versions, a confound the size of the defect) and six
  MEDIUM ones; the second found none HIGH and two MEDIUM ones. All were fixed before the freeze.
  The freeze commit changes only the status line.
- Decision 39: the freeze is pushed with the commits before it.
- No run of Netty 4.1.0.CR7 was made before the freeze, on any machine.

### 2026-10-30: H6 run and scored

The H6 code was committed at 5170eeb and fixed after two Opus reviews (e90d147, then a7bb33e).
a7bb33e was pushed with Alex's yes (decision 40) and is the measured commit. The lab ran it as
chain A25 on the laptop's Arch. Both jobs exited 0. The archives' sha256 were checked here:
- `p1-h6-functional-l.tar.gz`, fa7bf0215ded7ce7feca92617d22fe660fa6119f3e3c92029c564f264c96eb4a.
  Its record `h6-functional-a7bb33e74-L-20261029t220132.json`, sha256
  abc0db2e81648016489c22dcaadbee3951fa75f7dadfac4f3d0d4a83ea8fd236, is green: host
  alex-laptop, lab job set, kernel 7.2.6-arch2-1, all four steps passed. So section 7 holds: the
  native library of each jar loads on the laptop's Arch.
- `p1-h6-l.tar.gz`, b5cf14fedb1e8cfef2fba9d79d3813e95cceca5b515ac9c96c99745fbde4130e. Its
  `run.txt`: gate passed, host alex-laptop, the commit, the record above, both jars and the JDK
  as pinned, 0 incomplete processes. All 40 processes are good at their first run, and every
  `.err` file is empty. No resume, no void file.
- The run took 2 h 9 min 37 s (22:04:15 to 00:13:52 UTC). Section 8's 3 h had no measured
  basis; this is now the measured basis for a run of this shape.

H6 was scored at a7bb33e, on a clean tree, after that commit was pushed:

    python analysis/v2/h6.py --run <run>/out/h6 --records <functional>/out/functional
        --out results/v2/h6.json

- `<run>` and `<functional>` are the two archives' job folders. No refusal, no warning. All four
  cells are complete, with 10 of 10 good processes each. A second scoring gave the same bytes.
- `results/v2/h6.json`, sha256
  23b6f62898e35673a8d92b76bdb0c1d7637763449dfdfcd2173ec92dba57b03b, is committed with this
  entry byte for byte as made. Its summary line says that 2 of 2 F6 cells are rejected and no
  cell is incomplete; it prints each p as an exact fraction.

H6a holds (family F6, Holm, m = 2, alpha 0.025):
- CR7 forced: T_med flags 150 of 150 trials. p = 0.95^150 = 4.56e-4, rejected at the first step
  (0.0125).
- Final forced: T_med flags 0 of 150 trials. p = 4.56e-4, rejected at the second step (0.025).
- Section 5's diagnostics over all 6,300 forced posts: the hook read `wakenUp` 1 at every post on
  both arms, as predicted; no task was pending when it ran; every latch was counted down in
  time. On CR7, Y's delay after the hook returned has median 1.0013 s (range 1.0001 to
  1.0045 s), as section 3 derives. On Final its median is 6.9 us, and its largest 61.6 us. X's
  own delay has median 71 us on CR7 and 68 us on Final. Final's largest forced delay is 224 us.

H6b (exploratory, no test). Every prediction holds:
- T_med is blind on both natural arms: 0 of 150 trials each (exact 95% interval 0 to 0.0243).
- Posts delayed by more than B / 2: 318 of 3,150 on CR7 (0.101; exact 95% interval 0.0906 to
  0.1120); 0 of 3,150 on Final (one-sided upper 95% bound 9.5e-4). Final's largest natural delay
  is 13.2 us.
- T_count flags 71 of 150 CR7 trials (0.473; exact 95% interval 0.391 to 0.556; clustered 0.293
  to 0.667) and 0 of 150 Final trials. The CR7 cell is overdispersed (index 6.33 against the
  threshold 2.31).
- CR7's hits by process, 0 to 9: 86, 71, 11, 29, 0, 22, 1, 26, 29, 43. Process 4 had none (its
  largest delay is 0.21 ms). By trial index within the process, 0 to 14: 19, 23, 28, 33, 22, 23,
  34, 29, 28, 9, 19, 19, 6, 10, 16. Every hit is listed in the JSON.
- Realized gaps: median 150 ns on CR7 (largest 15.6 us), 131 ns on Final (largest 4.5 us).
- No post passed the hard limit on either arm, so there is no lost wake. The largest delay of
  the run is 1.0045 s (CR7 forced).

Reported as sections 4 and 5 ask:
- `EpollEventLoop::run` has no PrintCompilation line in any of the 20 CR7 processes. Each of them
  has 45 to 60 `io.netty` compile lines, so the lines are read. So all CR7 data, forced and
  natural, come from an interpreted loop. Nothing is excluded. A likely reason, derived from the
  counts and not tested: each CR7 hit costs about 1 s, so the 10 s warm-up makes only 529 to
  2,123 posts, against 3.44 to 3.83 million on Final.
- Final natural: `run()` has OSR code at the first measured post in all 10 processes. In each,
  that tier-4 code is made not entrant 10 to 12 ms after the first measured post (process 0:
  10,214 and 10,226 ms). The two times come from different JVM clocks (RuntimeMXBean uptime
  and PrintCompilation's stamp), and the posts carry no uptime, so whether this falls inside or
  after the measured posts is not known.
- Final forced: `run()` has no OSR code at the first measured post in any process. Its tier-4
  OSR code is made not entrant at 10,211 to 10,216 ms, after the natural warm-up, at about the
  time the hook's armed branch first runs (section 3 foresaw this). The first measured post is at
  15,714 to 15,722 ms. A tier-3 OSR compile comes at about 291.8 s in each process. H6a does
  not depend on it.
- Warm-up (descriptive): every CR7 process made exactly 10 warm-up posts delayed by more than
  B / 2. That is a ceiling set by the 10 s warm-up at about 1 s per hit, not a rate. No Final
  process made any.
- No GC fell in any measured trial of any process, and none was ambiguous. Timer slack is
  50,000 ns on the loop and main threads of every process.

### 2026-11-03: the paper's macros, tables and figures

The paper pipeline (Alex, 2026-11-03) gives the writing pack to this session. Its numbers come from
new generators under `analysis/v2/`, committed in 55e085d after every scoring; they score nothing
again.
- `paper_words.py` (wording, macro names, number formats), `paper_setup.py` (testbed facts from the
  run records), `paper_macros.py` (every number as a TeX macro), `paper_tables.py` (table bodies),
  `sweep_csv.py` (the phase sweep's posts, for the delay figure) and `paper_figures.py` (three
  figures), with `test_paper.py` and `test_paper_figures.py`.
- They read only committed outputs (`score.json`, `h5.json`, `h6.json`, `score-extra-w.json`,
  `pilot-h4.json`) and, for constants, `tests/CMakeLists.txt`, `tests/wake_tests.cpp`,
  `loop/include/wakeloop/defect.hpp` and `bench/wakeprobe.cpp`. `tests/`, `loop/` and
  `bench/wakeprobe.cpp` are unchanged since the measured code commit d9e0ef7. `score.py`, `h5.py`,
  `h6.py`, `pilot.py`, `readers.py` and `grid.py` are unchanged.
- `results/v2/setup.json` comes from the Linux campaign job's `run.kernel.json` and `build.log`, the
  Windows job's `build.log` (from `p1-campaign-l.tar.gz` and `p1-campaign-w.tar.gz`, extracted into
  new folders after their sha256 matched the entry of 2026-10-29), the four D5 records of c651e31
  (Linux ASan, TSan and MSan, Windows ASan) and the frozen testbed paragraph, which alone gives the
  laptop, the BIOS and the Windows build. Each input's sha256 is in the file.
- `results/v2/h3-sweep.csv` comes from the same two archives through the committed plans: 6,300
  posts. Each sweep's median of |residual| / B_eff, recomputed from the rows, equals H3's value in
  `score.json` exactly; the extractor refuses otherwise.
- The A/A pilot ran at c651e31, which differs from 796b71f only in the run plans, `pilot-h4.json`,
  `.gitattributes` and this log; the macro `PilotCommit` names it.
- One reading rule is the generator's, not the frozen file's: an N = 0 prediction holds when
  `score.py`'s median position (below 0.98, within the margins, above 1.02) is the predicted one. By
  it, 3 of the 4 predictions hold; IOCP's (below 0.98) does not, at 0.9955.
- An Opus review found 2 HIGH issues: rates at two places printed 299 of 300 as 1.00, and the plain
  wake's range macros included N = 0 while the fixed loop's did not. It also found 5 MEDIUM: the
  delay-model table showed the cells' B_eff, not the sweep's own; the name of the underpowered count
  read as confirmatory; macro values were not checked; the extra run's and the pilot's commits were
  not checked; tests could not catch swapped values. All were fixed before 55e085d: rates use three
  places, and a share strictly between 0 and 1 never prints as either; the N = 0 and confirmatory
  ranges are separate macros; the table shows both B_eff; values must be integers or safe TeX; the
  extra run's commit is checked and the pilot's named; tests compare a regeneration with the
  committed outputs byte for byte. Its LOW items were fixed too, or are stated in the modules.
- A render of every macro, table body and figure passes `lab/bin/check_manuscript_terms.py` with no
  hit. 49 tests of the new modules pass.
- Outputs (sha256): `results/v2/macros.tex`
  afca5a0e5d177f30cecec3b1203e839bc73ec9684f88624e871c3a0475873145; `results/v2/setup.json`
  048b656b594c8373f30a4ee57870d62c85c39323c593b3501e14ab84a7bda489; `results/v2/h3-sweep.csv`
  d6a0ca9cf482041e6842fda4054c7aae0eff88825c29f5126b35c2e5ffbac3c8; `figures/sawtooth.pdf`
  55a0b9c33723327ff3afaa03ea80799551e7d621c92ff90a1eacae1cfca1d0ca; `figures/detection.pdf`
  0b5074cc20ac74045ea75b99532f91696b4d4b849945b1f05e93cc2472280fc0; `figures/fixcost.pdf`
  cc174ae639fae3f3f9249e32dbc28a06587053b0673f57e63329f8daf17f6036; the nine table bodies under
  `results/v2/tables/` as committed.

### 2026-11-03: macros added after the review of the writing pack

An Opus review of the writing pack against the frozen files and the results (4 HIGH, 11 MEDIUM, no
CRITICAL; every macro value it checked matched) asked for numbers the pack had no macro for. Added
to `paper_macros.py`, read from the same inputs: the hard limit (`kHardLimit` in
`bench/wakeprobe.cpp`); plain wake's ratio per cell at N = 0; the lazy-wake cells with posts above
B_eff / 2, split into the natural and the widened race, and the same count in the correct loops'
cells as their background; the realized gaps of single posts per system and bound; the number of
cells whose dispersion index is defined. The functional table's column words no longer spell a count.
No value already in the macros changed. `results/v2/macros.tex` is now
24d226bd633a22f893c8b204caae8b7b1c870e65857cc93b2ed326409014cb47 (435 macros) and
`results/v2/tables/functional.tex`
6a9352a97c935b2cde90bcf89e0e44a486f9b4ac5897f02f6e2c29aa5ca2abfa. The render passes the
manuscript checker with no hit. The review's points on wording and disclosure are fixed in the pack
(in the Papers repository), not here. One finding is recorded here: the design named the test
suite's run on the release build at the campaign commit as the evidence for the functional verdicts,
and no such run was archived; the evidence is the suite's runs under the four sanitizer builds of the
same inputs, on the laptop, and the pack says so.
- Later the same day: the right panel's title of `figures/fixcost.pdf` ran past the figure's edge; it
  now takes two lines. No data or other figure changed. `figures/fixcost.pdf` is now
  bed41f450dd4a1c8f579aa20e3ca79cb4d61f8f09ab317ba439a3db37804b2f8, the same from two runs.

### 2026-11-07: macros and table bodies for the writer's fact requests

The paper's writer asked for numbers the generated outputs did not hold. Added, and nothing that
existed changed: the first 435 macro lines of `results/v2/macros.tex` and the nine table bodies
committed at 141c4a5 are byte for byte as before. No scorer changed: `score.py`, `h5.py`,
`readers.py`, `grid.py`, `plan.py` and the code under `bench/`, `loop/` and `tests/` are as they
were, and `score.json` and `h5.json` were not regenerated.
- `analysis/v2/raw_facts.py` (new) reads the raw outputs with `score.py`'s and `h5.py`'s own
  functions, unchanged, and writes `results/v2/raw-facts.json`: per good process of each power and
  false-positive cell, its trials flagged by the test (score_process against the cell's B_eff from
  `score.json`); the median round delay of the undrained-edge batch cells; and the correct arm's
  median delay per bound of the prior study's bound sweep (h5.py's read path with the records gate,
  2026-09's bound_sweep_tables). Every sum is checked against `score.json` and every group's
  smallest, largest and ratio against `h5.json`, or nothing is written. Inputs: the campaign and
  H5 archives, each verified against its .sha256 before use: `p1-campaign-l.tar.gz`
  239784122130b3bf6f855849faad1a55f3eeb139d3b8d3665037a828aa1e331a, `p1-campaign-w.tar.gz`
  9048d6ab407b635edc28020f3e25243cd866ad3577404b2ddfa772f64ecceedc, `p1-h5-l.tar.gz`
  ad8ddab9d7ffc1c7a2c3e6963f4b2aeaf96fa49e799e47eff326c1e6c1b0b6d1, `p1-h5-w.tar.gz`
  ff8971fe63317de23b475b4d56d023d05ca0150e311259b1c7e0e86c20552bd5; plans `plans/plan-*.jsonl`.
- `paper_macros.py` takes `--raw` and `--sweep` and adds 154 macros: interval ends over the power
  and false-positive cells and over the incomplete blind cells; process-level counts; the tail
  statistic per trial in the lazy-wake cells and the lazy-wake cells with no post over B_eff / 2 by
  backend and system; the phase sweep's median |residual| / B_eff per period of the phase target
  (from `h3-sweep.csv`, each pooled median checked against H3); the semantics tests' count and
  constants (`tests/CMakeLists.txt`, `tests/wake_tests.cpp`); the constants of wakeprobe's
  calibrate(); the passes and full bounded waits of a batch round under undrained edge
  (`defect.hpp`, `wakeprobe.cpp`) with the measured round delay over B_eff; the prior study's
  correct-arm median per bound.
- `paper_tables.py` takes `--raw` and adds five bodies: the exact and the clustered 95% interval of
  each detection cell, for single posts and batch rounds, in the rows and columns of the rate
  tables; and the process-level counts per power and false-positive cell.
- A disclosed observation, not explained here: the undrained-edge rounds took a median of about two
  B_eff on Linux, as the code predicts (two full bounded waits per round), and somewhat less on
  Windows.
- Outputs (sha256): `results/v2/macros.tex` (589 macros)
  5721c4b6ea39d69f88b331715dda9e69a58a93ac4ce2d085d0130cf4bf7d97fe; `results/v2/raw-facts.json`
  5306e84ca5d2ca5189647115c80505f769bc78c78260bfade3af2cb1ade3b66f; new table bodies
  `detection-single-exact.tex` a942a9a37eebec2adcec807e57df05cf91992675b7e101010435bd42572d431b,
  `detection-batch-exact.tex` 14d15c42ba3cdb5cbefa4c534889a63ebb8ded4123eb2424ab9e634c6a7f25e6,
  `detection-single-clustered.tex` f39b2fa4aa30999934dc4834e2ac3c6fb759de5314abeea6cbdfeb2ccff085a1,
  `detection-batch-clustered.tex` bde5d3b83aba067e3fd1ad496ff3198f95c245684acb6213d4b3275a27b524c6,
  `process-counts.tex` 06f090ea4895035fe18b4fea72eb0c91b73953aed5768d25f5fe973157b34b38. Two
  regenerations give the same bytes. A render of every macro and the new bodies passes
  `lab/bin/check_manuscript_terms.py --paper p1` with no hit. All tests under `analysis/v2` pass.

### 2026-11-07: macros and a table body for the writer's round 4 fact requests

The writer asked for four more facts: how one functional verdict covers the backends, the posts of
the phase sweep's figure outside its main range, the prior study's designs as run, and the
throughput of each arm of H4. Added, and nothing that existed changed: the first 589 macro lines of
`results/v2/macros.tex`, every table body and every earlier block of `results/v2/raw-facts.json`
are byte for byte as before. No scorer changed: `score.py`, `h5.py`, `readers.py`, `grid.py`,
`plan.py` and the code under `bench/`, `loop/` and `tests/` are as they were, and `score.json` and
`h5.json` were not regenerated.
- `analysis/v2/raw_facts.py` adds two blocks. `h4_throughput`: per H4 row of `score.json`, the
  median tasks per second of each arm's abpair windows (`readers.throughput`) over the good processes
  `score_h4` used; the pair ratios recomputed from those windows must equal the row's ratios, in
  order, and each window's arm must be its side's. `prior_design`: each system's H5 `plan.txt`
  (`h5.read_plan`), refused unless `h5.design_problem` finds it to be the runners' design. Inputs:
  the same four archives as the entry above, each verified again against its .sha256.
- `paper_macros.py` adds 79 macros: per sweep of `figures/sawtooth.pdf`, the posts past the last
  whole period of the phase targets, the posts with a delay above one B_eff (the closed form's
  top), the posts whose |residual| / B_eff exceeds the delay-model limit, and the largest
  |residual| / B_eff (from `h3-sweep.csv`, each post over its own process's B_eff, as the figure);
  and the prior study's designs as run (runs per design and system, posts and warm-up per run, the
  gap sweep's steps, Linux's fixed gaps and Windows's phase targets, the bound sweep's gap, the
  matrix's gap factor), with the cells of each delay-model configuration of `h5.json` checked
  against the plans (one per gap sweep plus one per defect matrix run).
- `paper_tables.py` adds the body `throughput`: per row of `fixcost`, the median tasks per second
  of the fixed loop, its absent wake, the plain wake and its absent wake, rounded to whole tasks.
  The table's ratio is the median of the pair ratios, not a ratio of these medians.
- Outputs (sha256): `results/v2/macros.tex` (668 macros)
  626bac7e567c569bcf3f4f7e573e0e6561ba3b5e291152162c5ca783d1865bfc; `results/v2/raw-facts.json`
  15db3f8a3db3e9404626225099c8639d70419d8a8beab128a86f9d7ff6948909; `tables/throughput.tex`
  fec2a75fbb25bf0b9136e12fab6ab21d404e4f2463b338d264f4787e10cecc9a. Two regenerations give the same
  bytes. A render of every macro and the new body passes `lab/bin/check_manuscript_terms.py
  --paper p1` with no hit. All tests under `analysis/v2` pass.

### 2026-11-07: macros, table bodies and figures for the writer's round 5 requests

The writer asked for five more things: a clustered interval of the buggy Netty version's natural
posts above half the bound, the flag rates of the two detection tables with the requested bound B
as the threshold base, the trial medians over B_eff of the power and false-positive cells, whether
a literature search record exists (answered in the Papers repository, not here), and figures whose
"fi", "ff" and "fl" and row labels print in every PDF reader. Added, and nothing that existed
changed in value: the first 701 lines of `results/v2/macros.tex` (668 macros), the fifteen earlier
table bodies and every earlier block of `results/v2/raw-facts.json` are byte for byte as before. No
scorer changed: `score.py`, `h5.py`, `h6.py`, `readers.py`, `grid.py`, `plan.py` and the code under
`bench/`, `loop/` and `tests/` are as they were, and `score.json`, `h5.json` and `h6.json` were not
regenerated. As in the two entries above, only the paper generators under `analysis/v2` changed, so
the analysis that scored the campaign is unchanged.
- `analysis/v2/raw_facts.py` adds two blocks and takes `--h6-run` and `--h6`. `trial_medians`: for
  every cell of single posts and batch rounds, per good process, its trials, the median over its
  trials of each trial's median delay (21 units, as `score_process` cuts them, `stats.median`), and
  its trials flagged with the cell's B_eff and with B as the base; per cell the pooled median of its
  trials' medians. The B_eff counts must equal every cell's T_med flags and trials in `score.json`.
  `netty_posts`: per good process of the buggy version's natural cell (`h6.py`'s read_plan,
  read_process and trials_of), its posts and its posts above half the bound, checked per process
  against `h6.json`'s over_half_b and in sum against its posts and posts_over_half_b. Inputs: the
  four archives of the entries above and `p1-h6-l.tar.gz`
  b5cf14fedb1e8cfef2fba9d79d3813e95cceca5b515ac9c96c99745fbde4130e (its `out/h6`), each verified
  again against its .sha256.
- `paper_macros.py` adds 22 macros: the clustered 95% interval over the 10 processes of the share
  of the buggy version's natural posts above half the bound (`stats.rate_interval`, the frozen seed
  and resamples, the process as the cluster); the cells whose flag count changes with B as the base,
  by protocol, family and system, and the false-positive cells' flags under that base; the lowest
  cell and process value of the trial medians over B_eff in the complete power cells, the highest in
  the false-positive cells, and the processes on the wrong side of B_eff / 5. All descriptive and
  outside the families.
- `paper_tables.py` adds three bodies: `detection-single-requested` and `detection-batch-requested`
  (the rows and columns of the detection tables, each entry the share flagged with B as the base),
  and `trial-medians` (per power and false-positive cell, the pooled median of its trial medians
  over B_eff and each process's value, smallest first).
- `paper_figures.py`: every text is drawn with the font's ligatures off ("-liga"); matplotlib 3.11
  shapes "fi", "ff" and "fl" into one glyph each, which some PDF readers drop ("xed loop",
  "e ective bound"). The detection figure's row labels, the fix-cost figure's row labels and top
  title, and the sawtooth figure's y label now stand further from the page edge. No data or other
  drawing changed. A test checks that no ToUnicode entry of the figures maps one glyph to two
  letters.
- Results of the B base, read from the outputs only: the counts change only in cells of single
  posts on Windows at the smaller bound (22 complete cells and one incomplete one, which the macros
  leave out), where B_eff is near the default timer period and B is much smaller, and never fall. The correct loops' process values of the trial medians all lie
  below B_eff / 5 and the complete power cells' all above.
- Outputs (sha256): `results/v2/macros.tex` (690 macros)
  367b5dd674d5aef170f5cfd7bbc2964303a71ae8b6a8d20c0a2ae3e3ca29c73e; `results/v2/raw-facts.json`
  25265ce7dbb74fd1ad8946f27b78f4b65e4eee0775226990afc485307c2d5112; `detection-single-requested.tex`
  5293e456086896ff4588c2597c968ac588e57a6384a9679728da2329ffc64ef4, `detection-batch-requested.tex`
  c45852d4aa163356549e680bf0eb16a96d8c80c12180a1657583afd086ee9d17, `trial-medians.tex`
  d0391ced0feeac455856f3409c10a4c8f2449ccc7f01c64a54412549ce9aae52; `figures/detection.pdf`
  8df39ca8b89db183611d324a0dc85410de61afd123ad963e341e7785eada1543, `figures/fixcost.pdf`
  dc7ca9d263ed36d5db78e25b1925edf9f0603e76013ba2b02d52b3d8424bff73, `figures/sawtooth.pdf`
  38ce6c409a37c351d5ba277ba2ec3ddba4a3f04ef2fe2daa8262d6f29081deb5. Two regenerations give the same
  bytes. A render of every macro, the new bodies and the figures passes
  `lab/bin/check_manuscript_terms.py --paper p1` with no hit. All tests under `analysis/v2` pass.

### 2026-11-07: macros for the writer's round 6 requests

The writer asked for five more things: the split of the cells whose flag count changes with B as
the base, by family and variant; why the release rerun and the sanitizer runs give equal test
counts; a check of the detection and phase-sweep figures; whether the design records why the
divisor is 5, with a recount at other divisors; and whether a period is measured on any variant
other than absent wake. The second, third and fourth's first half are answered in the Papers
repository from the archived logs, the figures and the frozen files; nothing here changed for them,
and no figure was regenerated. Added, and nothing that existed changed in value: the first 690
macros of `results/v2/macros.tex` are byte for byte its whole earlier text, every table body and
figure is as before, and every earlier block of `results/v2/raw-facts.json` is equal to before. No
scorer changed: `score.py`, `h5.py`, `h6.py`, `readers.py`, `grid.py`, `plan.py` and the code under
`bench/`, `loop/` and `tests/` are as they were, and `score.json`, `h5.json` and `h6.json` were not
regenerated. Only the paper generators under `analysis/v2` changed.
- `analysis/v2/raw_facts.py` adds two blocks, read in one pass over the campaign outputs
  (`score.resolve_plan`, unchanged). `divisor_flags`: for every cell of single posts and batch
  rounds, per good process, its trials and its trials whose median delay (21 units, as
  `score_process` cuts them) exceeds B_eff / d for d = 2, 4, 5 and 10, as d x median > B_eff; the
  count at d = 5 must equal every cell's T_med flags and trials in `score.json`. `calibrated_periods`:
  for every single-post cell of a variant that calibrates (`grid.delays_single_posts`), its good
  processes' calibrated periods (summary field b_eff_ns); for a source cell whose B_eff was
  measured, their median must equal its B_eff, and every other cell must report no period. Inputs:
  the five archives of the round 5 entry, each verified again against its .sha256.
- `paper_macros.py` adds 48 macros. The changed cells with B as the base, by family and variant:
  those outside the families are all predicted blind spots (checked), with the counts per variant,
  the changed false-positive cells seen in the earlier study, the incomplete cell left out, and the
  blind-spot cells' flags with B as the base. The test recounted at B_eff / 2, / 4 and / 10, per
  family over the complete cells: flags, the lowest power rate and the highest false-positive and
  blind-spot rates. The calibrated periods of the 82 single-post cells other than the source cells,
  each process's period over its cell's B_eff, lowest and highest per system and bound. All
  descriptive and outside the families; the design names one divisor and one B_eff source.
- `test_paper.py` adds one test of the three new macro groups on small cells.
- Outputs (sha256): `results/v2/macros.tex` (738 macros)
  bd3d8d88cfe372cd16c5ceca933ef07c76bcd6163867c08090a34f18f49ae950; `results/v2/raw-facts.json`
  2968573ba65a53bd04d0a3c034982d6ee8cfb7d4191ee18da4d072fcde3cdc67. Two regenerations give the same
  bytes. A render of the new macros passes `lab/bin/check_manuscript_terms.py --paper p1` with no
  hit. All tests under `analysis/v2` pass.

### 2026-11-07: macros and table bodies for the writer's round 7 requests and the process-level appendix

The writer asked what the semantics test of delivery after a missed wake runs with and asserts,
for the cells seen before the freeze with each family without them, for the delay criteria split
into those seen before the freeze and the new ones, and whether the prior study's bound ratios use
unrounded medians. The three-model vote on round 7 (decision d012 in the Papers repository, option
B, all three ballots first) added an appendix of descriptive process-level results from the stored
data: no new test, no run, outside the families, the frozen trial-level tests staying the stated
decisions. Added, and nothing that existed changed in value: the first 738 macros of
`results/v2/macros.tex` are byte for byte its whole earlier text, every earlier table body and
figure is as before, and every earlier block of `results/v2/raw-facts.json` is equal to before. No
scorer changed: `score.py`, `h5.py`, `h6.py`, `readers.py`, `grid.py`, `plan.py`, `stats.py` and the
code under `bench/`, `loop/` and `tests/` are as they were, and `score.json`, `h5.json` and
`h6.json` were not regenerated. Only the paper generators under `analysis/v2` changed. The bound
ratio question is answered from `h5.py` (h2_ratio is the largest over the smallest of the medians
as stored, before any rounding for print); nothing here changed for it.
- `analysis/v2/raw_facts.py` adds one block, read in the same pass as `h4_throughput`:
  `h4_process_ratios`, per H4 row of `score.json` and per good process in the row's order, its pair
  ratios (`readers.pair_ratios`, unchanged); concatenated they must equal the row's ratios, and each
  process must hold `grid.H4_PAIRS_PER_PROCESS` pairs. Inputs: the five archives of the round 5
  entry, each verified again against its .sha256 (`p1-campaign-l.tar.gz`, `p1-campaign-w.tar.gz`,
  `p1-h5-l.tar.gz`, `p1-h5-w.tar.gz`, `p1-h6-l.tar.gz`), and the earlier code reproduced the
  committed `raw-facts.json` byte for byte from them before the block was added.
- `paper_macros.py` adds 77 macros. The semantics test S11, read from `tests/wake_tests.cpp`: its
  posts, the pause after each, the loss deadline, the limit in whole timed returns, the tick per
  system and the limit per system, computed as the test computes it (refused if the limit line
  changes). Each family without the cells seen before the freeze (`score.json`'s replication field):
  cells, complete cells, flags, trials, cells rejected by the frozen test of the whole family, the
  lowest power rate and the highest false-positive rate; descriptive, no Holm step is run again.
  The delay criteria seen before the freeze and the new ones, with the holding count and largest
  value of each group; the seen backends are a constant of this module, `DELAY_SEEN_BACKENDS`, from
  `hypotheses-v2.md` section 2 (H3: "on epoll and IOCP the criterion held in 2026-09"), both bounds
  of each, as the 2026-09 criterion was set at 1, 10 and 100 ms (`hypotheses.md`, H1). The process-
  level appendix: the fewest flagged trials of any power process, the power processes not flagged
  in every trial, the most flagged trials of any false-positive process, and for the predicted
  blind spots the good processes, those with any trial flagged, the highest process median over
  B_eff and the processes above the threshold; for H4, each good process's ratio as the median of
  its pairs, the processes inside the equivalence margins and the lowest and highest ratio of the
  rows with work, and at N = 0 the processes of the rows predicted below the low margin that are
  below it. All descriptive and outside the families; no p value, sign test or binomial test.
- `paper_tables.py` adds four bodies: `seen-cells.tex` (the cells seen before the freeze, how each
  was seen, its family, flagged trials, trials and whether its family rejected it),
  `process-flags.tex` (per power, false-positive and predicted blind-spot cell, each good process's
  flagged trials, smallest first), `blind-trial-medians.tex` (as `trial-medians.tex`, for the
  predicted blind spots) and `throughput-processes.tex` (per H4 row and comparison, each good
  process's ratio, smallest first).
- `test_paper.py` adds three tests (the S11 limit and its refusals, the seen-cell split on small
  families, the round 7 sums and refusals on the committed results) and checks the new bodies.
- Outputs (sha256): `results/v2/macros.tex` (815 macros)
  69a07c4988683d99fdde26c697793788f7e288abb81281db2210092b30166e04; `results/v2/raw-facts.json`
  7d84808fc6a01965e3023a512e9f42a46171ef8e46f3be3a014b3725a93b0627; `seen-cells.tex`
  553087f8cbc98062ea62fdd8b33d7f81dbb31ceae7f38695cf49257a8fbea834, `process-flags.tex`
  0426dc8f3d98ada877e72e31495927cee15800ff22b6a27adce63ffaf5b139ef, `blind-trial-medians.tex`
  416e2c4f2338ae89178e2b0972a9cf9dfb2dffe769a41f0578814c8073d7f23c, `throughput-processes.tex`
  a34c5ef9145f07a850705958bc127ca43f18b0477b89d26dd2c478893f53add2. Two regenerations, each from
  the archives, give the same bytes. A render of the new macros and the four new bodies passes
  `lab/bin/check_manuscript_terms.py --paper p1` with no hit. All 436 tests under `analysis/v2`
  pass.

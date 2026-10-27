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

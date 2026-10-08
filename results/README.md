# P1 results

The paper measures this repository's minimal event loop (`loop/`, `design/minimal-loop.md`)
at code commit `47f14f0` (the root commit of this history), in two arms built from one tree:
fixed (`WAKELOOP_DEFECT=OFF`) and defect (`WAKELOOP_DEFECT=ON`). Each run records, per build
target, the hash of the first-party files its builds compiled (`inputs.json`). These hashes are
those of the files at `47f14f0` (the rule and a recomputation are in `../PROVENANCE.md`) and those
named by the sanitizer records in `../lab/sanitizer-records/` (below). The paper takes every
number in its prose from `rework/macros.tex`. The hypotheses are in `../hypotheses.md`; its
revision log records the change to this loop (2026-09-29).

The runs were built at commit `0af1ac4` of the repository's earlier history, which is archived
privately; the names of the raw runs and their metadata carry it, the tables of `rework/` carry it
in their column `code_commit` (and the runs' repository heads in `bench_commit`), and `rework/macros.tex` names it in its header
and in two macros the paper does not use. No claim rests on it;
`../PROVENANCE.md` maps it to `47f14f0`, for the record.

## Generated files (`rework/`)

Generated; do not edit them by hand.

- `matrix.csv`, `gap_sweep.csv`, `gap_sweep_summary.csv`, `gap_sweep_posts.csv`,
  `bound_sweep.csv`, `bound_sweep_summary.csv`, `random_gap.csv`, `macros.tex`: made on W by

      python analysis/summarize.py --out-dir results/rework \
          results/raw/2026-09-29-L-publication-0af1ac4 \
          results/raw/2026-09-30-W-publication \
          results/raw/2026-09-29-L-random-gap-0af1ac4

  from 292 runs (no cell with a bimodal calibration, so no `modes.csv`), with Python 3.14.5.
  The resampling of the gap sweep (the 21-post test at random phases) draws 100000 sets of 21
  posts per configuration, each without replacement, with `random.Random(20260926)`;
  `macros.tex` names the seed and the Python version. The directory keeps its name from the
  re-measurement; the paper and the scripts refer to it.
  The runs come from the tarballs that are the assets of release `data-2026-10` (below),
  unpacked into `results/raw/`.
- `fig_sawtooth.csv`: made by `python analysis/fig_sawtooth.py --results results/rework`,
  for `paper/fig-sawtooth.tex`.

On 2026-10-08 the three tarballs were unpacked again and found identical, file for file, to the
runs in `results/raw/`. The two commands above, run on W with Python 3.14.5 and without scipy
(the committed files carry no scipy p-value either), then made all nine files again, each
identical to the committed file after line endings are converted to LF.

## The runs

Each run was gated before any cell ran (below). The raw runs are not in git; each is a tarball,
an asset of the release `data-2026-10` of this repository
(https://github.com/Alex-Tsvetanov/paper-wake-defect/releases/tag/data-2026-10), with its
`.sha256` file beside it. Each tarball unpacks into one run directory, as `results/raw/` holds it.
The runs keep what they recorded: host names, home directories and the commits of the earlier
history appear in their metadata.

| Host | Run | Designs | Cells | Duration (env.txt to the last cell's end) | sha256 of the tarball |
|---|---|---|---|---|---|
| L | `2026-09-29-L-publication-0af1ac4` | gap-sweep, bound-sweep, bound-sweep-qos, matrix | 148, 0 failed | 1 min 44 s | `aa698e7173334d183561fe99612d6a5f91ba101935eb71d857b29b2144d895ab` |
| L | `2026-09-29-L-random-gap-0af1ac4` | random-gap | 8, 0 failed | 28 min 39 s | `5c3133f74b59c7d317cec45fd8abb1c6472aad203d592cffae432a2bd3c046f5` |
| W | `2026-09-30-W-publication` | gap-sweep, bound-sweep, matrix, each at both timer settings | 136, 0 failed | 2 min 36 s | `bd62edf6f97d7f1c08f7e882579782770f0a3d6efd22e9d61e9d8aeb33fe8f00` |

The tarballs are `p1-raw-<run>.tar.gz`, each with a `.sha256` file beside it. All runs use
seed 20260926 for the cell order. The random-gap design is exploratory (`hypotheses.md`,
revision log): both arms of epoll and io_uring at B = 1 and 10 ms, 1000 trials of 21 posts per
cell, each gap drawn uniformly from [0.1B, 3.0B] with gap seed 20260926. W has no random-gap
run; its detection at random phases is the resampling above.

- Compilers (CMake's identification): `Clang 22.1.8` on L, `Clang 22.1.0 with MSVC-like
  command-line` (clang-cl) on W. L ran kernel 7.2.3-arch1-2 with boost off and the performance
  governor (`pin.json`); `env.txt` records the cpuidle driver, governor and states.
- The W run passed the quiet check before any cell: mean CPU idle 97.3% over 10 s against 95%.
- Earlier W attempts: on 2026-09-29 the quiet check refused six times between 22:56 and 23:08
  EEST (mean idle 88.5% to 90.9%), and on 2026-09-30 once at 00:09 (90.6%). At 00:12 the check
  passed, but the run under the name `2026-09-29-W-publication` aborted while building the
  fixed arm, before any build finished or any cell ran: the caller had redirected the script's
  error stream, which in Windows PowerShell 5.1 turns vcvarsall's harmless stderr line into a
  terminating error. Its directory (env.txt, gate.json, quiet.json only) is the asset
  `p1-raw-2026-09-29-W-publication-aborted.tar.gz` of the same release (sha256
  `d9a76471225d115341e09ebf540d318fde76e4e276ab44d4d2c7fb8f19dd24f4`). The run above was
  started without the redirection, under a new name, since publication output is never
  overwritten.
- Package freeze on L: from the first L sanitizer record of the loop, 2026-09-29 19:00:02 EEST
  (clang 22.1.8, kernel 7.2.3-arch1-2, CMake 4.4.3, Python 3.14.7), to the last P1 publication
  run on L, L's packages are not upgraded (no `pacman -Syu`, no kernel, LLVM or glibc change).
  The last L run, the random gap, ended at 19:37:13 EEST on 2026-09-29 (its env.txt at 19:08:34
  plus 28 min 39 s).

## The sanitizer gate

A sanitizer record gates a measured build when the record's build compiled the same
first-party inputs, in the same configuration, with the same compiler.

- At the runs, the gate matched records of the loop named for code commit `0af1ac4` of the
  earlier history (`wakeloop-0af1ac47e-...`), each covering both arms, all green with 0 sanitizer
  reports: on L, ASan+UBSan, TSan and MSan, with every design including the PM QoS cells; on W,
  clang-cl ASan (gates the W runs) and MSVC ASan (extra coverage). Each run's `gate.json` names
  those that gated it (on W, the clang-cl record only).
- `../lab/sanitizer-records/` holds these five records, identical to the committed laboratory
  records, and the same five kinds of record made on 2026-10-08 for this history's code commit
  `47f14f09e` (`wakeloop-47f14f09e-...`), all green with 0 sanitizer reports, each covering both
  arms. Each record names the sha256 of an archive of its logs; the logs are not published.
- The public checks, on 2026-10-08, with the runs unpacked from the tarballs that are the
  release's assets: `python bench/check_records.py --records lab/sanitizer-records --code
  0af1ac47ec708595b91e5c15300556f2714314c1 --host L` (and `--host W`) writes a gate equal to the
  `gate.json` of each L run (the publication run and the random-gap run) and of the W run, record
  names included. With `--code 47f14f09efac14e2a14f39195a0c891b6c6f2e27` it writes a gate whose
  compiler, inputs hashes and configuration equal those of the same `gate.json` files. Each run's
  `inputs.json`, the hashes of what its builds compiled, holds the same inputs hashes and
  configuration.
- Each record runs the loop's tests (in the defect arm, the five wake detectors per backend
  must detect) and a reduced run of every design, and scans all of their output.
- `bench/check_records.py` builds the gate from the records (`gate.json`);
  `bench/inputs_gate.py` hashes what each build compiled for the targets `wakeloop` and
  `wakeprobe` (`inputs.json`), with `WAKELOOP_DEFECT` and the compiled backends as the
  configuration, and checks that the loop's sources are among the hashed inputs.
  `bench/run_matrix.sh` and `.ps1` refuse to run any cell unless both builds match their arm's
  records. Each run directory keeps its `gate.json` and `inputs.json`.

## Host check after the runs (W)

`powercfg /query SCHEME_CURRENT SUB_PROCESSOR PROCTHROTTLEMIN` on W, 2026-09-30: active scheme
`00acab9f-6807-4927-af55-c72a4c589dad` (ChrisTitus - Ultimate Power Plan), the scheme recorded
in the W run's `env.txt`; minimum processor state AC `0x00000064` (100%), DC `0x00000005` (5%).
The paper's statement about the power plan rests on this check.

## An earlier implementation's runs, archived

Before 2026-09-29 the hypotheses were measured on an earlier implementation, which is
withdrawn. None of its data is used, and they are not published. Its runs, their derived tables
and the smoke runs are archived privately (the derived tables and smoke runs as
`p1-old-results-and-smoke-2026-09-30.tar.gz`, sha256
`5626c19b06eb277d9ef175271004f38235018a72c14cf6cf820aae20d931b17e`). They were removed from
the tree on 2026-09-30, and the repository history that held them is archived privately.

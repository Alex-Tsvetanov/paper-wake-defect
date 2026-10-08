# Provenance of the cited commits

The public history of this repository starts at the root commit
`47f14f09efac14e2a14f39195a0c891b6c6f2e27`. The work before it was done in an earlier history
of this repository, which is archived privately. `results/README.md`,
`results/rework/macros.tex`, the tables of `results/rework/` (the columns `code_commit` and
`bench_commit`), `analysis/summarize.py`, `design/minimal-loop.md`, the raw runs and the sanitizer
records cite commits of that earlier history. This file maps each of them to this
history, for the record. Checked on 2026-10-08.

The paper cites none of them, and none of its claims rests on them. It names this
history's root commit and rests the identity of the measured code on public material: the inputs
hashes that each raw run records (the runs are the assets of the release `data-2026-10`), the
files at the root commit, which give the same hashes (below), and the sanitizer records in
`lab/sanitizer-records/`, which name them too.

## The measured code: `0af1ac4`

`0af1ac47ec708595b91e5c15300556f2714314c1` is the code commit of every publication run, the
commit the builds were made at. `analysis/summarize.py` maps it to the root commit of this
history (`PUBLIC_COMMIT`) and stops on a measured commit it does not list; `results/rework/macros.tex`
holds the root commit as `\WakeCodeCommitLinux` and `\WakeCodeCommitWindows`, which the paper
names, and `0af1ac4` as `\WakeMeasuredCommitLinux` and `\WakeMeasuredCommitWindows`, which the
paper named until 2026-10-08 and no longer uses. `macros.tex` gives both in full in its header,
`results/README.md` cites both, and the names of the raw runs (`...-0af1ac4`) and of the first
sanitizer records (`wakeloop-0af1ac47e-...`) carry `0af1ac4`.

It maps to the root commit `47f14f09efac14e2a14f39195a0c891b6c6f2e27`. What the builds compile
is byte-identical at the two commits.

### Same files

`bench/code_paths.txt` lists the paths that make up and select the compiled code. Git names
each file and directory by a hash of its content, and these names are the same at `0af1ac4`
and at the root commit:

| Path | Type | Git object id at `0af1ac4` and at the root commit |
|---|---|---|
| `CMakeLists.txt` | blob | `fa0daa5bca9216eab87e101ab8cd8d66e203d67e` |
| `loop` | tree | `681a150e4445c9e08244e8b643b4b204d5423813` |
| `tests` | tree | `288431501937f2380913676f7b509e300dc8a014` |
| `bench/CMakeLists.txt` | blob | `25219139eb3c1d62a2d28dcdace7c855a2e37b50` |
| `bench/wakeprobe.cpp` | blob | `fed91d156aeb7ce0fcbda93314a6161dd38e1c6c` |
| `bench/probe_platform.hpp` | blob | `2febf6e72268b2b8abcae1092442d3c2fc727971` |
| `bench/code_paths.txt` | blob | `b8229de3626d73f0f13ac1cc4ef59803e080e47d` |

To check a commit of this history: `git rev-parse <commit>:loop`, and so on for each path.
They are also the same at every later commit of this history that does not change these paths.
The code commit of this history, the last commit that changed one of them, is the root commit.

### Same inputs hashes

Each publication run and each sanitizer record states an inputs hash per build target. It is
the sha256 of the text made of one line `<path>\t<sha256 of the file>\n` per first-party file
the target compiled, sorted by path. A path is relative to the repository root, prefixed with
`project/` and lower-cased on Windows; a file is hashed with CRLF read as LF.
`bench/inputs_gate.py` computes it from a built tree, through the laboratory's
`lab/bin/inputs_hash.py`, which is not published; the recomputation below needs only Python.

| Target | Build | Files | Inputs hash |
|---|---|---|---|
| `wakeloop` | Linux (epoll and io_uring), both arms | 7 | `593662db48c94c1c902d844d5c925d3591a949d77a182ce26975376995b2be57` |
| `wakeloop` | Windows (IOCP), both arms | 5 | `040988e81654ff0fb689cacfafe1a57ff6c95829a7e473c13bc9310259ab1dc6` |
| `wakeprobe` | both systems, both arms | 4 | `976db6c5c0ca0b3a485c7a92b5e882aa49735cdc69acbdbc9b0d5db8fa31f41a` |

The files are:

- `wakeloop` on Linux: `loop/src/common.cpp`, `loop/src/epoll.cpp`, `loop/src/uring.cpp`,
  `loop/src/common.hpp`, `loop/src/linux_fd.hpp`, `loop/include/wakeloop/loop.hpp`,
  `loop/include/wakeloop/task_stack.hpp`;
- `wakeloop` on Windows: `loop/src/common.cpp`, `loop/src/iocp.cpp`, `loop/src/common.hpp`,
  `loop/include/wakeloop/loop.hpp`, `loop/include/wakeloop/task_stack.hpp`;
- `wakeprobe`: `bench/wakeprobe.cpp`, `bench/probe_platform.hpp`,
  `loop/include/wakeloop/loop.hpp`, `loop/include/wakeloop/task_stack.hpp`.

These are the hashes of the measured builds and of their sanitizer records. They were
reproduced at the root commit as follows.

- Windows, by a build. A clean clone of the root commit was built in both arms with clang-cl
  22.1.0 (Release, Ninja, as `bench/run_matrix.ps1` builds). `bench/inputs_gate.py` with
  `--expect` against the gate of the Windows record reported that both builds match:
  `wakeloop` `040988e8...`, `wakeprobe` `976db6c5...`. The loop's tests passed in both arms
  (17 of 17 each).
- Linux, by builds. The sanitizer records named for the root commit (below) were made on
  2026-10-08 on the Linux host, which built both arms with clang 22.1.8 from a clone of this
  history. Each names `wakeloop` `593662db...` and `wakeprobe` `976db6c5...`, as the records of
  `0af1ac4` do. The rule above, applied to the files of the root commit, gives the same
  hashes, and gives the two Windows hashes that the Windows build produced, which checks the
  rule.

A recomputation from a checkout, in Python:

```python
import hashlib, pathlib
def inputs_hash(files):
    lines = sorted(f"project/{p}\t" + hashlib.sha256(
        pathlib.Path(p).read_bytes().replace(b"\r\n", b"\n")).hexdigest() for p in files)
    return hashlib.sha256("".join(l + "\n" for l in lines).encode()).hexdigest()
```

### Sanitizer records

The measured builds were gated by these records, made in the author's laboratory repository;
`lab/sanitizer-records/` of this repository holds them, identical to the committed laboratory
records. Each covers both arms, is green, and reports 0 sanitizer findings. A record covers
a build when it names the same inputs hash per target and the same values of the options that
select compiled code (`WAKELOOP_DEFECT`, `WAKELOOP_BACKENDS`).

| Record | Compiler and sanitizer | `wakeloop` | `wakeprobe` |
|---|---|---|---|
| `wakeloop-0af1ac47e-L-asan.json` | Clang 22.1.8, ASan and UBSan | `593662db...` | `976db6c5...` |
| `wakeloop-0af1ac47e-L-tsan.json` | Clang 22.1.8, TSan | `593662db...` | `976db6c5...` |
| `wakeloop-0af1ac47e-L-msan.json` | Clang 22.1.8, MSan | `593662db...` | `976db6c5...` |
| `wakeloop-0af1ac47e-W-asan-clangcl.json` | clang-cl 22.1.0, ASan (gates the Windows runs) | `040988e8...` | `976db6c5...` |
| `wakeloop-0af1ac47e-W-asan.json` | MSVC 19.51.36246.0, ASan (extra coverage) | `040988e8...` | `976db6c5...` |

The inputs hashes at the root commit equal the records', so the records cover builds of this
history as well.

#### Records named for this history

These records were made on 2026-10-08 for this history, and `lab/sanitizer-records/` of this
repository holds them, identical to the committed laboratory records. They are named for its code commit, the
root commit `47f14f09efac14e2a14f39195a0c891b6c6f2e27` (`47f14f09e`), as
`bench/sanitize_wakeprobe.sh` and `bench/sanitize_wakeprobe.ps1` name a record.
`bench/check_records.py` accepts them for this code commit on both hosts (`--host L` and
`--host W`). Each is green, covers both arms, reports 0 sanitizer findings in the full output
of every test and every probe cell, and names the same inputs hashes, configuration and
compiler as the record of the same kind for `0af1ac4`. The tests passed in both arms: 33 of 33
on Linux, 17 of 17 on Windows. Each record names the repository head it was made at
(`repo_head`) and the sha256 of the archive of its logs; the logs are not published.

The public check: `bench/check_records.py --records lab/sanitizer-records --code
47f14f09efac14e2a14f39195a0c891b6c6f2e27`, with `--host L` and with `--host W`, passes and writes
a gate whose compiler, inputs hashes and configuration per arm equal those of the `gate.json` of
each L run (the publication run and the random-gap run) and of the W run, which name the records
of `0af1ac4`. The inputs hashes and configuration of each run's `inputs.json`, what its builds
compiled, are the same. With `--code 0af1ac47ec708595b91e5c15300556f2714314c1`, the same script
on the same directory writes a gate equal to each run's `gate.json`, record names included.
Checked on W on 2026-10-08, with the runs unpacked from the tarballs that are the release's assets.

| Record | Compiler and sanitizer | `wakeloop` | `wakeprobe` |
|---|---|---|---|
| `wakeloop-47f14f09e-L-asan.json` | Clang 22.1.8, ASan and UBSan | `593662db...` | `976db6c5...` |
| `wakeloop-47f14f09e-L-tsan.json` | Clang 22.1.8, TSan | `593662db...` | `976db6c5...` |
| `wakeloop-47f14f09e-L-msan.json` | Clang 22.1.8, MSan | `593662db...` | `976db6c5...` |
| `wakeloop-47f14f09e-W-asan-clangcl.json` | clang-cl 22.1.0, ASan | `040988e8...` | `976db6c5...` |
| `wakeloop-47f14f09e-W-asan.json` | MSVC 19.51.36246.0, ASan (extra coverage) | `040988e8...` | `976db6c5...` |

### Generated results

At the root commit, `analysis/summarize.py` and `analysis/fig_sawtooth.py`, run on the three
raw runs with the commands in `results/README.md`, regenerate all nine files of
`results/rework/`, each identical to the committed file after line endings are converted to
LF. The raw runs are not in git; they are the assets of the release `data-2026-10`, and
`results/README.md` lists them with their sha256. On 2026-10-08 the regeneration was repeated
from the tarballs that are the release's assets, with the same result.

## Other cited commits

None of these is a measured state, and none is in this history.

| Commit | Cited in | What it is |
|---|---|---|
| `4d34f460daf25f5b68bb980541acd9dd71ff7873` | the raw runs on Linux (`env.txt`, `repo_head`), the three Linux records of `0af1ac4`, and the column `bench_commit` of the tables of `results/rework/` | the repository head when those runs and records were made; its compiled code is that of `0af1ac4` (no difference in the paths of `bench/code_paths.txt`) |
| `62408d4a089e70b3cac46e9695b4cc0be1a05776` | the Windows publication run (`env.txt`, `repo_head`), and the column `bench_commit` of the tables of `results/rework/` | the repository head when that run was made; its compiled code is that of `0af1ac4` |
| `2233155` | `design/minimal-loop.md`, line 13 | the state before the minimal loop, when the probe was built on the earlier, withdrawn implementation; the design note's line numbers refer to its files. The note is frozen and keeps the reference |
| `4abc958` | `analysis/summarize.py`, the comment on `DROPPED_FIXED_BOUND_US` | the first version of `hypotheses.md` (2026-09-25); the comment quotes the sentence it cites, and the revision log of `hypotheses.md` (2026-10-08) quotes the whole first version |

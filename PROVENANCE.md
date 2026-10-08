# Provenance of the cited commits

The public history of this repository starts at the root commit
`47f14f09efac14e2a14f39195a0c891b6c6f2e27`. The work before it was done in an earlier history
of this repository, which is archived privately. The paper, `results/README.md`,
`results/rework/macros.tex`, `analysis/summarize.py` and `design/minimal-loop.md` cite commits
of that earlier history. This file maps each of them to this history. Checked on 2026-10-08.

## The measured code: `0af1ac4`

`0af1ac47ec708595b91e5c15300556f2714314c1` is the code commit of every publication run. The
paper names it ("repository commit 0af1ac4", from `\WakeCodeCommitLinux` and
`\WakeCodeCommitWindows` in `results/rework/macros.tex`), `macros.tex` gives it in full in its
header, `results/README.md` cites it, and the names of the raw runs (`...-0af1ac4`) and of the
sanitizer records (`wakeloop-0af1ac47e-...`) carry it.

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

### Same inputs hashes

Each publication run and each sanitizer record states an inputs hash per build target. It is
the sha256 of the text made of one line `<path>\t<sha256 of the file>\n` per first-party file
the target compiled, sorted by path. A path is relative to the repository root, prefixed with
`project/` and lower-cased on Windows; a file is hashed with CRLF read as LF.
`bench/inputs_gate.py` computes it from a built tree.

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
- Linux, by recomputation, not by a build. The rule above, applied to the files of the root
  commit, gives `593662db...` and `976db6c5...`. The same recomputation gives the two Windows
  hashes that the build produced, which checks the rule.

A recomputation from a checkout, in Python:

```python
import hashlib, pathlib
def inputs_hash(files):
    lines = sorted(f"project/{p}\t" + hashlib.sha256(
        pathlib.Path(p).read_bytes().replace(b"\r\n", b"\n")).hexdigest() for p in files)
    return hashlib.sha256("".join(l + "\n" for l in lines).encode()).hexdigest()
```

### Sanitizer records

The measured builds are covered by these records, kept in the author's laboratory repository
(not public). Each covers both arms, is green, and reports 0 sanitizer findings. A record covers
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

### Generated results

At the root commit, `analysis/summarize.py` and `analysis/fig_sawtooth.py`, run on the three
raw runs with the commands in `results/README.md`, regenerate all nine files of
`results/rework/`, each identical to the committed file after line endings are converted to
LF. The raw runs are not in git; `results/README.md` lists their archives and sha256.

## Other cited commits

None of these is a measured state, and none is in this history.

| Commit | Cited in | What it is |
|---|---|---|
| `4d34f460daf25f5b68bb980541acd9dd71ff7873` | the raw runs on Linux (`env.txt`, `repo_head`) and the three Linux records | the repository head when those runs and records were made; its compiled code is that of `0af1ac4` (no difference in the paths of `bench/code_paths.txt`) |
| `62408d4a089e70b3cac46e9695b4cc0be1a05776` | the Windows publication run (`env.txt`, `repo_head`) | the repository head when that run was made; its compiled code is that of `0af1ac4` |
| `2233155` | `design/minimal-loop.md`, line 13 | the state before the minimal loop, when the probe was built on the earlier, withdrawn implementation; the design note's line numbers refer to its files. The note is frozen and keeps the reference |
| `4abc958` | `analysis/summarize.py`, the comment on `DROPPED_FIXED_BOUND_US` | the first version of `hypotheses.md` (2026-09-25); the comment quotes the sentence it cites |

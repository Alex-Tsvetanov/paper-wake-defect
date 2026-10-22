# Bounded-wait wake defects in portable event loops

Paper P1 of Alex Tsvetanov's PhD at the Technical University of Sofia.

A worker thread that parks in a wait with a timeout (epoll_pwait2, io_uring_enter,
GetQueuedCompletionStatus) must be woken when another thread posts work to it. If the wake
is missing or points at something the wait does not watch, every test that only checks
eventual delivery still passes, but each post waits for the timeout to expire. This paper
models the delay the defect causes, gives a portable latency test that detects it, and
measures both on wakeloop, a minimal event loop written for the purpose. One compile-time
switch, `WAKELOOP_DEFECT`, seeds the defect at one site per backend.

## Layout

- `loop/`     wakeloop: the minimal event loop (epoll and io_uring on Linux, IOCP on Windows)
- `tests/`    the loop's semantics tests and wake detectors, run per backend in both arms
- `bench/`    wakeprobe (the measurement program), run matrices, and the scripts that make the
              sanitizer records and gate the builds
- `lab/sanitizer-records/` the sanitizer records that gated the measured builds, and those made
              later for this history's code commit (copies of the author's laboratory records, at
              the same path)
- `design/`   design notes, frozen once approved; changes go into their revision logs
- `analysis/` scripts that turn the raw runs into `results/rework/`
- `results/`  the generated tables and `macros.tex` of the publication runs, in `rework/`;
              `results/README.md` lists the runs, their archives (the assets of release
              `data-2026-10`) and their sanitizer gate
- `paper/`    LaTeX source of the Software: Practice and Experience draft, the prose check
              (`check_prose.py`), and the unpublished conference Word copy
- `historical/tokio-yield-lost-wake/` the Tokio yield lost-wake reproduction
- `paper/revision-spe.md` the retarget and that reproduction; not a frozen hypotheses file
- `hypotheses.md` written before the publication runs, with its revision log

## Build and test

    cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release [-DWAKELOOP_DEFECT=ON]
    cmake --build build
    ctest --test-dir build

`WAKELOOP_DEFECT=ON` builds the defect arm; its wake detectors pass only when they report the
missing wake. `WAKELOOP_SANITIZER` selects a sanitizer (see the top of `CMakeLists.txt`).

## Paper

`paper/main.tex` is a draft for Software: Practice and Experience. It is not a submission.
The conference running header is gone. Rebuild the PDF, then check the prose:

    cd paper && latexmk -pdf main.tex
    python paper/check_prose.py
    python paper/check_prose.py --outputs

`paper/main.pdf` is that draft and is committed with the sources.
`paper/Tsvetanov-wake-TELECOM2026.docx` is the unpublished conference manuscript.
`paper/build_docx.py` still targets that conference template and refuses to rebuild the
draft, so the Word file is not regenerated from the current sources.

## Historical wake

`historical/tokio-yield-lost-wake/` builds one Tokio program at the buggy release and at
the release that contains the fix. See its README. It is not part of the C++ build, and
it does not produce a latency number for the paper.

## Public material

The paper rests only on public material: this repository from its root commit on, and the raw
runs, which are the assets of its release `data-2026-10`, each with a `.sha256` file:
https://github.com/Alex-Tsvetanov/paper-wake-defect/releases/tag/data-2026-10.
`results/README.md` gives the commands that regenerate `results/rework/` from them and the check
of the published sanitizer records against the inputs each run records. The scripts' default
records directory is the laboratory repository's; give `lab/sanitizer-records` instead. Two
laboratory programs that the scripts call are not published: `lab/bin/inputs_hash.py`, which
`bench/inputs_gate.py` runs to hash what a build compiled (`PROVENANCE.md` gives the rule, the files
of each target and a recomputation that needs only Python), and the host's pinning script
`lab/bin/pin.sh`.

The repository's earlier history is archived privately. The runs' metadata, the sanitizer
records, `results/README.md`, `results/rework/macros.tex` (its header and two unused macros), the
tables of `results/rework/` (the columns `code_commit` and `bench_commit`),
`analysis/summarize.py` and `design/minimal-loop.md` name some of its commits; `PROVENANCE.md` maps them to this history, for
the record. No claim of the paper rests on them. An earlier implementation's runs, archived
privately, are described at the end of `results/README.md`; none of their data is used.

## Licence

Copyright 2026 Alex I. Tsvetanov.

This repository is licensed under the Apache License, Version 2.0. The full text is in
`LICENSE`. The licence covers every file except the manuscript (the LaTeX and BibTeX sources
in `paper/` and every PDF or Word copy built from them) and the conference Word template
`paper/template.docx`, a third-party file kept in this repository only to build the manuscript.
`NOTICE` lists what the licence does not cover.

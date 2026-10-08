# Bounded-wait wake defects in portable event loops

Paper P1 of Alex Tsvetanov's PhD (see the Papers mono-repo).

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
- `bench/`    wakeprobe (the measurement program), run matrices, sanitizer records and gates
- `design/`   design notes, frozen once approved; changes go into their revision logs
- `analysis/` scripts that turn the raw runs into `results/rework/`
- `results/`  the generated tables and `macros.tex` of the publication runs, in `rework/`;
              `results/README.md` lists the runs, their archives and their sanitizer gate
- `paper/`    LaTeX source, the Word build (`build_docx.py`) and the prose check
              (`check_prose.py`)
- `hypotheses.md` written before the publication runs, with its revision log

## Build and test

    cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release [-DWAKELOOP_DEFECT=ON]
    cmake --build build
    ctest --test-dir build

`WAKELOOP_DEFECT=ON` builds the defect arm; its wake detectors pass only when they report the
missing wake. `WAKELOOP_SANITIZER` selects a sanitizer (see the top of `CMakeLists.txt`).

## Paper

    python paper/build_docx.py --word-check   # PDF build, Word copy, page count in Word
    python paper/check_prose.py --outputs     # typed digits and dashes

An earlier implementation's runs, archived, are described at the end of `results/README.md`;
none of their data is used. The repository's earlier history is archived privately;
`PROVENANCE.md` maps the commits that the paper and the results cite to this history.

## Licence

Copyright 2026 Alex I. Tsvetanov.

This repository is licensed under the Apache License, Version 2.0. The full text is in
`LICENSE`. The licence covers every file except the manuscript (the LaTeX and BibTeX sources
in `paper/` and every PDF or Word copy built from them) and the conference Word template
`paper/template.docx`, a third-party file kept in this repository only to build the manuscript.
`NOTICE` lists what the licence does not cover.

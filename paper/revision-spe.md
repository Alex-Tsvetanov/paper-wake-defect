# Revision note, 22 October 2026

Not an edit of `hypotheses.md` or of `design/minimal-loop.md`. Those files stay frozen.
This note records the retarget of the manuscript and the historical-wake attempt.

## Manuscript

`paper/main.tex` is no longer the TELECOM 2026 conference manuscript. The IEEEtran
conference class, the running header (34th National Conference TELECOM 2026, 26-28
November 2026, Sofia), and the conference author block are gone. The draft is a one-column
article aimed at Software: Practice and Experience. It says, in the manuscript, that it
is not a submission.

The scientific claims that already had generated macros are unchanged. No new latency,
power, or false-positive number was written. The six injectable defects in the brief
(absent wake, unobserved primitive, lazy-wake race, wrong worker, no re-trigger,
undrained edge) are not in the code. The measured matrix remains one compile-time seed
per backend. The draft does not call that seed a historical result.

`paper/Tsvetanov-wake-TELECOM2026.docx` is left as the unpublished conference copy.
`paper/build_docx.py` refuses to rebuild it from the current sources.

## Historical missed wake

Searched public reports in Netty, Asio, Tokio, and libuv. The case that could be built
in this session is Tokio issue 6996, fixed by pull request 6999, commit
`c6f9e61d9ee7e69c1e6c47dacd5a7478bc011b59` (quininer, 29 November 2024), "runtime: fixed
yield_now lost wakeup".

Reproduced: yes, at a buggy commit.

- Buggy build: crates.io Tokio 1.41.1. The crate's `.cargo_vcs_info.json` records git
  commit `bb7ca7507b94d01ffe0e275ddc669734ab3bf783` (the 7 November 2024 release commit,
  "chore: prepare Tokio v1.41.1"). `git hash-object` of the crate's
  `src/runtime/context.rs` is `76918114bc3079708d5768d794a515db6915696b`, the pre-image
  blob named in the fix diff. `with_scheduler` always enters the scheduler context.
- Fixed build: crates.io Tokio 1.42.0, vcs commit
  `bb9d57017e100985f86d8ca41ac105ee9140423e`. The same hash of `context.rs` is
  `12f6edf13216f8a534365868267995680eb7d23c`, the post-image blob in that diff. The
  1.42.0 changelog says "runtime: do not defer `yield_now` inside `block_in_place`
  ([#6999])".

The test is `historical/tokio-yield-lost-wake/src/main.rs`. A multi-thread runtime
(two worker threads) runs `spawn`, then `block_in_place`, then
`futures_executor::block_on` of `yield_now`. Release builds, this Windows host, 22
October 2026, each process allowed 15 seconds.

- Tokio 1.41.1: stdout `start`, then the process was stopped at 15 seconds. No `done`.
- Tokio 1.42.0: stdout `start`, `done`, `finished`, and the process returned inside the
  wait.

That is a hang, not a bounded-wait delay. The paper's latency test was not applied. No
power and no false-positive rate are claimed, and no new macro was generated.

Negative control, same host, same wait: the issue's other snippet, `yield_now` inside
`futures_executor::block_on` without `block_in_place`, printed `start` and did not finish
on 1.41.1 or on 1.42.0. It does not separate the commits. It is not the result in the
paper. The paper states that.

Java is not installed here, so the Netty commit `f44f3e7926f1676315ae86d0f18bdd9b95681d9f`
was not built. The Asio report that is a Linux epoll race was not rebuilt as a kernel.
The 2019 Tokio scheduler hang (pull request 1788) was not built.

## What still blocks a submission

The latency matrix is still only the self-seeded switch on the loop written for this
study. The Tokio case does not exercise it. A historical bounded-wait miss, reproduced
at its buggy commit and scored by that test, is still absent. The draft says so.

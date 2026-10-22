# Tokio yield lost wake

A reproduction of one historical missed wake, at the buggy commit, separate from the
latency matrix on this repository's loop.

The program in `src/main.rs` enters a multi-thread Tokio runtime, then calls
`block_in_place` and, inside it, `futures_executor::block_on` on `yield_now`. That is
the shape of the test added with the fix. At the buggy commit the wake is deferred and
the task does not continue. At the commit that contains the fix, it does.

| Build | Crate | Git commit recorded by the crate | `context.rs` blob | Observed |
| --- | --- | --- | --- | --- |
| `buggy/` | Tokio 1.41.1 | `bb7ca7507b94d01ffe0e275ddc669734ab3bf783` | `76918114bc3079708d5768d794a515db6915696b` | prints `start`, does not print `done` |
| `fixed/` | Tokio 1.42.0 | `bb9d57017e100985f86d8ca41ac105ee9140423e` | `12f6edf13216f8a534365868267995680eb7d23c` | prints `start`, `done`, `finished` |

The blobs are the pre-image and post-image of `tokio/src/runtime/context.rs` in Tokio
pull request 6999 (commit `c6f9e61d9ee7e69c1e6c47dacd5a7478bc011b59`). Tokio 1.42.0's
changelog names that change: do not defer `yield_now` inside `block_in_place`. The
report is Tokio issue 6996.

This is a hang. The runtime parks without a bound, so the paper's latency test does
not apply. There is no power and no false-positive rate.

An earlier shape of the program, the same `yield_now` inside `futures_executor::block_on`
without `block_in_place`, printed `start` and did not finish on either crate. It does
not separate the two commits. The sources in this directory are the shape that does.

The run that these notes record was on one Windows host on 22 October 2026, outside the
publication runs. Each process was allowed 15 seconds. The buggy process was still at
`start` when it was stopped. The fixed process returned before that wait ended. Those
times are not a result in the paper.

Not part of the C++ build. From this directory:

    cargo build --release --manifest-path buggy/Cargo.toml
    cargo build --release --manifest-path fixed/Cargo.toml

Run `buggy/target/release/tokio_yield_lost_wake.exe` and stop it if it does not print
`done`. Run `fixed/target/release/tokio_yield_lost_wake.exe` and expect `done` and
`finished`.

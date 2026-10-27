#!/usr/bin/env bash
# Derived from paper-io-core bench/sanitize_iocore.sh (itself from paper-one-port bench/sanitize_oneport.sh
# at commit 1f27ea8), same author (Apache-2.0).
# Changed: wakeloop's names, cache options and paths; no flag line and no link order; the code paths decide
# what must be clean (bench/code_paths.txt), and a timed record also wants the whole tree clean; ctest runs
# serially and also writes a JUnit file and the list of registered tests; the campaign smoke; every
# *SAN_OPTIONS is set to an explicit value; a toolchain file joins the kept logs.
#
# The sanitizer record of wakeloop on L (hypotheses-v2.md, section 11 item 7; the Papers repo's rule D5).
#
#   bench/sanitize_wakeloop.sh asan|tsan|msan RECORDS_DIR
#
# From this checkout, whose whole tree must be clean (`git status --porcelain` empty, ignored build folders
# apart; a dry run only needs the code paths of bench/code_paths.txt clean): configures the whole CMake project
# with clang (Release, WAKELOOP_SANITIZER address+undefined, thread or memory with the MSan libc++), builds it
# with Ninja, hashes what it compiled (bench/build_inputs.py: wakeloop, wakeloop_tests, wakeprobe and abpair),
# lists the registered tests (ctest -N), runs the whole CTest suite serially (ctest -V, so the whole output of
# every test is kept, and a JUnit file) with the runtime options below, runs the campaign smoke
# (bench/campaign_smoke.py), keeps the logs (bench/keep_record_logs.sh) and writes
# RECORDS_DIR/wakeloop2-<commit>-L-<san>-<time stamp>.json (bench/wakeloop_record.py, which never replaces a file).
# The suite holds the wake matrix of epoll, io_uring and condvar, wakeprobe --check and abpair --check on each,
# the pinning and failure checks, and the Python tests; nothing in it is timed, so it runs none of the measured
# code. The smoke runs one reduced real invocation of every distinct code shape of the campaign, pilot and H5
# runs under the same sanitizer build and environment, right after CTest. It keeps only each run's exit code and
# sanitizer report count, never a measurement; see the text of bench/campaign_smoke.py. The record is green
# only if the build and every registered test passed, every backend has its required tests, every smoke run
# exited 0, and no log holds a sanitizer report. The gate (bench/gate_lib.py, run by bench/run_campaign.py)
# matches it to a measured build by inputs hash, compiler and configuration, whatever the commit; the measured
# build must use the same compiler (clang++ on L).
#
# A timed record needs LAB_JOB (the lab runner sets it) and the laptop's host name (gate_lib.LAB_HOSTS).
#
# Runtime options. Every *SAN_OPTIONS is set here, to an explicit value (empty for the sanitizers not in use),
# so that nothing the shell had can reach a run, and all five go into the record:
#   asan  ASAN_OPTIONS=detect_leaks=1:detect_stack_use_after_return=1:strict_string_checks=1:symbolize=1
#         UBSAN_OPTIONS=print_stacktrace=1:halt_on_error=1 (the build adds -fno-sanitize-recover=all)
#   tsan  TSAN_OPTIONS=halt_on_error=0:exitcode=66 (the runtime's defaults, written out)
#   msan  MSAN_OPTIONS=halt_on_error=1:exit_code=77 (the runtime's defaults, written out)
#
# DRY_RUN=1: a test of this driver, never a citable record. The smoke only lists its shapes (--list) and runs
# no tool: the desktop never times, and the smoke is the one step that runs the measurement programs for real.
# The record is named wakeloop2-<commit>-L-<san>-<time stamp>-dryrun.json and marked "dry_run" (the gate refuses it),
# RECORDS_DIR may not be the Papers repo's lab/sanitizer-records, and the logs go to WORK's records-logs, not
# ~/lab/records-logs, so the citable record's log directory stays free.
#
# Environment: WORK (default ~/lab/p1/records-work; the build tree is WORK/<san>[-dryrun], made afresh; it must be
# on a local filesystem: ninja's dependency log breaks on a 9p mount), JOBS (ninja -j, default 8), MSAN_LIBCXX
# (default ~/opt/libcxx-msan-gcc, passed as WAKELOOP_MSAN_LIBCXX), RECORDS_LOGS (default ~/lab/records-logs, under
# DRY_RUN WORK/records-logs), REPO_URL (recorded; default the checkout's origin, else "unknown"), LAB_BIN (a
# directory holding the Papers repo's inputs_hash.py; default as bench/build_inputs.py finds it, checked before the
# build). Exits 0 when the record is green, 2 before any build when a check fails.
set -euo pipefail

san=${1:?usage: sanitize_wakeloop.sh asan|tsan|msan RECORDS_DIR}
records=${2:?usage: sanitize_wakeloop.sh asan|tsan|msan RECORDS_DIR}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
dry=${DRY_RUN:-}
asan_options=detect_leaks=1:detect_stack_use_after_return=1:strict_string_checks=1:symbolize=1
ubsan_options=print_stacktrace=1:halt_on_error=1
tsan_options=halt_on_error=0:exitcode=66
msan_options=halt_on_error=1:exit_code=77
case "$san" in
    asan) sanitizer=address+undefined
          sanenv=("ASAN_OPTIONS=$asan_options" "UBSAN_OPTIONS=$ubsan_options" LSAN_OPTIONS= TSAN_OPTIONS= MSAN_OPTIONS=) ;;
    tsan) sanitizer=thread
          sanenv=(ASAN_OPTIONS= UBSAN_OPTIONS= LSAN_OPTIONS= "TSAN_OPTIONS=$tsan_options" MSAN_OPTIONS=) ;;
    msan) sanitizer=memory
          sanenv=(ASAN_OPTIONS= UBSAN_OPTIONS= LSAN_OPTIONS= TSAN_OPTIONS= "MSAN_OPTIONS=$msan_options") ;;
    *) echo "sanitize_wakeloop: unknown sanitizer '$san'" >&2; exit 2 ;;
esac
options="${sanenv[*]}"
if [ -z "$dry" ]; then
    # A timed record is made by a lab job on the laptop, from a clean checkout.
    [ -n "${LAB_JOB:-}" ] || { echo "sanitize_wakeloop: refusing a record: LAB_JOB is not set (the lab runner sets it on the laptop); use DRY_RUN=1 to test the driver" >&2; exit 2; }
    (cd "$here" && python3 -c 'import sys, gate_lib; sys.exit(0 if gate_lib.is_lab_host("L", sys.argv[1]) else 1)' "$(uname -n)") || {
        echo "sanitize_wakeloop: the host $(uname -n) is not the laptop's Arch; a record is made on the laptop" >&2; exit 2; }
    if [ -n "$(git -C "$repo" status --porcelain)" ]; then
        echo "sanitize_wakeloop: $repo has changed or untracked files; a record names a clean checkout" >&2
        exit 2
    fi
fi
mkdir -p "$records"
records=$(cd "$records" && pwd)
if [ -n "$dry" ]; then
    case "$records" in */lab/sanitizer-records|*/lab/sanitizer-records/*) echo "sanitize_wakeloop: a dry run never writes into lab/sanitizer-records" >&2; exit 2 ;; esac
fi
# The code paths count even for a dry run: the records driver and the gate are not code (wakeprobe's BENCH_DIRTY reads the same paths).
mapfile -t code_paths < <(grep -v '^#' "$here/code_paths.txt" | grep -v '^$')
if [ -n "$(git -C "$repo" status --porcelain --untracked-files=all -- "${code_paths[@]}")" ]; then
    echo "sanitize_wakeloop: the code paths have changes; a record must name committed code" >&2
    exit 2
fi
commit=$(git -C "$repo" rev-parse HEAD)
fingerprint=$(cd "$here" && python3 -c 'import build_inputs; print(build_inputs.code_fingerprint())') || {
    echo "sanitize_wakeloop: the code fingerprint cannot be read" >&2; exit 2; }
# The name is unique (a UTC time stamp): a second record of the same commit is never blocked by the name of a first that
# was red, and the path is checked here, before any build (wakeloop_record.py never replaces a file either).
name=wakeloop2-${commit:0:9}-L-$san-$(date -u +%Y%m%dt%H%M%S)${dry:+-dryrun}
[ ! -e "$records/$name.json" ] || { echo "sanitize_wakeloop: $records/$name.json exists; a record is never replaced" >&2; exit 2; }
work=${WORK:-$HOME/lab/p1/records-work}/$san${dry:+-dryrun}
if [ -n "$dry" ]; then export RECORDS_LOGS=${RECORDS_LOGS:-${WORK:-$HOME/lab/p1/records-work}/records-logs}; fi
bash "$here/keep_record_logs.sh" check "$name"
# The Papers repo's inputs_hash.py, found before the build so that a missing one costs no build.
if [ -n "${LAB_BIN:-}" ]; then
    tool=$LAB_BIN/inputs_hash.py
else
    tool=$(cd "$here" && python3 -c 'import build_inputs; print(build_inputs.default_inputs_hash())' 2>/dev/null) || tool=""
fi
if [ -z "$tool" ] || [ ! -f "$tool" ]; then
    echo "sanitize_wakeloop: no inputs_hash.py (give LAB_BIN, the Papers repo's lab/bin)" >&2
    exit 2
fi

cmake_args=(-G Ninja -DCMAKE_CXX_COMPILER=clang++ -DCMAKE_BUILD_TYPE=Release "-DWAKELOOP_SANITIZER=$sanitizer")
if [ "$san" = msan ]; then
    libcxx=${MSAN_LIBCXX:-$HOME/opt/libcxx-msan-gcc}
    if [ ! -d "$libcxx/include/c++/v1" ] || [ ! -d "$libcxx/lib" ]; then
        echo "sanitize_wakeloop: no instrumented libc++ at $libcxx (headers and lib); give MSAN_LIBCXX" >&2
        exit 2
    fi
    cmake_args+=("-DWAKELOOP_MSAN_LIBCXX=$libcxx")
fi
echo "sanitize_wakeloop: $name from $repo at $commit, work $work"
rm -rf "$work"
mkdir -p "$work"
{ uname -a; clang++ --version | head -n 2; cmake --version | head -n 1; ninja --version; python3 --version; git --version
  echo "inputs_hash.py $tool"; echo "sanitizer options: $options"; } > "$work/toolchain.txt" 2>&1 || true
start=$(date +%s)
set +e
{ cmake -S "$repo" -B "$work/build" "${cmake_args[@]}" && ninja -C "$work/build" -j "${JOBS:-8}"; } > "$work/build.log" 2>&1
build_rc=$?
inputs_rc=1
ctest_rc=1
smoke_rc=1
if [ "$build_rc" -eq 0 ]; then
    python3 "$here/build_inputs.py" --build "$work/build" --host L --out "$work/build.inputs.json" \
        --inputs-hash "$tool" >> "$work/build.log" 2>&1
    inputs_rc=$?
    ctest --test-dir "$work/build" -N > "$work/tests.txt" 2>&1
    (cd "$work/build" && env "${sanenv[@]}" ctest -V --timeout 900 --output-junit "$work/ctest.xml") > "$work/ctest.log" 2>&1
    ctest_rc=$?
    # The smoke, even after a red CTest: a laptop visit should show everything that is wrong at once.
    (cd "$work/build" && env "${sanenv[@]}" python3 "$here/campaign_smoke.py" --build-dir "$work/build" --system linux \
        --out "$work/smoke.json" ${dry:+--list}) > "$work/smoke.log" 2>&1
    smoke_rc=$?
fi
seconds=$(( $(date +%s) - start ))
# The logs are packed whole or not at all (keep_record_logs.sh): a failed pack leaves nothing that would block a rerun,
# and the record, which then names no archive, is red. The pack prints "ARCHIVE SHA256"; the path may hold a space.
archive=""
archive_sha=""
pack_out=$(bash "$here/keep_record_logs.sh" pack "$name" "$work/build.log" "$work/ctest.log" \
    "$work/ctest.xml" "$work/tests.txt" "$work/toolchain.txt" "$work/build.inputs.json" "$work/smoke.json" "$work/smoke.log")
if [ $? -eq 0 ]; then
    archive=${pack_out% *}
    archive_sha=${pack_out##* }
else
    echo "sanitize_wakeloop: the logs could not be packed; the record will be red" >&2
fi
set -e

python3 "$here/wakeloop_record.py" --work "$work" --record "$records/$name.json" \
    --repo "${REPO_URL:-$(git -C "$repo" remote get-url origin 2>/dev/null || echo unknown)}" --commit "$commit" --repo-head "$commit" \
    --fingerprint-start "$fingerprint" \
    --sanitizer "$san" --cmake-args="${cmake_args[*]}" --options="$options" --host "$(uname -n)" --host-tag L \
    --build-exit "$build_rc" --inputs-exit "$inputs_rc" --ctest-exit "$ctest_rc" --smoke-exit "$smoke_rc" \
    --seconds "$seconds" --ctest-jobs 1 \
    --logs-archive "$archive" --logs-sha256 "$archive_sha" ${dry:+--dry-run}

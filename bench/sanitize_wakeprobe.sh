#!/usr/bin/env bash
# Sanitizer record for wakeloop on Linux (lab host L).
#
# Usage: sanitize_wakeprobe.sh asan|tsan|msan [RECORDS_DIR]
#
# Builds the fixed and defect arms of the whole repository under the sanitizer (the loop, its
# tests and wakeprobe), runs each arm's tests with the full output of every test kept, then every
# design of run_matrix.sh at reduced size (11 posts, 5 gap-sweep steps, one random-gap trial of
# 21 posts), and writes RECORDS_DIR/wakeloop-<code commit>-<HOST_ID>-<sanitizer>.json with
# sanitizer_record.py. <code commit> is the last commit that changed a path in
# bench/code_paths.txt; those paths must have no local changes.
#
# Logs: every log and output of the run is kept in ~/lab/records-logs/<record>/ (never /tmp) and
# packed into ~/lab/records-logs/<record>.tar.gz, whose sha256 goes into the record. A record
# directory that already exists is never overwritten.
#
# Environment: MSAN_LIBCXX (default $HOME/opt/libcxx-msan-gcc), WORK (build trees; default
# ~/lab/records-build/<record>), HOST_ID (default L), DESIGNS (default all), EXTRA_CMAKE_ARGS
# (appended to the sanitizer's configure arguments, for example a compiler resource directory).
set -euo pipefail

san=${1:?usage: sanitize_wakeprobe.sh asan|tsan|msan [RECORDS_DIR]}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
records=${2:-$repo/../../lab/sanitizer-records}
host_id=${HOST_ID:-L}

case "$san" in
    asan) cmake_args="-DWAKELOOP_SANITIZER=address+undefined"
          export ASAN_OPTIONS="detect_leaks=1:detect_stack_use_after_return=1:strict_string_checks=1:symbolize=1"
          export UBSAN_OPTIONS="print_stacktrace=1:halt_on_error=1"
          options="ASAN_OPTIONS=$ASAN_OPTIONS;UBSAN_OPTIONS=$UBSAN_OPTIONS" ;;
    tsan) cmake_args="-DWAKELOOP_SANITIZER=thread"
          export TSAN_OPTIONS="halt_on_error=1:second_deadlock_stack=1"
          options="TSAN_OPTIONS=$TSAN_OPTIONS" ;;
    msan) libcxx=${MSAN_LIBCXX:-$HOME/opt/libcxx-msan-gcc}
          [ -d "$libcxx/include/c++/v1" ] && [ -d "$libcxx/lib" ] ||
              { echo "no instrumented libc++ at $libcxx (headers and lib)" >&2; exit 2; }
          cmake_args="-DWAKELOOP_SANITIZER=memory -DWAKELOOP_MSAN_LIBCXX=$libcxx"
          export MSAN_OPTIONS="halt_on_error=1:print_stats=1:fast_unwind_on_fatal=1"
          options="MSAN_OPTIONS=$MSAN_OPTIONS" ;;
    *) echo "unknown sanitizer '$san'" >&2; exit 2 ;;
esac
cmake_args="$cmake_args ${EXTRA_CMAKE_ARGS:-}"

mapfile -t code_paths <"$here/code_paths.txt"
code=$(git -C "$repo" log -1 --format=%H -- "${code_paths[@]}")
if [ -n "$(git -C "$repo" status --porcelain --untracked-files=all -- "${code_paths[@]}")" ]; then
    echo "the code paths have local changes; a record must name committed code" >&2
    exit 2
fi

record=wakeloop-${code:0:9}-$host_id-$san
logs_root=$HOME/lab/records-logs
logs=$logs_root/$record
work=${WORK:-$HOME/lab/records-build/$record}
if [ -e "$logs" ] || [ -e "$logs.tar.gz" ]; then
    echo "$logs exists; record logs are never overwritten" >&2
    exit 2
fi
mkdir -p "$logs"
rm -rf "$work"

start=$(date +%s)
set +e
OUT_DIR="$logs" BUILD_ROOT="$work/build" PIN=none ALLOW_UNPINNED=1 \
    POSTS=11 SWEEP_POSTS=11 SWEEP_STEPS=5 WARMUP=2 RANDOM_TRIALS=1 BUILD_TESTS=1 RUN_CTEST=1 \
    CMAKE_ARGS="$cmake_args" bash "$here/run_matrix.sh" "${DESIGNS:-all}" 2>&1 | tee "$logs/run_matrix.log"
rc=${PIPESTATUS[0]}
set -e
seconds=$(( $(date +%s) - start ))

tar -czf "$logs.tar.gz" -C "$logs_root" "$record"
sha=$(sha256sum "$logs.tar.gz" | cut -d' ' -f1)
echo "$sha  $record.tar.gz" >"$logs.tar.gz.sha256"

python3 "$here/sanitizer_record.py" --out-dir "$logs" --record "$records/$record.json" \
    --repo "$(git -C "$repo" remote get-url origin)" --commit "$code" --repo-head "$(git -C "$repo" rev-parse HEAD)" \
    --sanitizer "$san" --cmake-args="$cmake_args" --options="$options" \
    --matrix-exit "$rc" --seconds "$seconds" --host "$(uname -n)" \
    --logs-archive "$logs.tar.gz" --logs-sha256 "$sha"

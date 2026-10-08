#!/usr/bin/env bash
# Linux wake-defect runs (lab host L), backends io_uring and epoll.
#
# Usage: run_matrix.sh [all|DESIGN[,DESIGN...]]  designs: gap-sweep, bound-sweep,
#        bound-sweep-qos, matrix, random-gap (or a, b, q, c, r). Listed designs share one
#        build and one (optionally shuffled) cell order.
#
# Builds the repository twice, Release with clang: `fixed` and `defect` (WAKELOOP_DEFECT=ON,
# the wake seeded out at one site per backend). Designs, one wakeprobe process per cell:
#   (a) gap-sweep        defect, B in {1, 10} ms, gap 0.1B to 3B in 30 steps of 0.1B
#   (b) bound-sweep      fixed, gap 2 ms, B in {1, 10, 100} ms and 0 (blocking)
#   (q) bound-sweep-qos  as (b), holding a PM QoS CPU latency request of 0 us (H5); these
#                        cells run under sudo, and wakeprobe drops back to this user
#   (c) matrix           both arms, B in {1, 100} ms, gap 1.7B
#   (r) random-gap       both arms, B in {1, 10} ms, each gap drawn uniformly from
#                        [0.1B, 3.0B] with GAP_SEED; RANDOM_TRIALS trials of TRIAL_POSTS
#                        consecutive measured posts (exploratory, added after review)
# Timer slack is 1 ns everywhere, plus the kernel default for (c) at B = 1 ms.
# Defect runs first measure B_eff with calibration posts (see wakeprobe.cpp).
# The cells to run are written to plan.txt in the order they ran.
#
# Environment overrides:
#   OUT_DIR          output directory (default: results/raw/<date>-L)
#   BUILD_ROOT       build directories (default: bench/build)
#   PIN              pin.sh path (default: ../../lab/bin/pin.sh in the Papers mono-repo)
#   ALLOW_UNPINNED   1 to run even when pin.sh reports an unpinned host
#   SEED             shuffle the cell order with this seed (default: fixed design order)
#   POSTS            measured posts for matrix and bound sweeps (default 51)
#   SWEEP_POSTS      measured posts per gap-sweep step (default 21)
#   SWEEP_STEPS      gap-sweep steps out of the 30, evenly spaced (default 30)
#   WARMUP           warmup posts for matrix, bound sweeps and random-gap (default 5); sweep uses 3
#   RANDOM_TRIALS    random-gap trials per cell (default 1000)
#   TRIAL_POSTS      measured posts per random-gap trial (default 21)
#   GAP_SEED         seed of the random-gap draws, the same in every cell (default 20260926)
#   CMAKE_ARGS       extra configure arguments for both builds (e.g. a sanitizer)
#   CC, CXX          compilers (default clang, clang++)
#   BUILD_TESTS      1 to build every target, the loop's tests included (default: wakeprobe only)
#   RUN_CTEST        1 to run each build's tests before any cell, with the full output of every
#                    test in ctest-<arm>.log and ctest-<arm>.xml (needs BUILD_TESTS=1)
#   REQUIRE_COMPILER if set, both builds must identify their compiler exactly as this (CMake's
#                    "The CXX compiler identification is" line), or nothing is measured
#   REQUIRE_INPUTS   if set, a gate.json from check_records.py: each build must have compiled
#                    exactly its arm's first-party inputs and configuration, or nothing is measured
#   INPUTS_HASH_PY   the Papers mono-repo's lab/bin/inputs_hash.py (default: found from here)
# After the builds, inputs.json in the output directory records what each build compiled.
set -euo pipefail

mode=""
IFS=, read -r -a requested <<<"${1:-all}"
for m in "${requested[@]}"; do
    case "$m" in
        a) m=gap-sweep ;; b) m=bound-sweep ;; q) m=bound-sweep-qos ;; c) m=matrix ;; r) m=random-gap ;;
        all|gap-sweep|bound-sweep|bound-sweep-qos|matrix|random-gap) ;;
        *) echo "unknown design '$m'" >&2; exit 2 ;;
    esac
    mode="$mode,$m"
done
mode=${mode#,}
want() { [[ ",$mode," == *",all,"* || ",$mode," == *",$1,"* ]]; }

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
stamp=$(date +%Y-%m-%d)
out=${OUT_DIR:-$repo/results/raw/$stamp-L}
build=${BUILD_ROOT:-$here/build}
pin=${PIN:-$repo/../../lab/bin/pin.sh}
posts=${POSTS:-51}
sweep_posts=${SWEEP_POSTS:-21}
sweep_steps=${SWEEP_STEPS:-30}
warmup=${WARMUP:-5}
random_trials=${RANDOM_TRIALS:-1000}
trial_posts=${TRIAL_POSTS:-21}
gap_seed=${GAP_SEED:-20260926}
backends="io_uring epoll"
calibrate=11
mapfile -t code_paths <"$here/code_paths.txt"
mkdir -p "$out"
out=$(cd "$out" && pwd)

# Host state first. pin.sh sets the governor and boost and prints a JSON fingerprint.
if [ -f "$pin" ]; then
    set +e
    bash "$pin" >"$out/pin.json"
    pin_rc=$?
    set -e
    echo "pin.sh exit $pin_rc: $(cat "$out/pin.json")"
    if [ "$pin_rc" -ne 0 ] && [ "${ALLOW_UNPINNED:-0}" != 1 ]; then
        echo "host is not pinned; set ALLOW_UNPINNED=1 to run anyway" >&2
        exit 1
    fi
elif [ "${ALLOW_UNPINNED:-0}" != 1 ]; then
    echo "no pin.sh at $pin; set PIN or ALLOW_UNPINNED=1" >&2
    exit 1
fi

{
    echo "date: $(date -Is)"
    echo "mode: $mode"
    echo "seed: ${SEED:-none}"
    echo "sizes: posts=$posts sweep_posts=$sweep_posts sweep_steps=$sweep_steps warmup=$warmup"
    echo "random_gap: trials=$random_trials trial_posts=$trial_posts gap_seed=$gap_seed range=0.1:3.0"
    echo "cmake_args: ${CMAKE_ARGS:-}"
    echo "uname: $(uname -a)"
    echo "compiler: $(${CXX:-clang++} --version | head -1)"
    echo "timer_slack_default_ns: $(cat /proc/self/timerslack_ns 2>/dev/null || echo unknown)"
    echo "io_uring_disabled: $(cat /proc/sys/kernel/io_uring_disabled 2>/dev/null || echo unknown)"
    # The idle states the cores may enter: driver, governor, and per state of cpu0 its name,
    # exit latency and target residency in us, and whether it is disabled.
    echo "cpuidle_driver: $(cat /sys/devices/system/cpu/cpuidle/current_driver 2>/dev/null || echo unknown)"
    echo "cpuidle_governor: $(cat /sys/devices/system/cpu/cpuidle/current_governor 2>/dev/null || echo unknown)"
    for s in /sys/devices/system/cpu/cpu0/cpuidle/state*; do
        [ -d "$s" ] && echo "cpuidle_state: $(cat "$s/name") latency_us=$(cat "$s/latency") residency_us=$(cat "$s/residency") disable=$(cat "$s/disable")"
    done
    echo "repo_head: $(git -C "$repo" rev-parse HEAD)"
    echo "code_commit: $(git -C "$repo" log -1 --format=%H -- "${code_paths[@]}")"
} >"$out/env.txt"

# Fresh build directories, so the compiler and the injected commit cannot be stale.
for arm in fixed defect; do
    defect=OFF
    [ "$arm" = defect ] && defect=ON
    rm -rf "${build:?}/$arm"
    echo "building $arm (WAKELOOP_DEFECT=$defect)"
    # shellcheck disable=SC2086  # CMAKE_ARGS is a word list
    cmake -S "$repo" -B "$build/$arm" -G Ninja -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_C_COMPILER="${CC:-clang}" -DCMAKE_CXX_COMPILER="${CXX:-clang++}" \
        -DWAKELOOP_DEFECT="$defect" ${CMAKE_ARGS:-} >"$out/build-$arm.log" 2>&1
    if [ "${BUILD_TESTS:-0}" = 1 ]; then
        cmake --build "$build/$arm" -j"$(nproc)" >>"$out/build-$arm.log" 2>&1
    else
        cmake --build "$build/$arm" --target wakeprobe -j"$(nproc)" >>"$out/build-$arm.log" 2>&1
    fi
done
sha256sum "$build/fixed/bench/wakeprobe" "$build/defect/bench/wakeprobe" >"$out/binaries.sha256"

# The gate: the compiler CMake identified for each build must be the one the sanitizer
# records name, checked on the builds about to be measured and before any cell runs.
if [ -n "${REQUIRE_COMPILER:-}" ]; then
    for arm in fixed defect; do
        id=$(sed -n 's/^-- The CXX compiler identification is //p' "$out/build-$arm.log" | head -1)
        if [ "$id" != "$REQUIRE_COMPILER" ]; then
            echo "build $arm identified its compiler as '$id', the sanitizer records name '$REQUIRE_COMPILER'; nothing measured" >&2
            exit 1
        fi
    done
    echo "compiler matches the sanitizer records: $REQUIRE_COMPILER"
fi

# What each build compiled (first-party inputs per target, the options that select code), and
# that the loop's sources are among them. With REQUIRE_INPUTS this is also the gate.
inputs_py=${INPUTS_HASH_PY:-$repo/../../lab/bin/inputs_hash.py}
expect=()
[ -n "${REQUIRE_INPUTS:-}" ] && expect=(--expect "$REQUIRE_INPUTS")
if ! python3 "$here/inputs_gate.py" --inputs-hash "$inputs_py" --build "fixed=$build/fixed" \
        --build "defect=$build/defect" --out "$out/inputs.json" "${expect[@]}"; then
    echo "the builds failed the inputs gate (see above); nothing measured" >&2
    exit 1
fi

# The loop's tests, with the full output of every test kept (the record scans all of it).
if [ "${RUN_CTEST:-0}" = 1 ]; then
    for arm in fixed defect; do
        set +e
        ctest --test-dir "$build/$arm" -V --output-junit "$out/ctest-$arm.xml" >"$out/ctest-$arm.log" 2>&1
        echo "ctest $arm: exit $?; $(grep -E 'tests passed' "$out/ctest-$arm.log")"
        set -e
    done
fi

# The plan: one line per cell, fields separated by spaces:
#   design arm backend bound_us gap_us posts warmup slack_ns qos_us file
# gap_us is a number of microseconds, or rLO:HI for a gap drawn from [LO x B, HI x B].
cells=()
add() { cells+=("$*"); }
slack_name() { [ "$1" = 0 ] && echo default || echo "${1}ns"; }

if want gap-sweep; then
    for backend in $backends; do
        for bound in 1000 10000; do
            for i in $(seq 1 "$sweep_steps"); do
                step=$(( (i * 30 + sweep_steps - 1) / sweep_steps ))
                gap=$((bound * step / 10))
                add gap-sweep defect "$backend" "$bound" "$gap" "$sweep_posts" 3 1 - \
                    "$backend-${bound}us-g${gap}us-defect-slack1ns.jsonl"
            done
        done
    done
fi
for design in bound-sweep bound-sweep-qos; do
    if want "$design"; then
        qos=-; tag=""
        [ "$design" = bound-sweep-qos ] && { qos=0; tag="-qos0us"; }
        for backend in $backends; do
            for bound in 0 1000 10000 100000; do
                add "$design" fixed "$backend" "$bound" 2000 "$posts" "$warmup" 1 "$qos" \
                    "$backend-${bound}us-g2000us-fixed-slack1ns$tag.jsonl"
            done
        done
    fi
done
if want matrix; then
    for backend in $backends; do
        for bound in 1000 100000; do
            gap=$((bound * 17 / 10))
            slacks=1
            [ "$bound" = 1000 ] && slacks="1 0"
            for slack in $slacks; do
                for arm in fixed defect; do
                    add matrix "$arm" "$backend" "$bound" "$gap" "$posts" "$warmup" "$slack" - \
                        "$backend-${bound}us-g${gap}us-$arm-slack$(slack_name "$slack").jsonl"
                done
            done
        done
    done
fi

if want random-gap; then
    for backend in $backends; do
        for bound in 1000 10000; do
            for arm in fixed defect; do
                add random-gap "$arm" "$backend" "$bound" r0.1:3.0 $((random_trials * trial_posts)) "$warmup" 1 - \
                    "$backend-${bound}us-random-$arm-slack1ns.jsonl"
            done
        done
    done
fi

if [ -n "${SEED:-}" ]; then
    mapfile -t cells < <(printf '%s\n' "${cells[@]}" | python3 -c \
        'import random, sys; lines = sys.stdin.read().splitlines(); random.Random(int(sys.argv[1])).shuffle(lines); print("\n".join(lines))' \
        "$SEED")
fi
{ echo "# seed: ${SEED:-none}"; printf '%s\n' "${cells[@]}"; } >"$out/plan.txt"

# A failed cell is logged to failures.txt and the run goes on; the exit status reports it.
failed=0
run_cell() {
    local design=$1 arm=$2 backend=$3 bound=$4 gap=$5 n=$6 warm=$7 slack=$8 qos=$9 file=${10} rc=0
    local gap_args=(--gap-us "$gap")
    [[ "$gap" == r* ]] && gap_args=(--gap-frac-range "${gap#r}" --gap-seed "$gap_seed")
    local cmd=("$build/$arm/bench/wakeprobe" --design "$design" --label "$arm" --backend "$backend"
               --wait-us "$bound" "${gap_args[@]}" --posts "$n" --warmup "$warm"
               --calibrate "$calibrate" --timerslack-ns "$slack" --out "$out/$design/$file")
    mkdir -p "$out/$design"
    if [ "$qos" != - ]; then
        # Root only to open /dev/cpu_dma_latency; wakeprobe drops back to this user.
        cmd=(sudo --preserve-env=ASAN_OPTIONS,UBSAN_OPTIONS,TSAN_OPTIONS,MSAN_OPTIONS "${cmd[@]}" --pm-qos-us "$qos")
    fi
    "${cmd[@]}" >>"$out/summaries.log" 2>&1 || rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "exit $rc: $design/$file" | tee -a "$out/failures.txt" >&2
        failed=$((failed + 1))
    fi
}
echo "running ${#cells[@]} cells"
for cell in "${cells[@]}"; do
    # shellcheck disable=SC2086  # fields are space separated by construction
    run_cell $cell
done
echo "results in $out ($failed failed cells)"
[ "$failed" -eq 0 ]

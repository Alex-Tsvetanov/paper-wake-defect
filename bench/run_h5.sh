#!/usr/bin/env bash
# H5 on Linux: the 2026-09 designs, run again with v2's wakeprobe (backends io_uring and epoll).
#
# Usage: run_h5.sh --build-dir DIR --out-dir DIR [--seed N] [--no-pin] [--dry-run] [--expect-cells N]
#   --build-dir DIR     an existing v2 Release build directory; this script never builds
#   --out-dir DIR       plan.txt and, per cell, <cell>.jsonl, <cell>.out and <cell>.err go here
#   --seed N            shuffle the cell order with this seed (default 20261026)
#   --no-pin            do not pass --worker-cpus 14 --poster-cpu 2 to wakeprobe (pinned by default)
#   --dry-run           write plan.txt and print every wakeprobe command line; run nothing
#   --expect-cells N    exit 1 unless the plan has exactly N cells (the CTest entry uses this)
# A run that is not a dry run needs the environment variable LAB_JOB (the lab runner sets it on
# the laptop); without it the script exits 2. It also exits 2, before it writes anything, when the
# output directory already holds a .jsonl file.
#
# The designs of bench/run_matrix.sh, less its PM QoS design (bound-sweep-qos) and its random-gap
# design. One wakeprobe process per cell, always --workers 1 --protocol single:
#   gap-sweep    defect arm, B in {1, 10} ms, fixed gap 0.1 B to 3.0 B in 30 steps of 0.1 B
#   bound-sweep  fixed arm, gap 2 ms, B in {1, 10, 100} ms and 0 (blocking)
#   matrix       both arms, B in {1, 100} ms, gap 1.7 B
# Timer slack is 1 ns everywhere, plus the kernel default for the matrix at B = 1 ms.
# Backends: io_uring, epoll. The arms are variants of one binary (--variant):
#   fixed   plain_wake
#   defect  absent_wake on epoll, unobserved_primitive on io_uring
# Every cell is labelled with its arm (fixed or defect), as in 2026-09. Defect runs measure B_eff
# with calibration posts (see wakeprobe.cpp). Cells: 120 + 8 + 12 = 140.
#
# plan.txt lists the cells in run order and is written before any cell runs. It also records the
# seed, the placement and the sha256 of wakeprobe. One log line per cell gives its exit code.
# A failing cell is run once more, into <cell>-rerun.jsonl; the first run's files stay. Every run
# has its own stdout file (.out) and stderr file (.err). A cell whose rerun also fails is logged as
# incomplete and the run goes on. At the end the script prints "incomplete cells: N" and exits 1
# when N > 0, else 0 (hypotheses-v2.md, section 8).
set -euo pipefail

# The sizes of 2026-09 (run_matrix.sh defaults). They are fixed here on purpose.
posts=51
sweep_posts=21
warmup=5
sweep_warmup=3
calibrate=11
backends="io_uring epoll"

die() { echo "run_h5.sh: $*" >&2; exit 2; }
usage() {
    echo "usage: run_h5.sh --build-dir DIR --out-dir DIR [--seed N] [--no-pin] [--dry-run] [--expect-cells N]" >&2
    exit 2
}

build_dir=""
out_dir=""
seed=20261026
pin=1
dry=0
expect=""
while [ $# -gt 0 ]; do
    case "$1" in
        --build-dir|--out-dir|--seed|--expect-cells)
            [ $# -ge 2 ] || die "missing value for $1"
            case "$1" in
                --build-dir) build_dir=$2 ;;
                --out-dir) out_dir=$2 ;;
                --seed) seed=$2 ;;
                --expect-cells) expect=$2 ;;
            esac
            shift 2 ;;
        --no-pin) pin=0; shift ;;
        --dry-run) dry=1; shift ;;
        -h|--help) usage ;;
        *) die "unknown argument '$1'" ;;
    esac
done
[ -n "$build_dir" ] || usage
[ -n "$out_dir" ] || usage
[[ $seed =~ ^[0-9]+$ ]] || die "--seed expects a whole number, got '$seed'"
[[ -z $expect || $expect =~ ^[0-9]+$ ]] || die "--expect-cells expects a whole number, got '$expect'"

# The lab gate: a timed run is started by the lab runner, which sets LAB_JOB on the laptop.
if [ "$dry" != 1 ] && [ -z "${LAB_JOB:-}" ]; then
    die "refusing a timed run: LAB_JOB is not set (the lab runner sets it on the laptop); use --dry-run to check the plan"
fi

[ -d "$build_dir" ] || die "no build directory at $build_dir"
build_dir=$(cd "$build_dir" && pwd)
wakeprobe=$build_dir/bench/wakeprobe
[ -x "$wakeprobe" ] || die "no wakeprobe at $wakeprobe; build it first (this script never builds)"
command -v python3 >/dev/null || die "python3 is needed for the seeded shuffle"
command -v sha256sum >/dev/null || die "sha256sum is needed to record the wakeprobe hash"

# Only a Release build is ever timed. A dry run only warns. No CMakeCache.txt counts as unknown.
build_type=$(sed -n 's/^CMAKE_BUILD_TYPE:[A-Z]*=//p' "$build_dir/CMakeCache.txt" 2>/dev/null | head -1 || true)
if [ "$build_type" != Release ]; then
    if [ "$dry" = 1 ]; then
        echo "run_h5.sh: warning: $build_dir is built as '${build_type:-unknown}', not Release" >&2
    else
        die "$build_dir is built as '${build_type:-unknown}', not Release; nothing measured"
    fi
fi

# Nothing is overwritten: a directory that holds records is not reused.
if [ -d "$out_dir" ]; then
    old=$(find "$out_dir" -name '*.jsonl' -print -quit 2>/dev/null || true)
    [ -z "$old" ] || die "the output directory $out_dir already holds records (for example $old); use an empty one"
fi
mkdir -p "$out_dir"
out_dir=$(cd "$out_dir" && pwd)

# The plan: one line per cell, fields separated by spaces:
#   design arm variant backend bound_us gap_us posts warmup slack_ns file
cells=()
declare -A seen=()
add() {
    local file=${!#}
    [[ -z ${seen[$file]+x} ]] || die "two cells would write $file"
    seen[$file]=1
    cells+=("$*")
}
variant_of() {  # arm backend
    if [ "$1" = fixed ]; then echo plain_wake
    elif [ "$2" = io_uring ]; then echo unobserved_primitive
    else echo absent_wake
    fi
}
slack_name() { if [ "$1" = 0 ]; then echo default; else echo "${1}ns"; fi; }

for backend in $backends; do
    for bound in 1000 10000; do
        for i in $(seq 1 30); do
            gap=$((bound * i / 10))
            add gap-sweep defect "$(variant_of defect "$backend")" "$backend" "$bound" "$gap" "$sweep_posts" \
                "$sweep_warmup" 1 "gap-sweep-$backend-${bound}us-g${gap}us-defect-slack1ns.jsonl"
        done
    done
done
for backend in $backends; do
    for bound in 0 1000 10000 100000; do
        add bound-sweep fixed "$(variant_of fixed "$backend")" "$backend" "$bound" 2000 "$posts" "$warmup" 1 \
            "bound-sweep-$backend-${bound}us-g2000us-fixed-slack1ns.jsonl"
    done
done
for backend in $backends; do
    for bound in 1000 100000; do
        gap=$((bound * 17 / 10))
        slacks=1
        [ "$bound" = 1000 ] && slacks="1 0"
        for slack in $slacks; do
            for arm in fixed defect; do
                add matrix "$arm" "$(variant_of "$arm" "$backend")" "$backend" "$bound" "$gap" "$posts" "$warmup" \
                    "$slack" "matrix-$backend-${bound}us-g${gap}us-$arm-slack$(slack_name "$slack").jsonl"
            done
        done
    done
done
total=${#cells[@]}

# The shuffle is Python's random.Random(seed), as in run_matrix.sh.
mapfile -t cells < <(printf '%s\n' "${cells[@]}" | python3 -c \
    'import random, sys; lines = sys.stdin.read().splitlines(); random.Random(int(sys.argv[1])).shuffle(lines); print("\n".join(lines))' \
    "$seed")
[ "${#cells[@]}" -eq "$total" ] || die "the shuffle changed the number of cells (${#cells[@]} of $total)"

count_of() {
    local n=0 c
    for c in "${cells[@]}"; do [[ $c == "$1 "* ]] && n=$((n + 1)); done
    echo "$n"
}
counts="gap-sweep $(count_of gap-sweep), bound-sweep $(count_of bound-sweep), matrix $(count_of matrix)"
if [ "$pin" = 1 ]; then placement="worker-cpus 14, poster-cpu 2"; else placement="off (--no-pin)"; fi
wakeprobe_sha=$(sha256sum "$wakeprobe" | cut -d' ' -f1)
{
    echo "# seed: $seed"
    echo "# build: $build_dir"
    echo "# wakeprobe sha256: $wakeprobe_sha"
    echo "# placement: $placement"
    echo "# cells: $total ($counts)"
    echo "# fields: design arm variant backend bound_us gap_us posts warmup slack_ns file"
    printf '%s\n' "${cells[@]}"
} >"$out_dir/plan.txt"
echo "plan: $total cells ($counts), seed $seed, written to $out_dir/plan.txt"
if [ -n "$expect" ] && [ "$total" -ne "$expect" ]; then
    echo "run_h5.sh: the plan has $total cells, expected $expect" >&2
    exit 1
fi

# One run of one cell: the command, the record and the files of its stdout and stderr. The
# suffix is empty for the first run and -rerun for the second.
cmd=()
stdout_file=""
stderr_file=""
make_cmd() {  # the fields of a plan line, then the suffix
    local design=$1 arm=$2 variant=$3 backend=$4 bound=$5 gap=$6 n=$7 warm=$8 slack=$9 file=${10} suffix=${11:-}
    local stem=${file%.jsonl}
    stdout_file=$out_dir/$stem$suffix.out
    stderr_file=$out_dir/$stem$suffix.err
    cmd=("$wakeprobe" --design "$design" --label "$arm" --backend "$backend" --variant "$variant"
         --workers 1 --protocol single --wait-us "$bound" --gap-us "$gap" --posts "$n" --warmup "$warm"
         --calibrate "$calibrate" --timerslack-ns "$slack" --out "$out_dir/$stem$suffix.jsonl")
    if [ "$pin" = 1 ]; then
        cmd+=(--worker-cpus 14 --poster-cpu 2)
    fi
}
run_cell() {  # a plan line, then the suffix; returns wakeprobe's exit code
    local rc=0
    # shellcheck disable=SC2086  # fields are space separated by construction
    make_cmd $1 "$2"
    "${cmd[@]}" >"$stdout_file" 2>"$stderr_file" || rc=$?
    return "$rc"
}

if [ "$dry" = 1 ]; then
    i=0
    for cell in "${cells[@]}"; do
        i=$((i + 1))
        # shellcheck disable=SC2086
        make_cmd $cell ""
        printf '%s: ' "$i"
        printf '%q ' "${cmd[@]}"
        printf '> %q 2> %q\n' "$stdout_file" "$stderr_file"
    done
    echo "dry run: $total cells"
    exit 0
fi

echo "running $total cells"
i=0
incomplete=0
for cell in "${cells[@]}"; do
    i=$((i + 1))
    file=${cell##* }
    stem=${file%.jsonl}
    rc=0
    run_cell "$cell" "" || rc=$?
    echo "$(date -Is) cell $i/$total exit $rc $file"
    if [ "$rc" -ne 0 ]; then
        rc2=0
        run_cell "$cell" -rerun || rc2=$?
        echo "$(date -Is) cell $i/$total rerun exit $rc2 $stem-rerun.jsonl"
        if [ "$rc2" -ne 0 ]; then
            echo "$(date -Is) cell $i/$total INCOMPLETE $file (see $stem.err and $stem-rerun.err)"
            incomplete=$((incomplete + 1))
        fi
    fi
done
echo "results in $out_dir"
echo "incomplete cells: $incomplete"
if [ "$incomplete" -gt 0 ]; then
    exit 1
fi
exit 0

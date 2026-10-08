#!/usr/bin/env bash
# Publication runs (tier T3) on the Linux lab host L.
#
# Usage: run_publication.sh            run
#        CHECK_ONLY=1 run_publication.sh   checks and plan only, nothing measured
#
# Refuses to start unless
#   - the code paths (bench/code_paths.txt) have no local changes, and
#   - lab/sanitizer-records covers the builds about to be made (check_records.py): green
#     wakeloop records for the code commit (ASan+UBSan, TSan, MSan) that agree, per arm, on the
#     compiler, the compiled first-party inputs and the configuration.
# Then run_matrix.sh builds both arms and stops before any cell runs unless both builds
# identify their compiler as the records do and each compiled exactly its arm's inputs in its
# arm's configuration (gate.json in the run directory, checked by inputs_gate.py; inputs.json
# records what the builds compiled). CHECK_ONLY checks the records only: a build's compiled
# inputs exist only after it is built. Then the designs in DESIGNS run with pin.sh first (an
# unpinned host stops the run) and the cell order shuffled with SEED, written to
# results/raw/<RUN_NAME>/.
#
# Environment: RECORDS (default ../../lab/sanitizer-records), SEED (default 20260926), RUN_NAME
# (default <date>-L-publication), INPUTS_HASH_PY (default ../../lab/bin/inputs_hash.py), DESIGNS
# (default all; a run_matrix.sh design list such as random-gap).
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
records=${RECORDS:-$repo/../../lab/sanitizer-records}
seed=${SEED:-20260926}
out=$repo/results/raw/${RUN_NAME:-$(date +%Y-%m-%d)-L-publication}

refuse() { echo "run_publication: $*" >&2; exit 1; }

mapfile -t code_paths <"$here/code_paths.txt"
code=$(git -C "$repo" log -1 --format=%H -- "${code_paths[@]}")
[ -z "$(git -C "$repo" status --porcelain --untracked-files=all -- "${code_paths[@]}")" ] ||
    refuse "the code paths have local changes"

gate=$(python3 "$here/check_records.py" --records "$records" --code "$code" --host L) ||
    refuse "the sanitizer records do not cover this build (see above)"
record_compiler=$(printf '%s' "$gate" | python3 -c 'import json, sys; print(json.load(sys.stdin)["compiler"])')

echo "code $code, seed $seed, designs ${DESIGNS:-all} -> $out"
if [ "${CHECK_ONLY:-0}" = 1 ]; then
    echo "CHECK_ONLY: nothing run (the compiled inputs are checked after the builds of a real run)"
    exit 0
fi
[ ! -e "$out" ] || refuse "$out exists; publication output is never overwritten"
mkdir -p "$out"
printf '%s\n' "$gate" >"$out/gate.json"
OUT_DIR="$out" SEED="$seed" BUILD_ROOT="$here/build-publication" \
    REQUIRE_COMPILER="$record_compiler" REQUIRE_INPUTS="$out/gate.json" bash "$here/run_matrix.sh" "${DESIGNS:-all}"

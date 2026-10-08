#!/usr/bin/env python3
"""The sanitizer gate of a publication run: which records cover the builds about to be made.

    check_records.py --records DIR --code SHA --host L|W [--out gate.json]

The wakeloop records for the code commit must exist, be green, and agree on each arm:

    L: wakeloop-<code>-L-asan.json, -L-tsan.json, -L-msan.json (clang)
    W: wakeloop-<code>-W-asan-clangcl.json (clang-cl, the compiler of the measured W builds)

Each record has a fixed and a defect section, each with its compiler, config (the options that
select compiled code) and inputs_hash (first-party inputs per target). All required records
must name the same values per arm, and one compiler for both arms. Other records on the host
(for example W's MSVC ASan record) are extra coverage and do not gate.

Prints one "record ok" line per record on stderr and the gate as JSON on stdout or to --out:
{"compiler", "builds": {"fixed": {"inputs_hash", "config"}, "defect": {...}}, "records"}.
run_matrix then checks each build against its arm (inputs_gate.py --expect) before any cell
runs; this script alone cannot prove the match, since a build's inputs exist only once built.
Exits 1 with the reason when the records do not cover the build.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REQUIRED = {"L": ["asan", "tsan", "msan"], "W": ["asan-clangcl"]}
ARMS = ("fixed", "defect")


def refuse(why: str) -> None:
    sys.exit(f"check_records: {why}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--records", type=Path, required=True)
    ap.add_argument("--code", required=True, help="the code commit (full sha)")
    ap.add_argument("--host", choices=sorted(REQUIRED), required=True)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    seen = []
    for suffix in REQUIRED[args.host]:
        f = args.records / f"wakeloop-{args.code[:9]}-{args.host}-{suffix}.json"
        if not f.exists():
            refuse(f"no sanitizer record {f}")
        r = json.loads(f.read_text(encoding="utf-8"))
        if r.get("artifact") != "wakeloop" or r.get("commit") != args.code:
            refuse(f"{f.name} is not a wakeloop record of code commit {args.code}")
        if r.get("green") is not True:
            refuse(f"{f.name} is not green")
        values = {}
        for arm in ARMS:
            a = (r.get("arms") or {}).get(arm)
            if not a or not a.get("green") or not a.get("compiler") or not a.get("inputs_hash") or a.get("config") is None:
                refuse(f"{f.name}: the {arm} arm is missing, red, or names no compiler, inputs or config")
            values[arm] = {"compiler": a["compiler"], "inputs_hash": a["inputs_hash"], "config": a["config"]}
        if values["fixed"]["compiler"] != values["defect"]["compiler"]:
            refuse(f"{f.name}: the arms were built with different compilers")
        seen.append((f.name, values))
        print(f"record ok: {f.name} ({values['fixed']['compiler']}; fixed wakeloop "
              f"{values['fixed']['inputs_hash'].get('wakeloop', '')[:12]}, defect wakeloop "
              f"{values['defect']['inputs_hash'].get('wakeloop', '')[:12]})", file=sys.stderr)

    first_name, first = seen[0]
    for name, values in seen[1:]:
        if values != first:
            refuse(f"{name} disagrees with {first_name} on compiler, compiled inputs or configuration")
    gate = {"compiler": first["fixed"]["compiler"],
            "builds": {arm: {"inputs_hash": first[arm]["inputs_hash"], "config": first[arm]["config"]} for arm in ARMS},
            "records": [name for name, _ in seen]}
    text = json.dumps(gate, indent=1)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# Derived from paper-io-core bench/check_records.py (itself from paper-one-port bench/check_records.py
# at commit 1f27ea8), same author (Apache-2.0).
# Not the 2026-09 bench/check_records.py, which gates by code commit, belongs to the v1 study and stays as it is.
"""Reports the sanitizer gate of a measured wakeloop build. It is only a reporting wrapper:

    check_records_v2.py --build-dir DIR --records DIR --other-records DIR --host L|W [--inputs-hash PATH]
                        [--accept-dry-run] [--out gate.json]

The gate is bench/gate_lib.py's run_gate(), the same code bench/run_campaign.py runs in its own process at the
start of every timed run and every --resume (and in its --check-gate mode, which run_h5.sh and run_h5.ps1 call
before any cell). The runner never reads the file this script writes: the output is for people and for the
record of a run. See gate_lib.py for what the gate checks.

--records is the folder of the records of --host (W: laptop-w alone), --other-records the other system's. The
output (stdout or --out) names the host, the inputs and configuration it computed from the build, the records
used with their sha256, the other system's records, the code fingerprint, the generator files' and
coverage.json's sha256, and the binaries' sha256. A gate that fails writes nothing and exits 1 with the reason.
--accept-dry-run lets the drivers' dry run exercise the matching on dry-run records made on any host; its
output is marked "dry_run" and "citable": false, and the runner has no such option.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import gate_lib
from gate_lib import SANITIZERS_BY_HOST, GateError


def report(result: dict) -> dict:
    """The gate result as JSON: the shape keys as a count and a sorted list."""
    out = dict(result)
    keys = sorted(list(k) for k in out.pop("smoke_keys"))
    out["smoke_shape_keys"] = {"count": len(keys), "keys": keys}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-dir", type=Path, required=True, help="the measured build")
    ap.add_argument("--records", type=Path, required=True, help="the records of --host (W: laptop-w alone)")
    ap.add_argument("--other-records", type=Path, required=True, help="the records of the other system")
    ap.add_argument("--host", required=True, choices=sorted(SANITIZERS_BY_HOST), help="the host the build runs on")
    ap.add_argument("--inputs-hash", type=Path, help="lab/bin/inputs_hash.py (default: found by bench/build_inputs.py)")
    ap.add_argument("--accept-dry-run", action="store_true",
                    help="the drivers' dry run: dry-run records count, and the output is marked and never citable")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    try:
        result = gate_lib.run_gate(build=a.build_dir, host=a.host, records=a.records, other_records=a.other_records,
                                   tool=a.inputs_hash, accept_dry_run=a.accept_dry_run)
    except GateError as e:
        sys.exit(f"check_records_v2: {e}")
    text = json.dumps(report(result), indent=1)
    if a.out:
        a.out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

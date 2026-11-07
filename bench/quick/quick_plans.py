"""Writes or checks the five P1 quick plans (quick-plan.md) under bench/quick/plans/.

    python bench/quick/quick_plans.py          writes the plans and prints each plan's sha256 and arithmetic
    python bench/quick/quick_plans.py --check  exits 1 unless every committed plan equals what this generates

Each plan is JSON Lines: a header, then one row per wakeprobe process in run order. The rows are fully
expanded (rounds, condition blocks, the shuffled order and every argument), so the plan's sha256 pins
them all; the runner never reshuffles. Nothing here times anything.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quick_common as qc  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="compare the committed plans with a fresh generation")
    a = ap.parse_args(argv)
    qc.PLANS_DIR.mkdir(parents=True, exist_ok=True)
    bad = 0
    for name, spec in qc.SPECS.items():
        text = qc.plan_text(spec)
        path = qc.PLANS_DIR / f"{name}.jsonl"
        if a.check:
            if not path.exists() or path.read_bytes().replace(b"\r\n", b"\n") != text.encode("utf-8"):
                print(f"DIFFERS: {path.name}")
                bad += 1
            continue
        path.write_bytes(text.encode("utf-8"))
        trials = qc.trials_for(spec)
        round_s = qc.round_estimate_s(spec, trials)
        print(f"{name}: {spec.os} {spec.run} rounds {spec.rounds} trials/process {trials} round {round_s:.1f} s "
              f"total {spec.rounds * round_s:.1f} s with margin {spec.rounds * round_s * qc.MARGIN:.1f} s "
              f"sha256 {qc.file_sha256(path)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

"""One spinning process pinned to one CPU: the processor load of run R1 (quick-plan.md).

    python -I quick_hog.py --cpu N [--max-s S]

It pins itself to CPU N, prints "ready N" and spins until it is killed or S seconds pass (default 900),
so that no hog outlives its job even if the runner dies (CLAUDE.md rule 9). It reads no clock for any
measurement and writes nothing else.
"""

from __future__ import annotations

import argparse
import os
import sys
import time


def pin(cpu: int) -> None:
    if hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, {cpu})
        return
    import ctypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    k32.SetProcessAffinityMask.restype = ctypes.c_int
    if not k32.SetProcessAffinityMask(k32.GetCurrentProcess(), 1 << cpu):
        raise OSError(ctypes.get_last_error(), f"SetProcessAffinityMask to CPU {cpu} failed")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cpu", type=int, required=True)
    ap.add_argument("--max-s", type=float, default=900.0)
    a = ap.parse_args(argv)
    try:
        pin(a.cpu)
    except OSError as e:
        print(f"hog: cannot pin to CPU {a.cpu}: {e}", file=sys.stderr, flush=True)
        return 3
    print(f"ready {a.cpu}", flush=True)
    end = time.monotonic() + a.max_s
    n = 0
    while True:
        n += 1
        if n % 1_000_000 == 0 and time.monotonic() > end:
            return 0


if __name__ == "__main__":
    sys.exit(main())

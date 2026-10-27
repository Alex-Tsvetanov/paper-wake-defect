"""The plan generator against the frozen grid (sections 2, 3, 5, 7, 8 and 9) and the tools' parsers."""

import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import grid
import h4log
import plan
import readers
import synth
from tempgit import TempGit

STAND_IN_R = 14


def stand_in_r(system, r=STAND_IN_R):
    return {cell: r for cell in grid.h4_cells(system)}


def args_map(args):
    """Flag -> value; every flag in a plan takes a value."""
    out = {}
    for i in range(0, len(args), 2):
        out[args[i]] = args[i + 1]
    return out


# ---------------------------------------------------------------------------------------------
# A model of each tool's argument checks (bench/wakeprobe.cpp parse_args and run_with,
# bench/abpair.cpp parse_args). A plan line that fails here would exit 2 on the laptop.
# ---------------------------------------------------------------------------------------------

WAKEPROBE_FLAGS = {"--backend", "--variant", "--workers", "--worker-cpus", "--poster-cpu", "--protocol",
                   "--wait-us", "--gap-us", "--posts", "--warmup", "--calibrate", "--out", "--label", "--design",
                   "--phase-frac", "--gap-frac-range", "--gap-seed", "--gap-unit"}
WAKEPROBE_LINUX_ONLY = {"--timerslack-ns", "--pm-qos-us"}
WAKEPROBE_WINDOWS_ONLY = {"--timer-period-ms"}
ABPAIR_FLAGS = {"--backend", "--arm-a", "--arm-b", "--workers", "--producers", "--inflight", "--pairs", "--order",
                "--seed", "--window-ms", "--window-tasks", "--work-ns", "--worker-cpus", "--producer-cpus", "--out"}


def _pairs(args, allowed):
    if len(args) % 2:
        return None, "odd argument count"
    seen = {}
    for i in range(0, len(args), 2):
        flag, value = args[i], args[i + 1]
        if flag not in allowed:
            return None, "unknown flag %s" % flag
        if flag in seen:
            return None, "flag %s given twice" % flag
        seen[flag] = value
    return seen, None


def _cpus(text):
    parts = text.split(",")
    if not all(p.isdigit() for p in parts):
        return None
    return [int(p) for p in parts]


def _frac_ok(text):
    try:
        v = float(text)
    except ValueError:
        return False
    return 0 < v < 100


def wakeprobe_errors(args, system):
    allowed = set(WAKEPROBE_FLAGS)
    allowed |= WAKEPROBE_LINUX_ONLY if system == "linux" else WAKEPROBE_WINDOWS_ONLY
    a, err = _pairs(args, allowed)
    if err:
        return [err]
    errs = []
    if "--out" in a:
        errs.append("--out belongs to the runner")
    if a.get("--backend") not in grid.BACKENDS[system]:
        errs.append("backend")
    variant = a.get("--variant", "none")
    if variant not in grid.VARIANTS or not grid.has_site(a.get("--backend"), variant):
        errs.append("variant or site")
    workers = int(a.get("--workers", "1"))
    if not 1 <= workers <= 64 or workers < grid.min_workers(variant):
        errs.append("workers")
    cpus = _cpus(a["--worker-cpus"]) if "--worker-cpus" in a else []
    if cpus is None or (cpus and len(cpus) != workers):
        errs.append("worker cpus")
    all_cpus = list(cpus or []) + ([int(a["--poster-cpu"])] if "--poster-cpu" in a else [])
    if len(set(all_cpus)) != len(all_cpus):
        errs.append("cpus not distinct")
    gap_modes = sum(1 for f in ("--gap-us", "--phase-frac", "--gap-frac-range") if f in a)
    if gap_modes != 1:
        errs.append("exactly one gap mode")
    if "--gap-frac-range" in a:
        lo, _, hi = a["--gap-frac-range"].partition(":")
        if not (_frac_ok(lo) and _frac_ok(hi) and float(lo) < float(hi)):
            errs.append("gap range")
    if "--phase-frac" in a and not _frac_ok(a["--phase-frac"]):
        errs.append("phase frac")
    if a.get("--gap-unit", "bound") not in ("bound", "timer"):
        errs.append("gap unit")
    if a.get("--gap-unit") == "timer" and "--gap-frac-range" not in a:
        errs.append("gap unit timer needs a range")
    wait = int(a.get("--wait-us", "-1"))
    posts = int(a.get("--posts", "51"))
    warmup = int(a.get("--warmup", "5"))
    calibrate = int(a.get("--calibrate", "11"))
    if wait < 0:
        errs.append("wait")
    if "--gap-frac-range" in a and wait <= 0:
        errs.append("range needs a bound")
    protocol = a.get("--protocol", "single")
    if protocol not in ("single", "batch"):
        errs.append("protocol")
    if protocol == "batch":
        if workers != 1 or wait <= 0 or "--phase-frac" in a or ("--calibrate" in a and calibrate > 0):
            errs.append("batch rules")
        calibrate = 0
    if posts < 1 or warmup < 0 or calibrate < 0:
        errs.append("counts")
    if "--phase-frac" in a and workers > 1:
        errs.append("phase frac needs W = 1")
    if variant == "no_retrigger" and workers > 1 and warmup < workers:
        errs.append("no_retrigger warmup")
    delaying = grid.delays_single_posts(variant, workers)
    if wait == 0 and delaying:
        errs.append("blocking wait with a delaying variant")
    if "--phase-frac" in a and (not delaying or calibrate < 1):
        errs.append("phase frac needs a delaying variant and calibration")
    return errs


def abpair_errors(args, system):
    a, err = _pairs(args, ABPAIR_FLAGS)
    if err:
        return [err]
    errs = []
    if "--out" in a:
        errs.append("--out belongs to the runner")
    if a.get("--backend") not in grid.BACKENDS[system]:
        errs.append("backend")
    for flag in ("--workers", "--producers", "--inflight", "--pairs", "--window-ms", "--window-tasks"):
        if flag in a and not (a[flag].isdigit() and int(a[flag]) >= 1):
            errs.append("positive " + flag)
    workers = int(a.get("--workers", "1"))
    producers = int(a.get("--producers", "1"))
    if ("--window-ms" in a) == ("--window-tasks" in a):
        errs.append("exactly one window mode")
    if a.get("--order", "abba") not in ("abba", "shuffle"):
        errs.append("order")
    work = a.get("--work-ns", "0")
    if not (work.isdigit() and 0 <= int(work) <= 1000000000):
        errs.append("work")
    for flag, count in (("--worker-cpus", workers), ("--producer-cpus", producers)):
        if flag in a:
            cpus = _cpus(a[flag])
            if cpus is None or len(cpus) != count:
                errs.append(flag)
    for flag in ("--arm-a", "--arm-b"):
        arm = a.get(flag, "none" if flag == "--arm-a" else "absent_wake")
        if arm not in grid.VARIANTS or not grid.has_site(a.get("--backend"), arm) or workers < grid.min_workers(arm):
            errs.append(flag)
    return errs


class GridCounts(unittest.TestCase):
    def test_cells_per_system(self):
        self.assertEqual(len(grid.s_cells("linux")), 150)
        self.assertEqual(len(grid.s_cells("windows")), 92)
        self.assertEqual(len(grid.bt_cells("linux")), 46)
        self.assertEqual(len(grid.bt_cells("windows")), 28)

    def test_cells_per_bound_and_backend(self):
        for system, per_backend in (("linux", {"epoll": 26, "io_uring": 26, "condvar": 23}),
                                    ("windows", {"iocp": 23, "condvar": 23})):
            for bound_us in grid.BOUNDS_US:
                for backend, want in per_backend.items():
                    got = [c for c in grid.s_cells(system) if c.backend == backend and c.bound_us == bound_us]
                    self.assertEqual(len(got), want, (system, backend))
        bt = {"linux": {"epoll": 8, "io_uring": 8, "condvar": 7}, "windows": {"iocp": 7, "condvar": 7}}
        for system, per_backend in bt.items():
            for backend, want in per_backend.items():
                got = [c for c in grid.bt_cells(system) if c.backend == backend and c.bound_us == 1000]
                self.assertEqual(len(got), want)

    def test_hours_delaying(self):
        # Section 9: delaying S cells per bound 39 (Arch) and 22 (Windows); Bt 11 and 6.
        for system, s_want, bt_want in (("linux", 39, 11), ("windows", 22, 6)):
            s = [c for c in grid.s_cells(system) if c.bound_us == 1000]
            bt = [c for c in grid.bt_cells(system) if c.bound_us == 1000]
            self.assertEqual(sum(c.hours_delaying for c in s), s_want)
            self.assertEqual(len(s) - s_want, {"linux": 36, "windows": 24}[system])
            self.assertEqual(sum(c.hours_delaying for c in bt), bt_want)

    def test_families(self):
        for family, want in grid.FAMILY_M.items():
            if family in ("F1", "F2"):
                self.assertEqual(len(grid.family_cells(family)), want)
        self.assertEqual(len(grid.f4_cells()), grid.FAMILY_M["F4"])

        def count(system, protocol, family):
            return sum(1 for c in grid.all_cells(system) if c.protocol == protocol and c.family == family)

        self.assertEqual((count("linux", "S", "F1"), count("windows", "S", "F1"),
                          count("linux", "Bt", "F1"), count("windows", "Bt", "F1")), (60, 32, 6, 4))
        self.assertEqual((count("linux", "S", "F2"), count("windows", "S", "F2"),
                          count("linux", "Bt", "F2"), count("windows", "Bt", "F2")), (36, 24, 12, 8))
        self.assertEqual(len(set(grid.family_cells("F1"))), 102)

    def test_replication_cells(self):
        def keys(label, family):
            return sorted(c.key for s in grid.SYSTEMS for c in grid.all_cells(s)
                          if c.replication == label and c.family == family)

        b = ("1ms", "10ms")
        r_f1 = sorted("linux:S-%s-W1-B%s" % (x, y) for x in ("epoll-absent_wake", "io_uring-unobserved_primitive")
                      for y in b)
        r_f2 = sorted("linux:S-%s-plain_wake-W1-B%s" % (x, y) for x in ("epoll", "io_uring") for y in b)
        rd_f1 = sorted(["linux:S-io_uring-absent_wake-W1-B%s" % y for y in b]
                       + ["linux:S-condvar-absent_wake-W1-B%s" % y for y in b]
                       + ["windows:S-iocp-absent_wake-W1-B%s" % y for y in b]
                       + ["windows:S-condvar-absent_wake-W1-B%s" % y for y in b])
        rd_f2 = sorted(["linux:S-condvar-plain_wake-W1-B%s" % y for y in b]
                       + ["windows:S-iocp-plain_wake-W1-B%s" % y for y in b]
                       + ["windows:S-condvar-plain_wake-W1-B%s" % y for y in b]
                       + ["%s:Bt-%s-none-W1-B%s" % (s, k, y) for s in grid.SYSTEMS for k in grid.BACKENDS[s]
                          for y in b])
        self.assertEqual(keys("R", "F1"), r_f1)
        self.assertEqual(keys("R", "F2"), r_f2)
        self.assertEqual(keys("Rd", "F1"), rd_f1)
        self.assertEqual(keys("Rd", "F2"), rd_f2)
        self.assertEqual(len(r_f1) + len(rd_f1), 12)
        self.assertEqual(len(r_f2) + len(rd_f2), 20)
        no_label_outside = [c for s in grid.SYSTEMS for c in grid.all_cells(s) if c.replication and not c.family]
        self.assertEqual(no_label_outside, [])

    def test_blind_spot_cells(self):
        s = [c for c in grid.all_cells("linux") if c.blind_spot == "S"]
        bt = [c for c in grid.all_cells("linux") if c.blind_spot == "Bt"]
        self.assertEqual(len(s), 3 * 3 * 3 * 2)  # 3 variants x 3 W x 3 backends x 2 B
        self.assertEqual(len(bt), (5 + 5 + 4) * 2)
        self.assertFalse(any(c.family for c in s + bt))

    def test_sweep_targets(self):
        fracs = grid.sweep_fracs()
        self.assertEqual(len(fracs), 30)
        self.assertEqual(fracs[0], "0.05")
        self.assertEqual(fracs[-1], "2.95")
        self.assertEqual(len(set(fracs)), 30)
        self.assertEqual([float(f) for f in fracs], [round(0.05 + 0.1 * k, 2) for k in range(30)])


class CampaignPlan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entries = {s: plan.campaign_entries(s, stand_in_r(s)) for s in grid.SYSTEMS}
        cls.pilots = {s: plan.pilot_plan_entries(s) for s in grid.SYSTEMS}

    def test_counts_at_r_14(self):
        want = {"linux": ({"S": 1500, "Bt": 460, "sweep": 180, "H4": 168}, 2308, 672, 6888),
                "windows": ({"S": 920, "Bt": 280, "sweep": 120, "H4": 112}, 1432, 448, 4592)}
        for system, (families, total, windows, seconds) in want.items():
            c = plan.counts(self.entries[system])
            self.assertEqual(c["by_family"], families)
            self.assertEqual(c["processes"], total)
            self.assertEqual(c["abpair_windows"], windows)
            self.assertEqual(c["abpair_seconds"], seconds)  # section 9: 6,888 s and 4,592 s

    def test_cap_r_24(self):
        for system, seconds in (("linux", 11808), ("windows", 7872)):
            entries = plan.campaign_entries(system, stand_in_r(system, 24))
            self.assertEqual(plan.counts(entries)["abpair_seconds"], seconds)  # section 9's cap

    def test_pilot_counts(self):
        for system, procs, windows, seconds in (("linux", 48, 192, 1968), ("windows", 32, 128, 1312)):
            c = plan.counts(self.pilots[system])
            self.assertEqual((c["processes"], c["abpair_windows"], c["abpair_seconds"]), (procs, windows, seconds))
            self.assertEqual(c["by_family"], {"pilot": procs})
            for e in self.pilots[system]:
                a = args_map(e["args"])
                self.assertEqual((a["--arm-a"], a["--arm-b"], a["--pairs"]), ("none", "none", "2"))
            per_cell = {}
            for e in self.pilots[system]:
                per_cell[e["cell"]] = per_cell.get(e["cell"], 0) + 1
            self.assertEqual(set(per_cell.values()), {4})
            self.assertEqual(len(per_cell), len(grid.BACKENDS[system]) * 4)

    def test_limit_s_of_every_line_is_the_contract_s(self):
        # C1, worked out by hand. wakeprobe: units * (k * 10 + 1) + 120 with units = 1 probe + calibration +
        # warm-up + posts, k = 1 single or 2 batch. abpair: windows * (window_s + 10) + 120, windows = 2 pairs.
        # S, delaying: 1 + 11 + 5 + 630 = 647 units, 647 * 11 + 120. S, fixed or not delaying: 636 units.
        # Bt: 636 units, 636 * 21 + 120. Sweep: 1 + 11 + 3 + 21 = 36 units. H4 and pilot: 4 windows of 10 s.
        want = {"S delaying": 7237, "S": 7116, "Bt": 13476, "sweep": 516, "H4": 200, "pilot": 200}
        seen = set()
        for system in grid.SYSTEMS:
            for e in self.entries[system] + self.pilots[system]:
                a = args_map(e["args"])
                if e["tool"] == "wakeprobe":
                    kind = e["family"] + (" delaying" if e["family"] == "S" and a["--calibrate"] == "11" else "")
                else:
                    kind = e["family"]
                self.assertEqual(e["limit_s"], want[kind], e["id"])
                seen.add(kind)
        self.assertEqual(seen, set(want))

    def test_limit_s_follows_the_arguments(self):
        # The formula, not the values of the frozen grid.
        self.assertEqual(plan.wakeprobe_limit_s(["--protocol", "single", "--calibrate", "0", "--warmup", "0", "--posts", "1"]),
                         2 * 11 + 120)
        self.assertEqual(plan.wakeprobe_limit_s(["--protocol", "batch", "--calibrate", "0", "--warmup", "0", "--posts", "1"]),
                         2 * 21 + 120)
        self.assertEqual(plan.wakeprobe_limit_s(["--calibrate", "3", "--warmup", "4", "--posts", "5"]), 13 * 11 + 120)
        self.assertEqual(plan.abpair_limit_s(["--pairs", "1", "--window-ms", "1000"]), 2 * 11 + 120)
        self.assertEqual(plan.abpair_limit_s(["--pairs", "3", "--window-ms", "10000"]), 6 * 20 + 120)
        self.assertEqual(plan.abpair_limit_s(["--pairs", "2", "--window-ms", "1500"]), 4 * 12 + 120)  # 1.5 s rounds up

    def test_every_line_passes_the_tools_parsers(self):
        for system in grid.SYSTEMS:
            for e in self.entries[system] + self.pilots[system]:
                check = wakeprobe_errors if e["tool"] == "wakeprobe" else abpair_errors
                self.assertEqual(check(e["args"], system), [], e["id"])
        # The model itself rejects what the tools reject.
        self.assertTrue(wakeprobe_errors(["--backend", "epoll", "--wait-us", "1000", "--gap-us", "1",
                                          "--timer-period-ms", "1"], "linux"))
        self.assertTrue(wakeprobe_errors(["--backend", "iocp", "--wait-us", "1000", "--protocol", "batch",
                                          "--gap-us", "1", "--calibrate", "11"], "windows"))
        self.assertTrue(abpair_errors(["--backend", "epoll", "--window-ms", "10", "--label", "x"], "linux"))

    def test_every_s_and_bt_cell_has_ten_seeded_processes(self):
        for system in grid.SYSTEMS:
            seeds = {}
            for e in self.entries[system]:
                if e["family"] in ("S", "Bt"):
                    a = args_map(e["args"])
                    seeds.setdefault(e["cell"], []).append(int(a["--gap-seed"]))
                    i = readers.process_index(e["id"])
                    self.assertEqual(int(a["--gap-seed"]), grid.GAP_SEED_BASE + i)
            self.assertEqual(set(seeds), {c.name for c in grid.all_cells(system)})
            for cell, got in seeds.items():
                self.assertEqual(sorted(got), list(range(2026102700, 2026102710)), cell)

    def test_s_and_bt_arguments(self):
        cells = {c.name: c for s in grid.SYSTEMS for c in grid.all_cells(s)}
        for system in grid.SYSTEMS:
            for e in self.entries[system]:
                if e["family"] not in ("S", "Bt"):
                    continue
                a = args_map(e["args"])
                cell = cells[e["cell"]]
                self.assertEqual(a["--protocol"], "single" if cell.protocol == "S" else "batch")
                self.assertEqual((a["--posts"], a["--warmup"]), ("630", "5"))
                self.assertEqual(a["--gap-frac-range"], "0.1:3.0")
                self.assertEqual(a["--gap-unit"], "bound")
                self.assertEqual(a["--wait-us"], str(cell.bound_us))
                self.assertEqual(a["--workers"], str(cell.workers))
                self.assertEqual(a["--worker-cpus"], {1: "14", 2: "12,14", 4: "8,10,12,14"}[cell.workers])
                self.assertEqual(a["--poster-cpu"], "2")
                delaying = cell.protocol == "S" and cell.variant in (
                    "absent_wake", "unobserved_primitive", "no_retrigger", "wrong_worker")
                self.assertEqual(a["--calibrate"], "11" if delaying else "0", e["id"])
                self.assertEqual(a["--label"], e["id"])
                self.assertNotIn("--timerslack-ns", a)
                self.assertNotIn("--timer-period-ms", a)

    def test_sweep_arguments(self):
        for system in grid.SYSTEMS:
            sweeps = [e for e in self.entries[system] if e["family"] == "sweep"]
            self.assertEqual(len(sweeps), 30 * len(grid.sweep_cells(system)))
            by_cell = {}
            for e in sweeps:
                a = args_map(e["args"])
                self.assertEqual((a["--variant"], a["--workers"], a["--posts"], a["--warmup"], a["--calibrate"]),
                                 ("absent_wake", "1", "21", "3", "11"))
                self.assertEqual((a["--worker-cpus"], a["--poster-cpu"]), ("14", "2"))
                self.assertNotIn("--gap-frac-range", a)
                self.assertNotIn("--gap-seed", a)
                by_cell.setdefault(e["cell"], set()).add(a["--phase-frac"])
            for fracs in by_cell.values():
                self.assertEqual(fracs, set(grid.sweep_fracs()))

    def test_h4_arguments(self):
        for system in grid.SYSTEMS:
            per_cell = {}
            for e in self.entries[system]:
                if e["family"] != "H4":
                    continue
                a = args_map(e["args"])
                self.assertEqual(a["--arm-b"], "absent_wake")
                self.assertIn(a["--arm-a"], ("none", "plain_wake"))
                self.assertEqual((a["--workers"], a["--producers"], a["--inflight"], a["--pairs"], a["--order"],
                                  a["--window-ms"]), ("1", "1", "64", "2", "abba", "10000"))
                self.assertIn(a["--work-ns"], ("0", "1000", "10000", "100000"))
                self.assertEqual((a["--worker-cpus"], a["--producer-cpus"]), ("14", "2"))
                key = (a["--backend"], a["--work-ns"], a["--arm-a"])
                per_cell[key] = per_cell.get(key, 0) + 1
            self.assertEqual(len(per_cell), len(grid.BACKENDS[system]) * 4 * 2)
            self.assertEqual(set(per_cell.values()), {STAND_IN_R // 2})

    def test_uneven_r(self):
        r = stand_in_r("linux")
        r[("epoll", 1)] = 24
        r[("condvar", 100)] = 18
        entries = plan.campaign_entries("linux", r)
        n = {}
        for e in entries:
            if e["family"] == "H4":
                n[e["cell"]] = n.get(e["cell"], 0) + 1
        self.assertEqual(n["H4-epoll-N1us-none"], 12)
        self.assertEqual(n["H4-epoll-N1us-plain_wake"], 12)
        self.assertEqual(n["H4-condvar-N100us-none"], 9)
        self.assertEqual(n["H4-io_uring-N10us-none"], 7)

    def test_ids(self):
        for system in grid.SYSTEMS:
            for entries in (self.entries[system], self.pilots[system]):
                ids = [e["id"] for e in entries]
                self.assertEqual(len(ids), len(set(ids)))
                for e in entries:
                    self.assertRegex(e["id"], r"^[A-Za-z0-9_.-]+$")
                    self.assertTrue(e["id"].startswith(e["cell"] + "-"), e["id"])
                    self.assertEqual(list(e), ["id", "tool", "args", "cell", "family", "limit_s"])  # C1: no other key
                    self.assertIs(type(e["limit_s"]), int)
                    self.assertGreater(e["limit_s"], 0)
                    self.assertIn(e["family"], ("S", "Bt", "sweep", "H4", "pilot"))
                    self.assertNotIn("--out", e["args"])

    def test_one_shuffled_order(self):
        for system in grid.SYSTEMS:
            canonical = plan.s_bt_entries(system) + plan.sweep_entries(system) + plan.h4_entries(
                system, stand_in_r(system))
            got = self.entries[system]
            self.assertEqual(sorted(e["id"] for e in got), sorted(e["id"] for e in canonical))
            self.assertNotEqual([e["id"] for e in got], [e["id"] for e in canonical])
            families = [e["family"] for e in got[:200]]
            self.assertGreater(len(set(families)), 2)  # interleaved, not in blocks

    def test_golden_order(self):
        # The same ids in the same order on every platform and Python version (stand-in R = 14).
        def digest(entries):
            return hashlib.sha256("\n".join(e["id"] for e in entries).encode("ascii")).hexdigest()

        self.assertEqual(digest(self.entries["linux"]), GOLDEN["linux"])
        self.assertEqual(digest(self.entries["windows"]), GOLDEN["windows"])
        self.assertEqual(digest(self.pilots["linux"]), GOLDEN["pilot-linux"])
        self.assertEqual(digest(self.pilots["windows"]), GOLDEN["pilot-windows"])
        self.assertEqual(self.entries["linux"][0]["id"], GOLDEN["linux-first"])


GOLDEN = {
    "linux": "7d7225850aea7051f6ccbd1fb1fc0e8865b7dc2779c48a0ea16d4773a9b584dc",
    "windows": "617f12926e320ee52dd5e68ec19213d3d61364dae12c910a31efeaa765d3afd2",
    "pilot-linux": "f453f138c83943f01592efbd940998b1601ddc30fcb8450be6a876c2fa979fd4",
    "pilot-windows": "8bfbac3dfe3251f4efc51ad6a722d07b1c1023f0790f180497bae18d46d08e29",
    "linux-first": "S-io_uring-no_retrigger-W1-B1ms-p4",
}


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p1v2-plan-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, argv, **kwargs):
        with redirect_stdout(io.StringIO()):
            return plan.main(argv, **kwargs)

    def usage_error(self, argv):
        """The text argparse prints when it refuses argv (exit status 2); nothing may be written."""
        with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()) as err, \
                redirect_stdout(io.StringIO()):
            plan.main(argv)
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(os.listdir(self.tmp), [])
        return err.getvalue()

    def test_cli_writes_four_plans(self):
        self.run_cli(["pilot", "--out-dir", self.tmp, "--draft"])
        self.run_cli(["campaign", "--out-dir", self.tmp, "--stand-in-r", "14"])
        # C2: a stand-in or draft plan never has the name of a real plan.
        self.assertEqual(sorted(os.listdir(self.tmp)), ["pilot-linux.draft.jsonl", "pilot-windows.draft.jsonl",
                                                        "plan-linux.stand-in.jsonl", "plan-windows.stand-in.jsonl"])
        for name, system, n, kind in (("plan-linux.stand-in.jsonl", "linux", 2308, "campaign"),
                                      ("plan-windows.stand-in.jsonl", "windows", 1432, "campaign"),
                                      ("pilot-linux.draft.jsonl", "linux", 48, "pilot"),
                                      ("pilot-windows.draft.jsonl", "windows", 32, "pilot")):
            header, entries = readers.load_plan(os.path.join(self.tmp, name))
            self.assertEqual(list(header), ["plan", "system", "seed", "generated_by"])
            self.assertEqual((header["plan"], header["system"], header["seed"]), (1, system, 20261026))
            self.assertEqual(len(entries), n)
            self.assertTrue(header["generated_by"].startswith("analysis/v2/plan.py "))
            if kind == "pilot":
                self.assertTrue(header["generated_by"].endswith(" draft"), header["generated_by"])
                self.assertEqual(entries, plan.pilot_plan_entries(system))
            else:
                self.assertTrue(header["generated_by"].endswith(" stand-in-R"))
                want = plan.campaign_entries(system, stand_in_r(system))
                self.assertEqual(entries, want)

    def test_one_system(self):
        self.run_cli(["pilot", "--out-dir", self.tmp, "--draft", "--system", "windows"])
        self.assertEqual(sorted(os.listdir(self.tmp)), ["pilot-windows.draft.jsonl"])

    # --- --stand-in-r ----------------------------------------------------------------------------

    def test_stand_in_takes_neither_a_log_nor_a_clean_tree(self):
        nowhere = os.path.join(self.tmp, "no-such-repository")
        self.run_cli(["campaign", "--out-dir", os.path.join(self.tmp, "out"), "--stand-in-r", "24",
                      "--system", "linux"], repo=nowhere)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "out")), ["plan-linux.stand-in.jsonl"])
        _, entries = readers.load_plan(os.path.join(self.tmp, "out", "plan-linux.stand-in.jsonl"))
        self.assertEqual(plan.counts(entries)["abpair_seconds"], 11808)  # section 9's cap, R = 24 everywhere

    def test_stand_in_r_must_be_even_and_from_14_to_24(self):
        for bad in ("13", "15", "12", "26", "0", "-14", "x", "14.0", ""):
            text = self.usage_error(["campaign", "--out-dir", self.tmp, "--stand-in-r", bad])
            self.assertIn("--stand-in-r", text, bad)
        for good in ("14", "18", "24"):
            self.run_cli(["campaign", "--out-dir", self.tmp, "--stand-in-r", good, "--system", "windows"])

    def test_stand_in_excludes_the_revision_log(self):
        text = self.usage_error(["campaign", "--out-dir", self.tmp, "--stand-in-r", "14", "--revision-log", "x.md"])
        self.assertIn("not allowed with", text)

    def test_the_pilot_plan_takes_no_commit(self):
        text = self.usage_error(["pilot", "--out-dir", self.tmp, "--commit", "abc123"])
        self.assertIn("unrecognized arguments", text)
        text = self.usage_error(["campaign", "--out-dir", self.tmp, "--stand-in-r", "14", "--draft"])
        self.assertIn("unrecognized arguments", text)  # --draft is the pilot's; a campaign dry run is --stand-in-r

    def test_the_old_ways_to_feed_r_are_gone(self):
        for extra in (["--h4-r", "f.json"], ["--allow-stand-in"], ["--commit", "abc123"]):
            for source in (["--stand-in-r", "14"], []):
                text = self.usage_error(["campaign", "--out-dir", self.tmp] + source + extra)
                self.assertIn("unrecognized arguments", text, (extra, source))


# ---------------------------------------------------------------------------------------------
# A real plan: R from the revision log, the commit and the log's hash from git
# ---------------------------------------------------------------------------------------------


LOG_PREFIX = ("# Revision log, P1 v2\n\n## 2026-11-01 R from the A/A pilot\n\nThe block as pilot.py wrote it "
              "(this line mentions <!-- h4-r:begin --> in prose only):\n\n")
CELLS = synth.pilot_cells(14, {"windows-iocp-N1us": {"r": 18}, "linux-epoll-N10us": {"r": 24, "underpowered": True},
                               "linux-condvar-N0us": {"r": 20}})

# Contract C2, written out as the runner reads it (not read from plan.py).
C2_PILOT = r"^analysis/v2/plan\.py [0-9a-f]{40}$"
C2_CAMPAIGN = r"^analysis/v2/plan\.py [0-9a-f]{40} revision-log-v2\.md sha256:[0-9a-f]{64}$"


def log_text(cells=CELLS):
    return LOG_PREFIX + h4log.render_block(cells) + "\nEnd of the entry.\n"


class RealPlan(unittest.TestCase):
    def setUp(self):
        self.t = TempGit(self)
        self.repo = self.t.repo
        self.out = os.path.join(self.t.root, "out")  # outside the repository
        self.text = log_text()
        self.log = self.t.write(self.repo, plan.REVISION_LOG, self.text)
        self.t.write(self.repo, "other.txt", "x\n")
        self.t.commit_all(self.repo)

    def make(self, *extra, repo=None):
        with redirect_stdout(io.StringIO()):
            return plan.main(["campaign", "--out-dir", self.out] + list(extra), repo=repo or self.repo)

    def refused(self, *extra, repo=None, what="campaign"):
        """The refusal message; nothing may have been written."""
        with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()):
            plan.main([what, "--out-dir", self.out] + list(extra), repo=repo or self.repo)
        self.assertFalse(os.path.exists(self.out), "a refused plan wrote something")
        message = str(cm.exception.code)
        self.assertTrue(message.startswith("plan.py: refused: "), message)
        return message

    def r_of(self, key):
        return CELLS[key]["R"]

    def test_the_repository_root_is_found_from_the_script(self):
        self.assertTrue(os.path.isfile(os.path.join(plan.REPO_ROOT, "analysis", "v2", "plan.py")))
        self.assertEqual(plan.REVISION_LOG, "revision-log-v2.md")

    def test_a_clean_tree_writes_both_plans(self):
        self.assertEqual(self.make(), 0)  # the log is found at the root of the repository
        self.assertEqual(sorted(os.listdir(self.out)), ["plan-linux.jsonl", "plan-windows.jsonl"])  # the real names
        digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        head = self.t.head(self.repo)
        self.assertRegex(head, r"^[0-9a-f]{40}$")
        for system in grid.SYSTEMS:
            header, entries = readers.load_plan(os.path.join(self.out, "plan-%s.jsonl" % system))
            self.assertEqual(list(header), ["plan", "system", "seed", "generated_by"])
            self.assertEqual(header["system"], system)
            self.assertRegex(header["generated_by"], C2_CAMPAIGN)
            self.assertEqual(header["generated_by"],
                             "analysis/v2/plan.py %s revision-log-v2.md sha256:%s" % (head, digest))
            self.assertEqual(entries, plan.campaign_entries(system, plan.r_for_system(system, self.r_of)))
        # R came from the log: 24 and 16 where it says so, 18 on Windows, 14 elsewhere.
        _, linux = readers.load_plan(os.path.join(self.out, "plan-linux.jsonl"))
        _, windows = readers.load_plan(os.path.join(self.out, "plan-windows.jsonl"))

        def processes(entries, cell):
            return sum(1 for e in entries if e["cell"] == cell)

        self.assertEqual(processes(linux, "H4-epoll-N10us-none"), 12)
        self.assertEqual(processes(linux, "H4-epoll-N10us-plain_wake"), 12)
        self.assertEqual(processes(linux, "H4-condvar-N0us-none"), 10)
        self.assertEqual(processes(linux, "H4-epoll-N1us-none"), 7)
        self.assertEqual(processes(windows, "H4-iocp-N1us-none"), 9)
        self.assertEqual(processes(windows, "H4-iocp-N10us-none"), 7)

    def test_the_log_can_be_elsewhere_but_not_renamed(self):
        os.mkdir(os.path.join(self.repo, "copy"))
        elsewhere = self.t.write(self.repo, "copy/" + plan.REVISION_LOG, log_text(synth.pilot_cells(20)))
        renamed = self.t.write(self.repo, "pilot-log.md", log_text(synth.pilot_cells(20)))
        self.t.commit_all(self.repo)
        self.assertEqual(self.make("--revision-log", elsewhere, "--system", "windows"), 0)
        header, entries = readers.load_plan(os.path.join(self.out, "plan-windows.jsonl"))
        self.assertRegex(header["generated_by"], C2_CAMPAIGN)
        self.assertEqual(sum(1 for e in entries if e["cell"] == "H4-iocp-N1us-none"), 10)
        self.assertEqual(os.listdir(self.out), ["plan-windows.jsonl"])
        shutil.rmtree(self.out)
        # C2's header names revision-log-v2.md: a plan from a log of another name would fail the runner.
        self.assertIn("named revision-log-v2.md", self.refused("--revision-log", renamed, "--system", "windows"))

    def test_a_file_that_git_ignores_does_not_make_the_tree_dirty(self):
        self.t.write(self.repo, ".gitignore", "scratch.txt\n")
        self.t.commit_all(self.repo)
        self.t.write(self.repo, "scratch.txt", "x\n")
        self.assertEqual(self.make(), 0)

    # --- the working tree --------------------------------------------------------------------------

    def test_an_untracked_file_is_refused(self):
        self.t.write(self.repo, "new.txt", "x\n")
        message = self.refused()
        self.assertIn("not clean", message)
        self.assertIn("new.txt", message)

    def test_a_modified_file_is_refused(self):
        self.t.write(self.repo, "other.txt", "changed\n")
        self.assertIn("not clean", self.refused())

    def test_a_deleted_file_is_refused(self):
        os.remove(os.path.join(self.repo, "other.txt"))
        self.assertIn("not clean", self.refused())

    def test_a_staged_file_is_refused(self):
        self.t.write(self.repo, "staged.txt", "x\n")
        self.t.git(self.repo, "add", "staged.txt")
        self.assertIn("not clean", self.refused())

    def test_a_log_that_is_not_committed_is_refused(self):
        self.t.write(self.repo, plan.REVISION_LOG, log_text(synth.pilot_cells(20)))
        message = self.refused()
        self.assertIn("not clean", message)
        self.assertIn(plan.REVISION_LOG, message)

    def test_no_commit_is_refused(self):
        fresh = self.t.init("fresh")
        self.t.write(fresh, plan.REVISION_LOG, self.text)
        self.assertIn("rev-parse HEAD", self.refused(repo=fresh))

    def test_a_folder_that_is_not_a_repository_is_refused(self):
        plain = os.path.join(self.t.root, "plain")
        os.mkdir(plain)
        self.t.write(plain, plan.REVISION_LOG, self.text)
        self.assertIn("git rev-parse HEAD failed", self.refused(repo=plain))

    def test_a_failed_git_call_never_reads_as_clean(self):
        real = subprocess.run

        def fail_status(cmd, *args, **kwargs):
            if "status" in cmd:
                return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: boom")
            return real(cmd, *args, **kwargs)

        with mock.patch.object(plan.subprocess, "run", side_effect=fail_status):
            with self.assertRaisesRegex(plan.Refusal, "fatal: boom"):
                plan.clean_commit(self.repo)
        with mock.patch.object(plan.subprocess, "run", side_effect=FileNotFoundError("git")):
            with self.assertRaisesRegex(plan.Refusal, "cannot run git"):
                plan.clean_commit(self.repo)
        self.assertRegex(plan.clean_commit(self.repo), r"^[0-9a-f]{40}$")

    # --- the log -----------------------------------------------------------------------------------

    def test_a_log_outside_the_repository_is_refused(self):
        folder = os.path.join(self.t.root, "elsewhere")
        os.mkdir(folder)
        outside = self.t.write(folder, plan.REVISION_LOG, self.text)
        self.assertIn("outside the repository", self.refused("--revision-log", outside))

    def test_a_log_that_git_ignores_is_refused(self):
        self.t.write(self.repo, ".gitignore", "ignored/\n")
        self.t.commit_all(self.repo)
        os.mkdir(os.path.join(self.repo, "ignored"))
        ignored = self.t.write(self.repo, "ignored/" + plan.REVISION_LOG, self.text)
        self.assertIn("not tracked", self.refused("--revision-log", ignored))

    def test_a_missing_log_is_refused(self):
        self.assertIn("cannot read", self.refused("--revision-log", os.path.join(self.repo, "absent", plan.REVISION_LOG)))

    def test_a_log_without_a_usable_block_is_refused(self):
        def refused_with(text, pattern):
            self.t.write(self.repo, plan.REVISION_LOG, text)
            self.assertRegex(self.refused(), pattern)

        refused_with("# Revision log\n\nNone yet.\n", "no h4-r block")
        refused_with(self.text + self.text, "more than one h4-r block")
        refused_with(self.text.replace("| 24 | yes |", "| 15 | yes |"), "even integer from 14 to 24")
        refused_with("\n".join(line for line in self.text.split("\n") if not line.startswith("| windows-iocp-N0us |")),
                     "H4 cells are missing")
        refused_with(self.text.replace("<!-- h4-r:end -->", ""), "unbalanced")

    def test_one_system_still_needs_the_whole_block(self):
        short = "\n".join(line for line in self.text.split("\n") if not line.startswith("| linux-epoll-N1us |"))
        self.t.write(self.repo, plan.REVISION_LOG, short)
        self.t.commit_all(self.repo)
        message = self.refused("--system", "windows")
        self.assertIn("linux-epoll-N1us", message)

    # --- the pilot plans: the same provenance rule ------------------------------------------------

    def make_pilot(self, *extra):
        with redirect_stdout(io.StringIO()):
            return plan.main(["pilot", "--out-dir", self.out] + list(extra), repo=self.repo)

    def test_a_clean_tree_writes_both_pilot_plans(self):
        self.assertEqual(self.make_pilot(), 0)  # no revision log is read: the pilot comes before R
        self.assertEqual(sorted(os.listdir(self.out)), ["pilot-linux.jsonl", "pilot-windows.jsonl"])  # the real names
        head = self.t.head(self.repo)
        for system in grid.SYSTEMS:
            header, entries = readers.load_plan(os.path.join(self.out, "pilot-%s.jsonl" % system))
            self.assertEqual(list(header), ["plan", "system", "seed", "generated_by"])
            self.assertRegex(header["generated_by"], C2_PILOT)
            self.assertEqual(header["generated_by"], "analysis/v2/plan.py " + head)
            self.assertEqual(entries, plan.pilot_plan_entries(system))

    def test_a_dirty_tree_refuses_the_pilot_plans(self):
        self.t.write(self.repo, "new.txt", "x\n")
        message = self.refused(what="pilot")
        self.assertIn("not clean", message)
        self.assertIn("new.txt", message)
        self.t.write(self.repo, "other.txt", "changed\n")
        self.assertIn("not clean", self.refused("--system", "windows", what="pilot"))

    def test_the_pilot_plans_refuse_a_failed_git_call_and_a_missing_commit(self):
        fresh = self.t.init("fresh")
        self.assertIn("rev-parse HEAD", self.refused(repo=fresh, what="pilot"))
        plain = os.path.join(self.t.root, "plain")
        os.mkdir(plain)
        self.assertIn("git rev-parse HEAD failed", self.refused(repo=plain, what="pilot"))

    def test_a_draft_pilot_plan_may_come_from_a_dirty_tree(self):
        self.t.write(self.repo, "new.txt", "x\n")
        self.assertEqual(self.make_pilot("--draft"), 0)
        self.assertEqual(sorted(os.listdir(self.out)), ["pilot-linux.draft.jsonl", "pilot-windows.draft.jsonl"])
        for system in grid.SYSTEMS:
            header, entries = readers.load_plan(os.path.join(self.out, "pilot-%s.draft.jsonl" % system))
            self.assertTrue(header["generated_by"].startswith("analysis/v2/plan.py "))
            self.assertTrue(header["generated_by"].endswith(" draft"), header["generated_by"])
            self.assertNotRegex(header["generated_by"], C2_PILOT)
            self.assertEqual(len(entries), {"linux": 48, "windows": 32}[system])

    def test_a_commit_is_a_forty_digit_name(self):
        # C2's header form has exactly forty hex digits; a repository of another object format cannot make a real plan.
        for bad in ("a" * 39, "a" * 41, "A" * 40, "a" * 64, ""):
            self.assertIsNone(plan.COMMIT.fullmatch(bad), bad)
        self.assertIsNotNone(plan.COMMIT.fullmatch("0123456789abcdef0123456789abcdef01234567"))

    def test_the_commit_cannot_be_overridden(self):
        for what in ("campaign", "pilot"):
            with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
                plan.main([what, "--out-dir", self.out, "--commit", "abc123"], repo=self.repo)
            self.assertEqual(cm.exception.code, 2)
            self.assertFalse(os.path.exists(self.out))


if __name__ == "__main__":
    unittest.main()

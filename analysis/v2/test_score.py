"""A synthetic campaign with known answers, scored end to end by score.py.

Linux and Windows get a few cells each, with chosen flag counts; every other cell of the frozen
grid has no data and must stay in its family's m as incomplete. Nothing here is measured. The
outputs are written as the campaign runner leaves them (synth.write_process: .jsonl, -rerun.jsonl,
.done, void runs), and these reduced plans are drafts (score.score(..., draft=True)); the
provenance rules of a real scoring are tested in StrictProvenance.
"""

import hashlib
import io
import json
import math
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction

import beff
import grid
import h4log
import plan
import readers
import score
import stats
import synth
from tempgit import TempGit

LINUX_BEFF_VALUES = [1000000 + 1000 * i for i in range(10)]  # median 1,004,500 ns
LINUX_BEFF = 1004500
WINDOWS_BEFF = 15625000  # the fallback: 7 of 10 calibrate, so max(B, coarsest timer) = 15.625 ms
WINDOWS_TIMER_US = synth.WINDOWS_TIMER_US


def stand_in_r(system):
    return {cell: 14 for cell in grid.h4_cells(system)}


def write_log(folder, name="revision-log-v2.md", text=None, overrides=None):
    """A revision log whose block declares linux-epoll-N10us underpowered (R = 24); returns its path."""
    if text is None:
        cells = synth.pilot_cells(14, overrides or {"linux-epoll-N10us": {"r": 24, "underpowered": True}})
        text = "# Revision log\n\nThe pilot's block:\n\n" + h4log.render_block(cells) + "\nEnd.\n"
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


class Campaign:
    """Writes a reduced plan and its synthetic outputs for one system."""

    def __init__(self, root, system):
        self.system = system
        self.dir = os.path.join(root, system)
        os.makedirs(self.dir)
        self.by_cell = {}
        for e in plan.campaign_entries(system, stand_in_r(system)):
            self.by_cell.setdefault(e["cell"], []).append(e)
        for entries in self.by_cell.values():
            entries.sort(key=lambda e: int(e["id"].rsplit("-", 1)[1][1:]))
        self.used = []

    def entries(self, cell):
        self.used.extend(self.by_cell[cell])
        return self.by_cell[cell]

    def write(self, entry, first, rerun=None, **kw):
        synth.write_process(self.dir, entry, first=first, rerun=rerun, **kw)

    def lines(self, entry, delays, b_eff=None, coarsest=None, **kw):
        return synth.wakeprobe_output(entry, delays, b_eff, coarsest, system=self.system, **kw)

    def cell(self, cell, b_eff, flags, calibrated=None, coarsest=None, t_count_only=None):
        """Ten processes; process i flags flags[i] trials of T_med."""
        for i, e in enumerate(self.entries(cell)):
            delays = synth.process_delays(b_eff, flags[i], (t_count_only or [0] * 10)[i])
            cal = calibrated[i] if calibrated else None
            self.write(e, self.lines(e, delays, cal, coarsest))

    def write_plan(self):
        path = os.path.join(self.dir, "plan.jsonl")
        plan.write_plan(path, plan.header(self.system, "test"), self.used)
        return path


def build_linux(root):
    c = Campaign(root, "linux")
    full = [30] * 10
    # The B_eff source cells of epoll and io_uring at B = 1 ms.
    c.cell("S-epoll-absent_wake-W1-B1ms", LINUX_BEFF, full, LINUX_BEFF_VALUES)
    c.cell("S-io_uring-absent_wake-W1-B1ms", LINUX_BEFF, full, LINUX_BEFF_VALUES)
    # Rerun replacement: process 3's first run loses a unit and flags nothing; its rerun is whole.
    e3 = c.by_cell["S-io_uring-absent_wake-W1-B1ms"][3]
    c.write(e3, c.lines(e3, synth.process_delays(LINUX_BEFF, 0), LINUX_BEFF_VALUES[3], lose_at=100),
            c.lines(e3, synth.process_delays(LINUX_BEFF, 30), LINUX_BEFF_VALUES[3]))
    # 4 misses in 300: p = 6.9e-4, which fails F1's Holm at its rank.
    c.cell("S-io_uring-unobserved_primitive-W1-B1ms", LINUX_BEFF, [30] * 6 + [29] * 4, LINUX_BEFF_VALUES)
    # Incomplete: process 5 loses a unit in its first run and in its rerun.
    c.cell("S-epoll-absent_wake-W2-B1ms", LINUX_BEFF, full, LINUX_BEFF_VALUES)
    e5 = c.by_cell["S-epoll-absent_wake-W2-B1ms"][5]
    c.write(e5, c.lines(e5, synth.process_delays(LINUX_BEFF, 30), LINUX_BEFF, lose_at=7),
            c.lines(e5, synth.process_delays(LINUX_BEFF, 30), LINUX_BEFF, lose_at=7))
    # Overdispersed: half the processes flag every trial, half none.
    c.cell("S-epoll-no_retrigger-W1-B1ms", LINUX_BEFF, [30] * 5 + [0] * 5, LINUX_BEFF_VALUES)
    # F2: 3 false positives pass; 20 fail. Process 9 of the fixed cell lost a unit once (a lost wake).
    c.cell("S-epoll-none-W1-B1ms", LINUX_BEFF, [1, 1, 1] + [0] * 7)
    e9 = c.by_cell["S-epoll-none-W1-B1ms"][9]
    c.write(e9, c.lines(e9, synth.process_delays(LINUX_BEFF, 5), lose_at=5),
            c.lines(e9, synth.process_delays(LINUX_BEFF, 0)))
    c.cell("S-epoll-plain_wake-W1-B1ms", LINUX_BEFF, [2] * 10)
    # T_count only: 2 trials in process 0, and one 6 ms delay (beyond 5 B_eff, timed out) in process 1.
    c.cell("S-epoll-none-W2-B1ms", LINUX_BEFF, [0] * 10, t_count_only=[2] + [0] * 9)
    e1 = c.by_cell["S-epoll-none-W2-B1ms"][1]
    delays = synth.process_delays(LINUX_BEFF, 0)
    delays[0] = 6000000
    c.write(e1, c.lines(e1, delays, timed_out_at={0}))
    # Section 8's count against wakeprobe's own flag, each way. Process 0: two units that wakeprobe
    # flagged at 2 ms, under 5 x B_eff. Process 1: three units at 6 ms, over it, that it did not flag.
    c.cell("S-epoll-plain_wake-W2-B1ms", LINUX_BEFF, [0] * 10)
    f0, f1 = c.by_cell["S-epoll-plain_wake-W2-B1ms"][:2]
    under = synth.process_delays(LINUX_BEFF, 0)
    under[0] = under[1] = 2000000
    c.write(f0, c.lines(f0, under, timed_out_at={0, 1}))
    over = synth.process_delays(LINUX_BEFF, 0)
    over[0] = over[1] = over[2] = 6000000
    c.write(f1, c.lines(f1, over))
    # Blind spots: the lazy-wake race stays blind; undrained edge under S flags 10%.
    c.cell("S-epoll-lazy_wake_race-W1-B1ms", LINUX_BEFF, [0] * 10)
    c.cell("S-epoll-undrained_edge-W1-B1ms", LINUX_BEFF, [3] * 10)
    # Bt: undrained edge detected (F1), absent wake blind, the fixed loop clean (F2).
    c.cell("Bt-epoll-undrained_edge-W1-B1ms", LINUX_BEFF, full)
    c.cell("Bt-epoll-absent_wake-W1-B1ms", LINUX_BEFF, [0] * 10)
    c.cell("Bt-epoll-none-W1-B1ms", LINUX_BEFF, [0] * 10)

    # H3: epoll holds (median 0.01), io_uring fails (0.2); condvar has no data.
    for backend, frac in (("epoll", 0.01), ("io_uring", 0.2)):
        for k, e in enumerate(c.entries(grid.sweep_name(backend, 1000))):
            b = 1000000 + 100 * k
            res = [int(frac * b) * (1 if j % 2 else -1) for j in range(21)]
            c.write(e, c.lines(e, [b // 2] * 21, b, residuals=res))

    # H4.
    def h4(cell, ratios_per_process, failed=()):
        for j, e in enumerate(c.entries(cell)):
            if j in failed:
                c.write(e, synth.abpair_output(e, ratios_per_process, error="cannot pin worker 0"),
                        synth.abpair_output(e, ratios_per_process, drop_windows=1), exits=[3, 3])
            else:
                c.write(e, synth.abpair_output(e, ratios_per_process))

    h4("H4-epoll-N1us-none", [0.999, 1.001])
    h4("H4-epoll-N10us-none", [0.95, 0.95])
    h4("H4-epoll-N100us-none", [1.0, 1.0], failed={6})
    h4("H4-epoll-N0us-none", [0.90, 0.90])
    h4("H4-epoll-N1us-plain_wake", [1.0, 1.0])
    # io_uring N = 1: process 2 fails once, its rerun is whole.
    for j, e in enumerate(c.entries("H4-io_uring-N1us-none")):
        if j == 2:
            c.write(e, synth.abpair_output(e, [1.0, 1.0], drop_windows=3), synth.abpair_output(e, [1.0, 1.0]),
                    exits=[3, 0])
        else:
            c.write(e, synth.abpair_output(e, [1.0, 1.0]))
    return c.write_plan(), c.dir


def build_windows(root):
    c = Campaign(root, "windows")
    cal = [15600000 + 1000 * i for i in range(7)] + [None] * 3  # 7 of 10 calibrate: fallback
    c.cell("S-iocp-absent_wake-W1-B1ms", WINDOWS_BEFF, [30] * 10, cal, coarsest=WINDOWS_TIMER_US)
    # Delays of 1 ms flag nothing against 15.625 ms / 5, but would against 1 ms / 5.
    for i, e in enumerate(c.entries("S-iocp-none-W1-B1ms")):
        delays = [1000000] * 630
        if i == 0:
            # 60 ms is over 5 x B (5 ms) but under 5 x the 15.625 ms timer (78.125 ms); 80 ms is over both.
            delays[0], delays[1] = 60000000, 80000000
        c.write(e, c.lines(e, delays, coarsest=WINDOWS_TIMER_US))
    return c.write_plan(), c.dir


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="p1v2-score-")
        cls.linux = build_linux(cls.tmp)
        cls.windows = build_windows(cls.tmp)
        cls.log = write_log(cls.tmp)
        h4_r, cls.digest = h4log.read_log(cls.log)
        cls.source = {"file": "revision-log-v2.md", "sha256": cls.digest}
        cls.res = score.score([cls.linux, cls.windows], h4_r, h4_r_source=cls.source, draft=True)
        cls.cells = cls.res["cells"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def cell(self, key):
        return self.cells[key]

    def test_families_keep_every_cell_in_m(self):
        self.assertEqual(self.res["H1"]["m"], 102)
        self.assertEqual(self.res["H2"]["m"], 80)
        self.assertEqual(self.res["H4"]["F4"]["m"], 15)
        self.assertEqual(self.res["H4"]["F4c"]["m"], 15)
        self.assertFalse(self.res["H1"]["holds_on_every_cell"])

    def test_h1(self):
        rejected = {k for k, t in self.res["H1"]["tests"].items() if t["rejected"]}
        self.assertEqual(rejected, {"linux:S-epoll-absent_wake-W1-B1ms", "linux:S-io_uring-absent_wake-W1-B1ms",
                                    "linux:Bt-epoll-undrained_edge-W1-B1ms", "windows:S-iocp-absent_wake-W1-B1ms"})
        unobs = self.res["H1"]["tests"]["linux:S-io_uring-unobserved_primitive-W1-B1ms"]
        self.assertAlmostEqual(float(unobs["p"]), 6.9e-4, delta=0.05e-4)
        self.assertEqual(unobs["rank"], 5)
        self.assertFalse(unobs["rejected"])
        self.assertIn("linux:S-io_uring-unobserved_primitive-W1-B1ms", self.res["H1"]["failing_cells"])
        self.assertIn("linux:S-epoll-no_retrigger-W1-B1ms", self.res["H1"]["failing_cells"])
        self.assertEqual(len(self.res["H1"]["failing_cells"]), 2)
        self.assertEqual(len(self.res["H1"]["incomplete_cells"]), 102 - 6)

    def test_incomplete_cell_kept_in_m(self):
        key = "linux:S-epoll-absent_wake-W2-B1ms"
        c = self.cell(key)
        self.assertFalse(c["complete"])
        self.assertIsNone(c["p"])
        self.assertIn(key, self.res["H1"]["incomplete_cells"])
        self.assertFalse(self.res["H1"]["tests"][key]["rejected"])
        self.assertEqual(c["processes_good"], 9)
        self.assertEqual((c["lost_units_first"], c["lost_units_rerun"], c["lost_units_void"]), (1, 1, 0))

    def test_rerun_replaces_the_first_run(self):
        c = self.cell("linux:S-io_uring-absent_wake-W1-B1ms")
        self.assertTrue(c["complete"])
        self.assertEqual(c["t_med"]["flags"], 300)
        self.assertEqual(c["lost_units_first"], 1)
        p3 = c["processes"][3]
        self.assertEqual((p3["used"], p3["first_problem"], p3["rerun_problem"]), ("rerun", "exit 6: lost unit", None))
        self.assertEqual((p3["outcome"], p3["exits"]), ("ok after rerun", [6, 0]))

    def test_h2(self):
        tests = self.res["H2"]["tests"]
        three = tests["linux:S-epoll-none-W1-B1ms"]
        self.assertAlmostEqual(float(three["p"]), 1.64e-4, delta=0.005e-4)
        self.assertTrue(three["rejected"])
        self.assertFalse(tests["linux:S-epoll-plain_wake-W1-B1ms"]["rejected"])
        self.assertTrue(tests["linux:Bt-epoll-none-W1-B1ms"]["rejected"])
        self.assertTrue(tests["linux:S-epoll-none-W2-B1ms"]["rejected"])
        self.assertTrue(tests["windows:S-iocp-none-W1-B1ms"]["rejected"])

    def test_lost_wake_in_a_fixed_cell(self):
        c = self.cell("linux:S-epoll-none-W1-B1ms")
        self.assertTrue(c["complete"])
        self.assertEqual(c["t_med"]["flags"], 3)  # the rerun's data, not the first run's 5
        self.assertTrue(c["lost_wake"])
        self.assertEqual(c["lost_wake_runs"], [{"process": "S-epoll-none-W1-B1ms-p9", "run": "first", "lost_units": 1}])
        self.assertEqual(self.res["lost_wakes"], ["linux:S-epoll-none-W1-B1ms"])

    def test_b_eff(self):
        lin = {(e["backend"], e["bound_us"]): e for e in self.res["b_eff"]["linux"]}
        self.assertEqual(lin[("epoll", 1000)]["b_eff_ns"], LINUX_BEFF)
        self.assertEqual(lin[("epoll", 1000)]["source"], "measured: median of the calibrated b_eff_ns")
        self.assertEqual(lin[("condvar", 1000)]["source"], "fallback: B")  # no data
        win = {(e["backend"], e["bound_us"]): e for e in self.res["b_eff"]["windows"]}
        self.assertEqual(win[("iocp", 1000)]["b_eff_ns"], WINDOWS_BEFF)
        self.assertEqual(win[("iocp", 1000)]["source"], "fallback: coarsest timer resolution")
        self.assertEqual(self.cell("windows:S-iocp-none-W1-B1ms")["b_eff_source"],
                         "fallback: coarsest timer resolution")
        self.assertEqual(self.cell("windows:S-iocp-none-W1-B1ms")["t_med"]["flags"], 0)
        # A Windows backend with no data at all still has the system's timer: no fallback to B.
        self.assertEqual(win[("condvar", 1000)]["b_eff_ns"], WINDOWS_BEFF)
        self.assertEqual(win[("condvar", 1000)]["source"], "fallback: coarsest timer resolution")

    def test_intervals_and_overdispersion(self):
        od = self.cell("linux:S-epoll-no_retrigger-W1-B1ms")["t_med"]
        self.assertTrue(od["overdispersion"]["overdispersed"])
        self.assertEqual(od["clustered_95"][0], 0.2)  # 2 of 10 processes flagging is the 2.5% point
        full = self.cell("linux:S-epoll-absent_wake-W1-B1ms")["t_med"]
        self.assertFalse(full["overdispersion"]["defined"])
        self.assertFalse(full["overdispersion"]["overdispersed"])
        self.assertEqual(full["clustered_95"], (1.0, 1.0))
        self.assertIn("note", full)
        lo, hi = full["exact_95"]
        self.assertAlmostEqual(lo, 0.025 ** (1 / 300), delta=1e-12)

    def test_every_rate_has_a_clustered_interval(self):
        # Section 5: T_count's rate too, in the cells of every family, not only on F2.
        for key, family in (("linux:S-epoll-absent_wake-W1-B1ms", "F1"), ("linux:S-epoll-no_retrigger-W1-B1ms", "F1"),
                            ("linux:S-epoll-lazy_wake_race-W1-B1ms", None), ("linux:S-epoll-none-W1-B1ms", "F2"),
                            ("linux:Bt-epoll-absent_wake-W1-B1ms", None)):
            c = self.cell(key)
            self.assertEqual(c["family"], family)
            self.assertIn("clustered_95", c["t_count"], key)
            self.assertIsNotNone(c["t_count"]["clustered_95"], key)
        self.assertEqual(self.cell("linux:S-epoll-absent_wake-W1-B1ms")["t_count"]["clustered_95"], (1.0, 1.0))
        self.assertEqual(self.cell("linux:S-epoll-lazy_wake_race-W1-B1ms")["t_count"]["clustered_95"], (0.0, 0.0))
        # Half the processes flag every trial: the interval of the pooled rate over the 10 processes.
        self.assertEqual(self.cell("linux:S-epoll-no_retrigger-W1-B1ms")["t_count"]["clustered_95"], (0.2, 0.8))
        # The overdispersion check of T_count stays with the F2 cells.
        self.assertNotIn("overdispersion", self.cell("linux:S-epoll-no_retrigger-W1-B1ms")["t_count"])
        self.assertIn("overdispersion", self.cell("linux:S-epoll-none-W2-B1ms")["t_count"])

    def test_t_count_and_timeouts(self):
        c = self.cell("linux:S-epoll-none-W2-B1ms")
        self.assertEqual(c["t_med"]["flags"], 0)
        self.assertEqual(c["t_count"]["flags"], 3)
        self.assertIn("clustered_95", c["t_count"])
        self.assertEqual(c["timed_out"], 1)  # the 6 ms unit, beyond 5 x 1.0045 ms
        self.assertEqual(c["diagnostics"], {"timed_out_wakeprobe": 1})
        f2 = {x["cell"] for x in self.res["t_count_f2"]}
        self.assertIn("linux:S-epoll-none-W2-B1ms", f2)
        self.assertEqual(len(f2), 80)  # every F2 cell, with or without data

    def test_one_timed_out_count_by_section_8(self):
        for key, c in self.cells.items():
            self.assertNotIn("timed_out_rule", c, key)
            self.assertNotIn("timed_out_wakeprobe", c, key)  # wakeprobe's flag lives under diagnostics only
            self.assertEqual(set(c["diagnostics"]), {"timed_out_wakeprobe"}, key)
        # wakeprobe flagged 2 units under the limit and none of the 3 over it: the rule's count wins.
        c = self.cell("linux:S-epoll-plain_wake-W2-B1ms")
        self.assertEqual(c["timed_out"], 3)
        self.assertEqual(c["diagnostics"]["timed_out_wakeprobe"], 2)
        # Windows: 5 x max(B, B_eff, coarsest timer) = 78.125 ms, so 60 ms is in time and 80 ms is not.
        w = self.cell("windows:S-iocp-none-W1-B1ms")
        self.assertEqual(w["timed_out"], 1)
        self.assertEqual(w["diagnostics"]["timed_out_wakeprobe"], 0)

    def test_realized_gaps(self):
        g = self.cell("linux:Bt-epoll-none-W1-B1ms")["realized_gap_over_b"]
        self.assertEqual((g["min"], g["median"], g["max"]), (2.0, 2.0, 2.0))

    def test_blind_spots(self):
        bs = self.res["blind_spots"]
        self.assertEqual(bs["failed_predictions"], ["linux:S-epoll-undrained_edge-W1-B1ms"])
        und = self.cell("linux:S-epoll-undrained_edge-W1-B1ms")
        self.assertAlmostEqual(und["blind_lower_95"], 0.0729, delta=0.0001)
        self.assertFalse(self.cell("linux:Bt-epoll-absent_wake-W1-B1ms")["blind_prediction_fails"])
        lazy = self.cell("linux:S-epoll-lazy_wake_race-W1-B1ms")
        self.assertEqual((lazy["units"], lazy["units_over_half_b_eff"]), (6300, 0))
        self.assertAlmostEqual(lazy["per_unit_over_half_upper_95"], 4.75e-4, delta=0.005e-4)
        block = lazy["per_unit_over_half"]
        self.assertEqual((block["flags"], block["trials"], block["clustered_95"]), (0, 6300, (0.0, 0.0)))
        self.assertNotIn("overdispersion", block)
        # 2 variants x 2 B x (backends x 3 W under S + backends under Bt): Linux 48, Windows 32.
        self.assertEqual(len(self.res["lazy_wake_per_unit"]), 2 * 2 * (3 * 3 + 3) + 2 * 2 * (2 * 3 + 2))

    def test_h3(self):
        h3 = {(r["system"], r["backend"], r["bound_us"]): r for r in self.res["H3"]["results"]}
        self.assertEqual(self.res["H3"]["criteria"], 10)
        self.assertEqual(h3[("linux", "epoll", 1000)]["verdict"], "holds")
        self.assertEqual(h3[("linux", "epoll", 1000)]["posts"], 630)
        self.assertAlmostEqual(h3[("linux", "epoll", 1000)]["median_abs_residual_over_b_eff"], 0.01, delta=1e-6)
        self.assertEqual(h3[("linux", "io_uring", 1000)]["verdict"], "fails")
        self.assertEqual(h3[("linux", "condvar", 1000)]["verdict"], "incomplete")
        self.assertEqual(self.res["H3"]["holding"], 1)

    def test_h4(self):
        f4 = self.res["H4"]["F4"]["tests"]
        f4c = self.res["H4"]["F4c"]["tests"]
        self.assertTrue(f4["linux-epoll-N1us"]["rejected"])
        self.assertEqual(f4["linux-epoll-N1us"]["p"], Fraction(1, 16384))
        self.assertFalse(f4["linux-epoll-N10us"]["rejected"])
        self.assertTrue(f4c["linux-epoll-N10us"]["rejected"])
        self.assertFalse(f4c["linux-epoll-N1us"]["rejected"])
        self.assertIsNone(f4["linux-epoll-N100us"]["p"])
        self.assertTrue(f4["linux-io_uring-N1us"]["rejected"])
        readings = self.res["H4"]["readings"]
        self.assertTrue(readings["linux-epoll-N10us"]["reading"].startswith("inconclusive"))
        self.assertTrue(readings["linux-epoll-N10us"]["cost_claimed"])
        self.assertEqual(readings["linux-epoll-N100us"]["reading"], "incomplete")
        self.assertEqual(readings["windows-iocp-N1us"]["reading"], "incomplete")
        self.assertEqual(self.res["H4"]["underpowered_cells"], ["linux-epoll-N10us"])
        self.assertEqual(self.res["H4"]["h4_r_source"], self.source)
        self.assertFalse(any("underpowered H4 cells are not known" in w for w in self.res["warnings"]))
        self.assertFalse(self.res["H4"]["holds_on_every_cell"])

    def test_h4_cells(self):
        cells = {(c["key"], c["arm_a"]): c for c in self.res["H4"]["fixed_over_absent"]}
        inc = cells[("linux-epoll-N100us", "none")]
        self.assertEqual((inc["pairs"], inc["pairs_planned"], inc["complete"]), (12, 14, False))
        rerun = cells[("linux-io_uring-N1us", "none")]
        self.assertEqual(rerun["pairs"], 14)
        self.assertEqual(rerun["processes"][2]["used"], "rerun")
        ok = cells[("linux-epoll-N1us", "none")]
        self.assertAlmostEqual(ok["median_ratio"], 1.0, delta=1e-6)
        lo, hi = ok["clustered_95_mean_ratio"]
        self.assertAlmostEqual(lo, 1.0, delta=1e-6)
        self.assertAlmostEqual(hi, 1.0, delta=1e-6)
        n0 = {c["key"]: c for c in self.res["H4"]["n0_disclosed_repeat"]}
        self.assertEqual(n0["linux-epoll-N0us"]["prediction"], "below 0.98")
        self.assertEqual(n0["linux-epoll-N0us"]["median_position"], "below 0.98")
        self.assertEqual(n0["windows-condvar-N0us"]["prediction"], "no prediction")
        plain = {c["key"]: c for c in self.res["H4"]["plain_over_absent"]}
        self.assertEqual(plain["linux-epoll-N1us"]["pairs"], 14)
        self.assertNotIn("holm_f4", plain["linux-epoll-N1us"])

    def test_replications(self):
        reps = self.res["replications"]
        self.assertEqual(len(reps["R"]), 8)
        self.assertEqual(len(reps["Rd"]), 8 + 6 + 10)
        r = {x["cell"]: x for x in reps["R"]}
        self.assertTrue(r["linux:S-epoll-absent_wake-W1-B1ms"]["rejected_in_family"])
        self.assertFalse(r["linux:S-epoll-plain_wake-W1-B1ms"]["rejected_in_family"])
        rd = {x["cell"]: x for x in reps["Rd"]}
        self.assertTrue(rd["linux:Bt-epoll-none-W1-B1ms"]["rejected_in_family"])
        self.assertTrue(rd["windows:S-iocp-absent_wake-W1-B1ms"]["rejected_in_family"])

    def test_json_output(self):
        out = os.path.join(self.tmp, "results.json")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            score.main(["--linux", self.linux[0], self.linux[1], "--windows", self.windows[0], self.windows[1],
                        "--revision-log", self.log, "--draft", "--out", out])
        with open(out, "r", encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(doc["H4"]["h4_r_source"], self.source)  # file name and the sha256 of the bytes read
        self.assertEqual(doc["H4"]["underpowered_cells"], ["linux-epoll-N10us"])
        self.assertEqual(doc["H1"]["m"], 102)
        self.assertEqual(doc["systems_scored"], ["linux", "windows"])
        self.assertTrue(math.isclose(doc["H2"]["tests"]["linux:S-epoll-none-W1-B1ms"]["p"],
                                     float(stats.h2_pvalue(3, 300))))
        self.assertIs(doc["draft"], True)  # the output says it is a draft
        self.assertRegex(doc["generated_by"], r"^analysis/v2/score\.py \S+ draft$")


class LostWakes(unittest.TestCase):
    """Section 8: a lost unit in a fixed or plain cell is itself a lost wake, in whichever run it was lost."""

    BEFF = {"b_eff_ns": LINUX_BEFF, "source": "test", "from_cell": "test"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p1v2-lost-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def cell(self, variant="none", protocol="S"):
        return [c for c in grid.s_cells("linux") + grid.bt_cells("linux")
                if c.variant == variant and c.protocol == protocol and c.backend == "epoll" and c.workers == 1
                and c.bound_us == 1000][0]

    def score(self, cell, edit):
        """Score a cell of ten whole processes after edit(process index, entry, lines) changed the files."""
        entries = [e for e in plan.s_bt_entries("linux") if e["cell"] == cell.name]
        entries.sort(key=lambda e: readers.process_index(e["id"]))
        for i, e in enumerate(entries):
            whole = synth.wakeprobe_output(e, synth.process_delays(LINUX_BEFF, 0), None)
            if not edit(i, e, whole):
                synth.write_process(self.tmp, e, first=whole)
        folder = readers.DataDir(self.tmp)
        return score.score_cell(cell, [readers.Resolved(e, folder, "linux") for e in entries], self.BEFF)

    def lines(self, e, **kw):
        return synth.wakeprobe_output(e, synth.process_delays(LINUX_BEFF, 0), None, **kw)

    def test_a_clean_cell_has_no_lost_wake(self):
        c = self.score(self.cell(), lambda i, e, whole: False)
        self.assertTrue(c["complete"])
        self.assertNotIn("lost_wake", c)
        self.assertEqual((c["lost_units_first"], c["lost_units_rerun"], c["lost_units_void"]), (0, 0, 0))

    def test_a_lost_unit_of_the_first_run(self):
        def edit(i, e, whole):
            if i == 4:
                synth.write_process(self.tmp, e, first=self.lines(e, lose_at=50), rerun=whole)
                return True
        c = self.score(self.cell(), edit)
        self.assertTrue(c["complete"])
        self.assertTrue(c["lost_wake"])
        self.assertEqual((c["lost_units_first"], c["lost_units_rerun"], c["lost_units_void"]), (1, 0, 0))

    def test_a_lost_unit_of_the_rerun_alone(self):
        # The first run was killed at its time limit with no lost unit; the rerun lost one. The rerun's
        # lost unit is the cell's lost wake, and the process is incomplete.
        def edit(i, e, whole):
            if i == 4:
                synth.write_process(self.tmp, e, first=None, rerun=self.lines(e, lose_at=50), exits=["timeout", 6])
                return True
        c = self.score(self.cell(), edit)
        self.assertFalse(c["complete"])
        self.assertTrue(c["lost_wake"])
        self.assertEqual((c["lost_units_first"], c["lost_units_rerun"], c["lost_units_void"]), (0, 1, 0))
        self.assertEqual(c["lost_wake_runs"], [{"process": "S-epoll-none-W1-B1ms-p4", "run": "rerun", "lost_units": 1}])

    def test_a_lost_unit_of_a_void_run_alone(self):
        # The process ended whole on its second start; the run the runner was killed in lost a unit.
        def edit(i, e, whole):
            if i == 7:
                synth.write_process(self.tmp, e, first=whole, void=[(1, self.lines(e, lose_at=9), False),
                                                                    (1, self.lines(e, lose_at=3), True)])
                return True
        c = self.score(self.cell(), edit)
        self.assertTrue(c["complete"])  # the void runs' data never count, and never fail the process
        self.assertTrue(c["lost_wake"])
        self.assertEqual((c["lost_units_first"], c["lost_units_rerun"], c["lost_units_void"]), (0, 0, 2))
        self.assertEqual(sorted((r["run"], r["lost_units"]) for r in c["lost_wake_runs"]),
                         [("S-epoll-none-W1-B1ms-p7-rerun.interrupted-1.jsonl", 1),
                          ("S-epoll-none-W1-B1ms-p7.interrupted-1.jsonl", 1)])

    def test_a_lost_unit_in_an_unfinished_process(self):
        # No .done file: the process never ended. It is not good, and what it lost is still reported.
        def edit(i, e, whole):
            if i == 2:
                synth.write_process(self.tmp, e, first=self.lines(e, lose_at=12), done=False)
                return True
        c = self.score(self.cell(), edit)
        self.assertFalse(c["complete"])
        self.assertTrue(c["lost_wake"])
        self.assertEqual(c["lost_units_void"], 1)

    def test_a_plain_wake_cell_too_and_a_delaying_cell_not(self):
        def edit(i, e, whole):
            if i == 0:
                synth.write_process(self.tmp, e, first=self.lines(e, lose_at=50), rerun=whole)
                return True
        self.assertTrue(self.score(self.cell("plain_wake"), edit)["lost_wake"])
        shutil.rmtree(self.tmp)
        os.mkdir(self.tmp)
        c = self.score(self.cell("lazy_wake_race"), edit)  # a lost unit there is reported, but is no lost wake
        self.assertEqual(c["lost_units_first"], 1)
        self.assertNotIn("lost_wake", c)


class Bounds(unittest.TestCase):
    """The thresholds are strict: a delay of exactly B_eff / 5 (T_med) or B_eff / 2 (T_count) does not flag."""

    B = 1000000  # a B_eff that 5 and 2 divide

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p1v2-bounds-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.entry = plan.s_bt_entries("linux")[0]

    def process(self, trial_delays):
        """score_process of one trial (the rest quiet): trial_delays are the 21 delays of trial 0."""
        delays = list(trial_delays) + [1000] * (grid.POSTS - len(trial_delays))
        path = os.path.join(self.tmp, "x.jsonl")
        synth.write_lines(path, synth.wakeprobe_output(self.entry, delays))
        return score.score_process(readers.Run(path), self.B, 1000000, 0)

    def test_t_med_at_the_threshold(self):
        at = self.B // 5
        self.assertEqual(self.process([at] * 21)["t_med"], 0)
        self.assertEqual(self.process([at + 1] * 21)["t_med"], 1)
        self.assertEqual(self.process([at - 1] * 21)["t_med"], 0)

    def test_t_med_is_the_median_of_the_trial(self):
        high = self.B // 2
        self.assertEqual(self.process([high] * 11 + [1000] * 10)["t_med"], 1)  # 11 of 21 high: the median is high
        self.assertEqual(self.process([high] * 10 + [1000] * 11)["t_med"], 0)  # 10 of 21: the mean is high, the median not
        self.assertEqual(self.process([10 ** 9] * 10 + [1000] * 11)["t_med"], 0)

    def test_t_count_at_the_threshold(self):
        half = self.B // 2
        self.assertEqual(self.process([half] + [1000] * 20)["t_count"], 0)
        self.assertEqual(self.process([half + 1] + [1000] * 20)["t_count"], 1)
        self.assertEqual(self.process([1000] * 20 + [half + 1])["t_count"], 1)  # the last delay of the trial counts too
        self.assertEqual(self.process([half - 1] * 21)["t_count"], 0)

    def test_the_units_over_half_use_the_same_bound(self):
        half = self.B // 2
        out = self.process([half, half + 1, half + 2] + [1000] * 18)
        self.assertEqual(out["units_over_half"], 2)

    def test_a_trial_is_21_units_in_order(self):
        # Trial 0 holds units 0 to 20, trial 1 units 21 to 41: 11 high units split across the two do not flag either.
        out = self.process([1000] * 15 + [self.B] * 11 + [1000] * 10)
        self.assertEqual((out["trials"], out["t_med"]), (30, 0))
        out = self.process([1000] * 10 + [self.B] * 11 + [1000] * 10)
        self.assertEqual(out["t_med"], 1)


class H3Bound(unittest.TestCase):
    """H3's criterion is strict: the median of |residual| / B_eff must be below 0.05."""

    B = 1000000

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p1v2-h3-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def verdict(self, residual):
        entries = [e for e in plan.sweep_entries("linux") if e["cell"] == grid.sweep_name("epoll", 1000)]
        for e in entries:
            synth.write_process(self.tmp, e, first=synth.wakeprobe_output(
                e, [self.B // 2] * 21, self.B, residuals=[residual] * 21))
        folder = readers.DataDir(self.tmp)
        resolved = [readers.Resolved(e, folder, "linux") for e in entries]
        return score.score_sweep("linux", "epoll", 1000, resolved)

    def test_exactly_the_criterion_fails(self):
        at = self.verdict(self.B // 20)  # 50,000 / 1,000,000 = 0.05
        self.assertEqual(at["median_abs_residual_over_b_eff"], grid.H3_CRITERION)
        self.assertEqual(at["verdict"], "fails")
        self.assertEqual(self.verdict(self.B // 20 + 1)["verdict"], "fails")
        shutil.rmtree(self.tmp)
        os.mkdir(self.tmp)
        self.assertEqual(self.verdict(self.B // 20 - 1)["verdict"], "holds")

    def test_the_sign_of_the_residual_does_not_matter(self):
        self.assertEqual(self.verdict(-(self.B // 20))["verdict"], "fails")


class TimedOutCount(unittest.TestCase):
    """Section 8: delivered after 5 x max(B, B_eff, the coarsest timer resolution), strictly after."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p1v2-timeout-")
        self.entry = plan.s_bt_entries("linux")[0]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_of(self, delays, timed_out_at=(), lines=None):
        path = os.path.join(self.tmp, "x.jsonl")
        synth.write_lines(path, lines or synth.wakeprobe_output(self.entry, delays, timed_out_at=timed_out_at))
        return readers.Run(path)

    def count(self, extra, b_ns, b_eff_ns, coarsest_ns):
        """timed_out of a process whose first units have the delays in extra."""
        delays = [1000] * grid.POSTS
        delays[:len(extra)] = extra
        return score.score_process(self.run_of(delays), b_eff_ns, b_ns, coarsest_ns)["timed_out"]

    def test_the_limit_itself_is_in_time(self):
        limit = 5 * 1000000
        self.assertEqual(self.count([limit], 1000000, 1000000, 0), 0)
        self.assertEqual(self.count([limit + 1], 1000000, 1000000, 0), 1)
        self.assertEqual(self.count([limit - 1, limit, limit + 1, 2 * limit], 1000000, 1000000, 0), 2)

    def test_the_limit_is_five_times_the_largest_of_the_three(self):
        cases = {
            "B": (10000000, 1004500, 0),  # 50 ms
            "B_eff": (1000000, 15625000, 0),  # 78.125 ms
            "the coarsest timer": (1000000, 1004500, 15625000),  # 78.125 ms
            "no timer field": (1000000, 1004500, None),  # 5.0225 ms
        }
        for name, (b, b_eff, coarsest) in cases.items():
            limit = 5 * max(b, b_eff, coarsest or 0)
            self.assertEqual(self.count([limit, limit + 1], b, b_eff, coarsest), 1, name)
            self.assertEqual(self.count([limit - 1], b, b_eff, coarsest), 0, name)

    def test_wakeprobe_s_flag_is_only_a_cross_check(self):
        delays = [1000] * grid.POSTS
        delays[0], delays[1] = 2000000, 6000000
        run = self.run_of(delays, timed_out_at={0})  # wakeprobe flagged the 2 ms unit, not the 6 ms one
        out = score.score_process(run, 1004500, 1000000, 0)
        self.assertEqual((out["timed_out"], out["timed_out_wakeprobe"]), (1, 1))
        self.assertNotIn("timed_out_rule", out)

    def test_only_measured_units_count(self):
        lines = synth.wakeprobe_output(self.entry, [1000] * grid.POSTS)
        for rec in lines[:-1]:
            if rec["kind"] != "measured":
                rec["delay_ns"] = 10 ** 11  # a probe or warm-up unit that took 100 s
        out = score.score_process(self.run_of(None, lines=lines), 1000000, 1000000, 0)
        self.assertEqual(out["timed_out"], 0)


class RevisionLogOption(unittest.TestCase):
    """The underpowered cells come from the revision log's block: --revision-log, or --no-revision-log with --draft."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p1v2-score3-")
        self.plan, self.data = build_windows(self.tmp)
        self.out = os.path.join(self.tmp, "results.json")
        self.repo = os.path.join(self.tmp, "repo")  # a folder standing in for the repository root
        os.mkdir(self.repo)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_main(self, *extra):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = score.main(["--windows", self.plan, self.data, "--draft", "--out", self.out] + list(extra),
                            repo=self.repo)
        with open(self.out, "r", encoding="utf-8") as f:
            return rc, json.load(f)

    def refused(self, *extra):
        with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            score.main(["--windows", self.plan, self.data, "--draft", "--out", self.out] + list(extra), repo=self.repo)
        self.assertFalse(os.path.exists(self.out), "a refused scoring wrote its output")
        message = str(cm.exception.code)
        self.assertTrue(message.startswith("score.py: refused: "), message)
        self.assertIn("--no-revision-log", message)
        return message

    def test_the_default_log_is_at_the_repository_root(self):
        path = write_log(self.repo)
        rc, doc = self.run_main()
        self.assertEqual(rc, 0)
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(doc["H4"]["h4_r_source"], {"file": "revision-log-v2.md", "sha256": digest})
        self.assertEqual(doc["H4"]["underpowered_cells"], ["linux-epoll-N10us"])
        self.assertFalse(any("not known" in w for w in doc["warnings"]))

    def test_a_named_log(self):
        path = write_log(self.tmp, name="other-log.md")
        rc, doc = self.run_main("--revision-log", path)
        self.assertEqual(rc, 0)
        self.assertEqual(doc["H4"]["h4_r_source"]["file"], "other-log.md")

    def test_a_log_without_a_block_is_refused(self):
        write_log(self.repo, text="# Revision log\n\nNone yet.\n")
        self.assertIn("no h4-r block", self.refused())

    def test_an_unusable_log_is_refused(self):
        self.assertIn("cannot read", self.refused())  # no log at the default place
        good = write_log(self.repo)
        with open(good, "r", encoding="utf-8") as f:
            text = f.read()
        write_log(self.repo, text=text.replace("| 24 | yes |", "| 15 | yes |"))
        self.assertIn("even integer from 14 to 24", self.refused())
        write_log(self.repo, text=text + text)
        self.assertIn("more than one h4-r block", self.refused())

    def test_scoring_without_the_log_says_so(self):
        # No log at all at the default place, and none read: only the warning remains.
        rc, doc = self.run_main("--no-revision-log")
        self.assertEqual(rc, 0)
        self.assertIsNone(doc["H4"]["h4_r_source"])
        self.assertEqual(doc["H4"]["underpowered_cells"], [])
        self.assertTrue(any(w == "no revision log (--no-revision-log): the underpowered H4 cells are not known"
                            for w in doc["warnings"]))

    def test_the_two_options_exclude_each_other_and_the_old_options_are_gone(self):
        for argv in (["--revision-log", "x.md", "--no-revision-log"], ["--h4-r", "x.json"], ["--commit", "abc123"]):
            with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
                score.main(["--windows", self.plan, self.data, "--out", self.out] + argv, repo=self.repo)
            self.assertEqual(cm.exception.code, 2)
        self.assertFalse(os.path.exists(self.out))


class Warnings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p1v2-score2-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def edit_line(self, plan_path, n, edit):
        with open(plan_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        e = json.loads(lines[n])
        edit(e)
        lines[n] = json.dumps(e)
        with open(plan_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")

    def test_a_plan_off_the_frozen_grid_is_named(self):
        plan_path, data = build_windows(self.tmp)
        res = score.score([(plan_path, data)], draft=True)
        self.assertFalse(any("differ from the frozen grid" in w for w in res["warnings"]))
        self.assertTrue(any(w.startswith("no revision log") for w in res["warnings"]))
        self.assertIsNone(res["H4"]["h4_r_source"])
        self.edit_line(plan_path, 1, lambda e: e["args"].__setitem__(e["args"].index("--gap-seed") + 1, "20260926"))
        res = score.score([(plan_path, data)], draft=True)  # the 2026-09 seed, not v2's
        self.assertTrue(any("1 plan entries differ from the frozen grid" in w for w in res["warnings"]))

    def test_every_field_of_a_line_is_compared_with_the_plan_generators(self):
        for edit in (lambda e: e.update(limit_s=e["limit_s"] + 1), lambda e: e.update(cell="S-iocp-none-W1-B10ms"),
                     lambda e: e.update(family="Bt"), lambda e: e.update(tool="abpair", args=e["args"][:-2])):
            plan_path, data = build_windows(self.tmp)
            self.edit_line(plan_path, 1, edit)
            try:
                res = score.score([(plan_path, data)], draft=True)
            except ValueError:
                continue  # a cell that is not in the grid is refused outright
            self.assertTrue(any("differ from the frozen grid" in w for w in res["warnings"]))
            shutil.rmtree(self.tmp)
            os.mkdir(self.tmp)

    def test_the_plan_s_h4_lines_are_compared_too(self):
        plan_path, data = build_linux(self.tmp)
        res = score.score([(plan_path, data)], draft=True)
        self.assertFalse(any("differ from the frozen grid" in w for w in res["warnings"]))
        with open(plan_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        n = [i for i, line in enumerate(lines) if json.loads(line).get("family") == "H4"][0]
        for edit in (lambda e: e["args"].__setitem__(e["args"].index("--work-ns") + 1, "5"),
                     lambda e: e.update(limit_s=e["limit_s"] - 1)):
            self.edit_line(plan_path, n, edit)
            res = score.score([(plan_path, data)], draft=True)
            self.assertTrue(any("differ from the frozen grid" in w for w in res["warnings"]))
            with open(plan_path, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(lines) + "\n")

    def test_h4_process_counts_come_from_the_log(self):
        plan_path, data = build_linux(self.tmp)  # a stand-in plan: 7 processes per H4 comparison
        cells = synth.pilot_cells(14, {"linux-epoll-N10us": {"r": 24, "underpowered": True}})
        h4_r = h4log.parse_block(h4log.render_block(cells))
        res = score.score([(plan_path, data)], h4_r, h4_r_source={"file": "x", "sha256": "0" * 64}, draft=True)
        text = " ".join(res["warnings"])
        self.assertIn("H4 comparison H4-epoll-N10us-none has 7 processes in the plan, not R / 2 = 12", text)
        self.assertNotIn("H4 comparison H4-epoll-N1us-none has", text)
        self.assertNotIn("H4-epoll-N1us-plain_wake has", text)

    def test_an_abpair_window_of_the_wrong_variant_fails_the_process(self):
        e = plan.h4_entries("linux", stand_in_r("linux"))[0]
        path = os.path.join(self.tmp, "x.jsonl")
        lines = synth.abpair_output(e, [1.0, 1.0])
        lines[1]["ran"] = "plain_wake"
        synth.write_lines(path, lines)
        self.assertIn("arm", readers.abpair_problem(readers.Run(path), e, "linux"))


class OneSystem(unittest.TestCase):
    def test_the_other_system_is_incomplete(self):
        tmp = tempfile.mkdtemp(prefix="p1v2-score1-")
        try:
            res = score.score([build_windows(tmp)], draft=True)
            self.assertTrue(any(w.startswith("NOT SCORED: no linux data") for w in res["warnings"]))
            self.assertEqual(res["H1"]["m"], 102)
            self.assertTrue(res["H1"]["tests"]["windows:S-iocp-absent_wake-W1-B1ms"]["rejected"])
            self.assertIsNone(res["H1"]["tests"]["linux:S-epoll-absent_wake-W1-B1ms"]["p"])
            self.assertEqual(res["H1"]["rejected"], 1)
            self.assertEqual(res["H4"]["F4"]["rejected"], 0)
            self.assertEqual(res["H4"]["F4"]["m"], 15)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class WindowsNeedsATimer(unittest.TestCase):
    def test_a_windows_scoring_with_no_timer_reading_is_refused(self):
        tmp = tempfile.mkdtemp(prefix="p1v2-score4-")
        try:
            c = Campaign(tmp, "windows")
            for e in c.entries("S-iocp-none-W1-B1ms"):  # whole processes, but their summaries carry no period
                c.write(e, c.lines(e, [1000] * 630, coarsest=None))
            path, data = c.write_plan(), c.dir
            with self.assertRaises(beff.BeffError):
                score.score([(path, data)], draft=True)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                score.main(["--windows", path, data, "--draft", "--no-revision-log", "--out", os.path.join(tmp, "o.json")])
            self.assertIn("coarsest_timer_us", str(cm.exception.code))
            self.assertFalse(os.path.exists(os.path.join(tmp, "o.json")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_failed_process_s_timer_reading_is_enough(self):
        tmp = tempfile.mkdtemp(prefix="p1v2-score5-")
        try:
            c = Campaign(tmp, "windows")
            es = c.entries("S-iocp-none-W1-B1ms")
            for i, e in enumerate(es):
                if i == 0:  # a process that failed twice, whose summaries still tell the period
                    c.write(e, c.lines(e, [1000] * 630, coarsest=WINDOWS_TIMER_US, lose_at=5),
                            c.lines(e, [1000] * 630, coarsest=WINDOWS_TIMER_US, lose_at=5))
                else:
                    c.write(e, c.lines(e, [1000] * 630, coarsest=None))
            res = score.score([(c.write_plan(), c.dir)], draft=True)
            win = {(x["backend"], x["bound_us"]): x for x in res["b_eff"]["windows"]}
            self.assertEqual(win[("iocp", 1000)]["b_eff_ns"], WINDOWS_BEFF)
            self.assertEqual(win[("iocp", 1000)]["source"], "fallback: coarsest timer resolution")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------------------------
# A real scoring: the commit from git, a clean tree, the plan header, the log hash, R / 2, one build
# ---------------------------------------------------------------------------------------------


class StrictProvenance(unittest.TestCase):
    """score.py without --draft, on real plans of plan.py made in a throwaway repository."""

    def setUp(self):
        self.t = TempGit(self)
        self.repo = self.t.repo
        self.plans = os.path.join(self.t.root, "plans")
        self.text = "# Revision log\n\n" + h4log.render_block(synth.pilot_cells(14)) + "\nEnd.\n"
        self.t.write(self.repo, plan.REVISION_LOG, self.text)
        self.t.commit_all(self.repo)
        with redirect_stdout(io.StringIO()):
            plan.main(["campaign", "--out-dir", self.plans, "--system", "windows"], repo=self.repo)
        self.plan = os.path.join(self.plans, "plan-windows.jsonl")
        self.data = os.path.join(self.t.root, "data")
        os.mkdir(self.data)
        self.out = os.path.join(self.t.root, "results.json")
        _, self.entries = readers.load_plan(self.plan)
        self.by_cell = {}
        for e in self.entries:
            self.by_cell.setdefault(e["cell"], []).append(e)
        # One whole cell of data and nothing else: every other process has no output.
        for e in self.by_cell["S-iocp-none-W1-B1ms"]:
            synth.write_process(self.data, e, first=synth.wakeprobe_output(
                e, [1000] * 630, system="windows", coarsest_timer_us=WINDOWS_TIMER_US))
        for e in self.by_cell["H4-iocp-N1us-none"]:
            synth.write_process(self.data, e, first=synth.abpair_output(e, [1.0, 1.0]))

    def main(self, *extra, plan_path=None):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return score.main(["--windows", plan_path or self.plan, self.data, "--out", self.out] + list(extra),
                              repo=self.repo)

    def refused(self, *extra, plan_path=None):
        with self.assertRaises(SystemExit) as cm:
            self.main(*extra, plan_path=plan_path)
        self.assertFalse(os.path.exists(self.out), "a refused scoring wrote its output")
        message = str(cm.exception.code)
        self.assertTrue(message.startswith("score.py: refused: "), message)
        return message

    def result(self):
        with open(self.out, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_a_clean_tree_and_a_real_plan_are_scored(self):
        self.assertEqual(self.main(), 0)
        doc = self.result()
        self.assertEqual(doc["generated_by"], "analysis/v2/score.py " + self.t.head(self.repo))
        self.assertIs(doc["draft"], False)
        self.assertEqual(doc["warnings"].count("NOT SCORED: no linux data; its cells count as incomplete in F1, F2, F4 "
                                               "and F4c"), 1)
        self.assertFalse([w for w in doc["warnings"] if "differ" in w or "plan header" in w])
        self.assertEqual(doc["provenance"]["windows"]["bench_commits"], {synth.BENCH_COMMIT: 10 + 7})  # S cell, H4 cell
        self.assertTrue(doc["cells"]["windows:S-iocp-none-W1-B1ms"]["complete"])

    def test_the_commit_is_read_from_git_and_cannot_be_given(self):
        with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
            self.main("--commit", "abc123")
        self.assertEqual(cm.exception.code, 2)

    def test_a_dirty_tree_is_refused(self):
        self.t.write(self.repo, "new.txt", "x\n")
        self.assertIn("not clean", self.refused())
        os.remove(os.path.join(self.repo, "new.txt"))
        self.t.write(self.repo, plan.REVISION_LOG, self.text + "\nchanged\n")
        self.assertIn("not clean", self.refused())

    def test_a_draft_may_come_from_a_dirty_tree_and_is_marked(self):
        self.t.write(self.repo, "new.txt", "x\n")
        self.assertEqual(self.main("--draft"), 0)
        doc = self.result()
        self.assertIs(doc["draft"], True)
        self.assertRegex(doc["generated_by"], r"^analysis/v2/score\.py \S+ draft$")

    def test_no_log_is_for_drafts_only(self):
        self.assertIn("--no-revision-log is for --draft", self.refused("--no-revision-log"))
        self.assertEqual(self.main("--no-revision-log", "--draft"), 0)

    def test_a_plan_header_of_another_form_is_refused(self):
        for by in ("test", "analysis/v2/plan.py " + "a" * 40, "analysis/v2/plan.py " + "a" * 40 + " stand-in-R",
                   "analysis/v2/plan.py " + "a" * 40 + " revision-log-v2.md sha256:" + "b" * 63,
                   "analysis/v2/plan.py " + "a" * 40 + " other-log.md sha256:" + "b" * 64,
                   "analysis/v2/plan.py " + "a" * 39 + " revision-log-v2.md sha256:" + "b" * 64):
            path = os.path.join(self.t.root, "odd.jsonl")
            plan.write_plan(path, plan.header("windows", by), self.entries)
            self.assertIn("not of the real form", self.refused(plan_path=path), by)

    def test_a_stand_in_plan_is_refused(self):
        path = os.path.join(self.t.root, "stand-in.jsonl")
        plan.write_plan(path, plan.header("windows", plan.generated_by("a" * 40, "stand-in-R")),
                        plan.campaign_entries("windows", stand_in_r("windows")))
        self.assertIn("not of the real form", self.refused(plan_path=path))

    def test_a_pilot_plan_is_refused(self):
        with redirect_stdout(io.StringIO()):
            plan.main(["pilot", "--out-dir", self.plans, "--system", "windows"], repo=self.repo)
        message = self.refused(plan_path=os.path.join(self.plans, "pilot-windows.jsonl"))
        self.assertIn("not of the real form", message)

    def test_the_log_hash_in_the_header_must_be_the_log_read(self):
        # The log with a note added after the plan was made: the same R everywhere, another hash.
        self.t.write(self.repo, plan.REVISION_LOG, self.text + "\nA later note.\n")
        self.t.commit_all(self.repo)
        message = self.refused()
        self.assertIn("the plan header records a log with sha256", message)
        self.assertIn(hashlib.sha256(self.text.encode("utf-8")).hexdigest(), message)
        # The same bytes under another name hash the same: reading that copy is fine.
        copy = os.path.join(self.t.root, "copy.md")
        with open(copy, "w", encoding="utf-8", newline="\n") as f:
            f.write(self.text)
        self.assertEqual(self.main("--revision-log", copy), 0)

    def test_h4_process_counts_must_be_r_over_two(self):
        with open(self.plan, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        drop = [i for i, line in enumerate(lines) if json.loads(line).get("cell") == "H4-iocp-N10us-plain_wake"][-1]
        short = os.path.join(self.t.root, "short.jsonl")
        with open(short, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines[:drop] + lines[drop + 1:]) + "\n")
        message = self.refused(plan_path=short)
        self.assertIn("H4 comparison H4-iocp-N10us-plain_wake has 6 processes in the plan, not R / 2 = 7", message)

    def test_the_plan_s_lines_must_be_the_generators(self):
        with open(self.plan, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        e = json.loads(lines[5])
        e["limit_s"] += 1
        lines[5] = json.dumps(e)
        tampered = os.path.join(self.t.root, "tampered.jsonl")
        with open(tampered, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        self.assertIn("1 plan entries differ from the frozen grid", self.refused(plan_path=tampered))
        # Dropping an S line is a departure too.
        del lines[5]
        with open(tampered, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        self.assertIn("process lines of the frozen grid are not in the plan", self.refused(plan_path=tampered))

    def test_the_processes_run_in_the_shuffled_order(self):
        with open(self.plan, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        lines[3], lines[4] = lines[4], lines[3]  # the same lines, two of them swapped
        swapped = os.path.join(self.t.root, "swapped.jsonl")
        with open(swapped, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        self.assertIn("the processes are not in the order shuffled with seed 20261026", self.refused(plan_path=swapped))
        self.assertEqual(self.main("--draft", plan_path=swapped), 0)  # a warning in a draft
        self.assertTrue(any("not in the order" in w for w in self.result()["warnings"]))
        # In file order the plan is the generator's, and says nothing of the sort.
        os.remove(self.out)
        self.assertEqual(self.main(), 0)
        self.assertFalse([w for w in self.result()["warnings"] if "order" in w])

    def test_one_build_commit_for_every_process(self):
        e = self.by_cell["S-iocp-none-W1-B1ms"][4]
        synth.write_process(self.data, e, first=synth.wakeprobe_output(
            e, [1000] * 630, system="windows", coarsest_timer_us=WINDOWS_TIMER_US,
            summary={"bench_commit": synth.OTHER_COMMIT}))
        message = self.refused()
        self.assertIn("built from 2 commits, not one", message)
        self.assertIn(synth.OTHER_COMMIT + " (1)", message)
        # The H4 processes count too.
        self.write_s_cell()
        self.assertEqual(self.main(), 0)
        os.remove(self.out)
        e = self.by_cell["H4-iocp-N1us-none"][0]
        synth.write_process(self.data, e, first=synth.abpair_output(e, [1.0, 1.0],
                                                                   summary={"bench_commit": synth.OTHER_COMMIT}))
        self.assertIn("built from 2 commits, not one", self.refused())

    def write_s_cell(self, commit=synth.BENCH_COMMIT):
        for e in self.by_cell["S-iocp-none-W1-B1ms"]:
            synth.write_process(self.data, e, first=synth.wakeprobe_output(
                e, [1000] * 630, system="windows", coarsest_timer_us=WINDOWS_TIMER_US, summary={"bench_commit": commit}))

    def test_the_build_commit_must_be_a_commit_name(self):
        for commit in ("unknown", "", "abc123", "0123456789ABCDEF0123456789ABCDEF01234567"):
            self.write_s_cell(commit)
            for e in self.by_cell["H4-iocp-N1us-none"]:
                synth.write_process(self.data, e, first=synth.abpair_output(e, [1.0, 1.0], summary={"bench_commit": commit}))
            self.assertIn("not a commit name", self.refused(), commit)

    def test_a_process_that_is_not_good_does_not_vote_on_the_commit(self):
        e = self.by_cell["S-iocp-none-W1-B1ms"][4]  # a Debug build from another commit: not good, so not counted
        synth.write_process(self.data, e, first=synth.wakeprobe_output(
            e, [1000] * 630, system="windows", coarsest_timer_us=WINDOWS_TIMER_US,
            summary={"bench_commit": synth.OTHER_COMMIT, "build_type": "Debug"}))
        self.assertEqual(self.main(), 0)
        self.assertFalse(self.result()["cells"]["windows:S-iocp-none-W1-B1ms"]["complete"])


if __name__ == "__main__":
    unittest.main()

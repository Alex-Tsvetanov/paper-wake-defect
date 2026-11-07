# Copyright 2026 Alex Tsvetanov
# SPDX-License-Identifier: Apache-2.0
"""Tests of quick_analyse.py and quick_paper.py on synthetic values (never results).

    python bench/quick/test_quick_analyse.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import quick_analyse as A  # noqa: E402
import quick_paper as P  # noqa: E402

sys.path.insert(0, str(HERE.parent.parent / "analysis" / "v2"))
import beff  # noqa: E402


def proc(delays: list[int], **row) -> dict:
    return {"row": {"round": 0, "id": "x-r0", **row}, "summary": {},
            "measured": [{"delay_ns": d, "kind": "measured"} for d in delays]}


def summ(b: float) -> dict:
    return {"b_eff_ns": b, "coarsest_timer_us": 15625}


class Rules(unittest.TestCase):
    def test_trial_rule_is_score_py_strict(self):
        # B_eff 1000: T_med flags when 5 x median > 1000 (median > 200); T_count when one delay > 500.
        at = proc([200] * 21)
        over = proc([201] * 21)
        one = proc([0] * 20 + [501])
        edge = proc([0] * 20 + [500])
        self.assertEqual(A.flags(at, 1000)["t_med"], 0)
        self.assertEqual(A.flags(over, 1000)["t_med"], 1)
        self.assertEqual(A.flags(one, 1000)["t_count"], 1)
        self.assertEqual(A.flags(edge, 1000)["t_count"], 0)

    def test_trials_are_consecutive_groups_of_21(self):
        p = proc([0] * 21 + [999] * 21 + [0] * 10)
        f = A.flags(p, 1000)
        self.assertEqual((f["trials"], f["t_med"], f["t_count"]), (2, 1, 1))

    def test_spread_adds_iqr_from_five(self):
        self.assertNotIn("q1", A.spread([1.0, 2.0, 3.0, 4.0]))
        s = A.spread([5.0, 1.0, 4.0, 2.0, 3.0])
        self.assertEqual((s["median"], s["min"], s["max"], s["q1"], s["q3"]), (3.0, 1.0, 5.0, 2.0, 4.0))

    def test_rule_matches_frozen_decision_15_for_ten_processes(self):
        for values in ([1000] * 10, [1000] * 7 + [None] * 3, [1000] * 9 + [1200], [1000] * 8 + [None] * 2):
            for os_name in ("linux", "windows"):
                want = beff.beff_rule(os_name, 1000, [summ(v) if v else {"b_eff_ns": None, "coarsest_timer_us": 15625}
                                                      for v in values])
                got = A.beff_rule(os_name, 1000, values, 10, 8, 15625)
                self.assertEqual(got["b_eff_ns"], want["b_eff_ns"])

    def test_r1_rule_two_of_three(self):
        self.assertEqual(A.beff_rule("linux", 1000, [1001, 1002, None], 3, 2, None)["source"], "measured")
        self.assertEqual(A.beff_rule("linux", 1000, [1001, None, None], 3, 2, None)["b_eff_ns"], 1000 * 1000)
        w = A.beff_rule("windows", 1000, [10_000_000, 16_000_000, 16_100_000], 3, 2, 15625)
        self.assertEqual((w["source"], w["b_eff_ns"]), ("fallback: coarsest timer", 15_625_000))

    def test_r3_rule_stops_where_the_open_threshold_binds(self):
        self.assertEqual(A.beff_rule("windows", 1000, [16e6] * 6, 6, None, 15625)["source"], "measured")
        self.assertEqual(A.beff_rule("windows", 1000, [10e6] + [16e6] * 5, 6, None, 15625)["source"],
                         "fallback: coarsest timer")
        with self.assertRaises(A.AnalysisError):
            A.beff_rule("windows", 1000, [16e6] * 5 + [None], 6, None, 15625)

    def test_phase_is_reduced_modulo_beff(self):
        p = {"measured": [{"phase_ns": x} for x in (0, 150, 1050, 1999, 2500)], "row": {}}
        c = A.phase_cover([p], 1000)
        self.assertEqual(c["tenth_counts"][0], 2)   # 0 and 1050
        self.assertEqual(c["tenth_counts"][1], 1)   # 150
        self.assertEqual(c["tenth_counts"][5], 1)   # 2500
        self.assertEqual(c["tenth_counts"][9], 1)   # 1999
        self.assertAlmostEqual(c["over_one_period"], 3 / 5)

    def test_levels_regex(self):
        found = set(A.OPT_RE.findall("cl /nologo /O2 /Ob2 /DNDEBUG -c x.cpp"))
        self.assertEqual({x for x in found if not x.startswith("/Ob")}, {"/O2"})
        self.assertEqual(set(A.OPT_RE.findall("clang++ -O3 -DNDEBUG -o a.o -c b.cpp")), {"-O3"})
        self.assertEqual(set(A.OPT_RE.findall("clang++ -O0 -g -c b.cpp")), {"-O0"})


R4_CELLS = (("unobserved_primitive", 1, True), ("unobserved_primitive", 4, True), ("wrong_worker", 2, True),
            ("wrong_worker", 4, True), ("absent_wake", 4, True), ("none", 4, False))
R4_B = 16_000_000  # synthetic B_eff in ns, not a result


def r4_job(source_beff: dict | None = None, rounds: int = 10) -> dict:
    """A synthetic R4 job: every delaying post waits 0.6 B_eff, every fixed-loop post 0.01 B_eff."""
    procs = []
    for backend in ("condvar", "iocp"):
        for variant, workers, delaying in R4_CELLS:
            for r in range(rounds):
                own = (source_beff or {}).get((backend, r), R4_B) if (variant, workers) == ("unobserved_primitive", 1) \
                    else R4_B
                delay = int(0.6 * R4_B) if delaying else R4_B // 100
                procs.append({
                    "row": {"round": r, "id": f"R4-{backend}-{variant}-W{workers}-r{r}", "backend": backend,
                            "variant": variant, "workers": workers, "wait_us": 1000},
                    "summary": {"b_eff_ns": own if delaying else None, "b_eff_idle_ns": int(0.97 * R4_B),
                                "delays_single_posts": delaying, "gap_unit_us": 15625, "coarsest_timer_us": 15625,
                                "lost": 0, "timeouts": 0},
                    "measured": [{"delay_ns": delay, "kind": "measured", "phase_ns": (i * R4_B) // 10 + 1}
                                 for i in range(42)]})
    return {"os": "windows", "procs": procs}


class R4(unittest.TestCase):
    def test_count_threshold_is_the_ceiling_of_eight_tenths(self):
        self.assertEqual([A.r4_min_calibrated(n) for n in (10, 9, 8, 6, 5, 1)], [8, 8, 7, 5, 4, 1])

    def test_rule_at_ten_processes_matches_frozen_decision_15(self):
        for values in ([1000] * 10, [1000] * 7 + [None] * 3, [1000] * 8 + [None] * 2, [1000] * 9 + [1200]):
            want = beff.beff_rule("windows", 1000, [summ(v) if v else {"b_eff_ns": None, "coarsest_timer_us": 15625}
                                                    for v in values])
            got = A.beff_rule("windows", 1000, values, 10, A.r4_min_calibrated(10), 15625)
            self.assertEqual(got["b_eff_ns"], want["b_eff_ns"])

    def test_first_line_comes_from_the_unobserved_one_worker_cell(self):
        res = A.r4(r4_job({("iocp", r): R4_B + 1000 * r for r in range(10)}))
        self.assertEqual(res["rows"]["iocp"]["planted_rule"]["source"], "measured")
        self.assertEqual(res["rows"]["iocp"]["planted_rule"]["b_eff_ns"], R4_B + 4500)
        self.assertEqual(res["rows"]["condvar"]["planted_rule"]["b_eff_ns"], R4_B)
        self.assertEqual(res["rows"]["iocp"]["idle"]["median"], int(0.97 * R4_B))

    def test_fallback_when_fewer_than_eight_calibrate(self):
        res = A.r4(r4_job({("condvar", r): None for r in range(3)}))
        rule = res["rows"]["condvar"]["planted_rule"]
        self.assertEqual((rule["calibrated"], rule["min_calibrated"]), (7, 8))
        self.assertEqual((rule["source"], rule["b_eff_ns"]), ("fallback: coarsest timer", 15_625_000))

    def test_cells_flags_phase_and_no_own_beff_for_the_fixed_loop(self):
        res = A.r4(r4_job())
        cells = res["rows"]["iocp"]["cells"]
        self.assertEqual(len(cells), 6)
        fixed, wrong = cells[("none", 4)], cells[("wrong_worker", 4)]
        self.assertFalse(fixed["delaying"])
        self.assertNotIn("own_beff", fixed)
        self.assertNotIn("phase", fixed)
        self.assertEqual((fixed["planted"]["t_med_trials"], fixed["planted"]["trials"]), (0, 20))
        self.assertEqual((wrong["planted"]["t_med_trials"], wrong["idle"]["t_count_trials"]), (20, 20))
        self.assertEqual(wrong["phase"]["tenth_counts"], [50, 50, 40, 40, 40, 40, 40, 40, 40, 40])  # 42 posts
        self.assertEqual(res["lost"], {"lost": 0, "timeouts": 0})

    def test_mixed_delaying_flag_stops(self):
        job = r4_job()
        job["procs"][0]["summary"]["delays_single_posts"] = False
        with self.assertRaises(A.AnalysisError):
            A.r4(job)


class Paper(unittest.TestCase):
    def test_r4_macros_follow_every_earlier_macro(self):
        res = A.r4(r4_job())
        j = res | {"run": "R4", "plan": "quick-W-r4", "rounds": 10, "trials_per_process": 2, "wall_s": 1.0,
                   "commit": "0" * 40, "timer_off_default": [], "timer_changed": [], "timer_start": {}}
        earlier = P.Macros()
        P.common_macros(earlier, {"quick-W-r4": j})
        self.assertEqual(earlier.items, [])  # R4 adds nothing to the R1 to R3 block
        text = P.macros({"quick-W-r4": j})
        head = text.splitlines()
        self.assertTrue(head[1].startswith("% ---- R4"))
        self.assertIn("\\newcommand{\\QuickRFourWindowsIocpWrongWFourPlantedTmed}{20}", text)


    def test_macro_names_letters_and_once(self):
        m = P.Macros()
        m.add("QuickA", 1)
        with self.assertRaises(ValueError):
            m.add("QuickA", 2)
        with self.assertRaises(ValueError):
            m.add("QuickB1", 2)
        with self.assertRaises(ValueError):
            m.add("Other", 2)

    def test_signed_us_never_prints_minus_zero(self):
        self.assertEqual(P.signed_us(-20), "0.0")
        self.assertEqual(P.signed_us(-100), "-0.1")


if __name__ == "__main__":
    unittest.main()

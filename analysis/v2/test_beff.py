"""The B_eff rule of section 2: the measured median and every fallback case."""

import unittest

import beff
import grid


def summaries(values, coarsest=None):
    return [{"b_eff_ns": v, "coarsest_timer_us": coarsest} for v in values]


class Measured(unittest.TestCase):
    def test_ten_calibrated_within_ten_percent(self):
        vals = [1000000 + 1000 * i for i in range(10)]
        r = beff.beff_rule("linux", 1000, summaries(vals))
        self.assertEqual(r["source"], beff.SOURCE_MEASURED)
        self.assertEqual(r["b_eff_ns"], 1004500)  # the mean of the 5th and 6th values
        self.assertEqual(r["calibrated"], 10)

    def test_eight_calibrated_is_enough(self):
        vals = [15625000] * 8 + [None, None]
        r = beff.beff_rule("windows", 10000, summaries(vals, coarsest=15625))
        self.assertEqual(r["source"], beff.SOURCE_MEASURED)
        self.assertEqual(r["b_eff_ns"], 15625000)

    def test_eight_summaries_of_ten_processes(self):
        # Two processes lost twice: their summaries are absent, and the other 8 still calibrate.
        r = beff.beff_rule("linux", 1000, summaries([1000000] * 8))
        self.assertEqual(r["source"], beff.SOURCE_MEASURED)

    def test_span_of_exactly_ten_percent_holds(self):
        vals = [1000000] * 5 + [1100000] * 5  # median 1,050,000; span 100,000 < 105,000
        self.assertEqual(beff.beff_rule("linux", 1000, summaries(vals))["source"], beff.SOURCE_MEASURED)
        vals = [1000000] * 9 + [1100000]  # median 1,000,000; span 100,000, exactly 10%: not more
        r = beff.beff_rule("linux", 1000, summaries(vals))
        self.assertEqual(r["source"], beff.SOURCE_MEASURED)
        self.assertEqual(r["b_eff_ns"], 1000000)


class Fallback(unittest.TestCase):
    def test_seven_calibrated_linux_takes_b(self):
        vals = [1060000] * 7 + [None] * 3
        r = beff.beff_rule("linux", 1000, summaries(vals))
        self.assertEqual(r["source"], beff.SOURCE_BOUND)
        self.assertEqual(r["b_eff_ns"], 1000000)
        self.assertIn("7 of 10", r["reason"])

    def test_wide_span_linux_takes_b(self):
        vals = [1000000] * 9 + [1100001]  # span just above 10% of the median
        r = beff.beff_rule("linux", 10000, summaries(vals))
        self.assertEqual(r["source"], beff.SOURCE_BOUND)
        self.assertEqual(r["b_eff_ns"], 10000000)
        self.assertIn("span", r["reason"])

    def test_windows_takes_the_coarsest_timer_when_larger(self):
        vals = [None] * 10
        r = beff.beff_rule("windows", 1000, summaries(vals, coarsest=15625))
        self.assertEqual(r["source"], beff.SOURCE_TIMER)
        self.assertEqual(r["b_eff_ns"], 15625000)

    def test_windows_takes_b_when_larger(self):
        r = beff.beff_rule("windows", 10000, summaries([None] * 10, coarsest=1000))
        self.assertEqual(r["source"], beff.SOURCE_BOUND)
        self.assertEqual(r["b_eff_ns"], 10000000)
        r = beff.beff_rule("windows", 10000, summaries([None] * 10, coarsest=15625))
        self.assertEqual(r["b_eff_ns"], 15625000)

    def test_windows_without_a_timer_field_refuses(self):
        # Never B: B = 1 ms is not what a timed return takes on Windows at the default period.
        with self.assertRaisesRegex(beff.BeffError, "coarsest_timer_us"):
            beff.beff_rule("windows", 1000, summaries([None] * 10))
        with self.assertRaises(beff.BeffError):
            beff.beff_rule("windows", 1000, [])
        with self.assertRaises(beff.BeffError):  # a field that is not a positive number is no reading
            beff.beff_rule("windows", 1000, summaries([None] * 10, coarsest=0))
        with self.assertRaises(beff.BeffError):
            beff.beff_rule("windows", 1000, summaries([None] * 10, coarsest=True))

    def test_windows_reads_the_timer_from_any_summary_of_the_system(self):
        # Nothing calibrated in this cell and no good summary at all, but another process of the
        # system, failed or not, reports the period: that one sets the fallback.
        failed = summaries([None], coarsest=15625)
        r = beff.beff_rule("windows", 1000, [], failed)
        self.assertEqual((r["source"], r["b_eff_ns"]), (beff.SOURCE_TIMER, 15625000))
        # The coarsest of several readings.
        r = beff.beff_rule("windows", 1000, [], summaries([None], coarsest=1000) + summaries([None], coarsest=15625))
        self.assertEqual(r["b_eff_ns"], 15625000)
        # The cell's own summaries do not hide the system's.
        r = beff.beff_rule("windows", 1000, summaries([None] * 3), failed)
        self.assertEqual(r["b_eff_ns"], 15625000)
        # A larger B still wins over a finer period.
        r = beff.beff_rule("windows", 10000, [], summaries([None], coarsest=1000))
        self.assertEqual((r["source"], r["b_eff_ns"]), (beff.SOURCE_BOUND, 10000000))

    def test_the_measured_value_needs_no_timer(self):
        r = beff.beff_rule("windows", 1000, summaries([15620000 + 1000 * i for i in range(10)]))
        self.assertEqual(r["source"], beff.SOURCE_MEASURED)

    def test_linux_ignores_the_timer_field(self):
        r = beff.beff_rule("linux", 1000, summaries([None] * 10, coarsest=15625))
        self.assertEqual(r["b_eff_ns"], 1000000)

    def test_no_summaries(self):
        r = beff.beff_rule("linux", 1000, [])
        self.assertEqual((r["source"], r["calibrated"]), (beff.SOURCE_BOUND, 0))


class Table(unittest.TestCase):
    def test_source_cell(self):
        c = grid.beff_source_cell("windows", "iocp", 1000)
        self.assertEqual(c.name, "S-iocp-absent_wake-W1-B1ms")
        self.assertTrue(c.delaying)

    def test_a_windows_table_without_any_timer_refuses(self):
        with self.assertRaises(beff.BeffError):
            beff.beff_table("windows", {}, [])
        table = beff.beff_table("windows", {}, summaries([None], coarsest=15625))
        self.assertEqual(len(table), 4)
        self.assertTrue(all(e["b_eff_ns"] == 15625000 for e in table.values() if e["bound_us"] == 1000))
        self.assertTrue(all(e["b_eff_ns"] == 15625000 for e in table.values() if e["bound_us"] == 10000))

    def test_a_linux_table_needs_no_timer(self):
        table = beff.beff_table("linux", {}, [])
        self.assertEqual({e["source"] for e in table.values()}, {beff.SOURCE_BOUND})


if __name__ == "__main__":
    unittest.main()

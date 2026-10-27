"""Exact values of hypotheses-v2.md, sections 2, 4, 5 and 7, and the behaviour of stats.py."""

import math
import unittest
from fractions import Fraction
from unittest import mock

import grid
import rng
import stats
from rng import SplitMix64


class BinomialThresholds(unittest.TestCase):
    """Section 2: n = 300, alpha 0.025 one-sided, Holm."""

    def test_strictest_steps(self):
        self.assertEqual(grid.ALPHA / 102, Fraction(1, 4080))  # 2.45e-4 in F1
        self.assertAlmostEqual(float(grid.ALPHA / 102), 2.45e-4, delta=0.005e-4)
        self.assertEqual(grid.ALPHA / 80, Fraction(1, 3200))  # 3.125e-4, written 3.13e-4 in F2

    def test_three_misses_pass_four_fail_in_f1(self):
        p3 = stats.h1_pvalue(297, 300)
        p4 = stats.h1_pvalue(296, 300)
        self.assertIsInstance(p3, Fraction)  # exact
        self.assertAlmostEqual(float(p3), 1.64e-4, delta=0.005e-4)
        self.assertAlmostEqual(float(p4), 6.9e-4, delta=0.05e-4)
        self.assertLessEqual(p3, grid.ALPHA / 102)
        self.assertGreater(p4, grid.ALPHA / 102)

    def test_three_false_positives_pass_four_fail_in_f2(self):
        self.assertLessEqual(stats.h2_pvalue(3, 300), grid.ALPHA / 80)
        self.assertGreater(stats.h2_pvalue(4, 300), grid.ALPHA / 80)
        self.assertEqual(stats.h2_pvalue(3, 300), stats.h1_pvalue(297, 300))

    def test_seven_pass_at_the_last_step(self):
        self.assertLessEqual(stats.h2_pvalue(7, 300), grid.ALPHA)
        self.assertGreater(stats.h2_pvalue(8, 300), grid.ALPHA)
        self.assertLessEqual(stats.h1_pvalue(293, 300), grid.ALPHA)
        self.assertGreater(stats.h1_pvalue(292, 300), grid.ALPHA)

    def test_n_200_one_miss(self):
        self.assertAlmostEqual(float(stats.h1_pvalue(199, 200)), 4.0e-4, delta=0.05e-4)
        self.assertGreater(stats.h1_pvalue(199, 200), grid.ALPHA / 102)

    def test_power_at_the_strictest_step(self):
        # Up to 3 misses pass: P(misses <= 3) for per-trial power 0.995, 0.99 and 0.98.
        for miss, want in ((Fraction(5, 1000), 0.93), (Fraction(1, 100), 0.65), (Fraction(2, 100), 0.15)):
            self.assertAlmostEqual(float(stats.binom_cdf(3, 300, miss)), want, delta=0.005)

    def test_float_and_exact_agree(self):
        for k in (0, 3, 10, 150, 299):
            self.assertAlmostEqual(stats.binom_cdf(k, 300, 0.05), float(stats.binom_cdf(k, 300, Fraction(1, 20))),
                                   delta=1e-12)
            self.assertAlmostEqual(stats.binom_sf(k, 300, 0.95), float(stats.binom_sf(k, 300, Fraction(19, 20))),
                                   delta=1e-12)

    def test_large_n_does_not_overflow(self):
        self.assertAlmostEqual(stats.binom_cdf(10, 6300, 0.001), float(stats.binom_cdf(10, 6300, Fraction(1, 1000))),
                               delta=1e-10)


class Holm(unittest.TestCase):
    def test_step_down(self):
        res = stats.holm({"a": 0.001, "b": 0.012, "c": 0.02, "d": 0.5}, alpha=Fraction(5, 100))
        # thresholds 0.0125, 0.01667, 0.025, 0.05
        self.assertTrue(res["a"]["rejected"])
        self.assertTrue(res["b"]["rejected"])
        self.assertTrue(res["c"]["rejected"])
        self.assertFalse(res["d"]["rejected"])
        self.assertEqual(res["c"]["threshold"], Fraction(5, 100) / 2)

    def test_a_p_value_at_its_threshold_rejects(self):
        # Holm rejects when p <= alpha / (m - i + 1), the threshold included.
        res = stats.holm({"a": Fraction(1, 80), "b": Fraction(1, 40)}, alpha=Fraction(1, 40))
        self.assertEqual(res["a"]["threshold"], Fraction(1, 80))
        self.assertTrue(res["a"]["rejected"])
        self.assertTrue(res["b"]["rejected"])  # rank 2, threshold alpha
        self.assertFalse(stats.holm({"a": Fraction(1, 80) + Fraction(1, 10 ** 9), "b": Fraction(1, 40)},
                                    alpha=Fraction(1, 40))["a"]["rejected"])

    def test_stops_at_first_failure(self):
        res = stats.holm({"a": 0.03, "b": 0.001, "c": 0.04}, alpha=Fraction(5, 100))
        self.assertTrue(res["b"]["rejected"])  # rank 1, threshold 0.0167
        self.assertFalse(res["a"]["rejected"])  # rank 2, threshold 0.025: fails
        self.assertFalse(res["c"]["rejected"])  # rank 3: below 0.05, but Holm has stopped
        self.assertEqual(res["c"]["rank"], 3)

    def test_incomplete_cells_stay_in_m(self):
        complete = {"k%d" % i: 1e-6 for i in range(9)}
        res = stats.holm(dict(complete, missing=None), alpha=Fraction(1, 40))
        self.assertEqual(len(res), 10)
        first = [k for k in res if res[k]["rank"] == 1][0]
        self.assertEqual(res[first]["threshold"], Fraction(1, 400))  # alpha / 10, m counts the incomplete cell
        self.assertFalse(res["missing"]["rejected"])
        self.assertEqual(res["missing"]["rank"], 10)
        self.assertTrue(all(res[k]["rejected"] for k in complete))

    def test_three_misses_in_f1_at_rank_two(self):
        # A cell with 4 misses fails at the second step of F1 (threshold alpha / 101).
        pvals = {"pass": stats.h1_pvalue(300, 300), "four": stats.h1_pvalue(296, 300)}
        pvals.update({"other%d" % i: None for i in range(100)})
        res = stats.holm(pvals)
        self.assertTrue(res["pass"]["rejected"])
        self.assertFalse(res["four"]["rejected"])
        self.assertEqual(res["four"]["threshold"], grid.ALPHA / 101)


class ClopperPearson(unittest.TestCase):
    def test_zero_in_6300(self):
        self.assertAlmostEqual(stats.cp_upper(0, 6300), 4.75e-4, delta=0.005e-4)
        self.assertAlmostEqual(stats.cp_upper(0, 6300), 1 - 0.05 ** (1 / 6300), delta=1e-12)

    def test_closed_forms_at_the_edges(self):
        lo, hi = stats.clopper_pearson(0, 300)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 1 - 0.025 ** (1 / 300), delta=1e-12)
        lo, hi = stats.clopper_pearson(300, 300)
        self.assertAlmostEqual(lo, 0.025 ** (1 / 300), delta=1e-12)
        self.assertEqual(hi, 1.0)

    def test_bounds_invert_the_tails(self):
        for k in (1, 15, 150, 290):
            lo = stats.cp_lower(k, 300, Fraction(95, 100))
            hi = stats.cp_upper(k, 300, Fraction(95, 100))
            self.assertAlmostEqual(stats.binom_sf(k, 300, lo), 0.05, delta=1e-9)
            self.assertAlmostEqual(stats.binom_cdf(k, 300, hi), 0.05, delta=1e-9)

    def test_blind_spot_rule_matches_its_tail_form(self):
        # The one-sided lower bound exceeds 0.05 exactly when P(X >= k | 300, 0.05) < 0.05.
        for k in range(0, 41):
            by_bound = stats.cp_lower(k, 300, grid.BLIND_CONFIDENCE) > grid.BLIND_RATE
            by_tail = stats.binom_sf(k, 300, Fraction(1, 20)) < Fraction(1, 20)
            self.assertEqual(by_bound, by_tail, k)


class SignTests(unittest.TestCase):
    def test_section_7_numbers(self):
        self.assertEqual(stats.sign_tail_ge(13, 14), Fraction(15, 16384))  # P(X <= 1 | 14)
        self.assertEqual(stats.sign_tail_ge(12, 13), Fraction(14, 8192))  # P(X <= 1 | 13)
        level = grid.ALPHA / grid.FAMILY_M["F4"]
        self.assertAlmostEqual(float(level), 1.67e-3, delta=0.005e-3)
        self.assertLessEqual(Fraction(15, 16384), level)
        self.assertGreater(Fraction(14, 8192), level)
        self.assertEqual(stats.sign_critical(14, level), 13)  # one pair outside still passes
        self.assertEqual(stats.sign_critical(13, level), 13)  # R = 13: none may be outside

    def test_cell_tests(self):
        ratios = [1.0] * 13 + [0.97]
        t = stats.h4_sign_tests(ratios)
        self.assertEqual(t["above_low"], 13)
        self.assertEqual(t["below_high"], 14)
        self.assertEqual(t["p_low"], Fraction(15, 16384))
        self.assertEqual(t["p_high"], Fraction(1, 16384))
        self.assertEqual(t["p"], Fraction(15, 16384))
        self.assertEqual(t["below_low"], 1)
        self.assertEqual(t["p_cost"], Fraction(16383, 16384))

    def test_a_ratio_on_a_margin_is_inside_it(self):
        # p_low counts the pairs above 0.98, p_high those below 1.02, the cost test those below 0.98: all strict.
        t = stats.h4_sign_tests([0.98] * 14)
        self.assertEqual((t["above_low"], t["below_high"], t["below_low"]), (0, 14, 0))
        t = stats.h4_sign_tests([1.02] * 14)
        self.assertEqual((t["above_low"], t["below_high"], t["below_low"]), (14, 0, 0))

    def test_cost_test(self):
        t = stats.h4_sign_tests([0.95] * 14)
        self.assertEqual(t["p_cost"], Fraction(1, 16384))
        self.assertEqual(t["p"], 1)


class PilotPower(unittest.TestCase):
    def test_section_7_examples(self):
        r, power, under = stats.pilot_choose_r(0.010)
        self.assertEqual((r, under), (14, False))
        self.assertAlmostEqual(power, 0.92, delta=0.005)
        r, power, under = stats.pilot_choose_r(0.0125)
        self.assertEqual((r, under), (18, False))
        self.assertAlmostEqual(power, 0.86, delta=0.005)
        r, power, under = stats.pilot_choose_r(0.015)
        self.assertEqual((r, under), (20, False))
        self.assertAlmostEqual(power, 0.8001, delta=0.00005)

    def test_power_is_not_monotone_in_r(self):
        # At s = 0.0125, R = 16 has less power than R = 14; the search must not stop early.
        self.assertLess(stats.pilot_power(0.0125, 16), stats.pilot_power(0.0125, 14))
        self.assertLess(stats.pilot_power(0.0125, 14), 0.8)
        self.assertLess(stats.pilot_power(0.0125, 16), 0.8)

    def test_underpowered(self):
        r, power, under = stats.pilot_choose_r(0.05)
        self.assertEqual((r, under), (24, True))
        self.assertLess(power, 0.8)

    def test_trinomial_by_brute_force(self):
        # The exact trinomial sum against an enumeration of every outcome at a small R.
        s, pairs = 0.012, 6
        level = Fraction(1, 64)
        c = stats.sign_critical(pairs, level)
        pb = 0.5 * math.erfc(-math.log(0.98) / s / math.sqrt(2))
        pa = 0.5 * math.erfc(math.log(1.02) / s / math.sqrt(2))
        total = 0.0
        for outcome in range(3 ** pairs):
            nb = na = 0
            prob = 1.0
            for _ in range(pairs):
                d = outcome % 3
                outcome //= 3
                prob *= (pb, 1 - pb - pa, pa)[d]
                nb += d == 0
                na += d == 2
            if pairs - nb >= c and pairs - na >= c:
                total += prob
        self.assertAlmostEqual(stats.pilot_power(s, pairs, m=1, alpha=level), total, delta=1e-12)


class Generator(unittest.TestCase):
    def test_splitmix64_reference(self):
        g = rng.SplitMix64(0)
        self.assertEqual([g.next_u64() for _ in range(3)],
                         [0xE220A8397B1DCDAF, 0x6E789E6AA1B965F4, 0x06C45D188009454F])

    def test_shuffle_is_a_fixed_permutation(self):
        items = list(range(50))
        a = rng.shuffled(items, grid.SEED)
        self.assertEqual(sorted(a), items)
        self.assertEqual(a, rng.shuffled(items, grid.SEED))
        self.assertNotEqual(a, items)
        self.assertEqual(a[:8], [32, 26, 5, 22, 18, 34, 0, 33])

    def test_below_is_in_range(self):
        g = rng.SplitMix64(1)
        self.assertTrue(all(0 <= g.below(7) < 7 for _ in range(1000)))


class Bootstrap(unittest.TestCase):
    def test_single_point_when_all_or_none(self):
        self.assertEqual(stats.rate_interval([30] * 10, [30] * 10), (1.0, 1.0))
        self.assertEqual(stats.rate_interval([0] * 10, [30] * 10), (0.0, 0.0))

    def test_two_processes(self):
        # Resampling 2 processes, rates 0 and 1: the pooled rate is 0, 0.5 or 1 with 1/4, 1/2, 1/4.
        lo, hi = stats.rate_interval([0, 30], [30, 30])
        self.assertEqual((lo, hi), (0.0, 1.0))

    def test_deterministic_and_seeded(self):
        counts = [3, 0, 0, 1, 2, 0, 0, 0, 1, 0]
        a = stats.rate_interval(counts, [30] * 10)
        self.assertEqual(a, stats.rate_interval(counts, [30] * 10))
        self.assertLessEqual(a[0], sum(counts) / 300)
        self.assertGreaterEqual(a[1], sum(counts) / 300)

    def test_nearest_rank(self):
        values = list(range(1, 10001))
        self.assertEqual(stats.nearest_rank(values, Fraction(1, 40)), 250)
        self.assertEqual(stats.nearest_rank(values, Fraction(39, 40)), 9750)


def exact_points(k_high, k=10, low=Fraction(1, 40), high=Fraction(39, 40)):
    """The nearest-rank low and high points of the number of high processes in a resample of k processes,
    k_high of which flag every trial and the rest none: that number is Binomial(k, k_high / k)."""
    p = Fraction(k_high, k)
    cdf = Fraction(0)
    lo = hi = None
    for t in range(k + 1):
        cdf += math.comb(k, t) * p ** t * (1 - p) ** (k - t)
        if lo is None and cdf >= low:
            lo = t
        if hi is None and cdf >= high:
            hi = t
    return lo, hi


class BootstrapLevels(unittest.TestCase):
    """The interval is the percentile 95% interval of 10,000 resamples at seed 20261026 (section 5)."""

    def test_the_frozen_constants(self):
        self.assertEqual((grid.BOOTSTRAP_RESAMPLES, grid.SEED, grid.INTERVAL_CONFIDENCE), (10000, 20261026, Fraction(95, 100)))

    def test_the_interval_is_the_95_percent_one(self):
        # 6 of 10 processes flag every trial, 4 none: a resample holds Binomial(10, 0.6) flagging processes.
        # Its 2.5% point is 3 processes and its 97.5% point 9; the 90% interval would end at 8.
        for high in (6, 4):
            lo, hi = exact_points(high)
            lo90, hi90 = exact_points(high, low=Fraction(1, 20), high=Fraction(19, 20))
            self.assertNotEqual((lo, hi), (lo90, hi90))  # the fixture tells a 90% interval from a 95% one
            got = stats.rate_interval([30] * high + [0] * (10 - high), [30] * 10)
            self.assertEqual(got, (lo * 30 / 300, hi * 30 / 300), high)
        self.assertEqual(stats.rate_interval([30] * 6 + [0] * 4, [30] * 10), (0.3, 0.9))
        self.assertEqual(stats.rate_interval([30] * 4 + [0] * 6, [30] * 10), (0.1, 0.7))

    def test_ten_thousand_resamples_by_default(self):
        calls = []
        stats.cluster_bootstrap([(1, 30), (2, 30), (3, 30)], lambda groups: calls.append(1) or 0.0)
        self.assertEqual(len(calls), 10000)
        with mock.patch.object(stats, "pooled_rate", wraps=stats.pooled_rate) as spy:
            stats.rate_interval([3, 0, 0, 1, 2, 0, 0, 0, 1, 0], [30] * 10)  # the way score.py asks for it
        self.assertEqual(spy.call_count, 10000)

    def test_the_default_seed_and_level_are_the_frozen_ones(self):
        groups = [(i, 30) for i in range(10)]
        default = stats.cluster_bootstrap(groups, stats.pooled_rate)
        self.assertEqual(default, stats.cluster_bootstrap(groups, stats.pooled_rate, resamples=10000, seed=20261026,
                                                          confidence=Fraction(95, 100)))
        self.assertNotEqual(default, stats.cluster_bootstrap(groups, stats.pooled_rate, confidence=Fraction(80, 100)))
        # The first resample is the one SplitMix64(20261026) draws: ten indices below 10, in order.
        first = []
        stats.cluster_bootstrap(groups, lambda resample: first.append(resample) or 0.0)
        rng = SplitMix64(20261026)
        self.assertEqual(first[0], [groups[rng.below(10)] for _ in range(10)])

    def test_each_call_starts_from_the_seed(self):
        groups = list(zip([3, 0, 0, 1, 2, 0, 0, 0, 1, 0], [30] * 10))
        a = stats.cluster_bootstrap(groups, stats.pooled_rate)
        stats.cluster_bootstrap(list(zip([5] * 10, [30] * 10)), stats.pooled_rate)
        self.assertEqual(a, stats.cluster_bootstrap(groups, stats.pooled_rate))


class OverdispersionNull(unittest.TestCase):
    """The null draws each process's count from Binomial(n, p) at the cell's own pooled rate and reads the 99th percentile."""

    SIZES = [30] * 10

    def simulate(self, counts):
        """(the verdict, every call of dispersion_index): the cell's own index first, then the simulated ones."""
        calls = []
        real = stats.dispersion_index

        def record(c, s):
            d = real(c, s)
            calls.append((list(c), d))
            return d

        stats._threshold_cache.clear()
        with mock.patch.object(stats, "dispersion_index", record):
            od = stats.overdispersion(counts, self.SIZES)
        stats._threshold_cache.clear()
        return od, calls

    def test_the_simulated_campaigns_have_the_cells_rate(self):
        for counts in ([3, 0, 0, 1, 2, 0, 0, 0, 1, 0], [27, 29, 30, 28, 27, 30, 29, 28, 27, 29],
                       [15, 14, 16, 15, 15, 16, 14, 15, 15, 15]):
            pooled = sum(counts) / 300
            od, calls = self.simulate(counts)
            sims = [c for c, _ in calls[1:]]
            self.assertEqual(len(sims), 10000)
            cells = [x for xs in sims for x in xs]
            n = 30
            mean = sum(cells) / len(cells)
            var = sum((x - mean) ** 2 for x in cells) / len(cells)
            self.assertAlmostEqual(mean, n * pooled, delta=0.04 * max(1.0, n * pooled * (1 - pooled)) ** 0.5 + 0.03)
            self.assertAlmostEqual(var, n * pooled * (1 - pooled), delta=0.05 * n * pooled * (1 - pooled) + 0.02)
            self.assertTrue(all(0 <= x <= n for x in cells))

    def test_the_threshold_is_the_99th_percentile_of_the_simulated_indices(self):
        for counts in ([3, 0, 0, 1, 2, 0, 0, 0, 1, 0], [15, 14, 16, 15, 15, 16, 14, 15, 15, 15], [30] * 5 + [0] * 5):
            od, calls = self.simulate(counts)
            index = sorted(0.0 if d is None else d for _, d in calls[1:])  # all or none flag: index 0, as the null counts it
            self.assertEqual(od["threshold"], index[math.ceil(0.99 * len(index)) - 1])
            self.assertGreater(od["threshold"], index[len(index) // 2])
            self.assertGreater(od["threshold"], index[math.ceil(0.95 * len(index)) - 1])
            self.assertEqual(od["simulations"], 10000)
        self.assertEqual(grid.OVERDISPERSION_QUANTILE, Fraction(99, 100))

    def test_exceeding_the_threshold_is_strict(self):
        # Two flagged trials in one process, nothing elsewhere: its index is exactly the 99th percentile.
        od = stats.overdispersion([2] + [0] * 9, self.SIZES)
        self.assertEqual(od["index"], od["threshold"])
        self.assertFalse(od["overdispersed"])
        self.assertTrue(stats.overdispersion([3] + [0] * 9, self.SIZES)["overdispersed"])
        self.assertFalse(stats.overdispersion([1, 1] + [0] * 8, self.SIZES)["overdispersed"])

    def test_the_threshold_follows_the_cell_s_rate(self):
        low = stats.overdispersion([2] + [0] * 9, self.SIZES)["threshold"]  # 2 flags in 300
        mid = stats.overdispersion([15, 14, 16, 15, 15, 16, 14, 15, 15, 15], self.SIZES)["threshold"]
        self.assertAlmostEqual(low, 2.0134, places=3)  # all of its simulated campaigns hold at most a few flags
        self.assertNotAlmostEqual(low, mid, places=2)


class Overdispersion(unittest.TestCase):
    def test_all_or_none_is_undefined_and_not_flagged(self):
        for counts in ([30] * 10, [0] * 10):
            od = stats.overdispersion(counts, [30] * 10)
            self.assertFalse(od["defined"])
            self.assertIsNone(od["index"])
            self.assertFalse(od["overdispersed"])

    def test_clustered_cell_is_flagged(self):
        od = stats.overdispersion([30] * 5 + [0] * 5, [30] * 10)
        self.assertTrue(od["defined"])
        self.assertTrue(od["overdispersed"])

    def test_binomial_like_cell_is_not_flagged(self):
        od = stats.overdispersion([15, 14, 16, 15, 15, 16, 14, 15, 15, 15], [30] * 10)
        self.assertFalse(od["overdispersed"])
        self.assertGreater(od["threshold"], 1.0)

    def test_index_is_variance_over_binomial(self):
        counts = [3, 0, 0, 1, 2, 0, 0, 0, 1, 0]
        rates = [c / 30 for c in counts]
        p = sum(counts) / 300
        want = (sum((r - p) ** 2 for r in rates) / 9) / (p * (1 - p) / 30)
        self.assertAlmostEqual(stats.dispersion_index(counts, [30] * 10), want, delta=1e-12)


if __name__ == "__main__":
    unittest.main()

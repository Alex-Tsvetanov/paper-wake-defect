"""The statistics of hypotheses-v2.md, standard library only.

- Exact binomial tails (exact rationals when the null rate is a Fraction).
- Holm's step-down procedure, with incomplete cells kept in m and never rejected.
- Clopper-Pearson bounds, one-sided and two-sided.
- The clustered percentile bootstrap over processes (section 5).
- The overdispersion check of section 5, with its all-or-none edge case.
- The exact sign tests of H4 (section 7) and the pilot power rule.

Percentiles of resampled or simulated values use the nearest rank, as wakeprobe does.
"""

import math
import statistics
from bisect import bisect_right
from fractions import Fraction

import grid
from rng import SplitMix64

# ---------------------------------------------------------------------------------------------
# Binomial tails
# ---------------------------------------------------------------------------------------------


def _log_pmf(j, n, p):
    return (math.lgamma(n + 1) - math.lgamma(j + 1) - math.lgamma(n - j + 1)
            + j * math.log(p) + (n - j) * math.log1p(-p))


def _float_sum(lo, hi, n, p):
    """Sum of the Binomial(n, p) pmf over j = lo..hi, for 0 < p < 1, in floating point."""
    return sum(math.exp(_log_pmf(j, n, p)) for j in range(lo, hi + 1))


def _exact_sum(lo, hi, n, p):
    num = p.numerator
    rest = p.denominator - p.numerator
    total = sum(math.comb(n, j) * num ** j * rest ** (n - j) for j in range(lo, hi + 1))
    return Fraction(total, p.denominator ** n)


def _tail(lo, hi, n, p):
    """P(lo <= X <= hi) for X ~ Binomial(n, p); exact when p is a Fraction."""
    lo = max(lo, 0)
    hi = min(hi, n)
    if lo > hi:
        return Fraction(0) if isinstance(p, Fraction) else 0.0
    if isinstance(p, Fraction):
        return _exact_sum(lo, hi, n, p)
    if p <= 0.0:
        return 1.0 if lo == 0 else 0.0
    if p >= 1.0:
        return 1.0 if hi == n else 0.0
    return min(1.0, _float_sum(lo, hi, n, p))


def binom_cdf(k, n, p):
    """P(X <= k)."""
    return _tail(0, k, n, p)


def binom_sf(k, n, p):
    """P(X >= k)."""
    return _tail(k, n, n, p)


def h1_pvalue(flags, n):
    """H1, one cell: exact one-sided test of power <= 0.95; small when nearly every trial flags."""
    return binom_sf(flags, n, grid.H1_POWER)


def h2_pvalue(flags, n):
    """H2, one cell: exact one-sided test of a false-positive rate >= 0.05."""
    return binom_cdf(flags, n, grid.H2_RATE)


# ---------------------------------------------------------------------------------------------
# Holm
# ---------------------------------------------------------------------------------------------


def holm(pvalues, alpha=grid.ALPHA):
    """Holm's step-down procedure over a family.

    pvalues maps a cell key to its p-value, or to None for an incomplete cell. Every key counts
    in m. An incomplete cell sorts last and is never rejected. Returns key -> {p, rank,
    threshold, rejected}; the threshold of rank i (from 1) is alpha / (m - i + 1).
    """
    m = len(pvalues)
    order = sorted(pvalues, key=lambda k: (pvalues[k] is None, pvalues[k] if pvalues[k] is not None else 0, str(k)))
    out = {}
    stopped = False
    for i, key in enumerate(order):
        p = pvalues[key]
        threshold = Fraction(alpha) / (m - i)
        rejected = False
        if not stopped:
            if p is not None and p <= threshold:
                rejected = True
            else:
                stopped = True
        out[key] = {"p": p, "rank": i + 1, "threshold": threshold, "rejected": rejected}
    return out


# ---------------------------------------------------------------------------------------------
# Clopper-Pearson
# ---------------------------------------------------------------------------------------------

_BISECTIONS = 100


def _bisect(f, target, increasing):
    """The p in [0, 1] where the monotone f(p) crosses target."""
    lo, hi = 0.0, 1.0
    for _ in range(_BISECTIONS):
        mid = (lo + hi) / 2
        if (f(mid) < target) == increasing:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def cp_lower(k, n, confidence=grid.BLIND_CONFIDENCE):
    """Exact one-sided lower bound: the p at which P(X >= k | n, p) = 1 - confidence."""
    if k <= 0:
        return 0.0
    a = float(1 - Fraction(confidence))
    return _bisect(lambda p: binom_sf(k, n, p), a, increasing=True)


def cp_upper(k, n, confidence=grid.BLIND_CONFIDENCE):
    """Exact one-sided upper bound: the p at which P(X <= k | n, p) = 1 - confidence."""
    if k >= n:
        return 1.0
    a = float(1 - Fraction(confidence))
    return _bisect(lambda p: binom_cdf(k, n, p), a, increasing=False)


def clopper_pearson(k, n, confidence=grid.INTERVAL_CONFIDENCE):
    """The exact two-sided interval: each side at (1 + confidence) / 2."""
    if n <= 0:
        return None
    side = (1 + Fraction(confidence)) / 2
    return (cp_lower(k, n, side), cp_upper(k, n, side))


# ---------------------------------------------------------------------------------------------
# Percentiles, medians
# ---------------------------------------------------------------------------------------------


def nearest_rank(sorted_values, q):
    """The nearest-rank q-quantile of sorted values (q a Fraction in (0, 1])."""
    n = len(sorted_values)
    rank = math.ceil(Fraction(q) * n)
    rank = min(max(rank, 1), n)
    return sorted_values[rank - 1]


def median(values):
    """The usual median: the middle value, or the mean of the two middle values."""
    return statistics.median(values)


# ---------------------------------------------------------------------------------------------
# The clustered bootstrap and the overdispersion check (section 5)
# ---------------------------------------------------------------------------------------------


def cluster_bootstrap(groups, statistic, resamples=grid.BOOTSTRAP_RESAMPLES, seed=grid.SEED,
                      confidence=grid.INTERVAL_CONFIDENCE):
    """Percentile interval of statistic(groups resampled with replacement), by process.

    A fresh generator with the frozen seed serves each call, so a cell's interval does not
    depend on which cells were scored before it.
    """
    k = len(groups)
    if k == 0:
        return None
    rng = SplitMix64(seed)
    values = []
    for _ in range(resamples):
        values.append(statistic([groups[rng.below(k)] for _ in range(k)]))
    values.sort()
    tail = (1 - Fraction(confidence)) / 2
    return (nearest_rank(values, tail), nearest_rank(values, 1 - tail))


def pooled_rate(groups):
    """groups: (flags, trials) per process."""
    trials = sum(g[1] for g in groups)
    return sum(g[0] for g in groups) / trials if trials else 0.0


def rate_interval(counts, sizes):
    """The clustered interval of a pooled rate; a single point when all or none flag.

    When every process has the same rate, every resample has that rate too, so the interval is
    that point; it is returned without resampling.
    """
    groups = list(zip(counts, sizes))
    if not groups:
        return None
    if len({Fraction(c, s) for c, s in groups if s}) <= 1:
        r = pooled_rate(groups)
        return (r, r)
    return cluster_bootstrap(groups, pooled_rate)


def dispersion_index(counts, sizes):
    """The variance of the per-process rates over its binomial value.

    sum((x_i - n_i p)^2 / (n_i p (1 - p))) / (k - 1), with p the pooled rate. With equal n_i
    this is the sample variance of the rates over p (1 - p) / n. None when every trial or no
    trial flags (or fewer than two processes): the index is undefined.
    """
    k = len(counts)
    total = sum(counts)
    n = sum(sizes)
    if k < 2 or n == 0 or total == 0 or total == n:
        return None
    p = total / n
    chi = sum((x - s * p) ** 2 / (s * p * (1 - p)) for x, s in zip(counts, sizes))
    return chi / (k - 1)


def _binom_cdf_table(size, p):
    table = []
    acc = 0.0
    for j in range(size + 1):
        acc += math.exp(_log_pmf(j, size, p))
        table.append(acc)
    return table


def overdispersion(counts, sizes, simulations=grid.OVERDISPERSION_SIMULATIONS, seed=grid.SEED,
                   quantile=grid.OVERDISPERSION_QUANTILE):
    """Section 5: is the dispersion index above the 99th percentile of its binomial value?

    The null draws each process's count from Binomial(n_i, p), p the cell's pooled rate, with
    one uniform per process through the cumulative table. A simulated campaign in which all or
    none flag has variance 0, so its index counts as 0. When the cell itself is all or none,
    the index is undefined and the cell is not overdispersed (section 5).
    """
    index = dispersion_index(counts, sizes)
    if index is None:
        return {"defined": False, "index": None, "threshold": None, "overdispersed": False,
                "simulations": 0}
    threshold = _dispersion_threshold(sum(counts), tuple(sizes), simulations, seed, Fraction(quantile))
    return {"defined": True, "index": index, "threshold": threshold, "overdispersed": index > threshold,
            "simulations": simulations}


_threshold_cache = {}


def _dispersion_threshold(total, sizes, simulations, seed, quantile):
    """The simulated quantile of the index; it depends only on these arguments, so it is cached."""
    key = (total, sizes, simulations, seed, quantile)
    if key in _threshold_cache:
        return _threshold_cache[key]
    p = total / sum(sizes)
    tables = {s: _binom_cdf_table(s, p) for s in set(sizes)}
    rng = SplitMix64(seed)
    simulated = []
    for _ in range(simulations):
        xs = [min(bisect_right(tables[s], rng.uniform()), s) for s in sizes]
        d = dispersion_index(xs, sizes)
        simulated.append(0.0 if d is None else d)
    simulated.sort()
    _threshold_cache[key] = nearest_rank(simulated, quantile)
    return _threshold_cache[key]


# ---------------------------------------------------------------------------------------------
# H4: exact sign tests and the pilot power rule (section 7)
# ---------------------------------------------------------------------------------------------


def sign_tail_ge(k, n):
    """P(X >= k) for X ~ Binomial(n, 1/2), as an exact Fraction."""
    k = max(k, 0)
    if k > n:
        return Fraction(0)
    return Fraction(sum(math.comb(n, j) for j in range(k, n + 1)), 2 ** n)


def h4_sign_tests(ratios, low=grid.H4_MARGIN_LOW, high=grid.H4_MARGIN_HIGH):
    """The two one-sided sign tests of section 7, the cell's p, and F4c's cost test.

    p_low tests 'the median ratio is at most low' with the number of pairs above low; p_high
    tests 'the median ratio is at least high' with the number below high; p is their max. The
    cost test (F4c) tests 'the median ratio is at least low' with the number below low.
    """
    n = len(ratios)
    above_low = sum(1 for r in ratios if r > low)
    below_high = sum(1 for r in ratios if r < high)
    below_low = sum(1 for r in ratios if r < low)
    p_low = sign_tail_ge(above_low, n)
    p_high = sign_tail_ge(below_high, n)
    return {"pairs": n, "above_low": above_low, "below_high": below_high, "below_low": below_low,
            "p_low": p_low, "p_high": p_high, "p": max(p_low, p_high), "p_cost": sign_tail_ge(below_low, n)}


def sign_critical(n, level):
    """The least k with P(X >= k | n, 1/2) <= level, or None."""
    for k in range(n + 1):
        if sign_tail_ge(k, n) <= level:
            return k
    return None


def _upper_normal(z):
    """P(Z > z) for a standard normal Z."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def pilot_power(s, pairs, m=grid.FAMILY_M["F4"], alpha=grid.ALPHA,
                low=grid.H4_MARGIN_LOW, high=grid.H4_MARGIN_HIGH):
    """Power of the decision rule at the strictest Holm step, alpha / m.

    The true ratio is 1 and each pair's log ratio is normal with SD s. A pair falls below low,
    inside, or above high; the rule rejects when at most pairs - c fall below and at most
    pairs - c above, c the critical count of one sign test. Summed exactly over the trinomial.
    """
    c = sign_critical(pairs, Fraction(alpha) / m)
    if c is None:
        return 0.0
    if s <= 0:
        return 1.0
    p_below = _upper_normal(-math.log(low) / s)
    p_above = _upper_normal(math.log(high) / s)
    p_inside = 1.0 - p_below - p_above
    allowed = pairs - c
    power = 0.0
    for nb in range(0, min(allowed, pairs) + 1):
        for na in range(0, min(allowed, pairs - nb) + 1):
            ni = pairs - nb - na
            ways = math.factorial(pairs) // (math.factorial(nb) * math.factorial(na) * math.factorial(ni))
            power += ways * p_below ** nb * p_above ** na * p_inside ** ni
    return power


def pilot_choose_r(s):
    """The smallest even R in 14 to 24 with power >= 0.8; else 24, underpowered."""
    for r in range(grid.H4_R_MIN, grid.H4_R_MAX + 1, 2):
        power = pilot_power(s, r)
        if power >= grid.H4_POWER_TARGET:
            return r, power, False
    return grid.H4_R_MAX, pilot_power(s, grid.H4_R_MAX), True


# ---------------------------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------------------------


def jsonable(value):
    """Fractions become floats; tuples become lists; everything else is kept."""
    if isinstance(value, Fraction):
        return float(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value

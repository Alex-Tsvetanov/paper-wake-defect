"""The frozen grid of P1 v2: every cell, its family and its labels, in one place.

Every number here comes from hypotheses-v2.md (frozen 2026-10-27); the section is named next
to it. plan.py, score.py, pilot.py and the tests all read this module, so the plan, the
scoring and Holm's m cannot drift apart.
"""

from fractions import Fraction

# ---------------------------------------------------------------------------------------------
# Constants of the frozen file
# ---------------------------------------------------------------------------------------------

SEED = 20261026  # sections 5 and 8: the order, the bootstrap and the overdispersion check
GAP_SEED_BASE = 2026102700  # section 8: process i of every cell draws its gaps with this + i

SYSTEMS = ("linux", "windows")
BACKENDS = {"linux": ("epoll", "io_uring", "condvar"), "windows": ("iocp", "condvar")}

# The variants of the design note, section 4, in the order of wakeprobe's kAllVariants.
VARIANTS = (
    "none",
    "plain_wake",
    "absent_wake",
    "unobserved_primitive",
    "lazy_wake_race",
    "lazy_wake_wide",
    "wrong_worker",
    "no_retrigger",
    "undrained_edge",
)
FIXED = "none"
PLAIN = "plain_wake"
LAZY_WAKE = ("lazy_wake_race", "lazy_wake_wide")

BOUNDS_US = (1000, 10000)  # section 3: B in {1, 10} ms
WORKER_COUNTS = (1, 2, 4)  # section 3: W in {1, 2, 4}

# Section 2: n = 300 trials per cell, run as 10 processes of 30 trials of 21 units.
PROCESSES_PER_CELL = 10
TRIALS_PER_PROCESS = 30
UNITS_PER_TRIAL = 21
POSTS = TRIALS_PER_PROCESS * UNITS_PER_TRIAL  # 630
TRIALS_PER_CELL = PROCESSES_PER_CELL * TRIALS_PER_PROCESS  # 300
WARMUP = 5  # section 8: 5 warm-up units
CALIBRATE = 11  # section 2: 11 calibration posts per process
GAP_FRAC_RANGE = "0.1:3.0"  # section 2: gaps uniform in [0.1 B, 3.0 B]

# Section 8: placement. Workers on CPU 14, 12 and 14, or 8, 10, 12 and 14; the poster on CPU 2.
WORKER_CPUS = {1: "14", 2: "12,14", 4: "8,10,12,14"}
POSTER_CPU = "2"

# Section 2: the phase sweep, 30 targets 0.05, 0.15, ..., 2.95 B_eff, 21 measured posts,
# 3 warm-up posts, 11 calibration posts, absent wake at W = 1.
SWEEP_STEPS = 30
SWEEP_POSTS = 21
SWEEP_WARMUP = 3
SWEEP_VARIANT = "absent_wake"


def sweep_fracs():
    """The 30 phase targets as text, exactly 0.05 + 0.1 k for k = 0 to 29."""
    return ["%.2f" % (Fraction(5, 100) + Fraction(k, 10)) for k in range(SWEEP_STEPS)]


# Section 3 (H4) and section 7: busy worker W = 1, P = 1, K = 64; N in {0, 1, 10, 100} us;
# 10 s windows, ABBA, 2 pairs per process; arm B is absent wake.
H4_WORK_US = (0, 1, 10, 100)
H4_CONFIRMATORY_WORK_US = (1, 10, 100)  # N = 0 is a disclosed repeat outside F4
H4_ARM_B = "absent_wake"
H4_ARM_A_FIX = "none"  # F4 and F4c: the fixed loop over absent wake
H4_ARM_A_PLAIN = "plain_wake"  # descriptive: plain wake over absent wake, at the same R
H4_COMPARISONS = (H4_ARM_A_FIX, H4_ARM_A_PLAIN)
H4_WORKERS = 1
H4_PRODUCERS = 1
H4_INFLIGHT = 64
H4_PAIRS_PER_PROCESS = 2
H4_WINDOW_MS = 10000
H4_WORKER_CPUS = "14"
H4_PRODUCER_CPUS = "2"
H4_MARGIN_LOW = 0.98
H4_MARGIN_HIGH = 1.02
H4_R_MIN = 14  # section 7: R >= 14; a cell with fewer pairs is incomplete (section 8)
H4_R_MAX = 24  # section 7: the cap
H4_POWER_TARGET = 0.8  # section 7: the pilot rule

# N = 0, outside F4 (section 3): the seen direction is the prediction.
H4_N0_PREDICTION = {
    ("linux", "epoll"): "below 0.98",
    ("linux", "io_uring"): "below 0.98",
    ("windows", "iocp"): "below 0.98",
    ("linux", "condvar"): "within [0.98, 1.02]",
    ("windows", "condvar"): "no prediction",
}

# Section 7: the A/A pilot, the fixed loop against itself, 8 pairs per cell in 4 processes.
PILOT_ARM = "none"
PILOT_PAIRS = 8
PILOT_PROCESSES = PILOT_PAIRS // H4_PAIRS_PER_PROCESS  # 4

# Sections 2, 3 and 5: tests.
ALPHA = Fraction(25, 1000)  # 0.025, one-sided, per family
H1_POWER = Fraction(95, 100)  # H1: T_med flags more than 95% of trials
H2_RATE = Fraction(5, 100)  # H2: T_med flags fewer than 5% of trials
BLIND_RATE = Fraction(5, 100)  # section 4: "blind" means at most 5%, the H2 bound
BLIND_CONFIDENCE = Fraction(95, 100)  # section 4: exact one-sided 95% bounds
INTERVAL_CONFIDENCE = Fraction(95, 100)  # sections 4 and 5: exact 95% interval and clustered interval
T_MED_DIVISOR = 5  # T_med: the median of 21 delays exceeds B_eff / 5
T_COUNT_DIVISOR = 2  # T_count: at least one of 21 delays exceeds B_eff / 2
BEFF_MIN_CALIBRATED = 8  # section 2: fallback if fewer than 8 of 10 processes calibrate
BEFF_MAX_SPAN = Fraction(10, 100)  # section 2: or if their values span more than 10% of the median
H3_CRITERION = 0.05  # section 3: the median of |residual| / B_eff below 0.05
BOOTSTRAP_RESAMPLES = 10000  # section 5
OVERDISPERSION_QUANTILE = Fraction(99, 100)  # section 5: the 99th percentile
# Not in the frozen file: how many binomial campaigns the overdispersion check simulates per
# cell. Chosen equal to the bootstrap's resamples (a choice for the revision log).
OVERDISPERSION_SIMULATIONS = 10000
TIMEOUT_FACTOR = 5  # section 8: timed out after 5 x max(B, B_eff, coarsest timer resolution)

# Section 5: the family sizes, asserted against the grid below.
FAMILY_M = {"F1": 102, "F2": 80, "F4": 15, "F4c": 15}

# ---------------------------------------------------------------------------------------------
# What wakeprobe does with each variant (bench/wakeprobe.cpp and loop/include/wakeloop)
# ---------------------------------------------------------------------------------------------


def has_site(backend, variant):
    """No re-trigger needs a wake registration, which only epoll and io_uring have."""
    if variant == "no_retrigger":
        return backend in ("epoll", "io_uring")
    return True


def min_workers(variant):
    return 2 if variant == "wrong_worker" else 1


def delays_single_posts(variant, workers):
    """wakeprobe's own rule: these variants calibrate B_eff and delay a single post."""
    if variant in ("absent_wake", "unobserved_primitive", "no_retrigger"):
        return True
    if variant == "wrong_worker":
        return workers >= 2
    return False


# ---------------------------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------------------------


def bound_text(bound_us):
    return "%dms" % (bound_us // 1000)


class Cell:
    """One S or Bt cell: system, protocol, backend, variant, W and B."""

    __slots__ = ("system", "protocol", "backend", "variant", "workers", "bound_us")

    def __init__(self, system, protocol, backend, variant, workers, bound_us):
        self.system = system
        self.protocol = protocol
        self.backend = backend
        self.variant = variant
        self.workers = workers
        self.bound_us = bound_us

    @property
    def name(self):
        """Unique within a system and filename-safe, e.g. S-epoll-absent_wake-W1-B1ms."""
        return "%s-%s-%s-W%d-B%s" % (self.protocol, self.backend, self.variant, self.workers,
                                    bound_text(self.bound_us))

    @property
    def key(self):
        """Unique over both systems."""
        return "%s:%s" % (self.system, self.name)

    @property
    def delaying(self):
        """wakeprobe calibrates and delays single posts (protocol S only)."""
        return self.protocol == "S" and delays_single_posts(self.variant, self.workers)

    @property
    def hours_delaying(self):
        """Section 9's 'delaying, for hours' (the widened plant counted as delaying)."""
        if self.protocol == "S":
            return delays_single_posts(self.variant, self.workers) or self.variant == "lazy_wake_wide"
        return self.variant in ("absent_wake", "unobserved_primitive", "no_retrigger", "undrained_edge")

    @property
    def family(self):
        """F1 (H1), F2 (H2) or None (outside the confirmatory families)."""
        if self.protocol == "S":
            if self.variant in ("absent_wake", "unobserved_primitive", "wrong_worker", "no_retrigger"):
                return "F1"
            if self.variant in ("none", "plain_wake"):
                return "F2"
            return None
        if self.variant == "undrained_edge":
            return "F1"
        if self.variant in ("none", "plain_wake"):
            return "F2"
        return None

    @property
    def blind_spot(self):
        """Section 4: the cells where T_med is predicted to be blind."""
        if self.protocol == "S" and self.variant in ("lazy_wake_race", "lazy_wake_wide", "undrained_edge"):
            return "S"
        if self.protocol == "Bt" and self.variant in (
                "absent_wake", "unobserved_primitive", "no_retrigger", "lazy_wake_race", "lazy_wake_wide"):
            return "Bt"
        return None

    @property
    def lazy_wake(self):
        return self.variant in LAZY_WAKE

    @property
    def replication(self):
        """Section 1: 'R' (results seen), 'Rd' (seen through durations only) or None."""
        if self.protocol == "S" and self.workers == 1:
            if self.system == "linux":
                if (self.backend, self.variant) in (("epoll", "absent_wake"), ("io_uring", "unobserved_primitive"),
                                                    ("epoll", "plain_wake"), ("io_uring", "plain_wake")):
                    return "R"
            if self.variant == "absent_wake":
                return "Rd"  # io_uring and condvar on Linux, IOCP and condvar on Windows
            if self.variant == "plain_wake":
                return "Rd"  # condvar on Linux, IOCP and condvar on Windows
            return None
        if self.protocol == "Bt" and self.variant == FIXED:
            return "Rd"  # request 7: the fixed loop under Bt
        return None

    def __repr__(self):
        return "Cell(%s)" % self.key


def s_cells(system):
    """Protocol S: every variant with a site, W in {1, 2, 4} (wrong worker from 2), both bounds."""
    out = []
    for bound_us in BOUNDS_US:
        for backend in BACKENDS[system]:
            for variant in VARIANTS:
                if not has_site(backend, variant):
                    continue
                for workers in WORKER_COUNTS:
                    if workers >= min_workers(variant):
                        out.append(Cell(system, "S", backend, variant, workers, bound_us))
    return out


def bt_cells(system):
    """Protocol Bt: W = 1 only, so no wrong worker; every other variant with a site."""
    out = []
    for bound_us in BOUNDS_US:
        for backend in BACKENDS[system]:
            for variant in VARIANTS:
                if variant == "wrong_worker" or not has_site(backend, variant):
                    continue
                out.append(Cell(system, "Bt", backend, variant, 1, bound_us))
    return out


def all_cells(system):
    return s_cells(system) + bt_cells(system)


def beff_source_cell(system, backend, bound_us):
    """Section 2: the absent-wake, W = 1, S cell that sets B_eff for its backend, system and B."""
    return Cell(system, "S", backend, "absent_wake", 1, bound_us)


def family_cells(family):
    """The keys of every cell of F1 or F2 over both systems, in grid order."""
    return [c.key for system in SYSTEMS for c in all_cells(system) if c.family == family]


# ---------------------------------------------------------------------------------------------
# H3 and H4 cells
# ---------------------------------------------------------------------------------------------


def sweep_name(backend, bound_us):
    return "sweep-%s-B%s" % (backend, bound_text(bound_us))


def sweep_cells(system):
    """H3: one criterion per backend, system and B."""
    return [(backend, bound_us) for bound_us in BOUNDS_US for backend in BACKENDS[system]]


def h4_key(system, backend, work_us):
    """The key of an H4 cell (and of its pilot cell and its R) over both systems."""
    return "%s-%s-N%dus" % (system, backend, work_us)


def h4_cells(system):
    return [(backend, work_us) for backend in BACKENDS[system] for work_us in H4_WORK_US]


def f4_cells():
    """The 15 cells of F4 and F4c: 5 backend-system pairs x N in {1, 10, 100} us."""
    return [h4_key(system, backend, n) for system in SYSTEMS for backend in BACKENDS[system]
            for n in H4_CONFIRMATORY_WORK_US]


def h4_cell_name(backend, work_us, arm_a):
    return "H4-%s-N%dus-%s" % (backend, work_us, arm_a)


def pilot_cell_name(backend, work_us):
    return "pilot-%s-N%dus" % (backend, work_us)

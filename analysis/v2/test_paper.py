"""The paper generators: paper_words, paper_setup, paper_macros, paper_tables and sweep_csv, on synthetic
inputs and, where present, on the committed results."""

import csv
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from fractions import Fraction

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "historical", "netty"))

import grid  # noqa: E402
import paper_macros  # noqa: E402
import paper_setup  # noqa: E402
import paper_tables  # noqa: E402
import paper_words as pw  # noqa: E402
import plan  # noqa: E402
import readers  # noqa: E402
import score  # noqa: E402
import sweep_csv  # noqa: E402
import synth  # noqa: E402

RESULTS = os.path.join(HERE, "..", "..", "results", "v2")
CMAKE = os.path.join(HERE, "..", "..", "tests", "CMakeLists.txt")
WAKE_TESTS = os.path.join(HERE, "..", "..", "tests", "wake_tests.cpp")
# What the paper must never show (glossary.md): variant and backend keys, cell-key parts, labels, dashes.
FORBIDDEN = re.compile(r"absent_wake|plain_wake|wrong_worker|no_retrigger|undrained_edge|lazy_wake|unobserved_primitive"
                       r"|\bcondvar\b|\biocp\b|\bW\d\b|\bB\d+ms\b|\bH\d\b|\bF\d\b|[\u2012-\u2015\u2212]")


def results_present(*names):
    return all(os.path.exists(os.path.join(RESULTS, n)) for n in names)


def load(name):
    with open(os.path.join(RESULTS, name), encoding="utf-8") as f:
        return json.load(f)


def copy(obj):
    return json.loads(json.dumps(obj))


def committed_inputs():
    return {n: load(f) for n, f in (("score", "score.json"), ("h5", "h5.json"), ("h6", "h6.json"),
                                    ("extra", "score-extra-w.json"), ("pilot", "pilot-h4.json"),
                                    ("setup", "setup.json"), ("raw", "raw-facts.json"))}


def committed_sources():
    out = {}
    for key, rel in paper_macros.SOURCES.items():
        with open(os.path.join(HERE, "..", "..", rel), encoding="utf-8") as f:
            out[key] = f.read()
    return out


def committed_sweep():
    with open(os.path.join(RESULTS, "h3-sweep.csv"), encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


class Words(unittest.TestCase):
    def test_macro_names_are_letters_with_numbers_spelled(self):
        self.assertEqual(pw.macro_name("Beff", "linux", "io", "uring", 10), "BeffLinuxIoUringTen")
        self.assertEqual(pw.macro_name("N", 100), "NHundred")
        with self.assertRaises(ValueError):
            pw.macro_name("N", 37)

    def test_thousands_and_small_numbers(self):
        self.assertEqual(pw.thousands(6300), "6{,}300")
        self.assertEqual(pw.thousands(2026102700), "2{,}026{,}102{,}700")
        self.assertEqual(pw.thousands(999), "999")
        self.assertEqual(pw.sci(0.000451), r"\ensuremath{4.5 \times 10^{-4}}")
        self.assertEqual(pw.sci(0.0125), "0.013")
        self.assertEqual(pw.sci(0.000245, 3), r"\ensuremath{2.45 \times 10^{-4}}")
        self.assertEqual(pw.fixed(-0.5, 2), "-0.50")

    def test_a_partial_rate_never_prints_as_all_or_none(self):
        self.assertEqual(pw.rate_text(299 / 300), "0.997")
        self.assertEqual(pw.rate_text(299 / 300, 2), "0.99")
        self.assertEqual(pw.rate_text(1 / 300, 2), "0.01")
        self.assertEqual(pw.rate_text(1.0), "1.000")
        self.assertEqual(pw.rate_text(0.0), "0.000")
        with self.assertRaises(ValueError):
            pw.rate_text(1.5)

    def test_cell_keys_parse_and_read_as_words(self):
        p = pw.cell_parts("windows:Bt-condvar-unobserved_primitive-W1-B10ms")
        self.assertEqual((p["system"], p["protocol"], p["backend"], p["workers"], p["bound_us"]),
                         ("windows", "Bt", "condvar", 1, 10000))
        words = pw.cell_words("linux:S-io_uring-wrong_worker-W4-B1ms")
        self.assertEqual(words, r"Linux, io\_uring, wrong worker, single posts, 4 workers, $B = 1$~ms")
        self.assertIsNone(FORBIDDEN.search(words))
        with self.assertRaises(ValueError):
            pw.cell_parts("linux:S-epoll-made_up-W1-B1ms")


class BasisOfN(unittest.TestCase):
    """Section 2: at the strictest Holm step a cell passes with up to 3 misses (p = 1.64e-4), at the
    last step with up to 7."""

    def test_the_frozen_basis(self):
        strict = grid.ALPHA / grid.FAMILY_M["F1"]
        misses, pv = paper_macros.tolerated(grid.TRIALS_PER_CELL, grid.H1_POWER, strict)
        self.assertEqual(misses, 3)
        self.assertAlmostEqual(float(pv), 1.64e-4, delta=0.01e-4)
        self.assertEqual(paper_macros.tolerated(grid.TRIALS_PER_CELL, grid.H1_POWER, grid.ALPHA)[0], 7)

    def test_the_tail_is_exact(self):
        self.assertEqual(paper_macros.binomial_tail_ge(2, 2, Fraction(1, 2)), Fraction(1, 4))


TESTBED = ("**Testbed.** The laptop (ASUS Example, BIOS X.1, AMD Ryzen 7 5800H, 8 cores and\n16 logical CPUs, core k "
           "holding CPUs 2k and 2k + 1) dual-boots Arch Linux (kernel 9.9.9-arch1-1,\nclang 1.2.3) and Windows 11 Pro "
           "(build 1.2, MSVC cl 4.5.6), as read on 2026-01-02\n")
BUILD = "-- The CXX compiler identification is Clang 1.2.3\n-- bench: repo %s dirty=0, code %s\n" % ("a" * 40, "b" * 40)
BUILD_W = BUILD.replace("Clang 1.2.3", "MSVC 4.5.6.0")


class Setup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p1v2-setup-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def record(self, name, **over):
        r = {"host_tag": "L", "sanitizer": "asan", "compiler": "Clang 1.2.3", "commit": "c" * 40, "code_commit": "b" * 40,
             "tests_total": 10, "tests_failed": 0, "smoke": {"ran": 3, "failed": 0}, "sanitizer_reports": 0,
             "green": True}
        r.update(over)
        return self.write(name, json.dumps(r))

    def gather(self, kernel="9.9.9-arch1-1", build_w=BUILD_W, record=None):
        return paper_setup.gather(self.write("k.json", json.dumps({"kernel": kernel, "at": "t"})),
                                  self.write("bl.log", BUILD), self.write("bw.log", build_w),
                                  [record or self.record("r.json")], self.write("h.md", TESTBED))

    def test_facts_from_the_records(self):
        f = self.gather()
        self.assertEqual(f["laptop"]["model"], "ASUS Example")
        self.assertEqual(f["laptop"]["logical_cpus"], 16)
        self.assertEqual(f["windows"]["build"], "1.2")
        self.assertEqual(f["linux"]["kernel"], "9.9.9-arch1-1")
        self.assertEqual(set(f["sources"]), {"kernel", "build_log_linux", "build_log_windows", "hypotheses", "r.json"})

    def test_refusals(self):
        with self.assertRaises(paper_setup.Refusal):
            self.gather(kernel="1.0")
        with self.assertRaises(paper_setup.Refusal):
            self.gather(build_w=BUILD_W.replace("dirty=0", "dirty=1"))
        with self.assertRaises(paper_setup.Refusal):
            self.gather(build_w=BUILD_W.replace("a" * 40, "e" * 40))
        with self.assertRaises(paper_setup.Refusal):
            self.gather(build_w=BUILD_W.replace("4.5.6.0", "4.5.7.0"))  # not the testbed's MSVC
        with self.assertRaises(paper_setup.Refusal):
            self.gather(record=self.record("red.json", green=False))
        with self.assertRaises(paper_setup.Refusal):
            self.gather(record=self.record("other.json", code_commit="f" * 40))
        with self.assertRaises(paper_setup.Refusal):
            self.gather(record=self.record("nosmoke.json", smoke={}))
        with self.assertRaises(paper_setup.Refusal):
            self.gather(record=self.record("failsmoke.json", smoke={"ran": 3, "failed": 1}))

    def test_line_endings_do_not_change_a_hash(self):
        a = self.write("lf.txt", "x\ny\n")
        with open(os.path.join(self.tmp, "crlf.txt"), "wb") as f:
            f.write(b"x\r\ny\r\n")
        self.assertEqual(paper_setup.sha256(a), paper_setup.sha256(os.path.join(self.tmp, "crlf.txt")))


class Macros(unittest.TestCase):
    def test_a_name_defined_twice_is_refused(self):
        m = paper_macros.Macros()
        m.add("Alpha", 1)
        with self.assertRaises(paper_macros.Refusal):
            m.add("Alpha", 2)
        with self.assertRaises(paper_macros.Refusal):
            m.add("Alpha1", 2)

    def test_timer_reading_from_a_problem(self):
        cell = {"cell": "x", "processes": [{"problem": None}, {
            "problem": "timer resolution at the start is 100000 (100 ns), not the default of 15625 us"}]}
        self.assertEqual(paper_macros.timer_reading(cell), (10.0, 15.625))
        with self.assertRaises(paper_macros.Refusal):
            paper_macros.timer_reading({"cell": "y", "processes": [{"problem": "other"}]})

    def test_values_must_be_ints_or_safe_tex(self):
        m = paper_macros.Macros()
        for bad in (None, True, 1.5, [1], "a_b", "a--b", "50%", ""):
            with self.assertRaises(paper_macros.Refusal):
                m.add("Beta", bad)
        m.add("Gamma", r"io\_uring")

    @unittest.skipUnless(results_present("score.json", "h5.json", "h6.json", "score-extra-w.json", "pilot-h4.json",
                                         "setup.json", "raw-facts.json", "h3-sweep.csv", "macros.tex"),
                         "committed results not present")
    def test_the_committed_results(self):
        inputs = committed_inputs()
        sources = committed_sources()
        text = paper_macros.build(inputs, sources, committed_sweep())
        with open(os.path.join(RESULTS, "macros.tex"), encoding="utf-8", newline="") as f:
            self.assertEqual(text, f.read(), "results/v2/macros.tex is not what the generator makes now")
        names = re.findall(r"\\newcommand\{\\([A-Za-z]+)\}", text)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), text.count("\\newcommand"))
        values = re.findall(r"\\newcommand\{\\[A-Za-z]+\}\{(.*)\}(?:  %.*)?$", text, re.M)
        for v in values:
            self.assertIsNone(FORBIDDEN.search(v), v)
        for line in ("\\newcommand{\\PowerCells}{102}", "\\newcommand{\\NettyForcedBuggyFlags}{150}",
                     "\\newcommand{\\DetectorBoundMs}{200}", "\\newcommand{\\BatchTasks}{192}",
                     "\\newcommand{\\PowerLowestRate}{0.990}", "\\newcommand{\\SemanticsTests}{12}",
                     "\\newcommand{\\BatchTimedWaits}{2}", "\\newcommand{\\LazyTailMostFlags}{77}"):
            self.assertIn(line, text)

    @unittest.skipUnless(results_present("score.json", "h5.json", "h6.json", "score-extra-w.json", "pilot-h4.json",
                                         "setup.json", "raw-facts.json", "h3-sweep.csv"), "committed results not present")
    def test_refusals_on_the_committed_results(self):
        inputs, sources, sweep = committed_inputs(), committed_sources(), committed_sweep()

        def refused(sweep_rows=None, **changed):
            with self.assertRaises(paper_macros.Refusal):
                paper_macros.build(dict(inputs, **changed), sources, sweep if sweep_rows is None else sweep_rows)

        refused(h6=dict(inputs["h6"], draft=True))
        bad = copy(inputs["score"])
        bad["H1"]["m"] = 101
        refused(score=bad)
        setup = copy(inputs["setup"])
        setup["linux"]["bench_commit"] = "f" * 40
        refused(setup=setup)
        extra = copy(inputs["extra"])
        extra["provenance"]["windows"]["bench_commits"] = {"f" * 40: 10}
        refused(extra=extra)
        bad = copy(inputs["score"])
        bad["H4"]["fixed_over_absent"][1]["complete"] = False
        refused(score=bad)
        bad = copy(inputs["score"])
        key = sorted(bad["incomplete_cells"])[0]
        for p in bad["cells"][key]["processes"]:
            if p["problem"]:
                p["problem"] = p["problem"].replace("is 100000 (100 ns)", "is 50000 (100 ns)")
        refused(score=bad)
        h6 = copy(inputs["h6"])
        h6["cells"]["cr7-forced"]["diagnostics"]["wakenup_at_hook"] = {"0": 1, "1": 3149}
        refused(h6=h6)
        raw = copy(inputs["raw"])
        key = sorted(k for k, v in raw["processes"].items() if v["family"] == "F1")[0]
        raw["processes"][key]["flags"][0] -= 1
        refused(raw=raw)
        raw = copy(inputs["raw"])
        raw["prior_bound"] = raw["prior_bound"][1:]
        refused(raw=raw)
        first = (sweep[0]["system"], sweep[0]["backend"], sweep[0]["bound_us"])
        moved = [dict(r, residual_ns=str(3 * int(r["residual_ns"])))
                 if (r["system"], r["backend"], r["bound_us"]) == first else r for r in sweep]
        refused(sweep_rows=moved)
        whole = [dict(r) for r in sweep]
        whole[0]["phase_target"] = "1.00"
        refused(sweep_rows=whole)
        raw = copy(inputs["raw"])
        raw["prior_design"]["linux"] = [p for p in raw["prior_design"]["linux"] if p["design"] != "matrix"]
        refused(raw=raw)
        raw = copy(inputs["raw"])
        row = next(p for p in raw["prior_design"]["windows"] if p["design"] == "bound-sweep")
        row["gap_us"] = "3000"
        refused(raw=raw)
        raw = copy(inputs["raw"])
        row = next(p for p in raw["prior_design"]["linux"] if p["design"] == "matrix" and p["arm"] == "defect")
        row["arm"] = "fixed"
        refused(raw=raw)

    @unittest.skipUnless(results_present("score.json", "h5.json", "h6.json", "score-extra-w.json", "pilot-h4.json",
                                         "setup.json", "raw-facts.json", "h3-sweep.csv"), "committed results not present")
    def test_round_four_macros(self):
        text = paper_macros.build(committed_inputs(), committed_sources(), committed_sweep())
        for line in ("\\newcommand{\\PriorSweepSteps}{30}", "\\newcommand{\\PriorBoundSweepGapMs}{2}",
                     "\\newcommand{\\PriorMatrixGapFactor}{1.7}", "\\newcommand{\\SawtoothPhaseEdge}{3}"):
            self.assertIn(line, text)
        posts = re.findall(r"\\newcommand\{\\SawtoothPosts(?:Linux|Windows)[A-Za-z]+\}\{(\d+)\}", text)
        self.assertEqual(len(posts), 10)

    @unittest.skipUnless(results_present("score.json", "h5.json", "h6.json", "score-extra-w.json", "pilot-h4.json",
                                         "setup.json", "raw-facts.json", "h3-sweep.csv"), "committed results not present")
    def test_round_five_refusals(self):
        inputs = committed_inputs()
        bad = copy(inputs)
        bad["raw"]["netty_posts"][0]["over_half_b"] += 1
        with self.assertRaises(paper_macros.Refusal):
            paper_macros.netty_clustered(paper_macros.Macros(), bad["raw"], bad["h6"])
        bad = copy(inputs)
        key = sorted(bad["raw"]["trial_medians"])[0]
        bad["raw"]["trial_medians"][key]["processes"][0]["flags_b_eff"] += 1
        with self.assertRaises(paper_macros.Refusal):
            paper_macros.requested_bound(paper_macros.Macros(), bad["score"], bad["raw"])
        bad = copy(inputs)
        del bad["raw"]["trial_medians"][key]
        with self.assertRaises(paper_macros.Refusal):
            paper_macros.requested_bound(paper_macros.Macros(), bad["score"], bad["raw"])

    def test_trial_median_ratios_on_two_cells(self):
        def cell(family, pooled, values):
            return {"family": family, "complete": True, "b_eff_ns": 100,
                    "median_trial_median_ns": pooled,
                    "processes": [{"median_trial_median_ns": v} for v in values]}
        score = {"cells": {"a": {"family": "F1", "complete": True}, "b": {"family": "F2", "complete": True}}}
        raw = {"trial_medians": {"a": cell("F1", 50, [45, 20]), "b": cell("F2", 1, [2, 30])}}
        m = paper_macros.Macros()
        paper_macros.trial_median_ratios(m, score, raw)
        text = m.text()
        self.assertIn("\\newcommand{\\TrialMedianPowerLowestProcess}{0.2000}", text)
        self.assertIn("\\newcommand{\\TrialMedianPowerProcessesBelow}{1}", text)
        self.assertIn("\\newcommand{\\TrialMedianFalseHighestProcess}{0.3000}", text)
        self.assertIn("\\newcommand{\\TrialMedianFalseProcessesAbove}{1}", text)

    def test_round_six_macros_on_small_cells(self):
        score = {"cells": {
            "a": {"family": "F1", "complete": True, "protocol": "S", "variant": "absent_wake", "blind_spot": None,
                  "replication": None, "t_med": {"flags": 3}},
            "b": {"family": "F2", "complete": True, "protocol": "S", "variant": "none", "blind_spot": None,
                  "replication": "Rd", "t_med": {"flags": 0}},
            "c": {"family": None, "complete": True, "protocol": "S", "variant": "undrained_edge", "blind_spot": "S",
                  "replication": None, "t_med": {"flags": 0}},
            "d": {"family": None, "complete": False, "protocol": "S", "variant": "lazy_wake_wide", "blind_spot": "S",
                  "replication": None, "t_med": {"flags": 0}}}}

        def tm(protocol, family, complete, flags_requested, trials):
            return {"protocol": protocol, "family": family, "complete": complete,
                    "processes": [{"flags_requested": flags_requested, "trials": trials}]}
        raw = {"trial_medians": {"a": tm("S", "F1", True, 3, 4), "b": tm("S", "F2", True, 1, 4),
                                 "c": tm("S", None, True, 2, 4), "d": tm("S", None, False, 1, 4)},
               "divisor_flags": {
                   "a": {"family": "F1", "complete": True, "protocol": "S",
                         "processes": [{"trials": 4, "flags": {"2": 1, "4": 3, "5": 3, "10": 4}}]},
                   "b": {"family": "F2", "complete": True, "protocol": "S",
                         "processes": [{"trials": 4, "flags": {"2": 0, "4": 0, "5": 0, "10": 2}}]},
                   "c": {"family": None, "complete": True, "protocol": "S",
                         "processes": [{"trials": 4, "flags": {"2": 0, "4": 0, "5": 0, "10": 0}}]},
                   "d": {"family": None, "complete": False, "protocol": "S",
                         "processes": [{"trials": 4, "flags": {"2": 4, "4": 4, "5": 0, "10": 4}}]}},
               "calibrated_periods": {
                   "a": {"b_eff_source_cell": True, "system": "linux", "bound_ns": 1000000, "b_eff_ns": 100,
                         "processes": 1, "periods_ns": [100]},
                   "e": {"b_eff_source_cell": False, "system": "linux", "bound_ns": 1000000, "b_eff_ns": 100,
                         "processes": 3, "periods_ns": [98, 103]}}}
        m = paper_macros.Macros()
        paper_macros.requested_split(m, score, raw)
        paper_macros.divisor_recount(m, score, raw)
        text = m.text()
        for line in ("\\RequestedBlindCellsChanged}{1}", "\\RequestedChangedUndrained}{1}",
                     "\\RequestedChangedFixed}{1}", "\\RequestedFalseChangedSeen}{1}",
                     "\\RequestedIncompleteCellsChanged}{1}", "\\RequestedBlindHighestRate}{0.500}",
                     "\\RecountSmallPowerLowestRate}{0.250}", "\\RecountLargeFalseFlags}{2}",
                     "\\RecountLargeFalseHighestRate}{0.500}", "\\RecountSmallBlindFlags}{0}"):
            self.assertIn(line, text)
        with self.assertRaises(paper_macros.Refusal):  # one system and bound only: the others have no period
            paper_macros.calibrated_periods(paper_macros.Macros(), raw)
        for system in ("linux", "windows"):
            for bound in (1000000, 10000000):
                raw["calibrated_periods"]["%s%d" % (system, bound)] = {
                    "b_eff_source_cell": False, "system": system, "bound_ns": bound, "b_eff_ns": 100,
                    "processes": 1, "periods_ns": [100]}
        m = paper_macros.Macros()
        paper_macros.calibrated_periods(m, raw)
        text = m.text()
        self.assertIn("\\CalibratedLowestLinuxOneMs}{0.9800}", text)
        self.assertIn("\\CalibratedHighestAll}{1.0300}", text)
        self.assertIn("\\CalibratedMissing}{1}", text)

    def test_sawtooth_counts_on_a_small_sweep(self):
        rows = []
        for system, backend in pw.COLUMNS:
            for bound in (1000, 10000):
                rows.append({"system": system, "backend": backend, "bound_us": str(bound), "b_eff_ns": "100",
                             "phase_ns": "350", "delay_ns": "50", "residual_ns": "0"})
                rows.append({"system": system, "backend": backend, "bound_us": str(bound), "b_eff_ns": "100",
                             "phase_ns": "50", "delay_ns": "110", "residual_ns": "-60"})
        m = paper_macros.Macros()
        paper_macros.sawtooth_outliers(m, rows)
        text = m.text()
        self.assertIn("\\newcommand{\\SawtoothPastPhaseLinuxEpollOneMs}{1}", text)
        self.assertIn("\\newcommand{\\SawtoothAboveOneLinuxEpollOneMs}{1}", text)
        self.assertIn("\\newcommand{\\SawtoothOverLimitLinuxEpollOneMs}{1}", text)
        self.assertIn("\\newcommand{\\SawtoothLargestResidualLinuxEpollOneMs}{0.6000}", text)
        with self.assertRaises(paper_macros.Refusal):
            paper_macros.sawtooth_outliers(paper_macros.Macros(), rows[:-2])

    def test_a_semantics_constant_not_found_is_refused(self):
        with open(CMAKE, encoding="utf-8") as f:
            cmake = f.read()
        with open(WAKE_TESTS, encoding="utf-8") as f:
            tests = f.read()
        paper_macros.semantics(paper_macros.Macros(), cmake, tests)
        with self.assertRaises(paper_macros.Refusal):
            paper_macros.semantics(paper_macros.Macros(), cmake, tests.replace("// S10:", "// S10 gone:"))

    def test_the_detectors_must_share_one_bound(self):
        text = ("// D5: a\nconstexpr auto kBound = 200ms;\n// D9: b\nconstexpr auto kBound = 200ms;\n"
                "// D10: c\nconstexpr auto kBound = 100ms;\n")
        with self.assertRaises(paper_macros.Refusal):
            paper_macros.detectors(paper_macros.Macros(), text)


class Tables(unittest.TestCase):
    def cmake(self):
        with open(CMAKE, encoding="utf-8") as f:
            return f.read()

    def test_the_verdict_table_parses(self):
        v = paper_tables.parse_verdicts(self.cmake())
        self.assertEqual(v["absent_wake"][0], "D")
        self.assertEqual(v["none"], ["P"] * len(paper_tables.DETECTORS))

    def test_an_unknown_detector_list_is_refused(self):
        text = self.cmake().replace("detect_latency_forced)", "detect_latency_forced detect_new)")
        with self.assertRaises(paper_tables.Refusal):
            paper_tables.parse_verdicts(text)

    def test_a_row_must_fill_its_columns(self):
        with self.assertRaises(paper_tables.Refusal):
            paper_tables.body(["a", "b"], [["only one"]])

    @unittest.skipUnless(results_present("score.json", "h5.json", "h6.json", "score-extra-w.json", "h3-sweep.csv",
                                         "raw-facts.json"), "committed results not present")
    def test_the_committed_results(self):
        inputs = {"score": load("score.json"), "h5": load("h5.json"), "h6": load("h6.json"),
                  "extra": load("score-extra-w.json"), "raw": load("raw-facts.json")}
        tables = paper_tables.build(inputs, self.cmake(), committed_sweep())
        self.assertEqual(len(tables), 18)
        for name, text in tables.items():
            with open(os.path.join(RESULTS, "tables", name + ".tex"), encoding="utf-8", newline="") as f:
                self.assertEqual(text, f.read(), "results/v2/tables/%s.tex is not what the generator makes" % name)
            rows = [line for line in text.splitlines() if not line.startswith("%")]
            ncols = text.splitlines()[1].count(";") + 1
            for r in rows:
                self.assertEqual(r.count(" & ") + 1, ncols, (name, r))
                self.assertIsNone(FORBIDDEN.search(r), (name, r))
        self.assertEqual(len([line for line in tables["netty"].splitlines() if not line.startswith("%")]), 4)
        self.assertIn("0.997", tables["detection-single"])  # 299 of 300 is not printed as 1.000
        self.assertEqual(len([line for line in tables["throughput"].splitlines() if not line.startswith("%")]), 20)
        raw = copy(inputs["raw"])
        raw["h4_throughput"]["linux-epoll-N1us-none"]["windows"] = 13
        with self.assertRaises(paper_tables.Refusal):
            paper_tables.build(dict(inputs, raw=raw), self.cmake(), committed_sweep())

    def test_a_sweep_process_with_two_beff_values_is_refused(self):
        rows = [{"system": "linux", "backend": "epoll", "bound_us": "1000", "step": "0", "b_eff_ns": "5"},
                {"system": "linux", "backend": "epoll", "bound_us": "1000", "step": "0", "b_eff_ns": "6"}]
        with self.assertRaises(paper_tables.Refusal):
            paper_tables.sweep_beff(rows)


class Sweep(unittest.TestCase):
    B = 1000000

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p1v2-sweep-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def resolved(self, backend, bound_us, residual, lose_in=None):
        """A sweep whose posts all have this residual, except that process k's residuals are residual + k,
        and process lose_in (if given) loses a unit, so it is not good."""
        entries = [e for e in plan.sweep_entries("linux") if e["cell"] == grid.sweep_name(backend, bound_us)]
        for k, e in enumerate(entries):
            res = [residual + k * 7 + i for i in range(grid.SWEEP_POSTS)]
            synth.write_process(self.tmp, e, first=synth.wakeprobe_output(
                e, [self.B // 2] * grid.SWEEP_POSTS, self.B, residuals=res, lose_at=3 if k == lose_in else None))
        folder = readers.DataDir(self.tmp)
        return [readers.Resolved(e, folder, "linux") for e in entries]

    def test_rows_and_median_match_score_sweep(self):
        resolved = self.resolved("epoll", 1000, -1234, lose_in=5)
        self.assertFalse(resolved[5].ok)
        rows, med = sweep_csv.sweep_rows("linux", "epoll", 1000, resolved)
        self.assertEqual(len(rows), (grid.SWEEP_STEPS - 1) * grid.SWEEP_POSTS)
        self.assertNotIn(5, {r["step"] for r in rows})
        self.assertEqual(med, score.score_sweep("linux", "epoll", 1000, resolved)["median_abs_residual_over_b_eff"])
        self.assertEqual(rows[0]["residual_ns"], -1234)
        self.assertEqual(sweep_csv.to_csv(rows[:1]).splitlines()[0], ",".join(sweep_csv.COLUMNS))

    def collect_with(self, shift):
        """Every Linux sweep, each scored as score_sweep scores it; the first one's scored value moved by shift."""
        sweeps = grid.sweep_cells("linux")
        groups = {("sweep", grid.sweep_name(b, u)): self.resolved(b, u, 500 + 1000 * n) for n, (b, u) in enumerate(sweeps)}
        results = [dict(score.score_sweep("linux", b, u, groups[("sweep", grid.sweep_name(b, u))]), complete=True)
                   for b, u in sweeps]
        results[0]["median_abs_residual_over_b_eff"] += shift
        real = score.resolve_plan
        score.resolve_plan = lambda p, d: ({"system": "linux"}, groups, [])
        try:
            return sweep_csv.collect([("plan", "dir")], {"H3": {"results": results}})
        finally:
            score.resolve_plan = real

    def test_every_sweep_equal_to_its_scored_value_passes(self):
        rows = self.collect_with(0)
        self.assertEqual(len(rows), len(grid.sweep_cells("linux")) * grid.SWEEP_STEPS * grid.SWEEP_POSTS)

    def test_a_median_other_than_the_scored_one_is_refused(self):
        with self.assertRaises(sweep_csv.Refusal):
            self.collect_with(1 / self.B)


if __name__ == "__main__":
    unittest.main()

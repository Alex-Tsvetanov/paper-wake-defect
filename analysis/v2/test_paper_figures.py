"""paper_figures.py on synthetic inputs: the three PDFs, their bytes, their text, the refusals; and on the real results."""

import contextlib
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import matplotlib.pyplot as plt  # noqa: E402
import paper_figures as pf  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "paper_figures.py")
REAL_SCORE = os.path.normpath(os.path.join(HERE, "..", "..", "results", "v2", "score.json"))
REAL_SWEEP = os.path.normpath(os.path.join(HERE, "..", "..", "results", "v2", "h3-sweep.csv"))
PDFTOTEXT = shutil.which("pdftotext") or ("/mingw64/bin/pdftotext" if os.path.exists("/mingw64/bin/pdftotext")
                                          else None)

FORBIDDEN_CHARS = [chr(c) for c in range(0x2012, 0x2016)] + ["\u2212"]
FORBIDDEN_WORDS = ["absent_wake", "condvar", "W1", "B1ms"]
INTERNAL = re.compile(r"\b(?:Bt|W\d+|B\d+ms|F\d+|H\d+|[LWS])\b")
RATE = re.compile(r"(?<![\d.])[01]\.\d\d\d(?![\d.])")
INCOMPLETE = re.compile(r"inc\.(?!:)")


def ligature_maps(pdf):
    """ToUnicode entries of a PDF (streams inflated) that map one glyph to fi, ff or fl."""
    found = []
    for raw in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S):
        try:
            text = zlib.decompress(raw)
        except zlib.error:
            text = raw
        if b"begincmap" in text:
            found += re.findall(rb"<00660069>|<00660066>|<0066006c>", text, re.I)
    return found


def cell(system, protocol, backend, variant, workers, ms, rate, complete=True):
    key = "%s:%s-%s-%s-W%d-B%dms" % (system, protocol, backend, variant, workers, ms)
    t_med = {"flags": int(rate * 300), "trials": 300 if complete else 270, "rate": rate}
    return key, {"system": system, "protocol": protocol, "backend": backend, "variant": variant,
                 "workers": workers, "bound_us": ms * 1000, "complete": complete, "t_med": t_med}


def synthetic_score():
    cells = dict([
        cell("linux", "S", "epoll", "none", 1, 1, 0.0),
        cell("linux", "S", "epoll", "none", 2, 1, 0.0),
        cell("linux", "S", "epoll", "none", 1, 10, 0.0),
        cell("windows", "S", "iocp", "none", 1, 1, 0.0),
        cell("linux", "S", "epoll", "absent_wake", 1, 1, 1.0),
        cell("linux", "S", "io_uring", "absent_wake", 1, 10, 0.99),
        cell("windows", "S", "condvar", "absent_wake", 1, 1, 1.0),
        cell("linux", "S", "epoll", "wrong_worker", 2, 1, 1.0),
        cell("windows", "S", "iocp", "wrong_worker", 2, 1, 1.0, complete=False),
        cell("linux", "S", "epoll", "no_retrigger", 1, 1, 1.0),
        cell("linux", "Bt", "epoll", "absent_wake", 1, 1, 0.0),
        cell("linux", "Bt", "io_uring", "undrained_edge", 1, 10, 0.5),
        cell("windows", "Bt", "condvar", "lazy_wake_wide", 1, 10, 0.0, complete=False),
    ])
    rows = []
    for system, backend in pf.PAIRS:
        for n in pf.WORK_US:
            ratio = 0.7 if n == 0 else 1.0
            rows.append({"key": "%s-%s-N%dus" % (system, backend, n), "system": system, "backend": backend,
                         "work_us": n, "complete": True, "median_ratio": ratio,
                         "clustered_95_mean_ratio": [ratio - 0.01, ratio + 0.01], "pairs": 14})
    return {"cells": cells, "H4": {"fixed_over_absent": rows}}


def synthetic_sweep_rows():
    rows = []
    for system, backend in pf.PAIRS:
        for bound_us in pf.BOUNDS_US:
            b_eff = bound_us * 1000 + 7000
            for index, frac in enumerate((0.1, 0.45, 1.3, 2.7, 3.2)):
                phase = int(frac * b_eff)
                delay = b_eff - phase % b_eff + (index % 2) * 500
                rows.append({"system": system, "backend": backend, "bound_us": bound_us, "step": index,
                             "phase_target": frac, "index": index, "phase_ns": phase, "delay_ns": delay,
                             "residual_ns": 0, "b_eff_ns": b_eff})
    return rows


def write_sweep(path, rows, columns=pf.SWEEP_COLUMNS):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(columns)
        for r in rows:
            w.writerow([r[c] for c in columns])


def pdf_text(path):
    out = subprocess.run([PDFTOTEXT, "-enc", "UTF-8", path, "-"], capture_output=True, check=True)
    return out.stdout.decode("utf-8")


def fix_points(fig):
    """Per panel: ([(x, y, filled)] of the drawn medians, [(lo, hi)] of the drawn intervals)."""
    panels = []
    for ax in fig.axes:
        medians = [(l.get_xdata()[0], l.get_ydata()[0], l.get_markerfacecolor() != "white")
                   for l in ax.lines if l.get_gid() == "median"]
        bars = [tuple(l.get_xdata()) for l in ax.lines if l.get_gid() == "interval"]
        panels.append((medians, bars))
    return panels


def run_main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = pf.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="paper-figures-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.score_path = os.path.join(self.tmp, "score.json")
        self.sweep_path = os.path.join(self.tmp, "sweep.csv")
        self.out = os.path.join(self.tmp, "out")
        self.write_score(synthetic_score())
        write_sweep(self.sweep_path, synthetic_sweep_rows())

    def write_score(self, score):
        with open(self.score_path, "w", encoding="utf-8") as fh:
            json.dump(score, fh)

    def args(self, out=None):
        return ("--score", self.score_path, "--sweep", self.sweep_path, "--out-dir", out or self.out)

    def assert_refused(self):
        code, out, err = run_main(*self.args())
        self.assertEqual(code, 2, err)
        self.assertTrue(err.strip())
        self.assertEqual(out, "")
        self.assertFalse(os.path.exists(self.out), "a refusal must write nothing")
        return err


class Helpers(unittest.TestCase):
    def test_cell_key_parses_every_part(self):
        self.assertEqual(pf.parse_cell_key("linux:S-epoll-absent_wake-W1-B1ms"),
                         ("linux", "S", "epoll", "absent_wake", 1, 1000))
        self.assertEqual(pf.parse_cell_key("windows:Bt-iocp-none-W1-B10ms"),
                         ("windows", "Bt", "iocp", "none", 1, 10000))
        self.assertEqual(pf.parse_cell_key("linux:S-io_uring-wrong_worker-W4-B10ms")[2:5],
                         ("io_uring", "wrong_worker", 4))

    def test_cell_key_refusals(self):
        for key in ("linux:S-epoll-absent_wake-W1", "mac:S-epoll-none-W1-B1ms", "linux:X-epoll-none-W1-B1ms",
                    "linux:S-epoll-none-W1-B5ms", "linux:S-epoll-nonsense-W1-B1ms", "windows:S-epoll-none-W1-B1ms",
                    "linux:S-iocp-none-W1-B1ms", "", None):
            with self.assertRaises(pf.Refusal, msg=repr(key)):
                pf.parse_cell_key(key)

    def test_closed_form_is_broken_at_every_integer(self):
        segments = pf.closed_form_segments(3.5)
        self.assertEqual(len(segments), 4)
        for k, ((x0, y0), (x1, y1)) in enumerate(segments):
            self.assertEqual((x0, y0, x1, y1), (k, 1.0, k + 1, 0.0))

    def test_detection_rows_follow_the_paper_order_and_the_workers_that_exist(self):
        cells = pf.read_cells(synthetic_score())
        self.assertEqual(pf.detection_rows(cells, "S"),
                         [("none", 1), ("none", 2), ("absent_wake", 1), ("wrong_worker", 2), ("no_retrigger", 1)])
        self.assertEqual(pf.detection_rows(cells, "Bt"), [("absent_wake", 1), ("undrained_edge", 1),
                                                          ("lazy_wake_wide", 1)])

    def test_incomplete_cell_has_no_rate(self):
        cells = pf.read_cells(synthetic_score())
        self.assertIsNone(cells[("S", "wrong_worker", 2, "windows", "iocp", 1000)])
        self.assertEqual(cells[("S", "absent_wake", 1, "linux", "epoll", 1000)], 1.0)


class Output(Fixture):
    def test_three_pdfs_are_written_and_their_hashes_printed(self):
        code, out, err = run_main(*self.args())
        self.assertEqual(code, 0, err)
        for name in pf.OUTPUT_NAMES:
            path = os.path.join(self.out, name)
            with open(path, "rb") as fh:
                data = fh.read()
            self.assertTrue(data.startswith(b"%PDF"), name)
            self.assertIn(pf.hashlib.sha256(data).hexdigest(), out)
            self.assertNotIn(b"/CreationDate", data)
            self.assertNotIn(b"/Producer", data)

    def test_two_runs_in_two_processes_give_identical_bytes(self):
        dirs = []
        for seed in ("1", "2"):
            d = os.path.join(self.tmp, "run" + seed)
            env = dict(os.environ, PYTHONHASHSEED=seed)
            done = subprocess.run([sys.executable, "-B", SCRIPT, *self.args(d)], capture_output=True, env=env)
            self.assertEqual(done.returncode, 0, done.stderr.decode())
            dirs.append(d)
        for name in pf.OUTPUT_NAMES:
            with open(os.path.join(dirs[0], name), "rb") as a, open(os.path.join(dirs[1], name), "rb") as b:
                self.assertEqual(a.read(), b.read(), name)

    @unittest.skipUnless(PDFTOTEXT, "pdftotext is not on PATH")
    def test_figure_text_has_the_papers_words_only(self):
        self.assertEqual(run_main(*self.args())[0], 0)
        texts = {}
        for name in pf.OUTPUT_NAMES:
            texts[name] = pdf_text(os.path.join(self.out, name))
            self.assertGreater(len(texts[name].strip()), 20, name)
            for ch in FORBIDDEN_CHARS:
                self.assertNotIn(ch, texts[name], "%s has U+%04X" % (name, ord(ch)))
            for word in FORBIDDEN_WORDS:
                self.assertNotIn(word, texts[name], name)
            self.assertIsNone(INTERNAL.search(texts[name]), "%s: %r" % (name, INTERNAL.search(texts[name])))
        self.assertIn("absent wake", texts["detection.pdf"])
        self.assertIn("share of trials flagged", texts["detection.pdf"])
        self.assertIn("Linux, epoll, B = 1 ms", texts["sawtooth.pdf"])
        self.assertIn("Windows, condition variable, B = 10 ms", texts["sawtooth.pdf"])
        self.assertIn("phase / effective bound", texts["sawtooth.pdf"])
        self.assertIn("delay / effective bound", texts["sawtooth.pdf"])
        self.assertIn("throughput ratio, fixed loop over absent wake", texts["fixcost.pdf"])
        self.assertIn("Linux, io_uring, N = 10 \u03bcs", texts["fixcost.pdf"])
        self.assertIn("Windows, condition variable, N = 0 \u03bcs", texts["fixcost.pdf"])
        self.assertIn("confirmatory (N = 1, 10, 100 \u03bcs)", texts["fixcost.pdf"])
        self.assertIn("outside the family", texts["fixcost.pdf"])  # the right panel's title, on two lines
        self.assertIn("median pair ratio, N = 0, outside the family", texts["fixcost.pdf"])  # legend
        self.assertIn("clustered 95% interval of the mean pair ratio", texts["fixcost.pdf"])

    @unittest.skipUnless(PDFTOTEXT, "pdftotext is not on PATH")
    def test_detection_shows_a_rate_per_complete_cell_inc_for_the_rest_and_nothing_for_a_blank(self):
        self.assertEqual(run_main(*self.args())[0], 0)
        text = pdf_text(os.path.join(self.out, "detection.pdf"))
        cells = pf.read_cells(synthetic_score())
        complete = sum(1 for v in cells.values() if v is not None)
        self.assertEqual(len(RATE.findall(text)), complete)
        self.assertEqual(len(INCOMPLETE.findall(text)), len(cells) - complete)
        self.assertIn("0.99", text)


class FixcostPanels(unittest.TestCase):
    def draw(self, rows):
        fig = pf.fixcost_figure(rows)
        self.addCleanup(plt.close, fig)
        return fig

    def assert_drawn_and_visible(self, fig):
        panels = fix_points(fig)
        self.assertEqual([len(m) for m, _ in panels], [15, 5])
        self.assertEqual(sum(len(m) for m, _ in panels), 20)
        for ax, (medians, bars) in zip(fig.axes, panels):
            lo, hi = ax.get_xlim()
            ymin, ymax = sorted(ax.get_ylim())
            self.assertEqual(len(bars), len(medians))
            for (x, y, _), (a, b) in zip(medians, bars):
                # The median need not lie inside the interval of the mean; both must lie in the panel.
                self.assertTrue(lo <= min(a, x) and max(b, x) <= hi and a <= b, (x, a, b, lo, hi))
                self.assertTrue(ymin <= y <= ymax)
        return panels

    def test_synthetic_rows_fill_two_panels_with_filled_and_hollow_markers(self):
        fig = self.draw(pf.read_fixcost(synthetic_score()))
        (left, _), (right, _) = self.assert_drawn_and_visible(fig)
        self.assertTrue(all(filled for _, _, filled in left))
        self.assertFalse(any(filled for _, _, filled in right))
        self.assertEqual(tuple(round(v, 4) for v in fig.axes[0].get_xlim()), (0.975, 1.025))
        self.assertEqual(fig.axes[1].get_xlim()[0], 0.0)

    def test_left_range_grows_to_hold_a_confirmatory_point_outside_it(self):
        rows = dict(pf.read_fixcost(synthetic_score()))
        rows[("linux", "epoll", 10)] = (0.9, 0.89, 0.91)
        fig = self.draw(rows)
        self.assertLess(fig.axes[0].get_xlim()[0], 0.89)
        self.assert_drawn_and_visible(fig)

    def test_each_n_zero_row_sits_at_the_middle_of_its_block_of_the_left_panel(self):
        fig = self.draw(pf.read_fixcost(synthetic_score()))
        (left, _), (right, _) = fix_points(fig)
        for i, (_, y, _) in enumerate(right):
            block = [yy for _, yy, _ in left[3 * i:3 * i + 3]]
            self.assertEqual(sum(block) / 3, y)


class Refusals(Fixture):
    def test_sweep_csv_missing_a_column(self):
        write_sweep(self.sweep_path, synthetic_sweep_rows(), [c for c in pf.SWEEP_COLUMNS if c != "b_eff_ns"])
        self.assertIn("b_eff_ns", self.assert_refused())

    def test_nine_sweeps(self):
        rows = [r for r in synthetic_sweep_rows()
                if (r["system"], r["backend"], r["bound_us"]) != ("windows", "iocp", 1000)]
        write_sweep(self.sweep_path, rows)
        self.assertIn("9 sweeps", self.assert_refused())

    def test_eleven_sweeps(self):
        extra = dict(synthetic_sweep_rows()[0], bound_us=5000)
        write_sweep(self.sweep_path, synthetic_sweep_rows() + [extra])
        self.assertIn("11 sweeps", self.assert_refused())

    def test_ten_sweeps_but_one_is_not_the_papers(self):
        rows = [dict(r, backend="iocp") if (r["system"], r["backend"]) == ("linux", "epoll") else r
                for r in synthetic_sweep_rows()]
        write_sweep(self.sweep_path, rows)
        self.assert_refused()

    def test_sweep_values_that_cannot_be_used(self):
        for column, bad in (("delay_ns", "x"), ("b_eff_ns", 0), ("phase_ns", "nan")):
            rows = synthetic_sweep_rows()
            rows[3] = dict(rows[3], **{column: bad})
            write_sweep(self.sweep_path, rows)
            self.assert_refused()

    def test_empty_sweep_file_and_missing_files(self):
        with open(self.sweep_path, "w") as fh:
            fh.write("")
        self.assert_refused()
        os.remove(self.sweep_path)
        self.assert_refused()

    def test_missing_h4_row(self):
        score = synthetic_score()
        score["H4"]["fixed_over_absent"].pop(7)
        self.write_score(score)
        self.assertIn("missing H4 row", self.assert_refused())

    def test_h4_rows_that_are_unknown_twice_or_incomplete(self):
        for edit in (lambda rows: rows.append(dict(rows[0], key="linux-epoll-N5us", work_us=5)),
                     lambda rows: rows.append(dict(rows[0])),
                     lambda rows: rows[0].update(complete=False),
                     lambda rows: rows[0].update(work_us=1),
                     lambda rows: rows[0].pop("median_ratio")):
            score = synthetic_score()
            edit(score["H4"]["fixed_over_absent"])
            self.write_score(score)
            self.assert_refused()

    def test_score_without_cells_or_h4(self):
        for drop in ("cells", "H4"):
            score = synthetic_score()
            del score[drop]
            self.write_score(score)
            self.assert_refused()

    def test_cell_key_that_does_not_parse_or_disagrees_with_its_fields(self):
        score = synthetic_score()
        key = "linux:S-epoll-none-W1-B1ms"
        for new_key, value in (("linux:S-epoll-none", score["cells"][key]),
                               ("linux:S-epoll-none-W1-B10ms", score["cells"][key]),
                               ("linux:S-epoll-none-W3-B1ms", score["cells"][key])):
            broken = synthetic_score()
            del broken["cells"][key]
            broken["cells"][new_key] = value
            self.write_score(broken)
            self.assert_refused()

    def test_cell_without_completeness_or_with_a_rate_out_of_range(self):
        key = "linux:S-epoll-none-W1-B1ms"
        for edit in (lambda c: c.pop("complete"), lambda c: c["t_med"].update(rate=1.5),
                     lambda c: c.pop("t_med")):
            score = synthetic_score()
            edit(score["cells"][key])
            self.write_score(score)
            self.assert_refused()

    def test_a_refusal_leaves_an_existing_output_folder_alone(self):
        os.makedirs(self.out)
        marker = os.path.join(self.out, "keep.txt")
        with open(marker, "w") as fh:
            fh.write("x")
        score = synthetic_score()
        score["H4"]["fixed_over_absent"].pop()
        self.write_score(score)
        code, _, err = run_main(*self.args())
        self.assertEqual(code, 2, err)
        self.assertEqual(os.listdir(self.out), ["keep.txt"])


class RealResults(unittest.TestCase):
    @unittest.skipUnless(os.path.exists(REAL_SCORE) and os.path.exists(REAL_SWEEP),
                         "results/v2/score.json or the real sweep CSV is not here")
    def test_real_results_give_the_three_files(self):
        with tempfile.TemporaryDirectory(prefix="paper-figures-real-") as out:
            code, stdout, err = run_main("--score", REAL_SCORE, "--sweep", REAL_SWEEP, "--out-dir", out)
            self.assertEqual(code, 0, err)
            self.assertEqual(sorted(os.listdir(out)), sorted(pf.OUTPUT_NAMES))
            for name in pf.OUTPUT_NAMES:
                self.assertGreater(os.path.getsize(os.path.join(out, name)), 1000, name)
            self.assertEqual(len(stdout.strip().splitlines()), 3)

    @unittest.skipUnless(os.path.exists(REAL_SCORE) and os.path.exists(REAL_SWEEP),
                         "results/v2/score.json or the real sweep CSV is not here")
    def test_no_ligature_is_drawn(self):
        """fi, ff and fl are drawn as two glyphs: no ToUnicode entry of the PDFs maps one glyph to two letters."""
        with tempfile.TemporaryDirectory(prefix="paper-figures-liga-") as out:
            self.assertEqual(run_main("--score", REAL_SCORE, "--sweep", REAL_SWEEP, "--out-dir", out)[0], 0)
            for name in pf.OUTPUT_NAMES:
                with open(os.path.join(out, name), "rb") as fh:
                    data = fh.read()
                self.assertEqual(ligature_maps(data), [], name)

    @unittest.skipUnless(os.path.exists(REAL_SCORE), "results/v2/score.json is not here")
    def test_real_score_has_the_expected_shape(self):
        cells, fix = pf.read_score(REAL_SCORE)
        fig = pf.fixcost_figure(fix)
        self.addCleanup(plt.close, fig)
        panels = fix_points(fig)
        self.assertEqual([len(m) for m, _ in panels], [15, 5])
        for ax, (medians, bars) in zip(fig.axes, panels):
            lo, hi = ax.get_xlim()
            self.assertTrue(all(lo <= min(a, x) and max(b, x) <= hi for (x, _, _), (a, b) in zip(medians, bars)))
        self.assertEqual(tuple(round(v, 4) for v in fig.axes[0].get_xlim()), (0.975, 1.025))
        self.assertEqual(len(fix), 20)
        self.assertEqual(sum(1 for v in cells.values() if v is None), 4)
        self.assertEqual(len(pf.detection_rows(cells, "S")), 26)
        self.assertEqual(len(pf.detection_rows(cells, "Bt")), 8)


if __name__ == "__main__":
    unittest.main()

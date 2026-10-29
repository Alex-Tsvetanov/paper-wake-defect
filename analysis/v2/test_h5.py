"""H5's scorer on synthetic run_h5 folders: the arm mapping, the run rules, the gate, the 2026-09 verdicts and the reading."""

import io
import itertools
import json
import os
import shutil
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import h5
import provenance
import summarize as s2609
import synth

sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bench"))
import run_campaign  # noqa: E402

COMMIT = synth.BENCH_COMMIT
COARSEST_US = 15625
PROBE_SHA = "9" * 64
FIELDS = {"linux": "design arm variant backend bound_us gap_us posts warmup slack_ns file",
          "windows": "design arm variant bound_us gap_us phase_frac timer_ms posts warmup file"}


def run_lines(system, design, backend, label, wait_us, gap_us, frac, *, resid=0.01, delay_us=100, variant=None,
              timer=None, slack=1, bimodal=False, readings=None, median_off=0, lost=0, error=None):
    """One wakeprobe run as summarize.load_run reads it. A defect run calibrates B_eff = B + 7 us and its measured
    posts sit resid * B_eff above the model; a fixed run waits about delay_us at every post. lost: the last measured
    posts are never delivered (exec_ns and delay_ns null), as wakeprobe writes them."""
    variant = variant or (h5.FIXED_VARIANT if label == "fixed" else h5.DEFECT_VARIANT[backend])
    posts, warmup = h5.DESIGN_SIZES[design]
    lines, t = [], 0
    b_eff = wait_us * 1000 + 7000 if label == "defect" else None
    if b_eff:
        for i in range(11):
            t += b_eff * (2 if bimodal and i % 2 else 1)
            lines.append({"kind": "calib", "index": i, "exec_ns": t, "delay_ns": None, "phase_ns": None,
                          "predicted_ns": None, "residual_ns": None})
    delays = []
    for i in range(posts):
        if b_eff:
            phase = (i * 3301) % b_eff
            predicted = b_eff - phase % b_eff
            residual = int(resid * b_eff)
            delay = predicted + residual
        else:
            phase = predicted = residual = None
            delay = delay_us * 1000 + i
        if i >= posts - lost:
            delay = phase = predicted = residual = None
        else:
            delays.append(delay)
        t += 5_000_000
        lines.append({"kind": "measured", "index": i, "exec_ns": t if delay is not None else None, "delay_ns": delay,
                      "phase_ns": phase, "predicted_ns": predicted, "residual_ns": residual})
    summary = {"summary": True, "design": design, "backend": backend, "variant": variant, "label": label,
               "defect_active": variant != "none", "median_ns": s2609.nearest_rank(sorted(delays), 50) + median_off,
               "b_eff_ns": b_eff, "wait_us": wait_us, "gap_us": gap_us, "phase_frac": frac, "gap_frac_range": None,
               "gap_seed": None, "error": error, "timeouts": 0, "lost": lost, "posts": posts, "warmup": warmup,
               "bench_code_commit": COMMIT, "bench_commit": COMMIT, "bench_dirty": False, "build_type": "Release",
               "compiler": "synthetic", "host": "synthetic", "coarsest_timer_us": COARSEST_US, **h5.FIXED_RUN}
    if system == "linux":
        summary.update(timerslack_ns_requested=slack, timerslack_set_rc=0 if slack else None,
                       timerslack_ns_worker=1 if slack == 1 else 50000)
    else:
        summary["timer_period_ms_requested"] = timer
        want = COARSEST_US * 10 if timer == 0 else 10000 * timer
        start, gaps, end = readings or (want, [want], want)
        summary.update(timer_resolution_100ns_start=start, timer_resolution_100ns_gap=gaps,
                       timer_resolution_100ns_end=end)
    return lines + [summary]


def cell(system, design, backend, label, wait_us, gap_us=None, frac=None, **kw):
    """(file name, plan line, run lines) of one cell, in its runner's plan format."""
    setting = "timer%s" % kw["timer"] if system == "windows" else "slack%s" % kw.get("slack", 1)
    where = "g%dus" % gap_us if gap_us is not None else "f%s" % frac
    name = "%s-%s-%dus-%s-%s-%s.jsonl" % (design, backend, wait_us, where, label, setting)
    lines = run_lines(system, design, backend, label, wait_us, gap_us, frac, **kw)
    s = lines[-1]
    posts, warmup = h5.DESIGN_SIZES[design]
    if system == "linux":
        plan = "%s %s %s %s %d %d %d %d %d %s" % (design, label, s["variant"], backend, wait_us, gap_us, posts, warmup,
                                                  kw.get("slack", 1), name)
    else:
        plan = "%s %s %s %d gap=%s frac=%s timer=%d %d %d %s" % (
            design, label, s["variant"], wait_us, "" if gap_us is None else gap_us, "" if frac is None else frac,
            kw["timer"], posts, warmup, name)
    return name, plan, lines


def write_jsonl(path, lines):
    Path(path).write_text("".join(json.dumps(o) + "\n" for o in lines), encoding="utf-8")


def write_folder(root, system, cells, records, header=None, gate=True, probe_sha=PROBE_SHA, **gate_kw):
    """A run_h5 output folder: plan.txt, gate.txt (from synth.write_gate's run.txt lines) and the cells' runs."""
    folder = Path(root) / system
    folder.mkdir()
    plan = ["# seed: 1", "# wakeprobe sha256: %s" % PROBE_SHA,
            "# cells: %d (synthetic)" % (len(cells) if header is None else header), "# fields: " + FIELDS[system]]
    for name, line, lines in cells:
        plan.append(line)
        write_jsonl(folder / name, lines)
    (folder / "plan.txt").write_text("\n".join(plan) + "\n", encoding="utf-8")
    if gate:
        scratch = tempfile.mkdtemp(dir=root)
        synth.write_gate(scratch, system, records, **gate_kw)
        fields = provenance.read_run_txt(scratch)
        text = "records gate passed: " + "; ".join("%s %s" % (k, fields[k]) for k in provenance.GATE_KEYS)
        text += "\nbinary wakeprobe sha256 %s\nbinary abpair sha256 %s\n" % (probe_sha, "e" * 64)
        (folder / "gate.txt").write_text(text, encoding="utf-8")
    return folder


def linux_cells(bound_medians=(60, 70, 80, 200), sweep_resid=0.01):
    out = []
    for backend in ("epoll", "io_uring"):
        for b in (1000, 10000):
            for gap in (b // 10, b):
                out.append(cell("linux", "gap-sweep", backend, "defect", b, gap, resid=sweep_resid if b == 1000 else 0.01))
        for slack in (1, 0):
            out.append(cell("linux", "matrix", backend, "defect", 1000, 1700, slack=slack))
            out.append(cell("linux", "matrix", backend, "fixed", 1000, 1700, slack=slack))
        for b, med in zip((0, 1000, 10000, 100000), bound_medians):
            out.append(cell("linux", "bound-sweep", backend, "fixed", b, 2000, delay_us=med))
    return out


def windows_cells(split_resid=0.2, bound_medians=(1000, 1010, 1020, 1030), key_bimodal=False):
    out = []
    for timer in (1, 0):
        for b in (1000, 10000):
            if key_bimodal and (timer, b) == (1, 1000):
                continue
            for frac in (0.95, 2.45):
                r = split_resid if (timer, b) == (1, 1000) else 0.01
                out.append(cell("windows", "gap-sweep", "iocp", "defect", b, frac=frac, timer=timer, resid=r))
        out.append(cell("windows", "matrix", "iocp", "defect", 1000, 1700, timer=timer,
                        resid=split_resid if timer == 1 else 0.01, bimodal=key_bimodal and timer == 1))
        out.append(cell("windows", "matrix", "iocp", "fixed", 1000, 1700, timer=timer))
        for b, med in zip((0, 1000, 10000, 100000), bound_medians):
            out.append(cell("windows", "bound-sweep", "iocp", "fixed", b, 2000, timer=timer, delay_us=med))
    return out


def replace(cells, name, new):
    return [new if c[0] == name else c for c in cells]


class H5Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p1v2-h5-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.records = os.path.join(self.tmp, "records")
        self.designs = {}

    def folder(self, system, cells, **kw):
        """A folder, and its cells as the design the scorer checks (the runners' design is too large for a fixture)."""
        self.designs[system] = dict(Counter(line.split()[0] for _n, line, _l in cells))
        return write_folder(self.tmp, system, cells, self.records, **kw)

    def both(self, linux=None, windows=None):
        return [("linux", self.folder("linux", linux if linux is not None else linux_cells())),
                ("windows", self.folder("windows", windows if windows is not None else windows_cells()))]

    def score(self, inputs):
        return h5.score(inputs, "test", [self.records], design_cells=self.designs)

    def refused(self, inputs, text):
        with self.assertRaises(h5.Refusal) as e:
            self.score(inputs)
        self.assertIn(text, str(e.exception))


class Reading(H5Case):
    def test_the_2026_09_split_reads_a(self):
        r = self.score(self.both())
        failing = [(c["system"], c["variant"], c["wait_us"]) for c in r["delay_model"]["configurations"]
                   if c["verdict"] == "fail"]
        self.assertEqual(failing, [("windows", "timer=1ms", 1000)])
        self.assertEqual({(g["system"], g["verdict"]) for g in r["bound_independence"]["groups"]},
                         {("linux", "fail"), ("windows", "pass")})
        self.assertEqual(r["reading"], {"part1_delay_model": "recurs", "part2_bound_independence": "recurs",
                                        "outcome": "a"})
        self.assertEqual((r["bench_commit"], r["warnings"]), (COMMIT, []))
        self.assertEqual(r["criteria"]["delay_model_limit"], s2609.H1_LIMIT)
        self.assertEqual(r["notes"], {"timer": [], "slack": [], "lost_wakes": [], "reruns": [], "incomplete": []})

    def test_neither_part_reads_b(self):
        r = self.score(self.both(linux_cells(bound_medians=(60, 61, 62, 63)), windows_cells(split_resid=0.01)))
        self.assertEqual(r["reading"]["outcome"], "b")
        self.assertTrue(all(c["verdict"] == "pass" for c in r["delay_model"]["configurations"]))

    def test_failures_elsewhere_are_not_the_split(self):
        r = self.score(self.both(linux_cells(sweep_resid=0.3, bound_medians=(60, 61, 62, 63))))
        self.assertEqual(r["reading"], {"part1_delay_model": "does not recur",
                                        "part2_bound_independence": "does not recur", "outcome": "b"})

    def test_one_part_reads_c(self):
        r = self.score(self.both(windows=windows_cells(bound_medians=(1000, 1010, 1020, 5000))))
        self.assertEqual(r["reading"]["part2_bound_independence"], "does not recur")
        self.assertEqual(r["reading"]["outcome"], "c")

    def test_one_system_has_no_reading(self):
        r = self.score([("linux", self.folder("linux", linux_cells()))])
        self.assertIsNone(r["reading"])
        self.assertEqual(r["systems"]["linux"]["cells"], len(linux_cells()))

    def test_an_unscored_key_configuration_is_undetermined(self):
        r = self.score(self.both(windows=windows_cells(key_bimodal=True)))
        mx = [c for c in r["delay_model"]["cells"] if c["system"] == "windows" and c["design"] == "matrix"
              and c["variant"] == "timer=1ms"]
        self.assertEqual(mx[0]["h1_pass"], "bimodal")
        key = [c for c in r["delay_model"]["configurations"] if (c["system"], c["variant"], c["wait_us"]) ==
               h5.SPLIT_DELAY]
        self.assertEqual(key[0]["verdict"], "none")
        self.assertEqual(r["reading"]["part1_delay_model"], "undetermined")
        self.assertEqual(r["reading"]["outcome"], "undetermined")

    def test_a_bimodal_cell_elsewhere_is_left_out_as_in_2026_09(self):
        cells = replace(windows_cells(), "matrix-iocp-1000us-g1700us-defect-timer0.jsonl",
                        cell("windows", "matrix", "iocp", "defect", 1000, 1700, timer=0, bimodal=True))
        r = self.score(self.both(windows=cells))
        conf = [c for c in r["delay_model"]["configurations"]
                if (c["system"], c["variant"], c["wait_us"]) == ("windows", "timer=default", 1000)]
        self.assertEqual(conf[0]["verdict"], "pass")  # its gap-sweep cell still has a verdict
        self.assertEqual(r["reading"]["outcome"], "a")


class Runs(H5Case):
    def test_a_lost_unit_in_both_runs_leaves_the_cell_incomplete(self):
        name = "bound-sweep-epoll-0us-g2000us-fixed-slack1.jsonl"
        cells = replace(linux_cells(), name, cell("linux", "bound-sweep", "epoll", "fixed", 0, 2000, delay_us=60, lost=1))
        folder = self.folder("linux", cells)
        write_jsonl(folder / name.replace(".jsonl", "-rerun.jsonl"),
                    run_lines("linux", "bound-sweep", "epoll", "fixed", 0, 2000, None, delay_us=60, lost=2))
        r = self.score([("linux", folder)])
        self.assertEqual([n["cell"] for n in r["notes"]["incomplete"]], [name])
        self.assertEqual(r["notes"]["lost_wakes"][0]["lost_units"], {"first": 1, "rerun": 2})
        group = [g for g in r["bound_independence"]["groups"] if g["backend"] == "epoll"][0]
        self.assertEqual(group["incomplete_runs"], 1)
        self.assertEqual(group["verdict"], "fail")  # the rest already fails: a smaller set cannot pass when this fails

    def test_an_incomplete_run_leaves_a_passing_group_without_verdict(self):
        name = "bound-sweep-iocp-100000us-g2000us-fixed-timer0.jsonl"
        cells = replace(windows_cells(), name, cell("windows", "bound-sweep", "iocp", "fixed", 100000, 2000, timer=0,
                                                    delay_us=1030, error="worker thread failed"))
        r = self.score(self.both(windows=cells))
        group = [g for g in r["bound_independence"]["groups"] if g["variant"] == "timer=default"][0]
        self.assertEqual(group["verdict"], "none")
        self.assertEqual(r["reading"]["part2_bound_independence"], "undetermined")

    def test_a_clean_rerun_replaces_the_failed_first_run(self):
        name = "gap-sweep-epoll-1000us-g100us-defect-slack1.jsonl"
        cells = replace(linux_cells(), name, cell("linux", "gap-sweep", "epoll", "defect", 1000, 100, lost=1))
        folder = self.folder("linux", cells)
        write_jsonl(folder / name.replace(".jsonl", "-rerun.jsonl"),
                    run_lines("linux", "gap-sweep", "epoll", "defect", 1000, 100, None))
        r = self.score([("linux", folder)])
        self.assertEqual(r["notes"]["reruns"], [{"cell": name, "system": "linux",
                                                 "problems": {"first": "lost unit", "rerun": None}}])
        self.assertEqual(r["notes"]["incomplete"], [])
        self.assertEqual(r["notes"]["lost_wakes"], [])  # a defect cell: the lost unit is no lost wake

    def test_an_incomplete_gap_sweep_step_takes_its_pooled_cell_out(self):
        name = "gap-sweep-iocp-1000us-f0.95-defect-timer1.jsonl"
        cells = replace(windows_cells(), name, cell("windows", "gap-sweep", "iocp", "defect", 1000, frac=0.95, timer=1,
                                                    resid=0.2, lost=1))
        r = self.score(self.both(windows=cells))
        self.assertEqual(r["systems"]["windows"]["gap_sweep_cells_without_verdict"], [["iocp", "timer=1ms", 1000]])
        key = [c for c in r["delay_model"]["configurations"] if (c["system"], c["variant"], c["wait_us"]) ==
               h5.SPLIT_DELAY][0]
        self.assertEqual(key["verdict"], "fail")  # the matrix cell still fails

    def test_a_finer_timer_is_noted_for_both_settings(self):
        a = cell("windows", "bound-sweep", "iocp", "fixed", 10000, 2000, timer=0, delay_us=1020,
                 readings=(156250, [156250, 100000], 156250))
        b = cell("windows", "bound-sweep", "iocp", "fixed", 10000, 2000, timer=1, delay_us=1020,
                 readings=(10000, [5000], 10000))
        cells = replace(replace(windows_cells(), a[0], a), b[0], b)
        r = self.score([("windows", self.folder("windows", cells))])
        self.assertEqual(sorted((n["cell"], n["asked_us"], n["readings_100ns"]) for n in r["notes"]["timer"]),
                         [(a[0], 15625, [156250, 156250, 100000, 156250]), (b[0], 1000, [10000, 5000, 10000])])

    def test_a_slack_not_set_is_noted(self):
        name, line, lines = cell("linux", "matrix", "epoll", "fixed", 1000, 1700)
        lines[-1]["timerslack_ns_worker"] = 50000
        r = self.score([("linux", self.folder("linux", replace(linux_cells(), name, (name, line, lines))))])
        self.assertEqual([n["cell"] for n in r["notes"]["slack"]], [name])

    def test_main_writes_the_record_in_draft(self):
        inputs = self.both()
        out = os.path.join(self.tmp, "h5.json")
        with redirect_stdout(io.StringIO()) as printed, redirect_stderr(io.StringIO()), \
                mock.patch.dict(h5.DESIGN_CELLS, self.designs, clear=True):
            self.assertEqual(h5.main(["--linux", str(inputs[0][1]), "--windows", str(inputs[1][1]), "--draft",
                                      "--out", out]), 0)
        self.assertIn("reading a", printed.getvalue())
        with open(out, encoding="utf-8") as f:
            record = json.load(f)
        self.assertTrue(record["generated_by"].startswith("analysis/v2/h5.py "))
        self.assertTrue(record["generated_by"].endswith(" draft"))
        self.assertTrue(any("no --records folder" in w for w in record["warnings"]))


class Mapping(H5Case):
    def test_the_loader_is_2026_09s_own_on_a_defect_run(self):
        name, _line, lines = cell("linux", "gap-sweep", "epoll", "defect", 1000, 100)
        write_jsonl(Path(self.tmp) / name, lines)
        ours = h5.load(Path(self.tmp) / name, "defect")
        theirs = s2609.load_run(Path(self.tmp) / name)
        self.assertEqual({k: v for k, v in ours.items() if k != "path"}, {k: v for k, v in theirs.items() if k != "path"})

    def test_plain_wake_is_the_fixed_arm(self):
        name, _line, lines = cell("linux", "bound-sweep", "io_uring", "fixed", 1000, 2000)
        write_jsonl(Path(self.tmp) / name, lines)
        with self.assertRaises(SystemExit):
            s2609.load_run(Path(self.tmp) / name)  # 2026-09's loader: plain_wake is a defect build
        self.assertEqual(h5.load(Path(self.tmp) / name, "fixed")["arm"], "fixed")

    def test_a_label_against_the_plan_is_refused(self):
        name, line, lines = cell("linux", "matrix", "epoll", "fixed", 1000, 1700)
        lines[-1]["label"] = "defect"
        self.refused([("linux", self.folder("linux", [(name, line, lines)]))], "summary label")

    def test_a_variant_outside_h5_is_refused(self):
        c = cell("linux", "matrix", "epoll", "defect", 1000, 1700, variant="unobserved_primitive")
        self.refused([("linux", self.folder("linux", [c]))], "H5's mapping")

    def test_a_plan_field_against_the_run_is_refused(self):
        name, line, lines = cell("linux", "bound-sweep", "epoll", "fixed", 1000, 2000)
        self.refused([("linux", self.folder("linux", [(name, line.replace(" 1000 2000 ", " 10000 2000 "), lines)]))],
                     "summary wait_us")

    def test_a_design_against_the_plan_is_refused(self):
        name, line, lines = cell("linux", "matrix", "epoll", "fixed", 1000, 1700)
        self.refused([("linux", self.folder("linux", [(name, line.replace("matrix", "bound-sweep", 1), lines)]))],
                     "summary design")

    def test_the_loaders_own_checks_still_refuse_with_the_file_name(self):
        c = cell("linux", "gap-sweep", "epoll", "defect", 1000, 100, median_off=1)
        with self.assertRaises(h5.Refusal) as e:
            self.score([("linux", self.folder("linux", [c]))])
        self.assertIn("recomputed median", str(e.exception))
        self.assertIn(c[0], str(e.exception))
        self.assertNotIn(self.tmp, str(e.exception))

    def test_a_cell_twice_is_refused(self):
        name, line, lines = cell("linux", "matrix", "epoll", "defect", 1000, 1700)
        twin = (name.replace(".jsonl", "-b.jsonl"), line.replace(name, name.replace(".jsonl", "-b.jsonl")), lines)
        self.refused([("linux", self.folder("linux", [(name, line, lines), twin]))], "appears twice")


class Provenance(H5Case):
    def test_the_records_gate_is_checked(self):
        self.score(self.both())  # green records, matching sha256: no refusal
        red = "wakeloop2-abc123def-L-msan-20261028t100000.json"
        shutil.rmtree(self.tmp)
        os.makedirs(self.tmp)
        self.refused([("linux", self.folder("linux", linux_cells(), red=(red,)))], "not a green record")

    def test_gate_without_a_pass_line(self):
        folder = self.folder("linux", linux_cells(), gate=False)
        (folder / "gate.txt").write_text("gate refused\n", encoding="utf-8")
        self.refused([("linux", folder)], "no 'records gate passed:'")

    def test_gate_with_binary_changed(self):
        folder = self.folder("linux", linux_cells())
        with open(folder / "gate.txt", "a", encoding="utf-8") as f:
            f.write("BINARY-CHANGED wakeprobe\n")
        self.refused([("linux", folder)], "BINARY-CHANGED")

    def test_another_wakeprobe_than_the_gate_hashed(self):
        self.refused([("linux", self.folder("linux", linux_cells(), probe_sha="8" * 64))], "hashed wakeprobe")

    def test_plan_header_count(self):
        self.refused([("linux", self.folder("linux", linux_cells(), header=3))], "header")

    def test_an_output_not_in_the_plan(self):
        folder = self.folder("linux", linux_cells())
        (folder / "stray.jsonl").write_text("{}\n", encoding="utf-8")
        self.refused([("linux", folder)], "not in plan.txt")

    def test_two_commits_across_the_systems(self):
        w = [(n, line, lines[:-1] + [dict(lines[-1], bench_commit="d" * 40)]) for n, line, lines in windows_cells()]
        self.refused(self.both(windows=w), "commits, not one")

    def test_a_dirty_build_is_a_failed_run(self):
        name, line, lines = linux_cells()[0]
        cells = replace(linux_cells(), name, (name, line, lines[:-1] + [dict(lines[-1], bench_dirty=True)]))
        r = self.score([("linux", self.folder("linux", cells))])
        self.assertEqual(r["notes"]["incomplete"][0]["problems"], {"first": "bench_dirty is True, not false"})

    def test_a_plan_that_is_not_the_runners_design(self):
        inputs = self.both()
        with self.assertRaises(h5.Refusal) as e:
            h5.score(inputs, "test", [self.records])  # the runners' 140 and 136 cells
        self.assertIn("the runner's design", str(e.exception))

    def test_a_plan_with_other_sizes(self):
        name, line, lines = cell("linux", "matrix", "epoll", "fixed", 1000, 1700)
        self.refused([("linux", self.folder("linux", [(name, line.replace(" 51 5 ", " 21 3 "), lines)]))],
                     "plans posts 21")

    def test_an_unpinned_run(self):
        name, line, lines = cell("linux", "matrix", "epoll", "fixed", 1000, 1700)
        lines[-1]["worker_cpus_seen"] = [12]
        self.refused([("linux", self.folder("linux", [(name, line, lines)]))], "summary worker_cpus_seen")

    def test_a_windows_plan_field_against_the_run(self):
        name, line, lines = cell("windows", "bound-sweep", "iocp", "fixed", 1000, 2000, timer=1)
        self.refused([("windows", self.folder("windows", [(name, line.replace("timer=1", "timer=0"), lines)]))],
                     "summary timer_period_ms_requested")


class NoOutput(H5Case):
    NAME = "matrix-epoll-1000us-g1700us-fixed-slack1.jsonl"

    def folder_without_first(self, rerun):
        folder = self.folder("linux", linux_cells())
        os.remove(folder / self.NAME)
        (folder / self.NAME.replace(".jsonl", ".err")).write_text("cannot create the loop\n", encoding="utf-8")
        stem = self.NAME[:-len(".jsonl")]
        if rerun == "clean":
            write_jsonl(folder / (stem + "-rerun.jsonl"), run_lines("linux", "matrix", "epoll", "fixed", 1000, 1700, None))
        elif rerun == "none":
            (folder / (stem + "-rerun.err")).write_text("cannot create the loop\n", encoding="utf-8")
        return folder

    def test_a_first_run_without_output_and_a_clean_rerun(self):
        r = self.score([("linux", self.folder_without_first("clean"))])
        self.assertEqual(r["notes"]["reruns"][0]["problems"], {"first": "no output", "rerun": None})
        self.assertEqual(r["notes"]["incomplete"], [])

    def test_two_runs_without_output_leave_the_cell_incomplete(self):
        r = self.score([("linux", self.folder_without_first("none"))])
        self.assertEqual(r["notes"]["incomplete"], [{"cell": self.NAME, "system": "linux",
                                                     "problems": {"first": "no output", "rerun": "no output"}}])

    def test_a_cell_that_left_no_file_is_refused(self):
        folder = self.folder("linux", linux_cells())
        os.remove(folder / self.NAME)
        self.refused([("linux", folder)], "left no file")


class ReadingTable(unittest.TestCase):
    """reading() against an oracle that tries every pass or fail for each verdict that is none."""

    V = ("pass", "fail", "none")

    @staticmethod
    def state(outcomes):
        return {frozenset([True]): "recurs", frozenset([False]): "does not recur"}.get(frozenset(outcomes), "undetermined")

    @staticmethod
    def options(v):
        return ("pass", "fail") if v == "none" else (v,)

    def test_part_1(self):
        groups = [{"system": "linux", "verdict": "fail"}, {"system": "windows", "verdict": "pass"}]
        for key in self.V + (None,):
            for n in range(4):
                for others in itertools.product(self.V, repeat=n):
                    configs = [{"system": "linux", "variant": "slack=1ns", "wait_us": 1000 * (i + 2), "verdict": v}
                               for i, v in enumerate(others)]
                    if key:
                        configs.append({"system": "windows", "variant": "timer=1ms", "wait_us": 1000, "verdict": key})
                    want = self.state({k == "fail" and "fail" not in o for k in self.options(key or "none")
                                       for o in itertools.product(*map(self.options, others))})
                    got = h5.reading(configs, groups, {"linux": 1, "windows": 1})["part1_delay_model"]
                    self.assertEqual(got, want, (key, others))

    def test_part_2(self):
        configs = [{"system": "windows", "variant": "timer=1ms", "wait_us": 1000, "verdict": "fail"}]
        for nl, nw in itertools.product(range(3), repeat=2):
            for lin in itertools.product(self.V, repeat=nl):
                for win in itertools.product(self.V, repeat=nw):
                    groups = [{"system": "linux", "verdict": v} for v in lin] + \
                             [{"system": "windows", "verdict": v} for v in win]
                    if not lin or not win:
                        want = "undetermined"
                    else:
                        want = self.state({all(x == "fail" for x in a) and all(x == "pass" for x in b)
                                           for a in itertools.product(*map(self.options, lin))
                                           for b in itertools.product(*map(self.options, win))})
                    got = h5.reading(configs, groups, {"linux": 1, "windows": 1})["part2_bound_independence"]
                    self.assertEqual(got, want, (lin, win))

    def test_the_outcome(self):
        for p1, p2 in itertools.product(("fail", "pass", "none"), ("recurs", "does not recur", "undetermined")):
            groups = {"recurs": ["fail", "pass"], "does not recur": ["pass", "pass"], "undetermined": ["none", "pass"]}[p2]
            r = h5.reading([{"system": "windows", "variant": "timer=1ms", "wait_us": 1000, "verdict": p1}],
                           [{"system": "linux", "verdict": groups[0]}, {"system": "windows", "verdict": groups[1]}],
                           {"linux": 1, "windows": 1})
            parts = (r["part1_delay_model"], r["part2_bound_independence"])
            want = "undetermined" if "undetermined" in parts else {2: "a", 0: "b", 1: "c"}[parts.count("recurs")]
            self.assertEqual(r["outcome"], want)


class GateFields(unittest.TestCase):
    def test_the_runners_own_gate_lines_come_back_whole(self):
        def entry(name):
            return {"name": name, "sha256": "a" * 64}
        result = {"wakeloop": {"compiler": "Clang 22.1.8", "coverage": {
                      "asan": [entry("wakeloop2-1-L-asan-x.json"), entry("wakeloop2-2-L-asan-y.json")],
                      "msan": [entry("wakeloop2-1-L-msan-x.json")], "tsan": [entry("wakeloop2-1-L-tsan-x.json")]}},
                  "other_host": {"records": {"asan": [entry("wakeloop2-1-W-asan-x.json")]}},
                  "code_commit": "c" * 40, "code_fingerprint": "f" * 64, "coverage_json_sha256": "b" * 64,
                  "generators": {p: "d" * 64 for p in ("analysis/v2/plan.py", "analysis/v2/grid.py",
                                                       "bench/campaign_smoke.py", "bench/run_h5.sh", "bench/run_h5.ps1")},
                  "smoke_keys": set(range(129))}
        lines = run_campaign.gate_lines(result)
        text = "records gate passed: " + "; ".join("%s %s" % (k, v) for k, v in lines)
        self.assertEqual(h5.gate_fields(text), dict(lines))


if __name__ == "__main__":
    unittest.main()

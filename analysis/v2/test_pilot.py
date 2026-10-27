"""The A/A pilot on synthetic abpair outputs with known s, its JSON record and its revision-log block."""

import io
import json
import math
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import grid
import h4log
import pilot
import plan
import readers
import stats
import synth
from tempgit import TempGit


def ratios_for(s):
    """Two pairs per process whose 8 log ratios (4 at +a, 4 at -a) have sample SD s."""
    a = s * math.sqrt(7.0 / 8.0)
    return [math.exp(a), math.exp(-a)]


def write_windows(tmp):
    """Every Windows pilot cell whole: iocp N = 1 us at s 0.0125 (R 18), condvar N = 100 us at 0.05 (24)."""
    folder = os.path.join(tmp, "windows")
    os.makedirs(folder)
    entries = plan.pilot_plan_entries("windows")
    path = os.path.join(tmp, "pilot-windows.jsonl")
    plan.write_plan(path, plan.header("windows", "test"), entries)
    wanted = {"pilot-iocp-N1us": 0.0125, "pilot-condvar-N100us": 0.05}
    for e in entries:
        synth.write_process(folder, e, first=synth.abpair_output(e, ratios_for(wanted.get(e["cell"], 0.010))))
    return path, folder


class Pilot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="p1v2-pilot-")
        cls.dir = os.path.join(cls.tmp, "linux")
        os.makedirs(cls.dir)
        entries = plan.pilot_plan_entries("linux")
        cls.plan = os.path.join(cls.tmp, "pilot-linux.jsonl")
        plan.write_plan(cls.plan, plan.header("linux", "test"), entries)
        wanted = {"pilot-epoll-N0us": 0.010, "pilot-epoll-N1us": 0.0125, "pilot-epoll-N10us": 0.015,
                  "pilot-epoll-N100us": 0.05}
        for e in entries:
            j = int(e["id"].rsplit("-p", 1)[1])
            if e["cell"] == "pilot-condvar-N0us":
                continue  # no output at all
            ratios = ratios_for(wanted.get(e["cell"], 0.010))
            if e["cell"] == "pilot-io_uring-N0us" and j == 1:
                # Fails twice: its 2 pairs are missing, 6 remain.
                synth.write_process(cls.dir, e, first=synth.abpair_output(e, ratios, error="a window did not finish"),
                                    rerun=synth.abpair_output(e, ratios, drop_windows=2), exits=[4, 4])
                continue
            if e["cell"] == "pilot-io_uring-N1us" and j == 0:
                # Fails once; the rerun's data replace it.
                synth.write_process(cls.dir, e, first=synth.abpair_output(e, [5.0, 5.0], drop_windows=1),
                                    rerun=synth.abpair_output(e, ratios), exits=[4, 0])
                continue
            synth.write_process(cls.dir, e, first=synth.abpair_output(e, ratios))
        cls.windows_plan, cls.windows_dir = write_windows(cls.tmp)
        cls.out = os.path.join(cls.tmp, "pilot.json")
        cls.markdown = os.path.join(cls.tmp, "h4-r.md")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            pilot.main(["--linux", cls.plan, cls.dir, "--windows", cls.windows_plan, cls.windows_dir,
                        "--out", cls.out, "--markdown", cls.markdown, "--draft"])
        with open(cls.out, "r", encoding="utf-8") as f:
            cls.doc = json.load(f)
        with open(cls.markdown, "r", encoding="utf-8") as f:
            cls.block = f.read()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_section_7_examples_from_data(self):
        cells = self.doc["cells"]
        self.assertAlmostEqual(cells["linux-epoll-N0us"]["s"], 0.010, delta=1e-6)
        self.assertEqual(cells["linux-epoll-N0us"]["R"], 14)
        self.assertAlmostEqual(cells["linux-epoll-N0us"]["power"], 0.92, delta=0.005)
        self.assertEqual(cells["linux-epoll-N1us"]["R"], 18)
        self.assertAlmostEqual(cells["linux-epoll-N1us"]["power"], 0.86, delta=0.005)
        self.assertEqual(cells["linux-epoll-N10us"]["R"], 20)
        self.assertAlmostEqual(cells["linux-epoll-N10us"]["power"], 0.8001, delta=0.0001)
        self.assertFalse(cells["linux-epoll-N10us"]["underpowered"])

    def test_underpowered(self):
        c = self.doc["cells"]["linux-epoll-N100us"]
        self.assertEqual((c["R"], c["underpowered"]), (24, True))

    def test_missing_pairs(self):
        c = self.doc["cells"]["linux-io_uring-N0us"]
        self.assertEqual(c["pairs"], 6)
        self.assertEqual(c["note"], "6 pairs, not 8")
        none = self.doc["cells"]["linux-condvar-N0us"]
        self.assertEqual((none["pairs"], none["R"], none["underpowered"], none["s"]), (0, 24, True, None))

    def test_rerun_replaces(self):
        c = self.doc["cells"]["linux-io_uring-N1us"]
        self.assertEqual(c["pairs"], 8)
        self.assertAlmostEqual(c["s"], 0.010, delta=1e-6)
        self.assertEqual(c["processes"][0]["used"], "rerun")

    # --- the JSON record -------------------------------------------------------------------------

    def test_the_record_holds_all_twenty_cells(self):
        self.assertEqual(sorted(self.doc["cells"]), sorted(h4log.expected_cells()))
        self.assertNotIn("stand_in", self.doc)  # the pilot makes no stand-in; plan.py has --stand-in-r
        self.assertTrue(self.doc["generated_by"].startswith("analysis/v2/pilot.py "))
        self.assertTrue(self.doc["generated_by"].endswith(" draft"), self.doc["generated_by"])
        self.assertEqual(self.doc["cells"]["windows-iocp-N1us"]["R"], 18)
        self.assertEqual(len(self.doc["cells"]["linux-epoll-N0us"]["log_ratios"]), 8)  # the full record

    def test_one_system_without_a_block(self):
        out = os.path.join(self.tmp, "linux-only.json")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(pilot.main(["--linux", self.plan, self.dir, "--out", out, "--draft"]), 0)
        with open(out, "r", encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["cells"]), len(grid.h4_cells("linux")))

    # --- the block for the revision log ----------------------------------------------------------

    def test_the_block_matches_the_record(self):
        rows = h4log.parse_block(self.block)
        self.assertEqual(len(rows), 20)
        for key, row in rows.items():
            cell = self.doc["cells"][key]
            self.assertEqual((row["R"], row["underpowered"], row["pairs"]),
                             (cell["R"], cell["underpowered"], cell["pairs"]), key)
            self.assertEqual((row["system"], row["backend"], row["work_us"]),
                             (cell["system"], cell["backend"], cell["work_us"]), key)
            if cell["s"] is None:
                self.assertIsNone(row["s"])
            else:
                self.assertEqual(row["s"], cell["s"])  # exactly: s is written at full precision
        self.assertEqual((rows["windows-iocp-N1us"]["R"], rows["windows-condvar-N100us"]["R"]), (18, 24))
        self.assertTrue(rows["windows-condvar-N100us"]["underpowered"])
        self.assertIn("| linux-condvar-N0us | linux | condvar | 0 | 0 | - | - | 24 | yes |", self.block)
        self.assertEqual(self.block.count("\n"), 24)  # begin, header, separator, 20 rows, end

    def test_the_block_feeds_the_plan(self):
        log = os.path.join(self.tmp, "revision-log-v2.md")
        with open(log, "w", encoding="utf-8", newline="\n") as f:
            f.write("# Revision log\n\n## 2026-11-01 R from the A/A pilot\n\nThe pilot's block:\n\n" + self.block
                    + "\nEnd of the entry.\n")
        rows, _ = h4log.read_log(log)
        for system, key, want in (("linux", ("epoll", 1), 9), ("windows", ("iocp", 1), 9), ("linux", ("epoll", 100), 12)):
            r_by_cell = plan.r_for_system(system, lambda k: rows[k]["R"])
            entries = plan.campaign_entries(system, r_by_cell)
            name = grid.h4_cell_name(key[0], key[1], "none")
            self.assertEqual(sum(1 for e in entries if e["cell"] == name), want, (system, key))
        # N = 0 is planned too, at the R the log holds for it.
        r_linux = plan.r_for_system("linux", lambda k: rows[k]["R"])
        self.assertEqual(r_linux[("condvar", 0)], 24)
        self.assertEqual(len(r_linux), 12)

    def test_the_block_needs_both_systems(self):
        out = os.path.join(self.tmp, "one.json")
        md = os.path.join(self.tmp, "one.md")
        for args in (["--linux", self.plan, self.dir], ["--windows", self.windows_plan, self.windows_dir]):
            with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()) as err:
                pilot.main(args + ["--out", out, "--markdown", md])
            self.assertEqual(cm.exception.code, 2)
            self.assertIn("needs --linux and --windows", err.getvalue())
        self.assertFalse(os.path.exists(out))
        self.assertFalse(os.path.exists(md))

    # --- provenance: the same rule as a campaign plan ----------------------------------------------

    def run_in(self, repo, *extra, out="record.json"):
        out = os.path.join(self.tmp, out)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return pilot.main(["--linux", self.plan, self.dir, "--out", out, "--draft"] + list(extra), repo=repo), out

    def test_a_draft_may_come_from_a_dirty_tree(self):
        t = TempGit(self)
        t.write(t.repo, "x.txt", "x\n")
        t.commit_all(t.repo)
        t.write(t.repo, "new.txt", "x\n")
        rc, out = self.run_in(t.repo)
        self.assertEqual(rc, 0)
        with open(out, "r", encoding="utf-8") as f:
            doc = json.load(f)
        self.assertTrue(doc["generated_by"].startswith("analysis/v2/pilot.py ") and doc["generated_by"].endswith(" draft"))
        self.assertIs(doc["draft"], True)
        # The plan here is a draft of no real form: the record says so instead of refusing.
        self.assertTrue(any("not of the real form" in w for w in doc["warnings"]), doc["warnings"])

    def test_the_block_is_exact(self):
        # s is written at full precision, so the revision log's row gives back the pilot's own s to the last bit.
        rows = h4log.parse_block(self.block)
        for key, row in rows.items():
            self.assertEqual(row["s"], self.doc["cells"][key]["s"], key)
            if row["s"] is not None:
                r, _, under = stats.pilot_choose_r(row["s"])
                self.assertEqual((row["R"], row["underpowered"]), (r, under), key)

    def test_there_is_no_commit_option(self):
        with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()) as err:
            pilot.main(["--linux", self.plan, self.dir, "--out", os.path.join(self.tmp, "c.json"),
                        "--commit", "abc123"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("unrecognized arguments", err.getvalue())

    def test_there_is_no_stand_in_option(self):
        with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
            pilot.main(["--stand-in", "14", "--out", os.path.join(self.tmp, "x.json")])
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse(hasattr(pilot, "stand_in"))


class RealPilot(unittest.TestCase):
    """pilot.py without --draft, on a real pilot plan of plan.py made in a throwaway repository."""

    def setUp(self):
        self.t = TempGit(self)
        self.repo = self.t.repo
        self.t.write(self.repo, "x.txt", "x\n")
        self.t.commit_all(self.repo)
        self.plans = os.path.join(self.t.root, "plans")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            plan.main(["pilot", "--out-dir", self.plans, "--system", "windows"], repo=self.repo)
        self.plan = os.path.join(self.plans, "pilot-windows.jsonl")
        self.data = os.path.join(self.t.root, "data")
        os.mkdir(self.data)
        _, self.entries = readers.load_plan(self.plan)
        for e in self.entries:
            synth.write_process(self.data, e, first=synth.abpair_output(e, ratios_for(0.010)))
        self.out = os.path.join(self.t.root, "record.json")
        self.md = os.path.join(self.t.root, "block.md")

    def main(self, *extra, plan_path=None):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return pilot.main(["--windows", plan_path or self.plan, self.data, "--out", self.out] + list(extra),
                              repo=self.repo)

    def refused(self, *extra, plan_path=None):
        with self.assertRaises(SystemExit) as cm:
            self.main(*extra, plan_path=plan_path)
        self.assertFalse(os.path.exists(self.out), "a refused record was written")
        message = str(cm.exception.code)
        self.assertTrue(message.startswith("pilot.py: refused: "), message)
        return message

    def record(self):
        with open(self.out, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_a_clean_tree_and_a_real_plan_are_recorded(self):
        self.assertEqual(self.main(), 0)
        doc = self.record()
        self.assertEqual(doc["generated_by"], "analysis/v2/pilot.py " + self.t.head(self.repo))
        self.assertIs(doc["draft"], False)
        self.assertEqual(doc["warnings"], [])
        self.assertEqual(doc["bench_commits"], {synth.BENCH_COMMIT: 32})

    def test_a_dirty_tree_is_refused_and_nothing_is_written(self):
        self.t.write(self.repo, "new.txt", "x\n")
        self.assertIn("not clean", self.refused())

    def test_a_plan_header_of_another_form_is_refused(self):
        _, entries = readers.load_plan(self.plan)
        for by in ("test", "analysis/v2/plan.py " + "a" * 40 + " draft", "analysis/v2/plan.py " + "a" * 41,
                   "analysis/v2/plan.py " + "a" * 40 + " revision-log-v2.md sha256:" + "b" * 64):
            path = os.path.join(self.t.root, "odd.jsonl")
            plan.write_plan(path, plan.header("windows", by), entries)
            self.assertIn("not of the real form", self.refused(plan_path=path), by)

    def test_a_draft_plan_is_refused_outside_draft_and_warned_about_inside_it(self):
        _, entries = readers.load_plan(self.plan)
        path = os.path.join(self.t.root, "pilot-windows.draft.jsonl")
        plan.write_plan(path, plan.header("windows", plan.generated_by("a" * 40, "draft")), entries)
        self.assertIn("not of the real form", self.refused(plan_path=path))
        self.assertEqual(self.main("--draft", plan_path=path), 0)
        self.assertTrue(any("not of the real form" in w for w in self.record()["warnings"]))

    def test_the_plan_s_lines_must_be_the_generators(self):
        with open(self.plan, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        e = json.loads(lines[3])
        e["limit_s"] += 1
        lines[3] = json.dumps(e)
        tampered = os.path.join(self.t.root, "tampered.jsonl")
        with open(tampered, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        self.assertIn("1 plan entries differ from the frozen grid", self.refused(plan_path=tampered))
        del lines[3]
        with open(tampered, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        self.assertIn("process lines of the frozen grid are not in the plan", self.refused(plan_path=tampered))
        self.assertEqual(self.main("--draft", plan_path=tampered), 0)  # a warning in a draft

    def test_the_processes_run_in_the_shuffled_order(self):
        with open(self.plan, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        lines[3], lines[4] = lines[4], lines[3]
        swapped = os.path.join(self.t.root, "swapped.jsonl")
        with open(swapped, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        self.assertIn("the processes are not in the order shuffled with seed 20261026", self.refused(plan_path=swapped))
        self.assertEqual(self.main("--draft", plan_path=swapped), 0)

    def test_one_build_commit_for_every_process(self):
        e = self.entries[5]
        synth.write_process(self.data, e, first=synth.abpair_output(e, ratios_for(0.010),
                                                                   summary={"bench_commit": synth.OTHER_COMMIT}))
        self.assertIn("built from 2 commits, not one", self.refused())
        self.assertEqual(self.main("--draft"), 0)
        doc = self.record()
        self.assertEqual(doc["bench_commits"], {synth.BENCH_COMMIT: 31, synth.OTHER_COMMIT: 1})
        self.assertTrue(any("built from 2 commits" in w for w in doc["warnings"]))

    def test_the_settings_of_a_pilot_process_are_checked_like_a_campaign_s(self):
        e = self.entries[5]
        synth.write_process(self.data, e, first=synth.abpair_output(e, ratios_for(0.010), summary={"build_type": "Debug"}))
        self.assertEqual(self.main(), 0)  # that process is not good; the cell has 6 pairs
        cell = [c for c in self.record()["cells"].values() if any(p["id"] == e["id"] for p in c["processes"])][0]
        self.assertEqual(cell["pairs"], 6)

    def test_the_block_of_a_real_pilot_is_what_h4log_accepts(self):
        # Both systems are needed for the block; the Linux plan is made and filled the same way.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            plan.main(["pilot", "--out-dir", self.plans, "--system", "linux"], repo=self.repo)
        linux = os.path.join(self.plans, "pilot-linux.jsonl")
        linux_data = os.path.join(self.t.root, "linux-data")
        os.mkdir(linux_data)
        _, entries = readers.load_plan(linux)
        for i, e in enumerate(entries):
            synth.write_process(linux_data, e, first=synth.abpair_output(e, ratios_for(0.0125 if i % 2 else 0.011)))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = pilot.main(["--linux", linux, linux_data, "--windows", self.plan, self.data, "--out", self.out,
                             "--markdown", self.md], repo=self.repo)
        self.assertEqual(rc, 0)
        with open(self.md, "r", encoding="utf-8") as f:
            rows = h4log.parse_block(f.read())
        self.assertEqual(len(rows), 20)


if __name__ == "__main__":
    unittest.main()

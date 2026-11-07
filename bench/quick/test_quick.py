"""Unit tests of the P1 quick benchmarks (quick-plan.md). Nothing here times anything: no wakeprobe run,
no hog, no power change. Fakes stand in for the runner, sysfs, sudo and powercfg.

    python -B bench/quick/test_quick.py
    QUICK_WAKEPROBE=<build>/bench/wakeprobe python -B bench/quick/test_quick.py   also --validate-args every row
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import quick_common as qc  # noqa: E402
import quick_power  # noqa: E402
import quick_requests  # noqa: E402
import quick_run  # noqa: E402


def rows_of(name):
    return qc.expand(qc.SPECS[name])


class PlanTests(unittest.TestCase):
    def test_committed_plans_match_the_generator(self):
        for name, spec in qc.SPECS.items():
            path = qc.PLANS_DIR / f"{name}.jsonl"
            self.assertEqual(path.read_bytes().replace(b"\r\n", b"\n"), qc.plan_text(spec).encode(), name)

    def test_budget_with_margin(self):
        for name, spec in qc.SPECS.items():
            t = qc.trials_for(spec)
            total = spec.rounds * qc.round_estimate_s(spec, t) * qc.MARGIN
            self.assertLessEqual(total, qc.BUDGET_S, name)
            self.assertFalse(qc.fits(spec, t + 1), f"{name}: trials are not the most that fit")
            self.assertGreaterEqual(total, 0.5 * qc.BUDGET_S, f"{name}: under 5 minutes")

    def test_every_cell_once_per_round_in_a_new_order(self):
        for name, spec in qc.SPECS.items():
            header, rows = rows_of(name)
            cells = {r["cell"] for r in rows}
            orders = []
            for rnd in range(spec.rounds):
                rr = [r for r in rows if r["round"] == rnd]
                self.assertEqual(sorted(r["cell"] for r in rr), sorted(cells), f"{name} round {rnd}")
                orders.append([r["cell"] for r in rr])
                # condition blocks are contiguous and hold one condition each
                blocks = [r["block"] for r in rr]
                self.assertEqual(blocks, sorted(blocks))
                for b in set(blocks):
                    self.assertEqual(len({r["condition"] for r in rr if r["block"] == b}), 1)
            if spec.rounds > 1:
                self.assertGreater(len({tuple(o) for o in orders}), 1, f"{name}: the order never changes")

    def test_ids_unique_and_labels_match(self):
        for name in qc.SPECS:
            _, rows = rows_of(name)
            ids = [r["id"] for r in rows]
            self.assertEqual(len(ids), len(set(ids)))
            for r in rows:
                self.assertEqual(r["args"][r["args"].index("--label") + 1], r["id"])

    def test_r1_arms(self):
        for name, backends in (("quick-L-r1", 3), ("quick-W-r1", 2)):
            _, rows = rows_of(name)
            r0 = [r for r in rows if r["round"] == 0]
            # per condition: backends x bounds x (2 variants x 2 protocols + 1 calibration), plus the A/A pair when quiet
            self.assertEqual(len(r0), 3 * backends * 2 * 5 + 2)
            self.assertEqual({r["condition"] for r in r0}, {"quiet", "load", "power"})
            aa = [r for r in r0 if r["arm"] == "none-aa"]
            self.assertEqual(len(aa), 2)
            for a in aa:
                twin = [r for r in r0 if r["cell"] == a["cell"].replace("none-aa", "none")]
                self.assertEqual(len(twin), 1)
                strip = lambda args: [x for i, x in enumerate(args) if i == 0 or args[i - 1] != "--label"]
                self.assertEqual(strip(twin[0]["args"]), strip(a["args"]))
            self.assertEqual({r["variant"] for r in r0}, {"none", "plain_wake", "absent_wake"})
            self.assertTrue(all(r["posts"] == 1 for r in r0 if r["variant"] == "absent_wake"))

    def test_r2_and_r3_calibrations(self):
        _, rows = rows_of("quick-L-r2")
        for r in rows:
            self.assertIn("--calibrate-idle", r["args"])
            cal = r["args"][r["args"].index("--calibrate") + 1]
            self.assertEqual(cal, "11" if r["variant"] == "absent_wake" else "0")
        _, rows = rows_of("quick-W-r3")
        self.assertEqual({r["variant"] for r in rows}, {"absent_wake", "none", "plain_wake"})
        for r in rows:
            self.assertEqual(r["args"][r["args"].index("--gap-unit") + 1], "timer")

    def test_seeds_are_new_and_shared_per_round(self):
        for name, spec in qc.SPECS.items():
            _, rows = rows_of(name)
            for rnd in range(spec.rounds):
                seeds = {r["gap_seed"] for r in rows if r["round"] == rnd and r["gap_seed"] is not None}
                self.assertEqual(len(seeds), 1)
                self.assertFalse(seeds & qc.FROZEN_SEEDS)

    def test_placement(self):
        for name in qc.SPECS:
            _, rows = rows_of(name)
            for r in rows:
                a = r["args"]
                self.assertEqual(a[a.index("--worker-cpus") + 1], "14")
                self.assertEqual(a[a.index("--poster-cpu") + 1], "2")
        self.assertFalse({0, 1, 2, 14} & set(qc.LOAD_CPUS))

    @unittest.skipUnless(os.environ.get("QUICK_WAKEPROBE"), "set QUICK_WAKEPROBE to a built wakeprobe")
    def test_every_row_validates(self):
        exe = os.environ["QUICK_WAKEPROBE"]
        osname = quick_run.this_os()
        for name, spec in qc.SPECS.items():
            if spec.os != osname:
                continue
            _, rows = rows_of(name)
            for r in {tuple(r["args"][:-2]): r for r in rows}.values():
                p = subprocess.run([exe, *r["args"], "--validate-args"], capture_output=True, text=True, timeout=60)
                self.assertEqual((p.returncode, p.stdout.strip()), (0, "valid"), f"{r['id']}: {p.stderr}")


class RunnerTests(unittest.TestCase):
    def test_sha_refusal(self):
        with self.assertRaises(quick_run.Refusal):
            quick_run.read_plan("quick-L-r1", "0" * 64)
        sha = qc.file_sha256(qc.PLANS_DIR / "quick-L-r1.jsonl")
        header, rows = quick_run.read_plan("quick-L-r1", sha)
        self.assertEqual(header["plan"], "quick-L-r1")
        with self.assertRaises(quick_run.Refusal):
            quick_run.read_plan("../quick-L-r1", sha)

    def test_release_check(self):
        with tempfile.TemporaryDirectory() as d:
            b = Path(d)
            (b / "bench").mkdir()
            (b / "bench" / "wakeprobe").write_text("")
            cc = b / "compile_commands.json"
            cc.write_text(json.dumps([{"file": "/s/bench/wakeprobe.cpp", "command": "clang++ -O3 -DNDEBUG -c x"}]))
            self.assertEqual(quick_run.check_release(b, "linux")["level"], "-O3")
            cc.write_text(json.dumps([{"file": "/s/bench/wakeprobe.cpp", "command": "clang++ -O2 -c x"}]))
            with self.assertRaises(quick_run.Refusal):
                quick_run.check_release(b, "linux")
            cc.write_text(json.dumps([{"file": "/s/bench/wakeprobe.cpp", "command": "clang++ -O3 -O0 -c x"}]))
            with self.assertRaises(quick_run.Refusal):
                quick_run.check_release(b, "linux")
            (b / "bench" / "wakeprobe.exe").write_text("")
            cc.write_text(json.dumps([{"file": "C:/s/bench/wakeprobe.cpp", "command": "cl.exe /O2 /Ob2 /DNDEBUG"}]))
            self.assertEqual(quick_run.check_release(b, "windows")["level"], "/O2")
            cc.write_text(json.dumps([{"file": "C:/s/bench/wakeprobe.cpp", "command": "cl.exe /Od /Ob0"}]))
            with self.assertRaises(quick_run.Refusal):
                quick_run.check_release(b, "windows")

    def _fake_run(self, header, rows, clock_step, budget=qc.BUDGET_S):
        t = [0.0]
        seen = []

        def clock():
            return t[0]

        def runner(exe, row, out):
            t[0] += clock_step
            seen.append(row["id"])
            return {"id": row["id"], "rc": 0, "wall_s": clock_step}

        quiet_header = dict(header, conditions=["quiet"])
        quiet_rows = [dict(r, condition="quiet") for r in rows]  # no hog and no power change in a test
        job = quick_run.run_plan(quiet_header, quiet_rows, "x", Path("."), None, budget, runner, clock)
        return job, seen

    def test_run_order_is_the_plans(self):
        header, rows = rows_of("quick-L-r2")
        job, seen = self._fake_run(header, rows, 0.01)
        self.assertEqual(seen, [r["id"] for r in rows])
        self.assertEqual(job["rounds_run"], header["rounds"])
        self.assertIsNone(job["cut"])

    def test_budget_cut(self):
        header, rows = rows_of("quick-L-r2")
        per_round = len([r for r in rows if r["round"] == 0])
        job, seen = self._fake_run(header, rows, 100.0 / per_round)  # 100 s a round
        self.assertEqual(job["rounds_run"], 6)  # 6 x 100 = 600; a 7th would pass 600
        self.assertIn("cut rounds 6 of 10", job["cut"])
        self.assertEqual(len(seen), 6 * per_round)

    def test_error_keeps_what_ran(self):
        header, rows = rows_of("quick-L-r2")
        calls = []

        def runner(exe, row, out):
            if len(calls) == 3:
                raise RuntimeError("boom")
            calls.append(row["id"])
            return {"id": row["id"], "rc": 0, "wall_s": 0.0}

        job = quick_run.run_plan(header, rows, "x", Path("."), None, qc.BUDGET_S, runner)
        self.assertEqual([u["id"] for u in job["units"]], calls)
        self.assertIn("boom", job["error"])

    def test_power_start_failure_skips_only_its_block(self):
        header, rows = rows_of("quick-L-r1")
        rows = [dict(r, condition="quiet") if r["condition"] == "load" else r for r in rows]  # no real hog here

        class FakePower:
            calls = 0

            def apply(self):
                FakePower.calls += 1
                if FakePower.calls == 1:
                    raise quick_power.PowerError("did not take")
                return {"snapshot": "s"}

            def restore(self, applied):
                return {"restored": applied["snapshot"]}

        ran = []

        def runner(exe, row, out):
            ran.append(row["id"])
            return {"id": row["id"], "rc": 0, "wall_s": 0.0}

        old = quick_run.RAMP_S
        quick_run.RAMP_S = 0
        try:
            job = quick_run.run_plan(header, rows, "x", Path("."), FakePower(), qc.BUDGET_S, runner)
        finally:
            quick_run.RAMP_S = old
        self.assertNotIn("error", job)
        bad = [b for b in job["blocks"] if b.get("start_error")]
        self.assertEqual(len(bad), 1)
        self.assertEqual((bad[0]["condition"], bad[0]["round"]), ("power", 0))
        skipped = set(bad[0]["skipped"])
        self.assertEqual(skipped, {r["id"] for r in rows if r["round"] == 0 and r["condition"] == "power"})
        self.assertEqual(set(ran), {r["id"] for r in rows} - skipped)
        self.assertEqual(job["rounds_run"], header["rounds"])

    def test_power_restore_failure_ends_the_campaign(self):
        header, rows = rows_of("quick-L-r1")
        rows = [dict(r, condition="quiet") if r["condition"] == "load" else r for r in rows]

        class BadRestore:
            def apply(self):
                return {"snapshot": "s"}

            def restore(self, applied):
                raise quick_power.PowerError("did not come back")

        old = quick_run.RAMP_S
        quick_run.RAMP_S = 0
        try:
            job = quick_run.run_plan(header, rows, "x", Path("."), BadRestore(), qc.BUDGET_S,
                                     lambda e, r, o: {"id": r["id"], "rc": 0, "wall_s": 0.0})
        finally:
            quick_run.RAMP_S = old
        self.assertIn("did not come back", job["error"])
        self.assertLess(len(job["units"]), len(rows))

    def test_dry_run_runs_nothing(self):
        sha = qc.file_sha256(qc.PLANS_DIR / "quick-L-r2.jsonl")
        if quick_run.this_os() != "linux":
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(quick_run.main(["--plan", "quick-L-r2", "--quick", sha, "--commit", "x",
                                                 "--build-dir", ".", "--out-dir", ".", "--dry-run"]), 2)
        sha_w = qc.file_sha256(qc.PLANS_DIR / "quick-W-r2.jsonl")
        name, s = ("quick-W-r2", sha_w) if quick_run.this_os() == "windows" else ("quick-L-r2", sha)
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(quick_run.main(["--plan", name, "--quick", s, "--commit", "x", "--build-dir", d,
                                             "--out-dir", d, "--dry-run"]), 0)
            self.assertIn("nothing run", out.getvalue())
            self.assertEqual(list(Path(d).iterdir()), [])


class PowerTests(unittest.TestCase):
    def _tree(self, d: Path):
        root = d / "cpu"
        for c in (0, 1):
            p = root / f"cpu{c}" / "cpufreq"
            p.mkdir(parents=True)
            (p / "scaling_governor").write_text("performance\n")
            (p / "scaling_min_freq").write_text("3201000\n")
        (root / "cpufreq").mkdir()
        (root / "cpufreq" / "boost").write_text("0\n")
        pr = d / "power.json"
        pr.write_text(json.dumps({"before": {"boost": "1", "governor_by_cpu": {"0": "powersave", "1": "powersave"}}}))
        cr = d / "clock.json"
        cr.write_text(json.dumps({"before": {"cpus": {"0": {"scaling_min_freq": 1102866},
                                                       "1": {"scaling_min_freq": 1102866}}}}))
        return root, pr, cr

    def test_linux_apply_and_restore(self):
        with tempfile.TemporaryDirectory() as d:
            root, pr, cr = self._tree(Path(d))
            lp = quick_power.LinuxPower(str(root), (), str(pr), str(cr))
            lp.precheck()
            applied = lp.apply()
            self.assertEqual((root / "cpu1" / "cpufreq" / "scaling_governor").read_text().strip(), "powersave")
            self.assertEqual((root / "cpu0" / "cpufreq" / "scaling_min_freq").read_text().strip(), "1102866")
            self.assertEqual((root / "cpufreq" / "boost").read_text().strip(), "1")
            lp.restore(applied)
            self.assertEqual((root / "cpu1" / "cpufreq" / "scaling_governor").read_text().strip(), "performance")
            self.assertEqual((root / "cpu0" / "cpufreq" / "scaling_min_freq").read_text().strip(), "3201000")
            self.assertEqual((root / "cpufreq" / "boost").read_text().strip(), "0")

    def test_linux_refuses_without_records(self):
        with tempfile.TemporaryDirectory() as d:
            root, _, _ = self._tree(Path(d))
            with self.assertRaises(quick_power.PowerError):
                quick_power.LinuxPower(str(root), (), "", "").precheck()

    def test_windows_apply_and_restore(self):
        lab, alex = "b1bcd431-7706-4898-b26a-caeee179f68e", "381b4222-f694-41f0-9685-ff5bb260df2e"
        state = {"active": lab}

        def fake(*args):
            if args[0] == "/getactivescheme":
                return f"Power Scheme GUID: {state['active']}  (x)"
            if args[0] == "/setactive":
                state["active"] = args[1]
            return ""

        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "power-restore.json"
            f.write_text(json.dumps({"plan": alex, "at": "x"}))
            wp = quick_power.WindowsPower(str(f), fake)
            os.environ.pop("QUICK_POWER_RESTORE", None)
            self.assertEqual(wp.precheck()["target"], alex)
            applied = wp.apply()
            self.assertEqual(state["active"], alex)
            wp.restore(applied)
            self.assertEqual(state["active"], lab)
            f.unlink()
            with self.assertRaises(quick_power.PowerError):
                wp.precheck()


class RequestTests(unittest.TestCase):
    def test_requests(self):
        c = "a" * 40
        for name, spec in qc.SPECS.items():
            r = quick_requests.request(name, c)
            j = r["jobs"][0]
            self.assertEqual(j["os"], spec.os)
            self.assertIn(qc.file_sha256(qc.PLANS_DIR / f"{name}.jsonl"), j["run"])
            self.assertIn("-DCMAKE_BUILD_TYPE=Release", j["build"])
            self.assertLessEqual(j["expected_s"], qc.BUDGET_S)
            for k in ("stage_after", "consumer", "expected_basis"):
                self.assertIn(k, j)
            q = quick_requests.queue(name, c)["jobs"][0]
            self.assertFalse({"stage_after", "consumer", "expected_basis"} & set(q))
            text = json.dumps(r) + json.dumps(q)
            self.assertNotIn("\u2013", text)
            self.assertNotIn("\u2014", text)


if __name__ == "__main__":
    unittest.main()

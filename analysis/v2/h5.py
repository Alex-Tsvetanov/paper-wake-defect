"""H5 of P1 v2 (hypotheses-v2.md, section 3; exploratory): the 2026-09 delay-model fit and
bound-independence check, measured again on the laptop with v2's binary.

  python h5.py [--linux DIR] [--windows DIR] [--records DIR ...] [--draft] --out RESULTS.json

DIR is a system's H5 output folder as bench/run_h5.sh or run_h5.ps1 leaves it: plan.txt, gate.txt
and per cell <cell>.jsonl, plus <cell>-rerun.jsonl when the runner ran the cell again.

The criteria and their code are 2026-09's: analysis/summarize.py's loader (load_run), matrix_rows,
gap_sweep_tables and bound_sweep_tables, and its limits H1_LIMIT and H2_LIMIT. One thing differs.
2026-09 read a run's arm from its build (defect_active). v2 has one binary, and H5's fixed arm,
plain_wake, is one of its defect variants, so its runs say defect_active true. Here the arm comes
from H5's mapping: plain_wake is fixed; absent_wake on epoll and IOCP and unobserved_primitive on
io_uring are defect. The run then goes to load_run with defect_active set from the mapping, so
every check of the loader runs unchanged (the recomputed median, predicted_ns from phase and
B_eff, the calibration intervals and the bimodality guard).

The design is the runners' (bench/run_h5.sh and run_h5.ps1): per system the cells of each design
(DESIGN_CELLS), their posts and warm-up (DESIGN_SIZES), and one worker on CPU 14 with the poster
on CPU 2 (section 8's placement, FIXED_RUN). plan.txt must hold that design, and every run must
match its plan line field by field.

Runs (section 8, "the same rules for lost units"). A run fails when it left no output (only its
.err file), has no summary, reports an error, lost a unit, delivered fewer measured units than
planned, or is not a Release build from a clean tree (readers.py's rules). The runner reruns a
failed cell once into <cell>-rerun.jsonl, whose data replace the first run's; both runs are
reported. A cell whose used run failed is incomplete. A lost unit in a fixed cell is reported as
a lost wake.

Verdicts, as 2026-09 stated them (paper/main.tex, its table of hypotheses):
  delay-model fit: a defect cell (a matrix run, or the pooled steps of a gap-sweep cell) passes
    when its median |residual| / B_eff is below H1_LIMIT. A bimodal cell has no verdict, as in
    2026-09. A gap-sweep cell with an incomplete step has none either. A configuration is
    (system, timer setting, B): it fails when one of its cells fails; it passes when no cell is
    incomplete, at least one has a verdict and every verdict passes; otherwise it has none.
  bound independence: a group is (system, backend, timer setting) of the bound sweep; it passes
    when the largest fixed-arm median over the smallest across B is at most H2_LIMIT. With an
    incomplete run the ratio of the rest can only be smaller, so the group fails if the rest
    fails and has no verdict otherwise.
H5's reading (section 3), fixed in revision-log-v2.md (2026-10-29) before any H5 value was read:
  part 1 recurs when the failing configurations are exactly (windows, timer=1ms, 1000);
  part 2 recurs when every Linux group fails and every Windows group passes;
  (a) both parts recur, (b) neither part recurs, (c) exactly one recurs. A part is undetermined
  when configurations or groups without a verdict could decide it either way, or when a system
  has no bound-sweep group; then so is the outcome. Without both systems there is no reading.

Provenance, as score.py: the commit of a clean tree. The scoring is refused when gate.txt has no
passing line or has a BINARY-CHANGED line; when the records gate it records fails provenance.py's
check against the --records folders (the lines and records score.py checks in run.txt); when the
wakeprobe sha256 in plan.txt is not the one gate.txt hashed; when plan.txt's cell count differs
from its header or its cells are not the runners' design; when a planned cell left no file at
all or an output is not planned; when a run differs from its plan line, from FIXED_RUN or from
H5's mapping; or when the runs were not all built from one commit (provenance.commit_problem)
across both systems. With --draft (tests, dry runs) the gate's problems are warnings and
no clean tree is needed.

Reported with no verdict: each Windows run whose timer readings (start, gaps, end) differ from
what it asked for, and each Linux run that asked for 1 ns of slack and did not get it. H5 is
exempt from section 1's settings, so these are notes on validity, not rules.

Output: one JSON file.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.dirname(HERE))  # analysis/, for 2026-09's summarize.py

import argparse  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import tempfile  # noqa: E402
from collections import defaultdict  # noqa: E402
from pathlib import Path  # noqa: E402

import plan as plan_mod  # noqa: E402
import provenance  # noqa: E402
import readers  # noqa: E402
import stats  # noqa: E402
import summarize as s2609  # noqa: E402

FIXED_VARIANT = "plain_wake"
DEFECT_VARIANT = {"epoll": "absent_wake", "iocp": "absent_wake", "io_uring": "unobserved_primitive"}
BACKENDS = {"linux": ("epoll", "io_uring"), "windows": ("iocp",)}
DESIGNS = ("matrix", "gap-sweep", "bound-sweep")
DESIGN_CELLS = {"linux": {"gap-sweep": 120, "bound-sweep": 8, "matrix": 12},  # run_h5.sh: 140
                "windows": {"gap-sweep": 120, "bound-sweep": 8, "matrix": 8}}  # run_h5.ps1: 136
DESIGN_SIZES = {"gap-sweep": (21, 3), "bound-sweep": (51, 5), "matrix": (51, 5)}  # (posts, warm-up), as 2026-09
FIXED_RUN = {"workers": 1, "protocol": "single", "calibrate": 11, "worker_cpus": [14], "poster_cpu": 2,
             "worker_cpus_seen": [14], "poster_cpu_seen": 2}
GATE_PASSED = "records gate passed: "
SPLIT_DELAY = ("windows", "timer=1ms", 1000)  # 2026-09's one failing delay-model configuration
ONE_MS_US = 1000
CELLS_HEADER = re.compile(r"#\s*cells:\s*(\d+)")
FIELDS_HEADER = re.compile(r"#\s*fields:\s*(.+)")
WAKEPROBE_SHA = re.compile(r"#\s*wakeprobe sha256:\s*([0-9a-f]{64})")
BINARY_SHA = re.compile(r"binary wakeprobe sha256 ([0-9a-f]{64})")


class Refusal(Exception):
    pass


def h5_arm(backend, variant):
    """H5's arm of a run (module text), or None when the backend or the variant is not one of H5's."""
    if backend not in DEFECT_VARIANT:
        return None
    if variant == FIXED_VARIANT:
        return "fixed"
    return "defect" if DEFECT_VARIANT[backend] == variant else None


# ---------------------------------------------------------------------------------------------
# plan.txt and gate.txt
# ---------------------------------------------------------------------------------------------


def _value(text):
    """A plan field: 'gap=2000' and 'frac=' carry their name (run_h5.ps1); an empty value is None."""
    _, sep, rest = text.partition("=")
    text = rest if sep else text
    return text or None


def read_plan(folder):
    """plan.txt: {"cells": [fields by name], "wakeprobe_sha256": ...}. Refusal if its headers disagree."""
    text = (Path(folder) / "plan.txt").read_text(encoding="utf-8")
    cells, count, names, sha = [], None, None, None
    for line in text.splitlines():
        line = line.strip()
        for pattern in (CELLS_HEADER, FIELDS_HEADER, WAKEPROBE_SHA):
            m = pattern.match(line)
            if m and pattern is CELLS_HEADER:
                count = int(m.group(1))
            elif m and pattern is FIELDS_HEADER:
                names = m.group(1).split()
            elif m:
                sha = m.group(1)
        if not line or line.startswith("#"):
            continue
        values = line.split()
        if names is None or len(values) != len(names):
            raise Refusal("%s: plan.txt line %r does not match its fields header" % (folder, line))
        cells.append(dict(zip(names, [values[0]] + [_value(v) for v in values[1:]])))
    if count is None or count != len(cells):
        raise Refusal("%s: plan.txt lists %d cells against its header's %s" % (folder, len(cells), count))
    return {"cells": cells, "wakeprobe_sha256": sha}


def design_problem(system, cells, design_cells):
    """Why plan.txt's cells are not the runners' design (DESIGN_CELLS, DESIGN_SIZES), or None."""
    counts = {d: sum(p["design"] == d for p in cells) for d in set(design_cells[system]) | {p["design"] for p in cells}}
    if counts != design_cells[system]:
        return "plan.txt has cells %s, the runner's design is %s" % (counts, design_cells[system])
    for p in cells:
        if (int(p["posts"]), int(p["warmup"])) != DESIGN_SIZES[p["design"]]:
            return "%s plans posts %s and warm-up %s, not %s" % (p["file"], p["posts"], p["warmup"], DESIGN_SIZES[p["design"]])
    return None


def gate_fields(line):
    """The gate lines of run.txt (provenance.GATE_KEYS) from gate.txt's one passing line, whose parts the
    runner joins with '; ', the same separator that joins a key's own list (records, generators)."""
    keys = sorted(provenance.GATE_KEYS, key=len, reverse=True)
    fields, key = {}, None
    for part in line[len(GATE_PASSED):].split("; "):
        new = next((k for k in keys if part.startswith(k + " ")), None)
        if new:
            key = new
            fields[key] = part[len(key) + 1:]
        elif key:
            fields[key] += "; " + part
    return fields


def gate_problems(system, folder, plan, records):
    """Why the folder's run was not gated, as sentences (empty when it was): provenance.gate_problems on gate.txt's
    lines, plus BINARY-CHANGED and the wakeprobe sha256 against plan.txt's."""
    path = Path(folder) / "gate.txt"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    found = ["%s: gate.txt has a BINARY-CHANGED line" % system] if any("BINARY-CHANGED" in x for x in lines) else []
    passed = [x for x in lines if x.startswith(GATE_PASSED)]
    if not passed:
        return found + ["%s: gate.txt has no '%s' line" % (system, GATE_PASSED.strip())]
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, provenance.RUN_FILE), "w", encoding="utf-8") as f:
            for key, value in gate_fields(passed[0]).items():
                f.write("%s: %s\n" % (key, value))
        found += [p.replace(tmp, str(folder)) for p in provenance.gate_problems(system, tmp, records)]
    hashed = [m.group(1) for m in map(BINARY_SHA.match, lines) if m]
    if not hashed or hashed[0] != plan["wakeprobe_sha256"]:
        found.append("%s: gate.txt hashed wakeprobe as %s, plan.txt says %s" % (
            system, hashed[0] if hashed else None, plan["wakeprobe_sha256"]))
    return found


# ---------------------------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------------------------


def planned_variant(system, p):
    """2026-09's timer-setting label (summarize.variant_of) from a plan line."""
    if system == "windows":
        return s2609.variant_of({"timer_period_ms_requested": int(p["timer_ms"])})[0]
    return s2609.variant_of({"timerslack_ns_requested": int(p["slack_ns"])})[0]


def planned_backend(system, p):
    return p.get("backend") or BACKENDS[system][0]


def plan_mismatch(system, s, p):
    """The first summary field that differs from the plan line or H5's mapping, as text; else None."""
    backend = planned_backend(system, p)
    want = {"design": p["design"], "label": p["arm"], "variant": p["variant"], "backend": backend,
            "wait_us": int(p["bound_us"]), "posts": int(p["posts"]), "warmup": int(p["warmup"])}
    if system == "windows":
        want["timer_period_ms_requested"] = int(p["timer_ms"])
    else:
        want["timerslack_ns_requested"] = int(p["slack_ns"])
    if p.get("gap_us") is not None:
        want["gap_us"] = int(p["gap_us"])
    if p.get("phase_frac") is not None:
        want["phase_frac"] = float(p["phase_frac"])
    want.update(FIXED_RUN)
    for field, value in want.items():
        if not readers._same(s.get(field), value):
            return "summary %s is %r, plan.txt says %r" % (field, s.get(field), value)
    if backend not in BACKENDS[system] or h5_arm(backend, p["variant"]) != p["arm"]:
        return "arm %s, variant %s and backend %s disagree with H5's mapping" % (p["arm"], p["variant"], backend)
    return None


def run_problem(run, posts):
    """Why a run failed (module text), or None."""
    s = run.summary
    if s is None:
        return "no summary line"
    if s.get("error"):
        return "error: %s" % s["error"]
    if run.lost_in_all():
        return "lost unit"
    build = readers._build_problem(s)
    if build:
        return build
    delivered = [r for r in run.measured() if r.get("delay_ns") is not None]
    if len(delivered) != posts:
        return "%d of %d measured units delivered" % (len(delivered), posts)
    return None


def load(path, arm):
    """load_run on the run with defect_active set from H5's arm. Its refusals name the file."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            obj = json.loads(line)
            if obj.get("summary"):
                line = json.dumps(dict(obj, defect_active=arm == "defect"))
        out.append(line)
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / path.name
        copy.write_text("\n".join(out) + "\n", encoding="utf-8")
        try:
            run = s2609.load_run(copy)
        except SystemExit as e:
            raise Refusal(str(e).replace(str(copy), path.name))
    run["path"] = path
    return run


def read_cell(system, folder, p):
    """One planned cell: its runs, the one used, and the loaded run when the cell is complete."""
    stem = p["file"][:-len(".jsonl")]
    runs = {}
    for attempt, suffix in (("first", ""), ("rerun", "-rerun")):
        path, err = folder / (stem + suffix + ".jsonl"), folder / (stem + suffix + ".err")
        if path.is_file():
            run = readers.Run(str(path))
            if run.summary is not None:
                mismatch = plan_mismatch(system, run.summary, p)
                if mismatch:
                    raise Refusal("%s: %s" % (path.name, mismatch))
            runs[attempt] = {"run": run, "path": path, "problem": run_problem(run, int(p["posts"]))}
        elif err.is_file():  # wakeprobe stopped before it opened its output
            runs[attempt] = {"run": None, "path": None, "problem": "no output"}
    if not runs:
        raise Refusal("%s: planned cell %s left no file" % (folder, p["file"]))
    used = "rerun" if "rerun" in runs else "first"
    cell = {"system": system, "design": p["design"], "arm": p["arm"], "backend": planned_backend(system, p),
            "variant": planned_variant(system, p), "wait_us": int(p["bound_us"]), "file": p["file"], "used": used,
            "problems": {k: v["problem"] for k, v in runs.items()},
            "lost_units": {k: v["run"].lost_in_all() for k, v in runs.items() if v["run"]},
            "complete": runs[used]["problem"] is None, "loaded": None}
    if cell["complete"]:
        cell["loaded"] = load(runs[used]["path"], p["arm"])
        cell["loaded"]["system"] = system
    return cell


def read_system(system, folder, records, draft, design_cells=DESIGN_CELLS):
    """Every planned cell of one system, with the gate's problems (a refusal unless draft, then warnings)."""
    folder = Path(folder)
    plan = read_plan(folder)
    design = design_problem(system, plan["cells"], design_cells)
    if design:
        raise Refusal("%s: %s" % (folder, design))
    problems = gate_problems(system, folder, plan, records)
    if problems and not draft:
        raise Refusal("; ".join(problems[:6]) + ("" if len(problems) <= 6 else " (and %d more)" % (len(problems) - 6)))
    files = {p["file"] for p in plan["cells"]}
    reruns = {p["file"][:-len(".jsonl")] + "-rerun.jsonl" for p in plan["cells"]}
    stray = sorted(n for n in os.listdir(folder) if n.endswith(".jsonl") and n not in files | reruns)
    if stray:
        raise Refusal("%s: outputs not in plan.txt: %s" % (folder, ", ".join(stray[:5])))
    cells, keys = [], set()
    for p in plan["cells"]:
        if p["design"] not in DESIGNS:
            raise Refusal("%s: design %s is not one of H5's" % (p["file"], p["design"]))
        cell = read_cell(system, folder, p)
        if cell["loaded"] is not None:
            r = cell["loaded"]
            key = (r["design"], r["backend"], r["variant"], r["wait_us"], r["gap_us"], r["phase_frac"], r["arm"])
            if key in keys:
                raise Refusal("%s: cell %s appears twice" % (p["file"], key))
            keys.add(key)
        cells.append(cell)
    return {"cells": cells, "gate_problems": problems}


def commit_of(cells):
    """The one bench commit of every scored run (provenance.commit_problem). Refusal otherwise."""
    commits = defaultdict(int)
    for c in cells:
        if c["loaded"]:
            commits[c["loaded"]["summary"]["bench_commit"]] += 1
    problem = provenance.commit_problem(dict(commits)) if commits else "no run was scored"
    if problem:
        raise Refusal(problem)
    return next(iter(commits))


# ---------------------------------------------------------------------------------------------
# 2026-09's tables and the verdicts
# ---------------------------------------------------------------------------------------------

DELAY_KEYS = ("backend", "variant", "wait_us", "median_abs_residual_over_beff", "h1_pass", "bimodal")
BOUND_KEYS = ("backend", "variant", "bounds_us", "min_median_us", "max_median_us", "h2_ratio", "h2_pass")


def tables(system, cells):
    """2026-09's delay-model cells and bound-sweep groups of one system, incomplete cells left out (module text)."""
    loaded = [c["loaded"] for c in cells if c["complete"]]
    broken_sweep = {(c["backend"], c["variant"], c["wait_us"]) for c in cells
                    if c["design"] == "gap-sweep" and not c["complete"]}
    by = defaultdict(list)
    for r in loaded:
        if r["design"] == "gap-sweep" and (r["backend"], r["variant"], r["wait_us"]) in broken_sweep:
            continue
        by[r["design"]].append(r)
    matrix = s2609.matrix_rows(by["matrix"])
    _steps, sweep, _posts = s2609.gap_sweep_tables(by["gap-sweep"])
    _rows, bound = s2609.bound_sweep_tables(by["bound-sweep"])
    delay = [dict({k: r[k] for k in DELAY_KEYS}, system=system, design="matrix") for r in matrix if r["arm"] == "defect"]
    delay += [dict({k: c[k] for k in DELAY_KEYS}, system=system, design="gap-sweep", steps=c["steps"]) for c in sweep]
    groups = {(g["backend"], g["variant"]): dict({k: g[k] for k in BOUND_KEYS}, system=system) for g in bound}
    for c in cells:
        if c["design"] == "bound-sweep":
            g = groups.setdefault((c["backend"], c["variant"]), {"system": system, "backend": c["backend"],
                                                                   "variant": c["variant"], "h2_pass": ""})
            g.setdefault("incomplete_runs", 0)
            g["incomplete_runs"] += int(not c["complete"])
    out = []
    for g in groups.values():
        known = g["h2_pass"] if isinstance(g["h2_pass"], bool) else None
        g["verdict"] = "fail" if known is False else ("pass" if known and not g["incomplete_runs"] else "none")
        out.append(g)
    incomplete_delay = [c for c in cells if c["arm"] == "defect" and c["design"] in ("matrix", "gap-sweep")
                        and not c["complete"]]
    return delay, sorted(out, key=lambda g: (g["backend"], g["variant"])), incomplete_delay, sorted(broken_sweep)


def configurations(delay, incomplete):
    """Delay-model verdict per (system, timer setting, B): fail, pass or none (module text)."""
    by = defaultdict(list)
    missing = defaultdict(int)
    for c in delay:
        by[(c["system"], c["variant"], c["wait_us"])].append(c["h1_pass"])
    for c in incomplete:
        missing[(c["system"], c["variant"], c["wait_us"])] += 1
    out = []
    for key in sorted(set(by) | set(missing)):
        known = [v for v in by[key] if isinstance(v, bool)]
        if False in known:
            verdict = "fail"
        elif known and not missing[key]:
            verdict = "pass"
        else:
            verdict = "none"
        out.append({"system": key[0], "variant": key[1], "wait_us": key[2], "cells": len(by[key]),
                    "incomplete_runs": missing[key], "verdict": verdict})
    return out


def part_state(can_recur, can_not):
    return "recurs" if can_recur and not can_not else ("does not recur" if can_not and not can_recur else "undetermined")


def reading(configs, groups, systems):
    """H5's reading (module text): each part's state and the outcome; None without both systems."""
    if set(systems) != {"linux", "windows"}:
        return None
    verdict = {(c["system"], c["variant"], c["wait_us"]): c["verdict"] for c in configs}
    key = verdict.get(SPLIT_DELAY, "none")
    others = [v for k, v in verdict.items() if k != SPLIT_DELAY]
    p1 = part_state("fail" not in others and key in ("fail", "none"),
                    "fail" in others or "none" in others or key in ("pass", "none"))
    linux = [g["verdict"] for g in groups if g["system"] == "linux"]
    windows = [g["verdict"] for g in groups if g["system"] == "windows"]
    if not linux or not windows:
        p2 = "undetermined"
    else:
        p2 = part_state("pass" not in linux and "fail" not in windows,
                        any(v != "fail" for v in linux) or any(v != "pass" for v in windows))
    if "undetermined" in (p1, p2):
        outcome = "undetermined"
    else:
        outcome = {2: "a", 0: "b", 1: "c"}[(p1, p2).count("recurs")]
    return {"part1_delay_model": p1, "part2_bound_independence": p2, "outcome": outcome}


# ---------------------------------------------------------------------------------------------
# Notes (no verdict)
# ---------------------------------------------------------------------------------------------


def timer_note(s):
    """A Windows run's timer readings when one differs from what it asked for (readers._timer_problem's rounding)."""
    asked, coarsest = s.get("timer_period_ms_requested"), s.get("coarsest_timer_us")
    want_us = coarsest if asked == 0 else ONE_MS_US * asked if type(asked) is int else None
    gaps = s.get("timer_resolution_100ns_gap")
    seen = [s.get("timer_resolution_100ns_start")] + (gaps if isinstance(gaps, list) else [gaps])
    seen.append(s.get("timer_resolution_100ns_end"))
    if type(want_us) is not int or any(type(v) is not int or (v + 9) // 10 != want_us for v in seen):
        return {"asked_us": want_us, "readings_100ns": seen}
    return None


def slack_note(s):
    """A Linux run that asked for 1 ns of slack and did not get it (readers._slack_problem)."""
    if s.get("timerslack_ns_requested") == 1 and readers._slack_problem(s):
        return {"problem": readers._slack_problem(s)}
    return None


def notes(cells):
    out = {"timer": [], "slack": [], "lost_wakes": [], "reruns": [], "incomplete": []}
    for c in cells:
        if c["loaded"]:
            s = c["loaded"]["summary"]
            note = timer_note(s) if c["system"] == "windows" else slack_note(s)
            if note:
                out["timer" if c["system"] == "windows" else "slack"].append(dict(note, cell=c["file"]))
        if c["arm"] == "fixed" and any(c["lost_units"].values()):
            out["lost_wakes"].append({"cell": c["file"], "system": c["system"], "lost_units": c["lost_units"]})
        if c["used"] == "rerun":
            out["reruns"].append({"cell": c["file"], "system": c["system"], "problems": c["problems"]})
        if not c["complete"]:
            out["incomplete"].append({"cell": c["file"], "system": c["system"], "problems": c["problems"]})
    return out


def score(inputs, generated_by, records=(), draft=False, design_cells=DESIGN_CELLS):
    """inputs: [(system, folder)]. The H5 record (module text). design_cells: the tests' smaller designs."""
    systems, cells, delay, groups, incomplete, warnings = {}, [], [], [], [], []
    for system, folder in inputs:
        got = read_system(system, folder, list(records), draft, design_cells)
        warnings += got["gate_problems"]
        d, g, inc, broken = tables(system, got["cells"])
        systems[system] = {"cells": len(got["cells"]), "complete": sum(c["complete"] for c in got["cells"]),
                           "gap_sweep_cells_without_verdict": [list(b) for b in broken]}
        cells += got["cells"]
        delay += d
        groups += g
        incomplete += inc
    commit = commit_of(cells)
    configs = configurations(delay, incomplete)
    return {"analysis": "P1 v2 H5, hypotheses-v2.md section 3 (exploratory)", "generated_by": generated_by,
            "draft": draft, "warnings": warnings,
            "criteria": {"source": "analysis/summarize.py", "delay_model_limit": s2609.H1_LIMIT,
                         "bound_ratio_limit": s2609.H2_LIMIT},
            "bench_commit": commit, "systems": systems,
            "delay_model": {"cells": delay, "configurations": configs},
            "bound_independence": {"groups": groups},
            "reading": reading(configs, groups, systems), "notes": notes(cells)}


def main(argv=None, repo=None):
    repo = repo or plan_mod.REPO_ROOT
    ap = argparse.ArgumentParser(description="Score P1 v2's H5 with the 2026-09 criteria.")
    ap.add_argument("--linux", metavar="DIR")
    ap.add_argument("--windows", metavar="DIR")
    ap.add_argument("--records", action="append", default=[], metavar="DIR",
                    help="a folder of sanitizer records (the Papers repo's lab/sanitizer-records and its laptop-w); "
                    "give it once for each, as for score.py")
    ap.add_argument("--draft", action="store_true",
                    help="tests and dry runs: gate problems become warnings, no clean tree is needed, and the output "
                    "ends generated_by in draft")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    inputs = [(s, d) for s, d in (("linux", a.linux), ("windows", a.windows)) if d]
    if not inputs:
        ap.error("give --linux and/or --windows")
    try:
        result = score(inputs, plan_mod.provenance(repo, a.draft, tool="h5.py"), a.records, a.draft)
    except (Refusal, plan_mod.Refusal) as e:
        raise SystemExit("h5.py: refused: %s" % e)
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(stats.jsonable(result), f, indent=1, sort_keys=True)
        f.write("\n")
    for w in result["warnings"]:
        print("warning: " + w, file=sys.stderr)
    r = result["reading"]
    print("H5: %d delay-model configurations failing, %d bound-independence groups failing, %d incomplete cells; "
          "reading %s" % (sum(c["verdict"] == "fail" for c in result["delay_model"]["configurations"]),
                          sum(g["verdict"] == "fail" for g in result["bound_independence"]["groups"]),
                          len(result["notes"]["incomplete"]), r["outcome"] if r else "none (one system)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

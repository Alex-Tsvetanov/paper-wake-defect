# Copyright 2026 Alex Tsvetanov
# SPDX-License-Identifier: Apache-2.0
"""The analysis of P1's quick runs R1 to R3 (quick-plan.md, decision d010 option B): results/quick/summary.json,
report.txt, macros.tex, table bodies and figures from the extracted laptop archives.

    python -I bench/quick/quick_analyse.py JOB_FOLDER [JOB_FOLDER ...] [--out results/quick]

A JOB_FOLDER is one extracted job (the folder that holds out/job.json). Every number comes from the archives,
from results/v2/score.json (the campaign's B_eff, the second threshold line of R1 and R2) and from the plans.

What is computed is fixed by quick-plan.md, section "What is computed", before any data:
- A trial is 21 consecutive measured posts (rounds under batch rounds) of one process. T_med flags a trial when
  5 x the median of its 21 delays exceeds B_eff; T_count when 2 x one delay exceeds B_eff (analysis/v2/score.py).
- Per process: the share of its trials each test flags, and the median delay of its measured units.
- Per cell: the median over its processes (one per round), the spread as min and max, and the interquartile range
  (nearest-rank quartiles, analysis/v2/stats.py) where the cell has at least 5 processes. No test, no Holm step.
- B_eff rules: R1, the median of a condition's 3 calibration rows, with the fallback when fewer than 2 calibrate or
  their values span more than 10% of their median (B on Linux; on Windows the larger of B and coarsest_timer_us).
  R2, decision 15 as frozen (8 of 10, 10%). R3, decision 15 with 6 processes: its count threshold is an open point of
  the plan, so the rule here takes the measured median only when all 6 calibrate within 10% (no count threshold
  then binds) and the fallback when the span exceeds 10%; any other case stops the analysis.
- R3 phase coverage: wakeprobe's phase is the post time minus the worker's last wake, so a phase can exceed one
  period. The position in the wake period is phase mod B_eff, the reduction wakeprobe's own prediction uses; the
  share of posts whose phase exceeds one B_eff is reported beside it.

Refused, with nothing written (rule 1 and the plan): a plan whose sha256 is not the job's, a build level other than
-O3 (Linux) or /O2 (Windows) in any compile_commands entry, a summary that is not Release, dirty, from another
commit or carries an error, a cut or missing round, a process that did not exit 0, or a unit count other than the
plan's.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "analysis" / "v2"))
import grid  # noqa: E402
import stats  # noqa: E402

PLANS = HERE / "plans"
LEVEL = {"linux": "-O3", "windows": "/O2"}
OPT_RE = re.compile(r"(?<![\w-])[-/]O(?:[0-3sgdxz]|b\d|i)?(?![\w])")
CORRECT = ("none", "plain_wake")
R1_MIN_CALIBRATED = 2  # quick-plan.md: fallback if fewer than 2 of the 3 calibration rows calibrate
IQR_MIN = 5
TENTHS = 10
WIN_DEFAULT_100NS = 156250  # the default Windows timer period, 15.625 ms, in the 100 ns units wakeprobe reports


class AnalysisError(Exception):
    """An archive that does not match its plan or rule 1: the analysis stops."""


# ------------------------------------------------------------------------------------------------ reading

def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def lf_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def check_levels(folder: Path, os_name: str) -> None:
    want = LEVEL[os_name]
    for entry in read_json(folder / "build-rel" / "compile_commands.json"):
        cmd = entry.get("command") or " ".join(entry.get("arguments", []))
        levels = {m for m in OPT_RE.findall(cmd) if not m.startswith(("/Ob", "-Ob"))}
        if levels != {want}:
            raise AnalysisError(f"{folder}: {entry['file']} built with {sorted(levels)}, not {want} (rule 1)")


def load_job(folder: Path) -> dict:
    """One job: its plan, checks and processes (plan row, summary, measured units)."""
    job = read_json(folder / "out" / "job.json")
    plan_path = PLANS / f"{job['plan']}.jsonl"
    if lf_sha256(plan_path) != job["quick_sha256"]:
        raise AnalysisError(f"{folder}: plan {job['plan']} sha256 is not the job's {job['quick_sha256']}")
    rows = read_jsonl(plan_path)
    header, rows = rows[0], rows[1:]
    os_name = header["os"]
    if job["build"].get("level") != LEVEL[os_name]:
        raise AnalysisError(f"{folder}: job build level {job['build'].get('level')!r} (rule 1)")
    check_levels(folder, os_name)
    if job["rounds_run"] != job["rounds_planned"] or job["cut"] is not None:
        raise AnalysisError(f"{folder}: rounds {job['rounds_run']} of {job['rounds_planned']}, cut {job['cut']}")
    rc = {u["id"]: u["rc"] for u in job["units"]}
    if set(rc) != {r["id"] for r in rows} or any(v != 0 for v in rc.values()):
        raise AnalysisError(f"{folder}: units do not match the plan or a process did not exit 0")
    procs = []
    for row in rows:
        recs = read_jsonl(folder / "out" / "units" / f"{row['id']}.jsonl")
        summary = recs[-1]
        if not summary.get("summary"):
            raise AnalysisError(f"{row['id']}: no summary line")
        bad = {k: summary.get(k) for k, v in (("build_type", "Release"), ("bench_dirty", False),
                                             ("bench_commit", job["commit"]), ("error", None), ("label", row["id"]))
               if summary.get(k) != v}
        if bad:
            raise AnalysisError(f"{row['id']}: summary fields {bad}")
        measured = [r for r in recs[:-1] if r.get("kind") == "measured"]
        if len(measured) != row["posts"]:
            raise AnalysisError(f"{row['id']}: {len(measured)} measured units, plan {row['posts']}")
        procs.append({"row": row, "summary": summary, "measured": measured})
    return {"folder": str(folder), "header": header, "os": os_name, "job": job, "procs": procs}


# ------------------------------------------------------------------------------------------------ arithmetic

def spread(values: list[float]) -> dict | None:
    if not values:
        return None
    out = {"n": len(values), "median": statistics.median(values), "min": min(values), "max": max(values)}
    if len(values) >= IQR_MIN:
        s = sorted(values)
        out["q1"] = stats.nearest_rank(s, Fraction(1, 4))
        out["q3"] = stats.nearest_rank(s, Fraction(3, 4))
    return out


def beff_rule(os_name: str, bound_us: int, values: list, processes: int, min_calibrated: int | None,
              coarsest_us: float | None) -> dict:
    """The B_eff rule with a given count threshold; min_calibrated None is R3's rule (see the module text)."""
    vals = sorted(v for v in values if isinstance(v, (int, float)) and v > 0)
    out = {"processes": processes, "calibrated": len(vals), "median_ns": None, "span_over_median": None}
    reason = None
    if vals:
        med = statistics.median(vals)
        out.update(median_ns=med, span_over_median=(vals[-1] - vals[0]) / med)
        if Fraction(vals[-1] - vals[0]) > grid.BEFF_MAX_SPAN * Fraction(med):
            reason = "span over 10% of the median"
    if reason is None:
        if min_calibrated is None and len(vals) != processes:
            raise AnalysisError(f"{os_name} {bound_us} us: {len(vals)} of {processes} calibrate; R3's count threshold "
                                "is an open point of the plan and binds here")
        if min_calibrated is not None and len(vals) < min_calibrated:
            reason = f"{len(vals)} of {processes} calibrated"
    if reason is None:
        return out | {"b_eff_ns": out["median_ns"], "source": "measured"}
    b_eff, source = bound_us * 1000, "fallback: B"
    if os_name == "windows":
        if coarsest_us is None:
            raise AnalysisError("the Windows fallback needs coarsest_timer_us")
        if coarsest_us * 1000 > b_eff:
            b_eff, source = coarsest_us * 1000, "fallback: coarsest timer"
    return out | {"b_eff_ns": b_eff, "source": source, "reason": reason}


def coarsest(procs: list[dict]) -> float | None:
    t = [p["summary"].get("coarsest_timer_us") for p in procs]
    t = [x for x in t if isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0]
    return max(t) if t else None


def flags(proc: dict, b_eff: float) -> dict:
    """analysis/v2/score.py's trial rule on one process."""
    per = grid.UNITS_PER_TRIAL
    units = proc["measured"]
    n = len(units) // per
    t_med = t_count = 0
    for t in range(n):
        d = [u["delay_ns"] for u in units[t * per:(t + 1) * per]]
        t_med += grid.T_MED_DIVISOR * stats.median(d) > b_eff
        t_count += any(grid.T_COUNT_DIVISOR * x > b_eff for x in d)
    return {"trials": n, "t_med": t_med, "t_count": t_count, "t_med_share": t_med / n, "t_count_share": t_count / n}


def median_delay(proc: dict) -> float:
    return statistics.median(u["delay_ns"] for u in proc["measured"])


def cell_block(procs: list[dict], b_eff: float) -> dict:
    """Per-process values and their spread for one cell under one threshold."""
    fl = [flags(p, b_eff) for p in procs]
    return {"b_eff_ns": b_eff, "processes": len(procs), "trials": sum(f["trials"] for f in fl),
            "t_med_trials": sum(f["t_med"] for f in fl), "t_count_trials": sum(f["t_count"] for f in fl),
            "t_med_share": spread([f["t_med_share"] for f in fl]),
            "t_count_share": spread([f["t_count_share"] for f in fl]),
            "median_delay_ns": spread([median_delay(p) for p in procs])}


def lost(procs: list[dict]) -> dict:
    s = [p["summary"] for p in procs]
    return {"lost": sum(x.get("lost") or 0 for x in s), "timeouts": sum(x.get("timeouts") or 0 for x in s)}


def campaign_beff(score: dict) -> dict:
    return {(os_name, r["backend"], r["bound_us"]): r["b_eff_ns"] for os_name, rows in score["b_eff"].items()
            for r in rows}


def group(procs: list[dict], *keys) -> dict:
    out: dict = {}
    for p in procs:
        out.setdefault(tuple(p["row"][k] for k in keys), []).append(p)
    for v in out.values():
        v.sort(key=lambda p: p["row"]["round"])
    return out


# ------------------------------------------------------------------------------------------------ R1

def r1(job: dict, camp: dict) -> dict:
    os_name, procs = job["os"], job["procs"]
    co = coarsest(procs)
    calib = [p for p in procs if "-calib-" in p["row"]["id"]]
    probes = [p for p in procs if "-calib-" not in p["row"]["id"]]
    beff = {}
    for (cond, backend, bound), ps in group(calib, "condition", "backend", "wait_us").items():
        beff[(cond, backend, bound)] = beff_rule(os_name, bound, [p["summary"].get("b_eff_ns") for p in ps], len(ps),
                                                 R1_MIN_CALIBRATED, co) | {"values_ns": [p["summary"].get("b_eff_ns")
                                                                                         for p in ps]}
    cells = {}
    for (cond, backend, bound, arm, proto), ps in group(probes, "condition", "backend", "wait_us", "arm",
                                                         "protocol").items():
        cells[(cond, backend, bound, arm, proto)] = {
            "procs": ps, "rounds": [p["row"]["round"] for p in ps],
            "quick": cell_block(ps, beff[(cond, backend, bound)]["b_eff_ns"]),
            "campaign": cell_block(ps, camp[(os_name, backend, bound)]), "lost": lost(ps)}
    shifts = {}
    for (cond, backend, bound, arm, proto), c in cells.items():
        if cond == "quiet" or arm == "none-aa":
            continue
        base = cells[("quiet", backend, bound, arm, proto)]
        shifts[(cond, backend, bound, arm, proto)] = diffs(c["procs"], base["procs"],
                                                          beff[(cond, backend, bound)]["b_eff_ns"],
                                                          beff[("quiet", backend, bound)]["b_eff_ns"])
    aa = {}
    for (cond, backend, bound, arm, proto), c in cells.items():
        if arm == "none-aa":
            b = beff[(cond, backend, bound)]["b_eff_ns"]
            aa[(backend, bound)] = diffs(c["procs"], cells[(cond, backend, bound, "none", proto)]["procs"], b, b)
    return {"os": os_name, "coarsest_timer_us": co, "beff": beff, "cells": cells, "shifts": shifts, "aa": aa,
            "lost": lost(procs), "calib_lost": lost(calib), "processes": len(procs)}


def diffs(a: list[dict], b: list[dict], beff_a: float, beff_b: float) -> dict:
    """Per round, a minus b in the flag shares and the median delay; median and spread over the rounds."""
    by_b = {p["row"]["round"]: p for p in b}
    tm, tc, md = [], [], []
    for p in a:
        q = by_b[p["row"]["round"]]
        fa, fb = flags(p, beff_a), flags(q, beff_b)
        tm.append(fa["t_med_share"] - fb["t_med_share"])
        tc.append(fa["t_count_share"] - fb["t_count_share"])
        md.append(median_delay(p) - median_delay(q))
    return {"t_med_share": spread(tm), "t_count_share": spread(tc), "median_delay_ns": spread(md)}


# ------------------------------------------------------------------------------------------------ R2, R3

def r2(job: dict, camp: dict) -> dict:
    os_name, procs = job["os"], job["procs"]
    co = coarsest(procs)
    out = {}
    for (backend, bound), ps in group(procs, "backend", "wait_us").items():
        fixed = [p for p in ps if p["row"]["arm"] == "none"]
        planted = [p for p in ps if p["row"]["arm"] == "absent_wake"]
        idle = spread([p["summary"]["b_eff_idle_ns"] for p in fixed])
        rule = beff_rule(os_name, bound, [p["summary"].get("b_eff_ns") for p in planted], len(planted),
                         grid.BEFF_MIN_CALIBRATED, co)
        within = spread([p["summary"]["b_eff_idle_ns"] / p["summary"]["b_eff_ns"] for p in planted])
        idle_planted = spread([p["summary"]["b_eff_idle_ns"] for p in planted])
        cells = {}
        for arm, cps in (("none", fixed), ("absent_wake", planted)):
            cells[arm] = {"idle": cell_block(cps, idle["median"]), "planted": cell_block(cps, rule["b_eff_ns"]),
                          "lost": lost(cps)}
        out[(backend, bound)] = {"idle": idle, "planted_rule": rule, "ratio": idle["median"] / rule["b_eff_ns"],
                                 "within": within, "idle_on_planted": idle_planted,
                                 "campaign_ns": camp[(os_name, backend, bound)], "cells": cells,
                                 "processes": {"none": len(fixed), "absent_wake": len(planted)}}
    return {"os": os_name, "coarsest_timer_us": co, "rows": out, "lost": lost(procs), "processes": len(procs)}


def r3(job: dict) -> dict:
    os_name, procs = job["os"], job["procs"]
    co = coarsest(procs)
    out = {}
    for (backend, bound), ps in group(procs, "backend", "wait_us").items():
        planted = [p for p in ps if p["row"]["arm"] == "absent_wake"]
        fixed = [p for p in ps if p["row"]["arm"] == "none"]
        rule = beff_rule(os_name, bound, [p["summary"].get("b_eff_ns") for p in planted], len(planted), None, co)
        idle = spread([p["summary"]["b_eff_idle_ns"] for p in fixed])
        cells = {}
        for arm in ("absent_wake", "none", "plain_wake"):
            cps = [p for p in ps if p["row"]["arm"] == arm]
            cells[arm] = {"planted": cell_block(cps, rule["b_eff_ns"]), "idle": cell_block(cps, idle["median"]),
                          "lost": lost(cps)}
        out[(backend, bound)] = {"planted_rule": rule, "idle": idle, "cells": cells,
                                 "phase": phase_cover(planted, rule["b_eff_ns"]), "gap_unit": gap_unit(ps)}
    return {"os": os_name, "coarsest_timer_us": co, "rows": out, "lost": lost(procs), "processes": len(procs)}


def phase_cover(procs: list[dict], b_eff: float) -> dict:
    phases = [u["phase_ns"] for p in procs for u in p["measured"] if u.get("phase_ns") is not None
              and u["phase_ns"] >= 0]
    counts = [0] * TENTHS
    for ph in phases:
        counts[min(int(TENTHS * (ph % b_eff) / b_eff), TENTHS - 1)] += 1
    n = len(phases)
    missing = sum(len(p["measured"]) for p in procs) - n
    return {"posts": n, "without_phase": missing, "tenths": [c / n for c in counts], "tenth_counts": counts,
            "over_one_period": sum(ph > b_eff for ph in phases) / n}


def gap_unit(procs: list[dict]) -> dict:
    listed = []
    for p in procs:
        s = p["summary"]
        own = s.get("b_eff_ns") or s.get("b_eff_idle_ns")
        unit = s["gap_unit_us"] * 1000
        if abs(unit - own) > float(grid.BEFF_MAX_SPAN) * own:
            listed.append({"id": p["row"]["id"], "gap_unit_ns": unit, "b_eff_ns": own})
    units = sorted({p["summary"]["gap_unit_us"] for p in procs})
    return {"gap_unit_us": units, "processes": len(procs), "over_10pct": listed}


def timer_changed(procs: list[dict]) -> list[str]:
    """Processes whose timer resolution changed while they ran (start, gap readings and end differ)."""
    out = []
    for p in procs:
        s = p["summary"]
        vals = {s.get("timer_resolution_100ns_start"), s.get("timer_resolution_100ns_end"),
                *(s.get("timer_resolution_100ns_gap") or [])}
        if len(vals) > 1:
            out.append(p["row"]["id"])
    return out


def timer_readings(procs: list[dict]) -> dict:
    """Windows only: per condition, how many processes started at each timer resolution (100 ns units)."""
    out: dict = {}
    for p in procs:
        r = p["summary"].get("timer_resolution_100ns_start")
        if isinstance(r, int) and r > 0:
            per = out.setdefault(p["row"]["condition"], {})
            per[str(r)] = per.get(str(r), 0) + 1
    return out


# ------------------------------------------------------------------------------------------------ main

def analyse(folders: list[Path], score_path: Path) -> dict:
    camp = campaign_beff(read_json(score_path))
    jobs = {}
    for folder in folders:
        job = load_job(folder)
        run = job["header"]["run"]
        res = r1(job, camp) if run == "R1" else r2(job, camp) if run == "R2" else r3(job)
        j = job["job"]
        res |= {"timer_start": timer_readings(job["procs"]), "timer_changed": timer_changed(job["procs"]),
                "timer_off_default": [p["row"]["id"] for p in job["procs"] if job["os"] == "windows"
                                      and p["summary"].get("timer_resolution_100ns_start") != WIN_DEFAULT_100NS]}
        res |= {"plan": j["plan"], "plan_sha256": j["quick_sha256"], "commit": j["commit"],
                "level": j["build"]["level"], "rounds": j["rounds_run"], "wall_s": j["wall_s"],
                "trials_per_process": job["header"]["trials_per_process"], "run": run,
                "blocks": [{k: b.get(k) for k in ("condition", "round", "hog_exit")} | {
                    "power_set": "power" in b, "power_restored": "power_restore" in b} for b in j["blocks"]]}
        jobs[j["plan"]] = res
    return jobs


def plain(x):
    """summary.json: tuples as keys become strings; process lists are dropped."""
    if isinstance(x, dict):
        return {(" ".join(map(str, k)) if isinstance(k, tuple) else k): plain(v) for k, v in x.items() if k != "procs"}
    if isinstance(x, list):
        return [plain(v) for v in x]
    return x


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folders", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=REPO / "results" / "quick")
    ap.add_argument("--score", type=Path, default=REPO / "results" / "v2" / "score.json")
    a = ap.parse_args(argv)
    jobs = analyse(a.folders, a.score)
    import quick_paper as P  # noqa: E402  (rendering only)
    outputs = P.render_all(jobs)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "summary.json").write_text(json.dumps(plain(jobs), indent=1) + "\n", encoding="utf-8", newline="\n")
    for rel, text in outputs.items():
        path = a.out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8", newline="\n")
    print(outputs["report.txt"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

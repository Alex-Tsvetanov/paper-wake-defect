"""Scoring of H6 (hypotheses-v2-h6.md, frozen 2026-10-29): Netty's EpollEventLoop, 4.1.0.CR7 against
4.1.0.Final, forced and natural.

  python h6.py --run DIR --records DIR [--draft] --out RESULTS.json

DIR is the output folder of historical/netty/run_h6.py: plan.txt, run.txt and per process <id>.jsonl,
<id>.out (PrintCompilation), <id>.err and <id>.done, plus <id>-rerun.* when the runner ran it again.
--records is the folder that holds the H6 functional records (decision 37).

Rules, from the addendum:
- A process is good (section 5) when its used run exited 0, its summary says good, it matches its plan
  line (arm, Netty version, protocol, process) and the planned sizes and CPUs (its args), and it has
  every measured post: 15 trials of 21, each with a delay; a forced process also has one hook injection
  per forced post. The used run is the rerun when there is one. A cell is complete with 10 good
  processes; an incomplete cell stays in F6's m and is not rejected.
- T_med flags a trial when the median of its 21 delays exceeds B / 5 = 200 ms; T_count when one exceeds
  B / 2 = 500 ms (section 2, B_eff = B = 1 s).
- H6a: CR7 forced, the exact one-sided test of power <= 0.95; Final forced, of a false-positive rate
  >= 0.05; Holm over F6 (m = 2, alpha 0.025).
- H6b (natural, no test): T_med blind by the rule of hypotheses-v2.md section 4 (the prediction fails
  when the exact one-sided 95% lower bound of the flag rate exceeds 0.05); posts over B / 2 with their
  exact interval and one-sided upper bound, listed by trial; T_count; and the predictions' outcomes.
- Diagnostics (section 5): per forced cell the distributions of X's delay, of Y's run time after the
  hook returned and of the hook's hold, the values of wakenUp at the hook and the latch outcomes; per
  natural cell the realized gaps (section 3); per process the warm-up counts, GC, the largest delay,
  lost wakes and whether EpollEventLoop.run() had OSR-compiled code at the first measured post. run()
  is entered once, so only an OSR compile (PrintCompilation's %) can be in use; one counts from its
  line, which JDK 8 prints when the compile starts, until a "made not entrant" line of its compile id.

Provenance: the commit of a clean tree, as score.py. The scoring is refused when plan.txt is not
run_h6.py's plan; when run.txt lacks a passing H6 gate on the laptop's host with CPUs 0, 2 and 14;
when the functional record it names is not in --records with that sha256 or may not gate the run
(run_h6.record_gates: green, a lab job's on the laptop, at run.txt's commit, inputs and JDK); or when
the run did not end (no "ended utc" line, or a planned process without a .done file). --draft turns
these into warnings for tests and dry runs.

Output: one JSON file.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.join(HERE, "..", "..", "historical", "netty"))

import argparse  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
from collections import Counter, defaultdict  # noqa: E402

import grid  # noqa: E402
import plan as plan_mod  # noqa: E402
import run_h6  # noqa: E402
import score as score_mod  # noqa: E402
import stats  # noqa: E402

B_NS = 1000000000
T_MED_NS = B_NS // 5
T_COUNT_NS = B_NS // 2
# A PrintCompilation line: uptime ms, compile id, flags (% is an OSR compile), tier, method, the rest.
COMPILE_LINE = re.compile(r"^\s*(\d+)\s+(\d+)\s+([%sbn! ]*?)\s*(\d)\s+(\S+)(.*)$")
RUN_METHOD = "io.netty.channel.epoll.EpollEventLoop::run"


class Refusal(Exception):
    pass


# ---------------------------------------------------------------------------------------------
# Reading the run
# ---------------------------------------------------------------------------------------------


def read_plan(run_dir):
    with open(os.path.join(run_dir, "plan.txt"), encoding="utf-8") as f:
        entries = [json.loads(l) for l in f.read().splitlines() if l.strip() and not l.startswith("#")]
    if entries != run_h6.plan():
        raise Refusal("plan.txt is not run_h6.py's plan of section 3")
    return entries


def read_run_txt(run_dir):
    fields = {}
    with open(os.path.join(run_dir, "run.txt"), encoding="utf-8") as f:
        for line in f.read().splitlines():
            key, sep, value = line.partition(": ")
            if sep:
                fields.setdefault(key, value)
    return fields


def gate_problems(fields, records_dir):
    """Why run.txt does not show a passing H6 gate with its functional record in records_dir (empty if it does)."""
    if fields.get("h6 gate") != "passed":
        return ["run.txt has no 'h6 gate: passed' line"]
    found = []
    if not run_h6.is_lab_host(fields.get("h6 host")):
        found.append("run.txt's host is %r, not the laptop's %s" % (fields.get("h6 host"), run_h6.LAB_HOST))
    if fields.get("cpus") != ",".join(map(str, run_h6.CPUS)):
        found.append("run.txt's CPUs are %r, not %s" % (fields.get("cpus"), ",".join(map(str, run_h6.CPUS))))
    name, _, sha = (fields.get("h6 functional record") or "").partition(" ")
    path = os.path.join(records_dir or "", name)
    if not name or not os.path.isfile(path):
        return found + ["the functional record %r that run.txt names is not in %s" % (name, records_dir)]
    if run_h6.sha256_file(path) != sha:
        return found + ["the functional record %s has another sha256 than run.txt records" % name]
    with open(path, encoding="utf-8") as f:
        rec = json.load(f)
    if not run_h6.record_gates(rec, fields.get("h6 commit"), fields.get("h6 inputs hash"), fields.get("jdk")):
        found.append("the functional record %s may not gate this run (green, a lab job's on %s, at run.txt's commit, "
                     "inputs and JDK)" % (name, run_h6.LAB_HOST))
    return found


def end_problems(fields, run_dir, entries):
    """Why the run did not end: no 'ended utc' line, or a planned process whose attempts did not end."""
    found = [] if fields.get("ended utc") else ["run.txt has no 'ended utc' line: the run did not end"]
    open_ = [e["id"] for e in entries if not run_h6.ended(read_done(run_dir, e["id"]))]
    if open_:
        found.append("%d planned processes did not end (no .done file, or a rerun owed), e.g. %s"
                     % (len(open_), ", ".join(open_[:3])))
    return found


def read_done(run_dir, pid):
    """{'first': exit[, 'rerun': exit]} from <id>.done (run_h6.read_done), or {}."""
    return run_h6.read_done(os.path.join(run_dir, pid + ".done"))


def resumes(run_dir):
    """How the run was resumed: run.txt's 'resumed utc' lines and the files set aside as void runs."""
    with open(os.path.join(run_dir, "run.txt"), encoding="utf-8") as f:
        stamps = [l.split(": ", 1)[1] for l in f.read().splitlines() if l.startswith("resumed utc: ")]
    return {"resumed_utc": stamps, "void_files": sorted(n for n in os.listdir(run_dir) if ".void-" in n)}


def compile_events(out_path):
    """EpollEventLoop.run()'s PrintCompilation events: (ms, compile id, tier, osr, made_not_entrant), in file order."""
    events = []
    try:
        with open(out_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if RUN_METHOD not in line:
                    continue
                m = COMPILE_LINE.match(line)
                if m and m.group(5) == RUN_METHOD:
                    dead = "made not entrant" in m.group(6) or "COMPILE SKIPPED" in m.group(6)
                    events.append((int(m.group(1)), int(m.group(2)), int(m.group(4)), "%" in m.group(3), dead))
    except OSError:
        pass
    return events


def compiled_at(events, uptime_ms):
    """True when run() had OSR code at uptime_ms: an OSR compile started at or before it whose compile id was not
    made not entrant, or skipped, by then (module text). None when the time is not known."""
    if uptime_ms is None:
        return None
    live = set()
    for ms, cid, tier, osr, dead in events:
        if ms > uptime_ms:
            break
        if not osr:
            continue
        if dead:
            live.discard(cid)
        else:
            live.add(cid)
    return bool(live)


def problem_of(entry, lines, exit_code):
    """None when the used run is good by section 5; else why not."""
    if exit_code != 0:
        return "exit %s" % exit_code
    if not lines:
        return "no output"
    s = lines[-1]
    if not s.get("summary"):
        return "no summary line"
    if s.get("good") is not True:
        return "the probe says not good: %s" % (s.get("problems") or [])[:3]
    want = {"protocol": entry["protocol"], "expected_version": run_h6.ARMS[entry["arm"]],
            "process": entry["process"], "check": False}
    for k, v in want.items():
        if s.get(k) != v:
            return "summary %s is %r, the plan asks %r" % (k, s.get(k), v)
    args = s.get("args") or []
    rest, poster, loop = run_h6.CPUS
    planned_args = list(zip(run_h6.SIZES[0::2], run_h6.SIZES[1::2])) + [
        ("--loop-cpu", str(loop)), ("--poster-cpu", str(poster)), ("--rest-cpu", str(rest))]
    for flag, value in planned_args:
        given = [args[i + 1] for i, a in enumerate(args[:-1]) if a == flag]
        if given != [value]:
            return "args give %s %s, the plan asks %s" % (flag, given, value)
    planned = run_h6.TRIALS * run_h6.POSTS
    posts = [l for l in lines if l.get("kind") == "post"]
    timed = sum(isinstance(p.get("delay_ns"), int) for p in posts)
    if len(posts) != planned or timed != planned:
        return "%d measured posts with a delay, not %d" % (timed, planned)
    if entry["protocol"] == "forced" and s.get("hook_injections") != run_h6.WARMUP_POSTS + planned:
        return "the hook injected %s times" % s.get("hook_injections")
    return None


def read_process(run_dir, entry):
    done = read_done(run_dir, entry["id"])
    used = "rerun" if "rerun" in done else "first"
    stem = os.path.join(run_dir, entry["id"] + ("-rerun" if used == "rerun" else ""))
    lines = run_h6.read_lines(stem + ".jsonl")
    problem = problem_of(entry, lines, done.get(used)) if done else "no .done file"
    summary = lines[-1] if lines and lines[-1].get("summary") else {}
    events = compile_events(stem + ".out")
    lost = 0
    for attempt in done:
        other = run_h6.read_lines(os.path.join(run_dir, entry["id"] + ("-rerun" if attempt == "rerun" else "") + ".jsonl"))
        if other and other[-1].get("summary"):
            lost += other[-1].get("lost_wakes") or 0
    return {"entry": entry, "used": used, "exits": done, "good": problem is None, "problem": problem,
            "lines": lines or [], "summary": summary, "lost_wakes": lost,
            "run_compiled_at_first_post": compiled_at(events, summary.get("first_measured_uptime_ms")),
            "run_compile_events": events}


# ---------------------------------------------------------------------------------------------
# Statistics per cell
# ---------------------------------------------------------------------------------------------


def trials_of(proc):
    """A good process's trials, each the list of its 21 delays in post order."""
    by = defaultdict(list)
    for l in proc["lines"]:
        if l.get("kind") == "post":
            by[l["trial"]].append((l["index"], l["delay_ns"]))
    return [[d for _, d in sorted(by[t])] for t in sorted(by)]


def t_med(delays):
    return stats.median(delays) > T_MED_NS


def t_count(delays):
    return any(d > T_COUNT_NS for d in delays)


def spread(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return {"n": len(values), "min": values[0], "median": stats.median(values), "max": values[-1]}


def process_view(p):
    s = p["summary"]
    trials = [l for l in p["lines"] if l.get("kind") == "trial"]
    placed = None if any(l.get("gc_count") is None for l in trials) else sum(l["gc_count"] for l in trials)
    return {"id": p["entry"]["id"], "good": p["good"], "problem": p["problem"], "used": p["used"],
            "exits": p["exits"], "lost_wakes": p["lost_wakes"],
            "warmup_natural_posts": s.get("warmup_natural_posts"),
            "warmup_natural_over_half_b": s.get("warmup_natural_over_half_b"),
            "run_compiled_at_first_post": p["run_compiled_at_first_post"],
            "gc_measured": s.get("gc_count_measured"), "gc_in_trials": placed,
            "gc_ambiguous": sum(l.get("gc_ambiguous") or 0 for l in trials),
            "gc_time_ms_in_trials": sum(l.get("gc_time_ms") or 0 for l in trials),
            "gc_clock_error_ns": s.get("gc_clock_error_ns"),
            "max_delay_ns": s.get("max_delay_ns"), "timerslack_ns": s.get("timerslack_ns")}


def score_cell(arm, protocol, procs):
    good = [p for p in procs if p["good"]]
    counts_med, counts_count, sizes, over, posts = [], [], [], [], 0
    for p in good:
        trials = trials_of(p)
        counts_med.append(sum(t_med(t) for t in trials))
        counts_count.append(sum(t_count(t) for t in trials))
        sizes.append(len(trials))
        for ti, t in enumerate(trials):
            posts += len(t)
            over += [{"process": p["entry"]["process"], "trial": ti, "index": i, "delay_ns": d}
                     for i, d in enumerate(t) if d > T_COUNT_NS]
    cell = {"arm": arm, "protocol": protocol, "processes_planned": len(procs), "processes_good": len(good),
            "complete": len(good) == run_h6.PROCESSES_PER_CELL,
            "t_med": score_mod.rate_block(counts_med, sizes), "t_count": score_mod.rate_block(counts_count, sizes),
            "posts": posts, "posts_over_half_b": len(over),
            "posts_over_half_b_exact_95": stats.clopper_pearson(len(over), posts) if posts else None,
            "posts_over_half_b_upper_95": stats.cp_upper(len(over), posts, grid.BLIND_CONFIDENCE) if posts else None,
            "over_half_b": over,
            "max_delay_ns": max([p["summary"].get("max_delay_ns") or 0 for p in good], default=None),
            "lost_wakes": sum(p["lost_wakes"] for p in procs),
            "processes": [process_view(p) for p in procs]}
    if protocol == "forced":
        lines = [l for p in good for l in p["lines"] if l.get("kind") == "post"]
        cell["diagnostics"] = {
            "x_delay_ns": spread(l.get("x_delay_ns") for l in lines),
            "y_after_hook_ns": spread(l.get("y_after_hook_ns") for l in lines),
            "hook_held_ns": spread(l.get("hook_held_ns") for l in lines),
            "wakenup_at_hook": dict(Counter(str(l.get("wakenup_at_hook")) for l in lines)),
            "latch_ok": dict(Counter(str(l.get("latch_ok")) for l in lines)),
            "armed_with_tasks": sum(p["summary"].get("hook_armed_with_tasks") or 0 for p in good)}
    else:
        cell["realized_gap_ns"] = spread(l.get("realized_gap_ns") for p in good for l in p["lines"]
                                         if l.get("kind") == "post")
    return cell


def h6a(cells):
    cr7, final = cells[("cr7", "forced")], cells[("final", "forced")]
    p = {"cr7-forced": stats.h1_pvalue(cr7["t_med"]["flags"], cr7["t_med"]["trials"]) if cr7["complete"] else None,
         "final-forced": stats.h2_pvalue(final["t_med"]["flags"], final["t_med"]["trials"]) if final["complete"] else None}
    tests = stats.holm(p)
    return {"family": "F6", "m": 2, "alpha": grid.ALPHA, "tests": tests,
            "rejected": sum(t["rejected"] for t in tests.values()),
            "holds": all(t["rejected"] for t in tests.values())}


def h6b(cells):
    out = {}
    for arm in run_h6.ARMS:
        c = cells[(arm, "natural")]
        tm = c["t_med"]
        lower = stats.cp_lower(tm["flags"], tm["trials"], grid.BLIND_CONFIDENCE) if tm["trials"] else None
        out[arm] = {"t_med_blind_lower_95": lower,
                    "t_med_blind_prediction_fails": lower is not None and lower > grid.BLIND_RATE,
                    "posts_over_half_b": c["posts_over_half_b"], "t_count_flags": c["t_count"]["flags"]}
    out["predictions"] = {"cr7_some_post_over_half_b": out["cr7"]["posts_over_half_b"] > 0,
                          "final_no_post_over_half_b": out["final"]["posts_over_half_b"] == 0,
                          "cr7_t_count_flags_some": out["cr7"]["t_count_flags"] > 0,
                          "final_t_count_flags_none": out["final"]["t_count_flags"] == 0}
    return out


def score(run_dir, records_dir, generated_by, draft=False):
    entries = read_plan(run_dir)
    fields = read_run_txt(run_dir)
    problems = gate_problems(fields, records_dir) + end_problems(fields, run_dir, entries)
    if problems and not draft:
        raise Refusal("; ".join(problems))
    procs = defaultdict(list)
    for e in entries:
        procs[(e["arm"], e["protocol"])].append(read_process(run_dir, e))
    cells = {k: score_cell(k[0], k[1], v) for k, v in sorted(procs.items())}
    return {"analysis": "P1 v2 H6, hypotheses-v2-h6.md (frozen 2026-10-29)", "generated_by": generated_by,
            "draft": draft, "warnings": problems,
            "provenance": {k: fields.get(k) for k in ("h6 commit", "h6 inputs hash", "h6 functional record",
                                                      "jdk", "cpus", "lab job")},
            "thresholds_ns": {"t_med": T_MED_NS, "t_count": T_COUNT_NS}, "resumes": resumes(run_dir),
            "H6a": h6a(cells), "H6b": h6b(cells),
            "cells": {"%s-%s" % k: c for k, c in cells.items()},
            "incomplete_cells": sorted("%s-%s" % k for k, c in cells.items() if not c["complete"])}


def main(argv=None, repo=None):
    repo = repo or plan_mod.REPO_ROOT
    ap = argparse.ArgumentParser(description="Score H6 (hypotheses-v2-h6.md).")
    ap.add_argument("--run", required=True)
    ap.add_argument("--records", required=True)
    ap.add_argument("--draft", action="store_true",
                    help="tests and dry runs: gate problems become warnings, no clean tree is needed, and the output "
                    "ends generated_by in draft")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    try:
        result = score(a.run, a.records, plan_mod.provenance(repo, a.draft, tool="h6.py"), a.draft)
    except (Refusal, plan_mod.Refusal) as e:
        raise SystemExit("h6.py: refused: %s" % e)
    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(stats.jsonable(result), f, indent=1, sort_keys=True)
        f.write("\n")
    for w in result["warnings"]:
        print("warning: " + w, file=sys.stderr)
    t = result["H6a"]["tests"]
    print("H6a: %d of 2 F6 cells rejected (CR7 forced p %s, Final forced p %s); incomplete cells: %s" % (
        result["H6a"]["rejected"], t["cr7-forced"]["p"], t["final-forced"]["p"], result["incomplete_cells"] or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

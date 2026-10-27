"""The run plans of P1 v2 (hypotheses-v2.md, sections 2, 3, 5, 7, 8 and 9).

Each plan is JSON Lines. Line 1:
  {"plan": 1, "system": ..., "seed": 20261026, "generated_by": "analysis/v2/plan.py <commit> ..."}
Each further line is one process, in run order:
  {"id", "tool": "wakeprobe"|"abpair", "args": [...], "cell", "family", "limit_s"}
args hold every argument but --out, which the campaign runner appends. ids are unique and
filename-safe. limit_s is a positive integer, the most seconds the runner lets the tool run
before it kills it (C1; wakeprobe_limit_s and abpair_limit_s). These are chosen limits, not
estimates: they exist so that a hung tool fails its process.

  python plan.py pilot --out-dir DIR [--system linux|windows] [--draft]
      pilot-linux.jsonl and pilot-windows.jsonl: the A/A pilot (section 7). The same provenance
      rule as a campaign plan: refused unless the repository's working tree is clean, the commit
      read from git (no override), generated_by "analysis/v2/plan.py COMMIT" (C2). --draft is for
      dry runs from an uncommitted tree: the commit is read tolerantly (COMMIT-dirty or "unknown"),
      generated_by ends in "draft", and the files are named pilot-linux.draft.jsonl and
      pilot-windows.draft.jsonl, never the real names.
  python plan.py campaign --out-dir DIR [--system ...] [--revision-log FILE]
      plan-linux.jsonl and plan-windows.jsonl: S, Bt, the phase sweep and H4, one order per
      system shuffled with seed 20261026. Each H4 cell's R is read from the h4-r block of the
      revision log (default: revision-log-v2.md at the repository root; h4log.py), as section 8
      says. The plan is refused unless the block is whole, the repository's working tree is
      clean and the log is a file named revision-log-v2.md; its commit is read from git (no
      override), and generated_by records it with the sha256 of the log file read:
      "analysis/v2/plan.py COMMIT revision-log-v2.md sha256:HEX" (C2).
  python plan.py campaign --out-dir DIR [--system ...] --stand-in-r R
      the same plans for dry runs: every H4 cell gets R (even, 14 to 24) and nothing is read from
      the log or checked in git; generated_by ends in "stand-in-R" and the files are named
      plan-linux.stand-in.jsonl and plan-windows.stand-in.jsonl, never the real names.

The settings of section 1 are wakeprobe's defaults (Linux timer slack 1 ns, Windows' default
timer resolution), so no plan line names them.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402

import grid  # noqa: E402
import h4log  # noqa: E402
import rng  # noqa: E402

SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
PLAN_VERSION = 1
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
REVISION_LOG = "revision-log-v2.md"  # at the repository root
GIT_TIMEOUT_S = 120
COMMIT = re.compile(r"[0-9a-f]{40}")  # a SHA-1 object name; C2's header form has no other length

# C2: the form of generated_by in a real plan. Stand-in and draft plans do not match either.
PILOT_HEADER = re.compile(r"analysis/v2/plan\.py [0-9a-f]{40}")
CAMPAIGN_HEADER = re.compile(r"analysis/v2/plan\.py [0-9a-f]{40} revision-log-v2\.md sha256:([0-9a-f]{64})")

# C1: limit_s. A wakeprobe unit holds for at most kHardLimit (10 s, bench/wakeprobe.cpp line 96)
# once per wait that is counted from the same post, so the number of kHardLimit waits one unit can
# make depends on the protocol:
#   single  measure_one, lines 731 to 756: the wait to posted + timeout and then the wait to
#           posted + kHardLimit are deadlines from the same post, so a unit waits one kHardLimit.
#   batch   measure_round, lines 769 to 811: the gate must enter by posted + kHardLimit (lines 784
#           to 788) and then the tasks must all run by released + kHardLimit (lines 796 to 803),
#           the second counted from after the first, so a round waits two. The gate task itself
#           holds its worker for at most kHardLimit (lines 529 to 535), inside the first.
# The probe is one single post, so it fits under either.
HARD_LIMIT_S = 10  # wakeprobe's kHardLimit
HARD_WAITS = {"single": 1, "batch": 2}
UNIT_GAP_S = 1  # covers the gap before a unit (at most 3.0 B of 10 ms, or one phase of 2.95 B_eff)
RUN_OVERHEAD_S = 120  # covers start, settle and exit
ABPAIR_DRAIN_S = 10  # abpair's drain deadline after a window's end (C4)


class Refusal(Exception):
    """A real plan is refused; the message says why."""

# ---------------------------------------------------------------------------------------------
# Arguments of one process
# ---------------------------------------------------------------------------------------------


def wakeprobe_cell_args(cell, process, entry_id):
    """An S or Bt process: 630 units, 5 warm-up, gaps seeded 2026102700 + process."""
    args = ["--backend", cell.backend, "--variant", cell.variant,
            "--protocol", "single" if cell.protocol == "S" else "batch",
            "--workers", str(cell.workers), "--worker-cpus", grid.WORKER_CPUS[cell.workers],
            "--poster-cpu", grid.POSTER_CPU, "--wait-us", str(cell.bound_us),
            "--posts", str(grid.POSTS), "--warmup", str(grid.WARMUP),
            # 11 calibration posts where the variant delays single posts; none elsewhere, where
            # wakeprobe would not calibrate anyway (and Bt refuses any).
            "--calibrate", str(grid.CALIBRATE if cell.delaying else 0),
            "--gap-frac-range", grid.GAP_FRAC_RANGE, "--gap-unit", "bound",
            "--gap-seed", str(grid.GAP_SEED_BASE + process),
            "--design", "p1v2-" + cell.protocol, "--label", entry_id]
    return args


def sweep_args(backend, bound_us, frac, entry_id):
    """One phase-sweep process: absent wake, W = 1, 21 posts, 3 warm-up, 11 calibration."""
    return ["--backend", backend, "--variant", grid.SWEEP_VARIANT, "--protocol", "single",
            "--workers", "1", "--worker-cpus", grid.WORKER_CPUS[1], "--poster-cpu", grid.POSTER_CPU,
            "--wait-us", str(bound_us), "--posts", str(grid.SWEEP_POSTS), "--warmup", str(grid.SWEEP_WARMUP),
            "--calibrate", str(grid.CALIBRATE), "--phase-frac", frac,
            "--design", "p1v2-sweep", "--label", entry_id]


def abpair_args(backend, work_us, arm_a, arm_b):
    """One abpair process: 2 pairs ABBA of 10 s windows, W = 1, P = 1, K = 64, pinned."""
    return ["--backend", backend, "--arm-a", arm_a, "--arm-b", arm_b,
            "--workers", str(grid.H4_WORKERS), "--producers", str(grid.H4_PRODUCERS),
            "--inflight", str(grid.H4_INFLIGHT), "--pairs", str(grid.H4_PAIRS_PER_PROCESS),
            "--order", "abba", "--window-ms", str(grid.H4_WINDOW_MS), "--work-ns", str(work_us * 1000),
            "--worker-cpus", grid.H4_WORKER_CPUS, "--producer-cpus", grid.H4_PRODUCER_CPUS]


def flag_value(args, flag, default=None):
    """The value after a flag in a plan's argument list."""
    if flag in args:
        return args[args.index(flag) + 1]
    return default


def wakeprobe_limit_s(args):
    """C1: units * (k * 10 + 1) + 120, units = 1 probe + calibration + warm-up + measured posts or rounds."""
    units = 1 + sum(int(flag_value(args, f, d)) for f, d in (("--calibrate", "11"), ("--warmup", "5"), ("--posts", "51")))
    k = HARD_WAITS[flag_value(args, "--protocol", "single")]
    return units * (k * HARD_LIMIT_S + UNIT_GAP_S) + RUN_OVERHEAD_S


def abpair_limit_s(args):
    """C1: windows * (window_s + 10) + 120, windows = 2 pairs (ABBA), window_s = --window-ms / 1000 (rounded up)."""
    windows = 2 * int(flag_value(args, "--pairs"))
    window_s = -(-int(flag_value(args, "--window-ms")) // 1000)
    return windows * (window_s + ABPAIR_DRAIN_S) + RUN_OVERHEAD_S


LIMIT_S = {"wakeprobe": wakeprobe_limit_s, "abpair": abpair_limit_s}


def entry(entry_id, tool, args, cell, family):
    return {"id": entry_id, "tool": tool, "args": args, "cell": cell, "family": family,
            "limit_s": LIMIT_S[tool](args)}


# ---------------------------------------------------------------------------------------------
# The grid, in canonical order before the shuffle
# ---------------------------------------------------------------------------------------------


def s_bt_entries(system):
    out = []
    for cell in grid.all_cells(system):
        for i in range(grid.PROCESSES_PER_CELL):
            pid = "%s-p%d" % (cell.name, i)
            out.append(entry(pid, "wakeprobe", wakeprobe_cell_args(cell, i, pid), cell.name, cell.protocol))
    return out


def sweep_entries(system):
    out = []
    for backend, bound_us in grid.sweep_cells(system):
        name = grid.sweep_name(backend, bound_us)
        for k, frac in enumerate(grid.sweep_fracs()):
            pid = "%s-k%02d" % (name, k)
            out.append(entry(pid, "wakeprobe", sweep_args(backend, bound_us, frac, pid), name, "sweep"))
    return out


def r_for_system(system, r_of):
    """One system's H4 cells as (backend, N) -> R; r_of maps an H4 key to its R."""
    return {(backend, work_us): r_of(grid.h4_key(system, backend, work_us))
            for backend, work_us in grid.h4_cells(system)}


def h4_entries(system, r_by_cell):
    """R / 2 processes of 2 pairs per comparison, for each backend and N."""
    out = []
    for backend, work_us in grid.h4_cells(system):
        r = r_by_cell[(backend, work_us)]
        for arm_a in grid.H4_COMPARISONS:
            name = grid.h4_cell_name(backend, work_us, arm_a)
            for j in range(r // grid.H4_PAIRS_PER_PROCESS):
                pid = "%s-p%02d" % (name, j)
                out.append(entry(pid, "abpair", abpair_args(backend, work_us, arm_a, grid.H4_ARM_B), name, "H4"))
    return out


def pilot_entries(system):
    """The A/A pilot: the fixed loop against itself, 4 processes of 2 pairs per cell."""
    out = []
    for backend, work_us in grid.h4_cells(system):
        name = grid.pilot_cell_name(backend, work_us)
        for j in range(grid.PILOT_PROCESSES):
            pid = "%s-p%d" % (name, j)
            out.append(entry(pid, "abpair", abpair_args(backend, work_us, grid.PILOT_ARM, grid.PILOT_ARM), name, "pilot"))
    return out


def campaign_entries(system, r_by_cell):
    """Every process of one system's confirmatory campaign, shuffled with seed 20261026."""
    canonical = s_bt_entries(system) + sweep_entries(system) + h4_entries(system, r_by_cell)
    return rng.shuffled(canonical, grid.SEED)


def pilot_plan_entries(system):
    """The pilot's processes, shuffled with the same seed and rule (the frozen file is silent)."""
    return rng.shuffled(pilot_entries(system), grid.SEED)


# ---------------------------------------------------------------------------------------------
# Writing and counting
# ---------------------------------------------------------------------------------------------


def header(system, generated_by):
    return {"plan": PLAN_VERSION, "system": system, "seed": grid.SEED, "generated_by": generated_by}


def write_plan(path, head, entries):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(head) + "\n")
        for e in entries:
            f.write(json.dumps(e) + "\n")


def counts(entries):
    """Processes per family, planned H4 windows and the H4 time of section 9's basis."""
    by_family = {}
    windows = 0
    for e in entries:
        by_family[e["family"]] = by_family.get(e["family"], 0) + 1
        if e["tool"] == "abpair":
            pairs = int(e["args"][e["args"].index("--pairs") + 1])
            windows += 2 * pairs
    abpair_processes = sum(1 for e in entries if e["tool"] == "abpair")
    cells = len({e["cell"] for e in entries})
    return {"processes": len(entries), "by_family": by_family, "cells": cells, "abpair_windows": windows,
            # Section 9: windows of 10 s plus at most 1 s per abpair process.
            "abpair_seconds": windows * grid.H4_WINDOW_MS // 1000 + abpair_processes}


def _git(repo, *args):
    """Standard output of a read-only git call in repo; Refusal on any failure."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    try:
        done = subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True, env=env,
                              timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        raise Refusal("cannot run git in %s: %s" % (repo, e))
    if done.returncode != 0:
        raise Refusal("git %s failed in %s: %s" % (" ".join(args), repo, done.stderr.strip() or done.returncode))
    return done.stdout


def clean_commit(repo):
    """HEAD of a repository whose working tree is clean, else Refusal. A failed call never reads as clean.

    No pathspec: the whole repository counts, untracked files included (ignored files do not).
    """
    head = _git(repo, "rev-parse", "HEAD").strip()
    if not COMMIT.fullmatch(head):
        raise Refusal("git rev-parse HEAD in %s gave %r, not a commit" % (repo, head))
    dirty = [line for line in _git(repo, "status", "--porcelain", "--untracked-files=all").splitlines() if line.strip()]
    if dirty:
        raise Refusal("the working tree of %s is not clean (%d paths, e.g. %s): commit or remove them, then "
                      "generate the plan" % (repo, len(dirty), "; ".join(dirty[:3])))
    return head


def require_tracked(repo, path):
    """The revision log must be a file the repository tracks, so a clean tree means it is committed."""
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(repo))
    except ValueError:  # another drive
        rel = os.pardir
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        raise Refusal("%s lies outside the repository %s, so the clean tree says nothing about it" % (path, repo))
    try:
        _git(repo, "ls-files", "--error-unmatch", "--", rel.replace(os.sep, "/"))
    except Refusal:
        raise Refusal("%s is not tracked by the repository %s: commit it" % (path, repo))


def current_commit(where):
    """The commit of the analysis code, or 'unknown'. Read-only git calls. Tolerant: for drafts
    (--draft, --stand-in-r) and for score.py's own record only; a real plan uses clean_commit."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    try:
        sha = subprocess.run(["git", "-C", where, "rev-parse", "HEAD"], capture_output=True, text=True,
                             env=env, timeout=GIT_TIMEOUT_S).stdout.strip()
        if not sha:
            return "unknown"
        dirty = subprocess.run(["git", "-C", where, "status", "--porcelain", "--", "."], capture_output=True,
                               text=True, env=env, timeout=GIT_TIMEOUT_S).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def generated_by(commit, *more, tool="plan.py"):
    return " ".join(("analysis/v2/" + tool, commit) + more)


def provenance(repo, draft, tool="plan.py"):
    """generated_by text of a pilot plan or record: the commit of a clean tree, or with draft the
    tolerant commit and a closing 'draft'. Refusal if the tree is not clean and this is no draft."""
    if draft:
        return generated_by(current_commit(HERE), "draft", tool=tool)
    return generated_by(clean_commit(repo), tool=tool)


def plan_file_name(kind, system, dry):
    """C2: the real names are pilot-<system>.jsonl and plan-<system>.jsonl. A draft or stand-in plan,
    which runs only under the runner's --dry-run, is named pilot-<system>.draft.jsonl or
    plan-<system>.stand-in.jsonl."""
    if not dry:
        return "%s-%s.jsonl" % (kind, system)
    return "%s-%s.%s.jsonl" % (kind, system, "draft" if kind == "pilot" else "stand-in")


def stand_in_r_arg(text):
    """argparse type of --stand-in-r: an even integer from 14 to 24 (section 7)."""
    try:
        r = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("%r is not an integer" % text)
    if r % 2 or not grid.H4_R_MIN <= r <= grid.H4_R_MAX:
        raise argparse.ArgumentTypeError("%d is not an even integer from %d to %d" % (r, grid.H4_R_MIN, grid.H4_R_MAX))
    return r


def campaign_inputs(a, repo):
    """What the campaign plans need: (R of an H4 key, generated_by text). Refusal if a real plan cannot be made.

    Everything is checked here, before any plan is built or any file is written.
    """
    if a.stand_in_r is not None:
        return (lambda key: a.stand_in_r), generated_by(current_commit(HERE), "stand-in-R")
    log = a.revision_log or os.path.join(repo, REVISION_LOG)
    if os.path.basename(log) != REVISION_LOG:
        # C2's header form names the file; a plan from another name would not pass the runner.
        raise Refusal("the revision log must be a file named %s (it was %s)" % (REVISION_LOG, os.path.basename(log)))
    try:
        rows, digest = h4log.read_log(log)
    except h4log.LogError as e:
        raise Refusal(str(e))
    commit = clean_commit(repo)
    require_tracked(repo, log)
    return (lambda key: rows[key]["R"]), generated_by(commit, os.path.basename(log), "sha256:" + digest)


def main(argv=None, repo=REPO_ROOT):
    """repo: the repository whose commit and clean tree a real plan needs. Tests pass a temporary one."""
    ap = argparse.ArgumentParser(description="Write the run plans of P1 v2.")
    sub = ap.add_subparsers(dest="what", required=True)
    p_pilot = sub.add_parser("pilot", help="the A/A pilot plans")
    p_camp = sub.add_parser("campaign", help="the confirmatory plans")
    for p in (p_pilot, p_camp):
        p.add_argument("--out-dir", required=True)
        p.add_argument("--system", choices=grid.SYSTEMS + ("both",), default="both",
                       help="write one system's plan only (default both)")
    p_pilot.add_argument("--draft", action="store_true",
                         help="a dry run from an uncommitted tree: no clean-tree check, generated_by ends in draft")
    source = p_camp.add_mutually_exclusive_group()
    source.add_argument("--revision-log", help="the log with the h4-r block (default: %s at the repository root)"
                        % REVISION_LOG)
    source.add_argument("--stand-in-r", type=stand_in_r_arg, metavar="R",
                        help="a dry run: every H4 cell gets R (even, 14 to 24); no log, no git check")
    a = ap.parse_args(argv)

    systems = grid.SYSTEMS if a.system == "both" else (a.system,)
    try:
        if a.what == "pilot":
            by = provenance(repo, a.draft)
        else:
            r_of, by = campaign_inputs(a, repo)
    except Refusal as e:
        raise SystemExit("plan.py: refused: %s" % e)

    outputs = []
    for system in systems:
        if a.what == "pilot":
            entries, name = pilot_plan_entries(system), plan_file_name("pilot", system, a.draft)
        else:
            entries = campaign_entries(system, r_for_system(system, r_of))
            name = plan_file_name("plan", system, a.stand_in_r is not None)
        bad = [e["id"] for e in entries if not SAFE_ID.match(e["id"])]
        if bad:
            raise SystemExit("ids not filename-safe: %s" % bad[:5])
        outputs.append((name, header(system, by), entries))
    os.makedirs(a.out_dir, exist_ok=True)
    report = {}
    for name, head, entries in outputs:
        write_plan(os.path.join(a.out_dir, name), head, entries)
        report[name] = counts(entries)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

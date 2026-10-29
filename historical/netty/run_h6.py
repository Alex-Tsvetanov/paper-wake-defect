"""H6's runner (hypotheses-v2-h6.md, frozen 2026-10-29): builds H6Probe against both jars, checks the
H6 gate (decision 37: bench/coverage-h6.json), and runs the 40 processes of section 3.

  python run_h6.py --functional --jdk DIR --netty-dir DIR --out-dir DIR [--cpus 0,2,14]
  python run_h6.py --jdk DIR --netty-dir DIR --out-dir DIR --records DIR [--cpus 0,2,14] [--dry-run]

--functional is the stand-in for a sanitizer record (decision 37): at a clean commit whose H6 inputs
match bench/coverage-h6.json, it builds against both jars, loads both native libraries without a
post (H6Probe --epoll-only), and runs both protocols against 4.1.0.Final in check mode, which
writes no time (section 6). It writes h6-functional-<commit>-L-<utc>.json, green when every step
passed. Only a record made by a lab job (LAB_JOB set) can gate a timed run.

A timed run (needs LAB_JOB) refuses unless the tree is clean, every input matches
bench/coverage-h6.json, both jars match their sha256, and --records holds a green functional record
of this commit and these inputs. It then builds, writes plan.txt (the 40 processes in the order of
section 3, shuffled with seed 20261026) and run.txt (the gate lines), and runs each process under
`taskset -c <cpus>` with the JVM flags of section 2. A process that fails (exit not 0, or its
timeout) is run once more into <id>-rerun.*; <id>.done records both exits (section 5).

CPUs: --cpus REST,POSTER,LOOP, default 0,2,14 (section 2). Other values are for the desktop's check
runs only.

Exit: 0 every process good (or the functional record green), 1 a process failed twice (or a
functional step failed), 2 refused before anything ran.
"""

import argparse
import datetime
import hashlib
import json
import os
import platform
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "analysis", "v2"))

import rng  # noqa: E402

COVERAGE = "bench/coverage-h6.json"
ARMS = {"cr7": "4.1.0.CR7", "final": "4.1.0.Final"}
PROTOCOLS = ("forced", "natural")
PROCESSES_PER_CELL = 10
TRIALS, POSTS, WARMUP_S, WARMUP_POSTS = 15, 21, 10, 5
SLOT_MS, LIMIT_MS, HOOK_WAIT_MS = 1100, 10000, 1000
ORDER_SEED = 20261026
JVM_FLAGS = ("-XX:+UseSerialGC", "-Xms128m", "-Xmx128m", "-XX:+PrintCompilation")
CPUS = (0, 2, 14)  # rest, poster, loop
RECORD_PREFIX = "h6-functional-"
FUNCTIONAL_SIZES = ("--trials", "2", "--warmup-s", "2", "--warmup-posts", "2", "--slot-ms", "50")
# The fields H6Probe fills with a time, or a count made from times; --check leaves them null.
TIME_KEYS = ("delay_ns", "x_delay_ns", "hook_held_ns", "y_after_hook_ns", "realized_gap_ns", "gc_time_ms",
             "first_measured_uptime_ms", "max_delay_ns", "warmup_natural_over_half_b")


class Refusal(Exception):
    pass


def jar_name(arm):
    return "netty-all-%s.jar" % ARMS[arm]


def sha256_lf(path):
    """sha256 of a text file with CRLF read as LF (as the D5 gate hashes its inputs)."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load_coverage(repo=REPO):
    with open(os.path.join(repo, COVERAGE), encoding="utf-8") as f:
        return json.load(f)


def inputs_hash(inputs):
    """One hash over the declared inputs: sha256 of their sorted 'path sha256' lines."""
    text = "".join("%s %s\n" % (p, inputs[p]) for p in sorted(inputs))
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def input_problems(coverage, repo=REPO):
    """Why the files differ from the declared hashes, as sentences (empty when they match)."""
    found = []
    for rel, want in sorted(coverage["inputs"].items()):
        path = os.path.join(repo, rel)
        if not os.path.isfile(path):
            found.append("%s is missing" % rel)
        elif sha256_lf(path) != want:
            found.append("%s has sha256 %s, %s declares %s" % (rel, sha256_lf(path), COVERAGE, want))
    return found


def jar_problems(coverage, netty_dir):
    found = []
    for arm in ARMS:
        name = jar_name(arm)
        path = os.path.join(netty_dir, name)
        want = coverage["third_party"].get(name)
        if not os.path.isfile(path):
            found.append("%s is missing from %s" % (name, netty_dir))
        elif sha256_file(path) != want:
            found.append("%s has sha256 %s, not %s" % (name, sha256_file(path), want))
    return found


def git(repo, *args):
    exe = os.environ.get("LAB_GIT") or "git"
    done = subprocess.run([exe, "-C", repo] + list(args), capture_output=True, text=True)
    if done.returncode != 0:
        raise Refusal("git %s failed: %s" % (" ".join(args), done.stderr.strip()))
    return done.stdout


def clean_commit(repo=REPO):
    head = git(repo, "rev-parse", "HEAD").strip()
    dirty = [line for line in git(repo, "status", "--porcelain", "--untracked-files=all").splitlines() if line.strip()]
    if dirty:
        raise Refusal("the working tree is not clean (%d paths, e.g. %s)" % (len(dirty), "; ".join(dirty[:3])))
    return head


def matching_record(records_dir, commit, digest):
    """(name, sha256) of a green functional record made by a lab job at this commit and these inputs; else None."""
    if not records_dir or not os.path.isdir(records_dir):
        return None
    for name in sorted(os.listdir(records_dir)):
        if not (name.startswith(RECORD_PREFIX) and name.endswith(".json")):
            continue
        path = os.path.join(records_dir, name)
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if (rec.get("green") is True and rec.get("commit") == commit and rec.get("inputs_hash") == digest
                and rec.get("lab_job")):
            return name, sha256_file(path)
    return None


def plan(seed=ORDER_SEED):
    """The 40 processes of section 3, in their shuffled order."""
    entries = [{"id": "%s-%s-p%d" % (arm, protocol, i), "arm": arm, "protocol": protocol, "process": i}
               for arm in ARMS for protocol in PROTOCOLS for i in range(PROCESSES_PER_CELL)]
    return rng.shuffled(entries, seed)


def parse_cpus(text):
    parts = [int(x) for x in text.split(",")]
    if len(parts) != 3 or len(set(parts)) != 3:
        raise argparse.ArgumentTypeError("--cpus is REST,POSTER,LOOP, three different CPUs")
    return tuple(parts)


def java(jdk):
    return os.path.join(jdk, "bin", "java")


def build(jdk, netty_dir, out_dir):
    """javac against each jar; returns {arm: classes folder} and the class files' sha256."""
    classes, hashes = {}, {}
    for arm in ARMS:
        folder = os.path.join(out_dir, "classes-" + arm)
        os.makedirs(folder, exist_ok=True)
        cmd = [os.path.join(jdk, "bin", "javac"), "-Xlint:all", "-Werror", "-d", folder, "-cp",
               os.path.join(netty_dir, jar_name(arm)), os.path.join(HERE, "H6Probe.java")]
        done = subprocess.run(cmd, capture_output=True, text=True)
        if done.returncode != 0:
            raise Refusal("javac against %s failed: %s" % (jar_name(arm), done.stderr.strip()[:500]))
        classes[arm] = folder
        hashes[arm] = {n: sha256_file(os.path.join(folder, n)) for n in sorted(os.listdir(folder))}
    return classes, hashes


def probe_command(jdk, netty_dir, classes, arm, cpus, native_dir, args):
    rest, poster, loop = cpus
    return (["taskset", "-c", "%d,%d,%d" % (rest, poster, loop), java(jdk)] + list(JVM_FLAGS)
            + ["-Dio.netty.native.workdir=" + native_dir, "-cp",
               os.path.join(netty_dir, jar_name(arm)) + os.pathsep + classes[arm], "H6Probe",
               "--netty-version", ARMS[arm], "--loop-cpu", str(loop), "--poster-cpu", str(poster),
               "--rest-cpu", str(rest)] + list(args))


def timeout_s():
    """A process's timeout: twice its planned length plus a margin (no measured basis). A forced post owns
    one slot; a natural post on CR7 costs at most about 1 s per hit, so the same length bounds it."""
    planned = WARMUP_S + (WARMUP_POSTS + TRIALS * POSTS) * SLOT_MS / 1000.0
    return int(2 * planned + 120)


def run_one(cmd, stem, timeout):
    """Runs one attempt with stdout to <stem>.out and stderr to <stem>.err; returns its exit (124 on timeout)."""
    with open(stem + ".out", "wb") as out, open(stem + ".err", "wb") as err:
        try:
            return subprocess.run(cmd, stdout=out, stderr=err, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            return 124


def read_lines(path):
    """The JSON objects of a probe's output, or None when it cannot be read."""
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(l) for l in f.read().splitlines() if l.strip()]
    except (OSError, ValueError):
        return None


def summary_of(lines):
    s = lines[-1] if lines else None
    return s if isinstance(s, dict) and s.get("summary") else None


def null_times(lines):
    """True when no line of a check-mode output carries a time (section 6)."""
    return all(obj.get(k) is None for obj in lines for k in TIME_KEYS)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dt%H%M%S")


def jdk_version(jdk):
    done = subprocess.run([java(jdk), "-version"], capture_output=True, text=True)
    return " / ".join(l.strip() for l in done.stderr.splitlines() if l.strip())


def checked_inputs(netty_dir):
    """The coverage file, the clean commit and the inputs' hash; Refusal if the inputs or jars differ."""
    coverage = load_coverage()
    commit = clean_commit()
    problems = input_problems(coverage) + jar_problems(coverage, netty_dir)
    if problems:
        raise Refusal("; ".join(problems))
    return coverage, commit, inputs_hash(coverage["inputs"])


# ---------------------------------------------------------------------------------------------
# --functional
# ---------------------------------------------------------------------------------------------

FUNCTIONAL_STEPS = (("cr7-epoll-only", "cr7", ("--epoll-only",)),
                    ("final-epoll-only", "final", ("--epoll-only",)),
                    ("final-forced-check", "final", ("--protocol", "forced", "--check") + FUNCTIONAL_SIZES),
                    ("final-natural-check", "final", ("--protocol", "natural", "--check") + FUNCTIONAL_SIZES))


def functional(a, run=run_one):
    coverage, commit, digest = checked_inputs(a.netty_dir)
    os.makedirs(a.out_dir, exist_ok=True)
    classes, hashes = build(a.jdk, a.netty_dir, a.out_dir)
    native = os.path.join(a.out_dir, "native")
    os.makedirs(native, exist_ok=True)
    steps = []
    for name, arm, args in FUNCTIONAL_STEPS:
        stem = os.path.join(a.out_dir, name)
        cmd = probe_command(a.jdk, a.netty_dir, classes, arm, a.cpus, native,
                            list(args) + ["--process", "0", "--out", stem + ".jsonl"])
        rc = run(cmd, stem, 600)
        lines = read_lines(stem + ".jsonl")
        s = summary_of(lines)
        good = bool(s and s.get("good") is True)
        steps.append({"step": name, "exit": rc, "good": good, "passed": rc == 0 and good and null_times(lines),
                      "problems": (s or {}).get("problems")})
    record = {"kind": "h6-functional", "green": all(st["passed"] for st in steps), "dry_run": False,
              "commit": commit, "inputs_hash": digest, "lab_job": os.environ.get("LAB_JOB"),
              "host": platform.node(), "kernel": platform.release(), "jdk": jdk_version(a.jdk),
              "class_sha256": hashes, "cpus": list(a.cpus), "steps": steps, "made_utc": utc_now()}
    name = "%s%s-L-%s.json" % (RECORD_PREFIX, commit[:9], record["made_utc"])
    with open(os.path.join(a.out_dir, name), "w", encoding="utf-8", newline="\n") as f:
        json.dump(record, f, indent=1, sort_keys=True)
        f.write("\n")
    print("functional record %s: %s" % (name, "green" if record["green"] else "red"))
    return 0 if record["green"] else 1


# ---------------------------------------------------------------------------------------------
# the timed run
# ---------------------------------------------------------------------------------------------


def gate(a):
    """The H6 gate (decision 37). Returns the run.txt lines; Refusal when it does not pass."""
    coverage, commit, digest = checked_inputs(a.netty_dir)
    record = matching_record(a.records, commit, digest)
    if record is None:
        raise Refusal("no green functional record of commit %s and inputs %s, made by a lab job, in %s"
                      % (commit, digest, a.records))
    return [("h6 gate", "passed"), ("h6 commit", commit), ("h6 inputs hash", digest),
            ("h6 coverage sha256", sha256_lf(os.path.join(REPO, COVERAGE))),
            ("h6 functional record", "%s %s" % record),
            ("h6 jars", "; ".join("%s %s" % (jar_name(arm), coverage["third_party"][jar_name(arm)]) for arm in ARMS))]


def timed(a, run=run_one):
    if not a.dry_run and not os.environ.get("LAB_JOB"):
        raise Refusal("a timed run needs LAB_JOB (the lab runner sets it on the laptop); use --dry-run to check")
    gate_lines = gate(a)
    order = plan()
    os.makedirs(a.out_dir, exist_ok=True)
    if any(n.endswith(".jsonl") for n in os.listdir(a.out_dir)):
        raise Refusal("%s already holds outputs" % a.out_dir)
    classes, hashes = build(a.jdk, a.netty_dir, a.out_dir)
    native = os.path.join(a.out_dir, "native")
    os.makedirs(native, exist_ok=True)
    with open(os.path.join(a.out_dir, "plan.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("# H6 plan: %d processes, order seed %d\n" % (len(order), ORDER_SEED))
        for e in order:
            f.write(json.dumps(e, sort_keys=True) + "\n")
    sizes = ["--trials", str(TRIALS), "--posts", str(POSTS), "--warmup-s", str(WARMUP_S), "--warmup-posts",
             str(WARMUP_POSTS), "--slot-ms", str(SLOT_MS), "--limit-ms", str(LIMIT_MS), "--hook-wait-ms",
             str(HOOK_WAIT_MS)]
    with open(os.path.join(a.out_dir, "run.txt"), "w", encoding="utf-8", newline="\n") as f:
        for key, value in gate_lines + [("jdk", jdk_version(a.jdk)), ("lab job", os.environ.get("LAB_JOB")),
                                        ("cpus", ",".join(map(str, a.cpus))), ("jvm flags", " ".join(JVM_FLAGS)),
                                        ("class sha256", json.dumps(hashes, sort_keys=True)),
                                        ("started utc", utc_now())]:
            f.write("%s: %s\n" % (key, value))
    if a.dry_run:
        for e in order:
            print(" ".join(probe_command(a.jdk, a.netty_dir, classes, e["arm"], a.cpus, native,
                                         ["--protocol", e["protocol"], "--process", str(e["process"])] + sizes)))
        return 0
    failed = 0
    for n, e in enumerate(order, 1):
        exits = []
        for suffix in ("", "-rerun"):
            stem = os.path.join(a.out_dir, e["id"] + suffix)
            cmd = probe_command(a.jdk, a.netty_dir, classes, e["arm"], a.cpus, native,
                                ["--protocol", e["protocol"], "--process", str(e["process"]), "--out", stem + ".jsonl"]
                                + sizes)
            rc = run(cmd, stem, timeout_s())
            exits.append(rc)
            print("%s process %d/%d %s%s exit %d" % (utc_now(), n, len(order), e["id"], suffix, rc), flush=True)
            if rc == 0:
                break
        with open(os.path.join(a.out_dir, e["id"] + ".done"), "w", encoding="ascii", newline="\n") as f:
            f.write("".join("%s %d\n" % (k, rc) for k, rc in zip(("first", "rerun"), exits)))
        failed += exits[-1] != 0
    with open(os.path.join(a.out_dir, "run.txt"), "a", encoding="utf-8", newline="\n") as f:
        f.write("ended utc: %s\nincomplete processes: %d\n" % (utc_now(), failed))
    print("incomplete processes: %d" % failed)
    return 1 if failed else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run H6 (hypotheses-v2-h6.md).")
    ap.add_argument("--jdk", required=True)
    ap.add_argument("--netty-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--records", help="the folder that holds the H6 functional records (timed runs)")
    ap.add_argument("--cpus", type=parse_cpus, default=CPUS, help="REST,POSTER,LOOP (default 0,2,14)")
    ap.add_argument("--functional", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        return functional(a) if a.functional else timed(a)
    except Refusal as e:
        print("run_h6.py: refused: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

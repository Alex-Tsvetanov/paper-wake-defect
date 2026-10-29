"""H6's runner (hypotheses-v2-h6.md, frozen 2026-10-29): builds H6Probe against both jars, checks the
H6 gate (decision 37: bench/coverage-h6.json), and runs the 40 processes of section 3.

  python run_h6.py --functional --jdk DIR --netty-dir DIR --out-dir DIR
  python run_h6.py --jdk DIR --netty-dir DIR --out-dir DIR --records DIR [--dry-run | --resume]
  python run_h6.py --functional --desktop-check --cpus 0,2,4 ...   (the desktop's own check)

Both modes run only on the laptop's Arch (host alex-laptop, as bench/gate_lib.py's LAB_HOSTS), need
a clean commit whose H6 inputs match bench/coverage-h6.json, both jars at their sha256, and the
pinned JDK (1.8.0_504-b01).

--functional is the stand-in for a sanitizer record (decision 37): it builds against both jars,
loads both native libraries without a post (H6Probe --epoll-only), and runs both protocols against
4.1.0.Final in check mode, which writes no time (section 6). It writes
h6-functional-<commit>-L-<utc>.json, green when every step passed. With --desktop-check it runs off
the laptop too, and its record says so and can never gate a run.

A timed run (needs LAB_JOB) also needs, in --records, a green functional record made by a lab job
on the laptop at this commit, these inputs and this JDK. It builds, writes plan.txt (the 40
processes in the order of section 3, shuffled with seed 20261026) and run.txt (the gate lines),
and runs each process under `taskset -c 0,2,14` with the JVM flags of section 2. A process that
fails (exit not 0, or its timeout) is run once more into <id>-rerun.*; <id>.done records each
attempt's exit as it ends (section 5). --dry-run writes nothing into the folder. --resume continues an
interrupted run in its folder, as the campaign runner's resume does (bench/run_campaign.py): the
gate must give the same lines on the laptop and the build the same class files; an ended process is
kept; an attempt that did not end has its files set aside as <name>.void-<utc> and runs again (a void
run, which never counts as a failed one); a process whose first attempt failed and whose rerun did
not end gets only its rerun. The addendum does not foresee a resume: using it needs a revision-log
entry, and the scorer reports it.

The JVM must see none of JAVA_TOOL_OPTIONS, _JAVA_OPTIONS and JDK_JAVA_OPTIONS: they would add flags
to every probe.

CPUs: --cpus REST,POSTER,LOOP, default 0,2,14 (section 2); a timed run refuses any other.

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
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "analysis", "v2"))

import rng  # noqa: E402

COVERAGE = "bench/coverage-h6.json"
LAB_HOST = "alex-laptop"  # bench/gate_lib.py LAB_HOSTS["L"]: the laptop's Arch (test_run_h6.py checks they agree)
JDK_BUILD = "1.8.0_504-b01"  # Temurin 8u504-b01 (hypotheses-v2-h6.md section 1)
JAVA_OPTION_VARIABLES = ("JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS")
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
             "first_measured_uptime_ms", "max_delay_ns", "warmup_natural_posts", "warmup_natural_over_half_b")


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


def is_lab_host(name):
    return bool(name) and name.casefold() == LAB_HOST.casefold()


def record_gates(rec, commit, digest, jdk):
    """True when a functional record may gate a run: green, made by a lab job on the laptop (not a desktop
    check), at this commit, these inputs and this JDK."""
    return (rec.get("green") is True and bool(rec.get("lab_job")) and is_lab_host(rec.get("host"))
            and not rec.get("desktop_check") and rec.get("commit") == commit and rec.get("inputs_hash") == digest
            and rec.get("jdk") == jdk)


def matching_record(records_dir, commit, digest, jdk):
    """(name, sha256) of a record that may gate this run (record_gates); else None."""
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
        if isinstance(rec, dict) and record_gates(rec, commit, digest, jdk):
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


def checked_inputs(netty_dir, jdk, anywhere=False):
    """The coverage file, the clean commit, the inputs' hash and the JDK's version text. Refusal off the laptop
    (unless anywhere), or if the inputs, the jars or the JDK differ from what is declared."""
    if not anywhere and not is_lab_host(platform.node()):
        raise Refusal("this host is %r: H6 runs only on the laptop's Arch (%s)" % (platform.node(), LAB_HOST))
    set_options = [v for v in JAVA_OPTION_VARIABLES if os.environ.get(v)]
    if set_options:
        raise Refusal("%s set: the JVM would take flags the plan does not name" % ", ".join(set_options))
    coverage = load_coverage()
    commit = clean_commit()
    problems = input_problems(coverage) + jar_problems(coverage, netty_dir)
    version = jdk_version(jdk)
    if JDK_BUILD not in version:
        problems.append("the JDK is %r, not %s" % (version, JDK_BUILD))
    if problems:
        raise Refusal("; ".join(problems))
    return coverage, commit, inputs_hash(coverage["inputs"]), version


# ---------------------------------------------------------------------------------------------
# --functional
# ---------------------------------------------------------------------------------------------

FUNCTIONAL_STEPS = (("cr7-epoll-only", "cr7", ("--epoll-only",)),
                    ("final-epoll-only", "final", ("--epoll-only",)),
                    ("final-forced-check", "final", ("--protocol", "forced", "--check") + FUNCTIONAL_SIZES),
                    ("final-natural-check", "final", ("--protocol", "natural", "--check") + FUNCTIONAL_SIZES))


def functional(a, run=run_one):
    coverage, commit, digest, version = checked_inputs(a.netty_dir, a.jdk, anywhere=a.desktop_check)
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
              "desktop_check": bool(a.desktop_check), "commit": commit, "inputs_hash": digest,
              "lab_job": None if a.desktop_check else os.environ.get("LAB_JOB"),
              "host": platform.node(), "kernel": platform.release(), "jdk": version,
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
    coverage, commit, digest, version = checked_inputs(a.netty_dir, a.jdk, anywhere=a.dry_run)
    record = matching_record(a.records, commit, digest, version)
    if record is None:
        raise Refusal("no functional record in %s may gate this run: none is green, made by a lab job on %s, at "
                      "commit %s, inputs %s and this JDK" % (a.records, LAB_HOST, commit, digest))
    return [("h6 gate", "passed"), ("h6 host", platform.node()), ("h6 commit", commit), ("h6 inputs hash", digest),
            ("h6 coverage sha256", sha256_lf(os.path.join(REPO, COVERAGE))),
            ("h6 functional record", "%s %s" % record),
            ("h6 jars", "; ".join("%s %s" % (jar_name(arm), coverage["third_party"][jar_name(arm)]) for arm in ARMS)),
            ("jdk", version)]


# Every process's sizes, as section 3 sets them; the scorer checks them in each summary's args.
SIZES = ("--trials", str(TRIALS), "--posts", str(POSTS), "--warmup-s", str(WARMUP_S), "--warmup-posts",
         str(WARMUP_POSTS), "--slot-ms", str(SLOT_MS), "--limit-ms", str(LIMIT_MS), "--hook-wait-ms", str(HOOK_WAIT_MS))
GATE_KEYS = ("h6 gate", "h6 host", "h6 commit", "h6 inputs hash", "h6 coverage sha256", "h6 functional record",
             "h6 jars", "jdk")


def read_fields(path):
    """The 'key: value' lines of a run.txt, the first of each key."""
    fields = {}
    with open(path, encoding="utf-8") as f:
        for line in f.read().splitlines():
            key, sep, value = line.partition(": ")
            if sep:
                fields.setdefault(key, value)
    return fields


def resume_problems(a, order, gate_lines, hashes):
    """Why the folder cannot be resumed: another plan, another gate or other class files (empty if it can)."""
    found = []
    try:
        with open(os.path.join(a.out_dir, "plan.txt"), encoding="utf-8") as f:
            entries = [json.loads(l) for l in f.read().splitlines() if l.strip() and not l.startswith("#")]
        fields = read_fields(os.path.join(a.out_dir, "run.txt"))
    except (OSError, ValueError) as e:
        return ["the folder has no readable plan.txt and run.txt: %s" % e]
    if entries != order:
        found.append("plan.txt is not this plan")
    if not is_lab_host(fields.get("h6 host")):
        found.append("run.txt's host is %r, not the laptop's" % fields.get("h6 host"))
    for key, value in gate_lines:
        if key != "h6 host" and fields.get(key) != value:
            found.append("run.txt's %r is %r, the gate now gives %r" % (key, fields.get(key), value))
    if fields.get("class sha256") != json.dumps(hashes, sort_keys=True):
        found.append("the class files differ from run.txt's")
    return found


def set_aside(out_dir, stem, stamp):
    """Renames the files of an attempt that did not end (<stem>.*) to <name>.void-<stamp>: a void run."""
    for name in sorted(os.listdir(out_dir)):
        if name.startswith(stem + ".") and ".void-" not in name and not name.endswith(".done"):
            os.rename(os.path.join(out_dir, name), os.path.join(out_dir, "%s.void-%s" % (name, stamp)))


def read_done(path):
    """The exits recorded so far: {'first': rc[, 'rerun': rc]}; {} when there is no readable .done file."""
    try:
        with open(path, encoding="ascii") as f:
            pairs = [line.split() for line in f.read().splitlines() if line.strip()]
        exits = {k: int(v) for k, v in pairs}
    except (OSError, ValueError):
        return {}
    return exits if set(exits) in ({"first"}, {"first", "rerun"}) else {}


def ended(exits):
    """True when a process needs no more attempts: its first attempt passed, or its rerun ended."""
    return exits.get("first") == 0 or "rerun" in exits


def timed(a, run=run_one):
    if not a.dry_run and not os.environ.get("LAB_JOB"):
        raise Refusal("a timed run needs LAB_JOB (the lab runner sets it on the laptop); use --dry-run to check")
    if not a.dry_run and tuple(a.cpus) != CPUS:
        raise Refusal("a timed run uses CPUs %s (section 2), not %s" % (CPUS, tuple(a.cpus)))
    gate_lines = gate(a)
    order = plan()
    sizes = list(SIZES)
    if a.dry_run:  # builds in a temporary folder and only prints the commands
        with tempfile.TemporaryDirectory(prefix="h6-dry-") as tmp:
            classes, _ = build(a.jdk, a.netty_dir, tmp)
            for e in order:
                print(" ".join(probe_command(a.jdk, a.netty_dir, classes, e["arm"], a.cpus, os.path.join(tmp, "native"),
                                             ["--protocol", e["protocol"], "--process", str(e["process"])] + sizes)))
        return 0
    os.makedirs(a.out_dir, exist_ok=True)
    if not a.resume and any(n.endswith(".jsonl") or n.endswith(".done") for n in os.listdir(a.out_dir)):
        raise Refusal("%s already holds outputs; --resume continues an interrupted run" % a.out_dir)
    classes, hashes = build(a.jdk, a.netty_dir, a.out_dir)
    native = os.path.join(a.out_dir, "native")
    os.makedirs(native, exist_ok=True)
    stamp = utc_now()
    if a.resume:
        problems = resume_problems(a, order, gate_lines, hashes)
        if problems:
            raise Refusal("cannot resume: " + "; ".join(problems))
        with open(os.path.join(a.out_dir, "run.txt"), "a", encoding="utf-8", newline="\n") as f:
            f.write("resumed utc: %s\nresume lab job: %s\n" % (stamp, os.environ.get("LAB_JOB")))
    else:
        with open(os.path.join(a.out_dir, "plan.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write("# H6 plan: %d processes, order seed %d\n" % (len(order), ORDER_SEED))
            for e in order:
                f.write(json.dumps(e, sort_keys=True) + "\n")
        with open(os.path.join(a.out_dir, "run.txt"), "w", encoding="utf-8", newline="\n") as f:
            for key, value in gate_lines + [("lab job", os.environ.get("LAB_JOB")), ("cpus", ",".join(map(str, a.cpus))),
                                            ("jvm flags", " ".join(JVM_FLAGS)),
                                            ("class sha256", json.dumps(hashes, sort_keys=True)),
                                            ("started utc", stamp)]:
                f.write("%s: %s\n" % (key, value))
    failed = 0
    for n, e in enumerate(order, 1):
        done = os.path.join(a.out_dir, e["id"] + ".done")
        exits = read_done(done)
        if ended(exits):
            failed += exits.get("rerun", exits["first"]) != 0
            continue
        if not exits and os.path.exists(done):  # unreadable: the runner was killed while writing it
            os.rename(done, "%s.void-%s" % (done, stamp))
        attempts = ("-rerun",) if exits else ("", "-rerun")
        for suffix in attempts:
            set_aside(a.out_dir, e["id"] + suffix, stamp)
        for suffix in attempts:
            stem = os.path.join(a.out_dir, e["id"] + suffix)
            cmd = probe_command(a.jdk, a.netty_dir, classes, e["arm"], a.cpus, native,
                                ["--protocol", e["protocol"], "--process", str(e["process"]), "--out", stem + ".jsonl"]
                                + sizes)
            rc = run(cmd, stem, timeout_s())
            with open(done, "a", encoding="ascii", newline="\n") as f:  # each attempt as it ends
                f.write("%s %d\n" % ("rerun" if suffix else "first", rc))
                f.flush()
                os.fsync(f.fileno())
            print("%s process %d/%d %s%s exit %d" % (utc_now(), n, len(order), e["id"], suffix, rc), flush=True)
            if rc == 0:
                break
        exits = read_done(done)
        failed += exits.get("rerun", exits.get("first")) != 0
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
    ap.add_argument("--desktop-check", action="store_true",
                    help="with --functional: run off the laptop; the record says so and never gates a run")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--resume", action="store_true")
    a = ap.parse_args(argv)
    if a.desktop_check and not a.functional:
        ap.error("--desktop-check goes with --functional")
    try:
        return functional(a) if a.functional else timed(a)
    except Refusal as e:
        print("run_h6.py: refused: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

# Derived from paper-io-core bench/gate_lib.py (itself from paper-one-port bench/gate_lib.py at
# commit 1f27ea8), same author (Apache-2.0).
# Changed: wakeloop's names and section references; the gate itself is here and run by
# bench/run_campaign.py, which takes no gate file; records are accepted only from the laptop's
# hosts and only whole (one record matches all four targets); a declared gap never waives a record;
# the other system's records are checked by code fingerprint (hypotheses-v2.md section 8); a record that
# does not fit (other generator files, an older format, a missing archive) is skipped with a note and
# only a record of a real finding stops the gate.
"""The sanitizer records gate (hypotheses-v2.md, sections 8 and 11; the Papers repo's rule D5).

run_gate() is the whole gate. bench/run_campaign.py calls it in its own process at the start of a timed
run, again at every --resume and in its --check-gate mode, and bench/run_h5.sh and run_h5.ps1 call that
mode before any cell; bench/check_records_v2.py only reports its result. No file it did not compute itself
is trusted: the inputs of the build are hashed from the build directory, the records are read again, and the
binaries are hashed again.

What a gate checks, in this order, and refuses (GateError) on any failure:
1. The code paths (bench/code_paths.txt) have no change, tracked or untracked, and neither has
   bench/coverage.json. git is LAB_GIT or git; when it cannot be started the refusal names it.
2. The build was configured from this checkout as Release, with no sanitizer (WAKELOOP_SANITIZER empty or a CMake
   false constant), no toolchain file, no interprocedural optimization, and the compile and link flags that a fresh
   configure gives on the platform (PINNED_FLAGS); on W neither CL nor _CL_ is set; and neither binary holds the
   symbols of a sanitizer runtime (SANITIZER_SYMBOLS).
3. The build's own ninja (CMAKE_MAKE_PROGRAM, never a ninja from PATH) has nothing to build (`ninja -n`), and each
   binary's mtime agrees with its entry in .ninja_log, so that a binary put in place after the build is seen.
4. The build's inputs, computed now (bench/build_inputs.py): one hash per target, the configuration, and the
   compiler. The compiler is CMake's identification at configure time, the one that compiled the binaries. A
   timed run is refused when today's banner of the compiler differs from it ("the compiler changed since
   configure; configure and build again"); a resume (resume_compiler) skips that, because its binaries are
   bound by hash to the start, and wants the identity recorded at the start. On L the binaries' .comment
   section must name that clang version too. There is no way to name another compiler.
5. For every sanitizer the host needs (SANITIZERS_BY_HOST: L ASan+UBSan, TSan, MSan; W ASan; fixed here, not
   read from any file), a green record made on the laptop that matches all four targets, the compiler and the
   configuration, of the current format (RECORD_FORMAT), made with the generator files of today
   (build_inputs.generator_hashes), whose logs archive exists on this machine with the sha256 the record names.
   One record must match every target: records are never combined. A record that matches the inputs but does not
   fit in one of those ways is skipped with a note, never fatal; a record of other inputs is not looked at.
   A red record of the same inputs stops the gate only when it records a real finding (a sanitizer report, a
   failed or timed-out test, a failed or timed-out smoke run, the record's "blocking" field); a record that is
   red for an infrastructure reason (a failed log pack, a dirty tree, a host refusal, a job-level problem) never
   blocks.
6. The other system's records, for every sanitizer it needs, for the same code (code fingerprint), by the same
   rules. Their archives are on the other system and are not looked for.
A declared gap (bench/coverage.json) waives nothing. The file only annotates what the records cannot see;
its sha256 goes into the result, and "partial" is always empty (the runner refuses a result where it is not).

Only the laptop makes records (the Papers repo's rule D7: every timed run is on the laptop, and the
desktop's data never appears as a result). A record whose host is not one of LAB_HOSTS (compared without
case) is refused, and so is a dry-run record; accept_dry_run waives both, for the drivers' dry run, and marks
the result "citable": false.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import struct
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import build_inputs

# The sanitizers each host's records must cover (hypotheses-v2.md, section 11 item 7; rule D5).
SANITIZERS_BY_HOST = {"L": ("asan", "tsan", "msan"), "W": ("asan",)}
# The laptop's two systems, as their records name them (lab/laptop/versions.md; the host field of the
# records in lab/sanitizer-records/laptop-w): Arch is alex-laptop, Windows is DESKTOP-A1RHS5R. Names are compared
# without case (is_lab_host): Windows reports COMPUTERNAME in capitals and Python's platform.node() may not.
LAB_HOSTS = {"L": ("alex-laptop",), "W": ("DESKTOP-A1RHS5R",)}
OTHER_HOST = {"L": "W", "W": "L"}
PATTERN = "wakeloop2-*.json"
RECORD_NAME = re.compile(r"^wakeloop2-[^-]+-([LW])-")
COVERAGE_JSON = build_inputs.HERE / "coverage.json"
COVERAGE_REL = "bench/coverage.json"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RECORD_FORMAT = build_inputs.RECORD_FORMAT
# What a fresh `cmake -S . -B DIR -G Ninja -DCMAKE_BUILD_TYPE=Release` puts in the cache on each platform, read
# from fresh configures on 2026-10-27: L with clang++ 22.1.8 and CMake 4.4.3 (the container papers-dev:arch-2026-10-23,
# the laptop's Arch toolchain), W with MSVC 19.51 (cl) and CMake 4.3.2 (the desktop; the laptop's is 4.3.3). A
# measured build with other flags (an added -O0, -fsanitize, /fsanitize or a linker option) is refused. The sanitizer
# builds of the records add theirs with add_compile_options, so these cache entries are the same in a record's build.
# bench/test_records.py configures a fresh tree where a compiler is at hand and compares it with these.
PINNED_FLAGS = {
    "L": {"CMAKE_CXX_FLAGS": "", "CMAKE_CXX_FLAGS_RELEASE": "-O3 -DNDEBUG", "CMAKE_EXE_LINKER_FLAGS": "",
          "CMAKE_EXE_LINKER_FLAGS_RELEASE": ""},
    "W": {"CMAKE_CXX_FLAGS": "/DWIN32 /D_WINDOWS /EHsc", "CMAKE_CXX_FLAGS_RELEASE": "/O2 /Ob2 /DNDEBUG",
          "CMAKE_EXE_LINKER_FLAGS": "/machine:x64", "CMAKE_EXE_LINKER_FLAGS_RELEASE": "/INCREMENTAL:NO"},
}
# Names that a binary linked with a sanitizer runtime carries (ASan, TSan, MSan, UBSan; MSVC's ASan runtime).
SANITIZER_SYMBOLS = (b"__asan_init", b"__tsan_init", b"__msan_init", b"__ubsan_handle", b"clang_rt.asan")
# CMake's false constants (if() treats these, case-insensitively, and any name ending in -NOTFOUND, as false).
FALSE_CONSTANTS = ("", "0", "OFF", "FALSE", "NO", "N", "IGNORE", "NOTFOUND")
NINJA_TIMEOUT_S = 300
# How far a binary's mtime may be from what its .ninja_log entry says, against the other outputs of the same build
# (check_binaries_built). Ninja writes the time a command started; the file's own time is a link away, up to a
# second or so later. A binary put in place afterwards is minutes or more away.
NINJA_LOG_TOLERANCE_S = 5
NINJA_LOG_UNIT_NS = {"L": 1, "W": 100}  # one tick of a ninja mtime: nanoseconds on Linux, 100 ns on Windows
NOTES_SHOWN = 6


class GateError(Exception):
    pass


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sanitizers_for(host: str) -> tuple[str, ...]:
    if host not in SANITIZERS_BY_HOST:
        raise GateError(f"unknown host {host!r}; the hosts are {', '.join(SANITIZERS_BY_HOST)}")
    return SANITIZERS_BY_HOST[host]


def is_lab_host(tag: str, name: str | None) -> bool:
    """True when NAME is one of the laptop's host names for the system TAG (L or W), without regard to case."""
    return bool(name) and name.casefold() in (h.casefold() for h in LAB_HOSTS[tag])


def cmake_false(value: str | None) -> bool:
    """True for a value CMake's if() reads as false: empty, 0, OFF, FALSE, NO, N, IGNORE, NOTFOUND, *-NOTFOUND."""
    v = (value or "").strip().upper()
    return v in FALSE_CONSTANTS or v.endswith("-NOTFOUND")


def host_records(records: Path, pattern: str, host: str) -> list[Path]:
    """The records of a name pattern made on HOST (the host part of the name, as in
    wakeloop2-<commit>-L-asan-<suffix>.json)."""
    out = []
    for f in sorted(records.glob(pattern)):
        m = RECORD_NAME.match(f.name)
        if m and m.group(1) == host:
            out.append(f)
    return out


# ---------------------------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------------------------

def check_build_config(build: Path, host: str, repo: Path = build_inputs.REPO) -> None:
    """Refuses a build that is not a plain Release build of this checkout (step 2 of the module text)."""
    cache = build_inputs.read_cache(build)
    problems = []
    home = cache.get("CMAKE_HOME_DIRECTORY", "")
    if not home or os.path.normcase(os.path.realpath(home)) != os.path.normcase(os.path.realpath(repo)):
        problems.append(f"it was configured from {home or 'an unknown directory'}, not from this checkout ({repo}), "
                        "whose code paths and fingerprint this gate reads")
    if cache.get("CMAKE_BUILD_TYPE") != "Release":
        problems.append(f"CMAKE_BUILD_TYPE is {cache.get('CMAKE_BUILD_TYPE')!r}, not Release")
    if not cmake_false(cache.get("WAKELOOP_SANITIZER")):
        problems.append(f"WAKELOOP_SANITIZER is {cache['WAKELOOP_SANITIZER']!r}: a sanitizer build is never timed")
    if cache.get("CMAKE_TOOLCHAIN_FILE", ""):
        problems.append(f"CMAKE_TOOLCHAIN_FILE is set ({cache['CMAKE_TOOLCHAIN_FILE']})")
    if not cmake_false(cache.get("CMAKE_INTERPROCEDURAL_OPTIMIZATION")):
        problems.append(f"CMAKE_INTERPROCEDURAL_OPTIMIZATION is {cache['CMAKE_INTERPROCEDURAL_OPTIMIZATION']!r}: "
                        "link-time optimization changes the generated code")
    for key, want in PINNED_FLAGS[host].items():
        if cache.get(key) != want:
            problems.append(f"{key} is {cache.get(key)!r}, a fresh configure on {host} gives {want!r}")
    if problems:
        raise GateError(f"the build {build} is not a plain Release build: " + "; ".join(problems))


def check_environment(host: str) -> None:
    """Refuses an environment that changes what cl compiles: on W, CL or _CL_ set (MSVC reads both)."""
    if host != "W":
        return
    set_ = [v for v in ("CL", "_CL_") if os.environ.get(v)]
    if set_:
        raise GateError(f"{' and '.join(set_)} {'is' if len(set_) == 1 else 'are'} set in the environment: cl.exe reads "
                        "them as extra compiler options, so the binaries would not be those of a plain Release build")


def scan_binaries(build: Path, host: str) -> dict[str, str]:
    """{name: sha256} of the measured executables. Refuses a missing one and one that holds the symbols of a
    sanitizer runtime."""
    found, missing, tainted = {}, [], []
    for name, rel in build_inputs.BINARIES[host].items():
        path = build / rel
        if not path.is_file():
            missing.append(str(path))
            continue
        data = path.read_bytes()
        hits = [s.decode("ascii") for s in SANITIZER_SYMBOLS if s in data]
        if hits:
            tainted.append(f"{path} holds {', '.join(hits)}")
        found[name] = hashlib.sha256(data).hexdigest()
    if missing:
        raise GateError(f"the measured executables are missing: {', '.join(missing)}")
    if tainted:
        raise GateError("a measured executable is linked with a sanitizer runtime: " + "; ".join(tainted))
    return found


def build_is_current(build: Path) -> str:
    """Why the build is not up to date (empty when the build's own `ninja -n` has nothing to do). Raises GateError
    if ninja cannot be found or fails."""
    try:
        ninja = build_inputs.ninja_path(build)
    except build_inputs.InputsError as e:
        raise GateError(str(e)) from None
    try:
        p = subprocess.run([ninja, "-C", str(build), "-n"], capture_output=True, text=True, timeout=NINJA_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        raise GateError(f"ninja did not run ({ninja}): {e}") from None
    if p.returncode != 0:
        raise GateError(f"ninja -n failed in {build}: {(p.stderr or p.stdout).strip()[-300:]}")
    last = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""
    return "" if "no work to do" in last else f"ninja -n would build: {last}"


def read_ninja_log(build: Path) -> dict[str, tuple[int, int, int]]:
    """{output: (start_ms, end_ms, mtime)} of the last entry of each output of BUILD/.ninja_log. Raises GateError."""
    try:
        text = (Path(build) / ".ninja_log").read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise GateError(f"{build} has no readable .ninja_log ({e}); was it built by ninja in this folder?") from None
    entries = {}
    for line in text.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) >= 4:
            try:
                entries[parts[3]] = (int(parts[0]), int(parts[1]), int(parts[2]))
            except ValueError:
                continue
    return entries


def check_binaries_built(build: Path, host: str) -> None:
    """Refuses a measured binary that ninja did not make in this build (N5): `ninja -n` says nothing is to do after a
    binary has been replaced by a newer file. The .ninja_log entry of an output holds the time its command started
    (in ticks of NINJA_LOG_UNIT_NS[host]); the file's own time is that plus the command's duration, give or take what
    the linker does after. The difference of the two, less the duration, is the same for every output of the build,
    up to the tolerance; its typical value (the median over the other outputs) is the reference, because ninja's
    epoch differs between platforms. A binary whose difference departs from the reference was not written by the
    command that the log names. The commit baked into the binary is not looked at: it is set at configure time."""
    build = Path(build)
    entries = read_ninja_log(build)
    unit = NINJA_LOG_UNIT_NS[host]

    def deviation(out: str) -> int | None:
        start, end, logged = entries[out]
        path = Path(out) if Path(out).is_absolute() else build / out
        try:
            ticks = path.stat().st_mtime_ns // unit
        except OSError:
            return None
        return ticks - logged - (end - start) * 1_000_000 // unit

    binaries = {rel: name for name, rel in build_inputs.BINARIES[host].items()}
    reference = [d for out in entries if out not in binaries and (d := deviation(out)) is not None]
    if len(reference) < 3:
        raise GateError(f"{build}/.ninja_log lists too few outputs that exist ({len(reference)}) to compare the binaries "
                        "with; was the build made by ninja in this folder?")
    typical = statistics.median(reference)
    tolerance = NINJA_LOG_TOLERANCE_S * 1_000_000_000 // unit
    problems = []
    for rel in binaries:
        if rel not in entries:
            problems.append(f"{rel} has no entry in .ninja_log: ninja did not make it in this build")
            continue
        d = deviation(rel)
        if d is None:
            problems.append(f"{rel} is missing")
        elif abs(d - typical) > tolerance:
            problems.append(f"{rel} has a modification time {abs(d - typical) * unit / 1e9:.0f} s away from the one its "
                            ".ninja_log entry gives: it was written after the command that built it")
    if problems:
        raise GateError("a measured executable is not the one ninja built: " + "; ".join(problems))


def elf_comments(path: Path) -> list[str]:
    """The strings of the .comment section of a 64-bit little-endian ELF file (the compilers and linkers that made
    it). Raises GateError for a file that is not one, or has no such section."""
    data = Path(path).read_bytes()
    if data[:4] != b"\x7fELF" or len(data) < 64:
        raise GateError(f"{path} is not an ELF file")
    if data[4] != 2 or data[5] != 1:
        raise GateError(f"{path} is not a 64-bit little-endian ELF file")
    shoff, = struct.unpack_from("<Q", data, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x3A)
    if not shoff or not shnum or shoff + shnum * shentsize > len(data):
        raise GateError(f"{path} has no section headers")

    def section(i: int) -> tuple[int, int, int]:
        name, _type, _flags, _addr, offset, size = struct.unpack_from("<IIQQQQ", data, shoff + i * shentsize)
        return name, offset, size

    _, names_off, names_size = section(shstrndx)
    names = data[names_off:names_off + names_size]
    for i in range(shnum):
        name, offset, size = section(i)
        if names[name:names.find(b"\0", name)] == b".comment":
            return [s.decode("utf-8", "replace") for s in data[offset:offset + size].split(b"\0") if s]
    raise GateError(f"{path} has no .comment section")


def check_binary_compiler(build: Path, host: str, identity: str) -> None:
    """On L, the .comment section of each binary names the clang version that CMake identified (N4): a binary that
    clang 21 made does not become clang 22's by a pacman update. Every 'clang version X' it holds must be that
    version, and it must hold one. Other hosts and other compilers have no such check here."""
    if host != "L" or not identity.startswith("Clang "):
        return
    want = identity.split(" ", 1)[1]
    problems = []
    for rel in build_inputs.BINARIES[host].values():
        found = []
        for comment in elf_comments(build / rel):
            m = re.search(r"clang version (\d+(?:\.\d+)*)", comment)
            if m:
                found.append(m.group(1))
        if not found:
            problems.append(f"{rel} names no clang version in its .comment section")
        elif any(".".join(v.split(".")[:3]) != want for v in found):
            problems.append(f"{rel} was made by clang {', '.join(sorted(set(found)))}, not {want}")
    if problems:
        raise GateError(f"the binaries were not made by {identity}: " + "; ".join(problems))


# ---------------------------------------------------------------------------------------------
# The records
# ---------------------------------------------------------------------------------------------

def check_record_origin(f: Path, r: dict, host: str, records: Path, accept_dry_run: bool) -> None:
    """Refuses a dry-run record (unless accepted) and a record that the laptop did not make. These are not
    stale records but records that must never be among the records."""
    if r.get("dry_run") and not accept_dry_run:
        raise GateError(f"{f.name} is a dry run of the records driver: it never gates a build and never belongs "
                        f"among the records ({records})")
    if not accept_dry_run and not is_lab_host(host, r.get("host")):
        raise GateError(f"{f.name} was made on host {r.get('host')!r}, not on the laptop's {host} "
                        f"({', '.join(LAB_HOSTS[host])}); the desktop never makes a record that gates a build")


def is_finding(r: dict) -> bool:
    """True when a red record records a real finding: a sanitizer report, a failed or timed-out test, a failed or
    timed-out smoke run. The record says so ("blocking"); one that does not (an older writer's) is a finding when it
    shows a report or a failed test."""
    if "blocking" in r:
        return r["blocking"] is not False
    failed = r.get("tests_failed")
    return bool(r.get("sanitizer_reports")) or (isinstance(failed, int) and failed > 0)


def red_classes(r: dict) -> str:
    classes = sorted({x.get("class", "?") for x in r.get("red_reasons") or [] if isinstance(x, dict)})
    return ", ".join(classes) or "no reason class"


def record_problem(r: dict, generators: dict[str, str], check_archive: bool) -> str | None:
    """Why a green record that matches the build's inputs cannot serve as evidence (it is then skipped with a note),
    or None: another format than today's, no logs archive and sha256, other generator files than today's, no smoke
    keys, and, for a record made on this system (check_archive), a logs archive that is not on this machine or whose
    sha256 is not the one the record names."""
    if r.get("format") != RECORD_FORMAT:
        return f"it is of format {r.get('format')!r}, and today's is {RECORD_FORMAT}"
    archive, sha = r.get("logs_archive"), r.get("logs_archive_sha256")
    if not archive or not isinstance(sha, str) or not SHA256.match(sha):
        return "it has no logs archive and sha256 (logs_archive, logs_archive_sha256)"
    if r.get("generators") != generators:
        return ("it was made with other generator files than today's (analysis/v2/plan.py and grid.py, "
                "bench/campaign_smoke.py, run_h5.sh, run_h5.ps1)")
    keys = (r.get("smoke") or {}).get("shape_keys")
    if not isinstance(keys, list) or not keys:
        return "it carries no smoke shape keys"
    if check_archive:
        path = Path(archive)
        if not path.is_file():
            return f"its logs archive {archive} is not on this machine"
        if sha256_file(path) != sha:
            return f"its logs archive {archive} does not have the sha256 the record names"
    return None


def smoke_keys_of(r: dict) -> frozenset:
    return frozenset(tuple(k) for k in r["smoke"]["shape_keys"])


def load_record(f: Path, notes: list[str]) -> dict | None:
    try:
        r = load(f)
    except (OSError, ValueError) as e:
        notes.append(f"{f.name}: skipped, it cannot be read ({e})")
        return None
    if not isinstance(r, dict):
        notes.append(f"{f.name}: skipped, it is not a record")
        return None
    return r


def blocked_by(f: Path, r: dict, what: str) -> GateError:
    return GateError(f"{f.name} {what} and is red with a finding ({red_classes(r)}): it blocks this gate until it is "
                     "dealt with")


def missing_message(missing: list[str], where: str, notes: list[str]) -> str:
    text = f"no green {', '.join(missing)} record {where}"
    if notes:
        shown = notes[:NOTES_SHOWN]
        more = f"; and {len(notes) - len(shown)} more" if len(notes) > len(shown) else ""
        text += " (" + "; ".join(shown) + more + ")"
    return text


def target_coverage(records: Path, hashes: dict, config: dict, compiler: str, generators: dict[str, str], *,
                    host: str, accept_dry_run: bool = False, notes: list[str] | None = None) -> dict[str, list[dict]]:
    """{sanitizer: [{"name", "sha256", "keys"}]}: for every sanitizer the host needs, the green records that match
    ALL targets' inputs hashes, the compiler and the configuration and fit (record_problem). Raises GateError for a
    sanitizer without one, for a record of the same inputs that is red with a finding, and for a record that is a
    dry run or not the laptop's. A record that matches the inputs and does not fit, or is red for an infrastructure
    reason, is skipped with a line in NOTES."""
    notes = [] if notes is None else notes
    found: dict[str, list[dict]] = {}
    for f in host_records(records, PATTERN, host):
        r = load_record(f, notes)
        if r is None:
            continue
        check_record_origin(f, r, host, records, accept_dry_run)
        san = r.get("sanitizer")
        if san not in sanitizers_for(host):
            continue
        theirs = r.get("inputs_hash") or {}
        if any(theirs.get(t) != h for t, h in hashes.items()):
            continue
        if r.get("green") is not True:
            if is_finding(r):
                raise blocked_by(f, r, "compiled the same inputs")
            notes.append(f"{f.name}: skipped, red for a reason that is not a finding ({red_classes(r)})")
            continue
        if r.get("compiler") != compiler or r.get("config") != config:
            continue
        problem = record_problem(r, generators, check_archive=True)
        if problem:
            notes.append(f"{f.name}: skipped, {problem}")
            continue
        found.setdefault(san, []).append({"name": f.name, "sha256": sha256_file(f), "keys": smoke_keys_of(r)})
    missing = [s for s in sanitizers_for(host) if s not in found]
    if missing:
        raise GateError(missing_message(missing, f"on {host} matches every target of this build (inputs "
                        f"{', '.join(h[:12] for h in hashes.values())}) with compiler {compiler} and its configuration",
                        notes))
    for line in notes:
        print(f"skipped: {line}", file=sys.stderr)
    for san, entries in found.items():
        print(f"covered: {san} by {', '.join(e['name'] for e in entries)}", file=sys.stderr)
    return found


def other_host_coverage(records: Path, host: str, fingerprint: str, generators: dict[str, str], *,
                        accept_dry_run: bool = False, notes: list[str] | None = None) -> dict[str, list[dict]]:
    """Section 8: no timed run before the records exist for both systems. For the other system than HOST
    (OTHER_HOST), every sanitizer it needs must have a green record made on the laptop for the same code (its
    code_fingerprint equals FINGERPRINT) and the same generator files, of the current format. The inputs hashes of
    the two systems differ (different backends), so the match is by code. Returns {sanitizer: [{"name",
    "sha256"}]}. A record that is red with a finding stops the gate, as for the host's own; one that does not fit
    or is red for an infrastructure reason is skipped with a note. The archives are on the other system and are
    not looked for."""
    notes = [] if notes is None else notes
    other = OTHER_HOST[host]
    found: dict[str, list[dict]] = {}
    for f in host_records(records, PATTERN, other):
        r = load_record(f, notes)
        if r is None:
            continue
        check_record_origin(f, r, other, records, accept_dry_run)
        if r.get("code_fingerprint") != fingerprint or r.get("sanitizer") not in sanitizers_for(other):
            continue
        if r.get("green") is not True:
            if is_finding(r):
                raise blocked_by(f, r, f"was made for the same code on {other}")
            notes.append(f"{f.name}: skipped, red for a reason that is not a finding ({red_classes(r)})")
            continue
        problem = record_problem(r, generators, check_archive=False)
        if problem:
            notes.append(f"{f.name}: skipped, {problem}")
            continue
        found.setdefault(r["sanitizer"], []).append({"name": f.name, "sha256": sha256_file(f)})
    missing = [s for s in sanitizers_for(other) if s not in found]
    if missing:
        raise GateError(missing_message(missing, f"on {other} for this code (fingerprint {fingerprint[:12]}) in "
                        f"{records}; section 8 wants both systems' records before any timed run", notes))
    for line in notes:
        print(f"skipped: {line}", file=sys.stderr)
    for san, entries in found.items():
        print(f"other system {other}: {san} {', '.join(e['name'] for e in entries)}", file=sys.stderr)
    return found


# ---------------------------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------------------------

def run_gate(*, build: Path, host: str, records: Path, other_records: Path, tool: Path | None = None,
             repo: Path = build_inputs.REPO, accept_dry_run: bool = False, resume_compiler: str | None = None) -> dict:
    """The whole gate for the build in BUILD on HOST (L or W). Returns the result as a dict of JSON types plus
    "smoke_keys" (a frozenset of tuples, the shapes every required sanitizer's record ran). Raises GateError.
    resume_compiler: at --resume, the compiler identity that run.txt recorded at the start; the banner check is
    skipped (the binaries are bound to the start by hash) and the identity of the build must be that one."""
    build = Path(build)
    sanitizers_for(host)
    try:
        dirty = build_inputs.code_dirty(repo)
        if dirty:
            raise GateError("the code paths have changes (" + "; ".join(dirty[:3]) + "); a gate names committed code")
        if build_inputs.path_dirty(repo, COVERAGE_REL):
            raise GateError(f"{COVERAGE_REL} is changed or untracked; a gate names committed declared gaps")
        head = build_inputs.git(repo, "rev-parse", "HEAD").stdout.strip()
        code_commit = build_inputs.code_commit(repo)
        fingerprint = build_inputs.code_fingerprint(repo)
        generators = build_inputs.generator_hashes(repo)
        coverage_sha = hashlib.sha256(COVERAGE_JSON.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    except (RuntimeError, OSError) as e:
        raise GateError(str(e)) from None
    check_environment(host)
    check_build_config(build, host, repo)
    binaries = scan_binaries(build, host)
    stale = build_is_current(build)
    if stale:
        raise GateError(f"{build} is not up to date, so its binaries are not what the hashed files compiled: {stale}")
    check_binaries_built(build, host)
    try:
        info = build_inputs.compute_inputs(build, host, tool, repo)["builds"][build_inputs.BUILD]
    except build_inputs.InputsError as e:
        raise GateError(f"the inputs of {build} cannot be hashed: {e}") from None
    identity = info.get("compiler")
    if not identity:
        raise GateError(f"the compiler of {build} cannot be identified: CMake's CMakeCXXCompiler.cmake names none")
    if resume_compiler is None:
        changed = build_inputs.compiler_change(info)
        if changed:
            raise GateError(changed)
    elif identity != resume_compiler:
        raise GateError(f"the build was made by {identity}, and the run being resumed started with {resume_compiler}")
    check_binary_compiler(build, host, identity)
    hashes = {t: v["inputs_hash"] for t, v in info["targets"].items()}
    notes: list[str] = []
    own = target_coverage(records, hashes, info["config"], identity, generators, host=host,
                          accept_dry_run=accept_dry_run, notes=notes)
    theirs = other_host_coverage(other_records, host, fingerprint, generators, accept_dry_run=accept_dry_run, notes=notes)
    keys = frozenset.intersection(*(frozenset().union(*(e["keys"] for e in entries)) for entries in own.values()))
    return {"gate": 2, "host": host, "passed": True, "dry_run": accept_dry_run, "citable": not accept_dry_run,
            "date": datetime.now().astimezone().isoformat(timespec="seconds"), "head": head, "code_commit": code_commit,
            "code_fingerprint": fingerprint, "generators": generators, "coverage_json_sha256": coverage_sha,
            "partial": {}, "notes": notes,
            build_inputs.BUILD: {"compiler": identity, "compiler_banner": info.get("compiler_banner", ""),
                                 "inputs_hash": hashes, "config": info["config"],
                                 "build_dir": str(build), "binaries": binaries,
                                 "coverage": {san: [{"name": e["name"], "sha256": e["sha256"]} for e in entries]
                                              for san, entries in own.items()}},
            "other_host": {"host": OTHER_HOST[host], "records": theirs},
            "records_dir": str(records), "other_records_dir": str(other_records),
            "binaries": binaries, "smoke_keys": keys}

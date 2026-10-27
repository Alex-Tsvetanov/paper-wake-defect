#!/usr/bin/env python3
# Derived from paper-io-core bench/build_inputs.py (itself from paper-one-port bench/build_inputs.py
# at commit 1f27ea8), same author (Apache-2.0).
# Changed: wakeloop's targets, configuration keys and root label; the measured executables are
# wakeprobe and abpair; a check that the hashed files include the sources each platform compiles
# (from the 2026-09 bench/inputs_gate.py); the code fingerprint across both systems; the CMake
# files and the compile flags of the cache in the configuration; the compiler's identity is CMake's
# own, with today's banner checked against it; ninja is the build's own and git is LAB_GIT or git; the
# inputs file is written whole or not at all.
"""What a wakeloop build compiled, hashed the one way the records and the gate both use.

    build_inputs.py --build DIR --host L|W --out FILE [--inputs-hash PATH]

The sanitizer records (bench/sanitize_wakeloop.sh on L, bench/sanitize_wakeloop.ps1 on W) and the
gate of a measured build (bench/gate_lib.py, run by run_campaign.py itself) must hash a build with the
same targets, the same configuration keys and the same root label, or a record can never match the
build it is meant to cover. Both take them from here:
- TARGETS: every first-party target the build compiles: wakeloop (the static library), wakeloop_tests
  (the wake matrix), wakeprobe and abpair (the measurement programs). A target's hash covers only
  its own translation units (lab/bin/inputs_hash.py), so the library is named beside the programs.
  Before hashing, the build's compile database is read. Every target that compiled a translation
  unit must be in the list, and every listed target must have compiled one. A configure with
  -DWAKELOOP_BENCH=OFF builds no program, so it stops here; a target added later stops the hash
  until it is named here.
- CONFIG_KEYS: the cache options that select which code is compiled and how: CMAKE_BUILD_TYPE (the
  measured builds are Release, hypotheses-v2.md section 1), WAKELOOP_BACKENDS (the backends the platform
  compiles, an INTERNAL cache entry kept for this gate), WAKELOOP_BENCH, and the compile and link flags
  of the cache (CMAKE_CXX_FLAGS, CMAKE_CXX_FLAGS_RELEASE, CMAKE_EXE_LINKER_FLAGS and its Release variant). WAKELOOP_SANITIZER
  and WAKELOOP_MSAN_LIBCXX are not keys: the sanitizer is a record's own field, and a key that differs
  between a record and the measured build would let no record match. The configuration also holds
  CMAKE_FILES_SHA256, the hash of every tracked CMakeLists.txt and *.cmake file, because a flag or a
  define added there reaches the compiler without touching any file the compiler reads as input.
- EXPECTED_FILES: the files each target's hash must hold. The hash of a target would gate any code
  if a layout left a source outside the first-party root, so the build is refused unless every
  listed file is in it (the 2026-09 gate's coverage check).
- code_fingerprint(): the sha256 of git's listing of the code paths (bench/code_paths.txt) at HEAD,
  the same on both systems. A record carries it, so a gate on one system can see that the other
  system's records were made on the same code (hypotheses-v2.md, section 8, "Gate").
- generator_hashes(): the sha256 of the files that decide which runs the campaign smoke makes
  (analysis/v2/plan.py and grid.py, bench/campaign_smoke.py, run_h5.sh and run_h5.ps1). A record
  carries them; the gate wants the current files' to equal them.
- the compiler: the binaries were compiled by the compiler CMake identified at configure time
  (compiler_identification(), cut to three version parts: "Clang 22.1.8", "MSVC 19.51.36246"). That is the
  identity a record and the gate match on ("compiler"). compiler_identity() runs the compiler named in the
  cache and reads today's banner ("compiler_banner"); a banner that differs from the identity means the
  compiler changed after the configure (compiler_change()), and a timed run is refused then. The banner is
  what a pacman or Visual Studio update changes; CMake's identification stays with the binaries.
- ninja and git. The lab's W jobs build with -DCMAKE_MAKE_PROGRAM=<ninja> and have no ninja on PATH when the
  run phase starts, so ninja is always the build's own (ninja_path(), from CMakeCache.txt) and its folder is put
  first on PATH for the children that call a bare ninja (inputs_hash.py does). git is LAB_GIT when that is
  set, else git on PATH; when it cannot be started the refusal names it.
- ROOT_LABEL "wakeloop": the repository root is the CMake project root, labelled so.
- BINARIES: the measured executables per host, relative to the build directory, whose sha256 the
  gate records, so that a campaign run can be bound to a gated build.

inputs_hash.py is the Papers repo's lab/bin/inputs_hash.py: --inputs-hash, else as
default_inputs_hash() finds it. compute_inputs() returns the fields of builds.wakeloop and these
more: "compiler", "compiler_cmake" (CMake's own identification), "cxx_compiler" (its path), "host",
"inputs_hash_tool" (the path and sha256 of the inputs_hash.py used) and "file_hashes" (per target,
every hashed file and its sha256, from inputs_hash.py --list). main() writes them to --out whole,
by a rename, and removes an older --out first, so a refusal never leaves an inputs file behind. It
exits 0 with the file written, 2 if the targets or the hashed files differ from the lists here, 3 if
ninja or inputs_hash.py is missing or fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BUILD = "wakeloop"
ROOT_LABEL = "wakeloop"
CONFIG_KEYS = ("CMAKE_BUILD_TYPE", "WAKELOOP_BACKENDS", "WAKELOOP_BENCH", "CMAKE_CXX_FLAGS", "CMAKE_CXX_FLAGS_RELEASE",
               "CMAKE_EXE_LINKER_FLAGS", "CMAKE_EXE_LINKER_FLAGS_RELEASE")
CMAKE_FILES_KEY = "CMAKE_FILES_SHA256"
HOSTS = ("L", "W")
# The format number of a sanitizer record. The writer (wakeloop_record.py) puts it into every record and the gate
# (gate_lib.py) wants the current one: a record of an older format is skipped with a note, never trusted. Raise it
# when the writer's checks change in a way that makes older records weaker.
RECORD_FORMAT = 2
TARGETS = ("wakeloop", "wakeloop_tests", "wakeprobe", "abpair")
# The executables a campaign run starts, per host, relative to the build directory.
BINARIES: dict[str, dict[str, str]] = {
    "L": {"wakeprobe": "bench/wakeprobe", "abpair": "bench/abpair"},
    "W": {"wakeprobe": "bench/wakeprobe.exe", "abpair": "bench/abpair.exe"},
}
# The files whose text decides which runs the smoke makes and which the runner accepts.
GENERATOR_FILES = ("analysis/v2/plan.py", "analysis/v2/grid.py", "bench/campaign_smoke.py", "bench/run_h5.sh",
                   "bench/run_h5.ps1")
# The files each target's hash must hold, whatever the platform, then the loop sources of each backend.
# The library holds its own sources and the headers they include; the loop core (loop.hpp and what it
# includes) is a header template, so it is in the hash of every target that instantiates it.
_LIB_COMMON = ("loop/src/common.cpp", "loop/src/common.hpp", "loop/src/condvar.cpp",
               "loop/include/wakeloop/backends.hpp", "loop/include/wakeloop/defect.hpp")
_BACKEND_FILES = {"epoll": ("loop/src/epoll.cpp", "loop/src/linux_fd.hpp"),
                  "io_uring": ("loop/src/uring.cpp", "loop/src/linux_fd.hpp"),
                  "iocp": ("loop/src/iocp.cpp",),
                  "condvar": ()}
_CORE = ("loop/include/wakeloop/backends.hpp", "loop/include/wakeloop/defect.hpp", "loop/include/wakeloop/loop.hpp",
         "loop/include/wakeloop/task_stack.hpp", "loop/include/wakeloop/variants.hpp")
_PROGRAM_COMMON = ("bench/bench_common.hpp", "bench/probe_platform.hpp") + _CORE
_TARGET_FILES = {"wakeloop_tests": ("tests/wake_tests.cpp", "tests/harness.hpp") + _CORE,
                 "wakeprobe": ("bench/wakeprobe.cpp",) + _PROGRAM_COMMON,
                 "abpair": ("bench/abpair.cpp",) + _PROGRAM_COMMON}
# CMake writes each target's objects under CMakeFiles/<target>.dir/. An object rule's output ends
# in .o or .obj (lab/bin/inputs_hash.py reads the compile database the same way).
TARGET_DIR = re.compile(r"cmakefiles[/\\]([^/\\]+)\.dir[/\\]", re.IGNORECASE)
OBJECT = re.compile(r"\.(o|obj)$")
COMPILER_TIMEOUT_S = 60


class InputsError(Exception):
    """The build cannot be hashed. `code` is the exit code of build_inputs.py: 2 for a build that is not
    the one this adapter describes, 3 for a tool that is missing or failed."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def read_cache(build: Path) -> dict[str, str]:
    """CMakeCache.txt's entries; empty if the file is missing (inputs_hash.py then fails on its own)."""
    out: dict[str, str] = {}
    try:
        text = (Path(build) / "CMakeCache.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9_.+-]+):[A-Z]+=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def default_inputs_hash() -> Path:
    """The Papers repo's lab/bin/inputs_hash.py: beside a Papers checkout around this repository
    (p1-wake-defect/wake-defect), in the lab clone's sibling src/Papers, else in ~/lab/Papers.
    Its sha256 goes into the inputs file, so a copy that differs shows."""
    for cand in (REPO.parent.parent / "lab" / "bin" / "inputs_hash.py",
                 REPO.parent / "Papers" / "lab" / "bin" / "inputs_hash.py",
                 Path.home() / "lab" / "Papers" / "lab" / "bin" / "inputs_hash.py"):
        if cand.is_file():
            return cand
    raise FileNotFoundError("no lab/bin/inputs_hash.py beside a Papers checkout, in a sibling Papers clone or in "
                            "~/lab/Papers; give --inputs-hash")


def compiled_targets(compdb: list[dict]) -> set[str]:
    """The targets that compiled at least one translation unit, from `ninja -t compdb`."""
    out: set[str] = set()
    for entry in compdb:
        output = entry.get("output") or ""
        m = TARGET_DIR.search(output) if OBJECT.search(output) else None
        if m:
            out.add(m.group(1))
    return out


def check_targets(compiled: set[str]) -> list[str]:
    """Why the compiled targets differ from TARGETS (empty when they match)."""
    want = set(TARGETS)
    problems = []
    if compiled - want:
        problems.append(f"compiled but not listed in build_inputs.TARGETS: {', '.join(sorted(compiled - want))}")
    if want - compiled:
        problems.append(f"listed but compiled nothing: {', '.join(sorted(want - compiled))} "
                        "(a build with -DWAKELOOP_BENCH=OFF has no wakeprobe or abpair)")
    return problems


def expected_files(backends: list[str]) -> dict[str, tuple[str, ...]]:
    """The files each target's hash must hold, for a build that compiles BACKENDS."""
    lib = list(_LIB_COMMON)
    for backend in backends:
        lib += [f for f in _BACKEND_FILES.get(backend, ()) if f not in lib]
    return {"wakeloop": tuple(lib), **_TARGET_FILES}


def check_files(listing: dict[str, dict[str, str]], backends: list[str], windows: bool) -> list[str]:
    """Why a target's hashed files miss an expected file (empty when none does). LISTING is
    {target: {path: sha256}}, paths as inputs_hash.py names them ("wakeloop/loop/src/common.cpp"),
    lower-cased on Windows. An unknown backend is a problem too."""
    problems = [f"WAKELOOP_BACKENDS names '{b}', which build_inputs does not know" for b in backends
                if b not in _BACKEND_FILES]
    for target, files in expected_files(backends).items():
        have = set(listing.get(target, {}))
        for rel in files:
            name = f"{ROOT_LABEL}/{rel}".lower() if windows else f"{ROOT_LABEL}/{rel}"
            if name not in have:
                problems.append(f"target {target}: the hashed files do not include {name}")
    return problems


def read_listing(text: str, build_name: str = BUILD) -> dict[str, dict[str, str]]:
    """{target: {path: sha256}} from inputs_hash.py --list lines '<build>\\t<target>\\t<path>\\t<sha256>'."""
    out: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) == 4 and parts[0] == build_name:
            out.setdefault(parts[1], {})[parts[2]] = parts[3]
    return out


def normalize_identity(text: str) -> str:
    """A compiler identity cut to its name and the first three parts of its version: CMake's "MSVC 19.51.36246.0"
    and cl's banner "MSVC 19.51.36246" are the same compiler. Empty stays empty."""
    m = re.fullmatch(r"\s*(\S+)\s+(\d+(?:\.\d+)*)\s*", text or "")
    if not m:
        return ""
    return f"{m.group(1)} {'.'.join(m.group(2).split('.')[:3])}"


def compiler_identification(build: Path) -> str:
    """CMake's own identification of the build's compiler ("The CXX compiler identification is <ID> <VERSION>"),
    from CMakeFiles/<version>/CMakeCXXCompiler.cmake of the newest configure, cut to three version parts
    (normalize_identity); empty if none is found. This is what compiled the binaries, and the identity that records
    and the gate match on."""
    files = sorted(Path(build).glob("CMakeFiles/*/CMakeCXXCompiler.cmake"), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        cid = re.search(r'^set\(CMAKE_CXX_COMPILER_ID "([^"]*)"\)', text, re.MULTILINE)
        ver = re.search(r'^set\(CMAKE_CXX_COMPILER_VERSION "([^"]*)"\)', text, re.MULTILINE)
        if cid and ver:
            return normalize_identity(f"{cid.group(1)} {ver.group(1)}")
    return ""


def compiler_executable(build: Path) -> str:
    """The compiler's full path as CMake resolved it ("set(CMAKE_CXX_COMPILER "<path>")" in CMakeCXXCompiler.cmake of the
    newest configure); else the cache's CMAKE_CXX_COMPILER. A build configured with -DCMAKE_CXX_COMPILER=cl keeps just
    "cl" in its cache, which a process without the Visual Studio environment cannot start; CMake's own record has the path."""
    files = sorted(Path(build).glob("CMakeFiles/*/CMakeCXXCompiler.cmake"), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files:
        m = re.search(r'^set\(CMAKE_CXX_COMPILER "([^"]+)"\)', f.read_text(encoding="utf-8", errors="replace"), re.MULTILINE)
        if m:
            return m.group(1)
    return read_cache(build).get("CMAKE_CXX_COMPILER", "")


def compiler_change(info: dict) -> str:
    """Why the compiler is not the one that compiled the build (empty when it is): today's banner (compiler_banner)
    differs from CMake's identification at configure time (compiler). The binaries do not change when a compiler is
    updated in place, so a timed run on them is refused until the build is configured and built again."""
    identity, banner = info.get("compiler") or "", info.get("compiler_banner") or ""
    if not identity:
        return "the compiler of the build cannot be identified (CMake's CMakeCXXCompiler.cmake names none)"
    if not banner:
        return (f"the compiler {info.get('cxx_compiler')!r} cannot be started to read its banner (updated or removed since "
                "configure? configure and build again)")
    if banner != identity:
        return (f"the compiler changed since configure: CMake identified {identity}, and {info.get('cxx_compiler')} "
                f"now says {banner}; configure and build again")
    return ""


def compiler_identity(cxx: str) -> str:
    """The name and version of the compiler that runs when CXX is started: "Clang 22.1.8", "GNU 15.2.1" or
    "MSVC 19.51.36248", read from its own banner (cl.exe with no arguments, any other with --version). Empty
    if it cannot be started or its banner is not one of these. The cache can name a compiler that a later
    upgrade has replaced; the banner cannot."""
    if not cxx:
        return ""
    is_cl = Path(cxx).name.lower() in ("cl", "cl.exe")
    try:
        p = subprocess.run([cxx] if is_cl else [cxx, "--version"], capture_output=True, text=True, errors="replace",
                           timeout=COMPILER_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    text = p.stdout + "\n" + p.stderr
    for pattern, name in ((r"Optimizing Compiler Version (\d+(?:\.\d+)+)", "MSVC"), (r"clang version (\d+(?:\.\d+)*)", "Clang"),
                          (r"^(?:g\+\+|c\+\+|gcc)(?=[\s(])[^\n]*?\s(\d+\.\d+\.\d+)(?:\s|$)", "GNU")):
        m = re.search(pattern, text, re.MULTILINE)
        if m:
            return f"{name} {m.group(1)}"
    return ""


def normalized_sha256(path: Path) -> str:
    """sha256 of a file with CRLF read as LF, as lab/bin/inputs_hash.py hashes sources."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def git_program() -> str:
    """The git to run: LAB_GIT when it is set (the lab's W jobs may not have git on PATH), else git."""
    return os.environ.get("LAB_GIT") or "git"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """git -C REPO ARGS. Raises RuntimeError, naming the program, when git cannot be started."""
    program = git_program()
    try:
        return subprocess.run([program, "-C", str(repo), *args], capture_output=True, text=True)
    except OSError as e:
        raise RuntimeError(f"git could not be started ('{program}': {e}); put git on PATH or set LAB_GIT to a git "
                           "executable") from None


def ninja_path(build: Path) -> str:
    """The ninja that built BUILD: CMAKE_MAKE_PROGRAM of its CMakeCache.txt. Never a bare `ninja` from PATH: the lab's W
    jobs pass -DCMAKE_MAKE_PROGRAM=<ninja> and have no ninja on PATH in the run phase. Raises InputsError."""
    program = read_cache(build).get("CMAKE_MAKE_PROGRAM", "")
    if not program:
        raise InputsError(3, f"{build}/CMakeCache.txt names no CMAKE_MAKE_PROGRAM, so the build's ninja is unknown")
    if not Path(program).is_file():
        raise InputsError(3, f"the build's ninja {program} (CMAKE_MAKE_PROGRAM) does not exist on this machine")
    return program


def tool_env(build: Path) -> dict[str, str]:
    """The environment for a child that calls a bare `ninja` (the Lab's inputs_hash.py does): the build's ninja
    first on PATH."""
    env = dict(os.environ)
    env["PATH"] = str(Path(ninja_path(build)).parent) + os.pathsep + env.get("PATH", "")
    return env


def cmake_files_sha256(repo: Path = REPO) -> str:
    """sha256 of the sorted lines '<path>\\t<sha256>' of every tracked CMakeLists.txt and *.cmake file. Raises
    RuntimeError if git fails or the repository has none."""
    p = git(repo, "ls-files")
    if p.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {(p.stderr or p.stdout).strip()[-300:]}")
    names = sorted(n for n in p.stdout.splitlines() if Path(n).name == "CMakeLists.txt" or n.endswith(".cmake"))
    if not names:
        raise RuntimeError("the repository tracks no CMakeLists.txt")
    text = "".join(f"{n}\t{normalized_sha256(repo / n)}\n" for n in names)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generator_hashes(repo: Path = REPO) -> dict[str, str]:
    """{path: sha256} of GENERATOR_FILES (CRLF read as LF). Raises RuntimeError for a file that is missing."""
    out = {}
    for rel in GENERATOR_FILES:
        path = repo / rel
        if not path.is_file():
            raise RuntimeError(f"{rel} is missing from {repo}")
        out[rel] = normalized_sha256(path)
    return out


def code_paths(repo: Path = REPO) -> list[str]:
    """The paths of bench/code_paths.txt: the code that wakeprobe and abpair report as their code commit."""
    text = (repo / "bench" / "code_paths.txt").read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def code_fingerprint(repo: Path = REPO) -> str:
    """sha256 of `git ls-tree -r HEAD` over the code paths: blob ids and paths, so the same on every
    system and independent of the line endings of a checkout. Raises RuntimeError if git fails."""
    p = git(repo, "ls-tree", "-r", "HEAD", "--", *code_paths(repo))
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError(f"git ls-tree over the code paths failed: {(p.stderr or p.stdout).strip()[-300:]}")
    return hashlib.sha256(p.stdout.encode("utf-8")).hexdigest()


def code_commit(repo: Path = REPO) -> str:
    """The last commit that changed a code path (the commit wakeprobe and abpair report as their code commit)."""
    p = git(repo, "log", "-1", "--format=%H", "--", *code_paths(repo))
    return p.stdout.strip() if p.returncode == 0 else ""


def code_dirty(repo: Path = REPO) -> list[str]:
    """The code paths with changes, tracked or untracked (a new source file is built too); empty when clean.
    Only these paths count: the adapter's own files are not code."""
    p = git(repo, "status", "--porcelain", "--untracked-files=all", "--", *code_paths(repo))
    if p.returncode != 0:
        raise RuntimeError(f"git status over the code paths failed: {(p.stderr or p.stdout).strip()[-300:]}")
    return [line for line in p.stdout.splitlines() if line.strip()]


def repo_dirty(repo: Path = REPO) -> list[str]:
    """Every path of the whole repository with a change or not tracked and not ignored (`git status --porcelain`);
    empty when the tree is clean. Ignored build directories do not count."""
    p = git(repo, "status", "--porcelain")
    if p.returncode != 0:
        raise RuntimeError(f"git status failed: {(p.stderr or p.stdout).strip()[-300:]}")
    return [line for line in p.stdout.splitlines() if line.strip()]


def path_dirty(repo: Path, rel: str) -> list[str]:
    """The `git status --porcelain` lines of one path (changed, staged or untracked); empty when it is as committed."""
    p = git(repo, "status", "--porcelain", "--untracked-files=all", "--", rel)
    if p.returncode != 0:
        raise RuntimeError(f"git status failed: {(p.stderr or p.stdout).strip()[-300:]}")
    return [line for line in p.stdout.splitlines() if line.strip()]


def inputs_hash_command(build: Path, out: Path, listing: Path, tool: Path) -> list[str]:
    cmd = [sys.executable, str(tool), "--build", f"{BUILD}={build}", "--root-label", ROOT_LABEL,
           "--out", str(out), "--list", str(listing)]
    for t in TARGETS:
        cmd += ["--target", t]
    for k in CONFIG_KEYS:
        cmd += ["--config-key", k]
    return cmd


def run_inputs_hash(build: Path, out: Path, tool: Path) -> tuple[int, str, dict[str, dict[str, str]]]:
    """Runs inputs_hash.py: (its exit code, its stderr, the file listing), with the build's ninja on PATH."""
    with tempfile.TemporaryDirectory(prefix="wakeloop-inputs-") as tmp:
        listing = Path(tmp) / "inputs.tsv"
        r = subprocess.run(inputs_hash_command(build, out, listing, tool), capture_output=True, text=True,
                           env=tool_env(build))
        files = read_listing(listing.read_text(encoding="utf-8")) if listing.exists() else {}
    return r.returncode, r.stderr, files


def compute_inputs(build: Path, host: str, tool: Path | None = None, repo: Path = REPO) -> dict:
    """What the build in BUILD compiled, as {"scope": ..., "builds": {"wakeloop": {...}}}: the fields of
    inputs_hash.py's project mode and the more named in the module text. Nothing is written. Raises InputsError."""
    build = Path(build)
    ninja = ninja_path(build)
    try:
        p = subprocess.run([ninja, "-C", str(build), "-t", "compdb"], capture_output=True, text=True)
    except OSError as e:
        raise InputsError(3, f"ninja did not run ({ninja}): {e}") from None
    if p.returncode != 0:
        raise InputsError(3, f"ninja -t compdb failed: {p.stderr.strip()[-300:]}")
    problems = check_targets(compiled_targets(json.loads(p.stdout)))
    if problems:
        raise InputsError(2, "; ".join(problems))
    try:
        tool = tool or default_inputs_hash()
    except FileNotFoundError as e:
        raise InputsError(3, str(e)) from None
    with tempfile.TemporaryDirectory(prefix="wakeloop-inputs-") as tmp:
        out = Path(tmp) / "inputs.json"
        rc, stderr, listing = run_inputs_hash(build, out, tool)
        if rc != 0:
            raise InputsError(3, f"{tool} failed: {stderr.strip()[-500:]}")
        data = json.loads(out.read_text(encoding="utf-8"))
    info = data["builds"][BUILD]
    backends = [b for b in info["config"]["WAKELOOP_BACKENDS"].split(";") if b]
    problems = check_files(listing, backends, host == "W")
    if problems:
        raise InputsError(2, "; ".join(problems))
    cxx = compiler_executable(build)
    try:
        info["config"] = {**info["config"], CMAKE_FILES_KEY: cmake_files_sha256(repo)}
    except RuntimeError as e:
        raise InputsError(3, str(e)) from None
    info["compiler"] = compiler_identification(build)  # what compiled the binaries
    info["compiler_cmake"] = info["compiler"]  # the same, kept under its old name
    info["compiler_banner"] = compiler_identity(cxx)  # what the compiler says today
    info["cxx_compiler"] = cxx
    info["host"] = host
    info["inputs_hash_tool"] = {"path": str(tool), "sha256": hashlib.sha256(tool.read_bytes()).hexdigest()}
    info["file_hashes"] = listing
    return data


def write_whole(path: Path, text: str) -> None:
    """Writes PATH by a rename of a finished temporary file, so that it never holds half of what it should."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", type=Path, required=True, help="a configured and built Ninja tree of this repository")
    ap.add_argument("--host", required=True, choices=HOSTS)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--inputs-hash", type=Path, help="lab/bin/inputs_hash.py (default: found as above)")
    a = ap.parse_args(argv)
    a.out.unlink(missing_ok=True)  # an older file must not outlive a refusal
    try:
        data = compute_inputs(a.build, a.host, a.inputs_hash)
    except InputsError as e:
        print(f"build_inputs: {e}", file=sys.stderr)
        return e.code
    info = data["builds"][BUILD]
    changed = compiler_change(info)
    if changed:
        print(f"build_inputs: {changed}", file=sys.stderr)
        return 2
    write_whole(a.out, json.dumps(data, indent=1) + "\n")
    print(f"build_inputs: {a.out} ({len(TARGETS)} targets, config keys {', '.join(CONFIG_KEYS)}, "
          f"compiler {info['compiler'] or 'not found'})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

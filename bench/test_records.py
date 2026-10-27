#!/usr/bin/env python3
"""Tests of the D5 records adapter: bench/build_inputs.py, gate_lib.py, check_records_v2.py, wakeloop_record.py,
campaign_smoke.py and the drivers. Python 3, standard library only (unittest). Nothing here builds a project,
times anything or starts a sanitizer; the one test that starts a measurement program asks it only for its own
--validate-args and is skipped without a build that has the flag. The records of a real build are made by
bench/sanitize_wakeloop.sh and .ps1; the end-to-end test of this adapter is a dry run of those on the desktop
(DriverRunTests, opt-in because it takes minutes).

The tests marked "review" are the reproductions of a fresh review of this adapter (4 critical and 4 high findings,
2026-10-27), turned into tests that fail while the finding is there.

Run: python3 -I -B bench/test_records.py
"""

import atexit
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_inputs as bi  # noqa: E402
import campaign_smoke as cs  # noqa: E402
import check_records_v2 as cr  # noqa: E402
import gate_lib as gl  # noqa: E402
import wakeloop_record as wr  # noqa: E402

REAL_GIT = bi.git  # before any test patches it
FINGERPRINT = "ab" * 32
OTHER_FINGERPRINT = "cd" * 32
CMAKE_SHA = "cf" * 32
LINUX_BACKENDS = ["epoll", "io_uring", "condvar"]
WINDOWS_BACKENDS = ["iocp", "condvar"]
COMPILERS = {"L": "Clang 22.1.8", "W": "MSVC 19.51.36248"}
CANONICAL_REPORT = ("ERROR: (Address|Memory|Leak|Thread)Sanitizer|WARNING: (Memory|Thread)Sanitizer|"
                    "SUMMARY: [A-Za-z]+Sanitizer|runtime error:")
GENERATORS = {rel: hashlib.sha256(rel.encode()).hexdigest() for rel in bi.GENERATOR_FILES}
SHAPE_KEYS = [("wakeprobe", "epoll", "none", 1, "single", "S", False, "timed", "random", None, None),
              ("abpair", "epoll", "none", "absent_wake", 1, 1, "H4", True)]


def hashes(seed="a"):
    return {t: hashlib.sha256((seed + t).encode()).hexdigest() for t in bi.TARGETS}


def config(tag="L", backends=None, **changes):
    backends = backends or (LINUX_BACKENDS if tag == "L" else WINDOWS_BACKENDS)
    return {"CMAKE_BUILD_TYPE": "Release", "WAKELOOP_BACKENDS": ";".join(backends), "WAKELOOP_BENCH": "ON",
            **gl.PINNED_FLAGS[tag], bi.CMAKE_FILES_KEY: CMAKE_SHA, **changes}


CONFIG = config()


def record(tag="L", san="asan", seed="a", **changes):
    """A whole, green record of host tag `tag` (L or W); `changes` override any field, "host" included. With green=False
    it is red with a finding (a failed test), unless it says otherwise: blocking=False and the reasons of an
    infrastructure failure (see red_record)."""
    r = {"format": bi.RECORD_FORMAT, "sanitizer": san, "green": True, "blocking": False, "red_reasons": [], "dry_run": False,
         "host": gl.LAB_HOSTS[tag][0], "compiler": COMPILERS[tag],
         "inputs_hash": hashes(seed), "config": config(tag), "code_fingerprint": FINGERPRINT, "generators": GENERATORS,
         "logs_archive": f"/home/alex/lab/records-logs/wakeloop2-x-{tag}-{san}.tar.gz", "logs_archive_sha256": "ab" * 32,
         "smoke": {"shape_keys": [list(k) for k in SHAPE_KEYS]}}
    if changes.get("green") is False:
        r.update(blocking=True, red_reasons=[{"class": "test", "detail": "1 tests failed or timed out"}])
    r.update(changes)
    return r


def red_record(tag="L", san="asan", cls="logs-pack", **changes):
    """A record that is red for the reason class `cls` (wakeloop_record.FINDING_CLASSES: a finding that blocks; the
    others are infrastructure and never block)."""
    finding = cls in wr.FINDING_CLASSES
    return record(tag, san, green=False, blocking=finding, red_reasons=[{"class": cls, "detail": "a " + cls + " reason"}],
                  **changes)


def make_archive(directory, name, content=b"logs"):
    """A logs archive on disk: (its path, its sha256)."""
    path = Path(directory) / (name + ".tar.gz")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content + name.encode())
    return str(path), hashlib.sha256(path.read_bytes()).hexdigest()


def put(directory, name, rec):
    Path(directory, name).write_text(json.dumps(rec), encoding="utf-8")


class TempCase(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory(prefix="records test ")  # a space in every path
        self.addCleanup(holder.cleanup)
        self.root = Path(os.path.realpath(holder.name))


# ---------------------------------------------------------------------------------------------
# build_inputs
# ---------------------------------------------------------------------------------------------

def listing_for(backends, windows=False):
    """A file listing as inputs_hash.py would give it, with every expected file."""
    out = {}
    for target, files in bi.expected_files(backends).items():
        for rel in files:
            name = f"{bi.ROOT_LABEL}/{rel}"
            out.setdefault(target, {})[name.lower() if windows else name] = "0" * 64
    return out


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                   capture_output=True)


class BuildInputsTests(TempCase):
    def test_the_targets_a_build_must_compile(self):
        self.assertEqual(bi.check_targets(set(bi.TARGETS)), [])
        self.assertIn("wakeprobe", " ".join(bi.check_targets({"wakeloop", "wakeloop_tests"})))
        self.assertIn("WAKELOOP_BENCH=OFF", " ".join(bi.check_targets({"wakeloop", "wakeloop_tests"})))
        self.assertIn("extra_tool", " ".join(bi.check_targets(set(bi.TARGETS) | {"extra_tool"})))

    def test_targets_are_read_from_the_compile_database(self):
        db = [{"output": "CMakeFiles/wakeloop.dir/loop/src/common.cpp.o"},
              {"output": "bench\\CMakeFiles\\wakeprobe.dir\\wakeprobe.cpp.obj"},
              {"output": "libwakeloop.a"}, {"output": "bench/wakeprobe"}, {}]
        self.assertEqual(bi.compiled_targets(db), {"wakeloop", "wakeprobe"})

    def test_the_hash_must_hold_the_sources_each_platform_compiles(self):
        for backends, windows in ((LINUX_BACKENDS, False), (WINDOWS_BACKENDS, True)):
            with self.subTest(backends=backends):
                self.assertEqual(bi.check_files(listing_for(backends, windows), backends, windows), [])

    def test_a_missing_loop_source_is_found(self):
        listing = listing_for(LINUX_BACKENDS)
        del listing["wakeloop"]["wakeloop/loop/src/uring.cpp"]
        problems = bi.check_files(listing, LINUX_BACKENDS, False)
        self.assertEqual(len(problems), 1)
        self.assertIn("uring.cpp", problems[0])
        listing = listing_for(WINDOWS_BACKENDS, True)
        del listing["abpair"]["wakeloop/bench/abpair.cpp"]
        self.assertIn("abpair.cpp", " ".join(bi.check_files(listing, WINDOWS_BACKENDS, True)))

    def test_an_unknown_backend_is_a_problem(self):
        self.assertIn("kqueue", " ".join(bi.check_files(listing_for(LINUX_BACKENDS), ["kqueue"], False)))

    def test_the_backends_decide_which_sources_are_expected(self):
        self.assertIn("loop/src/epoll.cpp", bi.expected_files(["epoll"])["wakeloop"])
        self.assertNotIn("loop/src/uring.cpp", bi.expected_files(["epoll"])["wakeloop"])
        self.assertIn("loop/src/iocp.cpp", bi.expected_files(["iocp"])["wakeloop"])
        self.assertEqual(bi.expected_files(["epoll", "io_uring"])["wakeloop"].count("loop/src/linux_fd.hpp"), 1)

    def test_the_listing_is_read(self):
        text = "wakeloop\twakeprobe\twakeloop/bench/a.cpp\t" + "1" * 64 + "\nother\tx\ty\tz\nbroken line\n"
        self.assertEqual(bi.read_listing(text), {"wakeprobe": {"wakeloop/bench/a.cpp": "1" * 64}})

    def test_the_command_hashes_every_target_and_no_sanitizer_option(self):
        cmd = bi.inputs_hash_command(Path("b"), Path("o.json"), Path("l.tsv"), Path("inputs_hash.py"))
        self.assertEqual([cmd[i + 1] for i, word in enumerate(cmd) if word == "--target"], list(bi.TARGETS))
        keys = [cmd[i + 1] for i, word in enumerate(cmd) if word == "--config-key"]
        self.assertEqual(keys, list(bi.CONFIG_KEYS))
        self.assertFalse([k for k in keys if "SANITIZER" in k or "MSAN" in k],
                         "a key that differs between a record and the build lets no record match")
        self.assertIn(bi.ROOT_LABEL, cmd)

    def test_the_compile_and_link_flags_of_the_cache_are_part_of_the_configuration(self):
        self.assertTrue({"CMAKE_CXX_FLAGS", "CMAKE_CXX_FLAGS_RELEASE", "CMAKE_EXE_LINKER_FLAGS"} <= set(bi.CONFIG_KEYS))
        self.assertEqual(set(gl.PINNED_FLAGS["L"]), set(gl.PINNED_FLAGS["W"]))
        self.assertTrue(set(gl.PINNED_FLAGS["L"]) <= set(bi.CONFIG_KEYS))

    def test_the_binaries_are_the_two_programs_of_the_campaign(self):
        self.assertEqual(sorted(bi.BINARIES["L"]), sorted(bi.BINARIES["W"]))
        self.assertEqual(sorted(bi.BINARIES["L"]), ["abpair", "wakeprobe"])
        self.assertTrue(all(v.endswith(".exe") for v in bi.BINARIES["W"].values()))

    def test_cmake_identification_is_read_from_cmakes_file(self):
        build = self.root / "build"
        (build / "CMakeFiles" / "4.4.3").mkdir(parents=True)
        (build / "CMakeFiles" / "4.4.3" / "CMakeCXXCompiler.cmake").write_text(
            'set(CMAKE_CXX_COMPILER_ID "Clang")\nset(CMAKE_CXX_COMPILER_VERSION "22.1.8")\n', encoding="utf-8")
        self.assertEqual(bi.compiler_identification(build), "Clang 22.1.8")
        self.assertEqual(bi.compiler_identification(self.root), "")

    def test_the_cache_is_read(self):
        (self.root / "CMakeCache.txt").write_text(
            "// comment\nCMAKE_BUILD_TYPE:STRING=Release\nWAKELOOP_BACKENDS:INTERNAL=epoll;io_uring;condvar\n"
            "WAKELOOP_BENCH:BOOL=ON\nCMAKE_CXX_FLAGS:STRING=\n", encoding="utf-8")
        cache = bi.read_cache(self.root)
        self.assertEqual({k: cache[k] for k in ("CMAKE_BUILD_TYPE", "WAKELOOP_BACKENDS", "WAKELOOP_BENCH")},
                         {"CMAKE_BUILD_TYPE": "Release", "WAKELOOP_BACKENDS": "epoll;io_uring;condvar", "WAKELOOP_BENCH": "ON"})
        self.assertEqual(cache["CMAKE_CXX_FLAGS"], "")
        self.assertEqual(bi.read_cache(self.root / "nowhere"), {})

    # -- the compiler that ran (M7) -----------------------------------------------------------------------

    def identify(self, cxx, stdout="", stderr=""):
        done = subprocess.CompletedProcess([], 0, stdout, stderr)
        with mock.patch.object(bi.subprocess, "run", return_value=done) as run:
            return bi.compiler_identity(cxx), run.call_args.args[0]

    def test_the_compilers_identity_is_read_from_its_banner(self):
        name, cmd = self.identify("/usr/bin/clang++", "clang version 22.1.8\nTarget: x86_64-pc-linux-gnu\n")
        self.assertEqual((name, cmd), ("Clang 22.1.8", ["/usr/bin/clang++", "--version"]))
        self.assertEqual(self.identify("c++", "Ubuntu clang version 18.1.3 (1ubuntu1)\n")[0], "Clang 18.1.3")
        self.assertEqual(self.identify("g++", "g++ (GCC) 15.2.1 20250813\nCopyright\n")[0], "GNU 15.2.1")
        name, cmd = self.identify("C:/VS/bin/Hostx64/x64/cl.exe", stderr="Microsoft (R) C/C++ Optimizing Compiler Version "
                                  "19.51.36248 for x64\nCopyright (C) Microsoft Corporation.\n")
        self.assertEqual((name, cmd), ("MSVC 19.51.36248", ["C:/VS/bin/Hostx64/x64/cl.exe"]))

    def test_a_compiler_that_cannot_be_read_has_no_identity(self):
        self.assertEqual(self.identify("/usr/bin/clang++", "no idea what this is\n")[0], "")
        self.assertEqual(bi.compiler_identity(""), "")
        self.assertEqual(bi.compiler_identity(str(self.root / "no such compiler")), "")

    @unittest.skipUnless(shutil.which("clang++"), "no clang++ here")
    def test_the_real_clang_is_identified(self):
        self.assertRegex(bi.compiler_identity(shutil.which("clang++")), r"^Clang \d+\.\d+\.\d+")

    # -- the CMake files and the generator files ----------------------------------------------------------

    def make_repo(self):
        repo = self.root / "repo"
        for rel in ("CMakeLists.txt", "bench/CMakeLists.txt", "tests/CMakeLists.txt", "tests/defect_sites.cmake",
                    "loop/a.hpp", *bi.GENERATOR_FILES):
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rel + "\n", encoding="utf-8")
        (repo / "bench" / "code_paths.txt").write_text("CMakeLists.txt\nloop\ntests\nbench/CMakeLists.txt\n", encoding="utf-8")
        git(repo, "init", "-q")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "one")
        return repo

    def test_a_flag_added_to_a_cmake_file_changes_the_hash(self):
        """review M6: add_compile_options and target_compile_definitions reach the compiler through CMake files."""
        repo = self.make_repo()
        first = bi.cmake_files_sha256(repo)
        for rel in ("CMakeLists.txt", "bench/CMakeLists.txt", "tests/CMakeLists.txt", "tests/defect_sites.cmake"):
            with self.subTest(file=rel):
                path = repo / rel
                original = path.read_text(encoding="utf-8")
                path.write_text(original + "add_compile_options(-DSOMETHING=1)\n", encoding="utf-8")
                self.assertNotEqual(bi.cmake_files_sha256(repo), first)
                path.write_text(original, encoding="utf-8")
                self.assertEqual(bi.cmake_files_sha256(repo), first)

    def test_other_files_and_line_endings_do_not_change_the_cmake_hash(self):
        repo = self.make_repo()
        first = bi.cmake_files_sha256(repo)
        (repo / "loop" / "a.hpp").write_text("changed\n", encoding="utf-8")
        self.assertEqual(bi.cmake_files_sha256(repo), first)
        (repo / "CMakeLists.txt").write_bytes(b"CMakeLists.txt\r\n")
        self.assertEqual(bi.cmake_files_sha256(repo), first, "CRLF is read as LF")

    def test_a_repository_without_cmake_files_has_no_hash(self):
        empty = self.root / "empty"
        empty.mkdir()
        git(empty, "init", "-q")
        with self.assertRaises(RuntimeError):
            bi.cmake_files_sha256(empty)

    def test_the_configuration_of_the_inputs_holds_the_cmake_files_hash(self):
        repo = self.make_repo()
        real_run = subprocess.run
        fake_ninja = subprocess.CompletedProcess([], 0, json.dumps([{"output": f"x/CMakeFiles/{t}.dir/a.cpp.o"} for t in bi.TARGETS]), "")

        def run_or_git(cmd, *args, **kwargs):
            return real_run(cmd, *args, **kwargs) if cmd[0] == "git" else fake_ninja

        (self.root / "CMakeCache.txt").write_text(f"CMAKE_MAKE_PROGRAM:FILEPATH={Path(sys.executable).as_posix()}\n"
                                                  "CMAKE_CXX_COMPILER:FILEPATH=/usr/bin/clang++\n", encoding="utf-8")
        with mock.patch.object(bi.subprocess, "run", side_effect=run_or_git), mock.patch.object(bi, "run_inputs_hash") as rih, \
                mock.patch.object(bi, "compiler_identity", return_value="Clang 23.0.0"), \
                mock.patch.object(bi, "compiler_identification", return_value="Clang 22.1.8"):

            def fake(build, out, tool):
                info = {"targets": {t: {"inputs_hash": h, "files": 3, "tus": 1} for t, h in hashes().items()},
                        "config": {"WAKELOOP_BACKENDS": "epoll;io_uring;condvar", "CMAKE_BUILD_TYPE": "Release"},
                        "third_party": {}}
                out.write_text(json.dumps({"scope": "s", "builds": {bi.BUILD: info}}), encoding="utf-8")
                return 0, "", listing_for(LINUX_BACKENDS)
            rih.side_effect = fake
            tool = self.root / "inputs_hash.py"
            tool.write_text("# a stand-in\n", encoding="utf-8")
            data = bi.compute_inputs(self.root, "L", tool, repo)
        info = data["builds"][bi.BUILD]
        self.assertEqual(info["config"][bi.CMAKE_FILES_KEY], bi.cmake_files_sha256(repo))
        self.assertEqual((info["compiler"], info["host"]), ("Clang 22.1.8", "L"), "what CMake identified compiled the binaries")
        self.assertEqual(info["compiler_banner"], "Clang 23.0.0", "and today's banner is kept beside it, for the gate to compare")
        self.assertEqual(sorted(info["file_hashes"]), sorted(bi.TARGETS))

    def test_the_generator_files_are_hashed_and_a_missing_one_is_refused(self):
        repo = self.make_repo()
        got = bi.generator_hashes(repo)
        self.assertEqual(sorted(got), sorted(bi.GENERATOR_FILES))
        (repo / "analysis" / "v2" / "grid.py").write_text("a different grid\n", encoding="utf-8")
        self.assertNotEqual(bi.generator_hashes(repo)["analysis/v2/grid.py"], got["analysis/v2/grid.py"])
        (repo / "bench" / "run_h5.sh").unlink()
        with self.assertRaises(RuntimeError):
            bi.generator_hashes(repo)

    def test_the_generators_are_the_files_that_decide_the_smoke(self):
        self.assertEqual(set(bi.GENERATOR_FILES), {"analysis/v2/plan.py", "analysis/v2/grid.py", "bench/campaign_smoke.py",
                                                   "bench/run_h5.sh", "bench/run_h5.ps1"})
        for rel in bi.GENERATOR_FILES:
            self.assertTrue((bi.REPO / rel).is_file(), rel)

    # -- an inputs file is whole or absent (H4) -----------------------------------------------------------------

    def main_with(self, compute, out):
        stderr = io.StringIO()
        with mock.patch.object(bi, "compute_inputs", compute), contextlib.redirect_stderr(stderr):
            code = bi.main(["--build", str(self.root), "--host", "L", "--out", str(out)])
        return code, stderr.getvalue()

    def test_review_a_refused_inputs_file_is_not_left_behind(self):
        """review H4: build_inputs exited 2 after inputs_hash.py had written --out; the drivers ignored the code."""
        out = self.root / "build.inputs.json"

        def refuse(build, host, tool=None, repo=None):
            raise bi.InputsError(2, "target wakeloop: the hashed files do not include wakeloop/loop/src/uring.cpp")
        code, message = self.main_with(refuse, out)
        self.assertEqual(code, 2)
        self.assertIn("uring.cpp", message)
        self.assertFalse(out.exists())
        self.assertEqual([p.name for p in self.root.iterdir()], [], "no temporary file either")

    def test_an_older_inputs_file_does_not_outlive_a_failed_run(self):
        """review C1: a failed build_inputs run also left the old file in place."""
        out = self.root / "build.inputs.json"
        out.write_text('{"builds": {"stale": 1}}', encoding="utf-8")

        def fail(build, host, tool=None, repo=None):
            raise bi.InputsError(3, "ninja did not run")
        code, _ = self.main_with(fail, out)
        self.assertEqual(code, 3)
        self.assertFalse(out.exists())

    def test_a_good_run_writes_the_file_whole_by_a_rename(self):
        out = self.root / "sub" / "build.inputs.json"
        info = {"compiler": "Clang 22.1.8", "compiler_banner": "Clang 22.1.8", "targets": {}}
        replaced = []
        real_replace = os.replace
        with mock.patch.object(bi.os, "replace", lambda a, b: (replaced.append((Path(a).name, Path(b).name)), real_replace(a, b))[1]):
            code, _ = self.main_with(lambda build, host, tool=None, repo=None: {"scope": "s", "builds": {bi.BUILD: info}}, out)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["builds"][bi.BUILD], info)
        self.assertEqual(replaced, [("build.inputs.json.tmp", "build.inputs.json")])


class CodePathsTests(TempCase):
    """code_fingerprint, code_commit, code_dirty and repo_dirty on a small repository of their own."""

    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        (self.repo / "bench").mkdir(parents=True)
        (self.repo / "loop").mkdir()
        (self.repo / "bench" / "code_paths.txt").write_text("# comment\nloop\nbench/code_paths.txt\nbench/wakeprobe.cpp\n\n",
                                                            encoding="utf-8")
        (self.repo / "loop" / "a.hpp").write_text("a\n", encoding="utf-8")
        (self.repo / "bench" / "wakeprobe.cpp").write_text("p\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("build-*/\n", encoding="utf-8")
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "one")

    def test_paths_skip_comments_and_blank_lines(self):
        self.assertEqual(bi.code_paths(self.repo), ["loop", "bench/code_paths.txt", "bench/wakeprobe.cpp"])

    def test_a_clean_tree_is_clean_and_its_fingerprint_is_stable(self):
        self.assertEqual(bi.code_dirty(self.repo), [])
        self.assertEqual(bi.repo_dirty(self.repo), [])
        first = bi.code_fingerprint(self.repo)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(bi.code_fingerprint(self.repo), first)

    def test_a_change_to_code_is_dirty_and_a_new_code_file_too(self):
        (self.repo / "loop" / "a.hpp").write_text("changed\n", encoding="utf-8")
        self.assertEqual(len(bi.code_dirty(self.repo)), 1)
        git(self.repo, "checkout", "-q", "--", "loop/a.hpp")
        (self.repo / "loop" / "new.cpp").write_text("n\n", encoding="utf-8")
        self.assertIn("new.cpp", bi.code_dirty(self.repo)[0])

    def test_the_adapters_own_files_are_not_code_but_the_whole_tree_sees_them(self):
        (self.repo / "bench" / "check_records_v2.py").write_text("x\n", encoding="utf-8")
        (self.repo / "bench" / "coverage.json").write_text("{}\n", encoding="utf-8")
        self.assertEqual(bi.code_dirty(self.repo), [])
        self.assertEqual(len(bi.repo_dirty(self.repo)), 2, "bench/check_records_v2.py and bench/coverage.json, untracked")
        git(self.repo, "add", "-A")
        self.assertEqual(len(bi.repo_dirty(self.repo)), 2)

    def test_ignored_build_folders_do_not_make_the_tree_dirty(self):
        (self.repo / "build-d5-asan").mkdir()
        (self.repo / "build-d5-asan" / "x.o").write_text("object\n", encoding="utf-8")
        self.assertEqual(bi.repo_dirty(self.repo), [])

    def test_any_changed_file_makes_the_tree_dirty(self):
        (self.repo / "README.md").write_text("new\n", encoding="utf-8")
        self.assertEqual(bi.code_dirty(self.repo), [])
        self.assertEqual(len(bi.repo_dirty(self.repo)), 1)

    def test_the_fingerprint_follows_the_committed_code_only(self):
        first = bi.code_fingerprint(self.repo)
        (self.repo / "README.md").write_text("not code\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "two")
        self.assertEqual(bi.code_fingerprint(self.repo), first)
        (self.repo / "loop" / "a.hpp").write_text("changed\n", encoding="utf-8")
        git(self.repo, "commit", "-q", "-am", "three")
        self.assertNotEqual(bi.code_fingerprint(self.repo), first)

    def test_the_code_commit_is_the_last_that_changed_code(self):
        first = bi.git(self.repo, "rev-parse", "HEAD").stdout.strip()
        (self.repo / "README.md").write_text("not code\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "two")
        self.assertEqual(bi.code_commit(self.repo), first)

    def test_a_repository_without_commits_raises(self):
        empty = self.root / "empty"
        (empty / "bench").mkdir(parents=True)
        (empty / "bench" / "code_paths.txt").write_text("loop\n", encoding="utf-8")
        git(empty, "init", "-q")
        with self.assertRaises(RuntimeError):
            bi.code_fingerprint(empty)

    def test_the_real_repository_has_a_fingerprint(self):
        if not (bi.REPO / ".git").exists():
            self.skipTest("not a git checkout")
        self.assertRegex(bi.code_fingerprint(), r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------------------------
# gate_lib: the gate
# ---------------------------------------------------------------------------------------------

class GateCase(TempCase):
    """A build, records of both systems and the stand-ins for what needs git, ninja or a compiler. run_gate itself,
    check_build_config, scan_binaries and the records' matching are the real code."""

    def setUp(self):
        super().setUp()
        self.own = self.root / "own"
        self.theirs = self.root / "theirs"
        self.build = self.root / "build dir"
        for d in (self.own, self.theirs, self.build / "bench"):
            d.mkdir(parents=True)
        for tool, rel in bi.BINARIES["L"].items():
            (self.build / rel).write_bytes(b"binary " + tool.encode())
        self.write_cache()
        self.info_changes = {}
        self.compute_calls = []
        self.logs = self.root / "records-logs"
        for san in gl.SANITIZERS_BY_HOST["L"]:
            put(self.own, f"wakeloop2-abc-L-{san}.json", self.own_record(san))
        put(self.theirs, "wakeloop2-abc-W-asan.json", record("W", "asan", seed="w"))
        for patch in (mock.patch.object(bi, "code_dirty", return_value=[]),
                      mock.patch.object(bi, "path_dirty", return_value=[]),
                      mock.patch.object(gl, "check_binaries_built"),  # the real ones have tests of their own
                      mock.patch.object(gl, "check_binary_compiler"),
                      mock.patch.object(bi, "code_fingerprint", return_value=FINGERPRINT),
                      mock.patch.object(bi, "code_commit", return_value="c" * 40),
                      mock.patch.object(bi, "generator_hashes", return_value=GENERATORS),
                      mock.patch.object(bi, "git", return_value=subprocess.CompletedProcess([], 0, "h" * 40 + "\n", "")),
                      mock.patch.object(gl, "build_is_current", return_value=""),
                      mock.patch.object(bi, "compute_inputs", self.fake_compute)):
            patch.start()
            self.addCleanup(patch.stop)

    def own_record(self, san, **changes):
        """A record of this system's whose logs archive is on this machine, as the gate wants it."""
        archive, sha = make_archive(self.logs, f"wakeloop2-abc-L-{san}")
        return record("L", san, logs_archive=archive, logs_archive_sha256=sha, **changes)

    def write_cache(self, **overrides):
        entries = {"CMAKE_HOME_DIRECTORY": bi.REPO.as_posix(), "CMAKE_BUILD_TYPE": "Release",
                   "CMAKE_CXX_COMPILER": "/usr/bin/clang++", **gl.PINNED_FLAGS["L"], **overrides}
        text = "".join(f"{k}:STRING={v}\n" for k, v in entries.items() if v is not None)
        (self.build / "CMakeCache.txt").write_text(text, encoding="utf-8")

    def fake_compute(self, build, host, tool=None, repo=None):
        self.compute_calls.append((Path(build), host, tool))
        info = {"targets": {t: {"inputs_hash": h, "files": 3, "tus": 1} for t, h in hashes().items()},
                "config": config(), "compiler": COMPILERS["L"], "compiler_banner": COMPILERS["L"], "host": host,
                "cxx_compiler": "/usr/bin/clang++", "build_dir": str(build)}
        info.update(self.info_changes)
        return {"scope": "s", "builds": {bi.BUILD: info}}

    def gate(self, **kwargs):
        args = {"build": self.build, "host": "L", "records": self.own, "other_records": self.theirs, **kwargs}
        with contextlib.redirect_stderr(io.StringIO()):
            return gl.run_gate(**args)

    def refused(self, needle, **kwargs):
        with self.assertRaises(gl.GateError) as caught:
            self.gate(**kwargs)
        self.assertIn(needle, str(caught.exception))
        return str(caught.exception)


class GateTests(GateCase):
    def test_a_covered_build_passes_and_the_result_binds_the_binaries_and_the_records(self):
        result = self.gate()
        self.assertTrue(result["passed"] and result["citable"] and not result["dry_run"])
        self.assertEqual(result["host"], "L")
        self.assertEqual(result["partial"], {})
        self.assertEqual(sorted(result["binaries"]), ["abpair", "wakeprobe"])
        self.assertEqual(result["binaries"]["wakeprobe"], hashlib.sha256(b"binary wakeprobe").hexdigest())
        self.assertEqual(sorted(result["wakeloop"]["coverage"]), ["asan", "msan", "tsan"])
        entry = result["wakeloop"]["coverage"]["tsan"][0]
        self.assertEqual(entry["name"], "wakeloop2-abc-L-tsan.json")
        self.assertEqual(entry["sha256"], hashlib.sha256((self.own / entry["name"]).read_bytes()).hexdigest())
        self.assertEqual(result["other_host"]["host"], "W")
        self.assertEqual(result["other_host"]["records"]["asan"][0]["name"], "wakeloop2-abc-W-asan.json")
        self.assertEqual(result["code_fingerprint"], FINGERPRINT)
        self.assertEqual(result["generators"], GENERATORS)
        self.assertEqual(result["smoke_keys"], frozenset(SHAPE_KEYS))
        self.assertEqual(result["coverage_json_sha256"],
                         hashlib.sha256(gl.COVERAGE_JSON.read_bytes().replace(b"\r\n", b"\n")).hexdigest())

    # -- C1: the gate hashes the build itself ------------------------------------------------------------

    def test_review_c1_the_gate_hashes_the_build_and_takes_no_inputs_file(self):
        import inspect
        self.assertEqual(set(inspect.signature(gl.run_gate).parameters),
                         {"build", "host", "records", "other_records", "tool", "repo", "accept_dry_run", "resume_compiler"})
        self.gate()
        self.assertEqual(self.compute_calls, [(self.build, "L", None)])

    def test_review_c1_a_build_rebuilt_at_another_commit_is_not_covered_by_the_old_records(self):
        self.info_changes = {"targets": {t: {"inputs_hash": h, "files": 3, "tus": 1} for t, h in hashes("y").items()}}
        self.refused("no green asan, tsan, msan record")

    def test_a_record_turning_red_between_two_gates_stops_the_second(self):
        """review C4: a stale gate passed after a red record of the same inputs appeared."""
        self.gate()
        put(self.own, "wakeloop2-new-L-tsan.json", record("L", "tsan", green=False))
        self.refused("is red")

    def test_the_records_are_read_again_at_every_gate(self):
        self.gate()
        (self.own / "wakeloop2-abc-L-msan.json").unlink()
        self.refused("no green msan record")

    # -- H1 and N4: the compiler is the one that compiled the binaries -------------------------------------

    def test_review_h1_there_is_no_way_to_name_the_compiler(self):
        import inspect
        self.assertNotIn("compiler", inspect.signature(gl.run_gate).parameters)
        self.info_changes = {"compiler": "GNU 15.2.1", "compiler_banner": "GNU 15.2.1"}
        self.refused("no green")

    def test_a_compiler_that_cannot_be_identified_is_refused(self):
        self.info_changes = {"compiler": ""}
        self.refused("cannot be identified")

    def test_review_n4_a_compiler_that_changed_since_configure_is_refused(self):
        """E2: the compiler named in the cache said 19.52 after an in-place update, while 19.51 had compiled the
        binaries; records of the new banner passed, and those of the compiler that did compile were refused."""
        self.info_changes = {"compiler_banner": "Clang 23.0.0"}
        self.refused("the compiler changed since configure")
        self.refused("configure and build again")
        self.info_changes = {"compiler_banner": ""}
        self.refused("cannot be started to read its banner")

    def test_review_n4_the_records_are_those_of_the_compiler_that_compiled_not_of_todays_banner(self):
        self.info_changes = {"compiler_banner": "Clang 23.0.0"}
        result = self.gate(resume_compiler="Clang 22.1.8")  # a resume does not look at the banner
        self.assertEqual(result["wakeloop"]["compiler"], "Clang 22.1.8")
        self.assertEqual(result["wakeloop"]["compiler_banner"], "Clang 23.0.0")
        for san in gl.SANITIZERS_BY_HOST["L"]:  # records made with the banner's compiler do not cover these binaries
            put(self.own, f"wakeloop2-abc-L-{san}.json", self.own_record(san, compiler="Clang 23.0.0"))
        self.refused("no green", resume_compiler="Clang 22.1.8")

    def test_a_resume_wants_the_identity_the_run_started_with(self):
        self.refused("the run being resumed started with Clang 21.0.0", resume_compiler="Clang 21.0.0")

    # -- C2: no sanitizer or unusual flags in a measured build ---------------------------------------------

    def test_review_c2_a_sanitizer_build_is_not_a_measured_build(self):
        self.write_cache(WAKELOOP_SANITIZER="address")
        self.refused("WAKELOOP_SANITIZER")
        self.write_cache(WAKELOOP_SANITIZER="address+undefined")
        self.refused("a sanitizer build is never timed")
        self.write_cache(WAKELOOP_SANITIZER="")
        self.gate()

    def test_cmakes_false_constants_are_no_sanitizer(self):
        """CMake reads -DWAKELOOP_SANITIZER=OFF as no sanitizer (if(WAKELOOP_SANITIZER) is false)."""
        for value in ("", "OFF", "off", "0", "FALSE", "No", "N", "IGNORE", "NOTFOUND", "x-NOTFOUND"):
            with self.subTest(value=value):
                self.write_cache(WAKELOOP_SANITIZER=value)
                self.gate()
        for value in ("address", "thread", "ON", "1", "memory", "OFFX"):
            with self.subTest(value=value):
                self.write_cache(WAKELOOP_SANITIZER=value)
                self.refused("WAKELOOP_SANITIZER")

    def test_review_c2_flags_and_a_toolchain_file_are_refused(self):
        for key, value in (("CMAKE_CXX_FLAGS", "-O0 -fno-inline"), ("CMAKE_CXX_FLAGS", "-fsanitize=address"),
                           ("CMAKE_CXX_FLAGS_RELEASE", "-O0"), ("CMAKE_CXX_FLAGS_RELEASE", "-O3 -DNDEBUG -march=native"),
                           ("CMAKE_EXE_LINKER_FLAGS", "-fsanitize=thread"), ("CMAKE_TOOLCHAIN_FILE", "/x/toolchain.cmake"),
                           ("CMAKE_EXE_LINKER_FLAGS_RELEASE", "-flto")):
            with self.subTest(key=key, value=value):
                self.write_cache(**{key: value})
                self.refused(key)

    def test_link_time_optimization_is_refused_but_off_is_not(self):
        self.write_cache(CMAKE_INTERPROCEDURAL_OPTIMIZATION="ON")
        self.refused("CMAKE_INTERPROCEDURAL_OPTIMIZATION")
        self.write_cache(CMAKE_INTERPROCEDURAL_OPTIMIZATION="OFF")
        self.gate()
        self.write_cache(CMAKE_INTERPROCEDURAL_OPTIMIZATION=None)
        self.gate()

    def test_a_build_that_is_not_release_or_not_of_this_checkout_is_refused(self):
        self.write_cache(CMAKE_BUILD_TYPE="Debug")
        self.refused("not Release")
        self.write_cache(CMAKE_BUILD_TYPE=None)
        self.refused("CMAKE_BUILD_TYPE")
        self.write_cache(CMAKE_HOME_DIRECTORY=(self.root / "other").as_posix())
        self.refused("not from this checkout")
        self.write_cache(CMAKE_HOME_DIRECTORY=None)
        self.refused("an unknown directory")

    def test_the_pinned_flags_name_the_keys_of_the_configuration(self):
        for tag in ("L", "W"):
            self.assertTrue(set(gl.PINNED_FLAGS[tag]) <= set(bi.CONFIG_KEYS))
        self.assertEqual(set(gl.PINNED_FLAGS["L"]), set(gl.PINNED_FLAGS["W"]))

    def test_review_c2_a_binary_linked_with_a_sanitizer_runtime_is_refused(self):
        for symbol in (b"__asan_init", b"__tsan_init", b"__msan_init", b"__ubsan_handle_add_overflow", b"clang_rt.asan_dynamic"):
            with self.subTest(symbol=symbol):
                for rel in bi.BINARIES["L"].values():
                    (self.build / rel).write_bytes(b"\x7fELF....some code " + symbol + b" more code")
                self.refused("linked with a sanitizer runtime")
        for rel in bi.BINARIES["L"].values():
            (self.build / rel).write_bytes(b"clean")
        self.gate()

    def test_a_missing_binary_is_refused(self):
        (self.build / bi.BINARIES["L"]["abpair"]).unlink()
        self.refused("abpair")

    def linux_records_for_a_windows_gate(self):
        shutil.rmtree(self.theirs)
        self.theirs.mkdir()
        for san in gl.SANITIZERS_BY_HOST["L"]:
            put(self.theirs, f"wakeloop2-abc-L-{san}.json", record("L", san))

    def test_cl_and_underscore_cl_change_what_cl_compiles_so_a_windows_gate_refuses_them(self):
        for var in ("CL", "_CL_"):
            with self.subTest(var=var), mock.patch.dict(os.environ, {var: "/O0"}):
                self.gate()  # the variable is cl's: an Arch gate has nothing to say about it
        self.linux_records_for_a_windows_gate()
        for var in ("CL", "_CL_"):
            with self.subTest(var=var), mock.patch.dict(os.environ, {var: "/O0"}):
                with self.assertRaises(gl.GateError) as caught:
                    self.windows_gate()
                self.assertIn(var, str(caught.exception))
                self.assertIn("extra compiler options", str(caught.exception))
        self.windows_gate()

    def test_a_changed_coverage_json_is_refused(self):
        with mock.patch.object(bi, "path_dirty", return_value=[" M bench/coverage.json"]):
            self.refused("bench/coverage.json is changed or untracked")

    # -- H2: a declared gap waives nothing ----------------------------------------------------------------

    def test_review_h2_a_declared_gap_never_waives_a_required_record(self):
        for san in ("msan", "tsan"):
            (self.own / f"wakeloop2-abc-L-{san}.json").unlink()
        gaps = self.root / "coverage.json"
        gaps.write_text(json.dumps({"targets": {t: {"not_covered_by": ["msan", "tsan"], "reason": "x"} for t in bi.TARGETS}}),
                        encoding="utf-8")
        with mock.patch.object(gl, "COVERAGE_JSON", gaps):
            self.refused("no green tsan, msan record")

    def test_the_required_sanitizers_are_fixed_in_the_code(self):
        self.assertEqual(gl.SANITIZERS_BY_HOST, {"L": ("asan", "tsan", "msan"), "W": ("asan",)})
        import inspect
        self.assertNotIn("declared", inspect.signature(gl.target_coverage).parameters)
        self.assertNotIn("coverage", inspect.signature(gl.run_gate).parameters)

    def test_coverage_json_annotates_and_declares_no_target_gap(self):
        cov = json.loads(gl.COVERAGE_JSON.read_text(encoding="utf-8"))
        self.assertEqual(cov["targets"], {}, "a target gap would be a record that is allowed to be missing; none is")

    # -- M1, M2, N7, H3: the records are whole, kept and made by today's generators --------------------------

    def test_review_m1_two_records_that_each_match_half_the_targets_do_not_combine(self):
        for p in self.own.glob("*.json"):
            p.unlink()
        a, b = hashes(), hashes("x")
        half1 = {t: (a[t] if t in ("wakeloop", "wakeloop_tests") else b[t]) for t in bi.TARGETS}
        half2 = {t: (b[t] if t in ("wakeloop", "wakeloop_tests") else a[t]) for t in bi.TARGETS}
        for san in gl.SANITIZERS_BY_HOST["L"]:
            put(self.own, f"wakeloop2-old-L-{san}.json", self.own_record(san, inputs_hash=half1))
            put(self.own, f"wakeloop2-new-L-{san}.json", self.own_record(san, inputs_hash=half2))
        self.refused("matches every target")

    def test_review_m2_a_record_without_its_logs_archive_does_not_gate(self):
        for change in ({"logs_archive": None}, {"logs_archive": ""}, {"logs_archive_sha256": None},
                       {"logs_archive_sha256": "not a sha"}, {"logs_archive_sha256": "AB" * 32}):
            with self.subTest(change=change):
                rec = {k: v for k, v in self.own_record("asan").items() if k not in change}
                rec.update({k: v for k, v in change.items() if v is not None})
                put(self.own, "wakeloop2-abc-L-asan.json", rec)
                self.refused("no logs archive and sha256")

    def test_an_archive_that_is_here_must_have_the_sha256_the_record_names(self):
        archive = self.root / "wakeloop2-x-L-asan.tar.gz"
        archive.write_bytes(b"the logs")
        put(self.own, "wakeloop2-abc-L-asan.json", record("L", "asan", logs_archive=str(archive),
                                                         logs_archive_sha256=hashlib.sha256(b"the logs").hexdigest()))
        self.gate()
        archive.write_bytes(b"other logs")
        self.refused("does not have the sha256")

    def test_review_n7_a_record_whose_logs_archive_is_not_on_this_machine_does_not_gate(self):
        """E7: a record that named an archive that did not exist passed; on the system that made it, it must exist."""
        put(self.own, "wakeloop2-abc-L-asan.json", record("L", "asan", logs_archive=str(self.logs / "gone.tar.gz")))
        message = self.refused("no green asan record")
        self.assertIn("is not on this machine", message)
        (self.logs / "wakeloop2-abc-L-asan.tar.gz").unlink()
        put(self.own, "wakeloop2-abc-L-asan.json", self.own_record("asan"))  # made again: the archive is there
        self.gate()

    def test_the_other_systems_archives_are_not_looked_for_here(self):
        put(self.theirs, "wakeloop2-abc-W-asan.json", record("W", "asan", logs_archive=r"C:\\Users\\alex\\lab\\records-logs\\x.tar.gz"))
        self.gate()

    def test_review_h3_a_record_made_with_other_generator_files_does_not_gate(self):
        put(self.own, "wakeloop2-abc-L-asan.json", self.own_record("asan", generators={**GENERATORS, "analysis/v2/grid.py": "00" * 32}))
        self.refused("other generator files")
        put(self.own, "wakeloop2-abc-L-asan.json", self.own_record("asan", generators={}))
        self.refused("other generator files")

    def test_a_record_without_smoke_shape_keys_does_not_gate(self):
        put(self.own, "wakeloop2-abc-L-asan.json", self.own_record("asan", smoke={}))
        self.refused("no smoke shape keys")
        put(self.own, "wakeloop2-abc-L-asan.json", self.own_record("asan", smoke={"shape_keys": []}))
        self.refused("no smoke shape keys")

    def test_review_h3_the_shapes_are_those_every_required_sanitizer_ran(self):
        a, b, c = SHAPE_KEYS[0], SHAPE_KEYS[1], ("wakeprobe", "epoll", "plain_wake", 1, "single", "S", False, "timed", "random", None, None)
        put(self.own, "wakeloop2-abc-L-asan.json", self.own_record("asan", smoke={"shape_keys": [list(a), list(b), list(c)]}))
        put(self.own, "wakeloop2-abc-L-tsan.json", self.own_record("tsan", smoke={"shape_keys": [list(a), list(b)]}))
        put(self.own, "wakeloop2-abc-L-msan.json", self.own_record("msan", smoke={"shape_keys": [list(b), list(c)]}))
        self.assertEqual(self.gate()["smoke_keys"], frozenset({b}), "only the shape that all three ran")
        put(self.own, "wakeloop2-def-L-msan.json", self.own_record("msan", smoke={"shape_keys": [list(a)]}))
        self.assertEqual(self.gate()["smoke_keys"], frozenset({a, b}), "two records of one sanitizer: either proves a shape")

    # -- N2, N3, N6: records that do not fit are skipped; only a finding blocks ------------------------------

    def test_review_b1_an_older_green_record_of_the_same_inputs_with_other_generator_files_is_skipped(self):
        """B1: a comment fixed in run_h5.sh since the first records made every older record fatal."""
        put(self.own, "wakeloop2-0ld000000-L-tsan.json",
            self.own_record("tsan", generators={**GENERATORS, "bench/run_h5.sh": "00" * 32}))
        result = self.gate()
        self.assertEqual([e["name"] for e in result["wakeloop"]["coverage"]["tsan"]], ["wakeloop2-abc-L-tsan.json"])
        self.assertTrue([n for n in result["notes"] if "0ld000000" in n and "other generator files" in n], result["notes"])

    def test_review_b2_the_same_for_the_other_systems_records(self):
        put(self.theirs, "wakeloop2-0ld000000-W-asan.json", record("W", "asan", seed="w", generators={}))
        result = self.gate()
        self.assertEqual([e["name"] for e in result["other_host"]["records"]["asan"]], ["wakeloop2-abc-W-asan.json"])
        self.assertTrue([n for n in result["notes"] if "0ld000000" in n], result["notes"])

    def test_review_b3_an_older_record_without_logs_fields_is_skipped(self):
        old = self.own_record("msan")
        for k in ("logs_archive", "logs_archive_sha256", "smoke", "format"):
            old.pop(k)
        put(self.own, "wakeloop2-0ld000000-L-msan.json", old)
        result = self.gate()
        self.assertEqual([e["name"] for e in result["wakeloop"]["coverage"]["msan"]], ["wakeloop2-abc-L-msan.json"])
        self.assertTrue([n for n in result["notes"] if "0ld000000" in n and "format" in n], result["notes"])

    def test_review_n6_a_record_of_another_format_is_skipped_and_is_never_trusted_alone(self):
        for fmt in (1, None, bi.RECORD_FORMAT + 1):
            with self.subTest(format=fmt):
                rec = self.own_record("asan")
                if fmt is None:
                    rec.pop("format")
                else:
                    rec["format"] = fmt
                put(self.own, "wakeloop2-abc-L-asan.json", rec)
                message = self.refused("no green asan record")
                self.assertIn(f"format {fmt!r}", message)
        put(self.own, "wakeloop2-abc-L-asan.json", self.own_record("asan"))
        put(self.theirs, "wakeloop2-abc-W-asan.json", record("W", "asan", seed="w", format=1))
        self.refused("no green asan record on W")

    def test_review_b4_a_record_red_for_an_infrastructure_reason_never_blocks_the_inputs(self):
        """B4: a record red only because its logs could not be packed (a full disk) closed the inputs for good."""
        for n, cls in enumerate(wr.INFRASTRUCTURE_CLASSES):
            with self.subTest(cls=cls):
                put(self.own, f"wakeloop2-abc00000{n}-L-asan.json", red_record("L", "asan", cls, logs_archive="", logs_archive_sha256=""))
                result = self.gate()
                self.assertEqual([e["name"] for e in result["wakeloop"]["coverage"]["asan"]], ["wakeloop2-abc-L-asan.json"])
                self.assertTrue([x for x in result["notes"] if f"abc00000{n}" in x and "not a finding" in x and cls in x], result["notes"])
        self.assertEqual(sorted(wr.INFRASTRUCTURE_CLASSES), ["dirty-tree", "host", "job", "logs-pack"])

    def test_a_red_record_with_a_finding_of_the_same_inputs_stops_the_gate(self):
        for cls in wr.FINDING_CLASSES:
            with self.subTest(cls=cls):
                put(self.own, "wakeloop2-abc111111-L-asan.json", red_record("L", "asan", cls))
                message = self.refused("red with a finding")
                self.assertIn(cls, message)
        self.assertEqual(sorted(wr.FINDING_CLASSES), ["report", "smoke-run", "test"])

    def test_a_red_record_with_a_finding_stops_even_when_it_is_of_another_format_or_has_no_reasons_but_reports(self):
        old = record("L", "asan", green=False, format=1, sanitizer_reports=2)
        del old["blocking"], old["red_reasons"]
        put(self.own, "wakeloop2-abc111111-L-asan.json", old)
        self.refused("red with a finding")
        old.update(sanitizer_reports=0, tests_failed=0)  # red, and nothing in it says a finding
        put(self.own, "wakeloop2-abc111111-L-asan.json", old)
        self.gate()

    def test_a_red_record_of_the_other_system_stops_the_gate_with_a_finding_and_only_then(self):
        put(self.theirs, "wakeloop2-abd-W-asan.json", red_record("W", "asan", "logs-pack"))
        self.gate()
        put(self.theirs, "wakeloop2-abd-W-asan.json", red_record("W", "asan", "report"))
        self.refused("red with a finding")

    def test_an_unreadable_record_is_skipped_with_a_note(self):
        (self.own / "wakeloop2-bad000000-L-asan.json").write_text("{", encoding="utf-8")
        result = self.gate()
        self.assertTrue([n for n in result["notes"] if "bad000000" in n and "cannot be read" in n], result["notes"])

    # -- the origin of records -------------------------------------------------------------------------------

    def test_a_dry_run_record_never_gates(self):
        put(self.own, "wakeloop2-abc-L-asan.json", record("L", "asan", dry_run=True))
        self.refused("dry run")

    def test_a_dry_run_record_gates_only_when_accepted_and_the_result_is_not_citable(self):
        for p in self.own.glob("*.json"):
            put(self.own, p.name, {**json.loads(p.read_text()), "dry_run": True, "host": "3f9a1c2b7d10"})
        put(self.theirs, "wakeloop2-abc-W-asan.json", record("W", "asan", dry_run=True, host="3f9a1c2b7d10"))
        result = self.gate(accept_dry_run=True)
        self.assertTrue(result["dry_run"] and not result["citable"])

    def test_a_record_the_laptop_did_not_make_is_refused(self):
        for host in ("ALEX-PC", "DESKTOP-A1RHS5R", "3f9a1c2b7d10"):
            with self.subTest(host=host):
                put(self.own, "wakeloop2-abc-L-asan.json", self.own_record("asan", host=host))
                self.refused("desktop never makes a record")

    def test_review_n8_host_names_are_compared_without_case(self):
        self.assertTrue(gl.is_lab_host("W", "desktop-a1rhs5r"))
        self.assertTrue(gl.is_lab_host("W", "DESKTOP-A1RHS5R"))
        self.assertTrue(gl.is_lab_host("L", "Alex-Laptop"))
        self.assertFalse(gl.is_lab_host("L", "DESKTOP-A1RHS5R"))
        self.assertFalse(gl.is_lab_host("L", ""))
        self.assertFalse(gl.is_lab_host("L", None))
        put(self.own, "wakeloop2-abc-L-asan.json", self.own_record("asan", host="ALEX-LAPTOP"))
        self.gate()

    def test_a_red_record_of_other_inputs_is_not_this_builds_business(self):
        put(self.own, "wakeloop2-zzz-L-asan.json", record("L", "asan", seed="z", green=False))
        self.gate()

    def test_a_record_of_another_compiler_or_configuration_does_not_cover(self):
        put(self.own, "wakeloop2-abc-L-asan.json", self.own_record("asan", compiler="Clang 21.0.0"))
        self.refused("no green asan record")
        put(self.own, "wakeloop2-abc-L-asan.json", self.own_record("asan", config={**CONFIG, "CMAKE_BUILD_TYPE": "RelWithDebInfo"}))
        self.refused("no green asan record")
        put(self.own, "wakeloop2-abc-L-asan.json", self.own_record("asan", config={**CONFIG, bi.CMAKE_FILES_KEY: "00" * 32}))
        self.refused("no green asan record")

    def test_a_flag_added_to_a_cmake_file_after_the_records_makes_them_not_cover(self):
        """review M6, at the gate: the build's configuration holds the CMake files' hash."""
        self.info_changes = {"config": config(**{bi.CMAKE_FILES_KEY: "99" * 32})}
        self.refused("no green")

    def test_only_the_hosts_records_count(self):
        for p in self.own.glob("*.json"):
            p.unlink()
        put(self.own, "wakeloop2-abc-W-asan.json", record("W", "asan"))
        self.refused("no green")

    # -- the other system ---------------------------------------------------------------------------------------

    def test_the_other_system_needs_its_records_for_the_same_code(self):
        put(self.theirs, "wakeloop2-abc-W-asan.json", record("W", "asan", code_fingerprint=OTHER_FINGERPRINT))
        self.refused("no green asan record on W for this code")

    def test_the_other_system_needs_the_same_generator_files(self):
        put(self.theirs, "wakeloop2-abc-W-asan.json", record("W", "asan", generators={}))
        self.refused("other generator files")

    def test_the_other_systems_dry_run_and_desktop_records_are_refused_too(self):
        put(self.theirs, "wakeloop2-abc-W-asan.json", record("W", "asan", dry_run=True))
        self.refused("dry run")
        put(self.theirs, "wakeloop2-abc-W-asan.json", record("W", "asan", host="ALEX-PC"))
        self.refused("desktop")

    def test_every_sanitizer_of_the_other_system_is_needed(self):
        shutil.rmtree(self.theirs)
        self.theirs.mkdir()
        for san in gl.SANITIZERS_BY_HOST["L"][:2]:  # a Windows build is gated by the Linux records: asan, tsan, not msan
            put(self.theirs, f"wakeloop2-abc-L-{san}.json", record("L", san))
        with self.assertRaises(gl.GateError) as caught:
            self.windows_gate()
        self.assertIn("msan", str(caught.exception))

    def windows_gate(self, **kwargs):
        build = self.root / "wbuild"
        (build / "bench").mkdir(parents=True, exist_ok=True)
        for tool, rel in bi.BINARIES["W"].items():
            (build / rel).write_bytes(b"binary " + tool.encode())
        entries = {"CMAKE_HOME_DIRECTORY": bi.REPO.as_posix(), "CMAKE_BUILD_TYPE": "Release", **gl.PINNED_FLAGS["W"]}
        (build / "CMakeCache.txt").write_text("".join(f"{k}:STRING={v}\n" for k, v in entries.items()), encoding="utf-8")
        wconfig = config("W")
        info = {"targets": {t: {"inputs_hash": h, "files": 3, "tus": 1} for t, h in hashes("w").items()}, "config": wconfig,
                "compiler": COMPILERS["W"], "compiler_banner": COMPILERS["W"], "host": "W", "cxx_compiler": "cl.exe",
                "build_dir": str(build)}
        with mock.patch.object(bi, "compute_inputs", lambda *a, **k: {"scope": "s", "builds": {bi.BUILD: info}}), \
                contextlib.redirect_stderr(io.StringIO()):
            return gl.run_gate(build=build, host="W", records=self.theirs_w, other_records=self.theirs, **kwargs)

    @property
    def theirs_w(self):
        path = self.root / "laptop-w"
        path.mkdir(exist_ok=True)
        if not list(path.glob("*.json")):
            archive, sha = make_archive(self.logs, "wakeloop2-abc-W-asan")
            put(path, "wakeloop2-abc-W-asan.json", record("W", "asan", seed="w", logs_archive=archive, logs_archive_sha256=sha))
        return path

    def test_a_windows_build_needs_asan_alone_and_the_linux_records_of_the_same_code(self):
        self.linux_records_for_a_windows_gate()
        result = self.windows_gate()
        self.assertEqual(sorted(result["wakeloop"]["coverage"]), ["asan"])
        self.assertEqual(sorted(result["other_host"]["records"]), ["asan", "msan", "tsan"])
        self.assertEqual(sorted(result["binaries"]), ["abpair", "wakeprobe"])

    # -- the rest of the checks -------------------------------------------------------------------------------

    def test_changed_code_paths_stop_the_gate(self):
        with mock.patch.object(bi, "code_dirty", return_value=[" M loop/src/uring.cpp"]):
            self.refused("uring.cpp")

    def test_a_build_that_is_not_up_to_date_stops_the_gate(self):
        with mock.patch.object(gl, "build_is_current", return_value="ninja -n would build: [1/2] x"):
            self.refused("not up to date")

    def test_inputs_that_cannot_be_hashed_stop_the_gate(self):
        def fail(build, host, tool=None, repo=None):
            raise bi.InputsError(2, "target wakeloop: the hashed files do not include wakeloop/loop/src/uring.cpp")
        with mock.patch.object(bi, "compute_inputs", fail):
            self.refused("cannot be hashed")

    def test_review_n1_a_git_that_cannot_be_started_is_refused_by_name(self):
        def no_git(*args, **kwargs):
            raise FileNotFoundError(2, "No such file or directory")
        with mock.patch.object(bi, "git", REAL_GIT), mock.patch.object(bi.subprocess, "run", no_git), \
                mock.patch.dict(os.environ, {"LAB_GIT": "/no/such/git"}):
            with self.assertRaises(gl.GateError) as caught:
                self.gate()
        self.assertIn("/no/such/git", str(caught.exception))
        self.assertIn("LAB_GIT", str(caught.exception))

    def test_an_unknown_host_is_refused(self):
        with self.assertRaises(gl.GateError):
            gl.sanitizers_for("X")

    def test_the_result_is_json_but_for_the_shape_keys(self):
        result = self.gate()
        keys = result.pop("smoke_keys")
        json.dumps(result)
        self.assertIsInstance(keys, frozenset)
        reported = cr.report({**result, "smoke_keys": keys})
        self.assertEqual(reported["smoke_shape_keys"]["count"], len(SHAPE_KEYS))
        json.dumps(reported)


class BuildIsCurrentTests(TempCase):
    """build_is_current against a real ninja tree: what it sees, and what it cannot (review M3)."""

    def setUp(self):
        super().setUp()
        if not shutil.which("ninja"):
            self.skipTest("no ninja on PATH")
        self.build = self.root / "ninja tree"
        self.build.mkdir()
        (self.build / "CMakeCache.txt").write_text(f"CMAKE_MAKE_PROGRAM:FILEPATH={Path(shutil.which('ninja')).as_posix()}\n",
                                                   encoding="utf-8")
        (self.build / "in.txt").write_text("input\n", encoding="utf-8")
        copy = f'"{sys.executable}" -c "import shutil, sys; shutil.copyfile(sys.argv[1], sys.argv[2])"'
        (self.build / "build.ninja").write_text(f"rule cp\n  command = {copy} $in $out\nbuild out.txt: cp in.txt\n", encoding="utf-8")

    def ninja(self):
        subprocess.run(["ninja", "-C", str(self.build)], check=True, capture_output=True)

    def test_an_unbuilt_tree_is_not_current_and_a_built_one_is(self):
        self.assertIn("ninja -n would build", gl.build_is_current(self.build))
        self.ninja()
        self.assertEqual(gl.build_is_current(self.build), "")

    def test_a_changed_input_makes_it_not_current(self):
        self.ninja()
        later = (self.build / "out.txt").stat().st_mtime + 10
        (self.build / "in.txt").write_text("changed\n", encoding="utf-8")
        os.utime(self.build / "in.txt", (later, later))
        self.assertIn("ninja -n would build", gl.build_is_current(self.build))

    def test_ninja_n_does_not_see_an_output_that_was_replaced_by_a_newer_file(self):
        """What the review of M3 showed, and why check_binaries_built exists: BinariesBuiltTests.test_review_n5_* sees
        the same replacement, by the output's time against .ninja_log, and RealBuildGateTests on a real tree."""
        self.ninja()
        out = self.build / "out.txt"
        later = out.stat().st_mtime + 10
        out.write_text("a binary that is not what the rule made\n", encoding="utf-8")
        os.utime(out, (later, later))
        self.assertEqual(gl.build_is_current(self.build), "", "ninja -n says: no work to do")

    def test_review_n1_the_builds_own_ninja_is_used_when_there_is_none_on_path(self):
        """E3: the lab's W jobs build with -DCMAKE_MAKE_PROGRAM=<ninja> and have no ninja on PATH in the run phase."""
        bare = path_without("ninja")
        self.assertIsNone(shutil.which("ninja", path=bare), "the test needs a PATH with no ninja")
        with mock.patch.dict(os.environ, {"PATH": bare}):
            self.assertIn("ninja -n would build", gl.build_is_current(self.build))
        subprocess.run([shutil.which("ninja"), "-C", str(self.build)], check=True, capture_output=True)
        with mock.patch.dict(os.environ, {"PATH": bare}):
            self.assertEqual(gl.build_is_current(self.build), "")

    def test_a_cache_without_a_ninja_is_refused_not_guessed(self):
        (self.build / "CMakeCache.txt").write_text("CMAKE_CXX_COMPILER:FILEPATH=/usr/bin/clang++\n", encoding="utf-8")
        with self.assertRaises(gl.GateError) as caught:
            gl.build_is_current(self.build)
        self.assertIn("CMAKE_MAKE_PROGRAM", str(caught.exception))

    def test_a_directory_that_is_no_ninja_tree_is_an_error(self):
        with self.assertRaises(gl.GateError):
            gl.build_is_current(self.root)

    def test_a_missing_ninja_is_an_error(self):
        with mock.patch.object(gl.subprocess, "run", side_effect=FileNotFoundError("ninja")):
            with self.assertRaises(gl.GateError):
                gl.build_is_current(self.build)


class CheckRecordsWrapperTests(GateCase):
    """check_records_v2.py only reports what gate_lib.run_gate returns."""

    def report(self, *extra, host="L"):
        out = self.root / "gate.json"
        out.unlink(missing_ok=True)
        stderr = io.StringIO()
        argv = ["--build-dir", str(self.build), "--records", str(self.own), "--other-records", str(self.theirs),
                "--host", host, *extra, "--out", str(out)]
        with contextlib.redirect_stderr(stderr):
            try:
                code = cr.main(argv)
            except SystemExit as err:
                code = err.code
                if isinstance(code, str):
                    stderr.write(code)
                    code = 1
        return code, (json.loads(out.read_text(encoding="utf-8")) if out.exists() else None), stderr.getvalue()

    def test_a_passed_gate_is_reported_whole(self):
        code, gate, stderr = self.report()
        self.assertEqual(code, 0, stderr)
        self.assertTrue(gate["passed"] and gate["citable"])
        self.assertEqual(gate["smoke_shape_keys"]["count"], len(SHAPE_KEYS))
        self.assertEqual(gate["wakeloop"]["inputs_hash"], hashes())

    def test_a_refused_gate_writes_nothing_and_says_why(self):
        (self.own / "wakeloop2-abc-L-tsan.json").unlink()
        code, gate, stderr = self.report()
        self.assertEqual(code, 1)
        self.assertIsNone(gate)
        self.assertIn("tsan", stderr)

    def test_the_wrapper_has_no_inputs_compiler_or_coverage_option(self):
        for flag in ("--inputs", "--compiler", "--coverage"):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cr.main(["--build-dir", str(self.build), "--records", "r", "--other-records", "o", "--host", "L", flag, "x"])

    def test_accept_dry_run_marks_the_report_and_the_runner_refuses_it(self):
        for p in self.own.glob("*.json"):
            put(self.own, p.name, {**json.loads(p.read_text()), "dry_run": True, "host": "ctr"})
        put(self.theirs, "wakeloop2-abc-W-asan.json", record("W", "asan", dry_run=True, host="ctr"))
        self.assertEqual(self.report()[0], 1)
        code, gate, stderr = self.report("--accept-dry-run")
        self.assertEqual(code, 0, stderr)
        self.assertTrue(gate["dry_run"] and not gate["citable"])
        import run_campaign as rc
        result = {**gate, "smoke_keys": frozenset()}
        self.assertIn("not a passed, citable", " ".join(rc.result_problems(result, None, str(self.build))))


# ---------------------------------------------------------------------------------------------
# campaign_smoke: the shapes (logic only; no tool of the build is started here)
# ---------------------------------------------------------------------------------------------

def needs_h5(*systems):
    """Skip a test that needs H5's cells for a system: they come from the platform's own runner (bash, PowerShell)."""
    def decorate(test):
        def wrapper(self, *args, **kwargs):
            for system in systems:
                try:
                    cs.h5_entries(system)
                except cs.SmokeError as err:
                    self.skipTest(f"no H5 cells for {system} on this host: {err}")
            return test(self, *args, **kwargs)
        wrapper.__name__ = test.__name__
        wrapper.__doc__ = test.__doc__
        return wrapper
    return decorate


_USABLE = []


def usable_systems():
    """The systems whose shapes this host can compute: a Windows host has bash (Git for Windows) for Linux's H5 cells and
    PowerShell for its own; the container has bash only."""
    if not _USABLE:
        for system in cs.SYSTEMS:
            try:
                cs.h5_entries(system)
            except cs.SmokeError:
                continue
            _USABLE.append(system)
    return tuple(_USABLE)


def independent_plan_shape_count(system):
    """The number of distinct shapes of the campaign and pilot plans (not H5), counted from the frozen grid's constants
    and not from the plan's entries."""
    grid = cs.load_plan().grid
    n = 0
    for backend in grid.BACKENDS[system]:
        for variant in grid.VARIANTS:
            if not grid.has_site(backend, variant):
                continue
            n += len([w for w in grid.WORKER_COUNTS if w >= grid.min_workers(variant)])  # S, per W
            if variant != "wrong_worker":
                n += 1  # Bt, W = 1
        n += 1  # the phase sweep of the backend
        n += len(grid.H4_COMPARISONS) * 2  # H4: the comparisons, with task work and without
        n += 2  # the pilot, with and without
    return n


def c1_limit(tool, args):
    """The plan generator's C1 limit, written out again from its comment (not by calling it)."""
    def value(flag, default=None):
        return args[args.index(flag) + 1] if flag in args else default
    if tool == "abpair":
        return 2 * int(value("--pairs")) * (-(-int(value("--window-ms")) // 1000) + 10) + 120
    units = 1 + int(value("--calibrate", "11")) + int(value("--warmup", "5")) + int(value("--posts", "51"))
    return units * ((2 if value("--protocol", "single") == "batch" else 1) * 10 + 1) + 120


def plan_shapes(system):
    return [s for s in cs.shapes(system) if cs.key_family(s.key) != "H5"]


def h5_shapes(system):
    return [s for s in cs.shapes(system) if cs.key_family(s.key) == "H5"]


class DryLineTests(unittest.TestCase):
    """How a line of run_h5's dry run is cut into the program and its arguments: no interpreter needed."""

    def test_a_bash_line_with_escaped_spaces_and_redirections(self):
        line = ("7: /tmp/h5\\ x/build/bench/wakeprobe --design bound-sweep --label fixed --wait-us 0 --out "
                "/tmp/h5\\ x/out/bound-sweep-epoll-0us.jsonl --worker-cpus 14 --poster-cpu 2 > /tmp/a.out 2> /tmp/a.err")
        tokens = cs.split_dry_line(line, "linux")
        self.assertEqual(tokens[0], "/tmp/h5 x/build/bench/wakeprobe")
        self.assertEqual(tokens[-2:], ["--poster-cpu", "2"])
        self.assertNotIn(">", tokens)

    def test_a_powershell_line_with_quoted_paths(self):
        line = ('3: cmd.exe /c "C:\\Users\\a b\\build\\bench\\wakeprobe.exe" --design matrix --label fixed --wait-us 1000 '
                '--timer-period-ms 1 --out "C:\\Users\\a b\\out\\matrix-iocp.jsonl" --worker-cpus 14 > "C:\\o.out" 2> "C:\\o.err"')
        tokens = cs.split_dry_line(line, "windows")
        self.assertEqual(tokens[0], "C:\\Users\\a b\\build\\bench\\wakeprobe.exe")
        self.assertEqual(tokens[tokens.index("--out") + 1], "C:\\Users\\a b\\out\\matrix-iocp.jsonl")
        self.assertEqual(tokens[-2:], ["--worker-cpus", "14"])
        self.assertNotIn("cmd.exe", tokens)


class SmokeShapeTests(unittest.TestCase):
    def test_the_plan_shapes_of_each_system_are_the_grids(self):
        for system, want in (("linux", 119), ("windows", 74)):
            if system not in usable_systems():
                continue
            with self.subTest(system=system):
                self.assertEqual(len(plan_shapes(system)), want)
                self.assertEqual(independent_plan_shape_count(system), want)

    @needs_h5("linux", "windows")
    def test_the_shape_counts_with_h5(self):
        self.assertEqual([len(cs.shapes(s)) for s in ("linux", "windows")], [129, 82])
        self.assertEqual([len(h5_shapes(s)) for s in ("linux", "windows")], [10, 8])

    def test_the_shapes_are_distinct_and_their_ids_are_file_names(self):
        for system in usable_systems():
            shapes = cs.shapes(system)
            self.assertEqual(len({s.key for s in shapes}), len(shapes))
            self.assertEqual(len({s.id.lower() for s in shapes}), len(shapes))
            for s in shapes:
                self.assertRegex(s.id, r"^smoke-\d{3}-[A-Za-z0-9][A-Za-z0-9._-]*$")
                self.assertNotIn("\r", s.id)

    def test_every_shape_is_taken_from_a_generator_entry(self):
        for system in usable_systems():
            entries = {e["id"]: e for e in cs.canonical_entries(system)}
            for s in cs.shapes(system):
                self.assertIn(s.plan_id, entries)
                self.assertEqual(s.key, cs.shape_key(entries[s.plan_id]))

    def test_every_variant_backend_and_worker_count_of_the_plans_is_in_a_shape(self):
        for system in usable_systems():
            seen = {(s.key[1], s.key[2], s.key[3]) for s in cs.shapes(system) if s.tool == "wakeprobe"}
            for e in cs.canonical_entries(system):
                if e["tool"] == "wakeprobe":
                    a = e["args"]
                    self.assertIn((cs.flag(a, "--backend"), cs.flag(a, "--variant"), int(cs.flag(a, "--workers"))), seen)
            arms = {(s.key[1], s.key[2], s.key[3]) for s in cs.shapes(system) if s.tool == "abpair"}
            for e in cs.canonical_entries(system):
                if e["tool"] == "abpair":
                    a = e["args"]
                    self.assertIn((cs.flag(a, "--backend"), cs.flag(a, "--arm-a"), cs.flag(a, "--arm-b")), arms)

    def test_every_shape_key_of_every_plan_line_is_a_shape(self):
        """The runner refuses a plan line whose key is in no record: the keys of the whole plans are the smoke's."""
        for system in usable_systems():
            keys = {s.key for s in cs.shapes(system)}
            plan = cs.load_plan()
            r_by_cell = plan.r_for_system(system, lambda key: plan.grid.H4_R_MAX)
            lines = plan.campaign_entries(system, r_by_cell) + plan.pilot_plan_entries(system)
            missing = {cs.shape_key(e) for e in lines} - keys
            self.assertEqual(missing, set(), system)

    def test_each_family_has_its_shapes(self):
        families = {cs.key_family(s.key) for s in cs.shapes("linux")}
        self.assertTrue({"S", "Bt", "sweep", "H4", "pilot"} <= families)
        sweeps = [s for s in cs.shapes("linux") if s.tool == "wakeprobe" and cs.key_family(s.key) == "sweep"]
        self.assertEqual(len(sweeps), 3)
        for s in sweeps:
            self.assertIn("--phase-frac", s.args)
            self.assertEqual(s.key[8], "phase")
            self.assertTrue(int(cs.flag(s.args, "--calibrate")) > 0)

    def test_the_key_has_the_bound_class_the_gap_mode_and_the_timer_settings(self):
        """review H3: the key had no field for B = 0, the fixed gap, the timer slack or the timer period."""
        base = {"tool": "wakeprobe", "family": "H5", "args": ["--backend", "epoll", "--variant", "plain_wake", "--workers", "1",
                                                              "--protocol", "single", "--wait-us", "1000", "--gap-us", "2000",
                                                              "--calibrate", "11", "--timerslack-ns", "1"]}

        def with_args(**changes):
            args = list(base["args"])
            for name, value in changes.items():
                flag_name = "--" + name.replace("_", "-")
                if flag_name in args:
                    i = args.index(flag_name)
                    if value is None:
                        del args[i:i + 2]
                    else:
                        args[i + 1] = value
                elif value is not None:
                    args += [flag_name, value]
            return {**base, "args": args}
        reference = cs.shape_key(base)
        self.assertEqual(reference[7:], ("timed", "fixed", "1", None))
        self.assertEqual(cs.shape_key(with_args(wait_us="0"))[7], "blocking")
        self.assertEqual(cs.shape_key(with_args(wait_us="100000"))[7], "timed", "100 ms is a value, not a shape")
        self.assertNotEqual(cs.shape_key(with_args(timerslack_ns="0")), reference)
        self.assertNotEqual(cs.shape_key(with_args(timerslack_ns=None)), reference)
        self.assertEqual(cs.shape_key(with_args(gap_us=None, phase_frac="0.05"))[8], "phase")
        self.assertEqual(cs.shape_key(with_args(gap_us=None, gap_frac_range="0.1:3.0"))[8], "random")
        with_period = {**base, "args": base["args"] + ["--timer-period-ms", "1"]}
        self.assertEqual(cs.shape_key(with_period)[10], "1")
        self.assertNotEqual(cs.shape_key(with_period), reference)

    @needs_h5("linux")
    def test_review_h3_the_linux_h5_shapes_include_the_blocking_path_a_fixed_gap_and_both_slacks(self):
        keys = {s.key for s in h5_shapes("linux")}
        self.assertTrue(any(k[7] == "blocking" for k in keys), "B = 0, the blocking path")
        self.assertTrue(all(k[8] == "fixed" for k in keys), "H5 on Linux paces by a fixed gap")
        self.assertEqual({k[9] for k in keys}, {"0", "1"}, "timer slack 1 ns and the kernel's default")
        self.assertEqual({k[1] for k in keys}, {"epoll", "io_uring"})
        self.assertEqual({k[2] for k in keys}, {"plain_wake", "absent_wake", "unobserved_primitive"})
        self.assertTrue(all(k[10] is None for k in keys))
        self.assertFalse(any(k[8] == "fixed" for s in plan_shapes("linux") for k in [s.key] if s.tool == "wakeprobe"),
                         "none of the plans' shapes is paced by a fixed gap, so the H5 ones are new")

    @needs_h5("windows")
    def test_review_h3_the_windows_h5_shapes_include_the_blocking_path_the_phase_target_and_both_timer_periods(self):
        keys = {s.key for s in h5_shapes("windows")}
        self.assertTrue(any(k[7] == "blocking" for k in keys))
        self.assertEqual({k[8] for k in keys}, {"fixed", "phase"})
        self.assertEqual({k[10] for k in keys}, {"0", "1"}, "the system's timer and timeBeginPeriod(1)")
        self.assertEqual({k[1] for k in keys}, {"iocp"})
        self.assertTrue(all(k[9] is None for k in keys))

    @needs_h5("linux", "windows")
    def test_h5_lines_come_from_the_platforms_own_runner_and_are_cut_to_arguments(self):
        for system in usable_systems():
            entries = cs.h5_entries(system)
            self.assertEqual(len(entries), 140 if system == "linux" else 136)
            for e in entries:
                self.assertEqual(e["family"], "H5")
                self.assertNotIn("--out", e["args"])
                self.assertFalse([a for a in e["args"] if ">" in a or "\r" in a])
                self.assertRegex(e["id"], r"^[A-Za-z0-9_.-]+$")
            self.assertEqual([e["id"] for e in entries], sorted(e["id"] for e in entries))

    def test_the_reduction_changes_only_the_counts_and_keeps_the_pins_and_the_calibration(self):
        counts = {"--posts", "--warmup", "--pairs", "--window-ms"}
        for system in usable_systems():
            entries = {e["id"]: e for e in cs.canonical_entries(system)}
            for s in cs.shapes(system):
                plan_args = entries[s.plan_id]["args"]
                self.assertEqual(len(plan_args), len(s.args))
                for i, (old, new) in enumerate(zip(plan_args, s.args, strict=True)):
                    if old != new:
                        self.assertIn(plan_args[i - 1], counts, f"{s.id}: {old} became {new}")
                        self.assertLess(int(new), int(old))
                for pin in ("--worker-cpus", "--poster-cpu", "--producer-cpus", "--calibrate", "--phase-frac", "--wait-us",
                            "--workers", "--gap-seed", "--gap-frac-range", "--gap-us", "--gap-unit", "--arm-a", "--arm-b",
                            "--work-ns", "--timerslack-ns", "--timer-period-ms"):
                    self.assertEqual(cs.flag(s.args, pin), cs.flag(plan_args, pin), pin)

    def test_the_reduced_counts(self):
        for s in cs.shapes("linux"):
            if s.tool == "abpair":
                self.assertEqual((cs.flag(s.args, "--pairs"), cs.flag(s.args, "--window-ms")), ("1", str(cs.SMOKE_WINDOW_MS)))
            else:
                workers = int(cs.flag(s.args, "--workers"))
                self.assertEqual(int(cs.flag(s.args, "--posts")), cs.SMOKE_POSTS)
                self.assertEqual(int(cs.flag(s.args, "--warmup")), max(cs.SMOKE_WARMUP, workers))

    def test_a_count_the_plan_already_has_lower_is_kept(self):
        self.assertEqual(cs.reduce_args("wakeprobe", ["--workers", "1", "--posts", "3", "--warmup", "1"]),
                         ["--workers", "1", "--posts", "3", "--warmup", "1"])
        self.assertEqual(cs.reduce_args("abpair", ["--pairs", "1", "--window-ms", "50"]), ["--pairs", "1", "--window-ms", "50"])
        with self.assertRaises(cs.SmokeError):
            cs.reduce_args("abpair", ["--pairs", "2"])

    def test_the_limit_is_the_c1_formula_on_the_reduced_counts(self):
        for system in usable_systems():
            for s in cs.shapes(system):
                self.assertEqual(s.limit_s, c1_limit(s.tool, list(s.args)), s.id)
        by_tool = {s.tool: s for s in cs.shapes("linux")}
        self.assertEqual(by_tool["abpair"].limit_s, 2 * 1 * (1 + 10) + 120)

    def test_the_smoke_has_no_copy_of_the_tools_argument_rules(self):
        """The tools' own --validate-args decides what is valid; a Python copy of their rules drifted (review check 4)."""
        for name in ("wakeprobe_problems", "abpair_problems", "check_args", "MAX_WORKERS"):
            self.assertFalse(hasattr(cs, name), name)

    def test_the_plan_generator_is_imported_not_copied(self):
        plan = cs.load_plan()
        self.assertTrue(plan.__file__.endswith(os.path.join("analysis", "v2", "plan.py")))
        self.assertIs(cs.load_plan(), plan)

    def test_an_unknown_system_is_refused(self):
        with self.assertRaises(cs.SmokeError):
            cs.shapes("plan9")

    def test_review_h3_a_grid_without_w4_does_not_make_the_old_shapes_enough(self):
        """The record's smoke keys are the shapes it ran; the runner refuses a plan line whose key is not among them."""
        full = {s.key for s in cs.shapes("linux")}
        fewer = {k for k in full if not (k[0] == "wakeprobe" and k[3] == 4)}
        plan_w4 = [e for e in cs.canonical_entries("linux") if e["tool"] == "wakeprobe" and cs.flag(e["args"], "--workers") == "4"]
        self.assertTrue(plan_w4)
        self.assertTrue(all(cs.shape_key(e) in full for e in plan_w4))
        self.assertFalse(any(cs.shape_key(e) in fewer for e in plan_w4))


# ---------------------------------------------------------------------------------------------
# campaign_smoke: running, with stub programs in place of wakeprobe and abpair
# ---------------------------------------------------------------------------------------------

STUB = r"""
import json, os, sys, time
path, argv = sys.argv[1], sys.argv[2:]
out = argv[argv.index("--out") + 1]
stem = os.path.basename(out)
stem = stem[:-len(".validate.jsonl")] if stem.endswith(".validate.jsonl") else stem[:-len(".jsonl")]
plan = json.load(open(os.environ["STUB_PLAN"], encoding="utf-8"))
spec = dict(plan.get("default", {}))
spec.update(plan.get(stem, {}))
validating = "--validate-args" in argv
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"tool": os.path.basename(path), "argv": argv, "validate": validating,
                             "out_dir_exists": os.path.isdir(os.path.dirname(out))}) + "\n")
if validating:
    spec = spec.get("validate", {})
    if spec.get("creates_out"):
        open(out, "w").write("x")
    if spec.get("stderr"):
        print(spec["stderr"], file=sys.stderr)
    if not spec.get("exit"):
        print("valid")
    sys.exit(spec.get("exit", 0))
with open(out, "w", encoding="utf-8") as handle:
    handle.write('{"elapsed_ns": 987654321, "delay_ns": 123454321}\n')
print('{"summary": true, "elapsed_ns": 987654321}')
if spec.get("stdout"):
    print(spec["stdout"])
if spec.get("stderr"):
    print(spec["stderr"], file=sys.stderr)
if spec.get("sleep"):
    time.sleep(spec["sleep"])
sys.exit(spec.get("exit", 0))
"""
TIMING = ("987654321", "123454321")


class SmokeRunTests(TempCase):
    def setUp(self):
        super().setUp()
        self.stub = self.root / "stub.py"
        self.stub.write_text(STUB, encoding="utf-8")
        self.plan = self.root / "stub-plan.json"
        self.log = self.root / "stub-log.jsonl"
        self.build = self.root / "build dir"
        (self.build / "bench").mkdir(parents=True)
        for tool in cs.TOOLS:
            cs.tool_path(self.build, tool).write_bytes(b"not a program")
        self.out = self.root / "out" / "smoke.json"
        self.set_stub({})
        every = cs.shapes("linux")
        pick = lambda family, tool: next(s for s in every if s.tool == tool and cs.key_family(s.key) == family)  # noqa: E731
        # wakeprobe S, Bt, sweep; abpair H4, H4 (plain wake), pilot
        self.shapes = (pick("S", "wakeprobe"), pick("Bt", "wakeprobe"), pick("sweep", "wakeprobe"), pick("H4", "abpair"),
                       [s for s in every if s.tool == "abpair" and cs.key_family(s.key) == "H4"][1], pick("pilot", "abpair"))
        for patch in (mock.patch.object(cs, "launch_prefix", lambda path: [sys.executable, str(self.stub), path]),
                      mock.patch.object(cs, "shapes", lambda system: self.shapes),
                      mock.patch.object(cs, "host_problems", lambda system: []),
                      mock.patch.dict(os.environ, {"STUB_PLAN": str(self.plan), "STUB_LOG": str(self.log), "LAB_JOB": "job-1"})):
            patch.start()
            self.addCleanup(patch.stop)

    def set_stub(self, plan):
        self.plan.write_text(json.dumps(plan), encoding="utf-8")

    def smoke(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["--build-dir", str(self.build), "--out", str(self.out), "--system", "linux", *extra]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cs.main(argv)
        data = json.loads(self.out.read_text(encoding="utf-8")) if self.out.exists() else None
        return code, data, stdout.getvalue(), stderr.getvalue()

    def calls(self, validate=None):
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return rows if validate is None else [r for r in rows if r["validate"] == validate]

    def test_every_line_is_validated_by_the_tool_then_every_shape_runs_once_in_order(self):
        code, data, stdout, _ = self.smoke()
        self.assertEqual(code, 0, stdout)
        self.assertEqual((data["mode"], data["planned"], data["ran"], data["failed"], data["reports"]), ("run", 6, 6, 0, 0))
        rows = self.calls()
        self.assertEqual([r["validate"] for r in rows], [True] * 6 + [False] * 6, "all validated before the first run")
        for call, shape in zip(self.calls(validate=False), self.shapes, strict=True):
            self.assertEqual(call["tool"].split(".")[0], shape.tool)
            self.assertEqual(call["argv"][:len(shape.args)], list(shape.args))
            self.assertEqual(call["argv"][len(shape.args)], "--out")
        for call, shape in zip(self.calls(validate=True), self.shapes, strict=True):
            self.assertEqual(call["argv"][0], "--validate-args")
            self.assertEqual(call["argv"][1:1 + len(shape.args)], list(shape.args))
            self.assertEqual(call["argv"][1 + len(shape.args)], "--out", "the line is given whole, --out included")
        self.assertEqual([r["id"] for r in data["runs"]], [s.id for s in self.shapes])
        self.assertEqual(sum(1 for line in stdout.splitlines() if line.startswith("ok ")), 6)

    def test_a_line_the_tool_refuses_stops_everything_before_any_run(self):
        self.set_stub({self.shapes[2].id: {"validate": {"exit": 2, "stderr": "wakeprobe: --phase-frac needs a measured B_eff"}}})
        code, data, stdout, _ = self.smoke()
        self.assertEqual(code, 1)
        self.assertEqual(self.calls(validate=False), [], "nothing ran")
        self.assertEqual((data["ran"], data["failed"]), (0, 1))
        self.assertEqual(data["validate_failures"][0]["id"], self.shapes[2].id)
        self.assertEqual(data["validate_failures"][0]["exit"], 2)
        self.assertIn("needs a measured B_eff", data["validate_failures"][0]["message"])
        self.assertIn(f"INVALID {self.shapes[2].id} exit=2", stdout)

    def test_a_tool_that_creates_the_out_file_under_validate_args_breaks_its_contract(self):
        self.set_stub({self.shapes[0].id: {"validate": {"creates_out": True}}})
        code, data, _, _ = self.smoke()
        self.assertEqual(code, 1)
        self.assertIn("created the --out file", data["validate_failures"][0]["message"])
        self.assertEqual(list((self.out.parent / "smoke-scratch").glob("*")) if (self.out.parent / "smoke-scratch").exists() else [], [])

    def test_a_tool_that_does_not_say_valid_is_not_valid(self):
        self.set_stub({self.shapes[1].id: {"validate": {"exit": 0, "stderr": "hm"}}})
        code, data, _, _ = self.smoke()
        self.assertEqual(code, 0, "exit 0 and `valid` is the whole contract")
        stub = STUB.replace('print("valid")', 'print("fine")')
        self.stub.write_text(stub, encoding="utf-8")
        code, data, _, _ = self.smoke()
        self.assertEqual(code, 1)
        self.assertEqual(len(data["validate_failures"]), 6)

    def test_no_measurement_and_no_duration_is_read_or_kept(self):
        code, data, stdout, stderr = self.smoke()
        self.assertEqual(code, 0)
        text = self.out.read_text(encoding="utf-8")
        for number in TIMING:
            self.assertNotIn(number, text + stdout + stderr)

        def keys(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield k
                    yield from keys(v)
            elif isinstance(node, list):
                for v in node:
                    yield from keys(v)
        names = {k for k in keys({**data, "shapes": []})}
        self.assertEqual(names & {"seconds", "elapsed_ns", "elapsed", "duration", "time", "started", "date", "wall"}, set())
        self.assertEqual(set(data["runs"][0]), {"id", "exit", "timed_out", "reports"})

    def test_the_tools_output_files_are_deleted_and_the_scratch_folder_goes(self):
        self.smoke()
        self.assertFalse((self.out.parent / "smoke-scratch").exists())
        self.assertEqual(sorted(p.name for p in self.out.parent.iterdir()), ["smoke.json"])
        scratch = self.root / "scratch"
        self.smoke("--scratch", str(scratch))
        self.assertFalse(scratch.exists())

    def test_a_run_that_exits_non_zero_is_red_and_the_rest_still_run(self):
        self.set_stub({self.shapes[1].id: {"exit": 3, "stderr": "wakeprobe: refused something\nsecond line"}})
        code, data, stdout, _ = self.smoke()
        self.assertEqual(code, 1)
        self.assertEqual((data["ran"], data["failed"]), (6, 1))
        bad = data["runs"][1]
        self.assertEqual((bad["exit"], bad["reports"]), (3, 0))
        self.assertEqual(bad["stderr_head"], ["wakeprobe: refused something", "second line"])
        self.assertIn(f"FAIL {self.shapes[1].id} exit=3 reports=0", stdout)
        self.assertEqual(len(self.calls(validate=False)), 6)
        self.assertNotIn("stderr_head", data["runs"][0])

    def test_a_planted_report_is_red_even_when_the_run_exits_zero(self):
        lines = ("==4711==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x6020",
                 "WARNING: ThreadSanitizer: data race (pid=4711)", "==4711==WARNING: MemorySanitizer: use-of-uninitialized-value",
                 "src/a.cpp:12:5: runtime error: signed integer overflow", "SUMMARY: UndefinedBehaviorSanitizer: undefined-behavior x",
                 "FATAL: ThreadSanitizer: unexpected memory mapping 0x7f00-0x7f10", "==1==ThreadSanitizer: CHECK failed: tsan_rtl.cpp:1 (0 vs 1)")
        for where in ("stderr", "stdout"):
            for line in lines:
                with self.subTest(where=where, line=line):
                    self.set_stub({self.shapes[2].id: {where: "before\n" + line + "\nafter"}})
                    code, data, stdout, _ = self.smoke()
                    self.assertEqual(code, 1)
                    self.assertEqual((data["runs"][2]["exit"], data["runs"][2]["reports"]), (0, 1))
                    self.assertEqual(data["failed"], 1)
                    self.assertEqual(data["reports"], 1)
                    self.assertIn(line, data["runs"][2]["report_blocks"][0])
                    self.assertIn(line, stdout, "the report reaches smoke.log, which the record scans too")

    def test_a_clean_message_that_mentions_sanitizers_is_not_a_report(self):
        self.set_stub({"default": {"stderr": "note: 100% tests passed\n-- Sanitizer: memory"}})
        code, data, _, _ = self.smoke()
        self.assertEqual((code, data["reports"]), (0, 0))

    def test_the_time_limit_kills_a_run_and_it_is_red(self):
        shape = cs.Shape(id="smoke-900-limit", tool="wakeprobe", key=("k",), args=self.shapes[0].args, limit_s=1, plan_id="x")
        self.set_stub({"smoke-900-limit": {"sleep": 30}})
        scratch = self.root / "scratch"
        scratch.mkdir()
        result = cs.run_shape(shape, str(cs.tool_path(self.build, "wakeprobe")), scratch)
        self.assertEqual((result["exit"], result["timed_out"]), (cs.TIMEOUT, True))
        self.assertEqual(list(scratch.iterdir()), [], "the killed run's output file is deleted too")
        self.assertIn("stderr_head", result)

    def test_a_timed_out_run_makes_the_smoke_red(self):
        slow = cs.Shape(id="smoke-901-slow", tool="abpair", key=("k2",), args=self.shapes[3].args, limit_s=1, plan_id="y")
        self.set_stub({"smoke-901-slow": {"sleep": 30}})
        with mock.patch.object(cs, "shapes", lambda system: (self.shapes[0], slow)):
            code, data, _, _ = self.smoke()
        self.assertEqual(code, 1)
        self.assertEqual([r["exit"] for r in data["runs"]], [0, cs.TIMEOUT])
        self.assertTrue(data["runs"][1]["timed_out"])

    def test_a_tool_that_cannot_start_is_red_already_at_its_validation(self):
        with mock.patch.object(cs, "launch_prefix", lambda path: [str(self.root / "no such tool")]):
            code, data, _, _ = self.smoke()
        self.assertEqual(code, 1)
        self.assertEqual({f["exit"] for f in data["validate_failures"]}, {127})

    def test_a_missing_tool_refuses_before_anything_runs(self):
        cs.tool_path(self.build, "abpair").unlink()
        code, data, _, stderr = self.smoke()
        self.assertEqual(code, 2)
        self.assertIn("abpair", stderr)
        self.assertIsNone(data)
        self.assertEqual(self.calls(), [])

    def test_a_build_directory_is_required_unless_listing(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(cs.main(["--out", str(self.out), "--system", "linux"]), 2)
        self.assertIn("--build-dir", stderr.getvalue())

    def test_a_real_run_needs_lab_job(self):
        """review LOW: the smoke started tools without a lab job."""
        with mock.patch.dict(os.environ):
            os.environ.pop("LAB_JOB", None)
            code, data, _, stderr = self.smoke()
        self.assertEqual(code, 2)
        self.assertIn("LAB_JOB", stderr)
        self.assertEqual(self.calls(), [])

    def test_a_real_run_needs_the_laptops_host_name_and_platform(self):
        """review LOW: the desktop could run the smoke."""
        with mock.patch.object(cs, "host_problems", cs.__dict__["host_problems"].__wrapped__ if hasattr(
                cs.host_problems, "__wrapped__") else self.real_host_problems):
            code, data, _, stderr = self.smoke()
        self.assertEqual(code, 2)
        self.assertIn("desktop", stderr)
        self.assertEqual(self.calls(), [])

    @staticmethod
    def real_host_problems(system):
        with mock.patch.object(cs.platform, "node", return_value="ALEX-PC"), \
                mock.patch.object(cs.sys, "platform", "linux" if system == "linux" else "win32"):
            return REAL_HOST_PROBLEMS(system)

    def test_the_host_problems_of_each_case(self):
        with mock.patch.object(cs.sys, "platform", "linux"):
            self.assertIn("not windows", " ".join(REAL_HOST_PROBLEMS("windows")))
            with mock.patch.object(cs.platform, "node", return_value="alex-laptop"):
                self.assertEqual(REAL_HOST_PROBLEMS("linux"), [])
            with mock.patch.object(cs.platform, "node", return_value="ALEX-PC"):
                self.assertIn("never run on the desktop", " ".join(REAL_HOST_PROBLEMS("linux")))
        with mock.patch.object(cs.sys, "platform", "linux"), mock.patch.object(cs.platform, "node", return_value="ALEX-LAPTOP"):
            self.assertEqual(REAL_HOST_PROBLEMS("linux"), [], "host names are compared without case")
        with mock.patch.object(cs.sys, "platform", "win32"), mock.patch.dict(os.environ, {"COMPUTERNAME": "DESKTOP-A1RHS5R"}):
            self.assertEqual(REAL_HOST_PROBLEMS("windows"), [])
        # N8: on Windows the name is COMPUTERNAME, as the drivers and the lab have it, and its case does not matter
        with mock.patch.object(cs.sys, "platform", "win32"), mock.patch.object(cs.platform, "node", return_value="Alex-Pc"), \
                mock.patch.dict(os.environ, {"COMPUTERNAME": "desktop-a1rhs5r"}):
            self.assertEqual(cs.host_name(), "desktop-a1rhs5r")
            self.assertEqual(REAL_HOST_PROBLEMS("windows"), [])
        with mock.patch.object(cs.sys, "platform", "win32"), mock.patch.object(cs.platform, "node", return_value="DESKTOP-A1RHS5R"), \
                mock.patch.dict(os.environ, {"COMPUTERNAME": "ALEX-PC"}):
            self.assertIn("never run on the desktop", " ".join(REAL_HOST_PROBLEMS("windows")))

    def test_listing_runs_nothing_needs_no_build_and_no_lab_job(self):
        stdout = io.StringIO()
        with mock.patch.dict(os.environ), contextlib.redirect_stdout(stdout):
            os.environ.pop("LAB_JOB", None)
            code = cs.main(["--out", str(self.out), "--system", "linux", "--list"])
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual((code, data["mode"], data["ran"], data["planned"], data["runs"]), (0, "list", 0, 6, []))
        self.assertEqual(self.calls(), [])
        self.assertEqual(sum(1 for line in stdout.getvalue().splitlines() if line.startswith("list ")), 6)

    def test_the_limits_total_is_recorded_as_a_bound(self):
        _, data, _, _ = self.smoke()
        self.assertEqual(data["limit_s_total"], sum(s.limit_s for s in self.shapes))


REAL_HOST_PROBLEMS = cs.host_problems


class SmokeFullListTests(TempCase):
    """The whole list of the host's system through the stubs, once: the exit codes and the order only."""

    def test_the_whole_list_runs_green_through_stubs(self):
        system = cs.default_system()
        stub = self.root / "stub.py"
        stub.write_text(STUB, encoding="utf-8")
        plan, log = self.root / "plan.json", self.root / "log.jsonl"
        plan.write_text("{}", encoding="utf-8")
        build = self.root / "build"
        (build / "bench").mkdir(parents=True)
        for tool in cs.TOOLS:
            cs.tool_path(build, tool).write_bytes(b"x")
        out = self.root / "smoke.json"
        try:
            planned = cs.shapes(system)
        except cs.SmokeError as err:
            self.skipTest(str(err))
        with mock.patch.object(cs, "launch_prefix", lambda path: [sys.executable, str(stub), path]), \
                mock.patch.object(cs, "host_problems", lambda s: []), \
                mock.patch.dict(os.environ, {"STUB_PLAN": str(plan), "STUB_LOG": str(log), "LAB_JOB": "j"}), \
                contextlib.redirect_stdout(io.StringIO()):
            code = cs.main(["--build-dir", str(build), "--out", str(out), "--system", system])
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual((code, data["planned"], data["ran"], data["failed"]), (0, len(planned), len(planned), 0))
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 2 * len(planned))
        self.assertEqual([r["id"] for r in data["runs"]], [s.id for s in planned])

    def test_the_other_systems_list_is_listed_when_the_host_can_make_it(self):
        other = "windows" if cs.default_system() == "linux" else "linux"
        out = self.root / "smoke.json"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = cs.main(["--out", str(out), "--system", other, "--list"])
        except cs.SmokeError as err:
            self.skipTest(str(err))
        if code == 2:
            self.skipTest("this host cannot make the other system's H5 cells")
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["planned"], len(cs.shapes(other)))


def find_build_with_validate_args():
    """A build of this repository whose tools know --validate-args, or None. WAKELOOP_VALIDATE_BUILD names one; else the
    newest wakeprobe of the build-* folders of the repository (and /work/build-d5-rel) that has the flag."""
    wanted = os.environ.get("WAKELOOP_VALIDATE_BUILD")
    candidates = [Path(wanted)] if wanted else [*sorted(bi.REPO.glob("build*")), Path("/work/build-d5-rel")]
    probe = cs.shapes(cs.default_system())[0]
    found = []
    for build in candidates:
        path = cs.tool_path(build, "wakeprobe")
        if not path.is_file():
            continue
        try:
            done = subprocess.run([str(path), "--validate-args", *probe.args, "--out", str(build / "validate-probe.jsonl")],
                                  capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if "unknown flag" not in done.stderr and done.returncode in (0, 2):
            found.append((path.stat().st_mtime, build))
    return max(found)[1] if found else None


class ValidateArgsRealBuildTests(unittest.TestCase):
    """Every smoke line and every stand-in plan line, for the host's system, through the --validate-args of a real build
    of the measurement programs: exit 0 and `valid`. This starts the programs, but --validate-args creates no loop, no
    thread, no pin, no clock reading and no --out file, so nothing is timed. Skipped without a build that has the flag."""

    def test_every_line_of_the_smoke_and_of_the_plans_is_valid(self):
        system = cs.default_system()
        try:
            build = find_build_with_validate_args()
            lines = [(s.id, s.tool, list(s.args)) for s in cs.shapes(system)]
            full = [(e["id"], e["tool"], e["args"]) for e in cs.canonical_entries(system)]
        except cs.SmokeError as err:
            self.skipTest(str(err))
        if build is None:
            self.skipTest("no build of this repository has --validate-args in wakeprobe and abpair yet "
                          "(set WAKELOOP_VALIDATE_BUILD to a Release build that has it)")
        from concurrent.futures import ThreadPoolExecutor
        scratch = Path(tempfile.mkdtemp(prefix="validate "))
        self.addCleanup(shutil.rmtree, scratch, True)

        def check(item):
            name, tool, args = item
            out = scratch / (re.sub(r"[^A-Za-z0-9._-]", "_", name) + ".jsonl")
            done = subprocess.run([str(cs.tool_path(build, tool)), "--validate-args", *args, "--out", str(out)],
                                  capture_output=True, text=True, timeout=60)
            ok = done.returncode == 0 and "valid" in done.stdout.split() and not out.exists()
            return None if ok else f"{name} ({tool}) exit {done.returncode}: {(done.stderr or done.stdout).strip()[:200]}"
        with ThreadPoolExecutor(max_workers=8) as pool:
            bad = [r for r in pool.map(check, lines + full) if r]
        self.assertEqual(bad[:5], [], f"{len(bad)} of {len(lines) + len(full)} lines were refused by {build}")

    def test_review_wait_us_above_the_loops_limit_is_refused_and_the_limit_itself_is_not(self):
        """LOW: --validate-args said valid for --wait-us 86400000001, which a run refuses when it creates the loop (exit 3)."""
        build = find_build_with_validate_args()
        if build is None:
            self.skipTest("no build of this repository has --validate-args (set WAKELOOP_VALIDATE_BUILD)")
        probe = next(s for s in cs.shapes(cs.default_system()) if s.tool == "wakeprobe" and cs.flag(s.args, "--wait-us") == "1000")
        scratch = Path(tempfile.mkdtemp(prefix="validate "))
        self.addCleanup(shutil.rmtree, scratch, True)

        def run(wait_us):
            args = list(probe.args)
            args[args.index("--wait-us") + 1] = str(wait_us)
            out = scratch / "x.jsonl"
            done = subprocess.run([str(cs.tool_path(build, "wakeprobe")), "--validate-args", *args, "--out", str(out)],
                                  capture_output=True, text=True, timeout=60)
            self.assertFalse(out.exists())
            return done

        at = run(86_400_000_000)
        self.assertEqual((at.returncode, at.stdout.strip()), (0, "valid"), at.stderr)
        above = run(86_400_000_001)
        self.assertEqual(above.returncode, 2)
        self.assertIn("--wait-us must be from 0 to 86400000000", above.stderr)
        self.assertEqual(above.stdout, "")


# ---------------------------------------------------------------------------------------------
# wakeloop_record
# ---------------------------------------------------------------------------------------------

def all_test_names(backends):
    """A plausible CTest registration: every required test of every backend, plus the shared ones."""
    names = []
    for backend in backends:
        for need in wr.required_tests(backend):
            names.append(need + "w1.post_runs_once" if need.endswith(".") else need)
    return sorted(set(names + ["defect_sites", "run_h5_dry_run", "run_campaign_dry_run", "run_campaign_unittest"]))


def junit_xml(names, bad=()):
    cases = "".join(
        f'<testcase name="{n}" classname="{n}" time="0.1" status="{"fail" if n in bad else "run"}">'
        f'{"<failure message=\'Failed\'/>" if n in bad else ""}<system-out>out</system-out></testcase>' for n in names)
    return f'<?xml version="1.0" encoding="UTF-8"?><testsuite name="Linux" tests="{len(names)}">{cases}</testsuite>'


def smoke_data(system="linux", mode="run", **changes):
    """A smoke.json as bench/campaign_smoke.py writes it for the generators' shapes of a system."""
    planned = cs.shapes(system)
    runs = [] if mode == "list" else [{"id": s.id, "exit": 0, "timed_out": False, "reports": 0} for s in planned]
    data = cs.summary(system, mode, planned, runs, [])
    data.update(changes)
    return data


def ctest_n(names):
    lines = [f"  Test  #{i}: {n}" for i, n in enumerate(names, 1)]
    return "Test project /x/build\n" + "\n".join(lines) + f"\n\nTotal Tests: {len(names)}\n"


COMMIT = "c" * 40


class RecordWriterTests(TempCase):
    def setUp(self):
        super().setUp()
        self.work = self.root / "work"
        self.work.mkdir()
        self.record = self.root / "out" / "rec.json"
        self.names = all_test_names(LINUX_BACKENDS)
        self.end_fingerprint = FINGERPRINT
        self.head = COMMIT
        self.dirty = []
        for patch in (mock.patch.object(bi, "code_fingerprint", lambda *a: self.end_fingerprint),
                      mock.patch.object(bi, "code_commit", return_value="d" * 40),
                      mock.patch.object(bi, "generator_hashes", return_value=GENERATORS),
                      mock.patch.object(bi, "repo_dirty", lambda *a: self.dirty),
                      mock.patch.object(bi, "git", lambda *a: subprocess.CompletedProcess([], 0, self.head + "\n", ""))):
            patch.start()
            self.addCleanup(patch.stop)

    def inputs_info(self, tag="L", **changes):
        info = {"targets": {t: {"inputs_hash": h, "files": 3, "tus": 1} for t, h in hashes().items()},
                "config": config(tag), "third_party": {"fetched": {}, "found": {}}, "compiler": COMPILERS[tag],
                "compiler_cmake": "Clang 22.1.8", "host": tag, "file_hashes": {"wakeloop": {"wakeloop/loop/src/common.cpp": "0" * 64}},
                "cxx_compiler": "/usr/bin/clang++"}
        info.update(changes)
        return info

    def make_work(self, names=None, bad=(), registered=None, build_log=None, ctest_log="all tests passed\n", config_=None,
                  with_inputs=True, smoke="ok", smoke_log="ok\n", info=None, tag="L"):
        """smoke: "ok" (every shape of the system ran green), None (no smoke.json) or the dict to write."""
        names = self.names if names is None else names
        if smoke is not None:
            system = "linux" if tag == "L" else "windows"
            (self.work / "smoke.json").write_text(json.dumps(smoke_data(system) if smoke == "ok" else smoke), encoding="utf-8")
        (self.work / "smoke.log").write_text(smoke_log, encoding="utf-8")
        (self.work / "build.log").write_text(build_log or "-- configured\nok\n", encoding="utf-8")
        (self.work / "ctest.log").write_text(ctest_log, encoding="utf-8")
        (self.work / "ctest.xml").write_text(junit_xml(names, bad), encoding="utf-8")
        (self.work / "tests.txt").write_text(ctest_n(names if registered is None else registered), encoding="utf-8")
        if with_inputs:
            info = info or self.inputs_info(tag, **({"config": config_} if config_ else {}))
            (self.work / "build.inputs.json").write_text(json.dumps({"builds": {bi.BUILD: info}}), encoding="utf-8")

    def write(self, *extra, build_exit=0, inputs_exit=0, ctest_exit=0, smoke_exit=0, tag="L", archive="/home/alex/lab/records-logs/x.tar.gz",
              sha="ab" * 32, fingerprint=FINGERPRINT):
        argv = ["--work", str(self.work), "--record", str(self.record), "--repo", "r", "--commit", COMMIT, "--repo-head", COMMIT,
                "--fingerprint-start", fingerprint, "--sanitizer", "asan", "--cmake-args=-G Ninja",
                "--options=ASAN_OPTIONS=a UBSAN_OPTIONS=b LSAN_OPTIONS= TSAN_OPTIONS= MSAN_OPTIONS=", "--host", "alex-laptop",
                "--host-tag", tag, "--build-exit", str(build_exit), "--inputs-exit", str(inputs_exit), "--ctest-exit", str(ctest_exit),
                "--smoke-exit", str(smoke_exit), "--seconds", "7", "--logs-archive", archive, "--logs-sha256", sha, *extra]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = wr.main(argv)
        return code, (json.loads(self.record.read_text(encoding="utf-8")) if self.record.exists() else None)

    def test_a_clean_run_is_green_and_the_record_is_whole(self):
        self.make_work()
        code, rec = self.write()
        self.assertEqual(code, 0, rec["presets"])
        self.assertTrue(rec["green"])
        self.assertEqual((rec["tests_total"], rec["tests_failed"], rec["sanitizer_reports"]), (len(self.names), 0, 0))
        self.assertEqual(rec["inputs_hash"], hashes())
        self.assertEqual(rec["config"], config())
        self.assertEqual(rec["compiler"], "Clang 22.1.8")
        self.assertEqual(rec["code_fingerprint"], FINGERPRINT)
        self.assertEqual(rec["generators"], GENERATORS)
        self.assertEqual(rec["code_commit"], "d" * 40)
        self.assertEqual(rec["tool"], "wakeloop")
        self.assertFalse(rec["dry_run"])
        self.assertEqual(rec["ctest_jobs"], 1)
        self.assertEqual((rec["logs_archive"], rec["logs_archive_sha256"]), ("/home/alex/lab/records-logs/x.tar.gz", "ab" * 32))
        self.assertEqual(rec["sanitizer_options"], "ASAN_OPTIONS=a UBSAN_OPTIONS=b LSAN_OPTIONS= TSAN_OPTIONS= MSAN_OPTIONS=")
        self.assertEqual((rec["smoke_exit"], rec["inputs_exit"]), (0, 0))
        self.assertEqual(rec["smoke"]["mode"], "run")
        n = len(cs.shapes("linux"))
        self.assertEqual((rec["smoke"]["planned"], rec["smoke"]["ran"], rec["smoke"]["failed"], rec["smoke"]["reports"]), (n, n, 0, 0))
        self.assertEqual(rec["smoke"]["shape_keys"], [list(s.key) for s in cs.shapes("linux")])
        self.assertEqual([p["preset"] for p in rec["presets"]], ["build", "ctest", "smoke", "code", "inputs", "logs"])
        for backend in LINUX_BACKENDS:
            counts = rec["by_backend"][backend]
            self.assertTrue(counts["matrix"] and counts["wakeprobe_check"] and counts["abpair_check"], counts)

    def test_the_record_a_clean_run_makes_is_one_the_gate_takes(self):
        """The writer's output and the gate's input agree, field for field."""
        self.make_work()
        archive, sha = make_archive(self.root / "records-logs", "wakeloop2-abc-L-asan")
        code, rec = self.write(archive=archive, sha=sha)
        self.assertEqual(code, 0)
        self.assertEqual(rec["format"], bi.RECORD_FORMAT)
        records = self.root / "records"
        records.mkdir()
        rec.update(host=gl.LAB_HOSTS["L"][0])
        for san in gl.SANITIZERS_BY_HOST["L"]:
            put(records, f"wakeloop2-abc-L-{san}.json", {**rec, "sanitizer": san})
        got = gl.target_coverage(records, hashes(), config(), "Clang 22.1.8", GENERATORS, host="L")
        self.assertEqual(sorted(got), ["asan", "msan", "tsan"])
        self.assertEqual(got["asan"][0]["keys"], frozenset(s.key for s in cs.shapes("linux")))

    # -- the reasons a record is red, and whether they block (N2, N3) ------------------------------------------------

    def classes(self, rec):
        return sorted({r["class"] for r in rec["red_reasons"]})

    def test_a_green_record_has_its_format_and_no_reasons(self):
        self.make_work()
        code, rec = self.write()
        self.assertEqual((code, rec["format"], rec["red_reasons"], rec["blocking"]), (0, bi.RECORD_FORMAT, [], False))

    def test_the_findings_block(self):
        """A sanitizer report, a failed or timed-out test, a failed or timed-out smoke run."""
        cases = {}
        cases["report"] = dict(ctest_log="ok\n==4711==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x6020\n")
        cases["test"] = dict(bad=["epoll.none.w1.post_runs_once"], ctest_exit=8)
        for what, change in (("failed", {"exit": 3}), ("timeout", {"exit": "timeout", "timed_out": True}), ("reported", {"reports": 1})):
            data = smoke_data()
            data["runs"][5] = {**data["runs"][5], **change}
            cases["smoke-run " + what] = dict(smoke=data, smoke_exit=1)
        for label, spec in cases.items():
            with self.subTest(label):
                self.record.unlink(missing_ok=True)
                ctest_exit, smoke_exit = spec.pop("ctest_exit", 0), spec.pop("smoke_exit", 0)
                self.make_work(**spec)
                code, rec = self.write(ctest_exit=ctest_exit, smoke_exit=smoke_exit)
                self.assertEqual(code, 1)
                self.assertTrue(rec["blocking"], rec["red_reasons"])
                self.assertIn(label.split(" ")[0], self.classes(rec))
                self.assertTrue(gl.is_finding(rec))

    def test_infrastructure_reasons_never_block(self):
        """A failed log pack, a dirty tree, a host refusal and the job's own problems."""
        def tree_dirty():
            self.dirty = [" M analysis/v2/grid.py"]

        def drift():
            self.end_fingerprint = OTHER_FINGERPRINT

        cases = (("logs-pack", {}, dict(archive="-", sha="-"), None),
                 ("dirty-tree", {}, {}, tree_dirty),
                 ("host", dict(smoke=None), dict(smoke_exit=2), None),
                 ("job", dict(with_inputs=False), dict(build_exit=3, inputs_exit=1), None),
                 ("job", {}, dict(inputs_exit=2), None),
                 ("job", {}, {}, drift),
                 ("job", dict(names=self.names[:-3], registered=self.names), {}, None),
                 ("job", {}, dict(ctest_exit=1), None),
                 ("job", dict(smoke={"runs": []}), {}, None))
        for cls, make, write, setup in cases:
            with self.subTest(cls=cls, make=sorted(make), write=sorted(write)):
                self.record.unlink(missing_ok=True)
                self.dirty, self.end_fingerprint = [], FINGERPRINT
                if setup:
                    setup()
                self.make_work(**make)
                code, rec = self.write(**write)
                self.assertEqual(code, 1)
                self.assertEqual(self.classes(rec), [cls], rec["red_reasons"])
                self.assertFalse(rec["blocking"])
                self.assertFalse(gl.is_finding(rec))

    def test_a_skipped_test_did_not_run_and_is_a_job_problem_not_a_finding(self):
        self.make_work()
        text = (self.work / "ctest.xml").read_text(encoding="utf-8").replace(
            'status="run"><system-out>out</system-out></testcase>', 'status="run"><skipped/></testcase>', 1)
        (self.work / "ctest.xml").write_text(text, encoding="utf-8")
        code, rec = self.write()
        self.assertEqual((code, self.classes(rec), rec["blocking"]), (1, ["job"], False))

    def test_a_finding_and_an_infrastructure_reason_together_still_block(self):
        self.make_work(ctest_log="==4711==ERROR: AddressSanitizer: stack-use-after-return\n")
        code, rec = self.write(archive="-", sha="-")
        self.assertEqual(self.classes(rec), ["logs-pack", "report"])
        self.assertTrue(rec["blocking"])

    def test_review_b4_a_record_red_only_for_a_pack_failure_does_not_block_the_gate(self):
        """The writer's own red record, made when the logs could not be packed, beside green records of the same inputs."""
        records = self.root / "records"
        records.mkdir()
        for san in gl.SANITIZERS_BY_HOST["L"]:
            archive, sha = make_archive(self.root / "records-logs", f"wakeloop2-abc-L-{san}")
            self.record = records / f"wakeloop2-abc-L-{san}.json"
            self.make_work()
            self.assertEqual(self.write("--sanitizer", san, archive=archive, sha=sha)[0], 0)
        self.record = records / "wakeloop2-bad-L-asan.json"
        self.make_work()
        code, rec = self.write(archive="-", sha="-")
        self.assertEqual((code, rec["blocking"]), (1, False))
        notes = []
        with contextlib.redirect_stderr(io.StringIO()):
            got = gl.target_coverage(records, hashes(), config(), "Clang 22.1.8", GENERATORS, host="L", notes=notes)
        self.assertEqual([e["name"] for e in got["asan"]], ["wakeloop2-abc-L-asan.json"])
        self.assertTrue([n for n in notes if "wakeloop2-bad-L-asan.json" in n and "logs-pack" in n], notes)

    def test_a_failed_test_is_red(self):
        self.make_work(bad=["epoll.none.w1.post_runs_once"])
        code, rec = self.write(ctest_exit=8)
        self.assertEqual(code, 1)
        self.assertFalse(rec["green"])
        self.assertEqual(rec["tests_failed"], 1)
        self.assertEqual(rec["by_backend"]["epoll"]["matrix_bad"], 1)

    def test_a_ctest_exit_code_that_is_not_zero_is_red_whatever_the_junit_file_says(self):
        """CTest can fail (a crash, a timeout kill, a fixture) with every test it lists passing."""
        self.make_work()
        code, rec = self.write(ctest_exit=1)
        self.assertEqual(code, 1)
        self.assertEqual(rec["tests_failed"], 0)
        self.assertIn("ctest exit 1", rec["presets"][1]["detail"])

    def test_tests_that_did_not_run_are_red(self):
        self.make_work(names=self.names[:-3], registered=self.names)
        code, rec = self.write()
        self.assertEqual(code, 1)
        self.assertIn("registered", rec["presets"][1]["detail"])

    def test_a_skipped_test_is_red(self):
        self.make_work()
        text = (self.work / "ctest.xml").read_text(encoding="utf-8").replace(
            'status="run"><system-out>out</system-out></testcase>', 'status="run"><skipped/></testcase>', 1)
        (self.work / "ctest.xml").write_text(text, encoding="utf-8")
        self.assertEqual(self.write()[0], 1)

    def test_a_report_in_a_passing_test_is_red_and_the_report_is_in_the_record(self):
        """The wake detectors and the pinning checks pass on a regular expression whatever the exit code."""
        for line in ("==4711==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x6020",
                     "WARNING: ThreadSanitizer: data race (pid=4711)",
                     "==4711==WARNING: MemorySanitizer: use-of-uninitialized-value",
                     "src/a.cpp:12:5: runtime error: signed integer overflow",
                     "SUMMARY: UndefinedBehaviorSanitizer: undefined-behavior src/a.cpp:12:5"):
            with self.subTest(line=line):
                self.record.unlink(missing_ok=True)
                self.make_work(ctest_log=f"DETECTED: epoll.absent_wake\n{line}\nmore\n")
                code, rec = self.write()
                self.assertEqual(code, 1)
                self.assertGreaterEqual(rec["sanitizer_reports"], 1)
                joined = "".join(rec["report_blocks"])
                self.assertIn(line, joined, "the report line itself is in the record's blocks")
                self.assertTrue(rec["report_blocks"][0].startswith("ctest.log:"))

    def test_the_extra_patterns_catch_what_the_shared_one_misses(self):
        """review LOW: 'CHECK failed' and 'FATAL: ThreadSanitizer' lines are not reports by the shared pattern."""
        for line in ("FATAL: ThreadSanitizer: unexpected memory mapping 0x7f00-0x7f10",
                     "==9==ThreadSanitizer: CHECK failed: tsan_rtl.cpp:1 (0 vs 1)",
                     "==9==AddressSanitizer: CHECK failed: asan_x.cpp:2 ((0)) != (0)",
                     "MemorySanitizer: CHECK failed: msan.cpp:3"):
            with self.subTest(line=line):
                self.assertIsNone(wr.REPORT.search(line), "the shared pattern misses it, so the extra one is needed")
                self.assertTrue(wr.is_report(line), line)
                self.record.unlink(missing_ok=True)
                self.make_work(smoke_log=f"ok\n{line}\n")
                self.assertEqual(self.write()[0], 1)
        self.assertFalse(wr.is_report("100% tests passed"))
        self.assertFalse(wr.is_report("note: the check passed"))

    def test_a_report_in_the_build_log_the_smoke_log_or_a_standard_error_file_is_red(self):
        self.make_work(build_log="-- configured\nruntime error: x\n")
        self.assertEqual(self.write()[0], 1)
        self.record.unlink()
        self.make_work(smoke_log="ok smoke-001\nWARNING: ThreadSanitizer: data race (pid=1)\n")
        code, rec = self.write()
        self.assertEqual(code, 1)
        self.assertTrue(rec["report_blocks"][0].startswith("smoke.log:"))
        self.record.unlink()
        self.make_work()
        (self.work / "x.err").write_text("ERROR: LeakSanitizer: detected memory leaks\n", encoding="utf-8")
        code, rec = self.write()
        self.assertEqual(code, 1)
        self.assertTrue(rec["report_blocks"][0].startswith("x.err:"))

    # -- the inputs file (H4) --------------------------------------------------------------------------------------

    def test_review_h4_a_missing_inputs_file_is_red_and_says_so(self):
        self.make_work(with_inputs=False)
        code, rec = self.write(build_exit=3, inputs_exit=1)
        self.assertEqual(code, 1)
        self.assertIn("inputs_error", rec)
        self.assertNotIn("inputs_hash", rec)

    def test_review_h4_a_refused_inputs_run_makes_a_red_record_even_when_a_file_is_there(self):
        self.make_work()
        code, rec = self.write(inputs_exit=2)
        self.assertEqual(code, 1)
        self.assertIn("build_inputs.py exited 2", rec["presets"][4]["detail"])

    def test_review_h4_the_inputs_file_must_hold_the_host_the_compiler_and_the_file_hashes(self):
        for missing in ("host", "compiler", "file_hashes", "config"):
            with self.subTest(missing=missing):
                self.record.unlink(missing_ok=True)
                info = self.inputs_info()
                info.pop(missing)
                self.make_work(info=info)
                code, rec = self.write()
                self.assertEqual(code, 1)
                self.assertIn(missing, rec["presets"][4]["detail"])

    def test_an_unreadable_inputs_file_is_red(self):
        self.make_work()
        (self.work / "build.inputs.json").write_text("{", encoding="utf-8")
        code, rec = self.write()
        self.assertEqual(code, 1)
        self.assertIn("unreadable", rec["presets"][4]["detail"])

    # -- the code, the logs and the file -----------------------------------------------------------------------

    def test_review_m5_a_fingerprint_that_changed_during_the_job_is_red(self):
        self.make_work()
        self.end_fingerprint = OTHER_FINGERPRINT
        code, rec = self.write()
        self.assertEqual(code, 1)
        self.assertIn("fingerprint at the end", rec["presets"][3]["detail"])
        self.assertEqual(rec["code_fingerprint"], FINGERPRINT, "the record keeps the one from the start")

    def test_head_that_moved_during_the_job_is_red(self):
        self.make_work()
        self.head = "e" * 40
        code, rec = self.write()
        self.assertEqual(code, 1)
        self.assertIn("HEAD is", rec["presets"][3]["detail"])

    def test_a_tree_that_is_dirty_at_the_end_is_red_but_for_a_dry_run(self):
        self.make_work()
        self.dirty = [" M analysis/v2/grid.py"]
        code, rec = self.write()
        self.assertEqual(code, 1)
        self.assertIn("grid.py", rec["presets"][3]["detail"])
        self.record.unlink()
        code, rec = self.write("--dry-run")
        self.assertEqual(code, 0, rec["presets"])
        self.assertEqual(rec["worktree_dirty_paths"], 1)

    def test_review_m2_a_record_without_a_logs_archive_is_red(self):
        for archive, sha in (("-", "ab" * 32), ("/x/y.tar.gz", "-"), ("/x/y.tar.gz", "short"), ("-", "-")):
            with self.subTest(archive=archive, sha=sha):
                self.record.unlink(missing_ok=True)
                self.make_work()
                code, rec = self.write(archive=archive, sha=sha)
                self.assertEqual(code, 1)
                self.assertEqual(rec["presets"][5]["status"], "red")
                if archive == "-":
                    self.assertEqual(rec["logs_archive"], "")

    def test_review_m9_a_record_is_never_replaced(self):
        self.make_work(bad=["epoll.none.w1.post_runs_once"])
        code, first = self.write(ctest_exit=8)
        self.assertEqual(code, 1)
        text = self.record.read_text(encoding="utf-8")
        self.make_work()
        code, again = self.write()
        self.assertEqual(code, 2)
        self.assertEqual(self.record.read_text(encoding="utf-8"), text, "the red record is still there")

    # -- the backends and the smoke ---------------------------------------------------------------------------------

    def test_a_backend_without_its_check_runs_is_red(self):
        """A configure with -DWAKELOOP_BENCH=OFF has the wake matrix and none of the programs' checks."""
        names = [n for n in self.names if not n.startswith(("wakeprobe_check", "abpair_check"))]
        self.make_work(names=names)
        code, rec = self.write()
        self.assertEqual(code, 1)
        self.assertEqual(sorted(rec["missing_required"]), sorted(LINUX_BACKENDS))
        self.assertIn("required tests missing", rec["presets"][1]["detail"])

    def test_a_backend_left_out_of_the_build_is_red(self):
        names = [n for n in self.names if not (n.startswith("io_uring.") or ".io_uring" in n)]
        self.make_work(names=names)
        code, rec = self.write()
        self.assertEqual(code, 1)
        self.assertEqual(list(rec["missing_required"]), ["io_uring"])

    def test_the_required_tests_follow_the_backends_of_the_configuration(self):
        w_names = all_test_names(WINDOWS_BACKENDS)
        self.assertEqual(wr.missing_required(w_names, WINDOWS_BACKENDS), {})
        self.assertIn("epoll.no_retrigger.", wr.required_tests("epoll"))
        self.assertNotIn("iocp.no_retrigger.", wr.required_tests("iocp"))
        self.assertNotIn("condvar.no_retrigger.", wr.required_tests("condvar"))

    def test_a_configuration_with_no_backend_is_red(self):
        self.make_work(config_={**CONFIG, "WAKELOOP_BACKENDS": ""})
        self.assertEqual(self.write()[0], 1)

    def test_a_record_without_its_smoke_is_red(self):
        self.make_work(smoke=None)
        code, rec = self.write()
        self.assertEqual(code, 1)
        self.assertIn("no readable smoke.json", rec["presets"][2]["detail"])
        self.record.unlink()
        self.make_work(smoke="ok")
        self.assertEqual(self.write(smoke_exit=1)[0], 1, "the smoke's own exit code counts")

    def test_a_smoke_that_only_listed_its_shapes_is_red_unless_the_record_is_a_dry_run(self):
        self.make_work(smoke=smoke_data(mode="list"))
        code, rec = self.write()
        self.assertEqual(code, 1)
        self.assertIn("only a dry run may", rec["presets"][2]["detail"])
        self.record.unlink()
        code, rec = self.write("--dry-run")
        self.assertEqual(code, 0, rec["presets"])
        self.assertEqual((rec["smoke"]["mode"], rec["smoke"]["ran"], rec["smoke"]["planned"]), ("list", 0, len(cs.shapes("linux"))))

    def test_a_dry_run_that_claims_to_have_run_the_smoke_must_hold_every_run(self):
        data = smoke_data()
        data["runs"] = data["runs"][:-1]
        self.make_work(smoke=data)
        self.assertEqual(self.write("--dry-run")[0], 1)

    def test_a_smoke_with_a_failed_or_reporting_run_is_red(self):
        for change in ({"exit": 3}, {"exit": "timeout", "timed_out": True}, {"reports": 1}, {"timed_out": True}):
            with self.subTest(change=change):
                self.record.unlink(missing_ok=True)
                data = smoke_data()
                data["runs"][5] = {**data["runs"][5], **change}
                self.make_work(smoke=data)
                code, rec = self.write()
                self.assertEqual(code, 1)
                self.assertIn("smoke runs failed or reported", rec["presets"][2]["detail"])

    def test_a_smoke_with_lines_the_tools_refused_is_red(self):
        data = smoke_data(mode="run")
        data["validate_failures"] = [{"id": data["shapes"][0]["id"], "exit": 2, "message": "no"}]
        data["runs"] = []
        self.make_work(smoke=data)
        code, rec = self.write()
        self.assertEqual(code, 1)
        self.assertIn("--validate-args", rec["presets"][2]["detail"])

    def test_review_h3_a_smoke_that_missed_a_shape_or_ran_others_is_red(self):
        for what in ("dropped", "args", "id"):
            with self.subTest(what=what):
                self.record.unlink(missing_ok=True)
                data = smoke_data()
                if what == "dropped":
                    data["shapes"] = data["shapes"][:-1]
                    data["runs"] = data["runs"][:-1]
                    data["planned"], data["ran"] = len(data["shapes"]), len(data["runs"])
                elif what == "args":
                    data["shapes"][0] = {**data["shapes"][0], "args": data["shapes"][0]["args"] + ["--extra", "1"]}
                else:
                    data["runs"][0] = {**data["runs"][0], "id": "smoke-000-other"}
                self.make_work(smoke=data)
                self.assertEqual(self.write()[0], 1)

    def test_a_smoke_of_the_other_system_is_red(self):
        data = smoke_data("linux")
        data["system"] = "windows"
        self.make_work(smoke=data)
        self.assertEqual(self.write()[0], 1)

    @needs_h5("windows")
    def test_a_windows_record_holds_the_windows_smoke(self):
        self.make_work(tag="W", names=all_test_names(WINDOWS_BACKENDS))
        code, rec = self.write(tag="W")
        self.assertEqual(code, 0, rec["presets"])
        self.assertEqual(rec["smoke"]["planned"], len(cs.shapes("windows")))
        self.record.unlink()
        self.make_work(smoke="ok", tag="L", names=all_test_names(WINDOWS_BACKENDS), info=self.inputs_info("W"))
        self.assertEqual(self.write(tag="W")[0], 1, "a Windows record with the Linux smoke")

    def test_a_dry_run_is_marked(self):
        self.make_work()
        code, rec = self.write("--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(rec["dry_run"])
        self.assertIn("never citable", rec["note"])

    def test_the_junit_file_of_a_failed_run_may_be_missing_or_broken(self):
        self.make_work()
        (self.work / "ctest.xml").unlink()
        self.assertEqual(self.write(ctest_exit=1)[0], 1)
        self.record.unlink()
        (self.work / "ctest.xml").write_text("<testsuite", encoding="utf-8")
        self.assertEqual(self.write(ctest_exit=1)[0], 1)

    def test_the_registered_tests_are_read_from_ctest_n(self):
        self.assertEqual(wr.registered(self.root / "none.txt"), [])
        (self.root / "t.txt").write_text(ctest_n(["a.b", "c"]), encoding="utf-8")
        self.assertEqual(wr.registered(self.root / "t.txt"), ["a.b", "c"])

    def test_the_report_pattern_is_the_shared_one(self):
        self.assertEqual(wr.REPORT.pattern, CANONICAL_REPORT)
        for line in ("==4711==WARNING: MemorySanitizer: x", "ERROR: ThreadSanitizer: failed to allocate",
                     "SUMMARY: ThreadSanitizer: data race a.cpp:1", "a.cpp:1:2: runtime error: shift"):
            self.assertTrue(wr.REPORT.search(line), line)
        for line in ("100% tests passed, 0 tests failed out of 714", "-- Sanitizer: memory", "DETECTED: epoll.x"):
            self.assertFalse(wr.REPORT.search(line), line)
            self.assertFalse(wr.is_report(line), line)

    def test_the_report_pattern_is_the_labs(self):
        """lab/bin/test_report_pattern.sh holds the shared pattern as `want=`; LAB_BIN names that folder (or a Papers
        checkout lies beside this one)."""
        for cand in (Path(os.environ.get("LAB_BIN", "nowhere")), bi.REPO.parent / "Papers" / "lab" / "bin"):
            script = cand / "test_report_pattern.sh"
            if script.is_file():
                match = re.search(r"^want='(.*)'$", script.read_text(encoding="utf-8"), re.M)
                self.assertTrue(match, "the lab's script no longer has a want= line")
                self.assertEqual(match.group(1), wr.REPORT.pattern)
                return
        self.skipTest("LAB_BIN does not name the Papers lab/bin; lab/bin/test_report_pattern.sh is the lab's own check")


# ---------------------------------------------------------------------------------------------
# The drivers, run for real (the early refusals, always; the whole dry run, opt-in)
# ---------------------------------------------------------------------------------------------

def fixture_repo(root: Path) -> Path:
    """A repository of its own with the driver scripts, the code paths file and one commit: what the drivers need to
    get as far as their refusals, without a project to build."""
    repo = root / "driver repo"
    shutil.copytree(HERE, repo / "bench", ignore=shutil.ignore_patterns("__pycache__", "testdata", "build", "build-*"))
    shutil.copytree(bi.REPO / "analysis" / "v2", repo / "analysis" / "v2",
                    ignore=shutil.ignore_patterns("__pycache__", "test_*.py"))
    for rel in ("CMakeLists.txt", "loop/a.hpp", "tests/b.cpp"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(rel + "\n", encoding="utf-8")
    (repo / "bench" / "code_paths.txt").write_text("CMakeLists.txt\nloop\ntests\nbench/code_paths.txt\n", encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    return repo


def bash_path():
    """A bash that runs the .sh drivers with POSIX paths: Git for Windows' on a Windows host, never the WSL launcher."""
    found = shutil.which("bash")
    return found if found and not found.lower().endswith(os.path.join("system32", "bash.exe")) else None


class ShellDriverRefusalTests(TempCase):
    """bench/sanitize_wakeloop.sh run for real, as far as its refusals, which come before any build."""

    def setUp(self):
        super().setUp()
        self.bash = bash_path()
        if not self.bash:
            self.skipTest("no bash")
        skip_under_tmp(self)
        self.repo = fixture_repo(self.root)
        self.records = self.root / "records"
        self.shim = self.root / "shim"
        self.shim.mkdir()
        self.logs = self.root / "logs"

    def uname_shim(self, name):
        path = self.shim / "uname"
        path.write_text(f"#!/bin/sh\n[ \"$1\" = -n ] && echo {name} || echo Linux\n", encoding="utf-8")
        path.chmod(0o755)

    def date_shim(self):
        """A date that gives the driver's time stamp as 20261028t100001, then ...2, and is the real date otherwise."""
        counter = (self.root / "stamp-counter").as_posix()
        (self.shim / "date").write_text(
            '#!/bin/sh\ncase "$*" in\n  *%Y%m%dt%H%M%S*) n=$(cat "' + counter + '" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "'
            + counter + '"; printf "20261028t10%04d\\n" $n ;;\n  *) exec /usr/bin/date "$@" ;;\nesac\n', encoding="utf-8")
        (self.shim / "date").chmod(0o755)

    def lab_bin(self):
        folder = self.root / "lab bin"
        folder.mkdir(exist_ok=True)
        (folder / "inputs_hash.py").write_text("# stand-in\n", encoding="utf-8")
        return folder

    def commit9(self):
        return bi.git(self.repo, "rev-parse", "HEAD").stdout.strip()[:9]

    def drive(self, *args, env=None, cwd=None):
        base = {k: v for k, v in os.environ.items() if k not in ("LAB_JOB", "DRY_RUN", "LAB_BIN", "WORK", "RECORDS_LOGS", "MSAN_LIBCXX")}
        base["PATH"] = self.shim.as_posix() + os.pathsep + base["PATH"] if self.shim.exists() else base["PATH"]
        base.update(env or {})
        done = subprocess.run([self.bash, (self.repo / "bench" / "sanitize_wakeloop.sh").as_posix(), *args], capture_output=True,
                              text=True, env=base, cwd=cwd or self.repo, timeout=120)
        return done.returncode, done.stdout + done.stderr

    def dry(self, san="asan", **env):
        return self.drive(san, self.records.as_posix(), env={"DRY_RUN": "1", "WORK": (self.root / "work").as_posix(),
                                                              "RECORDS_LOGS": self.logs.as_posix(), **env})

    def test_an_unknown_sanitizer_is_refused(self):
        code, text = self.drive("ubsan", self.records.as_posix())
        self.assertEqual(code, 2)
        self.assertIn("unknown sanitizer", text)

    def test_a_record_needs_lab_job(self):
        code, text = self.drive("asan", self.records.as_posix())
        self.assertEqual(code, 2)
        self.assertIn("LAB_JOB is not set", text)

    def test_a_record_needs_the_laptops_host_name(self):
        self.uname_shim("ALEX-PC")
        code, text = self.drive("asan", self.records.as_posix(), env={"LAB_JOB": "j"})
        self.assertEqual(code, 2)
        self.assertIn("not the laptop's Arch", text)

    def test_a_record_needs_a_clean_tree_but_a_dry_run_does_not(self):
        self.uname_shim("alex-laptop")
        (self.repo / "NOTES.md").write_text("an untracked file\n", encoding="utf-8")
        code, text = self.drive("asan", self.records.as_posix(), env={"LAB_JOB": "j"})
        self.assertEqual(code, 2)
        self.assertIn("changed or untracked files", text)
        # The dry run gets past that check and stops at the next refusal that does not need a build: no inputs_hash.py.
        code, text = self.dry(LAB_BIN=(self.root / "nowhere").as_posix())
        self.assertEqual(code, 2)
        self.assertIn("no inputs_hash.py", text)

    def test_ignored_files_do_not_make_a_tree_dirty(self):
        self.uname_shim("alex-laptop")
        (self.repo / "__pycache__").mkdir(exist_ok=True)
        (self.repo / "__pycache__" / "x.pyc").write_text("c\n", encoding="utf-8")
        code, text = self.drive("asan", self.records.as_posix(), env={"LAB_JOB": "j", "LAB_BIN": (self.root / "nowhere").as_posix(),
                                                                       "RECORDS_LOGS": self.logs.as_posix()})
        self.assertEqual(code, 2)
        self.assertIn("no inputs_hash.py", text, "it got past the clean-tree check")

    def test_changed_code_paths_are_refused_even_for_a_dry_run(self):
        (self.repo / "loop" / "a.hpp").write_text("changed\n", encoding="utf-8")
        code, text = self.dry()
        self.assertEqual(code, 2)
        self.assertIn("code paths have changes", text)

    def test_a_dry_run_never_writes_into_lab_sanitizer_records(self):
        code, text = self.drive("asan", (self.root / "lab" / "sanitizer-records").as_posix(), env={"DRY_RUN": "1"})
        self.assertEqual(code, 2)
        self.assertIn("never writes into lab/sanitizer-records", text)

    def test_msan_without_an_instrumented_libcxx_is_refused_before_a_build(self):
        lab_bin = self.root / "lab bin"
        lab_bin.mkdir()
        (lab_bin / "inputs_hash.py").write_text("# stand-in\n", encoding="utf-8")  # so that the refusal is the libc++'s
        code, text = self.dry("msan", MSAN_LIBCXX=(self.root / "no libcxx").as_posix(), LAB_BIN=lab_bin.as_posix())
        self.assertEqual(code, 2)
        self.assertIn("no instrumented libc++", text)
        self.assertFalse((self.root / "work").exists() and any((self.root / "work").rglob("build.ninja")), "no build was started")

    def test_an_existing_log_folder_is_never_overwritten(self):
        code, text = self.dry(LAB_BIN=(self.root / "nowhere").as_posix())
        self.assertEqual(code, 2)
        first = [p.name for p in self.logs.glob("*")]
        self.assertEqual(first, [])
        self.date_shim()
        (self.logs / f"wakeloop2-{self.commit9()}-L-asan-20261028t100001-dryrun").mkdir(parents=True)
        code, text = self.dry()
        self.assertEqual(code, 2)
        self.assertIn("never overwritten", text)

    def test_review_n3_the_record_path_is_checked_before_any_build_and_the_name_has_a_time_stamp(self):
        self.date_shim()
        self.records.mkdir()
        existing = self.records / f"wakeloop2-{self.commit9()}-L-asan-20261028t100001-dryrun.json"
        existing.write_text("{}", encoding="utf-8")
        code, text = self.dry(LAB_BIN=self.lab_bin().as_posix())
        self.assertEqual(code, 2)
        self.assertIn("a record is never replaced", text)
        self.assertEqual(existing.read_text(encoding="utf-8"), "{}")
        self.assertFalse((self.root / "work").exists(), "nothing was built or made")

    def test_review_n3_a_build_that_fails_leaves_a_record_that_blocks_nothing_and_a_rerun_makes_another(self):
        """B4 through the driver: the name was one per commit, so a red record at a commit was the only one it could have."""
        if not shutil.which("cmake"):
            self.skipTest("no cmake: the driver's build step cannot be made to fail the ordinary way")
        self.uname_shim("alex-laptop")
        self.date_shim()
        env = {"LAB_JOB": "j", "LAB_BIN": self.lab_bin().as_posix(), "WORK": (self.root / "work").as_posix(),
               "RECORDS_LOGS": self.logs.as_posix()}
        names = []
        for stamp in ("20261028t100001", "20261028t100002"):
            code, text = self.drive("asan", self.records.as_posix(), env=env)
            self.assertEqual(code, 1, text[-1500:])  # a red record: exit 1, not a refusal
            names.append(f"wakeloop2-{self.commit9()}-L-asan-{stamp}.json")
            self.assertTrue((self.records / names[-1]).is_file(), sorted(p.name for p in self.records.glob("*")))
        self.assertEqual(sorted(p.name for p in self.records.glob("*.json")), names)
        for name in names:
            rec = json.loads((self.records / name).read_text(encoding="utf-8"))
            self.assertFalse(rec["green"])
            self.assertEqual(rec["format"], bi.RECORD_FORMAT)
            self.assertFalse(rec["blocking"], rec["red_reasons"])
            self.assertEqual({r["class"] for r in rec["red_reasons"]}, {"job"})
            self.assertFalse(gl.is_finding(rec))
            self.assertTrue(Path(rec["logs_archive"]).is_file())
            self.assertEqual(hashlib.sha256(Path(rec["logs_archive"]).read_bytes()).hexdigest(), rec["logs_archive_sha256"])


class KeepRecordLogsTests(TempCase):
    """keep_record_logs.sh run for real: check, pack, and a pack that fails (review LOW)."""

    def setUp(self):
        super().setUp()
        self.bash = bash_path()
        if not self.bash:
            self.skipTest("no bash")
        skip_under_tmp(self)
        self.script = (HERE / "keep_record_logs.sh").as_posix()
        self.logs = self.root / "logs dir"
        self.src = self.root / "src"
        self.src.mkdir()
        (self.src / "a.log").write_text("a\n", encoding="utf-8")
        (self.src / "b.log").write_text("b\n", encoding="utf-8")

    def keep(self, *args):
        env = {**os.environ, "RECORDS_LOGS": self.logs.as_posix()}
        done = subprocess.run([self.bash, self.script, *args], capture_output=True, text=True, env=env, timeout=120)
        return done.returncode, done.stdout.strip(), done.stderr.strip()

    def test_a_pack_makes_the_folder_the_archive_and_the_sha256_and_prints_them(self):
        code, out, err = self.keep("pack", "rec", (self.src / "a.log").as_posix(), (self.src / "b.log").as_posix(), (self.src / "gone.log").as_posix())
        self.assertEqual(code, 0, err)
        archive, sha = out.rsplit(" ", 1)
        self.assertEqual(archive.replace("\\", "/").split("/")[-2:], ["logs dir", "rec.tar.gz"])
        self.assertRegex(sha, r"^[0-9a-f]{64}$")
        self.assertEqual(hashlib.sha256((self.logs / "rec.tar.gz").read_bytes()).hexdigest(), sha)
        self.assertEqual(sorted(p.name for p in (self.logs / "rec").iterdir()), ["SHA256SUMS", "a.log", "b.log"])
        self.assertEqual((self.logs / "rec.tar.gz.sha256").read_text().split()[0], sha)
        self.assertEqual(sorted(p.name for p in self.logs.iterdir()), ["rec", "rec.tar.gz", "rec.tar.gz.sha256"], "no staging left")

    def test_a_path_with_a_space_survives_the_split_of_the_output_line(self):
        code, out, err = self.keep("pack", "rec")
        self.assertEqual(code, 0, err)
        self.assertEqual(out[:out.rindex(" ")].replace("\\", "/"), (self.logs / "rec.tar.gz").as_posix().replace("\\", "/"))

    def test_check_refuses_what_exists_and_pack_does_not_overwrite(self):
        self.assertEqual(self.keep("check", "rec")[0], 0)
        self.assertEqual(self.keep("pack", "rec")[0], 0)
        self.assertEqual(self.keep("check", "rec")[0], 2)
        code, _, err = self.keep("pack", "rec")
        self.assertEqual(code, 2)
        self.assertIn("exists", err)

    def test_a_pack_that_fails_leaves_nothing_that_blocks_a_rerun(self):
        """review LOW: a failed pack left the kept folder, which then refused the rerun."""
        shim = self.root / "shim"
        shim.mkdir()
        (shim / "tar").write_text("#!/bin/sh\necho 'tar: no space left on device' >&2\nexit 2\n", encoding="utf-8")
        (shim / "tar").chmod(0o755)
        env = {**os.environ, "RECORDS_LOGS": self.logs.as_posix(), "PATH": shim.as_posix() + os.pathsep + os.environ["PATH"]}
        done = subprocess.run([self.bash, self.script, "pack", "rec", (self.src / "a.log").as_posix()], capture_output=True,
                              text=True, env=env, timeout=120)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual([p.name for p in self.logs.iterdir()], [], "no folder, no archive, no staging")
        self.assertEqual(self.keep("check", "rec")[0], 0, "a rerun is not blocked")
        self.assertEqual(self.keep("pack", "rec", (self.src / "a.log").as_posix())[0], 0)

    def test_logs_never_go_to_tmp(self):
        env = {**os.environ, "RECORDS_LOGS": "/tmp/logs"}
        done = subprocess.run([self.bash, self.script, "check", "rec"], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 2)


def skip_under_tmp(case):
    """The drivers refuse a log folder under /tmp (rule D5): on a host whose temporary folder is /tmp, set TMPDIR."""
    if case.root.as_posix().startswith("/tmp/"):
        case.skipTest("TMPDIR is under /tmp, where record logs never go; set TMPDIR to another folder to run this")


def powershell_path():
    return shutil.which("powershell") if sys.platform == "win32" else None


class PowerShellDriverRefusalTests(TempCase):
    """bench/sanitize_wakeloop.ps1 run for real in Windows PowerShell, as far as its refusals."""

    def setUp(self):
        super().setUp()
        self.ps = powershell_path()
        if not self.ps:
            self.skipTest("no Windows PowerShell")
        self.repo = fixture_repo(self.root)  # no origin: the review's `2>$null` under Stop
        self.labbin = self.root / "lab bin"
        self.labbin.mkdir()
        (self.labbin / "inputs_hash.py").write_text("# stand-in\n", encoding="utf-8")
        self.records = self.root / "records"

    def drive(self, *args, env=None):
        base = {k: v for k, v in os.environ.items() if k not in ("LAB_JOB", "LAB_PYTHON", "LAB_NINJA", "LAB_CMAKE")}
        base.update(env or {})
        done = subprocess.run([self.ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                               str(self.repo / "bench" / "sanitize_wakeloop.ps1"), "-Records", str(self.records), *args],
                              capture_output=True, text=True, env=base, cwd=self.repo, timeout=300)
        return done.returncode, done.stdout + done.stderr

    def test_a_record_needs_lab_job(self):
        code, text = self.drive("-LabBin", str(self.labbin))
        self.assertNotEqual(code, 0)
        self.assertIn("LAB_JOB is not set", text)

    def test_a_record_needs_the_laptops_host_name(self):
        code, text = self.drive("-LabBin", str(self.labbin), env={"LAB_JOB": "j"})
        self.assertNotEqual(code, 0)
        self.assertIn("not the laptop's Windows", text)

    def test_review_n8_the_laptops_host_name_is_taken_without_regard_to_case(self):
        laptop = {"LAB_JOB": "j", "COMPUTERNAME": gl.LAB_HOSTS["W"][0].lower()}
        (self.repo / "NOTES.md").write_text("an untracked file\n", encoding="utf-8")
        code, text = self.drive("-LabBin", str(self.labbin), env=laptop)
        self.assertNotEqual(code, 0)
        self.assertNotIn("not the laptop's Windows", text)
        self.assertIn("changed or untracked files", text, "past the host check to the next refusal")

    def test_cl_and_underscore_cl_are_refused_even_for_a_dry_run(self):
        for var in ("CL", "_CL_"):
            with self.subTest(var=var):
                code, text = self.drive("-DryRun", "-LabBin", str(self.labbin), env={var: "/O0"})
                self.assertNotEqual(code, 0)
                self.assertIn("CL or _CL_ is set", text)

    def test_a_record_needs_a_clean_tree_on_the_laptop(self):
        """With the laptop's host name (set in the child's environment) and a job, an untracked file stops the driver."""
        laptop = {"LAB_JOB": "j", "COMPUTERNAME": gl.LAB_HOSTS["W"][0]}
        (self.repo / "NOTES.md").write_text("an untracked file\n", encoding="utf-8")
        code, text = self.drive("-LabBin", str(self.labbin), env=laptop)
        self.assertNotEqual(code, 0)
        self.assertIn("changed or untracked files", text)
        (self.repo / "NOTES.md").unlink()
        code, text = self.drive("-LabBin", str(self.labbin), "-Work", str(self.root / "wörk"), env=laptop)
        self.assertIn("not ASCII", text, "a clean tree gets past the check to the next refusal")

    def test_a_dry_run_into_lab_sanitizer_records_is_refused(self):
        records = self.root / "lab" / "sanitizer-records"
        self.records = records
        code, text = self.drive("-DryRun", "-LabBin", str(self.labbin))
        self.assertNotEqual(code, 0)
        self.assertIn("never writes into lab", text)

    def test_review_a_checkout_without_an_origin_does_not_stop_the_driver_at_the_remote_lookup(self):
        """`git remote get-url origin 2>$null` under $ErrorActionPreference = Stop aborted the driver (PowerShell 5.1)."""
        code, text = self.drive("-DryRun", "-LabBin", str(self.labbin), "-Work", str(self.root / "wörk"))
        self.assertNotEqual(code, 0)
        self.assertNotIn("No such remote", text)
        self.assertIn("not ASCII", text, "it got past the remote lookup to the next refusal")

    def test_a_non_ascii_path_is_refused_before_the_cmd_file_is_written(self):
        code, text = self.drive("-DryRun", "-LabBin", str(self.labbin), "-Work", str(self.root / "wörk"))
        self.assertNotEqual(code, 0)
        self.assertIn("not ASCII", text)
        self.assertFalse(list((self.root / "wörk").rglob("sanitize.cmd")) if (self.root / "wörk").exists() else [])

    def test_changed_code_paths_are_refused_for_a_dry_run_too(self):
        (self.repo / "loop" / "a.hpp").write_text("changed\n", encoding="utf-8")
        code, text = self.drive("-DryRun", "-LabBin", str(self.labbin))
        self.assertNotEqual(code, 0)
        self.assertIn("code paths", text)


@unittest.skipUnless(os.environ.get("P1_RUN_DRIVER_TESTS") == "1",
                     "the whole dry run of a driver takes minutes: set P1_RUN_DRIVER_TESTS=1 (and LAB_BIN to the Papers lab/bin)")
class DriverRunTests(TempCase):
    """Each driver run for real in its dry mode on this checkout, not read: the build, the whole CTest suite, the smoke
    listing, the logs and the record. The record must be green, marked dry_run, and a smoke of mode list."""

    def lab_bin(self):
        lab_bin = os.environ.get("LAB_BIN")
        if not lab_bin or not (Path(lab_bin) / "inputs_hash.py").is_file():
            self.skipTest("LAB_BIN does not name the Papers lab/bin")
        return lab_bin

    def check_record(self, records, tag):
        found = list(Path(records).glob("wakeloop2-*-dryrun.json"))
        self.assertEqual(len(found), 1, found)
        self.assertRegex(found[0].name, r"^wakeloop2-[0-9a-f]{9}-[LW]-[a-z]+-\d{8}t\d{6}-dryrun\.json$")  # unique: a time stamp
        rec = json.loads(found[0].read_text(encoding="utf-8"))
        self.assertTrue(rec["green"], rec["presets"])
        self.assertEqual((rec["format"], rec["red_reasons"], rec["blocking"]), (bi.RECORD_FORMAT, [], False))
        self.assertEqual(rec["compiler"], bi.normalize_identity(rec["compiler"]), "CMake's identity, cut to three parts")
        self.assertEqual(rec["compiler_banner"], rec["compiler"])
        self.assertTrue(rec["dry_run"])
        self.assertEqual(rec["smoke"]["mode"], "list")
        self.assertEqual(rec["host_tag"], tag)
        self.assertEqual([p["status"] for p in rec["presets"]], ["green"] * len(rec["presets"]))
        self.assertTrue(re.match(r"^[0-9a-f]{64}$", rec["logs_archive_sha256"]))
        for name in ("ASAN_OPTIONS", "UBSAN_OPTIONS", "LSAN_OPTIONS", "TSAN_OPTIONS", "MSAN_OPTIONS"):
            self.assertIn(name + "=", rec["sanitizer_options"], "review M4: every *SAN_OPTIONS is set and recorded")
        return rec

    @unittest.skipUnless(sys.platform.startswith("linux"), "the shell driver is Linux's")
    def test_the_shell_driver_in_its_dry_mode(self):
        lab_bin = self.lab_bin()
        work = os.environ.get("P1_DRIVER_WORK") or str(self.root / "work")  # a local file system: ninja's log breaks on 9p
        env = {**os.environ, "DRY_RUN": "1", "LAB_BIN": lab_bin, "WORK": work, "RECORDS_LOGS": str(self.root / "logs"), "JOBS": "4"}
        san = os.environ.get("P1_DRIVER_SAN", "asan")  # asan, tsan or msan
        done = subprocess.run(["bash", str(HERE / "sanitize_wakeloop.sh"), san, str(self.root / "records")], env=env,
                              capture_output=True, text=True, timeout=3600)
        self.assertEqual(done.returncode, 0, done.stdout[-2000:] + done.stderr[-2000:])
        rec = self.check_record(self.root / "records", "L")
        self.assertEqual(rec["sanitizer"], san)
        # The folder of this test has a space in its name (TempCase), so the archive path of the record has one: the
        # driver must not split it (review LOW) and the archive must be the file the record names.
        self.assertIn(" ", rec["logs_archive"])
        self.assertEqual(hashlib.sha256(Path(rec["logs_archive"]).read_bytes()).hexdigest(), rec["logs_archive_sha256"])

    @unittest.skipUnless(sys.platform == "win32", "the PowerShell driver is Windows'")
    def test_the_powershell_driver_in_its_dry_mode(self):
        lab_bin = self.lab_bin()
        work = os.environ.get("P1_DRIVER_WORK") or str(self.root / "work")
        done = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(HERE / "sanitize_wakeloop.ps1"),
                               "-DryRun", "-Records", str(self.root / "records"), "-Work", work, "-RecordsLogs", str(self.root / "logs"),
                               "-LabBin", lab_bin], capture_output=True, text=True, timeout=3600)
        self.assertEqual(done.returncode, 0, done.stdout[-2000:] + done.stderr[-2000:])
        self.check_record(self.root / "records", "W")


# ---------------------------------------------------------------------------------------------
# The tools the gate starts: the build's ninja and LAB_GIT (N1), the compiler (N4), the binaries (N5)
# ---------------------------------------------------------------------------------------------

def host_of_this_platform():
    """The records' name for the system these tests run on: ninja's log is in this platform's units."""
    return "W" if sys.platform == "win32" else "L"


def path_without(program):
    """PATH with every folder that holds PROGRAM taken out, so that a bare PROGRAM cannot be found."""
    kept = []
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if folder and not (Path(folder) / program).exists() and not (Path(folder) / (program + ".exe")).exists():
            kept.append(folder)
    return os.pathsep.join(kept)


class ToolDiscoveryTests(TempCase):
    def make_cache(self, **entries):
        build = self.root / "build"
        build.mkdir(exist_ok=True)
        (build / "CMakeCache.txt").write_text("".join(f"{k}:FILEPATH={v}\n" for k, v in entries.items()), encoding="utf-8")
        return build

    def test_review_n1_ninja_is_the_builds_own_never_one_from_path(self):
        fake = self.root / "tools" / "ninja.exe"
        fake.parent.mkdir()
        fake.write_text("x", encoding="utf-8")
        build = self.make_cache(CMAKE_MAKE_PROGRAM=fake.as_posix())
        self.assertEqual(bi.ninja_path(build), fake.as_posix())
        with self.assertRaises(bi.InputsError) as caught:
            bi.ninja_path(self.make_cache(CMAKE_CXX_COMPILER="/usr/bin/clang++"))
        self.assertEqual(caught.exception.code, 3)
        self.assertIn("CMAKE_MAKE_PROGRAM", str(caught.exception))
        with self.assertRaises(bi.InputsError) as caught:
            bi.ninja_path(self.make_cache(CMAKE_MAKE_PROGRAM=(self.root / "gone" / "ninja").as_posix()))
        self.assertIn("does not exist", str(caught.exception))

    def test_the_children_that_call_a_bare_ninja_get_the_builds_ninja_first_on_path(self):
        """The Lab's inputs_hash.py runs `ninja -t compdb` and `ninja -t deps` by name."""
        fake = self.root / "tools" / "ninja.exe"
        fake.parent.mkdir()
        fake.write_text("x", encoding="utf-8")
        build = self.make_cache(CMAKE_MAKE_PROGRAM=fake.as_posix())
        env = bi.tool_env(build)
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(fake.parent))
        self.assertIn(os.environ.get("PATH", "").split(os.pathsep)[0], env["PATH"])
        seen = {}

        def fake_run(cmd, *args, **kwargs):
            seen["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(bi.subprocess, "run", fake_run):
            bi.run_inputs_hash(build, self.root / "out.json", Path("inputs_hash.py"))
        self.assertEqual(seen["env"]["PATH"].split(os.pathsep)[0], str(fake.parent))

    def test_review_n1_git_is_lab_git_when_set_else_git_and_a_missing_one_is_named(self):
        git_path = shutil.which("git")
        if not git_path:
            self.skipTest("no git")
        repo = self.root / "repo"
        repo.mkdir()
        REAL_GIT(repo, "--version")
        self.assertEqual(bi.git_program(), os.environ.get("LAB_GIT") or "git")
        with mock.patch.dict(os.environ, {"LAB_GIT": git_path, "PATH": path_without("git")}):
            self.assertEqual(REAL_GIT(repo, "--version").returncode, 0, "LAB_GIT works with no git on PATH")
        with mock.patch.dict(os.environ, {"LAB_GIT": str(self.root / "no" / "git")}):
            with self.assertRaises(RuntimeError) as caught:
                REAL_GIT(repo, "--version")
        self.assertIn(str(self.root / "no" / "git"), str(caught.exception))
        self.assertIn("LAB_GIT", str(caught.exception))
        env = {k: v for k, v in os.environ.items() if k != "LAB_GIT"}
        env["PATH"] = path_without("git")
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                REAL_GIT(repo, "--version")
        self.assertIn("'git'", str(caught.exception))
        self.assertIn("set LAB_GIT", str(caught.exception))


class CompilerIdentityTests(TempCase):
    def test_a_cmake_identification_and_a_banner_of_the_same_compiler_are_the_same_text(self):
        self.assertEqual(bi.normalize_identity("MSVC 19.51.36246.0"), "MSVC 19.51.36246")
        self.assertEqual(bi.normalize_identity("MSVC 19.51.36246"), "MSVC 19.51.36246")
        self.assertEqual(bi.normalize_identity("Clang 22.1.8"), "Clang 22.1.8")
        self.assertEqual(bi.normalize_identity("GNU 15.2.1.0"), "GNU 15.2.1")
        self.assertEqual(bi.normalize_identity("Clang 22"), "Clang 22")
        for junk in ("", "Clang", "22.1.8", "Clang x.y"):
            self.assertEqual(bi.normalize_identity(junk), "", junk)

    def write_identification(self, build, cmake_version, name, version, mtime, executable="/opt/llvm/bin/clang++"):
        folder = build / "CMakeFiles" / cmake_version
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "CMakeCXXCompiler.cmake"
        path.write_text(f'set(CMAKE_CXX_COMPILER "{executable}")\nset(CMAKE_CXX_COMPILER_ID "{name}")\n'
                        f'set(CMAKE_CXX_COMPILER_VERSION "{version}")\n', encoding="utf-8")
        os.utime(path, (mtime, mtime))

    def test_a_compiler_given_as_cl_is_started_by_the_path_cmake_resolved(self):
        """A build configured with -DCMAKE_CXX_COMPILER=cl has just cl in its cache, and a gate without the Visual Studio
        environment cannot start that: the full path is in CMake's own record (found in the real build of RealBuildGateTests)."""
        build = self.root / "build"
        self.write_identification(build, "4.4.3", "MSVC", "19.51.36246.0", 2_000_000, executable="C:/VS/bin/Hostx64/x64/cl.exe")
        (build / "CMakeCache.txt").write_text("CMAKE_CXX_COMPILER:UNINITIALIZED=cl\n", encoding="utf-8")
        self.assertEqual(bi.read_cache(build)["CMAKE_CXX_COMPILER"], "cl")
        self.assertEqual(bi.compiler_executable(build), "C:/VS/bin/Hostx64/x64/cl.exe")
        no_record = self.root / "other"
        no_record.mkdir()
        (no_record / "CMakeCache.txt").write_text("CMAKE_CXX_COMPILER:FILEPATH=/usr/bin/clang++\n", encoding="utf-8")
        self.assertEqual(bi.compiler_executable(no_record), "/usr/bin/clang++")

    def test_the_identification_is_that_of_the_newest_configure_cut_to_three_parts(self):
        build = self.root / "build"
        self.write_identification(build, "4.3.2", "Clang", "21.0.0", 1_000_000)
        self.write_identification(build, "4.4.3", "MSVC", "19.51.36246.0", 2_000_000)
        self.assertEqual(bi.compiler_identification(build), "MSVC 19.51.36246")
        self.write_identification(build, "4.3.2", "Clang", "21.0.0", 3_000_000)
        self.assertEqual(bi.compiler_identification(build), "Clang 21.0.0", "the newest, whatever the CMake version names")
        self.assertEqual(bi.compiler_identification(self.root), "")

    def test_review_n4_the_compiler_changed_since_configure(self):
        info = {"compiler": "MSVC 19.51.36246", "compiler_banner": "MSVC 19.51.36246", "cxx_compiler": "cl.exe"}
        self.assertEqual(bi.compiler_change(info), "")
        self.assertIn("changed since configure", bi.compiler_change({**info, "compiler_banner": "MSVC 19.52.40000"}))
        self.assertIn("configure and build again", bi.compiler_change({**info, "compiler_banner": "MSVC 19.52.40000"}))
        self.assertIn("cannot be started", bi.compiler_change({**info, "compiler_banner": ""}))
        self.assertIn("cannot be identified", bi.compiler_change({**info, "compiler": ""}))

    def test_review_n4_build_inputs_refuses_a_build_whose_compiler_changed(self):
        out = self.root / "build.inputs.json"
        info = {"compiler": "Clang 22.1.8", "compiler_banner": "Clang 23.0.0", "cxx_compiler": "/usr/bin/clang++", "targets": {}}
        stderr = io.StringIO()
        with mock.patch.object(bi, "compute_inputs", lambda *a, **k: {"scope": "s", "builds": {bi.BUILD: info}}), \
                contextlib.redirect_stderr(stderr):
            code = bi.main(["--build", str(self.root), "--host", "L", "--out", str(out)])
        self.assertEqual(code, 2)
        self.assertIn("changed since configure", stderr.getvalue())
        self.assertFalse(out.exists())


def make_elf(path, comments):
    """A minimal 64-bit little-endian ELF file with a .comment section holding COMMENTS (and no code)."""
    comment = b"".join(c.encode() + b"\0" for c in comments)
    names = b"\0.comment\0.shstrtab\0"
    header_size, section_header_size = 64, 64
    comment_off = header_size
    names_off = comment_off + len(comment)
    table_off = (names_off + len(names) + 7) & ~7
    headers = [(0, 0, 0, 0), (1, 1, comment_off, len(comment)), (10, 3, names_off, len(names))]
    ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + bytes(8)
    elf_header = ident + struct.pack("<HHIQQQIHHHHHH", 2, 62, 1, 0, 0, table_off, 0, header_size, 0, 0,
                                     section_header_size, len(headers), 2)
    body = elf_header + comment + names
    body += bytes(table_off - len(body))
    for name, kind, offset, size in headers:
        body += struct.pack("<IIQQQQIIQQ", name, kind, 0, 0, offset, size, 0, 0, 1, 0)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(body)


class ElfCommentTests(TempCase):
    def binaries(self, comments):
        for rel in bi.BINARIES["L"].values():
            make_elf(self.root / rel, comments)
        return self.root

    def test_the_comment_section_is_read(self):
        make_elf(self.root / "a.out", ["GCC: (GNU) 16.2.1 20260810", "clang version 22.1.8"])
        self.assertEqual(gl.elf_comments(self.root / "a.out"), ["GCC: (GNU) 16.2.1 20260810", "clang version 22.1.8"])

    def test_what_is_no_elf_or_has_no_comment_is_refused(self):
        (self.root / "text").write_bytes(b"MZ not an ELF" * 10)
        with self.assertRaises(gl.GateError):
            gl.elf_comments(self.root / "text")
        make_elf(self.root / "be", ["x"])
        raw = bytearray((self.root / "be").read_bytes())
        raw[5] = 2  # big endian
        (self.root / "be").write_bytes(raw)
        with self.assertRaises(gl.GateError):
            gl.elf_comments(self.root / "be")
        raw[5], raw[4] = 1, 1  # 32 bit
        (self.root / "be").write_bytes(raw)
        with self.assertRaises(gl.GateError):
            gl.elf_comments(self.root / "be")

    def test_review_n4_the_binaries_name_the_clang_version_that_cmake_identified(self):
        gl.check_binary_compiler(self.binaries(["GCC: (GNU) 16.2.1 20260810", "clang version 22.1.8"]), "L", "Clang 22.1.8")
        gl.check_binary_compiler(self.binaries(["Ubuntu clang version 22.1.8-1ubuntu1 (x)"]), "L", "Clang 22.1.8")
        for comments, text in ((["clang version 21.1.0"], "21.1.0"), (["GCC: (GNU) 16.2.1"], "names no clang version"),
                               (["clang version 22.1.8", "clang version 21.1.0"], "21.1.0")):
            with self.subTest(comments=comments), self.assertRaises(gl.GateError) as caught:
                gl.check_binary_compiler(self.binaries(comments), "L", "Clang 22.1.8")
            self.assertIn(text, str(caught.exception))

    def test_only_l_and_clang_have_this_check(self):
        gl.check_binary_compiler(self.root / "nothing", "W", "MSVC 19.51.36246")
        gl.check_binary_compiler(self.root / "nothing", "L", "GNU 15.2.1")


class BinariesBuiltTests(TempCase):
    """check_binaries_built against a tree that ninja built: a binary put there afterwards is seen (N5)."""

    def setUp(self):
        super().setUp()
        if not shutil.which("ninja"):
            self.skipTest("no ninja on PATH")
        self.host = host_of_this_platform()
        self.build = self.root / "ninja tree"
        (self.build / "bench").mkdir(parents=True)
        (self.build / "in.txt").write_text("input\n", encoding="utf-8")
        copy = f'"{sys.executable}" -c "import shutil, sys; shutil.copyfile(sys.argv[1], sys.argv[2])"'
        self.binary_paths = list(bi.BINARIES[self.host].values())
        outputs = ["obj1.o", "obj2.o", "obj3.o", "lib.a", *self.binary_paths]
        rules = "".join(f"build {out}: cp in.txt\n" for out in outputs)
        (self.build / "build.ninja").write_text(f"rule cp\n  command = {copy} $in $out\n{rules}", encoding="utf-8")
        subprocess.run(["ninja", "-C", str(self.build)], check=True, capture_output=True)

    def test_a_tree_as_ninja_built_it_passes(self):
        gl.check_binaries_built(self.build, self.host)

    def test_a_binary_written_a_little_after_its_command_passes(self):
        """A linker finishes writing after the command's start; the tolerance is seconds."""
        binary = self.build / self.binary_paths[0]
        later = binary.stat().st_mtime + 1
        os.utime(binary, (later, later))
        gl.check_binaries_built(self.build, self.host)

    def test_review_n5_a_binary_put_there_after_the_build_is_refused(self):
        """ninja -n says nothing is to do after a newer file takes an output's place (BuildIsCurrentTests); the mtime
        against .ninja_log is what sees it."""
        binary = self.build / self.binary_paths[0]
        for shift in (+3600, +60, -3600):
            with self.subTest(shift=shift):
                moved = binary.stat().st_mtime + shift
                original = binary.stat().st_mtime
                binary.write_bytes(b"not what the rule made")
                os.utime(binary, (moved, moved))
                with self.assertRaises(gl.GateError) as caught:
                    gl.check_binaries_built(self.build, self.host)
                self.assertIn(self.binary_paths[0], str(caught.exception))
                self.assertIn("not the one ninja built", str(caught.exception))
                os.utime(binary, (original, original))
                gl.check_binaries_built(self.build, self.host)

    def test_a_binary_ninja_did_not_make_is_refused(self):
        other = self.build / "bench" / "extra"
        other.write_text("x", encoding="utf-8")
        with mock.patch.dict(bi.BINARIES[self.host], {"wakeprobe": "bench/extra"}):
            with self.assertRaises(gl.GateError) as caught:
                gl.check_binaries_built(self.build, self.host)
        self.assertIn("has no entry in .ninja_log", str(caught.exception))

    def test_a_missing_log_or_too_few_outputs_cannot_be_compared(self):
        (self.build / ".ninja_log").unlink()
        with self.assertRaises(gl.GateError) as caught:
            gl.check_binaries_built(self.build, self.host)
        self.assertIn("no readable .ninja_log", str(caught.exception))
        small = self.root / "small"
        small.mkdir()
        (small / ".ninja_log").write_text("# ninja log v7\n", encoding="utf-8")
        with self.assertRaises(gl.GateError) as caught:
            gl.check_binaries_built(small, self.host)
        self.assertIn("too few outputs", str(caught.exception))

    def test_the_log_is_read_as_ninja_writes_it(self):
        entries = gl.read_ninja_log(self.build)
        self.assertEqual({"obj1.o", "obj2.o", "obj3.o", "lib.a", *self.binary_paths}, set(entries))
        start, end, logged = entries["obj1.o"]
        self.assertTrue(0 <= start <= end and logged > 0)


# ---------------------------------------------------------------------------------------------
# A fresh configure and the real build, on this system (N4, the pinned flags, E1 to E7)
# ---------------------------------------------------------------------------------------------

def vcvars_path():
    """vcvars64.bat of the Visual Studio that the W records use (versions.md), or None; P1_VCVARS names another."""
    for cand in (os.environ.get("P1_VCVARS", ""), r"C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvars64.bat"):
        if cand and Path(cand).is_file():
            return cand
    return None


def configure_fresh(build: Path, host: str):
    """cmake -S REPO -B BUILD -G Ninja -DCMAKE_BUILD_TYPE=Release with this system's compiler: the configure that
    PINNED_FLAGS describes. Returns None, or a reason to skip."""
    if not shutil.which("cmake") or not shutil.which("ninja"):
        return "no cmake or ninja"
    args = ["-S", str(bi.REPO), "-B", str(build), "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release"]
    if host == "L":
        if not shutil.which("clang++"):
            return "no clang++"
        done = subprocess.run(["cmake", *args, "-DCMAKE_CXX_COMPILER=clang++"], capture_output=True, text=True)
    else:
        vcvars = vcvars_path()
        if not vcvars:
            return "no Visual Studio (give P1_VCVARS, the vcvars64.bat)"
        script = build.parent / "fresh.cmd"
        script.write_text('@echo off\r\ncall "' + vcvars + '" >nul || exit /b 1\r\ncmake ' + " ".join(f'"{a}"' for a in args)
                          + " -DCMAKE_CXX_COMPILER=cl\r\n", encoding="ascii")
        done = subprocess.run(["cmd.exe", "/c", str(script)], capture_output=True, text=True)
    if done.returncode != 0:
        return "the configure failed: " + (done.stdout + done.stderr)[-400:]
    return None


_FRESH = {}


def fresh_build(host):
    """(a fresh configure of this checkout for HOST, or None; why not, or None): made once for all the tests."""
    if host not in _FRESH:
        folder = Path(tempfile.mkdtemp(prefix="fresh configure "))
        atexit.register(shutil.rmtree, folder, True)
        reason = configure_fresh(folder / "fresh", host)
        _FRESH[host] = (None, reason) if reason else (folder / "fresh", None)
    return _FRESH[host]


class FreshConfigureTests(unittest.TestCase):
    """The constants of the gate against a configure made now, with this system's compiler, on each system where there
    is one. The test used to compare the constants with a copy of themselves (review of 2026-10-27)."""

    def setUp(self):
        self.host = host_of_this_platform()
        self.build, reason = fresh_build(self.host)
        if reason:
            self.skipTest(reason)
        self.cache = bi.read_cache(self.build)

    def test_the_pinned_flags_are_what_a_fresh_configure_gives(self):
        got = {key: self.cache.get(key) for key in gl.PINNED_FLAGS[self.host]}
        self.assertEqual(got, gl.PINNED_FLAGS[self.host])

    def test_the_fresh_cache_passes_the_build_configuration_check(self):
        gl.check_build_config(self.build, self.host)
        self.assertTrue(gl.cmake_false(self.cache.get("WAKELOOP_SANITIZER")))
        self.assertTrue(gl.cmake_false(self.cache.get("CMAKE_INTERPROCEDURAL_OPTIMIZATION")))
        self.assertFalse(self.cache.get("CMAKE_TOOLCHAIN_FILE"))

    def test_the_builds_own_ninja_is_named_in_the_cache(self):
        self.assertTrue(Path(bi.ninja_path(self.build)).is_file())

    def test_the_banner_is_the_identity_cmake_gave_to_the_compiler(self):
        """E1 and E2: MSVC's CMake version has a fourth part that cl's banner does not."""
        info = {"compiler": bi.compiler_identification(self.build), "cxx_compiler": self.cache["CMAKE_CXX_COMPILER"]}
        info["compiler_banner"] = bi.compiler_identity(info["cxx_compiler"])
        self.assertRegex(info["compiler"], r"^(Clang|MSVC) \d+\.\d+\.\d+$")
        self.assertEqual(bi.compiler_change(info), "", info)

    def test_the_keys_of_the_configuration_are_in_the_cache(self):
        for key in bi.CONFIG_KEYS:
            self.assertIn(key, self.cache, key)


def find_real_build():
    """A Release build of this checkout that is up to date, on this system, or None: P1_REAL_BUILD names one, else the
    folders the desktop's dev runs use."""
    host = host_of_this_platform()
    names = [os.environ.get("P1_REAL_BUILD", "")]
    names += [str(bi.REPO / n) for n in ("build-d5-msvcrel", "build-msvc-val")] if host == "W" else ["/work/build-d5-rel"]
    for name in names:
        build = Path(name) if name else None
        if build and (build / "CMakeCache.txt").is_file() and all((build / rel).is_file() for rel in bi.BINARIES[host].values()):
            return build
    return None


class RealBuildGateTests(TempCase):
    """The whole gate, with the real compute_inputs, inputs_hash.py, compiler banner, scan of the binaries, ninja -n,
    ninja log and ELF comment, on a real build of this system. Only the dirty check of the worktree is stubbed (it holds the
    uncommitted work of the review) and the records are made here from what the build computes."""

    def setUp(self):
        super().setUp()
        self.host = host_of_this_platform()
        self.build = find_real_build()
        if not self.build:
            self.skipTest("no real build of this checkout (P1_REAL_BUILD names a Release build made from it)")
        try:
            self.tool = Path(os.environ["LAB_BIN"]) / "inputs_hash.py" if os.environ.get("LAB_BIN") else bi.default_inputs_hash()
        except FileNotFoundError:
            self.tool = None
        if not self.tool or not self.tool.is_file():
            self.skipTest("no inputs_hash.py (LAB_BIN)")
        for patch in (mock.patch.object(bi, "code_dirty", return_value=[]), mock.patch.object(bi, "path_dirty", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)
        try:
            self.info = bi.compute_inputs(self.build, self.host, self.tool)["builds"][bi.BUILD]
        except bi.InputsError as e:
            self.skipTest(f"the real build cannot be hashed: {e}")
        if gl.build_is_current(self.build):
            self.skipTest(f"{self.build} is not up to date: build it")
        self.other = gl.OTHER_HOST[self.host]
        self.own, self.theirs = self.root / "own", self.root / "theirs"
        self.own.mkdir()
        self.theirs.mkdir()
        self.write_records(self.info["compiler"])

    def write_records(self, identity):
        hashes_ = {t: v["inputs_hash"] for t, v in self.info["targets"].items()}
        for san in gl.SANITIZERS_BY_HOST[self.host]:
            name = f"wakeloop2-abc-{self.host}-{san}"
            archive, sha = make_archive(self.root / "logs", name)
            put(self.own, name + ".json", record(self.host, san, compiler=identity, inputs_hash=hashes_, config=self.info["config"],
                                                 host=gl.LAB_HOSTS[self.host][0], logs_archive=archive, logs_archive_sha256=sha,
                                                 code_fingerprint=bi.code_fingerprint(), generators=bi.generator_hashes()))
        for san in gl.SANITIZERS_BY_HOST[self.other]:
            put(self.theirs, f"wakeloop2-abc-{self.other}-{san}.json",
                record(self.other, san, code_fingerprint=bi.code_fingerprint(), generators=bi.generator_hashes()))

    def gate(self, **kwargs):
        with contextlib.redirect_stderr(io.StringIO()):
            return gl.run_gate(build=self.build, host=self.host, records=self.own, other_records=self.theirs, tool=self.tool, **kwargs)

    def test_the_real_gate_passes_on_a_real_build_and_names_what_compiled_it(self):
        result = self.gate()
        self.assertTrue(result["passed"])
        self.assertEqual(result["wakeloop"]["compiler"], bi.compiler_identification(self.build))
        self.assertEqual(result["wakeloop"]["compiler"], result["wakeloop"]["compiler_banner"])
        for name, rel in bi.BINARIES[self.host].items():
            self.assertEqual(result["binaries"][name], hashlib.sha256((self.build / rel).read_bytes()).hexdigest())
        self.assertEqual(result["wakeloop"]["inputs_hash"], {t: v["inputs_hash"] for t, v in self.info["targets"].items()})

    def test_review_e3_no_ninja_on_path_is_no_reason_to_refuse(self):
        no_ninja = path_without("ninja")
        self.assertIsNone(shutil.which("ninja", path=no_ninja), "the test needs a PATH with no ninja")
        # git may live in the folder of ninja (it does in the container): LAB_GIT names it, as the lab's jobs may have to
        with mock.patch.dict(os.environ, {"PATH": no_ninja, "LAB_GIT": shutil.which("git") or "git"}):
            self.assertTrue(self.gate()["passed"])

    def test_review_e3_no_git_on_path_is_refused_by_name_and_lab_git_cures_it(self):
        git_path = shutil.which("git")
        if not git_path:
            self.skipTest("no git")
        no_git = path_without("git")
        self.assertIsNone(shutil.which("git", path=no_git), "the test needs a PATH with no git")
        env = {k: v for k, v in os.environ.items() if k != "LAB_GIT"}
        env["PATH"] = no_git
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(gl.GateError) as caught:
                self.gate()
            self.assertIn("LAB_GIT", str(caught.exception))
            self.assertIn("'git'", str(caught.exception))
            with mock.patch.dict(os.environ, {"LAB_GIT": git_path}):
                self.assertTrue(self.gate()["passed"])

    def test_review_e2_after_a_compiler_update_the_records_of_the_compiler_that_compiled_still_cover(self):
        with mock.patch.object(bi, "compiler_identity", return_value="Clang 99.0.0"):
            with self.assertRaises(gl.GateError) as caught:
                self.gate()
            self.assertIn("changed since configure", str(caught.exception))
            result = self.gate(resume_compiler=self.info["compiler"])  # the resume of a run that started before the update
        self.assertEqual(result["wakeloop"]["compiler"], self.info["compiler"])
        self.write_records("Clang 99.0.0")  # records of the banner's compiler are not those of the binaries
        with mock.patch.object(bi, "compiler_identity", return_value="Clang 99.0.0"):
            with self.assertRaises(gl.GateError):
                self.gate(resume_compiler=self.info["compiler"])

    def test_review_e7_a_record_that_names_a_missing_archive_does_not_gate(self):
        name = f"wakeloop2-abc-{self.host}-asan"
        (self.root / "logs" / (name + ".tar.gz")).unlink()
        with self.assertRaises(gl.GateError) as caught:
            self.gate()
        self.assertIn("is not on this machine", str(caught.exception))

    def test_a_binary_put_in_place_after_the_build_is_refused_on_a_real_tree(self):
        copy = self.root / "tree"
        shutil.copytree(self.build, copy, ignore=shutil.ignore_patterns("*.tmp"))
        binary = copy / bi.BINARIES[self.host]["wakeprobe"]
        data = binary.read_bytes()
        binary.write_bytes(data)  # the same bytes, a new time
        later = binary.stat().st_mtime + 3600
        os.utime(binary, (later, later))
        with mock.patch.object(gl, "build_is_current", return_value=""):
            with self.assertRaises(gl.GateError) as caught:
                with contextlib.redirect_stderr(io.StringIO()):
                    gl.run_gate(build=copy, host=self.host, records=self.own, other_records=self.theirs, tool=self.tool)
        self.assertIn("not the one ninja built", str(caught.exception))

    @unittest.skipUnless(sys.platform.startswith("linux"), "the .comment section is Linux's")
    def test_review_n4_the_real_binaries_name_the_clang_that_made_them(self):
        for rel in bi.BINARIES["L"].values():
            self.assertTrue([c for c in gl.elf_comments(self.build / rel) if "clang version" in c], rel)
        gl.check_binary_compiler(self.build, "L", self.info["compiler"])
        with self.assertRaises(gl.GateError):
            gl.check_binary_compiler(self.build, "L", "Clang 1.2.3")


# ---------------------------------------------------------------------------------------------
# run_h5 with a gate that refuses (C3), and one that passes and then sees the binary change
# ---------------------------------------------------------------------------------------------

STUB_GATE = '''import hashlib, os, sys
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
build = args[args.index("--build-dir") + 1]
if open(os.path.join(here, "gate-mode.txt")).read().strip() == "refuse":
    print("run_campaign: refused: the stub gate refuses", file=sys.stderr)
    sys.exit(2)
with open(os.path.join(build, "bench", "wakeprobe"), "rb") as f:
    print("records gate passed: gate passed; gate compiler Clang 22.1.8")
    print("binary wakeprobe sha256 " + hashlib.sha256(f.read()).hexdigest())
    print("binary abpair sha256 " + "0" * 64)
'''

STUB_WAKEPROBE = '''#!/bin/sh
# Writes its --out file; on its first run it also changes itself, as a binary put in place between two cells would be.
prev=""
for a in "$@"; do
    if [ "$prev" = --out ]; then echo '{}' > "$a"; fi
    prev=$a
done
marker="$(dirname "$0")/ran-once"
if [ ! -e "$marker" ]; then : > "$marker"; echo "# changed" >> "$0"; fi
exit 0
'''


JOB_ENV = {**os.environ, "LAB_JOB": "p1-test"}  # run_h5 is a lab job's, as the runner is


class H5GateTests(TempCase):
    def setUp(self):
        super().setUp()
        self.bash = bash_path()
        self.bench = self.root / "bench copy"
        shutil.copytree(HERE, self.bench, ignore=shutil.ignore_patterns("__pycache__", "testdata", "build", "build-*"))
        (self.bench / "run_campaign.py").write_text(STUB_GATE, encoding="utf-8")
        self.build = self.root / "build"
        (self.build / "bench").mkdir(parents=True)
        (self.build / "CMakeCache.txt").write_text("CMAKE_BUILD_TYPE:STRING=Release\n", encoding="utf-8")
        self.out = self.root / "out"

    def mode(self, text):
        (self.bench / "gate-mode.txt").write_text(text, encoding="utf-8")

    def test_review_c3_run_h5_sh_with_a_refusing_gate_runs_no_cell_and_writes_nothing(self):
        if not self.bash:
            self.skipTest("no bash")
        wakeprobe = self.build / "bench" / "wakeprobe"
        wakeprobe.write_text(STUB_WAKEPROBE, encoding="utf-8")
        wakeprobe.chmod(0o755)
        self.mode("refuse")
        done = subprocess.run([self.bash, (self.bench / "run_h5.sh").as_posix(), "--build-dir", self.build.as_posix(), "--out-dir",
                               self.out.as_posix(), "--records", self.root.as_posix(), "--other-records", self.root.as_posix()],
                              capture_output=True, text=True, timeout=120, env=JOB_ENV)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("the records gate does not pass", done.stderr)
        self.assertIn("the stub gate refuses", done.stderr)
        self.assertFalse((self.build / "bench" / "ran-once").exists(), "wakeprobe was started")
        self.assertFalse(self.out.exists(), "an output folder was made")

    def test_run_h5_sh_without_the_records_folders_refuses_a_timed_run(self):
        if not self.bash:
            self.skipTest("no bash")
        wakeprobe = self.build / "bench" / "wakeprobe"
        wakeprobe.write_text(STUB_WAKEPROBE, encoding="utf-8")
        wakeprobe.chmod(0o755)
        done = subprocess.run([self.bash, (self.bench / "run_h5.sh").as_posix(), "--build-dir", self.build.as_posix(), "--out-dir",
                               self.out.as_posix()], capture_output=True, text=True, timeout=120, env=JOB_ENV)
        self.assertEqual(done.returncode, 2)
        self.assertIn("give --records and --other-records", done.stderr)
        self.assertFalse(self.out.exists())

    def test_review_n9_run_h5_sh_keeps_the_gate_output_and_stops_when_the_binary_changes(self):
        """LOW: the gate's output goes to gate.txt in the output folder, and wakeprobe is hashed again before every cell."""
        if not self.bash:
            self.skipTest("no bash")
        wakeprobe = self.build / "bench" / "wakeprobe"
        wakeprobe.write_text(STUB_WAKEPROBE, encoding="utf-8")
        wakeprobe.chmod(0o755)
        gate_sha = hashlib.sha256(wakeprobe.read_bytes()).hexdigest()
        self.mode("pass")
        done = subprocess.run([self.bash, (self.bench / "run_h5.sh").as_posix(), "--build-dir", self.build.as_posix(), "--out-dir",
                               self.out.as_posix(), "--records", self.root.as_posix(), "--other-records", self.root.as_posix()],
                              capture_output=True, text=True, timeout=240, env=JOB_ENV)
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)  # the second cell finds another binary
        gate_txt = (self.out / "gate.txt").read_text(encoding="utf-8")
        self.assertIn("records gate passed", gate_txt)
        self.assertIn(f"binary wakeprobe sha256 {gate_sha}", gate_txt)
        self.assertIn("BINARY-CHANGED wakeprobe", gate_txt)
        self.assertIn(f"expected={gate_sha}", gate_txt)
        self.assertIn(f"wakeprobe sha256: {gate_sha}", (self.out / "plan.txt").read_text(encoding="utf-8"), "the plan holds the gate's hash")
        self.assertEqual(len(list(self.out.glob("*.jsonl"))), 1, "one cell ran before the change was seen")

    @unittest.skipUnless(sys.platform == "win32", "run_h5.ps1 is Windows'")
    def test_review_c3_run_h5_ps1_with_a_refusing_gate_runs_no_cell_and_writes_nothing(self):
        (self.build / "bench" / "wakeprobe.exe").write_bytes(b"MZ not run")
        self.mode("refuse")
        done = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.bench / "run_h5.ps1"),
                               "-BuildDir", str(self.build), "-OutDir", str(self.out), "-Records", str(self.root),
                               "-OtherRecords", str(self.root)], capture_output=True, text=True, timeout=240, env=JOB_ENV)
        self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("the records gate does not pass", done.stdout + done.stderr)
        self.assertFalse(self.out.exists(), "an output folder was made")

    @unittest.skipUnless(sys.platform == "win32", "run_h5.ps1 is Windows'")
    def test_run_h5_ps1_without_the_records_folders_refuses_a_timed_run(self):
        (self.build / "bench" / "wakeprobe.exe").write_bytes(b"MZ not run")
        done = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.bench / "run_h5.ps1"),
                               "-BuildDir", str(self.build), "-OutDir", str(self.out)], capture_output=True, text=True, timeout=240, env=JOB_ENV)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("give -Records and -OtherRecords", done.stdout + done.stderr)
        self.assertFalse(self.out.exists())


class CoverageJsonTests(unittest.TestCase):
    def test_coverage_json_declares_no_target_gap_and_names_every_gap(self):
        cov = json.loads((HERE / "coverage.json").read_text(encoding="utf-8"))
        self.assertEqual(cov["targets"], {}, "a target gap lets a record be missing; none is intended")
        text = json.dumps(cov["declared_gaps"])
        for needle in ("io_uring", "libc++", "TSan", "use-after-return", "Full-size runs"):
            self.assertIn(needle, text.replace("ThreadSanitizer", "TSan"))
        for gap in cov["declared_gaps"]:
            self.assertTrue(gap["gap"] and gap["sanitizers"] and gap["reason"], gap)

    def test_the_v1_scripts_are_not_replaced(self):
        for name in ("check_records.py", "sanitizer_record.py", "inputs_gate.py", "sanitize_wakeprobe.sh"):
            self.assertTrue((HERE / name).is_file(), name + " belongs to the 2026-09 study")


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""The name contract: the strings of the plan, the readers and the synthetic fixtures are the tools' own.

plan.py takes its backends, variants and protocols from grid.py, readers.py names the summary fields
it reads, and synth.py writes lines for the tests. Each of these is checked here against the
sources of the tools: loop/include/wakeloop/*.hpp, bench/wakeprobe.cpp and bench/abpair.cpp. A
name that drifted in the plan, the readers or a fixture, so that every test still agreed with the
code beside it, fails here. The sources are read through the repository root, so these tests need
the repository (they fail, not skip, without it).
"""

import os
import re
import unittest

import grid
import plan
import readers
import synth

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
INCLUDE = os.path.join(REPO, "loop", "include", "wakeloop")
SOURCES = {
    "defect": os.path.join(INCLUDE, "defect.hpp"),
    "backends": os.path.join(INCLUDE, "backends.hpp"),
    "variants": os.path.join(INCLUDE, "variants.hpp"),
    "wakeprobe": os.path.join(REPO, "bench", "wakeprobe.cpp"),
    "abpair": os.path.join(REPO, "bench", "abpair.cpp"),
}
WORKERS = (1, 2, 4)


def read(name):
    path = SOURCES[name]
    if not os.path.isfile(path):
        raise AssertionError("%s is not where the name contract looks for it (%s)" % (name, path))
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def json_names(text):
    """Every "name" the source writes inside a JSON string, i.e. the \\"name\\" of its string literals."""
    return set(re.findall(r'\\"([A-Za-z_][A-Za-z0-9_]*)\\"', text))


def literals(text):
    """Every plain string literal of a source."""
    return set(re.findall(r'"([^"\\\n]*)"', text))


def function_body(text, start, stop):
    """The text from the line holding `start` up to the line holding `stop`, comments removed."""
    i = text.index(start)
    j = text.index(stop, i)
    return "\n".join(re.sub(r"//.*", "", line) for line in text[i:j].splitlines())


class Variants(unittest.TestCase):
    def test_the_variants_are_the_loop_s_own_in_its_order(self):
        text = read("defect")
        names = re.findall(r'case Defect::(\w+): return "(\w+)";', function_body(text, "defect_name", "parse_defect"))
        self.assertEqual(len(names), 9)
        self.assertTrue(all(enum == string for enum, string in names), names)
        self.assertEqual(tuple(string for _, string in names), grid.VARIANTS)
        listed = re.findall(r"Defect::(\w+)", function_body(text, "kAllVariants{", "/// Tasks an undrained-edge"))
        self.assertEqual(tuple(listed), grid.VARIANTS)

    def test_fixed_and_plain_wake_are_the_loop_s_names(self):
        self.assertIn(grid.FIXED, re.findall(r'return "(\w+)";', function_body(read("defect"), "defect_name", "parse_defect")))
        self.assertIn(grid.PLAIN, grid.VARIANTS)
        for name in grid.LAZY_WAKE + (grid.H4_ARM_B, grid.SWEEP_VARIANT):
            self.assertIn(name, grid.VARIANTS)

    def test_the_sites_and_the_least_workers(self):
        text = read("variants")
        found = re.search(r"if \(d == Defect::(\w+)\) return ((?:backend == \"\w+\"(?: \|\| )?)+);", text)
        self.assertIsNotNone(found, "has_site changed shape")
        with_site = set(re.findall(r'backend == "(\w+)"', found.group(2)))
        for backend in ("epoll", "io_uring", "iocp", "condvar"):
            for variant in grid.VARIANTS:
                want = backend in with_site if variant == found.group(1) else True
                self.assertEqual(grid.has_site(backend, variant), want, (backend, variant))
        least = re.search(r"min_workers\(Defect d\) noexcept \{ return d == Defect::(\w+) \? (\d+)u : (\d+)u; \}", text)
        self.assertIsNotNone(least, "min_workers changed shape")
        for variant in grid.VARIANTS:
            want = int(least.group(2)) if variant == least.group(1) else int(least.group(3))
            self.assertEqual(grid.min_workers(variant), want, variant)

    def test_the_variants_that_delay_single_posts(self):
        body = function_body(read("wakeprobe"), "constexpr bool delays_single_posts", "[[noreturn]] void usage")
        groups = re.findall(r"((?:case wakeloop::Defect::\w+:\s*)+)return ([^;]+);", body)
        self.assertEqual(sum(g[0].count("case") for g in groups), 9)
        for cases, expression in groups:
            for variant in re.findall(r"Defect::(\w+):", cases):
                for workers in WORKERS:
                    want = {"true": True, "false": False, "workers >= 2": workers >= 2}[expression.strip()]
                    self.assertEqual(grid.delays_single_posts(variant, workers), want, (variant, workers))


class Backends(unittest.TestCase):
    def test_the_backends_of_each_system(self):
        text = read("variants")
        linux = re.search(r"#if defined\(__linux__\)\s*inline constexpr std::array<std::string_view, (\d)> kBackends\{([^}]*)\};",
                          text)
        windows = re.search(r"#elif defined\(_WIN32\)\s*inline constexpr std::array<std::string_view, (\d)> kBackends\{([^}]*)\};",
                            text)
        self.assertIsNotNone(linux)
        self.assertIsNotNone(windows)
        for system, found in (("linux", linux), ("windows", windows)):
            names = tuple(re.findall(r'"(\w+)"', found.group(2)))
            self.assertEqual(int(found.group(1)), len(names))
            self.assertEqual(names, grid.BACKENDS[system], system)

    def test_the_names_the_summary_reports(self):
        # The backend a summary names is the loop type's `name`, which must be the name the plan passes.
        names = set(re.findall(r'static constexpr const char\* name = "(\w+)";', read("backends")))
        self.assertEqual(names, {b for system in grid.SYSTEMS for b in grid.BACKENDS[system]})


class Flags(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entries = {s: plan.campaign_entries(s, {c: 14 for c in grid.h4_cells(s)}) + plan.pilot_plan_entries(s)
                       for s in grid.SYSTEMS}

    def used(self, tool):
        flags, values = set(), {}
        for entries in self.entries.values():
            for e in entries:
                if e["tool"] == tool:
                    for flag, value in zip(e["args"][::2], e["args"][1::2]):
                        flags.add(flag)
                        values.setdefault(flag, set()).add(value)
        return flags, values

    def test_every_flag_of_a_plan_line_is_the_tool_s(self):
        for tool in readers.TOOLS:
            source = literals(read(tool))
            flags, _ = self.used(tool)
            self.assertTrue(flags)
            for flag in flags:
                self.assertIn(flag, source, "%s has no flag %s" % (tool, flag))

    def test_the_values_that_are_names(self):
        wp = literals(read("wakeprobe"))
        ab = literals(read("abpair"))
        _, wp_values = self.used("wakeprobe")
        _, ab_values = self.used("abpair")
        self.assertEqual(wp_values["--protocol"], {"single", "batch"})
        for name in wp_values["--protocol"] | wp_values["--gap-unit"]:
            self.assertIn(name, wp)
        self.assertEqual(ab_values["--order"], {"abba"})
        self.assertIn("abba", ab)
        self.assertEqual(readers.arg_value(plan.s_bt_entries("linux")[0]["args"], "--protocol"), "single")
        bt = [e for e in plan.s_bt_entries("linux") if e["family"] == "Bt"][0]
        self.assertEqual(readers.arg_value(bt["args"], "--protocol"), "batch")
        # The sources agree with themselves: protocol_name gives exactly these two.
        found = re.search(r'p == Protocol::batch \? "(\w+)" : "(\w+)"', read("wakeprobe"))
        self.assertEqual({found.group(1), found.group(2)}, {"single", "batch"})
        for variant in wp_values["--variant"] | ab_values["--arm-a"] | ab_values["--arm-b"]:
            self.assertIn(variant, grid.VARIANTS)


class Fields(unittest.TestCase):
    def test_the_summary_and_record_fields_the_readers_read(self):
        wakeprobe = json_names(read("wakeprobe"))
        abpair = json_names(read("abpair"))
        for name in readers.WAKEPROBE_SUMMARY_FIELDS + readers.WAKEPROBE_RECORD_FIELDS:
            self.assertIn(name, wakeprobe, "wakeprobe does not write %r" % name)
        for name in readers.ABPAIR_SUMMARY_FIELDS + readers.ABPAIR_WINDOW_FIELDS:
            self.assertIn(name, abpair, "abpair does not write %r" % name)

    def test_the_literals_the_readers_compare_with(self):
        # "Release" is the build type the root CMakeLists.txt defaults to and both tools write; abpair's
        # mode and program names are the ones its summary writes.
        with open(os.path.join(REPO, "CMakeLists.txt"), "r", encoding="utf-8") as f:
            self.assertRegex(f.read(), r"set\(CMAKE_BUILD_TYPE %s CACHE" % readers.RELEASE)
        with open(os.path.join(REPO, "bench", "CMakeLists.txt"), "r", encoding="utf-8") as f:
            cmake = f.read()
        self.assertIn('WAKEPROBE_BUILD_TYPE="${CMAKE_BUILD_TYPE}"', cmake)
        self.assertIn('ABPAIR_BUILD_TYPE="${CMAKE_BUILD_TYPE}"', cmake)
        abpair = read("abpair")
        self.assertIn('\\"program\\":\\"%s\\"' % readers.ABPAIR_PROGRAM, abpair)
        self.assertRegex(abpair, r'\\"mode\\":\\"" << \(o\.timed\(\) \? "%s" : "count"\)' % readers.ABPAIR_MODE)
        self.assertIn('\\"check\\":" << (o.check ? "true" : "false")', abpair)
        # The build and the tree the summaries report are the bench's own CMake variables.
        self.assertIn("BENCH_DIRTY", cmake)
        self.assertIn("BENCH_COMMIT", cmake)

    def test_the_unit_kinds(self):
        body = function_body(read("wakeprobe"), "const char* kind_name", "struct Record")
        self.assertEqual(tuple(re.findall(r'return "(\w+)";', body)), readers.WAKEPROBE_KINDS)

    def test_the_synthetic_lines_write_only_the_tools_names(self):
        wakeprobe = json_names(read("wakeprobe"))
        abpair = json_names(read("abpair"))
        entries = {
            "S": plan.s_bt_entries("linux")[0],
            "S delaying": [e for e in plan.s_bt_entries("windows") if "absent_wake-W1" in e["id"] and e["family"] == "S"][0],
            "Bt": [e for e in plan.s_bt_entries("linux") if e["family"] == "Bt"][0],
            "sweep": plan.sweep_entries("windows")[0],
        }
        for label, e in entries.items():
            for system in grid.SYSTEMS:
                lines = synth.wakeprobe_output(e, [1000] * int(readers.arg_value(e["args"], "--posts")), 1000000,
                                               15625 if system == "windows" else None, system=system)
                for line in lines:
                    for key in line:
                        self.assertIn(key, wakeprobe, "%s: synth writes %r, which wakeprobe does not" % (label, key))
        h4 = plan.h4_entries("linux", {c: 14 for c in grid.h4_cells("linux")})[0]
        for line in synth.abpair_output(h4, [1.0, 1.0]):
            for key in line:
                self.assertIn(key, abpair, "synth writes %r, which abpair does not" % key)

    def test_the_synthetic_summaries_hold_every_field_the_readers_read(self):
        e = plan.s_bt_entries("linux")[0]
        summary = synth.wakeprobe_output(e, [1000] * 630)[-1]
        self.assertEqual(set(readers.WAKEPROBE_SUMMARY_FIELDS) - set(summary), set())
        summary = synth.abpair_output(plan.h4_entries("linux", {c: 14 for c in grid.h4_cells("linux")})[0], [1.0, 1.0])[-1]
        self.assertEqual(set(readers.ABPAIR_SUMMARY_FIELDS) - set(summary), set())


class HardLimit(unittest.TestCase):
    """C1's k: the kHardLimit waits one unit can make in wakeprobe.cpp, counted from its source."""

    def test_the_limit_and_the_waits_per_unit(self):
        text = read("wakeprobe")
        self.assertRegex(text, r"constexpr auto kHardLimit = std::chrono::seconds\(%d\);" % plan.HARD_LIMIT_S)
        single = function_body(text, "Record measure_one(", "std::int64_t since(")
        batch = function_body(text, "Record measure_round(", "// B_eff from the intervals")
        self.assertEqual(single.count("kHardLimit"), plan.HARD_WAITS["single"])
        self.assertEqual(batch.count("kHardLimit"), plan.HARD_WAITS["batch"])
        # The single post's second wait is a deadline from the same post, not a new wait on top of the first.
        self.assertIn("posted + kHardLimit", single)


if __name__ == "__main__":
    unittest.main()

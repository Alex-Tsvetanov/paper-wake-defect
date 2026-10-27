"""The h4-r block of the revision log: writing, reading and every refusal (hypotheses-v2.md, sections 7 and 8)."""

import hashlib
import os
import shutil
import tempfile
import unittest

import grid
import h4log
import stats
import synth

OVERRIDES = {
    "linux-epoll-N1us": {"r": 18},
    "linux-condvar-N0us": {"pairs": 0},
    "windows-iocp-N100us": {"r": 24, "underpowered": True},
}


def good_block():
    return h4log.render_block(synth.pilot_cells(14, OVERRIDES))


def lines_of(text):
    return text.split("\n")


def replace_line(text, key, old, new):
    """Change `old` to `new` in the one row of cell `key`."""
    out = []
    hit = 0
    for line in lines_of(text):
        if line.startswith("| %s |" % key):
            assert old in line
            line = line.replace(old, new)
            hit += 1
        out.append(line)
    assert hit == 1
    return "\n".join(out)


def drop_line(text, key):
    return "\n".join(line for line in lines_of(text) if not line.startswith("| %s |" % key))


class Block(unittest.TestCase):
    def test_round_trip(self):
        rows = h4log.parse_block(good_block())
        self.assertEqual(len(rows), 20)  # 5 backend-system pairs x N in {0, 1, 10, 100} us
        self.assertEqual(list(rows), list(h4log.expected_cells()))
        self.assertEqual({k: r["R"] for k, r in rows.items() if r["R"] != 14},
                         {"linux-epoll-N1us": 18, "linux-condvar-N0us": 24, "windows-iocp-N100us": 24})
        e = rows["linux-epoll-N1us"]
        self.assertEqual((e["system"], e["backend"], e["work_us"], e["pairs"], e["underpowered"]),
                         ("linux", "epoll", 1, 8, False))
        self.assertEqual(e["s"], 0.0125)  # exactly: s is written at full precision
        self.assertAlmostEqual(e["power"], 0.8584, places=4)  # section 7: s = 0.0125 gives R = 18, power 0.86
        none = rows["linux-condvar-N0us"]
        self.assertEqual((none["pairs"], none["s"], none["power"], none["R"], none["underpowered"]),
                         (0, None, None, 24, True))
        self.assertTrue(rows["windows-iocp-N100us"]["underpowered"])

    def test_shape(self):
        lines = lines_of(good_block())
        self.assertEqual(lines[0], "<!-- h4-r:begin -->")
        self.assertEqual(lines[1], "| cell | system | backend | N (us) | pairs | s | power | R | underpowered |")
        self.assertEqual(lines[2], "|---|---|---|---|---|---|---|---|---|")
        self.assertEqual(lines[-2:], ["<!-- h4-r:end -->", ""])
        self.assertEqual(len(lines), 1 + 2 + 20 + 1 + 1)
        self.assertIn("| linux-condvar-N0us | linux | condvar | 0 | 0 | - | - | 24 | yes |", lines)
        self.assertNotIn(chr(0x2013), good_block())
        self.assertNotIn(chr(0x2014), good_block())

    def test_the_block_is_found_among_prose(self):
        text = ("# Revision log\n\nThe pilot wrote <!-- h4-r:begin --> and <!-- h4-r:end --> around its table.\n\n"
                "## 2026-11-01 R from the pilot\n\nWhy.\n\n" + good_block() + "\nAfter the block.\n")
        self.assertEqual(len(h4log.parse_block(text)), 20)
        indented = text.replace("<!-- h4-r:end -->\n", "  <!-- h4-r:end -->  \n")
        self.assertEqual(len(h4log.parse_block(indented)), 20)

    def test_blank_lines_inside_the_block_are_fine(self):
        text = good_block().replace("|---|---|---|---|---|---|---|---|---|\n", "|---|---|---|---|---|---|---|---|---|\n\n")
        self.assertEqual(len(h4log.parse_block(text)), 20)

    def test_render_needs_all_cells(self):
        cells = synth.pilot_cells(14)
        del cells["windows-condvar-N10us"]
        with self.assertRaisesRegex(h4log.LogError, "no pilot cell windows-condvar-N10us"):
            h4log.render_block(cells)

    def test_render_refuses_what_the_parser_refuses(self):
        for r in (13, 12, 26):
            cells = synth.pilot_cells(14)
            cells["linux-epoll-N1us"]["R"] = r
            with self.assertRaisesRegex(h4log.LogError, "R"):
                h4log.render_block(cells)
        cells = synth.pilot_cells(14)
        cells["linux-epoll-N1us"]["R"] = 18  # an R that does not follow from its s
        with self.assertRaisesRegex(h4log.LogError, "does not follow from s"):
            h4log.render_block(cells)

    def test_s_is_written_at_full_precision(self):
        for s in (0.0125, 0.010000000000000002, 1.0 / 3.0, 0.1 + 0.2, 5e-05, 1e-07, 0.0, 12345.678):
            text = h4log.format_s(s)
            self.assertEqual(float(text), s, text)
            self.assertNotIn("e", text.lower())
            self.assertRegex(text, r"^[0-9]+(\.[0-9]+)?$")
        # A real cell: whatever s the pilot found, the block gives it back to the last bit.
        s = 0.012345678901234567
        r, power, _ = stats.pilot_choose_r(s)
        cells = synth.pilot_cells(14)
        cells["linux-epoll-N1us"].update(s=s, R=r, power=power)
        rows = h4log.parse_block(h4log.render_block(cells))
        self.assertEqual(rows["linux-epoll-N1us"]["s"], s)


class Refusals(unittest.TestCase):
    def refuse(self, text, pattern):
        with self.assertRaisesRegex(h4log.LogError, pattern):
            h4log.parse_block(text)

    # --- the markers -----------------------------------------------------------------------

    def test_no_block(self):
        self.refuse("# Revision log\n\nNone yet.\n", "no h4-r block")
        self.refuse("", "no h4-r block")
        # Markers quoted in prose are not markers.
        self.refuse("Use `<!-- h4-r:begin -->` and `<!-- h4-r:end -->`.\n", "no h4-r block")

    def test_more_than_one_block(self):
        self.refuse(good_block() + "\n" + good_block(), "more than one h4-r block")
        self.refuse(good_block() + "<!-- h4-r:begin -->\n", "more than one h4-r block")
        self.refuse("<!-- h4-r:end -->\n" + good_block(), "more than one h4-r block")

    def test_unbalanced_or_reversed_markers(self):
        self.refuse(good_block().replace("<!-- h4-r:end -->\n", ""), "unbalanced")
        self.refuse(good_block().replace("<!-- h4-r:begin -->\n", ""), "unbalanced")
        lines = lines_of(good_block())
        reversed_ = [lines[-2]] + lines[1:-2] + [lines[0], ""]
        self.refuse("\n".join(reversed_), "out of order")

    # --- the cells -----------------------------------------------------------------------------

    def test_missing_cell(self):
        self.refuse(drop_line(good_block(), "linux-epoll-N1us"), r"1 of 20 H4 cells are missing.*linux-epoll-N1us")
        self.refuse(drop_line(drop_line(good_block(), "windows-condvar-N0us"), "windows-iocp-N0us"),
                    "2 of 20 H4 cells are missing")

    def test_a_block_with_only_one_systems_cells_is_refused(self):
        text = good_block()
        for key in h4log.expected_cells():
            if key.startswith("windows-"):
                text = drop_line(text, key)
        self.refuse(text, "8 of 20 H4 cells are missing")

    def test_unknown_cell(self):
        self.refuse(replace_line(good_block(), "linux-epoll-N1us", "linux-epoll-N1us", "linux-epoll-N5us"),
                    "unknown H4 cell 'linux-epoll-N5us'")
        self.refuse(replace_line(good_block(), "linux-epoll-N1us", "linux-epoll", "linux-iocp"), "unknown H4 cell")
        # IOCP does not exist on Linux, and io_uring does not exist on Windows.
        self.refuse(replace_line(good_block(), "windows-iocp-N1us", "windows-iocp", "windows-io_uring"),
                    "unknown H4 cell")

    def test_duplicate_cell(self):
        row = [line for line in lines_of(good_block()) if line.startswith("| linux-epoll-N1us |")][0]
        text = good_block().replace("<!-- h4-r:end -->", row + "\n<!-- h4-r:end -->")
        self.refuse(text, "cell linux-epoll-N1us appears twice")

    def test_columns_must_agree_with_the_cell(self):
        key = "linux-epoll-N1us"
        self.refuse(replace_line(good_block(), key, "| linux |", "| windows |"), "do not match the cell")
        self.refuse(replace_line(good_block(), key, "| epoll |", "| condvar |"), "do not match the cell")
        self.refuse(replace_line(good_block(), key, "| 1 |", "| 10 |"), "do not match the cell")

    # --- R -----------------------------------------------------------------------------------

    def test_r_must_be_an_even_integer_from_14_to_24(self):
        key = "linux-epoll-N1us"
        for bad in ("13", "15", "12", "26", "2", "100", "0", "16.0", "+16", "-16", "sixteen", "", "1 6", "0x10"):
            text = replace_line(good_block(), key, "| 18 |", "| %s |" % bad)
            with self.assertRaisesRegex(h4log.LogError, r"even integer from 14 to 24", msg=repr(bad)):
                h4log.parse_block(text)
        for good in (14, 18, 20, 24):  # the R values the rule gives (16 and 22 never come out of it)
            rows = h4log.parse_block(h4log.render_block(synth.pilot_cells(good)))
            self.assertEqual(rows[key]["R"], good)

    def test_r_text_with_unicode_digits_is_refused(self):
        text = replace_line(good_block(), "linux-epoll-N1us", "| 18 |", "| " + chr(0x661) + chr(0x668) + " |")  # Arabic-Indic digits for 18
        self.refuse(text, "even integer from 14 to 24")

    # --- the table -------------------------------------------------------------------------------

    def test_malformed_table(self):
        good = good_block()
        self.refuse("<!-- h4-r:begin -->\n<!-- h4-r:end -->\n", "no header and separator")
        self.refuse(good.replace("| N (us) |", "| N |"), "the header is not")
        self.refuse(good.replace("| cell | system |", "| system | cell |"), "the header is not")
        self.refuse(good.replace("|---|---|---|---|---|---|---|---|---|\n", ""), "not the table's separator row")
        self.refuse(good.replace("|---|---|---|---|---|---|---|---|---|", "|---|---|---|"), "not the table's separator row")
        self.refuse(good.replace("|---|---|---|---|---|---|---|---|---|\n",
                                 "|---|---|---|---|---|---|---|---|---|\nsome prose\n"), "not a table row")
        self.refuse(good.replace("<!-- h4-r:end -->", "stray text\n<!-- h4-r:end -->"), "not a table row")
        self.refuse(replace_line(good, "linux-epoll-N1us", "| 18 | no |", "| 18 |"), "8 columns, not 9")
        self.refuse(replace_line(good, "linux-epoll-N1us", "| 18 | no |", "| 18 | no | extra |"), "10 columns, not 9")
        row = [line for line in lines_of(good) if line.startswith("| linux-epoll-N1us |")][0]
        self.refuse(good.replace(row, row[:-1]), "not a table row")  # no closing bar

    def test_bad_values(self):
        key = "linux-epoll-N1us"
        s_text = h4log.format_s(synth.S_FOR_R[18])
        power_text = "%.4f" % stats.pilot_choose_r(synth.S_FOR_R[18])[1]
        self.refuse(replace_line(good_block(), key, "| 8 |", "| x |"), "pairs 'x' is not an integer")
        self.refuse(replace_line(good_block(), key, "| %s |" % s_text, "| abc |"), "s 'abc' is not a number")
        self.refuse(replace_line(good_block(), key, "| %s |" % power_text, "| 1e-3 |"), "power '1e-3' is not a number")
        self.refuse(replace_line(good_block(), key, "| no |", "| maybe |"), "underpowered 'maybe'")
        self.refuse(replace_line(good_block(), key, "| no |", "| No |"), "underpowered 'No'")


class PilotRule(unittest.TestCase):
    """Each row must be what the pilot's rule gives for its pairs and s (section 7): R, the flag and the power."""

    def setUp(self):
        self.key = "linux-epoll-N1us"  # s = 0.0125: R 18, power 0.8584, not underpowered
        self.block = good_block()
        self.s_text = h4log.format_s(0.0125)
        self.power_text = "%.4f" % stats.pilot_choose_r(0.0125)[1]

    def refuse(self, text, pattern):
        with self.assertRaisesRegex(h4log.LogError, pattern):
            h4log.parse_block(text)

    def test_the_rule_gives_the_row(self):
        rows = h4log.parse_block(self.block)
        self.assertEqual((rows[self.key]["R"], rows[self.key]["underpowered"]), (18, False))
        self.assertEqual(self.power_text, "0.8584")

    def test_an_r_that_does_not_follow_from_s_is_refused(self):
        for wrong in ("14", "20", "24"):
            self.refuse(replace_line(self.block, self.key, "| 18 |", "| %s |" % wrong), "does not follow from s")
        # Another s, the R and the power as they were.
        self.refuse(replace_line(self.block, self.key, "| %s |" % self.s_text, "| %s |" % h4log.format_s(0.010)),
                    "does not follow from s")

    def test_the_underpowered_flag_must_follow(self):
        self.refuse(replace_line(self.block, self.key, "| no |", "| yes |"), "underpowered yes")
        # The converse: a cell the rule declares underpowered, written as not.
        self.refuse(replace_line(self.block, "windows-iocp-N100us", "| yes |", "| no |"), "underpowered no")

    def test_the_power_must_follow_at_its_written_precision(self):
        self.refuse(replace_line(self.block, self.key, "| %s |" % self.power_text, "| 0.8585 |"),
                    "power 0.8585 does not follow")
        self.refuse(replace_line(self.block, self.key, "| %s |" % self.power_text, "| 0.9000 |"), "does not follow")
        # Fewer decimals are compared with the rule, rounded to the same place.
        rows = h4log.parse_block(replace_line(self.block, self.key, "| %s |" % self.power_text, "| 0.86 |"))
        self.assertAlmostEqual(rows[self.key]["power"], 0.86)
        self.refuse(replace_line(self.block, self.key, "| %s |" % self.power_text, "| 0.87 |"), "does not follow")

    def test_fewer_than_two_pairs_have_no_s(self):
        none = "linux-condvar-N0us"  # pairs 0 in the fixture
        rows = h4log.parse_block(self.block)
        self.assertEqual((rows[none]["pairs"], rows[none]["s"], rows[none]["power"], rows[none]["R"]),
                         (0, None, None, 24))
        for pairs in ("0", "1"):
            block = replace_line(self.block, none, "| 0 | - |", "| %s | - |" % pairs)
            self.assertEqual(h4log.parse_block(block)[none]["pairs"], int(pairs))
        # An s, a power, another R or the flag off, where there is no s.
        self.refuse(replace_line(self.block, none, "| - | - | 24 | yes |", "| 0.01 | - | 24 | yes |"), "no s")
        self.refuse(replace_line(self.block, none, "| - | - | 24 | yes |", "| - | 0.5000 | 24 | yes |"), "no s")
        self.refuse(replace_line(self.block, none, "| - | - | 24 | yes |", "| - | - | 14 | yes |"), "no s")
        self.refuse(replace_line(self.block, none, "| - | - | 24 | yes |", "| - | - | 24 | no |"), "no s")

    def test_two_pairs_or_more_need_an_s(self):
        both = "| %s | %s |" % (self.s_text, self.power_text)
        self.refuse(replace_line(self.block, self.key, both, "| - | - |"), "give an s")
        self.refuse(replace_line(self.block, self.key, both, "| %s | - |" % self.s_text), "give an s")
        # Two pairs are enough to have an s.
        rows = h4log.parse_block(replace_line(self.block, self.key, "| 8 |", "| 2 |"))
        self.assertEqual(rows[self.key]["pairs"], 2)

    def test_what_the_rule_can_give(self):
        # Only R of 14, 18, 20 and 24 come out of the rule (power is not monotone in R, so 16 and 22
        # never do); anything above s = 0.016 is the cap, underpowered.
        got = {stats.pilot_choose_r(s / 10000.0)[:1] + stats.pilot_choose_r(s / 10000.0)[2:] for s in range(40, 800, 5)}
        self.assertEqual(got, {(14, False), (18, False), (20, False), (24, False), (24, True)})


class ReadLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p1v2-h4log-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, data):
        path = os.path.join(self.tmp, "revision-log-v2.md")
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_rows_and_hash_of_the_bytes_read(self):
        text = "# Log\n\n" + good_block() + "\nDone.\n"
        path = self.write(text.encode("utf-8"))
        rows, digest = h4log.read_log(path)
        self.assertEqual(len(rows), 20)
        self.assertEqual(digest, hashlib.sha256(text.encode("utf-8")).hexdigest())

    def test_crlf_hashes_as_lf(self):
        text = "# Log\n\n" + good_block() + "\nDone.\n"
        lf = hashlib.sha256(text.encode("utf-8")).hexdigest()
        rows, digest = h4log.read_log(self.write(text.replace("\n", "\r\n").encode("utf-8")))
        self.assertEqual(len(rows), 20)
        self.assertEqual(digest, lf)  # the same as `git show COMMIT:FILE | sha256sum`

    def test_unreadable_or_bad_files(self):
        with self.assertRaisesRegex(h4log.LogError, "cannot read"):
            h4log.read_log(os.path.join(self.tmp, "absent.md"))
        with self.assertRaisesRegex(h4log.LogError, "not UTF-8"):
            h4log.read_log(self.write(b"\xff\xfe\x00 not text"))
        path = self.write(b"# Log\n\nNone yet.\n")
        with self.assertRaises(h4log.LogError) as cm:
            h4log.read_log(path)
        self.assertIn(path, str(cm.exception))  # the message names the file
        self.assertIn("no h4-r block", str(cm.exception))


class Grid(unittest.TestCase):
    def test_twenty_cells(self):
        cells = h4log.expected_cells()
        self.assertEqual(len(cells), 20)
        self.assertEqual({n for _, _, n in cells.values()}, set(grid.H4_WORK_US))
        self.assertEqual(sum(1 for s, _, _ in cells.values() if s == "linux"), 12)
        self.assertEqual(sum(1 for s, _, _ in cells.values() if s == "windows"), 8)
        self.assertTrue(set(grid.f4_cells()) < set(cells))  # F4's 15 cells are the N > 0 ones


if __name__ == "__main__":
    unittest.main()

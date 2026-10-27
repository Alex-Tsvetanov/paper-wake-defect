"""The H4 R block of the revision log (hypotheses-v2.md, sections 7 and 8).

Section 8: the plan generator "reads each H4 cell's R from the revision log". The pilot writes
the block (pilot.py --markdown), a person appends it to revision-log-v2.md with its date and
reason, and plan.py reads it back. The block sits between two marker lines on lines of their
own and holds one table, one row per H4 cell, N = 0 included (20 rows, both systems):

<!-- h4-r:begin -->
| cell | system | backend | N (us) | pairs | s | power | R | underpowered |
|---|---|---|---|---|---|---|---|---|
| linux-epoll-N0us | linux | epoll | 0 | 8 | 0.01 | 0.9225 | 14 | no |
...
<!-- h4-r:end -->

s is written at full precision (the shortest decimal that reads back as the same float), so
the parser can recompute the pilot's rule from it exactly: a row whose R, whose underpowered
flag or whose power (at the precision written) is not what stats.pilot_choose_r gives for its s
is refused. A cell with fewer than 2 pilot pairs has no s: its row must read R 24,
underpowered yes, and "-" for s and power.

The parser refuses anything else with a message that says why: no block, more than one,
unbalanced markers, a cell missing or unknown or twice, an R that is not an even integer from
14 to 24, an R that does not follow from s, or a malformed table.
"""

import decimal
import hashlib
import re

import grid
import stats

BEGIN = "<!-- h4-r:begin -->"
END = "<!-- h4-r:end -->"
COLUMNS = ("cell", "system", "backend", "N (us)", "pairs", "s", "power", "R", "underpowered")
NO_VALUE = "-"  # s and power of a cell with fewer than 2 pilot pairs

_DIGITS = re.compile(r"[0-9]+")
_NUMBER = re.compile(r"[0-9]+(\.[0-9]+)?")
_SEPARATOR = re.compile(r":?-+:?")


class LogError(ValueError):
    """The revision log does not hold a usable R block."""


def expected_cells():
    """The 20 H4 cells as key -> (system, backend, work_us), in grid order."""
    return {grid.h4_key(s, b, n): (s, b, n) for s in grid.SYSTEMS for b, n in grid.h4_cells(s)}


# ---------------------------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------------------------


def _row(values):
    return "| " + " | ".join(values) + " |"


def format_s(s):
    """s as plain decimal text that reads back as the same float (no exponent, no lost digits)."""
    return format(decimal.Decimal(repr(s)), "f")


def render_block(cells):
    """The block for pilot cells (pilot.py's `cells`: key -> dict). All 20 cells are needed.

    The block is parsed before it is returned, so no block is written that plan.py would refuse.
    """
    lines = [BEGIN, _row(COLUMNS), "|" + "---|" * len(COLUMNS)]
    for key, (system, backend, work_us) in expected_cells().items():
        c = cells.get(key)
        if c is None:
            raise LogError("no pilot cell %s: the block needs all %d H4 cells" % (key, len(expected_cells())))
        lines.append(_row((
            key, system, backend, str(work_us), str(c["pairs"]),
            NO_VALUE if c["s"] is None else format_s(c["s"]),
            NO_VALUE if c["power"] is None else "%.4f" % c["power"],
            str(c["R"]), "yes" if c["underpowered"] else "no")))
    lines.append(END)
    text = "\n".join(lines) + "\n"
    parse_block(text)
    return text


# ---------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------


def _split(line, lineno):
    if not (line.startswith("|") and line.endswith("|") and len(line) > 1):
        raise LogError("line %d: not a table row: %r" % (lineno, line))
    return [c.strip() for c in line[1:-1].split("|")]


def _rule_problem(pairs, s_text, power_text, r, underpowered):
    """None when the row is what the pilot's rule (section 7) gives for its pairs and s; else why not."""
    if pairs < 2:
        # The rule needs an SD of at least 2 log ratios. Without one the cell takes the cap.
        if (s_text, power_text) != (NO_VALUE, NO_VALUE) or r != grid.H4_R_MAX or not underpowered:
            return "%d pilot pairs give no s: the row must read s %r, power %r, R %d and underpowered yes" % (
                pairs, NO_VALUE, NO_VALUE, grid.H4_R_MAX)
        return None
    if NO_VALUE in (s_text, power_text):
        return "%d pilot pairs give an s, and the row has %r for s or power" % (pairs, NO_VALUE)
    s = float(s_text)
    want_r, want_power, want_under = stats.pilot_choose_r(s)
    if r != want_r or underpowered != want_under:
        return "R %d (underpowered %s) does not follow from s %s: the pilot's rule gives R %d (underpowered %s)" % (
            r, "yes" if underpowered else "no", s_text, want_r, "yes" if want_under else "no")
    decimals = len(power_text.partition(".")[2])
    if "%.*f" % (decimals, want_power) != power_text:
        return "power %s does not follow from s %s: the pilot's rule gives %.*f" % (power_text, s_text, decimals, want_power)
    return None


def _parse_row(cells, lineno, known):
    if len(cells) != len(COLUMNS):
        raise LogError("line %d: %d columns, not %d" % (lineno, len(cells), len(COLUMNS)))
    key, system, backend, work, pairs, s, power, r_text, under = cells
    if key not in known:
        raise LogError("line %d: unknown H4 cell %r" % (lineno, key))
    want = known[key]
    if (system, backend, work) != (want[0], want[1], str(want[2])):
        raise LogError("line %d: system, backend and N (us) do not match the cell %s" % (lineno, key))
    if not _DIGITS.fullmatch(pairs):
        raise LogError("line %d: cell %s: pairs %r is not an integer" % (lineno, key, pairs))
    for name, value in (("s", s), ("power", power)):
        if value != NO_VALUE and not _NUMBER.fullmatch(value):
            raise LogError("line %d: cell %s: %s %r is not a number or %r" % (lineno, key, name, value, NO_VALUE))
    if not _DIGITS.fullmatch(r_text):
        raise LogError("line %d: cell %s: R %r is not an even integer from %d to %d" % (
            lineno, key, r_text, grid.H4_R_MIN, grid.H4_R_MAX))
    r = int(r_text)
    if r % 2 or not grid.H4_R_MIN <= r <= grid.H4_R_MAX:
        raise LogError("line %d: cell %s: R is %d, not an even integer from %d to %d" % (
            lineno, key, r, grid.H4_R_MIN, grid.H4_R_MAX))
    if under not in ("yes", "no"):
        raise LogError("line %d: cell %s: underpowered %r is not yes or no" % (lineno, key, under))
    problem = _rule_problem(int(pairs), s, power, r, under == "yes")
    if problem:
        raise LogError("line %d: cell %s: %s" % (lineno, key, problem))
    return {"cell": key, "system": system, "backend": backend, "work_us": want[2], "pairs": int(pairs),
            "s": None if s == NO_VALUE else float(s), "power": None if power == NO_VALUE else float(power),
            "R": r, "underpowered": under == "yes"}


def _parse_table(body):
    """body: (line number, stripped text) of the non-blank lines between the markers."""
    if len(body) < 2:
        raise LogError("the table has no header and separator row")
    head_no, head = body[0]
    if tuple(_split(head, head_no)) != COLUMNS:
        raise LogError("line %d: the header is not %s" % (head_no, _row(COLUMNS)))
    sep_no, sep = body[1]
    seps = _split(sep, sep_no)
    if len(seps) != len(COLUMNS) or not all(_SEPARATOR.fullmatch(c) for c in seps):
        raise LogError("line %d: the second row is not the table's separator row" % sep_no)
    known = expected_cells()
    rows = {}
    for lineno, line in body[2:]:
        row = _parse_row(_split(line, lineno), lineno, known)
        if row["cell"] in rows:
            raise LogError("line %d: cell %s appears twice" % (lineno, row["cell"]))
        rows[row["cell"]] = row
    missing = [k for k in known if k not in rows]
    if missing:
        raise LogError("%d of %d H4 cells are missing, e.g. %s" % (len(missing), len(known), missing[:3]))
    return rows


def parse_block(text):
    """The one R block of a revision log's text as key -> row; LogError on anything else.

    A marker counts only as a whole line, so prose that mentions it does not.
    """
    lines = text.split("\n")
    begins = [i for i, line in enumerate(lines) if line.strip() == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == END]
    if not begins and not ends:
        raise LogError("no h4-r block: the log needs %s, one table and %s on lines of their own" % (BEGIN, END))
    if len(begins) > 1 or len(ends) > 1:
        raise LogError("more than one h4-r block (%d begin and %d end lines): exactly one is allowed" % (
            len(begins), len(ends)))
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        raise LogError("the h4-r markers are unbalanced or out of order (begin on lines %s, end on lines %s)" % (
            [i + 1 for i in begins], [i + 1 for i in ends]))
    body = [(i + 1, lines[i].strip()) for i in range(begins[0] + 1, ends[0]) if lines[i].strip()]
    return _parse_table(body)


def read_log(path):
    """(rows, sha256 hex) of a revision log file: its R block and the hash of the bytes read.

    The file is read once. The hash covers those bytes with CRLF turned into LF, so it equals
    `git show COMMIT:FILE | sha256sum` on a checkout that converts line ends (the form the log's
    own table of frozen files uses).
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise LogError("cannot read %s: %s" % (path, e))
    lf = raw.replace(b"\r\n", b"\n")
    try:
        text = lf.decode("utf-8")
    except UnicodeDecodeError:
        raise LogError("%s is not UTF-8" % path)
    try:
        rows = parse_block(text)
    except LogError as e:
        raise LogError("%s: %s" % (path, e))
    return rows, hashlib.sha256(lf).hexdigest()

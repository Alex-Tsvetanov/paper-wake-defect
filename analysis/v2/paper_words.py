"""Paper wording and TeX formatting shared by paper_setup.py, paper_macros.py and paper_tables.py.

The paper never shows an internal label (a variant key, a cell key, a family or hypothesis label, L
or W): these maps give the words. Macro names are letters only, as TeX needs: macro_name() turns a
list of parts into CamelCase and spells the numbers that occur in them.
"""

import re

VARIANT_WORDS = {
    "none": "fixed loop",
    "plain_wake": "plain wake",
    "absent_wake": "absent wake",
    "unobserved_primitive": "unobserved primitive",
    "lazy_wake_race": "lazy-wake race",
    "lazy_wake_wide": "lazy-wake race, widened",
    "wrong_worker": "wrong worker",
    "no_retrigger": "no re-trigger",
    "undrained_edge": "undrained edge",
}
# The order in which tables list the variants: the correct loops, the defects that lose a wake on
# every post, then the rare and the batch-only ones.
VARIANT_ORDER = ("none", "plain_wake", "absent_wake", "unobserved_primitive", "wrong_worker", "no_retrigger",
                 "undrained_edge", "lazy_wake_race", "lazy_wake_wide")
BACKEND_WORDS = {"epoll": "epoll", "io_uring": r"io\_uring", "condvar": "condition variable", "iocp": "IOCP"}
SYSTEM_WORDS = {"linux": "Linux", "windows": "Windows"}
PROTOCOL_WORDS = {"S": "single posts", "Bt": "batch rounds"}
# Backend and system columns of every table, in this order.
COLUMNS = (("linux", "epoll"), ("linux", "io_uring"), ("linux", "condvar"), ("windows", "iocp"),
           ("windows", "condvar"))

_NUMBER_WORDS = {0: "Zero", 1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six", 7: "Seven",
                 8: "Eight", 9: "Nine", 10: "Ten", 14: "Fourteen", 24: "TwentyFour", 100: "Hundred",
                 1000: "Thousand"}
_NAME = re.compile(r"[A-Za-z]+")


def number_word(n):
    if n not in _NUMBER_WORDS:
        raise ValueError("no word for %r in a macro name" % (n,))
    return _NUMBER_WORDS[n]


def macro_name(*parts):
    """CamelCase of the parts; digits become words; anything else but letters is dropped."""
    out = []
    for part in parts:
        for token in re.findall(r"[A-Za-z]+|\d+", str(part)):
            out.append(number_word(int(token)) if token.isdigit() else token[:1].upper() + token[1:])
    name = "".join(out)
    if not _NAME.fullmatch(name):
        raise ValueError("bad macro name from %r" % (parts,))
    return name


def backend_key(backend):
    """The backend's part of a macro name."""
    return {"io_uring": "IoUring", "condvar": "Condvar", "iocp": "Iocp", "epoll": "Epoll"}[backend]


def bound_key(bound_us):
    return {1000: "OneMs", 10000: "TenMs", 100000: "HundredMs", 0: "Blocking"}[bound_us]


def thousands(n):
    """An integer with TeX thousands separators: 6300 -> 6{,}300."""
    s = "%d" % n
    neg = s.startswith("-")
    s = s.lstrip("-")
    groups = []
    while len(s) > 3:
        groups.insert(0, s[-3:])
        s = s[:-3]
    groups.insert(0, s)
    return ("-" if neg else "") + "{,}".join(groups)


def fixed(x, places):
    """A decimal with a fixed number of places; never a Unicode minus."""
    return "%.*f" % (places, x)


def rate_text(rate, places=3):
    """A share in [0, 1] with fixed places; a share strictly between 0 and 1 never prints as the
    bounds, so 299 of 300 reads 0.997 and never 1.000, and 1 of 1,000 never 0.000."""
    if not 0 <= rate <= 1:
        raise ValueError("not a share: %r" % (rate,))
    s = fixed(rate, places)
    if 0 < rate < 1:
        step = 10 ** -places
        if s == fixed(1, places):
            s = fixed(1 - step, places)
        elif s == fixed(0, places):
            s = fixed(step, places)
    return s


def sci(x, digits=2):
    """A small number as \\ensuremath{a \\times 10^{b}}; plain decimals from 0.01 up."""
    if x == 0:
        return "0"
    if abs(x) >= 0.01:
        return fixed(x, digits + 1)
    mant, exp = ("%.*e" % (digits - 1, x)).split("e")
    return r"\ensuremath{%s \times 10^{%d}}" % (mant, int(exp))


def interval(lo, hi, places):
    return "%s to %s" % (fixed(lo, places), fixed(hi, places))


def ms(ns, places=3):
    return fixed(ns / 1e6, places)


def us(ns, places=1):
    return fixed(ns / 1e3, places)


def cell_parts(key):
    """'linux:S-epoll-absent_wake-W1-B1ms' -> dict(system, protocol, backend, variant, workers, bound_us)."""
    m = re.fullmatch(r"(linux|windows):(S|Bt)-(epoll|io_uring|condvar|iocp)-([a-z_]+)-W(\d+)-B(\d+)ms", key)
    if not m:
        raise ValueError("not a cell key: %r" % (key,))
    system, protocol, backend, variant, w, b = m.groups()
    if variant not in VARIANT_WORDS:
        raise ValueError("unknown variant in %r" % (key,))
    return {"system": system, "protocol": protocol, "backend": backend, "variant": variant,
            "workers": int(w), "bound_us": int(b) * 1000}


def cell_words(key):
    """A cell in the paper's words."""
    c = cell_parts(key)
    workers = "%d worker%s" % (c["workers"], "" if c["workers"] == 1 else "s")
    return "%s, %s, %s, %s, %s, $B = %d$~ms" % (
        SYSTEM_WORDS[c["system"]], BACKEND_WORDS[c["backend"]], VARIANT_WORDS[c["variant"]],
        PROTOCOL_WORDS[c["protocol"]], workers, c["bound_us"] // 1000)


def h4_parts(key):
    """'linux-condvar-N10us' -> (system, backend, work_us)."""
    m = re.fullmatch(r"(linux|windows)-(epoll|io_uring|condvar|iocp)-N(\d+)us", key)
    if not m:
        raise ValueError("not an H4 key: %r" % (key,))
    return m.group(1), m.group(2), int(m.group(3))

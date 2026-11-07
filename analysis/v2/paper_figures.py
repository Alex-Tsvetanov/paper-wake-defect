"""The three figures of the journal article, as deterministic PDF files, from already scored results.

  python paper_figures.py --score SCORE.json --sweep SWEEP.csv --out-dir DIR

SCORE.json is score.py's output (results/v2/score.json); SWEEP.csv is sweep_csv.py's output, the
posts of H3's ten phase sweeps. Both are only read. Written to DIR, each file's sha256 printed:

  sawtooth.pdf   the delay of a missed wake against the closed form: one panel per sweep (five
                 system and backend pairs by two bounds), x = phase / B_eff, y = delay / B_eff,
                 each post a dot, the closed form 1 - frac(x) as a line broken at the integers.
                 The x and y ranges are shared and cover every post (a post beyond x = 3 or
                 y = 1 is drawn, not clipped).
  detection.pdf  the detection matrix of the test T_med: share of trials flagged per variant and
                 worker count (rows) against system, backend and bound (columns), for single
                 posts and for batch rounds. A combination that was not run is blank; an
                 incomplete cell is grey and says "inc.", it never shows a rate.
  fixcost.pdf    the throughput cost of the fix: for each system and backend and each per-task
                 work N (0, 1, 10, 100 microseconds) the median pair ratio of the fixed loop over
                 the absent wake with its clustered 95% interval of the mean pair ratio, against
                 the equivalence margins 0.98 and 1.02. Two panels side by side with their own x
                 ranges and row labels: the 15 confirmatory rows (N of 1, 10, 100) around 1, and
                 the 5 rows of N = 0 (hollow, outside the confirmatory family) over the full range.
                 A range grows if a point or interval would fall outside it.

The files are byte-identical for equal inputs (Agg backend, embedded TrueType fonts, no dates or
producer in the metadata). Every text is drawn with the font's ligatures off, so "fi", "ff" and "fl"
are two glyphs each. Figure text uses the paper's words, never an internal key, and neither
a dash nor the Unicode minus sign.

Refused with exit code 2, a message on stderr and nothing written: an input that cannot be read or
parsed; a sweep CSV with a missing column, a non-numeric value, a B_eff that is not positive, a
sweep count other than 10 or a sweep that is not one of the ten of the paper; a score.json without
cells or H4 rows; a cell key that does not parse (or names an unknown system, backend, variant or
bound) or disagrees with the cell's own fields; a cell without its completeness flag or, when
complete, without a rate in [0, 1]; a missing, duplicate, unknown or incomplete H4 row of the
twenty (five system and backend pairs by N of 0, 1, 10 and 100).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse  # noqa: E402
import csv  # noqa: E402
import hashlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402

import matplotlib  # noqa: E402

import paper_words as pw  # noqa: E402

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.text  # noqa: E402
from matplotlib.colors import Normalize  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch, Rectangle  # noqa: E402

SYSTEM_WORDS = {"linux": "Linux", "windows": "Windows"}
BACKEND_WORDS = {"epoll": "epoll", "io_uring": "io_uring", "condvar": "condition variable", "iocp": "IOCP"}
VARIANT_WORDS = {
    "none": "fixed loop",
    "plain_wake": "plain wake",
    "absent_wake": "absent wake",
    "unobserved_primitive": "unobserved primitive",
    "wrong_worker": "wrong worker",
    "no_retrigger": "no re-trigger",
    "undrained_edge": "undrained edge",
    "lazy_wake_race": "lazy-wake race",
    "lazy_wake_wide": "lazy-wake race, widened",
}
VARIANT_ORDER = ("none", "plain_wake", "absent_wake", "unobserved_primitive", "wrong_worker", "no_retrigger",
                 "undrained_edge", "lazy_wake_race", "lazy_wake_wide")
PROTOCOL_WORDS = {"S": "Single posts", "Bt": "Batch rounds"}
PAIRS = (("linux", "epoll"), ("linux", "io_uring"), ("linux", "condvar"), ("windows", "iocp"),
         ("windows", "condvar"))
BOUNDS_US = (1000, 10000)
WORK_US = (0, 1, 10, 100)
MARGINS = (0.98, 1.02)
OUTPUT_NAMES = ("sawtooth.pdf", "detection.pdf", "fixcost.pdf")

SWEEP_COLUMNS = ("system", "backend", "bound_us", "step", "phase_target", "index", "phase_ns", "delay_ns",
                 "residual_ns", "b_eff_ns")

CELL_KEY = re.compile(r"^(linux|windows):(S|Bt)-([a-z_]+)-([a-z_]+)-W(\d+)-B(\d+)ms$")
H4_KEY = re.compile(r"^(linux|windows)-([a-z_]+)-N(\d+)us$")

BLUE = "#0072B2"
VERMILION = "#D55E00"
GREY = "#bdbdbd"
STYLE = {
    "pdf.fonttype": 42,
    "axes.unicode_minus": False,
    "font.family": "DejaVu Sans",
    "mathtext.fontset": "dejavusans",
    "font.size": 8,
    "axes.labelsize": 8,
    "axes.titlesize": 8,
    "xtick.labelsize": 7.5,
    "ytick.labelsize": 7.5,
    "legend.fontsize": 7.5,
    "axes.linewidth": 0.6,
}
FIX_W, FIX_LEFT, FIX_A1, FIX_GAP, FIX_LABELS_R, FIX_EDGE = 6.5, 2.06, 1.5, 0.1, 1.79, 0.1
FIX_ROW, FIX_BLOCK, FIX_TOP, FIX_FOOT = 0.2, 3.5, 0.4, 1.05
EDGE_PAD_IN = 0.12  # 2026-11-07: the sawtooth figure's outer pad, so its y label is not at the page edge
FIX_MARKER, FIX_BAR_WIDTH = 3.6, 1.6
FIX_UNITS = (len(PAIRS) - 1) * FIX_BLOCK + 3
NO_METADATA = {"CreationDate": None, "ModDate": None, "Producer": None, "Creator": None}


class Refusal(Exception):
    pass


def bound_ms(bound_us):
    return bound_us // 1000


def pair_words(system, backend):
    return "%s, %s" % (SYSTEM_WORDS[system], BACKEND_WORDS[backend])


def worker_words(n):
    return "%d worker%s" % (n, "" if n == 1 else "s")


# ---------------------------------------------------------------- reading

def read_sweep(path):
    """{(system, backend, bound_us): (x, y)} with x = phase / B_eff and y = delay / B_eff per post."""
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            missing = [c for c in SWEEP_COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                raise Refusal("%s: missing column(s) %s" % (path, ", ".join(missing)))
            sweeps = {}
            for n, row in enumerate(reader, start=2):
                try:
                    key = (row["system"], row["backend"], int(row["bound_us"]))
                    phase, delay, b_eff = (float(row[c]) for c in ("phase_ns", "delay_ns", "b_eff_ns"))
                except (TypeError, ValueError):
                    raise Refusal("%s line %d: a value is not a number" % (path, n)) from None
                if not (math.isfinite(phase) and math.isfinite(delay) and math.isfinite(b_eff) and b_eff > 0):
                    raise Refusal("%s line %d: phase, delay or B_eff is not a finite positive reading" % (path, n))
                xs, ys = sweeps.setdefault(key, ([], []))
                xs.append(phase / b_eff)
                ys.append(delay / b_eff)
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        raise Refusal("%s: cannot be read as a CSV: %s" % (path, e)) from None
    expected = {(s, b, u) for s, b in PAIRS for u in BOUNDS_US}
    if len(sweeps) != len(expected):
        raise Refusal("%s: %d sweeps, expected %d" % (path, len(sweeps), len(expected)))
    unknown = sorted(set(sweeps) - expected)
    if unknown:
        raise Refusal("%s: a sweep that is not one of the paper's ten: %s" % (path, unknown[0]))
    return sweeps


def parse_cell_key(key):
    """(system, protocol, backend, variant, workers, bound_us) of a score.json cell key, or Refusal."""
    m = CELL_KEY.match(key) if isinstance(key, str) else None
    if not m:
        raise Refusal("cell key %r does not parse" % (key,))
    system, protocol, backend, variant, workers, ms = m.groups()
    bound_us = int(ms) * 1000
    if (system, backend) not in PAIRS or variant not in VARIANT_WORDS or bound_us not in BOUNDS_US:
        raise Refusal("cell key %r names an unknown system, backend, variant or bound" % key)
    return system, protocol, backend, variant, int(workers), bound_us


def read_cells(score):
    """{(protocol, variant, workers, system, backend, bound_us): rate, or None when incomplete}."""
    cells = score.get("cells")
    if not isinstance(cells, dict) or not cells:
        raise Refusal("score.json has no cells")
    out = {}
    for key in sorted(cells):
        system, protocol, backend, variant, workers, bound_us = parse_cell_key(key)
        value = cells[key]
        own = {"system": system, "protocol": protocol, "backend": backend, "variant": variant,
               "workers": workers, "bound_us": bound_us}
        for field, want in own.items():
            if not isinstance(value, dict) or value.get(field) != want:
                raise Refusal("cell %s: field %s does not agree with the key" % (key, field))
        if not isinstance(value.get("complete"), bool):
            raise Refusal("cell %s has no completeness flag" % key)
        rate = None
        if value["complete"]:
            try:
                rate = float(value["t_med"]["rate"])
            except (KeyError, TypeError, ValueError):
                raise Refusal("cell %s is complete but has no t_med rate" % key) from None
            if not 0.0 <= rate <= 1.0:
                raise Refusal("cell %s: rate %r is not in [0, 1]" % (key, rate))
        out[(protocol, variant, workers, system, backend, bound_us)] = rate
    return out


def read_fixcost(score):
    """{(system, backend, work_us): (median, lo, hi)} of the twenty H4 rows, fixed over absent."""
    try:
        rows = score["H4"]["fixed_over_absent"]
    except (KeyError, TypeError):
        raise Refusal("score.json has no H4 fixed_over_absent rows") from None
    if not isinstance(rows, list):
        raise Refusal("H4 fixed_over_absent is not a list")
    out = {}
    for row in rows:
        m = H4_KEY.match(str(row.get("key", ""))) if isinstance(row, dict) else None
        if not m:
            raise Refusal("H4 row %r does not parse" % (row.get("key") if isinstance(row, dict) else row,))
        system, backend, work = m.group(1), m.group(2), int(m.group(3))
        key = (system, backend, work)
        if (system, backend) not in PAIRS or work not in WORK_US:
            raise Refusal("H4 row %s is not one of the twenty" % row["key"])
        if key in out:
            raise Refusal("H4 row %s appears twice" % row["key"])
        if row.get("system") != system or row.get("backend") != backend or row.get("work_us") != work:
            raise Refusal("H4 row %s: system, backend or work_us disagrees with the key" % row["key"])
        if row.get("complete") is not True:
            raise Refusal("H4 row %s is not complete" % row["key"])
        try:
            lo, hi = (float(v) for v in row["clustered_95_mean_ratio"])
            median = float(row["median_ratio"])
        except (KeyError, TypeError, ValueError):
            raise Refusal("H4 row %s has no median ratio or interval" % row["key"]) from None
        if not all(math.isfinite(v) for v in (lo, hi, median)) or lo > hi:
            raise Refusal("H4 row %s: the ratio or its interval is not usable" % row["key"])
        out[key] = (median, lo, hi)
    for s, b in PAIRS:
        for n in WORK_US:
            if (s, b, n) not in out:
                raise Refusal("missing H4 row %s-%s-N%dus" % (s, b, n))
    return out


def read_score(path):
    try:
        with open(path, encoding="utf-8") as fh:
            score = json.load(fh)
    except (OSError, ValueError) as e:
        raise Refusal("%s: cannot be read as JSON: %s" % (path, e)) from None
    if not isinstance(score, dict):
        raise Refusal("%s: not a JSON object" % path)
    return read_cells(score), read_fixcost(score)


# ---------------------------------------------------------------- drawing

NO_LIGATURES = ("-liga",)  # 2026-11-07: fi, ff and fl as one glyph each read as "xed loop" in some PDF readers


def to_pdf(fig):
    buf = io.BytesIO()
    with plt.rc_context(STYLE):
        fig.canvas.draw()  # tick and colorbar labels exist only after a draw
        for text in fig.findobj(matplotlib.text.Text):
            text.set_fontfeatures(NO_LIGATURES)
        fig.savefig(buf, format="pdf", metadata=NO_METADATA)
    plt.close(fig)
    return buf.getvalue()


def closed_form_segments(x_end):
    """The teeth of delay / B_eff = 1 - frac(phase / B_eff) up to x_end: ((x0, y0), (x1, y1)) each, so no
    line joins the end of a tooth (y = 0) to the start of the next (y = 1)."""
    return [((float(k), 1.0), (float(k + 1), 0.0)) for k in range(int(math.ceil(x_end)))]


def sawtooth_pdf(sweeps):
    x_end = max(3.0, math.ceil(2 * max(max(xs) for xs, _ in sweeps.values())) / 2)
    y_end = max(1.1, math.ceil(20 * max(max(ys) for _, ys in sweeps.values())) / 20)
    with plt.rc_context(STYLE):
        fig, axes = plt.subplots(len(PAIRS), len(BOUNDS_US), figsize=(6.5, 7.0), sharex=True, sharey=True,
                                 layout="constrained")
        fig.get_layout_engine().set(w_pad=EDGE_PAD_IN, h_pad=EDGE_PAD_IN)  # no text within a few points of the edge
        for r, (system, backend) in enumerate(PAIRS):
            for c, bound_us in enumerate(BOUNDS_US):
                ax = axes[r][c]
                xs, ys = sweeps[(system, backend, bound_us)]
                for (x0, y0), (x1, y1) in closed_form_segments(x_end):
                    ax.plot([x0, x1], [y0, y1], color=VERMILION, linewidth=0.8, zorder=2)
                ax.plot(xs, ys, linestyle="none", marker="o", markersize=1.6, markeredgewidth=0, color=BLUE,
                        zorder=3)
                ax.set_title("%s, B = %d ms" % (pair_words(system, backend), bound_ms(bound_us)))
                ax.set_xlim(0, x_end)
                ax.set_ylim(0, y_end)
                ax.set_xticks(range(int(x_end) + 1))
                ax.set_yticks([0, 0.5, 1])
                ax.grid(True, linewidth=0.3, color="#dddddd")
                ax.tick_params(length=2.5, width=0.5)
        fig.supxlabel("phase / effective bound", fontsize=8)
        fig.supylabel("delay / effective bound", fontsize=8)
        handles = [Line2D([], [], linestyle="none", marker="o", markersize=3, color=BLUE),
                   Line2D([], [], color=VERMILION, linewidth=0.9)]
        fig.legend(handles, ["measured post", "closed form"], loc="outside upper center", ncol=2, frameon=False)
        return to_pdf(fig)


def detection_rows(cells, protocol):
    """[(variant, workers)] of one protocol in the paper's variant order, from the cells that exist."""
    have = {(v, w) for p, v, w, _, _, _ in cells if p == protocol}
    return [(v, w) for v in VARIANT_ORDER for w in sorted(n for u, n in have if u == v)]


def text_colour(rgba):
    r, g, b = rgba[:3]
    return "black" if 0.299 * r + 0.587 * g + 0.114 * b > 0.5 else "white"


def draw_matrix(ax, cells, protocol, rows, columns, cmap, norm):
    """One heatmap of rates. A missing combination stays blank, an incomplete cell is grey and says inc."""
    ax.set_xlim(0, len(columns))
    ax.set_ylim(len(rows), 0)
    for i, (variant, workers) in enumerate(rows):
        for j, (system, backend, bound_us) in enumerate(columns):
            key = (protocol, variant, workers, system, backend, bound_us)
            if key not in cells:
                continue
            rate = cells[key]
            if rate is None:
                face, label, ink = GREY, "inc.", "black"
            else:
                face = cmap(norm(rate))
                label, ink = pw.rate_text(rate), text_colour(face)
            ax.add_patch(Rectangle((j, i), 1, 1, facecolor=face, edgecolor="white", linewidth=0.5))
            ax.text(j + 0.5, i + 0.5, label, ha="center", va="center", fontsize=6, color=ink)
    for j in range(2, len(columns), 2):
        ax.axvline(j, color="white", linewidth=1.6)
    for i in range(1, len(rows)):
        if rows[i][0] != rows[i - 1][0]:
            ax.axhline(i, color="white", linewidth=1.4)
    one_count = len({w for _, w in rows}) == 1
    ax.set_yticks([i + 0.5 for i in range(len(rows))])
    ax.set_yticklabels([VARIANT_WORDS[v] if one_count else "%s, %s" % (VARIANT_WORDS[v], worker_words(w))
                        for v, w in rows], fontsize=6.5)
    ax.tick_params(axis="y", length=0)
    for side in ("top", "right", "bottom", "left"):
        ax.spines[side].set_visible(False)
    return one_count


def detection_pdf(cells):
    columns = [(s, b, u) for s, b in PAIRS for u in BOUNDS_US]
    protocols = [p for p in ("S", "Bt") if detection_rows(cells, p)]
    if not protocols:
        raise Refusal("score.json has no cell of protocol S or Bt")
    row_sets = [detection_rows(cells, p) for p in protocols]
    row_h, width = 0.145, 6.5
    left, right, strip, label_h, title_h, gap, foot = 1.9, 0.95, 0.12, 1.75, 0.2, 0.3, 0.6
    height = label_h + sum(title_h + row_h * len(r) for r in row_sets) + gap * (len(row_sets) - 1) + foot
    cmap, norm = plt.get_cmap("viridis"), Normalize(0.0, 1.0)
    with plt.rc_context(STYLE):
        fig = plt.figure(figsize=(width, height))
        top = height - 0.05 - label_h
        axes = []
        for k, (protocol, rows) in enumerate(zip(protocols, row_sets)):
            ax = fig.add_axes((left / width, (top - title_h - row_h * len(rows)) / height,
                               (width - left - right) / width, row_h * len(rows) / height))
            one_count = draw_matrix(ax, cells, protocol, rows, columns, cmap, norm)
            words = PROTOCOL_WORDS[protocol] + (", %s" % worker_words(rows[0][1]) if one_count else "")
            fig.text(0.02, (top - title_h + 0.04) / height, words, ha="left", va="bottom", fontsize=8,
                     fontweight="bold")
            ax.set_xticks([j + 0.5 for j in range(len(columns))])
            if k == 0:
                ax.xaxis.tick_top()
                ax.set_xticklabels(["%s, %d ms" % (pair_words(s, b), bound_ms(u)) for s, b, u in columns],
                                   rotation=90, fontsize=6.5)
            else:
                ax.set_xticklabels(["%d ms" % bound_ms(u) for _, _, u in columns], fontsize=6.5)
            ax.tick_params(axis="x", length=0, pad=2)
            axes.append(ax)
            top -= title_h + row_h * len(rows) + gap
        y0, y1 = axes[-1].get_position().y0, axes[0].get_position().y1
        cax = fig.add_axes(((width - right + 0.15) / width, y0, strip / width, y1 - y0))
        bar = fig.colorbar(plt.cm.ScalarMappable(norm=norm, cmap=cmap), cax=cax, ticks=[0, 0.2, 0.4, 0.6, 0.8, 1])
        bar.set_label("share of trials flagged", fontsize=8)
        bar.ax.tick_params(labelsize=7, length=2, width=0.5)
        bar.outline.set_linewidth(0.4)
        keys = [Patch(facecolor=GREY, edgecolor="none"), Patch(facecolor="white", edgecolor=GREY, linewidth=0.6)]
        fig.legend(keys, ["inc.: incomplete cell, no rate shown", "blank: combination not run"],
                   loc="lower left", bbox_to_anchor=(0.02, 0.005), ncol=2, frameon=False, fontsize=7)
        return to_pdf(fig)


def draw_fix_panel(ax, rows, keys, ys, labels, x_lim, ticks, tick_format):
    """One panel of fixcost: a bar (clustered interval) and a marker (median) per key, reference lines at the
    margins and at 1, the row labels on its own left side."""
    for i in range(0, len(PAIRS), 2):
        top = i * FIX_BLOCK
        ax.axhspan(top - 0.5, top + FIX_BLOCK - 1.0, color="#f1f1f1", linewidth=0, zorder=0)
    ax.axvline(1.0, color="#555555", linewidth=0.6, zorder=1)
    for m in MARGINS:
        ax.axvline(m, color="#555555", linewidth=0.6, linestyle=(0, (3, 2)), zorder=1)
    for key, y in zip(keys, ys):
        median, lo, hi = rows[key]
        ax.plot([lo, hi], [y, y], color=BLUE, linewidth=FIX_BAR_WIDTH, solid_capstyle="butt", zorder=2,
                gid="interval")
        ax.plot([lo, hi], [y, y], linestyle="none", marker="|", markersize=6, markeredgewidth=1.0, color=BLUE,
                zorder=2)
        ax.plot([median], [y], linestyle="none", marker="o", markersize=FIX_MARKER, markeredgewidth=0.7,
                color=BLUE, markerfacecolor=BLUE if key[2] != 0 else "white", zorder=3, gid="median")
    ax.set_xlim(*x_lim)
    ax.set_ylim(FIX_UNITS - 0.5, -0.5)
    ax.set_yticks(ys)
    ax.set_yticklabels(labels, fontsize=6.5)
    ax.set_xticks(ticks)
    ax.set_xticklabels([tick_format % t for t in ticks])
    ax.tick_params(length=2.5, width=0.5, pad=2)
    ax.grid(True, axis="x", linewidth=0.3, color="#dddddd")
    ax.set_axisbelow(True)


def fixcost_figure(rows):
    """Two panels side by side, each with its own x range and its own row labels: the 15 confirmatory rows
    (N of 1, 10, 100) where the margins and the interval widths show, and the 5 rows of N = 0 over the full
    range. Both span the same height, one block of rows per system and backend. Returned open."""
    left_keys = [(s, b, n) for s, b in PAIRS for n in WORK_US if n]
    right_keys = [(s, b, 0) for s, b in PAIRS]
    left_ys = [i * FIX_BLOCK + j for i in range(len(PAIRS)) for j in range(len(WORK_US) - 1)]
    right_ys = [i * FIX_BLOCK + 1.0 for i in range(len(PAIRS))]
    unit = r"$\mu$s"
    left_labels = ["%s, N = %d %s" % (pair_words(s, b), n, unit) for s, b, n in left_keys]
    right_labels = ["%s, N = 0 %s" % (pair_words(s, b), unit) for s, b in PAIRS]
    left_x = (min(MARGINS[0] - 0.005, min(rows[k][1] for k in left_keys) - 0.005),
              max(MARGINS[1] + 0.005, max(rows[k][2] for k in left_keys) + 0.005))
    right_x = (max(0.0, math.floor(20 * (min(rows[k][1] for k in right_keys) - 0.03)) / 20),
               max(MARGINS[1], max(rows[k][2] for k in right_keys)) + 0.03)
    h = FIX_ROW * FIX_UNITS
    height = FIX_TOP + h + FIX_FOOT
    x_right = FIX_LEFT + FIX_A1 + FIX_GAP + FIX_LABELS_R
    with plt.rc_context(STYLE):
        fig = plt.figure(figsize=(FIX_W, height))
        frame = [(FIX_LEFT, FIX_A1), (x_right, FIX_W - FIX_EDGE - x_right)]
        axes = [fig.add_axes((x0 / FIX_W, FIX_FOOT / height, w / FIX_W, h / height)) for x0, w in frame]
        draw_fix_panel(axes[0], rows, left_keys, left_ys, left_labels, left_x, [0.98, 0.99, 1.0, 1.01, 1.02], "%.2f")
        draw_fix_panel(axes[1], rows, right_keys, right_ys, right_labels, right_x, [0.0, 0.5, 1.0], "%.1f")
        axes[0].set_title(r"confirmatory (N = 1, 10, 100 %s)" % unit, loc="left", fontsize=7.5)
        axes[1].set_title("N = 0,\noutside the family", loc="left", fontsize=7.5)
        fig.text((FIX_LEFT + FIX_W - FIX_EDGE) / 2 / FIX_W, (FIX_FOOT - 0.34) / height,
                 "throughput ratio, fixed loop over absent wake", ha="center", va="top", fontsize=8)
        handles = [Line2D([], [], linestyle="none", marker="o", markersize=FIX_MARKER, color=BLUE),
                   Line2D([], [], linestyle="none", marker="o", markersize=FIX_MARKER, markeredgewidth=0.7,
                          color=BLUE, markerfacecolor="white"),
                   Line2D([], [], color=BLUE, linewidth=FIX_BAR_WIDTH, marker="|", markersize=6,
                          markeredgewidth=1.0),
                   Line2D([], [], color="#555555", linewidth=0.6, linestyle=(0, (3, 2)))]
        names = ["median pair ratio, confirmatory (N = 1, 10, 100)", "median pair ratio, N = 0, outside the family",
                 "clustered 95% interval of the mean pair ratio", "equivalence margins, 0.98 and 1.02"]
        fig.legend(handles, names, loc="lower center", ncol=2, bbox_to_anchor=(0.5, 0.0), frameon=False,
                   fontsize=7.5, columnspacing=1.5)
        return fig


def fixcost_pdf(rows):
    return to_pdf(fixcost_figure(rows))


# ---------------------------------------------------------------- command line

def make_all(score_path, sweep_path):
    """The three PDFs as bytes, nothing written; every input checked before the first is drawn."""
    cells, fix = read_score(score_path)
    sweeps = read_sweep(sweep_path)
    return {"sawtooth.pdf": sawtooth_pdf(sweeps), "detection.pdf": detection_pdf(cells),
            "fixcost.pdf": fixcost_pdf(fix)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="The three figures of the article as deterministic PDFs.")
    ap.add_argument("--score", required=True, help="results/v2/score.json")
    ap.add_argument("--sweep", required=True, help="the sweep CSV of sweep_csv.py")
    ap.add_argument("--out-dir", required=True, help="folder for sawtooth.pdf, detection.pdf and fixcost.pdf")
    args = ap.parse_args(argv)
    try:
        files = make_all(args.score, args.sweep)
    except Refusal as e:
        print("paper_figures: refused: %s" % e, file=sys.stderr)
        return 2
    try:
        os.makedirs(args.out_dir, exist_ok=True)
        for name in OUTPUT_NAMES:
            with open(os.path.join(args.out_dir, name), "wb") as fh:
                fh.write(files[name])
    except OSError as e:
        print("paper_figures: cannot write to %s: %s" % (args.out_dir, e), file=sys.stderr)
        return 1
    for name in OUTPUT_NAMES:
        print("%s  %s" % (hashlib.sha256(files[name]).hexdigest(), os.path.join(args.out_dir, name)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

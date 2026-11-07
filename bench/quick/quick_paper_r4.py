# Copyright 2026 Alex Tsvetanov
# SPDX-License-Identifier: Apache-2.0
"""Rendering of P1's quick run R4 (quick-plan-r4.md, decision d013 option H) for quick_paper.py: macros, four table
bodies, one figure and the report lines.

Nothing is computed from data here beyond sums, minima, maxima and formatting of quick_analyse.r4's values. The macros
come after every R1 to R3 macro, behind one comment line, so the R1 to R3 part of macros.tex stays byte for byte as
before. Every table body has one field per column (the writer's request 9), and the flag columns print "x of y".
"""
from __future__ import annotations

import quick_paper as P
from quick_paper import pw

BASE = "QuickRFour"
VARIANT_KEY = {"none": "Fixed", "absent_wake": "Absent", "unobserved_primitive": "Unobserved", "wrong_worker": "Wrong"}
MARK = "---- R4: quick-plan-r4.md (decision d013 option H), Windows, B = 1 ms, timer-period gaps"
NO_CALIBRATION = "no calibration posts"


def jobs_r4(jobs: dict) -> list[dict]:
    return P.by_os(jobs, "R4")


def ordered(cells: dict) -> list[tuple]:
    return sorted(cells.items(), key=lambda kv: (pw.VARIANT_ORDER.index(kv[0][0]), kv[0][1]))


def cell_key(variant: str, workers: int) -> str:
    return VARIANT_KEY[variant] + "W" + pw.number_word(workers)


def of(x: int, n: int) -> str:
    return f"{x} of {n}"


# ------------------------------------------------------------------------------------------------ macros

def common(m: P.Macros, j: dict, base: str) -> None:
    m.add(base + "Rounds", j["rounds"])
    m.add(base + "Processes", pw.thousands(j["processes"]))
    m.add(base + "TrialsPerProcess", j["trials_per_process"])
    m.add(base + "WallSeconds", round(j["wall_s"]))
    m.add(base + "Lost", j["lost"]["lost"])
    m.add(base + "Timeouts", j["lost"]["timeouts"])
    m.add(base + "Commit", "\\texttt{%s}" % j["commit"][:7])
    m.add(base + "TimerOffDefault", len(j["timer_off_default"]))
    m.add(base + "TimerChanged", len(j["timer_changed"]))
    if j["coarsest_timer_us"] is not None:
        m.add(base + "CoarsestTimerMs", pw.ms(j["coarsest_timer_us"] * 1000))


def backend_macros(m: P.Macros, k: str, r: dict) -> None:
    rule, idle = r["planted_rule"], r["idle"]
    m.add(k + "BoundMs", P.bwords(r["bound_us"]))
    m.add(k + "BeffMs", pw.ms(rule["b_eff_ns"]))
    m.add(k + "BeffSource", P.SOURCE_WORDS[rule["source"]])
    m.add(k + "Calibrated", rule["calibrated"])
    m.add(k + "CalibrationProcesses", rule["processes"])
    m.add(k + "MinCalibrated", rule["min_calibrated"])
    m.add(k + "SpanPct", P.pct(rule["span_over_median"]))
    m.add(k + "BeffIdleMs", pw.ms(idle["median"]))
    m.add(k + "BeffIdleMinMs", pw.ms(idle["min"]))
    m.add(k + "BeffIdleMaxMs", pw.ms(idle["max"]))


def cell_macros(m: P.Macros, k: str, c: dict) -> None:
    pl, idl, d = c["planted"], c["idle"], c["planted"]["median_delay_ns"]
    m.add(k + "Processes", pl["processes"])
    m.add(k + "Trials", pl["trials"])
    m.add(k + "PlantedTmed", pl["t_med_trials"])
    m.add(k + "PlantedTcount", pl["t_count_trials"])
    m.add(k + "IdleTmed", idl["t_med_trials"])
    m.add(k + "IdleTcount", idl["t_count_trials"])
    m.add(k + "TmedShareMin", P.share(pl["t_med_share"]["min"]))
    m.add(k + "TmedShareMax", P.share(pl["t_med_share"]["max"]))
    for x, n in (("median", ""), ("min", "Min"), ("max", "Max"), ("q1", "QOne"), ("q3", "QThree")):
        m.add(k + "Delay" + n + "Ms", pw.ms(d[x]))
    m.add(k + "OwnIdleMs", pw.ms(c["own_idle"]["median"]))
    m.add(k + "GapUnitOff", len(c["gap_unit"]["over_10pct"]))
    m.add(k + "Lost", c["lost"]["lost"])
    if not c["delaying"]:
        return
    m.add(k + "OwnBeffMs", pw.ms(c["own_beff"]["median"]))
    m.add(k + "OwnCalibrated", c["own_calibrated"])
    ph = c["phase"]
    m.add(k + "PhasePosts", pw.thousands(ph["posts"]))
    m.add(k + "PhaseWithout", ph["without_phase"])
    m.add(k + "PhaseMinTenth", P.share(min(ph["tenths"])))
    m.add(k + "PhaseMaxTenth", P.share(max(ph["tenths"])))
    m.add(k + "PhaseFirstTenth", P.share(ph["tenths"][0]))
    m.add(k + "PhaseOverPeriod", P.share(ph["over_one_period"]))


def summary_macros(m: P.Macros, base: str, j: dict) -> None:
    cells = [c for r in j["rows"].values() for c in r["cells"].values()]
    delaying = [c for c in cells if c["delaying"]]
    fixed = [c for c in cells if not c["delaying"]]
    m.add(base + "Cells", len(cells))
    m.add(base + "DelayingCells", len(delaying))
    m.add(base + "FixedCells", len(fixed))
    for name, cs in (("Delaying", delaying), ("Fixed", fixed)):
        m.add(base + name + "Trials", pw.thousands(sum(c["planted"]["trials"] for c in cs)))
        for line, lk in (("planted", "Planted"), ("idle", "Idle")):
            m.add(base + name + lk + "Tmed", pw.thousands(sum(c[line]["t_med_trials"] for c in cs)))
            m.add(base + name + lk + "Tcount", pw.thousands(sum(c[line]["t_count_trials"] for c in cs)))
        med = [c["planted"]["median_delay_ns"]["median"] for c in cs]
        m.add(base + name + "DelayMinMs", pw.ms(min(med)))
        m.add(base + name + "DelayMaxMs", pw.ms(max(med)))
    m.add(base + "DelayingFullCells", sum(c["planted"]["t_med_trials"] == c["planted"]["trials"] for c in delaying))
    tenths = [t for c in delaying for t in c["phase"]["tenths"]]
    m.add(base + "PhaseMinTenth", P.share(min(tenths)))
    m.add(base + "PhaseMaxTenth", P.share(max(tenths)))
    m.add(base + "PhaseWithout", sum(c["phase"]["without_phase"] for c in delaying))
    own = [c["own_beff"]["median"] for c in delaying]
    m.add(base + "OwnBeffMinMs", pw.ms(min(own)))
    m.add(base + "OwnBeffMaxMs", pw.ms(max(own)))
    idle = [c["own_idle"]["median"] for c in cells]
    m.add(base + "OwnIdleMinMs", pw.ms(min(idle)))
    m.add(base + "OwnIdleMaxMs", pw.ms(max(idle)))
    m.add(base + "GapUnitOff", sum(len(c["gap_unit"]["over_10pct"]) for c in cells))
    units = sorted({u for c in cells for u in c["gap_unit"]["gap_unit_us"]})
    if len(units) == 1:
        m.add(base + "GapUnitMs", pw.ms(units[0] * 1000))
    m.add(base + "BeffMeasured", sum(r["planted_rule"]["source"] == "measured" for r in j["rows"].values()))
    m.add(base + "Backends", len(j["rows"]))


def macros(m: P.Macros, jobs: dict) -> None:
    js = jobs_r4(jobs)
    if js:
        m.comment(MARK)
    for j in js:
        base = BASE + P.SYS[j["os"]]
        common(m, j, base)
        summary_macros(m, base, j)
        for backend, r in sorted(j["rows"].items()):
            k = base + pw.backend_key(backend)
            backend_macros(m, k, r)
            for (variant, workers), c in ordered(r["cells"]):
                cell_macros(m, k + cell_key(variant, workers), c)


# ------------------------------------------------------------------------------------------------ tables

def cells_table(jobs: dict) -> str:
    rows = []
    for j in jobs_r4(jobs):
        for backend, r in sorted(j["rows"].items()):
            for (variant, workers), c in ordered(r["cells"]):
                pl, idl, d = c["planted"], c["idle"], c["planted"]["median_delay_ns"]
                rows.append(" & ".join([
                    pw.BACKEND_WORDS[backend], P.loop_words(variant), str(workers), str(pl["processes"]),
                    of(pl["t_med_trials"], pl["trials"]), of(pl["t_count_trials"], pl["trials"]),
                    of(idl["t_med_trials"], idl["trials"]), of(idl["t_count_trials"], idl["trials"]),
                    pw.ms(d["median"]), pw.ms(d["min"]), pw.ms(d["max"])]))
    return P.table("backend (Windows, B = 1 ms); loop; workers; processes; trials flagged by the median test with the "
                   "first B_eff line (the unobserved-primitive, one-worker cell); trials flagged by the count test with "
                   "it; trials flagged by the median test with the second line (the fixed loop's idle timed returns); "
                   "the same for the count test; median delay of a process (ms), median over the processes; smallest; "
                   "largest", rows)


def beff_table(jobs: dict) -> str:
    rows = []
    for j in jobs_r4(jobs):
        for backend, r in sorted(j["rows"].items()):
            rule, idle = r["planted_rule"], r["idle"]
            rows.append(" & ".join([
                pw.BACKEND_WORDS[backend], P.bwords(r["bound_us"]), str(rule["processes"]), str(rule["calibrated"]),
                str(rule["min_calibrated"]), pw.ms(rule["b_eff_ns"]), P.pct(rule["span_over_median"]),
                P.SOURCE_WORDS[rule["source"]], pw.ms(idle["median"]), pw.ms(idle["min"]), pw.ms(idle["max"])]))
    return P.table("backend (Windows); B (ms); processes of the unobserved-primitive, one-worker cell; of them, the "
                   "processes that calibrated; the count they had to reach; first B_eff line (ms); span of their values "
                   "over their median (percent); source; second line, the fixed loop's idle B_eff with four workers "
                   "(ms), median over its processes; smallest; largest", rows)


def own_table(jobs: dict) -> str:
    rows = []
    for j in jobs_r4(jobs):
        for backend, r in sorted(j["rows"].items()):
            for (variant, workers), c in ordered(r["cells"]):
                own = (pw.ms(c["own_beff"]["median"]), str(c["own_calibrated"])) if c["delaying"] \
                    else (NO_CALIBRATION, "0")
                rows.append(" & ".join([pw.BACKEND_WORDS[backend], P.loop_words(variant), str(workers), *own,
                                        pw.ms(c["own_idle"]["median"]), str(len(c["gap_unit"]["over_10pct"]))]))
    return P.table("backend (Windows, B = 1 ms); loop; workers; the cell's own B_eff from its calibration posts (ms), "
                   "median over its processes; processes that calibrated; the cell's own idle B_eff (ms), median over "
                   "its processes; processes whose gap unit differs from their own B_eff by more than 10 percent", rows)


def phase_table(jobs: dict) -> str:
    rows = []
    for j in jobs_r4(jobs):
        for backend, r in sorted(j["rows"].items()):
            for (variant, workers), c in ordered(r["cells"]):
                if not c["delaying"]:
                    continue
                ph = c["phase"]
                rows.append(" & ".join([pw.BACKEND_WORDS[backend], P.loop_words(variant), str(workers),
                                        pw.ms(r["planted_rule"]["b_eff_ns"]), pw.thousands(ph["posts"])]
                                       + [P.share(x) for x in ph["tenths"]] + [P.share(ph["over_one_period"])]))
    return P.table("backend (Windows, B = 1 ms); loop; workers; B_eff, first line (ms); measured posts with a phase; "
                   "share of those posts whose phase, reduced modulo B_eff, falls in each tenth of B_eff, first tenth "
                   "to tenth tenth; share whose phase exceeds one B_eff before the reduction", rows)


# ------------------------------------------------------------------------------------------------ figure, report

def phase_figure(jobs: dict) -> bytes:
    import paper_figures as pf  # noqa: E402  (style, ligatures off, no metadata)
    import matplotlib.pyplot as plt  # noqa: E402

    panels = [(backend, variant, workers, c) for j in jobs_r4(jobs) for backend, r in sorted(j["rows"].items())
              for (variant, workers), c in ordered(r["cells"]) if c["delaying"]]
    backends = sorted({p[0] for p in panels})
    per_row = len(panels) // len(backends)
    with plt.rc_context(pf.STYLE):
        fig, grid = plt.subplots(len(backends), per_row, figsize=(6.5, 4.0), sharex=True, sharey=True,
                                 constrained_layout=True, squeeze=False)
        for ax, (backend, variant, workers, c) in zip(grid.flat, panels):
            x = [i + 0.5 for i in range(len(c["phase"]["tenths"]))]
            ax.bar(x, c["phase"]["tenths"], width=0.85, color=pf.BLUE)
            ax.axhline(0.1, color=pf.GREY, linewidth=0.8)
            ax.set_xlim(0, 10)
            ax.set_xticks([0, 5, 10])
            ax.set_xticklabels(["0", "0.5", "1"])
            word = "worker" if workers == 1 else "workers"
            ax.set_title(f"{pf.BACKEND_WORDS[backend]}\n{pf.VARIANT_WORDS[variant]}\n{workers} {word}", fontsize=6)
        fig.supxlabel("Phase of the post within the effective bound (share of the bound)")
        fig.supylabel("Share of posts")
        return pf.to_pdf(fig)


def report_lines(j: dict) -> list[str]:
    lines = []
    for backend, r in sorted(j["rows"].items()):
        rule = r["planted_rule"]
        lines.append(f"  {backend}: B_eff {rule['b_eff_ns'] / 1e6:.4f} ms ({rule['source']}, {rule['calibrated']} of "
                     f"{rule['processes']}, threshold {rule['min_calibrated']}, span {rule['span_over_median']:.4f}), "
                     f"idle {r['idle']['median'] / 1e6:.4f} ms")
        for (variant, workers), c in ordered(r["cells"]):
            pl, idl = c["planted"], c["idle"]
            extra = ""
            if c["delaying"]:
                extra = (f", own {c['own_beff']['median'] / 1e6:.4f} ms, phase tenths "
                         f"{[round(x, 3) for x in c['phase']['tenths']]}, over one period "
                         f"{c['phase']['over_one_period']:.3f}")
            lines.append(f"    {variant} W{workers}: T_med {pl['t_med_trials']}/{pl['trials']}, T_count "
                         f"{pl['t_count_trials']}; idle line {idl['t_med_trials']}, {idl['t_count_trials']}; median "
                         f"delay {pl['median_delay_ns']['median'] / 1e6:.3f} ms{extra}")
    return lines


def render(jobs: dict) -> dict:
    if not jobs_r4(jobs):
        return {}
    return {"tables/quick-r4.tex": cells_table(jobs), "tables/quick-r4-beff.tex": beff_table(jobs),
            "tables/quick-r4-own.tex": own_table(jobs), "tables/quick-r4-phase.tex": phase_table(jobs),
            "figures/quick-r4-phase.pdf": phase_figure(jobs)}

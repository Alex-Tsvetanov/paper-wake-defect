r"""Check the prose of P1 for typed digits and for en or em dashes.

    python paper/check_prose.py            # sources only
    python paper/check_prose.py --outputs  # also paper/main.pdf and the Word copy

Digits. Every number in the prose must come from a macro of results/rework/macros.tex. The
check reads main.tex between \begin{document} and \end{document} and the text fields of
fig-sawtooth.tex (node labels, legend entries, axis labels), drops comments, and removes what
is not prose: macro names, the arguments of \cite, \ref, \label, \input, \url, \href (target),
\bibliography, \bibliographystyle, \bstctlcite, \setcounter, \pagenumbering, \thispagestyle,
\fontsize, \renewcommand, \fancyfoot, a tabular column specification, and the running header
\chead, which the template supplies. Any digit left is reported with its line, except these
names, which are listed with their counts:
    H1 to H5            the names of the pre-registered hypotheses
    epoll_pwait2        a system call name
    \textsuperscript{1} the template's affiliation mark after the author's name
Dashes. U+2013 and U+2014, LaTeX's -- and ---, and \textendash or \textemdash are reported
in main.tex, fig-sawtooth.tex and refs.bib (outside comments); with --outputs, U+2013 and
U+2014 in the text of paper/main.pdf and of the Word copy.
Exit status 1 if anything is reported.
"""
import argparse
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DASHES = {"\u2013": "U+2013 en dash", "\u2014": "U+2014 em dash"}
ALLOWED = [(re.compile(r"\bH[1-5]\b"), "hypothesis name H1 to H5"),
           (re.compile(r"epoll_pwait2"), "system call epoll_pwait2"),
           (re.compile(r"\\textsuperscript\{1\}"), "affiliation mark")]
# Commands whose arguments are not prose: name -> number of mandatory arguments removed.
DROP_ARGS = {"cite": 1, "ref": 1, "label": 1, "input": 1, "url": 1, "bibliography": 1,
             "bibliographystyle": 1, "bstctlcite": 1, "setcounter": 2, "pagenumbering": 1,
             "thispagestyle": 1, "fontsize": 2, "renewcommand": 2, "chead": 1, "fancyfoot": 1}


def read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def blank(text, start, end):
    """Replace text[start:end] by its newlines only, so line numbers survive."""
    return text[:start] + "\n" * text.count("\n", start, end) + text[end:]


def strip_comments(text):
    return re.sub(r"(?<!\\)%[^\n]*", "", text)


def group_end(text, i):
    """text[i] is '{' or '['; the index after the matching close."""
    close = "}" if text[i] == "{" else "]"
    depth = 0
    for j in range(i, len(text)):
        if text[j] == text[i]:
            depth += 1
        elif text[j] == close:
            depth -= 1
            if depth == 0:
                return j + 1
    raise SystemExit(f"unbalanced group at offset {i}")


def skip_space(text, i):
    while i < len(text) and text[i] in " \t\n":
        i += 1
    return i


def drop_arguments(text):
    for name, count in DROP_ARGS.items():
        pos = 0
        while (m := re.compile(r"\\" + name + r"(?![A-Za-z])").search(text, pos)):
            i = skip_space(text, m.end())
            while i < len(text) and text[i] == "[":
                i = skip_space(text, group_end(text, i))
            for _ in range(count):
                i = skip_space(text, i)
                if i < len(text) and text[i] == "{":
                    i = group_end(text, i)
            text = blank(text, m.start(), i)
            pos = m.start()
    pos = 0  # \href{target}{text}: the target goes, the text stays
    while (m := re.compile(r"\\href\s*\{").search(text, pos)):
        end = group_end(text, m.end() - 1)
        text = blank(text, m.start(), end)
        pos = m.start()
    pos = 0  # \begin{tabular}{spec}
    while (m := re.compile(r"\\begin\{tabular\}\s*\{").search(text, pos)):
        end = group_end(text, m.end() - 1)
        text = text[:m.start()] + "\\begin{tabular}" + blank(text, m.start(), end)[m.start():]
        pos = m.start() + len("\\begin{tabular}")
    return text


def prose_digits(text, fname, first_line):
    text = drop_arguments(strip_comments(text))
    text = text.replace("\\allowbreak{}", "").replace("\\_", "_")
    allowed_counts, problems = {}, []
    for pat, label in ALLOWED:
        allowed_counts[label] = len(pat.findall(text))
        text = pat.sub(lambda m: re.sub(r"\d", "#", m.group(0)), text)
    text = re.sub(r"\\[A-Za-z@]+\*?", " ", text)  # macro names (the results macros included)
    for n, line in enumerate(text.split("\n"), start=first_line):
        for m in re.finditer(r"\d+", line):
            problems.append(f"{fname}:{n}: typed digit '{m.group(0)}' in: {line.strip()[:90]}")
    return allowed_counts, problems


def figure_text(src):
    """Label, legend and node text of the figure, one string per field, with its line."""
    src = strip_comments(src)
    out = []
    for pat in (r"\\addlegendentry\s*\{", r"(?:xlabel|ylabel)\s*=\s*\{", r"\\node\[[^\]]*\]\s*at\s*\([^)]*\)\s*\{"):
        for m in re.finditer(pat, src):
            end = group_end(src, m.end() - 1)
            out.append((src.count("\n", 0, m.start()) + 1, src[m.end():end - 1]))
    return out


def dash_problems(text, fname):
    problems = []
    for n, line in enumerate(text.split("\n"), start=1):
        code = re.sub(r"(?<!\\)%.*", "", line)
        for ch, name in DASHES.items():
            if ch in line:
                problems.append(f"{fname}:{n}: {name}")
        if re.search(r"(?<!-)--(?!-)|---", code) or re.search(r"\\text(en|em)dash", code):
            problems.append(f"{fname}:{n}: LaTeX dash in: {code.strip()[:90]}")
    return problems


def pdf_text(path):
    import fitz  # PyMuPDF
    with fitz.open(path) as d:
        return "".join(p.get_text() for p in d)


def docx_text(path):
    with zipfile.ZipFile(path) as z:
        pat = r"word/(document|header\d*|footer\d*|footnotes)\.xml"
        return "".join(re.sub(r"<[^>]+>", "", z.read(n).decode("utf-8"))
                       for n in z.namelist() if re.match(pat, n))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--outputs", action="store_true", help="also check main.pdf and the .docx")
    args = ap.parse_args()
    main_tex = read("main.tex")
    begin = main_tex.index("\\begin{document}") + len("\\begin{document}")
    end = main_tex.index("\\end{document}")
    counts, problems = prose_digits(main_tex[begin:end], "main.tex", main_tex.count("\n", 0, begin) + 1)
    for line, field in figure_text(read("fig-sawtooth.tex")):
        _, more = prose_digits(field, "fig-sawtooth.tex", line)
        problems += more
    for name in ("main.tex", "fig-sawtooth.tex"):
        problems += dash_problems(read(name), name)
    bib = read("refs.bib")  # comments and text outside entries are not typeset
    problems += dash_problems("\n".join(l if not l.lstrip().startswith("%") else "" for l in bib.split("\n")),
                              "refs.bib")
    if args.outputs:
        for label, path, reader in (("main.pdf", os.path.join(HERE, "main.pdf"), pdf_text),
                                    ("Word copy", os.path.join(HERE, "Tsvetanov-wake-TELECOM2026.docx"), docx_text)):
            text = reader(path)
            for ch, name in DASHES.items():
                if ch in text:
                    problems.append(f"{label}: {text.count(ch)} x {name}")
            print(f"{label}: {sum(text.count(ch) for ch in DASHES)} en or em dashes")
    for label, n in counts.items():
        print(f"allowed: {n} x {label}")
    for p in problems:
        print(p)
    print(f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

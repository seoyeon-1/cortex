"""Phase 7.3c - latex_generator: results JSON -> arXiv-ish tech-report draft (.tex).

Escapes everything, keeps one column table of related work + metrics table of experiments.
Compilable by any pdflatex; the loop does not require LaTeX installed to produce the draft.
"""
from pathlib import Path
from typing import Dict


def _esc(s: str) -> str:
    for a, b in [("&", r"\&"), ("%", r"\%"), ("$", r"\$"), ("#", r"\#"), ("_", r"\_"),
                 ("{", r"\{"), ("}", r"\}"), ("~", r"\textasciitilde{}")]:
        s = s.replace(a, b)
    return s


def render(title: str, papers: list, experiments: list, hypothesis: str = "") -> str:
    rel = "\n".join(
        f"  \\item \\textbf{{\\texttt{{{_esc(p['id'])}}}}} -- {_esc(p['title'])}"
        + (f" \\footnote{{\\url{{{_esc(p['url'])}}}}}" if p.get("url") else "")
        for p in papers[:8]) or "  \\item (offline fixture: no related work fetched)"
    rows = "\n".join(
        f"    {_esc(str(e.get('name', '?')))} & {e.get('metric', '-')} & {e.get('baseline', '-')} & {e.get('result', '-')} \\\\"
        for e in experiments) or "    (no experiments logged yet) & -- & -- & -- \\\\"
    hyp = f"\\paragraph{{Hypothesis}} {_esc(hypothesis)}" if hypothesis else ""
    return f"""\\documentclass{{article}}
\\usepackage{{hyperref}}
\\usepackage{{booktabs}}
\\title{{{_esc(title)}}}
\\author{{Cortex Research Agent (draft -- human review required)}}
\\begin{{document}}
\\maketitle
\\begin{{abstract}}
Automated draft produced by cortex/research: literature crawl, hypothesis mining and
experiment results, without claims of statistical significance.
\\end{{abstract}}
\\section{{Related work}}
\\begin{{itemize}}
{rel}
\\end{{itemize}}
\\section{{Method}}
{hyp}
\\section{{Results}}
\\begin{{tabular}}{{llll}}
\\toprule
Experiment & Metric & Baseline & Result \\\\
\\midrule
{rows}
\\bottomrule
\\end{{tabular}}
\\section{{Limitations}}
Auto-generated; numbers come from sandbox simulations unless an experiment says otherwise.
\\end{{document}}
"""


def write(tex: str, out: Path | str) -> Path:
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tex, encoding="utf-8")
    return p

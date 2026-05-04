#!/usr/bin/env python3
"""Generate a static site from probe run results.

Usage: python3 build-site.py <results_dir> <output_dir>

Reads each subdirectory of results_dir that contains a manifest.json,
produces an index page with a comparison table, individual run pages
rendering report.md as HTML, and a shared stylesheet.
"""

import json
import os
import re
import shutil
import sys
from html import escape
from pathlib import Path

# ── CSS ──────────────────────────────────────────────────────────────────────

STYLE_CSS = """\
:root {
  --bg: #0d1117;
  --bg-elev: #161b22;
  --bg-elev2: #1c222b;
  --text: #e6edf3;
  --text-dim: #8b949e;
  --text-faint: #6e7681;
  --accent: #d8b89a;
  --accent-warm: #c89a78;
  --accent-cool: #79b8ff;
  --link: #79b8ff;
  --rule: #30363d;
  --max: 960px;
  --code-bg: #0a0d12;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  font-size: 16px; line-height: 1.55;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
code {
  font-family: "SF Mono", "Menlo", monospace;
  font-size: 0.92em;
  background: var(--bg-elev);
  padding: 1px 6px; border-radius: 4px;
  border: 1px solid var(--rule);
}
pre {
  background: var(--code-bg);
  border: 1px solid var(--rule);
  border-radius: 10px;
  padding: 20px 24px;
  overflow-x: auto;
}
pre code {
  background: transparent; border: 0; padding: 0;
  font-size: 0.88rem; line-height: 1.65;
}
.container { max-width: var(--max); margin: 0 auto; padding: 0 24px; }

/* Hero */
.hero {
  background: radial-gradient(1100px 360px at 14% 0%, rgba(216,184,154,0.08), transparent 60%),
              linear-gradient(180deg, #141a26 0%, var(--bg) 100%);
  padding: 64px 0 48px;
  border-bottom: 1px solid var(--rule);
}
.hero h1 {
  margin: 0 0 10px;
  font-size: clamp(1.8rem, 5vw, 2.8rem);
  font-weight: 700; letter-spacing: -0.02em;
}
.hero .tagline {
  margin: 0 0 16px;
  color: var(--text-dim);
  font-size: 1.1rem;
  max-width: 60ch;
}
.hero .run-meta {
  display: flex; gap: 8px; flex-wrap: wrap;
  margin-bottom: 16px;
}
.badge {
  display: inline-flex; align-items: center; gap: 6px;
  font-family: "SF Mono", "Menlo", monospace;
  font-size: 0.78rem;
  padding: 4px 11px; border: 1px solid var(--rule);
  border-radius: 999px; color: var(--text-dim);
  background: rgba(13,17,23,0.45);
}
.badge .dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--accent);
}
.nav {
  padding: 12px 0;
  border-bottom: 1px solid var(--rule);
  font-size: 0.88rem;
}
.nav a { color: var(--accent-cool); }

/* Content */
.content { padding: 40px 0 24px; }
.content h2 {
  margin: 0 0 20px;
  font-size: 1.5rem; font-weight: 700;
}

/* Tables */
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.92rem;
}
th, td {
  text-align: left;
  padding: 10px 14px;
  border-bottom: 1px solid var(--rule);
}
th {
  background: var(--bg-elev);
  color: var(--text-dim);
  font-family: "SF Mono", "Menlo", monospace;
  font-size: 0.82rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
td.num {
  font-family: "SF Mono", "Menlo", monospace;
  text-align: right;
  font-variant-numeric: tabular-nums;
}
tr:hover td { background: var(--bg-elev); }
td a { color: var(--accent-cool); font-weight: 500; }

/* Report content (rendered from report.md) */
.report-content { padding: 32px 0; }
.report-content h2 {
  margin: 40px 0 16px;
  padding-top: 24px;
  border-top: 1px solid var(--rule);
  font-size: 1.4rem;
}
.report-content h2:first-child { margin-top: 0; border-top: none; padding-top: 0; }
.report-content h3 { margin: 24px 0 12px; font-size: 1.1rem; }
.report-content p { margin: 0 0 12px; color: var(--text); line-height: 1.6; }
.report-content ul { margin: 0 0 12px; padding-left: 24px; }
.report-content li { margin-bottom: 6px; color: var(--text-dim); }
.report-content li code { color: var(--text); }
.report-content ul ul { margin-top: 6px; }
.report-content blockquote {
  margin: 0 0 12px;
  padding: 8px 16px;
  border-left: 3px solid var(--accent);
  background: var(--bg-elev);
  color: var(--text-dim);
  font-size: 0.92rem;
}
.report-content em { color: var(--text-dim); }
.report-content table {
  margin: 12px 0 20px;
  font-size: 0.88rem;
}
.report-content table code {
  background: transparent; border: 0; padding: 0;
}
.report-content hr { border: 0; border-top: 1px solid var(--rule); margin: 32px 0; }

/* Summary cards */
.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
  margin: 20px 0 32px;
}
.summary-card {
  background: var(--bg-elev);
  border: 1px solid var(--rule);
  border-radius: 10px;
  padding: 16px 20px;
}
.summary-card .label {
  font-family: "SF Mono", "Menlo", monospace;
  font-size: 0.78rem;
  color: var(--text-faint);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-bottom: 4px;
}
.summary-card .value {
  font-family: "SF Mono", "Menlo", monospace;
  font-size: 1.3rem;
  font-weight: 600;
  color: var(--text);
}
.summary-card .sub {
  font-family: "SF Mono", "Menlo", monospace;
  font-size: 0.78rem;
  color: var(--text-dim);
  margin-top: 2px;
}

footer {
  padding: 24px 0 32px;
  border-top: 1px solid var(--rule);
  color: var(--text-faint);
  font-family: "SF Mono", "Menlo", monospace;
  font-size: 0.78rem;
}
footer a { color: var(--text-dim); }

@media (max-width: 600px) {
  .hero { padding: 40px 0 32px; }
  .summary-grid { grid-template-columns: 1fr; }
}
"""

# ── Markdown → HTML ──────────────────────────────────────────────────────────

def md_to_html(text: str) -> str:
    """Convert the regular markdown subset used in report.md to HTML."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Blank line
        if line.strip() == "":
            out.append("")
            i += 1
            continue

        # Horizontal rule
        if re.match(r"^-{3,}\s*$", line):
            out.append("<hr>")
            i += 1
            continue

        # Headings
        hm = re.match(r"^(#{1,4})\s+(.+)$", line)
        if hm:
            level = len(hm.group(1))
            content = inline_format(hm.group(2))
            out.append(f"<h{level}>{content}</h{level}>")
            i += 1
            continue

        # Blockquote
        if line.startswith(">"):
            bq_lines = []
            while i < len(lines) and lines[i].startswith(">"):
                bq_lines.append(re.sub(r"^>\s?", "", lines[i]))
                i += 1
            bq_html = md_to_html("\n".join(bq_lines))
            out.append(f"<blockquote>{bq_html}</blockquote>")
            continue

        # Table
        if "|" in line and i + 1 < len(lines) and re.match(r"^\|[\s\-:|]+\|$", lines[i + 1].strip()):
            table_rows = []
            while i < len(lines) and "|" in lines[i]:
                row = lines[i]
                cells = [c.strip() for c in row.strip().strip("|").split("|")]
                # Skip separator row
                if re.match(r"^[\s\-:]+$", cells[0]) and all(re.match(r"^[\s\-:]+$", c) for c in cells):
                    i += 1
                    continue
                table_rows.append(cells)
                i += 1
            if table_rows:
                header = table_rows[0]
                body = table_rows[1:]
                out.append("<table>")
                out.append("<thead><tr>")
                for cell in header:
                    out.append(f"<th>{inline_format(cell)}</th>")
                out.append("</tr></thead>")
                if body:
                    out.append("<tbody>")
                    for row in body:
                        out.append("<tr>")
                        for cell in row:
                            out.append(f"<td>{inline_format(cell)}</td>")
                        out.append("</tr>")
                    out.append("</tbody>")
                out.append("</table>")
            continue

        # Unordered list
        if re.match(r"^\s*[-*]\s", line):
            list_items = []
            while i < len(lines) and re.match(r"^\s*[-*]\s", lines[i]):
                item_text = re.sub(r"^\s*[-*]\s+", "", lines[i])
                list_items.append(f"<li>{inline_format(item_text)}</li>")
                i += 1
            out.append(f"<ul>{''.join(list_items)}</ul>")
            continue

        # Paragraph (collect contiguous non-blank, non-structural lines)
        para_lines = []
        while i < len(lines):
            l = lines[i]
            if l.strip() == "" or l.startswith("#") or l.startswith(">") or l.startswith("- ") or l.startswith("* ") or re.match(r"^-{3,}", l):
                break
            if "|" in l and i + 1 < len(lines) and re.match(r"^\|[\s\-:|]+\|", lines[i + 1].strip()):
                break
            para_lines.append(l)
            i += 1
        if para_lines:
            out.append(f"<p>{inline_format(' '.join(para_lines))}</p>")
        continue

    return "\n".join(out)


def inline_format(text: str) -> str:
    """Handle bold, code, italic inline markup."""
    text = escape(text)
    # Bold
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # Inline code
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # Italic
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<em>\1</em>", text)
    return text


# ── Page builders ────────────────────────────────────────────────────────────

def fmt_num(v, decimals=2):
    if v is None:
        return "&mdash;"
    return f"{v:,.{decimals}f}"


def fmt_tokens(v):
    if v is None:
        return "&mdash;"
    if v >= 1_000_000_000:
        return f"{v / 1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return str(v)


def build_index(runs: list[dict]) -> str:
    rows = []
    for r in runs:
        mid = r["bounds"]["windows"]["5h"]["weighted_usd"]["midpoint"]
        ticks_5h = r["bounds"]["windows"]["5h"]["clean_measured_ticks"]
        mid_7d = r["bounds"]["windows"]["7d"]["weighted_usd"]["midpoint"]
        ticks_7d = r["bounds"]["windows"]["7d"]["clean_measured_ticks"]
        started = r["manifest"]["started"]
        date_str = f"{started[:4]}-{started[4:6]}-{started[6:8]}"
        window = r["manifest"].get("window", "?")
        model = r["manifest"].get("model", "?")

        rows.append(f"""\
      <tr>
        <td><a href="run/{escape(started)}/">{escape(started)}</a></td>
        <td>{escape(date_str)}</td>
        <td><span class="badge"><span class="dot"></span>{escape(window)}</span></td>
        <td>{escape(model.split("-")[-1])}</td>
        <td class="num">{fmt_num(mid)}</td>
        <td class="num">{ticks_5h}</td>
        <td class="num">{fmt_num(mid_7d)}</td>
        <td class="num">{ticks_7d}</td>
      </tr>""")

    return page_wrap(
        title="cc-nerf-buster &mdash; Capacity Probe Results",
        hero=f"""\
    <h1>Capacity Probe Results</h1>
    <p class="tagline">Directly measured quota sizes for Claude Code, bracketed by utilization-meter ticks.</p>""",
        body=f"""\
    <div class="content">
      <h2>Run Comparison</h2>
      <table>
        <thead>
          <tr>
            <th>Run</th>
            <th>Date</th>
            <th>Window</th>
            <th>Model</th>
            <th>5h Mid (USD)</th>
            <th>5h Ticks</th>
            <th>7d Mid (USD)</th>
            <th>7d Ticks</th>
          </tr>
        </thead>
        <tbody>
{"".join(rows)}
        </tbody>
      </table>
    </div>""",
    )


def build_run_page(r: dict) -> str:
    started = r["manifest"]["started"]
    date_str = f"{started[:4]}-{started[4:6]}-{started[6:8]}"
    model = r["manifest"].get("model", "?")
    window = r["manifest"].get("window", "?")

    b5 = r["bounds"]["windows"]["5h"]
    b7 = r["bounds"]["windows"]["7d"]

    # Summary cards
    mid_5h = b5["weighted_usd"]["midpoint"]
    spread_5h = b5["weighted_usd"]["high_post"] and b5["weighted_usd"]["low_pre"]
    spread_pct_5h = ""
    if spread_5h:
        low, high = b5["weighted_usd"]["low_pre"], b5["weighted_usd"]["high_post"]
        diff = low - high
        pct = (diff / mid_5h * 100) if mid_5h else 0
        spread_pct_5h = f"spread: {pct:.2f}%"

    mid_7d = b7["weighted_usd"]["midpoint"]
    spread_7d = b7["weighted_usd"]["high_post"] and b7["weighted_usd"]["low_pre"]
    spread_pct_7d = ""
    if spread_7d and mid_7d:
        low, high = b7["weighted_usd"]["low_pre"], b7["weighted_usd"]["high_post"]
        diff = low - high
        pct = (diff / mid_7d * 100)
        spread_pct_7d = f"spread: {pct:.2f}%"

    # Best run gets opus tokens highlighted
    opus_5h = b5["tokens"]["midpoint"]["opus"] if b5["tokens"] else None
    opus_7d = b7["tokens"]["midpoint"]["opus"] if b7["tokens"] else None

    cards_html = f"""\
    <div class="summary-grid">
      <div class="summary-card">
        <div class="label">5h Capacity (mid)</div>
        <div class="value">${fmt_num(mid_5h)}</div>
        <div class="sub">{spread_pct_5h}</div>
      </div>
      <div class="summary-card">
        <div class="label">5h Opus Input</div>
        <div class="value">{fmt_tokens(opus_5h["input_full_quota"]) if opus_5h else "&mdash;"}</div>
        <div class="sub">{f'{opus_5h["input_per_tick"]:,.0f} per tick' if opus_5h else 'no data'}</div>
      </div>
      <div class="summary-card">
        <div class="label">7d Capacity (mid)</div>
        <div class="value">${fmt_num(mid_7d)}</div>
        <div class="sub">{spread_pct_7d}</div>
      </div>
      <div class="summary-card">
        <div class="label">7d Opus Input</div>
        <div class="value">{fmt_tokens(opus_7d["input_full_quota"]) if opus_7d else "&mdash;"}</div>
        <div class="sub">{f'{opus_7d["input_per_tick"]:,.0f} per tick' if opus_7d else 'no data'}</div>
      </div>
    </div>"""

    # Render report.md as HTML
    report_html = md_to_html(r["report_md"])

    return page_wrap(
        title=f"Run {started} &mdash; cc-nerf-buster",
        hero=f"""\
    <h1>Run {escape(started)}</h1>
    <div class="run-meta">
      <span class="badge"><span class="dot"></span>{escape(date_str)}</span>
      <span class="badge"><span class="dot"></span>{escape(model)}</span>
      <span class="badge"><span class="dot"></span>window: {escape(window)}</span>
      <span class="badge"><span class="dot"></span>{b5['clean_measured_ticks']} 5h ticks</span>
      <span class="badge"><span class="dot"></span>{b7['clean_measured_ticks']} 7d ticks</span>
    </div>""",
        body=f"""\
    {cards_html}
    <div class="report-content">
{report_html}
    </div>""",
        nav=True,
    )


def page_wrap(title: str, hero: str, body: str, nav: bool = False) -> str:
    nav_html = '<div class="nav"><div class="container"><a href="../../index.html">All runs</a></div></div>\n' if nav else ""
    hero_depth = "../../" if nav else ""
    css_href = f"{hero_depth}style.css"
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet" href="{css_href}">
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%93%8A%3C/text%3E%3C/svg%3E">
</head>
<body>
  <header class="hero">
    <div class="container">
{hero}
    </div>
  </header>
{nav_html}  <div class="container">
{body}
  </div>
  <footer>
    <div class="container">
      <a href="https://github.com/brandon-fryslie/cc-nerf-buster">brandon-fryslie/cc-nerf-buster</a>
      &middot; capacity probe results
    </div>
  </footer>
</body>
</html>"""


# ── Main ─────────────────────────────────────────────────────────────────────

def load_run(run_dir: Path) -> dict:
    manifest = json.loads((run_dir / "manifest.json").read_text())
    bounds = json.loads((run_dir / "bounds.json").read_text())
    report_md = (run_dir / "report.md").read_text()
    return {"manifest": manifest, "bounds": bounds, "report_md": report_md, "dir": run_dir}


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <results_dir> <output_dir>", file=sys.stderr)
        sys.exit(1)

    results_dir = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])

    if not results_dir.is_dir():
        print(f"Error: {results_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Discover runs
    runs = []
    for entry in sorted(results_dir.iterdir()):
        if (entry / "manifest.json").is_file():
            runs.append(load_run(entry))

    if not runs:
        print("Error: no runs found in results directory", file=sys.stderr)
        sys.exit(1)

    # Sort by started timestamp
    runs.sort(key=lambda r: r["manifest"]["started"])

    print(f"Found {len(runs)} runs")

    # Clean and create output directory
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    # Write stylesheet
    (output_dir / "style.css").write_text(STYLE_CSS)

    # Write index
    index_html = build_index(runs)
    (output_dir / "index.html").write_text(index_html)
    print(f"  index.html ({len(runs)} runs)")

    # Write individual run pages
    for r in runs:
        started = r["manifest"]["started"]
        run_page_dir = output_dir / "run" / started
        run_page_dir.mkdir(parents=True)
        run_html = build_run_page(r)
        (run_page_dir / "index.html").write_text(run_html)
        print(f"  run/{started}/index.html")

    print("Done.")


if __name__ == "__main__":
    main()

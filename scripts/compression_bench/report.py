"""Stage 5: render one mode's report from that mode's results file.

--live renders results.json into report.html, --dry-run renders
results.dry-run.json into report.dry-run.html, and a results file scored from the
other mode's responses stops the run.

Sections in order: verdict, degradation curve, cost-accuracy frontier, the full
grid, error against effective speaking rate, what breaking looks like, the two
billing probes, what was actually transcribed, and what the test set can and
cannot support.

Charts size their axes to the data. Nothing is clipped at an axis maximum and no
label is placed where another one already is, because a chart that quietly drops
a point is worse than no chart.
"""

import argparse
import html
import json
import math

import config as cfg
import response_log

SERIES = {
    "nova-3": "#7c3043",
    "whisper-turbo": "#35706b",
    "whisper": "#a8792f",
    "whisper-tiny-en": "#4e7a9c",
}
SHAPES = {
    "nova-3": "circle",
    "whisper-turbo": "square",
    "whisper": "triangle",
    "whisper-tiny-en": "diamond",
}
FALLBACK_COLOR = "#6a6a6a"
SURFACE = "#faf9f7"
GRID = "#e0ddd5"
AXIS = "#6a6a6a"
RULE = "#3a3a3a"
HISTOGRAM_FILL = "#e0ddd5"

STYLE = """
:root {
  --pb-burgundy-700: #5c2233;  --pb-burgundy-600: #7c3043;
  --pb-burgundy-500: #a3415a;  --pb-burgundy-100: #f5eaed;
  --pb-cream-50:  #f5f4f0;     --pb-cream-200: #ebe7e3;
  --pb-cream-300: #e0ddd5;     --pb-paper:     #faf9f7;
  --pb-ink-900: #3a3a3a;       --pb-ink-700: #4a4a4a;   --pb-ink-500: #6a6a6a;
  --pb-border: #dddddd;        --pb-border-soft: #e0ddd5;

  --font-serif: "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif;
  --font-mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  --radius-sm: 4px;  --radius-md: 6px;
  --line-height-body: 1.618;  --line-height-tight: 1.3;
  --measure: 56rem;

  --fg: var(--pb-ink-700);       --fg-strong: var(--pb-ink-900);
  --fg-muted: var(--pb-ink-500); --bg: var(--pb-cream-50);
  --bg-surface: var(--pb-paper); --accent: var(--pb-burgundy-600);
  --accent-hover: var(--pb-burgundy-500); --accent-tint: var(--pb-burgundy-100);
  --border: var(--pb-border);    --border-soft: var(--pb-border-soft);
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
       font-family: var(--font-serif); font-size: 18px;
       line-height: var(--line-height-body); overflow-wrap: break-word; }
main { max-width: var(--measure); margin: 0 auto; padding: 3rem 1.4rem 5rem; }
h1 { font-size: clamp(1.9rem, 5vw, 2.6rem); line-height: var(--line-height-tight);
     color: var(--fg-strong); margin: 0 0 0.6rem; }
.project-meta { color: var(--fg-muted); font-family: var(--font-mono); font-size: 0.72rem;
                letter-spacing: 0.04em; margin: 0 0 1.6rem; }

.report h3 { font-size: 1.08rem; font-weight: 700; color: var(--fg-strong);
             margin: 2.6rem 0 0.6rem; }
.report h3 .num { color: var(--fg-muted); font-family: var(--font-mono);
                  font-size: 0.8rem; margin-right: 0.5rem; }
.report h4 { font-size: 0.95rem; font-weight: 700; color: var(--fg-strong);
             margin: 1.4rem 0 0.4rem; }
.report p { margin: 0 0 1rem; }
.report .lead { font-weight: 700; color: var(--fg-strong); }

.abstract { background: var(--bg-surface); border: 1px solid var(--border-soft);
            border-left: 3px solid var(--accent); border-radius: var(--radius-md);
            padding: 1.1rem 1.3rem; margin: 0 0 1.4rem; }
.abstract h2 { font-size: 0.72rem; font-family: var(--font-mono); letter-spacing: 0.1em;
               text-transform: uppercase; color: var(--fg-muted); border: 0;
               margin: 0 0 0.5rem; padding: 0; }
.abstract p { margin: 0; font-size: 0.95rem; }

.notice { background: var(--accent-tint); border: 1px solid var(--accent);
          border-radius: var(--radius-md); padding: 0.9rem 1.2rem; margin: 1.2rem 0; }
.notice p { margin: 0.3rem 0 0; font-size: 0.9rem; color: var(--fg); }
.notice .tag { font-family: var(--font-mono); font-size: 0.7rem; letter-spacing: 0.1em;
               text-transform: uppercase; color: var(--accent); font-weight: 700; }

.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 11rem), 1fr));
         gap: 0.9rem; margin: 1.2rem 0 0; }
.tile { background: var(--bg-surface); border: 1px solid var(--border-soft);
        border-radius: var(--radius-md); padding: 0.9rem 1rem; min-width: 0; }
.tile .k { font-family: var(--font-mono); font-size: 0.66rem; letter-spacing: 0.08em;
           text-transform: uppercase; color: var(--fg-muted); }
.tile .v { font-size: 1.7rem; font-weight: 700; color: var(--accent);
           line-height: var(--line-height-tight); margin-top: 0.2rem; }
.tile .d { font-size: 0.8rem; color: var(--fg-muted); }

figure.tbl, figure.fig { margin: 1.6rem 0; }
.tbl-scroll { overflow-x: auto; max-width: 100%; }
table.data { width: 100%; border-collapse: collapse; font-size: 0.85rem; line-height: 1.45; }
table.data th, table.data td { text-align: left; vertical-align: top; padding: 0.45rem 0.6rem; }
table.data thead th { color: var(--fg-strong); font-weight: 700;
                      border-top: 2px solid var(--fg-strong);
                      border-bottom: 1px solid var(--fg-strong); white-space: nowrap; }
table.data tbody tr + tr td { border-top: 1px solid var(--border-soft); }
table.data tbody tr:last-child td { border-bottom: 2px solid var(--fg-strong); }
table.data .n { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
table.data th.n { text-align: right; }

figcaption.cap { margin-top: 0.6rem; font-size: 0.82rem; color: var(--fg-muted); line-height: 1.5; }
figcaption.cap .lbl { font-family: var(--font-mono); font-size: 0.7rem; letter-spacing: 0.05em;
                      text-transform: uppercase; color: var(--fg-strong); margin-right: 0.4rem; }

.chart { border: 1px solid var(--border-soft); border-radius: var(--radius-md);
         overflow: hidden; background: var(--bg-surface); }
svg { display: block; width: 100%; height: auto; }
.tick { fill: var(--fg-muted); font-family: var(--font-mono); font-size: 10.5px; }
.axis { fill: var(--fg-strong); font-family: var(--font-serif); font-size: 12px; }
.lab { fill: var(--fg-strong); font-family: var(--font-serif); font-size: 12px; }
.grid-line { stroke: #e0ddd5; stroke-width: 1; }
.base-line { stroke: #6a6a6a; stroke-width: 1; }
.zero-line { stroke: #6a6a6a; stroke-width: 1.2; }

.eq { margin: 1.2rem 0; text-align: center; }
.eq .expr { font-size: 1.05rem; color: var(--fg-strong); }
.eq .reading { display: block; margin-top: 0.5rem; font-size: 0.8rem; color: var(--fg-muted);
               font-style: italic; }

.findings { margin: 0; padding-left: 1.2rem; font-size: 0.95rem; }
.findings li { margin: 0.5rem 0; }
.findings li::marker { color: var(--accent); }

.panel { background: var(--bg-surface); border: 1px solid var(--border-soft);
         border-radius: var(--radius-md); padding: 1.1rem 1.3rem; margin: 1.2rem 0; }
.ok { color: #2f6b3f; } .bad { color: var(--accent); } .warn { color: #8a6516; }
.pair { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 18rem), 1fr));
        gap: 0.3rem 1.4rem; margin-top: 0.6rem; }
.pair .who { font-family: var(--font-mono); font-size: 0.66rem; letter-spacing: 0.08em;
             text-transform: uppercase; color: var(--fg-muted); }
.pair .txt { font-size: 0.86rem; overflow-wrap: anywhere; }
.pair .txt.hyp { color: var(--fg-muted); }
.example { border-top: 1px solid var(--border-soft); padding-top: 0.9rem; margin-top: 0.9rem; }
.example:first-of-type { border-top: 0; padding-top: 0; margin-top: 0; }
.example h4 { margin-top: 0; }
.none { color: var(--fg-muted); font-size: 0.85rem; }
details { margin-top: 0.9rem; }
summary { cursor: pointer; font-family: var(--font-mono); font-size: 0.72rem;
          letter-spacing: 0.05em; text-transform: uppercase; color: var(--fg-muted); }
summary::marker { color: var(--fg-muted); }
code { font-family: var(--font-mono); font-size: 0.86em; overflow-wrap: anywhere; }
"""

def esc(text):
    return html.escape(str(text))


def color_of(model_key):
    return SERIES.get(model_key, FALLBACK_COLOR)


def label_of(model_key):
    return cfg.MODELS.get(model_key, {}).get("label", model_key)


def marker(shape, cx, cy, color):
    if shape == "square":
        return f'<rect x="{cx-4.2:.1f}" y="{cy-4.2:.1f}" width="8.4" height="8.4" rx="1.2" fill="{color}"/>'
    if shape == "triangle":
        return f'<polygon points="{cx:.1f},{cy-5.2:.1f} {cx+4.8:.1f},{cy+3.6:.1f} {cx-4.8:.1f},{cy+3.6:.1f}" fill="{color}"/>'
    if shape == "diamond":
        return f'<polygon points="{cx:.1f},{cy-5.4:.1f} {cx+5:.1f},{cy:.1f} {cx:.1f},{cy+5.4:.1f} {cx-5:.1f},{cy:.1f}" fill="{color}"/>'
    return f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.4" fill="{color}"/>'


def dodge(labels, top, bottom, gap=17):
    """Spread labels vertically so none overlaps, keeping every one in bounds.

    Down pass first, then an up pass for anything pushed past the bottom, so a
    crowded chart loses no label off either edge.
    """
    labels = sorted(labels, key=lambda item: item[0])
    for i in range(1, len(labels)):
        if labels[i][0] - labels[i - 1][0] < gap:
            labels[i] = (labels[i - 1][0] + gap,) + labels[i][1:]
    if labels and labels[-1][0] > bottom:
        labels[-1] = (bottom,) + labels[-1][1:]
        for i in range(len(labels) - 2, -1, -1):
            if labels[i + 1][0] - labels[i][0] < gap:
                labels[i] = (labels[i + 1][0] - gap,) + labels[i][1:]
    if labels and labels[0][0] < top:
        labels[0] = (top,) + labels[0][1:]
        for i in range(1, len(labels)):
            if labels[i][0] - labels[i - 1][0] < gap:
                labels[i] = (labels[i - 1][0] + gap,) + labels[i][1:]
    return labels


def nice_ticks(lo, hi, count=5):
    """Round tick values spanning [lo, hi], always including both ends."""
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / count
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    first = math.floor(lo / step) * step
    ticks = []
    value = first
    while value <= hi + step / 2:
        ticks.append(round(value, 6))
        value += step
    return ticks


def line_chart(series, speeds, y_title, x_title, budget=None):
    """Speed on x, one line per model, direct labels on the right."""
    W, H, ml, mr, mt, mb = 900, 400, 62, 168, 20, 48
    iw, ih = W - ml - mr, H - mt - mb

    values = [v for _, points, band in series for v in points]
    values += [v for _, _, band in series for pair in (band or []) for v in pair]
    values.append(0.0)
    if budget is not None:
        values.append(budget)
    y_lo, y_hi = min(values), max(values)
    span = (y_hi - y_lo) or 1.0
    # The domain hugs the data. Ticks are rounded to the nearest sensible value
    # inside it rather than the domain being stretched out to reach one, which
    # would leave a third of the chart empty.
    y_lo, y_hi = y_lo - span * 0.03, y_hi + span * 0.08
    ticks = [t for t in nice_ticks(y_lo, y_hi) if y_lo <= t <= y_hi]

    span = (speeds[-1] - speeds[0]) or 1.0
    fx = lambda s: ml + (s - speeds[0]) / span * iw
    fy = lambda v: mt + ih - (v - y_lo) / (y_hi - y_lo) * ih

    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(y_title)} against {esc(x_title)}">']
    for v in ticks:
        out.append(f'<line class="grid-line" x1="{ml}" x2="{ml+iw}" y1="{fy(v):.1f}" y2="{fy(v):.1f}"/>')
        out.append(f'<text class="tick" x="{ml-8}" y="{fy(v)+4:.1f}" text-anchor="end">{v:g}</text>')
    for s in speeds:
        out.append(f'<text class="tick" x="{fx(s):.1f}" y="{mt+ih+20}" text-anchor="middle">{s:g}x</text>')
    out.append(f'<line class="base-line" x1="{ml}" x2="{ml+iw}" y1="{mt+ih}" y2="{mt+ih}"/>')
    if y_lo < 0 < y_hi:
        out.append(f'<line class="zero-line" x1="{ml}" x2="{ml+iw}" y1="{fy(0):.1f}" y2="{fy(0):.1f}"/>')
    if budget is not None:
        out.append(f'<line x1="{ml}" x2="{ml+iw}" y1="{fy(budget):.1f}" y2="{fy(budget):.1f}" '
                   f'stroke="{RULE}" stroke-width="1.2" stroke-dasharray="5 4" opacity=".8"/>')
        out.append(f'<text class="tick" x="{ml+6}" y="{fy(budget)-6:.1f}" fill="{RULE}">'
                   f'{budget:g} pt budget</text>')

    labels = []
    for model_key, points, band in series:
        color, shape = color_of(model_key), SHAPES.get(model_key, "circle")
        if band:
            up = " ".join(f"{fx(s):.1f},{fy(hi):.1f}" for s, (_, hi) in zip(speeds, band))
            dn = " ".join(f"{fx(s):.1f},{fy(lo):.1f}"
                          for s, (lo, _) in reversed(list(zip(speeds, band))))
            out.append(f'<polygon points="{up} {dn}" fill="{color}" opacity=".13"/>')
        path = " ".join(f"{fx(s):.1f},{fy(v):.1f}" for s, v in zip(speeds, points))
        out.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round"/>')
        for s, v in zip(speeds, points):
            out.append(f'<circle cx="{fx(s):.1f}" cy="{fy(v):.1f}" r="6.5" fill="{SURFACE}"/>')
            out.append(marker(shape, fx(s), fy(v), color))
        labels.append((fy(points[-1]), color, label_of(model_key), shape))

    for y, color, text, shape in dodge(labels, mt + 6, mt + ih):
        out.append(marker(shape, ml + iw + 15, y, color))
        out.append(f'<text class="lab" x="{ml+iw+26}" y="{y+3:.1f}">{esc(text)}</text>')

    out.append(f'<text class="axis" x="{ml+iw/2:.0f}" y="{H-8}" text-anchor="middle">{esc(x_title)}</text>')
    out.append(f'<text class="axis" x="16" y="{mt+ih/2:.0f}" text-anchor="middle" '
               f'transform="rotate(-90 16 {mt+ih/2:.0f})">{esc(y_title)}</text>')
    out.append("</svg>")
    return "".join(out)


def frontier_chart(grid, models):
    """Cost per hour on a log x, WER on y, one line per model."""
    W, H, ml, mr, mt, mb = 900, 400, 62, 172, 22, 50
    iw, ih = W - ml - mr, H - mt - mb
    costs = [row["usd_per_hour"] for row in grid]
    wers = [row["wer"] for row in grid]
    lo, hi = math.log10(min(costs) * 0.55), math.log10(max(costs) * 1.9)
    y_lo, y_hi = 0.0, max(wers) * 1.12 or 1.0
    ticks = nice_ticks(y_lo, y_hi)
    y_hi = max(y_hi, ticks[-1])
    fx = lambda v: ml + (math.log10(v) - lo) / (hi - lo) * iw
    fy = lambda v: mt + ih - (v - y_lo) / (y_hi - y_lo) * ih

    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="error rate against cost per hour">']
    for v in ticks:
        out.append(f'<line class="grid-line" x1="{ml}" x2="{ml+iw}" y1="{fy(v):.1f}" y2="{fy(v):.1f}"/>')
        out.append(f'<text class="tick" x="{ml-8}" y="{fy(v)+4:.1f}" text-anchor="end">{v:g}%</text>')
    decade = math.floor(lo)
    while decade <= hi:
        for mult in (1, 3):
            v = mult * 10 ** decade
            if lo <= math.log10(v) <= hi:
                out.append(f'<line class="grid-line" x1="{fx(v):.1f}" x2="{fx(v):.1f}" y1="{mt}" y2="{mt+ih}"/>')
                out.append(f'<text class="tick" x="{fx(v):.1f}" y="{mt+ih+20}" text-anchor="middle">'
                           f'${v:g}</text>')
        decade += 1
    out.append(f'<line class="base-line" x1="{ml}" x2="{ml+iw}" y1="{mt+ih}" y2="{mt+ih}"/>')

    placed = []
    for model_key in models:
        rows = sorted((r for r in grid if r["model"] == model_key), key=lambda r: r["speed"])
        if not rows:
            continue
        color, shape = color_of(model_key), SHAPES.get(model_key, "circle")
        path = " ".join(f"{fx(r['usd_per_hour']):.1f},{fy(r['wer']):.1f}" for r in rows)
        out.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2" opacity=".75"/>')
        for r in rows:
            cx, cy = fx(r["usd_per_hour"]), fy(r["wer"])
            out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="7" fill="{SURFACE}"/>')
            out.append(marker(shape, cx, cy, color))
        base = rows[0]
        placed.append((fy(base["wer"]), color, f"{label_of(model_key)} · {base['speed']:g}x", shape))

    # Labels live in the right margin and carry the series marker, so nothing is
    # drawn over a line and no legend lookup is needed to read the chart.
    for y, color, text, shape in dodge(placed, mt + 8, mt + ih - 4):
        out.append(marker(shape, ml + iw + 15, y, color))
        out.append(f'<text class="lab" x="{ml+iw+26:.1f}" y="{y+3:.1f}">{esc(text)}</text>')

    out.append(f'<text class="axis" x="{ml+iw/2:.0f}" y="{H-8}" text-anchor="middle">'
               f'cost per hour of real speech, log scale</text>')
    out.append(f'<text class="axis" x="16" y="{mt+ih/2:.0f}" text-anchor="middle" '
               f'transform="rotate(-90 16 {mt+ih/2:.0f})">WER</text>')
    out.append("</svg>")
    return "".join(out)


def rate_chart(rate_curve, models):
    """Effective speaking rate on x, error on y, one line per model.

    The corpus's own distribution sits behind the lines as bars on a secondary
    scale, because the outer bands hold a third of the utterances the middle
    ones do and the error there is correspondingly softer.

    Bands differ between models, since a band is only reported once it holds
    enough utterances, so each line is drawn over its own x positions.
    """
    W, H, ml, mr, mt, mb = 900, 380, 62, 214, 22, 50
    iw, ih = W - ml - mr, H - mt - mb
    x_lo = min(r["wpm_lo"] for r in rate_curve)
    x_hi = max(r["wpm_hi"] for r in rate_curve)
    y_hi = max(r["wer"] for r in rate_curve) * 1.1 or 1.0
    ticks = nice_ticks(0, y_hi)
    y_hi = max(y_hi, ticks[-1])
    fx = lambda v: ml + (v - x_lo) / ((x_hi - x_lo) or 1) * iw
    fy = lambda v: mt + ih - v / y_hi * ih

    # Every model scores the same utterances, so a band's count is a property of
    # the corpus rather than of any one model.
    counts = {}
    for row in rate_curve:
        counts[(row["wpm_lo"], row["wpm_hi"])] = max(counts.get((row["wpm_lo"], row["wpm_hi"]), 0),
                                                     row["n"])
    peak = max(counts.values()) if counts else 1

    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="error rate against effective speaking rate">']
    for (lo, hi), count in sorted(counts.items()):
        x0, x1 = fx(lo), fx(hi)
        height = count / peak * ih * 0.9
        out.append(f'<rect x="{x0+1:.1f}" y="{mt+ih-height:.1f}" width="{max(1, x1-x0-2):.1f}" '
                   f'height="{height:.1f}" fill="{HISTOGRAM_FILL}" opacity=".55"/>')
    for v in ticks:
        out.append(f'<line class="grid-line" x1="{ml}" x2="{ml+iw}" y1="{fy(v):.1f}" y2="{fy(v):.1f}"/>')
        out.append(f'<text class="tick" x="{ml-8}" y="{fy(v)+4:.1f}" text-anchor="end">{v:g}%</text>')
    for v in nice_ticks(x_lo, x_hi, 6):
        if x_lo <= v <= x_hi:
            out.append(f'<text class="tick" x="{fx(v):.1f}" y="{mt+ih+20}" text-anchor="middle">{v:g}</text>')
    out.append(f'<line class="base-line" x1="{ml}" x2="{ml+iw}" y1="{mt+ih}" y2="{mt+ih}"/>')
    # The bars carry a label rather than a full second axis, so they stay
    # subordinate to the curves and cannot be misread as a result.
    out.append(f'<rect x="{ml+iw+8}" y="{mt+4}" width="8" height="9" fill="{HISTOGRAM_FILL}"/>')
    out.append(f'<text class="tick" x="{ml+iw+21}" y="{mt+12}">utterances per band</text>')
    out.append(f'<text class="tick" x="{ml+iw+21}" y="{mt+25}">peak {peak}, floor 0</text>')

    labels = []
    for model_key in models:
        rows = sorted((r for r in rate_curve if r["model"] == model_key),
                      key=lambda r: r["wpm_lo"])
        if not rows:
            continue
        color, shape = color_of(model_key), SHAPES.get(model_key, "circle")
        points = [((r["wpm_lo"] + r["wpm_hi"]) / 2, r["wer"]) for r in rows]
        path = " ".join(f"{fx(x):.1f},{fy(y):.1f}" for x, y in points)
        out.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round"/>')
        for x, y in points:
            out.append(f'<circle cx="{fx(x):.1f}" cy="{fy(y):.1f}" r="6" fill="{SURFACE}"/>')
            out.append(marker(shape, fx(x), fy(y), color))
        labels.append((fy(points[-1][1]), color, label_of(model_key), shape))

    for y, color, text, shape in dodge(labels, mt + 40, mt + ih):
        out.append(marker(shape, ml + iw + 62, y, color))
        out.append(f'<text class="lab" x="{ml+iw+73}" y="{y+3:.1f}">{esc(text)}</text>')

    out.append(f'<text class="axis" x="{ml+iw/2:.0f}" y="{H-8}" text-anchor="middle">'
               f'effective words per minute</text>')
    out.append(f'<text class="axis" x="16" y="{mt+ih/2:.0f}" text-anchor="middle" '
               f'transform="rotate(-90 16 {mt+ih/2:.0f})">WER</text>')
    out.append("</svg>")
    return "".join(out)


def histogram(bins, counts, median, x_title):
    W, H, ml, mr, mt, mb = 620, 250, 46, 20, 20, 46
    iw, ih = W - ml - mr, H - mt - mb
    y_max = max(max(counts) * 1.15, 1)
    fx = lambda v: ml + (v - bins[0]) / (bins[-1] - bins[0]) * iw
    fy = lambda v: mt + ih - v / y_max * ih
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(x_title)}">']
    for v in nice_ticks(0, y_max, 4):
        out.append(f'<line class="grid-line" x1="{ml}" x2="{ml+iw}" y1="{fy(v):.1f}" y2="{fy(v):.1f}"/>')
        out.append(f'<text class="tick" x="{ml-7}" y="{fy(v)+4:.1f}" text-anchor="end">{v:g}</text>')
    bw = iw / max(1, len(counts))
    for i, count in enumerate(counts):
        x = ml + i * bw
        out.append(f'<rect x="{x+1:.1f}" y="{fy(count):.1f}" width="{max(1, bw-2):.1f}" '
                   f'height="{max(0.5, mt+ih-fy(count)):.1f}" rx="2" fill="{SERIES["nova-3"]}"/>')
    step = max(1, len(bins) // 10)
    for v in bins[::step]:
        out.append(f'<text class="tick" x="{fx(v):.1f}" y="{mt+ih+18}" text-anchor="middle">{v:g}</text>')
    anchor, offset = ("start", 6) if fx(median) < ml + iw * 0.75 else ("end", -6)
    out.append(f'<line x1="{fx(median):.1f}" x2="{fx(median):.1f}" y1="{mt}" y2="{mt+ih}" '
               f'stroke="{RULE}" stroke-width="1.6" stroke-dasharray="4 3"/>')
    out.append(f'<text class="lab" x="{fx(median)+offset:.1f}" y="{mt+12}" '
               f'text-anchor="{anchor}">median {median:.0f}</text>')
    out.append(f'<line class="base-line" x1="{ml}" x2="{ml+iw}" y1="{mt+ih}" y2="{mt+ih}"/>')
    out.append(f'<text class="axis" x="{ml+iw/2:.0f}" y="{H-8}" text-anchor="middle">{esc(x_title)}</text>')
    out.append("</svg>")
    return "".join(out)


def verdict_text(results):
    lines = []
    for model_key, speed in results["recommended"].items():
        label = label_of(model_key)
        if not speed:
            lines.append(f"{label} does not tolerate compression inside the budget.")
            continue
        row = next(r for r in results["grid"] if r["model"] == model_key and r["speed"] == speed)
        lines.append(
            f"{label} holds to {speed:g}x for {row['saving_pct']:.0f}% off, "
            f"at +{row['delta_wer']:.1f} points and "
            f"{row['free_minutes_per_day']:.0f} free minutes a day."
        )
    return lines or ["No model produced a scored baseline, so there is no verdict to give."]


def ms(value):
    return "n/a" if value is None else f"{value:.0f} ms"


def gallery_html(gallery):
    if not gallery:
        return '<p class="none">No transcripts were kept, so there is nothing to show here.</p>'
    blocks = []
    for item in gallery:
        flag = ' <span class="warn">repetition loop</span>' if item["loop"] else ""
        # LibriSpeech stores its references in block capitals. The Whisper
        # normalizer lowercases both sides before jiwer sees them, so lowercasing
        # here shows the pair as it was actually compared. The stored data keeps
        # the raw casing.
        reference = item["reference"].lower()
        hypothesis = item["hypothesis"].strip().lower() or "(empty transcript)"
        blocks.append(
            f'<div class="example"><h4>{esc(label_of(item["model"]))} at {item["speed"]:g}x '
            f'<span class="bad">{item["wer"]:.0f}% WER</span>{flag}</h4>'
            f'<p class="none">{esc(item["utt_id"])}</p>'
            f'<div class="pair"><div><div class="who">reference</div>'
            f'<div class="txt">{esc(reference)}</div></div>'
            f'<div><div class="who">transcript</div>'
            f'<div class="txt hyp">{esc(hypothesis)}</div></div></div></div>'
        )
    return "".join(blocks)


def measured_rates(data):
    """Billed neurons per minute of real speech, per model and speed.

    A file compressed by r holds 1/r of the real speech it came from, so the
    quantity the cost argument rests on is neurons per minute of speech as
    spoken, not per minute of file.
    """
    measured = {}
    for replicate in data["replicates"]:
        for window in replicate["windows"]:
            speed = window["speed"]
            for row in window["models"]:
                real_minutes = row["audio_seconds_sent"] * speed / 60
                if not real_minutes:
                    continue
                measured.setdefault((row["model"], speed), []).append(
                    row["neurons_billed"] / real_minutes)
    return {key: sum(values) / len(values) for key, values in measured.items()}


def billing_rate_chart(data):
    """Predicted billing band against what was measured, on a log axis.

    The four models span three decades, so a linear axis would flatten every
    Whisper line onto the baseline and hide the only thing worth seeing: a
    measured line leaving its own band.
    """
    speeds = sorted(data["speeds"])
    measured = measured_rates(data)
    models = [m for m in cfg.MODELS if any(k[0] == m for k in measured)]
    if not models or len(speeds) < 1:
        return '<p class="none">The probe recorded no billed rate to plot.</p>'

    tol = data.get("tolerance", 0.02)
    W, H, ml, mr, mt, mb = 900, 380, 68, 200, 22, 50
    iw, ih = W - ml - mr, H - mt - mb
    predicted = {(m, s): cfg.MODELS[m]["neurons_per_audio_minute"] / s
                 for m in models for s in speeds}
    values = list(predicted.values()) + list(measured.values())
    lo, hi = math.log10(min(values) * 0.5), math.log10(max(values) * 2.0)
    span = (speeds[-1] - speeds[0]) or 1.0
    fx = lambda s: ml + (s - speeds[0]) / span * iw if len(speeds) > 1 else ml + iw / 2
    fy = lambda v: mt + ih - (math.log10(v) - lo) / (hi - lo) * ih

    out = [f'<svg viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="billed neurons per minute of real speech against compression factor">']
    decade = math.floor(lo)
    while decade <= hi:
        v = 10 ** decade
        if lo <= math.log10(v) <= hi:
            out.append(f'<line class="grid-line" x1="{ml}" x2="{ml+iw}" y1="{fy(v):.1f}" y2="{fy(v):.1f}"/>')
            out.append(f'<text class="tick" x="{ml-8}" y="{fy(v)+4:.1f}" text-anchor="end">{v:g}</text>')
        decade += 1
    for s in speeds:
        out.append(f'<text class="tick" x="{fx(s):.1f}" y="{mt+ih+20}" text-anchor="middle">{s:g}x</text>')
    out.append(f'<line class="base-line" x1="{ml}" x2="{ml+iw}" y1="{mt+ih}" y2="{mt+ih}"/>')

    labels = []
    for model_key in models:
        color, shape = color_of(model_key), SHAPES.get(model_key, "circle")
        top = [(fx(s), fy(predicted[(model_key, s)] * (1 + tol))) for s in speeds]
        bottom = [(fx(s), fy(predicted[(model_key, s)] * (1 - tol))) for s in reversed(speeds)]
        # A band this tight is a couple of pixels tall, so it is widened to stay
        # visible. The tolerance it stands for is stated under the chart.
        band = [(x, y - 3) for x, y in top] + [(x, y + 3) for x, y in bottom]
        out.append(f'<polygon points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in band)}" '
                   f'fill="{color}" opacity=".22"/>')
        points = [(fx(s), fy(measured[(model_key, s)])) for s in speeds if (model_key, s) in measured]
        if len(points) > 1:
            out.append(f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in points)}" '
                       f'fill="none" stroke="{color}" stroke-width="1.6"/>')
        for x, y in points:
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="{SURFACE}"/>')
            out.append(marker(shape, x, y, color))
        if points:
            labels.append((points[-1][1], color, label_of(model_key), shape))

    for y, color, text, shape in dodge(labels, mt + 6, mt + ih):
        out.append(marker(shape, ml + iw + 15, y, color))
        out.append(f'<text class="lab" x="{ml+iw+26}" y="{y+3:.1f}">{esc(text)}</text>')

    out.append(f'<text class="axis" x="{ml+iw/2:.0f}" y="{H-8}" text-anchor="middle">'
               f'compression factor r</text>')
    out.append(f'<text class="axis" x="16" y="{mt+ih/2:.0f}" text-anchor="middle" '
               f'transform="rotate(-90 16 {mt+ih/2:.0f})">neurons per minute of real speech, log scale</text>')
    out.append("</svg>")
    return "".join(out)


def silence_chart(data):
    """Billed seconds against added silence, with the file duration as reference."""
    windows = sorted(data["windows"], key=lambda w: w["padding_s"])
    models = [m for m in cfg.MODELS if m in data["summary"]]
    if not windows or not models:
        return '<p class="none">The probe recorded no billed seconds to plot.</p>'

    W, H, ml, mr, mt, mb = 900, 360, 68, 200, 22, 50
    iw, ih = W - ml - mr, H - mt - mb
    paddings = [w["padding_s"] for w in windows]
    files = [w["file_seconds"] for w in windows]
    billed = [row["billed_seconds_per_request"] for w in windows for row in w["models"]]
    y_hi = max(files + billed) * 1.12 or 1.0
    ticks = nice_ticks(0, y_hi)
    y_hi = max(y_hi, ticks[-1])
    span = (paddings[-1] - paddings[0]) or 1.0
    fx = lambda p: ml + (p - paddings[0]) / span * iw
    fy = lambda v: mt + ih - v / y_hi * ih

    out = [f'<svg viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="billed seconds against added silence">']
    for v in ticks:
        out.append(f'<line class="grid-line" x1="{ml}" x2="{ml+iw}" y1="{fy(v):.1f}" y2="{fy(v):.1f}"/>')
        out.append(f'<text class="tick" x="{ml-8}" y="{fy(v)+4:.1f}" text-anchor="end">{v:g}</text>')
    for p in paddings:
        out.append(f'<text class="tick" x="{fx(p):.1f}" y="{mt+ih+20}" text-anchor="middle">+{p:g}s</text>')
    out.append(f'<line class="base-line" x1="{ml}" x2="{ml+iw}" y1="{mt+ih}" y2="{mt+ih}"/>')

    reference = " ".join(f"{fx(p):.1f},{fy(f):.1f}" for p, f in zip(paddings, files))
    out.append(f'<polyline points="{reference}" fill="none" stroke="{AXIS}" stroke-width="1.4" '
               f'stroke-dasharray="6 4"/>')
    out.append(f'<text class="tick" x="{fx(paddings[-1])-6:.1f}" y="{fy(files[-1])-8:.1f}" '
               f'text-anchor="end">file duration</text>')

    labels = []
    for index, model_key in enumerate(models):
        color, shape = color_of(model_key), SHAPES.get(model_key, "circle")
        # Series that agree would otherwise draw exactly on top of each other.
        nudge = (index - (len(models) - 1) / 2) * 7
        points = []
        for window in windows:
            row = next((r for r in window["models"] if r["model"] == model_key), None)
            if row:
                points.append((fx(window["padding_s"]) + nudge,
                               fy(row["billed_seconds_per_request"])))
        if len(points) > 1:
            out.append(f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in points)}" '
                       f'fill="none" stroke="{color}" stroke-width="1.8"/>')
        for x, y in points:
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="{SURFACE}"/>')
            out.append(marker(shape, x, y, color))
        if points:
            labels.append((points[-1][1], color, label_of(model_key), shape))

    for y, color, text, shape in dodge(labels, mt + 6, mt + ih):
        out.append(marker(shape, ml + iw + 15, y, color))
        out.append(f'<text class="lab" x="{ml+iw+26}" y="{y+3:.1f}">{esc(text)}</text>')

    out.append(f'<text class="axis" x="{ml+iw/2:.0f}" y="{H-8}" text-anchor="middle">'
               f'silence added, split evenly before and after the speech</text>')
    out.append(f'<text class="axis" x="16" y="{mt+ih/2:.0f}" text-anchor="middle" '
               f'transform="rotate(-90 16 {mt+ih/2:.0f})">billed seconds per request</text>')
    out.append("</svg>")
    return "".join(out)


def billing_probe_html(path):
    if not path.exists():
        return ('<p class="none">Not run yet. '
                '<code>python probe_billing.py --live</code> fills this in.</p>')
    data = json.loads(path.read_text())
    fast = [f"{s:g}" for s in data["speeds"] if s != cfg.BASELINE_SPEED]
    header = "".join(f'<th class="n">Billed at {s}x, share of 1x</th>' for s in fast)
    rows = ""
    for model_key, summary in data["summary"].items():
        cells = ""
        for speed in fast:
            row = summary["proportionality"].get(speed)
            if not row or row["observed_billed_fraction"] is None:
                cells += '<td class="n">n/a</td>'
                continue
            cells += (f'<td class="n {"ok" if row["proportional"] else "bad"}">'
                      f'{row["observed_billed_fraction"]:.4f}'
                      f'<span class="none"> of {row["expected_billed_fraction"]:.4f}</span></td>')
        rows += (f'<tr><td>{esc(label_of(model_key))}</td>'
                 f'<td class="{"ok" if summary["billed_as_sent"] else "bad"}">'
                 f'{"yes" if summary["billed_as_sent"] else "no"}</td>{cells}</tr>')
    tol = data.get("tolerance", 0.02)
    settle = data.get("settle_seconds_observed", {})
    lag = ("not measured" if settle.get("mean") is None
           else f"{settle['mean']:.0f} s mean, {settle['max']:.0f} s worst")
    note = ('<p class="none warn">Synthetic: the billed side was generated from the durations that '
            'were sent, so this run proves the arithmetic, not the billing.</p>'
            ) if data.get("synthetic") else ""
    return (f'<figure class="fig"><div class="chart">{billing_rate_chart(data)}</div>'
            f'<figcaption class="cap"><span class="lbl">Figure 4</span>Bands are the published '
            f'rate divided by r, widened to stay visible; the tolerance they stand for is '
            f'{tol:.0%}. Markers are what the account was billed, per minute of speech as '
            f'spoken. A marker outside its own band is the finding.</figcaption>'
            f'<p class="none">{data["clips_per_window"]} clips per window at '
            f'{", ".join(f"{s:g}x" for s in data["speeds"])}, '
            f'{data["audio_minutes_per_replicate"]:.1f} audio minutes per replicate, '
            f'{len(data["replicates"])} replicates. Windows are isolated by model id, '
            f'requestSource <code>{esc(data["request_source_filter"])}</code> and the minute '
            f'range. Observed analytics settle lag: {lag}.</p>'
            f'<details><summary>The same windows as numbers</summary>'
            f'<div class="tbl-scroll"><table class="data"><thead><tr><th>Model</th>'
            f'<th>Billed for what was sent</th>{header}</tr></thead>'
            f'<tbody>{rows}</tbody></table></div></details>{note}</figure>')


def silence_probe_html(path):
    if not path.exists():
        return ('<p class="none">Not run yet. '
                '<code>python probe_silence.py --live</code> fills this in.</p>')
    data = json.loads(path.read_text())
    paddings = [w["padding_s"] for w in data["windows"]]
    header = "".join(f'<th class="n">+{p:g} s</th>' for p in paddings)
    rows = ""
    for model_key, s in data["summary"].items():
        cells = "".join(
            f'<td class="n">{s["billed_seconds_by_padding"].get(f"{p:g}", 0):.1f}</td>'
            for p in paddings
        )
        slope = s["slope_seconds_per_padding_second"]
        rows += (f'<tr><td>{esc(label_of(model_key))}</td>{cells}'
                 f'<td class="n">{"n/a" if slope is None else f"{slope:.3f}"}</td>'
                 f'<td>{esc(s["verdict"])}</td></tr>')
    note = ('<p class="none warn">Synthetic: the billed side assumes Nova-3 meters speech and the '
            'Whisper family meters files, which is the hypothesis this probe exists to test.</p>'
            ) if data.get("synthetic") else ""
    return (f'<figure class="fig"><div class="chart">{silence_chart(data)}</div>'
            f'<figcaption class="cap"><span class="lbl">Figure 5</span>Billed seconds against '
            f'added silence, with the file duration drawn as reference. A model that meters '
            f'detected speech stays flat; one that meters the file climbs the '
            f'diagonal.</figcaption>'
            f'<p class="none">{data["speech_seconds"]:.1f} s of speech held fixed, padded with '
            f'silence split evenly before and after, {data["repeats"]} requests per cell. '
            f'A slope of 1 means the file is metered, a slope of 0 means only speech is.</p>'
            f'<details><summary>The same paddings as numbers</summary>'
            f'<div class="tbl-scroll"><table class="data"><thead><tr><th>Model</th>{header}'
            f'<th class="n">Slope</th><th>Reads as</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></details>{note}</figure>')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="render the results of a dry run")
    mode.add_argument("--live", action="store_true",
                      help="render the results of a live run")
    parser.add_argument("--results", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    results_path = cfg.RUN_DIR / args.results if args.results else cfg.results_path(args.dry_run)
    out_path = cfg.RUN_DIR / args.out if args.out else cfg.report_path(args.dry_run)
    if not results_path.exists():
        stage = "score.py --dry-run" if args.dry_run else "score.py --live"
        raise SystemExit(f"missing {results_path}; run {stage} first")
    results = json.loads(results_path.read_text())
    response_log.verify_results(results_path, args.dry_run, results)
    grid, corpus = results["grid"], results["corpus"]
    if not grid:
        raise SystemExit(f"{results_path} holds no scored cells")
    models = [m for m in cfg.MODELS if any(r["model"] == m for r in grid)]
    speeds = results["config"]["speeds"]

    delta_series = []
    for model_key in models:
        rows = sorted((r for r in grid if r["model"] == model_key), key=lambda r: r["speed"])
        delta_series.append((model_key,
                             [r["delta_wer"] for r in rows],
                             [tuple(r["delta_ci"]) for r in rows]))

    best = {r["model"]: r for r in grid
            if results["recommended"].get(r["model"]) == r["speed"]}

    def cell_class(row):
        if row["speed"] == cfg.BASELINE_SPEED:
            return ""
        return "ok" if row["passes"] else "bad"

    rows = "".join(
        f'<tr><td>{esc(label_of(r["model"]))}</td>'
        f'<td class="n">{r["speed"]:g}x</td>'
        f'<td class="n">{r["wer"]:.1f}%</td>'
        f'<td class="n">{r["delta_wer"]:+.1f}</td>'
        f'<td class="n">[{r["delta_ci"][0]:.1f}, {r["delta_ci"][1]:.1f}]</td>'
        f'<td class="n">{r["catastrophic"]:.1f}%</td>'
        f'<td class="n">{r["del_rate"]:.1f}%</td>'
        f'<td class="n">${r["usd_per_hour"]:.4f}</td>'
        f'<td class="n">{r["free_minutes_per_day"]:.0f}</td>'
        f'<td class="n">{ms(r["latency_p50"])}</td>'
        f'<td class="{cell_class(r)}">'
        f'{"baseline" if r["speed"] == cfg.BASELINE_SPEED else ("within budget" if r["passes"] else "over budget")}'
        f'</td></tr>'
        for r in grid
    )

    rate_rows = "".join(
        f'<tr><td>{esc(label_of(r["model"]))}</td>'
        f'<td class="n">{r["wpm_lo"]} to {r["wpm_hi"]}</td>'
        f'<td class="n">{r["n"]}</td><td class="n">{r["wer"]:.1f}%</td></tr>'
        for r in results["rate_curve"]
    )
    rate_table = (
        f'<figure class="fig"><div class="chart">{rate_chart(results["rate_curve"], models)}</div>'
        f'<figcaption class="cap"><span class="lbl">Figure 3</span>Error against the speaking '
        f'rate that actually reaches the model. Bars behind the lines are how many utterances '
        f'fall in each band: the outer bands hold a fraction of what the middle ones do, so the '
        f'ends of every curve are the softest points on it.</figcaption>'
        f'<details><summary>The same bands as numbers</summary>'
        f'<div class="tbl-scroll"><table class="data"><thead><tr><th>Model</th>'
        f'<th class="n">Effective wpm</th>'
        f'<th class="n">Utterances</th><th class="n">WER</th></tr></thead>'
        f'<tbody>{rate_rows}</tbody></table></div></details></figure>'
    ) if rate_rows else (
        '<p class="none">No speaking-rate band held enough utterances to report.</p>'
    )

    banner = ""
    if results.get("synthetic"):
        banner = (f'<div class="notice"><span class="tag">Dry run</span>'
                  f'<p>{results["synthetic"]} of {results["responses"]} responses were synthesised '
                  f'by <code>run.py --dry-run</code>. Every accuracy number below is the shape of '
                  f'the pipeline, not a measurement of any model.</p></div>')

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cloud-dictation: audio compression versus transcription cost</title>
<style>{STYLE}</style></head><body><main class="report">

<h1>Audio compression versus transcription accuracy</h1>
<p class="project-meta">LibriSpeech test-clean · {corpus["utterances"]} utterances ·
{len(speeds)} compression factors · {len(models)} models · seed {cfg.SAMPLE_SEED}</p>

<div class="abstract"><h2>Abstract</h2>
<p>Cloudflare bills speech to text per audio minute, so compressing a recording by a factor
r cuts that bill by exactly 1 − 1/r. What compression costs in accuracy is not arithmetic,
and this is the measurement of it: {corpus["utterances"]} utterances of
{esc(results["config"]["corpus"])}, {corpus["total_minutes"]:.1f} minutes of speech, put
through {len(models)} models at {len(speeds)} speeds. Cost is computed from the published
per-minute rates; only accuracy and latency are measured.</p></div>
{banner}

<h3><span class="num">1</span>Verdict</h3>
{"".join(f"<p>{esc(line)}</p>" for line in verdict_text(results))}
<p>A speed is recommended when the error increase is at most {cfg.DELTA_WER_BUDGET:.1f}
percentage points, the upper bound of its 95% interval stays under
{cfg.DELTA_WER_CI_CEILING:.1f}, and the catastrophic rate rises by at most
{cfg.CATASTROPHIC_BUDGET:.1f} points.</p>
<div class="tiles">
{"".join(
    f'<div class="tile"><div class="k">{esc(label_of(m))}</div>'
    f'<div class="v">{(f"{best[m]['speed']:g}x" if m in best else "1x")}</div>'
    f'<div class="d">{(f"{best[m]['saving_pct']:.0f}% off at +{best[m]['delta_wer']:.1f} pt" if m in best else "compression does not pay")}</div></div>'
    for m in models)}
</div>

<h3><span class="num">2</span>The degradation curve</h3>
<figure class="fig"><div class="chart">{line_chart(delta_series, speeds,
    "ΔWER, percentage points", "compression factor r", budget=cfg.DELTA_WER_BUDGET)}</div>
<figcaption class="cap"><span class="lbl">Figure 1</span>Change in error rate against each
model's own 1x baseline, with 95% paired-bootstrap bands over utterances. Read the shape,
not the cell: the budget is one point on an axis that runs to tens, so a pass or fail on any
single cell comes from the grid in section 4.</figcaption></figure>

<h3><span class="num">3</span>The frontier</h3>
<figure class="fig"><div class="chart">{frontier_chart(grid, models)}</div>
<figcaption class="cap"><span class="lbl">Figure 2</span>Error against cost per hour of real
speech, each line walking one model from {speeds[0]:g}x to {speeds[-1]:g}x. Down and to the
left is better. Only the vertical axis is measured; the horizontal one is arithmetic on the
published rates.</figcaption></figure>

<h3><span class="num">4</span>Full grid</h3>
<figure class="tbl"><div class="tbl-scroll"><table class="data">
<thead><tr><th>Model</th><th class="n">r</th><th class="n">WER</th><th class="n">ΔWER</th>
<th class="n">95% CI</th><th class="n">Catastrophic</th><th class="n">Deletions</th>
<th class="n">$/hr</th><th class="n">Free min/day</th>
<th class="n">p50</th><th>Verdict</th></tr></thead><tbody>{rows}</tbody></table></div>
<figcaption class="cap"><span class="lbl">Table 1</span>Every cell of the experiment. The
verdict column applies the acceptance rule in section 1; a speed is only recommended if every
slower speed also passed.</figcaption></figure>

<h3><span class="num">5</span>Error against effective speaking rate</h3>
<p>A compression factor is not a physical quantity. What reaches the model is a speaking
rate, and that is what transfers to a speaker whose natural pace differs from this corpus.</p>
<div class="eq"><span class="expr">wpm_eff(u, r) = r · wpm₀(u)</span>
<span class="reading">Effective words per minute equals the compression factor times the
utterance's own baseline rate.</span></div>
{rate_table}

<h3><span class="num">6</span>What breaking looks like</h3>
<div class="panel">{gallery_html(results.get("gallery", []))}</div>
<p class="none">The worst utterance for each model at {speeds[-1]:g}x, against its reference.
A catastrophic rate is a count; this is what it is a count of.</p>

<h3><span class="num">7</span>Billing checks</h3>
<p>Cost here is arithmetic on the published rates. These two probes are what stop that
arithmetic from resting on an assumption.</p>
<h4>P1 · Do billed seconds fall with compression?</h4>
{billing_probe_html(cfg.BILLING_PROBE_RESULT)}
<h4>P2 · Is silence billed?</h4>
{silence_probe_html(cfg.SILENCE_PROBE_RESULT)}

<h3><span class="num">8</span>What was actually transcribed</h3>
<figure class="tbl"><div class="tbl-scroll"><table class="data">
<thead><tr><th>Corpus</th><th>Source</th><th class="n">Utterances</th><th class="n">Total audio</th>
<th class="n">Mean duration</th><th class="n">Reference words</th><th class="n">Median rate</th></tr></thead>
<tbody><tr><td>LibriSpeech</td><td>test-clean split, seed {cfg.SAMPLE_SEED}</td>
<td class="n">{corpus["utterances"]}</td><td class="n">{corpus["total_minutes"]:.1f} min</td>
<td class="n">{corpus["mean_duration_s"]:.1f} s</td><td class="n">{corpus["words"]:,}</td>
<td class="n">{corpus["wpm_median"]:.0f} wpm</td></tr></tbody></table></div>
<figcaption class="cap"><span class="lbl">Table 2</span>Sampled with a fixed seed, capped at
{cfg.MAX_UTTERANCE_SECONDS:g} s per utterance, no other filtering. Every cell above is scored
on these same {corpus["utterances"]} utterances, which is what makes the paired intervals
valid.</figcaption></figure>
<figure class="fig"><div class="chart">{histogram(corpus["wpm_bins"], corpus["wpm_histogram"],
    corpus["wpm_median"], "baseline words per minute")}</div>
<figcaption class="cap"><span class="lbl">Figure 6</span>How fast the corpus speaks before
any compression, from {corpus["wpm_min"]:.0f} to {corpus["wpm_max"]:.0f} wpm. Multiply this
distribution by r to see what each speed actually asks a model to follow.</figcaption></figure>

<h3><span class="num">9</span>What this test set can and cannot support</h3>
<ul class="findings">
<li><strong>Supports.</strong> A calibrated answer for read audiobook speech in studio
conditions. LibriSpeech test-clean has published Whisper numbers, so a wrong absolute WER at
1x would have exposed a broken harness before any compression result was trusted.</li>
<li><strong>Does not support.</strong> Consumer microphones, room noise, non-native accents,
spontaneous disfluency, the app's own recording path, and product-name accuracy. Every number
here is a best case. A speed that looks safe on this corpus is not yet a claim that it is safe
on a laptop microphone.</li>
<li><strong>Known unfairness.</strong> All four models likely saw LibriSpeech-like audio in
training, which flatters the 1x baselines. The report leads with ΔWER so that flattery
cancels.</li>
</ul>

<p class="project-meta" style="margin-top:2.4rem">Generated from
<code>{esc(results_path.name)}</code>. Rates from <code>src/core/models.js</code>.
{esc(results["failures"])} failed responses excluded.</p>
</main></body></html>"""

    out_path.write_text(doc)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()

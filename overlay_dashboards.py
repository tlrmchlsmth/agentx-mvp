#!/usr/bin/env python3
import argparse
import html
import importlib.util
import json
import os
import re
from pathlib import Path


def _load_common():
    path = Path(__file__).with_name("dashboard_common.py")
    spec = importlib.util.spec_from_file_location("agentx_dashboard_common", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Missing shared dashboard plumbing: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COMMON = _load_common()


def extract_data(html_path):
    # Prefer the JSON sidecar written next to the page by export_dashboard;
    # fall back to scraping the page for artifacts that predate it.
    sidecar = os.path.splitext(html_path)[0] + ".json"
    if os.path.exists(sidecar):
        with open(sidecar) as f:
            payload = json.load(f)
        return payload["panels"], payload.get("rows", [])
    with open(html_path) as f:
        content = f.read()
    panels_match = re.search(r'const panels = ({.*?});\s*\n\s*const rows', content, re.DOTALL)
    rows_match = re.search(r'const rows = (\[.*?\]);\s*\n', content, re.DOTALL)
    if not panels_match:
        raise ValueError(f"Could not extract panel data from {html_path}")
    panels = json.loads(panels_match.group(1))
    rows = json.loads(rows_match.group(1)) if rows_match else []
    return panels, rows


def guess_label(path):
    dirname = os.path.basename(os.path.dirname(path))
    return dirname


def merge(file_data):
    all_panel_ids = set()
    for panels, _, _ in file_data:
        all_panel_ids.update(panels.keys())

    merged = {}
    for pid in all_panel_ids:
        entries = []
        for panels, _, label in file_data:
            if pid in panels:
                entries.append({"label": label, "panel": panels[pid]})
        if entries:
            merged[pid] = {
                "title": entries[0]["panel"]["title"],
                "unit": entries[0]["panel"]["unit"],
                "entries": entries,
            }
    return merged


def generate_html(merged, rows, labels, plotly_bundle=None, *, notes=None):
    # A merged view must keep every input distinguishable.  In particular,
    # do not collapse repeated c<N> labels into one color/filter entry.
    labels = list(dict.fromkeys(labels))
    merged_json = json.dumps(merged)
    rows_json = json.dumps(rows)
    labels_json = json.dumps(labels)
    notice_html = "".join(f'<p class="subtitle">{html.escape(note)}</p>' for note in (notes or []))

    plotly_tag = COMMON.plotly_tag(plotly_bundle)
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Overlay — {', '.join(labels)}</title>
{plotly_tag}
<style>
{COMMON.BASE_CSS}  .toolbar {{ position: sticky; top: 0; z-index: 20; display: flex; align-items: center; flex-wrap: wrap; gap: 8px;
              margin: 0 0 12px; padding: 9px 10px; background: rgba(17,18,23,.96); border: 1px solid #2a2a2e; border-radius: 5px; }}
  .toolbar-label {{ color: #8e8e8e; font-size: 12px; margin-right: 2px; }}
  #concurrencyFilters {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  button {{ border: 1px solid #3a3a3e; border-radius: 4px; background: #1e2127; color: #a9a9a9; padding: 4px 9px; cursor: pointer; }}
  button:hover {{ color: #fff; border-color: #666; }}
  .concurrency-filter.active {{ color: #fff; border-color: var(--filter-color); box-shadow: inset 0 0 0 1px var(--filter-color); }}
</style>
</head>
<body>
<h1>Overlay Dashboard</h1>
<div class="subtitle">{', '.join(labels)}</div>
{notice_html}
<div class="toolbar"><span class="toolbar-label">Run / concurrency</span><span id="concurrencyFilters"></span><button id="showAll">All</button><button id="showNone">None</button></div>
<div id="root"></div>
<script>
{COMMON.PARENT_PLOTLY_SHIM}const merged = {merged_json};
const rows = {rows_json};
const labels = {labels_json};

const LABEL_COLORS = {{}};
const BASE_COLORS = [
  [239,83,80], [255,167,38], [255,213,79], [102,187,106],
  [38,198,218], [66,165,245], [126,87,194], [236,64,122],
  [0,150,136], [141,110,99], [171,71,188], [124,179,66],
  [255,112,67], [38,166,154], [92,107,192], [244,143,177],
  [121,134,203], [255,202,40], [0,172,193], [156,204,101],
];
labels.forEach((l, i) => {{ LABEL_COLORS[l] = BASE_COLORS[i % BASE_COLORS.length]; }});
const activeLabels = new Set(labels);
const SERIES_COLORS = {{}};
let nextSeriesColor = 0;
function colorForSeries(key) {{
  if (!SERIES_COLORS[key]) {{
    SERIES_COLORS[key] = BASE_COLORS[nextSeriesColor % BASE_COLORS.length];
    nextSeriesColor++;
  }}
  return SERIES_COLORS[key];
}}

// Keep the legend readable without losing the full identity.  A label such
// as "sweep-id / c4-r1" becomes "c4-r1" when unique, or "run2 · c4-r1"
// when multiple sweeps contain the same concurrency/rerun point.  The full
// label remains available as a toolbar tooltip and in each trace hovercard.
const SHORT_LABELS = {{}};
const pointCounts = {{}};
const sourceIds = {{}};
labels.forEach(label => {{
  const split = label.lastIndexOf(' / ');
  const point = split >= 0 ? label.slice(split + 3) : label;
  pointCounts[point] = (pointCounts[point] || 0) + 1;
}});
labels.forEach(label => {{
  const split = label.lastIndexOf(' / ');
  if (split < 0) {{ SHORT_LABELS[label] = label; return; }}
  const source = label.slice(0, split);
  const point = label.slice(split + 3);
  if (!sourceIds[source]) sourceIds[source] = Object.keys(sourceIds).length + 1;
  SHORT_LABELS[label] = pointCounts[point] === 1 ? point : `run${{sourceIds[source]}} · ${{point}}`;
}});
function shortLabel(label) {{ return SHORT_LABELS[label] || label; }}

function applyConcurrencyFilter() {{
  document.querySelectorAll('.plot[data-ready="true"]').forEach(plot => {{
    const visible = (plot._traceConcurrencies || []).map(label => activeLabels.has(label));
    if (visible.length) Plotly.restyle(plot, {{ visible }});
  }});
  document.querySelectorAll('.concurrency-filter').forEach(button => button.classList.toggle('active', activeLabels.has(button.dataset.label)));
}}

const filterRoot = document.getElementById('concurrencyFilters');
labels.forEach(label => {{
  const button = document.createElement('button');
  button.className = 'concurrency-filter active';
  button.dataset.label = label;
  button.textContent = shortLabel(label);
  button.title = label;
  button.style.setProperty('--filter-color', rgbStr(LABEL_COLORS[label], 1));
  button.addEventListener('click', () => {{ activeLabels.has(label) ? activeLabels.delete(label) : activeLabels.add(label); applyConcurrencyFilter(); }});
  filterRoot.appendChild(button);
}});
document.getElementById('showAll').addEventListener('click', () => {{ labels.forEach(label => activeLabels.add(label)); applyConcurrencyFilter(); }});
document.getElementById('showNone').addEventListener('click', () => {{ activeLabels.clear(); applyConcurrencyFilter(); }});

function compactText(value, maxLength = 26) {{
  const text = String(value || '');
  return text.length <= maxLength ? text : '…' + text.slice(-(maxLength - 1));
}}

function seriesName(label, q, s, seriesIndex) {{
  const base = shortLabel(label);
  let legend = q.legend;
  if (legend) {{
    for (const [k,v] of Object.entries(s.labels)) legend = legend.replace('{{{{'+k+'}}}}', v);
    if (!legend.includes('{{{{') && legend.trim()) {{
      const parts = legend.split('/').map(part => part.trim()).filter(Boolean);
      if (parts.length) return base + ' / ' + compactText(parts[parts.length - 1]);
    }}
  }}
  const preferred = ['quantile', 'percentile', 'stat', 'phase', 'pod', 'instance', 'engine'];
  const parts = preferred
    .filter(key => s.labels[key] != null && s.labels[key] !== '')
    .map(key => `${{key}}=${{compactText(s.labels[key])}}`);
  const suffix = parts.length ? parts.slice(0, 2).join(', ') : `series ${{seriesIndex + 1}}`;
  return base + ' / ' + suffix;
}}

function rgbStr(rgb, alpha) {{
  return alpha < 1 ? `rgba(${{rgb[0]}},${{rgb[1]}},${{rgb[2]}},${{alpha}})` : `rgb(${{rgb[0]}},${{rgb[1]}},${{rgb[2]}})`;
}}

const root = document.getElementById('root');
{COMMON.PANEL_JS}
// Building dozens of Plotly charts at page load blocks the overlay window.
// Render each chart when it approaches the viewport instead.
const panelObserver = new IntersectionObserver((entries, observer) => {{
  for (const entry of entries) {{
    if (!entry.isIntersecting) continue;
    observer.unobserve(entry.target);
    entry.target.renderPlot();
    delete entry.target.renderPlot;
  }}
}}, {{ rootMargin: '300px 0px' }});

scaffoldRows(rows, merged, renderPanel);

function renderPanel(container, pid, m) {{
  const div = document.createElement('div');
  div.className = 'panel';
  div.innerHTML = panelHeaderHtml(m.title);

  let hasAny = false;
  for (const e of m.entries) {{
    for (const q of e.panel.queries) {{
      for (const s of q.series) {{
        if (s.values.length > 0 && s.values.some(v => !isNaN(parseFloat(v[1])))) hasAny = true;
      }}
    }}
  }}

  if (!hasAny) {{
    div.innerHTML += '<div class="empty">No data</div>';
    container.appendChild(div);
    return;
  }}

  const plotDiv = document.createElement('div');
  plotDiv.className = 'plot';
  div.appendChild(plotDiv);
  container.appendChild(div);

  div.renderPlot = () => {{
  const traces = [];
  const traceConcurrencies = [];
  let traceIndex = 0;
  const allY = [];
  for (const e of m.entries) for (const q of e.panel.queries) for (const s of q.series)
    for (const v of s.values) {{ const value = parseFloat(v[1]); if (Number.isFinite(value)) allY.push(value); }}
  const largestY = allY.reduce((largest, value) => Math.max(largest, value), -Infinity);
  const smallestY = allY.reduce((smallest, value) => Math.min(smallest, value), Infinity);
  const decimals = yTickDecimals(largestY, smallestY);
  const yFormat = `,.${{decimals}}f`;
  const formattedLargest = largestY.toLocaleString('en-US', {{minimumFractionDigits: decimals, maximumFractionDigits: decimals}});
  const formattedSmallest = smallestY.toLocaleString('en-US', {{minimumFractionDigits: decimals, maximumFractionDigits: decimals}});
  const leftMargin = Math.max(76, (Math.max(formattedLargest.length, formattedSmallest.length) + 2) * 8);
  const hoverFormat = '%{{y:' + yFormat + '}}<extra>%{{fullData.name}}<br>%{{customdata}}</extra>';
  for (const e of m.entries) {{
    let si = 0;
    for (const q of e.panel.queries) {{
      for (const s of q.series) {{
        if (s.values.length === 0) continue;
        const t0 = s.values[0][0];
        const name = seriesName(e.label, q, s, si);
        const rgb = colorForSeries(`${{e.label}}::${{name}}`);
        traces.push({{
          x: s.values.map(v => (v[0] - t0)),
          y: s.values.map(v => parseFloat(v[1])),
          name,
          type: 'scatter',
          mode: 'lines',
          customdata: s.values.map(() => e.label),
          // Every Prometheus series from this run/concurrency gets the same
          // solid color.  The legend label identifies the individual series;
          // opacity shades make related metrics harder to compare.
          line: {{ width: 1.5, color: rgbStr(rgb, 1) }},
          // Keep legend clicks independent.  The toolbar still filters all
          // traces belonging to the same run/concurrency label together.
          legendgroup: `${{e.label}}::${{traceIndex}}`,
          hovertemplate: hoverFormat,
        }});
        traceConcurrencies.push(e.label);
        traceIndex++;
        si++;
      }}
    }}
  }}

  Plotly.newPlot(plotDiv, traces, {{
    margin: {{ l: leftMargin, r: 16, t: 4, b: 30 }},
    paper_bgcolor: 'transparent',
    plot_bgcolor: 'transparent',
    font: {{ color: '#8e8e8e', size: 10 }},
    xaxis: {{ gridcolor: '#2a2a2e', linecolor: '#2a2a2e', title: 'seconds', tickformat: ',d', exponentformat: 'none', showexponent: 'none' }},
    yaxis: {{ gridcolor: '#2a2a2e', linecolor: '#2a2a2e', tickformat: yFormat, hoverformat: yFormat, exponentformat: 'none', showexponent: 'none', separatethousands: true, automargin: true }},
    legend: {{ font: {{ size: 10 }}, orientation: 'v', x: 1, xanchor: 'right', y: 1, yanchor: 'top', maxheight: 0.45, itemclick: 'toggle', itemdoubleclick: 'toggleothers', bgcolor: 'rgba(17,18,23,.92)', bordercolor: '#3a3a3e', borderwidth: 1 }},
    // Keep the legend available so every individual run/concurrency series
    // can be clicked on or off.  The Legend button remains useful for
    // compacting dense panels.
    showlegend: true,
    hovermode: 'x unified',
    uirevision: pid,
  }}, {{ responsive: true, edits: {{ legendPosition: true }}, displayModeBar: true, displaylogo: false, scrollZoom: true, modeBarButtonsToRemove: ['lasso2d','select2d'] }}).then(() => {{
    plotDiv._traceConcurrencies = traceConcurrencies;
    plotDiv.dataset.ready = 'true';
    makePanelInteractive(div, plotDiv);
    applyConcurrencyFilter();
  }});
  }};
  panelObserver.observe(div);
}}
</script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Overlay multiple dashboard HTML exports into one comparison view")
    parser.add_argument("files", nargs="+", help="HTML dashboard files to overlay")
    parser.add_argument("--label", action="append", help="Labels for each file (default: auto-detect from directory name)")
    parser.add_argument("--output", "-o", default="overlay.html", help="Output HTML file (default: overlay.html)")
    parser.add_argument("--plotly-bundle", help="gzip-compressed Plotly JS to embed for offline output")
    args = parser.parse_args()

    if args.label and len(args.label) != len(args.files):
        parser.error(f"Got {len(args.label)} labels for {len(args.files)} files")

    file_data = []
    labels = []
    for i, path in enumerate(args.files):
        label = args.label[i] if args.label else guess_label(path)
        labels.append(label)
        print(f"  [{label}] {path}")
        panels, rows = extract_data(path)
        file_data.append((panels, rows, label))

    merged = merge(file_data)
    rows = file_data[0][1]

    html = generate_html(merged, rows, labels, args.plotly_bundle)
    with open(args.output, "w") as f:
        f.write(html)

    panel_count = len([m for m in merged.values() if any(
        s["values"] for e in m["entries"] for q in e["panel"]["queries"] for s in q["series"]
    )])
    print(f"\nOverlaid {len(labels)} runs across {panel_count} panels → {args.output}")


if __name__ == "__main__":
    main()

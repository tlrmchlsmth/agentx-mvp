#!/usr/bin/env python3
import argparse
import gzip
import json
import os
import re


def extract_data(html_path):
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
    m = re.search(r'c(\d+)', dirname)
    if m:
        return f"c{m.group(1)}"
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


def generate_html(merged, rows, labels, plotly_bundle=None):
    merged_json = json.dumps(merged)
    rows_json = json.dumps(rows)
    labels_json = json.dumps(labels)

    plotly_tag = '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>'
    if plotly_bundle:
        with gzip.open(plotly_bundle, "rt", encoding="utf-8") as f:
            plotly_tag = "<script>" + f.read() + "</script>"
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Overlay — {', '.join(labels)}</title>
{plotly_tag}
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #111217; color: #d8d9da; font-family: Inter, -apple-system, sans-serif; padding: 16px; }}
  h1 {{ font-size: 20px; font-weight: 500; margin-bottom: 4px; }}
  .subtitle {{ font-size: 13px; color: #8e8e8e; margin-bottom: 20px; }}
  .row-header {{ font-size: 15px; font-weight: 500; color: #d8d9da; padding: 10px 0 6px 4px;
                 border-bottom: 1px solid #2a2a2e; margin: 16px 0 8px 0; cursor: pointer; user-select: none; }}
  .row-header:hover {{ color: #fff; }}
  .row-header .arrow {{ display: inline-block; width: 16px; transition: transform .15s; }}
  .row-header.collapsed .arrow {{ transform: rotate(-90deg); }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(580px, 1fr)); gap: 8px; }}
  .panel {{ background: #181b1f; border: 1px solid #2a2a2e; border-radius: 4px; overflow: hidden; }}
  .panel-title {{ font-size: 13px; font-weight: 500; padding: 8px 12px; color: #d8d9da; }}
  .panel .plot {{ width: 100%; height: 250px; }}
  .empty {{ color: #555; font-size: 12px; padding: 60px 12px; text-align: center; }}
  .hidden {{ display: none; }}
</style>
</head>
<body>
<h1>Overlay Dashboard</h1>
<div class="subtitle">{', '.join(labels)}</div>
<div id="root"></div>
<script>
const merged = {merged_json};
const rows = {rows_json};
const labels = {labels_json};

const LABEL_COLORS = {{}};
const BASE_COLORS = [
  [126,178,109], [234,184,57], [110,208,224], [239,132,60],
  [226,77,66], [31,120,193], [186,67,169], [112,93,160],
];
labels.forEach((l, i) => {{ LABEL_COLORS[l] = BASE_COLORS[i % BASE_COLORS.length]; }});

function seriesName(label, q, s, totalSeries) {{
  let legend = q.legend;
  if (legend) {{
    for (const [k,v] of Object.entries(s.labels)) legend = legend.replace('{{{{'+k+'}}}}', v);
    if (!legend.includes('{{{{')) return totalSeries > 1 ? label + ' / ' + legend : label;
  }}
  const parts = Object.entries(s.labels).filter(([k]) => k !== '__name__');
  const suffix = parts.length ? parts.map(([k,v])=>v).join(',') : '';
  return suffix ? label + ' / ' + suffix : label;
}}

function rgbStr(rgb, alpha) {{
  return alpha < 1 ? `rgba(${{rgb[0]}},${{rgb[1]}},${{rgb[2]}},${{alpha}})` : `rgb(${{rgb[0]}},${{rgb[1]}},${{rgb[2]}})`;
}}

const root = document.getElementById('root');
let currentGrid = null;

if (rows.length === 0) {{
  currentGrid = document.createElement('div');
  currentGrid.className = 'grid';
  root.appendChild(currentGrid);
  for (const pid of Object.keys(merged)) renderPanel(currentGrid, pid, merged[pid]);
}} else {{
  for (const item of rows) {{
    if (item.type === 'row') {{
      const h = document.createElement('div');
      h.className = 'row-header';
      h.innerHTML = '<span class="arrow">▾</span> ' + item.title;
      currentGrid = document.createElement('div');
      currentGrid.className = 'grid';
      h.addEventListener('click', () => {{
        h.classList.toggle('collapsed');
        currentGrid.classList.toggle('hidden');
      }});
      root.appendChild(h);
      root.appendChild(currentGrid);
    }} else if (item.type === 'panel' && merged[item.id]) {{
      if (!currentGrid) {{
        currentGrid = document.createElement('div');
        currentGrid.className = 'grid';
        root.appendChild(currentGrid);
      }}
      renderPanel(currentGrid, item.id, merged[item.id]);
    }}
  }}
}}

function renderPanel(container, pid, m) {{
  const div = document.createElement('div');
  div.className = 'panel';
  div.innerHTML = '<div class="panel-title">' + m.title + '</div>';

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

  const traces = [];
  for (const e of m.entries) {{
    const rgb = LABEL_COLORS[e.label] || [200,200,200];
    const totalSeries = e.panel.queries.reduce((n, q) => n + q.series.length, 0);
    let si = 0;
    for (const q of e.panel.queries) {{
      for (const s of q.series) {{
        if (s.values.length === 0) continue;
        const t0 = s.values[0][0];
        const alpha = totalSeries > 1 ? 0.5 + 0.5 * (si / Math.max(totalSeries - 1, 1)) : 1;
        traces.push({{
          x: s.values.map(v => (v[0] - t0)),
          y: s.values.map(v => parseFloat(v[1])),
          name: seriesName(e.label, q, s, totalSeries),
          type: 'scatter',
          mode: 'lines',
          line: {{ width: 1.5, color: rgbStr(rgb, alpha) }},
          hovertemplate: '%{{y:.4g}}<extra>%{{fullData.name}}</extra>',
        }});
        si++;
      }}
    }}
  }}

  Plotly.newPlot(plotDiv, traces, {{
    margin: {{ l: 50, r: 16, t: 4, b: 30 }},
    paper_bgcolor: 'transparent',
    plot_bgcolor: 'transparent',
    font: {{ color: '#8e8e8e', size: 10 }},
    xaxis: {{ gridcolor: '#2a2a2e', linecolor: '#2a2a2e', title: 'seconds', tickformat: 'd' }},
    yaxis: {{ gridcolor: '#2a2a2e', linecolor: '#2a2a2e', tickformat: ',.4f', hoverformat: ',.4f' }},
    legend: {{ font: {{ size: 9 }}, orientation: 'h', y: -0.35 }},
    showlegend: true,
    hovermode: 'x unified',
  }}, {{ responsive: true, displayModeBar: false }});
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

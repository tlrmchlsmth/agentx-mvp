#!/usr/bin/env python3
"""
Scan results/ for deployment configs, extract aiperf metrics,
and generate an interactive interactivity-vs-throughput HTML chart.

Usage:
    python3 gen_interactivity_chart.py [results_dir]

Defaults to ./results if no argument given. Output: interactivity_vs_throughput.html
in the same directory as this script (or parent of results_dir).
"""

import base64
import json
import os
import re
import sys
from pathlib import Path

GPUS_PER_NODE = 8
STAT_KEYS = ['avg', 'min', 'p50', 'p90', 'p95', 'p99', 'max']

COLORS = [
    '#f97316', '#22d3ee', '#a78bfa', '#34d399', '#f472b6',
    '#facc15', '#fb923c', '#38bdf8', '#c084fc', '#4ade80',
]


def discover_configs(results_dir):
    """Auto-discover deployment configs and concurrency levels from folder structure.

    Expected layout (PR:PW:DR:DW format):
        results_dir/
            results_p1w1_d1w1/
                results_p1w1_d1w1_c1/profile_export_aiperf.json
                results_p1w1_d1w1_c4/...
            results_p1w1_d1w2/
                ...
    """
    configs = {}
    metric_units = {}
    config_pattern = re.compile(r'^results_(p(\d+)w(\d+)_d(\d+)w(\d+))$')

    for entry in sorted(os.listdir(results_dir)):
        m = config_pattern.match(entry)
        if not m:
            continue
        config_name = m.group(1)
        pr = int(m.group(2))
        pw = int(m.group(3))
        dr = int(m.group(4))
        dw = int(m.group(5))
        n_prefill_nodes = pr * pw
        n_decode_nodes = dr * dw
        decode_gpus = n_decode_nodes * GPUS_PER_NODE

        config_dir = os.path.join(results_dir, entry)
        if not os.path.isdir(config_dir):
            continue

        conc_pattern = re.compile(rf'^results_{re.escape(config_name)}_c(\d+)$')
        runs = {}
        for sub in sorted(os.listdir(config_dir)):
            cm = conc_pattern.match(sub)
            if not cm:
                continue
            c_val = int(cm.group(1))
            json_path = os.path.join(config_dir, sub, 'profile_export_aiperf.json')
            if not os.path.isfile(json_path):
                continue
            with open(json_path) as f:
                d = json.load(f)

            run_data = {}
            for key, val in d.items():
                if isinstance(val, dict) and 'avg' in val:
                    run_data[key] = {s: val[s] for s in STAT_KEYS if s in val}
                    if key not in metric_units:
                        metric_units[key] = val.get('unit', '')

            logs_dir = os.path.join(config_dir, sub, 'logs')
            if os.path.isdir(logs_dir):
                for role in ('prefill', 'decode'):
                    for lf in os.listdir(logs_dir):
                        if role in lf and lf.endswith('.log'):
                            lpath = os.path.join(logs_dir, lf)
                            with open(lpath) as lfile:
                                for line in lfile:
                                    m_kv = re.search(r'GPU KV cache size:\s*([\d,]+)\s*tokens', line)
                                    if m_kv:
                                        run_data[f'_kv_cache_{role}'] = int(m_kv.group(1).replace(',', ''))
                                        break
                            if f'_kv_cache_{role}' in run_data:
                                break

            runs[c_val] = run_data

        if not runs:
            continue

        version = ''
        ver_path = os.path.join(config_dir, 'vllm_version.txt')
        if os.path.isfile(ver_path):
            with open(ver_path) as vf:
                version = vf.read().strip()

        yamls = {}
        for yname in sorted(os.listdir(config_dir)):
            if yname.endswith('.yaml') or yname.endswith('.yml'):
                ypath = os.path.join(config_dir, yname)
                if os.path.isfile(ypath):
                    with open(ypath) as yf:
                        yamls[yname] = yf.read()

        prefill_gpus = n_prefill_nodes * GPUS_PER_NODE
        label = f'{pr}P{"×"+str(pw) if pw > 1 else ""} {dr}D{"×"+str(dw) if dw > 1 else ""}'
        configs[config_name] = {
            'label': label,
            'decode_gpus': decode_gpus,
            'prefill_gpus': prefill_gpus,
            'pods': f'{pr}×{pw} prefill + {dr}×{dw} decode ({n_prefill_nodes + n_decode_nodes} nodes)',
            'runs': dict(sorted(runs.items())),
            'version': version,
            'yamls': yamls,
        }

    return configs, metric_units


def highlight_yaml(text):
    import html as htmlmod
    lines = text.split('\n')
    out = []
    for line in lines:
        escaped = htmlmod.escape(line)
        if not escaped.strip():
            out.append(escaped)
            continue
        if escaped.lstrip().startswith('#'):
            out.append(re.sub(r'(#.*)', r'<span class="yc">\1</span>', escaped))
            continue
        m = re.match(r'^(\s*)(- )?([A-Za-z_][\w./\-]*):(.*)$', escaped)
        if m:
            indent, dash, key, rest = m.groups()
            dash = dash or ''
            rest = rest.strip()
            if rest.startswith('#'):
                rest = f' <span class="yc">{rest}</span>'
            elif rest.startswith('"') or rest.startswith('&#x27;') or rest.startswith("'"):
                rest = f' <span class="ys">{rest}</span>'
            elif re.match(r'^-?\d[\d.]*$', rest):
                rest = f' <span class="yn">{rest}</span>'
            elif rest in ('true', 'false', 'null', 'True', 'False', 'None', '""', "''"):
                rest = f' <span class="yn">{rest}</span>'
            elif rest == '|' or rest == '>':
                rest = f' <span class="yv">{rest}</span>'
            elif rest:
                rest = f' <span class="yv">{rest}</span>'
            out.append(f'{indent}{dash}<span class="yk">{key}</span>:{rest}')
        else:
            out.append(escaped)
    return '\n'.join(out)


def generate_html(configs, output_path, results_dir, metric_units):
    color_map = {}
    for i, cfg in enumerate(sorted(configs.keys())):
        color_map[cfg] = COLORS[i % len(COLORS)]

    configs_js = {}
    data_js = {}
    concurrencies = set()
    for cfg, meta in configs.items():
        configs_js[cfg] = {
            'label': meta['label'],
            'decodeGPUs': meta['decode_gpus'],
            'prefillGPUs': meta['prefill_gpus'],
            'pods': meta['pods'],
        }
        data_js[cfg] = {}
        for c_val, metrics in meta['runs'].items():
            key = f'c{c_val}'
            concurrencies.add(c_val)
            data_js[cfg][key] = metrics

    sorted_conc = sorted(concurrencies)
    conc_list_js = [f'c{c}' for c in sorted_conc]
    c_labels_js = {f'c{c}': c for c in sorted_conc}

    metrics_js = {k: {'unit': v} for k, v in sorted(metric_units.items())}

    versions = set(meta['version'] for meta in configs.values() if meta['version'])
    version_str = ', '.join(sorted(versions)) if versions else 'unknown'

    results_dir = os.path.abspath(results_dir)
    embedded_dashboards = {}
    for cfg in configs:
        for c_val in configs[cfg]['runs']:
            key = f'c{c_val}'
            dash_path = os.path.join(results_dir, f'results_{cfg}', f'results_{cfg}_{key}', 'dashboard.html')
            if os.path.isfile(dash_path):
                with open(dash_path, 'rb') as df:
                    embedded_dashboards[f'{cfg}_{key}'] = base64.b64encode(df.read()).decode('ascii')

    yaml_parts = []
    configs_with_yamls = {cfg: meta for cfg, meta in sorted(configs.items()) if meta.get('yamls')}
    if configs_with_yamls:
        yaml_parts.append('<div class="row-header" onclick="this.classList.toggle(\'collapsed\');'
                          'this.nextElementSibling.classList.toggle(\'hidden\')">'
                          '<span class="arrow">&#9660;</span> Pod Specs (YAML)</div>')
        yaml_parts.append('<div style="padding: 0 4px;">')
        for cfg in sorted(configs_with_yamls):
            meta = configs_with_yamls[cfg]
            color = color_map.get(cfg, '#d8d9da')
            for yname, ycontent in sorted(meta['yamls'].items()):
                uid = f'yaml-{cfg}-{yname.replace(".", "-")}'
                label = f'{meta["label"]} — {yname}'
                highlighted = highlight_yaml(ycontent)
                yaml_parts.append(
                    f'<div class="yaml-section">'
                    f'<button class="yaml-toggle" style="border-color:{color};color:{color}" '
                    f'onclick="document.getElementById(\'{uid}\').classList.toggle(\'open\')">{label}</button>'
                    f'<div class="yaml-block" id="{uid}"><pre>{highlighted}</pre></div>'
                    f'</div>'
                )
        yaml_parts.append('</div>')
    yaml_sections_html = '\n'.join(yaml_parts)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>GLM-5.2-FP8 — Interactivity vs Throughput — vLLM {version_str}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
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
  .panel {{ background: #181b1f; border: 1px solid #2a2a2e; border-radius: 4px; padding: 0;
             overflow: hidden; resize: both; min-width: 400px; min-height: 300px; }}
  .panel::-webkit-resizable {{ background: transparent; }}
  .panel-title {{ font-size: 13px; font-weight: 500; padding: 8px 12px; color: #d8d9da; }}
  .panel .plot {{ width: 100%; height: calc(100% - 36px); min-height: 250px; }}
  .panel .plot .nsewdrag {{ cursor: pointer !important; }}
  .summary {{ background: #181b1f; border: 1px solid #2a2a2e; border-radius: 4px; padding: 16px; margin-bottom: 16px; overflow-x: auto; }}
  .summary table {{ width: 100%; border-collapse: collapse; font-size: 12px; min-width: 900px; }}
  .summary th {{ text-align: left; padding: 6px 8px; color: #8e8e8e; border-bottom: 1px solid #2a2a2e; font-weight: 500;
                 cursor: pointer; user-select: none; white-space: nowrap; }}
  .summary th:hover {{ color: #d8d9da; }}
  .chart-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 8px; }}
  @media (max-width: 1200px) {{ .chart-row {{ grid-template-columns: 1fr; }} }}
  .chart-col {{ display: flex; flex-direction: column; gap: 4px; }}
  .chart-col .panel {{ min-width: 0; }}
  .axis-controls {{ display: flex; gap: 10px; align-items: center; padding: 6px 4px; flex-wrap: wrap; }}
  .axis-controls label {{ color: #8e8e8e; font-size: 12px; display: flex; align-items: center; gap: 4px; }}
  .axis-controls select {{ background: #181b1f; color: #d8d9da; border: 1px solid #2a2a2e; border-radius: 4px;
                           padding: 4px 6px; font-size: 11px; font-family: inherit; max-width: 300px; }}
  .summary td {{ padding: 6px 8px; border-bottom: 1px solid #1e1e22; }}
  .summary tr:hover {{ background: #1e2127; }}
  .highlight {{ color: #58a6ff; font-weight: 500; }}
  .hidden {{ display: none; }}
  .yaml-section {{ margin-bottom: 8px; }}
  .yaml-toggle {{ background: none; border: 1px solid #2a2a2e; color: #8e8e8e; border-radius: 4px;
                   padding: 6px 14px; cursor: pointer; font-size: 12px; font-family: inherit; }}
  .yaml-toggle:hover {{ color: #d8d9da; border-color: #3a3a3e; }}
  .yaml-block {{ background: #0d1117; border: 1px solid #2a2a2e; border-radius: 4px; padding: 16px;
                  margin-top: 8px; overflow-x: auto; display: none; }}
  .yaml-block.open {{ display: block; }}
  .yaml-block pre {{ font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace; font-size: 12px;
                      line-height: 1.5; color: #d8d9da; white-space: pre; margin: 0; }}
  .yaml-block .yk {{ color: #7ee787; }}
  .yaml-block .yv {{ color: #d2a8ff; }}
  .yaml-block .ys {{ color: #a5d6ff; }}
  .yaml-block .yc {{ color: #8b949e; font-style: italic; }}
  .yaml-block .yn {{ color: #79c0ff; }}
  .side-panel {{ position: fixed; top: 0; right: 0; width: 55vw; height: 100vh; background: #111217;
                 border-left: 2px solid #2a2a2e; z-index: 1000; transform: translateX(100%);
                 transition: transform .25s ease; display: flex; flex-direction: column; }}
  .side-panel.open {{ transform: translateX(0); }}
  .side-panel-header {{ display: flex; align-items: center; justify-content: space-between;
                        padding: 10px 16px; border-bottom: 1px solid #2a2a2e; flex-shrink: 0; }}
  .side-panel-header span {{ font-size: 14px; font-weight: 500; color: #d8d9da; }}
  .side-panel-close {{ background: none; border: 1px solid #3a3a3e; color: #d8d9da; border-radius: 4px;
                       padding: 4px 12px; cursor: pointer; font-size: 13px; }}
  .side-panel-close:hover {{ background: #2a2a2e; }}
  .side-panel iframe {{ flex: 1; border: none; width: 100%; }}
  .side-panel-resize {{ position: absolute; left: -4px; top: 0; width: 8px; height: 100%;
                        cursor: col-resize; z-index: 1001; }}
  .side-overlay {{ position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 999;
                   display: none; cursor: pointer; }}
  .side-overlay.open {{ display: block; }}
</style>
</head>
<body>
<h1>GLM-5.2-FP8 Disaggregated Serving — Interactivity vs Throughput</h1>
<div class="subtitle">vLLM {version_str} &middot; {len(configs)} deployment configs &middot; {len(sorted_conc)} concurrency levels</div>

<div id="root"></div>
<div class="side-overlay" id="overlay"></div>
<div class="side-panel" id="sidePanel">
  <div class="side-panel-resize" id="sidePanelResize"></div>
  <div class="side-panel-header">
    <span id="sidePanelTitle"></span>
    <button class="side-panel-close" id="sidePanelClose">Close</button>
  </div>
  <iframe id="sidePanelFrame"></iframe>
</div>

<script>
const LAYOUT_DEFAULTS = {{
  paper_bgcolor: '#181b1f',
  plot_bgcolor: '#181b1f',
  font: {{ family: 'Inter, -apple-system, sans-serif', size: 12, color: '#d8d9da' }},
  margin: {{ t: 40, r: 30, b: 60, l: 70 }},
  xaxis: {{ gridcolor: '#2a2a2e', zerolinecolor: '#2a2a2e', linecolor: '#2a2a2e' }},
  yaxis: {{ gridcolor: '#2a2a2e', zerolinecolor: '#2a2a2e', linecolor: '#2a2a2e' }},
  legend: {{ bgcolor: 'rgba(0,0,0,0)', font: {{ size: 11 }} }},
  hoverlabel: {{ bgcolor: '#23262b', bordercolor: '#3a3a3e', font: {{ size: 12, color: '#ffffff' }} }},
  hovermode: 'closest',
}};

const COLORS = {json.dumps(color_map)};
const CONFIGS = {json.dumps(configs_js)};
const CONCURRENCIES = {json.dumps(conc_list_js)};
const C_LABELS = {json.dumps(c_labels_js)};
const DATA = {json.dumps(data_js)};
const DASHBOARDS = {json.dumps(embedded_dashboards)};
const METRICS = {json.dumps(metrics_js)};
const STAT_KEYS = {json.dumps(STAT_KEYS)};
const CONFIG_KEYS = Object.keys(CONFIGS);

const root = document.getElementById('root');
const sidePanel = document.getElementById('sidePanel');
const sidePanelFrame = document.getElementById('sidePanelFrame');
const sidePanelTitle = document.getElementById('sidePanelTitle');
const overlay = document.getElementById('overlay');

const blobCache = {{}};

// Pre-decode all dashboards in the background so clicks are instant
setTimeout(() => {{
  for (const [key, b64] of Object.entries(DASHBOARDS)) {{
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    blobCache[key] = URL.createObjectURL(new Blob([bytes], {{ type: 'text/html;charset=utf-8' }}));
  }}
}}, 100);

function openDashboard(cfg, conc) {{
  const key = cfg + '_' + conc;
  if (!DASHBOARDS[key]) return;
  if (!blobCache[key]) {{
    const bin = atob(DASHBOARDS[key]);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    blobCache[key] = URL.createObjectURL(new Blob([bytes], {{ type: 'text/html;charset=utf-8' }}));
  }}
  sidePanelTitle.textContent = `${{CONFIGS[cfg].label}} @ c${{C_LABELS[conc]}} — Dashboard`;
  sidePanelFrame.src = blobCache[key];
  sidePanel.classList.add('open');
  overlay.classList.add('open');
}}

function closeDashboard() {{
  sidePanel.classList.remove('open');
  overlay.classList.remove('open');
  sidePanelFrame.src = 'about:blank';
}}

document.getElementById('sidePanelClose').addEventListener('click', closeDashboard);
overlay.addEventListener('click', closeDashboard);
document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeDashboard(); }});

(function() {{
  const resizer = document.getElementById('sidePanelResize');
  const dragOverlay = document.createElement('div');
  dragOverlay.style.cssText = 'position:fixed;inset:0;z-index:10000;cursor:col-resize;display:none;';
  document.body.appendChild(dragOverlay);

  function stopDrag() {{
    dragOverlay.style.display = 'none';
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', stopDrag);
    dragOverlay.removeEventListener('mousemove', onMove);
    dragOverlay.removeEventListener('mouseup', stopDrag);
  }}
  function onMove(e) {{
    const w = window.innerWidth - e.clientX;
    sidePanel.style.width = Math.max(300, Math.min(w, window.innerWidth - 100)) + 'px';
  }}
  resizer.addEventListener('mousedown', e => {{
    e.preventDefault();
    dragOverlay.style.display = 'block';
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', stopDrag);
    dragOverlay.addEventListener('mousemove', onMove);
    dragOverlay.addEventListener('mouseup', stopDrag);
  }});
}})();

function attachClickHandler(plotEl) {{
  plotEl.on('plotly_click', function(eventData) {{
    const pt = eventData.points[0];
    const cfg = CONFIG_KEYS[pt.curveNumber];
    const validConcs = CONCURRENCIES.filter(c => DATA[cfg] && DATA[cfg][c]);
    const conc = validConcs[pt.pointIndex];
    if (cfg && conc) openDashboard(cfg, conc);
  }});
}}

function makeSection(title, id) {{
  const hdr = document.createElement('div');
  hdr.className = 'row-header';
  hdr.innerHTML = `<span class="arrow">&#9660;</span> ${{title}}`;
  const wrap = document.createElement('div');
  wrap.className = 'grid';
  wrap.id = id;
  hdr.addEventListener('click', () => {{
    hdr.classList.toggle('collapsed');
    wrap.classList.toggle('hidden');
  }});
  root.appendChild(hdr);
  root.appendChild(wrap);
  return wrap;
}}

function makePanel(parent, title, cls) {{
  const panel = document.createElement('div');
  panel.className = 'panel' + (cls ? ' ' + cls : '');
  panel.style.height = '500px';
  const t = document.createElement('div');
  t.className = 'panel-title';
  t.textContent = title;
  const plot = document.createElement('div');
  plot.className = 'plot';
  panel.appendChild(t);
  panel.appendChild(plot);
  parent.appendChild(panel);
  new ResizeObserver(() => Plotly.Plots.resize(plot)).observe(panel);
  return plot;
}}

// ── Chart factory ──
function metricLabel(key) {{
  return key.replace(/_/g, ' ');
}}

const NORM_MODES = [
  {{ value: 'none', text: 'none' }},
  {{ value: 'decode', text: '/ decode GPUs' }},
  {{ value: 'total', text: '/ total GPUs' }},
  {{ value: 'cost_decode', text: '$/M tok (decode GPUs)' }},
  {{ value: 'cost_total', text: '$/M tok (total GPUs)' }},
];

let gpuCostPerHour = 0;

function applyNorm(val, norm, meta) {{
  if (val == null || val === 0) return null;
  if (norm === 'decode') return val / meta.decodeGPUs;
  if (norm === 'total') return val / (meta.decodeGPUs + meta.prefillGPUs);
  if (norm === 'cost_decode') return (meta.decodeGPUs * gpuCostPerHour * 1e6) / (val * 3600);
  if (norm === 'cost_total') return ((meta.decodeGPUs + meta.prefillGPUs) * gpuCostPerHour * 1e6) / (val * 3600);
  return val;
}}

function normSuffix(norm) {{
  if (norm === 'decode') return ' / decode GPU';
  if (norm === 'total') return ' / total GPU';
  if (norm === 'cost_decode') return ' ($/M tokens, decode)';
  if (norm === 'cost_total') return ' ($/M tokens, total)';
  return '';
}}

function hoverText(cfg, c, d) {{
  const meta = CONFIGS[cfg];
  const out = d.output_token_throughput;
  const itl = d.inter_token_latency;
  const otpu = d.output_token_throughput_per_user;
  const ttft = d.time_to_first_token;
  const norm = out ? (out.avg / meta.decodeGPUs).toFixed(1) : '?';
  return `<b>${{meta.label}} @ c${{C_LABELS[c]}}</b><br>` +
    `Output: ${{out?.avg?.toFixed(1) ?? '?'}} tok/s (${{norm}} tok/s/GPU)<br>` +
    `ITL p50: ${{itl?.p50?.toFixed(1) ?? '?'}} ms · p99: ${{itl?.p99?.toFixed(1) ?? '?'}} ms<br>` +
    `Per-user: ${{otpu?.avg?.toFixed(1) ?? '?'}} tok/s/user<br>` +
    `TTFT avg: ${{ttft ? (ttft.avg/1000).toFixed(1) : '?'}}s · p50: ${{ttft ? (ttft.p50/1000).toFixed(1) : '?'}}s · p99: ${{ttft ? (ttft.p99/1000).toFixed(1) : '?'}}s`;
}}

const allCharts = [];

function createChart(container, defaults) {{
  const state = {{
    xMetric: defaults.xMetric || 'output_token_throughput_per_user',
    xStat: defaults.xStat || 'avg',
    xNorm: defaults.xNorm || 'none',
    yMetric: defaults.yMetric || 'output_token_throughput',
    yStat: defaults.yStat || 'avg',
    yNorm: defaults.yNorm || 'decode',
    el: null,
  }};

  // Controls
  const ctrl = document.createElement('div');
  ctrl.className = 'axis-controls';

  function mkSelect(label, options, value, onChange) {{
    const lbl = document.createElement('label');
    lbl.textContent = label;
    const sel = document.createElement('select');
    options.forEach(o => {{
      const opt = document.createElement('option');
      opt.value = o.value;
      opt.textContent = o.text;
      sel.appendChild(opt);
    }});
    sel.value = value;
    sel.addEventListener('change', () => onChange(sel.value));
    lbl.appendChild(sel);
    ctrl.appendChild(lbl);
    return sel;
  }}

  const metricOpts = Object.keys(METRICS).sort().map(k => ({{
    value: k, text: metricLabel(k) + (METRICS[k].unit ? ` (${{METRICS[k].unit}})` : '')
  }}));
  const statOpts = STAT_KEYS.map(s => ({{ value: s, text: s }}));

  // X row
  const xDiv = document.createElement('div');
  xDiv.className = 'axis-controls';
  xDiv.style.borderBottom = '1px solid #2a2a2e';
  function mkSelIn(parent, label, options, value, onChange) {{
    const lbl = document.createElement('label');
    lbl.textContent = label;
    const sel = document.createElement('select');
    options.forEach(o => {{
      const opt = document.createElement('option');
      opt.value = o.value;
      opt.textContent = o.text;
      sel.appendChild(opt);
    }});
    sel.value = value;
    sel.addEventListener('change', () => onChange(sel.value));
    lbl.appendChild(sel);
    parent.appendChild(lbl);
    return sel;
  }}

  mkSelIn(xDiv, 'X:', metricOpts, state.xMetric, v => {{ state.xMetric = v; update(); }});
  mkSelIn(xDiv, 'stat:', statOpts, state.xStat, v => {{ state.xStat = v; update(); }});
  mkSelIn(xDiv, 'norm:', NORM_MODES, state.xNorm, v => {{ state.xNorm = v; update(); }});
  container.appendChild(xDiv);

  // Y row
  const yDiv = document.createElement('div');
  yDiv.className = 'axis-controls';
  mkSelIn(yDiv, 'Y:', metricOpts, state.yMetric, v => {{ state.yMetric = v; update(); }});
  mkSelIn(yDiv, 'stat:', statOpts, state.yStat, v => {{ state.yStat = v; update(); }});
  mkSelIn(yDiv, 'norm:', NORM_MODES, state.yNorm, v => {{ state.yNorm = v; update(); }});
  container.appendChild(yDiv);

  // Panel
  const panel = document.createElement('div');
  panel.className = 'panel';
  panel.style.height = '500px';
  const plot = document.createElement('div');
  plot.className = 'plot';
  panel.appendChild(plot);
  container.appendChild(panel);
  new ResizeObserver(() => Plotly.Plots.resize(plot)).observe(panel);
  state.el = plot;

  function axisTitle(metric, stat, norm) {{
    const name = metricLabel(metric);
    const unit = METRICS[metric]?.unit || '';
    const ss = stat === 'avg' ? '' : ` (${{stat}})`;
    const ns = normSuffix(norm);
    return `${{name}}${{ss}}${{ns}} (${{unit}})`;
  }}

  function buildTraces() {{
    return CONFIG_KEYS.map(cfg => {{
      const meta = CONFIGS[cfg];
      const validConcs = CONCURRENCIES.filter(c => DATA[cfg] && DATA[cfg][c]);
      return {{
        x: validConcs.map(c => {{
          const m = DATA[cfg][c][state.xMetric];
          return m ? applyNorm(m[state.xStat], state.xNorm, meta) : null;
        }}),
        y: validConcs.map(c => {{
          const m = DATA[cfg][c][state.yMetric];
          return m ? applyNorm(m[state.yStat], state.yNorm, meta) : null;
        }}),
        text: validConcs.map(c => hoverText(cfg, c, DATA[cfg][c])),
        hoverinfo: 'text',
        mode: 'lines+markers+text',
        textposition: 'top center',
        textfont: {{ size: 10, color: COLORS[cfg] }},
        texttemplate: validConcs.map(c => `c${{C_LABELS[c]}}`),
        name: `${{meta.label}} (${{meta.decodeGPUs}} decode GPUs)`,
        line: {{ color: COLORS[cfg], width: 2.5 }},
        marker: {{ size: 10, color: COLORS[cfg] }},
      }};
    }});
  }}

  function getLayout() {{
    return {{
      ...LAYOUT_DEFAULTS,
      title: {{ text: `${{metricLabel(state.yMetric)}} vs ${{metricLabel(state.xMetric)}}`, font: {{ size: 13, color: '#d8d9da' }} }},
      xaxis: {{ ...LAYOUT_DEFAULTS.xaxis, title: {{ text: axisTitle(state.xMetric, state.xStat, state.xNorm), font: {{ size: 11 }} }} }},
      yaxis: {{ ...LAYOUT_DEFAULTS.yaxis, title: {{ text: axisTitle(state.yMetric, state.yStat, state.yNorm), font: {{ size: 11 }} }}, rangemode: 'tozero' }},
      legend: {{ ...LAYOUT_DEFAULTS.legend, x: 0.99, y: 0.99, xanchor: 'right', yanchor: 'top' }},
    }};
  }}

  function update() {{
    const traces = buildTraces();
    if (state.el.data) {{
      state.el.data.forEach((old, i) => {{
        if (traces[i] && old.visible === 'legendonly') traces[i].visible = 'legendonly';
      }});
    }}
    Plotly.react(state.el, traces, getLayout(), {{ responsive: true, edits: {{ legendPosition: true }} }});
  }}

  Plotly.newPlot(state.el, buildTraces(), getLayout(), {{ responsive: true, edits: {{ legendPosition: true }} }});
  attachClickHandler(state.el);

  // Legend sync → table
  state.el.on('plotly_restyle', function() {{
    CONFIG_KEYS.forEach((cfg, i) => {{
      if (!state.el.data[i]) return;
      const vis = state.el.data[i].visible;
      const show = vis !== 'legendonly' && vis !== false;
      document.querySelectorAll(`tr[data-cfg="${{cfg}}"]`).forEach(r => {{
        r.style.display = show ? '' : 'none';
      }});
    }});
  }});

  const chart = {{ state, update }};
  allCharts.push(chart);
  return chart;
}}

// ── Top bar: hint + cost input ──
const topBar = document.createElement('div');
topBar.style.cssText = 'display:flex; justify-content:space-between; align-items:center; padding:12px 4px 8px; flex-wrap:wrap; gap:8px;';
const hint = document.createElement('span');
hint.style.cssText = 'color:#ffffff; font-size:15px;';
hint.textContent = 'Click any data point to open its Prometheus dashboard.';
topBar.appendChild(hint);

root.appendChild(topBar);

// ── Charts: two side by side ──
const chartRow = document.createElement('div');
chartRow.className = 'chart-row';
root.appendChild(chartRow);

const chartCol1 = document.createElement('div');
chartCol1.className = 'chart-col';
const chartCol2 = document.createElement('div');
chartCol2.className = 'chart-col';
chartRow.appendChild(chartCol1);
chartRow.appendChild(chartCol2);

createChart(chartCol1, {{ xMetric: 'output_token_throughput_per_user', yMetric: 'output_token_throughput', yNorm: 'decode' }});
createChart(chartCol2, {{ xMetric: 'inter_token_latency', xStat: 'p99', yMetric: 'output_token_throughput', yNorm: 'decode' }});

// ── Cost input ──
const costWrap = document.createElement('label');
costWrap.style.cssText = 'color:#8e8e8e; font-size:13px; display:flex; align-items:center; gap:6px; padding:12px 4px 4px;';
costWrap.textContent = '$/node/hr:';
const costInput = document.createElement('input');
costInput.type = 'number';
costInput.step = '0.01';
costInput.min = '0';
costInput.value = '0';
costInput.placeholder = '0.00';
costInput.style.cssText = 'background:#181b1f; color:#d8d9da; border:1px solid #2a2a2e; border-radius:4px; padding:4px 8px; font-size:12px; width:80px; font-family:inherit;';
costInput.addEventListener('input', () => {{
  gpuCostPerHour = (parseFloat(costInput.value) || 0) / {GPUS_PER_NODE};
  allCharts.forEach(ch => {{
    if (ch.state.xNorm.startsWith('cost') || ch.state.yNorm.startsWith('cost')) ch.update();
  }});
}});
gpuCostPerHour = 0;
costWrap.appendChild(costInput);
root.appendChild(costWrap);

// ── Section 2: Sortable summary table ──
const sec2Hdr = document.createElement('div');
sec2Hdr.className = 'row-header';
sec2Hdr.innerHTML = '<span class="arrow">&#9660;</span> Data Summary';
const sec2Wrap = document.createElement('div');
sec2Wrap.id = 'sec-table';
sec2Hdr.addEventListener('click', () => {{ sec2Hdr.classList.toggle('collapsed'); sec2Wrap.classList.toggle('hidden'); }});
root.appendChild(sec2Hdr);
root.appendChild(sec2Wrap);

(function() {{
  const div = document.createElement('div');
  div.className = 'summary';
  const table = document.createElement('table');
  const thead = document.createElement('thead');
  const tbody = document.createElement('tbody');
  table.appendChild(thead);
  table.appendChild(tbody);

  const colDefs = [
    'Config', 'Concurrency', 'Decode GPUs', 'KV Cache Prefill', 'KV Cache Decode', 'Output tok/s', 'tok/s/GPU',
    'ITL p50 (ms)', 'ITL p99 (ms)', 'Per-user tok/s',
    'TTFT avg (s)', 'TTFT p50 (s)', 'TTFT p99 (s)', 'TTFT min (s)', 'TTFT max (s)',
    '$/M input', '$/M output', '$/M total',
  ];
  const headerRow = document.createElement('tr');
  colDefs.forEach((label, i) => {{
    const th = document.createElement('th');
    th.textContent = label;
    th.dataset.col = i;
    th.dataset.label = label;
    th.addEventListener('click', () => sortTableBy(i, th));
    headerRow.appendChild(th);
  }});
  thead.appendChild(headerRow);

  const costCells = [];

  for (const cfg of CONFIG_KEYS) {{
    const meta = CONFIGS[cfg];
    const totalGPUs = meta.decodeGPUs + meta.prefillGPUs;
    for (const c of CONCURRENCIES) {{
      if (!DATA[cfg] || !DATA[cfg][c]) continue;
      const d = DATA[cfg][c];
      const out = d.output_token_throughput;
      const inp = d.input_token_throughput;
      const itl = d.inter_token_latency;
      const otpu = d.output_token_throughput_per_user;
      const ttft = d.time_to_first_token;
      const norm = out ? (out.avg / meta.decodeGPUs).toFixed(1) : '-';

      const tr = document.createElement('tr');
      tr.dataset.cfg = cfg;
      tr.dataset.conc = c;

      const kvPrefill = d._kv_cache_prefill;
      const kvDecode = d._kv_cache_decode;

      const vals = [
        meta.label, C_LABELS[c], meta.decodeGPUs,
        kvPrefill != null ? kvPrefill.toLocaleString() : '-',
        kvDecode != null ? kvDecode.toLocaleString() : '-',
        out?.avg?.toFixed(1) ?? '-', norm,
        itl?.p50?.toFixed(1) ?? '-', itl?.p99?.toFixed(1) ?? '-',
        otpu?.avg?.toFixed(1) ?? '-',
        ttft ? (ttft.avg/1000).toFixed(1) : '-',
        ttft ? (ttft.p50/1000).toFixed(1) : '-',
        ttft ? (ttft.p99/1000).toFixed(1) : '-',
        ttft ? (ttft.min/1000).toFixed(1) : '-',
        ttft ? (ttft.max/1000).toFixed(1) : '-',
        '-', '-', '-',
      ];
      vals.forEach((v, i) => {{
        const td = document.createElement('td');
        td.textContent = v;
        if (i === 0) {{ td.style.color = COLORS[cfg]; td.style.fontWeight = '500'; }}
        if (i === 6) td.className = 'highlight';
        tr.appendChild(td);
      }});

      const ci = colDefs.length;
      costCells.push({{
        inpTd: tr.cells[ci-3],
        outTd: tr.cells[ci-2],
        totTd: tr.cells[ci-1],
        inpTps: inp?.avg ?? 0,
        outTps: out?.avg ?? 0,
        prefillGPUs: meta.prefillGPUs,
        decodeGPUs: meta.decodeGPUs,
        totalGPUs,
      }});

      tbody.appendChild(tr);
    }}
  }}

  function costPerMTok(gpus, tps) {{
    return (gpuCostPerHour > 0 && tps > 0) ? ((gpus * gpuCostPerHour * 1e6) / (tps * 3600)).toFixed(2) : '-';
  }}
  function updateCostCols() {{
    costCells.forEach(cc => {{
      cc.inpTd.textContent = costPerMTok(cc.prefillGPUs, cc.inpTps);
      cc.outTd.textContent = costPerMTok(cc.decodeGPUs, cc.outTps);
      cc.totTd.textContent = costPerMTok(cc.totalGPUs, cc.inpTps + cc.outTps);
    }});
  }}
  updateCostCols();
  costInput.addEventListener('input', updateCostCols);

  let sortCol = -1, sortAsc = true;
  function sortTableBy(col, th) {{
    if (sortCol === col) sortAsc = !sortAsc;
    else {{ sortCol = col; sortAsc = true; }}

    thead.querySelectorAll('th').forEach(h => h.textContent = h.dataset.label);
    th.textContent = th.dataset.label + (sortAsc ? ' \\u25B2' : ' \\u25BC');

    const cfgOrder = {{}};
    CONFIG_KEYS.forEach((k, i) => cfgOrder[k] = i);
    const rows = Array.from(tbody.querySelectorAll('tr'));
    rows.sort((a, b) => {{
      const va = parseFloat(a.cells[col]?.textContent) || 0;
      const vb = parseFloat(b.cells[col]?.textContent) || 0;
      const diff = sortAsc ? va - vb : vb - va;
      if (diff !== 0) return diff;
      return cfgOrder[a.dataset.cfg] - cfgOrder[b.dataset.cfg];
    }});
    rows.forEach(r => tbody.appendChild(r));
  }}

  div.appendChild(table);
  sec2Wrap.appendChild(div);
}})();
</script>

{yaml_sections_html}

</body>
</html>"""

    with open(output_path, 'w') as f:
        f.write(html)
    return output_path


def main():
    if len(sys.argv) > 1:
        results_dir = sys.argv[1]
    else:
        results_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results')

    if not os.path.isdir(results_dir):
        print(f"Error: {results_dir} not found", file=sys.stderr)
        sys.exit(1)

    configs, metric_units = discover_configs(results_dir)
    if not configs:
        print(f"Error: no results_p<R>w<W>_d<R>w<W>/ directories found in {results_dir}", file=sys.stderr)
        sys.exit(1)

    output_dir = os.path.dirname(os.path.abspath(results_dir))
    output_path = os.path.join(output_dir, 'interactivity_vs_throughput.html')

    generate_html(configs, output_path, results_dir, metric_units)

    print(f"Discovered {len(configs)} configs:")
    for cfg, meta in sorted(configs.items()):
        concs = sorted(meta['runs'].keys())
        print(f"  {cfg}: {meta['label']} ({meta['decode_gpus']} decode GPUs) — c{',c'.join(str(c) for c in concs)}")
    print(f"\nGenerated: {output_path}")


if __name__ == '__main__':
    main()

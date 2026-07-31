#!/usr/bin/env python3
"""
Scan results/ for deployment configs, extract aiperf metrics,
and generate an interactive interactivity-vs-throughput HTML chart.

Usage:
    python3 gen_interactivity_chart.py [results_dir ...]

Defaults to ./results if no argument given. Multiple directories can be
specified to consolidate all configs into a single report, with the
folder name prefixed to each config label for differentiation.
"""

import base64
import csv
import json
import os
import re
import sys
from pathlib import Path

import numpy as np

GPUS_PER_NODE = 8
STAT_KEYS = ['avg', 'min', 'p50', 'p90', 'p95', 'p99', 'max']

COLORS_DARK = [
    '#ff4444', '#22ccff', '#ffcc00', '#bb66ff', '#00ee77',
    '#ff8822', '#4499ff', '#ff55aa', '#00ddbb', '#aadd00',
    '#dd44ff', '#44ff66', '#ff5588', '#33aaff', '#ffaa33',
    '#66ccee', '#ff3377', '#55ffbb', '#cc99ff', '#99ee33',
]
COLORS_LIGHT = [
    '#cc0000', '#0077aa', '#aa8800', '#7722cc', '#008844',
    '#cc5500', '#1155cc', '#cc2277', '#007766', '#668800',
    '#9900cc', '#119933', '#cc2244', '#0066cc', '#cc7700',
    '#226699', '#bb0044', '#228866', '#7744bb', '#558800',
]
COLORS = COLORS_DARK


def aggregate_jsonl(path):
    """Compute per-metric stats from profile_export.jsonl (fallback when aiperf JSON is missing)."""
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get('metadata', {}).get('benchmark_phase') != 'profiling':
                continue
            records.append(r)
    if not records:
        return {}
    metric_values = {}
    metric_units_local = {}
    for r in records:
        for name, entry in r.get('metrics', {}).items():
            v = entry['value']
            if not isinstance(v, (int, float)):
                continue
            metric_values.setdefault(name, []).append(v)
            if name not in metric_units_local:
                metric_units_local[name] = entry.get('unit', '')
    result = {}
    for name, vals in metric_values.items():
        a = np.array(vals)
        result[name] = {
            'avg': float(np.mean(a)), 'min': float(np.min(a)),
            'p50': float(np.percentile(a, 50)), 'p90': float(np.percentile(a, 90)),
            'p95': float(np.percentile(a, 95)), 'p99': float(np.percentile(a, 99)),
            'max': float(np.max(a)), 'unit': metric_units_local.get(name, ''),
        }
    ts_start = [r['metadata']['request_start_ns'] for r in records if r['metadata'].get('request_start_ns')]
    ts_end = [r['metadata']['request_end_ns'] for r in records if r['metadata'].get('request_end_ns')]
    if ts_start and ts_end:
        duration_s = (max(ts_end) - min(ts_start)) / 1e9
        total_out = sum(r['metrics'].get('output_sequence_length', {}).get('value', 0) for r in records)
        total_in = sum(r['metrics'].get('input_sequence_length', {}).get('value', 0) for r in records)
        num_requests = len(records)
        if duration_s > 0:
            out_tps = total_out / duration_s
            in_tps = total_in / duration_s
            rps = num_requests / duration_s
            for name, val, unit in [
                ('output_token_throughput', out_tps, 'tokens/sec'),
                ('input_token_throughput', in_tps, 'tokens/sec'),
                ('total_token_throughput', out_tps + in_tps, 'tokens/sec'),
                ('request_throughput', rps, 'req/sec'),
            ]:
                result[name] = {
                    'avg': val, 'min': val, 'p50': val,
                    'p90': val, 'p95': val, 'p99': val,
                    'max': val, 'unit': unit,
                }
            result['request_count'] = {
                'avg': num_requests, 'min': num_requests, 'p50': num_requests,
                'p90': num_requests, 'p95': num_requests, 'p99': num_requests,
                'max': num_requests, 'unit': 'count',
            }
    return result


def read_model_label(results_dir):
    candidates = [Path(results_dir) / 'model_label.txt']
    candidates.extend(sorted(Path(results_dir).glob('results_*/model_label.txt')))
    for path in candidates:
        if path.is_file():
            label = path.read_text().strip()
            if label:
                return label
    return os.environ.get('MODEL_LABEL', 'DeepSeek-V4-Pro')


def read_text_file(path, default=''):
    try:
        text = Path(path).read_text().strip()
    except OSError:
        return default
    return text or default


def read_int_file(path, default):
    try:
        return int(read_text_file(path, str(default)))
    except ValueError:
        return default


def parse_int(value):
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        value = value.strip().replace(',', '')
        if value.isdigit():
            return int(value)
    return None


def tokens_from_cache_config_labels(labels):
    if not isinstance(labels, dict):
        return None
    kv_cache_size_tokens = parse_int(labels.get('kv_cache_size_tokens'))
    if kv_cache_size_tokens is not None:
        return kv_cache_size_tokens
    num_gpu_blocks = parse_int(labels.get('num_gpu_blocks'))
    block_size = parse_int(labels.get('block_size'))
    if num_gpu_blocks is None or block_size is None:
        return None
    return num_gpu_blocks * block_size


def classify_kv_role(labels, token_value=None):
    label_text = ' '.join(str(v).lower() for v in labels.values())
    if 'prefill' in label_text:
        return 'prefill'
    if 'decode' in label_text:
        return 'decode'
    if token_value is not None:
        return '_by_size'
    return None


def kv_cache_tokens_from_series(series):
    by_role = {'prefill': [], 'decode': []}
    by_size = []
    for item in series:
        labels = item.get('labels', {}) if isinstance(item, dict) else {}
        token_value = tokens_from_cache_config_labels(labels)
        if token_value is None:
            continue
        role = classify_kv_role(labels, token_value)
        if role in by_role:
            by_role[role].append(token_value)
        elif role == '_by_size':
            by_size.append(token_value)

    if not by_role['prefill'] and not by_role['decode'] and by_size:
        sizes = sorted(set(by_size))
        if len(sizes) >= 2:
            by_role['prefill'] = [v for v in by_size if v == sizes[0]]
            by_role['decode'] = [v for v in by_size if v == sizes[-1]]
        else:
            by_role['decode'] = by_size

    return {
        'prefill': by_role['prefill'][0] if by_role['prefill'] else None,
        'decode': by_role['decode'][0] if by_role['decode'] else None,
    }


def find_kv_cache_tokens_in_json(value):
    if isinstance(value, dict):
        metric = value.get('vllm:cache_config_info')
        if isinstance(metric, dict) and isinstance(metric.get('series'), list):
            tokens = kv_cache_tokens_from_series(metric['series'])
            if tokens['prefill'] is not None or tokens['decode'] is not None:
                return tokens

        if isinstance(value.get('series'), list):
            tokens = kv_cache_tokens_from_series(value['series'])
            if tokens['prefill'] is not None or tokens['decode'] is not None:
                return tokens

        result = {'prefill': None, 'decode': None}
        for child in value.values():
            child_value = find_kv_cache_tokens_in_json(child)
            if child_value is None:
                continue
            for role in result:
                if result[role] is None and child_value.get(role) is not None:
                    result[role] = child_value[role]
            if result['prefill'] is not None or result['decode'] is not None:
                return result
    elif isinstance(value, list):
        result = {'prefill': None, 'decode': None}
        for child in value:
            child_value = find_kv_cache_tokens_in_json(child)
            if child_value is None:
                continue
            for role in result:
                if result[role] is None and child_value.get(role) is not None:
                    result[role] = child_value[role]
            if result['prefill'] is not None or result['decode'] is not None:
                return result
    return None


def read_kv_cache_tokens_from_cache_config(run_dir):
    json_path = os.path.join(run_dir, 'server_metrics_export.json')
    if os.path.isfile(json_path):
        try:
            with open(json_path) as f:
                payload = json.load(f)
                token_values = find_kv_cache_tokens_in_json(payload.get('metrics', payload))
            if token_values is not None:
                return token_values
        except (OSError, json.JSONDecodeError):
            pass

    csv_path = os.path.join(run_dir, 'server_metrics_export.csv')
    if os.path.isfile(csv_path):
        by_role = {'prefill': [], 'decode': []}
        by_size = []
        try:
            with open(csv_path, newline='') as f:
                reader = csv.reader(row for row in f if not row.startswith('#'))
                current = {}
                for row in reader:
                    if len(row) < 4:
                        continue
                    if row[1] == 'vllm:cache_config_info':
                        label_name = row[2]
                        label_value = row[3]
                    elif len(row) >= 5 and row[2] == 'vllm:cache_config_info':
                        label_name = row[3]
                        label_value = row[4]
                    else:
                        continue
                    current[label_name] = label_value
                    token_value = tokens_from_cache_config_labels(current)
                    if token_value is not None:
                        role = classify_kv_role(current, token_value)
                        if role in by_role:
                            by_role[role].append(token_value)
                        else:
                            by_size.append(token_value)
                        current = {}
            if not by_role['prefill'] and not by_role['decode'] and by_size:
                sizes = sorted(set(by_size))
                if len(sizes) >= 2:
                    by_role['prefill'] = [v for v in by_size if v == sizes[0]]
                    by_role['decode'] = [v for v in by_size if v == sizes[-1]]
                else:
                    by_role['decode'] = by_size
            if by_role['prefill'] or by_role['decode']:
                return {
                    'prefill': by_role['prefill'][0] if by_role['prefill'] else None,
                    'decode': by_role['decode'][0] if by_role['decode'] else None,
                }
        except OSError:
            pass
    return None


def read_kv_cache_tokens(run_dir):
    result = read_kv_cache_tokens_from_cache_config(run_dir)
    if result is not None:
        return result
    json_path = os.path.join(run_dir, 'kv_cache_config.json')
    if not os.path.isfile(json_path):
        json_path = os.path.join(os.path.dirname(run_dir), 'kv_cache_config.json')
    if os.path.isfile(json_path):
        try:
            with open(json_path) as f:
                data = json.load(f)
            if isinstance(data, dict) and (data.get('prefill') or data.get('decode')):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return None


def read_prefix_cache_report(run_dir):
    """Load the Prometheus-derived prefix-cache report written by `just report`.

    Returns None if the run predates this feature or the report step failed
    (e.g. Grafana unreachable) — callers should render '-' in that case.
    """
    json_path = os.path.join(run_dir, 'prefix_cache_report.json')
    if not os.path.isfile(json_path):
        return None
    try:
        with open(json_path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _extract_waiting_requests(dash_html_path):
    """Extract max(sum(waiting)) for prefill and decode from dashboard HTML."""
    try:
        with open(dash_html_path) as f:
            text = f.read()
    except OSError:
        return {}
    m = re.search(r'const panels = ({.*?});\s*(?:const|var|let|function|//)', text, re.DOTALL)
    if not m:
        return {}
    try:
        panels = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    result = {}
    for panel in panels.values():
        title = panel.get('title', '').lower()
        if 'waiting' not in title or 'request' not in title:
            continue
        if 'prefill' in title:
            key = '_max_waiting_prefill'
        elif 'decode' in title:
            key = '_max_waiting_decode'
        else:
            continue
        ts_sums = {}
        for q in panel.get('queries', []):
            for s in q.get('series', []):
                for v in s.get('values', []):
                    ts = v[0]
                    val = float(v[1])
                    ts_sums[ts] = ts_sums.get(ts, 0) + val
        if ts_sums:
            result[key] = max(ts_sums.values())
    return result


def discover_configs(results_dir, only=None):
    """Auto-discover deployment configs and concurrency levels from folder structure.

    Expected layout:
        results_dir/
            results_<user>-wide-ep/
                results_<user>-wide-ep_c64/profile_export_aiperf.json
                results_<user>-wide-ep_c256/...

    If `only` is given (a set of config names), all other configs are skipped.
    """
    configs = {}
    metric_units = {}

    for entry in sorted(os.listdir(results_dir)):
        if not entry.startswith('results_'):
            continue
        config_dir = os.path.join(results_dir, entry)
        if os.path.islink(config_dir) and not os.path.exists(config_dir):
            target = os.readlink(config_dir)
            if not os.path.isabs(target) and os.path.isdir(target):
                config_dir = os.path.realpath(target)
            else:
                continue
        elif not os.path.isdir(config_dir):
            continue

        config_name = entry[len('results_'):]
        if only is not None and config_name not in only:
            continue
        decode_gpus = read_int_file(os.path.join(config_dir, 'decode_gpus.txt'), GPUS_PER_NODE)
        prefill_gpus = read_int_file(os.path.join(config_dir, 'prefill_gpus.txt'), GPUS_PER_NODE)
        default_label = config_name
        default_pods = read_text_file(os.path.join(config_dir, 'pods.txt'), config_name)

        conc_pattern = re.compile(rf'^results_{re.escape(config_name)}_c(\d+)$')
        runs = {}
        for sub in sorted(os.listdir(config_dir)):
            cm = conc_pattern.match(sub)
            if not cm:
                continue
            c_val = int(cm.group(1))
            json_path = os.path.join(config_dir, sub, 'profile_export_aiperf.json')
            jsonl_path = os.path.join(config_dir, sub, 'profile_export.jsonl')
            if os.path.isfile(json_path):
                with open(json_path) as f:
                    d = json.load(f)
            elif os.path.isfile(jsonl_path):
                d = aggregate_jsonl(jsonl_path)
            else:
                continue

            run_data = {}
            for key, val in d.items():
                if isinstance(val, dict) and 'avg' in val:
                    run_data[key] = {s: val[s] for s in STAT_KEYS if s in val}
                    if key not in metric_units:
                        metric_units[key] = val.get('unit', '')

            error_summary = d.get('error_summary', [])
            error_count = sum(e.get('count', 0) for e in error_summary) if error_summary else 0
            run_data['_error_count'] = error_count

            kv_cache_tokens = read_kv_cache_tokens(os.path.join(config_dir, sub))
            if kv_cache_tokens is not None:
                run_data['_kv_cache_tokens'] = kv_cache_tokens

            prefix_cache = read_prefix_cache_report(os.path.join(config_dir, sub))
            if prefix_cache is not None:
                run_data['_prefix_cache'] = prefix_cache

            dash_html = os.path.join(config_dir, sub, 'dashboard.html')
            if os.path.isfile(dash_html):
                waiting = _extract_waiting_requests(dash_html)
                if waiting:
                    run_data.update(waiting)

            itl = run_data.get('inter_token_latency')
            if itl:
                unit = metric_units.get('inter_token_latency', 'ms')
                scale = {'ms': 1000.0, 's': 1.0, 'us': 1_000_000.0}.get(unit, 1000.0)
                interactivity = {s: scale / v for s, v in itl.items() if v}
                if interactivity:
                    run_data['interactivity'] = interactivity
                    metric_units.setdefault('interactivity', 'tokens/sec/user')

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

        label = read_text_file(os.path.join(config_dir, 'config_label.txt'), default_label)
        pods = read_text_file(os.path.join(config_dir, 'pods.txt'), default_pods)
        configs[config_name] = {
            'label': label,
            'decode_gpus': decode_gpus,
            'prefill_gpus': prefill_gpus,
            'pods': pods,
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


DASHBOARD_AGG_INLINE = r"""
// ── Aggregation dropdown injection ──
(function() {
  function _isLight() { return document.documentElement.classList.contains('light'); }
  function _tFg() { return _isLight() ? '#333' : '#8e8e8e'; }
  function _tGrid() { return _isLight() ? '#ddd' : '#2a2a2e'; }
  function _selStyle() {
    return _isLight()
      ? 'float:right;background:#f5f5f5;color:#222;border:1px solid #ccc;border-radius:3px;padding:2px 6px;font-size:11px;cursor:pointer;'
      : 'float:right;background:#111;color:#ccc;border:1px solid #2a2a2e;border-radius:3px;padding:2px 6px;font-size:11px;cursor:pointer;';
  }
  const _origRenderPanel = renderPanel;

  function _aggSeries(allSeries, mode) {
    const tsMap = new Map();
    allSeries.forEach(function(x) {
      x.s.values.forEach(function(v) {
        const val = parseFloat(v[1]);
        if (isNaN(val)) return;
        if (!tsMap.has(v[0])) tsMap.set(v[0], []);
        tsMap.get(v[0]).push(val);
      });
    });
    const sorted = Array.from(tsMap.entries()).sort(function(a, b) { return a[0] - b[0]; });
    return {
      x: sorted.map(function(e) { return new Date(e[0] * 1000); }),
      y: sorted.map(function(e) {
        var vals = e[1].slice().sort(function(a, b) { return a - b; });
        var s = vals.reduce(function(a, b) { return a + b; }, 0);
        if (mode === 'sum') return s;
        if (mode === 'avg') return s / vals.length;
        if (mode === 'min') return vals[0];
        if (mode === 'max') return vals[vals.length - 1];
        var mid = Math.floor(vals.length / 2);
        return vals.length % 2 ? vals[mid] : (vals[mid - 1] + vals[mid]) / 2;
      }),
    };
  }

  function _replotPanel(div, mode, filter) {
    var p = div._panelData;
    var plotDiv = div.querySelector('.plot');
    if (!plotDiv || !p) return;
    var allSeries = p.queries.flatMap(function(q) { return q.series.map(function(s) { return {q:q, s:s}; }); });
    var hasSeries = allSeries.filter(function(x) { return x.s.values.length > 0; });
    if (filter && filter !== 'all') {
      hasSeries = hasSeries.filter(function(x) {
        var lbl = seriesLabel(x.q, x.s).toLowerCase();
        return lbl.indexOf(filter) !== -1;
      });
    }
    if (!hasSeries.length) return;

    var traces;
    if (mode === 'all') {
      traces = hasSeries.map(function(x, i) {
        return {
          x: x.s.values.map(function(v) { return new Date(v[0] * 1000); }),
          y: x.s.values.map(function(v) { return parseFloat(v[1]); }),
          name: seriesLabel(x.q, x.s),
          type: 'scatter', mode: 'lines',
          line: { width: 1.5, color: COLORS[i % COLORS.length] },
          hovertemplate: '%{y}<extra>%{fullData.name}</extra>',
        };
      });
    } else {
      var agg = _aggSeries(hasSeries, mode);
      traces = [{
        x: agg.x, y: agg.y,
        name: {sum:'Sum',avg:'Average',min:'Min',max:'Max',median:'Median'}[mode],
        type: 'scatter', mode: 'lines',
        line: { width: 2, color: '#58a6ff' },
        hovertemplate: '%{y}<extra>' + {sum:'Sum',avg:'Avg',min:'Min',max:'Max',median:'Median'}[mode] + '</extra>',
      }];
    }
    var fmt = (typeof UNIT_FMT !== 'undefined') ? UNIT_FMT[p.unit] : undefined;
    var yTitle = ((typeof UNIT_LABEL !== 'undefined') ? UNIT_LABEL[p.unit] : '') || p.unit || '';
    Plotly.react(plotDiv, traces, {
      margin: { l: 58, r: 16, t: 4, b: 30 },
      paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
      font: { color: _tFg(), size: 10 },
      xaxis: { gridcolor: _tGrid(), linecolor: _tGrid(), tickformat: '%H:%M' },
      yaxis: { gridcolor: _tGrid(), linecolor: _tGrid(),
               title: yTitle ? { text: yTitle, font: { size: 10 } } : undefined,
               tickformat: fmt ? undefined : '.3s', hoverformat: '.4g' },
      legend: { font: { size: 9 }, orientation: 'h', y: -0.3 },
      showlegend: traces.length > 1,
      hovermode: 'x unified',
    }, { responsive: true, displayModeBar: false });
  }

  renderPanel = function(container, p) {
    _origRenderPanel(container, p);
    var div = container.lastElementChild;
    if (!div || !div._panelData) return;
    var titleEl = div.querySelector('.panel-title');
    if (!titleEl) return;
    var sel = document.createElement('select');
    sel.style.cssText = _selStyle();
    [['all','All Series'],['sum','Sum'],['avg','Average'],['min','Min'],['max','Max'],['median','Median']].forEach(function(pair) {
      var o = document.createElement('option');
      o.value = pair[0];
      o.textContent = pair[1];
      sel.appendChild(o);
    });
    var filterSel = document.createElement('select');
    filterSel.style.cssText = _selStyle() + 'margin-right:4px;';
    [['all','All Engines'],['prefill','Prefill Only'],['decode','Decode Only']].forEach(function(pair) {
      var o = document.createElement('option');
      o.value = pair[0];
      o.textContent = pair[1];
      filterSel.appendChild(o);
    });
    sel.addEventListener('change', function() { _replotPanel(div, sel.value, filterSel.value); });
    filterSel.addEventListener('change', function() { _replotPanel(div, sel.value, filterSel.value); });
    titleEl.appendChild(filterSel);
    titleEl.appendChild(sel);
  };
})();
"""

DASHBOARD_AGG_INLINE_WRAPPED = '<script>' + DASHBOARD_AGG_INLINE + '</script>'


def inject_dashboard_aggregation(content):
    if isinstance(content, bytes):
        text = content.decode('utf-8', errors='replace')
    else:
        text = content
    inject_target = 'if (rows.length === 0) {'
    if inject_target in text:
        text = text.replace(inject_target, DASHBOARD_AGG_INLINE + '\n' + inject_target, 1)
    else:
        text = text.replace('</body>', DASHBOARD_AGG_INLINE_WRAPPED + '\n</body>', 1)
    return text.encode('utf-8') if isinstance(content, bytes) else text


def read_reference_csv(csv_path, framework_filter='vllm'):
    """Parse InferenceX CSV and return reference data grouped by config."""
    if not os.path.isfile(csv_path):
        return {}
    groups = {}
    try:
        with open(csv_path, newline='') as f:
            lines = [line for line in f if not line.startswith('#')]
        reader = csv.DictReader(lines)
        for row in reader:
            fw = row.get('Framework', '').strip().lower()
            if framework_filter and fw != framework_filter.lower():
                continue
            hardware = row.get('Hardware', '').strip()
            tp = int(row.get('TP', '0') or '0')
            ep = int(row.get('EP', '0') or '0')
            dp_attn = row.get('DP Attention', '').strip().lower() == 'true'
            concurrency = int(row.get('Concurrency', '0') or '0')
            num_prefill_gpus = int(row.get('Num Prefill GPUs', '0') or '0')
            num_decode_gpus = int(row.get('Num Decode GPUs', '0') or '0')
            is_disagg = row.get('Disaggregated', '').strip().lower() == 'true'
            if is_disagg and (num_prefill_gpus + num_decode_gpus) > 0:
                total_gpus = num_prefill_gpus + num_decode_gpus
            else:
                total_gpus = tp

            out_per_gpu = float(row.get('Output Throughput/GPU (tok/s)', '0') or '0')
            inp_per_gpu = float(row.get('Input Throughput/GPU (tok/s)', '0') or '0')
            interactivity_avg = float(row.get('Mean Interactivity (tok/s/user)', '0') or '0')
            interactivity_median = float(row.get('Median Interactivity (tok/s/user)', '0') or '0')
            itl_avg = float(row.get('Mean ITL (ms)', '0') or '0') * 1000
            itl_median = float(row.get('Median ITL (ms)', '0') or '0') * 1000
            itl_p99 = float(row.get('P99 ITL (ms)', '0') or '0') * 1000
            ttft_avg = float(row.get('Mean TTFT (ms)', '0') or '0') * 1000
            ttft_median = float(row.get('Median TTFT (ms)', '0') or '0') * 1000
            ttft_p99 = float(row.get('P99 TTFT (ms)', '0') or '0') * 1000
            e2e_latency_ms = float(row.get('Mean E2E Latency (ms)', '0') or '0') * 1000

            e2e_output_throughput = out_per_gpu * total_gpus
            # Per-user E2E output rate: accounts for TTFT dilution
            if e2e_latency_ms > 0 and interactivity_avg > 0:
                e2e_output_per_user = interactivity_avg * (e2e_latency_ms - ttft_avg) / e2e_latency_ms
            else:
                e2e_output_per_user = interactivity_avg

            dp_str = ' DP' if dp_attn else ''
            key = f'{fw} {hardware} EP{ep}{dp_str} ({total_gpus}GPU)'
            if key not in groups:
                groups[key] = {
                    'label': key,
                    'hardware': hardware,
                    'framework': fw,
                    'tp': tp,
                    'ep': ep,
                    'dp_attention': dp_attn,
                    'total_gpus': total_gpus,
                    'points': [],
                }
            groups[key]['points'].append({
                'concurrency': concurrency,
                'e2e_output_throughput': e2e_output_throughput,
                'e2e_output_per_user': e2e_output_per_user,
                'output_per_gpu': out_per_gpu,
                'input_per_gpu': inp_per_gpu,
                'interactivity_avg': interactivity_avg,
                'interactivity_median': interactivity_median,
                'itl_avg': itl_avg,
                'itl_median': itl_median,
                'itl_p99': itl_p99,
                'ttft_avg': ttft_avg,
                'ttft_median': ttft_median,
                'ttft_p99': ttft_p99,
            })
    except (OSError, ValueError, KeyError) as e:
        print(f"Warning: failed to parse reference CSV {csv_path}: {e}", file=sys.stderr)
        return {}

    # Sort points by concurrency within each group
    for g in groups.values():
        g['points'].sort(key=lambda p: p['concurrency'])
    return groups


def _filter_reference_by_gpus(reference_data, configs):
    """Keep only reference groups with total_gpus <= 2x the max GPU count in our configs."""
    if not reference_data:
        return {}
    max_our_gpus = max(
        (meta.get('decode_gpus', 0) + meta.get('prefill_gpus', 0))
        for meta in configs.values()
    ) if configs else 0
    if max_our_gpus == 0:
        return reference_data
    gpu_limit = 2 * max_our_gpus
    return {k: v for k, v in reference_data.items() if v.get('total_gpus', 0) <= gpu_limit}


def generate_html(configs, output_path, results_dir, metric_units, reference_data=None):
    model_label = read_model_label(results_dir)
    color_map_dark = {}
    color_map_light = {}
    for i, cfg in enumerate(sorted(configs.keys())):
        color_map_dark[cfg] = COLORS_DARK[i % len(COLORS_DARK)]
        color_map_light[cfg] = COLORS_LIGHT[i % len(COLORS_LIGHT)]
    color_map = color_map_dark

    MARKER_SHAPES = [
        'circle', 'square', 'diamond', 'triangle-up', 'cross',
        'star', 'hexagon', 'triangle-down', 'pentagon', 'hourglass',
    ]
    folder_set = []
    for cfg in sorted(configs.keys()):
        folder = cfg.rsplit('/', 1)[0] if '/' in cfg else ''
        if folder not in folder_set:
            folder_set.append(folder)
    folder_shape_map = {f: MARKER_SHAPES[i % len(MARKER_SHAPES)] for i, f in enumerate(folder_set)}
    shape_map = {}
    for cfg in sorted(configs.keys()):
        folder = cfg.rsplit('/', 1)[0] if '/' in cfg else ''
        shape_map[cfg] = folder_shape_map[folder]

    configs_js = {}
    data_js = {}
    concurrencies = set()
    for cfg, meta in configs.items():
        folder = cfg.rsplit('/', 1)[0] if '/' in cfg else ''
        configs_js[cfg] = {
            'label': meta['label'],
            'decodeGPUs': meta['decode_gpus'],
            'prefillGPUs': meta['prefill_gpus'],
            'pods': meta['pods'],
            'folder': folder,
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
    x_axis_metrics = [
        'output_token_throughput_per_user',
        'e2e_output_token_throughput',
        'interactivity',
        'inter_token_latency',
        'time_to_first_token',
        'time_to_second_token',
        'request_latency',
        'effective_latency',
        'credit_to_start_latency',
        'input_sequence_length',
        'output_sequence_length',
        'tokens_in_flight',
        'effective_concurrency',
        'effective_decode_concurrency',
        'effective_prefill_concurrency',
        'request_throughput',
        'theoretical_prefix_cache_hit',
    ]
    y_axis_metrics = [
        'output_token_throughput',
        'output_token_throughput_per_user',
        'e2e_output_token_throughput',
        'input_token_throughput',
        'total_token_throughput',
        'effective_decode_throughput',
        'effective_prefill_throughput',
        'effective_total_throughput',
        'active_decode_throughput',
        'active_prefill_throughput',
        'active_total_throughput',
        'request_throughput',
        'request_count',
        'total_output_tokens',
        'total_usage_prompt_tokens',
        'total_usage_completion_tokens',
        'total_usage_total_tokens',
    ]
    decode_normalized_metrics = [
        'output_token_throughput',
        'e2e_output_token_throughput',
        'effective_decode_throughput',
        'active_decode_throughput',
    ]
    prefill_normalized_metrics = [
        'input_token_throughput',
        'effective_prefill_throughput',
        'active_prefill_throughput',
    ]
    total_normalized_metrics = [
        'total_token_throughput',
        'effective_total_throughput',
        'active_total_throughput',
    ]
    metric_labels = {
        'active_decode_throughput': 'Active decode throughput',
        'active_decode_throughput_per_user': 'Active decode throughput/user',
        'active_prefill_throughput': 'Active prefill throughput',
        'active_prefill_throughput_per_user': 'Active prefill throughput/user',
        'active_total_throughput': 'Active total throughput',
        'credit_to_start_latency': 'Credit-to-start latency',
        'e2e_output_token_throughput': 'E2E output token throughput',
        'effective_concurrency': 'Effective concurrency',
        'effective_decode_concurrency': 'Effective decode concurrency',
        'effective_decode_throughput': 'Effective decode throughput',
        'effective_decode_throughput_per_user': 'Effective decode throughput/user',
        'effective_latency': 'Effective latency',
        'effective_prefill_concurrency': 'Effective prefill concurrency',
        'effective_prefill_throughput': 'Effective prefill throughput',
        'effective_prefill_throughput_per_user': 'Effective prefill throughput/user',
        'effective_total_throughput': 'Effective total throughput',
        'input_sequence_length': 'Input sequence length',
        'input_token_throughput': 'Input token throughput',
        'interactivity': 'Interactivity (1/TPOT)',
        'inter_token_latency': 'Inter-token latency',
        'output_sequence_length': 'Output sequence length',
        'output_token_throughput': 'Output token throughput',
        'output_token_throughput_per_user': 'Output token throughput/user',
        'request_count': 'Request count',
        'request_latency': 'Request latency',
        'request_throughput': 'Request throughput',
        'theoretical_prefix_cache_hit': 'Theoretical prefix cache hit',
        'time_to_first_token': 'Time to first token',
        'time_to_second_token': 'Time to second token',
        'tokens_in_flight': 'Tokens in flight',
        'total_output_tokens': 'Total output tokens',
        'total_token_throughput': 'Total token throughput',
        'total_usage_completion_tokens': 'Total completion tokens',
        'total_usage_prompt_tokens': 'Total prompt tokens',
        'total_usage_total_tokens': 'Total tokens',
    }

    versions = set(meta['version'] for meta in configs.values() if meta['version'])
    version_str = ', '.join(sorted(versions)) if versions else 'unknown'

    results_dir = os.path.abspath(results_dir)
    embedded_dashboards = {}
    for cfg, meta in configs.items():
        cfg_results_dir = meta.get('_results_dir', results_dir)
        raw_cfg = cfg.split('/', 1)[-1] if '/' in cfg else cfg
        for c_val in meta['runs']:
            key = f'c{c_val}'
            dash_path = os.path.join(cfg_results_dir, f'results_{raw_cfg}', f'results_{raw_cfg}_{key}', 'dashboard.html')
            if os.path.isfile(dash_path):
                with open(dash_path, 'rb') as df:
                    content = df.read()
                content = inject_dashboard_aggregation(content)
                embedded_dashboards[f'{cfg}_{key}'] = base64.b64encode(content).decode('ascii')

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
            yaml_parts.append(f'<div class="yaml-section" data-yaml-cfg="{cfg}">')
            yaml_parts.append(f'<div class="yaml-cfg-label" style="color:{color}">{meta["label"]}</div>')
            yaml_parts.append('<div class="yaml-btn-row">')
            for yname, ycontent in sorted(meta['yamls'].items()):
                uid = f'yaml-{cfg}-{yname.replace(".", "-")}'
                yaml_parts.append(
                    f'<button class="yaml-toggle" style="border-color:{color};color:{color}" '
                    f'onclick="var t=document.getElementById(\'{uid}\'),s=this.closest(\'.yaml-section\');'
                    f's.querySelectorAll(\'.yaml-block.open\').forEach(function(b){{if(b!==t)b.classList.remove(\'open\')}});'
                    f't.classList.toggle(\'open\')">{yname}</button>'
                )
            yaml_parts.append('</div>')
            for yname, ycontent in sorted(meta['yamls'].items()):
                uid = f'yaml-{cfg}-{yname.replace(".", "-")}'
                highlighted = highlight_yaml(ycontent)
                yaml_parts.append(f'<div class="yaml-block" id="{uid}"><pre>{highlighted}</pre></div>')
            yaml_parts.append('</div>')
        yaml_parts.append('</div>')
    yaml_sections_html = '\n'.join(yaml_parts)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{model_label} — Interactivity vs Throughput — vLLM {version_str}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  :root {{
    --bg: #111217; --bg2: #181b1f; --bg3: #0d1117; --border: #2a2a2e; --border2: #1e1e22;
    --fg: #ececec; --fg2: #c0c0c0; --fg3: #8e8e8e; --hover-bg: #1e2127;
    --highlight: #58a6ff; --overlay: rgba(0,0,0,0.4);
    --input-bg: #181b1f; --input-fg: #d8d9da; --input-border: #2a2a2e;
  }}
  html.light {{
    --bg: #f5f6f8; --bg2: #ffffff; --bg3: #f0f1f3; --border: #d4d6db; --border2: #e4e6ea;
    --fg: #1a1a1a; --fg2: #444; --fg3: #666; --hover-bg: #eef0f4;
    --highlight: #0969da; --overlay: rgba(0,0,0,0.15);
    --input-bg: #ffffff; --input-fg: #1a1a1a; --input-border: #c8cacd;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: var(--bg); color: var(--fg); font-family: Inter, -apple-system, sans-serif; padding: 16px;
         transition: background .2s, color .2s; }}
  h1 {{ font-size: 20px; font-weight: 600; margin-bottom: 4px; }}
  .subtitle {{ font-size: 13px; color: var(--fg3); margin-bottom: 20px; }}
  .row-header {{ font-size: 15px; font-weight: 600; color: var(--fg); padding: 10px 0 6px 4px;
                 border-bottom: 1px solid var(--border); margin: 16px 0 8px 0; cursor: pointer; user-select: none; }}
  .row-header:hover {{ opacity: 0.8; }}
  .row-header .arrow {{ display: inline-block; width: 16px; transition: transform .15s; }}
  .row-header.collapsed .arrow {{ transform: rotate(-90deg); }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(580px, 1fr)); gap: 8px; }}
  .panel {{ background: var(--bg2); border: 1px solid var(--border); border-radius: 6px; padding: 0;
             overflow: hidden; resize: both; min-width: 400px; min-height: 300px; }}
  .panel::-webkit-resizable {{ background: transparent; }}
  .panel-title {{ font-size: 13px; font-weight: 600; padding: 8px 12px; color: var(--fg); }}
  .panel .plot {{ width: 100%; height: calc(100% - 36px); min-height: 250px; }}
  .panel .plot .nsewdrag {{ cursor: pointer !important; }}
  .summary {{ background: var(--bg2); border: 1px solid var(--border); border-radius: 6px; padding: 16px; margin-bottom: 16px; overflow-x: auto; }}
  .summary table {{ width: 100%; border-collapse: collapse; font-size: 12px; min-width: 900px; }}
  .summary th {{ text-align: left; padding: 7px 8px; color: var(--fg); border-bottom: 2px solid var(--border); font-weight: 700;
                 cursor: pointer; user-select: none; white-space: nowrap; font-size: 12px; }}
  .summary th:hover {{ color: var(--fg); }}
  .chart-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 8px; }}
  @media (max-width: 1200px) {{ .chart-row {{ grid-template-columns: 1fr; }} }}
  .chart-col {{ display: flex; flex-direction: column; gap: 4px; }}
  .chart-col .panel {{ min-width: 0; }}
  .axis-controls {{ display: flex; gap: 10px; align-items: center; padding: 6px 4px; flex-wrap: wrap; }}
  .axis-controls label {{ color: var(--fg3); font-size: 12px; display: flex; align-items: center; gap: 4px; }}
  .axis-controls select {{ background: var(--input-bg); color: var(--input-fg); border: 1px solid var(--input-border); border-radius: 4px;
                           padding: 4px 6px; font-size: 11px; font-family: inherit; max-width: 300px; }}
  .summary td {{ padding: 6px 8px; border-bottom: 1px solid var(--border2); }}
  .summary tr:hover {{ background: var(--hover-bg); }}
  .highlight {{ color: var(--highlight); font-weight: 600; }}
  .hidden {{ display: none; }}
  .slo-fail {{ opacity: 0.25; text-decoration: line-through; }}
  .yaml-section {{ margin-bottom: 12px; }}
  .yaml-cfg-label {{ font-size: 13px; font-weight: 600; margin-bottom: 4px; }}
  .yaml-btn-row {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 4px; }}
  .yaml-toggle {{ background: none; border: 1px solid var(--border); color: var(--fg3); border-radius: 4px;
                   padding: 4px 10px; cursor: pointer; font-size: 12px; font-family: inherit; }}
  .yaml-toggle:hover {{ color: var(--fg); border-color: var(--fg3); }}
  .yaml-block {{ background: var(--bg3); border: 1px solid var(--border); border-radius: 4px; padding: 16px;
                  margin-top: 4px; overflow-x: auto; display: none; }}
  .yaml-block.open {{ display: block; }}
  .yaml-block pre {{ font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace; font-size: 12px;
                      line-height: 1.5; color: var(--fg); white-space: pre; margin: 0; }}
  .yaml-block .yk {{ color: #7ee787; }}
  .yaml-block .yv {{ color: #d2a8ff; }}
  .yaml-block .ys {{ color: #a5d6ff; }}
  .yaml-block .yc {{ color: #8b949e; font-style: italic; }}
  .yaml-block .yn {{ color: #79c0ff; }}
  html.light .yaml-block .yk {{ color: #116329; }}
  html.light .yaml-block .yv {{ color: #6f42c1; }}
  html.light .yaml-block .ys {{ color: #0550ae; }}
  html.light .yaml-block .yc {{ color: #6a737d; }}
  html.light .yaml-block .yn {{ color: #0550ae; }}
  .side-panel {{ position: fixed; top: 0; right: 0; width: 55vw; height: 100vh; background: var(--bg);
                 border-left: 2px solid var(--border); z-index: 1000; transform: translateX(100%);
                 transition: transform .25s ease; display: flex; flex-direction: column; }}
  .side-panel.open {{ transform: translateX(0); }}
  .side-panel-header {{ display: flex; align-items: center; justify-content: space-between;
                        padding: 10px 16px; border-bottom: 1px solid var(--border); flex-shrink: 0; }}
  .side-panel-header span {{ font-size: 14px; font-weight: 600; color: var(--fg); }}
  .side-panel-close {{ background: none; border: 1px solid var(--border); color: var(--fg); border-radius: 4px;
                       padding: 4px 12px; cursor: pointer; font-size: 13px; }}
  .side-panel-close:hover {{ background: var(--hover-bg); }}
  .side-panel iframe {{ flex: 1; border: none; width: 100%; }}
  .side-panel-resize {{ position: absolute; left: -4px; top: 0; width: 8px; height: 100%;
                        cursor: col-resize; z-index: 1001; }}
  .side-overlay {{ position: fixed; inset: 0; background: var(--overlay); z-index: 999;
                   display: none; cursor: pointer; }}
  .side-overlay.open {{ display: block; }}
  .theme-toggle {{ background: none; border: 1px solid var(--border); color: var(--fg); border-radius: 4px;
                   padding: 5px 12px; cursor: pointer; font-size: 18px; line-height: 1; }}
  .theme-toggle:hover {{ background: var(--hover-bg); }}
</style>
</head>
<body>
<div id="titleBar" style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; padding:8px 0 12px; border-bottom:1px solid var(--border); margin-bottom:12px;">
  <div>
    <h1 style="margin:0">{model_label} Disaggregated Serving — Interactivity vs Throughput</h1>
    <div class="subtitle" style="margin:0">vLLM {version_str} &middot; {len(configs)} deployment configs &middot; {len(sorted_conc)} concurrency levels</div>
  </div>
  <div id="controlsBar" style="display:flex; gap:14px; align-items:center; flex-wrap:wrap;"></div>
</div>

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
let isDark = true;
const THEMES = {{
  dark: {{
    paper: '#181b1f', plot: '#181b1f', fg: '#ececec', fg2: '#c0c0c0', fg3: '#ffffff',
    grid: '#242429', legend: 'rgba(0,0,0,0.35)', legendBorder: '#333',
    hover: '#1a1d22', hoverBorder: '#555',
  }},
  light: {{
    paper: '#ffffff', plot: '#ffffff', fg: '#1a1a1a', fg2: '#444', fg3: '#1a1a1a',
    grid: '#e0e0e0', legend: 'rgba(255,255,255,0.85)', legendBorder: '#ccc',
    hover: '#ffffff', hoverBorder: '#aaa',
  }},
}};

function getLayoutDefaults() {{
  const t = isDark ? THEMES.dark : THEMES.light;
  return {{
    paper_bgcolor: t.paper, plot_bgcolor: t.plot,
    font: {{ family: 'Inter, -apple-system, sans-serif', size: 13, color: t.fg }},
    margin: {{ t: 44, r: 30, b: 50, l: 76 }},
    xaxis: {{ gridcolor: t.grid, zerolinecolor: t.grid, linecolor: t.grid, gridwidth: 1,
              title: {{ font: {{ size: 14, color: t.fg, weight: 700 }} }}, tickfont: {{ size: 12, color: t.fg2 }} }},
    yaxis: {{ gridcolor: t.grid, zerolinecolor: t.grid, linecolor: t.grid, gridwidth: 1,
              title: {{ font: {{ size: 14, color: t.fg, weight: 700 }} }}, tickfont: {{ size: 12, color: t.fg2 }} }},
    legend: {{ bgcolor: t.legend, font: {{ size: 12, color: t.fg }}, bordercolor: t.legendBorder, borderwidth: 1 }},
    hoverlabel: {{ bgcolor: t.hover, bordercolor: t.hoverBorder, font: {{ size: 13, color: t.fg }} }},
    hovermode: 'closest',
  }};
}}
let LAYOUT_DEFAULTS = getLayoutDefaults();

const COLORS_DARK = {json.dumps(color_map_dark)};
const COLORS_LIGHT = {json.dumps(color_map_light)};
let COLORS = isDark ? COLORS_DARK : COLORS_LIGHT;
const SHAPES = {json.dumps(shape_map)};
const CONFIGS = {json.dumps(configs_js)};
const CONCURRENCIES = {json.dumps(conc_list_js)};
const C_LABELS = {json.dumps(c_labels_js)};
const DATA = {json.dumps(data_js)};
const DASHBOARDS = {json.dumps(embedded_dashboards)};
const METRICS = {json.dumps(metrics_js)};
const X_AXIS_METRICS = {json.dumps(x_axis_metrics)};
const Y_AXIS_METRICS = {json.dumps(y_axis_metrics)};
const DECODE_NORMALIZED_METRICS = new Set({json.dumps(decode_normalized_metrics)});
const PREFILL_NORMALIZED_METRICS = new Set({json.dumps(prefill_normalized_metrics)});
const TOTAL_NORMALIZED_METRICS = new Set({json.dumps(total_normalized_metrics)});
const METRIC_LABELS = {json.dumps(metric_labels)};
const STAT_KEYS = {json.dumps(STAT_KEYS)};
const CONFIG_KEYS = Object.keys(CONFIGS).sort((a, b) => CONFIGS[a].label.localeCompare(CONFIGS[b].label));
{f"const REFERENCE_DATA = {json.dumps(_filter_reference_by_gpus(reference_data, configs))};"}
const REF_KEYS = Object.keys(REFERENCE_DATA).sort();

const root = document.getElementById('root');
const sidePanel = document.getElementById('sidePanel');
const sidePanelFrame = document.getElementById('sidePanelFrame');
const sidePanelTitle = document.getElementById('sidePanelTitle');
const overlay = document.getElementById('overlay');

const blobCache = {{}};

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
  window.open(blobCache[key], '_blank', 'width=1200,height=800,menubar=no,toolbar=no');
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
    if (pt.curveNumber >= CONFIG_KEYS.length) return;
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
  return METRIC_LABELS[key] || key.replace(/_/g, ' ');
}}

let gpuCostPerHour = 0;
let sloTTFT = null;
let sloTTFTStat = 'p99';
let sloITL = null;
let sloITLStat = 'p99';
let sloTokGPU = null;
let sloTokUser = null;
let sloTokUserStat = 'avg';
let maxWaitPrefill = null;
let maxWaitDecode = null;
let minConcurrency = null;
let maxConcurrency = null;

function passesSLO(cfg, c) {{
  if (minConcurrency != null && C_LABELS[c] < minConcurrency) return false;
  if (maxConcurrency != null && C_LABELS[c] > maxConcurrency) return false;
  const d = DATA[cfg]?.[c];
  if (!d) return true;
  const meta = CONFIGS[cfg];
  if (sloTTFT != null) {{
    const v = d.time_to_first_token?.[sloTTFTStat];
    if (v != null && v / 1000 > sloTTFT) return false;
  }}
  if (sloITL != null) {{
    const v = d.inter_token_latency?.[sloITLStat];
    if (v != null && v > sloITL) return false;
  }}
  if (sloTokGPU != null) {{
    const v = d.output_token_throughput?.avg;
    var g = meta.decodeGPUs || (meta.decodeGPUs + meta.prefillGPUs);
    if (v != null && g > 0 && v / g < sloTokGPU) return false;
  }}
  if (sloTokUser != null) {{
    const v = d.output_token_throughput_per_user?.[sloTokUserStat];
    if (v != null && v < sloTokUser) return false;
  }}
  if (maxWaitPrefill != null) {{
    const v = d._max_waiting_prefill;
    if (v != null && v > maxWaitPrefill) return false;
  }}
  if (maxWaitDecode != null) {{
    const v = d._max_waiting_decode;
    if (v != null && v > maxWaitDecode) return false;
  }}
  return true;
}}

function applyNorm(val, norm, meta) {{
  if (val == null) return null;
  if (norm.startsWith('cost') && (val <= 0 || gpuCostPerHour <= 0)) return null;
  if (val === 0) return null;
  var dGPU = meta.decodeGPUs || 0;
  var pGPU = meta.prefillGPUs || 0;
  var tGPU = dGPU + pGPU;
  if (norm === 'decode') {{ var g = dGPU || tGPU; return g > 0 ? val / g : null; }}
  if (norm === 'prefill') {{ var g = pGPU || tGPU; return g > 0 ? val / g : null; }}
  if (norm === 'total') return tGPU > 0 ? val / tGPU : null;
  if (norm === 'cost_decode') {{ var g = dGPU || tGPU; return g > 0 ? (g * gpuCostPerHour * 1e6) / (val * 3600) : null; }}
  if (norm === 'cost_prefill') {{ var g = pGPU || tGPU; return g > 0 ? (g * gpuCostPerHour * 1e6) / (val * 3600) : null; }}
  if (norm === 'cost_total') return tGPU > 0 ? (tGPU * gpuCostPerHour * 1e6) / (val * 3600) : null;
  return val;
}}

function normSuffix(norm) {{
  if (norm === 'decode') return ' / decode GPU';
  if (norm === 'prefill') return ' / prefill GPU';
  if (norm === 'total') return ' / total GPU';
  if (norm === 'cost_decode') return ' ($/M tokens, decode)';
  if (norm === 'cost_prefill') return ' ($/M tokens, prefill)';
  if (norm === 'cost_total') return ' ($/M tokens, total)';
  return '';
}}

function metricOptions(keys) {{
  return keys.filter(k => METRICS[k]).map(k => ({{
    value: k, text: metricLabel(k) + (METRICS[k].unit ? ` (${{METRICS[k].unit}})` : '')
  }}));
}}

function metricSample(metric) {{
  for (const cfg of CONFIG_KEYS) {{
    for (const c of CONCURRENCIES) {{
      const sample = DATA[cfg]?.[c]?.[metric];
      if (sample) return sample;
    }}
  }}
  return null;
}}

function statOptionsForMetric(metric) {{
  const sample = metricSample(metric);
  const keys = sample ? STAT_KEYS.filter(s => Object.prototype.hasOwnProperty.call(sample, s)) : ['avg'];
  return (keys.length ? keys : ['avg']).map(s => ({{ value: s, text: s }}));
}}

function normOptionsForMetric(metric, axis) {{
  const opts = [{{ value: 'none', text: 'none' }}];
  if (DECODE_NORMALIZED_METRICS.has(metric)) {{
    opts.push({{ value: 'decode', text: '/ decode GPUs' }});
    opts.push({{ value: 'total', text: '/ total GPUs' }});
    if (axis === 'y') opts.push({{ value: 'cost_decode', text: '$/M tok (decode GPUs)' }});
  }}
  if (PREFILL_NORMALIZED_METRICS.has(metric)) {{
    opts.push({{ value: 'prefill', text: '/ prefill GPUs' }});
    opts.push({{ value: 'total', text: '/ total GPUs' }});
    if (axis === 'y') opts.push({{ value: 'cost_prefill', text: '$/M tok (prefill GPUs)' }});
  }}
  if (TOTAL_NORMALIZED_METRICS.has(metric)) {{
    opts.push({{ value: 'total', text: '/ total GPUs' }});
    if (axis === 'y') opts.push({{ value: 'cost_total', text: '$/M tok (total GPUs)' }});
  }}
  return opts;
}}

function setSelectOptions(sel, options, value) {{
  sel.replaceChildren();
  options.forEach(o => {{
    const opt = document.createElement('option');
    opt.value = o.value;
    opt.textContent = o.text;
    sel.appendChild(opt);
  }});
  const values = new Set(options.map(o => o.value));
  sel.value = values.has(value) ? value : options[0]?.value;
  return sel.value;
}}

function hoverText(cfg, c, d) {{
  const meta = CONFIGS[cfg];
  const out = d.output_token_throughput;
  const itl = d.inter_token_latency;
  const otpu = d.output_token_throughput_per_user;
  const ttft = d.time_to_first_token;
  const normGpus = meta.decodeGPUs || (meta.decodeGPUs + meta.prefillGPUs);
  const norm = out && normGpus > 0 ? (out.avg / normGpus).toFixed(1) : '?';
  const gpuLabel = meta.prefillGPUs > 0 ? 'decode GPU' : 'GPU';
  let text = `<b>${{meta.label}} @ c${{C_LABELS[c]}}</b><br>` +
    `Output: ${{out?.avg?.toFixed(1) ?? '?'}} tok/s (${{norm}} tok/s/${{gpuLabel}})<br>` +
    `ITL p50: ${{itl?.p50?.toFixed(1) ?? '?'}} ms · p99: ${{itl?.p99?.toFixed(1) ?? '?'}} ms<br>` +
    `Per-user: ${{otpu?.avg?.toFixed(1) ?? '?'}} tok/s/user<br>` +
    `TTFT avg: ${{ttft ? (ttft.avg/1000).toFixed(1) : '?'}}s · p50: ${{ttft ? (ttft.p50/1000).toFixed(1) : '?'}}s · p99: ${{ttft ? (ttft.p99/1000).toFixed(1) : '?'}}s`;

  const pc = d._prefix_cache;
  if (pc) {{
    const fp = v => (v == null ? '?' : v.toFixed(1) + '%');
    const roles = pc.roles || {{}};
    const localParts = Object.keys(roles).map(r => `${{r}} ${{fp(roles[r].local_hit_pct)}}`);
    const usageParts = Object.keys(roles).map(r => `${{r}} ${{fp(roles[r].kv_cache_usage_avg_pct)}}`);
    text += `<br>Cache hit (theoretical ${{fp(pc.theoretical_pct)}}): local ${{localParts.join(' · ')}}<br>` +
      `KV usage: ${{usageParts.join(' · ')}}`;
  }}
  return text;
}}

const allCharts = [];

function computePareto(points) {{
  // points: [{{x, y, idx}}] — returns indices of non-dominated points (higher x AND higher y is better)
  const valid = points.filter(p => p.x != null && p.y != null);
  if (!valid.length) return new Set();
  valid.sort((a, b) => b.x - a.x || b.y - a.y);
  const frontier = new Set();
  let maxY = -Infinity;
  for (const p of valid) {{
    if (p.y >= maxY) {{
      frontier.add(p.idx);
      maxY = p.y;
    }}
  }}
  return frontier;
}}

// Map chart metric+norm to reference data point values
function refMetricValue(pt, metric, norm, ref) {{
  const totalGPUs = ref.total_gpus || 1;
  let raw = null;
  if (metric === 'e2e_output_token_throughput') raw = pt.e2e_output_per_user;
  else if (metric === 'output_token_throughput') raw = pt.output_per_gpu * totalGPUs;
  else if (metric === 'output_token_throughput_per_user') raw = pt.interactivity_avg;
  else if (metric === 'input_token_throughput') raw = pt.input_per_gpu * totalGPUs;
  else if (metric === 'inter_token_latency') raw = pt.itl_avg;
  else if (metric === 'interactivity') raw = pt.interactivity_avg;
  else if (metric === 'time_to_first_token') raw = pt.ttft_avg;
  else if (metric === 'total_token_throughput') raw = (pt.output_per_gpu + pt.input_per_gpu) * totalGPUs;
  else return null;
  if (raw == null || raw === 0) return null;
  // Apply normalization using total_gpus (non-disaggregated: decode=prefill=total)
  const meta = {{ decodeGPUs: totalGPUs, prefillGPUs: totalGPUs }};
  return applyNorm(raw, norm, meta);
}}

function createChart(container, defaults) {{
  const state = {{
    xMetric: defaults.xMetric || 'e2e_output_token_throughput',
    xStat: defaults.xStat || 'avg',
    xNorm: defaults.xNorm || 'none',
    yMetric: defaults.yMetric || 'output_token_throughput',
    yStat: defaults.yStat || 'avg',
    yNorm: defaults.yNorm || 'decode',
    paretoEnabled: true,
    el: null,
  }};

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

  const xMetricOpts = metricOptions(X_AXIS_METRICS);
  const yMetricOpts = metricOptions(Y_AXIS_METRICS);

  const xDiv = document.createElement('div');
  xDiv.className = 'axis-controls';
  xDiv.style.borderBottom = '1px solid var(--border)';
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

  const xMetricSel = mkSelIn(xDiv, 'X:', xMetricOpts, state.xMetric, v => {{
    state.xMetric = v;
    state.xStat = setSelectOptions(xStatSel, statOptionsForMetric(v), state.xStat);
    state.xNorm = setSelectOptions(xNormSel, normOptionsForMetric(v, 'x'), state.xNorm);
    update();
  }});
  const xStatSel = mkSelIn(xDiv, 'stat:', statOptionsForMetric(state.xMetric), state.xStat, v => {{ state.xStat = v; update(); }});
  const xNormSel = mkSelIn(xDiv, 'norm:', normOptionsForMetric(state.xMetric, 'x'), state.xNorm, v => {{ state.xNorm = v; update(); }});
  state.xMetric = xMetricSel.value;
  state.xStat = xStatSel.value;
  state.xNorm = xNormSel.value;
  container.appendChild(xDiv);

  const yDiv = document.createElement('div');
  yDiv.className = 'axis-controls';
  const yMetricSel = mkSelIn(yDiv, 'Y:', yMetricOpts, state.yMetric, v => {{
    state.yMetric = v;
    state.yStat = setSelectOptions(yStatSel, statOptionsForMetric(v), state.yStat);
    state.yNorm = setSelectOptions(yNormSel, normOptionsForMetric(v, 'y'), state.yNorm);
    update();
  }});
  const yStatSel = mkSelIn(yDiv, 'stat:', statOptionsForMetric(state.yMetric), state.yStat, v => {{ state.yStat = v; update(); }});
  const yNormSel = mkSelIn(yDiv, 'norm:', normOptionsForMetric(state.yMetric, 'y'), state.yNorm, v => {{ state.yNorm = v; update(); }});
  state.yMetric = yMetricSel.value;
  state.yStat = yStatSel.value;
  state.yNorm = yNormSel.value;
  container.appendChild(yDiv);

  const panel = document.createElement('div');
  panel.className = 'panel';
  panel.style.height = '500px';
  panel.style.overflow = 'visible';
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
    if (norm.startsWith('cost')) return `${{name}}${{ss}}${{ns}}`;
    return `${{name}}${{ss}}${{ns}} (${{unit}})`;
  }}

  function buildTraces() {{
    const baseTraces = CONFIG_KEYS.map((cfg, cfgIdx) => {{
      const meta = CONFIGS[cfg];
      const validConcs = CONCURRENCIES.filter(c => DATA[cfg] && DATA[cfg][c]);
      return {{
        cfg, cfgIdx, validConcs,
        x: validConcs.map(c => {{
          if (!passesSLO(cfg, c)) return null;
          const m = DATA[cfg][c][state.xMetric];
          return m ? applyNorm(m[state.xStat], state.xNorm, meta) : null;
        }}),
        y: validConcs.map(c => {{
          if (!passesSLO(cfg, c)) return null;
          const m = DATA[cfg][c][state.yMetric];
          return m ? applyNorm(m[state.yStat], state.yNorm, meta) : null;
        }}),
      }};
    }});

    let paretoSet = null;
    if (state.paretoEnabled) {{
      const allPts = [];
      baseTraces.forEach((bt, ci) => {{
        bt.x.forEach((xv, pi) => {{
          if (xv != null && bt.y[pi] != null) {{
            allPts.push({{ x: xv, y: bt.y[pi], idx: `${{ci}}_${{pi}}` }});
          }}
        }});
      }});
      paretoSet = computePareto(allPts);
    }}

    const traces = baseTraces.map((bt, ci) => {{
      const meta = CONFIGS[bt.cfg];
      return {{
        x: bt.x.map((xv, pi) => {{
          if (paretoSet && !paretoSet.has(`${{ci}}_${{pi}}`)) return null;
          return xv;
        }}),
        y: bt.y.map((yv, pi) => {{
          if (paretoSet && !paretoSet.has(`${{ci}}_${{pi}}`)) return null;
          return yv;
        }}),
        text: bt.validConcs.map((c, pi) => {{
          if (paretoSet && !paretoSet.has(`${{ci}}_${{pi}}`)) return '';
          return passesSLO(bt.cfg, c) ? hoverText(bt.cfg, c, DATA[bt.cfg][c]) : '';
        }}),
        hoverinfo: 'text',
        mode: 'lines+markers+text',
        textposition: 'top center',
        textfont: {{ size: 11, color: COLORS[bt.cfg], family: 'Inter, -apple-system, sans-serif', weight: 600 }},
        texttemplate: bt.validConcs.map((c, pi) => {{
          if (paretoSet && !paretoSet.has(`${{ci}}_${{pi}}`)) return '';
          return passesSLO(bt.cfg, c) ? `c${{C_LABELS[c]}}` : '';
        }}),
        name: meta.label,
        line: {{ color: COLORS[bt.cfg], width: 2 }},
        marker: {{ size: 10, color: COLORS[bt.cfg], symbol: SHAPES[bt.cfg] || 'circle', line: {{ color: 'rgba(0,0,0,0.4)', width: 0.5 }} }},
      }};
    }});

    // Add Pareto frontier line when enabled
    if (state.paretoEnabled && paretoSet && paretoSet.size > 1) {{
      const frontierPts = [];
      baseTraces.forEach((bt, ci) => {{
        bt.x.forEach((xv, pi) => {{
          if (paretoSet.has(`${{ci}}_${{pi}}`) && xv != null && bt.y[pi] != null) {{
            frontierPts.push({{ x: xv, y: bt.y[pi] }});
          }}
        }});
      }});
      frontierPts.sort((a, b) => a.x - b.x);
      traces.push({{
        x: frontierPts.map(p => p.x),
        y: frontierPts.map(p => p.y),
        mode: 'lines',
        line: {{ color: isDark ? 'rgba(255,255,255,0.4)' : 'rgba(0,0,0,0.3)', width: 2, dash: 'dot' }},
        name: 'Pareto frontier',
        hoverinfo: 'skip',
        showlegend: true,
      }});
    }}

    // Add reference Pareto frontier (InferenceX vllm)
    if (REF_KEYS.length > 0) {{
      const allRefPts = [];
      REF_KEYS.forEach(rk => {{
        const ref = REFERENCE_DATA[rk];
        ref.points.forEach((p, pi) => {{
          const xv = refMetricValue(p, state.xMetric, state.xNorm, ref);
          const yv = refMetricValue(p, state.yMetric, state.yNorm, ref);
          if (xv != null && yv != null) {{
            allRefPts.push({{ x: xv, y: yv, p, ref, idx: `ref_${{rk}}_${{pi}}` }});
          }}
        }});
      }});
      if (allRefPts.length > 0) {{
        const refPareto = computePareto(allRefPts);
        const frontier = allRefPts.filter(pt => refPareto.has(pt.idx)).sort((a, b) => a.x - b.x);
        if (frontier.length > 0) {{
          traces.push({{
            x: frontier.map(f => f.x),
            y: frontier.map(f => f.y),
            text: frontier.map(f => {{
              const p = f.p; const ref = f.ref;
              const cfgLabel = (ref.ep > 1 && ref.dp_attention) ? 'DEP' + ref.ep : 'TP' + ref.tp;
              return `<b>vllm ${{ref.hardware}} ${{cfgLabel}} (${{ref.total_gpus}}GPU) @ c${{p.concurrency}}</b><br>` +
                `Output/GPU: ${{p.output_per_gpu.toFixed(1)}} tok/s<br>` +
                `E2E output: ${{p.e2e_output_throughput.toFixed(0)}} tok/s<br>` +
                `Interactivity: ${{p.interactivity_avg.toFixed(1)}} tok/s/user<br>` +
                `ITL avg: ${{p.itl_avg.toFixed(1)}} ms · TTFT avg: ${{(p.ttft_avg).toFixed(0)}}ms`;
            }}),
            hoverinfo: 'text',
            mode: 'lines+markers+text',
            textposition: 'bottom center',
            textfont: {{ size: 9, color: isDark ? '#cccccc' : '#555555', family: 'Inter, -apple-system, sans-serif' }},
            texttemplate: frontier.map(f => `c${{f.p.concurrency}}`),
            name: 'vllm Pareto (InferenceX)',
            line: {{ color: isDark ? '#ffffff' : '#333333', width: 2, dash: 'dash' }},
            marker: {{ size: 9, color: isDark ? '#ffffff' : '#333333', symbol: 'diamond', line: {{ color: isDark ? '#333' : '#fff', width: 1 }} }},
          }});
        }}
      }}
    }}

    return traces;
  }}

  function getLayout() {{
    const yAxis = {{ ...LAYOUT_DEFAULTS.yaxis, title: {{ text: axisTitle(state.yMetric, state.yStat, state.yNorm), font: {{ size: 14, color: LAYOUT_DEFAULTS.font.color, weight: 700 }} }} }};
    if (!state.yNorm.startsWith('cost')) yAxis.rangemode = 'tozero';
    return {{
      ...LAYOUT_DEFAULTS,
      title: {{ text: `${{metricLabel(state.yMetric)}} vs ${{metricLabel(state.xMetric)}}`, font: {{ size: 15, color: LAYOUT_DEFAULTS.font.color, weight: 700 }} }},
      xaxis: {{ ...LAYOUT_DEFAULTS.xaxis, title: {{ text: axisTitle(state.xMetric, state.xStat, state.xNorm), font: {{ size: 14, color: LAYOUT_DEFAULTS.font.color, weight: 700 }} }} }},
      yaxis: yAxis,
      legend: {{ ...LAYOUT_DEFAULTS.legend, orientation: 'v', x: 1.02, y: 1, xanchor: 'left', yanchor: 'top' }},
    }};
  }}

  function update() {{
    const traces = buildTraces();
    const layout = getLayout();
    if (state.el.data) {{
      state.el.data.forEach((old, i) => {{
        if (!traces[i]) return;
        if (i < CONFIG_KEYS.length) {{
          if (old.visible === 'legendonly' || old.visible === false) traces[i].visible = old.visible;
          if (old.showlegend === false) traces[i].showlegend = false;
        }}
      }});
    }}
    if (state.el.layout && state.el.layout.legend) {{
      const cur = state.el.layout.legend;
      if (cur.x != null) layout.legend.x = cur.x;
      if (cur.y != null) layout.legend.y = cur.y;
    }}
    Plotly.react(state.el, traces, layout, {{ responsive: true, edits: {{ legendPosition: true }} }});
  }}

  Plotly.newPlot(state.el, buildTraces(), getLayout(), {{ responsive: true, edits: {{ legendPosition: true }} }});
  attachClickHandler(state.el);

  // Filter buttons
  const btnBar = document.createElement('div');
  btnBar.style.cssText = 'display:flex;gap:6px;margin:4px 0;align-items:center;';
  const btnStyle = 'background:var(--input-bg);color:var(--input-fg);border:1px solid var(--input-border);border-radius:4px;padding:4px 12px;font-size:11px;cursor:pointer;font-family:inherit;';

  const paretoBtn = document.createElement('button');
  paretoBtn.textContent = 'Pareto Frontier';
  paretoBtn.style.cssText = btnStyle + 'border-color:var(--highlight);color:var(--highlight);';
  paretoBtn.addEventListener('click', () => {{
    state.paretoEnabled = !state.paretoEnabled;
    paretoBtn.style.borderColor = state.paretoEnabled ? 'var(--highlight)' : 'var(--input-border)';
    paretoBtn.style.color = state.paretoEnabled ? 'var(--highlight)' : 'var(--input-fg)';
    update();
  }});
  btnBar.appendChild(paretoBtn);

  const keepSelBtn = document.createElement('button');
  keepSelBtn.textContent = 'Keep Selected Only';
  keepSelBtn.style.cssText = btnStyle;
  keepSelBtn.addEventListener('click', () => {{
    const showlegend = state.el.data.map(t => t.visible !== 'legendonly' && t.visible !== false);
    Plotly.restyle(state.el, {{ showlegend }});
    showAllBtn.style.display = '';
  }});

  const showAllBtn = document.createElement('button');
  showAllBtn.textContent = 'Show All';
  showAllBtn.style.cssText = btnStyle + 'display:none;';
  showAllBtn.addEventListener('click', () => {{
    Plotly.restyle(state.el, {{ visible: state.el.data.map(() => true), showlegend: state.el.data.map(() => true) }});
    showAllBtn.style.display = 'none';
  }});

  btnBar.appendChild(keepSelBtn);
  btnBar.appendChild(showAllBtn);
  container.appendChild(btnBar);

  function syncBtnVisibility() {{
    const allVisible = state.el.data.every(t => t.visible !== 'legendonly' && t.visible !== false);
    const anyLegendHidden = state.el.data.some(t => !t.showlegend);
    btnBar.style.display = 'flex';
    keepSelBtn.style.display = allVisible ? 'none' : '';
    showAllBtn.style.display = anyLegendHidden ? '' : 'none';
  }}
  syncBtnVisibility();

  state.el.on('plotly_restyle', function() {{
    CONFIG_KEYS.forEach((cfg, i) => {{
      if (!state.el.data[i]) return;
      const vis = state.el.data[i].visible;
      const show = vis !== 'legendonly' && vis !== false;
      document.querySelectorAll(`tr[data-cfg="${{cfg}}"]`).forEach(r => {{
        r.style.display = show ? '' : 'none';
      }});
    }});
    syncBtnVisibility();
    updateYamlVisibility();
  }});

  const chart = {{ state, update }};
  allCharts.push(chart);
  return chart;
}}

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

createChart(chartCol1, {{ xMetric: 'e2e_output_token_throughput', yMetric: 'output_token_throughput', yNorm: 'decode' }});
createChart(chartCol2, {{ xMetric: 'e2e_output_token_throughput', yMetric: 'input_token_throughput', yNorm: 'prefill' }});

// ── Controls: cost + SLO filters (in title bar) ──
const sloBar = document.getElementById('controlsBar');

const inputStyle = 'background:var(--input-bg); color:var(--input-fg); border:1px solid var(--input-border); border-radius:4px; padding:4px 8px; font-size:12px; width:70px; font-family:inherit;';
const selStyle = 'background:var(--input-bg); color:var(--input-fg); border:1px solid var(--input-border); border-radius:4px; padding:4px 6px; font-size:12px; font-family:inherit;';

// Cost input
const costWrap = document.createElement('label');
costWrap.style.cssText = 'color:var(--fg3); font-size:12px; display:flex; align-items:center; gap:4px;';
costWrap.textContent = '$/node/hr:';
const costInput = document.createElement('input');
costInput.type = 'number';
costInput.step = '0.01';
costInput.min = '0';
costInput.value = '21.68';
costInput.placeholder = '21.68';
costInput.style.cssText = inputStyle + ' width:80px;';
costInput.addEventListener('input', () => {{
  gpuCostPerHour = (parseFloat(costInput.value) || 0) / {GPUS_PER_NODE};
  allCharts.forEach(ch => {{
    if (ch.state.xNorm.startsWith('cost') || ch.state.yNorm.startsWith('cost')) ch.update();
  }});
}});
gpuCostPerHour = (parseFloat(costInput.value) || 0) / {GPUS_PER_NODE};
costWrap.appendChild(costInput);
sloBar.appendChild(costWrap);

function makeSLOStatSel(defaultVal) {{
  const sel = document.createElement('select');
  sel.style.cssText = selStyle;
  ['p50','p90','p95','p99','avg','max'].forEach(s => {{
    const o = document.createElement('option');
    o.value = s; o.textContent = s;
    sel.appendChild(o);
  }});
  sel.value = defaultVal;
  return sel;
}}

// TTFT SLO
const ttftWrap = document.createElement('label');
ttftWrap.style.cssText = 'color:var(--fg3); font-size:12px; display:flex; align-items:center; gap:4px;';
ttftWrap.textContent = 'TTFT SLO (s):';
const ttftStatSel = makeSLOStatSel('p99');
const ttftInput = document.createElement('input');
ttftInput.type = 'number'; ttftInput.step = '0.1'; ttftInput.min = '0';
ttftInput.value = ''; ttftInput.placeholder = 'off';
ttftInput.style.cssText = inputStyle;
ttftWrap.appendChild(ttftStatSel);
ttftWrap.appendChild(ttftInput);
sloBar.appendChild(ttftWrap);

// ITL SLO
const itlWrap = document.createElement('label');
itlWrap.style.cssText = 'color:var(--fg3); font-size:12px; display:flex; align-items:center; gap:4px;';
itlWrap.textContent = 'ITL SLO (ms):';
const itlStatSel = makeSLOStatSel('p99');
const itlInput = document.createElement('input');
itlInput.type = 'number'; itlInput.step = '1'; itlInput.min = '0';
itlInput.value = ''; itlInput.placeholder = 'off';
itlInput.style.cssText = inputStyle;
itlWrap.appendChild(itlStatSel);
itlWrap.appendChild(itlInput);
sloBar.appendChild(itlWrap);

// tok/s/decode GPU SLO (minimum)
const tokWrap = document.createElement('label');
tokWrap.style.cssText = 'color:var(--fg3); font-size:12px; display:flex; align-items:center; gap:4px;';
tokWrap.textContent = 'Min tok/s/GPU:';
const tokInput = document.createElement('input');
tokInput.type = 'number'; tokInput.step = '1'; tokInput.min = '0';
tokInput.value = ''; tokInput.placeholder = 'off';
tokInput.style.cssText = inputStyle;
tokWrap.appendChild(tokInput);
sloBar.appendChild(tokWrap);

// Min tok/s/user SLO
const tokUserWrap = document.createElement('label');
tokUserWrap.style.cssText = 'color:var(--fg3); font-size:12px; display:flex; align-items:center; gap:4px;';
tokUserWrap.textContent = 'Min tok/s/user:';
const tokUserStatSel = makeSLOStatSel('avg');
const tokUserInput = document.createElement('input');
tokUserInput.type = 'number'; tokUserInput.step = '1'; tokUserInput.min = '0';
tokUserInput.value = ''; tokUserInput.placeholder = 'off';
tokUserInput.style.cssText = inputStyle;
tokUserWrap.appendChild(tokUserStatSel);
tokUserWrap.appendChild(tokUserInput);
sloBar.appendChild(tokUserWrap);

// Max waiting request filters
const maxWaitPWrap = document.createElement('div');
maxWaitPWrap.style.cssText = 'color:var(--fg3); font-size:12px; display:flex; align-items:center; gap:4px;';
maxWaitPWrap.textContent = 'Max wait P:';
const maxWaitPInput = document.createElement('input');
maxWaitPInput.type = 'number'; maxWaitPInput.step = '1'; maxWaitPInput.min = '0';
maxWaitPInput.value = ''; maxWaitPInput.placeholder = 'off';
maxWaitPInput.style.cssText = inputStyle;
maxWaitPWrap.appendChild(maxWaitPInput);
sloBar.appendChild(maxWaitPWrap);

const maxWaitDWrap = document.createElement('div');
maxWaitDWrap.style.cssText = 'color:var(--fg3); font-size:12px; display:flex; align-items:center; gap:4px;';
maxWaitDWrap.textContent = 'Max wait D:';
const maxWaitDInput = document.createElement('input');
maxWaitDInput.type = 'number'; maxWaitDInput.step = '1'; maxWaitDInput.min = '0';
maxWaitDInput.value = ''; maxWaitDInput.placeholder = 'off';
maxWaitDInput.style.cssText = inputStyle;
maxWaitDWrap.appendChild(maxWaitDInput);
sloBar.appendChild(maxWaitDWrap);

// Concurrency filters
const minConcWrap = document.createElement('div');
minConcWrap.style.cssText = 'color:var(--fg3); font-size:12px; display:flex; align-items:center; gap:4px;';
minConcWrap.textContent = 'Min conc:';
const minConcInput = document.createElement('input');
minConcInput.type = 'number'; minConcInput.step = '1'; minConcInput.min = '1';
minConcInput.value = ''; minConcInput.placeholder = 'all';
minConcInput.style.cssText = inputStyle;
minConcWrap.appendChild(minConcInput);
sloBar.appendChild(minConcWrap);

const concWrap = document.createElement('label');
concWrap.style.cssText = 'color:var(--fg3); font-size:12px; display:flex; align-items:center; gap:4px;';
concWrap.textContent = 'Max conc:';
const concInput = document.createElement('input');
concInput.type = 'number'; concInput.step = '1'; concInput.min = '1';
concInput.value = ''; concInput.placeholder = 'all';
concInput.style.cssText = inputStyle;
concWrap.appendChild(concInput);
sloBar.appendChild(concWrap);

// Theme toggle
const themeBtn = document.createElement('button');
themeBtn.className = 'theme-toggle';
themeBtn.textContent = '\\u263E';
themeBtn.title = 'Toggle light/dark mode';
themeBtn.addEventListener('click', () => {{
  isDark = !isDark;
  document.documentElement.classList.toggle('light', !isDark);
  themeBtn.textContent = isDark ? '\\u263E' : '\\u2600';
  COLORS = isDark ? COLORS_DARK : COLORS_LIGHT;
  LAYOUT_DEFAULTS = getLayoutDefaults();
  allCharts.forEach(ch => ch.update());
  if (window._updateTableColors) window._updateTableColors();
}});
sloBar.appendChild(themeBtn);

function applySLOFilter() {{
  sloTTFT = parseFloat(ttftInput.value) || null;
  sloTTFTStat = ttftStatSel.value;
  sloITL = parseFloat(itlInput.value) || null;
  sloITLStat = itlStatSel.value;
  sloTokGPU = parseFloat(tokInput.value) || null;
  sloTokUser = parseFloat(tokUserInput.value) || null;
  sloTokUserStat = tokUserStatSel.value;
  maxWaitPrefill = parseFloat(maxWaitPInput.value);
  if (isNaN(maxWaitPrefill)) maxWaitPrefill = null;
  maxWaitDecode = parseFloat(maxWaitDInput.value);
  if (isNaN(maxWaitDecode)) maxWaitDecode = null;
  minConcurrency = parseFloat(minConcInput.value) || null;
  maxConcurrency = parseFloat(concInput.value) || null;
  allCharts.forEach(ch => ch.update());
  document.querySelectorAll('tr[data-cfg]').forEach(tr => {{
    tr.classList.toggle('slo-fail', !passesSLO(tr.dataset.cfg, tr.dataset.conc));
  }});
  if (window._resortTable) window._resortTable();
  updateYamlVisibility();
}}

function updateYamlVisibility() {{
  const cfgHasPass = {{}};
  CONFIG_KEYS.forEach((cfg, i) => {{
    let legendHidden = false;
    for (const ch of allCharts) {{
      if (ch.state.el.data && ch.state.el.data[i]) {{
        const vis = ch.state.el.data[i].visible;
        if (vis === 'legendonly' || vis === false) {{ legendHidden = true; break; }}
      }}
    }}
    if (legendHidden) {{
      cfgHasPass[cfg] = false;
    }} else {{
      cfgHasPass[cfg] = CONCURRENCIES.some(c => DATA[cfg]?.[c] && passesSLO(cfg, c));
    }}
  }});
  document.querySelectorAll('[data-yaml-cfg]').forEach(el => {{
    el.style.display = cfgHasPass[el.dataset.yamlCfg] === false ? 'none' : '';
  }});
}}

[ttftInput, itlInput, tokInput, tokUserInput, maxWaitPInput, maxWaitDInput, minConcInput, concInput].forEach(el => el.addEventListener('input', applySLOFilter));
[ttftStatSel, itlStatSel, tokUserStatSel].forEach(el => el.addEventListener('change', applySLOFilter));

// ── Section 2: Sortable summary table ──
const sec2Wrap = document.createElement('div');
sec2Wrap.id = 'sec-table';
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
    'Config', 'Concurrency', 'Errors', 'Prefill GPUs', 'Decode GPUs',
    'Prefill KV Cache (tokens)', 'Decode KV Cache (tokens)',
    'Theoretical Hit %', 'Prefill Local Hit %', 'Decode Local Hit %',
    'Prefill KV Usage %', 'Decode KV Usage %',
    'Output tok/s', 'Input tok/s', 'Total tok/s',
    'Output tok/s/GPU', 'Input tok/s/GPU', 'Total tok/s/GPU',
    'ITL p50 (ms)', 'ITL p99 (ms)', 'Per-user tok/s',
    'TTFT avg (s)', 'TTFT p50 (s)', 'TTFT p99 (s)',
    'Max Wait P', 'Max Wait D',
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
      const total = d.total_token_throughput;
      const itl = d.inter_token_latency;
      const otpu = d.output_token_throughput_per_user;
      const ttft = d.time_to_first_token;
      const outGpus = meta.decodeGPUs || (meta.decodeGPUs + meta.prefillGPUs);
      const inpGpus = meta.prefillGPUs || (meta.decodeGPUs + meta.prefillGPUs);
      const outPerGpu = out && outGpus > 0 ? (out.avg / outGpus).toFixed(1) : '-';
      const inpPerGpu = inp && inpGpus > 0 ? (inp.avg / inpGpus).toFixed(1) : '-';
      const totalTps = total?.avg ?? ((out?.avg ?? 0) + (inp?.avg ?? 0));
      const totalPerGpu = totalTps > 0 ? (totalTps / totalGPUs).toFixed(1) : '-';

      const errCount = d._error_count || 0;

      const tr = document.createElement('tr');
      tr.dataset.cfg = cfg;
      tr.dataset.conc = c;

      const kvCache = d._kv_cache_tokens;
      const prefillKvStr = kvCache?.prefill != null ? kvCache.prefill.toLocaleString() : '-';
      const decodeKvStr = kvCache?.decode != null ? kvCache.decode.toLocaleString() : '-';

      const theoreticalHit = d.theoretical_prefix_cache_hit?.avg;
      const prefixCache = d._prefix_cache;
      const fmtPct = v => (v == null ? '-' : v.toFixed(1));
      const pcRoles = prefixCache?.roles ?? {{}};
      const aggRole = pcRoles.aggregate;
      const prefillRole = pcRoles.prefill ?? aggRole;
      const decodeRole = pcRoles.decode ?? aggRole;
      const prefillLocalHit = fmtPct(prefillRole?.local_hit_pct);
      const decodeLocalHit = fmtPct(decodeRole?.local_hit_pct);
      const prefillKvUsage = fmtPct(prefillRole?.kv_cache_usage_avg_pct);
      const decodeKvUsage = fmtPct(decodeRole?.kv_cache_usage_avg_pct);

      const vals = [
        meta.label, C_LABELS[c], errCount, meta.prefillGPUs, meta.decodeGPUs,
        prefillKvStr, decodeKvStr,
        fmtPct(theoreticalHit), prefillLocalHit, decodeLocalHit,
        prefillKvUsage, decodeKvUsage,
        out?.avg?.toFixed(1) ?? '-', inp?.avg?.toFixed(1) ?? '-', totalTps > 0 ? totalTps.toFixed(1) : '-',
        outPerGpu, inpPerGpu, totalPerGpu,
        itl?.p50?.toFixed(1) ?? '-', itl?.p99?.toFixed(1) ?? '-',
        otpu?.avg?.toFixed(1) ?? '-',
        ttft ? (ttft.avg/1000).toFixed(1) : '-',
        ttft ? (ttft.p50/1000).toFixed(1) : '-',
        ttft ? (ttft.p99/1000).toFixed(1) : '-',
        d._max_waiting_prefill != null ? Math.round(d._max_waiting_prefill) : '-',
        d._max_waiting_decode != null ? Math.round(d._max_waiting_decode) : '-',
        '-', '-', '-',
      ];
      vals.forEach((v, i) => {{
        const td = document.createElement('td');
        td.textContent = v;
        if (i === 0) {{ td.style.color = COLORS[cfg]; td.style.fontWeight = '600'; td.dataset.cfgColor = cfg; }}
        if (i === 2 && errCount > 0) {{ td.style.color = '#f44336'; td.style.fontWeight = '600'; }}
        if (i >= 16 && i <= 18) td.className = 'highlight';
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
      cc.inpTd.textContent = costPerMTok(cc.prefillGPUs || cc.totalGPUs, cc.inpTps);
      cc.outTd.textContent = costPerMTok(cc.decodeGPUs || cc.totalGPUs, cc.outTps);
      cc.totTd.textContent = costPerMTok(cc.totalGPUs, cc.inpTps + cc.outTps);
    }});
  }}
  updateCostCols();
  costInput.addEventListener('input', updateCostCols);

  let sortCol = -1, sortAsc = true;
  const cfgOrder = {{}};
  CONFIG_KEYS.forEach((k, i) => cfgOrder[k] = i);

  window._resortTable = function() {{
    const rows = Array.from(tbody.querySelectorAll('tr'));
    rows.sort((a, b) => {{
      const aFail = a.classList.contains('slo-fail') ? 1 : 0;
      const bFail = b.classList.contains('slo-fail') ? 1 : 0;
      if (aFail !== bFail) return aFail - bFail;
      if (sortCol >= 0) {{
        const va = parseFloat(a.cells[sortCol]?.textContent) || 0;
        const vb = parseFloat(b.cells[sortCol]?.textContent) || 0;
        const diff = sortAsc ? va - vb : vb - va;
        if (diff !== 0) return diff;
      }}
      const co = cfgOrder[a.dataset.cfg] - cfgOrder[b.dataset.cfg];
      if (co !== 0) return co;
      return (C_LABELS[a.dataset.conc] || 0) - (C_LABELS[b.dataset.conc] || 0);
    }});
    rows.forEach(r => tbody.appendChild(r));
  }};

  function sortTableBy(col, th) {{
    if (sortCol === col) sortAsc = !sortAsc;
    else {{ sortCol = col; sortAsc = true; }}

    thead.querySelectorAll('th').forEach(h => h.textContent = h.dataset.label);
    th.textContent = th.dataset.label + (sortAsc ? ' \\u25B2' : ' \\u25BC');
    window._resortTable();
  }}

  window._updateTableColors = function() {{
    document.querySelectorAll('td[data-cfg-color]').forEach(td => {{
      td.style.color = COLORS[td.dataset.cfgColor];
    }});
  }};

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
    reference_csv = None
    results_dirs = []
    for arg in sys.argv[1:]:
        if arg.startswith('--reference-csv='):
            reference_csv = arg.split('=', 1)[1]
        elif arg == '--reference-csv':
            continue
        else:
            results_dirs.append(arg)
    # Handle --reference-csv VALUE (two-arg form)
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == '--reference-csv' and i < len(sys.argv) - 1:
            reference_csv = sys.argv[i + 1]
            if reference_csv in results_dirs:
                results_dirs.remove(reference_csv)

    if not results_dirs:
        results_dirs = [os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results')]

    # Auto-detect reference CSV if not specified
    if reference_csv is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(script_dir, '..', 'Analysis', 'InferenceX_DeepSeek-V4-Pro_interactivity.csv'),
            os.path.join(script_dir, 'Analysis', 'InferenceX_DeepSeek-V4-Pro_interactivity.csv'),
        ]
        for c in candidates:
            if os.path.isfile(c):
                reference_csv = c
                break

    reference_data = {}
    if reference_csv:
        reference_data = read_reference_csv(reference_csv)
        if reference_data:
            print(f"Loaded {len(reference_data)} reference configs from {reference_csv}")

    all_configs = {}
    all_metric_units = {}
    first_results_dir = results_dirs[0]
    multi = len(results_dirs) > 1

    for results_dir in results_dirs:
        if not os.path.isdir(results_dir):
            print(f"Warning: {results_dir} not found, skipping", file=sys.stderr)
            continue

        only = None
        scan_dir = results_dir
        basename = os.path.basename(os.path.abspath(results_dir))
        if basename.startswith('results_'):
            suffix = basename[len('results_'):]
            # Check if this is a concurrency-run dir (results_<config>_c<N>)
            conc_match = re.match(r'^(.+)_c(\d+)$', suffix)
            if conc_match:
                # Two levels up: concurrency dir → config dir → results dir
                config_name = conc_match.group(1)
                scan_dir = os.path.dirname(os.path.dirname(os.path.abspath(results_dir)))
                only = {config_name}
            else:
                # One level up: config dir → results dir
                only = {suffix}
                scan_dir = os.path.dirname(os.path.abspath(results_dir))

        configs, metric_units = discover_configs(scan_dir, only=only)
        if not configs:
            print(f"Warning: no configs found in {scan_dir}, skipping", file=sys.stderr)
            continue
        all_metric_units.update(metric_units)
        folder_name = os.path.basename(os.path.abspath(results_dir))
        for cfg, meta in configs.items():
            if multi:
                key = f'{folder_name}/{cfg}'
                meta['label'] = f'[{folder_name}] {meta["label"]}'
            else:
                key = cfg
            meta['_results_dir'] = os.path.abspath(scan_dir)
            all_configs[key] = meta

    if not all_configs:
        print("Error: no configs discovered in any directory", file=sys.stderr)
        sys.exit(1)

    output_dir = os.path.abspath(first_results_dir)
    # If we pointed at a concurrency run subdir, go up to the results root
    output_basename = os.path.basename(output_dir)
    if output_basename.startswith('results_'):
        if re.match(r'^results_.+_c\d+$', output_basename):
            output_dir = os.path.dirname(os.path.dirname(output_dir))
        else:
            output_dir = os.path.dirname(output_dir)
    output_path = os.path.join(output_dir, 'interactivity_vs_throughput.html')

    generate_html(all_configs, output_path, first_results_dir, all_metric_units, reference_data=reference_data)

    print(f"Discovered {len(all_configs)} configs:")
    for cfg, meta in sorted(all_configs.items()):
        concs = sorted(meta['runs'].keys())
        print(f"  {cfg}: {meta['label']} ({meta['decode_gpus']} decode GPUs) — c{',c'.join(str(c) for c in concs)}")
    print(f"\nGenerated: {output_path}")


if __name__ == '__main__':
    main()

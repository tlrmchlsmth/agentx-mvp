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

COLORS = [
    '#ff3333', '#00ccff', '#ffdd00', '#aa44ff', '#00ee77',
    '#ff7700', '#3388ff', '#ff55aa', '#00ffcc', '#dddd00',
    '#cc33ff', '#33ff33', '#ff4477', '#0088ff', '#ffaa00',
    '#77ddff', '#ff0066', '#44ffaa', '#bb88ff', '#88ff00',
]


def read_model_label(results_dir):
    candidates = [Path(results_dir) / 'model_label.txt']
    candidates.extend(sorted(Path(results_dir).glob('results_*/model_label.txt')))
    for path in candidates:
        if path.is_file():
            label = path.read_text().strip()
            if label:
                return label
    return os.environ.get('MODEL_LABEL', 'GLM-5.2-FP8')


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


def find_kv_cache_tokens_in_json(value):
    if isinstance(value, dict):
        found = []
        token_value = tokens_from_cache_config_labels(value)
        if token_value is not None:
            found.append(token_value)
        for child in value.values():
            child_value = find_kv_cache_tokens_in_json(child)
            if child_value is not None:
                found.append(child_value)
        if found:
            return max(found)
    elif isinstance(value, list):
        found = [v for v in (find_kv_cache_tokens_in_json(child) for child in value) if v is not None]
        if found:
            return max(found)
    return None


def read_kv_cache_tokens_from_cache_config(run_dir):
    json_path = os.path.join(run_dir, 'server_metrics_export.json')
    if os.path.isfile(json_path):
        try:
            with open(json_path) as f:
                token_value = find_kv_cache_tokens_in_json(json.load(f))
            if token_value is not None:
                return token_value
        except (OSError, json.JSONDecodeError):
            pass

    csv_path = os.path.join(run_dir, 'server_metrics_export.csv')
    if os.path.isfile(csv_path):
        found = []
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
                        found.append(token_value)
                        current = {}
            if found:
                return max(found)
        except OSError:
            pass
    return None


def read_kv_cache_tokens(run_dir):
    return read_kv_cache_tokens_from_cache_config(run_dir)


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
        if not entry.startswith('results_'):
            continue
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

        config_name = entry[len('results_'):]
        decode_gpus = read_int_file(os.path.join(config_dir, 'decode_gpus.txt'), n_decode_nodes * GPUS_PER_NODE)
        prefill_gpus = read_int_file(os.path.join(config_dir, 'prefill_gpus.txt'), n_prefill_nodes * GPUS_PER_NODE)
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

        def _role_label(replicas, width, letter):
            return f'{replicas} x {letter} (EP {width * GPUS_PER_NODE})'
        label = f'{_role_label(pr, pw, "P")} | {_role_label(dr, dw, "D")}'
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
    model_label = read_model_label(results_dir)
    color_map = {}
    for i, cfg in enumerate(sorted(configs.keys())):
        color_map[cfg] = COLORS[i % len(COLORS)]

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
  .summary th {{ text-align: left; padding: 6px 8px; color: var(--fg3); border-bottom: 1px solid var(--border); font-weight: 600;
                 cursor: pointer; user-select: none; white-space: nowrap; }}
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
  .yaml-section {{ margin-bottom: 8px; }}
  .yaml-toggle {{ background: none; border: 1px solid var(--border); color: var(--fg3); border-radius: 4px;
                   padding: 6px 14px; cursor: pointer; font-size: 12px; font-family: inherit; }}
  .yaml-toggle:hover {{ color: var(--fg); border-color: var(--fg3); }}
  .yaml-block {{ background: var(--bg3); border: 1px solid var(--border); border-radius: 4px; padding: 16px;
                  margin-top: 8px; overflow-x: auto; display: none; }}
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
    <h1 style="margin:0">{model_label} Disaggregated Serving</h1>
    <div class="subtitle" style="margin:0">vLLM {version_str}</div>
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
    margin: {{ t: 44, r: 30, b: 64, l: 76 }},
    xaxis: {{ gridcolor: t.grid, zerolinecolor: t.grid, linecolor: t.grid, gridwidth: 1,
              title: {{ font: {{ size: 13, color: t.fg3 }} }}, tickfont: {{ size: 12, color: t.fg2 }} }},
    yaxis: {{ gridcolor: t.grid, zerolinecolor: t.grid, linecolor: t.grid, gridwidth: 1,
              title: {{ font: {{ size: 13, color: t.fg3 }} }}, tickfont: {{ size: 12, color: t.fg2 }} }},
    legend: {{ bgcolor: t.legend, font: {{ size: 12, color: t.fg }}, bordercolor: t.legendBorder, borderwidth: 1 }},
    hoverlabel: {{ bgcolor: t.hover, bordercolor: t.hoverBorder, font: {{ size: 13, color: t.fg }} }},
    hovermode: 'closest',
  }};
}}
let LAYOUT_DEFAULTS = getLayoutDefaults();

const COLORS = {json.dumps(color_map)};
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
let maxConcurrency = null;

function passesSLO(cfg, c) {{
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
    if (v != null && v / meta.decodeGPUs < sloTokGPU) return false;
  }}
  if (sloTokUser != null) {{
    const v = d.output_token_throughput_per_user?.[sloTokUserStat];
    if (v != null && v < sloTokUser) return false;
  }}
  return true;
}}

function applyNorm(val, norm, meta) {{
  if (val == null) return null;
  if (norm.startsWith('cost') && (val <= 0 || gpuCostPerHour <= 0)) return null;
  if (val === 0) return null;
  if (norm === 'decode') return val / meta.decodeGPUs;
  if (norm === 'prefill') return val / meta.prefillGPUs;
  if (norm === 'total') return val / (meta.decodeGPUs + meta.prefillGPUs);
  if (norm === 'cost_decode') return (meta.decodeGPUs * gpuCostPerHour * 1e6) / (val * 3600);
  if (norm === 'cost_prefill') return (meta.prefillGPUs * gpuCostPerHour * 1e6) / (val * 3600);
  if (norm === 'cost_total') return ((meta.decodeGPUs + meta.prefillGPUs) * gpuCostPerHour * 1e6) / (val * 3600);
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

  const xMetricOpts = metricOptions(X_AXIS_METRICS);
  const yMetricOpts = metricOptions(Y_AXIS_METRICS);

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

  // Y row
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
    if (norm.startsWith('cost')) return `${{name}}${{ss}}${{ns}}`;
    return `${{name}}${{ss}}${{ns}} (${{unit}})`;
  }}

  function buildTraces() {{
    return CONFIG_KEYS.map(cfg => {{
      const meta = CONFIGS[cfg];
      const validConcs = CONCURRENCIES.filter(c => DATA[cfg] && DATA[cfg][c]);
      return {{
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
        text: validConcs.map(c => passesSLO(cfg, c) ? hoverText(cfg, c, DATA[cfg][c]) : ''),
        hoverinfo: 'text',
        mode: 'lines+markers+text',
        textposition: 'top center',
        textfont: {{ size: 11, color: COLORS[cfg], family: 'Inter, -apple-system, sans-serif', weight: 600 }},
        texttemplate: validConcs.map(c => passesSLO(cfg, c) ? `c${{C_LABELS[c]}}` : ''),
        name: `${{meta.label}} (${{meta.decodeGPUs}} decode GPUs)`,
        line: {{ color: COLORS[cfg], width: 1.5 }},
        marker: {{ size: 10, color: COLORS[cfg], symbol: SHAPES[cfg] || 'circle', line: {{ color: 'rgba(0,0,0,0.4)', width: 0.5 }} }},
      }};
    }});
  }}

  function getLayout() {{
    const yAxis = {{ ...LAYOUT_DEFAULTS.yaxis, title: {{ text: axisTitle(state.yMetric, state.yStat, state.yNorm), font: {{ size: 11 }} }} }};
    if (!state.yNorm.startsWith('cost')) yAxis.rangemode = 'tozero';
    return {{
      ...LAYOUT_DEFAULTS,
      title: {{ text: `${{metricLabel(state.yMetric)}} vs ${{metricLabel(state.xMetric)}}`, font: {{ size: 13, color: '#d8d9da' }} }},
      xaxis: {{ ...LAYOUT_DEFAULTS.xaxis, title: {{ text: axisTitle(state.xMetric, state.xStat, state.xNorm), font: {{ size: 11 }} }} }},
      yaxis: yAxis,
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
createChart(chartCol2, {{ xMetric: 'e2e_output_token_throughput', yMetric: 'output_token_throughput', yNorm: 'decode' }});

// ── Controls: cost + SLO filters (in title bar) ──
const sloBar = document.getElementById('controlsBar');

const inputStyle = 'background:var(--input-bg); color:var(--input-fg); border:1px solid var(--input-border); border-radius:4px; padding:6px 10px; font-size:15px; width:80px; font-family:inherit;';

// Cost input
const costWrap = document.createElement('label');
costWrap.style.cssText = 'color:var(--fg3); font-size:15px; display:flex; align-items:center; gap:6px;';
costWrap.textContent = '$/node/hr:';
const costInput = document.createElement('input');
costInput.type = 'number';
costInput.step = '0.01';
costInput.min = '0';
costInput.value = '21.68';
costInput.placeholder = '21.68';
costInput.style.cssText = inputStyle + ' width:90px;';
costInput.addEventListener('input', () => {{
  gpuCostPerHour = (parseFloat(costInput.value) || 0) / {GPUS_PER_NODE};
  allCharts.forEach(ch => {{
    if (ch.state.xNorm.startsWith('cost') || ch.state.yNorm.startsWith('cost')) ch.update();
  }});
}});
gpuCostPerHour = (parseFloat(costInput.value) || 0) / {GPUS_PER_NODE};
costWrap.appendChild(costInput);
sloBar.appendChild(costWrap);
const selStyle = 'background:var(--input-bg); color:var(--input-fg); border:1px solid var(--input-border); border-radius:4px; padding:6px 8px; font-size:15px; font-family:inherit;';

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
ttftWrap.style.cssText = 'color:var(--fg3); font-size:15px; display:flex; align-items:center; gap:6px;';
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
itlWrap.style.cssText = 'color:var(--fg3); font-size:15px; display:flex; align-items:center; gap:6px;';
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
tokWrap.style.cssText = 'color:var(--fg3); font-size:15px; display:flex; align-items:center; gap:6px;';
tokWrap.textContent = 'Min tok/s/decode GPU:';
const tokInput = document.createElement('input');
tokInput.type = 'number'; tokInput.step = '1'; tokInput.min = '0';
tokInput.value = ''; tokInput.placeholder = 'off';
tokInput.style.cssText = inputStyle;
tokWrap.appendChild(tokInput);
sloBar.appendChild(tokWrap);

// Min tok/s/user SLO
const tokUserWrap = document.createElement('label');
tokUserWrap.style.cssText = 'color:var(--fg3); font-size:15px; display:flex; align-items:center; gap:6px;';
tokUserWrap.textContent = 'Min tok/s/user:';
const tokUserStatSel = makeSLOStatSel('avg');
const tokUserInput = document.createElement('input');
tokUserInput.type = 'number'; tokUserInput.step = '1'; tokUserInput.min = '0';
tokUserInput.value = ''; tokUserInput.placeholder = 'off';
tokUserInput.style.cssText = inputStyle;
tokUserWrap.appendChild(tokUserStatSel);
tokUserWrap.appendChild(tokUserInput);
sloBar.appendChild(tokUserWrap);

// Max concurrency filter
const concWrap = document.createElement('label');
concWrap.style.cssText = 'color:var(--fg3); font-size:15px; display:flex; align-items:center; gap:6px;';
concWrap.textContent = 'Max concurrency:';
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
  LAYOUT_DEFAULTS = getLayoutDefaults();
  allCharts.forEach(ch => ch.update());
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
  maxConcurrency = parseFloat(concInput.value) || null;
  allCharts.forEach(ch => ch.update());
  document.querySelectorAll('tr[data-cfg]').forEach(tr => {{
    const cfg = tr.dataset.cfg;
    const c = tr.dataset.conc;
    tr.classList.toggle('slo-fail', !passesSLO(cfg, c));
  }});
}}

[ttftInput, itlInput, tokInput, tokUserInput, concInput].forEach(el => el.addEventListener('input', applySLOFilter));
[ttftStatSel, itlStatSel, tokUserStatSel].forEach(el => el.addEventListener('change', applySLOFilter));

// ── Section 2: Summary table ──
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
    'Config', 'Concurrency', 'Errors', 'Decode GPUs', 'KV Cache Prefill', 'KV Cache Decode', 'Input tok/s', 'Output tok/s', 'tok/s/GPU',
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

      const errCount = d._error_count || 0;

      const vals = [
        meta.label, C_LABELS[c], errCount, meta.decodeGPUs,
        kvPrefill != null ? kvPrefill.toLocaleString() : '-',
        kvDecode != null ? kvDecode.toLocaleString() : '-',
        inp?.avg?.toFixed(1) ?? '-',
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
        if (i === 2 && errCount > 0) {{ td.style.color = '#f44336'; td.style.fontWeight = '600'; }}
        if (i === 8) td.className = 'highlight';
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
        results_dirs = sys.argv[1:]
    else:
        results_dirs = [os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results')]

    all_configs = {}
    all_metric_units = {}
    first_results_dir = results_dirs[0]
    multi = len(results_dirs) > 1

    for results_dir in results_dirs:
        if not os.path.isdir(results_dir):
            print(f"Warning: {results_dir} not found, skipping", file=sys.stderr)
            continue
        configs, metric_units = discover_configs(results_dir)
        if not configs:
            print(f"Warning: no configs found in {results_dir}, skipping", file=sys.stderr)
            continue
        all_metric_units.update(metric_units)
        folder_name = os.path.basename(os.path.abspath(results_dir))
        for cfg, meta in configs.items():
            if multi:
                key = f'{folder_name}/{cfg}'
                meta['label'] = f'[{folder_name}] {meta["label"]}'
            else:
                key = cfg
            meta['_results_dir'] = os.path.abspath(results_dir)
            all_configs[key] = meta

    if not all_configs:
        print("Error: no configs discovered in any directory", file=sys.stderr)
        sys.exit(1)

    output_dir = os.path.dirname(os.path.abspath(first_results_dir))
    output_path = os.path.join(output_dir, 'interactivity_vs_throughput.html')

    generate_html(all_configs, output_path, first_results_dir, all_metric_units)

    print(f"Discovered {len(all_configs)} configs:")
    for cfg, meta in sorted(all_configs.items()):
        concs = sorted(meta['runs'].keys())
        print(f"  {cfg}: {meta['label']} ({meta['decode_gpus']} decode GPUs) — c{',c'.join(str(c) for c in concs)}")
    print(f"\nGenerated: {output_path}")


if __name__ == '__main__':
    main()

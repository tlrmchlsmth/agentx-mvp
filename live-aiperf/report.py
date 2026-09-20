#!/usr/bin/env python3
"""Write portable AIPerf HTML reports from a run directory or result root."""

from __future__ import annotations

import html
import base64
import gzip
import importlib.util
import json
import re
import sys
from urllib.parse import quote
from pathlib import Path
from typing import Any


DISPLAY_METRICS = (
    "request_throughput",
    "output_token_throughput",
    "input_token_throughput",
    "time_to_first_token",
    "time_to_second_token",
    "time_to_first_output_token",
    "inter_token_latency",
    "request_latency",
    "request_error_rate",
)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def metric_value(value: Any) -> tuple[float | None, str]:
    if not isinstance(value, dict):
        return None, ""
    raw = value.get("avg")
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None, str(value.get("unit", ""))
    return number, str(value.get("unit", ""))


def statistic(profile: dict[str, Any], metric: str, stat: str = "avg") -> float | None:
    try:
        return float(profile[metric][stat])
    except (KeyError, TypeError, ValueError):
        return None


def display_stat(profile: dict[str, Any], metric: str, stat: str = "avg") -> str:
    value = statistic(profile, metric, stat)
    return "—" if value is None else number(value)


def per_gpu(value: float | None, gpus: float) -> str:
    return "—" if value is None or gpus <= 0 else number(value / gpus)


def label(metric: str) -> str:
    return metric.replace("_", " ").title()


def number(value: float) -> str:
    """Human-readable benchmark values; never use scientific notation."""
    precision = 4 if abs(value) < 1 else 2
    return f"{value:,.{precision}f}".rstrip("0").rstrip(".")


def display_json(value: Any) -> Any:
    """Format floats for the visible source-data view without exponent notation."""
    if isinstance(value, float):
        return number(value)
    if isinstance(value, dict):
        return {key: display_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [display_json(item) for item in value]
    return value


def vllm_source(metadata: dict[str, Any]) -> str:
    """Link the immutable benchmark branch while retaining its resolved SHA."""
    ref = str(metadata.get("vllm_build_ref", ""))
    commit = str(metadata.get("vllm_build_commit", "unknown"))
    if ref:
        branch = html.escape(ref)
        url = "https://github.com/elvircrn/vllm/tree/" + quote(ref, safe="/")
        return f'<a href="{html.escape(url, quote=True)}">{branch}</a><br><code>{html.escape(commit)}</code>'
    return f"<code>{html.escape(commit)}</code>"


def run_data(directory: Path) -> dict[str, Any] | None:
    profile_path = directory / "profile_export_aiperf.json"
    if not profile_path.is_file():
        return None
    profile = read_json(profile_path)
    metadata = read_json(directory / "benchmark-metadata.json")
    # A sweep has one Kubernetes Job shared by all c<N> result directories.
    # Submission stores a copy in every directory, while report recovery
    # deliberately stores it once at the sweep root.  Accept either layout so
    # a report regenerated from a completed Job never loses the manifest.
    aiperf_job_path = directory / "aiperf-job.yaml"
    if not aiperf_job_path.is_file():
        aiperf_job_path = directory.parent / "aiperf-job.yaml"
    llmd_yaml_path = directory / "llm-d-deployment.yaml"
    if not llmd_yaml_path.is_file():
        llmd_yaml_path = directory.parent / "llm-d-deployment.yaml"
    return {
        "directory": directory,
        "profile": profile,
        "metadata": metadata,
        "yaml": (directory / "serving-pods.yaml").read_text(encoding="utf-8")
        if (directory / "serving-pods.yaml").is_file()
        else "Pod manifest snapshot was not captured.",
        "aiperf_job_yaml": aiperf_job_path.read_text(encoding="utf-8")
        if aiperf_job_path.is_file()
        else "AIPerf Job manifest was not captured.",
        "llmd_yaml": llmd_yaml_path.read_text(encoding="utf-8")
        if llmd_yaml_path.is_file()
        else "Live llm-d deployment snapshot was not captured.",
        "dashboard": (directory / "dashboard.html").read_bytes()
        if (directory / "dashboard.html").is_file() else None,
    }


def document(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>{html.escape(title)}</title>
<style>
*{{box-sizing:border-box}}body{{background:#111217;color:#d8d9da;font:14px Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;padding:16px}}h1{{font-size:22px;margin:0 0 4px}}h2{{font-size:16px;border-bottom:1px solid #2a2a2e;margin:24px 0 8px;padding:10px 4px 6px}}h3{{font-size:14px;margin:14px 0 6px}}
.summary{{background:#181b1f;border:1px solid #2a2a2e;border-radius:4px;overflow-x:auto;padding:0 12px}}table{{border-collapse:collapse;width:100%;margin:8px 0;font-size:12px}}th,td{{border-bottom:1px solid #1e1e22;padding:7px 8px;text-align:left;vertical-align:top;white-space:nowrap}}th{{color:#8e8e8e;font-weight:500;border-bottom:1px solid #2a2a2e}}tbody tr:hover{{background:#1e2127}}
table.sortable th{{cursor:pointer;user-select:none}}table.sortable th::after{{content:" ↕";color:#8e8e8e;font-size:.8em}}.derived{{color:#58a6ff;font-weight:500}}
code,pre{{font-family:"SF Mono",Menlo,Consolas,monospace}}code{{overflow-wrap:anywhere}}pre{{background:#0d1117;border:1px solid #2a2a2e;border-radius:4px;line-height:1.5;overflow:auto;padding:16px;white-space:pre-wrap;overflow-wrap:anywhere}}
details{{background:#181b1f;border:1px solid #2a2a2e;border-radius:4px;margin:8px 0;padding:0 12px}}summary{{cursor:pointer;font-weight:500;padding:10px 0}}details details{{background:#111217;margin:8px 0;padding:0 10px}}.muted{{color:#8e8e8e}}a{{color:#58a6ff}}label{{color:#8e8e8e;margin-right:10px}}select{{background:#181b1f;border:1px solid #3a3a3e;border-radius:4px;color:#d8d9da;font:inherit;padding:4px 6px}}
.chart-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}}.chart-panel{{background:#181b1f;border:1px solid #2a2a2e;border-radius:4px;padding:10px;min-width:0}}.chart-controls{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px}}.chart-controls label{{font-size:12px}}.plot{{height:460px}}@media(max-width:1000px){{.chart-grid{{grid-template-columns:1fr}}}}
</style></head><body>{body}
<script>
for (const table of document.querySelectorAll('table.sortable')) {{
  const headers = [...table.tHead.rows[0].cells];
  const sort = (column, direction) => {{
    const rows = [...table.tBodies[0].rows];
    const value = row => row.cells[column].textContent.trim();
    const numeric = text => {{
      const parsed = Number.parseFloat(text.replaceAll(',', ''));
      return Number.isFinite(parsed) && /^[+-]?[0-9,.]+(?:\\s|$)/.test(text) ? parsed : null;
    }};
    rows.sort((left, right) => {{
      const a = value(left), b = value(right), an = numeric(a), bn = numeric(b);
      if (an !== null && bn !== null) return direction * (an - bn);
      if (an !== null) return -direction;
      if (bn !== null) return direction;
      return direction * a.localeCompare(b, undefined, {{numeric: true, sensitivity: 'base'}});
    }});
    rows.forEach(row => table.tBodies[0].append(row));
  }};
  headers.forEach((header, column) => {{
    let direction = 1;
    header.addEventListener('click', () => {{ sort(column, direction); direction *= -1; }});
  }});
  const concurrency = headers.findIndex(header => header.textContent.trim() === 'Concurrency');
  if (concurrency >= 0) sort(concurrency, 1);
}}
</script></body></html>"""


def metadata_table(metadata: dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><th>{html.escape(str(key))}</th><td><code>{html.escape(str(value))}</code></td></tr>"
        for key, value in sorted(metadata.items())
    )
    return "<table><tbody>" + rows + "</tbody></table>"


def metrics_table(profile: dict[str, Any]) -> str:
    preferred_stats = ("min", "p50", "p90", "p95", "p99", "max", "avg", "std")
    available_stats = {
        stat
        for value in profile.values()
        if isinstance(value, dict)
        for stat, candidate in value.items()
        if stat != "unit" and isinstance(candidate, (int, float)) and not isinstance(candidate, bool)
    }
    stats = [stat for stat in preferred_stats if stat in available_stats]
    stats.extend(sorted(available_stats - set(stats)))
    rows = []
    seen = set()
    for metric in DISPLAY_METRICS + tuple(sorted(profile)):
        if metric in seen or metric not in profile:
            continue
        seen.add(metric)
        entry = profile[metric]
        if not isinstance(entry, dict):
            continue
        value, unit = metric_value(entry)
        if value is None and not any(stat in entry for stat in stats):
            continue
        statistic_cells = []
        for stat in stats:
            raw = entry.get(stat)
            try:
                statistic_cells.append(f"<td>{number(float(raw))}</td>")
            except (TypeError, ValueError):
                statistic_cells.append("<td>—</td>")
        rows.append(
            f"<tr><th>{html.escape(label(metric))}</th><td>{html.escape(unit)}</td>"
            + "".join(statistic_cells)
            + "</tr>"
        )
    header = "".join(f"<th>{html.escape(stat)}</th>" for stat in stats)
    return "<table class=\"sortable\"><thead><tr><th>Metric</th><th>Unit</th>" + header + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def write_run(directory: Path) -> None:
    data = run_data(directory)
    if data is None:
        raise SystemExit(f"No profile_export_aiperf.json in {directory}")
    metadata = data["metadata"]
    title = f"AIPerf — {metadata.get('run_id', directory.name)}"
    body = (
        f"<h1>{html.escape(title)}</h1><h2>Reproducibility</h2>{metadata_table(metadata)}"
        f"<h2>Metrics</h2>{metrics_table(data['profile'])}"
        f"<details><summary>AIPerf Job manifest</summary><pre>{html.escape(data['aiperf_job_yaml'])}</pre></details>"
        f"<details><summary>Serving pod YAML snapshot</summary><pre>{html.escape(data['yaml'])}</pre></details>"
        f"<details><summary>Live llm-d deployment and router YAML</summary><pre>{html.escape(data['llmd_yaml'])}</pre></details>"
        f"<details><summary>AIPerf metrics source values</summary><pre>{html.escape(json.dumps(display_json(data['profile']), indent=2, sort_keys=True))}</pre></details>"
    )
    (directory / "report.html").write_text(document(title, body), encoding="utf-8")


def metric_picker(runs: list[dict[str, Any]]) -> str:
    """Interactive, dependency-free sweep chart modelled on the v2 picker."""
    points = []
    for data in runs:
        metadata = data["metadata"]
        try:
            concurrency = int(metadata.get("concurrency"))
        except (TypeError, ValueError):
            continue
        series = series_key(data)
        points.append(
            {
                "series": series,
                "concurrency": concurrency,
                "metrics": data["profile"],
            }
        )
    encoded = json.dumps(points, separators=(",", ":")).replace("</", "<\\/")
    return """<h2>Metric explorer</h2>
<p class="muted">Choose any AIPerf metric and statistic. Toggle sweep lines to compare only what matters.</p>
<p><label>Metric <select id="metric-picker"></select></label> <label>Statistic <select id="stat-picker"></select></label></p>
<fieldset id="series-picker"><legend>Sweeps</legend></fieldset>
<svg id="metric-chart" viewBox="0 0 820 390" role="img" aria-label="Selected AIPerf metric by concurrency"></svg>
<script>
(() => {
  const points = """ + encoded + """;
  const metric = document.getElementById('metric-picker');
  const stat = document.getElementById('stat-picker');
  const seriesBox = document.getElementById('series-picker');
  const svg = document.getElementById('metric-chart');
  const metrics = [...new Set(points.flatMap(p => Object.entries(p.metrics)
    .filter(([, v]) => v && typeof v === 'object' && Object.keys(v).some(k => typeof v[k] === 'number'))
    .map(([k]) => k)))].sort();
  const esc = text => String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;');
  const pretty = key => key.replaceAll('_', ' ').replace(/\\b\\w/g, c => c.toUpperCase());
  metrics.forEach(key => metric.add(new Option(pretty(key), key)));
  metric.value = metrics.includes('request_throughput') ? 'request_throughput' : metrics[0];
  const series = [...new Set(points.map(p => p.series))];
  series.forEach((name, index) => {
    const label = document.createElement('label');
    const box = document.createElement('input'); box.type = 'checkbox'; box.checked = true; box.value = name;
    box.addEventListener('change', draw); label.append(box, ' ' + name); seriesBox.append(label, document.createElement('br'));
  });
  function updateStats() {
    const values = points.map(p => p.metrics[metric.value]).filter(Boolean);
    const stats = [...new Set(values.flatMap(v => Object.keys(v).filter(k => typeof v[k] === 'number')))].sort();
    stat.replaceChildren(); stats.forEach(key => stat.add(new Option(key, key)));
    stat.value = stats.includes('avg') ? 'avg' : stats[0]; draw();
  }
  function draw() {
    const selected = new Set([...seriesBox.querySelectorAll('input:checked')].map(box => box.value));
    const values = points.filter(p => selected.has(p.series)).map(p => ({...p, value: p.metrics[metric.value]?.[stat.value]}))
      .filter(p => Number.isFinite(p.value));
    const width = 820, height = 390, left = 76, right = 20, top = 28, bottom = 62;
    const xs = values.map(p => p.concurrency), ys = values.map(p => p.value);
    if (!values.length) { svg.innerHTML = '<text x="20" y="40">No values for this selection.</text>'; return; }
    const xmin = Math.min(...xs), xmax = Math.max(...xs), ymin = Math.min(0, ...ys), ymax = Math.max(...ys);
    const x = v => xmin === xmax ? (left + width - right) / 2 : left + (v - xmin) / (xmax - xmin) * (width - left - right);
    const y = v => ymax === ymin ? (top + height - bottom) / 2 : height - bottom - (v - ymin) / (ymax - ymin) * (height - top - bottom);
    const colors = ['#2563eb','#dc2626','#059669','#9333ea','#c2410c','#0891b2'];
    let output = `<line x1="${left}" y1="${height-bottom}" x2="${width-right}" y2="${height-bottom}" stroke="#64748b"/><line x1="${left}" y1="${top}" x2="${left}" y2="${height-bottom}" stroke="#64748b"/>`;
    [...new Set(xs)].sort((a,b)=>a-b).forEach(v => { output += `<text x="${x(v)}" y="${height-38}" text-anchor="middle">${v}</text>`; });
    [ymin, (ymin+ymax)/2, ymax].forEach(v => { output += `<text x="${left-8}" y="${y(v)+4}" text-anchor="end">${v.toLocaleString(undefined,{maximumFractionDigits:4})}</text>`; });
    series.forEach((name, index) => {
      const line = values.filter(p => p.series === name).sort((a,b)=>a.concurrency-b.concurrency); if (!line.length) return;
      const color = colors[index % colors.length]; output += `<polyline fill="none" stroke="${color}" stroke-width="2" points="${line.map(p=>`${x(p.concurrency)},${y(p.value)}`).join(' ')}"/>`;
      line.forEach(p => { output += `<circle cx="${x(p.concurrency)}" cy="${y(p.value)}" r="4" fill="${color}"><title>${esc(name)} · c${p.concurrency}: ${p.value.toLocaleString()}</title></circle>`; });
    });
    output += `<text x="${(left+width-right)/2}" y="${height-10}" text-anchor="middle">Concurrency</text><text x="${left}" y="16">${esc(pretty(metric.value))} (${esc(stat.value)})</text>`;
    svg.innerHTML = output;
  }
  metric.addEventListener('change', updateStats); stat.addEventListener('change', draw); updateStats();
})();
</script>"""


def embedded_plotly() -> str:
    """Read the pinned local bundle, avoiding a network dependency in the HTML."""
    bundle = Path(__file__).with_name("plotly-basic-2.35.2.min.js.gz")
    try:
        source = gzip.decompress(bundle.read_bytes()).decode("utf-8")
    except (OSError, EOFError, UnicodeDecodeError) as exc:
        raise SystemExit(f"Missing Plotly bundle: {bundle}") from exc
    return source.replace("</script", "<\\/script")


def snapshot_gpu_counts(snapshot: str) -> dict[str, float]:
    """Recover declared GPU capacity for older runs that predate metadata."""
    counts = {"prefill_gpus": 0.0, "decode_gpus": 0.0, "total_gpus": 0.0}
    for pod in re.split(r"(?m)^-\s+apiVersion:", snapshot):
        # A `kubectl get pods -o yaml` snapshot repeats GPU quantities in pod
        # status (allocatedResources, resource IDs) and in requests. Capacity
        # is the declared container *limit* only.
        spec = re.split(r"(?m)^\s*status:\s*$", pod, maxsplit=1)[0]
        values = re.findall(
            r"(?m)^\s*limits:\s*\n(?:(?!^\s*requests:\s*$)[^\n]*\n)*?^\s*nvidia\.com/gpu:\s*[\"']?([0-9]+(?:\.[0-9]+)?)",
            spec,
        )
        gpus = sum(float(value) for value in values)
        counts["total_gpus"] += gpus
        if re.search(r"llm-d\.ai/role:\s*[\"']?prefill", pod):
            counts["prefill_gpus"] += gpus
        elif re.search(r"llm-d\.ai/role:\s*[\"']?decode", pod):
            counts["decode_gpus"] += gpus
    return counts


def gpu_counts(data: dict[str, Any]) -> dict[str, float]:
    """Use explicit run metadata, with a YAML-snapshot fallback for old runs."""
    recovered = snapshot_gpu_counts(data["yaml"])
    metadata = data["metadata"]
    result = {}
    for name, metadata_name in (
        ("prefill_gpus", "prefill_gpu_count"),
        ("decode_gpus", "decode_gpu_count"),
        ("total_gpus", "total_gpu_count"),
    ):
        try:
            result[name] = float(metadata.get(metadata_name) or recovered[name])
        except (TypeError, ValueError):
            result[name] = recovered[name]
    return result


def plotly_metric_picker(runs: list[dict[str, Any]]) -> str:
    """The v2 Plotly interaction model, embedded for an offline report."""
    points = []
    for data in runs:
        try:
            concurrency = int(data["metadata"].get("concurrency"))
        except (TypeError, ValueError):
            continue
        counts = gpu_counts(data)
        points.append({
            "series": series_key(data),
            "concurrency": concurrency,
            "metrics": data["profile"],
            "prefill_gpus": counts["prefill_gpus"],
            "decode_gpus": counts["decode_gpus"],
            "total_gpus": counts["total_gpus"],
        })
    data_json = json.dumps(points, separators=(",", ":")).replace("</", "<\\/")
    return """<h2>Metric explorer</h2>
<p class="muted">Choose the x and y measurements independently. Click a legend entry to show or hide a sweep.</p>
<p><label>X metric <select id="x-metric-picker"></select></label> <label>statistic <select id="x-stat-picker"></select></label> <label>normalize <select id="x-normalize-picker"></select></label></p>
<p><label>Y metric <select id="y-metric-picker"></select></label> <label>statistic <select id="y-stat-picker"></select></label> <label>normalize <select id="y-normalize-picker"></select></label></p>
<div id="metric-chart" style="height:560px"></div>
<script>""" + embedded_plotly() + """</script>
<script>
(() => {
  const points = """ + data_json + """;
  const xMetric = document.getElementById('x-metric-picker');
  const xStat = document.getElementById('x-stat-picker');
  const xNormalize = document.getElementById('x-normalize-picker');
  const yMetric = document.getElementById('y-metric-picker');
  const yStat = document.getElementById('y-stat-picker');
  const yNormalize = document.getElementById('y-normalize-picker');
  const chart = document.getElementById('metric-chart');
  const metrics = [...new Set(points.flatMap(p => Object.entries(p.metrics)
    .filter(([, v]) => v && typeof v === 'object' && Object.keys(v).some(k => typeof v[k] === 'number'))
    .map(([k]) => k)))].sort();
  // Wall-clock timestamps do not compare benchmark performance and force
  // unreadably large axes. Keep them in raw artifacts, not the chart picker.
  const chartMetrics = metrics.filter(key => !/(^|_)timestamp(_|$)/.test(key));
  if (!chartMetrics.length) return;
  const pretty = key => key.replaceAll('_', ' ').replace(/\\b\\w/g, c => c.toUpperCase());
  const unit = key => points.map(p => p.metrics[key]?.unit).find(Boolean) || '';
  const optionText = key => unit(key) ? `${pretty(key)} [${unit(key)}]` : pretty(key);
  const fillMetrics = select => chartMetrics.forEach(key => select.add(new Option(optionText(key), key)));
  fillMetrics(xMetric); fillMetrics(yMetric);
  xMetric.value = chartMetrics.includes('inter_token_latency') ? 'inter_token_latency' : chartMetrics[0];
  yMetric.value = chartMetrics.includes('request_throughput') ? 'request_throughput' : chartMetrics[0];
  const normalizationOptions = [['none', 'none']];
  if (points.some(p => Number(p.total_gpus) > 0)) normalizationOptions.push(['total_gpus', '/ total serving GPUs']);
  if (points.some(p => Number(p.prefill_gpus) > 0)) normalizationOptions.push(['prefill_gpus', '/ prefill GPUs']);
  if (points.some(p => Number(p.decode_gpus) > 0)) normalizationOptions.push(['decode_gpus', '/ decode GPUs']);
  [xNormalize, yNormalize].forEach(select => normalizationOptions.forEach(([value, text]) => select.add(new Option(text, value))));
  const series = [...new Set(points.map(p => p.series))];
  function setStats(metric, stat) {
    const stats = [...new Set(points.flatMap(p => Object.entries(p.metrics[metric.value] || {})
      .filter(([, value]) => typeof value === 'number').map(([key]) => key)))].sort();
    stat.replaceChildren(); stats.forEach(key => stat.add(new Option(key, key)));
    stat.value = stats.includes('avg') ? 'avg' : stats[0];
  }
  function draw() {
    const normalize = (point, value, option) => {
      const divisor = option === 'none' ? 1 : Number(point[option]);
      return divisor > 0 ? value / divisor : null;
    };
    const normalizationLabel = option => normalizationOptions.find(([value]) => value === option)?.[1] || 'none';
    const tickFormat = values => {
      const largest = Math.max(...values.map(value => Math.abs(value)));
      if (largest < 0.01) return ',.6f';
      if (largest < 1) return ',.4f';
      return ',.2f';
    };
    const traces = series.map(name => {
      const values = points.filter(p => p.series === name)
        .map(p => ({...p,
          x: normalize(p, p.metrics[xMetric.value]?.[xStat.value], xNormalize.value),
          y: normalize(p, p.metrics[yMetric.value]?.[yStat.value], yNormalize.value)}))
        .filter(p => Number.isFinite(p.x) && Number.isFinite(p.y))
        .sort((a, b) => a.concurrency - b.concurrency);
      return {x: values.map(p => p.x), y: values.map(p => p.y), mode: 'lines+markers+text',
        text: values.map(p => `c${p.concurrency}`), textposition: 'top center', name,
        customdata: values.map(p => [p.concurrency]),
        hovertemplate: '%{fullData.name}<br>concurrency %{customdata[0]}<br>x %{x:,.6f}<br>y %{y:,.6f}<extra></extra>'};
    });
    const xValues = traces.flatMap(trace => trace.x);
    const yValues = traces.flatMap(trace => trace.y);
    Plotly.react(chart, traces, {paper_bgcolor: '#181b1f', plot_bgcolor: '#181b1f',
      font: {color: '#d8d9da'}, margin: {t: 35, r: 25, b: 75, l: 85}, hovermode: 'closest',
      xaxis: {title: `${pretty(xMetric.value)} (${xStat.value}, ${normalizationLabel(xNormalize.value)})${unit(xMetric.value) ? ` [${unit(xMetric.value)}]` : ''}`, zeroline: false, tickformat: tickFormat(xValues), separatethousands: true, gridcolor: '#2a2a2e', linecolor: '#3a3a3e'},
      yaxis: {title: `${pretty(yMetric.value)} (${yStat.value}, ${normalizationLabel(yNormalize.value)})${unit(yMetric.value) ? ` [${unit(yMetric.value)}]` : ''}`, zeroline: false, tickformat: tickFormat(yValues), separatethousands: true, gridcolor: '#2a2a2e', linecolor: '#3a3a3e'},
      legend: {orientation: 'h', bgcolor: 'rgba(0,0,0,0)'},
      hoverlabel: {bgcolor: '#23262b', bordercolor: '#3a3a3e', font: {color: '#ffffff'}}}, {responsive: true});
  }
  xMetric.addEventListener('change', () => { setStats(xMetric, xStat); draw(); });
  yMetric.addEventListener('change', () => { setStats(yMetric, yStat); draw(); });
  xStat.addEventListener('change', draw); yStat.addEventListener('change', draw);
  xNormalize.addEventListener('change', draw); yNormalize.addEventListener('change', draw);
  setStats(xMetric, xStat); setStats(yMetric, yStat); draw();
})();
</script>"""


def multiple_plotly_metric_picker(runs: list[dict[str, Any]]) -> str:
    """Two v2-style interactive charts with sensible benchmark defaults."""
    points = []
    for data in runs:
        try:
            concurrency = int(data["metadata"].get("concurrency"))
        except (TypeError, ValueError):
            continue
        counts = gpu_counts(data)
        points.append({
            "series": series_key(data), "concurrency": concurrency,
            "metrics": data["profile"], **counts,
        })
    data_json = json.dumps(points, separators=(",", ":")).replace("</", "<\\/")
    return """<h2>Metric explorer</h2>
<p class="muted">Two useful defaults. Change either axis, statistic, or GPU normalization independently; click a legend entry to hide a sweep.</p>
<div class="chart-grid"><div class="chart-panel" id="chart-throughput"></div><div class="chart-panel" id="chart-latency"></div></div>
<script>""" + embedded_plotly() + """</script>
<script>
(() => {
  const points = """ + data_json + """;
  const metrics = [...new Set(points.flatMap(p => Object.entries(p.metrics)
    .filter(([, value]) => value && typeof value === 'object' && Object.keys(value).some(key => typeof value[key] === 'number'))
    .map(([key]) => key)))].filter(key => !/(^|_)timestamp(_|$)/.test(key)).sort();
  if (!metrics.length) return;
  const series = [...new Set(points.map(p => p.series))];
  const pretty = key => key.replaceAll('_', ' ').replace(/\\b\\w/g, letter => letter.toUpperCase());
  const unit = key => points.map(p => p.metrics[key]?.unit).find(Boolean) || '';
  const capacityLabel = (key, label) => {
    const values = [...new Set(points.map(point => Number(point[key])).filter(value => value > 0))].sort((a,b) => a-b);
    const count = values.map(value => value.toLocaleString()).join(', ');
    return values.length === 1 ? `${label} (${count})` : `${label} (varies: ${count})`;
  };
  const norms = [['none', 'none']];
  if (points.some(p => Number(p.total_gpus) > 0)) norms.push(['total_gpus', capacityLabel('total_gpus', '/ total serving GPUs')]);
  if (points.some(p => Number(p.prefill_gpus) > 0)) norms.push(['prefill_gpus', capacityLabel('prefill_gpus', '/ prefill GPUs')]);
  if (points.some(p => Number(p.decode_gpus) > 0)) norms.push(['decode_gpus', capacityLabel('decode_gpus', '/ decode GPUs')]);
  const pick = (wanted, fallback) => metrics.includes(wanted) ? wanted : fallback;
  const addOptions = (select, options) => options.forEach(([value, text]) => select.add(new Option(text, value)));
  const statsFor = key => [...new Set(points.flatMap(point => Object.entries(point.metrics[key] || {})
    .filter(([, value]) => typeof value === 'number').map(([stat]) => stat)))].sort();
  const chooseStats = (select, metric, wanted) => {
    const stats = statsFor(metric.value); select.replaceChildren();
    stats.forEach(stat => select.add(new Option(stat, stat)));
    select.value = stats.includes(wanted) ? wanted : (stats.includes('avg') ? 'avg' : stats[0]);
  };
  function createChart(id, defaults) {
    const panel = document.getElementById(id);
    const controls = document.createElement('div'); controls.className = 'chart-controls';
    const plot = document.createElement('div'); plot.className = 'plot'; panel.append(controls, plot);
    const control = (text, select) => { const label = document.createElement('label'); label.append(text + ' ', select); controls.append(label); };
    const xMetric = document.createElement('select'), xStat = document.createElement('select'), xNorm = document.createElement('select');
    const yMetric = document.createElement('select'), yStat = document.createElement('select'), yNorm = document.createElement('select');
    addOptions(xMetric, metrics.map(key => [key, unit(key) ? `${pretty(key)} [${unit(key)}]` : pretty(key)]));
    addOptions(yMetric, metrics.map(key => [key, unit(key) ? `${pretty(key)} [${unit(key)}]` : pretty(key)]));
    addOptions(xNorm, norms); addOptions(yNorm, norms);
    xMetric.value = pick(defaults.xMetric, metrics[0]); yMetric.value = pick(defaults.yMetric, metrics[0]);
    xNorm.value = norms.some(([value]) => value === defaults.xNorm) ? defaults.xNorm : 'none';
    yNorm.value = norms.some(([value]) => value === defaults.yNorm) ? defaults.yNorm : 'none';
    chooseStats(xStat, xMetric, defaults.xStat); chooseStats(yStat, yMetric, defaults.yStat);
    control('X', xMetric); control('stat', xStat); control('normalize', xNorm);
    control('Y', yMetric); control('stat', yStat); control('normalize', yNorm);
    const labelForNorm = value => norms.find(([key]) => key === value)?.[1] || 'none';
    const tickFormat = values => { const maximum = Math.max(...values.map(value => Math.abs(value))); return maximum < .01 ? ',.6f' : maximum < 1 ? ',.4f' : ',.2f'; };
    function draw() {
      const normalize = (point, value, mode) => { const divisor = mode === 'none' ? 1 : Number(point[mode]); return divisor > 0 ? value / divisor : null; };
      const traces = series.map(name => {
        const values = points.filter(point => point.series === name).map(point => ({...point,
          x: normalize(point, point.metrics[xMetric.value]?.[xStat.value], xNorm.value),
          y: normalize(point, point.metrics[yMetric.value]?.[yStat.value], yNorm.value)}))
          .filter(point => Number.isFinite(point.x) && Number.isFinite(point.y)).sort((a,b) => a.concurrency - b.concurrency);
        return {x: values.map(point => point.x), y: values.map(point => point.y), mode: 'lines+markers+text', name,
          text: values.map(point => `c${point.concurrency}`), textposition: 'top center', customdata: values.map(point => [point.concurrency]),
          hovertemplate: '%{fullData.name}<br>concurrency %{customdata[0]}<br>x %{x:,.6f}<br>y %{y:,.6f}<extra></extra>'};
      });
      const xValues = traces.flatMap(trace => trace.x), yValues = traces.flatMap(trace => trace.y);
      Plotly.react(plot, traces, {paper_bgcolor:'#181b1f', plot_bgcolor:'#181b1f', font:{color:'#d8d9da'}, hovermode:'closest',
        margin:{t:30,r:20,b:75,l:85}, legend:{orientation:'h', bgcolor:'rgba(0,0,0,0)'}, hoverlabel:{bgcolor:'#23262b',bordercolor:'#3a3a3e',font:{color:'#fff'}},
        xaxis:{title:`${pretty(xMetric.value)} (${xStat.value}, ${labelForNorm(xNorm.value)})${unit(xMetric.value) ? ` [${unit(xMetric.value)}]` : ''}`, tickformat:tickFormat(xValues), separatethousands:true, gridcolor:'#2a2a2e',linecolor:'#3a3a3e',zeroline:false},
        yaxis:{title:`${pretty(yMetric.value)} (${yStat.value}, ${labelForNorm(yNorm.value)})${unit(yMetric.value) ? ` [${unit(yMetric.value)}]` : ''}`, tickformat:tickFormat(yValues), separatethousands:true, gridcolor:'#2a2a2e',linecolor:'#3a3a3e',zeroline:false}}, {responsive:true});
    }
    xMetric.addEventListener('change', () => { chooseStats(xStat, xMetric, 'avg'); draw(); });
    yMetric.addEventListener('change', () => { chooseStats(yStat, yMetric, 'avg'); draw(); });
    [xStat, yStat, xNorm, yNorm].forEach(select => select.addEventListener('change', draw)); draw();
  }
  createChart('chart-throughput', {xMetric:'e2e_output_token_throughput', xStat:'avg', xNorm:'none', yMetric:'output_token_throughput', yStat:'avg', yNorm:'decode_gpus'});
  createChart('chart-latency', {xMetric:'inter_token_latency', xStat:'p99', xNorm:'none', yMetric:'output_token_throughput', yStat:'avg', yNorm:'decode_gpus'});
})();
</script>"""


def sweep_key(data: dict[str, Any]) -> str:
    run_id = str(data["metadata"].get("run_id", data["directory"].name))
    return run_id.rsplit("-c", 1)[0]


def repeat_number(data: dict[str, Any]) -> int | None:
    """Return the rerun number when a sweep contains repeated concurrency values."""
    metadata = data["metadata"]
    try:
        repeat_count = int(metadata.get("repeat_count", 1))
        repeat_index = int(metadata.get("repeat_index", 1))
    except (TypeError, ValueError):
        return None
    return repeat_index if repeat_count > 1 and repeat_index > 0 else None


def series_key(data: dict[str, Any]) -> str:
    """Keep each repeated sample as a distinct chart series."""
    base = sweep_key(data)
    repeat = repeat_number(data)
    return f"{base}-r{repeat}" if repeat is not None else base


def run_label(data: dict[str, Any]) -> str:
    """Human-readable concurrency label, including a rerun number when present."""
    concurrency = str(data["metadata"].get("concurrency", "unknown"))
    repeat = repeat_number(data)
    return f"c{concurrency}-r{repeat}" if repeat is not None else f"c{concurrency}"


def sweep_source_details(runs: list[dict[str, Any]]) -> str:
    """Render one shared source bundle per sweep, rather than per concurrency."""
    sweeps: dict[str, list[dict[str, Any]]] = {}
    for data in runs:
        sweeps.setdefault(sweep_key(data), []).append(data)
    sections = []
    for name, members in sorted(sweeps.items()):
        members.sort(key=lambda data: int(data["metadata"].get("concurrency", 0)))
        first = members[0]
        concurrencies = ", ".join(run_label(data) for data in members)
        shared_metadata = {
            key: value
            for key, value in first["metadata"].items()
            if key not in {"run_id", "concurrency", "repeat_index", "repeat_count"}
        }
        metric_files = "".join(
            f"<details><summary>{html.escape(run_label(data))} — profile_export_aiperf.json</summary>"
            f"{metrics_table(data['profile'])}"
            f"<details><summary>source values</summary><pre>{html.escape(json.dumps(display_json(data['profile']), indent=2, sort_keys=True))}</pre></details>"
            "</details>"
            for data in members
        )
        sections.append(
            f"<details><summary>{html.escape(name)} — {html.escape(concurrencies)}</summary>"
            f"<details><summary>benchmark-metadata.json — shared run settings</summary>{metadata_table(shared_metadata)}</details>"
            f"<details><summary>aiperf-job.yaml — full AIPerf Job YAML</summary><pre>{html.escape(first['aiperf_job_yaml'])}</pre></details>"
            f"<details><summary>serving-pods.yaml — shared deployment snapshot</summary><pre>{html.escape(first['yaml'])}</pre></details>"
            f"<details><summary>llm-d-deployment.yaml — router and deployed llm-d resources</summary><pre>{html.escape(first['llmd_yaml'])}</pre></details>"
            f"<details><summary>profile_export_aiperf.json — per-concurrency metrics</summary>{metric_files}</details>"
            "</details>"
        )
    return "".join(sections)


def write_index_legacy(root: Path) -> None:
    # Historical directories on the shared PVC predate source tracking.
    # Do not mix them into a commit comparison as an ambiguous "unknown" run.
    runs = [
        data
        for path in sorted(root.rglob("profile_export_aiperf.json"))
        if (data := run_data(path.parent))
        and data["metadata"].get("vllm_build_commit")
    ]
    rows = []
    for data in runs:
        metadata = data["metadata"]
        profile = data["profile"]
        counts = gpu_counts(data)
        gpu_cells = [
            "—" if counts[key] == 0 else number(counts[key])
            for key in ("prefill_gpus", "decode_gpus", "total_gpus")
        ]
        output = statistic(profile, "output_token_throughput")
        input_tokens = statistic(profile, "input_token_throughput")
        total_tokens = statistic(profile, "total_token_throughput")
        total_display = display_stat(profile, "total_token_throughput")
        if total_tokens is None and output is not None and input_tokens is not None:
            total_display = number(output + input_tokens)
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(metadata.get('run_id', data['directory'].name)))}</code></td>"
            f"<td>{vllm_source(metadata)}</td>"
            f"<td>{html.escape(str(metadata.get('concurrency', 'unknown')))}</td>"
            + "".join(f"<td>{value}</td>" for value in gpu_cells)
            + f"<td>{display_stat(profile, 'request_throughput')}</td>"
            + f"<td>{display_stat(profile, 'output_token_throughput')}</td>"
            + f"<td>{display_stat(profile, 'input_token_throughput')}</td>"
            + f"<td>{total_display}</td>"
            + f"<td class=\"derived\">{per_gpu(output, counts['decode_gpus'])}</td>"
            + f"<td class=\"derived\">{per_gpu(input_tokens, counts['prefill_gpus'])}</td>"
            + f"<td>{display_stat(profile, 'inter_token_latency', 'p50')}</td>"
            + f"<td>{display_stat(profile, 'inter_token_latency', 'p99')}</td>"
            + f"<td>{display_stat(profile, 'time_to_first_token', 'p50')}</td>"
            + f"<td>{display_stat(profile, 'time_to_first_token', 'p99')}</td>"
            + "</tr>"
        )
    body = "<h1>AIPerf benchmark history</h1><p class=\"muted\">Portable summary of completed runs with the pushed vLLM branch and resolved commit.</p>"
    body += "<h2>Data summary</h2><div class=\"summary\"><table class=\"sortable\"><thead><tr><th>Run</th><th>vLLM branch</th><th>Concurrency</th><th>Prefill GPUs</th><th>Decode GPUs</th><th>Total GPUs</th><th>Requests/s</th><th>Output tok/s</th><th>Input tok/s</th><th>Total tok/s</th><th>Output tok/s/decode GPU</th><th>Input tok/s/prefill GPU</th><th>ITL p50 (ms)</th><th>ITL p99 (ms)</th><th>TTFT p50 (ms)</th><th>TTFT p99 (ms)</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    body += multiple_plotly_metric_picker(runs)
    body += "<h2>Source and run details</h2>" + sweep_source_details(runs)
    (root / "index.html").write_text(document("AIPerf benchmark history", body), encoding="utf-8")


def write_index(root: Path) -> None:
    """Adapt live artifacts to the repository's established v2 renderer."""
    renderer_path = Path(__file__).with_name("gen_interactivity_chart.py")
    module_spec = importlib.util.spec_from_file_location("agentx_v2_charts", renderer_path)
    if module_spec is None or module_spec.loader is None:
        raise SystemExit(f"Missing shared chart renderer: {renderer_path}")
    renderer = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(renderer)

    runs = [
        data for path in sorted(root.rglob("profile_export_aiperf.json"))
        if (data := run_data(path.parent)) and data["metadata"].get("vllm_build_commit")
    ]
    if not runs:
        raise SystemExit(f"No completed live AIPerf runs in {root}")

    overlay_html = monitoring_overlay(root, runs)

    configs: dict[str, dict[str, Any]] = {}
    metric_units: dict[str, str] = {}
    for data in runs:
        metadata = data["metadata"]
        config_name = series_key(data)
        counts = gpu_counts(data)
        repeat = repeat_number(data)
        source_label = str(metadata.get("vllm_build_ref", config_name))
        if repeat is not None:
            source_label += f" — rerun {repeat}"
        config = configs.setdefault(config_name, {
            "label": source_label,
            "decode_gpus": counts["decode_gpus"],
            "prefill_gpus": counts["prefill_gpus"],
            "pods": str(metadata.get("topology", "live deployment")),
            "runs": {},
            "version": str(metadata.get("vllm_build_commit", "")),
            "yamls": {
                "aiperf-job.yaml": data["aiperf_job_yaml"],
                "serving-pods.yaml": data["yaml"],
                "llm-d-deployment.yaml": data["llmd_yaml"],
            },
        })
        try:
            concurrency = int(metadata["concurrency"])
        except (KeyError, TypeError, ValueError):
            continue
        profile = {
            key: value for key, value in data["profile"].items()
            if isinstance(value, dict) and any(isinstance(item, (int, float)) for item in value.values())
        }
        config["runs"][concurrency] = profile
        if data["dashboard"] is not None:
            config.setdefault("dashboards", {})[f"c{concurrency}"] = base64.b64encode(data["dashboard"]).decode("ascii")
        for key, value in profile.items():
            metric_units.setdefault(key, str(value.get("unit", "")))

    first_metadata = runs[0]["metadata"]
    output = root / "index.html"
    renderer.generate_html(
        configs, str(output), str(root), metric_units,
        model_label=str(first_metadata.get("model_label", "Live llm-d")),
        chart_defaults={
            "throughput": {"xMetric": "e2e_output_token_throughput", "yMetric": "output_token_throughput", "yNorm": "decode"},
            "latency": {"xMetric": "e2e_output_token_throughput", "yMetric": "input_token_throughput", "yNorm": "prefill"},
        },
    )
    page = output.read_text(encoding="utf-8")
    page = page.replace(
        '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>',
        "<script>" + embedded_plotly() + "</script>",
        1,
    )
    source_rows = "".join(
        f'<p><a href="https://github.com/elvircrn/vllm/tree/{quote(str(data["metadata"].get("vllm_build_ref", "")), safe="/")}">{html.escape(str(data["metadata"].get("vllm_build_ref", "unknown")))}</a> '
        f'<code>{html.escape(str(data["metadata"].get("vllm_build_commit", "unknown")))}</code> '
        f'— {number(gpu_counts(data)["prefill_gpus"])} prefill GPUs, {number(gpu_counts(data)["decode_gpus"])} decode GPUs</p>'
        for data in sorted({sweep_key(data): data for data in runs}.values(), key=sweep_key)
    )
    source = '<div class="subtitle">' + source_rows + '</div>'
    overlay_control = ""
    if overlay_html:
        encoded_overlay = base64.b64encode(overlay_html).decode("ascii")
        overlay_control = (
            '<p><button id="monitoring-overlay">Overlay monitoring across concurrencies</button></p>'
            '<script>document.getElementById("monitoring-overlay").addEventListener("click",()=>{'
            f'const b=atob("{encoded_overlay}");const a=new Uint8Array(b.length);'
            'for(let i=0;i<b.length;i++)a[i]=b.charCodeAt(i);'
            'window.open(URL.createObjectURL(new Blob([a],{type:"text/html"})),"_blank");});</script>'
        )
    page = page.replace('<div id="root"></div>', source + overlay_control + '<div id="root"></div>', 1)
    output.write_text(page, encoding="utf-8")


def monitoring_overlay(root: Path, runs: list[dict[str, Any]]) -> bytes | None:
    """Build one relative-time dashboard comparison from all saved concurrencies."""
    paths: list[tuple[int, str, Path]] = []
    for data in runs:
        try:
            concurrency = int(data["metadata"]["concurrency"])
        except (KeyError, TypeError, ValueError):
            continue
        path = data["directory"] / "dashboard.html"
        if path.is_file():
            paths.append((concurrency, run_label(data), path))
    if len(paths) < 2:
        return None
    overlay_path = Path(__file__).with_name("overlay_dashboards.py")
    spec = importlib.util.spec_from_file_location("agentx_dashboard_overlay", overlay_path)
    if spec is None or spec.loader is None:
        return None
    overlay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(overlay)
    file_data = []
    labels = []
    for concurrency, label, path in sorted(paths, key=lambda item: (item[0], item[1])):
        panels, rows = overlay.extract_data(path)
        file_data.append((panels, rows, label))
        labels.append(label)
    merged = overlay.merge(file_data)
    page = overlay.generate_html(
        merged, file_data[0][1], labels,
        str(Path(__file__).with_name("plotly-basic-2.35.2.min.js.gz")),
    )
    (root / "monitoring-overlay.html").write_text(page, encoding="utf-8")
    return page.encode("utf-8")


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in {"run", "index"}:
        raise SystemExit("Usage: aiperf_report.py {run|index} DIRECTORY")
    directory = Path(sys.argv[2])
    if sys.argv[1] == "run":
        write_run(directory)
    else:
        write_index(directory)


if __name__ == "__main__":
    main()

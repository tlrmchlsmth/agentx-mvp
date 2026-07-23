#!/usr/bin/env python3
import argparse
import base64
import concurrent.futures
import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

_http_semaphore = threading.Semaphore(32)

SKIP_PANEL_TITLES = {
    "Decode CUDA Graph Mode (iterations/s)",
}

SCOPED_METRIC_PREFIXES = (
    "vllm:",
    "DCGM_",
    "nixl_",
    "llm_d",
    "llmd_",
    "envoy_",
)

POD_MATCHER_RE = re.compile(r'pod\s*(=~|=)\s*"([^"\\]*(?:\\.[^"\\]*)*)"')
METRIC_SELECTOR_RE = re.compile(
    r'(?P<metric>[a-zA-Z_:][a-zA-Z0-9_:]*)'
    r'(?P<selector>\{[^{}]*\})'
)


def parse_relative_time(s):
    if s == "now":
        return time.time()
    m = re.match(r"now-(\d+)([smhd])", s)
    if not m:
        raise ValueError(f"Cannot parse time: {s!r}. Use ISO 8601 or now-<N><s|m|h|d>")
    val, unit = int(m.group(1)), m.group(2)
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return time.time() - val * multiplier


def parse_time(s):
    if s.startswith("now"):
        return parse_relative_time(s)
    try:
        return float(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse time: {s!r}")


def grafana_request(base_url, path, auth, data=None):
    url = base_url.rstrip("/") + path
    headers = {
        "Authorization": "Basic " + base64.b64encode(auth.encode()).decode(),
        "Content-Type": "application/json",
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers)
    with _http_semaphore:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())


def extract_panels(panels):
    result = []
    for p in panels:
        if should_skip_panel(p):
            continue
        if p.get("targets"):
            result.append(p)
        if p.get("panels"):
            result.extend(extract_panels(p["panels"]))
    return result


def should_skip_panel(panel):
    return panel.get("title", "") in SKIP_PANEL_TITLES


def convert_scenes_to_classic(dashboard):
    """Convert Grafana Scenes/v2 dashboard (elements+layout) to classic panels format."""
    elements = dashboard.get("elements", {})
    layout = dashboard.get("layout", {})
    items = layout.get("spec", {}).get("items", [])

    panels = []
    for item in items:
        ref = item.get("spec", {}).get("element", {}).get("name", "")
        elem = elements.get(ref)
        if not elem or elem.get("kind") != "Panel":
            continue

        spec = elem["spec"]
        queries = spec.get("data", {}).get("spec", {}).get("queries", [])
        viz = spec.get("vizConfig", {}).get("spec", {})
        field_config = viz.get("fieldConfig", {})

        targets = []
        for q in queries:
            qs = q.get("spec", {}).get("query", {}).get("spec", {})
            expr = qs.get("expr", "")
            if not expr:
                continue
            targets.append({
                "expr": expr,
                "legendFormat": qs.get("legendFormat", ""),
                "refId": q.get("spec", {}).get("refId", ""),
            })

        if not targets:
            continue

        pos = item.get("spec", {})
        panels.append({
            "id": ref,
            "title": spec.get("title", ref),
            "targets": targets,
            "fieldConfig": field_config,
            "gridPos": {"x": pos.get("x", 0), "y": pos.get("y", 0),
                        "w": pos.get("width", 12), "h": pos.get("height", 8)},
        })

    panels.sort(key=lambda p: (p["gridPos"]["y"], p["gridPos"]["x"]))
    return panels


def substitute_vars(expr, deployment):
    expr = expr.replace("${deployment}", deployment)
    expr = expr.replace("$deployment", deployment)
    expr = expr.replace("${DS_PROMETHEUS}", "PBFA97CFB590B2093")
    expr = expr.replace("$__all", ".*")
    expr = expr.replace("$model_name", ".*")
    expr = expr.replace("${model_name}", ".*")
    expr = expr.replace("$namespace", ".*")
    expr = expr.replace("${namespace}", ".*")
    expr = expr.replace("$__rate_interval", "15s")
    return expr


def prom_regex_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def regex_literal(s):
    return re.escape(s).replace(r"\-", "-")


def parse_mixed_ep_deployment(deployment):
    # Matches both the "wide-ep" release naming (deepseek-v4) and the "ix"
    # release naming (deepseek-v4-ix), e.g. "<user>-wide-ep-1p-ep8-1d-ep8"
    # or "<user>-ix-1p-ep8-1d-ep12". Prefill and decode can use different
    # EP widths, which also means different pod-name prefixes, so callers
    # need both widths to build a pod regex that matches real pod names.
    m = re.match(
        r"^(?P<user>[^-]+)-(?:wide-ep|ix)-"
        r"(?P<prefill>\d+)p-ep(?P<prefill_ep>\d+)-"
        r"(?P<decode>\d+)d-ep(?P<decode_ep>\d+)$",
        deployment,
    )
    return m.groupdict() if m else None


def derive_pod_regex(deployment, pod_regex):
    parts = []

    def add(part):
        part = (part or "").strip()
        if part and part not in parts and part != ".*":
            parts.append(part)

    add(deployment)
    for part in re.split(r"[\s,|]+", pod_regex or ""):
        add(part)

    mixed_ep = parse_mixed_ep_deployment(deployment)
    if mixed_ep:
        user = mixed_ep["user"]
        release = deployment[len(user) + 1:]
        add(regex_literal(deployment) + ".*")
        add(regex_literal(release) + ".*")
        for ep in sorted({mixed_ep["prefill_ep"], mixed_ep["decode_ep"]}):
            add(regex_literal(f"{user}-vllm-ep{ep}") + "-.*")

    if not parts:
        return ""
    return "|".join(f"(?:{p})" for p in parts)


def should_scope_metric(metric):
    return metric.startswith(SCOPED_METRIC_PREFIXES)


def scope_promql_expr(expr, pod_regex):
    if not pod_regex:
        return expr

    matcher = f'pod=~"{prom_regex_escape(pod_regex)}"'

    def replace_existing_pod_matcher(match):
        return matcher

    expr = POD_MATCHER_RE.sub(replace_existing_pod_matcher, expr)

    def add_pod_matcher(match):
        metric = match.group("metric")
        selector = match.group("selector")
        if "pod=" in selector or "pod=~" in selector or not should_scope_metric(metric):
            return match.group(0)

        inner = selector[1:-1].strip()
        if inner:
            inner += ", "
        inner += matcher
        return f"{metric}" + "{" + inner + "}"

    return METRIC_SELECTOR_RE.sub(add_pod_matcher, expr)


def infer_unit(title, unit):
    if unit:
        return unit
    t = title.lower()
    if "throughput" in t and ("token" in t or "prompt" in t or "generation" in t):
        return "tps"
    if "request" in t and ("per second" in t or "rate" in t):
        return "reqps"
    if "latency" in t or "time" in t or "ttft" in t or "itl" in t:
        return "s"
    if "utilization" in t or ("usage" in t and "cache" in t):
        return "percentunit"
    if "bandwidth" in t or "bytes" in t:
        return "Bps"
    if "power" in t:
        return "watt"
    if "requests" in t or "queue" in t or "preemptions" in t:
        return "short"
    return unit


def query_prometheus(base_url, auth, ds_id, expr, start, end, step):
    path = f"/api/datasources/proxy/{ds_id}/api/v1/query_range"
    params = urllib.parse.urlencode({
        "query": expr,
        "start": int(start),
        "end": int(end),
        "step": f"{step}s",
    })
    try:
        return grafana_request(base_url, f"{path}?{params}", auth)
    except urllib.error.HTTPError as e:
        return {"status": "error", "error": e.read().decode()[:200]}


def export(args):
    start = parse_time(args.start)
    end = parse_time(args.end)
    range_seconds = end - start
    step = args.step or max(15, int(range_seconds / 1000))

    print(f"Time range: {datetime.fromtimestamp(start, tz=timezone.utc).isoformat()} → "
          f"{datetime.fromtimestamp(end, tz=timezone.utc).isoformat()} (step={step}s)")

    dash = grafana_request(args.grafana_url, f"/api/dashboards/uid/{args.dashboard}", args.auth)
    ds_info = grafana_request(args.grafana_url, "/api/datasources/uid/PBFA97CFB590B2093", args.auth)
    ds_id = ds_info["id"]

    dash_body = dash["dashboard"]
    if "elements" in dash_body and "panels" not in dash_body:
        all_panels = convert_scenes_to_classic(dash_body)
    else:
        all_panels = dash_body["panels"]
    query_panels = extract_panels(all_panels)
    pod_regex = derive_pod_regex(args.deployment, args.pod_regex)
    print(f"Found {len(query_panels)} panels with queries")
    if pod_regex:
        print(f"Pod filter: {pod_regex}")

    row_order = []
    for p in all_panels:
        if should_skip_panel(p):
            continue
        if p.get("type") == "row":
            row_order.append({"type": "row", "title": p.get("title", ""), "id": p["id"]})
            if p.get("panels"):
                for sp in p["panels"]:
                    if sp.get("targets") and not should_skip_panel(sp):
                        row_order.append({"type": "panel", "id": sp["id"]})
        elif p.get("targets"):
            row_order.append({"type": "panel", "id": p["id"]})

    def query_panel(panel):
        pid = panel["id"]
        title = panel.get("title", f"panel-{pid}")
        unit = infer_unit(title, panel.get("fieldConfig", {}).get("defaults", {}).get("unit", ""))

        queries = []
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            if not expr:
                continue
            expr = substitute_vars(expr, args.deployment)
            expr = scope_promql_expr(expr, pod_regex)
            legend = target.get("legendFormat", "")
            result = query_prometheus(args.grafana_url, args.auth, ds_id, expr, start, end, step)

            series = []
            if result.get("status") == "success":
                for r in result.get("data", {}).get("result", []):
                    series.append({
                        "labels": r.get("metric", {}),
                        "values": r.get("values", []),
                    })

            queries.append({"expr": expr, "legend": legend, "series": series})

        total_points = sum(len(s["values"]) for q in queries for s in q["series"])
        print(f"  [{pid}] {title} ({len(queries)} queries, {total_points} datapoints)")

        return pid, {
            "id": pid,
            "title": title,
            "unit": unit,
            "queries": queries,
        }

    panel_data = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(query_panels)) as pool:
        for pid, data in pool.map(query_panel, query_panels):
            panel_data[pid] = data

    ts_start = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    ts_end = datetime.fromtimestamp(end, tz=timezone.utc).strftime("%Y%m%dT%H%M%S")

    if args.output:
        out_path = args.output
    else:
        out_path = f"dashboard_{ts_start}_{ts_end}.html"

    html = generate_html(panel_data, row_order, start, end, args.dashboard)

    with open(out_path, "w") as f:
        f.write(html)

    print(f"\nExported to {out_path}")


def generate_html(panel_data, row_order, start, end, dashboard_name):
    start_str = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    end_str = datetime.fromtimestamp(end, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    panels_json = json.dumps(panel_data)
    rows_json = json.dumps(row_order)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{dashboard_name} — {start_str} to {end_str}</title>
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
  .panel {{ background: #181b1f; border: 1px solid #2a2a2e; border-radius: 4px; padding: 0; overflow: hidden; }}
  .panel-title {{ font-size: 13px; font-weight: 500; padding: 8px 12px; color: #d8d9da; }}
  .panel .plot {{ width: 100%; height: 220px; }}
  .empty {{ color: #555; font-size: 12px; padding: 60px 12px; text-align: center; }}
  .hidden {{ display: none; }}
</style>
</head>
<body>
<h1>{dashboard_name}</h1>
<div class="subtitle">{start_str} — {end_str}</div>
<div id="root"></div>
<script>
if (!window.Plotly && window.parent && window.parent.Plotly) {{
  window.Plotly = window.parent.Plotly;
}}
const panels = {panels_json};
const rows = {rows_json};

const UNIT_FMT = {{
  tps: v => v.toFixed(1) + ' tok/s',
  percentunit: v => (v * 100).toFixed(1) + '%',
  s: v => v < 0.001 ? (v*1e6).toFixed(0)+'µs' : v < 1 ? (v*1000).toFixed(1)+'ms' : v.toFixed(2)+'s',
  bytes: v => v > 1e9 ? (v/1e9).toFixed(1)+'GB' : v > 1e6 ? (v/1e6).toFixed(1)+'MB' : v > 1e3 ? (v/1e3).toFixed(1)+'KB' : v+'B',
  Bps: v => v > 1e9 ? (v/1e9).toFixed(1)+' GB/s' : v > 1e6 ? (v/1e6).toFixed(1)+' MB/s' : v > 1e3 ? (v/1e3).toFixed(1)+' KB/s' : v+' B/s',
  watt: v => v.toFixed(0) + ' W',
  percent: v => v.toFixed(1) + '%',
  short: v => v > 1e6 ? (v/1e6).toFixed(1)+'M' : v > 1e3 ? (v/1e3).toFixed(1)+'K' : Number(v.toPrecision(4)).toString(),
}};

const UNIT_LABEL = {{
  tps: 'tokens/s',
  reqps: 'requests/s',
  ops: 'ops/s',
  percentunit: '%',
  percent: '%',
  s: 'seconds',
  bytes: 'bytes',
  Bps: 'bytes/s',
  watt: 'watts',
  short: 'count',
}};

const COLORS = ['#7EB26D','#EAB839','#6ED0E0','#EF843C','#E24D42','#1F78C1','#BA43A9','#705DA0',
                '#508642','#CCA300','#447EBC','#C15C17','#890F02','#0A437C','#6D1F62','#584477'];

function seriesLabel(q, s) {{
  let l = q.legend;
  if (l) {{
    for (const [k,v] of Object.entries(s.labels)) l = l.replace('{{{{'+k+'}}}}', v);
    if (!l.includes('{{{{')) return l;
  }}
  const parts = Object.entries(s.labels).filter(([k]) => k !== '__name__');
  return parts.length ? parts.map(([k,v])=>k+'='+v).join(', ') : q.expr.slice(0,60);
}}

const root = document.getElementById('root');
let currentGrid = null;

const lazyObserver = new IntersectionObserver((entries) => {{
  entries.forEach(entry => {{
    if (entry.isIntersecting) {{
      const div = entry.target;
      lazyObserver.unobserve(div);
      const p = div._panelData;
      const allSeries = p.queries.flatMap(q => q.series.map(s => ({{q, s}})));
      const plotDiv = div.querySelector('.plot');
      const traces = allSeries.filter(x => x.s.values.length > 0).map((x, i) => ({{
        x: x.s.values.map(v => new Date(v[0] * 1000)),
        y: x.s.values.map(v => parseFloat(v[1])),
        name: seriesLabel(x.q, x.s),
        type: 'scatter',
        mode: 'lines',
        line: {{ width: 1.5, color: COLORS[i % COLORS.length] }},
        hovertemplate: '%{{y}}<extra>%{{fullData.name}}</extra>',
      }}));
      const fmt = UNIT_FMT[p.unit];
      const yTitle = UNIT_LABEL[p.unit] || p.unit || '';
      if (!window.Plotly) {{
        plotDiv.innerHTML = '<div class="empty">Plotly unavailable</div>';
        return;
      }}
      Plotly.newPlot(plotDiv, traces, {{
        margin: {{ l: 58, r: 16, t: 4, b: 30 }},
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        font: {{ color: '#8e8e8e', size: 10 }},
        xaxis: {{ gridcolor: '#2a2a2e', linecolor: '#2a2a2e', tickformat: '%H:%M' }},
        yaxis: {{ gridcolor: '#2a2a2e', linecolor: '#2a2a2e',
                  title: yTitle ? {{ text: yTitle, font: {{ size: 10 }} }} : undefined,
                  tickformat: fmt ? undefined : '.3s',
                  hoverformat: '.4g' }},
        legend: {{ font: {{ size: 9 }}, orientation: 'h', y: -0.3 }},
        showlegend: traces.length > 1,
        hovermode: 'x unified',
      }}, {{ responsive: true, displayModeBar: false }});
    }}
  }});
}}, {{ rootMargin: '200px' }});

function renderPanel(container, p) {{
  const div = document.createElement('div');
  div.className = 'panel';
  div.innerHTML = '<div class="panel-title">' + p.title + '</div>';

  const allSeries = p.queries.flatMap(q => q.series.map(s => ({{q, s}})));
  const hasData = allSeries.some(x => x.s.values.length > 0 && x.s.values.some(v => !isNaN(parseFloat(v[1]))));

  if (!hasData) {{
    div.innerHTML += '<div class="empty">No data</div>';
    container.appendChild(div);
    return;
  }}

  const plotDiv = document.createElement('div');
  plotDiv.className = 'plot';
  div.appendChild(plotDiv);
  container.appendChild(div);
  div._panelData = p;
  lazyObserver.observe(div);
}}

if (rows.length === 0) {{
  currentGrid = document.createElement('div');
  currentGrid.className = 'grid';
  root.appendChild(currentGrid);
  for (const p of Object.values(panels)) renderPanel(currentGrid, p);
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
    }} else if (item.type === 'panel' && panels[item.id]) {{
      if (!currentGrid) {{
        currentGrid = document.createElement('div');
        currentGrid.className = 'grid';
        root.appendChild(currentGrid);
      }}
      renderPanel(currentGrid, panels[item.id]);
    }}
  }}
}}
</script>
</body>
</html>"""


def export_results(args):
    tasks = []
    for d in args.dirs:
        d = os.path.abspath(d)
        parent = os.path.dirname(d)
        json_path = os.path.join(d, "profile_export_aiperf.json")
        if not os.path.exists(json_path):
            print(f"Skipping {d} — no profile_export_aiperf.json")
            continue

        with open(json_path) as f:
            data = json.load(f)

        start_ns = data["min_request_timestamp"]["avg"]
        end_ns = data["max_response_timestamp"]["avg"]
        start = start_ns / 1e9 - args.pad
        end = end_ns / 1e9 + args.pad

        out_path = os.path.join(d, "dashboard.html")
        name = os.path.basename(d)
        deployment = args.deployment
        pod_regex = args.pod_regex
        config_name_path = os.path.join(parent, "config_name.txt")
        pods_path = os.path.join(parent, "pods.txt")
        if deployment == ".*" and os.path.exists(config_name_path):
            with open(config_name_path) as f:
                deployment = f.read().strip()
        if not pod_regex and os.path.exists(pods_path):
            with open(pods_path) as f:
                pod_regex = f.read().strip()
        tasks.append((name, start, end, args.pad, argparse.Namespace(
            start=str(start), end=str(end),
            deployment=deployment, pod_regex=pod_regex, step=args.step,
            grafana_url=args.grafana_url, auth=args.auth,
            output=out_path, dashboard=args.dashboard,
        )))

    if not tasks:
        return

    def run_one(task):
        name, start, end, pad, ns = task
        print(f"\n{'='*60}")
        print(f"{name}: {datetime.fromtimestamp(start, tz=timezone.utc).strftime('%H:%M:%S')} → "
              f"{datetime.fromtimestamp(end, tz=timezone.utc).strftime('%H:%M:%S')} "
              f"(±{pad}s pad)")
        export(ns)

    print(f"Exporting {len(tasks)} directories in parallel...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        list(pool.map(run_one, tasks))


def main():
    parser = argparse.ArgumentParser(description="Export Grafana dashboard to a self-contained HTML file")
    parser.add_argument("--deployment", default=".*", help="Deployment filter (default: .* for all)")
    parser.add_argument("--pod-regex", default="", help="Pod regex used to scope dashboard metrics")
    parser.add_argument("--step", type=int, help="Query step in seconds (auto if omitted)")
    parser.add_argument("--grafana-url", default="http://localhost:3001")
    parser.add_argument("--auth", default="admin:admin", help="user:password")
    parser.add_argument("--dashboard", default="wideep-overview", help="Dashboard UID")

    sub = parser.add_subparsers(dest="command")

    single = sub.add_parser("single", help="Export a single time range")
    single.add_argument("--start", required=True, help="Start time (ISO 8601, epoch, or now-30m)")
    single.add_argument("--end", default="now", help="End time (default: now)")
    single.add_argument("--output", "-o", help="Output file path")

    results = sub.add_parser("results", help="Export dashboards for benchmark result directories")
    results.add_argument("dirs", nargs="+", help="Result directories containing profile_export_aiperf.json")
    results.add_argument("--pad", type=int, default=60, help="Seconds of padding before/after run (default: 60)")

    args = parser.parse_args()

    if args.command == "results":
        export_results(args)
    elif args.command == "single":
        export(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

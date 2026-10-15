#!/usr/bin/env python3
import argparse
import base64
import concurrent.futures
import importlib.util
import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
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
KUBECTL_LOG_TIMESTAMP_RE = re.compile(r"^(?P<timestamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+(?P<message>.*)$")
PROFILE_LANES_RE = re.compile(r"PROFILING setup:\s*(?P<concurrency>\d+) trajectory lanes")
METRIC_SELECTOR_RE = re.compile(
    r'(?P<metric>[a-zA-Z_:][a-zA-Z0-9_:]*)'
    r'(?P<selector>\{[^{}]*\})'
)
BARE_SCOPED_METRIC_RE = re.compile(
    r"(?<![A-Za-z0-9_:])(?:" + "|".join(re.escape(prefix) for prefix in SCOPED_METRIC_PREFIXES) +
    r")[A-Za-z0-9_:]*"
)
INFERENCE_POOL_SELECTOR_RE = re.compile(r"(?<![A-Za-z0-9_:])(inference_pool_[A-Za-z0-9_:]+)(\{[^{}]*\})")
BARE_INFERENCE_POOL_RE = re.compile(r"(?<![A-Za-z0-9_:])inference_pool_[A-Za-z0-9_:]+")


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
    # Grafana turns an "All" variable into a regex matcher.  The live adapter
    # has no legacy deployment name, so its equivalent is `.*`; leaving that
    # behind an exact `=` matcher asks Prometheus for a literal asterisk and
    # silently produces empty panels.
    expr = re.sub(r'(?<![!~])=\s*"\.\*"', '=~".*"', expr)
    return expr


def prom_regex_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def regex_literal(s):
    return re.escape(s).replace(r"\-", "-")


def parse_wide_ep_deployment(deployment):
    m = re.match(
        r"^(?P<user>[^-]+)-wide-ep-"
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

    wide_ep = parse_wide_ep_deployment(deployment)
    if wide_ep:
        user = wide_ep["user"]
        release = deployment[len(user) + 1:]
        add(regex_literal(deployment) + ".*")
        add(regex_literal(release) + ".*")
        for ep in sorted({wide_ep["prefill_ep"], wide_ep["decode_ep"]}):
            add(regex_literal(f"{user}-vllm-ep{ep}") + "-.*")

    if not parts:
        return ""
    return "|".join(f"(?:{p})" for p in parts)


def pod_regex_for_panel(title, pod_regex):
    """Restrict phase-specific panels to pods serving that phase.

    Some deployed dashboard revisions omit their old ``job=`` matcher.  The
    report exporter already knows the exact serving pod names, so use those
    names instead of trusting a mutable Prometheus job label.
    """
    normalized = title.strip().lower()
    role = None
    if normalized.startswith("prefill "):
        role = "prefill"
    elif normalized.startswith("decode "):
        role = "decode"
    if not role or not pod_regex:
        return pod_regex

    # derive_pod_regex emits each concrete expression as a non-capturing
    # alternative.  Keep only alternatives naming the requested phase.
    alternatives = re.findall(r"\(\?:([^()]*)\)", pod_regex)
    selected = [part for part in alternatives if f"-{role}-" in part]
    if not selected:
        return pod_regex
    return "|".join(f"(?:{part})" for part in selected)


def should_scope_metric(metric):
    return metric.startswith(SCOPED_METRIC_PREFIXES)


def scope_promql_expr(expr, pod_regex, metrics_namespace=None):
    if not pod_regex and not metrics_namespace:
        return expr

    matcher = f'pod=~"{prom_regex_escape(pod_regex)}"' if pod_regex else ""

    def replace_existing_pod_matcher(match):
        return matcher

    if pod_regex:
        expr = POD_MATCHER_RE.sub(replace_existing_pod_matcher, expr)

    def add_pod_matcher(match):
        metric = match.group("metric")
        selector = match.group("selector")
        if not pod_regex or "pod=" in selector or "pod=~" in selector or not should_scope_metric(metric):
            return match.group(0)

        inner = selector[1:-1].strip()
        if inner:
            inner += ", "
        inner += matcher
        return f"{metric}" + "{" + inner + "}"

    expr = METRIC_SELECTOR_RE.sub(add_pod_matcher, expr)
    namespace_matcher = f'namespace="{metrics_namespace}"' if metrics_namespace else ""
    if metrics_namespace:
        def scope_inference_pool(match):
            selector = match.group(2)[1:-1]
            if re.search(r"\bnamespace\s*(?:=~|!~|=|!=)", selector):
                selector = re.sub(r'\bnamespace\s*(?:=~|!~|=|!=)\s*"(?:[^"\\]|\\.)*"',
                                  namespace_matcher, selector)
            else:
                selector = (selector + ", " if selector.strip() else "") + namespace_matcher
            return match.group(1) + "{" + selector + "}"
        expr = INFERENCE_POOL_SELECTOR_RE.sub(scope_inference_pool, expr)

    # Dashboard queries also use bare names such as rate(vllm:foo_total[1m]).
    # Scope those names without changing quoted strings or existing selectors.
    result = []
    index = 0
    quote = None
    selector_depth = 0
    while index < len(expr):
        char = expr[index]
        if quote:
            result.append(char)
            if char == "\\" and index + 1 < len(expr):
                index += 1
                result.append(expr[index])
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in ('"', "'", "`"):
            quote = char
        elif char == "{":
            selector_depth += 1
        elif char == "}" and selector_depth:
            selector_depth -= 1
        if not selector_depth and quote is None:
            metric = BARE_SCOPED_METRIC_RE.match(expr, index) if pod_regex else None
            namespace_metric = BARE_INFERENCE_POOL_RE.match(expr, index) if metrics_namespace else None
            metric = metric or namespace_metric
            if metric:
                name = metric.group(0)
                result.append(name)
                index = metric.end()
                if not expr[index:].lstrip().startswith("{"):
                    result.append("{" + (namespace_matcher if namespace_metric else matcher) + "}")
                continue
        result.append(char)
        index += 1
    return "".join(result)


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


def query_prometheus(base_url, auth, ds_uid, expr, start, end, step):
    # Grafana 12 removed the numeric-ID proxy route.  The datasource UID is
    # stable and works on both the dashboard API and its proxy route.
    path = f"/api/datasources/proxy/uid/{ds_uid}/api/v1/query_range"
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
    ds_uid = ds_info["uid"]

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
        panel_pod_regex = pod_regex_for_panel(title, pod_regex)

        queries = []
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            if not expr:
                continue
            expr = substitute_vars(expr, args.deployment)
            expr = scope_promql_expr(expr, panel_pod_regex, args.metrics_namespace)
            legend = target.get("legendFormat", "")
            result = query_prometheus(args.grafana_url, args.auth, ds_uid, expr, start, end, step)
            if result.get("status") != "success":
                raise RuntimeError(
                    f"Prometheus query failed for panel {pid} ({title}): "
                    f"{result.get('error') or result}"
                )

            series = []
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

    html = generate_html(panel_data, row_order, start, end, args.dashboard, args.plotly_bundle)

    with open(out_path, "w") as f:
        f.write(html)

    # Machine-readable copy of the panel data next to the page, so
    # overlay_dashboards reads it instead of scraping the HTML.
    sidecar = os.path.splitext(out_path)[0] + ".json"
    with open(sidecar, "w") as f:
        json.dump({"panels": panel_data, "rows": row_order}, f)

    print(f"\nExported to {out_path}")


def generate_html(panel_data, row_order, start, end, dashboard_name, plotly_bundle=None):
    start_str = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    end_str = datetime.fromtimestamp(end, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    panels_json = json.dumps(panel_data)
    rows_json = json.dumps(row_order)

    plotly_tag = COMMON.plotly_tag(plotly_bundle)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{dashboard_name} — {start_str} to {end_str}</title>
{plotly_tag}
<style>
{COMMON.BASE_CSS}</style>
</head>
<body>
<h1>{dashboard_name}</h1>
<div class="subtitle">{start_str} — {end_str}</div>
<div id="root"></div>
<script>
{COMMON.PARENT_PLOTLY_SHIM}const panels = {panels_json};
const rows = {rows_json};

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
{COMMON.PANEL_JS}
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
      const yTitle = UNIT_LABEL[p.unit] || p.unit || '';
      const allY = traces.flatMap(trace => trace.y).filter(Number.isFinite);
      const largestY = allY.reduce((largest, value) => Math.max(largest, value), -Infinity);
      const smallestY = allY.reduce((smallest, value) => Math.min(smallest, value), Infinity);
      const decimals = yTickDecimals(largestY, smallestY);
      const yFormat = `,.${{decimals}}f`;
      const widestLabel = Math.max(
        largestY.toLocaleString('en-US', {{minimumFractionDigits: decimals, maximumFractionDigits: decimals}}).length,
        smallestY.toLocaleString('en-US', {{minimumFractionDigits: decimals, maximumFractionDigits: decimals}}).length,
      );
      if (!window.Plotly) {{
        plotDiv.innerHTML = '<div class="empty">Plotly unavailable</div>';
        return;
      }}
      Plotly.newPlot(plotDiv, traces, {{
        margin: {{ l: Math.max(76, (widestLabel + 2) * 8), r: 16, t: 4, b: 40 }},
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        font: {{ color: '#8e8e8e', size: 10 }},
        xaxis: {{ gridcolor: '#2a2a2e', linecolor: '#2a2a2e', tickformat: '%H:%M', exponentformat: 'none', showexponent: 'none' }},
        yaxis: {{ gridcolor: '#2a2a2e', linecolor: '#2a2a2e',
                  title: yTitle ? {{ text: yTitle, font: {{ size: 10 }} }} : undefined,
                  tickformat: yFormat, hoverformat: yFormat, exponentformat: 'none', showexponent: 'none', separatethousands: true, automargin: true }},
        legend: {{ font: {{ size: 9 }}, orientation: 'v', x: 1, xanchor: 'right', y: 1, yanchor: 'top', bgcolor: 'rgba(17,18,23,.88)' }},
        // Long pod/rank names otherwise cover the data; the panel button can
        // reveal the legend on demand.
        showlegend: false,
        hovermode: 'x unified',
      }}, {{ responsive: true, displayModeBar: true, displaylogo: false, scrollZoom: true, modeBarButtonsToRemove: ['lasso2d','select2d'] }});
    }}
  }});
}}, {{ rootMargin: '200px' }});

function renderPanel(container, pid, p) {{
  const div = document.createElement('div');
  div.className = 'panel';
  div.innerHTML = panelHeaderHtml(p.title);

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
  makePanelInteractive(div, plotDiv);
  div._panelData = p;
  lazyObserver.observe(div);
}}

scaffoldRows(rows, panels, renderPanel);
</script>
</body>
</html>"""


def logged_aiperf_window(log_path, directory):
    """Return exact profiling boundaries for c<N> or c<N>-r<M> runs."""
    match = re.fullmatch(r"c(\d+)(?:-r\d+)?", os.path.basename(directory))
    if not match:
        raise RuntimeError(f"Cannot identify concurrency from {directory}")
    wanted = match.group(1)
    repeat_match = re.fullmatch(r"c\d+-r(\d+)", os.path.basename(directory))
    occurrence = int(repeat_match.group(1)) if repeat_match else 1
    try:
        lines = open(log_path, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"Cannot read AIPerf Job log {log_path}: {exc}") from exc
    pending_concurrency = None
    active_window = None
    windows = {}
    with lines:
        for line in lines:
            stamped = KUBECTL_LOG_TIMESTAMP_RE.match(line)
            if not stamped:
                continue
            timestamp = datetime.fromisoformat(stamped["timestamp"].replace("Z", "+00:00")).timestamp()
            message = stamped["message"]
            lanes = PROFILE_LANES_RE.search(message)
            if lanes:
                pending_concurrency = lanes["concurrency"]
            # AIPerf now emits "Phase profiling (profiling) started" and
            # "Phase profiling (profiling) complete".  Keep the match
            # specific so the intermediate "sending complete" line does not
            # close the monitoring window early.
            if re.search(r"Phase profiling(?: \([^)]*\))? started\b", message):
                if pending_concurrency:
                    active_window = [pending_concurrency, timestamp, None]
                    windows.setdefault(pending_concurrency, []).append(active_window)
            elif (
                re.search(r"Phase profiling(?: \([^)]*\))? complete\b", message)
                and active_window is not None
            ):
                active_window[2] = timestamp
                active_window = None

    concurrency_windows = windows.get(wanted, [])
    if occurrence > len(concurrency_windows):
        raise RuntimeError(
            f"No profiling window #{occurrence} for c{wanted} in {log_path}; "
            f"found {len(concurrency_windows)} window(s)"
        )
    _, start, end = concurrency_windows[occurrence - 1]
    if start is not None and end is not None and end > start:
        return start, end
    raise RuntimeError(
        f"No complete timestamped profiling phase for c{wanted}-r{occurrence} in {log_path}; "
        "refusing to guess a monitoring range"
    )


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

        if args.aiperf_log:
            start, end = logged_aiperf_window(args.aiperf_log, d)
        else:
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
            deployment=deployment, pod_regex=pod_regex, metrics_namespace=args.metrics_namespace, step=args.step,
            grafana_url=args.grafana_url, auth=args.auth,
            output=out_path, dashboard=args.dashboard,
            plotly_bundle=args.plotly_bundle, aiperf_log=args.aiperf_log,
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
    parser.add_argument("--metrics-namespace", help="namespace for inference pool dashboard metrics")
    parser.add_argument("--step", type=int, help="Query step in seconds (auto if omitted)")
    parser.add_argument("--grafana-url", default="http://localhost:3001")
    parser.add_argument("--auth", help="user:password")
    parser.add_argument("--auth-env", help="environment variable containing user:password")
    parser.add_argument("--dashboard", default="wideep-overview", help="Dashboard UID")
    parser.add_argument("--plotly-bundle", help="gzip-compressed Plotly JS to embed for offline output")
    parser.add_argument("--aiperf-log", help="Timestamped AIPerf Job log used for profiling-phase boundaries")

    sub = parser.add_subparsers(dest="command")

    single = sub.add_parser("single", help="Export a single time range")
    single.add_argument("--start", required=True, help="Start time (ISO 8601, epoch, or now-30m)")
    single.add_argument("--end", default="now", help="End time (default: now)")
    single.add_argument("--output", "-o", help="Output file path")

    results = sub.add_parser("results", help="Export dashboards for benchmark result directories")
    results.add_argument("dirs", nargs="+", help="Result directories containing profile_export_aiperf.json")
    results.add_argument("--pad", type=int, default=60, help="Seconds of padding before/after run (default: 60)")

    args = parser.parse_args()
    if args.auth_env:
        if args.auth:
            parser.error("use --auth or --auth-env, not both")
        args.auth = os.environ.get(args.auth_env)
        if not args.auth:
            parser.error(f"{args.auth_env} is empty or unset")
    elif args.auth is None:
        args.auth = "admin:admin"

    if args.command == "results":
        export_results(args)
    elif args.command == "single":
        export(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

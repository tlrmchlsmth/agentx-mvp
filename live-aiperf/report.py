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


def support_file(name: str) -> Path:
    """Find helpers next to the copied Job script or in the source checkout."""
    adjacent = Path(__file__).with_name(name)
    return adjacent if adjacent.is_file() else adjacent.parent.parent / name


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


def source_ref(metadata: dict[str, Any]) -> str:
    return str(metadata.get("source_ref") or metadata.get("vllm_build_ref") or "unknown")


def source_commit(metadata: dict[str, Any]) -> str:
    return str(metadata.get("source_commit") or metadata.get("vllm_build_commit") or "")


def vllm_version(metadata: dict[str, Any]) -> str:
    steps = metadata.get("vllm_build_steps")
    if isinstance(steps, list) and steps:
        return " + ".join(f"{step['ref']}@{step['commit'][:12]}" for step in steps)
    return str(metadata.get("vllm_image") or source_commit(metadata))


def source_row(data: dict[str, Any]) -> str:
    metadata = data["metadata"]
    ref = source_ref(metadata)
    commit = source_commit(metadata)
    kind = str(metadata.get("source_kind") or "vllm")
    identity = f'{html.escape(kind)} <code>{html.escape(ref)}</code>'
    if metadata.get("campaign_label"):
        identity = f'<strong>{html.escape(str(metadata["campaign_label"]))}</strong> — ' + identity
    build = metadata.get("vllm_build_steps") or []
    if build:
        vllm_identity = "; vLLM inputs " + ", ".join(
            f"{html.escape(step['action'])} {html.escape(step['ref'])}@<code>{html.escape(step['commit'])}</code>"
            for step in build)
    elif metadata.get("vllm_image"):
        vllm_identity = f'; vLLM image <code>{html.escape(str(metadata["vllm_image"]))}</code>'
    else:
        vllm_identity = ""
    deepep = metadata.get("deepep_build")
    if isinstance(deepep, dict):
        vllm_identity += (f'; DeepEP {html.escape(str(deepep["ref"]))}@'
                          f'<code>{html.escape(str(deepep["commit"]))}</code>')
    counts = gpu_counts(data)
    return (
        f'<p>{identity} <code>{html.escape(commit)}</code> '
        f'{vllm_identity} '
        f'— {number(counts["prefill_gpus"])} prefill GPUs, '
        f'{number(counts["decode_gpus"])} decode GPUs</p>'
    )


def write_index(root: Path) -> None:
    """Adapt live artifacts to the repository's established v2 renderer."""
    runs = [
        data for path in sorted(root.rglob("profile_export_aiperf.json"))
        if (data := run_data(path.parent)) and source_commit(data["metadata"])
    ]
    if not runs:
        raise SystemExit(f"No completed live AIPerf runs in {root}")
    write_index_from_runs(root, runs)


def preview_header_css() -> str:
    return """<style>
.campaign-overview{background:#181b1f;border:1px solid #2a2a2e;border-radius:7px;padding:14px 16px;margin:12px 0 20px}
.campaign-status{display:flex;align-items:baseline;flex-wrap:wrap;gap:8px;font-size:13px}
.campaign-status strong{color:#f0f1f2;font-size:15px;font-weight:600}
.campaign-status .separator{color:#555}
.campaign-updated{margin-left:auto;color:#8e8e8e;font-size:11px}
.campaign-running{color:#b9c9da;font-size:12px;margin-top:7px}
.campaign-actions{display:flex;align-items:center;flex-wrap:wrap;gap:10px;margin-top:12px}
.campaign-actions button{background:#1e2127;border:1px solid #58a6ff;border-radius:5px;color:#d8eaff;cursor:pointer;padding:7px 11px;font:inherit;font-size:12px}
.campaign-actions button:hover{background:#25364a}
.campaign-detail{color:#aeb3bb;font-size:12px;margin-top:10px}
.campaign-actions .campaign-detail{margin-top:0}
.campaign-detail summary{cursor:pointer;width:max-content;max-width:100%;color:#9dc7ee}
.campaign-detail[open]{width:100%}
.campaign-detail table{border-collapse:collapse;width:100%;font-size:12px;margin-top:9px}
.campaign-detail th,.campaign-detail td{border-bottom:1px solid #2a2a2e;padding:5px 8px;text-align:left}
.campaign-detail th{color:#8e8e8e;font-weight:500}
.campaign-detail p{margin-top:8px;overflow-wrap:anywhere}
.campaign-note{font-size:12px;color:#d3bd85;margin-top:10px}
.campaign-benchmark-summary{font-size:12px;color:#d8eaff;margin:10px 0 4px;overflow-wrap:anywhere}
</style>"""


def campaign_banner(campaign: dict[str, Any]) -> str:
    """Summarize campaign state above the shared interactive charts."""
    records = campaign.get("overlays", [])
    completed = sum(record.get("status") == "completed" for record in records)
    samples = {
        tool: sum(len(bench.get("measurements", [])) for record in records
                  for bench in record.get("benchmarks", []) if bench.get("tool") == tool)
        for tool in ("aiperf", "nyann")
    }
    status = html.escape(str(campaign.get("status", "unknown")))
    planned = campaign.get("planned_deployments", len(records))
    counts = (f"{completed}/{planned} deployments completed ({len(records)} attempted) · "
              f"{samples['aiperf']} AIPerf samples · {samples['nyann']} Nyann stages")
    banner = (f'<div class="campaign-status"><strong>Campaign {status}</strong>'
              f'<span>{counts}</span></div>')
    if campaign.get("mode") == "mock-test":
        banner += '<p class="campaign-note"><strong>MOCK DATA:</strong> no benchmark or Grafana query ran.</p>'
    errors = []
    for field in ("error", "report_error"):
        if campaign.get(field):
            errors.append(f"Campaign: {campaign[field]}")
    for record in records:
        if record.get("error"):
            errors.append(f"{record['name']}: {record['error']}")
        for bench in record.get("benchmarks", []):
            if bench.get("error"):
                errors.append(f"{record['name']} / {bench['tool']}: {bench['error']}")
    if errors:
        banner += ('<details class="campaign-detail"><summary>Failure details</summary><pre>'
                   + html.escape("\n\n".join(errors)) + '</pre></details>')
    return banner


def nyann_setup(config: dict[str, Any], campaign_dir: Path | None = None) -> str:
    """Show the configured Nyann sweep and the fixed scenario used by submit.sh."""
    benchmarks = [bench for bench in config.get("benchmarks", []) if bench.get("tool") == "nyann"]
    if not benchmarks:
        return ""
    sections = []
    models = ", ".join(sorted({str(overlay["model_label"]) for overlay in config.get("overlays", [])
                               if overlay.get("model_label")}))
    target = config.get("base_url") or f"http://llm-d-inference-gateway-istio.{config['namespace']}.svc.cluster.local/v1"
    images = set()
    if campaign_dir is not None:
        for log in campaign_dir.glob("*/nyann-submit.log"):
            match = re.search(r"(?m)^nyann-bench image: (.+)$", log.read_text(errors="replace"))
            if match:
                images.add(match.group(1).strip())
    for bench in benchmarks:
        concurrencies = bench["concurrencies"]
        stages = ", ".join(f"c{value}" for value in concurrencies)
        warmup = (f"{bench['warmup_seconds']} s at c{max(concurrencies)}"
                  if bench["warmup_seconds"] else "disabled")
        headline = (f"Nyann synthetic workload · ISL {bench['isl']} · OSL {bench['osl']} · "
                    f"{stages} · {bench['duration_seconds']} s/stage · warmup {warmup}")
        settings = [
            ("Tool", bench["tool"]), ("Workload", "synthetic"), ("Turns", "1"),
            ("Input sequence length (ISL)", f"{bench['isl']} tokens"),
            ("Output sequence length (OSL)", f"{bench['osl']} tokens"),
            ("Concurrency stages", stages), ("Duration per stage", f"{bench['duration_seconds']} s"),
            ("Warmup", warmup), ("Target", str(target)), ("Model", models or "unspecified"),
        ]
        if images:
            settings.append(("Nyann image", ", ".join(sorted(images))))
        rows = "".join(f"<tr><th>{html.escape(key)}</th><td>{html.escape(value)}</td></tr>"
                       for key, value in settings)
        exact = html.escape(json.dumps(bench, indent=2))
        sections.append(
            '<div class="campaign-benchmark">'
            f'<p class="campaign-benchmark-summary">{html.escape(headline)}</p>'
            '<details class="campaign-detail"><summary>Full Nyann benchmark configuration</summary>'
            f'<table><tbody>{rows}</tbody></table>'
            '<p>TPOT is calculated per completed request from Nyann JSONL as '
            '(end-to-end latency − TTFT) / (output tokens − 1); one-token requests are excluded.</p>'
            '<p>Prompt-token counts use Nyann response usage. A reported zero can mean the server '
            'did not supply prompt-token usage; check the configured ISL.</p>'
            f'<p>Configured JSON</p><pre>{exact}</pre>'
            '</details></div>')
    return "".join(sections)


def write_index_from_runs(root: Path, runs: list[dict[str, Any]], *, extra_html: str = "",
                          model_label: str | None = None, save_monitoring_overlay: bool = True,
                          compact_header: bool = False,
                          campaign: dict[str, Any] | None = None) -> None:
    """Render selected runs into one portable AIPerf report."""
    renderer_path = support_file("gen_interactivity_chart.py")
    module_spec = importlib.util.spec_from_file_location("agentx_v2_charts", renderer_path)
    if module_spec is None or module_spec.loader is None:
        raise SystemExit(f"Missing shared chart renderer: {renderer_path}")
    renderer = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(renderer)

    if campaign is not None:
        extra_html = campaign_banner(campaign) + extra_html
    if not runs:
        if campaign is None:
            raise SystemExit(f"No completed live AIPerf runs in {root}")
        title = model_label or f"Campaign {campaign['id']}"
        page = document(title, f'<h1>{html.escape(title)}</h1><section class="campaign-overview">'
                        + extra_html + '</section>')
        (root / "index.html").write_text(page.replace('</head>', preview_header_css() + '</head>', 1),
                                         encoding="utf-8")
        return

    overlay_html = monitoring_overlay(root, runs, save_file=save_monitoring_overlay)

    configs: dict[str, dict[str, Any]] = {}
    metric_units: dict[str, str] = {}
    for data in runs:
        metadata = data["metadata"]
        config_name = series_key(data)
        counts = gpu_counts(data)
        repeat = repeat_number(data)
        source_label = str(metadata.get("campaign_label") or source_ref(metadata))
        if repeat is not None:
            source_label += f" — rerun {repeat}"
        config = configs.setdefault(config_name, {
            "label": source_label,
            "decode_gpus": counts["decode_gpus"],
            "prefill_gpus": counts["prefill_gpus"],
            "pods": str(metadata.get("topology", "live deployment")),
            "runs": {},
            "version": vllm_version(metadata),
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
    nyann_runs = [data for data in runs if data["metadata"].get("benchmark_tool") == "nyann"]
    has_tpot = nyann_runs and all("p90" in data["profile"].get("time_per_output_token", {})
                                   for data in runs)
    nyann_latency_stat = ("p90" if all("p90" in data["profile"].get("time_to_first_token", {})
                                      for data in nyann_runs) else "p95")
    chart_defaults = ({
        "throughput": {"xMetric": "request_throughput", "yMetric": "output_token_throughput", "yNorm": "none"},
        "latency": {"xMetric": "time_per_output_token" if has_tpot else "time_to_first_token",
                    "xStat": "p90" if has_tpot else nyann_latency_stat,
                    "yMetric": "output_token_throughput", "yNorm": "none"},
    } if nyann_runs else {
        "throughput": {"xMetric": "e2e_output_token_throughput", "yMetric": "output_token_throughput", "yNorm": "decode"},
        "latency": {"xMetric": "e2e_output_token_throughput", "yMetric": "input_token_throughput", "yNorm": "prefill"},
    })
    renderer.generate_html(
        configs, str(output), str(root), metric_units,
        model_label=model_label or str(first_metadata.get("model_label", "Live llm-d")),
        chart_defaults=chart_defaults,
    )
    page = output.read_text(encoding="utf-8")
    page = page.replace(
        '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>',
        "<script>" + embedded_plotly() + "</script>",
        1,
    )
    source_rows = "".join(
        source_row(data)
        for data in sorted({sweep_key(data): data for data in runs}.values(), key=sweep_key)
    )
    source = ('<details class="campaign-detail"><summary>Run identity</summary>' + source_rows + '</details>'
              if compact_header else '<div class="subtitle">' + source_rows + '</div>')
    overlay_control = ""
    if overlay_html:
        encoded_overlay = base64.b64encode(overlay_html).decode("ascii")
        overlay_control = (
            ('<button id="monitoring-overlay">Overlay monitoring across concurrencies</button>' if compact_header
             else '<p><button id="monitoring-overlay">Overlay monitoring across concurrencies</button></p>') +
            '<script>document.getElementById("monitoring-overlay").addEventListener("click",()=>{'
            f'const b=atob("{encoded_overlay}");const a=new Uint8Array(b.length);'
            'for(let i=0;i<b.length;i++)a[i]=b.charCodeAt(i);'
            'window.open(URL.createObjectURL(new Blob([a],{type:"text/html"})),"_blank");});</script>'
        )
    if compact_header:
        title = html.escape(model_label or str(first_metadata.get("model_label", "Live llm-d")))
        rendered_title = f'<h1>{title} Disaggregated Serving — Interactivity vs Throughput</h1>'
        page = page.replace(rendered_title, f'<h1>{title}</h1>', 1)
        subtitle_start = page.find('<div class="subtitle">', page.find(f'<h1>{title}</h1>'))
        if subtitle_start >= 0:
            subtitle_end = page.find('</div>', subtitle_start)
            page = page[:subtitle_start] + page[subtitle_end + len('</div>'):]
        page = page.replace('</head>', preview_header_css() + '</head>', 1)
        header = '<section class="campaign-overview">' + extra_html + \
                 '<div class="campaign-actions">' + overlay_control + source + '</div></section>'
    else:
        header = extra_html + source + overlay_control
    page = page.replace('<div id="root"></div>', header + '<div id="root"></div>', 1)
    page = page.replace("</body>", pareto_section_html() + "\n</body>", 1)
    output.write_text(page, encoding="utf-8")


def pareto_section_html() -> str:
    """Return the built-in rerun-reduced concurrency Pareto view."""
    return r'''
<section id="pareto-front-section" style="margin:24px 0">
<h2 style="font-size:18px;font-weight:500;margin:16px 0 4px">Reduced concurrency sequence</h2>
<p class="subtitle">Select the best rerun at each concurrency over the two displayed metrics. The highlighted sequence keeps every selected concurrency, even when one concurrency dominates another.</p>
<div id="pareto-controls" class="axis-controls" style="display:flex;flex-wrap:wrap;gap:8px;align-items:end"></div>
<div id="pareto-range-controls" style="display:flex;flex-wrap:wrap;gap:8px;align-items:end;margin:4px 0 8px"></div>
<p id="pareto-summary" class="subtitle" aria-live="polite"></p>
<div id="pareto-plot" class="plot" style="height:580px;cursor:pointer" role="img" aria-label="Reduced concurrency sequence"></div>
<div class="summary" style="overflow-x:auto">
<table id="pareto-table">
<thead><tr><th>Concurrency</th><th>Selected rerun</th><th>X value</th><th>Y value</th></tr></thead>
<tbody></tbody>
</table>
</div>
</section>
<script>
(() => {
  const section = document.getElementById('pareto-front-section');
  const controls = document.getElementById('pareto-controls');
  const rangeControls = document.getElementById('pareto-range-controls');
  const summary = document.getElementById('pareto-summary');
  const plot = document.getElementById('pareto-plot');
  const table = document.getElementById('pareto-table');
  if (!section || !controls || !rangeControls || !summary || !plot || !table) return;

  const palette = ['#facc15'];
  const metricState = {
    xMetric: 'e2e_output_token_throughput', xStat: 'avg', xNorm: 'none', xGoal: 'max',
    yMetric: 'output_token_throughput', yStat: 'avg', yNorm: 'decode', yGoal: 'max'
  };
  const fields = {};

  function addField(key, label, options, selected) {
    const wrapper = document.createElement('label');
    wrapper.textContent = label;
    wrapper.style.cssText = 'display:flex;flex-direction:column;gap:3px;color:#8e8e8e;font-size:12px';
    const select = document.createElement('select');
    select.style.cssText = 'background:#181b1f;color:#d8d9da;border:1px solid #3a3a3e;border-radius:4px;padding:4px 6px;font:inherit;max-width:320px';
    options.forEach(option => {
      const item = document.createElement('option');
      item.value = option.value;
      item.textContent = option.text;
      select.appendChild(item);
    });
    select.value = selected;
    wrapper.appendChild(select);
    controls.appendChild(wrapper);
    fields[key] = select;
    return select;
  }
  function resetSelect(select, options, selected) {
    select.replaceChildren();
    options.forEach(option => {
      const item = document.createElement('option');
      item.value = option.value;
      item.textContent = option.text;
      select.appendChild(item);
    });
    select.value = options.some(option => option.value === selected) ? selected : options[0].value;
    return select.value;
  }
  function goalOptions() {
    return [{value:'max', text:'maximize'}, {value:'min', text:'minimize'}];
  }
  function defaultGoal(metric) {
    return /(latency|time_to_|duration|error_rate|error_count)/.test(metric) ? 'min' : 'max';
  }
  addField('xMetric', 'X metric', metricOptions(X_AXIS_METRICS), metricState.xMetric);
  addField('xStat', 'X statistic', statOptionsForMetric(metricState.xMetric), metricState.xStat);
  addField('xNorm', 'X normalization', normOptionsForMetric(metricState.xMetric, 'x'), metricState.xNorm);
  addField('xGoal', 'X objective', goalOptions(), metricState.xGoal);
  addField('yMetric', 'Y metric', metricOptions(Y_AXIS_METRICS), metricState.yMetric);
  addField('yStat', 'Y statistic', statOptionsForMetric(metricState.yMetric), metricState.yStat);
  addField('yNorm', 'Y normalization', normOptionsForMetric(metricState.yMetric, 'y'), metricState.yNorm);
  addField('yGoal', 'Y objective', goalOptions(), metricState.yGoal);

  function rangeField(label, selected) {
    const wrapper = document.createElement('label');
    wrapper.textContent = label;
    wrapper.style.cssText = 'display:flex;flex-direction:column;gap:3px;color:#8e8e8e;font-size:12px';
    const select = document.createElement('select');
    select.style.cssText = 'background:#181b1f;color:#d8d9da;border:1px solid #3a3a3e;border-radius:4px;padding:4px 6px;font:inherit';
    CONCURRENCIES.forEach(concurrency => {
      const option = document.createElement('option');
      option.value = concurrency;
      option.textContent = concurrency;
      select.appendChild(option);
    });
    select.value = selected;
    wrapper.appendChild(select);
    rangeControls.appendChild(wrapper);
    return select;
  }
  const rangeFrom = rangeField('Concurrency from', CONCURRENCIES[0]);
  const rangeTo = rangeField('Concurrency to', CONCURRENCIES[CONCURRENCIES.length - 1]);

  function activeConcurrencies() {
    const low = Math.min(Number(rangeFrom.value.replace(/^c/, '')), Number(rangeTo.value.replace(/^c/, '')));
    const high = Math.max(Number(rangeFrom.value.replace(/^c/, '')), Number(rangeTo.value.replace(/^c/, '')));
    return CONCURRENCIES.filter(concurrency => {
      const value = Number(concurrency.replace(/^c/, ''));
      return value >= low && value <= high;
    });
  }
  function finiteValue(cfg, concurrency, metric, stat, norm) {
    const sample = DATA[cfg]?.[concurrency]?.[metric];
    if (!sample || !Number.isFinite(sample[stat])) return null;
    const value = applyNorm(sample[stat], norm, CONFIGS[cfg]);
    return Number.isFinite(value) ? value : null;
  }
  function chooseBest(points, state) {
    if (!points.length) return null;
    const orientedX = point => state.xGoal === 'max' ? point.x : -point.x;
    const orientedY = point => state.yGoal === 'max' ? point.y : -point.y;
    const xs = points.map(orientedX), ys = points.map(orientedY);
    const minX = Math.min(...xs), maxX = Math.max(...xs);
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    const scale = (value, low, high) => high === low ? 1 : (value - low) / (high - low);
    return [...points].sort((a, b) => {
      const scoreA = scale(orientedX(a), minX, maxX) + scale(orientedY(a), minY, maxY);
      const scoreB = scale(orientedX(b), minX, maxX) + scale(orientedY(b), minY, maxY);
      return scoreB - scoreA || orientedX(b) - orientedX(a) || orientedY(b) - orientedY(a) || a.label.localeCompare(b.label);
    })[0];
  }
  function format(value) {
    return Number(value).toLocaleString(undefined, {maximumFractionDigits: 3});
  }
  function drawTable(selected, state) {
    const body = table.querySelector('tbody');
    body.replaceChildren();
    selected.forEach(point => {
      const row = document.createElement('tr');
      [point.concurrency, point.label, format(point.x), format(point.y)].forEach(value => {
        const cell = document.createElement('td');
        cell.textContent = value;
        row.appendChild(cell);
      });
      body.appendChild(row);
    });
  }
  function attachDashboardClick() {
    if (plot.__paretoDashboardClick || typeof plot.on !== 'function') return;
    plot.on('plotly_click', eventData => {
      const point = eventData?.points?.[0];
      const metadata = point?.customdata;
      if (!Array.isArray(metadata)) return;
      const cfg = metadata[0];
      const concurrency = metadata[1];
      if (cfg && concurrency) openDashboard(cfg, concurrency);
    });
    plot.__paretoDashboardClick = true;
  }
  function draw() {
    const state = {
      xMetric: fields.xMetric.value, xStat: fields.xStat.value, xNorm: fields.xNorm.value, xGoal: fields.xGoal.value,
      yMetric: fields.yMetric.value, yStat: fields.yStat.value, yNorm: fields.yNorm.value, yGoal: fields.yGoal.value
    };
    const selectedConcurrencies = activeConcurrencies();
    const points = [];
    CONFIG_KEYS.forEach(cfg => selectedConcurrencies.forEach(concurrency => {
      const x = finiteValue(cfg, concurrency, state.xMetric, state.xStat, state.xNorm);
      const y = finiteValue(cfg, concurrency, state.yMetric, state.yStat, state.yNorm);
      if (x == null || y == null) return;
      points.push({cfg, concurrency, n:Number(concurrency.replace(/^c/, '')), label:CONFIGS[cfg].label, x, y});
    }));
    const selected = selectedConcurrencies.map(concurrency => chooseBest(points.filter(point => point.concurrency === concurrency), state)).filter(Boolean);
    selected.sort((a, b) => a.n - b.n);
    const xTitle = metricLabel(state.xMetric) + (state.xNorm !== 'none' ? ' ' + normSuffix(state.xNorm) : '');
    const yTitle = metricLabel(state.yMetric) + (state.yNorm !== 'none' ? ' ' + normSuffix(state.yNorm) : '');
    const traces = [{
      x: points.map(point => point.x), y: points.map(point => point.y), mode:'markers', name:'All rerun points',
      customdata: points.map(point => [point.cfg, point.concurrency, point.label]),
      hovertemplate:'%{customdata[1]}<br>%{customdata[2]}<br>%{x:.4g}<br>%{y:.4g}<extra></extra>',
      marker:{color:'#59616b', size:8, opacity:0.45}
    }];
    if (selected.length) traces.push({
      x:selected.map(point => point.x), y:selected.map(point => point.y), text:selected.map(point => point.concurrency),
      customdata:selected.map(point => [point.cfg, point.concurrency, point.label]),
      mode:selected.length >= 2 ? 'lines+markers+text' : 'markers+text', textposition:'top center',
      name:selected.length >= 2 ? 'Selected front sequence' : 'Selected concurrency point',
      hovertemplate:'%{text}<br>%{customdata[2]}<br>%{x:.4g}<br>%{y:.4g}<extra></extra>',
      line:{color:palette[0], width:4}, marker:{color:palette[0], size:13, symbol:'diamond'}
    });
    const layout = {
      ...LAYOUT_DEFAULTS, title:{text:'Reduced concurrency sequence', font:{size:18}},
      xaxis:{...LAYOUT_DEFAULTS.xaxis, ...fixedAxis(points.map(point => point.x)), title:{text:xTitle}},
      yaxis:{...LAYOUT_DEFAULTS.yaxis, ...fixedAxis(points.map(point => point.y)), title:{text:yTitle}},
      showlegend:true, legend:{orientation:'h', y:1.08, x:0}, hovermode:'closest',
      margin:{...LAYOUT_DEFAULTS.margin, t:72, b:72}, plot_bgcolor:'#171a1e', paper_bgcolor:'#171a1e'
    };
    const rendered = plot.data ? Plotly.react(plot, traces, layout, {responsive:true, displaylogo:false}) : Plotly.newPlot(plot, traces, layout, {responsive:true, displaylogo:false});
    Promise.resolve(rendered).then(attachDashboardClick);
    drawTable(selected, state);
    const first = selectedConcurrencies[0], last = selectedConcurrencies[selectedConcurrencies.length - 1];
    summary.textContent = 'Range ' + first + '–' + last + ': ' + points.length + ' usable rerun/concurrency points reduced to ' + selected.length + ' selected concurrency point' + (selected.length === 1 ? '' : 's') + '. Each selected point may come from a different rerun.';
  }
  fields.xMetric.addEventListener('change', () => {
    metricState.xMetric = fields.xMetric.value;
    metricState.xStat = resetSelect(fields.xStat, statOptionsForMetric(metricState.xMetric), metricState.xStat);
    metricState.xNorm = resetSelect(fields.xNorm, normOptionsForMetric(metricState.xMetric, 'x'), metricState.xNorm);
    metricState.xGoal = defaultGoal(metricState.xMetric);
    fields.xGoal.value = metricState.xGoal;
    draw();
  });
  fields.yMetric.addEventListener('change', () => {
    metricState.yMetric = fields.yMetric.value;
    metricState.yStat = resetSelect(fields.yStat, statOptionsForMetric(metricState.yMetric), metricState.yStat);
    metricState.yNorm = resetSelect(fields.yNorm, normOptionsForMetric(metricState.yMetric, 'y'), metricState.yNorm);
    metricState.yGoal = defaultGoal(metricState.yMetric);
    fields.yGoal.value = metricState.yGoal;
    draw();
  });
  [fields.xStat, fields.xNorm, fields.xGoal, fields.yStat, fields.yNorm, fields.yGoal, rangeFrom, rangeTo].forEach(select => select.addEventListener('change', draw));
  draw();
})();
</script>
'''


def monitoring_overlay(root: Path, runs: list[dict[str, Any]], *, save_file: bool = True) -> bytes | None:
    """Build one relative-time dashboard comparison from all saved concurrencies."""
    paths: list[tuple[int, str, Path]] = []
    for data in runs:
        try:
            concurrency = int(data["metadata"]["concurrency"])
        except (KeyError, TypeError, ValueError):
            continue
        path = data["directory"] / "dashboard.html"
        if path.is_file():
            # The concurrency alone is not a unique identity in a merged
            # Prometheus view: reruns (and even separate sweeps) can contain
            # the same c<N>. Keep both the sweep and c<N>-r<M> visible so
            # colors, legend items, and filters cannot collide.
            source = sweep_key(data)
            paths.append((concurrency, f"{source} / {run_label(data)}", path))
    if len(paths) < 2:
        return None
    overlay_path = support_file("overlay_dashboards.py")
    spec = importlib.util.spec_from_file_location("agentx_dashboard_overlay", overlay_path)
    if spec is None or spec.loader is None:
        return None
    overlay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(overlay)
    file_data = []
    labels = []
    notes = []
    for concurrency, label, path in sorted(paths, key=lambda item: (item[0], item[1])):
        panels, rows = overlay.extract_data(path)
        queries = [query for panel in panels.values() for query in panel.get("queries", [])]
        if not any(series.get("values") for query in queries for series in query.get("series", [])):
            continue
        has_vllm_data = any(
            "vllm:" in query.get("expr", "") and
            any(series.get("values") for series in query.get("series", []))
            for query in queries
        )
        if not has_vllm_data:
            notes.append(f"{label}: no vLLM metrics were scraped; only other available monitoring series are shown.")
            label += " (no vLLM metrics)"
        file_data.append((panels, rows, label))
        labels.append(label)
    if len(file_data) < 2:
        return None
    merged = overlay.merge(file_data)
    page = overlay.generate_html(
        merged, file_data[0][1], labels,
        str(Path(__file__).with_name("plotly-basic-2.35.2.min.js.gz")),
        notes=notes,
    )
    if save_file:
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

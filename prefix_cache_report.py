#!/usr/bin/env python3
"""Prefix-cache hit rate report: theoretical vs actual, split by prefill/decode.

Compares three independent signals collected during a benchmark run:

1. Theoretical (client-side, trace-based) — aiperf's `theoretical_prefix_cache_hit`,
   computed purely from the conversation trace structure assuming an infinite cache.
2. Usage-based (client-side, per-request) — aiperf's `overall_usage_prompt_cache_read_pct`,
   read from the API response's `usage.prompt_tokens_details.cached_tokens` field
   (requires the server to be launched with --enable-prompt-tokens-details). In
   disaggregated prefill/decode serving this reflects only whichever role's response
   is actually returned to the client (decode), and cannot be split by role.
3. Server-side (actual, Prometheus) — vLLM's own `prefix_cache_hits`/`queries` (local,
   in-engine radix-tree reuse) and `external_prefix_cache_hits`/`queries` (KV received
   via cross-instance transfer, e.g. the prefill->decode NIXL handoff). Unlike #2, this
   *can* be split by role, since prefill and decode pods are independently scrapable
   (distinguished by the `job` label: vllm-prefill / vllm-decode).

Run inside the orchestrator pod (same pattern as export_dashboard.py) so that the
in-cluster Grafana URL resolves and the query proxies through to Prometheus.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from export_dashboard import (  # noqa: E402
    derive_pod_regex,
    grafana_request,
    prom_regex_escape,
    query_prometheus,
)

ROLES = ("vllm-prefill", "vllm-decode", "vllm-aggregate")

COUNTER_PAIRS = {
    "local": ("vllm:prefix_cache_hits_total", "vllm:prefix_cache_queries_total"),
    "external": ("vllm:external_prefix_cache_hits_total", "vllm:external_prefix_cache_queries_total"),
}


def _extract_scalar(result):
    """Pull the last numeric value out of a query_prometheus() range-query response."""
    if result.get("status") != "success":
        return 0.0
    series = result.get("data", {}).get("result", [])
    if not series:
        return 0.0
    values = series[0].get("values", [])
    if not values:
        return 0.0
    try:
        return float(values[-1][1])
    except (TypeError, ValueError, IndexError):
        return 0.0


def _build_selector(pod_regex, job):
    labels = []
    if pod_regex:
        labels.append(f'pod=~"{prom_regex_escape(pod_regex)}"')
    if job:
        labels.append(f'job="{job}"')
    return "{" + ", ".join(labels) + "}" if labels else ""


def query_increase(grafana_url, auth, ds_id, metric, pod_regex, job, start, end):
    selector = _build_selector(pod_regex, job)
    window = max(1, int(end - start))
    expr = f"sum(increase({metric}{selector}[{window}s]))"
    result = query_prometheus(grafana_url, auth, ds_id, expr, end, end, 15)
    return _extract_scalar(result)


def query_avg_over_time(grafana_url, auth, ds_id, metric, pod_regex, job, start, end):
    """Average of a gauge over exactly [start, end], not since pod start.

    avg_over_time(metric[window_s]) evaluated at time=end only considers
    samples within that lookback window, mirroring query_increase()'s framing
    for counters.
    """
    selector = _build_selector(pod_regex, job)
    window = max(1, int(end - start))
    expr = f"avg(avg_over_time({metric}{selector}[{window}s]))"
    result = query_prometheus(grafana_url, auth, ds_id, expr, end, end, 15)
    return _extract_scalar(result)


def role_pod_count(grafana_url, auth, ds_id, pod_regex, job, start, end):
    """Whether any pods are actually scraped under this job/pod filter.

    Aggregated deployments only run a single (decode-labeled) role, so the
    vllm-prefill job has zero matching targets; `up` is present for every
    scraped target regardless of whether it has served any requests yet,
    making it a reliable existence probe independent of traffic volume.
    """
    selector = _build_selector(pod_regex, job)
    result = query_prometheus(grafana_url, auth, ds_id, f"count(up{selector})", end, end, 15)
    return int(_extract_scalar(result))


def pct(hits, queries):
    if not queries:
        return None
    return 100.0 * min(hits, queries) / queries


def fmt_pct(value):
    return f"{value:.1f}%" if value is not None else "N/A"


def build_report(args):
    ds_info = grafana_request(args.grafana_url, "/api/datasources/uid/PBFA97CFB590B2093", args.auth)
    ds_id = ds_info["id"]

    pod_regex = derive_pod_regex(args.deployment, args.pod_regex)
    start, end = float(args.start), float(args.end)

    present_roles = [
        role for role in ROLES
        if role_pod_count(args.grafana_url, args.auth, ds_id, pod_regex, role, start, end) > 0
    ]

    per_role = {}
    for role in present_roles:
        role_stats = {}
        for kind, (hits_metric, queries_metric) in COUNTER_PAIRS.items():
            hits = query_increase(args.grafana_url, args.auth, ds_id, hits_metric, pod_regex, role, start, end)
            queries = query_increase(args.grafana_url, args.auth, ds_id, queries_metric, pod_regex, role, start, end)
            role_stats[kind] = pct(hits, queries)
        kv_usage_ratio = query_avg_over_time(
            args.grafana_url, args.auth, ds_id, "vllm:kv_cache_usage_perc", pod_regex, role, start, end
        )
        role_stats["kv_cache_usage_avg_pct"] = kv_usage_ratio * 100.0
        per_role[role] = role_stats

    is_agg = set(present_roles) <= {"vllm-decode", "vllm-aggregate"} and len(present_roles) == 1
    deployment_kind = "aggregated" if is_agg else "disaggregated" if present_roles else "unknown"
    report_data = {
        "theoretical_pct": args.theoretical_pct,
        "usage_overall_pct": args.usage_overall_pct,
        "deployment_kind": deployment_kind,
        "roles": {
            role.replace("vllm-", ""): {
                "local_hit_pct": per_role[role]["local"],
                "external_hit_pct": per_role[role]["external"],
                "kv_cache_usage_avg_pct": per_role[role]["kv_cache_usage_avg_pct"],
            }
            for role in present_roles
        },
    }

    lines = []
    lines.append(f"=== Prefix Cache Hit Rate Report: {args.name} ===")
    lines.append(f"Time range: {int(start)} -> {int(end)} ({int(end - start)}s), pod filter: {pod_regex}")
    lines.append("")
    lines.append("Client-side (aiperf):")
    lines.append(f"  Theoretical (trace-based, infinite cache)      : {fmt_pct(args.theoretical_pct)}")
    lines.append(f"  Usage-based overall (decode-reported)          : {fmt_pct(args.usage_overall_pct)}")
    lines.append("")
    if not present_roles:
        lines.append(f"Server-side (Prometheus, actual): no vLLM pods matched pod filter {pod_regex!r}")
    else:
        kind_label = "aggregated (single role)" if is_agg else "disaggregated"
        lines.append(f"Server-side (Prometheus, actual), by role [{kind_label}]:")
        lines.append(f"{'':<12}{'Local (in-engine reuse)':<28}{'External (KV transfer)':<28}{'KV Cache Usage (avg)':<24}")
        for role in present_roles:
            label = role.replace("vllm-", "").capitalize()
            lines.append(
                f"  {label:<10}{fmt_pct(per_role[role]['local']):<28}{fmt_pct(per_role[role]['external']):<28}"
                f"{fmt_pct(per_role[role]['kv_cache_usage_avg_pct']):<24}"
            )
    lines.append("")
    if is_agg:
        agg_role_label = present_roles[0].replace("vllm-", "").capitalize()
        lines.append(
            f"NOTE: aggregated deployment (single {agg_role_label}-labeled role, no separate\n"
            "prefill pods) — the Local row above is the true cache-effectiveness\n"
            "signal to compare against the theoretical rate."
        )
    else:
        lines.append(
            "NOTE: the usage-based percentage above reflects only the role whose response\n"
            "is returned to the client (decode, in disaggregated serving) and cannot be\n"
            "split by role from the client side. Use Prefill/Local above — the rate at\n"
            "which prefill reuses its own radix-tree cache across requests — for the\n"
            "genuine comparison against the theoretical rate; Decode/Local is expected\n"
            "to be near-zero since decode receives its KV via external transfer rather\n"
            "than computing it locally."
        )
    return "\n".join(lines), report_data


def main():
    parser = argparse.ArgumentParser(description="Prefix-cache hit rate report (theoretical vs actual, prefill/decode split)")
    parser.add_argument("--grafana-url", default="http://localhost:3001")
    parser.add_argument("--auth", default="admin:admin", help="user:password")
    parser.add_argument("--deployment", default=".*")
    parser.add_argument("--pod-regex", default="")
    parser.add_argument("--start", required=True, help="Start time (epoch seconds)")
    parser.add_argument("--end", required=True, help="End time (epoch seconds)")
    parser.add_argument("--name", default="run", help="Label for the report header")
    parser.add_argument("--theoretical-pct", default=None, help="omit or leave blank if unavailable")
    parser.add_argument("--usage-overall-pct", default=None, help="omit or leave blank if unavailable")
    parser.add_argument("--output", "-o", help="Text output file path (default: stdout only)")
    parser.add_argument("--output-json", help="Machine-readable JSON output file path")
    args = parser.parse_args()

    def _to_float(v):
        try:
            return float(v) if v not in (None, "") else None
        except ValueError:
            return None

    args.theoretical_pct = _to_float(args.theoretical_pct)
    args.usage_overall_pct = _to_float(args.usage_overall_pct)

    report, report_data = build_report(args)
    print(report)
    if args.output:
        with open(args.output, "w") as f:
            f.write(report + "\n")
    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(report_data, f, indent=2)


if __name__ == "__main__":
    main()

# AgentX-MVP Benchmark

AIPerf AgentX-MVP benchmark harness for llm-d/manifesto deployments with prefill/decode disaggregation.

The service branch exposes the same bounded benchmark tools over MCP
`2025-11-25` and `2026-07-28`; see `docs/agentx-service.md`.

The repository also ships a bounded, durable MCP benchmark service. Start
with [the service contract and deployment guide](docs/agentx-service.md).

## Live llm-d sweep

Use this when the question is: “how does the model deployed in this namespace
perform right now?” It is separate from the typed service and legacy manifesto
paths: it discovers the live model and topology, records the deployed vLLM
branch/SHA, captures the serving-pod and AIPerf Job YAML, and submits one
autonomous Kubernetes Job for the entire concurrency sweep.

```bash
just live-aiperf 1,4,8,16
just live-aiperf-report
```

The first command returns after submitting the Job, so the laptop can close.
The second command downloads `~/Downloads/aiperf-history.html`; it rebuilds the
self-contained report from persisted artifacts for the newest completed Job.

## Prerequisites

- `kubectl` configured for your cluster
- Kueue installed in the cluster
- `AGENTX_API_TOKEN` set for service deployment and MCP clients
- an operator-provisioned ReadWriteMany results PVC matching the service
  manifest and operator configuration (`agentx-results` by default)

The historical GB200 helpers additionally use a legacy `.env` file with:

```
NAMESPACE=vllm
MANIFESTO_ROOT=$HOME/code/llm-manifesto
MODEL_SPEC=models/deepseek-v4/3P-EP8-1D-EP8.yaml
MANIFESTO_CLUSTER=clusters/oci-gb200.yaml
MANIFESTO_USER=$USER
KUEUE_QUEUE=nightly-eval
LUSTRE_CLAIM=lustre-pvc-vllm
LUSTRE_PREFIX=/mnt/lustre/agentx-mvp
```

These legacy defaults target `deepseek-ai/DeepSeek-V4-Pro` on the smallest
GB200/NVL72 manifesto profile and use the existing `vllm` namespace.

Legacy report/dashboard helpers read Grafana and Prometheus from:

```bash
MONITORING_NAMESPACE=vllm
PROMETHEUS_NAMESPACE=$MONITORING_NAMESPACE
GRAFANA_NAMESPACE=$MONITORING_NAMESPACE
```

Override these only when manifesto installs the monitoring stack somewhere else.

## Quick start

```bash
just setup           # deploy the authenticated typed service
just check           # verify the model endpoint is reachable
just run             # submit AGENTX_REQUEST to the in-cluster typed service
just legacy-run 256 900 # explicitly legacy positional workflow
just smoke           # fast Kueue Job plumbing test (~60s, invalid result)
just orchestrator-run      # submit to the durable in-cluster service
just logs            # tail typed service logs
just shell           # shell into the typed service
just clean           # remove typed Jobs/service resources; preserve PVC data
```

The orchestrator image contains this harness at `/workspace/agentx-mvp` and
`llm-manifesto` at `/workspace/llm-manifesto`; `just orchestrator-run` does not
copy source trees into the pod. Build it with `just orchestrator-build`.

The typed service has a separate, non-root runtime image. Build it with
`just agentx-service-build`, publish it with `just agentx-service-push`, and
select the deployed reference with `AGENTX_SERVICE_IMAGE`.

## Legacy model deployment helpers

```bash
just legacy-setup-kueue # install/update the historical GB200 queue objects
just start-model     # deploy the llm-manifesto spec
just stop-model      # tear down the manifesto deployment
```

Model deployment is Kueue-aware by default. `just start-model` renders the
configured `llm-manifesto` spec and labels each rendered `LeaderWorkerSet` with
`kueue.x-k8s.io/queue-name: nightly-eval`. Override with `KUEUE_QUEUE=...`.
It does not call the `llm-manifesto` `just start` recipe.

## Legacy benchmark result layout

Benchmarks run as Kueue-managed `batch/v1` Jobs, not as `kubectl exec` commands
into a long-lived AIPerf pod. Each Job mounts `LUSTRE_CLAIM` at `/mnt/lustre`
and writes artifacts to:

```bash
$LUSTRE_PREFIX/$MANIFESTO_USER/<result-directory>
```

The local or orchestrator-side result directory receives a copy for report generation,
but the PVC path is the durable source of truth.

## Result Directories

By default, orchestrated sweeps write under:

```bash
results/<UTC timestamp>_<manifesto user>_<spec slug>_<duration>s/
```

For example:

```bash
results/20260713T210000Z_tms_3p-ep8-1d-ep8_900s/
```

Inside that run root, each config gets `results_<instance>/`, and each
concurrency level gets `results_<instance>_c<concurrency>/`. The run root also
contains `interactivity_vs_throughput.html`.

## Sweep

The public sweep submits the strict operator/request JSON through the typed
controller:

```bash
just sweep
AGENTX_REQUEST=examples/kimi-k3-a100-smoke.json just sweep
```

Historical manifesto-managed positional sweeps remain explicit compatibility
paths:

```bash
just legacy-sweep "$(just --quiet run-dir 900)" 900
```

Each sweep produces result directories like `results/<run>/results_$USER-wide-ep-3p-ep8-1d-ep8/results_$USER-wide-ep-3p-ep8-1d-ep8_c64/`, `results/<run>/results_$USER-wide-ep-3p-ep8-1d-ep8/results_$USER-wide-ep-3p-ep8-1d-ep8_c256/`, etc. Each run directory contains:
- `profile_export_aiperf.json` — benchmark metrics
- `profile_export.jsonl` — per-request data
- `vllm_image.txt` — vLLM container image tag
- `vllm_fingerprint.txt` — vLLM `system_fingerprint` from the API

The parent config directory contains `manifest.yaml`, the monolithic rendered manifesto manifest used for the run.

## Grafana dashboard export

Export Grafana dashboards for benchmark result directories. Automatically extracts the exact time range each run executed (from `profile_export_aiperf.json` timestamps) and queries Prometheus for that window.

```bash
# Export dashboards for specific result directories
just scrape-grafana results/<run>/results_$USER-wide-ep-3p-ep8-1d-ep8/results_$USER-wide-ep-3p-ep8-1d-ep8_c64

# Or use the script directly for a single time range
python3 export_dashboard.py single --start now-30m --end now -o report.html
```

Each result directory gets a self-contained `dashboard.html` with interactive Plotly charts mirroring the Grafana dashboard.

## Dashboard overlay / comparison

Overlay multiple dashboard exports onto the same charts for side-by-side comparison across concurrency levels. X-axis is rebased to relative time (seconds from start) so runs that happened at different absolute times align.

```bash
# Overlay three concurrency levels — auto-labeled from filenames
python3 overlay_dashboards.py results/<run>/results_$USER-wide-ep-3p-ep8-1d-ep8/results_$USER-wide-ep-3p-ep8-1d-ep8_c64/dashboard.html results/<run>/results_$USER-wide-ep-3p-ep8-1d-ep8/results_$USER-wide-ep-3p-ep8-1d-ep8_c256/dashboard.html

# Custom labels
python3 overlay_dashboards.py c64.html c256.html --label "concurrency=64" --label "concurrency=256"
```

Each concurrency level gets a distinct color across all panels.

## vLLM version capture

Capture the vLLM version from a running deployment:

```bash
just vllm-version results/<run>/results_$USER-wide-ep-3p-ep8-1d-ep8
```

This is called automatically during sweeps.

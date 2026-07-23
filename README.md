# AgentX-MVP Benchmark

AIPerf AgentX-MVP benchmark harness for llm-d/manifesto deployments with prefill/decode disaggregation.

## Prerequisites

- `kubectl` configured for your cluster
- Kueue installed in the cluster
- `lustre-pvc-vllm` available in the benchmark namespace
- `.env` file with:
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

Defaults target `deepseek-ai/DeepSeek-V4-Pro` on the smallest GB200/NVL72 manifesto profile and use only the existing `vllm` namespace.

For report/dashboard collection, the harness reads Grafana and Prometheus from:

```bash
MONITORING_NAMESPACE=vllm
PROMETHEUS_NAMESPACE=$MONITORING_NAMESPACE
GRAFANA_NAMESPACE=$MONITORING_NAMESPACE
```

Override these only when manifesto installs the monitoring stack somewhere else.

## Quick start

```bash
just setup           # install queue manifests and deploy the orchestrator
just check           # verify the model endpoint is reachable
just run             # run a Kueue-managed AIPerf Job
just run 256 900     # override concurrency / duration
just smoke           # fast Kueue Job plumbing test (~60s, invalid result)
just orchestrator-run      # run the sweep from the in-cluster orchestrator
just logs            # tail orchestrator logs
just shell           # shell into the orchestrator
just clean           # delete benchmark jobs and the orchestrator pod
```

The orchestrator image contains this harness at `/workspace/agentx-mvp` and
`llm-manifesto` at `/workspace/llm-manifesto`. `just orchestrator-run` syncs your local
`MANIFESTO_ROOT/models/` and `clusters/` plus the agentx-mvp `Justfile` into the pod
before starting the sweep. Rebuild the image only when manifesto Python/templates change.
Override with `ORCHESTRATOR_FORCE_SYNC=true`. Build the image with `just orchestrator-build`.

## Model Deployment

```bash
just setup-kueue     # install/update the GB200 Kueue queue objects
just start-model     # deploy the llm-manifesto spec
just stop-model      # tear down the manifesto deployment
```

Model deployment is Kueue-aware by default. `just start-model` renders the
configured `llm-manifesto` spec and labels each rendered `LeaderWorkerSet` with
`kueue.x-k8s.io/queue-name: nightly-eval`. Override with `KUEUE_QUEUE=...`.
It does not call the `llm-manifesto` `just start` recipe.

## Benchmark Jobs

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

Run the benchmark across concurrency levels for the configured manifesto spec:

```bash
just sweep "$(just --quiet run-dir 900)"

# Custom duration (default 900s)
just sweep "$(just --quiet run-dir 1200)" 1200
```

Each sweep produces result directories like `results/<run>/results_$USER-wide-ep-3p-ep8-1d-ep8/results_$USER-wide-ep-3p-ep8-1d-ep8_c64/`, `results/<run>/results_$USER-wide-ep-3p-ep8-1d-ep8/results_$USER-wide-ep-3p-ep8-1d-ep8_c256/`, etc. Each run directory contains:
- `profile_export_aiperf.json` — benchmark metrics
- `profile_export.jsonl` — per-request data
- `vllm_image.txt` — vLLM container image tag
- `vllm_fingerprint.txt` — vLLM `system_fingerprint` from the API

The parent config directory contains `manifest.yaml`, the monolithic rendered manifesto manifest used for the run.

## Grafana dashboard export

Browse live dashboards during a run:

```bash
just grafana    # background port-forward to http://localhost:3000
```

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

## Crash log recovery

`just dump-logs` only captures `kubectl logs` for each pod's *current*
container instance. When a pod crashloops, that only shows the fresh
post-restart run — the log from the run that actually crashed is gone from
`kubectl logs` by the time you go looking.

vLLM's launch script (llm-manifesto `manifesto/launch.py`) also tees every
run's stdout into its own timestamped file on the shared Lustre PVC
(`$(cd $MANIFESTO_ROOT && uv run manifesto log-path <spec> --role <role>)/${HOSTNAME}_<timestamp>.log`),
so those never get lost on restart. `just dump-crash-logs` reads that
directory back through a tiny dedicated pod (`just logs-dev-up`) that only
mounts `lustre-pvc-vllm` at `/mnt/lustre` — no RBAC, no GPU, and no
dependency on decode/prefill pods being up or the benchmark-orchestrator
Deployment's redeploy lifecycle:

```bash
just dump-crash-logs results/<run>   # auto-deploys the log-reader pod if needed;
                                      # writes results/<run>/lustre-logs/{decode,prefill}/
just logs-dev-down                   # tear down the log-reader pod when done
```

## vLLM version capture

Capture the vLLM version from a running deployment:

```bash
just vllm-version results/<run>/results_$USER-wide-ep-3p-ep8-1d-ep8
```

This is called automatically during sweeps.

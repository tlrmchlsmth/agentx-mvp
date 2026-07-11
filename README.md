# AgentX-MVP Benchmark

AIPerf AgentX-MVP benchmark harness for llm-d optimized-baseline deployments with prefill/decode disaggregation.

## Prerequisites

- `kubectl` configured for your cluster
- `.env` file with:
  ```
  NAMESPACE=ecrncevi-dev
  GLM_PD_PREFILL=path/to/prefill.yaml
  GLM_PD_DECODE=path/to/decode.yaml
  ```
- Grafana port-forwarded to `localhost:3001` (for dashboard export)

## Quick start

```bash
just deploy          # deploy the aiperf runner pod
just check           # verify the model endpoint is reachable
just run             # run benchmark (default: concurrency=64, duration=900s)
just run 16 900      # override concurrency / duration
just smoke           # fast plumbing test (~60s, marks result invalid)
just results         # copy artifacts to ./results
just logs            # tail runner logs
just shell           # shell into the runner
just clean           # delete the runner pod
```

## P/D deployment

Config format: `PR:PW:DR:DW` (prefill_replicas:prefill_width:decode_replicas:decode_width)

```bash
just start-pd 2 1 1 1  # 2 prefill replicas (1 node each), 1 decode replica (1 node)
just start-pd 1 2 1 1  # 1 prefill replica spanning 2 nodes (wide EP), 1 decode (1 node)
just stop-pd            # tear down prefill/decode pods
```

## Sweep

Run the full benchmark across multiple configurations and concurrency levels (1, 16, 64, 256):

```bash
# Single config
just sweep "1:1:1:2"

# Multiple configs — each gets deployed, swept, then torn down
just sweep "1:1:1:2 2:1:1:1 2:1:2:1"

# Custom duration (default 900s)
just sweep "1:1:1:2" 1200
```

Each sweep produces result directories like `results_p1w1_d1w2_c1/`, `results_p1w1_d1w2_c4/`, etc. containing:
- `profile_export_aiperf.json` — benchmark metrics
- `profile_export.jsonl` — per-request data
- `prefill.yaml` / `decode.yaml` — pod specs at time of run
- `vllm_image.txt` — vLLM container image tag
- `vllm_fingerprint.txt` — vLLM `system_fingerprint` from the API

## Grafana dashboard export

Export Grafana dashboards for benchmark result directories. Automatically extracts the exact time range each run executed (from `profile_export_aiperf.json` timestamps) and queries Prometheus for that window.

```bash
# Export dashboards for specific result directories
just scrape-grafana results_p1w1_d1w2_c1 results_p1w1_d1w2_c4

# Or use the script directly for a single time range
python3 export_dashboard.py single --start now-30m --end now -o report.html
```

Each result directory gets a self-contained `dashboard.html` with interactive Plotly charts mirroring the Grafana dashboard.

## Dashboard overlay / comparison

Overlay multiple dashboard exports onto the same charts for side-by-side comparison (e.g. different concurrency levels or configs). X-axis is rebased to relative time (seconds from start) so runs that happened at different absolute times align.

```bash
# Overlay three concurrency levels — auto-labeled from filenames
python3 overlay_dashboards.py results_p1w1_d1w2_c1/dashboard.html results_p1w1_d1w2_c4/dashboard.html results_p1w1_d1w2_c16/dashboard.html

# Custom labels
python3 overlay_dashboards.py c1.html c4.html --label "concurrency=1" --label "concurrency=4"
```

Each concurrency level gets a distinct color across all panels.

## vLLM version capture

Capture the vLLM version from a running deployment:

```bash
just vllm-version results_p1w1_d1w2
```

This is called automatically during sweeps.

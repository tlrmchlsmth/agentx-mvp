# AgentX benchmark service

The service turns the AgentX-MVP AIPerf sweep into a bounded, durable
controller. Callers select operator-owned names; they cannot provide URLs,
images, paths, manifests, AIPerf arguments, or shell. Every benchmark is
planned before it is admitted as one suspended, CPU-only Kubernetes Job per
concurrency. Planning reads the actual LocalQueue and ClusterQueue, fails unless
the ClusterQueue is Active and covers every requested resource, and verifies
healthy, unique Prometheus targets. Kueue owns unsuspension.

## Request schema

Both `plan_agentx_benchmark` and `submit_agentx_benchmark` accept exactly this
object (`additionalProperties: false`):

| Field | Type and bound |
| --- | --- |
| `logical_model_target` | DNS-like operator key, 1-63 characters |
| `served_model_name` | 1-160 characters, `[A-Za-z0-9._/-]` |
| `scenario` | exactly `agentx-mvp` |
| `concurrencies` | 1-8 unique integers, each 1-2048 |
| `duration_seconds` | integer, 60-7200; values below the operator validity minimum are smoke-only |
| `retries` | integer, 0-5; retries after the initial attempt |
| `monitoring_profile` | DNS-like operator key |
| `local_queue` | DNS-like operator key allowed for the selected target |
| `result_label` | Kubernetes-label-safe string, 1-63 characters |
| `vdp_workstream_id` | optional bounded identifier, 1-128 characters |

Unknown fields are errors. The other tool schemas are:

- `list_agentx_benchmarks`: optional `state` enum and `limit` 1-100.
- `get_agentx_benchmark`, `cancel_agentx_benchmark`,
  `list_agentx_artifacts`, and `get_agentx_report`: exactly one `run_id`, a
  24-character lowercase hexadecimal identifier.

`plan_agentx_benchmark` reports queue/resource coverage, the effective target,
Job and worst-case attempt counts, estimated deadline, durable destination,
monitoring sources, and scenario validity without writing state or creating
Kubernetes resources. Planning performs read-only LocalQueue and
ClusterQueue inspection plus target and monitoring health checks.

## Operator configuration

The JSON document is strict and has these required sections:

- `aiperf_image`, optional `hf_token_secret_name`, and
  `max_context_length`.
- `targets`: logical keys mapped to `endpoint_url`, allowed
  `served_model_names`, allowed LocalQueues, pinned `model_revision`,
  digest-pinned `vllm_image`, and optional `vllm_fingerprint`. The image must
  match the deployed model; runtime warmup/AIPerf fingerprints take precedence
  in the report.
- `service_namespace`: the one namespace containing the service, PVC,
  LocalQueues, and benchmark Jobs. Cross-namespace queue configuration is
  rejected.
- `queues`: LocalQueue keys mapped to that namespace and explicit CPU, memory,
  and ephemeral-storage requests and limits. Coverage, flavors, and quota are
  read from Kueue rather than asserted by configuration.
- `monitoring_profiles`: fixed server metrics, GPU telemetry, Prometheus, and
  Grafana URLs; bounded PromQL; expected server/GPU target counts; and capture
  queries. Planning rejects unhealthy, missing, or duplicate targets.
- `storage`: PVC name, absolute mount path, and scoped runs subdirectory.
- `limits`: admission/runtime bounds, MCP/profile/log/file/attempt limits,
  terminal-run retention count/age, and scenario-valid duration.

See [the Kimi K3 A100 example](../examples/operator-config.kimi-k3-a100.json).
Replace its expected target counts and PromQL with the rendered deployment's
actual per-rank PodMonitor coverage before deployment; the example values are
fail-closed placeholders.
The service and AIPerf Jobs must mount the same PVC at the configured path.
The supplied manifest and Kimi example use the storage-neutral
`agentx-results` claim mounted at `/mnt/agentx`; operators may use any durable
ReadWriteMany storage implementation by changing the claim and mount path in
both places. The PVC contains atomic state records, immutable attempt trees,
canonical successful artifacts, and reports, so a replacement Pod reconstructs
all non-terminal runs from Kubernetes. A persisted-but-missing Job is recreated
idempotently; a transient observation error remains retryable.
The controller mounts the PVC root, while each benchmark Job receives only its
pre-created attempt directory through a Kubernetes `subPath`; workers cannot
read or modify controller state, other attempts, canonical data, or reports.

## MCP contract

The only MCP endpoint is authenticated `POST /mcp`. It supports both the
initialize-era `2025-11-25` Streamable HTTP protocol and the stateless
`2026-07-28` protocol. Every request requires `Authorization: Bearer <token>`
from the `agentx-service-auth` Secret.

`2025-11-25` clients initialize normally and then send
`MCP-Protocol-Version: 2025-11-25`. The server does not allocate an optional
session identifier and returns JSON responses rather than an SSE stream.
`2026-07-28` requests supply per-request metadata and matching
`MCP-Protocol-Version`, `Mcp-Method`, and, for calls, `Mcp-Name` headers. Both
protocols expose the same tools and controller behavior.
Explicit browser origins are denied unless `AGENTX_ALLOWED_ORIGINS` names them.

The tools are:

- `plan_agentx_benchmark` (read-only, idempotent)
- `submit_agentx_benchmark` (mutating, non-idempotent)
- `list_agentx_benchmarks` (read-only, idempotent)
- `get_agentx_benchmark` (read-only, idempotent)
- `cancel_agentx_benchmark` (destructive, idempotent)
- `list_agentx_artifacts` (read-only, idempotent)
- `get_agentx_report` (read-only, idempotent)

Raw logs, JSONL request traces, exact-window Prometheus exports, and generated
dashboards remain on the PVC. Per-file, file-count, per-attempt, log, retention,
and MCP bounds are operator configured. An oversized attempt is replaced by a
small durable error manifest. Artifact listings explicitly report truncation.

## Deploy

Build the dedicated, non-root service image with `just agentx-service-build`
(and publish it with `just agentx-service-push`); set `AGENTX_SERVICE_IMAGE` to
the resulting reference. Ensure
the configured results PVC and a ClusterQueue covering `cpu`, `memory`, and
`ephemeral-storage` exist; inspect and apply the supplied LocalQueue; then:

```bash
export NAMESPACE=vllm
export AGENTX_OPERATOR_CONFIG=examples/operator-config.kimi-k3-a100.json
export AGENTX_API_TOKEN='<generated secret value>'
kubectl apply -n "$NAMESPACE" -f examples/localqueue.kimi-k3-a100.yaml
just agentx-service-deploy
kubectl -n "$NAMESPACE" get deploy,svc agentx-service
kubectl -n "$NAMESPACE" get role agentx-service -o yaml
```

The supplied namespace Role has only the operations exercised by the service:
create/get/list/delete Jobs, list their Pods, read Pod logs, get the configured
LocalQueue, and list the Job's Kueue Workload for admission diagnostics. It has
no patch, update, watch, exec, Secret, ConfigMap, Deployment, or PVC access. A
read-only ClusterRole permits only `get` on ClusterQueues for preflight. Health
is `GET /healthz`.
Readiness verifies PVC writes, Kubernetes access, active queues, Prometheus,
and reconciler health. The deploy recipe creates the ClusterRoleBinding for
the selected `NAMESPACE`; the static manifest contains no cluster namespace.
Apply an environment-specific copy
of `examples/network-policy.agentx-service.yaml` only after replacing its documented
selectors and API CIDR.

## Smoke and full requests

Plan the smoke request locally:

```bash
PYTHONPATH=src python3 -m agentx_service.cli \
  --config examples/operator-config.kimi-k3-a100.json \
  plan examples/kimi-k3-a100-smoke.json
```

The 60-second smoke result deliberately reports `scenario.valid: false`. The
full request uses two 900-second concurrency levels:

```bash
AGENTX_REQUEST=examples/kimi-k3-a100-full.json just orchestrator-run
```

An MCP plan call uses the matching service headers:

```bash
curl -sS http://agentx-service.vllm.svc.cluster.local:8080/mcp \
  -H "Authorization: Bearer $AGENTX_API_TOKEN" \
  -H 'content-type: application/json' \
  -H 'MCP-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: tools/call' \
  -H 'Mcp-Name: plan_agentx_benchmark' \
  --data-binary @examples/mcp-plan-kimi-k3-a100.json
```

Initialize-era clients begin with:

```bash
curl -sS http://agentx-service.vllm.svc.cluster.local:8080/mcp \
  -H "Authorization: Bearer $AGENTX_API_TOKEN" \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"example","version":"1"}}}'
```

## Lifecycle, reports, and migration

Submit performs a fixed eight-token warmup and records its exact boundaries.
Admission pending, runtime pending, and running are separate phases. Failed
attempts retain their artifact directories. Only a successful attempt with a
valid, bounded AIPerf profile is atomically promoted. Existing canonical data
is accepted only when its marker and recomputed hashes match the same attempt.
Kueue Workload admission conditions are retained in pending/timeout errors.
Job cleanup is persisted before deletion and completed after restart rather
than rerunning a terminal attempt. Cleanup continues to count against active
capacity until both the Job and its Pods are absent. A failed concurrency does
not discard successful peers: the terminal state becomes `partial`.
Cancellation deletes only the active owned Job and is idempotent.

Reports contain AgentX scenario validity, interactivity/throughput metrics,
every attempt and exact unpadded measurement window, vLLM image/fingerprint,
live target-health and duplicate-scrape evidence, exact-window Prometheus
exports, benchmark stdout, server/GPU telemetry provenance and warnings,
effective request, target, and queue configuration, and SHA-256 artifact
hashes. `gen_interactivity_chart.py` is the shared chart renderer driven by
`report.py`, which also performs Grafana export, dashboard overlay, and report
generation on the canonical directories.

`just run`, `just smoke`, `just sweep`, and `just orchestrator-run` all submit
strict request JSON through the authenticated MCP endpoint of the same
in-cluster typed controller, preserving its single capacity transaction.
Existing scripts
needing positional behavior are explicitly prefixed `legacy-` (`legacy-run`,
`legacy-sweep`, and `legacy-orchestrator-run`) and are not agent-facing.
Current runs are reported through `report.py`; the historical report/dashboard
recipes for legacy `results_*` trees have been removed.

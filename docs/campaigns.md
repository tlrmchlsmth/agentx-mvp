# Overlay benchmark campaigns

A campaign config is one experiment matrix: optional named vLLM builds or runtime
images, concrete llm-d
Kustomize overlays, and AIPerf/nyann sweeps. For branch builds, the runner resolves
each branch to an exact commit and warms the shared wheel cache for **all builds
before deploying any serving overlay**. It then benchmarks every build ×
overlay combination in a dedicated namespace, deploying one at a time and
removing it before the next. One Kueue campaign
queue slot prevents two campaign runners from changing the deployment at once.
The separate benchmark queue admits the child benchmark Jobs; the campaign Job
must never use that queue or it could block its own children.

## Prepare

1. Copy `examples/campaign.example.json`. Set `source.repo` and `source.ref`
   to the llm-d fork and branch/tag/commit that contains the overlays. Set
   `vllm_image` to the exact image reference to run, such as your
   nightly tag or a digest, or a `vllm_images` list to sweep several existing
   images (see below). The campaign applies it to every `vllm` container
   in the selected LeaderWorkerSet or DisaggregatedSet role templates and uses
   it for prebuild Jobs.
   The overlays' `imagePullPolicy` still controls whether a moving tag is
   refreshed; use `Always` for a nightly tag.
   Set `base_url` to the in-cluster `/v1` model API endpoint when the serving
   router is not at the benchmark submitters' default Istio gateway address;
   omit it when the campaign manages the router and the endpoint is derived
   from the release (see [below](#campaign-managed-epp-router)).
   Set
   `build_repo` and list named `builds` with ordered `steps` when comparing vLLM
   branches. The first
   action is `checkout`; later actions can be `merge`, `cherry-pick`,
   `cherry-pick-mN`, or `cherry-pick-parent1`. A build may also include
   `"deepep": {"repo": "https://github.com/your-org/DeepEP.git", "ref": "feature-branch"}`.
   Omit it to keep the runtime image's DeepEP installation. When included,
   the campaign pins that branch to an exact commit, builds its wheel after
   the chosen vLLM is available, and reuses the shared DeepEP wheel cache across compatible builds and
   overlays. The build Job and serving Pods install the same cached wheel.
   Increase `rollout_timeout_seconds` if building both wheels needs more time.
   List concrete Kustomize overlay
   paths relative to llm-d. Use `dimensions` to label MTP, offloading,
   topology, PD size, or other settings in the final report. Each overlay also
   needs its serving Pod selector and expected Pod count. The runner fetches
   llm-d once and resolves every requested vLLM branch once. The configured image
   must be available to the cluster. When an overlay contains an existing
   compatible build recipe, the runner replaces it with
   [`campaign/vllm-wheel-build.sh`](../campaign/vllm-wheel-build.sh). For an
   overlay without a build recipe, it injects
   [`campaign/vllm-source-build.sh`](../campaign/vllm-source-build.sh), a
   `vllm-build-ref` ConfigMap, and a cache volume backed by `results_pvc` into
   each vLLM worker template.
   The prebuild Job and serving Pods therefore use the same versioned recipe.
   The runner starts the build Job before the serving deployment. The Job
   uses that worker's image, GPU resources, and shared build-cache PVC. The
   script validates and installs an existing wheel or builds and caches it on
   a miss. A failed build is recorded before any serving Pods are started.
   The order, actions, and resolved commits enter the wheel cache key. Repeated
   overlays with the same build and runtime reuse the READY wheel. The runner
   does not push an integration branch, so Git write credentials are unnecessary.
2. Each overlay must render a **complete, disposable, namespaced** deployment.
   Resources that already exist are rejected so cleanup cannot delete shared
   infrastructure. Do not put the results PVC, Kueue objects, namespace, or
   shared monitoring stack in these overlays. The runner records the llm-d
   source commit for every overlay. If an overlay also publishes
   `vllm-build-ref`, its vLLM commit is recorded separately.
3. Ensure the results PVC is ReadWriteMany, mounted by both benchmark Jobs and
   the campaign Job, and that the AIPerf and nyann images/secrets are available.
   `campaign-prepare` creates the namespace, both Kueue queues, and that PVC
   (`workload-pvc`, 20Gi, `shared-vast` by default) in one idempotent step.
   The RBAC it installs is a namespace Role that can apply the resource kinds
   used by the overlays and submit benchmark Jobs; extend it only if your
   overlays contain additional kinds.
4. To include Grafana dashboards in the final HTML, set `monitoring` explicitly:

   ```json
   "monitoring": {
     "grafana_url": "http://llmd-grafana.vllm.svc.cluster.local",
     "auth_secret": "llmd-grafana",
     "dashboard_uid": "wideep-overview"
   }
   ```

   The URL must be reachable from the campaign Job. The named Secret must be
   in the campaign namespace with `admin-user` and `admin-password` keys;
   credentials stay out of the JSON. After each AIPerf sweep, the runner uses
   the existing dashboard exporter with the timestamped Job log and saved
   serving Pod names, then the existing AIPerf renderer embeds those dashboards
   into the final report. A Grafana export failure marks that sweep failed
   while preserving its AIPerf measurements. Omit `monitoring` when no Grafana
   export is wanted.

   Grafana may live in another namespace; the laptop then backfills dashboards
   after downloading results. Use `grafana_namespace`, `grafana_service`,
   `auth_secret`, and `dashboard_uid` instead of `grafana_url`: the in-cluster
   runner records benchmark results without Grafana credentials, and
   `campaign-download` exports the dashboards from the laptop through a
   temporary service port-forward after the run. The GLM 5.2 example uses this
   form. Apply its PodMonitor before the benchmark so
   Prometheus collects both prefill and decode metrics:

   ```bash
   kubectl apply -f examples/campaign.glm52-h200-podmonitor.yaml
   ```

## Run in the cluster

```bash
export NAMESPACE=vllm
just campaign-prepare "$NAMESPACE"
just campaign-validate examples/campaign.example.json
just campaign-up examples/campaign.example.json
```

`campaign-up` submits a Kueue-managed Job that runs the entire campaign in the
cluster — deploy overlay, benchmark, tear down, next case — so the run survives
laptop disconnects, sleeps, and Wi-Fi blips. It appends a `-YYYYMMDD-HHMM`
timestamp to the campaign ID so reruns never collide, writes the modified
config beside the output directory as the config of record, and prints
ready-to-paste `campaign-watch`, `campaign-stop`, and `campaign-download`
commands plus `kubectl` status and logs lines. The Job reuses the published
`quay.io/tms/benchmark-orchestrator:amd64` runtime and mounts a compressed
snapshot of the checked-out campaign code through a ConfigMap; no image build
or push is needed. Set `CAMPAIGN_IMAGE` only to use another existing runtime.
Pass `--no-monitoring` to skip the config's Grafana block for one run. After
submitting, the terminal can close: check the Job with
`kubectl -n "$NAMESPACE" get job campaign-<id>` or follow its logs with
`kubectl -n "$NAMESPACE" logs -f job/campaign-<id>`.

The example fork URL, branch, overlay paths, and model label are placeholders;
edit them for the actual deployment before submitting. `campaign-validate`
checks the experiment file locally. It cannot validate paths in the remote
repository or cluster permissions.

To follow progress from the laptop, run the printed watch command:

```bash
just campaign-watch campaign-output/<id>.json
```

It refreshes `campaign-output/<id>/campaigns/<id>/preview/index.html` every
60 seconds — planned, running, and completed samples in one self-contained
HTML, with live Nyann stage progress and completed AIPerf charts — and exits
when the campaign finishes: 0 when completed, 1 when failed or stopped, 130 on
Ctrl-C. Ctrl-C only detaches the watching; the campaign keeps running. A
watch refresh that fails (Wi-Fi blip, pod churn) is retried after 60 seconds
and never affects the campaign itself. A campaign still waiting for the
one-slot campaign queue reports that it has not started yet instead of
erroring; it begins as soon as the campaign ahead of it finishes. Completed
AIPerf samples and Nyann stages each carry their Grafana dashboards embedded —
click a chart point or the overlay button to open them; a point whose
monitoring export has not landed yet opens a panel saying so, and while a
sweep is still running the preview has no samples yet, and therefore no
dashboards. Nyann stage windows come from the live Job log while the Job
exists and from the runner's durable `nyann-job.log` afterwards, so a watch
attached after a bench completed still gets its dashboards.

To tail every log the campaign produces in one terminal, run:

```bash
just campaign-logs campaign-output/<id>.json
```

It aggregates the runner Job, the serving Pods of every overlay, the benchmark
Jobs the campaign submits, the vLLM prebuild Jobs that compile source builds,
and the campaign-managed router when configured,
each line prefixed with its Pod (requires [stern](https://github.com/stern/stern),
`brew install stern`). Only running Pods are tailed, so completed benchmark
Jobs and prebuild Jobs left over from earlier campaigns stay quiet. Ctrl-C
detaches without affecting the campaign.

To stop a running campaign, use the printed stop command:

```bash
just campaign-stop campaign-output/<id>.json
```

Deleting the campaign Job delivers SIGTERM to the runner, which unwinds
through the same cleanup path as a failure: child benchmark Jobs are deleted,
the serving overlay manifest is removed, the PVC summary is marked
`cancelled` with a final report, and the campaign's ConfigMap is deleted on
every exit path (the runner exits 130). The Job's termination grace period
covers the whole cleanup window; if the runner was killed past it,
`campaign-stop` reconciles the leftover Jobs, ConfigMap, and download pods
itself and reports any serving Pods that still remain. Only Pods that
existed before the stop count as leftovers — the campaign queue can admit
the next campaign (which deploys the same overlay names) the moment this
Job is deleted — and the runner gets its full teardown window before that
verdict. Results already written to the PVC are kept.

At any point, download results with the printed command:

```bash
just campaign-download campaign-output/<id>.json
```

While the campaign runs, it captures campaign status, individual Nyann stage
progress, and completed AIPerf samples through the preview renderer and saves
`~/Downloads/<id>-latest.html`; repeat it to refresh the file. After
completion it downloads the campaign state and the AIPerf inputs needed by
the final report generator, using resumable checked chunks, and saves the
final self-contained report. If the JSON uses a Grafana service in another
namespace, dashboards are exported from the laptop during the snapshot or
final download. `--dest PATH` chooses another HTML destination.

Downloads copy AIPerf report inputs and Nyann artifacts from the results PVC
through an ephemeral PVC-mounted Pod in independently verified chunks. They
start with 1 MiB chunks, double the size after four successful chunks (up to
32 MiB), and halve it after a transfer or checksum failure. A `.part` file
and checkpoint preserve verified chunks if the process exits. To pull the
full raw artifact directories (including AIPerf traces, which the report
download excludes) into the same local output, run:

```bash
just campaign-download-artifacts campaign-output/<id>.json \
  campaign-output/<id>
```

The command discovers existing AIPerf and Nyann run directories for the
configured campaign; a benchmark that did not run has no directory to fetch.
To fetch one directory, use `campaign-download-artifact CONFIG REMOTE OUTPUT`
with a path printed by the discovery command. Both commands create and remove
a temporary PVC-mounted Pod. Repeating a download verifies saved chunks and
downloads only what remains. The original artifact files stay on the PVC.

If monitoring was added to the JSON after the campaign started, regenerate
its final HTML locally after it finishes:

```bash
python3 campaign/run.py report-local campaign-output/<id>.json
```

This exports Grafana dashboards for completed AIPerf samples and Nyann
stages, then rebuilds the existing chart report from the downloaded
artifacts. The saved campaign identity check permits this monitoring
addition. Each completed stage is scraped over its measured start/end
timestamps (or its configured duration while the final summary is still
pending); cached dashboards make later refreshes faster. Apply the PodMonitor
before the benchmark so the historical vLLM series exist in Grafana. Nyann's
JSON stage summary supplies TTFT, ITL, and end-to-end latency. The report
also reads its request JSONL to calculate client-side TPOT per completed
request as `(E2E − TTFT) / (output tokens − 1)`, excluding requests crossing
stage boundaries and requests with fewer than two output tokens. The cluster
download copies those JSONL files in checked chunks; completed stages gain
TPOT once their request records are complete. The Nyann chart defaults to TPOT
versus output throughput when all plotted stages have TPOT.

The main example shows two builds (`branch0 + branch1 + branch2` and
`branch0 + branch1 + branch3`) crossed with three overlays. Each overlay path
must point to a complete Kustomize deployment; `dimensions` are report labels,
not manifest patches. To use `vllm_image` without a source build, omit `build_repo`
and `builds` entirely. To compare that image with source builds, add
`{"name": "nightly", "steps": []}` to the `builds` list; that entry uses
the configured image unchanged. A nightly entry can still specify `deepep` to build only
DeepEP. Nightly runs have no vLLM wheel prebuild; DeepEP-only runs prebuild its
wheel. The saved `serving-pods.json` records the image ID used by each deployment.
To compare several existing images without compiling anything, replace `vllm_image`
with a `vllm_images` list:

```json
"vllm_images": [
  {"name": "nightly", "image": "docker.io/vllm/vllm-openai:nightly"},
  {"name": "0-11-0", "image": "docker.io/vllm/vllm-openai:v0.11.0"}
]
```

Each entry needs a short `name` and an explicit `image` reference. The campaign
deploys every image × overlay combination; folders, benchmark run IDs, and report
labels use `<image-name>-<overlay-name>`, and `comparison.csv` records the exact
`vllm_image` per row. Image sweeps never compile vLLM: no wheel prebuild Job, no
vLLM repository, no build cache — each image runs exactly as published. Because a
sweep of prebuilt images cannot express source changes, `vllm_images` is exclusive
with `vllm_image`, top-level `builds` and `build_repo`, and per-overlay `build`;
validation rejects every combination, as it rejects a `dimensions` key named
`image` that would collide with the report label.
`examples/campaign.qwen3-30b-a3b-h200.json` sweeps the nightly and a pinned
release image across the single-node Qwen3-30B-A3B prefill/decode overlay, deploying
the wide-ep EPP router with the Qwen3-30B-A3B override alongside each case.
`examples/campaign.kimi-nightly.example.json` shows a concrete Kimi aggregate
overlay using `vllm/vllm-openai:nightly`; set its llm-d fork/ref and cluster
PVC and queues before submitting.
`examples/campaign.glm52-h200.json` runs the configured vLLM nightly
image across two GLM 5.2 prefill/decode DisaggregatedSet overlays. It replaces
both role images without a source build or DeepEP override. Its explicit
`vllm_cli_args` disables CUDA graph capture on the prefill role because the
2026-10-06 nightly failed during DeepEP graph capture on the H200 cluster.
The report labels this setting as `prefill_cuda_graphs=off`; it affects benchmark
performance. The standalone router's InferencePool must select
`llm-d.ai/inference-serving=true` and `llm-d.ai/model=GLM-5.2-FP8` so cache
evictor Pods are not treated as model endpoints. The sample also sets
`VLLM_SERVER_DEV_MODE=1` on both roles so AIPerf can reset each vLLM prefix
cache before every concurrency run. AIPerf uses the `inferencex-agentx-mvp`
scenario, which requires at least 900 seconds per concurrency sample. Its
`max_context_length` is required in campaign JSON to filter traces that exceed
the tested model's context limit. For other campaigns,
the generic source script uses
precompiled native libraries from the pinned first source commit; branch sets
that change native C++/CUDA code need an overlay-specific full build recipe.
DeepEP stays as shipped in the image unless `deepep` is specified explicitly.

### Campaign-managed EPP router

A campaign can deploy the llm-d router itself instead of depending on a shared
installation. Add a top-level `router` block listing the chart values files from
the llm-d checkout, in the same order you would pass them to `helm install`:

```json
"router": {
  "release": "wide-ep",
  "chart": "oci://ghcr.io/llm-d/charts/llm-d-router-standalone",
  "version": "v0",
  "values": [
    "guides/recipes/router/base.values.yaml",
    "guides/wide-ep/router/wide-ep.values.yaml",
    "guides/wide-ep/router/qwen3-30b-a3b-overrides.values.yaml"
  ]
}
```

`release`, `chart`, and `version` are optional and default to the standalone
llm-d router chart pinned in llm-d's `guides/env.sh`. The runner renders the
chart once per campaign with `helm template`, drops the chart's cluster-scoped
leader-election RBAC (the campaign may only create namespaced resources; the
default plugin set does not use the TokenReview/SubjectAccessReview permissions
it grants), and pins every remaining resource into the campaign namespace. Each
case's saved manifest then contains the router **and** the model server, so the
existing pre-existing-resource and empty-namespace checks cover both, they are
applied together, the runner waits for the router Deployment to report
`Available` once the serving Pods are ready, and cleanup deletes the combined
manifest before the next case. A manually installed router in the namespace is
therefore rejected as a pre-existing resource — uninstall it first. Keep
`router.epp.replicas` at 1: with the chart's `Recreate` strategy, multi-replica
EPP Deployments never report `Available`. When `base_url` is omitted, it is
derived from the release as `http://<release>-epp.<namespace>.svc.cluster.local/v1`.
The values files must point the InferencePool and rollout screener at the
deployed model server (a model-specific override such as
`qwen3-30b-a3b-overrides.values.yaml`), and every overlay in the campaign must
share one router configuration. The campaign Job does not rely on the runner
image shipping `helm`: when a `router` block is configured, submit adds a
`fetch-helm` init container that unpacks a pinned helm release
(`HELM_VERSION` in `campaign/run.py`) into an emptyDir mounted ahead of the
image's `PATH`, and the public OCI chart is fetched in-cluster. The chart's
EPP Role grants the router read access to `inferencepools`,
`inferenceobjectives`, and `inferencemodelrewrites`; since Kubernetes refuses
to let a service account create a Role whose permissions it does not itself
hold, `campaign/rbac.yaml` grants the campaign runner those same reads — no
extra RBAC beyond `campaign-setup` is required.

## Test locally without a cluster

Use a local llm-d checkout and an output directory that does not yet exist:

```bash
just campaign-test-local examples/campaign.kimi-nightly.example.json \
  ../llm-d /tmp/kimi-nightly-local-test
open /tmp/kimi-nightly-local-test/index.html
```

This renders every build and overlay combination with `kubectl kustomize`,
applies `vllm_image` and the build script, validates the manifests, and saves
their YAML plus any planned `prebuild-job.yaml`. It writes deterministic mock
AIPerf and nyann measurements, then passes the AIPerf artifacts through the
same `live-aiperf/report.py` and `gen_interactivity_chart.py` path as a live
campaign. When AIPerf is configured, the single `index.html` is labeled
**MOCK DATA**. No serving Pod,
benchmark, or Grafana query runs locally. The command uses
the local checkout instead of fetching `source.repo/ref`; source build branches
and optional DeepEP branches are still resolved from their Git remotes. The
local test needs Python dependencies, `git`, and `kubectl` for its offline
Kustomize renderer, but never contacts a Kubernetes API server. To test the
configured source repo/ref instead, run `python3 campaign/run.py test-local
<config> --output <new-directory>` without `--source-dir`.

### Legacy per-overlay builds

For a legacy single-overlay campaign without top-level
`builds`, an overlay may still have its own `build` field:

```json
"build": {
  "repo": "https://github.com/your-org/vllm.git",
  "steps": [
    {"ref": "base-branch", "action": "checkout"},
    {"ref": "feature-branch", "action": "merge"},
    {"ref": "patch-branch", "action": "cherry-pick-m2"}
  ]
}
```

The campaign resolves those branch heads once, injects the same commit list
into the build Job and serving Pods, and fails if a branch moves before the
build fetches it. The resolved inputs are saved in `summary.json`.

The bundled script also retains the original laptop publishing workflow:
`bash campaign/vllm-wheel-build.sh publish` merges or cherry-picks its ordered
inputs, pushes a unique integration branch, and writes `build-ref.env`.
Set `VLLM_BUILD_REFS` and `VLLM_BUILD_ACTIONS` together to override its
original branch recipe, and `VLLM_BUILD_REF_FILE` to choose the output path.
`publish-and-deploy` additionally requires `VLLM_BUILD_OVERLAY` and
`VLLM_BUILD_REF_FILE`; `NAMESPACE` and `VLLM_BUILD_POD_SELECTOR` control its
deployment and optional Pod restart. Campaign build recipes run inside the
cluster without pushing an integration branch.

## Execution and results

The campaign Job runs in the configured namespace. It is queued by Kueue,
fetches the selected llm-d fork/ref once, saves its resolved commit, then
renders each overlay with `kubectl kustomize`. For every requested source or
DeepEP build and overlay, it prepares the runtime wheel in the shared cache and removes
the temporary build Job and ConfigMaps. Logs for those builds are saved under
`builds/<build>/<overlay>/build.log`. After all builds finish, it saves each
combination's rendered manifest and SHA-256 hash, checks that its resources do
not pre-exist, and applies the
manifest, waits for the configured Pod selector to match exactly
`expected_pods` Ready Pods, and records Pod UIDs plus any source vLLM build commit.
After each benchmark it checks that those Pod UIDs and any build commit are
unchanged. A failed or timed-out benchmark is marked failed; a timed-out child
Job is deleted before overlay cleanup. Cleanup always attempts to delete the
saved rendered manifest and waits for serving Pods to disappear. If cleanup
fails, the campaign stops to avoid deploying another overlay on top of it.
Poll loops tolerate transient kubectl failures for up to five minutes, so a
connectivity blip cannot fail a healthy in-flight benchmark. On SIGTERM
(`campaign-stop`), the runner cancels its children, cleans up the same way,
records `cancelled`, and exits 130; its ConfigMap is deleted on every exit
path.

```bash
kubectl -n vllm get job campaign-example-overlay-sweep-001
kubectl -n vllm logs -f job/campaign-example-overlay-sweep-001
```

Results are written to `<results PVC>:/workload/campaigns/<campaign-id>/`:

- `campaign.json`: the exact campaign request, including fork and ref.
- `source-commit.txt`: the resolved llm-d commit shared by all overlays.
- `builds/<build>/<overlay>/build.log`: cache hit or build details for each
  build/runtime combination, including optional DeepEP builds.
- `<build>-<overlay>/manifest.yaml`, `serving-pods.json`, and submit logs
  (`<image>-<overlay>/` for image sweeps).
- `summary.json`: resolved build inputs, every combination, benchmark status,
  artifacts, and per-concurrency AIPerf and nyann measurements.
- `comparison.csv` and `index.html`: the CSV keeps every deployment, sample,
  measurement, resolved commit, status, and artifact path. The single HTML uses
  the existing interactive chart renderer for AIPerf samples and Nyann stages
  across builds and overlays. Its compact header shows campaign status and
  failures; Run identity expands to show full llm-d and vLLM source commits,
  the configured image, and optional DeepEP commit. It embeds saved Grafana
  dashboards for AIPerf samples and Nyann stages. Every requested AIPerf sample is checked,
  including repeated concurrencies. Nyann stages come from its Job summary,
  also saved as `nyann-job.log`. A deployment that fails while its overlay was
  applied gets `failure-diagnostics.json` in its record directory — serving
  Pod phases, restart counts, waiting reasons, the Pod's scheduler events
  (whose `describe` Events section can already be empty by capture time), and
  the last 200 lines of each container's logs (plus the previous, crashed
  instance) — and the report's Failure details section shows them; without
  this capture the teardown would delete the only copy of the crashed
  containers' output.

Full AIPerf and nyann artifacts stay in their existing `/workload/aiperf-agentx`
and `/workload/nyann-agentx` directories, keyed by campaign, overlay, and tool.
The generated AIPerf `index.html` for each sweep is saved there. A new campaign
needs a new `id`; reusing an ID is rejected instead of overwriting results.
Set `continue_on_failure: true` to attempt later overlays after a benchmark or
deployment failure that cleaned up successfully.

This runner expects a disposable deployment in the namespace. It does not
manage other users' traffic or prevent an operator from changing the serving
model during a run; any Pod UID or build-commit drift found after a sweep
invalidates that result.

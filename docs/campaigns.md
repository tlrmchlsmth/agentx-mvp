# Overlay benchmark campaigns

A campaign config is one experiment matrix: optional named vLLM builds, concrete llm-d
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
   the required `vllm_image` to the exact image reference to run, such as your
   nightly tag or a digest. The campaign applies it to every `vllm` container
   in the selected LeaderWorkerSet or DisaggregatedSet role templates and uses
   it for prebuild Jobs.
   The overlays' `imagePullPolicy` still controls whether a moving tag is
   refreshed; use `Always` for a nightly tag.
   Set `base_url` to the in-cluster `/v1` model API endpoint when the serving
   router is not at the benchmark submitters' default Istio gateway address.
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
   `campaign-setup` installs a namespace Role that can apply the resource kinds
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

   For `run-local`, Grafana may live in another namespace. Use
   `grafana_namespace`, `grafana_service`, `auth_secret`, and `dashboard_uid`
   instead of `grafana_url`. The runner reads the named Secret, opens a temporary
   service port-forward during export, and closes it afterward. The GLM 5.2
   example uses this form. An in-cluster submission using this form records
   benchmark results without Grafana credentials; `campaign-download-cluster`
   backfills dashboards after reconnecting. Apply its PodMonitor before the benchmark so
   Prometheus collects both prefill and decode metrics:

   ```bash
   kubectl apply -f examples/campaign.glm52-h200-podmonitor.yaml
   ```

```bash
export NAMESPACE=vllm
just live-benchmark-kueue-setup "$NAMESPACE"
just campaign-setup "$NAMESPACE"
just campaign-validate examples/campaign.example.json
just campaign-submit examples/campaign.example.json
```

`campaign-submit` creates a Kueue-managed Job that keeps running after the
launching laptop disconnects. It reuses the published
`quay.io/tms/benchmark-orchestrator:amd64` runtime and mounts a compressed
snapshot of the checked-out campaign code through a ConfigMap. No image build
or push is needed. Set `CAMPAIGN_IMAGE` only to use another existing runtime.

After submitting, the terminal can close and the laptop can sleep. Check the
cluster Job with `kubectl -n "$NAMESPACE" get job campaign-<id>` or read its
logs with `kubectl -n "$NAMESPACE" logs job/campaign-<id>`. At any point,
download a single HTML snapshot from the PVC with:

```bash
export KUBECONFIG=~/.kube/config.kermit
just campaign-download-cluster examples/campaign.glm52-h200-kermit.json /tmp/glm52-campaign-results
```

The command prints and saves `~/Downloads/<id>-latest.html`. While the Job is
running, it captures campaign status, individual Nyann stage progress, and
completed AIPerf samples through the existing preview renderer; repeat it to refresh
the file. After completion it downloads the campaign state and the AIPerf
inputs needed by the final report generator, using resumable checked chunks.
If the JSON uses a Grafana service in another namespace, dashboards are
exported from the laptop during the snapshot or final download. Benchmark and
overlay work continues in the cluster independently of the laptop.

The example fork URL, branch, overlay paths, and model label are placeholders;
edit them for the actual deployment before submitting. `campaign-validate`
checks the experiment file locally. It cannot validate paths in the remote
repository or cluster permissions.

The main example shows two builds (`branch0 + branch1 + branch2` and
`branch0 + branch1 + branch3`) crossed with three overlays. Each overlay path
must point to a complete Kustomize deployment; `dimensions` are report labels,
not manifest patches. To use `vllm_image` without a source build, omit `build_repo`
and `builds` entirely. To compare that image with source builds, add
`{"name": "nightly", "steps": []}` to the `builds` list; that entry uses
the configured image unchanged. A nightly entry can still specify `deepep` to build only
DeepEP. Nightly runs have no vLLM wheel prebuild; DeepEP-only runs prebuild its
wheel. The saved `serving-pods.json` records the image ID used by each deployment.
`examples/campaign.kimi-nightly.example.json` shows a concrete Kimi aggregate
overlay using `vllm/vllm-openai:nightly`; set its llm-d fork/ref and cluster
PVC and queues before submitting.
`examples/campaign.glm52-h200-kermit.json` runs the configured vLLM nightly
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

## Run from this checkout

With `KUBECONFIG` set to the target cluster, run the live campaign from the
local checkout without publishing a runner image:

```bash
export KUBECONFIG=~/.kube/config.kermit
just campaign-run-local examples/campaign.glm52-h200-kermit.json /tmp/glm52-campaign-live
```

`campaign-run-local` keeps the orchestration process in the foreground while
serving Pods and benchmark Jobs run on the cluster. To return to your shell
immediately without publishing a runner image, use `campaign-start-local` with
a fresh campaign ID and output directory:

```bash
jq '.id = "glm52-h200-pd-nightly-012"' \
  examples/campaign.glm52-h200-kermit.json > /tmp/campaign.glm52-h200-kermit-012.json
just campaign-start-local /tmp/campaign.glm52-h200-kermit-012.json \
  /tmp/agentx-glm52-nightly-live-20261006-012
```

The detached runner inherits `KUBECONFIG`, writes progress to
`<output-directory>.log`, and writes campaign state under the output directory.
Use `campaign-stop-local` with the same config and output path to stop it.

To terminate that run from another terminal, pass the same configuration and
output directory to `campaign-stop-local`:

```bash
just campaign-stop-local /tmp/campaign.glm52-h200-kermit-010.json \
  /tmp/agentx-glm52-nightly-live-20261006-010
```

This signals only the matching local runner, removes benchmark Jobs whose
annotated run IDs belong to the campaign, waits for the runner's overlay
cleanup, and records `cancelled` in `summary.json`. It keeps downloaded
artifacts and reports. If the runner already exited but left `running` in the
summary, the same command reconciles that stale status. If serving Pods remain,
it reports a cleanup error without deleting resources from another run.

While the sweep runs, use the single preview HTML to see planned, running,
and completed samples. The final `index.html` is generated when the run finishes.

To keep that preview updated throughout the campaign, run this in another terminal:

```bash
export KUBECONFIG=~/.kube/config.kermit
python3 campaign/run.py preview-local examples/campaign.glm52-h200-kermit.json --output /tmp/glm52-campaign-live --watch
open /tmp/glm52-campaign-live/campaigns/glm52-h200-pd-nightly-009/preview/index.html
```

The command updates `preview/index.html` every minute; the open page reloads
periodically. Its progress table shows active and pending samples even before
the first result finishes. Completed AIPerf results use the existing chart
renderer and Grafana exporter. On success, the preview file becomes the final
self-contained report; on failure, it retains the last partial preview.
Copy `preview/index.html` to download a standalone snapshot at any time.
Omit `--watch` to take one snapshot. A sample with no
scraped vLLM metrics keeps its AIPerf charts and displays a missing-monitoring
notice instead of an empty per-run Grafana dashboard. The monitoring overlay
still includes any available GPU or other time series for that sample and
labels its missing vLLM coverage.

For a one-shot download of the latest state, run:

```bash
export KUBECONFIG=~/.kube/config.kermit
just campaign-download-latest examples/campaign.glm52-h200-kermit.json /tmp/glm52-campaign-live
```

This refreshes a running campaign before saving `~/Downloads/<campaign-id>-latest.html`.
For a completed campaign it copies the final report; for a failed campaign it
copies the last partial preview. The Python command also accepts `--dest PATH`.

The local runner downloads AIPerf report inputs and Nyann artifacts from the
results PVC in independently verified chunks. It starts with 1 MiB chunks,
doubles the size after four successful chunks (up to 32 MiB), and halves it
after a transfer or checksum failure. A `.part` file and checkpoint preserve
verified chunks if the process exits. To resume a PVC artifact directory into
the same local output after an interrupted campaign, run:

```bash
just campaign-download-artifacts examples/campaign.glm52-h200-kermit.json \
  /tmp/glm52-campaign-live
```

The command discovers existing AIPerf and Nyann run directories for the
configured campaign; a benchmark that did not run has no directory to fetch.
To fetch one directory, use `campaign-download-artifact CONFIG REMOTE OUTPUT`
with a path printed by the discovery command. Both commands create and remove
a temporary PVC-mounted Pod. Repeating a download verifies saved chunks and
downloads only what remains. The original artifact files stay on the PVC.
AIPerf's raw traces are still excluded from the automatic report download;
these commands retrieve them explicitly.

If monitoring was added to the JSON after a `run-local` campaign started,
regenerate its final HTML after the campaign finishes:

```bash
python3 campaign/run.py report-local examples/campaign.glm52-h200-kermit.json --output /tmp/glm52-campaign-live
```

This exports Grafana dashboards for completed AIPerf samples and Nyann stages,
then rebuilds the existing chart report from the saved local artifacts. For a
running Nyann campaign, add the `monitoring` block to the local JSON and run
`campaign-download-cluster` again. The saved campaign identity check permits
this monitoring addition. Each completed stage is scraped over its measured
start/end timestamps (or its configured duration while the final summary is
still pending); cached dashboards make later refreshes faster. Apply the
PodMonitor before the benchmark so the historical vLLM series exist in Grafana.
Nyann's JSON stage summary supplies TTFT, ITL, and end-to-end latency. The
report also reads its request JSONL to calculate client-side TPOT per completed
request as `(E2E − TTFT) / (output tokens − 1)`, excluding requests crossing
stage boundaries and requests with fewer than two output tokens. The cluster
download copies those JSONL files in checked chunks; completed stages gain
TPOT once their request records are complete. The Nyann chart defaults to TPOT
versus output throughput when all plotted stages have TPOT.

The orchestrator applies one serving overlay at a time and submits the AIPerf
and nyann Jobs to the cluster. A temporary Pod mounts `results_pvc` so completed
benchmark artifacts can be copied into the local output directory. The final
HTML and summary are written locally; the temporary Pod is removed at the end.
Choose a new campaign ID and an output directory that does not yet exist for
each run.

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

```bash
kubectl -n vllm get job campaign-example-overlay-sweep-001
kubectl -n vllm logs -f job/campaign-example-overlay-sweep-001
```

Results are written to `<results PVC>:/workload/campaigns/<campaign-id>/`:

- `campaign.json`: the exact campaign request, including fork and ref.
- `source-commit.txt`: the resolved llm-d commit shared by all overlays.
- `builds/<build>/<overlay>/build.log`: cache hit or build details for each
  build/runtime combination, including optional DeepEP builds.
- `<build>-<overlay>/manifest.yaml`, `serving-pods.json`, and submit logs.
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
  also saved as `nyann-job.log`.

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

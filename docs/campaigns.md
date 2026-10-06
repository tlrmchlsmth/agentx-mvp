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

```bash
export NAMESPACE=vllm
export CAMPAIGN_IMAGE=quay.io/your-org/benchmark-orchestrator:<immutable-tag>
just campaign-build
just campaign-push
just live-benchmark-kueue-setup "$NAMESPACE"
just campaign-setup "$NAMESPACE"
just campaign-validate examples/campaign.example.json
just campaign-submit examples/campaign.example.json
```

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
both role images without a source build or DeepEP override. For other campaigns,
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
open /tmp/glm52-campaign-live/campaigns/glm52-h200-pd-nightly-002/index.html
```

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
- `comparison.csv` and `index.html`: the final HTML is one portable report for
  every build, overlay, and sweep. A compact run identity table has one row
  per deployment with its llm-d commit, every pinned vLLM build commit, any
  DeepEP commit, and the configured image. A separate measurements table shows
  AIPerf and nyann throughput, latency, dimensions, and status. The CSV keeps
  every metric and artifact path per sample. The chart labels identify each variant by
  its build commits; the source details below the charts retain full hashes,
  the configured image, and any DeepEP commit. Nightly runs show their image
  because they have no pinned vLLM source commit; the commits cell says this
  explicitly. The existing AIPerf renderer
  draws cross-variant charts and embeds saved Grafana dashboards in that same
  HTML file. Every requested AIPerf sample is checked, including repeated
  concurrencies. Nyann rows come from its per-stage Job summary, also saved
  as `nyann-job.log`.

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

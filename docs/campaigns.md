# Overlay benchmark campaigns

A campaign deploys Kustomize overlays **one at a time** in a dedicated serving
namespace. For each overlay it waits for the exact expected number of Ready
serving Pods, runs the configured AIPerf and/or nyann sweeps, records results,
and deletes the rendered overlay before moving to the next. One Kueue campaign
queue slot prevents two campaign runners from changing the deployment at once.
The separate benchmark queue admits the child benchmark Jobs; the campaign Job
must never use that queue or it could block its own children.

## Prepare

1. Copy `examples/campaign.example.json`. Set `source.repo` to the llm-d
   fork and `source.ref` to the branch, tag, or commit to benchmark. List the
   Kustomize overlay paths relative to that repository, with each overlay's
   serving Pod selector and expected Pod count. The campaign Job fetches the
   source once at run time, resolves it to a commit, and uses that checkout for
   every overlay. Changing forks or branches does not require rebuilding the
   campaign image. Container images referenced by those overlays must already
   be published; the campaign checks out deployment manifests but does not
   build llm-d images. If an overlay contains the `humming-build` script and
   `vllm-build-ref` ConfigMaps plus a prefill LeaderWorkerSet, the runner
   automatically starts a build Job before the serving deployment. The Job
   uses that worker's image, GPU resources, and shared build-cache PVC. The
   script validates and installs an existing wheel or builds and caches it on
   a miss. A failed build stops that overlay before serving Pods are started.
   The build ref must identify an already published immutable vLLM commit;
   the runner does not push branches or require Git write credentials.
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

## Execution and results

The campaign Job runs in the configured namespace. It is queued by Kueue,
fetches the selected llm-d fork/ref once, saves its resolved commit, then
renders each overlay with `kubectl kustomize`, stores the rendered manifest and
its SHA-256 hash, and checks that its resources do not pre-exist. Humming overlays
first create their build ConfigMaps and run the cache-aware build Job. Its output
is saved as `<overlay>/build.log`; the Job is removed before deployment. It applies the
manifest, waits for the configured Pod selector to match exactly
`expected_pods` Ready Pods, and records Pod UIDs plus the vLLM build commit.
After each benchmark it checks that those Pod UIDs and the build commit are
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
- `<overlay>/manifest.yaml`, `serving-pods.json`, and submit logs.
- `summary.json`: each overlay, benchmark Job, status, artifact path, and AIPerf
  measurements.
- `index.html`: a compact cross-overlay AIPerf comparison table.

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

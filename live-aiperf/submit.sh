#!/usr/bin/env bash
set -euo pipefail

# Usage: ./run_aiperf.sh <concurrency|comma-separated-sweep> [duration_seconds]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPORTER_SCRIPT="${SCRIPT_DIR}/report.py"
GENERATOR_SCRIPT="${SCRIPT_DIR}/../gen_interactivity_chart.py"
OVERLAY_SCRIPT="${SCRIPT_DIR}/../overlay_dashboards.py"
PLOTLY_BUNDLE="${SCRIPT_DIR}/plotly-basic-2.35.2.min.js.gz"
RESET_SCRIPT="${SCRIPT_DIR}/reset-prefix-caches.py"
LLMD_CAPTURE_SCRIPT="${SCRIPT_DIR}/capture-llmd-resources.sh"

CONCURRENCY="${1:-}"
DURATION="${2:-900}"
IFS=',' read -r -a SWEEP_CONCURRENCIES <<< "$CONCURRENCY"
if (( ${#SWEEP_CONCURRENCIES[@]} == 0 )); then
  echo "usage: $0 <concurrency|comma-separated-sweep> [duration_seconds 900-7200]" >&2
  exit 2
fi
for sweep_concurrency in "${SWEEP_CONCURRENCIES[@]}"; do
  if [[ ! "$sweep_concurrency" =~ ^[1-9][0-9]*$ ]] || (( sweep_concurrency > 2048 )); then
    echo "invalid concurrency: ${sweep_concurrency}" >&2
    exit 2
  fi
done
if [[ ! "$DURATION" =~ ^[1-9][0-9]*$ ]] || (( DURATION < 900 || DURATION > 7200 )); then
  echo "usage: $0 <concurrency 1-2048> [duration_seconds 900-7200]" >&2
  exit 2
fi

# Do not inherit the repository's legacy NAMESPACE setting. The live workflow
# targets the namespace currently carrying one valid deployed vLLM build.
NAMESPACE="${LIVE_AIPERF_NAMESPACE:-}"
if [[ -z "$NAMESPACE" ]]; then
  DEPLOYMENT_NAMESPACES=()
  while IFS=$'\t' read -r namespace commit; do
    [[ "$commit" =~ ^[0-9a-f]{40}$ ]] && DEPLOYMENT_NAMESPACES+=("$namespace")
  done < <(kubectl get configmaps --all-namespaces -o go-template='{{range .items}}{{if eq .metadata.name "vllm-build-ref"}}{{.metadata.namespace}}{{"\t"}}{{index .data "VLLM_BUILD_COMMIT"}}{{"\n"}}{{end}}{{end}}')
  if (( ${#DEPLOYMENT_NAMESPACES[@]} != 1 )); then
    echo "Could not identify exactly one deployed vLLM namespace (found: ${DEPLOYMENT_NAMESPACES[*]:-none})" >&2
    echo "Set LIVE_AIPERF_NAMESPACE only when choosing intentionally among multiple deployments." >&2
    exit 1
  fi
  NAMESPACE="${DEPLOYMENT_NAMESPACES[0]}"
fi
BASE_URL="${BASE_URL:-http://llm-d-inference-gateway-istio.${NAMESPACE}.svc.cluster.local/v1}"
AIPERF_IMAGE="${AIPERF_IMAGE:-quay.io/rh-ee-robshaw/aiperf@sha256:9bb54497579481be375e3730dd52c353dc01e8a2fc8e0840acf36a843c3122e4}"
HF_SECRET="${HF_SECRET:-llm-d-hf-token}"
RESULTS_PVC="${RESULTS_PVC:-kimi-k3-build-cache}"
OUTPUT_ROOT="${OUTPUT_ROOT:-aiperf-agentx}"
MAX_CONTEXT_LENGTH="${MAX_CONTEXT_LENGTH:-1000000000}"
TOPOLOGY="${TOPOLOGY:-auto}"
REQUESTED_TOPOLOGY="$TOPOLOGY"
KUBECTL_IMAGE="${KUBECTL_IMAGE:-docker.io/alpine/k8s:1.31.0}"
BENCHMARK_QUEUE="${LIVE_BENCHMARK_QUEUE:-live-benchmark-client}"
READY_TIMEOUT_SECONDS="${LIVE_AIPERF_READY_TIMEOUT_SECONDS:-1800}"
if [[ ! "$BENCHMARK_QUEUE" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]] || (( ${#BENCHMARK_QUEUE} > 63 )); then
  echo "invalid LIVE_BENCHMARK_QUEUE: ${BENCHMARK_QUEUE}" >&2
  exit 2
fi
if [[ ! "$READY_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || (( READY_TIMEOUT_SECONDS < 60 || READY_TIMEOUT_SECONDS > 7200 )); then
  echo "LIVE_AIPERF_READY_TIMEOUT_SECONDS must be 60-7200" >&2
  exit 2
fi
kubectl get localqueue "$BENCHMARK_QUEUE" -n "$NAMESPACE" -o name >/dev/null || {
  echo "Missing Kueue LocalQueue ${NAMESPACE}/${BENCHMARK_QUEUE}; run just live-benchmark-kueue-setup ${NAMESPACE}" >&2
  exit 1
}

# Campaigns pin the llm-d deployment source directly. Standalone live runs
# continue to read the published vLLM build marker from the namespace.
if [[ -n "${LIVE_BENCHMARK_SOURCE_COMMIT:-}" ]]; then
  SOURCE_REF="${LIVE_BENCHMARK_SOURCE_REF:-}"
  SOURCE_COMMIT="$LIVE_BENCHMARK_SOURCE_COMMIT"
  SOURCE_KIND="${LIVE_BENCHMARK_SOURCE_KIND:-llm-d}"
else
  SOURCE_REF="$(kubectl get configmap vllm-build-ref -n "$NAMESPACE" -o jsonpath='{.data.VLLM_BUILD_REF}')"
  SOURCE_COMMIT="$(kubectl get configmap vllm-build-ref -n "$NAMESPACE" -o jsonpath='{.data.VLLM_BUILD_COMMIT}')"
  SOURCE_KIND=vllm
fi
if [[ ! "$SOURCE_REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] ||
   ! [[ "$SOURCE_COMMIT" =~ ^[0-9a-f]{40}$ ]] ||
   ! [[ "$SOURCE_KIND" == vllm || "$SOURCE_KIND" == llm-d ]]; then
  echo "Benchmark source ref/commit is missing or malformed" >&2
  exit 1
fi
SOURCE_SHORT="${SOURCE_COMMIT:0:12}"
echo "Benchmarking ${SOURCE_KIND} source ${SOURCE_REF} (${SOURCE_COMMIT})"

RUN_TIMESTAMP="$(date -u +%Y%m%d%H%M%S)"
CONCURRENCY_LABEL="$(IFS=-; echo "${SWEEP_CONCURRENCIES[*]}")"
CONCURRENCY_ARGS="${SWEEP_CONCURRENCIES[*]}"

# Preserve every occurrence in a sweep. A repeated concurrency gets a stable
# sample suffix (c<N>-r<M>) so a rerun cannot overwrite the earlier sample.
# Use indexed arrays only: the macOS system Bash is commonly version 3.2 and
# does not support associative arrays.
RUN_SPECS=()
for ((run_index = 0; run_index < ${#SWEEP_CONCURRENCIES[@]}; run_index++)); do
  sweep_concurrency="${SWEEP_CONCURRENCIES[$run_index]}"
  repeat_index=0
  repeat_count=0
  for ((scan_index = 0; scan_index < ${#SWEEP_CONCURRENCIES[@]}; scan_index++)); do
    [[ "${SWEEP_CONCURRENCIES[$scan_index]}" == "$sweep_concurrency" ]] || continue
    repeat_count=$((repeat_count + 1))
    if (( scan_index <= run_index )); then
      repeat_index=$((repeat_index + 1))
    fi
  done
  if (( repeat_count > 1 )); then
    output_name="c${sweep_concurrency}-r${repeat_index}"
  else
    output_name="c${sweep_concurrency}"
  fi
  RUN_SPECS+=("${sweep_concurrency}:${repeat_index}:${repeat_count}:${output_name}")
done
RUN_SPECS_ARGS="${RUN_SPECS[*]}"
if [[ -n "${LIVE_AIPERF_RUN_ID:-}" ]]; then
  RUN_ID="$LIVE_AIPERF_RUN_ID"
elif (( ${#SWEEP_CONCURRENCIES[@]} == 1 )); then
  RUN_ID="agentx-c${CONCURRENCY_LABEL}-vllm-${SOURCE_SHORT}-${RUN_TIMESTAMP}"
else
  RUN_ID="agentx-sweep-c${CONCURRENCY_LABEL}-vllm-${SOURCE_SHORT}-${RUN_TIMESTAMP}"
fi
# Kubernetes names are limited to 63 characters; keep this stable for long
# comma-separated sweeps while retaining the complete run identity in output.
JOB_NAME="aiperf-${SOURCE_SHORT}-${RUN_TIMESTAMP}"
OUTPUT_PATH="/workload/${OUTPUT_ROOT}/${RUN_ID}"
ARTIFACT_CONFIGMAP="aiperf-artifacts-${SOURCE_SHORT}-${RUN_TIMESTAMP}"

# Discover the serving model from the cluster rather than baking a model name
# into this launcher. More than one model is an ambiguous benchmark target, so
# require an explicit MODEL_LABEL only in that exceptional case.
DISCOVERED_MODELS=()
while IFS= read -r discovered_model; do
  [[ -n "$discovered_model" ]] && DISCOVERED_MODELS+=("$discovered_model")
done < <(
  kubectl get pods -n "$NAMESPACE" -l llm-d.ai/inference-serving=true \
    -o go-template='{{range .items}}{{index .metadata.labels "llm-d.ai/model"}}{{"\n"}}{{end}}' \
    | awk 'NF' | sort -u
)
if [[ -z "${MODEL_LABEL:-}" ]]; then
  if (( ${#DISCOVERED_MODELS[@]} == 0 )); then
    echo "No inference-serving pods with llm-d.ai/model found in ${NAMESPACE}" >&2
    exit 1
  fi
  if (( ${#DISCOVERED_MODELS[@]} != 1 )); then
    echo "More than one serving model was found: ${DISCOVERED_MODELS[*]}" >&2
    echo "Set MODEL_LABEL only when selecting one intentionally." >&2
    exit 1
  fi
  MODEL_LABEL="${DISCOVERED_MODELS[0]}"
fi

BASE_SELECTOR="llm-d.ai/model=${MODEL_LABEL},llm-d.ai/inference-serving=true"
PREFILL_SELECTOR="${BASE_SELECTOR},llm-d.ai/role=prefill"
DECODE_SELECTOR="${BASE_SELECTOR},llm-d.ai/role=decode"
AGGREGATE_SELECTOR="${BASE_SELECTOR},!llm-d.ai/role"

prefill_pods="$(kubectl get pods -n "$NAMESPACE" -l "$PREFILL_SELECTOR" -o name)"
decode_pods="$(kubectl get pods -n "$NAMESPACE" -l "$DECODE_SELECTOR" -o name)"
aggregate_pods="$(kubectl get pods -n "$NAMESPACE" -l "$AGGREGATE_SELECTOR" -o name)"

if [[ "$TOPOLOGY" == auto ]]; then
  if [[ -n "$prefill_pods" || -n "$decode_pods" ]]; then
    TOPOLOGY=pd
  elif [[ -n "$aggregate_pods" ]]; then
    TOPOLOGY=aggregate
  else
    echo "No ${MODEL_LABEL} inference-serving pods found in ${NAMESPACE}" >&2
    exit 1
  fi
fi

case "$TOPOLOGY" in
pd)
  if [[ "$REQUESTED_TOPOLOGY" == auto && -n "$aggregate_pods" ]]; then
    echo "Both PD and aggregate ${MODEL_LABEL} pods exist in ${NAMESPACE}; refusing an ambiguous benchmark" >&2
    echo "Set TOPOLOGY=pd explicitly to benchmark PD during a rollout" >&2
    exit 1
  fi
  echo "Detected PD topology; the submitted Job will wait for prefill and decode pods"
  WAIT_SELECTORS="${PREFILL_SELECTOR} ${DECODE_SELECTOR}"
  ;;
aggregate)
  echo "Detected aggregate topology; the submitted Job will wait for aggregate pods"
  WAIT_SELECTORS="${AGGREGATE_SELECTOR}"
  ;;
*)
  echo "TOPOLOGY must be auto, pd, or aggregate (got: ${TOPOLOGY})" >&2
  exit 2
  ;;
esac

# Preserve the serving capacity used for this run so charts can normalize a
# result per prefill GPU, decode GPU, or total serving GPU later. Count GPU
# limits rather than assuming a particular machine shape.
gpu_count() {
  kubectl get pods -n "$NAMESPACE" -l "$1" \
    -o go-template='{{range .items}}{{range .spec.containers}}{{index .resources.limits "nvidia.com/gpu"}}{{"\n"}}{{end}}{{end}}' \
    | awk 'NF { total += $1 } END { print total + 0 }'
}
PREFILL_GPU_COUNT="$(gpu_count "$PREFILL_SELECTOR")"
DECODE_GPU_COUNT="$(gpu_count "$DECODE_SELECTOR")"
TOTAL_GPU_COUNT="$(gpu_count "$BASE_SELECTOR")"
if (( TOTAL_GPU_COUNT == 0 )); then
  echo "Serving pods are not present yet; GPU counts will be recorded as zero in the submission snapshot"
fi

# Capture the exact serving and llm-d resources while submitting the run. The
# AIPerf pod also has narrowly scoped pod-list permission for cache resets.
POD_SNAPSHOT="$(mktemp "${TMPDIR:-/tmp}/aiperf-serving-pods.XXXXXX.yaml")"
JOB_MANIFEST="$(mktemp "${TMPDIR:-/tmp}/aiperf-job.XXXXXX.yaml")"
LLMD_SNAPSHOT="$(mktemp "${TMPDIR:-/tmp}/aiperf-llmd.XXXXXX.yaml")"
LLMD_SNAPSHOT_GZ="${LLMD_SNAPSHOT}.gz"
trap 'rm -f "$POD_SNAPSHOT" "$JOB_MANIFEST" "$LLMD_SNAPSHOT" "$LLMD_SNAPSHOT_GZ"' EXIT
kubectl get pods -n "$NAMESPACE" -l "$BASE_SELECTOR" -o yaml > "$POD_SNAPSHOT"
bash "$LLMD_CAPTURE_SCRIPT" "$NAMESPACE" "$MODEL_LABEL" > "$LLMD_SNAPSHOT"
gzip -c "$LLMD_SNAPSHOT" > "$LLMD_SNAPSHOT_GZ"

echo "Submitting ${JOB_NAME} in ${NAMESPACE}"
cat > "$JOB_MANIFEST" <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: aiperf-cache-resetter
  namespace: ${NAMESPACE}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: aiperf-cache-resetter
  namespace: ${NAMESPACE}
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: aiperf-cache-resetter
  namespace: ${NAMESPACE}
subjects:
  - kind: ServiceAccount
    name: aiperf-cache-resetter
    namespace: ${NAMESPACE}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: aiperf-cache-resetter
---
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
  labels:
    kueue.x-k8s.io/queue-name: "${BENCHMARK_QUEUE}"
    benchmark.llm-d.ai/workload: inferencex-agentx-mvp
    benchmark.llm-d.ai/model: "${MODEL_LABEL}"
    benchmark.llm-d.ai/concurrency: "${CONCURRENCY_LABEL}"
    benchmark.llm-d.ai/source-commit: "${SOURCE_COMMIT}"
  annotations:
    benchmark.llm-d.ai/source-ref: "${SOURCE_REF}"
    benchmark.llm-d.ai/source-kind: "${SOURCE_KIND}"
    benchmark.llm-d.ai/source-commit: "${SOURCE_COMMIT}"
    benchmark.llm-d.ai/run-id: "${RUN_ID}"
spec:
  suspend: true
  backoffLimit: 0
  template:
    metadata:
      labels:
        benchmark.llm-d.ai/workload: inferencex-agentx-mvp
        benchmark.llm-d.ai/model: "${MODEL_LABEL}"
        benchmark.llm-d.ai/source-commit: "${SOURCE_COMMIT}"
      annotations:
        benchmark.llm-d.ai/source-ref: "${SOURCE_REF}"
        benchmark.llm-d.ai/source-kind: "${SOURCE_KIND}"
        benchmark.llm-d.ai/source-commit: "${SOURCE_COMMIT}"
    spec:
      serviceAccountName: aiperf-cache-resetter
      restartPolicy: Never
      # The shared results PVC is root-owned from the legacy AIPerf image;
      # the NGC image otherwise runs as UID 1000 and cannot create artifacts.
      securityContext:
        runAsUser: 0
        runAsGroup: 0
      initContainers:
        - name: wait-for-serving
          image: ${KUBECTL_IMAGE}
          imagePullPolicy: IfNotPresent
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -eu
              deadline=\$(( \$(date +%s) + ${READY_TIMEOUT_SECONDS} ))
              for selector in ${WAIT_SELECTORS}; do
                while :; do
                  if [ "\$(date +%s)" -ge "\$deadline" ]; then
                    echo "Timed out waiting for serving pods matching \$selector" >&2
                    exit 1
                  fi
                  pods=\$(kubectl get pods -n "${NAMESPACE}" -l "\$selector" -o name)
                  if [ -n "\$pods" ] && kubectl wait -n "${NAMESPACE}" --for=condition=Ready pod -l "\$selector" --timeout=15s; then
                    break
                  fi
                  sleep 15
                done
              done
              mkdir -p "${OUTPUT_PATH}"
              : > "${OUTPUT_PATH}/serving-pods.txt"
              for selector in ${WAIT_SELECTORS}; do
                kubectl get pods -n "${NAMESPACE}" -l "\$selector" \\
                  -o jsonpath='{range .items[*]}{.metadata.name}{"\\n"}{end}' >> "${OUTPUT_PATH}/serving-pods.txt"
              done
              test -s "${OUTPUT_PATH}/serving-pods.txt"
          volumeMounts:
            - name: workload
              mountPath: /workload
      containers:
        - name: aiperf
          image: ${AIPERF_IMAGE}
          imagePullPolicy: IfNotPresent
          command: ["/bin/bash", "-lc"]
          args:
            - |
              set -euo pipefail
              output_root=${OUTPUT_PATH}
              aiperf_python=/opt/venv/bin/python3
              aiperf_bin=/opt/venv/bin/aiperf
              schema=/aiperf/src/aiperf/common/models/export_models.py
              if [[ -f "\$schema" ]] && grep -qx '    hostname: str | None' "\$schema"; then
                sed -i 's/^    hostname: str | None$/    hostname: str | None = None/' "\$schema"
              fi
              export HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0
              model_deadline=\$(( \$(date +%s) + 300 ))
              while :; do
                if model=\$("\$aiperf_python" -c 'import json,sys,urllib.request; payload=json.load(urllib.request.urlopen(sys.argv[1], timeout=30)); models=payload.get("data", []); assert len(models) == 1, f"expected exactly one served model, got {models!r}"; print(models[0]["id"])' "${BASE_URL}/models" 2>&1); then
                  break
                fi
                if (( \$(date +%s) >= model_deadline )); then
                  echo "Model discovery failed after 300 seconds: \$model" >&2
                  exit 1
                fi
                sleep 5
              done
              "\$aiperf_python" -c 'from transformers import AutoTokenizer; import sys; AutoTokenizer.from_pretrained(sys.argv[1], trust_remote_code=True)' "\$model"
              overall_status=0
              for run_spec in ${RUN_SPECS_ARGS}; do
                IFS=: read -r concurrency repeat_index repeat_count output_name <<< "\$run_spec"
                echo "Clearing every prefill and decode vLLM prefix cache before c\$concurrency"
                "\$aiperf_python" /benchmark-input/reset-prefix-caches.py
                output="\$output_root/\$output_name"
                if (( repeat_count > 1 )); then
                  run_suffix="c\$concurrency-r\$repeat_index"
                else
                  run_suffix="c\$concurrency"
                fi
                mkdir -p "\$output"
                # Keep the exact source details with the AIPerf output, not
                # only in ephemeral Kubernetes metadata.
                printf '{\n  "run_id": "%s-%s",\n  "model_label": "%s",\n  "model": "%s",\n  "source_kind": "%s",\n  "source_ref": "%s",\n  "source_commit": "%s",\n  "topology": "%s",\n  "prefill_gpu_count": %s,\n  "decode_gpu_count": %s,\n  "total_gpu_count": %s,\n  "concurrency": %s,\n  "repeat_index": %s,\n  "repeat_count": %s,\n  "duration_seconds": %s\n}\n' \\
                  "${RUN_ID}" "\$run_suffix" "${MODEL_LABEL}" "\$model" "${SOURCE_KIND}" "${SOURCE_REF}" "${SOURCE_COMMIT}" "${TOPOLOGY}" "${PREFILL_GPU_COUNT}" "${DECODE_GPU_COUNT}" "${TOTAL_GPU_COUNT}" "\$concurrency" "\$repeat_index" "\$repeat_count" "${DURATION}" \\
                  > "\$output/benchmark-metadata.json"
                # The vLLM Kimi-K3 build now reports reasoning_tokens from
                # token IDs; use those server counts instead of retokenizing
                # stripped XTML reasoning/content text in AIPerf.
                # Bound the AgentX synthesized warmup drain; without this,
                # one slow priming request can block the entire sweep forever.
                # Raw export preserves the parsed streamed response messages
                # needed to diagnose inter-chunk accounting. It also implies
                # the concatenated outputs.json export.
                if "\$aiperf_bin" profile \\
                --scenario inferencex-agentx-mvp \\
                --url ${BASE_URL} \\
                --model "\$model" \\
                --max-context-length ${MAX_CONTEXT_LENGTH} \\
                --tokenizer-trust-remote-code \\
                --endpoint-type chat \\
                --public-dataset semianalysis_cc_traces_weka_062126 \\
                --concurrency "\$concurrency" \\
                --benchmark-duration ${DURATION} \\
                --dataset-sampling-strategy sequential \\
                --use-server-token-count \\
                --streaming \\
                --random-seed 42 \\
                --export-level raw \\
                --output-artifact-dir "\$output" \\
                --ui simple; then
                  status=0
                else
                  status=\$?
                fi
                cp /benchmark-input/serving-pods.yaml "\$output/serving-pods.yaml"
                cp /benchmark-input/aiperf-job.yaml "\$output/aiperf-job.yaml"
                "\$aiperf_python" -c 'import gzip,pathlib,sys; pathlib.Path(sys.argv[2]).write_bytes(gzip.decompress(pathlib.Path(sys.argv[1]).read_bytes()))' \
                  /benchmark-input/llm-d-deployment.yaml.gz "\$output/llm-d-deployment.yaml"
                if [[ -f "\$output/profile_export_aiperf.json" ]]; then
                  "\$aiperf_python" /benchmark-input/aiperf_report.py run "\$output"
                fi
                (( status == 0 )) || overall_status=\$status
              done
              "\$aiperf_python" /benchmark-input/aiperf_report.py index "\$output_root"
              exit "\$overall_status"
          env:
            - name: HF_HOME
              value: /workload/.cache/huggingface
            - name: HF_HUB_OFFLINE
              value: "0"
            - name: TRANSFORMERS_OFFLINE
              value: "0"
            - name: HF_TOKEN
              valueFrom:
                secretKeyRef:
                  name: ${HF_SECRET}
                  key: HF_TOKEN
            - name: AIPERF_DATASET_CONFIGURATION_TIMEOUT
              value: "1800"
            - name: AIPERF_SERVICE_PROFILE_CONFIGURE_TIMEOUT
              value: "1800"
            - name: AIPERF_NAMESPACE
              value: "${NAMESPACE}"
            - name: MODEL_LABEL
              value: "${MODEL_LABEL}"
          resources:
            requests:
              cpu: "4"
              memory: 8Gi
            limits:
              cpu: "16"
              memory: 32Gi
              ephemeral-storage: 20Gi
          volumeMounts:
            - name: workload
              mountPath: /workload
            - name: benchmark-input
              mountPath: /benchmark-input
              readOnly: true
      volumes:
        - name: workload
          persistentVolumeClaim:
            claimName: ${RESULTS_PVC}
        - name: benchmark-input
          configMap:
            name: ${ARTIFACT_CONFIGMAP}
EOF

kubectl create configmap "$ARTIFACT_CONFIGMAP" -n "$NAMESPACE" \
  --from-file=serving-pods.yaml="$POD_SNAPSHOT" \
  --from-file=aiperf-job.yaml="$JOB_MANIFEST" \
  --from-file=aiperf_report.py="$REPORTER_SCRIPT" \
  --from-file=reset-prefix-caches.py="$RESET_SCRIPT" \
  --from-file=llm-d-deployment.yaml.gz="$LLMD_SNAPSHOT_GZ" \
  --from-file=gen_interactivity_chart.py="$GENERATOR_SCRIPT" \
  --from-file=overlay_dashboards.py="$OVERLAY_SCRIPT" \
  --from-file=plotly-basic-2.35.2.min.js.gz="$PLOTLY_BUNDLE" \
  --dry-run=client -o yaml | kubectl create -f -
kubectl apply -f "$JOB_MANIFEST"

echo "Job queued: ${JOB_NAME} (Kueue LocalQueue ${NAMESPACE}/${BENCHMARK_QUEUE})"
echo "Benchmark source: ${SOURCE_KIND} ${SOURCE_REF} (${SOURCE_COMMIT})"
echo "Logs: kubectl logs -n ${NAMESPACE} -f job/${JOB_NAME}"
for run_spec in "${RUN_SPECS[@]}"; do
  IFS=: read -r reported_concurrency reported_repeat reported_count reported_output_name <<< "$run_spec"
  echo "Artifacts: ${RESULTS_PVC}:${OUTPUT_PATH}/${reported_output_name}"
  echo "Portable report: ${RESULTS_PVC}:${OUTPUT_PATH}/${reported_output_name}/report.html"
done

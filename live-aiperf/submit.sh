#!/usr/bin/env bash
set -euo pipefail

# Usage: ./run_aiperf.sh <concurrency|comma-separated-sweep> [duration_seconds]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPORTER_SCRIPT="${SCRIPT_DIR}/report.py"
GENERATOR_SCRIPT="${SCRIPT_DIR}/../gen_interactivity_chart.py"
PLOTLY_BUNDLE="${SCRIPT_DIR}/plotly-basic-2.35.2.min.js.gz"

CONCURRENCY="${1:-}"
DURATION="${2:-900}"
IFS=',' read -r -a SWEEP_CONCURRENCIES <<< "$CONCURRENCY"
if (( ${#SWEEP_CONCURRENCIES[@]} == 0 )); then
  echo "usage: $0 <concurrency|comma-separated-sweep> [duration_seconds 60-7200]" >&2
  exit 2
fi
for sweep_concurrency in "${SWEEP_CONCURRENCIES[@]}"; do
  if [[ ! "$sweep_concurrency" =~ ^[1-9][0-9]*$ ]] || (( sweep_concurrency > 2048 )); then
    echo "invalid concurrency: ${sweep_concurrency}" >&2
    exit 2
  fi
done
if [[ ! "$DURATION" =~ ^[1-9][0-9]*$ ]] || (( DURATION < 60 || DURATION > 7200 )); then
  echo "usage: $0 <concurrency 1-2048> [duration_seconds 60-7200]" >&2
  exit 2
fi

KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config.kermit}"
export KUBECONFIG
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
READY_TIMEOUT="${READY_TIMEOUT:-1800s}"
TOPOLOGY="${TOPOLOGY:-auto}"
REQUESTED_TOPOLOGY="$TOPOLOGY"

# This is the exact commit selected by the PD deployment. Read it before
# creating the Job so the benchmark source is explicit in both the Job and its
# saved artifacts. An invalid marker means `humming-build.sh publish` has not
# prepared a reproducible build yet.
VLLM_BUILD_REF="$(kubectl get configmap vllm-build-ref -n "$NAMESPACE" -o jsonpath='{.data.VLLM_BUILD_REF}')"
VLLM_BUILD_COMMIT="$(kubectl get configmap vllm-build-ref -n "$NAMESPACE" -o jsonpath='{.data.VLLM_BUILD_COMMIT}')"
if [[ -z "$VLLM_BUILD_REF" ]] || ! [[ "$VLLM_BUILD_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo "vllm-build-ref in ${NAMESPACE} is unpublished or malformed; publish and apply the benchmark branch first" >&2
  exit 1
fi
VLLM_BUILD_SHORT="${VLLM_BUILD_COMMIT:0:12}"
echo "Benchmarking vLLM ${VLLM_BUILD_REF} (${VLLM_BUILD_COMMIT})"

RUN_TIMESTAMP="$(date -u +%Y%m%d%H%M%S)"
CONCURRENCY_LABEL="$(IFS=-; echo "${SWEEP_CONCURRENCIES[*]}")"
CONCURRENCY_ARGS="${SWEEP_CONCURRENCIES[*]}"
if (( ${#SWEEP_CONCURRENCIES[@]} == 1 )); then
  RUN_ID="agentx-c${CONCURRENCY_LABEL}-vllm-${VLLM_BUILD_SHORT}-${RUN_TIMESTAMP}"
else
  RUN_ID="agentx-sweep-c${CONCURRENCY_LABEL}-vllm-${VLLM_BUILD_SHORT}-${RUN_TIMESTAMP}"
fi
# Kubernetes names are limited to 63 characters; keep this stable for long
# comma-separated sweeps while retaining the complete run identity in output.
JOB_NAME="aiperf-${VLLM_BUILD_SHORT}-${RUN_TIMESTAMP}"
OUTPUT_PATH="/workload/${OUTPUT_ROOT}/${RUN_ID}"
ARTIFACT_CONFIGMAP="aiperf-artifacts-${VLLM_BUILD_SHORT}-${RUN_TIMESTAMP}"

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

# Stop any currently running aiperf benchmark before starting a new one.
running_jobs="$(kubectl get jobs -n "$NAMESPACE" \
  -l benchmark.llm-d.ai/workload=inferencex-agentx-mvp \
  -o jsonpath='{range .items[?(@.status.active>0)]}{.metadata.name}{"\n"}{end}')"
if [[ -n "$running_jobs" ]]; then
  echo "Stopping running aiperf job(s): $running_jobs"
  while IFS= read -r job; do
    [[ -z "$job" ]] || kubectl delete job "$job" -n "$NAMESPACE" --wait=false
  done <<< "$running_jobs"
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
  if [[ -z "$prefill_pods" || -z "$decode_pods" ]]; then
    echo "PD topology requires both prefill and decode pods in ${NAMESPACE}" >&2
    exit 1
  fi
  echo "Detected PD topology; waiting up to ${READY_TIMEOUT} for prefill and decode pods"
  kubectl wait -n "$NAMESPACE" \
    --for=condition=Ready pod \
    -l "$PREFILL_SELECTOR" \
    --timeout="$READY_TIMEOUT"
  kubectl wait -n "$NAMESPACE" \
    --for=condition=Ready pod \
    -l "$DECODE_SELECTOR" \
    --timeout="$READY_TIMEOUT"
  ;;
aggregate)
  if [[ -z "$aggregate_pods" ]]; then
    echo "No aggregate ${MODEL_LABEL} pods found in ${NAMESPACE}" >&2
    exit 1
  fi
  echo "Detected aggregate topology; waiting up to ${READY_TIMEOUT} for aggregate pods"
  kubectl wait -n "$NAMESPACE" \
    --for=condition=Ready pod \
    -l "$AGGREGATE_SELECTOR" \
    --timeout="$READY_TIMEOUT"
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
  echo "Serving pods have no nvidia.com/gpu limits; cannot record chart normalization capacity" >&2
  exit 1
fi

# Capture the manifest from the laptop, where kubectl already has the intended
# credentials. The AIPerf pod receives a read-only copy and writes it into its
# artifact directory; it does not need Kubernetes API permissions itself.
POD_SNAPSHOT="$(mktemp "${TMPDIR:-/tmp}/aiperf-serving-pods.XXXXXX.yaml")"
JOB_MANIFEST="$(mktemp "${TMPDIR:-/tmp}/aiperf-job.XXXXXX.yaml")"
trap 'rm -f "$POD_SNAPSHOT" "$JOB_MANIFEST"' EXIT
kubectl get pods -n "$NAMESPACE" -l "$BASE_SELECTOR" -o yaml > "$POD_SNAPSHOT"

echo "Submitting ${JOB_NAME} in ${NAMESPACE}"
cat > "$JOB_MANIFEST" <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
  labels:
    benchmark.llm-d.ai/workload: inferencex-agentx-mvp
    benchmark.llm-d.ai/model: "${MODEL_LABEL}"
    benchmark.llm-d.ai/concurrency: "${CONCURRENCY_LABEL}"
    benchmark.llm-d.ai/vllm-build-commit: "${VLLM_BUILD_COMMIT}"
  annotations:
    benchmark.llm-d.ai/vllm-build-ref: "${VLLM_BUILD_REF}"
    benchmark.llm-d.ai/vllm-build-commit: "${VLLM_BUILD_COMMIT}"
    benchmark.llm-d.ai/run-id: "${RUN_ID}"
spec:
  backoffLimit: 0
  template:
    metadata:
      labels:
        benchmark.llm-d.ai/workload: inferencex-agentx-mvp
        benchmark.llm-d.ai/model: "${MODEL_LABEL}"
        benchmark.llm-d.ai/vllm-build-commit: "${VLLM_BUILD_COMMIT}"
      annotations:
        benchmark.llm-d.ai/vllm-build-ref: "${VLLM_BUILD_REF}"
        benchmark.llm-d.ai/vllm-build-commit: "${VLLM_BUILD_COMMIT}"
    spec:
      restartPolicy: Never
      containers:
        - name: aiperf
          image: ${AIPERF_IMAGE}
          imagePullPolicy: IfNotPresent
          command: ["/bin/bash", "-lc"]
          args:
            - |
              set -euo pipefail
              output_root=${OUTPUT_PATH}
              schema=/aiperf/src/aiperf/common/models/export_models.py
              if grep -qx '    hostname: str | None' "\$schema"; then
                sed -i 's/^    hostname: str | None$/    hostname: str | None = None/' "\$schema"
              fi
              export HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0
              model=\$(/opt/venv/bin/python3 -c 'import json,sys,urllib.request; payload=json.load(urllib.request.urlopen(sys.argv[1], timeout=30)); models=payload.get("data", []); assert len(models) == 1, f"expected exactly one served model, got {models!r}"; print(models[0]["id"])' "${BASE_URL}/models")
              /opt/venv/bin/python3 -c 'from transformers import AutoTokenizer; import sys; AutoTokenizer.from_pretrained(sys.argv[1], trust_remote_code=True)' "\$model"
              overall_status=0
              for concurrency in ${CONCURRENCY_ARGS}; do
                output="\$output_root/c\$concurrency"
                mkdir -p "\$output"
                # Keep the exact source details with the AIPerf output, not
                # only in ephemeral Kubernetes metadata.
                printf '{\n  "run_id": "%s-c%s",\n  "model_label": "%s",\n  "model": "%s",\n  "vllm_build_ref": "%s",\n  "vllm_build_commit": "%s",\n  "topology": "%s",\n  "prefill_gpu_count": %s,\n  "decode_gpu_count": %s,\n  "total_gpu_count": %s,\n  "concurrency": %s,\n  "duration_seconds": %s\n}\n' \\
                  "${RUN_ID}" "\$concurrency" "${MODEL_LABEL}" "\$model" "${VLLM_BUILD_REF}" "${VLLM_BUILD_COMMIT}" "${TOPOLOGY}" "${PREFILL_GPU_COUNT}" "${DECODE_GPU_COUNT}" "${TOTAL_GPU_COUNT}" "\$concurrency" "${DURATION}" \\
                  > "\$output/benchmark-metadata.json"
                if /opt/venv/bin/aiperf profile \\
                --scenario inferencex-agentx-mvp \\
                --url ${BASE_URL} \\
                --model "\$model" \\
                --max-context-length ${MAX_CONTEXT_LENGTH} \\
                --tokenizer-trust-remote-code \\
                --endpoint-type chat \\
                --public-dataset semianalysis_cc_traces_weka_with_subagents \\
                --concurrency "\$concurrency" \\
                --benchmark-duration ${DURATION} \\
                --use-server-token-count \\
                --streaming \\
                --random-seed 42 \\
                --output-artifact-dir "\$output" \\
                --ui simple; then
                  status=0
                else
                  status=\$?
                fi
                cp /benchmark-input/serving-pods.yaml "\$output/serving-pods.yaml"
                cp /benchmark-input/aiperf-job.yaml "\$output/aiperf-job.yaml"
                if [[ -f "\$output/profile_export_aiperf.json" ]]; then
                  /opt/venv/bin/python3 /benchmark-input/aiperf_report.py run "\$output"
                fi
                (( status == 0 )) || overall_status=\$status
              done
              /opt/venv/bin/python3 /benchmark-input/aiperf_report.py index "\$output_root"
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
  --from-file=gen_interactivity_chart.py="$GENERATOR_SCRIPT" \
  --from-file=plotly-basic-2.35.2.min.js.gz="$PLOTLY_BUNDLE" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl create -f "$JOB_MANIFEST"

echo "Job created: ${JOB_NAME}"
echo "vLLM build: ${VLLM_BUILD_REF} (${VLLM_BUILD_COMMIT})"
echo "Logs: kubectl logs -n ${NAMESPACE} -f job/${JOB_NAME}"
for reported_concurrency in "${SWEEP_CONCURRENCIES[@]}"; do
  echo "Artifacts: ${RESULTS_PVC}:${OUTPUT_PATH}/c${reported_concurrency}"
  echo "Portable report: ${RESULTS_PVC}:${OUTPUT_PATH}/c${reported_concurrency}/report.html"
done

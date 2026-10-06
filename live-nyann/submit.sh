#!/usr/bin/env bash
set -euo pipefail

# Submit one nyann-bench Job containing one synthetic stage per concurrency.
# Usage: ./submit.sh <concurrency|comma-separated-sweep> [isl] [osl] [duration_seconds] [warmup_seconds]

CONCURRENCY="${1:-}"
ISL="${2:-${NYANN_ISL:-1024}}"
OSL="${3:-${NYANN_OSL:-512}}"
DURATION="${4:-${NYANN_DURATION:-900}}"
WARMUP="${5:-${NYANN_WARMUP:-60}}"
IFS=',' read -r -a SWEEP_CONCURRENCIES <<< "$CONCURRENCY"

if (( ${#SWEEP_CONCURRENCIES[@]} == 0 )); then
  echo "usage: $0 <concurrency|comma-separated-sweep> [isl] [osl] [duration_seconds] [warmup_seconds]" >&2
  exit 2
fi
for sweep_concurrency in "${SWEEP_CONCURRENCIES[@]}"; do
  if [[ ! "$sweep_concurrency" =~ ^[1-9][0-9]*$ ]] || (( sweep_concurrency > 16384 )); then
    echo "invalid concurrency: ${sweep_concurrency} (expected 1-16384)" >&2
    exit 2
  fi
done
for sequence_length in "$ISL" "$OSL"; do
  if [[ ! "$sequence_length" =~ ^[1-9][0-9]*$ ]] || (( sequence_length > 1000000 )); then
    echo "invalid sequence length: ${sequence_length} (expected 1-1000000)" >&2
    exit 2
  fi
done
if [[ ! "$DURATION" =~ ^[1-9][0-9]*$ ]] || (( DURATION < 1 || DURATION > 7200 )); then
  echo "invalid duration: ${DURATION} (expected 1-7200 seconds)" >&2
  exit 2
fi
if [[ ! "$WARMUP" =~ ^[0-9][0-9]*$ ]] || (( WARMUP > 3600 )); then
  echo "invalid warmup: ${WARMUP} (expected 0-3600 seconds)" >&2
  exit 2
fi

NAMESPACE="${LIVE_NYANN_NAMESPACE:-}"
if [[ -z "$NAMESPACE" ]]; then
  echo "Discovering the deployed vLLM namespace..."
  DEPLOYMENT_NAMESPACES=()
  while IFS=$'\t' read -r namespace commit; do
    [[ "$commit" =~ ^[0-9a-f]{40}$ ]] && DEPLOYMENT_NAMESPACES+=("$namespace")
  done < <(kubectl get configmaps --all-namespaces -o go-template='{{range .items}}{{if eq .metadata.name "vllm-build-ref"}}{{.metadata.namespace}}{{"\t"}}{{index .data "VLLM_BUILD_COMMIT"}}{{"\n"}}{{end}}{{end}}')
  if (( ${#DEPLOYMENT_NAMESPACES[@]} != 1 )); then
    echo "Could not identify exactly one deployed vLLM namespace (found: ${DEPLOYMENT_NAMESPACES[*]:-none})" >&2
    echo "Set LIVE_NYANN_NAMESPACE when choosing intentionally among multiple deployments." >&2
    exit 1
  fi
  NAMESPACE="${DEPLOYMENT_NAMESPACES[0]}"
fi
echo "Using namespace: ${NAMESPACE}"
BENCHMARK_QUEUE="${LIVE_BENCHMARK_QUEUE:-live-benchmark-client}"
if [[ ! "$BENCHMARK_QUEUE" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]] || (( ${#BENCHMARK_QUEUE} > 63 )); then
  echo "invalid LIVE_BENCHMARK_QUEUE: ${BENCHMARK_QUEUE}" >&2
  exit 2
fi
kubectl get localqueue "$BENCHMARK_QUEUE" -n "$NAMESPACE" -o name >/dev/null || {
  echo "Missing Kueue LocalQueue ${NAMESPACE}/${BENCHMARK_QUEUE}; run just live-benchmark-kueue-setup ${NAMESPACE}" >&2
  exit 1
}

BASE_URL="${BASE_URL:-http://llm-d-inference-gateway-istio.${NAMESPACE}.svc.cluster.local/v1}"
NYANN_IMAGE="${NYANN_IMAGE:-ghcr.io/neuralmagic/nyann-bench:latest}"
RESULTS_PVC="${RESULTS_PVC:-kimi-k3-build-cache}"
OUTPUT_ROOT="${OUTPUT_ROOT:-nyann-agentx}"
READY_TIMEOUT="${READY_TIMEOUT:-1800s}"

if [[ -n "${LIVE_BENCHMARK_SOURCE_COMMIT:-}" ]]; then
  SOURCE_REF="${LIVE_BENCHMARK_SOURCE_REF:-}"
  SOURCE_COMMIT="$LIVE_BENCHMARK_SOURCE_COMMIT"
  SOURCE_KIND="${LIVE_BENCHMARK_SOURCE_KIND:-llm-d}"
else
  echo "Reading deployed vLLM build metadata..."
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

DISCOVERED_MODELS=()
echo "Discovering the serving model..."
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
echo "Waiting up to ${READY_TIMEOUT} for serving pods matching ${BASE_SELECTOR}..."
kubectl wait -n "$NAMESPACE" \
  --for=condition=Ready pod \
  -l "$BASE_SELECTOR" \
  --timeout="$READY_TIMEOUT"

RUN_TIMESTAMP="$(date -u +%Y%m%d%H%M%S)"
CONCURRENCY_LABEL="$(IFS=-; echo "${SWEEP_CONCURRENCIES[*]}")"
RUN_ID="${LIVE_NYANN_RUN_ID:-nyann-sweep-c${CONCURRENCY_LABEL}-isl${ISL}-osl${OSL}-vllm-${SOURCE_SHORT}-${RUN_TIMESTAMP}}"
JOB_NAME="nyann-${SOURCE_SHORT}-${RUN_TIMESTAMP}"
ARTIFACT_CONFIGMAP="nyann-artifacts-${SOURCE_SHORT}-${RUN_TIMESTAMP}"
METRICS_SERVICE_NAME="${JOB_NAME}-metrics"
OUTPUT_PATH="/workload/${OUTPUT_ROOT}/${RUN_ID}"

echo "Preparing the benchmark Job..."

POD_SNAPSHOT="$(mktemp "${TMPDIR:-/tmp}/nyann-serving-pods.XXXXXX.yaml")"
JOB_MANIFEST="$(mktemp "${TMPDIR:-/tmp}/nyann-job.XXXXXX.yaml")"
NYANN_CONFIG="$(mktemp "${TMPDIR:-/tmp}/nyann-config.XXXXXX.star")"
trap 'rm -f "$POD_SNAPSHOT" "$JOB_MANIFEST" "$NYANN_CONFIG"' EXIT
kubectl get pods -n "$NAMESPACE" -l "$BASE_SELECTOR" -o yaml > "$POD_SNAPSHOT"

MAX_CONCURRENCY="${SWEEP_CONCURRENCIES[0]}"
for sweep_concurrency in "${SWEEP_CONCURRENCIES[@]}"; do
  (( sweep_concurrency > MAX_CONCURRENCY )) && MAX_CONCURRENCY="$sweep_concurrency"
done

{
  printf 'scenario(\n  stages=['
  if (( WARMUP > 0 )); then
    printf 'stage("%ss", concurrency=%s, warmup=True), ' "$WARMUP" "$MAX_CONCURRENCY"
  fi
  for ((run_index = 0; run_index < ${#SWEEP_CONCURRENCIES[@]}; run_index++)); do
    (( run_index == 0 )) || printf ', '
    printf 'stage("%ss", concurrency=%s)' "$DURATION" "${SWEEP_CONCURRENCIES[$run_index]}"
  done
  printf '],\n  workload=workload("synthetic", isl=%s, osl=%s, turns=1),\n)\n' "$ISL" "$OSL"
} > "$NYANN_CONFIG"

TOTAL_DURATION=$((WARMUP + DURATION * ${#SWEEP_CONCURRENCIES[@]} + 7200))
cat > "$JOB_MANIFEST" <<EOF
apiVersion: v1
kind: Service
metadata:
  name: ${METRICS_SERVICE_NAME}
  namespace: ${NAMESPACE}
  labels:
    benchmark.llm-d.ai/workload: nyann-agentx-mvp
    benchmark.llm-d.ai/job: "${JOB_NAME}"
  annotations:
    prometheus.io/scrape: "true"
    prometheus.io/port: "9090"
    prometheus.io/path: /metrics
spec:
  selector:
    benchmark.llm-d.ai/job: "${JOB_NAME}"
  ports:
    - name: metrics
      port: 9090
      targetPort: metrics
      protocol: TCP
---
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
  labels:
    kueue.x-k8s.io/queue-name: "${BENCHMARK_QUEUE}"
    benchmark.llm-d.ai/workload: nyann-agentx-mvp
    benchmark.llm-d.ai/model: "${MODEL_LABEL}"
    benchmark.llm-d.ai/concurrency: "${CONCURRENCY_LABEL}"
    benchmark.llm-d.ai/isl: "${ISL}"
    benchmark.llm-d.ai/osl: "${OSL}"
    benchmark.llm-d.ai/job: "${JOB_NAME}"
    benchmark.llm-d.ai/source-commit: "${SOURCE_COMMIT}"
  annotations:
    benchmark.llm-d.ai/source-ref: "${SOURCE_REF}"
    benchmark.llm-d.ai/source-kind: "${SOURCE_KIND}"
    benchmark.llm-d.ai/source-commit: "${SOURCE_COMMIT}"
    benchmark.llm-d.ai/run-id: "${RUN_ID}"
spec:
  suspend: true
  backoffLimit: 0
  activeDeadlineSeconds: ${TOTAL_DURATION}
  template:
    metadata:
      labels:
        benchmark.llm-d.ai/workload: nyann-agentx-mvp
        benchmark.llm-d.ai/model: "${MODEL_LABEL}"
        benchmark.llm-d.ai/job: "${JOB_NAME}"
        benchmark.llm-d.ai/source-commit: "${SOURCE_COMMIT}"
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "9090"
        prometheus.io/path: /metrics
        benchmark.llm-d.ai/source-ref: "${SOURCE_REF}"
        benchmark.llm-d.ai/source-kind: "${SOURCE_KIND}"
        benchmark.llm-d.ai/source-commit: "${SOURCE_COMMIT}"
    spec:
      restartPolicy: Never
      containers:
        - name: nyann-bench
          image: ${NYANN_IMAGE}
          imagePullPolicy: IfNotPresent
          ports:
            - name: metrics
              containerPort: 9090
              protocol: TCP
          command: ["/nyann-bench"]
          args:
            - generate
            - --target
            - ${BASE_URL}
            - --config
            - /benchmark-input/nyann-config.star
            - --output-dir
            - ${OUTPUT_PATH}
            - --metrics
            - :9090
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
  --from-file=nyann-config.star="$NYANN_CONFIG" \
  --from-file=nyann-job.yaml="$JOB_MANIFEST" \
  --from-file=serving-pods.yaml="$POD_SNAPSHOT" \
  --dry-run=client -o yaml | kubectl create -f -
kubectl apply -f "$JOB_MANIFEST"

echo "Job queued: ${JOB_NAME} (Kueue LocalQueue ${NAMESPACE}/${BENCHMARK_QUEUE})"
echo "nyann-bench image: ${NYANN_IMAGE}"
echo "Target: ${BASE_URL} (model ${MODEL_LABEL})"
echo "Synthetic workload: ISL=${ISL}, OSL=${OSL}"
echo "Concurrency stages: ${CONCURRENCY_LABEL//-/,}"
echo "Warmup: ${WARMUP}s at concurrency ${MAX_CONCURRENCY}"
echo "Metrics: http://${METRICS_SERVICE_NAME}.${NAMESPACE}.svc.cluster.local:9090/metrics"
echo "Logs: kubectl logs -n ${NAMESPACE} -f job/${JOB_NAME}"
echo "Artifacts: ${RESULTS_PVC}:${OUTPUT_PATH}"

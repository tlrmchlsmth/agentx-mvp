#!/usr/bin/env bash
set -euo pipefail

# Download the self-contained report from the newest submitted AIPerf Job.
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
DESTINATION="${DESTINATION:-${HOME}/Downloads/aiperf-history.html}"
RESULTS_PVC="${RESULTS_PVC:-kimi-k3-build-cache}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

JOB_NAME="$(kubectl get jobs -n "$NAMESPACE" \
  -l benchmark.llm-d.ai/workload=inferencex-agentx-mvp \
  --sort-by=.metadata.creationTimestamp \
  -o name | sed 's#^job.batch/##' | tail -n 1)"
if [[ -z "$JOB_NAME" ]]; then
  echo "No AIPerf Jobs found in ${NAMESPACE}" >&2
  exit 1
fi
RUN_ID="$(kubectl get job -n "$NAMESPACE" "$JOB_NAME" \
  -o jsonpath='{.metadata.annotations.benchmark\.llm-d\.ai/run-id}')"
BUILD_COMMIT="$(kubectl get job -n "$NAMESPACE" "$JOB_NAME" \
  -o jsonpath='{.metadata.labels.benchmark\.llm-d\.ai/vllm-build-commit}')"
JOB_TIMESTAMP="${JOB_NAME##*-}"
if [[ -n "$RUN_ID" ]] && ! [[ "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "Newest AIPerf Job has an unsafe run-id annotation" >&2
  exit 1
fi
if [[ -z "$RUN_ID" ]] && { ! [[ "$BUILD_COMMIT" =~ ^[0-9a-f]{40}$ ]] || ! [[ "$JOB_TIMESTAMP" =~ ^[0-9]{14}$ ]]; }; then
  echo "Cannot locate artifacts for legacy Job ${JOB_NAME}: it has no run-id or build/timestamp identity" >&2
  exit 1
fi

echo "Waiting for ${JOB_NAME} to finish..."
while :; do
  complete="$(kubectl get job -n "$NAMESPACE" "$JOB_NAME" -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}')"
  failed="$(kubectl get job -n "$NAMESPACE" "$JOB_NAME" -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}')"
  if [[ "$complete" == True ]]; then
    break
  fi
  if [[ "$failed" == True ]]; then
    echo "${JOB_NAME} failed; no completed comparison report to download" >&2
    exit 1
  fi
  sleep 20
done

mkdir -p "$(dirname "$DESTINATION")"
RETRIEVER_POD="aiperf-report-download-$$"
REPORTER_CONFIGMAP="aiperf-report-download-$$"
JOB_SNAPSHOT="$(mktemp "${TMPDIR:-/tmp}/aiperf-job.XXXXXX.yaml")"
cleanup() {
  kubectl delete pod -n "$NAMESPACE" "$RETRIEVER_POD" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  kubectl delete configmap -n "$NAMESPACE" "$REPORTER_CONFIGMAP" --ignore-not-found >/dev/null 2>&1 || true
  rm -f "$JOB_SNAPSHOT"
}
trap cleanup EXIT

# A completed Job cannot be exec'd into. Mount the RWX results PVC in this
# short-lived helper pod, regenerate HTML from persisted AIPerf JSON/YAML,
# then stream it out. This is independent of the submission-time UI.
kubectl get job -n "$NAMESPACE" "$JOB_NAME" -o yaml > "$JOB_SNAPSHOT"
kubectl create configmap "$REPORTER_CONFIGMAP" -n "$NAMESPACE" \
  --from-file=aiperf_report.py="${SCRIPT_DIR}/report.py" \
  --from-file=gen_interactivity_chart.py="${SCRIPT_DIR}/../gen_interactivity_chart.py" \
  --from-file=aiperf-job.yaml="$JOB_SNAPSHOT" \
  --from-file=plotly-basic-2.35.2.min.js.gz="${SCRIPT_DIR}/plotly-basic-2.35.2.min.js.gz"
kubectl create -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${RETRIEVER_POD}
  namespace: ${NAMESPACE}
spec:
  restartPolicy: Never
  containers:
    - name: retrieve
      image: python:3.12-alpine
      command: ["sh", "-c", "sleep 600"]
      volumeMounts:
        - name: workload
          mountPath: /workload
        - name: reporter
          mountPath: /reporter
          readOnly: true
  volumes:
    - name: workload
      persistentVolumeClaim:
        claimName: ${RESULTS_PVC}
    - name: reporter
      configMap:
        name: ${REPORTER_CONFIGMAP}
EOF
kubectl wait -n "$NAMESPACE" --for=condition=Ready "pod/${RETRIEVER_POD}" --timeout=180s
if [[ -z "$RUN_ID" ]]; then
  BUILD_SHORT="${BUILD_COMMIT:0:12}"
  RUN_DIR="$(kubectl exec -n "$NAMESPACE" "$RETRIEVER_POD" -- find /workload/aiperf-agentx \
    -mindepth 1 -maxdepth 1 -type d -name "*-vllm-${BUILD_SHORT}-${JOB_TIMESTAMP}" | head -n 1)"
else
  RUN_DIR="/workload/aiperf-agentx/${RUN_ID}"
fi
if [[ -z "$RUN_DIR" ]]; then
  echo "Could not find persisted artifacts for ${JOB_NAME}" >&2
  exit 1
fi
kubectl exec -n "$NAMESPACE" "$RETRIEVER_POD" -- \
  cp /reporter/aiperf-job.yaml "${RUN_DIR}/aiperf-job.yaml"
kubectl exec -n "$NAMESPACE" "$RETRIEVER_POD" -- \
  python3 /reporter/aiperf_report.py index "$RUN_DIR"
kubectl exec -n "$NAMESPACE" "$RETRIEVER_POD" -- cat "${RUN_DIR}/index.html" > "$DESTINATION"
test -s "$DESTINATION"
echo "Downloaded ${JOB_NAME} report to ${DESTINATION}"

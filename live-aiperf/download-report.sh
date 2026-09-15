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
MONITORING="${LIVE_AIPERF_MONITORING:-false}"
case "$MONITORING" in true|false) ;; *) echo "LIVE_AIPERF_MONITORING must be true or false" >&2; exit 2 ;; esac
decode_base64() { base64 --decode 2>/dev/null || base64 -D; }

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
MONITORING_SECRET=""
JOB_SNAPSHOT="$(mktemp "${TMPDIR:-/tmp}/aiperf-job.XXXXXX.yaml")"
JOB_LOG_SNAPSHOT=""
cleanup() {
  kubectl delete pod -n "$NAMESPACE" "$RETRIEVER_POD" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  kubectl delete configmap -n "$NAMESPACE" "$REPORTER_CONFIGMAP" --ignore-not-found >/dev/null 2>&1 || true
  [[ -z "$MONITORING_SECRET" ]] || kubectl delete secret -n "$NAMESPACE" "$MONITORING_SECRET" --ignore-not-found >/dev/null 2>&1 || true
  rm -f "$JOB_SNAPSHOT"
  [[ -z "$JOB_LOG_SNAPSHOT" ]] || rm -f "$JOB_LOG_SNAPSHOT"
}
trap cleanup EXIT

# A completed Job cannot be exec'd into. Mount the RWX results PVC in this
# short-lived helper pod, regenerate HTML from persisted AIPerf JSON/YAML,
# then stream it out. This is independent of the submission-time UI.
kubectl get job -n "$NAMESPACE" "$JOB_NAME" -o yaml > "$JOB_SNAPSHOT"
if [[ "$MONITORING" == true ]]; then
  # Do this before creating the helper: container environment is resolved at
  # startup, and the short-lived Secret must exist then.
  GRAFANA_URL="${LIVE_AIPERF_GRAFANA_URL:-}"
  GRAFANA_SERVICE="${LIVE_AIPERF_GRAFANA_SERVICE:-llmd-grafana}"
  GRAFANA_NAMESPACE="${LIVE_AIPERF_GRAFANA_NAMESPACE:-}"
  if [[ -z "$GRAFANA_URL" ]]; then
    matches="$(kubectl get svc --all-namespaces -o go-template='{{range .items}}{{if eq .metadata.name "llmd-grafana"}}{{.metadata.namespace}}{{"\t"}}{{.metadata.name}}{{"\n"}}{{end}}{{end}}')"
    if [[ -z "$GRAFANA_NAMESPACE" ]]; then
      [[ "$(printf '%s\n' "$matches" | awk 'NF' | wc -l | tr -d ' ')" == 1 ]] || { echo "Could not uniquely discover Grafana; set LIVE_AIPERF_GRAFANA_URL" >&2; exit 1; }
      GRAFANA_NAMESPACE="${matches%%$'\t'*}"
      GRAFANA_SERVICE="${matches#*$'\t'}"
    fi
    GRAFANA_URL="http://${GRAFANA_SERVICE}.${GRAFANA_NAMESPACE}.svc.cluster.local"
  fi
  GRAFANA_SECRET_NAME="${LIVE_AIPERF_GRAFANA_SECRET:-$GRAFANA_SERVICE}"
  GRAFANA_USER="$(kubectl get secret -n "$GRAFANA_NAMESPACE" "$GRAFANA_SECRET_NAME" -o jsonpath='{.data.admin-user}' | decode_base64)"
  GRAFANA_PASSWORD="$(kubectl get secret -n "$GRAFANA_NAMESPACE" "$GRAFANA_SECRET_NAME" -o jsonpath='{.data.admin-password}' | decode_base64)"
  [[ -n "$GRAFANA_USER" && -n "$GRAFANA_PASSWORD" ]] || { echo "Grafana credentials are missing from ${GRAFANA_NAMESPACE}/${GRAFANA_SECRET_NAME}" >&2; exit 1; }
  MONITORING_SECRET="aiperf-grafana-auth-$$"
  kubectl create secret generic "$MONITORING_SECRET" -n "$NAMESPACE" --from-literal=auth="${GRAFANA_USER}:${GRAFANA_PASSWORD}" >/dev/null
  JOB_LOG_SNAPSHOT="$(mktemp "${TMPDIR:-/tmp}/aiperf-job-log.XXXXXX")"
  kubectl logs -n "$NAMESPACE" "job/${JOB_NAME}" --timestamps > "$JOB_LOG_SNAPSHOT"
fi
REPORTER_FILES=(
  --from-file=aiperf_report.py="${SCRIPT_DIR}/report.py"
  --from-file=gen_interactivity_chart.py="${SCRIPT_DIR}/../gen_interactivity_chart.py"
  --from-file=export_dashboard.py="${SCRIPT_DIR}/../export_dashboard.py"
  --from-file=aiperf-job.yaml="$JOB_SNAPSHOT"
  --from-file=plotly-basic-2.35.2.min.js.gz="${SCRIPT_DIR}/plotly-basic-2.35.2.min.js.gz"
)
[[ -z "$JOB_LOG_SNAPSHOT" ]] || REPORTER_FILES+=(--from-file=aiperf-job.log="$JOB_LOG_SNAPSHOT")
kubectl create configmap "$REPORTER_CONFIGMAP" -n "$NAMESPACE" "${REPORTER_FILES[@]}"
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
      env:
        - name: GRAFANA_AUTH
          valueFrom:
            secretKeyRef:
              name: ${MONITORING_SECRET:-aiperf-no-monitoring}
              key: auth
              optional: true
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
if [[ "$MONITORING" == true ]]; then
  kubectl exec -n "$NAMESPACE" "$RETRIEVER_POD" -- \
    cp /reporter/aiperf-job.log "${RUN_DIR}/aiperf-job.log"
  # Scope dashboard PromQL to the serving pods captured with the benchmark,
  # rather than the legacy deployment-name convention.  This is especially
  # important for live llm-d names such as *-prefill-* and *-decode-*.
  kubectl exec -n "$NAMESPACE" "$RETRIEVER_POD" -- sh -c '
    first="$(find "$1" -mindepth 1 -maxdepth 1 -type d -name "c*" | sort | head -n 1)"
    [ -n "$first" ] && [ -f "$first/serving-pods.yaml" ] || exit 1
    awk "\$1 == \"name:\" { print \$2 }" "$first/serving-pods.yaml" | sort -u | paste -sd "|" - > "$1/pods.txt"
    [ -s "$1/pods.txt" ]
  ' sh "$RUN_DIR"
  echo "Capturing Grafana dashboard data for each inferred AIPerf time range..."
  kubectl exec -n "$NAMESPACE" "$RETRIEVER_POD" -- sh -c '
    for directory in "$1"/c*; do
      [ -f "$directory/profile_export_aiperf.json" ] || continue
      # The timestamped Job log supplies the exact profiling start/end; do not
      # use exported metrics or padding, which could include warm-up traffic.
      python3 /reporter/export_dashboard.py --grafana-url "$2" --auth "$GRAFANA_AUTH" --plotly-bundle /reporter/plotly-basic-2.35.2.min.js.gz --aiperf-log "$1/aiperf-job.log" results "$directory" --pad 0
    done
  ' sh "$RUN_DIR" "$GRAFANA_URL"
fi
kubectl exec -n "$NAMESPACE" "$RETRIEVER_POD" -- \
  cp /reporter/aiperf-job.yaml "${RUN_DIR}/aiperf-job.yaml"
kubectl exec -n "$NAMESPACE" "$RETRIEVER_POD" -- \
  python3 /reporter/aiperf_report.py index "$RUN_DIR"
kubectl exec -n "$NAMESPACE" "$RETRIEVER_POD" -- cat "${RUN_DIR}/index.html" > "$DESTINATION"
test -s "$DESTINATION"
echo "Downloaded ${JOB_NAME} report to ${DESTINATION}"

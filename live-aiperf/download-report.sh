#!/usr/bin/env bash
set -euo pipefail

# Download the self-contained report from the newest submitted AIPerf Job.
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
# Zero means retry forever. Set LIVE_AIPERF_EXEC_ATTEMPTS to a positive
# number only when an operator explicitly wants a finite retry cap.
EXEC_ATTEMPTS="${LIVE_AIPERF_EXEC_ATTEMPTS:-0}"
if [[ ! "$EXEC_ATTEMPTS" =~ ^[0-9]+$ ]]; then
  echo "LIVE_AIPERF_EXEC_ATTEMPTS must be a non-negative integer (0 means unlimited)" >&2
  exit 2
fi
# Keep report transfers below the websocket stream size at which kubectl exec
# commonly closes with an unexpected EOF.  Each chunk is copied with kubectl
# cp and the completed partial file remains resumable.
TRANSFER_CHUNK_BYTES=$((1 * 1024 * 1024))
backoff() { local seconds=$(( $1 * 2 )); (( seconds > 15 )) && seconds=15; echo "$seconds"; }
kexec() {
  local attempt=0 retry_limit
  while :; do
    attempt=$((attempt + 1))
    if kubectl -n "$NAMESPACE" --request-timeout=90s exec "$RETRIEVER_POD" -- "$@"; then
      return 0
    fi
    if (( EXEC_ATTEMPTS > 0 && attempt >= EXEC_ATTEMPTS )); then
      echo "  helper exec failed after ${attempt} attempts" >&2
      return 1
    fi
    retry_limit="unlimited"
    (( EXEC_ATTEMPTS > 0 )) && retry_limit="$EXEC_ATTEMPTS"
    echo "  helper exec failed (attempt ${attempt}/${retry_limit}); retrying in $(backoff "$attempt")s..." >&2
    sleep "$(backoff "$attempt")"
  done
}
upload_input() {
  local source="$1" remote="$2" total remote_total attempt=0 retry_limit
  total="$(local_size "$source")"
  [[ "$total" =~ ^[0-9]+$ ]] || { echo "Could not size helper input ${source}" >&2; return 1; }
  while :; do
    attempt=$((attempt + 1))
    if kubectl --request-timeout=90s -n "$NAMESPACE" cp "$source" "${RETRIEVER_POD}:${remote}"; then
      remote_total="$(kexec sh -c 'wc -c < "$1"' sh "$remote" | tr -d '[:space:]')"
      if [[ "$remote_total" == "$total" ]]; then
        return 0
      fi
      echo "Helper input size mismatch for ${source}: local=${total}, remote=${remote_total:-unknown}" >&2
    fi
    if (( EXEC_ATTEMPTS > 0 && attempt >= EXEC_ATTEMPTS )); then
      echo "Helper input upload failed after ${attempt} attempts: ${source}" >&2
      return 1
    fi
    retry_limit="unlimited"
    (( EXEC_ATTEMPTS > 0 )) && retry_limit="$EXEC_ATTEMPTS"
    echo "  helper input copy failed (attempt ${attempt}/${retry_limit}); retrying in $(backoff "$attempt")s..." >&2
    sleep "$(backoff "$attempt")"
  done
}
local_size() { stat -f%z "$1" 2>/dev/null || stat -c%s "$1" 2>/dev/null || echo 0; }
download_report() {
  local remote="$1" partial remote_size remote_digest local_digest offset remaining want chunk remote_chunk attempt=0 retry_limit
  remote_size="$(kexec sh -c 'wc -c < "$1"' sh "$remote" | tr -d '[:space:]')"
  [[ "$remote_size" =~ ^[1-9][0-9]*$ ]] || { echo "Remote report is empty or unreadable" >&2; return 1; }
  remote_digest="$(kexec sha256sum "$remote" | awk '{print $1}')"
  [[ "$remote_digest" =~ ^[0-9a-f]{64}$ ]] || { echo "Could not hash remote report" >&2; return 1; }
  partial="${DESTINATION}.${remote_digest}.part"
  [[ -f "$partial" ]] || : > "$partial"
  offset="$(local_size "$partial")"
  if (( offset > remote_size )); then : > "$partial"; offset=0; fi
  remote_chunk="/tmp/aiperf-report-download-${RETRIEVER_POD}.chunk"
  while (( offset < remote_size )); do
    remaining=$((remote_size - offset)); want=$TRANSFER_CHUNK_BYTES; (( remaining < want )) && want=$remaining
    chunk="${partial}.chunk"
    rm -f "$chunk"
    attempt=0
    while :; do
      attempt=$((attempt + 1))
      if kexec sh -c \
          'tail -c +"$1" "$2" | head -c "$3" > "$4"' \
          sh "$((offset + 1))" "$remote" "$want" "$remote_chunk" \
          && kubectl -n "$NAMESPACE" --request-timeout=90s cp \
               "$RETRIEVER_POD:${remote_chunk}" "$chunk" \
          && [[ "$(local_size "$chunk")" == "$want" ]]; then
        kexec rm -f "$remote_chunk" >/dev/null 2>&1 || true
        cat "$chunk" >> "$partial"
        break
      fi
      kexec rm -f "$remote_chunk" >/dev/null 2>&1 || true
      if (( EXEC_ATTEMPTS > 0 && attempt >= EXEC_ATTEMPTS )); then
        echo "Report transfer failed at byte ${offset} after ${attempt} attempts" >&2
        return 1
      fi
      retry_limit="unlimited"
      (( EXEC_ATTEMPTS > 0 )) && retry_limit="$EXEC_ATTEMPTS"
      echo "  report chunk transfer failed at byte ${offset} (attempt ${attempt}/${retry_limit}); retrying in $(backoff "$attempt")s..." >&2
      sleep "$(backoff "$attempt")"
    done
    offset=$((offset + want))
  done
  rm -f "${partial}.chunk"
  local_digest="$(shasum -a 256 "$partial" | awk '{print $1}')"
  [[ "$local_digest" == "$remote_digest" ]] || { echo "Downloaded report hash mismatch" >&2; rm -f "$partial"; return 1; }
  mv "$partial" "$DESTINATION"
}

JOB_NAME="${LIVE_AIPERF_JOB_NAME:-}"
if [[ -z "$JOB_NAME" ]]; then
  JOB_NAME="$(kubectl get jobs -n "$NAMESPACE" \
    -l benchmark.llm-d.ai/workload=inferencex-agentx-mvp \
    --sort-by=.metadata.creationTimestamp \
    -o name | sed 's#^job.batch/##' | tail -n 1)"
fi
if [[ -z "$JOB_NAME" ]]; then
  echo "No AIPerf Jobs found in ${NAMESPACE}" >&2
  exit 1
fi
RUN_ID="$(kubectl get job -n "$NAMESPACE" "$JOB_NAME" \
  -o jsonpath='{.metadata.annotations.benchmark\.llm-d\.ai/run-id}')"
BUILD_COMMIT="$(kubectl get job -n "$NAMESPACE" "$JOB_NAME" \
  -o jsonpath='{.metadata.labels.benchmark\.llm-d\.ai/source-commit}')"
if [[ -z "$BUILD_COMMIT" ]]; then
  BUILD_COMMIT="$(kubectl get job -n "$NAMESPACE" "$JOB_NAME" \
    -o jsonpath='{.metadata.labels.benchmark\.llm-d\.ai/vllm-build-commit}')"
fi
MODEL_LABEL="$(kubectl get job -n "$NAMESPACE" "$JOB_NAME" \
  -o jsonpath='{.metadata.labels.benchmark\.llm-d\.ai/model}')"
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
JOB_SNAPSHOT="$(mktemp "${TMPDIR:-/tmp}/aiperf-job.XXXXXX")"
LLMD_SNAPSHOT="$(mktemp "${TMPDIR:-/tmp}/aiperf-llmd.XXXXXX.yaml")"
LLMD_SNAPSHOT_GZ="${LLMD_SNAPSHOT}.gz"
JOB_LOG_SNAPSHOT=""
cleanup() {
  kubectl delete pod -n "$NAMESPACE" "$RETRIEVER_POD" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  kubectl delete configmap -n "$NAMESPACE" "$REPORTER_CONFIGMAP" --ignore-not-found >/dev/null 2>&1 || true
  [[ -z "$MONITORING_SECRET" ]] || kubectl delete secret -n "$NAMESPACE" "$MONITORING_SECRET" --ignore-not-found >/dev/null 2>&1 || true
  rm -f "$JOB_SNAPSHOT"
  rm -f "$LLMD_SNAPSHOT" "$LLMD_SNAPSHOT_GZ"
  [[ -z "$JOB_LOG_SNAPSHOT" ]] || rm -f "$JOB_LOG_SNAPSHOT"
}
trap cleanup EXIT

# A completed Job cannot be exec'd into. Mount the RWX results PVC in this
# short-lived helper pod, regenerate HTML from persisted AIPerf JSON/YAML,
# then stream it out. This is independent of the submission-time UI.
kubectl get job -n "$NAMESPACE" "$JOB_NAME" -o yaml > "$JOB_SNAPSHOT"
bash "${SCRIPT_DIR}/capture-llmd-resources.sh" "$NAMESPACE" "$MODEL_LABEL" > "$LLMD_SNAPSHOT"
gzip -c "$LLMD_SNAPSHOT" > "$LLMD_SNAPSHOT_GZ"
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
  [[ -n "$MODEL_LABEL" ]] || { echo "AIPerf Job has no model label; cannot scope monitoring safely" >&2; exit 1; }
fi
REPORTER_FILES=(
  --from-file=aiperf_report.py="${SCRIPT_DIR}/report.py"
  --from-file=gen_interactivity_chart.py="${SCRIPT_DIR}/../gen_interactivity_chart.py"
  --from-file=overlay_dashboards.py="${SCRIPT_DIR}/../overlay_dashboards.py"
  --from-file=export_dashboard.py="${SCRIPT_DIR}/../export_dashboard.py"
  --from-file=plotly-basic-2.35.2.min.js.gz="${SCRIPT_DIR}/plotly-basic-2.35.2.min.js.gz"
)
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
      # A monitoring export can query many dashboards for many repeated
      # profiles before the final report transfer begins. Keep the helper
      # alive for the whole export/transfer window; cleanup() deletes it.
      command: ["sh", "-c", "sleep 3600"]
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
        - name: inputs
          mountPath: /inputs
        - name: reporter
          mountPath: /reporter
          readOnly: true
  volumes:
    - name: workload
      persistentVolumeClaim:
        claimName: ${RESULTS_PVC}
    - name: inputs
      emptyDir: {}
    - name: reporter
      configMap:
        name: ${REPORTER_CONFIGMAP}
EOF
kubectl wait -n "$NAMESPACE" --for=condition=Ready "pod/${RETRIEVER_POD}" --timeout=180s
upload_input "$JOB_SNAPSHOT" /inputs/aiperf-job.yaml
upload_input "$LLMD_SNAPSHOT_GZ" /inputs/llm-d-deployment.yaml.gz
if [[ "$MONITORING" == true ]]; then
  upload_input "$JOB_LOG_SNAPSHOT" /inputs/aiperf-job.log
fi
if [[ -z "$RUN_ID" ]]; then
  BUILD_SHORT="${BUILD_COMMIT:0:12}"
  RUN_DIR="$(kexec find /workload/aiperf-agentx \
    -mindepth 1 -maxdepth 1 -type d -name "*-vllm-${BUILD_SHORT}-${JOB_TIMESTAMP}" | head -n 1)"
else
  RUN_DIR="/workload/aiperf-agentx/${RUN_ID}"
fi
if [[ -z "$RUN_DIR" ]]; then
  echo "Could not find persisted artifacts for ${JOB_NAME}" >&2
  exit 1
fi
if [[ "$MONITORING" == true ]]; then
  pod_scope_source="$(kexec sh -c '
    if [ -s "$1/serving-pods.txt" ]; then echo run; exit 0; fi
    for snapshot in "$1"/c*/serving-pods.yaml; do
      [ -f "$snapshot" ] || continue
      awk '\''/^  metadata:$/ { in_metadata=1; next }
        in_metadata && /^    name: / { print $2; in_metadata=0; next }
        in_metadata && /^  [^ ]/ { in_metadata=0 }'\'' "$snapshot" > "$1/serving-pods.txt"
      if [ -s "$1/serving-pods.txt" ]; then echo submission; exit 0; fi
    done
    echo missing
  ' sh "$RUN_DIR")"
  [[ "$pod_scope_source" != missing ]] || { echo "No serving-pod identity was saved for ${JOB_NAME}; historical monitoring cannot be scoped safely" >&2; exit 1; }
  echo "Monitoring pod scope: saved ${pod_scope_source} snapshot"
  kexec cp /inputs/aiperf-job.log "${RUN_DIR}/aiperf-job.log"
  # Scope PromQL to the saved run identity (or the submission snapshot for older Jobs).
  kexec sh -c '
    tr "\n" "|" < "$1/serving-pods.txt" | sed "s/|\$//" > "$1/pods.txt"
    [ -s "$1/pods.txt" ]
  ' sh "$RUN_DIR"
  echo "Capturing Grafana dashboard data for each inferred AIPerf time range..."
  while IFS= read -r directory; do
    [[ -n "$directory" ]] || continue
    # The timestamped Job log supplies the exact profiling start/end; do not
    # use exported metrics or padding, which could include warm-up traffic.
    kexec sh -c '
      python3 /reporter/export_dashboard.py --grafana-url "$1" --auth "$GRAFANA_AUTH" \
        --plotly-bundle /reporter/plotly-basic-2.35.2.min.js.gz --aiperf-log "$2/aiperf-job.log" \
        results "$3" --pad 0
    ' sh "$GRAFANA_URL" "$RUN_DIR" "$directory"
  done < <(kexec find "$RUN_DIR" -mindepth 1 -maxdepth 1 -type d -name 'c*' | sort)
fi
kexec cp /inputs/aiperf-job.yaml "${RUN_DIR}/aiperf-job.yaml"
kexec python3 -c 'import gzip,pathlib,sys; pathlib.Path(sys.argv[2]).write_bytes(gzip.decompress(pathlib.Path(sys.argv[1]).read_bytes()))' \
  /inputs/llm-d-deployment.yaml.gz "${RUN_DIR}/llm-d-deployment.yaml"
kexec python3 /reporter/aiperf_report.py index "$RUN_DIR"
download_report "${RUN_DIR}/index.html"
test -s "$DESTINATION"
echo "Downloaded ${JOB_NAME} report to ${DESTINATION}"

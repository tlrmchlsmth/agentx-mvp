set dotenv-load
set export

# AIPerf AgentX-MVP benchmark against a manifesto-managed llm-d deployment.
#
# Usage:
#   just setup             # install Kueue objects and deploy the orchestrator
#   just check             # confirm the model endpoint is reachable
#   just run               # run the full AgentX-MVP benchmark as a Job
#   just run 256 900       # override concurrency / duration
#   just smoke             # fast plumbing test (~60s, marks result invalid)
#   just orchestrator-run              # run a detached in-cluster sweep
#   just logs / just shell # inspect the orchestrator
#   just clean             # delete benchmark Jobs and the orchestrator

NAMESPACE := env_var_or_default('NAMESPACE', 'vllm')
repo_root := justfile_directory()
home := env_var_or_default('HOME', '')
manifesto_root := env_var_or_default('MANIFESTO_ROOT', home + '/code/llm-manifesto')
manifesto_spec := env_var_or_default('MODEL_SPEC', 'models/deepseek-v4/3P-EP8-1D-EP8.yaml')
manifesto_cluster := env_var_or_default('MANIFESTO_CLUSTER', 'clusters/oci-gb200.yaml')
manifesto_user := env_var_or_default('MANIFESTO_USER', env_var_or_default('USER', 'dev'))
manifesto_args := env_var_or_default('MANIFESTO_ARGS', '')
kueue_queue := env_var_or_default('KUEUE_QUEUE', 'nightly-eval')
aiperf_image := env_var_or_default('AIPERF_IMAGE', 'quay.io/tms/aiperf:agentx-v0')
lustre_claim := env_var_or_default('LUSTRE_CLAIM', 'lustre-pvc-vllm')
lustre_mount := env_var_or_default('LUSTRE_MOUNT', '/mnt/lustre')
lustre_prefix := env_var_or_default('LUSTRE_PREFIX', '/mnt/lustre/agentx-mvp')
orchestrator_image := env_var_or_default('ORCHESTRATOR_IMAGE', 'quay.io/tms/benchmark-orchestrator:amd64')
orchestrator_manifesto_repo := env_var_or_default('ORCHESTRATOR_MANIFESTO_REPO', 'https://github.com/tlrmchlsmth/llm-manifesto.git')
orchestrator_manifesto_ref := env_var_or_default('ORCHESTRATOR_MANIFESTO_REF', 'main')
orchestrator_deploy := "benchmark-orchestrator"
orchestrator_spec_configmap := "benchmark-orchestrator-spec"
model     := env_var_or_default('MODEL', 'deepseek-ai/DeepSeek-V4-Pro')
model_label := env_var_or_default('MODEL_LABEL', 'DeepSeek-V4-Pro')
max_context_length := env_var_or_default('MAX_CONTEXT_LENGTH', '128000')
url       := env_var_or_default('URL', '')
server_metrics_url := env_var_or_default('SERVER_METRICS_URL', '')
gpu_telemetry_urls := env_var_or_default('GPU_TELEMETRY_URLS', '')
benchmark_retries := env_var_or_default('BENCHMARK_RETRIES', '3')
benchmark_concurrencies := env_var_or_default('BENCHMARK_CONCURRENCIES', '64 256')
pod_start_timeout := env_var_or_default('POD_START_TIMEOUT', '900')
monitoring_namespace := env_var_or_default('MONITORING_NAMESPACE', NAMESPACE)
prometheus_namespace := env_var_or_default('PROMETHEUS_NAMESPACE', monitoring_namespace)
prometheus_service := env_var_or_default('PROMETHEUS_SERVICE', 'prometheus-server')
prometheus_snapshot := env_var_or_default('PROMETHEUS_SNAPSHOT', 'false')
grafana_namespace := env_var_or_default('GRAFANA_NAMESPACE', monitoring_namespace)
grafana_service := env_var_or_default('GRAFANA_SERVICE', 'grafana')
concurrency := "64"
duration    := "900"

default:
    @just --list

_spec-slug:
    #!/usr/bin/env bash
    set -euo pipefail
    spec="$(basename "{{manifesto_spec}}" .yaml)"
    printf '%s\n' "$spec" | tr '[:upper:]' '[:lower:]'

run-dir duration=duration:
    #!/usr/bin/env bash
    set -euo pipefail
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    spec="$(just --quiet _spec-slug)"
    printf 'results/%s_%s_%s_%ss\n' "$ts" "{{manifesto_user}}" "$spec" "{{duration}}"

# Deploy the in-cluster orchestrator pod.
deploy:
    just orchestrator-deploy

# Sanity check: list models served through the llm-d router from an in-cluster probe pod.
check:
    #!/usr/bin/env bash
    set -euo pipefail
    URL=$(just --quiet _model-url)
    NAME="agentx-check-$(date -u +%Y%m%d%H%M%S)"
    kubectl run "$NAME" -n {{NAMESPACE}} --rm -i --restart=Never \
      --image={{orchestrator_image}} \
      --overrides='{"spec":{"nodeSelector":{"kubernetes.io/arch":"amd64"}}}' \
      --env="URL=$URL" \
      --command -- python3 -c "import os, urllib.request as u; print(u.urlopen(os.environ['URL'] + '/models', timeout=10).read().decode())"

# Send a single short request to warm up Triton JIT compilation (up to 10min timeout).
warmup:
    #!/usr/bin/env bash
    set -euo pipefail
    URL=$(just --quiet _model-url)
    echo "Warming up model (this can take several minutes on first request)..."
    attempt=0
    while true; do
        attempt=$((attempt + 1))
        NAME="agentx-warmup-$(date -u +%Y%m%d%H%M%S)-${attempt}"
        if kubectl run "$NAME" -n {{NAMESPACE}} --rm -i --restart=Never \
          --image={{orchestrator_image}} \
          --overrides='{"spec":{"nodeSelector":{"kubernetes.io/arch":"amd64"}}}' \
          --env="URL=$URL" \
          --env="MODEL={{model}}" \
          --command -- python3 -c "
    import os, urllib.request, json
    req = urllib.request.Request(os.environ['URL'] + '/chat/completions',
        data=json.dumps({'model': os.environ['MODEL'], 'messages':[{'role':'user','content':'Hi'}],'max_tokens':8}).encode(),
        headers={'Content-Type':'application/json'})
    resp = urllib.request.urlopen(req, timeout=600).read().decode()
    print(resp)
    "; then
            echo "Warmup complete."
            break
        fi
        echo "Warmup attempt $attempt failed, retrying in 30s..."
        sleep 30
    done

# Run the AgentX-MVP benchmark as a Kueue-managed Kubernetes Job.
run concurrency=concurrency duration=duration dest="./results" attempt="1":
    just _run-job {{concurrency}} {{duration}} "{{dest}}" "" {{attempt}}

_run-job concurrency duration dest unsafe_args attempt:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    URL=$(just --quiet _model-url)
    SERVER_METRICS_ARGS=$(just --quiet _server-metrics-args)
    GPU_TELEMETRY_ARGS=$(just --quiet _gpu-telemetry-args)
    TS=$(date -u +%Y%m%d%H%M%S)
    JOB="agentx-aiperf-c{{concurrency}}-a{{attempt}}-${TS}"
    DEST_CLEAN=$(printf '%s' "{{dest}}" | sed 's#^\./##')
    CANONICAL_ARTIFACT_DIR="{{lustre_prefix}}/{{manifesto_user}}/${DEST_CLEAN}"
    ARTIFACT_DIR="${CANONICAL_ARTIFACT_DIR}_attempt{{attempt}}"
    TIMEOUT=$(({{duration}} + 7200))
    TMP=$(mktemp)
    trap "rm -f $TMP" EXIT
    cat > "$TMP" <<EOF
    apiVersion: batch/v1
    kind: Job
    metadata:
      name: ${JOB}
      labels:
        app: agentx-aiperf
        kueue.x-k8s.io/queue-name: {{kueue_queue}}
    spec:
      suspend: true
      backoffLimit: 0
      activeDeadlineSeconds: ${TIMEOUT}
      template:
        metadata:
          labels:
            app: agentx-aiperf
        spec:
          restartPolicy: Never
          containers:
            - name: aiperf
              image: {{aiperf_image}}
              imagePullPolicy: IfNotPresent
              workingDir: /workspace
              env:
                - name: AIPERF_DATASET_WEKA_LIVE_ASSISTANT_RESPONSES
                  value: "1"
                - name: HF_HOME
                  value: /workspace/.cache/huggingface
                - name: URL
                  value: "${URL}"
                - name: MODEL
                  value: "{{model}}"
                - name: SERVER_METRICS_ARGS
                  value: "${SERVER_METRICS_ARGS}"
                - name: GPU_TELEMETRY_ARGS
                  value: "${GPU_TELEMETRY_ARGS}"
                - name: UNSAFE_ARGS
                  value: "{{unsafe_args}}"
                - name: ARTIFACT_DIR
                  value: "${ARTIFACT_DIR}"
                - name: CANONICAL_ARTIFACT_DIR
                  value: "${CANONICAL_ARTIFACT_DIR}"
              envFrom:
                - secretRef:
                    name: aiperf-hf-token
                    optional: true
              command:
                - /bin/bash
                - -lc
              args:
                - |-
                  set -euo pipefail
                  if [ -z "\$ARTIFACT_DIR" ] || [ "\$ARTIFACT_DIR" = "/" ]; then
                    echo "Refusing to clean unsafe ARTIFACT_DIR='\$ARTIFACT_DIR'" >&2
                    exit 1
                  fi
                  if [ -z "\$CANONICAL_ARTIFACT_DIR" ] || [ "\$CANONICAL_ARTIFACT_DIR" = "/" ]; then
                    echo "Refusing to promote unsafe CANONICAL_ARTIFACT_DIR='\$CANONICAL_ARTIFACT_DIR'" >&2
                    exit 1
                  fi
                  rm -rf "\$ARTIFACT_DIR"
                  mkdir -p "\$ARTIFACT_DIR/logs"
                  set +e
                  /opt/venv/bin/aiperf profile \
                    --scenario inferencex-agentx-mvp \
                    \$UNSAFE_ARGS \
                    --url "\$URL" \
                    --model "\$MODEL" \
                    --max-context-length {{max_context_length}} \
                    --endpoint-type chat \
                    --streaming \
                    --use-server-token-count \
                    --public-dataset semianalysis_cc_traces_weka_with_subagents \
                    --concurrency {{concurrency}} \
                    --benchmark-duration {{duration}} \
                    \$SERVER_METRICS_ARGS \
                    \$GPU_TELEMETRY_ARGS \
                    --output-artifact-dir "\$ARTIFACT_DIR" \
                    --ui simple \
                    2>&1 | tee "\$ARTIFACT_DIR/logs/aiperf.log"
                  STATUS="\${PIPESTATUS[0]}"
                  set -e
                  if [ "\$STATUS" -ne 0 ]; then
                    exit "\$STATUS"
                  fi
                  if [ ! -f "\$ARTIFACT_DIR/profile_export_aiperf.json" ]; then
                    echo "ERROR: profile_export_aiperf.json missing from \$ARTIFACT_DIR after successful aiperf exit" >&2
                    find "\$ARTIFACT_DIR" -maxdepth 2 -type f | sort >&2 || true
                    exit 1
                  fi
                  rm -rf "\$CANONICAL_ARTIFACT_DIR"
                  mkdir -p "\$(dirname "\$CANONICAL_ARTIFACT_DIR")"
                  cp -a "\$ARTIFACT_DIR/." "\$CANONICAL_ARTIFACT_DIR/"
              resources:
                requests:
                  cpu: "4"
                  memory: 8Gi
                limits:
                  cpu: "16"
                  memory: 32Gi
                  ephemeral-storage: 20Gi
              volumeMounts:
                - name: workspace
                  mountPath: /workspace
                - name: lustre
                  mountPath: {{lustre_mount}}
          volumes:
            - name: workspace
              emptyDir:
                sizeLimit: 20Gi
            - name: lustre
              persistentVolumeClaim:
                claimName: {{lustre_claim}}
    EOF
    kubectl apply -n "$NS" -f "$TMP"
    echo "Benchmark job submitted: $JOB"
    echo "Attempt artifacts: $ARTIFACT_DIR"
    echo "Canonical artifacts: $CANONICAL_ARTIFACT_DIR"
    echo "Waiting for benchmark pod..."
    POD=""
    while [ -z "$POD" ]; do
        POD=$(kubectl get pod -n "$NS" -l job-name="$JOB" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
        if [ -z "$POD" ]; then
            sleep 5
        fi
    done
    echo "Benchmark pod created: $POD"
    POD_START_DEADLINE=$((SECONDS + {{pod_start_timeout}}))
    LAST_STATUS=""
    while true; do
        PHASE=$(kubectl get pod -n "$NS" "$POD" -o jsonpath='{.status.phase}' 2>/dev/null || true)
        REASON=$(kubectl get pod -n "$NS" "$POD" -o jsonpath='{.status.containerStatuses[0].state.waiting.reason}' 2>/dev/null || true)
        STATUS="$PHASE"
        if [ -n "$REASON" ]; then
            STATUS="$STATUS/$REASON"
        fi
        if [ "$STATUS" != "$LAST_STATUS" ]; then
            echo "Benchmark pod status: $STATUS"
            LAST_STATUS="$STATUS"
        fi
        case "$PHASE" in
            Running|Succeeded)
                break
                ;;
            Failed)
                kubectl logs -n "$NS" "$POD" --all-containers --tail=200 || true
                kubectl describe -n "$NS" "pod/$POD" || true
                exit 1
                ;;
        esac
        if [ "$SECONDS" -ge "$POD_START_DEADLINE" ]; then
            echo "ERROR: benchmark pod did not start within {{pod_start_timeout}}s"
            kubectl logs -n "$NS" "$POD" --all-containers --tail=200 || true
            kubectl describe -n "$NS" "pod/$POD" || true
            kubectl describe -n "$NS" "job/$JOB" || true
            exit 1
        fi
        sleep 10
    done
    JOB_DEADLINE=$((SECONDS + TIMEOUT))
    while true; do
        COMPLETE=$(kubectl get job -n "$NS" "$JOB" -o jsonpath='{range .status.conditions[?(@.type=="Complete")]}{.status}{end}' 2>/dev/null || true)
        FAILED=$(kubectl get job -n "$NS" "$JOB" -o jsonpath='{range .status.conditions[?(@.type=="Failed")]}{.status}{end}' 2>/dev/null || true)
        if [ "$COMPLETE" = "True" ]; then
            echo "Benchmark job completed: $JOB"
            break
        fi
        if [ "$FAILED" = "True" ]; then
            kubectl logs -n "$NS" "job/$JOB" --all-containers --tail=200 || true
            kubectl describe -n "$NS" "job/$JOB" || true
            exit 1
        fi
        if [ "$SECONDS" -ge "$JOB_DEADLINE" ]; then
            echo "ERROR: benchmark job did not complete within ${TIMEOUT}s"
            kubectl logs -n "$NS" "job/$JOB" --all-containers --tail=200 || true
            kubectl describe -n "$NS" "job/$JOB" || true
            exit 1
        fi
        sleep 10
    done
    SELECTOR=$(just --quiet _pod-selector prefill)
    PVC_POD=$(kubectl get pod -n "$NS" -l "$SELECTOR" -o jsonpath='{.items[0].metadata.name}')
    kubectl exec -n "$NS" "$PVC_POD" -c vllm -- test -f "${CANONICAL_ARTIFACT_DIR}/profile_export_aiperf.json" || {
        echo "ERROR: profile_export_aiperf.json missing from canonical PVC path after job completion"
        kubectl exec -n "$NS" "$PVC_POD" -c vllm -- find "$CANONICAL_ARTIFACT_DIR" -maxdepth 2 -type f 2>/dev/null || true
        exit 1
    }
    mkdir -p "{{dest}}/logs"
    kubectl cp -c vllm "$NS/${PVC_POD}:${CANONICAL_ARTIFACT_DIR}/." "{{dest}}"
    printf '%s\n' "$JOB" > "{{dest}}/job_name.txt"
    printf '%s\n' "$CANONICAL_ARTIFACT_DIR" > "{{dest}}/remote_artifact_dir.txt"
    printf '%s\n' "$ARTIFACT_DIR" > "{{dest}}/remote_attempt_artifact_dir.txt"
    if [ ! -f "{{dest}}/profile_export_aiperf.json" ]; then
        echo "ERROR: profile_export_aiperf.json not found after job completion"
        exit 1
    fi
    kubectl delete -n "$NS" job "$JOB" --ignore-not-found=true

# Fast plumbing validation. Uses --unsafe-override so it runs below the
# scenario's 900s minimum; result is marked submission_valid: false.
smoke concurrency="1" duration="60" dest="results_smoke":
    just _run-job {{concurrency}} {{duration}} "{{dest}}" "--unsafe-override" 1

# End-to-end smoke workflow: run a small profile, copy artifacts, export the
# Grafana dashboard, and build interactivity_vs_throughput.html.
smoke-e2e dest="results_smoke" concurrency="1" duration="60":
    #!/usr/bin/env bash
    set -euo pipefail
    DEST="{{dest}}"
    C="{{concurrency}}"
    D="{{duration}}"
    if [ -e "$DEST" ]; then
        DEST="${DEST}_$(date -u +%Y%m%dT%H%M%SZ)"
    fi
    just smoke "$C" "$D" "$DEST"
    just dump-logs "$DEST"
    just report "$DEST" || true
    just _smoke-interactivity "$DEST" "$C"
    echo "=== Smoke artifacts: $DEST ==="
    echo "  AIPerf:       $DEST/profile_export_aiperf.json"
    echo "  Dashboard:    $DEST/dashboard.html"
    echo "  Interactivity: $DEST/interactivity_vs_throughput.html"

results dest="./results":
    @echo "AIPerf Jobs copy artifacts directly into the run directory; no runner pod copy is needed."

_copy-result-from-pvc remote dest:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    SELECTOR=$(just --quiet _pod-selector prefill)
    PVC_POD=$(kubectl get pod -n "$NS" -l "$SELECTOR" -o jsonpath='{.items[0].metadata.name}')
    if ! kubectl exec -n "$NS" "$PVC_POD" -c vllm -- test -f "{{remote}}/profile_export_aiperf.json" 2>/dev/null; then
        exit 1
    fi
    mkdir -p "{{dest}}"
    kubectl cp -c vllm "$NS/${PVC_POD}:{{remote}}/." "{{dest}}"

# Wait for all running requests to drain on prefill and decode pods.
drain:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    PREFILL_SELECTOR=$(just --quiet _pod-selector prefill)
    DECODE_SELECTOR=$(just --quiet _pod-selector decode)
    echo "Waiting for all requests to drain..."
    while true; do
        TOTAL=0
        for pod in $(kubectl get pods -n "$NS" -l "$PREFILL_SELECTOR" -o jsonpath='{.items[*].metadata.name}'); do
            for port in 8000 8001 8002 8003 8004 8005 8006 8007; do
                N=$(kubectl exec -n "$NS" "$pod" -c vllm -- \
                    curl -sf "http://localhost:${port}/metrics" 2>/dev/null \
                    | grep '^vllm:num_requests_running' | awk '{printf "%d", $2}') || N=0
                TOTAL=$((TOTAL + N))
            done
        done
        for pod in $(kubectl get pods -n "$NS" -l "$DECODE_SELECTOR" -o jsonpath='{.items[*].metadata.name}'); do
            for port in 8200 8201 8202 8203 8204 8205 8206 8207; do
                N=$(kubectl exec -n "$NS" "$pod" -c vllm -- \
                    curl -sf "http://localhost:${port}/metrics" 2>/dev/null \
                    | grep '^vllm:num_requests_running' | awk '{printf "%d", $2}') || N=0
                TOTAL=$((TOTAL + N))
            done
        done
        if [ "$TOTAL" -eq 0 ]; then
            echo "All requests drained."
            break
        fi
        echo "  $TOTAL requests still running, waiting 5s..."
        sleep 5
    done

# Clear NVMe KV cache on all prefill and decode nodes between benchmark runs.
clear-kv-cache:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    PREFILL_SELECTOR=$(just --quiet _pod-selector prefill)
    DECODE_SELECTOR=$(just --quiet _pod-selector decode)
    ALL_WORKER_SELECTOR=$(just --quiet _pod-selector)
    # Reset vLLM prefix cache (GPU + CPU tiers) via API
    for pod in $(kubectl get pods -n "$NS" -l "$PREFILL_SELECTOR" -o jsonpath='{.items[*].metadata.name}'); do
        echo "Resetting prefix cache on prefill $pod..."
        for port in 8000 8001 8002 8003 8004 8005 8006 8007; do
            kubectl exec -n "$NS" "$pod" -c vllm -- \
                curl -sf -X POST "http://localhost:${port}/reset_prefix_cache?reset_external=true" 2>/dev/null || true
        done
    done
    for pod in $(kubectl get pods -n "$NS" -l "$DECODE_SELECTOR" -o jsonpath='{.items[*].metadata.name}'); do
        echo "Resetting prefix cache on decode $pod..."
        for port in 8200 8201 8202 8203 8204 8205 8206 8207; do
            kubectl exec -n "$NS" "$pod" -c vllm -- \
                curl -sf -X POST "http://localhost:${port}/reset_prefix_cache?reset_external=true" 2>/dev/null || true
        done
    done
    # Clear NVMe filesystem tier
    for pod in $(kubectl get pods -n "$NS" -l "$ALL_WORKER_SELECTOR" -o jsonpath='{.items[*].metadata.name}'); do
        echo "Clearing NVMe KV cache on $pod..."
        kubectl exec -n "$NS" "$pod" -c vllm -- rm -rf /mnt/nvme-cache/* 2>/dev/null || true
    done
    echo "All prefix caches reset (GPU + CPU + NVMe)."

# Delete any leftover benchmark Jobs.
wipe:
    kubectl delete job -n {{NAMESPACE}} -l app=agentx-aiperf --ignore-not-found=true

logs:
    kubectl logs -n {{NAMESPACE}} deploy/{{orchestrator_deploy}} -f

shell:
    kubectl exec -it -n {{NAMESPACE}} deploy/{{orchestrator_deploy}} -- bash

clean:
    just wipe
    just orchestrator-clean

# Capture vllm version info from a running deployment into a directory.
vllm-version dest=".":
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    PREFILL_SELECTOR=$(just --quiet _pod-selector prefill)
    # Image tag
    kubectl get pod -n "$NS" -l "$PREFILL_SELECTOR" -o jsonpath='{.items[0].spec.containers[0].image}' > "{{dest}}/vllm_image.txt"
    echo "" >> "{{dest}}/vllm_image.txt"
    # vLLM version from prefill pod startup logs
    POD=$(kubectl get pod -n "$NS" -l "$PREFILL_SELECTOR" -o jsonpath='{.items[0].metadata.name}')
    kubectl logs -n "$NS" "$POD" --all-containers 2>/dev/null \
      | sed -n 's/.*version \([^ ]*\).*/\1/p' | head -1 > "{{dest}}/vllm_version.txt" || true
    echo "vllm version saved to {{dest}}/"
    cat "{{dest}}/vllm_image.txt"
    cat "{{dest}}/vllm_version.txt"

# Dump logs from all involved pods into a directory.
dump-logs dest=".":
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    WORKER_SELECTOR=$(just --quiet _pod-selector)
    INSTANCE=$(just --quiet _manifesto-info | sed -n 's/^instance=//p')
    mkdir -p "{{dest}}/logs"
    for pod in $(kubectl get pods -n "$NS" -l "$WORKER_SELECTOR" -o jsonpath='{.items[*].metadata.name}'); do
        echo "  logs: $pod"
        kubectl logs -n "$NS" "$pod" --all-containers > "{{dest}}/logs/${pod}.log" 2>&1 || true
    done
    # EPP
    for pod in $(kubectl get pods -n "$NS" -l "app.kubernetes.io/instance=${INSTANCE},llm-d-router-gateway=wide-ep-lws-epp" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
        echo "  logs: $pod"
        kubectl logs -n "$NS" "$pod" --all-containers > "{{dest}}/logs/${pod}.log" 2>&1 || true
    done
    # Gateway
    for pod in $(kubectl get pods -n "$NS" -l "app.kubernetes.io/instance=${INSTANCE},gateway.networking.k8s.io/gateway-name" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
        echo "  logs: $pod"
        kubectl logs -n "$NS" "$pod" --all-containers > "{{dest}}/logs/${pod}.log" 2>&1 || true
    done
    echo "Logs saved to {{dest}}/logs/"

# Export Grafana dashboards for result directories.
# Usage: just scrape-grafana results/<run>/results_$USER-wide-ep-3p-ep8-1d-ep8/results_$USER-wide-ep-3p-ep8-1d-ep8_c64
scrape-grafana +dirs:
    python3 export_dashboard.py results {{dirs}}

# Scrape Grafana dashboards and generate interactivity chart.
# Reads namespace.txt from each result directory to find the right Grafana instance.
# Runs export_dashboard.py inside the orchestrator pod via kubectl exec (no port-forward needed).
# Usage: just report results/<run>
report outdir:
    #!/usr/bin/env bash
    set -euo pipefail
    DIRS=$(find "{{outdir}}" -name "profile_export_aiperf.json" -exec dirname {} \;)
    if [ -z "$DIRS" ]; then
        echo "No result directories found in {{outdir}}"
        exit 1
    fi
    SETUP_NAMESPACES=""
    for dir in $DIRS; do
        PARENT=$(dirname "$dir")
        if [ -f "$PARENT/namespace.txt" ]; then
            NS=$(cat "$PARENT/namespace.txt")
        else
            NS={{NAMESPACE}}
        fi
        # Copy script to pod once per namespace
        if ! echo "$SETUP_NAMESPACES" | grep -q "|${NS}|"; then
            POD=$(kubectl get pod -n "$NS" -l app={{orchestrator_deploy}} -o jsonpath='{.items[0].metadata.name}')
            kubectl cp export_dashboard.py "$NS/${POD}:/workspace/export_dashboard.py"
            SETUP_NAMESPACES="${SETUP_NAMESPACES}|${NS}|${POD}|"
        fi
        POD=$(echo "$SETUP_NAMESPACES" | grep -o "|${NS}|[^|]*|" | head -1 | cut -d'|' -f3)
        GRAFANA_URL="http://{{grafana_service}}.{{grafana_namespace}}.svc.cluster.local:80"
        NAME=$(basename "$dir")
        if [ -f "$PARENT/config_name.txt" ]; then
            DEPLOYMENT=$(cat "$PARENT/config_name.txt")
        else
            DEPLOYMENT=${NAME#results_}
            DEPLOYMENT=${DEPLOYMENT%_c*}
        fi
        if [ -f "$PARENT/pods.txt" ]; then
            POD_REGEX=$(cat "$PARENT/pods.txt")
        else
            POD_REGEX="$DEPLOYMENT"
        fi
        echo "=== $NAME ($NS): scraping Grafana ==="
        TIMESTAMPS=$(python3 -c "import json; d=json.load(open('$dir/profile_export_aiperf.json')); print(d['min_request_timestamp']['avg']/1e9-60); print(d['max_response_timestamp']['avg']/1e9+60)")
        START=$(echo "$TIMESTAMPS" | head -1)
        END=$(echo "$TIMESTAMPS" | tail -1)
        echo "  Time range: $START → $END"
        echo "  Deployment: $DEPLOYMENT; pod filter seed: $POD_REGEX"
        kubectl exec -n "$NS" "$POD" -- python3 /workspace/export_dashboard.py \
            --grafana-url "$GRAFANA_URL" \
            --deployment "$DEPLOYMENT" \
            --pod-regex "$POD_REGEX" \
            single --start "$START" --end "$END" -o "/workspace/dashboard_${NAME}.html" || {
            echo "  WARNING: scrape failed for $NAME, skipping"
            continue
        }
        kubectl cp "$NS/${POD}:/workspace/dashboard_${NAME}.html" "$dir/dashboard.html" 2>/dev/null || true
        kubectl exec -n "$NS" "$POD" -- rm -f "/workspace/dashboard_${NAME}.html"
    done
    # Snapshot Prometheus TSDB for each namespace (preserves all metrics)
    SNAPPED=""
    for dir in $DIRS; do
        PARENT=$(dirname "$dir")
        if [ -f "$PARENT/namespace.txt" ]; then
            NS=$(cat "$PARENT/namespace.txt")
        else
            NS={{NAMESPACE}}
        fi
        if echo "$SNAPPED" | grep -q "|${NS}|"; then continue; fi
        SNAPPED="${SNAPPED}|${NS}|"
        if [ "{{prometheus_snapshot}}" = "true" ]; then
            just snapshot-prometheus "$NS" "$PARENT"
        else
            echo "=== Prometheus TSDB snapshot disabled (PROMETHEUS_SNAPSHOT=false) ==="
        fi
    done
    python3 gen_interactivity_chart.py "{{outdir}}" 2>/dev/null || true


# Install Kueue objects and deploy the benchmark orchestrator.
setup:
    just setup-kueue
    just orchestrator-deploy
    echo "=== Benchmark orchestrator ready in {{NAMESPACE}} ==="

# Install the same GB200 Kueue queue objects used by nightly-eval.
setup-kueue:
    kubectl apply -f kueue/resource-flavor.yaml
    kubectl apply -f kueue/cluster-queue.yaml
    kubectl apply -f kueue/local-queue.yaml -n {{NAMESPACE}}

_manifesto-info:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{manifesto_root}}"
    uv run python - <<'PY'
    from manifesto.cluster import load_cluster
    from manifesto.instance import Instance
    from manifesto.spec import load_spec
    spec = load_spec("{{manifesto_spec}}", load_cluster("{{manifesto_cluster}}"))
    instance = Instance("{{manifesto_user}}", spec.release).instance_id
    roles = {r.name: r for r in spec.roles}
    def role_gpus(name):
        r = roles.get(name)
        return 0 if r is None else r.lws.size * r.lws.replicas * r.gpus_per_pod
    print(f"instance={instance}")
    print(f"model={spec.model.id}")
    print(f"model_label={spec.model.label}")
    print(f"release={spec.release}")
    print(f"decode_gpus={role_gpus('decode')}")
    print(f"prefill_gpus={role_gpus('prefill')}")
    print(f"pods={spec.release}")
    PY

_model-url:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "{{url}}" ]; then
        printf '%s\n' "{{url}}"
        exit 0
    fi
    cd "{{manifesto_root}}"
    GATEWAY_SVC=$(uv run manifesto name "{{manifesto_spec}}" inference-gateway-istio --user "{{manifesto_user}}")
    printf 'http://%s:80/v1\n' "$GATEWAY_SVC"

_server-metrics-args:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "{{server_metrics_url}}" ]; then
        printf -- '--server-metrics %s\n' "{{server_metrics_url}}"
        exit 0
    fi
    URL=$(just --quiet _model-url)
    BASE="${URL%/v1}"
    printf -- '--server-metrics %s/metrics\n' "$BASE"

_gpu-telemetry-args:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "{{gpu_telemetry_urls}}" ]; then
        printf -- '--gpu-telemetry %s\n' "{{gpu_telemetry_urls}}"
        exit 0
    fi
    INFO=$(just --quiet _manifesto-info)
    INSTANCE=$(printf '%s\n' "$INFO" | sed -n 's/^instance=//p')
    URLS=$(kubectl get pod -n "{{NAMESPACE}}" \
        -l "app.kubernetes.io/instance=${INSTANCE},llm-d.ai/role" \
        -o jsonpath='{range .items[*]}http://{.status.podIP}:9400/metrics{" "}{end}' 2>/dev/null \
        | xargs)
    if [ -n "$URLS" ]; then
        printf -- '--gpu-telemetry %s\n' "$URLS"
    else
        printf -- '--no-gpu-telemetry\n'
    fi

_pod-selector role="":
    #!/usr/bin/env bash
    set -euo pipefail
    INFO=$(just --quiet _manifesto-info)
    INSTANCE=$(printf '%s\n' "$INFO" | sed -n 's/^instance=//p')
    if [ -n "{{role}}" ]; then
        printf 'app.kubernetes.io/instance=%s,llm-d.ai/role=%s\n' "$INSTANCE" "{{role}}"
    else
        printf 'app.kubernetes.io/instance=%s,llm-d.ai/role\n' "$INSTANCE"
    fi

_smoke-interactivity dest concurrency:
    #!/usr/bin/env bash
    set -euo pipefail
    DEST="{{dest}}"
    C="{{concurrency}}"
    if [ ! -f "$DEST/profile_export_aiperf.json" ]; then
        echo "ERROR: $DEST/profile_export_aiperf.json not found"
        exit 1
    fi
    INFO=$(just --quiet _manifesto-info)
    MODEL_LABEL=$(printf '%s\n' "$INFO" | sed -n 's/^model_label=//p')
    RELEASE=$(printf '%s\n' "$INFO" | sed -n 's/^release=//p')
    DECODE_GPUS=$(printf '%s\n' "$INFO" | sed -n 's/^decode_gpus=//p')
    PREFILL_GPUS=$(printf '%s\n' "$INFO" | sed -n 's/^prefill_gpus=//p')
    PODS=$(printf '%s\n' "$INFO" | sed -n 's/^pods=//p')
    SPEC="{{manifesto_spec}}"
    MODEL_DIR=$(basename "$(dirname "$SPEC")")
    SPEC_NAME=$(basename "$SPEC" .yaml)
    CONFIG_NAME=$(printf '%s-%s' "$MODEL_DIR" "$SPEC_NAME" | tr '[:upper:]' '[:lower:]')
    WORK="$DEST/_interactivity"
    RUN_DIR="$WORK/results_${CONFIG_NAME}/results_${CONFIG_NAME}_c${C}"
    rm -rf "$WORK"
    mkdir -p "$RUN_DIR"
    for f in profile_export_aiperf.json profile_export_aiperf.csv profile_export.jsonl profile_export_console.txt server_metrics_export.json server_metrics_export.csv gpu_telemetry_export.jsonl dashboard.html; do
        [ -f "$DEST/$f" ] && cp "$DEST/$f" "$RUN_DIR/$f"
    done
    if [ -f "rendered-manifests/${CONFIG_NAME}.yaml" ]; then
        cp "rendered-manifests/${CONFIG_NAME}.yaml" "$WORK/results_${CONFIG_NAME}/manifest.yaml"
    elif [ -f "$DEST/manifest.yaml" ]; then
        cp "$DEST/manifest.yaml" "$WORK/results_${CONFIG_NAME}/manifest.yaml"
    fi
    [ -d "$DEST/logs" ] && cp -R "$DEST/logs" "$RUN_DIR/logs"
    printf '%s\n' "$MODEL_LABEL" > "$WORK/results_${CONFIG_NAME}/model_label.txt"
    printf '%s smoke\n' "$RELEASE" > "$WORK/results_${CONFIG_NAME}/config_label.txt"
    printf '%s\n' "$CONFIG_NAME" > "$WORK/results_${CONFIG_NAME}/config_name.txt"
    printf '%s\n' "$DECODE_GPUS" > "$WORK/results_${CONFIG_NAME}/decode_gpus.txt"
    printf '%s\n' "$PREFILL_GPUS" > "$WORK/results_${CONFIG_NAME}/prefill_gpus.txt"
    printf '%s\n' "$PODS" > "$WORK/results_${CONFIG_NAME}/pods.txt"
    python3 gen_interactivity_chart.py "$WORK"
    mv "$WORK/interactivity_vs_throughput.html" "$DEST/interactivity_vs_throughput.html"

start-model:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{manifesto_root}}"
    uv run manifesto render "{{manifesto_spec}}" --cluster "{{manifesto_cluster}}" --namespace "{{NAMESPACE}}" --user "{{manifesto_user}}" {{manifesto_args}} \
        | uv run python "{{repo_root}}/inject_kueue_queue.py" --queue "{{kueue_queue}}" \
        | kubectl apply -n "{{NAMESPACE}}" -f -
    MANIFESTO_NAMESPACE="{{NAMESPACE}}" MANIFESTO_CLUSTER="{{manifesto_cluster}}" USER="{{manifesto_user}}" \
        just ready "{{manifesto_spec}}"
    cd "{{repo_root}}"
    just clear-kv-cache

stop-model:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{manifesto_root}}"
    uv run manifesto render "{{manifesto_spec}}" --cluster "{{manifesto_cluster}}" --namespace "{{NAMESPACE}}" --user "{{manifesto_user}}" {{manifesto_args}} \
        | uv run python "{{repo_root}}/inject_kueue_queue.py" --queue "{{kueue_queue}}" \
        | kubectl delete -n "{{NAMESPACE}}" -f - --ignore-not-found=true

sweep-concurrency config_name dest="." duration="900":
    #!/usr/bin/env bash
    set -uo pipefail
    FAILED=""
    for C in {{benchmark_concurrencies}}; do
        RDIR="{{dest}}/results_{{config_name}}_c${C}"
        DEST_CLEAN=$(printf '%s' "$RDIR" | sed 's#^\./##')
        REMOTE="{{lustre_prefix}}/{{manifesto_user}}/${DEST_CLEAN}"
        if [ ! -f "$RDIR/profile_export_aiperf.json" ] && just _copy-result-from-pvc "$REMOTE" "$RDIR" 2>/dev/null; then
            echo "=== concurrency=$C restored from PVC, skipping ==="
        fi
        if [ -f "$RDIR/profile_export_aiperf.json" ]; then
            echo "=== concurrency=$C already exists, skipping ==="
            continue
        fi
        echo "=== concurrency=$C ({{duration}}s) ==="
        RUN_OK=false
        attempt=1
        while [ "$attempt" -le "{{benchmark_retries}}" ]; do
            if [ "$attempt" -gt 1 ]; then
                echo "=== concurrency=$C retry $attempt/{{benchmark_retries}} ==="
            fi
            just drain
            just clear-kv-cache
            if just warmup && just run $C {{duration}} "$RDIR" "$attempt"; then
                RUN_OK=true
                break
            fi
            echo "Attempt $attempt/{{benchmark_retries}} failed for concurrency=$C"
            just wipe 2>/dev/null || true
            attempt=$((attempt + 1))
            if [ "$attempt" -le "{{benchmark_retries}}" ]; then
                sleep 30
            fi
        done
        if [ "$RUN_OK" != true ]; then
            echo "FAILED: concurrency=$C, skipping"
            FAILED="${FAILED} c${C}"
            continue
        fi
        just dump-logs "$RDIR"
        for attempt in 1 2 3 4 5; do
            if just report "{{dest}}"; then break; fi
            echo "Dashboard scrape attempt $attempt failed for c${C}, retrying in 10s..."
            sleep 10
        done
        sleep 10
    done
    if [ -n "$FAILED" ]; then
        echo "Failed concurrency levels:${FAILED}"
    fi

sweep outdir duration="900":
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p "{{outdir}}"
    INFO=$(just --quiet _manifesto-info)
    CONFIG_NAME=$(printf '%s\n' "$INFO" | sed -n 's/^instance=//p')
    MODEL_ID=$(printf '%s\n' "$INFO" | sed -n 's/^model=//p')
    MODEL_LABEL=$(printf '%s\n' "$INFO" | sed -n 's/^model_label=//p')
    RELEASE=$(printf '%s\n' "$INFO" | sed -n 's/^release=//p')
    DECODE_GPUS=$(printf '%s\n' "$INFO" | sed -n 's/^decode_gpus=//p')
    PREFILL_GPUS=$(printf '%s\n' "$INFO" | sed -n 's/^prefill_gpus=//p')
    PODS=$(printf '%s\n' "$INFO" | sed -n 's/^pods=//p')
    dir="{{outdir}}/results_${CONFIG_NAME}"
    ALL_DONE=true
    for C in {{benchmark_concurrencies}}; do
        if [ ! -f "$dir/results_${CONFIG_NAME}_c${C}/profile_export_aiperf.json" ]; then
            ALL_DONE=false
            break
        fi
    done
    if [ "$ALL_DONE" = true ]; then
        echo "====== ${CONFIG_NAME} all concurrency levels done, skipping ======"
        exit 0
    fi
    echo "====== ${CONFIG_NAME} (${RELEASE}) ======"
    just stop-model 2>/dev/null || true
    just start-model
    just check
    just warmup
    mkdir -p "$dir"
    echo "{{NAMESPACE}}" > "$dir/namespace.txt"
    echo "$MODEL_ID" > "$dir/model.txt"
    echo "$MODEL_LABEL" > "$dir/model_label.txt"
    echo "$CONFIG_NAME" > "$dir/config_name.txt"
    echo "$RELEASE" > "$dir/config_label.txt"
    echo "$DECODE_GPUS" > "$dir/decode_gpus.txt"
    echo "$PREFILL_GPUS" > "$dir/prefill_gpus.txt"
    echo "$PODS" > "$dir/pods.txt"
    echo "{{manifesto_spec}}" > "$dir/manifesto_spec.txt"
    (cd "{{manifesto_root}}" && uv run manifesto instance-id "{{manifesto_spec}}" --user "{{manifesto_user}}") > "$dir/manifesto_instance.txt"
    (cd "{{manifesto_root}}" && uv run manifesto render "{{manifesto_spec}}" --cluster "{{manifesto_cluster}}" --namespace "{{NAMESPACE}}" --user "{{manifesto_user}}" {{manifesto_args}}) \
        | uv run python "{{repo_root}}/inject_kueue_queue.py" --queue "{{kueue_queue}}" \
        > "$dir/manifest.yaml"
    just vllm-version "$dir"
    just sweep-concurrency "$CONFIG_NAME" "$dir" {{duration}}
    just stop-model

snapshot-prometheus ns dest:
    #!/usr/bin/env bash
    set -euo pipefail
    PROM_NS="{{prometheus_namespace}}"
    PROM_POD=$(kubectl get pod -n "$PROM_NS" -l app.kubernetes.io/name=prometheus,app.kubernetes.io/component=server -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || {
        echo "  No Prometheus pod in $PROM_NS, skipping snapshot"
        exit 0
    }
    echo "=== $PROM_NS: snapshotting Prometheus TSDB for {{ns}} ==="
    SNAP_NAME=$(kubectl exec -n "$PROM_NS" "$PROM_POD" -c prometheus-server -- \
        wget -qO- --post-data= http://localhost:9090/api/v1/admin/tsdb/snapshot 2>/dev/null \
        | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['name'])" 2>/dev/null) || {
        echo "  WARNING: snapshot failed for $PROM_NS, skipping"
        exit 0
    }
    SNAP_DIR="{{dest}}/prometheus_snapshot"
    mkdir -p "$SNAP_DIR"
    kubectl cp "$PROM_NS/${PROM_POD}:/data/snapshots/${SNAP_NAME}" "$SNAP_DIR" -c prometheus-server 2>/dev/null || {
        echo "  WARNING: snapshot copy failed for $PROM_NS"
        exit 0
    }
    kubectl exec -n "$PROM_NS" "$PROM_POD" -c prometheus-server -- rm -rf "/data/snapshots/${SNAP_NAME}" 2>/dev/null || true
    echo "  Saved to $SNAP_DIR"

orchestrator-build:
    podman build --platform linux/amd64 \
      --build-arg MANIFESTO_REPO="{{orchestrator_manifesto_repo}}" \
      --build-arg MANIFESTO_REF="{{orchestrator_manifesto_ref}}" \
      -f Dockerfile.orchestrator -t {{orchestrator_image}} .
    podman push {{orchestrator_image}}

orchestrator-spec-config:
    #!/usr/bin/env bash
    set -euo pipefail
    TMP=$(mktemp)
    trap "rm -f $TMP" EXIT
    {
        printf "BENCHMARK_CONCURRENCIES='%s'\n" "{{benchmark_concurrencies}}"
        printf "BENCHMARK_RETRIES='%s'\n" "{{benchmark_retries}}"
        printf "POD_START_TIMEOUT='%s'\n" "{{pod_start_timeout}}"
    } > "$TMP"
    kubectl create configmap {{orchestrator_spec_configmap}} -n {{NAMESPACE}} \
      --from-file=benchmark-sweep.env="$TMP" \
      --dry-run=client -o yaml | kubectl apply -n {{NAMESPACE}} -f -

orchestrator-deploy:
    #!/usr/bin/env bash
    set -euo pipefail
    just orchestrator-spec-config
    kubectl apply -n {{NAMESPACE}} -f orchestrator.yaml
    kubectl set image -n {{NAMESPACE}} deploy/{{orchestrator_deploy}} orchestrator={{orchestrator_image}}
    kubectl rollout restart -n {{NAMESPACE}} deploy/{{orchestrator_deploy}}
    kubectl rollout status deploy/{{orchestrator_deploy}} -n {{NAMESPACE}} --timeout=300s
    POD=$(kubectl get pod -n {{NAMESPACE}} -l app={{orchestrator_deploy}} -o jsonpath='{.items[0].metadata.name}')
    echo "Orchestrator pod ready: $POD"

orchestrator-run outdir="" duration="900":
    #!/usr/bin/env bash
    set -euo pipefail
    OUTDIR="{{outdir}}"
    if [ -z "$OUTDIR" ]; then
        OUTDIR=$(just --quiet run-dir "{{duration}}")
    fi
    just orchestrator-deploy
    NS={{NAMESPACE}}
    POD=$(kubectl get pod -n "$NS" -l app={{orchestrator_deploy}} -o jsonpath='{.items[0].metadata.name}')
    kubectl exec -n "$NS" "$POD" -- env \
      JUST_NO_DOTENV=true \
      NAMESPACE="{{NAMESPACE}}" \
      MODEL_SPEC="{{manifesto_spec}}" \
      MANIFESTO_ROOT="/workspace/llm-manifesto" \
      MANIFESTO_CLUSTER="{{manifesto_cluster}}" \
      MANIFESTO_USER="{{manifesto_user}}" \
      MANIFESTO_ARGS="{{manifesto_args}}" \
      KUEUE_QUEUE="{{kueue_queue}}" \
      MAX_CONTEXT_LENGTH="{{max_context_length}}" \
      SWEEP_OUTDIR="$OUTDIR" \
      SWEEP_DURATION="{{duration}}" \
      bash -lc 'set -euo pipefail; set -a; [ -f /workspace/benchmark-sweep.env ] && . /workspace/benchmark-sweep.env; set +a; cd /workspace/agentx-mvp; rm -f /workspace/orchestrator-sweep.exit_code; : > /workspace/orchestrator-sweep.log; nohup bash -lc '"'"'just sweep "$SWEEP_OUTDIR" "$SWEEP_DURATION" > /workspace/orchestrator-sweep.log 2>&1; code=$?; echo "$code" > /workspace/orchestrator-sweep.exit_code; rm -f /workspace/orchestrator-sweep.pid; exit "$code"'"'"' </dev/null >/dev/null 2>&1 & pid=$!; echo "$pid" > /workspace/orchestrator-sweep.pid; echo "Launched PID $pid"'
    echo "Sweep running detached: $OUTDIR"
    echo "Monitor: just orchestrator-logs"
    echo "Copy results: just orchestrator-results $OUTDIR"

orchestrator-logs:
    POD=$(kubectl get pod -n {{NAMESPACE}} -l app={{orchestrator_deploy}} -o jsonpath='{.items[0].metadata.name}')
    kubectl exec -n {{NAMESPACE}} "$POD" -- tail -f /workspace/orchestrator-sweep.log

orchestrator-results outdir:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    POD=$(kubectl get pod -n "$NS" -l app={{orchestrator_deploy}} -o jsonpath='{.items[0].metadata.name}')
    DIRS=$(kubectl exec -n "$NS" "$POD" -- find /workspace/agentx-mvp/{{outdir}} -name "profile_export_aiperf.json" -exec dirname {} \; 2>/dev/null)
    for dir in $DIRS; do
        LOCAL=${dir#/workspace/agentx-mvp/}
        mkdir -p "$LOCAL"
        kubectl cp "$NS/${POD}:${dir}" "$LOCAL" 2>/dev/null || true
    done
    EXTRAS=$(kubectl exec -n "$NS" "$POD" -- find /workspace/agentx-mvp/{{outdir}} -maxdepth 2 \( -name "*.yaml" -o -name "*.txt" -o -name "*.html" \) 2>/dev/null)
    for f in $EXTRAS; do
        LOCAL=${f#/workspace/agentx-mvp/}
        mkdir -p "$(dirname "$LOCAL")"
        kubectl cp "$NS/${POD}:${f}" "$LOCAL" 2>/dev/null || true
    done
    echo "Results copied to {{outdir}}/"
    python3 gen_interactivity_chart.py "{{outdir}}" 2>/dev/null || true

orchestrator-stop:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    POD=$(kubectl get pod -n "$NS" -l app={{orchestrator_deploy}} -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || true
    if [ -n "$POD" ]; then
        kubectl exec -n "$NS" "$POD" -- bash -c 'kill $(cat /workspace/orchestrator-sweep.pid 2>/dev/null) 2>/dev/null; rm -f /workspace/orchestrator-sweep.pid' 2>/dev/null || true
    fi
    just stop-model 2>/dev/null || true
    echo "Sweep stopped; namespace preserved."

orchestrator-clean:
    #!/usr/bin/env bash
    set -euo pipefail
    kubectl delete deploy {{orchestrator_deploy}} -n {{NAMESPACE}} --ignore-not-found
    kubectl delete rolebinding benchmark-orchestrator -n {{NAMESPACE}} --ignore-not-found
    kubectl delete role benchmark-orchestrator -n {{NAMESPACE}} --ignore-not-found
    kubectl delete clusterrolebinding benchmark-orchestrator --ignore-not-found 2>/dev/null || true
    kubectl delete sa benchmark-orchestrator -n {{NAMESPACE}} --ignore-not-found
    echo "Orchestrator pod cleaned up."

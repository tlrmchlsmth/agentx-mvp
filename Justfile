set dotenv-load
set export

# AIPerf AgentX-MVP benchmark against the running llm-d optimized-baseline deployment.
#
# Usage:
#   just setup             # install Kueue objects and deploy the orchestrator
#   just check             # confirm the model endpoint is reachable
#   just run               # run the full AgentX-MVP benchmark as a Job
#   just run 16 900        # override concurrency / duration
#   just smoke             # fast plumbing test (~60s, marks result invalid)
#   just orchestrator-run outdir 900   # run a detached in-cluster sweep
#   just logs / just shell # inspect the orchestrator
#   just clean             # delete benchmark Jobs and the orchestrator

NAMESPACE := env_var_or_default('NAMESPACE', 'vllm')
deploy    := env_var_or_default('DEPLOY', 'aiperf-agentx')
repo_root := justfile_directory()
home := env_var_or_default('HOME', '')
manifesto_root := env_var_or_default('MANIFESTO_ROOT', home + '/code/llm-manifesto')
manifesto_spec := env_var_or_default('MODEL_SPEC', 'models/deepseek-v4/1P-EP8-1D-EP8.yaml')
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
seed_image := env_var_or_default('SEED_IMAGE', 'quay.io/tms/benchmark-seed:amd64')
seed_deploy := env_var_or_default('SEED_DEPLOY', 'benchmark-seed')
model     := env_var_or_default('MODEL', 'deepseek-ai/DeepSeek-V4-Pro')
model_label := env_var_or_default('MODEL_LABEL', 'DeepSeek-V4-Pro')
max_context_length := env_var_or_default('MAX_CONTEXT_LENGTH', '128000')
url       := env_var_or_default('URL', 'http://llm-d-inference-gateway-istio:80/v1')
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
sweep_concurrencies := "64 128 256"

llm_d_root := env_var('LLM_D_ROOT')

default:
    @just --list

# Deploy the in-cluster orchestrator pod.
deploy:
    just orchestrator-deploy

# Sanity check: list models served through the llm-d router from inside the runner.
# Retries for up to 5 minutes while the gateway/EPP discovers endpoints.
check:
    #!/usr/bin/env bash
    set -euo pipefail
    for attempt in $(seq 1 30); do
        if kubectl exec -n {{NAMESPACE}} deploy/{{deploy}} -- \
          python -c "import urllib.request as u; print(u.urlopen('{{url}}/models', timeout=10).read().decode())" 2>/dev/null; then
            exit 0
        fi
        echo "Check attempt $attempt/30 failed, retrying in 10s..."
        sleep 10
    done
    echo "ERROR: gateway not reachable after 5 minutes"
    exit 1

# Send a single short request to warm up Triton JIT compilation (up to 10min timeout).
warmup:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Warming up model (this can take several minutes on first request)..."
    attempt=0
    while true; do
        attempt=$((attempt + 1))
        if kubectl exec -n {{NAMESPACE}} deploy/{{deploy}} -- \
          python -c "
    import urllib.request, json
    req = urllib.request.Request('{{url}}/chat/completions',
        data=json.dumps({'model':'{{model}}','messages':[{'role':'user','content':'Hi'}],'max_tokens':8}).encode(),
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

# Fast plumbing validation (~60s). Uses --unsafe-override so it runs below the
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
    # Pod descriptions (events, exit codes, OOM kills, restart reasons)
    for pod in $(kubectl get pods -n "$NS" -l llm-d.ai/role -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
        kubectl describe pod -n "$NS" "$pod" > "{{dest}}/logs/${pod}.describe" 2>&1 || true
    done
    # Namespace events (sorted by time)
    kubectl get events -n "$NS" --sort-by='.lastTimestamp' > "{{dest}}/logs/events.txt" 2>&1 || true
    echo "Logs saved to {{dest}}/logs/"

# Export Grafana dashboards for result directories.
# Usage: just scrape-grafana results_p1w1_d1w2_c1 results_p1w1_d1w2_c4
scrape-grafana +dirs:
    python3 export_dashboard.py results {{dirs}}

# Scrape Grafana dashboards and generate interactivity chart.
# Reads namespace.txt from each result directory to find the right Grafana instance.
# Runs export_dashboard.py inside the orchestrator pod via kubectl exec (no port-forward needed).
# Usage: just report results_routing
report outdir:
    #!/usr/bin/env bash
    set -euo pipefail
    DIRS=$(find "{{outdir}}" -name "profile_export.jsonl" -exec dirname {} \;)
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
        GRAFANA_URL="http://grafana.${NS}.svc.cluster.local:80"
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
        TIMESTAMPS=$(python3 extract_timestamps.py "$dir")
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
    python3 gen_interactivity_chart.py "$(dirname "{{outdir}}")" 2>/dev/null || true


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

# Bootstrap a new namespace with all resources needed for benchmarking.
# Usage: just setup-namespace ecrncevi-dev-p2w1d2w1
setup-namespace ns:
    #!/usr/bin/env bash
    set -euo pipefail
    ROOT={{llm_d_root}}
    echo "=== Creating namespace {{ns}} ==="
    kubectl create namespace {{ns}} --dry-run=client -o yaml | kubectl apply -f -
    # RBAC for prometheus and grafana sidecars
    kubectl apply -n {{ns}} -f - <<'RBACEOF'
    apiVersion: rbac.authorization.k8s.io/v1
    kind: Role
    metadata:
      name: prometheus-server
    rules:
    - apiGroups: [""]
      resources: [pods, services, endpoints]
      verbs: [get, list, watch]
    - apiGroups: [""]
      resources: [configmaps]
      verbs: [get]
    ---
    apiVersion: rbac.authorization.k8s.io/v1
    kind: RoleBinding
    metadata:
      name: prometheus-server
    roleRef:
      apiGroup: rbac.authorization.k8s.io
      kind: Role
      name: prometheus-server
    subjects:
    - kind: ServiceAccount
      name: prometheus-server
      namespace: {{ns}}
    ---
    apiVersion: rbac.authorization.k8s.io/v1
    kind: Role
    metadata:
      name: grafana-sidecar
    rules:
    - apiGroups: [""]
      resources: [configmaps, secrets]
      verbs: [get, list, watch]
    ---
    apiVersion: rbac.authorization.k8s.io/v1
    kind: RoleBinding
    metadata:
      name: grafana-sidecar
    roleRef:
      apiGroup: rbac.authorization.k8s.io
      kind: Role
      name: grafana-sidecar
    subjects:
    - kind: ServiceAccount
      name: grafana
      namespace: {{ns}}
    RBACEOF
    # ClusterRole+Binding for Prometheus to scrape dcgm-exporter in cw-exporters namespace
    kubectl apply -f - <<DCGMRBAC
    apiVersion: rbac.authorization.k8s.io/v1
    kind: ClusterRole
    metadata:
      name: {{ns}}-prometheus-cw-exporters-reader
    rules:
    - apiGroups: [""]
      resources: [pods]
      verbs: [get, list, watch]
    ---
    apiVersion: rbac.authorization.k8s.io/v1
    kind: ClusterRoleBinding
    metadata:
      name: {{ns}}-prometheus-cw-exporters-reader
    roleRef:
      apiGroup: rbac.authorization.k8s.io
      kind: ClusterRole
      name: {{ns}}-prometheus-cw-exporters-reader
    subjects:
    - kind: ServiceAccount
      name: prometheus-server
      namespace: {{ns}}
    DCGMRBAC
    kubectl create secret generic llm-d-hf-token --from-literal=HF_TOKEN=dummy -n {{ns}} --dry-run=client -o yaml | kubectl apply -f -
    # Service account
    kubectl apply -n {{ns}} -f "$ROOT/guides/wide-ep-lws/modelserver/gpu/vllm-glm-5.2/base/serviceAccount.yaml"
    # Gateway (configmap + gateway via kustomize)
    kubectl kustomize "$ROOT/guides/recipes/gateway/istio/" | kubectl apply -n {{ns}} -f -
    # InferenceModel
    kubectl apply -n {{ns}} -f - <<EOF
    apiVersion: inference.networking.x-k8s.io/v1alpha2
    kind: InferenceModel
    metadata:
      name: glm-5-2-fp8
    spec:
      criticality: Critical
      modelName: zai-org/GLM-5.2-FP8
      poolRef:
        name: wide-ep-lws
    EOF
    # Prometheus
    helm upgrade --install prometheus prometheus-community/prometheus \
      -n {{ns}} --version 29.13.0 \
      --set alertmanager.enabled=false \
      --set kube-state-metrics.enabled=false \
      --set prometheus-node-exporter.enabled=false \
      --set prometheus-pushgateway.enabled=false \
      --set rbac.create=false \
      --set server.persistentVolume.enabled=false \
      --set 'server.extraFlags[0]=web.enable-admin-api' \
      --set 'server.extraFlags[1]=web.enable-lifecycle' \
      --set serviceAccounts.server.create=true \
      --set serviceAccounts.server.name=prometheus-server \
      --set 'server.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].key=kubernetes.io/arch' \
      --set 'server.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].operator=In' \
      --set 'server.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].values[0]=amd64' \
      -f - <<PROMEOF
    extraScrapeConfigs: |
      - job_name: 'vllm-decode'
        kubernetes_sd_configs:
          - role: pod
            namespaces:
              names:
                - {{ns}}
        relabel_configs:
          - source_labels: [__meta_kubernetes_pod_label_llm_d_ai_role]
            regex: decode
            action: keep
          - source_labels: [__meta_kubernetes_pod_container_name]
            regex: vllm
            action: keep
          - source_labels: [__meta_kubernetes_pod_container_port_number]
            regex: '820[0-7]'
            action: keep
          - source_labels: [__meta_kubernetes_pod_name]
            target_label: pod
          - source_labels: [__meta_kubernetes_pod_node_name]
            target_label: node
          - source_labels: [__meta_kubernetes_pod_label_llm_d_ai_model]
            target_label: model
          - source_labels: [__meta_kubernetes_pod_container_port_number]
            target_label: rank
        scrape_interval: 1s
        metrics_path: /metrics
      - job_name: 'vllm-prefill'
        kubernetes_sd_configs:
          - role: pod
            namespaces:
              names:
                - {{ns}}
        relabel_configs:
          - source_labels: [__meta_kubernetes_pod_label_llm_d_ai_role]
            regex: prefill
            action: keep
          - source_labels: [__meta_kubernetes_pod_container_name]
            regex: vllm
            action: keep
          - source_labels: [__meta_kubernetes_pod_container_port_number]
            regex: '800[0-7]'
            action: keep
          - source_labels: [__meta_kubernetes_pod_name]
            target_label: pod
          - source_labels: [__meta_kubernetes_pod_node_name]
            target_label: node
          - source_labels: [__meta_kubernetes_pod_label_llm_d_ai_model]
            target_label: model
          - source_labels: [__meta_kubernetes_pod_container_port_number]
            target_label: rank
        scrape_interval: 1s
        metrics_path: /metrics
      - job_name: 'epp'
        kubernetes_sd_configs:
          - role: pod
            namespaces:
              names:
                - {{ns}}
        relabel_configs:
          - source_labels: [__meta_kubernetes_pod_label_llm_d_router_gateway]
            regex: .+
            action: keep
          - source_labels: [__meta_kubernetes_pod_ip]
            target_label: __address__
            replacement: '\${1}:9090'
          - source_labels: [__meta_kubernetes_pod_name]
            target_label: pod
          - source_labels: [__meta_kubernetes_pod_label_llm_d_router_gateway]
            target_label: inferencepool
        scrape_interval: 1s
        metrics_path: /metrics
      - job_name: 'dcgm'
        kubernetes_sd_configs:
          - role: pod
            namespaces:
              names:
                - cw-exporters
        relabel_configs:
          - source_labels: [__meta_kubernetes_pod_label_app_kubernetes_io_name]
            regex: dcgm-exporter
            action: keep
          - source_labels: [__meta_kubernetes_pod_node_name]
            target_label: node
        metric_relabel_configs:
          - source_labels: [Hostname]
            target_label: node
        scrape_interval: 5s
        metrics_path: /metrics
      - job_name: 'node-exporter'
        kubernetes_sd_configs:
          - role: pod
            namespaces:
              names:
                - {{ns}}
        relabel_configs:
          - source_labels: [__meta_kubernetes_pod_container_name]
            regex: node-exporter
            action: keep
          - source_labels: [__meta_kubernetes_pod_container_port_number]
            regex: '9100'
            action: keep
          - source_labels: [__meta_kubernetes_pod_name]
            target_label: pod
          - source_labels: [__meta_kubernetes_pod_node_name]
            target_label: node
          - source_labels: [__meta_kubernetes_pod_label_llm_d_ai_role]
            target_label: role
        scrape_interval: 5s
        metrics_path: /metrics
    PROMEOF
    # Grafana
    helm upgrade --install grafana grafana/grafana \
      -n {{ns}} --version 10.5.15 \
      --set adminPassword=admin \
      --set persistence.enabled=false \
      --set rbac.create=false \
      --set sidecar.dashboards.enabled=true \
      --set sidecar.dashboards.label=grafana_dashboard \
      --set sidecar.dashboards.searchNamespace={{ns}} \
      --set 'affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].key=kubernetes.io/arch' \
      --set 'affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].operator=In' \
      --set 'affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[0].matchExpressions[0].values[0]=amd64' \
      -f - <<GRAFEOF
    datasources:
      datasources.yaml:
        apiVersion: 1
        datasources:
        - name: Prometheus
          type: prometheus
          url: http://prometheus-server.{{ns}}.svc.cluster.local
          access: proxy
          isDefault: true
    GRAFEOF
    # Apply Grafana dashboards
    kubectl create configmap wideep-overview-dashboard \
        --from-file=wideep-overview.json=dashboards/grafana-wideep-overview.json \
        -n {{ns}} --dry-run=client -o yaml \
      | kubectl label --local -f - grafana_dashboard=1 -o yaml \
      | kubectl apply -f -
    kubectl create configmap aggregate-overview-dashboard \
        --from-file=aggregate-overview.json=dashboards/grafana-aggregate.json \
        -n {{ns}} --dry-run=client -o yaml \
      | kubectl label --local -f - grafana_dashboard=1 -o yaml \
      | kubectl apply -f -
    # Deploy aiperf runner
    kubectl apply -f agentx.yaml -n {{ns}}
    kubectl rollout status deploy/{{deploy}} -n {{ns}} --timeout=300s
    echo "=== Namespace {{ns}} ready ==="

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

# Free GPUs in a namespace by removing model serving, but keep prometheus/grafana alive.
# Usage: just teardown-serving ecrncevi-dev-p2w1d2w1
teardown-serving ns:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "=== Tearing down serving in {{ns}} (keeping monitoring) ==="
    kubectl delete lws wide-ep-lws-nvidia-gpu-vllm-glm-5-2-prefill wide-ep-lws-nvidia-gpu-vllm-glm-5-2-decode -n {{ns}} --ignore-not-found 2>/dev/null || true
    helm uninstall wide-ep-lws -n {{ns}} 2>/dev/null || true
    echo "=== Serving removed from {{ns}} ==="

# Fully tear down an isolated benchmark namespace (including monitoring).
# Snapshot Prometheus TSDB from a benchmark namespace into a local directory.
# Usage: just snapshot-prometheus ecrncevi-dev-p1w1d1w1 results_run1/results_p1w1_d1w1
snapshot-prometheus ns dest:
    #!/usr/bin/env bash
    set -euo pipefail
    PROM_POD=$(kubectl get pod -n "{{ns}}" -l app.kubernetes.io/name=prometheus,app.kubernetes.io/component=server -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || {
        echo "  No Prometheus pod in {{ns}}, skipping snapshot"
        exit 0
    }
    echo "=== {{ns}}: snapshotting Prometheus TSDB ==="
    SNAP_NAME=$(kubectl exec -n "{{ns}}" "$PROM_POD" -c prometheus-server -- \
        wget -qO- --post-data= http://localhost:9090/api/v1/admin/tsdb/snapshot 2>/dev/null \
        | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['name'])" 2>/dev/null) || {
        echo "  WARNING: snapshot failed for {{ns}}, skipping"
        exit 0
    }
    SNAP_DIR="{{dest}}/prometheus_snapshot"
    mkdir -p "$SNAP_DIR"
    kubectl cp "{{ns}}/${PROM_POD}:/data/snapshots/${SNAP_NAME}" "$SNAP_DIR" -c prometheus-server 2>/dev/null || {
        echo "  WARNING: snapshot copy failed for {{ns}}"
        exit 0
    }
    kubectl exec -n "{{ns}}" "$PROM_POD" -c prometheus-server -- rm -rf "/data/snapshots/${SNAP_NAME}" 2>/dev/null || true
    echo "  Saved to $SNAP_DIR"

# Usage: just teardown-namespace ecrncevi-dev-p2w1d2w1
teardown-namespace ns:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "=== Tearing down namespace {{ns}} ==="
    kubectl delete lws wide-ep-lws-nvidia-gpu-vllm-glm-5-2-prefill wide-ep-lws-nvidia-gpu-vllm-glm-5-2-decode -n {{ns}} --ignore-not-found 2>/dev/null || true
    helm uninstall wide-ep-lws -n {{ns}} 2>/dev/null || true
    helm uninstall prometheus -n {{ns}} 2>/dev/null || true
    helm uninstall grafana -n {{ns}} 2>/dev/null || true
    kubectl delete clusterrole {{ns}}-prometheus-cw-exporters-reader --ignore-not-found 2>/dev/null || true
    kubectl delete clusterrolebinding {{ns}}-prometheus-cw-exporters-reader --ignore-not-found 2>/dev/null || true
    kubectl delete namespace {{ns}} --ignore-not-found
    echo "=== Namespace {{ns}} deleted ==="

# Run sweep in an isolated namespace per config.
# Format: PR:PW:DR:DW (prefill_replicas:prefill_width:decode_replicas:decode_width)
# Usage: just sweep-isolated results_run1 "2:1:1:1 1:1:1:1 1:2:1:1"
sweep-isolated outdir configs duration="900":
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p "{{outdir}}"
    NAMESPACES=()
    RESULT_DIRS=()
    for cfg in {{configs}}; do
        IFS=: read -r PR PW DR DW <<< "$cfg"
        NS="{{NAMESPACE}}-p${PR}w${PW}d${DR}w${DW}"
        PREFIX="p${PR}w${PW}_d${DR}w${DW}"
        dir="{{outdir}}/results_${PREFIX}"
        # Skip if all concurrency results already exist
        ALL_DONE=true
        for C in {{sweep_concurrencies}}; do
            if [ ! -f "$dir/results_${PREFIX}_c${C}/profile_export.jsonl" ]; then
                ALL_DONE=false
                break
            fi
        done
        if [ "$ALL_DONE" = true ]; then
            echo "====== ${cfg} — all concurrency levels done, skipping ======"
            continue
        fi
        echo "====== Setting up ${cfg} in namespace $NS ======"
        just setup-namespace "$NS"
        NAMESPACES+=("$NS")
        RESULT_DIRS+=("$dir")
        NAMESPACE="$NS" just sweep "{{outdir}}" "${cfg}" {{duration}} &
    done
    # Wait for all parallel sweeps to complete
    wait
    echo "====== All sweeps complete ======"
    # Snapshot Prometheus TSDB before tearing down serving
    for i in "${!NAMESPACES[@]}"; do
        just snapshot-prometheus "${NAMESPACES[$i]}" "${RESULT_DIRS[$i]}" || echo "WARNING: snapshot failed for ${NAMESPACES[$i]}"
    done
    # Free GPUs but keep prometheus/grafana for after-the-fact reporting
    for NS in "${NAMESPACES[@]}"; do
        just teardown-serving "$NS"
    done
    # Generate combined interactivity chart (dashboards already scraped per-sweep)
    python3 gen_interactivity_chart.py "{{outdir}}" 2>/dev/null || true
    echo ""
    echo "Monitoring still running. To scrape dashboards after the fact:"
    echo "  just report-ns <namespace> <outdir>"
    echo "To fully clean up:"
    for NS in "${NAMESPACES[@]}"; do
        echo "  just teardown-namespace $NS"
    done

# Deploy PD: just start-pd <prefill_replicas> <prefill_size> <decode_replicas> <decode_size>
# prefill_replicas = number of prefill LWS replica groups
# prefill_size = nodes per prefill replica (EP width, 1 = single-node)
# decode_replicas = number of decode LWS replica groups
# decode_size = nodes per decode replica (EP width, 1 = single-node)
start-pd prefill_replicas prefill_size decode_replicas decode_size:
    #!/usr/bin/env bash
    set -euo pipefail
    ROOT={{llm_d_root}}
    source "$ROOT/guides/env.sh"
    CHART="oci://ghcr.io/llm-d/charts/llm-d-router-gateway"
    helm upgrade --install wide-ep-lws \
        "$CHART" \
        -f "$ROOT/guides/recipes/router/base.values.yaml" \
        -f "$ROOT/guides/recipes/router/features/httproute-flags.yaml" \
        -f "$ROOT/guides/wide-ep-lws/router/wide-ep-lws.values.yaml" \
        -f "$ROOT/guides/wide-ep-lws/router/glm-5.2-overrides.values.yaml" \
        --set provider.name=istio \
        --set router.epp.image.tag=v0.9.0 \
        --set 'router.epp.flags.metrics-endpoint-auth=false' \
        -n {{NAMESPACE}} --version v0.9.0
    kubectl create secret generic llm-d-hf-token --from-literal=HF_TOKEN=dummy -n {{NAMESPACE}} --dry-run=client -o yaml | kubectl apply -f -
    kubectl apply -n {{NAMESPACE}} -k "$ROOT/guides/recipes/gateway/istio/"
    PROVIDER_DIR="$ROOT/guides/wide-ep-lws/modelserver/gpu/vllm-glm-5.2/providers/coreweave"
    export PREFILL_REPLICAS={{prefill_replicas}} PREFILL_SIZE={{prefill_size}} \
           DECODE_REPLICAS={{decode_replicas}} DECODE_SIZE={{decode_size}}
    kubectl kustomize "$PROVIDER_DIR" | envsubst '${PREFILL_REPLICAS} ${PREFILL_SIZE} ${DECODE_REPLICAS} ${DECODE_SIZE}' | kubectl apply -n {{NAMESPACE}} -f -
    echo "Deployed PR{{prefill_replicas}} PW{{prefill_size}} DR{{decode_replicas}} DW{{decode_size}} — waiting for pods..."
    kubectl rollout status --watch statefulset/wide-ep-lws-nvidia-gpu-vllm-glm-5-2-prefill -n {{NAMESPACE}} --timeout=7200s &
    kubectl rollout status --watch statefulset/wide-ep-lws-nvidia-gpu-vllm-glm-5-2-decode -n {{NAMESPACE}} --timeout=7200s &
    wait
    for pod in $(kubectl get pods -n {{NAMESPACE}} -l llm-d.ai/role -o jsonpath='{.items[*].metadata.name}'); do
        kubectl exec -n {{NAMESPACE}} "$pod" -c vllm -- find /dev/shm -name 'vllm_offload*' -delete 2>/dev/null || true
    done
    just clear-kv-cache

# Start PD deployment via manifesto (alternative to llm-d direct).
start-pd-manifesto prefill_replicas prefill_size decode_replicas decode_size:
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

# Tear down PD deployment.
stop-pd:
    kubectl delete lws wide-ep-lws-nvidia-gpu-vllm-glm-5-2-prefill wide-ep-lws-nvidia-gpu-vllm-glm-5-2-decode -n {{NAMESPACE}} --ignore-not-found

# Tear down model via manifesto (alternative to stop-pd).
stop-model:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{manifesto_root}}"
    uv run manifesto render "{{manifesto_spec}}" --cluster "{{manifesto_cluster}}" --namespace "{{NAMESPACE}}" --user "{{manifesto_user}}" {{manifesto_args}} \
        | uv run python "{{repo_root}}/inject_kueue_queue.py" --queue "{{kueue_queue}}" \
        | kubectl delete -n "{{NAMESPACE}}" -f - --ignore-not-found=true

# Sweep concurrency levels for the currently deployed config.
# Results go to results_<prefix>_c<N>/ directories.
# Usage: just sweep-concurrency p2w1_d1w1
sweep-concurrency prefix="sweep" dest="." duration="900":
    #!/usr/bin/env bash
    set -uo pipefail
    FAILED=""
    for C in {{sweep_concurrencies}}; do
        RDIR="{{dest}}/results_{{prefix}}_c${C}"
        if [ -f "$RDIR/profile_export.jsonl" ]; then
            echo "=== concurrency=$C — already exists, skipping ==="
            continue
        fi
        echo "=== concurrency=$C ({{duration}}s) ==="
        just drain
        just clear-kv-cache
        if ! just warmup || ! just run $C {{duration}}; then
            echo "FAILED: concurrency=$C, dumping logs before cleanup"
            FAILED="${FAILED} c${C}"
            mkdir -p "$RDIR"
            just dump-logs "$RDIR"
            just wipe 2>/dev/null || true
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

# Full sweep: deploy each config, run concurrency sweep, tear down.
# Format: PR:PW:DR:DW (prefill_replicas:prefill_width:decode_replicas:decode_width)
# Usage: just sweep results_glm52_run1 "1:1:1:2 2:1:1:1 2:1:2:1"
sweep outdir configs duration="900":
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    mkdir -p "{{outdir}}"
    just wipe
    for cfg in {{configs}}; do
        IFS=: read -r PR PW DR DW <<< "$cfg"
        PREFIX="p${PR}w${PW}_d${DR}w${DW}"
        dir="{{outdir}}/results_${PREFIX}"
        ALL_DONE=true
        for C in {{sweep_concurrencies}}; do
            if [ ! -f "$dir/results_${PREFIX}_c${C}/profile_export.jsonl" ]; then
                ALL_DONE=false
                break
            fi
        done
        if [ "$ALL_DONE" = true ]; then
            echo "====== ${cfg} — all concurrency levels done, skipping ======"
            continue
        fi
        echo "====== ${cfg} ======"
        just stop-pd
        just start-pd "$PR" "$PW" "$DR" "$DW"
        just check
        just warmup
        mkdir -p "$dir"
        echo "$NS" > "$dir/namespace.txt"
        kubectl get pod -n "$NS" -l llm-d.ai/role=prefill -o yaml > "$dir/prefill.yaml"
        kubectl get pod -n "$NS" -l llm-d.ai/role=decode -o yaml > "$dir/decode.yaml"
        kubectl get pod -n "$NS" -l llm-d-router-gateway=wide-ep-lws-epp -o yaml > "$dir/epp.yaml" 2>/dev/null || true
        kubectl get inferencepool -n "$NS" -o yaml > "$dir/inferencepool.yaml" 2>/dev/null || true
        kubectl get httproute -n "$NS" -o yaml > "$dir/httproute.yaml" 2>/dev/null || true
        kubectl get configmap wide-ep-lws-epp -n "$NS" -o yaml > "$dir/epp-config.yaml" 2>/dev/null || true
        just vllm-version "$dir"
        just sweep-concurrency "${PREFIX}" "$dir" {{duration}}
        just stop-pd
    done

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

orchestrator-run outdir duration="900":
    #!/usr/bin/env bash
    set -euo pipefail
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
      SWEEP_OUTDIR="{{outdir}}" \
      SWEEP_DURATION="{{duration}}" \
      bash -lc 'set -euo pipefail; set -a; [ -f /workspace/benchmark-sweep.env ] && . /workspace/benchmark-sweep.env; set +a; cd /workspace/agentx-mvp; rm -f /workspace/orchestrator-sweep.exit_code; : > /workspace/orchestrator-sweep.log; nohup bash -lc '"'"'just sweep "$SWEEP_OUTDIR" "$SWEEP_DURATION" > /workspace/orchestrator-sweep.log 2>&1; code=$?; echo "$code" > /workspace/orchestrator-sweep.exit_code; rm -f /workspace/orchestrator-sweep.pid; exit "$code"'"'"' </dev/null >/dev/null 2>&1 & pid=$!; echo "$pid" > /workspace/orchestrator-sweep.pid; echo "Launched PID $pid"'
    echo "Sweep running detached. Monitor: just orchestrator-logs"

orchestrator-logs:
    POD=$(kubectl get pod -n {{NAMESPACE}} -l app={{orchestrator_deploy}} -o jsonpath='{.items[0].metadata.name}')
    kubectl exec -n {{NAMESPACE}} "$POD" -- tail -f /workspace/orchestrator-sweep.log

orchestrator-results outdir:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    POD=$(kubectl get pod -n "$NS" -l app={{orchestrator_deploy}} -o jsonpath='{.items[0].metadata.name}')
    DIRS=$(kubectl exec -n "$NS" "$POD" -- find /workspace/agentx-mvp/{{outdir}} -name "profile_export_aiperf.json" -exec dirname {} \; 2>/dev/null)

# Build and push the seed orchestrator image.
seed-build:
    podman build --platform linux/amd64 -f Dockerfile.seed -t {{seed_image}} .
    podman push {{seed_image}}

# Deploy the seed orchestrator pod into the cluster.
seed-deploy:
    #!/usr/bin/env bash
    set -euo pipefail
    sed "s/NAMESPACE_PLACEHOLDER/{{NAMESPACE}}/" seed.yaml | kubectl apply -n {{NAMESPACE}} -f -
    kubectl rollout status deploy/{{seed_deploy}} -n {{NAMESPACE}} --timeout=300s
    POD=$(kubectl get pod -n {{NAMESPACE}} -l app={{seed_deploy}} -o jsonpath='{.items[0].metadata.name}')
    echo "Adding helm repos..."
    kubectl exec -n {{NAMESPACE}} "$POD" -- helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
    kubectl exec -n {{NAMESPACE}} "$POD" -- helm repo add grafana https://grafana.github.io/helm-charts
    kubectl exec -n {{NAMESPACE}} "$POD" -- helm repo update
    echo "Seed pod ready: $POD"

# Launch a sweep inside the seed pod (detached — safe to disconnect).
# Usage: just seed results_run1 "1:1:1:1 2:1:2:1"
seed outdir configs:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    DEPLOY=deploy/{{seed_deploy}}
    kubectl exec -n "$NS" "$DEPLOY" -- helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null
    kubectl exec -n "$NS" "$DEPLOY" -- helm repo add grafana https://grafana.github.io/helm-charts 2>/dev/null
    kubectl exec -n "$NS" "$DEPLOY" -- helm repo update
    POD=$(kubectl get pod -n "$NS" -l app={{seed_deploy}} -o jsonpath='{.items[0].metadata.name}')
    echo "=== Syncing files to seed pod ==="
    kubectl exec -n "$NS" "$POD" -- mkdir -p /workspace/agentx-mvp
    for f in Justfile agentx.yaml dashboard.json extract_timestamps.py export_dashboard.py gen_interactivity_chart.py overlay_dashboards.py; do
        [ -f "$f" ] && kubectl cp "$f" "$NS/${POD}:/workspace/agentx-mvp/$f"
    done
    kubectl exec -n "$NS" "$POD" -- mkdir -p /workspace/agentx-mvp/dashboards
    for f in dashboards/*.json; do
        kubectl cp "$f" "$NS/${POD}:/workspace/agentx-mvp/$f"
    done
    kubectl exec -n "$NS" "$POD" -- bash -c \
        'if [ -d /workspace/llm-d/.git ]; then cd /workspace/llm-d && git pull; else git clone --branch wip-glm https://github.com/elvircrn/llm-d.git /workspace/llm-d; fi'
    TMPENV=$(mktemp)
    trap "rm -f $TMPENV" EXIT
    printf 'NAMESPACE=%s\nLLM_D_ROOT=/workspace/llm-d\n' "{{NAMESPACE}}" > "$TMPENV"
    kubectl cp "$TMPENV" "$NS/${POD}:/workspace/agentx-mvp/.env"
    echo "=== Launching sweep (detached) ==="
    OUTDIR="{{outdir}}"
    CONFIGS="{{configs}}"
    TMPSCRIPT=$(mktemp)
    printf '#!/bin/bash\ncd /workspace/agentx-mvp\njust sweep-isolated %s '\''%s'\'' > /workspace/seed-sweep.log 2>&1\necho $? > /workspace/seed-sweep.exit_code\nrm -f /workspace/seed-sweep.pid\n' "$OUTDIR" "$CONFIGS" > "$TMPSCRIPT"
    kubectl cp "$TMPSCRIPT" "$NS/${POD}:/workspace/run_seed.sh"
    rm -f "$TMPSCRIPT"
    kubectl exec -n "$NS" "$POD" -- chmod +x /workspace/run_seed.sh
    kubectl exec -n "$NS" "$POD" -- bash -c 'nohup bash /workspace/run_seed.sh </dev/null >/dev/null 2>&1 & echo $! > /workspace/seed-sweep.pid && echo "Launched PID $!"'
    echo ""
    echo "Sweep running detached. Safe to disconnect."
    echo "  Monitor:  just seed-logs"
    echo "  Results:  just seed-results {{outdir}}"
    echo "  Cleanup:  just seed-clean"

# Launch a sequential sweep inside the seed pod (one config at a time).
# Each config gets its own namespace, runs all concurrencies, then is torn down.
# Format: PR:PW:DR:DW (prefill_replicas:prefill_width:decode_replicas:decode_width)
# Usage: just seed-sequential results_run1 "1:1:1:1 2:1:1:1 1:2:1:1"
seed-sequential outdir configs:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    DEPLOY=deploy/{{seed_deploy}}
    kubectl exec -n "$NS" "$DEPLOY" -- helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null
    kubectl exec -n "$NS" "$DEPLOY" -- helm repo add grafana https://grafana.github.io/helm-charts 2>/dev/null
    kubectl exec -n "$NS" "$DEPLOY" -- helm repo update
    POD=$(kubectl get pod -n "$NS" -l app={{seed_deploy}} -o jsonpath='{.items[0].metadata.name}')
    echo "=== Syncing files to seed pod ==="
    kubectl exec -n "$NS" "$POD" -- mkdir -p /workspace/agentx-mvp
    for f in Justfile agentx.yaml dashboard.json extract_timestamps.py export_dashboard.py gen_interactivity_chart.py overlay_dashboards.py; do
        [ -f "$f" ] && kubectl cp "$f" "$NS/${POD}:/workspace/agentx-mvp/$f"
    done
    kubectl exec -n "$NS" "$POD" -- mkdir -p /workspace/agentx-mvp/dashboards
    for f in dashboards/*.json; do
        kubectl cp "$f" "$NS/${POD}:/workspace/agentx-mvp/$f"
    done
    kubectl exec -n "$NS" "$POD" -- bash -c \
        'if [ -d /workspace/llm-d/.git ]; then cd /workspace/llm-d && git pull; else git clone --branch wip-glm https://github.com/elvircrn/llm-d.git /workspace/llm-d; fi'
    TMPENV=$(mktemp)
    trap "rm -f $TMPENV" EXIT
    printf 'NAMESPACE=%s\nLLM_D_ROOT=/workspace/llm-d\n' "{{NAMESPACE}}" > "$TMPENV"
    kubectl cp "$TMPENV" "$NS/${POD}:/workspace/agentx-mvp/.env"
    echo "=== Launching sequential sweep (detached) ==="
    OUTDIR="{{outdir}}"
    CONFIGS="{{configs}}"
    TMPSCRIPT=$(mktemp)
    printf '#!/bin/bash\ncd /workspace/agentx-mvp\nfor cfg in %s; do\n  IFS=: read -r PR PW DR DW <<< "$cfg"\n  NS=ecrncevi-dev-p${PR}w${PW}d${DR}w${DW}\n  echo "====== Sequential: ${cfg} in $NS ======"\n  if ! just setup-namespace "$NS"; then\n    echo "FAILED: setup for ${cfg}, skipping"\n    just teardown-namespace "$NS" 2>/dev/null || true\n    continue\n  fi\n  NAMESPACE="$NS" just sweep "%s" "${cfg}" || echo "FAILED: sweep for ${cfg}"\n  just snapshot-prometheus "$NS" "%s/results_p${PR}w${PW}_d${DR}w${DW}" || echo "WARNING: snapshot failed for ${cfg}"\n  just teardown-namespace "$NS"\ndone\n' "$CONFIGS" "$OUTDIR" "$OUTDIR" > "$TMPSCRIPT"
    kubectl cp "$TMPSCRIPT" "$NS/${POD}:/workspace/run_seed.sh"
    rm -f "$TMPSCRIPT"
    kubectl exec -n "$NS" "$POD" -- chmod +x /workspace/run_seed.sh
    kubectl exec -n "$NS" "$POD" -- bash -c 'nohup bash /workspace/run_seed.sh </dev/null >/workspace/seed-sweep.log 2>&1 & echo $! > /workspace/seed-sweep.pid && echo "Launched PID $!"'
    echo ""
    echo "Sequential sweep running detached. Safe to disconnect."
    echo "  Monitor:  just seed-logs"
    echo "  Results:  just seed-results {{outdir}}"
    echo "  Cleanup:  just seed-clean"

# Tail the seed sweep log.
seed-logs:
    #!/usr/bin/env bash
    set -euo pipefail
    POD=$(kubectl get pod -n {{NAMESPACE}} -l app={{seed_deploy}} -o jsonpath='{.items[0].metadata.name}')
    kubectl exec -n {{NAMESPACE}} "$POD" -- tail -f /workspace/seed-sweep.log

# Copy results from the seed pod to local.
# Usage: just seed-results results_run1
seed-results outdir:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    POD=$(kubectl get pod -n "$NS" -l app={{seed_deploy}} -o jsonpath='{.items[0].metadata.name}')
    DIRS=$(kubectl exec -n "$NS" "$POD" -- find /workspace/agentx-mvp/{{outdir}} -name "profile_export.jsonl" -exec dirname {} \; 2>/dev/null)
    for dir in $DIRS; do
        LOCAL=${dir#/workspace/agentx-mvp/}
        mkdir -p "$LOCAL"
        kubectl cp "$NS/${POD}:${dir}" "$LOCAL" 2>/dev/null || true
        # Re-copy critical files individually — directory copies truncate large files
        for f in profile_export.jsonl profile_export_aiperf.json; do
            kubectl cp "$NS/${POD}:${dir}/${f}" "$LOCAL/$f" 2>/dev/null || true
        done
    done
    # Also copy non-result files (namespace.txt, yamls, etc.)
    EXTRAS=$(kubectl exec -n "$NS" "$POD" -- find /workspace/agentx-mvp/{{outdir}} -maxdepth 4 \( -name "*.yaml" -o -name "*.txt" -o -name "*.html" \) 2>/dev/null)
    for f in $EXTRAS; do
        LOCAL=${f#/workspace/agentx-mvp/}
        mkdir -p "$(dirname "$LOCAL")"
        kubectl cp "$NS/${POD}:${f}" "$LOCAL" 2>/dev/null || true
    done
    # Snapshot and download Prometheus TSDB from each benchmark namespace
    SNAPPED=""
    for dir in $DIRS; do
        PARENT=$(dirname "$dir")
        NS_FILE=$(kubectl exec -n "$NS" "$POD" -- cat "${PARENT}/namespace.txt" 2>/dev/null) || continue
        if echo "$SNAPPED" | grep -q "|${NS_FILE}|"; then continue; fi
        SNAPPED="${SNAPPED}|${NS_FILE}|"
        PROM_POD=$(kubectl get pod -n "$NS_FILE" -l app.kubernetes.io/name=prometheus,app.kubernetes.io/component=server -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || continue
        echo "=== $NS_FILE: snapshotting Prometheus TSDB ==="
        SNAP_NAME=$(kubectl exec -n "$NS_FILE" "$PROM_POD" -c prometheus-server -- \
            wget -qO- --post-data= http://localhost:9090/api/v1/admin/tsdb/snapshot 2>/dev/null \
            | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['name'])" 2>/dev/null) || {
            echo "  WARNING: snapshot failed for $NS_FILE, skipping"
            continue
        }
        LOCAL_PARENT=${PARENT#/workspace/agentx-mvp/}
        SNAP_DIR="${LOCAL_PARENT}/prometheus_snapshot"
        mkdir -p "$SNAP_DIR"
        kubectl cp "$NS_FILE/${PROM_POD}:/data/snapshots/${SNAP_NAME}" "$SNAP_DIR" -c prometheus-server 2>/dev/null || {
            echo "  WARNING: snapshot copy failed for $NS_FILE"
            continue
        }
        kubectl exec -n "$NS_FILE" "$PROM_POD" -c prometheus-server -- rm -rf "/data/snapshots/${SNAP_NAME}" 2>/dev/null || true
        echo "  Saved to $SNAP_DIR"
    done
    echo "Results copied to {{outdir}}/"
    python3 gen_interactivity_chart.py "{{outdir}}" 2>/dev/null || true

seed-stop:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    POD=$(kubectl get pod -n "$NS" -l app={{seed_deploy}} -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || true
    if [ -n "$POD" ]; then
        kubectl exec -n "$NS" "$POD" -- bash -c 'kill $(cat /workspace/seed-sweep.pid 2>/dev/null) 2>/dev/null; rm -f /workspace/seed-sweep.pid' 2>/dev/null || true
        echo "Sweep process killed."
    fi
    # Find and tear down all benchmark namespaces (ecrncevi-dev-p*d*)
    for BNS in $(kubectl get ns -o name | grep "^namespace/{{NAMESPACE}}-p[0-9]" | sed 's|namespace/||'); do
        echo "Tearing down $BNS..."
        kubectl delete lws wide-ep-lws-nvidia-gpu-vllm-glm-5-2-prefill wide-ep-lws-nvidia-gpu-vllm-glm-5-2-decode -n "$BNS" --ignore-not-found 2>/dev/null || true
        helm uninstall wide-ep-lws -n "$BNS" 2>/dev/null || true
        helm uninstall prometheus -n "$BNS" 2>/dev/null || true
        helm uninstall grafana -n "$BNS" 2>/dev/null || true
        kubectl delete namespace "$BNS" --ignore-not-found &
    done
    wait
    echo "All benchmark namespaces deleted."

seed-clean:
    #!/usr/bin/env bash
    set -euo pipefail
    kubectl delete deploy {{seed_deploy}} -n {{NAMESPACE}} --ignore-not-found
    kubectl delete clusterrolebinding benchmark-orchestrator --ignore-not-found
    kubectl delete sa benchmark-orchestrator -n {{NAMESPACE}} --ignore-not-found
    echo "Orchestrator pod cleaned up."

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
#   just sweep outdir 900 true            # keep model up (skip stop/start/warmup)
#   just orchestrator-run              # run a detached in-cluster sweep
#   just orchestrator-run '' 900 false true   # keep model, skip orchestrator redeploy
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
manifesto_gateway := env_var_or_default('MANIFESTO_GATEWAY_COMPONENT', 'gateway-istio')
kueue_queue := env_var_or_default('KUEUE_QUEUE', 'nightly-eval')
aiperf_image := env_var_or_default('AIPERF_IMAGE', 'quay.io/rh-ee-imarkov/aiperf:agentx-v0.11.1')
lustre_claim := env_var_or_default('LUSTRE_CLAIM', 'lustre-pvc-vllm')
lustre_mount := env_var_or_default('LUSTRE_MOUNT', '/mnt/lustre')
lustre_prefix := env_var_or_default('LUSTRE_PREFIX', '/mnt/lustre/agentx-mvp')
orchestrator_image := env_var_or_default('ORCHESTRATOR_IMAGE', 'quay.io/tms/benchmark-orchestrator:amd64')
container_cli := env_var_or_default('CONTAINER_CLI', 'docker')
orchestrator_manifesto_repo := env_var_or_default('ORCHESTRATOR_MANIFESTO_REPO', 'https://github.com/tlrmchlsmth/llm-manifesto.git')
orchestrator_manifesto_ref := env_var_or_default('ORCHESTRATOR_MANIFESTO_REF', 'main')
orchestrator_deploy := "benchmark-orchestrator"
orchestrator_spec_configmap := "benchmark-orchestrator-spec"
orchestrator_force_sync := env_var_or_default('ORCHESTRATOR_FORCE_SYNC', 'false')
model     := env_var_or_default('MODEL', 'deepseek-ai/DeepSeek-V4-Pro')
model_label := env_var_or_default('MODEL_LABEL', 'DeepSeek-V4-Pro')
max_context_length := env_var_or_default('MAX_CONTEXT_LENGTH', '128000')
url       := env_var_or_default('URL', '')
server_metrics_url := env_var_or_default('SERVER_METRICS_URL', '')
gpu_telemetry_urls := env_var_or_default('GPU_TELEMETRY_URLS', '')
# AIPerf profile flags aligned with InferenceX build_replay_cmd (benchmark_lib.sh).
aiperf_public_dataset := env_var_or_default('AIPERF_PUBLIC_DATASET', 'semianalysis_cc_traces_weka_with_subagents')
aiperf_random_seed := env_var_or_default('AIPERF_RANDOM_SEED', '42')
aiperf_failed_request_threshold := env_var_or_default('AIPERF_FAILED_REQUEST_THRESHOLD', '0.10')
aiperf_trajectory_start_min_ratio := env_var_or_default('AIPERF_TRAJECTORY_START_MIN_RATIO', '0.25')
aiperf_trajectory_start_max_ratio := env_var_or_default('AIPERF_TRAJECTORY_START_MAX_RATIO', '0.75')
# "all" (or empty) omits --num-dataset-entries entirely, which loads the
# entire corpus regardless of size (see semianalysis_cc_traces_weka.py: the
# loader only caps when the flag is explicitly passed).
aiperf_num_dataset_entries := env_var_or_default('AIPERF_NUM_DATASET_ENTRIES', 'all')
aiperf_slice_duration := env_var_or_default('AIPERF_SLICE_DURATION', '1.0')
# Allow concurrency to exceed the loaded trace pool by reusing traces across
# lanes. Required when BENCHMARK_CONCURRENCIES exceeds AIPERF_NUM_DATASET_ENTRIES.
aiperf_allow_dataset_wrap := env_var_or_default('AIPERF_ALLOW_DATASET_WRAP', 'false')
aiperf_dataset_configuration_timeout := env_var_or_default('AIPERF_DATASET_CONFIGURATION_TIMEOUT', '1800')
# Persist the tokenized-dataset mmap cache on Lustre (survives pod restarts)
# instead of the default ~/.cache/aiperf/dataset_mmap on ephemeral /workspace,
# so repeat attempts/concurrencies skip re-tokenizing the whole corpus.
aiperf_mmap_cache_dir := env_var_or_default('AIPERF_DATASET_MMAP_CACHE_DIR', lustre_prefix + '/' + manifesto_user + '/aiperf_mmap_cache')
# Agentic warmup/grace are absolute seconds, independent of benchmark-duration.
aiperf_cache_warmup_seconds := env_var_or_default('AIPERF_CACHE_WARMUP_SECONDS', '150')
aiperf_warmup_grace_seconds := env_var_or_default('AIPERF_WARMUP_GRACE_SECONDS', '450')
# aiperf normally emits a progress/heartbeat log line at least every ~30s; if
# its log goes quiet for this long without the Job completing, aiperf has
# likely deadlocked internally (e.g. the agentic subagent/session-replay
# orchestrator can hang instead of exiting on error) and we fail fast instead
# of waiting out the multi-hour activeDeadlineSeconds safety net.
aiperf_stall_timeout := env_var_or_default('AIPERF_STALL_TIMEOUT', '300')
benchmark_retries := env_var_or_default('BENCHMARK_RETRIES', '3')
benchmark_concurrencies := env_var_or_default('BENCHMARK_CONCURRENCIES', '64 256')
# Files kubectl cp pulls from CANONICAL_ARTIFACT_DIR (see _run-job); used to
# detect a partial/incomplete local copy (e.g. after a mid-transfer EOF).
benchmark_artifact_files := "profile_export_aiperf.json profile_export_aiperf.csv profile_export.jsonl profile_export_console.txt"
pod_start_timeout := env_var_or_default('POD_START_TIMEOUT', '900')
monitoring_namespace := env_var_or_default('MONITORING_NAMESPACE', NAMESPACE)
prometheus_namespace := env_var_or_default('PROMETHEUS_NAMESPACE', monitoring_namespace)
prometheus_service := env_var_or_default('PROMETHEUS_SERVICE', 'prometheus-server')
prometheus_snapshot := env_var_or_default('PROMETHEUS_SNAPSHOT', 'false')
grafana_namespace := env_var_or_default('GRAFANA_NAMESPACE', monitoring_namespace)
grafana_service := env_var_or_default('GRAFANA_SERVICE', 'grafana')
concurrency := "64"
duration    := env_var_or_default('BENCHMARK_DURATION', '900')
sweep_keep_model := env_var_or_default('SWEEP_KEEP_MODEL', 'false')
orchestrator_redeploy := env_var_or_default('ORCHESTRATOR_REDEPLOY', 'true')

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

# Print gateway URL, service name, and probe result (for orchestrator debugging).
debug-gateway:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "=== env ==="
    echo "MANIFESTO_ROOT=${MANIFESTO_ROOT:-<unset>}"
    echo "MANIFESTO_GATEWAY_COMPONENT=${MANIFESTO_GATEWAY_COMPONENT:-<unset>}"
    echo "URL override={{url}}"
    echo "manifesto_gateway={{manifesto_gateway}}"
    echo "manifesto_user={{manifesto_user}} (USER=${USER:-<unset>})"
    echo "manifesto_spec={{manifesto_spec}}"
    echo
    echo "=== _model-url ==="
    just --quiet _model-url || echo "FAILED"
    echo
    echo "=== manifesto gateway service name ==="
    (cd "{{manifesto_root}}" && uv run manifesto name "{{manifesto_spec}}" {{manifesto_gateway}} --user "{{manifesto_user}}") || echo "FAILED"
    echo
    echo "=== kubernetes service ==="
    SVC=$(cd "{{manifesto_root}}" && uv run manifesto name "{{manifesto_spec}}" {{manifesto_gateway}} --user "{{manifesto_user}}")
    kubectl get svc -n {{NAMESPACE}} "$SVC" -o wide 2>&1 || true
    echo
    echo "=== kubectl probe (same as _gateway-ready) ==="
    if just --justfile "{{repo_root}}/Justfile" --working-directory "{{repo_root}}" _gateway-run-probe; then
        echo "probe: OK"
    else
        echo "probe: FAILED"
    fi

# Sanity check: list models served through the llm-d router from an in-cluster probe pod.
check:
    #!/usr/bin/env bash
    set -euo pipefail
    URL=$(just --quiet _model-url)
    NAME="agentx-check-$(date -u +%Y%m%d%H%M%S)"
    kubectl run "$NAME" -n {{NAMESPACE}} --rm --attach --restart=Never \
      --image={{orchestrator_image}} \
      --overrides='{"spec":{"nodeSelector":{"kubernetes.io/arch":"amd64"}}}' \
      --env="URL=$URL" \
      --command -- python3 -c "import os, urllib.request as u; print(u.urlopen(os.environ['URL'] + '/models', timeout=10).read().decode())"

# Exit 0 when GET $URL/models returns a model id.
_gateway-probe:
    #!/usr/bin/env bash
    set -euo pipefail
    URL=$(just --quiet _model-url)
    echo "probing $URL" >&2
    URL="$URL" python3 - <<'PY'
    import os, sys, urllib.error, urllib.request
    url = os.environ["URL"].rstrip("/") + "/models"
    try:
        body = urllib.request.urlopen(url, timeout=10).read().decode()
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.reason} for {url}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"request failed for {url}: {exc}", file=sys.stderr)
        sys.exit(1)
    if '"id"' not in body:
        print(f"no model id in response from {url}: {body[:300]}", file=sys.stderr)
        sys.exit(1)
    PY

_gateway-run-probe:
    #!/usr/bin/env bash
    set -euo pipefail
    URL=$(just --quiet _model-url)
    NAME="agentx-gateway-check-$(date -u +%Y%m%d%H%M%S)"
    kubectl run "$NAME" -n {{NAMESPACE}} --rm --attach --restart=Never \
      --image={{orchestrator_image}} \
      --overrides='{"spec":{"nodeSelector":{"kubernetes.io/arch":"amd64"}}}' \
      --env="URL=$URL" \
      --command -- python3 -c "import os, sys, urllib.request; body = urllib.request.urlopen(os.environ['URL'] + '/models', timeout=10).read().decode(); sys.exit(0 if '\"id\"' in body else 1)"

# Exit 0 when the gateway serves /models (in-cluster probe; no curl required).
_gateway-ready:
    just --quiet _gateway-run-probe

# Wait for model pods, EPP, and gateway /models (replaces manifesto just ready).
_wait-model-ready:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    MANIFESTO="{{manifesto_root}}"
    INSTANCE=$(cd "$MANIFESTO" && uv run manifesto instance-id "{{manifesto_spec}}" --user "{{manifesto_user}}")
    EPP=$(cd "$MANIFESTO" && uv run manifesto name "{{manifesto_spec}}" infpool-epp --user "{{manifesto_user}}")
    kubectl wait -n "$NS" --for=condition=Ready pod -l "app.kubernetes.io/instance=$INSTANCE,llm-d.ai/role=decode" --timeout=1200s &
    (kubectl wait -n "$NS" --for=condition=Ready pod -l "app.kubernetes.io/instance=$INSTANCE,llm-d.ai/role=prefill" --timeout=1200s 2>/dev/null || true) &
    kubectl wait -n "$NS" --for=condition=Available "deploy/$EPP" --timeout=120s &
    echo "Waiting for model pods and EPP..."
    wait
    echo "Checking gateway..."
    GATEWAY_URL=$(just --quiet _model-url)
    echo "Gateway URL: $GATEWAY_URL"
    GATEWAY_DEADLINE=$((SECONDS + 1200))
    attempt=0
    until just --quiet _gateway-ready; do
        attempt=$((attempt + 1))
        if [ "$SECONDS" -ge "$GATEWAY_DEADLINE" ]; then
            echo "ERROR: gateway /models did not become ready within 1200s" >&2
            echo "Last probe:" >&2
            just _gateway-run-probe >&2 || true
            exit 1
        fi
        if [ $((attempt % 6)) -eq 0 ]; then
            echo "Gateway not ready yet (attempt $attempt); retrying..."
            just _gateway-run-probe >&2 || true
        fi
        sleep 5
    done
    echo "Ready."

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
        if kubectl run "$NAME" -n {{NAMESPACE}} --rm --attach --restart=Never \
          --image={{orchestrator_image}} \
          --overrides='{"spec":{"nodeSelector":{"kubernetes.io/arch":"amd64"}}}' \
          --env="URL=$URL" \
          --env="MODEL={{model}}" \
          --command -- python3 -c "import os, urllib.request, json; req = urllib.request.Request(os.environ['URL'] + '/chat/completions', data=json.dumps({'model': os.environ['MODEL'], 'messages':[{'role':'user','content':'Hi'}], 'max_tokens':8}).encode(), headers={'Content-Type':'application/json'}); print(urllib.request.urlopen(req, timeout=600).read().decode())"; then
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
    if [ "{{aiperf_allow_dataset_wrap}}" = "true" ]; then
        DATASET_WRAP_ARGS="--allow-dataset-wrap"
    else
        DATASET_WRAP_ARGS=""
    fi
    if [ "{{aiperf_num_dataset_entries}}" = "all" ] || [ -z "{{aiperf_num_dataset_entries}}" ]; then
        NUM_DATASET_ENTRIES_ARGS=""
    else
        NUM_DATASET_ENTRIES_ARGS="--num-dataset-entries {{aiperf_num_dataset_entries}}"
    fi
    TS=$(date -u +%Y%m%d%H%M%S)
    JOB="agentx-aiperf-c{{concurrency}}-a{{attempt}}-${TS}"
    DEST_CLEAN=$(printf '%s' "{{dest}}" | sed 's#^\./##')
    CANONICAL_ARTIFACT_DIR="{{lustre_prefix}}/{{manifesto_user}}/${DEST_CLEAN}"
    ARTIFACT_DIR="${CANONICAL_ARTIFACT_DIR}_attempt{{attempt}}"
    DURATION={{duration}}
    AGENTIC_CACHE_WARMUP={{aiperf_cache_warmup_seconds}}
    WARMUP_GRACE={{aiperf_warmup_grace_seconds}}
    TIMEOUT=$((DURATION + AGENTIC_CACHE_WARMUP + WARMUP_GRACE + 7200))
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
                  value: "0"
                - name: AIPERF_DATASET_CONFIGURATION_TIMEOUT
                  value: "{{aiperf_dataset_configuration_timeout}}"
                - name: AIPERF_SERVICE_PROFILE_CONFIGURE_TIMEOUT
                  value: "{{aiperf_dataset_configuration_timeout}}"
                - name: HF_HOME
                  value: /workspace/.cache/huggingface
                - name: AIPERF_DATASET_MMAP_CACHE_DIR
                  value: "{{aiperf_mmap_cache_dir}}"
                - name: URL
                  value: "${URL}"
                - name: MODEL
                  value: "{{model}}"
                - name: SERVER_METRICS_ARGS
                  value: "${SERVER_METRICS_ARGS}"
                - name: GPU_TELEMETRY_ARGS
                  value: "${GPU_TELEMETRY_ARGS}"
                - name: DATASET_WRAP_ARGS
                  value: "${DATASET_WRAP_ARGS}"
                - name: NUM_DATASET_ENTRIES_ARGS
                  value: "${NUM_DATASET_ENTRIES_ARGS}"
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
                  echo "AIPerf profile: duration=${DURATION}s agentic-cache-warmup=${AGENTIC_CACHE_WARMUP}s warmup-grace=${WARMUP_GRACE}s" \
                    >> "\$ARTIFACT_DIR/logs/aiperf.log"
                  set +e
                  /opt/venv/bin/aiperf profile \
                    --scenario inferencex-agentx-mvp \
                    \$UNSAFE_ARGS \
                    --url "\$URL" \
                    --endpoint /chat/completions \
                    --model "\$MODEL" \
                    --max-context-length {{max_context_length}} \
                    --endpoint-type chat \
                    --streaming \
                    --use-server-token-count \
                    --public-dataset {{aiperf_public_dataset}} \
                    --concurrency {{concurrency}} \
                    --benchmark-duration ${DURATION} \
                    --random-seed {{aiperf_random_seed}} \
                    --failed-request-threshold {{aiperf_failed_request_threshold}} \
                    --trajectory-start-min-ratio {{aiperf_trajectory_start_min_ratio}} \
                    --trajectory-start-max-ratio {{aiperf_trajectory_start_max_ratio}} \
                    --agentic-cache-warmup-duration ${AGENTIC_CACHE_WARMUP} \
                    --warmup-grace-period ${WARMUP_GRACE} \
                    \$NUM_DATASET_ENTRIES_ARGS \
                    --slice-duration {{aiperf_slice_duration}} \
                    --tokenizer-trust-remote-code \
                    \$DATASET_WRAP_ARGS \
                    \$SERVER_METRICS_ARGS \
                    \$GPU_TELEMETRY_ARGS \
                    --output-artifact-dir "\$ARTIFACT_DIR" \
                    --ui simple \
                    2>&1 | tee -a "\$ARTIFACT_DIR/logs/aiperf.log"
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
                  cpu: "8"
                  memory: 16Gi
                limits:
                  cpu: "32"
                  memory: 64Gi
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
    STALL_LOG_LAST=""
    STALL_LAST_CHANGE=$SECONDS
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
        STALL_LOG_NEW=$(kubectl logs -n "$NS" "job/$JOB" --tail=5 2>/dev/null)
        if [ "$STALL_LOG_NEW" != "$STALL_LOG_LAST" ]; then
            STALL_LOG_LAST="$STALL_LOG_NEW"
            STALL_LAST_CHANGE=$SECONDS
        elif [ $((SECONDS - STALL_LAST_CHANGE)) -ge {{aiperf_stall_timeout}} ]; then
            echo "ERROR: benchmark job produced no new log output for {{aiperf_stall_timeout}}s; aiperf is likely stuck/deadlocked internally, treating as failed"
            kubectl logs -n "$NS" "job/$JOB" --all-containers --tail=200 || true
            kubectl describe -n "$NS" "job/$JOB" || true
            exit 1
        fi
        sleep 10
    done
    ACCESS=$(just --quiet _lustre-access-pod "$POD" aiperf) || {
        echo "ERROR: no pod found to verify PVC artifacts (benchmark job pod, model pods, or log-reader)"
        exit 1
    }
    PVC_POD=$(printf '%s\n' "$ACCESS" | awk '{print $1}')
    PVC_CONTAINER=$(printf '%s\n' "$ACCESS" | awk '{print $2}')
    kubectl exec -n "$NS" "$PVC_POD" -c "$PVC_CONTAINER" -- test -f "${CANONICAL_ARTIFACT_DIR}/profile_export_aiperf.json" || {
        echo "ERROR: profile_export_aiperf.json missing from canonical PVC path after job completion"
        kubectl exec -n "$NS" "$PVC_POD" -c "$PVC_CONTAINER" -- find "$CANONICAL_ARTIFACT_DIR" -maxdepth 2 -type f 2>/dev/null || true
        exit 1
    }
    # Benchmark itself succeeded (canonical artifacts confirmed on the PVC above) — record the
    # remote paths now, before attempting the local copy, so they survive even if the copy fails.
    mkdir -p "{{dest}}/logs"
    printf '%s\n' "$JOB" > "{{dest}}/job_name.txt"
    printf '%s\n' "$CANONICAL_ARTIFACT_DIR" > "{{dest}}/remote_artifact_dir.txt"
    printf '%s\n' "$ARTIFACT_DIR" > "{{dest}}/remote_attempt_artifact_dir.txt"
    CP_RETRIES=3
    cp_attempt=1
    until kubectl cp -c "$PVC_CONTAINER" "$NS/${PVC_POD}:${CANONICAL_ARTIFACT_DIR}/." "{{dest}}"; do
        if [ "$cp_attempt" -ge "$CP_RETRIES" ]; then
            echo "WARNING: kubectl cp failed after $CP_RETRIES attempts. The benchmark itself succeeded"
            echo "  and results are safe on the PVC at $CANONICAL_ARTIFACT_DIR, but could not be"
            echo "  downloaded locally right now (likely a flaky kubectl connection). NOT retrying"
            echo "  the benchmark - moving on. Recover later with:"
            echo "  just fetch-from-reader \"$CANONICAL_ARTIFACT_DIR\" \"{{dest}}\""
            exit 0
        fi
        echo "kubectl cp failed (attempt $cp_attempt/$CP_RETRIES), retrying in 10s..."
        sleep 10
        cp_attempt=$((cp_attempt + 1))
    done
    if [ ! -f "{{dest}}/profile_export_aiperf.json" ]; then
        echo "WARNING: kubectl cp reported success but profile_export_aiperf.json is still missing"
        echo "  from {{dest}} (likely a silently truncated transfer). The benchmark itself succeeded"
        echo "  and results are safe on the PVC at $CANONICAL_ARTIFACT_DIR. NOT retrying the"
        echo "  benchmark - moving on. Recover later with:"
        echo "  just fetch-from-reader \"$CANONICAL_ARTIFACT_DIR\" \"{{dest}}\""
        kubectl delete -n "$NS" job "$JOB" --ignore-not-found=true
        exit 0
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
    ACCESS=$(just --quiet _lustre-access-pod) || {
        echo "ERROR: no pod found to copy results from PVC"
        exit 1
    }
    PVC_POD=$(printf '%s\n' "$ACCESS" | awk '{print $1}')
    PVC_CONTAINER=$(printf '%s\n' "$ACCESS" | awk '{print $2}')
    if ! kubectl exec -n "$NS" "$PVC_POD" -c "$PVC_CONTAINER" -- test -f "{{remote}}/profile_export_aiperf.json" 2>/dev/null; then
        exit 1
    fi
    mkdir -p "{{dest}}"
    CP_RETRIES=3
    cp_attempt=1
    until kubectl cp -c "$PVC_CONTAINER" "$NS/${PVC_POD}:{{remote}}/." "{{dest}}"; do
        if [ "$cp_attempt" -ge "$CP_RETRIES" ]; then
            echo "ERROR: kubectl cp from PVC failed after $CP_RETRIES attempts"
            exit 1
        fi
        echo "kubectl cp from PVC failed (attempt $cp_attempt/$CP_RETRIES), retrying in 10s..."
        sleep 10
        cp_attempt=$((cp_attempt + 1))
    done

# Manually fetch already-completed benchmark results straight from the Lustre PVC (via the
# dedicated log-reader pod, see logs-dev-up) to a local directory on this machine. Use this when
# the orchestrator successfully ran a benchmark but couldn't copy the results out itself (see the
# WARNING printed by _run-job, which includes the exact command to run).
#
# Copies file-by-file with per-file retries and skips files already present locally, so it's safe
# to re-run: a huge file (e.g. server_metrics_export.json) repeatedly failing won't block the rest,
# and a second run only fetches what's still missing.
# Usage: just fetch-from-reader <remote-canonical-artifact-dir> <local-dest-dir> [all]
#   Pass "all" as 3rd arg to include optional archival files (server_metrics, gpu_telemetry, etc.).
fetch-from-reader remote dest fetch_all="":
    #!/usr/bin/env bash
    {{ if fetch_all == "all" { "export FETCH_ALL=1" } else { "true" } }}
    set -euxo pipefail
    NS={{NAMESPACE}}
    POD={{log_reader_pod}}
    if ! kubectl get pod -n "$NS" "$POD" >/dev/null 2>&1; then
        echo "log-reader pod not found, deploying..."
        just logs-dev-up
    fi
    kubectl wait -n "$NS" --for=condition=Ready "pod/$POD" --timeout=60s
    mkdir -p "{{dest}}/logs"

    SIZEMAP=$(mktemp)
    trap 'rm -f "$SIZEMAP"' EXIT

    _lsize() { stat -f%z "$1" 2>/dev/null || stat -c%s "$1" 2>/dev/null || echo 0; }

    # Snapshot remote file sizes (one kubectl call).
    kubectl exec -n "$NS" "$POD" -c log-reader -- \
        ls -la "{{remote}}/" 2>/dev/null | tail -n +2 > "$SIZEMAP"

    _rsize() { awk -v f="$1" '$NF == f {print $5; exit}' "$SIZEMAP"; }

    _fetch() {
        local rpath="$1" lpath="$2" label="$3" expected="$4"
        if [ -f "$lpath" ]; then
            local cur; cur=$(_lsize "$lpath")
            if [ "$cur" = "$expected" ]; then
                echo "  $label: already present ($expected bytes), skipping"
                return 0
            fi
            echo "  $label: local $cur ≠ remote $expected, re-downloading"
            rm -f "$lpath"
        fi
        local try=0 delay=5 ok=false
        while [ "$try" -lt 5 ]; do
            if kubectl cp -c log-reader "$NS/${POD}:${rpath}" "$lpath" 2>/dev/null; then
                local cur; cur=$(_lsize "$lpath")
                if [ "$cur" = "$expected" ]; then ok=true; break; fi
                echo "  $label: truncated ($cur/$expected bytes)"
                rm -f "$lpath"
            else
                echo "  $label: kubectl cp failed"
            fi
            try=$((try + 1))
            echo "  $label: retry $try/5 in ${delay}s..."
            sleep "$delay"
            delay=$((delay < 30 ? delay * 2 : 30))
        done
        if $ok; then echo "  $label: done ($expected bytes)"; return 0; fi
        echo "  $label: FAILED after 5 attempts"
        rm -f "$lpath"
        return 1
    }

    FAILED=""
    # Required files (report + chart).
    set -- \
        profile_export_aiperf.json \
        profile_export_aiperf.csv \
        profile_export_console.txt
    # Pass --all to also fetch optional archival artifacts.
    if [ "${FETCH_ALL:-}" = "1" ]; then
        set -- "$@" \
            server_metrics_export.csv \
            gpu_telemetry_export.jsonl \
            profile_export_aiperf_timeslices.json \
            profile_export_aiperf_timeslices.csv \
            profile_export.jsonl \
            server_metrics_export.json
    fi
    for f do
        rsz=$(_rsize "$f")
        if [ -z "$rsz" ]; then echo "  $f: not on PVC, skipping"; continue; fi
        _fetch "{{remote}}/$f" "{{dest}}/$f" "$f" "$rsz" || FAILED="$FAILED $f"
    done

    # Fetch logs/ with the same retry + size-verification logic.
    # kubectl exec -n "$NS" "$POD" -c log-reader -- \
    #     ls -la "{{remote}}/logs/" 2>/dev/null | tail -n +2 > "$SIZEMAP"
    # while read -r _ _ _ _ sz _ _ _ lf; do
    #     [ -z "$lf" ] && continue
    #     _fetch "{{remote}}/logs/$lf" "{{dest}}/logs/$lf" "logs/$lf" "$sz" \
    #         || FAILED="$FAILED logs/$lf"
    # done < "$SIZEMAP"

    if [ -n "$FAILED" ]; then
        echo "Done with failures:$FAILED"
        echo "Re-run to retry just the missing files: just fetch-from-reader \"{{remote}}\" \"{{dest}}\""
        exit 1
    fi
    echo "All artifacts fetched to {{dest}}/"

# Rename a results_<old> config directory (and every results_<old>_c<N>
# subdirectory) to results_<new>, so a re-run of the same setup (e.g. after an
# aiperf/image fix) shows up as its own distinct series in
# interactivity_vs_throughput.html instead of colliding with the original.
# Also updates config_name.txt / config_label.txt to the new name.
#   just rename-results-config results/infX-v1/clear ilmarkov-ix-1p-ep8-1d-ep8 ilmarkov-ix-1p-ep8-1d-ep8-v2
rename-results-config dir old new:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{dir}}"
    OLD="{{old}}"
    NEW="{{new}}"
    SRC="results_${OLD}"
    DST="results_${NEW}"
    if [ ! -d "$SRC" ]; then
        echo "ERROR: $SRC not found in {{dir}}" >&2
        exit 1
    fi
    if [ -e "$DST" ]; then
        echo "ERROR: $DST already exists in {{dir}}" >&2
        exit 1
    fi
    mv "$SRC" "$DST"
    cd "$DST"
    COUNT=0
    for d in "results_${OLD}"_c* "results_${OLD}"_attempt*; do
        [ -d "$d" ] || continue
        SUFFIX="${d#results_${OLD}}"
        mv "$d" "results_${NEW}${SUFFIX}"
        COUNT=$((COUNT + 1))
    done
    echo "$NEW" > config_name.txt
    echo "$NEW" > config_label.txt
    echo "Local: renamed results_${OLD} -> results_${NEW} ($COUNT subdirs)"

    # Also rename on the PVC so re-runs don't recover stale results.
    NS={{NAMESPACE}}
    POD={{log_reader_pod}}
    DIR_CLEAN=$(printf '%s' "{{dir}}" | sed 's#^\./##')
    REMOTE_BASE="{{lustre_prefix}}/{{manifesto_user}}/${DIR_CLEAN}"
    REMOTE_SRC="${REMOTE_BASE}/results_${OLD}"
    REMOTE_DST="${REMOTE_BASE}/results_${NEW}"
    if ! kubectl get pod -n "$NS" "$POD" >/dev/null 2>&1; then
        echo "PVC: log-reader pod not available, skipping PVC rename"
        echo "  To rename manually later:"
        echo "  kubectl exec -n $NS $POD -c log-reader -- mv \"$REMOTE_SRC\" \"$REMOTE_DST\""
        exit 0
    fi
    if ! kubectl exec -n "$NS" "$POD" -c log-reader -- test -d "$REMOTE_SRC" 2>/dev/null; then
        echo "PVC: $REMOTE_SRC not found, nothing to rename"
        exit 0
    fi
    if kubectl exec -n "$NS" "$POD" -c log-reader -- test -e "$REMOTE_DST" 2>/dev/null; then
        echo "PVC: ERROR $REMOTE_DST already exists, skipping PVC rename" >&2
        exit 1
    fi
    kubectl exec -n "$NS" "$POD" -c log-reader -- mv "$REMOTE_SRC" "$REMOTE_DST"
    # Rename concurrency and attempt subdirs inside the PVC config dir.
    PVC_COUNT=0
    while IFS= read -r d; do
        [ -z "$d" ] && continue
        SUFFIX="${d#results_${OLD}}"
        kubectl exec -n "$NS" "$POD" -c log-reader -- \
            mv "${REMOTE_DST}/${d}" "${REMOTE_DST}/results_${NEW}${SUFFIX}"
        PVC_COUNT=$((PVC_COUNT + 1))
    done < <(kubectl exec -n "$NS" "$POD" -c log-reader -- \
        ls "${REMOTE_DST}/" 2>/dev/null | grep "^results_${OLD}")
    echo "PVC:   renamed results_${OLD} -> results_${NEW} ($PVC_COUNT subdirs)"

# Wait for all running requests to drain on prefill and decode pods.
drain:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    PREFILL_SELECTOR=$(just --quiet _pod-selector prefill)
    DECODE_SELECTOR=$(just --quiet _pod-selector decode)
    PREFILL_PORTS=$(just --quiet _vllm-ports prefill)
    DECODE_PORTS=$(just --quiet _vllm-ports decode)
    echo "Waiting for all requests to drain..."
    while true; do
        TOTAL=0
        for pod in $(kubectl get pods -n "$NS" -l "$PREFILL_SELECTOR" -o jsonpath='{.items[*].metadata.name}'); do
            for port in $PREFILL_PORTS; do
                N=$(kubectl exec -n "$NS" "$pod" -c vllm -- \
                    curl -sf "http://localhost:${port}/metrics" 2>/dev/null \
                    | grep '^vllm:num_requests_running' | awk '{printf "%d", $2}') || N=0
                TOTAL=$((TOTAL + N))
            done
        done
        for pod in $(kubectl get pods -n "$NS" -l "$DECODE_SELECTOR" -o jsonpath='{.items[*].metadata.name}'); do
            for port in $DECODE_PORTS; do
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
    PREFILL_PORTS=$(just --quiet _vllm-ports prefill)
    DECODE_PORTS=$(just --quiet _vllm-ports decode)
    # Reset vLLM prefix cache (GPU + CPU tiers) via API
    for pod in $(kubectl get pods -n "$NS" -l "$PREFILL_SELECTOR" -o jsonpath='{.items[*].metadata.name}'); do
        echo "Resetting prefix cache on prefill $pod..."
        for port in $PREFILL_PORTS; do
            kubectl exec -n "$NS" "$pod" -c vllm -- \
                curl -sf -X POST "http://localhost:${port}/reset_prefix_cache?reset_external=true" 2>/dev/null || true
        done
    done
    for pod in $(kubectl get pods -n "$NS" -l "$DECODE_SELECTOR" -o jsonpath='{.items[*].metadata.name}'); do
        echo "Resetting prefix cache on decode $pod..."
        for port in $DECODE_PORTS; do
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
    POD=""
    for role in prefill decode; do
        SELECTOR=$(just --quiet _pod-selector "$role")
        POD=$(kubectl get pod -n "$NS" -l "$SELECTOR" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || true
        if [ -n "$POD" ]; then break; fi
    done
    if [ -z "$POD" ]; then
        echo "ERROR: no prefill or decode pod found for vllm-version"
        exit 1
    fi
    # Image tag
    kubectl get pod -n "$NS" "$POD" -o jsonpath='{.spec.containers[0].image}' > "{{dest}}/vllm_image.txt"
    echo "" >> "{{dest}}/vllm_image.txt"
    # vLLM version from pod startup logs
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

log_reader_pod := "agentx-log-reader"
log_reader_image := env_var_or_default('LOG_READER_IMAGE', 'busybox:1.36')

# Deploy a tiny, dedicated pod that only mounts the Lustre PVC (no RBAC, no
# benchmark-orchestrator lifecycle to fight with). busybox is multi-arch and
# has everything `kubectl cp`/exec need (sh, ls, test, tar).
#
# The Lustre CSI driver's lnet kernel module is only loaded on some amd64
# nodes (whichever pool vllm runs on), not amd64 nodes in general - plain
# `kubernetes.io/arch: amd64` (what orchestrator.yaml uses) isn't a strong
# enough constraint and can still land on a node without lnet. Instead, use
# podAffinity to force scheduling onto the SAME node as an already-running
# vllm pod (llm-d.ai/role), since that node has already proven it can mount
# the PVC.
#
# Pod specs are mostly immutable in-place, so always delete + recreate
# instead of `kubectl apply`-patching a possibly-stuck previous pod.
logs-dev-up:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    just logs-dev-down
    TMP=$(mktemp)
    trap "rm -f $TMP" EXIT
    cat > "$TMP" <<EOF
    apiVersion: v1
    kind: Pod
    metadata:
      name: {{log_reader_pod}}
      labels:
        app: {{log_reader_pod}}
    spec:
      restartPolicy: Always
      affinity:
        podAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector:
                matchExpressions:
                  - key: llm-d.ai/role
                    operator: Exists
              topologyKey: kubernetes.io/hostname
      securityContext:
        runAsUser: 0
        runAsGroup: 0
      containers:
        - name: log-reader
          image: {{log_reader_image}}
          imagePullPolicy: IfNotPresent
          command: ["sleep", "infinity"]
          resources:
            requests:
              cpu: "50m"
              memory: 64Mi
            limits:
              cpu: "200m"
              memory: 128Mi
          volumeMounts:
            - name: lustre
              mountPath: {{lustre_mount}}
      volumes:
        - name: lustre
          persistentVolumeClaim:
            claimName: {{lustre_claim}}
    EOF
    kubectl apply -n "$NS" -f "$TMP"
    kubectl wait -n "$NS" --for=condition=Ready "pod/{{log_reader_pod}}" --timeout=120s
    echo "Log-reader pod ready: {{log_reader_pod}} (ns=$NS, mounts {{lustre_claim}} at {{lustre_mount}})"

# Delete the log-reader pod.
logs-dev-down:
    kubectl delete pod -n {{NAMESPACE}} {{log_reader_pod}} --ignore-not-found=true

# Dump the FULL persisted vLLM log history from Lustre, including crashed runs.
#
# `dump-logs` only runs `kubectl logs`, which returns stdout for the pod's
# *current* container instance. llm-manifesto's launch script
# (manifesto/launch.py:build_launch_script) tees vLLM's stdout into a NEW,
# uniquely-named file on every pod (re)start:
#   LOG_FILE="$LOG_DIR/${HOSTNAME}_$(date +%Y%m%d-%H%M%S).log"
# where LOG_DIR is `manifesto log-path <spec> --role <role>`
# (e.g. /mnt/lustre/$USER/logs/decode on oci-gb200). So when a pod crashes
# and restarts, `kubectl logs` only shows the fresh post-restart run, but the
# crashed run's full log is still sitting untouched on Lustre. This reads it
# back through the dedicated `just logs-dev-up` pod (auto-deployed if not
# already running) instead of a live/crashlooping vllm pod.
dump-crash-logs dest=".":
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    MANIFESTO="{{manifesto_root}}"
    OUTDIR="{{dest}}/lustre-logs"
    mkdir -p "$OUTDIR"

    if ! kubectl get pod -n "$NS" {{log_reader_pod}} >/dev/null 2>&1; then
        echo "log-reader pod not found, deploying..."
        just logs-dev-up
    fi
    kubectl wait -n "$NS" --for=condition=Ready "pod/{{log_reader_pod}}" --timeout=60s

    if ! kubectl exec -n "$NS" {{log_reader_pod}} -c log-reader -- sh -c "test -d '{{lustre_mount}}'" 2>/dev/null; then
        echo "ERROR: {{lustre_mount}} is not mounted in {{log_reader_pod}}." >&2
        echo "  Try: just logs-dev-down && just logs-dev-up" >&2
        exit 1
    fi

    for role in decode prefill; do
        LOG_DIR=$(cd "$MANIFESTO" && uv run manifesto log-path "{{manifesto_spec}}" --user "{{manifesto_user}}" --role "$role")
        if ! kubectl exec -n "$NS" {{log_reader_pod}} -c log-reader -- sh -c "test -d '$LOG_DIR'" 2>/dev/null; then
            echo "  $role: $LOG_DIR does not exist (no pod for this role has started yet, or wrong --user/spec)"
            continue
        fi
        COUNT=$(kubectl exec -n "$NS" {{log_reader_pod}} -c log-reader -- sh -c "ls -1 '$LOG_DIR' | wc -l" 2>/dev/null | tr -d '[:space:]') || COUNT=0
        if [ -z "$COUNT" ] || [ "$COUNT" = "0" ]; then
            echo "  $role: $LOG_DIR exists but is empty"
            continue
        fi
        echo "  $role: $COUNT log files in $LOG_DIR"
        mkdir -p "$OUTDIR/$role"
        kubectl cp -c log-reader "$NS/{{log_reader_pod}}:${LOG_DIR}/." "$OUTDIR/$role" 2>/dev/null || true
    done
    echo "Crash logs saved to $OUTDIR/ (every restart, not just the current run)"

# Port-forward Grafana to localhost (background, like llm-manifesto `just grafana`).
grafana port="3000":
    kubectl port-forward -n {{grafana_namespace}} svc/{{grafana_service}} {{port}}:80 > /dev/null 2>&1 &
    @echo "Grafana: http://localhost:{{port}}  (background; namespace={{grafana_namespace}} svc={{grafana_service}})"

# Export Grafana dashboards for result directories.
# Usage: just scrape-grafana results/<run>/results_$USER-wide-ep-3p-ep8-1d-ep8/results_$USER-wide-ep-3p-ep8-1d-ep8_c64
scrape-grafana +dirs:
    python3 export_dashboard.py results {{dirs}}

# Scrape Grafana dashboards and generate interactivity chart.
# Reads namespace.txt from each result directory to find the right Grafana instance.
# Runs export_dashboard.py inside the orchestrator pod via kubectl exec (no port-forward needed).
# Usage: just report results/<run>
#        just report results/<run> --force   (re-run even if output files exist)
report outdir *flags:
    #!/usr/bin/env bash
    set -euo pipefail
    FORCE=false
    for f in {{flags}}; do
        case "$f" in --force|-f) FORCE=true;; esac
    done
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
            kubectl cp prefix_cache_report.py "$NS/${POD}:/workspace/prefix_cache_report.py"
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
        DASHBOARD_FLAG=""
        if [ -f "$PARENT/topology.txt" ] && [ "$(cat "$PARENT/topology.txt")" = "aggregated" ]; then
            DASHBOARD_FLAG="--dashboard aggregate-overview"
        fi
        echo "=== $NAME ($NS): scraping Grafana ==="
        TIMESTAMPS=$(python3 -c "import json, datetime; d=json.load(open('$dir/profile_export_aiperf.json')); f=lambda s: datetime.datetime.fromisoformat(s).replace(tzinfo=datetime.timezone.utc).timestamp(); print(f(d['start_time'])-60); print(f(d['end_time'])+60)")
        START=$(echo "$TIMESTAMPS" | head -1)
        END=$(echo "$TIMESTAMPS" | tail -1)
        if [ "$FORCE" = false ] && [ -f "$dir/dashboard.html" ]; then
            echo "  dashboard.html exists, skipping (use --force to re-run)"
        else
        echo "  Time range: $START → $END"
        echo "  Deployment: $DEPLOYMENT; pod filter seed: $POD_REGEX"
        kubectl exec -n "$NS" "$POD" -- python3 /workspace/export_dashboard.py \
            --grafana-url "$GRAFANA_URL" \
            --deployment "$DEPLOYMENT" \
            --pod-regex "$POD_REGEX" \
            $DASHBOARD_FLAG \
            single --start "$START" --end "$END" -o "/workspace/dashboard_${NAME}.html" || {
            echo "  WARNING: scrape failed for $NAME, skipping"
            continue
        }
        kubectl cp "$NS/${POD}:/workspace/dashboard_${NAME}.html" "$dir/dashboard.html" 2>/dev/null || true
        kubectl exec -n "$NS" "$POD" -- rm -f "/workspace/dashboard_${NAME}.html"
        fi
        echo "=== $NAME ($NS): prefix-cache hit rate report ==="
        if [ "$FORCE" = false ] && [ -f "$dir/prefix_cache_report.json" ]; then
            echo "  prefix_cache_report.json exists, skipping (use --force to re-run)"
        else
        CACHE_STATS=$(python3 -c "import json; d=json.load(open('$dir/profile_export_aiperf.json')); print(d.get('theoretical_prefix_cache_hit',{}).get('avg','')); print(d.get('overall_usage_prompt_cache_read_pct',{}).get('avg',''))")
        THEORETICAL_PCT=$(echo "$CACHE_STATS" | sed -n '1p')
        USAGE_PCT=$(echo "$CACHE_STATS" | sed -n '2p')
        kubectl exec -n "$NS" "$POD" -- python3 /workspace/prefix_cache_report.py \
            --grafana-url "$GRAFANA_URL" \
            --deployment "$DEPLOYMENT" \
            --pod-regex "$POD_REGEX" \
            --start "$START" --end "$END" \
            --name "$NAME" \
            --theoretical-pct "$THEORETICAL_PCT" \
            --usage-overall-pct "$USAGE_PCT" \
            -o "/workspace/prefix_cache_${NAME}.txt" \
            --output-json "/workspace/prefix_cache_${NAME}.json" || {
            echo "  WARNING: prefix-cache report failed for $NAME, skipping"
            continue
        }
        kubectl cp "$NS/${POD}:/workspace/prefix_cache_${NAME}.txt" "$dir/prefix_cache_report.txt" 2>/dev/null || true
        kubectl cp "$NS/${POD}:/workspace/prefix_cache_${NAME}.json" "$dir/prefix_cache_report.json" 2>/dev/null || true
        kubectl exec -n "$NS" "$POD" -- rm -f "/workspace/prefix_cache_${NAME}.txt" "/workspace/prefix_cache_${NAME}.json"
        fi
        echo "=== $NAME ($NS): KV cache config ==="
        if [ "$FORCE" = false ] && [ -f "$dir/kv_cache_config.json" ]; then
            echo "  kv_cache_config.json exists, skipping (use --force to re-run)"
        else
        KV_DECODE=0 KV_PREFILL=0 KV_FOUND=false
        MANIFESTO="{{manifesto_root}}"
        LOG_READER={{log_reader_pod}}
        BENCH_TS=$(python3 -c "from datetime import datetime, timezone; print(datetime.fromtimestamp($START+60, tz=timezone.utc).strftime('%Y%m%d-%H%M%S'))")
        if kubectl get pod -n "$NS" "$LOG_READER" >/dev/null 2>&1; then
            for role in decode prefill; do
                LOG_DIR=$(cd "$MANIFESTO" && uv run manifesto log-path "{{manifesto_spec}}" --user "{{manifesto_user}}" --role "$role" 2>/dev/null) || continue
                # Find log files with timestamp <= benchmark start, pick the latest per pod
                MATCHING=$(kubectl exec -n "$NS" "$LOG_READER" -c log-reader -- sh -c "
                    for f in ${LOG_DIR}/*.log; do
                        [ -f \"\$f\" ] || continue
                        bn=\$(basename \"\$f\")
                        ts=\$(echo \"\$bn\" | grep -o '[0-9]\\{8\\}-[0-9]\\{6\\}' | tail -1)
                        [ -z \"\$ts\" ] && continue
                        [ \"\$ts\" \\> \"$BENCH_TS\" ] && continue
                        echo \"\$ts \$f\"
                    done | sort -t' ' -k1,1r" 2>/dev/null) || continue
                [ -z "$MATCHING" ] && continue
                # Deduplicate: keep the latest file per pod hostname
                # Only consider logs from pods matching this deployment
                POD_FILTER=$(echo "$POD_REGEX" | sed 's/ /|/g')
                SEEN_PODS=""
                FILES=""
                while IFS=' ' read -r _ts fpath; do
                    pod_name=$(basename "$fpath" | sed 's/_[0-9]\{8\}-[0-9]\{6\}\.log$//')
                    # Skip logs from other deployments
                    if ! echo "$pod_name" | grep -qE "$POD_FILTER"; then
                        continue
                    fi
                    if ! echo "$SEEN_PODS" | grep -qF "|${pod_name}|"; then
                        SEEN_PODS="${SEEN_PODS}|${pod_name}|"
                        FILES="${FILES} ${fpath}"
                    fi
                done <<< "$MATCHING"
                [ -z "$FILES" ] && continue
                # Take the first GPU KV cache size value (all ranks report the same capacity)
                ROLE_TOKENS=""
                for f in $FILES; do
                    ROLE_TOKENS=$(kubectl exec -n "$NS" "$LOG_READER" -c log-reader -- grep 'GPU KV cache size:' "$f" 2>/dev/null \
                        | head -1 | sed 's/.*GPU KV cache size: *//;s/ *tokens.*//' | tr -d ',') || true
                    if [ -n "$ROLE_TOKENS" ] && [ "$ROLE_TOKENS" != "0" ]; then
                        echo "  $role: $ROLE_TOKENS tokens (from $(basename "$f"))"
                        KV_FOUND=true
                        break
                    fi
                done
                if [ -n "$ROLE_TOKENS" ] && [ "$ROLE_TOKENS" != "0" ]; then
                    if [ "$role" = "decode" ]; then KV_DECODE=$ROLE_TOKENS; else KV_PREFILL=$ROLE_TOKENS; fi
                fi
            done
        fi
        if $KV_FOUND; then
            python3 -c "import json; json.dump({'prefill': $KV_PREFILL or None, 'decode': $KV_DECODE or None}, open('$dir/kv_cache_config.json','w'), indent=2)"
            echo "  Written to $dir/kv_cache_config.json"
        else
            echo "  WARNING: no KV cache size found in logs, skipping"
        fi
        fi
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
    inst = Instance("{{manifesto_user}}", spec.release)
    instance = inst.instance_id
    roles = {r.name: r for r in spec.roles}
    def role_gpus(name):
        r = roles.get(name)
        return 0 if r is None else r.lws.size * r.lws.replicas * r.gpus_per_pod
    pods_parts = []
    for r in spec.roles:
        wn = inst.user_scoped_name(r.workload_name) if r.workload_name else inst.name(r.name)
        pods_parts.append(wn + ".*")
    print(f"instance={instance}")
    print(f"model={spec.model.id}")
    print(f"model_label={spec.model.label}")
    print(f"release={spec.release}")
    print(f"topology={spec.topology.value}")
    print(f"decode_gpus={role_gpus('decode')}")
    print(f"prefill_gpus={role_gpus('prefill')}")
    print(f"pods={' '.join(pods_parts)}")
    PY

_model-url:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "{{url}}" ]; then
        printf '%s\n' "{{url}}"
        exit 0
    fi
    cd "{{manifesto_root}}"
    GATEWAY_SVC=$(uv run manifesto name "{{manifesto_spec}}" {{manifesto_gateway}} --user "{{manifesto_user}}")
    printf 'http://%s.%s.svc.cluster.local:80/v1\n' "$GATEWAY_SVC" "{{NAMESPACE}}"

_server-metrics-args:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "{{server_metrics_url}}" ]; then
        printf -- '--server-metrics %s\n' "{{server_metrics_url}}"
        exit 0
    fi
    printf -- '--no-server-metrics\n'

_gpu-telemetry-args:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "{{gpu_telemetry_urls}}" ]; then
        printf -- '--gpu-telemetry %s\n' "{{gpu_telemetry_urls}}"
        exit 0
    fi
    printf -- '--no-gpu-telemetry\n'

_pod-selector role="":
    #!/usr/bin/env bash
    set -euo pipefail
    # Use llm-d.ai/owner, not app.kubernetes.io/instance: the instance id embeds
    # spec.release (e.g. ...-v2 vs ...-v3) and stops matching running pods after
    # a release bump while the old deployment is still up.
    if [ -n "{{role}}" ]; then
        printf 'llm-d.ai/owner=%s,llm-d.ai/role=%s,app.kubernetes.io/component=model-server\n' \
            "{{manifesto_user}}" "{{role}}"
    else
        printf 'llm-d.ai/owner=%s,app.kubernetes.io/component=model-server\n' "{{manifesto_user}}"
    fi

# Print "pod_name container" for a pod that mounts the Lustre PVC.
# Prefer an explicit pod (e.g. the completed benchmark job) when provided.
_lustre-access-pod preferred_pod="" preferred_container="":
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    if [ -n "{{preferred_pod}}" ] && [ -n "{{preferred_container}}" ]; then
        if kubectl get pod -n "$NS" "{{preferred_pod}}" >/dev/null 2>&1; then
            printf '%s %s\n' "{{preferred_pod}}" "{{preferred_container}}"
            exit 0
        fi
    fi
    for role in prefill decode; do
        SELECTOR=$(just --quiet _pod-selector "$role")
        POD=$(kubectl get pod -n "$NS" -l "$SELECTOR" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || true
        if [ -n "$POD" ]; then
            printf '%s vllm\n' "$POD"
            exit 0
        fi
    done
    INFO=$(just --quiet _manifesto-info)
    PODS=$(printf '%s\n' "$INFO" | sed -n 's/^pods=//p')
    for pattern in $PODS; do
        POD=$(kubectl get pods -n "$NS" -o name 2>/dev/null | grep -E "$pattern" | head -1 | sed 's|pod/||')
        if [ -n "$POD" ]; then
            printf '%s vllm\n' "$POD"
            exit 0
        fi
    done
    LR={{log_reader_pod}}
    if kubectl get pod -n "$NS" "$LR" >/dev/null 2>&1; then
        printf '%s log-reader\n' "$LR"
        exit 0
    fi
    exit 1

_vllm-ports role:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{manifesto_root}}"
    uv run python - <<'PY'
    from manifesto.spec import load_spec
    from manifesto.cluster import load_cluster
    from manifesto.parallelism import parallel_layout
    from manifesto.dp_ports import derive_ports
    spec = load_spec("{{manifesto_spec}}", load_cluster("{{manifesto_cluster}}"))
    try:
        role = spec.role("{{role}}")
    except (KeyError, StopIteration):
        pass
    else:
        layout = parallel_layout(role)
        ports = derive_ports(rank_count=layout.dp_local_size, public_base=role.serving_port_base, backend_base=role.backend_port_base)
        print(" ".join(str(p) for p in ports.backend))
    PY

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
    TOPOLOGY=$(printf '%s\n' "$INFO" | sed -n 's/^topology=//p')
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
    printf '%s\n' "$TOPOLOGY" > "$WORK/results_${CONFIG_NAME}/topology.txt"
    python3 gen_interactivity_chart.py "$WORK"
    mv "$WORK/interactivity_vs_throughput.html" "$DEST/interactivity_vs_throughput.html"

start-model:
    #!/usr/bin/env bash
    set -euo pipefail
    cd "{{manifesto_root}}"
    uv run manifesto render "{{manifesto_spec}}" --cluster "{{manifesto_cluster}}" --namespace "{{NAMESPACE}}" --user "{{manifesto_user}}" {{manifesto_args}} \
        | uv run python "{{repo_root}}/inject_kueue_queue.py" --queue "{{kueue_queue}}" \
        | kubectl apply -n "{{NAMESPACE}}" -f -
    cd "{{repo_root}}"
    just _wait-model-ready
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
        MISSING=""
        for f in {{benchmark_artifact_files}}; do
            [ -f "$RDIR/$f" ] || MISSING="${MISSING} $f"
        done
        if [ -n "$MISSING" ]; then
            echo "concurrency=$C: missing local files (${MISSING# }), attempting recovery from PVC..."
            just _copy-result-from-pvc "$REMOTE" "$RDIR" 2>/dev/null || true
            MISSING=""
            for f in {{benchmark_artifact_files}}; do
                [ -f "$RDIR/$f" ] || MISSING="${MISSING} $f"
            done
        fi
        if [ -z "$MISSING" ]; then
            echo "=== concurrency=$C already exists (recovered from PVC if needed), skipping ==="
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
            if just _copy-result-from-pvc "$REMOTE" "$RDIR" 2>/dev/null; then
                echo "concurrency=$C: benchmark completed but a post-run step failed (e.g. artifact copy); recovered full result from PVC, not retrying"
                RUN_OK=true
                just wipe 2>/dev/null || true
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

sweep outdir duration="900" keep_model=sweep_keep_model:
    #!/usr/bin/env bash
    set -euo pipefail
    KEEP_MODEL="{{keep_model}}"
    mkdir -p "{{outdir}}"
    INFO=$(just --quiet _manifesto-info)
    CONFIG_NAME=$(printf '%s\n' "$INFO" | sed -n 's/^instance=//p')
    MODEL_ID=$(printf '%s\n' "$INFO" | sed -n 's/^model=//p')
    MODEL_LABEL=$(printf '%s\n' "$INFO" | sed -n 's/^model_label=//p')
    RELEASE=$(printf '%s\n' "$INFO" | sed -n 's/^release=//p')
    DECODE_GPUS=$(printf '%s\n' "$INFO" | sed -n 's/^decode_gpus=//p')
    PREFILL_GPUS=$(printf '%s\n' "$INFO" | sed -n 's/^prefill_gpus=//p')
    PODS=$(printf '%s\n' "$INFO" | sed -n 's/^pods=//p')
    TOPOLOGY=$(printf '%s\n' "$INFO" | sed -n 's/^topology=//p')
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
    if [ "$KEEP_MODEL" = "true" ] || [ "$KEEP_MODEL" = "1" ]; then
        echo "Keeping existing model (skip stop/start/check/warmup)"
    else
        just stop-model 2>/dev/null || true
        just start-model
        just check
        just warmup
    fi
    mkdir -p "$dir"
    echo "{{NAMESPACE}}" > "$dir/namespace.txt"
    echo "$MODEL_ID" > "$dir/model.txt"
    echo "$MODEL_LABEL" > "$dir/model_label.txt"
    echo "$CONFIG_NAME" > "$dir/config_name.txt"
    echo "$RELEASE" > "$dir/config_label.txt"
    echo "$DECODE_GPUS" > "$dir/decode_gpus.txt"
    echo "$PREFILL_GPUS" > "$dir/prefill_gpus.txt"
    echo "$PODS" > "$dir/pods.txt"
    echo "$TOPOLOGY" > "$dir/topology.txt"
    echo "{{manifesto_spec}}" > "$dir/manifesto_spec.txt"
    (cd "{{manifesto_root}}" && uv run manifesto instance-id "{{manifesto_spec}}" --user "{{manifesto_user}}") > "$dir/manifesto_instance.txt"
    (cd "{{manifesto_root}}" && uv run manifesto render "{{manifesto_spec}}" --cluster "{{manifesto_cluster}}" --namespace "{{NAMESPACE}}" --user "{{manifesto_user}}" {{manifesto_args}}) \
        | uv run python "{{repo_root}}/inject_kueue_queue.py" --queue "{{kueue_queue}}" \
        > "$dir/manifest.yaml"
    just vllm-version "$dir"
    # Capture per-rank KV cache size from pod logs (before any restarts)
    echo "=== Capturing KV cache config ==="
    KV_DECODE="" KV_PREFILL=""
    for pod_pattern in $PODS; do
        POD_NAME=$(kubectl get pods -n "{{NAMESPACE}}" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null \
            | grep -m1 "$pod_pattern") || true
        [ -z "$POD_NAME" ] && continue
        TOKENS=""
        while IFS= read -r line; do
            case "$line" in
                *"GPU KV cache size:"*)
                    TOKENS=$(printf '%s' "$line" | sed 's/.*GPU KV cache size: *//;s/ *tokens.*//' | tr -d ',')
                    break
                    ;;
            esac
        done < <(kubectl logs -n "{{NAMESPACE}}" "$POD_NAME" -c vllm 2>/dev/null || true)
        [ -z "$TOKENS" ] && continue
        if echo "$pod_pattern" | grep -qi "prefill"; then
            KV_PREFILL="$TOKENS"
            echo "  prefill: $TOKENS tokens (from $POD_NAME)"
        else
            KV_DECODE="$TOKENS"
            echo "  decode: $TOKENS tokens (from $POD_NAME)"
        fi
    done
    if [ -n "$KV_PREFILL" ]; then PREFILL_JSON="$KV_PREFILL"; else PREFILL_JSON="null"; fi
    if [ -n "$KV_DECODE" ]; then DECODE_JSON="$KV_DECODE"; else DECODE_JSON="null"; fi
    python3 -c "import json; json.dump({'prefill': $PREFILL_JSON, 'decode': $DECODE_JSON}, open('$dir/kv_cache_config.json','w'), indent=2)"
    echo "  Saved to $dir/kv_cache_config.json"
    just sweep-concurrency "$CONFIG_NAME" "$dir" {{duration}}
    if [ "$KEEP_MODEL" != "true" ] && [ "$KEEP_MODEL" != "1" ]; then
        just stop-model
    else
        echo "Keeping model running after sweep"
    fi

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
    {{container_cli}} build --platform linux/amd64 \
      --build-arg MANIFESTO_REPO="{{orchestrator_manifesto_repo}}" \
      --build-arg MANIFESTO_REF="{{orchestrator_manifesto_ref}}" \
      -f Dockerfile.orchestrator -t {{orchestrator_image}} .
    {{container_cli}} push {{orchestrator_image}}

orchestrator-spec-config:
    #!/usr/bin/env bash
    set -euo pipefail
    TMP=$(mktemp)
    trap "rm -f $TMP" EXIT
    {
        printf "BENCHMARK_DURATION='%s'\n" "{{duration}}"
        printf "BENCHMARK_CONCURRENCIES='%s'\n" "{{benchmark_concurrencies}}"
        printf "BENCHMARK_RETRIES='%s'\n" "{{benchmark_retries}}"
        printf "POD_START_TIMEOUT='%s'\n" "{{pod_start_timeout}}"
        printf "MAX_CONTEXT_LENGTH='%s'\n" "{{max_context_length}}"
        printf "LUSTRE_CLAIM='%s'\n" "{{lustre_claim}}"
        printf "LUSTRE_MOUNT='%s'\n" "{{lustre_mount}}"
        printf "LUSTRE_PREFIX='%s'\n" "{{lustre_prefix}}"
        printf "AIPERF_IMAGE='%s'\n" "{{aiperf_image}}"
        printf "AIPERF_PUBLIC_DATASET='%s'\n" "{{aiperf_public_dataset}}"
        printf "AIPERF_RANDOM_SEED='%s'\n" "{{aiperf_random_seed}}"
        printf "AIPERF_FAILED_REQUEST_THRESHOLD='%s'\n" "{{aiperf_failed_request_threshold}}"
        printf "AIPERF_TRAJECTORY_START_MIN_RATIO='%s'\n" "{{aiperf_trajectory_start_min_ratio}}"
        printf "AIPERF_TRAJECTORY_START_MAX_RATIO='%s'\n" "{{aiperf_trajectory_start_max_ratio}}"
        printf "AIPERF_NUM_DATASET_ENTRIES='%s'\n" "{{aiperf_num_dataset_entries}}"
        printf "AIPERF_SLICE_DURATION='%s'\n" "{{aiperf_slice_duration}}"
        printf "AIPERF_ALLOW_DATASET_WRAP='%s'\n" "{{aiperf_allow_dataset_wrap}}"
        printf "AIPERF_DATASET_CONFIGURATION_TIMEOUT='%s'\n" "{{aiperf_dataset_configuration_timeout}}"
        printf "AIPERF_DATASET_MMAP_CACHE_DIR='%s'\n" "{{aiperf_mmap_cache_dir}}"
        printf "AIPERF_CACHE_WARMUP_SECONDS='%s'\n" "{{aiperf_cache_warmup_seconds}}"
        printf "AIPERF_WARMUP_GRACE_SECONDS='%s'\n" "{{aiperf_warmup_grace_seconds}}"
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

# Sync only llm-manifesto models/ + clusters/ into the orchestrator pod when
# local and in-pod YAML hashes differ (not manifesto Python/templates/etc.).
orchestrator-sync-manifesto:
    #!/usr/bin/env bash
    set -euo pipefail
    MANIFESTO="{{manifesto_root}}"
    NS={{NAMESPACE}}
    POD_DEST="/workspace/llm-manifesto"
    if [ ! -d "$MANIFESTO/models" ] || [ ! -d "$MANIFESTO/clusters" ]; then
        echo "ERROR: MANIFESTO_ROOT must contain models/ and clusters/: $MANIFESTO" >&2
        exit 1
    fi
    # Hash YAML under models/ and clusters/ only — not the rest of llm-manifesto.
    compute_local_hash() {
        find "$MANIFESTO/models" "$MANIFESTO/clusters" \
            -type f \( -name '*.yaml' -o -name '*.yml' \) -print0 \
            | sort -z \
            | xargs -0 shasum -a 256 2>/dev/null \
            | awk '{print $1}' \
            | shasum -a 256 \
            | awk '{print $1}'
    }
    LOCAL_HASH=$(compute_local_hash)
    POD=$(kubectl get pod -n "$NS" -l app={{orchestrator_deploy}} -o jsonpath='{.items[0].metadata.name}')
    POD_HASH=$(kubectl exec -n "$NS" "$POD" -- sh -c \
        'find '"$POD_DEST"'/models '"$POD_DEST"'/clusters \
            -type f \( -name "*.yaml" -o -name "*.yml" \) -print0 2>/dev/null \
            | sort -z \
            | xargs -0 sha256sum 2>/dev/null \
            | awk "{print \$1}" \
            | sha256sum \
            | awk "{print \$1}"')
    FORCE="{{orchestrator_force_sync}}"
    if [ "$FORCE" != "true" ] && [ "$FORCE" != "1" ]; then
        if [ -n "$POD_HASH" ] && [ "$POD_HASH" = "$LOCAL_HASH" ]; then
            echo "models/ + clusters/ match pod (hash $(printf '%.12s' "$LOCAL_HASH")…), skipping sync"
            exit 0
        fi
    fi
    echo "Syncing models/ + clusters/ to pod $POD (local $(printf '%.12s' "$LOCAL_HASH")… pod $(printf '%.12s' "$POD_HASH")…)..."
    kubectl cp "$MANIFESTO/models/." "$NS/${POD}:${POD_DEST}/models/"
    kubectl cp "$MANIFESTO/clusters/." "$NS/${POD}:${POD_DEST}/clusters/"
    echo "models/ + clusters/ sync complete."

# Copy local agentx-mvp harness files into the orchestrator pod (Justfile, inject script).
orchestrator-sync-harness:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    POD=$(kubectl get pod -n "$NS" -l app={{orchestrator_deploy}} -o jsonpath='{.items[0].metadata.name}')
    echo "Syncing agentx-mvp harness to pod $POD ..."
    kubectl cp "{{repo_root}}/Justfile" "$NS/${POD}:/workspace/agentx-mvp/Justfile"
    kubectl cp "{{repo_root}}/inject_kueue_queue.py" "$NS/${POD}:/workspace/agentx-mvp/inject_kueue_queue.py"
    echo "Harness sync complete."

orchestrator-sync:
    just orchestrator-spec-config
    # /workspace/agentx-mvp is ephemeral container storage (not a persistent volume), so restarting
    # after syncing wipes the just-copied files back to whatever's baked into the image. Restart
    # first to pick up the benchmark-sweep.env ConfigMap (subPath mounts don't hot-reload), then
    # sync the harness/manifesto files onto the fresh pod so they actually stick.
    kubectl rollout restart -n {{NAMESPACE}} deploy/{{orchestrator_deploy}}
    kubectl rollout status -n {{NAMESPACE}} deploy/{{orchestrator_deploy}} --timeout=300s
    just orchestrator-sync-manifesto
    just orchestrator-sync-harness

orchestrator-run outdir="" duration=duration redeploy=orchestrator_redeploy keep_model=sweep_keep_model:
    #!/usr/bin/env bash
    set -euo pipefail
    OUTDIR="{{outdir}}"
    REDEPLOY="{{redeploy}}"
    if [ -z "$OUTDIR" ]; then
        OUTDIR=$(just --quiet run-dir "{{duration}}")
    fi
    if [ "$REDEPLOY" = "true" ] || [ "$REDEPLOY" = "1" ]; then
        just orchestrator-deploy
    else
        echo "Keeping existing orchestrator (skip deploy/restart)"
        POD=$(kubectl get pod -n {{NAMESPACE}} -l app={{orchestrator_deploy}} -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || true
        if [ -z "$POD" ]; then
            echo "ERROR: no orchestrator pod found; run just orchestrator-deploy first or set ORCHESTRATOR_REDEPLOY=true" >&2
            exit 1
        fi
        echo "Orchestrator pod: $POD"
    fi
    # orchestrator-deploy already restarted the pod with the current ConfigMap,
    # so sync files directly onto that fresh pod instead of going through
    # orchestrator-sync (which would trigger a second, redundant restart+wait).
    just orchestrator-sync-manifesto
    just orchestrator-sync-harness
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
      MANIFESTO_GATEWAY_COMPONENT="{{manifesto_gateway}}" \
      KUEUE_QUEUE="{{kueue_queue}}" \
      URL="{{url}}" \
      SWEEP_OUTDIR="$OUTDIR" \
      SWEEP_DURATION="{{duration}}" \
      SWEEP_KEEP_MODEL="{{keep_model}}" \
      bash -lc 'set -euo pipefail; set -a; [ -f /workspace/benchmark-sweep.env ] && . /workspace/benchmark-sweep.env; set +a; cd /workspace/agentx-mvp; rm -f /workspace/orchestrator-sweep.exit_code; : > /workspace/orchestrator-sweep.log; nohup bash -lc '"'"'set -a; [ -f /workspace/benchmark-sweep.env ] && . /workspace/benchmark-sweep.env; set +a; just sweep "$SWEEP_OUTDIR" "$SWEEP_DURATION" "$SWEEP_KEEP_MODEL" > /workspace/orchestrator-sweep.log 2>&1; code=$?; echo "$code" > /workspace/orchestrator-sweep.exit_code; rm -f /workspace/orchestrator-sweep.pid; exit "$code"'"'"' </dev/null >/dev/null 2>&1 & pid=$!; echo "$pid" > /workspace/orchestrator-sweep.pid; echo "Launched PID $pid"'
    echo "Sweep running detached: $OUTDIR"
    echo "Monitor: just orchestrator-logs"
    echo "Copy results: just orchestrator-results $OUTDIR"

orchestrator-logs:
    #!/usr/bin/env bash
    set -euo pipefail
    POD=$(kubectl get pod -n {{NAMESPACE}} -l app={{orchestrator_deploy}} -o jsonpath='{.items[0].metadata.name}')
    kubectl exec -n {{NAMESPACE}} "$POD" -- tail -n 150 -f /workspace/orchestrator-sweep.log

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

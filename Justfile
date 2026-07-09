set dotenv-load
set export

# AIPerf AgentX-MVP benchmark against the running llm-d optimized-baseline deployment.
#
# Usage:
#   just deploy            # apply the manifest and wait for the pod
#   just check             # confirm the runner can reach the model endpoint
#   just run               # run the full AgentX-MVP benchmark (default 1800s)
#   just run 16 900        # override concurrency / duration
#   just smoke             # fast plumbing test (~60s, marks result invalid)
#   just results           # copy artifacts out to ./results
#   just logs / just shell # inspect the runner
#   just clean             # delete the runner

# Let's take this from .env
# namespace := "llm-d-optimized-baseline"
NAMESPACE := env_var('NAMESPACE')
deploy    := "aiperf-agentx"
# model     := "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-FP8"
model     := "zai-org/GLM-5.2-FP8"
url       := "http://llm-d-inference-gateway-istio:80/v1"
# url       := "http://optimized-baseline-direct"
concurrency := "64"
duration    := "900"
sweep_concurrencies := "64 128 256"

pd_prefill := env_var('GLM_PD_PREFILL')
pd_decode  := env_var('GLM_PD_DECODE')
llm_d_root := env_var('LLM_D_ROOT')

default:
    @just --list

# Apply the manifest and wait for the runner pod to be ready.
deploy:
    kubectl apply -f agentx.yaml -n {{NAMESPACE}}
    kubectl rollout status deploy/{{deploy}} -n {{NAMESPACE}} --timeout=300s

# Sanity check: list models served through the llm-d router from inside the runner.
# (The slim image has no curl, so use python's urllib.)
check:
    kubectl exec -n {{NAMESPACE}} deploy/{{deploy}} -- \
      python -c "import urllib.request as u; print(u.urlopen('{{url}}/models', timeout=10).read().decode())"

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

# Run the AgentX-MVP benchmark. Args: [concurrency] [duration-seconds].
# Launches detached inside the pod and polls for completion to survive kubectl connection drops.
run concurrency=concurrency duration=duration:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    DEPLOY={{deploy}}
    POD=$(kubectl get pod -n "$NS" -l app=$DEPLOY -o jsonpath='{.items[0].metadata.name}')
    # Kill any existing benchmark before launching a new one
    kubectl exec -n "$NS" "$POD" -- bash -c '
      for f in /proc/[0-9]*/cmdline; do
        pid=${f#/proc/}; pid=${pid%/cmdline}
        tr "\0" " " < "$f" 2>/dev/null | grep -q "aiperf" && kill -9 "$pid" 2>/dev/null
      done
      rm -f /workspace/aiperf.pid /workspace/aiperf.exit_code
    ' 2>/dev/null || true
    # Write benchmark script into the pod, then launch fully detached
    SCRIPT="#!/bin/bash
    aiperf profile \
      --scenario inferencex-agentx-mvp \
      --url '{{url}}' \
      --model '{{model}}' \
      --max-context-length 128000 \
      --endpoint-type chat \
      --streaming \
      --use-server-token-count \
      --public-dataset semianalysis_cc_traces_weka_with_subagents \
      --concurrency {{concurrency}} \
      --benchmark-duration {{duration}} \
      --output-artifact-dir /workspace/artifacts \
      --ui simple \
      > /workspace/aiperf.log 2>&1
    echo \$? > /workspace/aiperf.exit_code
    rm -f /workspace/aiperf.pid"
    kubectl exec -n "$NS" "$POD" -- bash -c "echo '$SCRIPT' > /workspace/run_benchmark.sh && chmod +x /workspace/run_benchmark.sh"
    kubectl exec -n "$NS" "$POD" -- bash -c 'nohup bash /workspace/run_benchmark.sh </dev/null >/dev/null 2>&1 & echo $! > /workspace/aiperf.pid && echo "Launched PID $!"'
    # Poll for completion — pid file is removed when the benchmark finishes
    echo "Benchmark running detached (polling every 30s)..."
    while kubectl exec -n "$NS" "$POD" -- test -f /workspace/aiperf.pid 2>/dev/null; do
        kubectl exec -n "$NS" "$POD" -- tail -1 /workspace/aiperf.log 2>/dev/null || true
        sleep 30
    done
    echo "Benchmark process finished."
    EXIT_CODE=$(kubectl exec -n "$NS" "$POD" -- cat /workspace/aiperf.exit_code 2>/dev/null || echo "1")
    kubectl exec -n "$NS" "$POD" -- tail -20 /workspace/aiperf.log || true
    if [ "$EXIT_CODE" != "0" ]; then
        echo "ERROR: Benchmark exited with code $EXIT_CODE"
        exit 1
    fi

# Fast plumbing validation (~60s). Uses --unsafe-override so it runs below the
# scenario's 900s minimum; result is marked submission_valid: false.
smoke:
    kubectl exec -n {{NAMESPACE}} deploy/{{deploy}} -- \
      aiperf profile \
        --scenario inferencex-agentx-mvp \
        --unsafe-override \
        --url {{url}} \
        --model {{model}} \
        --max-context-length 128000 \
        --endpoint-type chat \
        --streaming \
        --use-server-token-count \
        --public-dataset semianalysis_cc_traces_weka_with_subagents \
        --concurrency {{concurrency}} \
        --benchmark-duration {{duration}} \
        --output-artifact-dir /workspace/artifacts \
        --ui simple

# Copy benchmark artifacts out of the runner to a local directory (default ./results).
results dest="./results":
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p "{{dest}}"
    POD=$(kubectl get pod -n {{NAMESPACE}} -l app={{deploy}} -o jsonpath='{.items[0].metadata.name}')
    # kubectl cp often exits non-zero due to a spurious tar stream error; retry individual files if needed
    kubectl cp {{NAMESPACE}}/${POD}:/workspace/artifacts "{{dest}}" 2>/dev/null || true
    # Verify critical files arrived; retry individual copies if kubectl cp dropped them
    if [ ! -f "{{dest}}/profile_export.jsonl" ]; then
        kubectl cp {{NAMESPACE}}/${POD}:/workspace/artifacts/profile_export.jsonl "{{dest}}/profile_export.jsonl" 2>/dev/null || true
    fi
    if [ ! -f "{{dest}}/profile_export_aiperf.json" ]; then
        kubectl cp {{NAMESPACE}}/${POD}:/workspace/artifacts/profile_export_aiperf.json "{{dest}}/profile_export_aiperf.json" 2>/dev/null || true
    fi
    if [ ! -f "{{dest}}/profile_export.jsonl" ]; then
        echo "ERROR: profile_export.jsonl not found after copy"
        exit 1
    fi

# Wait for all running requests to drain on prefill and decode pods.
drain:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    echo "Waiting for all requests to drain..."
    while true; do
        TOTAL=0
        for pod in $(kubectl get pods -n "$NS" -l llm-d.ai/role=prefill -o jsonpath='{.items[*].metadata.name}'); do
            for port in 8000 8001 8002 8003 8004 8005 8006 8007; do
                N=$(kubectl exec -n "$NS" "$pod" -c vllm -- \
                    curl -sf "http://localhost:${port}/metrics" 2>/dev/null \
                    | grep '^vllm:num_requests_running' | awk '{printf "%d", $2}') || N=0
                TOTAL=$((TOTAL + N))
            done
        done
        for pod in $(kubectl get pods -n "$NS" -l llm-d.ai/role=decode -o jsonpath='{.items[*].metadata.name}'); do
            N=$(kubectl exec -n "$NS" "$pod" -c vllm -- \
                curl -sf "http://localhost:8200/metrics" 2>/dev/null \
                | grep '^vllm:num_requests_running' | awk '{printf "%d", $2}') || N=0
            TOTAL=$((TOTAL + N))
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
    # Reset vLLM prefix cache (GPU + CPU tiers) via API
    for pod in $(kubectl get pods -n "$NS" -l llm-d.ai/role=prefill -o jsonpath='{.items[*].metadata.name}'); do
        echo "Resetting prefix cache on prefill $pod..."
        for port in 8000 8001 8002 8003 8004 8005 8006 8007; do
            kubectl exec -n "$NS" "$pod" -c vllm -- \
                curl -sf -X POST "http://localhost:${port}/reset_prefix_cache?reset_external=true" 2>/dev/null || true
        done
    done
    for pod in $(kubectl get pods -n "$NS" -l llm-d.ai/role=decode -o jsonpath='{.items[*].metadata.name}'); do
        echo "Resetting prefix cache on decode $pod..."
        kubectl exec -n "$NS" "$pod" -c vllm -- \
            curl -sf -X POST "http://localhost:8200/reset_prefix_cache?reset_external=true" 2>/dev/null || true
    done
    # Clear NVMe filesystem tier
    for pod in $(kubectl get pods -n "$NS" -l llm-d.ai/role -o jsonpath='{.items[*].metadata.name}'); do
        echo "Clearing NVMe KV cache on $pod..."
        kubectl exec -n "$NS" "$pod" -c vllm -- rm -rf /mnt/nvme-cache/* 2>/dev/null || true
    done
    echo "All prefix caches reset (GPU + CPU + NVMe)."

# Delete benchmark artifacts from the runner pod.
wipe:
    kubectl exec -n {{NAMESPACE}} deploy/{{deploy}} -- rm -rf /workspace/artifacts

logs:
    kubectl logs -n {{NAMESPACE}} deploy/{{deploy}} -f

shell:
    kubectl exec -it -n {{NAMESPACE}} deploy/{{deploy}} -- bash

clean:
    kubectl delete -f agentx.yaml --ignore-not-found

# Capture vllm version info from a running deployment into a directory.
vllm-version dest=".":
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    # Image tag
    kubectl get pod -n "$NS" -l llm-d.ai/role=prefill -o jsonpath='{.items[0].spec.containers[0].image}' > "{{dest}}/vllm_image.txt"
    echo "" >> "{{dest}}/vllm_image.txt"
    # vLLM version from prefill pod startup logs
    POD=$(kubectl get pod -n "$NS" -l llm-d.ai/role=prefill -o jsonpath='{.items[0].metadata.name}')
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
    mkdir -p "{{dest}}/logs"
    for pod in $(kubectl get pods -n "$NS" -l llm-d.ai/role -o jsonpath='{.items[*].metadata.name}'); do
        echo "  logs: $pod"
        kubectl logs -n "$NS" "$pod" --all-containers > "{{dest}}/logs/${pod}.log" 2>&1 || true
    done
    # EPP
    for pod in $(kubectl get pods -n "$NS" -l llm-d-router-gateway=wide-ep-lws-epp -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
        echo "  logs: $pod"
        kubectl logs -n "$NS" "$pod" --all-containers > "{{dest}}/logs/${pod}.log" 2>&1 || true
    done
    # Gateway
    for pod in $(kubectl get pods -n "$NS" -l gateway.networking.k8s.io/gateway-name -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
        echo "  logs: $pod"
        kubectl logs -n "$NS" "$pod" --all-containers > "{{dest}}/logs/${pod}.log" 2>&1 || true
    done
    # aiperf runner
    for pod in $(kubectl get pods -n "$NS" -l app={{deploy}} -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
        echo "  logs: $pod"
        kubectl logs -n "$NS" "$pod" --all-containers > "{{dest}}/logs/${pod}.log" 2>&1 || true
    done
    echo "Logs saved to {{dest}}/logs/"

# Export Grafana dashboards for result directories.
# Usage: just scrape-grafana results_p1w1_d1w2_c1 results_p1w1_d1w2_c4
scrape-grafana +dirs:
    python3 export_dashboard.py results {{dirs}}

# Scrape Grafana dashboards and generate interactivity chart.
# Reads namespace.txt from each result directory to find the right Grafana instance.
# Runs export_dashboard.py inside the aiperf pod via kubectl exec (no port-forward needed).
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
            POD=$(kubectl get pod -n "$NS" -l app={{deploy}} -o jsonpath='{.items[0].metadata.name}')
            kubectl cp export_dashboard.py "$NS/${POD}:/workspace/export_dashboard.py"
            SETUP_NAMESPACES="${SETUP_NAMESPACES}|${NS}|${POD}|"
        fi
        POD=$(echo "$SETUP_NAMESPACES" | grep -o "|${NS}|[^|]*|" | head -1 | cut -d'|' -f3)
        GRAFANA_URL="http://grafana.${NS}.svc.cluster.local:80"
        NAME=$(basename "$dir")
        echo "=== $NAME ($NS): scraping Grafana ==="
        TIMESTAMPS=$(python3 extract_timestamps.py "$dir")
        START=$(echo "$TIMESTAMPS" | head -1)
        END=$(echo "$TIMESTAMPS" | tail -1)
        echo "  Time range: $START → $END"
        kubectl exec -n "$NS" "$POD" -- python3 /workspace/export_dashboard.py \
            --grafana-url "$GRAFANA_URL" \
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
        PROM_POD=$(kubectl get pod -n "$NS" -l app.kubernetes.io/name=prometheus,app.kubernetes.io/component=server -o jsonpath='{.items[0].metadata.name}' 2>/dev/null) || continue
        echo "=== $NS: snapshotting Prometheus TSDB ==="
        SNAP_NAME=$(kubectl exec -n "$NS" "$PROM_POD" -c prometheus-server -- \
            wget -qO- --post-data= http://localhost:9090/api/v1/admin/tsdb/snapshot 2>/dev/null \
            | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['name'])" 2>/dev/null) || {
            echo "  WARNING: snapshot failed for $NS, skipping"
            continue
        }
        SNAP_DIR="$PARENT/prometheus_snapshot"
        mkdir -p "$SNAP_DIR"
        kubectl cp "$NS/${PROM_POD}:/data/snapshots/${SNAP_NAME}" "$SNAP_DIR" -c prometheus-server 2>/dev/null || {
            echo "  WARNING: snapshot copy failed for $NS"
            continue
        }
        kubectl exec -n "$NS" "$PROM_POD" -c prometheus-server -- rm -rf "/data/snapshots/${SNAP_NAME}" 2>/dev/null || true
        echo "  Saved to $SNAP_DIR"
    done
    python3 gen_interactivity_chart.py "$(dirname "{{outdir}}")" 2>/dev/null || true

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
    # Copy HF token secret from source namespace, or create a dummy if source is gone
    if kubectl get secret llm-d-hf-token -n {{NAMESPACE}} -o json 2>/dev/null \
      | python3 -c "import json,sys; d=json.load(sys.stdin); d['metadata']={'name':'llm-d-hf-token','namespace':'{{ns}}'}; print(json.dumps(d))" \
      | kubectl apply -f - 2>/dev/null; then
        true
    else
        kubectl create secret generic llm-d-hf-token --from-literal=HF_TOKEN=dummy -n {{ns}} --dry-run=client -o yaml | kubectl apply -f -
    fi
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
    # Upgrade router with wide-ep-lws + GLM-5.2 overrides (sets all 8 DP ports)
    # Use non-dev chart (the -dev chart was pruned from the registry)
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
    # Deploy prefill/decode LWS
    PREFILL_REPLICAS={{prefill_replicas}} PREFILL_SIZE={{prefill_size}} envsubst '${PREFILL_REPLICAS} ${PREFILL_SIZE}' < {{pd_prefill}} | kubectl apply -n {{NAMESPACE}} -f -
    DECODE_REPLICAS={{decode_replicas}} DECODE_SIZE={{decode_size}} envsubst '${DECODE_REPLICAS} ${DECODE_SIZE}' < {{pd_decode}} | kubectl apply -n {{NAMESPACE}} -f -
    # Deploy KV cache evictor for NVMe tier cleanup
    kubectl apply -n {{NAMESPACE}} -f "$ROOT/guides/wide-ep-lws/modelserver/gpu/vllm-glm-5.2/base/kv-cache-evictor.yaml"
    echo "Deployed PR{{prefill_replicas}} PW{{prefill_size}} DR{{decode_replicas}} DW{{decode_size}} — waiting for pods..."
    kubectl rollout status --watch statefulset/wide-ep-lws-nvidia-gpu-vllm-glm-5-2-prefill -n {{NAMESPACE}} --timeout=7200s &
    kubectl rollout status --watch statefulset/wide-ep-lws-nvidia-gpu-vllm-glm-5-2-decode -n {{NAMESPACE}} --timeout=7200s &
    wait
    # Clear stale mmap files and KV cache from previous runs
    for pod in $(kubectl get pods -n {{NAMESPACE}} -l llm-d.ai/role -o jsonpath='{.items[*].metadata.name}'); do
        kubectl exec -n {{NAMESPACE}} "$pod" -c vllm -- find /dev/shm -name 'vllm_offload*' -delete 2>/dev/null || true
    done
    just clear-kv-cache

# Tear down PD deployment.
stop-pd:
    kubectl delete lws wide-ep-lws-nvidia-gpu-vllm-glm-5-2-prefill wide-ep-lws-nvidia-gpu-vllm-glm-5-2-decode -n {{NAMESPACE}} --ignore-not-found

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
            echo "FAILED: concurrency=$C, skipping"
            FAILED="${FAILED} c${C}"
            just wipe 2>/dev/null || true
            continue
        fi
        just results "$RDIR"
        just dump-logs "$RDIR"
        for attempt in 1 2 3 4 5; do
            if just report "{{dest}}"; then break; fi
            echo "Dashboard scrape attempt $attempt failed for c${C}, retrying in 10s..."
            sleep 10
        done
        just wipe
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

seed_image := "quay.io/rh-ee-ecrncevi/benchmark-seed:amd64"
seed_deploy := "benchmark-seed"

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
    # Ensure helm repos are configured (survives pod restarts)
    kubectl exec -n "$NS" "$DEPLOY" -- helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null
    kubectl exec -n "$NS" "$DEPLOY" -- helm repo add grafana https://grafana.github.io/helm-charts 2>/dev/null
    kubectl exec -n "$NS" "$DEPLOY" -- helm repo update
    POD=$(kubectl get pod -n "$NS" -l app={{seed_deploy}} -o jsonpath='{.items[0].metadata.name}')
    echo "=== Syncing files to seed pod ==="
    kubectl exec -n "$NS" "$POD" -- mkdir -p /workspace/agentx-mvp
    # Copy agentx-mvp files (NOT .env — written fresh below)
    for f in Justfile agentx.yaml dashboard.json extract_timestamps.py export_dashboard.py gen_interactivity_chart.py overlay_dashboards.py; do
        [ -f "$f" ] && kubectl cp "$f" "$NS/${POD}:/workspace/agentx-mvp/$f"
    done
    kubectl exec -n "$NS" "$POD" -- mkdir -p /workspace/agentx-mvp/dashboards
    for f in dashboards/*.json; do
        kubectl cp "$f" "$NS/${POD}:/workspace/agentx-mvp/$f"
    done
    # Clone llm-d repo (or pull if already cloned)
    kubectl exec -n "$NS" "$POD" -- bash -c \
        'if [ -d /workspace/llm-d/.git ]; then cd /workspace/llm-d && git pull; else git clone --branch wip-glm https://github.com/elvircrn/llm-d.git /workspace/llm-d; fi'
    # Write clean .env (no KUBECONFIG — uses ServiceAccount auth)
    TMPENV=$(mktemp)
    trap "rm -f $TMPENV" EXIT
    printf 'NAMESPACE=%s\nGLM_PD_PREFILL=/workspace/llm-d/guides/wide-ep-lws/modelserver/gpu/vllm-glm-5.2/base/prefill.yaml\nGLM_PD_DECODE=/workspace/llm-d/guides/wide-ep-lws/modelserver/gpu/vllm-glm-5.2/base/decode.yaml\nLLM_D_ROOT=/workspace/llm-d\n' "{{NAMESPACE}}" > "$TMPENV"
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
    # Ensure helm repos are configured (survives pod restarts)
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
    # Clone llm-d repo (or pull if already cloned)
    kubectl exec -n "$NS" "$POD" -- bash -c \
        'if [ -d /workspace/llm-d/.git ]; then cd /workspace/llm-d && git pull; else git clone --branch wip-glm https://github.com/elvircrn/llm-d.git /workspace/llm-d; fi'
    TMPENV=$(mktemp)
    trap "rm -f $TMPENV" EXIT
    printf 'NAMESPACE=%s\nGLM_PD_PREFILL=/workspace/llm-d/guides/wide-ep-lws/modelserver/gpu/vllm-glm-5.2/base/prefill.yaml\nGLM_PD_DECODE=/workspace/llm-d/guides/wide-ep-lws/modelserver/gpu/vllm-glm-5.2/base/decode.yaml\nLLM_D_ROOT=/workspace/llm-d\n' "{{NAMESPACE}}" > "$TMPENV"
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
    # Copy each result directory individually to avoid kubectl cp dropping files
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

# Stop the running sweep, tear down all benchmark namespaces, and clean up the seed pod.
seed-stop:
    #!/usr/bin/env bash
    set -euo pipefail
    NS={{NAMESPACE}}
    # Kill the sweep process
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

# Delete the seed orchestrator pod and RBAC.
seed-clean:
    #!/usr/bin/env bash
    set -euo pipefail
    kubectl delete deploy {{seed_deploy}} -n {{NAMESPACE}} --ignore-not-found
    kubectl delete clusterrolebinding benchmark-orchestrator --ignore-not-found
    kubectl delete sa benchmark-orchestrator -n {{NAMESPACE}} --ignore-not-found
    echo "Seed pod cleaned up."
